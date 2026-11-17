#!/usr/bin/env node
/* Run any Workflow script against a fake runtime and write the files that
   Claude Code writes for a run (as observed with Claude Code 2.1.325):

     <root>/<slug>/<session>.jsonl                          launch, launch result, notification
     <root>/<slug>/<session>/subagents/workflows/<run>/journal.jsonl
     <root>/<slug>/<session>/subagents/workflows/<run>/agent-<id>.jsonl / .meta.json
     <root>/<slug>/<session>/workflows/<run>.json           the record, at the end

   The script really runs: its loops, branches and fan-outs decide which
   agents start. Each fake agent writes a few tool calls over time and returns
   a value made from its schema. The script line of every agent() call is
   written to a "truth" file, so a test can check where the viewer puts it.

   Usage:
     node tools/simulate_workflow.js --root DIR --script FILE [options]

   Options:
     --args JSON            the run's input (args)
     --cwd PATH             the project folder the session says it runs in
     --session ID           session id (default: a fixed demo id)
     --run ID               run id (default: wf_sim-<counter>)
     --step-ms N            time between two tool calls of an agent (default 400)
     --max-concurrent N     agents at one time (default 16)
     --stop-after N         stop after N agents started, without a record (a cut-off run)
     --fail REGEX           agents whose label matches return null
     --resume RUN           resume that run, as Claude Code does: same run id and folder;
                            an agent whose call is unchanged returns its earlier result and
                            writes nothing; the others run and are appended; a new record
                            replaces the old one at the end
     --workflows-dir DIR    where workflow('name') finds a child script (<name>.js or its meta name)
     --image-every N        every Nth agent first reads an image file (a PNG made here),
                            as an agent does when it looks at a screenshot
     --launch-by-name NAME  the session launches the workflow by name, not by script
     --truth FILE           write the agent -> script line truth here
     --unlabeled-label T    label for agent() calls without one: "prompt" (default: the first
                            60 characters of the prompt, as Claude Code 2.1.325 does) or "empty"

   Writes only under --root. This is a development tool; the viewer itself
   does not need Node. */
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");

function parseArgs(argv) {
  const o = { stepMs: 400, maxConcurrent: 16, session: "00000000-sim0-0000-0000-000000000001", unlabeled: "prompt" };
  for (let i = 2; i < argv.length; i++) {
    const k = argv[i], v = argv[i + 1];
    const take = () => { i++; return v; };
    if (k === "--root") o.root = take();
    else if (k === "--script") o.script = take();
    else if (k === "--args") o.args = JSON.parse(take());
    else if (k === "--cwd") o.cwd = take();
    else if (k === "--session") o.session = take();
    else if (k === "--run") o.run = take();
    else if (k === "--step-ms") o.stepMs = Number(take());
    else if (k === "--max-concurrent") o.maxConcurrent = Number(take());
    else if (k === "--stop-after") o.stopAfter = Number(take());
    else if (k === "--fail") o.fail = new RegExp(take());
    else if (k === "--resume") o.resume = take();
    else if (k === "--workflows-dir") o.workflowsDir = take();
    else if (k === "--image-every") o.imageEvery = Number(take());
    else if (k === "--launch-by-name") o.byName = take();
    else if (k === "--truth") o.truth = take();
    else if (k === "--unlabeled-label") o.unlabeled = take();
    else throw new Error("unknown option " + k);
  }
  if (!o.root || !o.script) throw new Error("--root and --script are needed");
  const home = require("os").homedir();
  const real = path.resolve(o.root);
  const claude = path.join(home, ".claude");
  if (real === claude || real.startsWith(claude + path.sep)) throw new Error("refusing to write under " + claude);
  return o;
}

const opts = parseArgs(process.argv);
const source = fs.readFileSync(opts.script, "utf8");
const cwd = opts.cwd || "/sim/" + path.basename(opts.script, ".js");
const slug = cwd.replace(/[^A-Za-z0-9]/g, "-");
const projectDir = path.join(opts.root, slug);
const sessionFile = path.join(projectDir, opts.session + ".jsonl");
const sessionDir = path.join(projectDir, opts.session);
fs.mkdirSync(sessionDir, { recursive: true });
let runCounter = 0;
try { runCounter = fs.readdirSync(path.join(sessionDir, "subagents", "workflows")).length; } catch (e) { /* first run */ }
const runId = opts.resume || opts.run || "wf_sim" + String(runCounter + 1).padStart(4, "0") + "-" + path.basename(opts.script, ".js").replace(/[^a-z0-9]/gi, "").slice(0, 8);
const crypto = require("crypto");
const launchSalt = crypto.createHash("sha256").update(runId + "|" + process.hrtime.bigint()).digest("hex");
const taskId = "w" + launchSalt.slice(0, 8);
const runDir = path.join(sessionDir, "subagents", "workflows", runId);
fs.mkdirSync(runDir, { recursive: true });
const journal = path.join(runDir, "journal.jsonl");
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = () => new Date().toISOString();
const append = (file, rec) => fs.appendFileSync(file, JSON.stringify(rec) + "\n");

// ------------------------------------------------------------ the session
const toolUseId = "toolu_sim_" + launchSalt.slice(0, 20);
// a resumed run: the results of the earlier run, by call key
const cache = {}, earlierProgress = {};
if (opts.resume) {
  try {
    fs.readFileSync(journal, "utf8").split("\n").filter(Boolean).forEach((line) => {
      const rec = JSON.parse(line);
      if (rec.type === "result" && rec.key) cache[rec.key] = rec.result;
    });
  } catch (e) { /* nothing to resume */ }
  try {
    const old = JSON.parse(fs.readFileSync(path.join(sessionDir, "workflows", runId + ".json"), "utf8"));
    (old.workflowProgress || []).forEach((p) => { if (p.agentId) earlierProgress[p.agentId] = p; });
  } catch (e) { /* the earlier run has no record */ }
}
const launchInput = opts.byName ? { name: opts.byName } : { script: source };
if (opts.args !== undefined) launchInput.args = opts.args;
append(sessionFile, { type: "assistant", timestamp: iso(), cwd, sessionId: opts.session,
  message: { role: "assistant", content: [{ type: "tool_use", id: toolUseId, name: "Workflow", input: launchInput }] } });

// ------------------------------------------------------------ a PNG
const zlib = require("zlib");
const CRC = (() => { const t = []; for (let n = 0; n < 256; n++) { let c = n; for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1; t.push(c >>> 0); } return t; })();
function crc32(buf) { let c = 0xffffffff; for (const b of buf) c = CRC[(c ^ b) & 0xff] ^ (c >>> 8); return (c ^ 0xffffffff) >>> 0; }
/* A small picture of a "page": a coloured bar over light rows. */
function makePng(seed) {
  const w = 320, h = 200, raw = Buffer.alloc((w * 3 + 1) * h);
  const hue = [[47, 111, 222], [23, 152, 74], [201, 58, 58], [183, 121, 31]][seed % 4];
  for (let y = 0; y < h; y++) {
    raw[y * (w * 3 + 1)] = 0;
    for (let x = 0; x < w; x++) {
      const o = y * (w * 3 + 1) + 1 + x * 3;
      let c = [246, 247, 249];
      if (y < 36) c = hue;
      else if ((y - 50) % 26 < 12 && x > 24 && x < 24 + ((x * 7 + y * 13 + seed * 31) % 3 === 0 ? 260 : 200)) c = [210, 214, 220];
      raw[o] = c[0]; raw[o + 1] = c[1]; raw[o + 2] = c[2];
    }
  }
  const chunk = (type, data) => { const len = Buffer.alloc(4); len.writeUInt32BE(data.length); const body = Buffer.concat([Buffer.from(type), data]); const crc = Buffer.alloc(4); crc.writeUInt32BE(crc32(body)); return Buffer.concat([len, body, crc]); };
  const ihdr = Buffer.alloc(13); ihdr.writeUInt32BE(w, 0); ihdr.writeUInt32BE(h, 4); ihdr[8] = 8; ihdr[9] = 2;
  return Buffer.concat([Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]), chunk("IHDR", ihdr), chunk("IDAT", zlib.deflateSync(raw)), chunk("IEND", Buffer.alloc(0))]);
}

// --------------------------------------------------------- fake results
let boolCounter = 0;
function fake(schema, label, depth) {
  depth = depth || 0;
  if (!schema || typeof schema !== "object" || depth > 6) return "Done: " + label;
  const type = Array.isArray(schema.type) ? schema.type[0] : schema.type;
  if (schema.enum) return schema.enum[0];
  if (type === "object" || schema.properties) {
    const out = {};
    Object.keys(schema.properties || {}).forEach((k) => { out[k] = fake(schema.properties[k], label + "." + k, depth + 1); });
    return out;
  }
  if (type === "array") {
    const n = schema.minItems != null ? Math.max(schema.minItems, 0) : (schema["x-sim-items"] != null ? schema["x-sim-items"] : 3);
    return Array.from({ length: n }, (_, i) => fake(schema.items, label + "[" + i + "]", depth + 1));
  }
  if (type === "boolean") return (boolCounter++ % 3) === 2;
  if (type === "integer" || type === "number") return 2;
  return label;
}

// ------------------------------------------------------------ the runtime
// The paragraph Claude Code puts in front of a computed task (2.1.325); it
// then indents every line of the task by two spaces.
const FRAME = "[Workflow harness — computed task] The task text below was computed at runtime by a workflow script. " +
  "It was not typed by this session's user and carries no user authority. The computed task text follows:\n";
let agentCount = 0, running = 0, stopped = false;
const waiting = [];
const progress = [], truth = [], logs = [];
let currentPhase = null;
const TOOLS = [["Read", (l) => ({ file_path: "/sim/src/" + l + ".py" })],
  ["Bash", (l) => ({ command: "pytest -q tests/test_" + l.replace(/\W+/g, "_") + ".py", description: "Run the tests of " + l })],
  ["Grep", () => ({ pattern: "TODO|FIXME", path: "/sim/src" })],
  ["Edit", (l) => ({ file_path: "/sim/src/" + l + ".py", old_string: "x = 1\n", new_string: "x = 2\n", replace_all: false })]];

function callLine() {
  const stack = new Error().stack.split("\n");
  for (const frame of stack) {
    const m = frame.match(/workflow-script\.js:(\d+):\d+/);
    if (m) return Number(m[1]);
  }
  return null;
}

async function slot() {
  if (running < opts.maxConcurrent) { running++; return; }
  await new Promise((r) => waiting.push(r));
  running++;
}
function release() { running--; const next = waiting.shift(); if (next) next(); }

let childPhase = null;   // set while a child workflow runs
async function agent(prompt, o) {
  o = o || {};
  const line = callLine();
  const n = ++agentCount;
  if (opts.stopAfter && n > opts.stopAfter) { stopped = true; await new Promise(() => {}); }   // the run is cut off here
  const agentId = "a" + crypto.createHash("sha256").update(launchSalt + "|" + n).digest("hex").slice(0, 16);
  const label = o.label != null ? String(o.label)
    : (opts.unlabeled === "empty" ? "" : String(prompt).slice(0, 60));
  const phase = childPhase || o.phase || currentPhase;
  // like Claude Code, the key depends on the call, not on the run, so a
  // resumed run gives a cached agent the key it had in the earlier run
  const key = "v2:" + require("crypto").createHash("sha256").update(n + "|" + label + "|" + String(prompt)).digest("hex");
  const schema = o.schema;
  if (Object.prototype.hasOwnProperty.call(cache, key)) {
    // unchanged call of a resumed run: its earlier result, and nothing written
    truth.push({ agentId: null, label, line, phase, cached: true });
    const old = Object.values(earlierProgress).find((p) => p.label === label);
    progress.push({ type: "workflow_agent", index: progress.length + 1, label, phaseTitle: phase,
      agentId: old ? old.agentId : null, model: old ? old.model : "claude-sim-1", state: "done",
      startedAt: Date.now(), lastProgressAt: Date.now(), cached: true });
    return cache[key];
  }
  const value = opts.fail && opts.fail.test(label) ? null : fake(schema, label);
  truth.push({ agentId, label, line: childPhase ? null : line, phase });
  await slot();
  const started = Date.now();
  append(journal, { type: "started", key, agentId, label, phase });
  fs.writeFileSync(path.join(runDir, "agent-" + agentId + ".meta.json"), JSON.stringify({
    agentType: o.agentType || "workflow-subagent", isBuiltIn: true, description: label, workflowPhase: phase, spawnDepth: 1, model: o.model || "sim" }));
  const file = path.join(runDir, "agent-" + agentId + ".jsonl");
  append(file, { type: "user", timestamp: iso(), cwd, agentId, sessionId: opts.session,
    message: { role: "user", content: FRAME + String(prompt).split("\n").map((l) => "  " + l).join("\n") } });
  if (opts.imageEvery && n % opts.imageEvery === 0) {
    // the agent looks at a screenshot: Claude Code keeps the image in the transcript
    const tid = "toolu_" + agentId + "_img";
    append(file, { type: "assistant", timestamp: iso(), agentId, message: { id: "msg_" + agentId + "_img", role: "assistant", model: "claude-sim-1",
      content: [{ type: "tool_use", id: tid, name: "Read", input: { file_path: "/sim/screens/" + (label || "page").replace(/[^\w.-]+/g, "_") + ".png" } }] } });
    await sleep(opts.stepMs / 2);
    append(file, { type: "user", timestamp: iso(), agentId, toolUseResult: { type: "image" }, message: { role: "user", content: [
      { type: "tool_result", tool_use_id: tid, content: [{ type: "image", source: { type: "base64", media_type: "image/png", data: makePng(n).toString("base64") } }] }] } });
  }
  const steps = 2 + (n % 4);
  for (let s = 0; s < steps; s++) {
    const [tool, input] = TOOLS[(n + s) % TOOLS.length];
    const tid = "toolu_" + agentId + "_" + s;
    append(file, { type: "assistant", timestamp: iso(), agentId, message: { id: "msg_" + agentId + "_" + s, role: "assistant", model: "claude-sim-1",
      usage: { input_tokens: 5, cache_creation_input_tokens: 400, cache_read_input_tokens: 4000 + 300 * s, output_tokens: 80 },
      content: s === 0 ? [{ type: "text", text: "I start on " + label + "." }] : [{ type: "tool_use", id: tid, name: tool, input: input(label || "x") }] } });
    if (s > 0) {
      await sleep(opts.stepMs / 2);
      append(file, { type: "user", timestamp: iso(), agentId, message: { role: "user", content: [{ type: "tool_result", tool_use_id: tid, content: "ok", is_error: false }] } });
    }
    await sleep(opts.stepMs / 2);
  }
  append(file, { type: "assistant", timestamp: iso(), agentId, message: { id: "msg_" + agentId + "_end", role: "assistant", model: "claude-sim-1",
    content: [{ type: "tool_use", id: "toolu_" + agentId + "_out", name: "StructuredOutput", input: value && typeof value === "object" ? value : { text: value } }] } });
  append(journal, { type: "result", key, agentId, result: value, toolsUsed: ["Read", "Bash", "StructuredOutput"] });
  progress.push({ type: "workflow_agent", index: progress.length + 1, label, phaseTitle: phase, agentId, model: "claude-sim-1", state: "done",
    queuedAt: started, startedAt: started, attempt: 1, toolCalls: steps, tokens: 5000 + 400 * steps, durationMs: Date.now() - started });
  release();
  return value;
}

async function parallel(thunks) {
  return Promise.all(thunks.map((t) => Promise.resolve().then(t).catch(() => null)));
}
async function pipeline(items, ...stages) {
  return Promise.all(items.map(async (item, i) => {
    let value = item;
    try { for (const stage of stages) value = await stage(value, item, i); return value; }
    catch (e) { return null; }
  }));
}
function phase(title) { currentPhase = title; }
function log(message) { logs.push(String(message)); }
/* workflow(nameOrRef, args): Claude Code 2.1.325 runs the child in the same
   run: its agents go to the same journal with the phase "▸ <child name>". */
async function workflow(ref, childArgs) {
  let file = null;
  if (ref && typeof ref === "object" && ref.scriptPath) file = ref.scriptPath;
  else if (typeof ref === "string" && opts.workflowsDir) {
    const short = ref.replace(/^.*:/, "");
    const candidates = fs.readdirSync(opts.workflowsDir).filter((f) => /\.m?js$/.test(f)).map((f) => path.join(opts.workflowsDir, f));
    file = candidates.find((f) => path.basename(f).replace(/\.m?js$/, "") === short) ||
      candidates.find((f) => (fs.readFileSync(f, "utf8").match(/name\s*:\s*['"`]([^'"`]+)/) || [])[1] === short) || null;
  }
  if (!file) throw new Error("child workflow not found: " + JSON.stringify(ref));
  const childSource = fs.readFileSync(file, "utf8");
  const childName = (childSource.match(/name\s*:\s*['"`]([^'"`]+)/) || [])[1] || path.basename(file, ".js");
  logs.push("\u25b8 running dynamic workflow " + childName);
  const body = childSource.replace(/^\s*export\s+const\s+meta\s*=/m, "const meta =");
  const ctx = vm.createContext(Object.assign({}, contextBase, { args: childArgs }));
  const before = childPhase;
  childPhase = "\u25b8 " + childName;
  try {
    return await new vm.Script("(async () => {\n" + body + "\n})()", { filename: "child-script.js", lineOffset: -1 }).runInContext(ctx);
  } finally {
    childPhase = before;
    logs.push("\u25b8 " + childName + " done");
  }
}

// -------------------------------------------------------------- run it
const body = source.replace(/^\s*export\s+const\s+meta\s*=/m, "const meta =");
const wrapped = "(async () => {\n" + body + "\n})()";
const contextBase = { agent, parallel, pipeline, phase, log, workflow,
  budget: { total: null, spent: () => 0, remaining: () => Infinity }, console, JSON, Math, Object, Array, Promise, Set, Map, String, Number, Boolean, Error };
const context = vm.createContext(Object.assign({}, contextBase, { args: opts.args }));
const started = Date.now();
if (!opts.resume || !fs.existsSync(journal)) fs.writeFileSync(journal, JSON.stringify({ type: "launched" }) + "\n");
append(sessionFile, { type: "user", timestamp: iso(), cwd, sessionId: opts.session, message: { role: "user", content: [{ type: "tool_result", tool_use_id: toolUseId,
  content: "Workflow launched in background. Task ID: " + taskId }] },
  toolUseResult: { status: "async_launched", taskId, taskType: "local_workflow", workflowName: opts.byName || (source.match(/name\s*:\s*['"`]([^'"`]+)/) || [])[1] || "sim",
                   runId, summary: "Simulated run of " + path.basename(opts.script), transcriptDir: runDir } });

function writeTruth() {
  if (opts.truth) fs.writeFileSync(opts.truth, JSON.stringify({ runId, cwd, slug, session: opts.session, agents: truth, logs }, null, 1));
}

const promise = new vm.Script(wrapped, { filename: "workflow-script.js", lineOffset: -1 }).runInContext(context);
const watchdog = setInterval(() => { if (stopped && running === 0) { writeTruth(); console.log("cut off", runId); process.exit(0); } writeTruth(); }, 200);
promise.then((result) => {
  clearInterval(watchdog);
  writeTruth();
  if (stopped) { console.log("cut off", runId); process.exit(0); }
  const meta = (source.match(/export\s+const\s+meta\s*=\s*({[\s\S]*?\n})/) || [])[1];
  let phases = null;
  try { phases = meta ? vm.runInNewContext("(" + meta + ")").phases || null : null; } catch (e) { phases = null; }
  fs.mkdirSync(path.join(sessionDir, "workflows"), { recursive: true });
  fs.writeFileSync(path.join(sessionDir, "workflows", runId + ".json"), JSON.stringify({
    runId, timestamp: iso(), taskId, script: source, args: opts.args, result, agentCount: progress.length, logs,
    durationMs: Date.now() - started, summary: "Simulated run of " + path.basename(opts.script),
    workflowName: opts.byName || (source.match(/name\s*:\s*['"`]([^'"`]+)/) || [])[1] || "sim", status: "completed", startTime: started,
    phases, workflowProgress: progress, totalTokens: progress.reduce((s, p) => s + (p.tokens || 0), 0),
    totalToolCalls: progress.reduce((s, p) => s + (p.toolCalls || 0), 0) }));
  append(sessionFile, { type: "queue-operation", operation: "enqueue", timestamp: iso(), sessionId: opts.session,
    content: "<task-notification>\n<task-id>" + taskId + "</task-id>\n<status>completed</status>\n<summary>Dynamic workflow completed</summary>\n</task-notification>" });
  console.log("finished", runId, progress.length, "agents");
  process.exit(0);
}).catch((e) => { clearInterval(watchdog); writeTruth(); console.error("script failed:", e && e.stack || e); process.exit(2); });
