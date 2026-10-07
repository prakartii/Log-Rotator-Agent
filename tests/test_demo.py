"""Smoke test: the presentation demo must keep working."""

import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@unittest.skipUnless(os.path.isdir("/proc"), "requires Linux")
class InodeDemoTest(unittest.TestCase):
    def test_demo_runs_and_shows_same_inode(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = subprocess.run(
                [sys.executable, str(PROJECT_ROOT / "demo" / "inode_demo.py"),
                 "--dir", tmp, "--prefill-mb", "1", "--compare"],
                capture_output=True, text=True, timeout=60)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        out = run.stdout

        before = re.search(r"BEFORE\s+inode = (\d+)", out).group(1)
        after = re.search(r"AFTER\s+inode = (\d+)\s+size =\s+0 B", out).group(1)
        self.assertEqual(before, after)
        self.assertIn("inode preserved: True", out)
        self.assertIn(f"still writes to inode {before}", out)
        self.assertNotIn("EXITED", out)
        self.assertIn("(deleted)", out, "comparison should show the writer on a deleted inode")


if __name__ == "__main__":
    unittest.main()
