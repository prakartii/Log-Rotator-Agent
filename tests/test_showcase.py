"""The showcase runner (demo/showcase.py) plays through every step against a sandbox."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SHOWCASE = Path(__file__).resolve().parent.parent / "demo" / "showcase.py"


class ShowcaseTest(unittest.TestCase):
    def test_auto_run_goes_through_every_step(self):
        with tempfile.TemporaryDirectory() as sandbox:
            run = subprocess.run([sys.executable, str(SHOWCASE), "--auto", "--fast", "--delay", "0",
                                  "--prefill-mb", "1", "--dir", sandbox],
                                 capture_output=True, text=True, timeout=120)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            for marker in ("LOG ROTATOR AGENT", "STEP 1", "STEP 2", "STEP 3", "STEP 4", "STEP 5",
                           "STEP 6", "inode:", "(preserved)", "archive OK", "never deletes logs",
                           "OUTSIDE_ALLOWED_DIR", "writer still running after the rotation: yes"):
                self.assertIn(marker, run.stdout)
            self.assertTrue(list(Path(sandbox, "rotated_logs").glob("*.gz")))


if __name__ == "__main__":
    unittest.main()
