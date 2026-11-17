#!/usr/bin/env python3
"""Write an invented Workflow run, step by step, into a folder of your choice.

Use it to see the live view without a real run:

    python3 tools/simulate_run.py /tmp/ww-demo &
    python3 workflow_watch.py --root /tmp/ww-demo

The run has a scan agent, three parallel reviewers, and a fix/verify loop of
two rounds. Agents write a tool call every two seconds. At the end the run
record is written, as Claude Code does. All data is invented.
The script refuses to write under ~/.claude.
"""

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SCRIPT = """export const meta = {
  name: 'demo-review',
  description: 'Invented demo: scan, review three files in parallel, then fix and verify in rounds',
  phases: [{ title: 'Scan' }, { title: 'Review' }, { title: 'Fix' }, { title: 'Verify' }],
}
const scan = await agent('List the files to review.', { label: 'scan', phase: 'Scan', schema: FILES })
const reviews = await parallel(scan.files.map(f => () =>
  agent(`Review ${f}.`, { label: `review:${f}`, phase: 'Review', schema: FINDINGS })))
for (let round = 1; round <= 2; round++) {
  const fix = await agent('Fix the findings.', { label: `fix:r${round}`, phase: 'Fix' })
  const verdict = await agent('Verify the fix.', { label: `verify:r${round}`, phase: 'Verify', schema: VERDICT })
  if (verdict.ok) break
}
return { reviews }
"""

TOOLS = [("Read", {"file_path": "src/app/{f}"}),
         ("Grep", {"pattern": "TODO", "path": "src"}),
         ("Bash", {"command": "pytest -q tests/test_{f}", "description": "Run the tests of {f}"}),
         ("Edit", {"file_path": "src/app/{f}", "old_string": "a", "new_string": "b"})]


def iso(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


class Sim:
    def __init__(self, root, speed):
        self.root = root
        self.speed = speed
        self.project = root / "-demo-project"
        self.session = "00000000-demo-0000-0000-000000000001"
        stamp = int(time.time()) % 100000
        self.run_id = "wf_demo%05d-sim" % stamp
        self.task_id = "wdemo%05d" % stamp
        self.session_dir = self.project / self.session
        self.run_dir = self.session_dir / "subagents" / "workflows" / self.run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.journal = self.run_dir / "journal.jsonl"
        self.start = time.time()
        self.n = 0
        self.progress = []

    def append(self, path, record):
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")

    def sleep(self, seconds):
        time.sleep(seconds / self.speed)

    def launch(self):
        session_file = self.project / (self.session + ".jsonl")
        now = time.time()
        self.append(session_file, {"type": "assistant", "timestamp": iso(now), "cwd": "/demo/project",
                                   "message": {"role": "assistant", "content": [
                                       {"type": "tool_use", "id": "toolu_demo%s" % self.task_id, "name": "Workflow",
                                        "input": {"script": SCRIPT, "args": {"target": "src/app"}}}]}})
        self.sleep(3)
        self.append(session_file, {"type": "user", "timestamp": iso(time.time()), "cwd": "/demo/project",
                                   "message": {"role": "user", "content": [
                                       {"type": "tool_result", "tool_use_id": "toolu_demo%s" % self.task_id,
                                        "content": "Workflow launched in background."}]},
                                   "toolUseResult": {"status": "async_launched", "taskId": self.task_id,
                                                     "workflowName": "demo-review", "runId": self.run_id,
                                                     "summary": "Invented demo run"}})
        self.append(self.journal, {"type": "launched"})
        self.start = time.time()

    def agent_start(self, label, phase, prompt):
        self.n += 1
        agent_id = "ademo%012d" % self.n
        self.append(self.journal, {"type": "started", "key": "v2:%s" % agent_id, "agentId": agent_id,
                                   "label": label, "phase": phase})
        (self.run_dir / ("agent-%s.meta.json" % agent_id)).write_text(json.dumps({
            "agentType": "workflow-subagent", "isBuiltIn": True, "description": label,
            "workflowPhase": phase, "spawnDepth": 1, "model": "demo-model"}))
        path = self.run_dir / ("agent-%s.jsonl" % agent_id)
        self.append(path, {"type": "user", "timestamp": iso(time.time()), "cwd": "/demo/project",
                           "message": {"role": "user", "content": "[Workflow harness — computed task] " + prompt}})
        return {"id": agent_id, "path": path, "label": label, "phase": phase, "start": time.time(), "step": 0,
                "tools": 0}

    def agent_step(self, agent, file_name="main.py"):
        name, tool_input = TOOLS[agent["step"] % len(TOOLS)]
        tool_input = {k: v.format(f=file_name) for k, v in tool_input.items()}
        tool_id = "toolu_%s_%d" % (agent["id"], agent["step"])
        msg = "msg_%s_%d" % (agent["id"], agent["step"])
        now = time.time()
        usage = {"input_tokens": 5, "cache_creation_input_tokens": 900, "cache_read_input_tokens": 9000 + 900 * agent["step"],
                 "output_tokens": 120}
        if agent["step"] % 2 == 0:
            self.append(agent["path"], {"type": "assistant", "timestamp": iso(now), "message": {
                "id": msg, "role": "assistant", "model": "demo-model", "usage": usage,
                "content": [{"type": "text", "text": "Step %d: I look at %s next." % (agent["step"] + 1, file_name)}]}})
        self.append(agent["path"], {"type": "assistant", "timestamp": iso(now), "message": {
            "id": msg, "role": "assistant", "model": "demo-model", "usage": usage,
            "content": [{"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}]}})
        self.sleep(1)
        error = name == "Bash" and agent["step"] == 2 and agent["label"].startswith("fix:r1")
        self.append(agent["path"], {"type": "user", "timestamp": iso(time.time()), "message": {
            "role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_id, "is_error": error,
                                         "content": "1 failed, 12 passed" if error else "ok (%s)" % name}]}})
        agent["step"] += 1
        agent["tools"] += 1

    def agent_finish(self, agent, value):
        self.append(self.journal, {"type": "result", "key": "v2:%s" % agent["id"], "agentId": agent["id"],
                                   "result": value, "toolsUsed": ["Read", "Grep", "Bash", "StructuredOutput"]})
        self.progress.append({"type": "workflow_agent", "index": len(self.progress) + 1, "label": agent["label"],
                              "phaseTitle": agent["phase"], "agentId": agent["id"], "model": "demo-model",
                              "state": "done", "startedAt": int(agent["start"] * 1000),
                              "durationMs": int((time.time() - agent["start"]) * 1000),
                              "toolCalls": agent["tools"], "tokens": 12000 + 900 * agent["tools"]})

    def run(self):
        self.launch()
        scan = self.agent_start("scan", "Scan", "List the files to review.")
        for _ in range(3):
            self.agent_step(scan)
            self.sleep(1)
        files = ["auth.py", "billing.py", "search.py"]
        self.agent_finish(scan, {"files": files})
        reviewers = [(self.agent_start("review:%s" % f, "Review", "Review %s." % f), f) for f in files]
        # three reviewers work at the same time; they need 3, 5 and 7 steps
        while not all("done" in agent for agent, _f in reviewers):
            for index, (agent, f) in enumerate(reviewers):
                if "done" not in agent and agent["step"] < 3 + index * 2:
                    self.agent_step(agent, f)
            self.sleep(1)
            for index, (agent, f) in enumerate(reviewers):
                if "done" not in agent and agent["step"] >= 3 + index * 2:
                    agent["done"] = True
                    self.agent_finish(agent, {"file": f, "findings": [
                        {"severity": "must_fix", "where": "%s:12" % f, "problem": "Missing permission check"},
                        {"severity": "note", "where": "%s:40" % f, "problem": "Long function"}]})
        for round_no in (1, 2):
            fix = self.agent_start("fix:r%d" % round_no, "Fix", "Fix the findings.")
            for _ in range(4):
                self.agent_step(fix)
                self.sleep(1.5)
            self.agent_finish(fix, "Fixed %d findings. Tests: %s" % (3, "12 passed" if round_no == 2 else "1 failed"))
            verify = self.agent_start("verify:r%d" % round_no, "Verify", "Verify the fix.")
            for _ in range(3):
                self.agent_step(verify)
                self.sleep(1)
            ok = round_no == 2
            self.agent_finish(verify, {"ok": ok, "summary": "All findings fixed." if ok else "One test still fails."})
            if ok:
                break
        record = {"runId": self.run_id, "timestamp": iso(time.time()), "taskId": self.task_id, "script": SCRIPT,
                  "args": {"target": "src/app"}, "result": {"reviews": 3, "approved": True}, "agentCount": self.n,
                  "logs": ["scan found 3 files", "round 1: one test still fails", "round 2: approved"],
                  "durationMs": int((time.time() - self.start) * 1000), "summary": "Invented demo run",
                  "workflowName": "demo-review", "status": "completed", "startTime": int(self.start * 1000),
                  "phases": [{"title": t} for t in ("Scan", "Review", "Fix", "Verify")],
                  "workflowProgress": self.progress, "totalTokens": sum(p["tokens"] for p in self.progress),
                  "totalToolCalls": sum(p["toolCalls"] for p in self.progress)}
        path = self.session_dir / "workflows" / (self.run_id + ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record))
        print("finished", self.run_id)


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    root = Path(argv[1]).expanduser().resolve()
    claude = (Path.home() / ".claude").resolve()
    if root == claude or claude in root.parents:
        print("Refusing to write under %s." % claude)
        return 1
    speed = float(argv[2]) if len(argv) > 2 else 1.0
    sim = Sim(root, speed)
    print("writing", sim.run_id, "into", root)
    sim.run()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
