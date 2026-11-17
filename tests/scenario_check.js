/* Check how the page's graph code places the agents of one simulated run.
   Run by test_scenarios.py:
     node scenario_check.js <acorn.js> <graph.js> <input.json>
   input.json: {script, truth: {agents: [{agentId, line}]}, agents: [...], prompts: {...}, phases}
   Prints one JSON report. */
"use strict";
const fs = require("fs");
globalThis.acorn = require(process.argv[2]);
const G = require(process.argv[3]);
const input = JSON.parse(fs.readFileSync(process.argv[4], "utf8"));

const report = { wrongLine: [], unmatched: [], notShown: [], shownTwice: [], loops: [], fanouts: [], layout: null, error: null };
try {
  const graph = input.script ? G.buildGraph(input.script) : G.runtimeGraph(input.agents, input.phases);
  const truth = {};
  (input.truth.agents || []).forEach((t) => { truth[t.agentId] = t; });
  const m = G.matchAgents(graph, input.agents, input.prompts || {});
  report.unmatched = m.unmatched.map((a) => a.label);
  if (input.script) {
    input.agents.forEach((a) => {
      const node = m.nodeOf[a.agentId], t = truth[a.agentId];
      if (node && t && t.line !== null && node.line !== t.line) report.wrongLine.push({ label: a.label, placedAt: node.line, calledAt: t.line });
    });
  }
  const shown = G.expandFanouts(graph, m);
  const seenIn = {};
  (function walk(n, path) {
    if (!n) return;
    if (n.k === "agent") G.instancesFor(n, m).forEach((a) => { (seenIn[a.agentId] = seenIn[a.agentId] || []).push(n.id); });
    if (n.k === "loop") { const r = G.loopRounds(n, m); report.loops.push({ title: n.title, rounds: r ? r.rounds : null }); }
    if (n.k === "each") report.fanouts.push({ title: n.title, columns: n.expanded || 0, id: n.id });
    (n.items || []).forEach((c) => walk(c));
    (n.branches || []).forEach((b) => walk(b.seq));
    if (n.body) walk(n.body);
  })(shown.graph.seq);
  input.agents.forEach((a) => {
    const where = seenIn[a.agentId] || [];
    if (!where.length && m.nodeOf[a.agentId]) report.notShown.push(a.label);
    if (where.length > 1) report.shownTwice.push({ label: a.label, nodes: where });
  });
  // agent boxes drawn from the same script line (copies of a helper)
  const byLine = {};
  graph.agents.forEach((n) => { (byLine[n.line] = byLine[n.line] || []).push(n); });
  report.helperCopies = [];
  Object.keys(byLine).forEach((line) => {
    if (byLine[line].length > 1) byLine[line].forEach((n) => report.helperCopies.push((m.byNode[n.id] || []).length));
  });
  const L = G.layout(shown.graph);
  report.layout = { width: L.width, height: L.height, boxes: L.boxes.length,
    finite: L.boxes.every((b) => [b.x, b.y, b.w, b.h].every(Number.isFinite)) && L.edges.every((e) => e.pts.every((p) => p.every(Number.isFinite))) };
  const ids = L.boxes.filter((b) => b.node && b.node.id).map((b) => b.node.id);
  report.duplicateIds = ids.filter((id, i) => ids.indexOf(id) !== i);
} catch (e) {
  report.error = String(e && e.stack || e);
}
console.log(JSON.stringify(report));
