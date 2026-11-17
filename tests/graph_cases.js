/* Run by test_graph.py with Node: node graph_cases.js <acorn.js> <graph.js>.
   Prints one JSON line: {"passed": n, "failed": [messages]}. */
"use strict";
globalThis.acorn = require(process.argv[2]);
const G = require(process.argv[3]);

const failed = [];
let passed = 0;
function check(name, cond, info) {
  if (cond) passed++;
  else failed.push(name + (info === undefined ? "" : ": " + JSON.stringify(info)));
}
function kinds(seq) { return seq.items.map((i) => i.k); }
function find(node, pred, out = []) {
  if (!node) return out;
  if (pred(node)) out.push(node);
  (node.items || []).forEach((i) => find(i, pred, out));
  (node.branches || []).forEach((b) => find(b.seq, pred, out));
  if (node.body) find(node.body, pred, out);
  return out;
}
function agentsOf(g) { return find(g.seq, (n) => n.k === "agent"); }
function layoutOk(name, g) {
  const L = G.layout(g);
  check(name + " layout has size", L.width > 0 && L.height > 0, [L.width, L.height]);
  check(name + " layout boxes are finite", L.boxes.every((b) => [b.x, b.y, b.w, b.h].every(Number.isFinite)));
  check(name + " layout edges are finite", L.edges.every((e) => e.pts.every((p) => p.every(Number.isFinite))));
  return L;
}

// 1. serial loop with a helper that builds the options (the user's real shape)
{
  const src = `export const meta = { name: 'build-stage', description: 'd', phases: [{ title: 'Build' }, { title: 'Review' }, { title: 'Fix' }] }
const { stage } = args
const opts = (label, phase, schema) => ({ label, phase, model: 'opus', schema })
let dev = await agent(\`build \${stage}\`, opts(\`dev:\${stage}\`, 'Build', {}))
for (let r = 1; r <= 3 && dev; r++) {
  const review = await agent('review', opts(\`review:\${stage}:r\${r}\`, 'Review', {}))
  if (!review) break
  if (review.ok) { break }
  const fix = await agent('fix', opts(\`fix:\${stage}:r\${r}\`, 'Fix', {}))
  if (fix) dev = fix
}
return { dev }`;
  const g = G.buildGraph(src);
  check("1 meta name", g.meta && g.meta.name === "build-stage", g.meta);
  check("1 meta phases", g.meta.phases.length === 3);
  check("1 top kinds", JSON.stringify(kinds(g.seq)) === JSON.stringify(["agent", "loop", "exit"]), kinds(g.seq));
  const ag = agentsOf(g);
  check("1 three agents", ag.length === 3, ag.map((a) => a.label));
  check("1 labels from helper", ag.map((a) => a.label).join("|") === "dev:${stage}|review:${stage}:r${r}|fix:${stage}:r${r}", ag.map((a) => a.label));
  check("1 phases from helper", ag.map((a) => a.phase).join("|") === "Build|Review|Fix");
  check("1 model from helper", ag[0].model === "opus");
  const loop = g.seq.items[1];
  check("1 loop title", loop.title === "loop: r <= 3 && dev", loop.title);
  const exits = find(loop.body, (n) => n.k === "exit").map((n) => n.text);
  check("1 two breaks", exits.filter((t) => t === "break").length === 2, exits);
  const runtime = [
    { agentId: "a", label: "dev:S1", phase: "Build", state: "done" },
    { agentId: "b", label: "review:S1:r1", phase: "Review", state: "done" },
    { agentId: "c", label: "fix:S1:r1", phase: "Fix", state: "done" },
    { agentId: "d", label: "review:S1:r2", phase: "Review", state: "running" },
  ];
  const m = G.matchAgents(g, runtime);
  check("1 all matched", m.unmatched.length === 0, m.unmatched);
  check("1 review twice", (m.byNode[ag[1].id] || []).map((a) => a.agentId).join() === "b,d");
  check("1 dev once", (m.byNode[ag[0].id] || []).length === 1);
  layoutOk("1", g);
}

// 2. fan-out with parallel(map) and a verify pipeline, no meta phases
{
  const src = `export const meta = { name: 'review', description: 'x' }
const files = await agent('list files', { label: 'scan', schema: {} })
const results = await parallel(files.list.map(f => () => agent('review ' + f, { label: \`review:\${f}\`, phase: 'Review' })))
const verified = await pipeline(results.filter(Boolean), r => agent('verify', { label: 'verify:' + r.file, phase: 'Verify' }), v => agent('judge', { label: \`judge:\${v.id}\` }))
return verified`;
  const g = G.buildGraph(src);
  check("2 kinds", JSON.stringify(kinds(g.seq)) === JSON.stringify(["agent", "each", "each", "exit"]), kinds(g.seq));
  check("2 parallel title", /^parallel: for each of files\.list/.test(g.seq.items[1].title), g.seq.items[1].title);
  check("2 pipeline title", /^pipeline over results\.filter/.test(g.seq.items[2].title), g.seq.items[2].title);
  const ag = agentsOf(g);
  check("2 four agents", ag.length === 4, ag.map((a) => a.label));
  check("2 plus label", ag[2].label === "verify:${r.file}", ag[2].label);
  const runtime = [{ agentId: "s", label: "scan" }]
    .concat(["a.py", "b.py", "c.py"].map((f, i) => ({ agentId: "r" + i, label: "review:" + f, phase: "Review", state: "done" })))
    .concat([{ agentId: "v0", label: "verify:a.py", phase: "Verify" }, { agentId: "j0", label: "judge:7" }]);
  const m = G.matchAgents(g, runtime);
  check("2 matched", m.unmatched.length === 0, m.unmatched);
  check("2 three reviews on one node", (m.byNode[ag[1].id] || []).length === 3);
  layoutOk("2", g);
  // each started review gets its own column
  const ex = G.expandFanouts(g, m);
  const cols = find(ex.graph.seq, (n) => n.k === "par" && n.bare);
  check("2 expanded into columns", cols.length === 1 && cols[0].branches.length === 3, cols.length);
  const clones = find(ex.graph.seq, (n) => n.k === "agent" && n.baseId === ag[1].id);
  check("2 three clones", clones.length === 3);
  check("2 clone instance", G.instancesFor(clones[2], m)[0].label === "review:c.py");
  check("2 signature", /:3/.test(ex.signature), ex.signature);
  const L2 = layoutOk("2x", ex.graph);
  check("2 expanded is wider", L2.width > G.layout(g).width);
  // a pipeline pairs its stages by the item, not by start order
  {
    const src2 = `const r = await pipeline(LENSES, l => agent('r', { label: \`review:\${l.key}\` }), (x, l) => agent('v', { label: \`verify:\${l.key}\` }))`;
    const g2 = G.buildGraph(src2);
    const a2 = agentsOf(g2);
    const rt = [{ agentId: "r1", label: "review:A" }, { agentId: "r2", label: "review:B" }, { agentId: "r3", label: "review:C" },
      { agentId: "v2", label: "verify:B" }, { agentId: "v3", label: "verify:C" }, { agentId: "v1", label: "verify:A" }];
    const m2 = G.matchAgents(g2, rt);
    const ex2 = G.expandFanouts(g2, m2);
    const verifyClones = find(ex2.graph.seq, (n) => n.k === "agent" && n.baseId === a2[1].id);
    const pairs = verifyClones.map((c) => G.instancesFor(c, m2)[0].label).join();
    check("2 pipeline pairs by item", pairs === "verify:A,verify:B,verify:C", pairs);
  }
  const many = runtime.concat(Array.from({ length: 30 }, (_, i) => ({ agentId: "m" + i, label: "review:m" + i, phase: "Review" })));
  const exMany = G.expandFanouts(g, G.matchAgents(g, many));
  check("2 more than the limit stays one node", find(exMany.graph.seq, (n) => n.k === "par" && n.bare).length === 0);
}

// 3. no phases, no labels, branches and Promise.all
{
  const src = `const a = await agent('first')
let b
if (a.big) {
  b = await agent('big path')
} else {
  b = await agent('small path')
}
const [x, y] = await Promise.all([agent('left'), agent('right')])
await workflow('child-flow', { a })`;
  const g = G.buildGraph(src);
  check("3 meta null", g.meta === null);
  check("3 kinds", JSON.stringify(kinds(g.seq)) === JSON.stringify(["agent", "branch", "par", "sub"]), kinds(g.seq));
  const br = g.seq.items[1];
  check("3 branch labels", br.branches.map((b) => b.label).join() === "yes,no");
  check("3 par branches", g.seq.items[2].branches.length === 2);
  const ag = agentsOf(g);
  check("3 unlabeled agents have no regex", ag.every((a) => a.labelRe === null));
  const m = G.matchAgents(g, [{ agentId: "z", label: "agent 1" }]);
  check("3 unlabeled runtime agent goes somewhere", m.unmatched.length === 0);
  layoutOk("3", g);
}

// 4. a helper function that calls agent(), Array.from fan-out, while loop, switch
{
  const src = `async function check(name) {
  const r = await agent('check ' + name, { label: \`check:\${name}\`, phase: 'Check' })
  return r
}
await check('api')
const votes = await parallel(Array.from({ length: 3 }, (_, i) => () => agent('vote', { label: 'vote' })))
let n = 0
while (n < 5) { n++; await agent('poll', { label: 'poll' }) }
switch (args.mode) {
  case 'a': await agent('mode a', { label: 'mode-a' }); break
  default: await agent('mode b', { label: 'mode-b' })
}`;
  const g = G.buildGraph(src);
  check("4 kinds", JSON.stringify(kinds(g.seq)) === JSON.stringify(["group", "each", "loop", "branch"]), kinds(g.seq));
  check("4 group title", g.seq.items[0].title === "check()");
  check("4 repeat title", /^parallel: repeat/.test(g.seq.items[1].title), g.seq.items[1].title);
  check("4 switch cases", !!g.seq.items[3] && g.seq.items[3].branches.length === 2);
  const m = G.matchAgents(g, [{ agentId: "1", label: "check:api", phase: "Check" }, { agentId: "2", label: "vote" },
    { agentId: "3", label: "vote" }, { agentId: "4", label: "poll" }, { agentId: "5", label: "mode-b" }, { agentId: "6", label: "stranger" }]);
  check("4 one unmatched", m.unmatched.length === 1 && m.unmatched[0].label === "stranger", m.unmatched);
  layoutOk("4", g);
}

// 5. bad script: buildGraph throws, runtimeGraph still works
{
  let threw = false;
  try { G.buildGraph("const = = ;"); } catch (e) { threw = true; }
  check("5 bad script throws", threw);
  const g = G.runtimeGraph([{ agentId: "a", label: "x", phase: "P1" }, { agentId: "b", label: "y", phase: "P2" },
    { agentId: "c", label: "z" }], [{ title: "P2" }, { title: "P1" }]);
  check("5 runtime phases in meta order", g.seq.items.filter((i) => i.k === "phase").map((i) => i.title).join() === "P2,P1");
  const m = G.matchAgents(g, [{ agentId: "a", label: "x" }, { agentId: "b", label: "y" }, { agentId: "c", label: "z" }]);
  check("5 runtime all matched exactly", m.unmatched.length === 0 && Object.keys(m.byNode).length === 3);
  layoutOk("5", g);
}

// 6. empty script
{
  const g = G.buildGraph("");
  check("6 empty", g.seq.items.length === 0);
  layoutOk("6", g);
}

console.log(JSON.stringify({ passed, failed }));
