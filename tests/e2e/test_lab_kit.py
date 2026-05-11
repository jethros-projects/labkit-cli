from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
LAB_KIT = ROOT / "lab-kit"
INSTALL_SH = ROOT / "install.sh"


FAKE_CODEX = r'''#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path


DEFAULT_CATALOG = {
    "models": [
        {
            "slug": "gpt-5.5",
            "context_window": 272000,
            "max_context_window": 272000,
            "effective_context_window_percent": 95,
        },
        {
            "slug": "gpt-4.1",
            "context_window": 128000,
            "max_context_window": 128000,
            "effective_context_window_percent": 100,
        },
    ]
}


FEATURES = """\
apps stable true
browser_use stable true
computer_use stable true
codex_hooks stable true
external_migration experimental false
enable_request_compression stable true
fast_mode stable true
goals experimental false
hooks stable true
memories experimental false
multi_agent stable true
personality stable true
plugins stable true
prevent_idle_sleep experimental false
shell_snapshot stable true
shell_tool stable true
terminal_resize_reflow experimental true
tool_suggest stable true
unified_exec stable true
remote_control under development false
old_removed removed false
old_deprecated deprecated false
"""


def configured_catalog() -> dict:
    codex_home = Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()
    config = codex_home / "config.toml"
    if config.exists():
        text = config.read_text(encoding="utf-8")
        match = re.search(r'^model_catalog_json\s*=\s*"([^"]+)"', text, re.M)
        if match:
            path = Path(match.group(1)).expanduser()
            if path.exists():
                return json.loads(path.read_text(encoding="utf-8"))
    return DEFAULT_CATALOG


def main() -> int:
    args = sys.argv[1:]
    if args == ["--version"]:
        print("codex-cli 9.9.9")
        return 0
    if args == ["features", "list"]:
        print(FEATURES, end="")
        return 0
    if args == ["debug", "models"]:
        print(json.dumps(configured_catalog()))
        return 0
    print("fake codex: unsupported command " + " ".join(args), file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
'''


FAKE_CLAUDE = r'''#!/usr/bin/env python3
from __future__ import annotations

import sys


def main() -> int:
    if sys.argv[1:] == ["--version"]:
        print("2.1.999 (Claude Code)")
        return 0
    print("fake claude: unsupported command " + " ".join(sys.argv[1:]), file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
'''


def write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class LabKitE2ETest(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="lab-kit-e2e."))
        self.home = self.tmp / "home"
        self.codex_home = self.tmp / "codex-home"
        self.claude_home = self.tmp / "claude-home"
        self.bin = self.tmp / "bin"
        self.project = self.tmp / "project"
        self.home.mkdir()
        self.codex_home.mkdir()
        self.claude_home.mkdir()
        self.bin.mkdir()
        self.project.mkdir()
        write_executable(self.bin / "codex", FAKE_CODEX)
        write_executable(self.bin / "claude", FAKE_CLAUDE)
        self.env = os.environ.copy()
        self.env.update(
            {
                "HOME": str(self.home),
                "CODEX_HOME": str(self.codex_home),
                "CLAUDE_HOME": str(self.claude_home),
                "PATH": str(self.bin) + os.pathsep + self.env.get("PATH", ""),
                "NO_COLOR": "1",
                "COLUMNS": "120",
            }
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp)

    def run_lab(
        self,
        *args: str,
        cwd: Path | None = None,
        input_text: str | None = None,
        check: bool = True,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        command = [sys.executable, str(LAB_KIT), *args]
        result = subprocess.run(
            command,
            cwd=str(cwd or self.project),
            env=env or self.env,
            input=input_text,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if check and result.returncode != 0:
            self.fail(
                "command failed\n"
                f"cmd: {' '.join(command)}\n"
                f"returncode: {result.returncode}\n"
                f"stdout:\n{result.stdout}\n"
                f"stderr:\n{result.stderr}"
            )
        return result

    def run_json(self, *args: str, cwd: Path | None = None, check: bool = True) -> dict:
        result = self.run_lab(*args, "--json", cwd=cwd, check=check)
        return json.loads(result.stdout)

    def codex_config_text(self) -> str:
        return (self.codex_home / "config.toml").read_text(encoding="utf-8")

    def claude_settings(self, scope: str = "user") -> dict:
        if scope == "user":
            path = self.claude_home / "settings.json"
        elif scope == "project":
            path = self.project / ".claude" / "settings.json"
        elif scope == "local":
            path = self.project / ".claude" / "settings.local.json"
        else:
            raise AssertionError(scope)
        return read_json(path)

    def write_runtime_event(self, window: int = 950000, total_tokens: int = 42) -> Path:
        session_dir = self.codex_home / "sessions" / "2026" / "05" / "10"
        session_dir.mkdir(parents=True)
        path = session_dir / "rollout-e2e.jsonl"
        event = {
            "timestamp": "2026-05-10T12:00:00.000Z",
            "payload": {
                "type": "task_started",
                "model_context_window": window,
                "info": {"total_token_usage": {"total_tokens": total_tokens}},
            },
        }
        path.write_text(json.dumps(event) + "\n", encoding="utf-8")
        return path

    def test_top_level_help_is_short_and_namespaced(self) -> None:
        result = self.run_lab("--no-color", "--help")
        self.assertIn("{codex,claude-code}", result.stdout)
        self.assertIn("codex", result.stdout)
        self.assertIn("claude-code", result.stdout)
        self.assertNotIn("==SUPPRESS==", result.stdout)
        self.assertNotIn("./lab-kit", result.stdout)

    def test_enable_help_uses_control_id_metavar_not_choice_wall(self) -> None:
        codex = self.run_lab("--no-color", "codex", "enable", "--help").stdout
        claude = self.run_lab("--no-color", "claude-code", "enable", "--help").stdout
        self.assertIn("control-id [control-id ...]", codex)
        self.assertIn("control-id [control-id ...]", claude)
        self.assertNotIn("agent-teams,all-updates", claude)
        self.assertNotIn("1m-context,apps", codex)

    def test_installer_uses_global_command_hint_and_installs_executable(self) -> None:
        install_dir = self.tmp / "install-bin"
        profile = self.tmp / "shell-profile"
        env = self.env.copy()
        env["INSTALL_DIR"] = str(install_dir)
        env["LAB_KIT_PROFILE"] = str(profile)
        env["SHELL"] = "/bin/zsh"
        result = subprocess.run(
            ["sh", str(INSTALL_SH)],
            cwd=str(ROOT),
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        installed = install_dir / "lab-kit"
        self.assertTrue(installed.exists())
        self.assertTrue(os.access(installed, os.X_OK))
        self.assertIn("is installed, but", result.stdout)
        self.assertIn(f"updated profile: {profile}", result.stdout)
        self.assertIn(f"{installed} codex check", result.stdout)
        self.assertIn("export PATH=", result.stdout)
        self.assertIn("For future terminals", result.stdout)
        self.assertIn("new terminal:", result.stdout)
        profile_text = profile.read_text(encoding="utf-8")
        self.assertIn("# >>> lab-kit PATH >>>", profile_text)
        self.assertIn(str(install_dir), profile_text)
        help_result = subprocess.run(
            [str(installed), "--help"],
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(help_result.returncode, 0, help_result.stderr)

    def test_installer_respects_no_color(self) -> None:
        install_dir = self.tmp / "plain-install-bin"
        profile = self.tmp / "plain-shell-profile"
        env = self.env.copy()
        env["INSTALL_DIR"] = str(install_dir)
        env["LAB_KIT_PROFILE"] = str(profile)
        env["SHELL"] = "/bin/zsh"
        env["NO_COLOR"] = "1"
        result = subprocess.run(
            ["sh", str(INSTALL_SH)],
            cwd=str(ROOT),
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("\x1b[", result.stdout)
        self.assertIn("current terminal:", result.stdout)

    def test_codex_check_json_reads_fake_binary_and_isolated_home(self) -> None:
        data = self.run_json("codex", "check")
        self.assertTrue(data["ok"])
        self.assertEqual(data["command"], "codex check")
        self.assertEqual(data["binary"]["version"], "codex-cli 9.9.9")
        self.assertEqual(data["paths"]["codex_home"], str(self.codex_home))
        self.assertEqual(data["binary"]["model"]["catalog_context_window"], 272000)

    def test_codex_list_json_exposes_polished_titles_and_control_ids(self) -> None:
        data = self.run_json("codex", "list")
        features = {feature["name"]: feature for feature in data["features"]}
        self.assertEqual(len(data["features"]), 20)
        self.assertEqual(features["1m-context"]["title"], "1M Context")
        self.assertEqual(features["goals"]["status"], "off")
        self.assertEqual(features["goals"]["source"], "default/experimental")
        self.assertEqual(features["codex-hooks"]["status"], "on")

    def test_codex_dry_run_does_not_create_config(self) -> None:
        data = self.run_json("codex", "enable", "--dry-run", "goals")
        self.assertTrue(data["dry_run"])
        self.assertFalse(data["changed"])
        self.assertFalse((self.codex_home / "config.toml").exists())

    def test_codex_enable_feature_writes_config_and_backup(self) -> None:
        config = self.codex_home / "config.toml"
        config.write_text("[features]\ngoals = false\n", encoding="utf-8")
        data = self.run_json("codex", "enable", "goals")
        self.assertFalse(data["dry_run"])
        self.assertTrue(data["changed"])
        self.assertRegex(self.codex_config_text(), r"(?m)^goals = true$")
        backups = list(self.codex_home.glob("config.toml.backup.*"))
        self.assertEqual(len(backups), 1)
        self.assertIn("goals = false", backups[0].read_text(encoding="utf-8"))

    def test_codex_disable_feature_writes_false(self) -> None:
        (self.codex_home / "config.toml").write_text("[features]\ngoals = true\n", encoding="utf-8")
        self.run_json("codex", "disable", "goals")
        self.assertRegex(self.codex_config_text(), r"(?m)^goals = false$")

    def test_codex_top_level_control_enable_and_disable(self) -> None:
        self.run_json("codex", "enable", "web-search-live")
        self.assertRegex(self.codex_config_text(), r'(?m)^web_search = "live"$')
        self.run_json("codex", "disable", "web-search-live")
        self.assertRegex(self.codex_config_text(), r'(?m)^web_search = "cached"$')

    def test_codex_1m_context_patch_writes_catalog_and_runtime_verify_passes(self) -> None:
        self.run_json("codex", "enable", "1m-context")
        text = self.codex_config_text()
        self.assertRegex(text, r'(?m)^model = "gpt-5\.5"$')
        self.assertRegex(text, r"(?m)^model_context_window = 1000000$")
        self.assertRegex(text, r"(?m)^model_auto_compact_token_limit = 800000$")
        catalog_path = Path(re.search(r'(?m)^model_catalog_json = "([^"]+)"$', text).group(1))
        catalog = read_json(catalog_path)
        model = next(item for item in catalog["models"] if item["slug"] == "gpt-5.5")
        self.assertEqual(model["context_window"], 1052632)
        self.assertEqual(model["max_context_window"], 1052632)

        self.write_runtime_event(window=950000, total_tokens=123)
        verify = self.run_json("codex", "verify", "--strict")
        self.assertTrue(verify["ok"])
        self.assertTrue(verify["config_layer"]["ok"])
        self.assertTrue(verify["catalog_metadata"]["ok"])
        self.assertTrue(verify["runtime_evidence"]["ok"])

    def test_codex_verify_strict_fails_without_runtime_evidence(self) -> None:
        self.run_json("codex", "enable", "1m-context")
        result = self.run_lab("codex", "verify", "--strict", "--json", check=False)
        self.assertEqual(result.returncode, 2)
        data = json.loads(result.stdout)
        self.assertFalse(data["ok"])
        self.assertFalse(data["runtime_evidence"]["ok"])

    def test_discover_filters_removed_and_deprecated_unless_all(self) -> None:
        normal = self.run_json("codex", "discover")
        names = {feature["name"] for feature in normal["features"]}
        self.assertIn("remote_control", names)
        self.assertNotIn("old_removed", names)
        self.assertNotIn("old_deprecated", names)

        all_data = self.run_json("codex", "discover", "--all")
        all_names = {feature["name"] for feature in all_data["features"]}
        self.assertIn("old_removed", all_names)
        self.assertIn("old_deprecated", all_names)

    def test_claude_check_json_reads_fake_binary(self) -> None:
        data = self.run_json("claude-code", "check")
        self.assertTrue(data["ok"])
        self.assertEqual(data["binary"]["version"], "2.1.999 (Claude Code)")
        self.assertEqual(data["paths"]["home"], str(self.claude_home))
        self.assertEqual(len(data["features"]), 119)

    def test_claude_list_json_exposes_full_catalog_and_polished_copy(self) -> None:
        data = self.run_json("claude-code", "list")
        self.assertEqual(len(data["features"]), 119)
        self.assertEqual(sum(1 for feature in data["features"] if feature["selectable"]), 115)
        features = {feature["name"]: feature for feature in data["features"]}
        self.assertEqual(features["auto-memory"]["title"], "Auto Memory")
        self.assertEqual(features["auto-memory"]["description"], "Reads and writes project memory between sessions.")
        self.assertEqual(features["1m-context"]["status"], "on")
        self.assertEqual(features["agent-teams"]["status"], "off")

    def test_claude_dry_run_does_not_create_settings(self) -> None:
        data = self.run_json("claude-code", "enable", "--dry-run", "agent-teams")
        self.assertTrue(data["dry_run"])
        self.assertFalse(data["changed"])
        self.assertFalse((self.claude_home / "settings.json").exists())

    def test_claude_enable_env_control_writes_settings_and_backup(self) -> None:
        settings = self.claude_home / "settings.json"
        settings.write_text('{"env":{"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS":"0"}}\n', encoding="utf-8")
        self.run_json("claude-code", "enable", "agent-teams")
        data = self.claude_settings()
        self.assertEqual(data["env"]["CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"], "1")
        backups = list(self.claude_home.glob("settings.json.backup.*"))
        self.assertEqual(len(backups), 1)
        self.assertIn('"0"', backups[0].read_text(encoding="utf-8"))

    def test_claude_disable_inverted_env_control_writes_opt_out_value(self) -> None:
        self.run_json("claude-code", "disable", "1m-context")
        data = self.claude_settings()
        self.assertEqual(data["env"]["CLAUDE_CODE_DISABLE_1M_CONTEXT"], "1")

    def test_claude_setting_control_writes_boolean_setting(self) -> None:
        self.run_json("claude-code", "enable", "thinking")
        data = self.claude_settings()
        self.assertIs(data["alwaysThinkingEnabled"], True)
        self.run_json("claude-code", "disable", "thinking")
        data = self.claude_settings()
        self.assertIs(data["alwaysThinkingEnabled"], False)

    def test_claude_project_and_local_scopes_write_project_files(self) -> None:
        self.run_json("claude-code", "enable", "--scope", "project", "agent-teams")
        project = self.claude_settings("project")
        self.assertEqual(project["env"]["CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"], "1")
        self.assertFalse((self.claude_home / "settings.json").exists())

        self.run_json("claude-code", "enable", "--scope", "local", "thinking")
        local = self.claude_settings("local")
        self.assertIs(local["alwaysThinkingEnabled"], True)

    def test_claude_manual_session_controls_are_reference_only(self) -> None:
        result = self.run_lab("claude-code", "enable", "chrome-session", "--json", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid choice", result.stderr)

    def test_legacy_top_level_codex_alias_still_works_but_is_hidden(self) -> None:
        help_text = self.run_lab("--no-color", "--help").stdout
        self.assertNotIn(" check ", help_text)
        data = self.run_json("list")
        self.assertEqual(data["command"], "codex list")

    def test_readme_deployed_usage_has_no_local_invocation_or_old_placeholder(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("lab-kit codex select", readme)
        self.assertIn("lab-kit claude-code select", readme)
        self.assertNotIn("./lab-kit", readme)
        self.assertNotIn("<feature-name>", readme)
        self.assertNotIn("codex-lab-kit", readme)


if __name__ == "__main__":
    unittest.main(verbosity=2)
