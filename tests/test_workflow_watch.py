import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import workflow_watch as ww  # noqa: E402
from fixtures import (ProjectBuilder, append_jsonl, now_ms, result, set_mtime,  # noqa: E402
                      started, transcript)

HOUR = 3_600_000
SCRIPT = """export const meta = { name: 'demo-flow', description: 'Two steps', phases: [{ title: 'Build' }] }
phase('Build')
const a = await agent('build it', { label: 'build', phase: 'Build' })
return a
"""


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.p = ProjectBuilder(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def store(self, **kw):
        store = ww.Store(self.root, **kw)
        store.refresh(force=True)
        return store

    def runs(self, store):
        store.last_refresh = None
        return {r["id"]: r for r in store.list_runs()["runs"]}

    def detail(self, store, run_id):
        store.last_refresh = None
        return store.run_detail(run_id)


class FinishedRun(StoreCase):
    def test_a_finished_run_shows_its_record(self):
        start = now_ms() - 2 * HOUR
        self.p.session("s1", [])
        self.p.journal("s1", "wf_done-001", [{"type": "launched"}, started("a1", "build", "Build"),
                                             result("a1", {"ok": True, "files": ["a.py"]})])
        self.p.agent("s1", "wf_done-001", "a1", "build", "Build", transcript(start))
        self.p.record("s1", "wf_done-001", {
            "runId": "wf_done-001", "workflowName": "demo-flow", "summary": "Two steps", "status": "completed",
            "startTime": start, "durationMs": 60_000, "agentCount": 1, "totalTokens": 1234,
            "totalToolCalls": 2, "script": SCRIPT, "args": {"stage": "S1"}, "result": {"done": 1},
            "logs": ["round 1: fine"], "phases": [{"title": "Build"}],
            "workflowProgress": [{"type": "workflow_agent", "agentId": "a1", "label": "build", "state": "done",
                                  "phaseTitle": "Build", "tokens": 1234, "toolCalls": 2, "startedAt": start,
                                  "durationMs": 30_000, "model": "claude-test-1"}]})
        store = self.store()
        run = self.runs(store)["wf_done-001"]
        self.assertEqual(run["status"], "finished")
        self.assertEqual(run["name"], "demo-flow")
        self.assertEqual(run["agentsDone"], 1)
        self.assertEqual(run["tokens"], 1234)
        self.assertTrue(run["tokensExact"])
        self.assertEqual(run["end"], start + 60_000)
        d = self.detail(store, "wf_done-001")
        self.assertEqual(d["logs"], ["round 1: fine"])
        self.assertEqual(d["result"], {"done": 1})
        agent = d["agents"][0]
        self.assertEqual(agent["state"], "done")
        self.assertEqual(agent["end"], start + 30_000)
        static = store.run_static("wf_done-001")
        self.assertEqual(static["script"], SCRIPT)
        self.assertEqual(static["scriptOrigin"], "record")
        self.assertEqual(static["args"], {"stage": "S1"})
        full = store.agent_detail("wf_done-001", "a1")
        self.assertEqual(full["result"], {"ok": True, "files": ["a.py"]})
        self.assertIn("Do the task.", full["prompt"])
        tools = [s for s in full["steps"] if s["kind"] == "tool"]
        self.assertEqual(len(tools), 2)
        self.assertEqual(tools[0]["summary"], "Run test step 0")
        self.assertEqual(tools[0]["result"], "ok 0")


class RunningRun(StoreCase):
    def build_running(self):
        start = now_ms() - 120_000
        self.p.session("s1", [
            self.p.launch_entry("toolu_1", start - 1000, {"script": SCRIPT, "args": {"stage": "S2"}}),
            self.p.launch_result_entry("toolu_1", start - 500, "wf_live-002", "wtask1", "demo-flow"),
        ])
        self.p.journal("s1", "wf_live-002", [{"type": "launched"}, started("a1", "build", "Build")])
        path = self.p.agent("s1", "wf_live-002", "a1", "build", "Build", transcript(start, steps=3))
        return start, path

    def test_a_running_run_shows_the_working_agent(self):
        start, _path = self.build_running()
        store = self.store()
        run = self.runs(store)["wf_live-002"]
        self.assertEqual(run["status"], "running")
        self.assertEqual(run["name"], "demo-flow")
        self.assertEqual(run["taskId"], "wtask1")
        self.assertEqual(run["agentsRunning"], 1)
        self.assertEqual(run["cwd"], "/work/demo")
        d = self.detail(store, "wf_live-002")
        agent = d["agents"][0]
        self.assertEqual(agent["state"], "running")
        self.assertEqual(agent["lastTool"]["name"], "Bash")
        self.assertEqual(agent["lastTool"]["summary"], "Run test step 2")
        self.assertEqual(agent["toolCalls"], 3)
        self.assertFalse(agent["quiet"])
        static = store.run_static("wf_live-002")
        self.assertEqual(static["script"], SCRIPT)
        self.assertEqual(static["scriptOrigin"], "launch")
        self.assertEqual(static["args"], {"stage": "S2"})

    def test_new_lines_show_up_and_only_new_bytes_are_read(self):
        start, path = self.build_running()
        store = self.store()
        self.detail(store, "wf_live-002")
        run = store.runs["wf_live-002"]
        tail = run.transcripts["a1"].tail
        before = tail.bytes_read
        size_before = path.stat().st_size
        extra = transcript(start + 60_000, steps=1)[1:]
        extra[0]["message"]["id"] = "msg_new"
        extra[0]["message"]["content"][0]["id"] = "tu_new"
        extra[0]["message"]["content"][0]["input"]["description"] = "Second pass"
        extra[1]["message"]["content"][0]["tool_use_id"] = "tu_new"
        append_jsonl(path, extra)
        appended = path.stat().st_size - size_before
        d = self.detail(store, "wf_live-002")
        self.assertEqual(d["agents"][0]["lastTool"]["summary"], "Second pass")
        self.assertEqual(tail.bytes_read - before, appended)
        # the journal grows: the agent returns
        append_jsonl(path.parent / "journal.jsonl", [result("a1", "all done")])
        d = self.detail(store, "wf_live-002")
        self.assertEqual(d["agents"][0]["state"], "done")
        self.assertEqual(d["agents"][0]["resultPreview"], "all done")

    def test_an_unchanged_run_answers_unchanged(self):
        self.build_running()
        store = self.store()
        first = self.detail(store, "wf_live-002")
        store.last_refresh = None
        again = store.run_detail("wf_live-002", first["version"])
        self.assertTrue(again.get("unchanged"))

    def test_a_quiet_agent_is_marked_but_still_running(self):
        start, path = self.build_running()
        old = now_ms() - 20 * 60_000
        for f in path.parent.iterdir():
            set_mtime(f, old)
        store = self.store(stale_minutes=10)
        # 20 minutes of silence with a 10 minute limit: probably stopped
        self.assertEqual(self.runs(store)["wf_live-002"]["status"], "probably stopped")
        store = self.store(stale_minutes=60)
        self.assertEqual(self.runs(store)["wf_live-002"]["status"], "running")


class CutOffRun(StoreCase):
    def test_a_run_without_record_and_long_silence_is_probably_stopped(self):
        start = now_ms() - 5 * HOUR
        self.p.session("s1", [])
        journal = self.p.journal("s1", "wf_cut-003", [{"type": "launched"}, started("a1", "dev", "Build")])
        agent = self.p.agent("s1", "wf_cut-003", "a1", "dev", "Build", transcript(start))
        for f in (journal, agent, agent.parent / "agent-a1.meta.json"):
            set_mtime(f, start + 10_000)
        run = self.runs(self.store())["wf_cut-003"]
        self.assertEqual(run["status"], "probably stopped")
        self.assertIn("No file of this run changed", run["statusNote"])
        self.assertEqual(run["agentsDone"], 0)
        self.assertEqual(run["agentsRunning"], 0)

    def test_a_stopped_report_wins_only_without_later_activity(self):
        start = now_ms() - 3 * HOUR
        entries = [
            self.p.launch_entry("toolu_9", start, {"scriptPath": "/nonexistent/x.js"}),
            self.p.launch_result_entry("toolu_9", start + 100, "wf_stop-004", "wtask9", "demo-flow"),
            self.p.notification_entry(start + HOUR, "wtask9", "stopped",
                                      "Background workflow didn't finish before the previous session ended"),
        ]
        self.p.session("s1", entries)
        journal = self.p.journal("s1", "wf_stop-004", [{"type": "launched"}, started("a1", "dev", "Build")])
        agent = self.p.agent("s1", "wf_stop-004", "a1", "dev", "Build", transcript(start))
        for f in journal.parent.iterdir():
            set_mtime(f, start + HOUR - 5000)
        run = self.runs(self.store())["wf_stop-004"]
        self.assertEqual(run["status"], "stopped")
        # files change after the report: the run is still going
        for f in journal.parent.iterdir():
            set_mtime(f, now_ms())
        run = self.runs(self.store())["wf_stop-004"]
        self.assertEqual(run["status"], "running")
        self.assertIn("kept changing", run["statusNote"])
        del agent


class ShapesAndDamage(StoreCase):
    def test_parallel_agents_keep_their_start_order(self):
        start = now_ms() - 60_000
        self.p.session("s1", [])
        self.p.journal("s1", "wf_par-005", [
            {"type": "launched"}, started("a1", "review:api", "Review"), started("a2", "review:db", "Review"),
            started("a3", "review:ui", "Review"), result("a2", {"findings": []}), result("a1", {"findings": [1]}),
        ])
        for aid, label in (("a1", "review:api"), ("a2", "review:db"), ("a3", "review:ui")):
            self.p.agent("s1", "wf_par-005", aid, label, "Review", transcript(start))
        d = self.detail(self.store(), "wf_par-005")
        self.assertEqual([a["label"] for a in d["agents"]], ["review:api", "review:db", "review:ui"])
        self.assertEqual([a["state"] for a in d["agents"]], ["done", "done", "running"])
        self.assertEqual(d["run"]["agentsDone"], 2)
        self.assertEqual(d["run"]["agentsRunning"], 1)

    def test_text_result_and_null_result(self):
        start = now_ms() - 60_000
        self.p.session("s1", [])
        self.p.journal("s1", "wf_txt-006", [
            {"type": "launched"}, started("a1", "write", "Write"), result("a1", "Here is the report.\nLine 2"),
            started("a2", "check", "Write"), result("a2", None)])
        self.p.agent("s1", "wf_txt-006", "a1", "write", "Write", transcript(start, finish_text="Here is the report."))
        self.p.agent("s1", "wf_txt-006", "a2", "check", "Write", transcript(start))
        store = self.store()
        d = self.detail(store, "wf_txt-006")
        self.assertEqual(d["agents"][0]["resultPreview"], "Here is the report. Line 2")
        self.assertEqual(d["agents"][1]["state"], "failed")
        full = store.agent_detail("wf_txt-006", "a1")
        self.assertEqual(full["result"], "Here is the report.\nLine 2")
        texts = [s for s in full["steps"] if s["kind"] == "text"]
        self.assertEqual(texts[-1]["text"], "Here is the report.")

    def test_half_written_last_line_waits_for_its_end(self):
        start = now_ms() - 60_000
        self.p.session("s1", [])
        half = json.dumps(result("a1", {"ok": True}))
        journal = self.p.journal("s1", "wf_half-007", [{"type": "launched"}, started("a1", "dev", "Build")],
                                 tail=half[:25])
        self.p.agent("s1", "wf_half-007", "a1", "dev", "Build", transcript(start))
        store = self.store()
        d = self.detail(store, "wf_half-007")
        self.assertEqual(d["agents"][0]["state"], "running")
        self.assertEqual(store.runs["wf_half-007"].journal.bad_lines, 0)
        append_jsonl(journal, [], raw=half[25:] + "\n")
        d = self.detail(store, "wf_half-007")
        self.assertEqual(d["agents"][0]["state"], "done")

    def test_unknown_types_bad_lines_and_missing_meta_are_noted(self):
        start = now_ms() - 60_000
        self.p.session("s1", [])
        journal = self.p.journal("s1", "wf_odd-008", [
            {"type": "launched"}, {"type": "teleported", "agentId": "zz"}, started("a1", "dev", "Build")],
            tail="this is not json\n{\"type\": \"started\", \"agentId\": \"a2\", \"label\": \"x\"}\n")
        self.p.agent("s1", "wf_odd-008", "a1", "dev", "Build", transcript(start), meta=False)
        transcript_path = journal.parent / "agent-a1.jsonl"
        append_jsonl(transcript_path, [{"type": "brand-new-entry", "weird": [1, 2]}], raw="{broken\n")
        d = self.detail(self.store(), "wf_odd-008")
        notes = " ".join(d["notes"])
        self.assertIn("unknown type 'teleported'", notes)
        self.assertIn("1 journal line(s) could not be read", notes)
        self.assertIn("No meta file for: dev", notes)
        self.assertIn("1 agent transcript line(s) could not be read", notes)
        self.assertEqual(len(d["agents"]), 2)

    def test_a_result_without_start_is_marked_cached(self):
        self.p.session("s1", [])
        self.p.journal("s1", "wf_res-009", [{"type": "launched"}, result("a1", {"x": 1})])
        d = self.detail(self.store(), "wf_res-009")
        self.assertTrue(d["agents"][0]["cached"])
        self.assertEqual(d["agents"][0]["state"], "done")

    def test_empty_and_missing_roots_do_not_crash(self):
        store = ww.Store(self.root / "nothing-here")
        data = store.list_runs()
        self.assertEqual(data["runs"], [])
        self.assertTrue(data["notes"])
        (self.root / "empty-project").mkdir()
        (self.root / "empty-project" / "s.jsonl").write_text("")
        self.assertEqual(ww.Store(self.root).list_runs()["runs"], [])

    def test_missing_files_after_discovery(self):
        start = now_ms() - 60_000
        self.p.session("s1", [])
        journal = self.p.journal("s1", "wf_gone-010", [{"type": "launched"}, started("a1", "dev", "Build")])
        self.p.agent("s1", "wf_gone-010", "a1", "dev", "Build", transcript(start))
        store = self.store()
        self.detail(store, "wf_gone-010")
        for f in journal.parent.iterdir():
            f.unlink()
        d = self.detail(store, "wf_gone-010")
        self.assertEqual(len(d["agents"]), 1)

    def test_a_workflow_call_waiting_for_approval_is_listed(self):
        when = now_ms() - 5_000
        self.p.session("s1", [self.p.launch_entry("toolu_p", when, {"script": SCRIPT})])
        store = self.store()
        runs = self.runs(store)
        self.assertIn("pending-toolu_p", runs)
        self.assertEqual(runs["pending-toolu_p"]["status"], "awaiting launch")
        self.assertEqual(runs["pending-toolu_p"]["name"], "demo-flow")
        self.assertEqual(store.run_static("pending-toolu_p")["script"], SCRIPT)
        # once launched, the pending entry points at the run
        append_jsonl(self.p.project / "s1.jsonl",
                     [self.p.launch_result_entry("toolu_p", when + 1000, "wf_new-011", "wt", "demo-flow")])
        store.last_refresh = None
        moved = store.run_detail("pending-toolu_p")
        self.assertEqual(moved["moved"], "wf_new-011")

    def test_a_rejected_workflow_call_is_not_listed(self):
        when = now_ms() - 5_000
        entry = self.p.launch_entry("toolu_r", when, {"script": SCRIPT})
        rejected = {"type": "user", "timestamp": entry["timestamp"], "toolUseResult": "User rejected tool use",
                    "message": {"role": "user", "content": [
                        {"type": "tool_result", "tool_use_id": "toolu_r", "is_error": True, "content": "rejected"}]}}
        self.p.session("s1", [entry, rejected])
        self.assertEqual(self.runs(self.store()), {})

    def test_a_replaced_file_is_read_again(self):
        start = now_ms() - 60_000
        self.p.session("s1", [])
        journal = self.p.journal("s1", "wf_rep-012", [{"type": "launched"}, started("a1", "dev", "Build"),
                                                      started("a2", "dev2", "Build")])
        store = self.store()
        self.assertEqual(len(self.detail(store, "wf_rep-012")["agents"]), 2)
        journal.unlink()
        self.p.journal("s1", "wf_rep-012", [{"type": "launched"}, started("a9", "other", "Build")])
        agents = self.detail(store, "wf_rep-012")["agents"]
        self.assertIn("other", [a["label"] for a in agents])
        del start


class ConfigRoot(unittest.TestCase):
    def test_claude_config_dir_moves_the_root(self):
        old = os.environ.get("CLAUDE_CONFIG_DIR")
        try:
            os.environ["CLAUDE_CONFIG_DIR"] = "/somewhere/else"
            self.assertEqual(ww.default_root(), Path("/somewhere/else/projects"))
            del os.environ["CLAUDE_CONFIG_DIR"]
            self.assertEqual(ww.default_root(), Path.home() / ".claude" / "projects")
        finally:
            if old is not None:
                os.environ["CLAUDE_CONFIG_DIR"] = old


class Http(StoreCase):
    def setUp(self):
        super().setUp()
        start = now_ms() - 60_000
        self.p.session("s1", [])
        self.p.journal("s1", "wf_http-013", [{"type": "launched"}, started("a1", "dev", "Build")])
        self.p.agent("s1", "wf_http-013", "a1", "<img src=x onerror=alert(1)>", "Build",
                     transcript(start, prompt="<script>alert(1)</script>"))
        self.server = ww.make_server(ww.Store(self.root), 0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def get(self, path, host=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", path, headers={"Host": host or "127.0.0.1:%d" % self.port})
        response = conn.getresponse()
        body = response.read()
        conn.close()
        return response, body

    def test_page_and_assets(self):
        response, body = self.get("/")
        self.assertEqual(response.status, 200)
        self.assertIn(b"<title>Workflow Watch</title>", body)
        self.assertIn("script-src 'self'", response.getheader("Content-Security-Policy"))
        for asset, marker in (("/app.js", b"renderValue"), ("/graph.js", b"buildGraph"),
                              ("/acorn.js", b"acorn"), ("/app.css", b"--run")):
            response, body = self.get(asset)
            self.assertEqual(response.status, 200, asset)
            self.assertIn(marker, body)

    def test_api(self):
        response, body = self.get("/api/runs")
        self.assertEqual(response.status, 200)
        runs = json.loads(body)["runs"]
        self.assertEqual(runs[0]["id"], "wf_http-013")
        response, body = self.get("/api/run?id=wf_http-013")
        self.assertEqual(json.loads(body)["run"]["status"], "running")
        response, body = self.get("/api/agent?run=wf_http-013&agent=a1")
        data = json.loads(body)
        self.assertIn("<script>alert(1)</script>", data["prompt"])   # as data, escaped by the page
        self.assertEqual(self.get("/api/run?id=wf_nope")[0].status, 404)
        self.assertEqual(self.get("/api/agent?run=wf_http-013&agent=../../etc")[0].status, 404)
        self.assertEqual(self.get("/nope")[0].status, 404)

    def test_other_hosts_are_refused(self):
        self.assertEqual(self.get("/api/runs", host="evil.example:%d" % self.port)[0].status, 403)
        self.assertEqual(self.get("/", host="localhost:%d" % self.port)[0].status, 200)

    def test_page_never_builds_html_from_data(self):
        js = ww.APP_JS + ww.GRAPH_JS
        self.assertNotIn("innerHTML", js)
        self.assertNotIn("insertAdjacentHTML", js)
        self.assertNotIn("document.write", js)

    def test_server_binds_to_loopback_only(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")


if __name__ == "__main__":
    unittest.main()
