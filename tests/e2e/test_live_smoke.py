from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
LAB_KIT = ROOT / "lab-kit"


LIVE_ENABLED = os.environ.get("LAB_KIT_LIVE_E2E") == "1"


@unittest.skipUnless(LIVE_ENABLED, "set LAB_KIT_LIVE_E2E=1 to run live smoke tests")
class LiveSmokeTest(unittest.TestCase):
    maxDiff = None

    def run_lab(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.update({"NO_COLOR": "1", "COLUMNS": "120"})
        result = subprocess.run(
            [sys.executable, str(LAB_KIT), *args],
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if check and result.returncode != 0:
            self.fail(
                "command failed\n"
                f"cmd: {' '.join([sys.executable, str(LAB_KIT), *args])}\n"
                f"returncode: {result.returncode}\n"
                f"stdout:\n{result.stdout}\n"
                f"stderr:\n{result.stderr}"
            )
        return result

    def run_json(self, *args: str) -> dict:
        result = self.run_lab(*args, "--json")
        return json.loads(result.stdout)

    @unittest.skipUnless(shutil.which("codex"), "codex is not installed on PATH")
    def test_real_codex_read_only_surfaces(self) -> None:
        check = self.run_json("codex", "check")
        self.assertEqual(check["command"], "codex check")
        self.assertTrue(check["binary"]["path"])

        catalog = self.run_json("codex", "list")
        self.assertEqual(catalog["command"], "codex list")
        self.assertGreaterEqual(len(catalog["features"]), 1)

        dry_run = self.run_json("codex", "enable", "--dry-run", "goals")
        self.assertTrue(dry_run["dry_run"])
        self.assertFalse(dry_run["changed"])

    @unittest.skipUnless(shutil.which("claude"), "claude is not installed on PATH")
    def test_real_claude_code_read_only_surfaces(self) -> None:
        check = self.run_json("claude-code", "check")
        self.assertEqual(check["command"], "claude-code check")
        self.assertTrue(check["binary"]["path"])

        catalog = self.run_json("claude-code", "list")
        self.assertEqual(catalog["command"], "claude-code list")
        self.assertGreaterEqual(len(catalog["features"]), 100)

        dry_run = self.run_json("claude-code", "enable", "--dry-run", "agent-teams")
        self.assertTrue(dry_run["dry_run"])
        self.assertFalse(dry_run["changed"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
