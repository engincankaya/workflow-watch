"""Checks the page's graph code (graph.js) with Node, when Node is installed.

The tool itself does not need Node; the page runs this code in the browser.
"""

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import workflow_watch as ww  # noqa: E402

NODE = shutil.which("node")


@unittest.skipUnless(NODE, "Node.js is not installed")
class GraphCode(unittest.TestCase):
    def test_graph_cases(self):
        with tempfile.TemporaryDirectory() as tmp:
            acorn = Path(tmp) / "acorn.js"
            graph = Path(tmp) / "graph.js"
            acorn.write_text(ww.ACORN_JS, encoding="utf-8")
            graph.write_text(ww.GRAPH_JS, encoding="utf-8")
            cases = Path(__file__).resolve().parent / "graph_cases.js"
            out = subprocess.run([NODE, str(cases), str(acorn), str(graph)],
                                 capture_output=True, text=True, timeout=60)
            self.assertEqual(out.returncode, 0, out.stderr)
            report = json.loads(out.stdout.strip().splitlines()[-1])
            self.assertEqual(report["failed"], [], report)
            self.assertGreater(report["passed"], 40)


if __name__ == "__main__":
    unittest.main()
