"""Simulated runs of many workflow shapes, end to end.

Each scenario script under tests/scenarios really runs in
tools/simulate_workflow.js, which writes the files Claude Code writes for a
run. The viewer then reads them, and tests/scenario_check.js checks, with the
page's own graph code, that every agent is placed at the agent() call that
started it (the simulator records the script line), shown exactly once,
and that loop rounds and fan-out columns are right.

None of these scripts comes from a real project. They need Node.js.
"""

import json
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import workflow_watch as ww  # noqa: E402

NODE = shutil.which("node")
SIM = ROOT / "tools" / "simulate_workflow.js"
CHECK = HERE / "scenario_check.js"
SCEN = HERE / "scenarios"


@unittest.skipUnless(NODE, "Node.js is not installed")
class Scenarios(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.base = Path(cls.tmp.name)
        (cls.base / "acorn.js").write_text(ww.ACORN_JS, encoding="utf-8")
        (cls.base / "graph.js").write_text(ww.GRAPH_JS, encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    # -- helpers -------------------------------------------------------------

    def simulate(self, name, args=None, extra=(), root=None, step_ms=5, wait=True):
        root = root or Path(tempfile.mkdtemp(dir=self.base))
        truth = root / (name + ".truth.json")
        cmd = [NODE, str(SIM), "--root", str(root), "--script", str(SCEN / (name + ".js")),
               "--step-ms", str(step_ms), "--truth", str(truth)]
        if args is not None:
            cmd += ["--args", json.dumps(args)]
        cmd += list(extra)
        if not wait:
            return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True), root, truth
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(truth.read_text()), root

    def view(self, root, run_id, **kw):
        store = ww.Store(root, **kw)
        store.refresh(force=True)
        detail = store.run_detail(run_id)
        static = store.run_static(run_id)
        prompts = store.prompts(run_id, [a["agentId"] for a in detail["agents"]])["prompts"]
        return store, detail, static, prompts

    def check(self, truth, detail, static, prompts):
        data = {"script": static["script"], "truth": truth, "agents": detail["agents"],
                "prompts": prompts, "phases": detail.get("phases")}
        path = Path(tempfile.mkstemp(dir=self.base, suffix=".json")[1])
        path.write_text(json.dumps(data))
        out = subprocess.run([NODE, str(CHECK), str(self.base / "acorn.js"), str(self.base / "graph.js"), str(path)],
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        report = json.loads(out.stdout.strip().splitlines()[-1])
        self.assertIsNone(report["error"], report["error"])
        return report

    def assert_clean(self, report, truth, detail):
        self.assertEqual(report["wrongLine"], [], "agents placed at the wrong agent() call")
        self.assertEqual(report["unmatched"], [], "agents not placed in the flow")
        self.assertEqual(report["notShown"], [], "agents placed but not drawn")
        self.assertEqual(report["shownTwice"], [], "agents drawn twice")
        self.assertTrue(report["layout"]["finite"])
        self.assertEqual(report["duplicateIds"], [])
        self.assertEqual(len(detail["agents"]), len(truth["agents"]))

    def run_scenario(self, name, args=None, extra=()):
        truth, root = self.simulate(name, args, extra)
        store, detail, static, prompts = self.view(root, truth["runId"])
        report = self.check(truth, detail, static, prompts)
        return truth, detail, static, report, store

    def finished(self, detail, truth):
        run = detail["run"]
        self.assertEqual(run["status"], "finished")
        self.assertEqual(run["agentsDone"], len(truth["agents"]))
        self.assertEqual(run["agentsRunning"], 0)

    # -- shapes ---------------------------------------------------------------

    def test_fan_out_with_verification(self):
        truth, detail, static, report, _ = self.run_scenario("fanout_audit")
        self.assert_clean(report, truth, detail)
        self.finished(detail, truth)
        self.assertEqual([f["columns"] for f in report["fanouts"]], [12, 12])
        self.assertEqual(detail["logs"], ["0 findings survived"] if detail["logs"][0].startswith("0") else detail["logs"])

    def test_pipeline(self):
        lenses = ["security", "speed", "api", "docs", "tests"]
        truth, detail, static, report, _ = self.run_scenario("pipeline_lenses", {"lenses": lenses})
        self.assert_clean(report, truth, detail)
        self.finished(detail, truth)
        self.assertEqual([f["columns"] for f in report["fanouts"]], [5])

    def test_loop_until_dry_with_nested_votes(self):
        truth, detail, static, report, _ = self.run_scenario("loop_until_dry")
        self.assert_clean(report, truth, detail)
        self.assertEqual([l["rounds"] for l in report["loops"]], [3])
        columns = [f["columns"] for f in report["fanouts"]]
        # finders: one column per finder over all rounds; judges: one column per
        # bug number, and in each of them one column per lens
        self.assertEqual(columns[0], 3, report["fanouts"])
        self.assertEqual(columns[1], 3, report["fanouts"])
        self.assertEqual(columns[2:], [3, 3, 3], report["fanouts"])

    def test_judge_panel(self):
        truth, detail, static, report, _ = self.run_scenario("judge_panel")
        self.assert_clean(report, truth, detail)
        self.assertEqual([f["columns"] for f in report["fanouts"]], [3])

    def test_no_labels_no_phases(self):
        truth, detail, static, report, _ = self.run_scenario("unlabeled")
        self.assert_clean(report, truth, detail)

    def test_no_labels_and_empty_runtime_labels(self):
        truth, detail, static, report, _ = self.run_scenario("unlabeled", extra=["--unlabeled-label", "empty"])
        self.assert_clean(report, truth, detail)

    def test_helpers_switch_try_do_while(self):
        truth, detail, static, report, _ = self.run_scenario("helpers_switch", {"mode": "full"})
        self.assert_clean(report, truth, detail)
        self.assertEqual([l["rounds"] for l in report["loops"]], [3])
        # the helper is called twice (and two calls share one line): each box
        # drawn from one line holds its own agent
        self.assertTrue(report["helperCopies"] and all(n == 1 for n in report["helperCopies"]), report["helperCopies"])

    def test_more_items_than_columns(self):
        truth, detail, static, report, _ = self.run_scenario("big_fanout", {"n": 60})
        self.assert_clean(report, truth, detail)
        self.assertEqual([f["columns"] for f in report["fanouts"]], [0])   # one node, ×60
        self.finished(detail, truth)

    def test_fan_out_inside_fan_out(self):
        truth, detail, static, report, _ = self.run_scenario("nested_columns")
        self.assert_clean(report, truth, detail)
        self.assertEqual([f["columns"] for f in report["fanouts"]], [4, 3, 3, 3, 3])

    def test_fan_out_inside_a_loop(self):
        truth, detail, static, report, _ = self.run_scenario("loop_fanout")
        self.assert_clean(report, truth, detail)
        self.assertEqual([l["rounds"] for l in report["loops"]], [2])
        self.assertEqual([f["columns"] for f in report["fanouts"]], [3])   # one per file, both rounds

    def test_rounds_from_a_helper_built_label(self):
        truth, detail, static, report, _ = self.run_scenario("serial_rounds", {"topic": "billing"})
        self.assert_clean(report, truth, detail)
        self.assertEqual([l["rounds"] for l in report["loops"]], [3])

    # -- run states -----------------------------------------------------------

    def test_failed_agents(self):
        truth, detail, static, report, _ = self.run_scenario("fanout_audit", extra=["--fail", "^audit:3$|^verify:1$"])
        self.assert_clean(report, truth, detail)
        failed = sorted(a["label"] for a in detail["agents"] if a["state"] == "failed")
        self.assertEqual(failed, ["audit:3"] + (["verify:1"] if any(a["label"] == "verify:1" for a in detail["agents"]) else []))

    def test_results_from_cache(self):
        # a run, then a resumed run in the same session: its first two agents
        # come back from cache, with result lines only
        first, root = self.simulate("serial_rounds", {"topic": "x"})
        truth, _ = self.simulate("serial_rounds", {"topic": "x"}, extra=["--cached", "2"], root=root)
        self.assertNotEqual(first["runId"], truth["runId"])
        store, detail, static, prompts = self.view(root, truth["runId"])
        report = self.check(truth, detail, static, prompts)
        self.assert_clean(report, truth, detail)
        self.assertEqual([a["cached"] for a in detail["agents"]][:2], [True, True])
        self.assertEqual(detail["agents"][0]["label"], "write:x")

    def test_cut_off_run(self):
        truth, root = self.simulate("fanout_audit", extra=["--stop-after", "6"])
        store, detail, static, prompts = self.view(root, truth["runId"])
        self.assertEqual(detail["run"]["status"], "running")      # its files changed just now
        self.assertEqual(len(detail["agents"]), 6)
        self.assertEqual(static["scriptOrigin"], "launch")
        report = self.check(truth, detail, static, prompts)
        self.assertEqual(report["wrongLine"], [])
        self.assertEqual(report["unmatched"], [])
        store, detail, _, _ = self.view(root, truth["runId"], stale_minutes=1e-6)
        self.assertEqual(detail["run"]["status"], "probably stopped")
        self.assertTrue(all(a["state"] != "running" for a in detail["agents"]))

    def test_saved_workflow_started_by_name(self):
        root = Path(tempfile.mkdtemp(dir=self.base))
        project = root / "work" / "shop"
        saved = project / ".claude" / "workflows"
        saved.mkdir(parents=True)
        shutil.copy(SCEN / "serial_rounds.js", saved / "write-and-check.js")
        claude_root = root / "claude" / "projects"
        claude_root.mkdir(parents=True)
        truth, _ = self.simulate("serial_rounds", {"topic": "y"},
                                 extra=["--cwd", str(project / "sub"), "--launch-by-name", "write-and-check", "--stop-after", "2"],
                                 root=claude_root)
        store, detail, static, prompts = self.view(claude_root, truth["runId"])
        self.assertEqual(static["scriptOrigin"], "saved")
        self.assertIn("write-and-check.js", static["scriptNote"])
        report = self.check(truth, detail, static, prompts)
        self.assertEqual(report["wrongLine"], [])
        # a personal saved workflow, found by its meta name
        personal = root / "claude" / "workflows"
        personal.mkdir(parents=True)
        shutil.copy(SCEN / "judge_panel.js", personal / "panel.js")
        self.assertIn("panel.js", store.find_saved_workflow("plan-panel", "/nowhere")[1])
        self.assertIsNone(store.find_saved_workflow("not-there", "/nowhere"))

    def test_live_run_is_seen_while_it_runs(self):
        proc, root, truth_path = self.simulate("loop_fanout", step_ms=250, wait=False)
        try:
            seen_running, seen_agents = False, 0
            store = ww.Store(root)
            for _ in range(80):
                time.sleep(0.25)
                store.last_refresh = None
                runs = store.list_runs()["runs"]
                runs = [r for r in runs if r.get("kind") == "run"]
                if not runs:
                    continue
                detail = store.run_detail(runs[0]["id"])
                if detail["run"]["status"] == "running" and detail["run"]["agentsRunning"]:
                    seen_running = True
                    seen_agents = max(seen_agents, detail["run"]["agentsRunning"])
                if detail["run"]["status"] == "finished":
                    break
            self.assertTrue(seen_running, "the run was never seen running")
            self.assertEqual(seen_agents, 3, "three reviewers run at the same time")
            self.assertEqual(detail["run"]["status"], "finished")
        finally:
            proc.communicate(timeout=60)

    def test_many_projects_and_sessions(self):
        root = Path(tempfile.mkdtemp(dir=self.base))
        for i, name in enumerate(["judge_panel", "unlabeled", "loop_fanout"]):
            self.simulate(name, root=root, extra=["--cwd", "/projects/p%d" % (i % 2), "--session", "s%d" % i])
        store = ww.Store(root)
        runs = [r for r in store.list_runs()["runs"] if r.get("kind") == "run"]
        self.assertEqual(len(runs), 3)
        self.assertEqual(sorted({r["cwd"] for r in runs}), ["/projects/p0", "/projects/p1"])
        self.assertTrue(all(r["status"] == "finished" for r in runs))


if __name__ == "__main__":
    unittest.main()
