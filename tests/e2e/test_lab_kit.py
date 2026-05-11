from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LABKIT = ROOT / "labkit"
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


FAKE_CLAUDE = r"""#!/usr/bin/env python3
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
"""


def write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class LabKitE2ETest(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="labkit-e2e."))
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
        command = [sys.executable, str(LABKIT), *args]
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

    def run_json(self, *args: str, cwd: Path | None = None, check: bool = True, env: dict[str, str] | None = None) -> dict:
        result = self.run_lab(*args, "--json", cwd=cwd, check=check, env=env)
        return json.loads(result.stdout)

    def codex_config_text(self) -> str:
        return (self.codex_home / "config.toml").read_text(encoding="utf-8")

    def write_codex_1m_override(self) -> Path:
        catalog = {
            "models": [
                {
                    "slug": "gpt-5.5",
                    "context_window": 1052632,
                    "max_context_window": 1052632,
                    "effective_context_window_percent": 95,
                }
            ]
        }
        catalog_path = self.codex_home / "model-catalog-1m.json"
        catalog_path.write_text(json.dumps(catalog) + "\n", encoding="utf-8")
        (self.codex_home / "config.toml").write_text(
            "\n".join(
                [
                    'model = "gpt-5.5"',
                    f'model_catalog_json = "{catalog_path}"',
                    "model_context_window = 1000000",
                    "model_auto_compact_token_limit = 800000",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        return catalog_path

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
        self.assertIn("{codex,claude-code,update-features}", result.stdout)
        self.assertIn("codex", result.stdout)
        self.assertIn("claude-code", result.stdout)
        self.assertNotIn("==SUPPRESS==", result.stdout)
        self.assertNotIn("./labkit", result.stdout)

    def test_enable_help_uses_control_id_metavar_not_choice_wall(self) -> None:
        codex = self.run_lab("--no-color", "codex", "enable", "--help").stdout
        claude = self.run_lab("--no-color", "claude-code", "enable", "--help").stdout
        self.assertIn("control-id [control-id ...]", codex)
        self.assertIn("control-id [control-id ...]", claude)
        self.assertNotIn("agent-teams,all-updates", claude)
        self.assertNotIn("1m-context,apps", codex)

    def test_version_flag(self) -> None:
        result = self.run_lab("--version")
        self.assertRegex(result.stdout.strip(), r"^labkit \d+\.\d+\.\d+")

    def test_installer_uses_global_command_hint_and_installs_executable(self) -> None:
        install_dir = self.tmp / "install-bin"
        profile = self.tmp / "shell-profile"
        env = self.env.copy()
        env["INSTALL_DIR"] = str(install_dir)
        env["LABKIT_PROFILE"] = str(profile)
        env["SHELL"] = "/bin/zsh"
        legacy = install_dir / "lab-kit"
        install_dir.mkdir()
        legacy.write_text("old command\n", encoding="utf-8")
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
        installed = install_dir / "labkit"
        self.assertTrue(installed.exists())
        self.assertFalse(legacy.exists())
        self.assertTrue(os.access(installed, os.X_OK))
        self.assertIn("is installed, but", result.stdout)
        self.assertIn(f"updated profile: {profile}", result.stdout)
        self.assertIn(f"{installed} codex check", result.stdout)
        self.assertIn("export PATH=", result.stdout)
        self.assertIn("For future terminals", result.stdout)
        self.assertIn("new terminal:", result.stdout)
        profile_text = profile.read_text(encoding="utf-8")
        self.assertIn("# >>> labkit PATH >>>", profile_text)
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
        env["LABKIT_PROFILE"] = str(profile)
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

    def test_installer_verifies_pinned_archive_checksum(self) -> None:
        archive_root = self.tmp / "archive-root" / "labkit-cli-test"
        archive_root.mkdir(parents=True)
        shutil.copy2(ROOT / "labkit", archive_root / "labkit")
        shutil.copytree(ROOT / "lab_kit", archive_root / "lab_kit")
        archive = self.tmp / "labkit-cli-test.tar.gz"
        with tarfile.open(archive, "w:gz") as handle:
            handle.add(archive_root, arcname="labkit-cli-test")
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()

        install_dir = self.tmp / "archive-install-bin"
        env = self.env.copy()
        env["INSTALL_DIR"] = str(install_dir)
        env["ARCHIVE_URL"] = archive.as_uri()
        env["LABKIT_SHA256"] = digest
        env["LABKIT_NO_PATH_UPDATE"] = "1"
        result = subprocess.run(
            ["sh", str(INSTALL_SH)],
            cwd=str(self.tmp),
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("verified SHA256", result.stdout)
        self.assertTrue((install_dir / "labkit").exists())
        self.assertTrue((install_dir / "lab_kit" / "cli.py").exists())

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
        self.assertEqual(data["mode"], "marked")
        self.assertIn("1m-context", features)
        self.assertEqual(features["1m-context"]["risk_level"], "high")
        self.assertFalse(features["1m-context"]["recommended"])
        self.assertFalse(features["1m-context"]["selectable"])
        self.assertEqual(features["goals"]["status"], "off")
        self.assertEqual(features["goals"]["source"], "default/experimental")
        self.assertEqual(features["codex-hooks"]["status"], "on")
        self.assertNotIn("remote-control", features)

    def test_codex_all_mode_includes_dynamic_registry_controls(self) -> None:
        data = self.run_json("codex", "list", "--all")
        features = {feature["name"]: feature for feature in data["features"]}
        self.assertEqual(data["mode"], "all")
        self.assertIn("remote-control", features)
        self.assertEqual(features["remote-control"]["key"], "remote_control")
        self.assertEqual(features["remote-control"]["title"], "Remote Control")
        self.assertTrue(features["remote-control"]["dependencies"])
        self.assertTrue(features["remote-control"]["limitations"])
        self.assertIn("1m-context", features)
        self.assertEqual(features["1m-context"]["title"], "Unsupported 1M Context Override")
        self.assertFalse(features["1m-context"]["selectable"])
        self.assertTrue(features["1m-context"]["limitations"])
        self.assertNotIn("old-removed", features)

    def test_codex_info_explains_dependencies_limitations_and_verification(self) -> None:
        data = self.run_json("codex", "info", "1m-context")
        feature = data["feature"]
        self.assertEqual(feature["name"], "1m-context")
        self.assertIn("dependencies", feature)
        self.assertIn("limitations", feature)
        self.assertEqual(feature["verification"], "runtime")
        self.assertIn("verification_steps", feature)
        self.assertTrue(any(item["kind"] == "strict-runtime" for item in feature["verification_steps"]))

        text = self.run_lab("--no-color", "codex", "list", "--details").stdout
        self.assertIn("Dependencies", text)
        self.assertIn("Limitations", text)
        self.assertIn("Verification", text)

    def test_codex_dry_run_does_not_create_config(self) -> None:
        data = self.run_json("codex", "enable", "--dry-run", "goals")
        self.assertTrue(data["dry_run"])
        self.assertFalse(data["changed"])
        self.assertIn("limitations", data["features"][0])
        self.assertIn("verification", data["features"][0])
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

    def test_codex_select_fallback_can_disable_enabled_control(self) -> None:
        (self.codex_home / "config.toml").write_text("[features]\ngoals = true\n", encoding="utf-8")
        self.run_lab("codex", "select", input_text="goals\ninactive\napply\n")
        self.assertRegex(self.codex_config_text(), r"(?m)^goals = false$")

    def test_codex_enable_and_disable_dynamic_registry_feature(self) -> None:
        self.run_json("codex", "enable", "remote-control")
        self.assertRegex(self.codex_config_text(), r"(?m)^remote_control = true$")
        self.run_json("codex", "disable", "remote_control")
        self.assertRegex(self.codex_config_text(), r"(?m)^remote_control = false$")

    def test_codex_top_level_control_enable_and_disable(self) -> None:
        self.run_json("codex", "enable", "web-search-live")
        self.assertRegex(self.codex_config_text(), r'(?m)^web_search = "live"$')
        self.run_json("codex", "disable", "web-search-live")
        self.assertRegex(self.codex_config_text(), r'(?m)^web_search = "cached"$')

    def test_codex_1m_context_enable_is_blocked_as_unsupported(self) -> None:
        result = self.run_lab("codex", "enable", "1m-context", "--json", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("reference-only", result.stderr)
        self.assertFalse((self.codex_home / "config.toml").exists())

    def test_codex_1m_context_override_runtime_verify_passes_when_preexisting(self) -> None:
        self.write_codex_1m_override()
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

    def test_codex_1m_context_disable_removes_catalog_override(self) -> None:
        self.write_codex_1m_override()
        text = self.codex_config_text()
        catalog_path = Path(re.search(r'(?m)^model_catalog_json = "([^"]+)"$', text).group(1))
        self.assertTrue(catalog_path.exists())

        data = self.run_json("codex", "disable", "1m-context")
        self.assertFalse(data["features"][0]["enabled"])
        text = self.codex_config_text()
        self.assertNotRegex(text, r"(?m)^model_catalog_json = ")
        self.assertNotRegex(text, r"(?m)^model_context_window = ")
        self.assertNotRegex(text, r"(?m)^model_auto_compact_token_limit = ")
        self.assertFalse(catalog_path.exists())

    def test_codex_verify_strict_fails_without_runtime_evidence(self) -> None:
        self.write_codex_1m_override()
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
        self.assertEqual(len(data["features"]), 144)

    def test_claude_list_json_exposes_full_catalog_and_polished_copy(self) -> None:
        data = self.run_json("claude-code", "list")
        self.assertEqual(len(data["features"]), 144)
        self.assertEqual(data["mode"], "marked")
        self.assertEqual(sum(1 for feature in data["features"] if feature["selectable"]), 120)
        features = {feature["name"]: feature for feature in data["features"]}
        self.assertEqual(features["auto-memory"]["title"], "Auto Memory")
        self.assertEqual(features["auto-memory"]["description"], "Reads and writes project memory between sessions.")
        self.assertTrue(features["auto-memory"]["dependencies"])
        self.assertTrue(features["auto-memory"]["limitations"])
        self.assertEqual(features["auto-memory"]["risk_level"], "low")
        self.assertEqual(features["auto-memory"]["verification"], "runtime")
        self.assertTrue(features["auto-memory"]["verification_steps"])
        self.assertIn("agent-view", features)
        self.assertIn("voice-dictation", features)
        self.assertIn("auto-permissions", features)
        self.assertIn("channels", features)
        self.assertIn("kairos", features)
        self.assertEqual(features["kairos"]["risk_level"], "internal")
        self.assertFalse(features["kairos"]["recommended"])
        self.assertIn("ultraplan", features)
        self.assertEqual(features["ultraplan"]["risk_level"], "medium")
        self.assertEqual(features["agent-teams"]["status"], "off")

        risky = self.run_json("claude-code", "list", "--risk", "high")
        risky_names = {feature["name"] for feature in risky["features"]}
        self.assertIn("command-injection-check-bypass", risky_names)
        self.assertIn("kairos", risky_names)
        self.assertNotIn("auto-memory", risky_names)

    def test_claude_all_mode_merges_schema_and_settings_keys(self) -> None:
        settings = {
            "includeCoAuthoredBy": False,
            "customBoolean": True,
            "env": {"LABKIT_PRIVATE_FLAG": "yes"},
        }
        (self.claude_home / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
        data = self.run_json("claude-code", "list", "--all")
        features = {feature["name"]: feature for feature in data["features"]}
        self.assertEqual(data["mode"], "all")
        self.assertIn("include-co-authored-by", features)
        self.assertEqual(features["include-co-authored-by"]["key"], "includeCoAuthoredBy")
        self.assertTrue(features["include-co-authored-by"]["dependencies"])
        self.assertTrue(features["include-co-authored-by"]["limitations"])
        self.assertIn("custom-boolean", features)
        self.assertTrue(features["custom-boolean"]["limitations"])
        self.assertIn("env-labkit-private-flag", features)
        self.assertIn("kairos", features)
        self.assertFalse(features["kairos"]["selectable"])
        self.assertIn("ultraplan", features)
        self.assertFalse(features["ultraplan"]["selectable"])
        self.assertIn("removed-auto-mode-flag", features)
        self.assertEqual(features["removed-auto-mode-flag"]["stage"], "removed")

    def test_claude_info_explains_curated_and_schema_controls(self) -> None:
        curated = self.run_json("claude-code", "info", "auto-memory")
        self.assertEqual(curated["feature"]["name"], "auto-memory")
        self.assertTrue(any(item["kind"] == "version" for item in curated["feature"]["dependencies"]))

        schema = self.run_json("claude-code", "info", "include-co-authored-by")
        self.assertEqual(schema["feature"]["key"], "includeCoAuthoredBy")
        self.assertTrue(any(item["kind"] == "schema" for item in schema["feature"]["dependencies"]))

        text = self.run_lab("--no-color", "claude-code", "list", "--details").stdout
        self.assertIn("Dependencies", text)
        self.assertIn("Limitations", text)
        self.assertIn("Verification", text)
        self.assertIn("Sources", text)

    def test_claude_leaked_feature_info_marks_reference_only_status(self) -> None:
        kairos = self.run_json("claude-code", "info", "kairos")
        self.assertEqual(kairos["feature"]["name"], "kairos")
        self.assertFalse(kairos["feature"]["selectable"])
        self.assertEqual(kairos["feature"]["risk_level"], "internal")
        self.assertEqual(kairos["feature"]["stability"], "internal")
        self.assertFalse(kairos["feature"]["recommended"])
        self.assertTrue(any(item["severity"] == "blocking" for item in kairos["feature"]["limitations"]))
        self.assertTrue(any("techsy.io" in item.get("url", "") for item in kairos["feature"]["sources"]))

        removed = self.run_json("claude-code", "info", "removed-auto-mode-flag")
        self.assertEqual(removed["feature"]["stage"], "removed")
        self.assertTrue(any("removed" in item["detail"] for item in removed["feature"]["limitations"]))

    def test_feature_metadata_uses_smart_marking_schema(self) -> None:
        for filename in ("claude_feature_metadata.json", "codex_feature_metadata.json"):
            data = read_json(ROOT / "lab_kit" / "data" / filename)
            self.assertEqual(data["schema_version"], 2)
            self.assertNotIn("default_hidden", data)
            for name, entry in data["features"].items():
                with self.subTest(filename=filename, feature=name):
                    self.assertIn(entry["risk_level"], {"low", "medium", "high", "internal"})
                    self.assertIn(entry["stability"], {"stable", "experimental", "beta", "internal"})
                    self.assertIsInstance(entry["recommended"], bool)
                    self.assertIn(entry["verification"], {"runtime", "config", "manual", "none"})
                    self.assertIsInstance(entry["notes"], str)
                    self.assertIsInstance(entry["tags"], list)

    def test_claude_schema_derived_boolean_can_be_changed(self) -> None:
        self.run_json("claude-code", "enable", "include-co-authored-by")
        data = self.claude_settings()
        self.assertIs(data["includeCoAuthoredBy"], True)
        self.run_json("claude-code", "disable", "include-co-authored-by")
        data = self.claude_settings()
        self.assertIs(data["includeCoAuthoredBy"], False)

    def test_claude_discover_reports_schema_and_settings_extras(self) -> None:
        (self.claude_home / "settings.json").write_text('{"customBoolean": true}\n', encoding="utf-8")
        data = self.run_json("claude-code", "discover")
        names = {feature["name"] for feature in data["features"]}
        self.assertIn("auto-memory", names)
        self.assertIn("include-co-authored-by", names)
        self.assertIn("custom-boolean", names)
        self.assertIn("customBoolean", data["settings_keys"])

    def test_claude_dry_run_does_not_create_settings(self) -> None:
        data = self.run_json("claude-code", "enable", "--dry-run", "agent-teams")
        self.assertTrue(data["dry_run"])
        self.assertFalse(data["changed"])
        self.assertIn("limitations", data["features"][0])
        self.assertIn("verification", data["features"][0])
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

    def test_claude_experimental_settings_write_nested_values(self) -> None:
        self.run_json("claude-code", "enable", "voice-dictation")
        data = self.claude_settings()
        self.assertIs(data["voice"]["enabled"], True)
        self.run_json("claude-code", "enable", "voice-tap-mode")
        data = self.claude_settings()
        self.assertEqual(data["voice"]["mode"], "tap")
        self.run_json("claude-code", "enable", "auto-permissions")
        data = self.claude_settings()
        self.assertEqual(data["permissions"]["defaultMode"], "auto")
        self.run_json("claude-code", "disable", "auto-permissions")
        data = self.claude_settings()
        self.assertEqual(data["permissions"]["defaultMode"], "default")

    def test_claude_leaked_reference_controls_cannot_be_enabled(self) -> None:
        result = self.run_lab("claude-code", "enable", "kairos", "--json", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("reference-only", result.stderr)
        self.assertFalse((self.claude_home / "settings.json").exists())

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

    def test_update_features_refreshes_claude_schema_cache(self) -> None:
        schema = self.tmp / "schema.json"
        schema.write_text(
            json.dumps(
                {
                    "type": "object",
                    "properties": {
                        "env": {"type": "object", "properties": {}},
                        "newSchemaToggle": {"type": "boolean", "description": "A schema-only toggle."},
                    },
                }
            ),
            encoding="utf-8",
        )
        env = self.env.copy()
        env["LABKIT_DATA_HOME"] = str(self.tmp / "labkit-data")
        env["LABKIT_CLAUDE_SCHEMA_URL"] = schema.as_uri()
        data = self.run_json("update-features", "--skip-codex", env=env)
        self.assertTrue(data["claude"]["ok"])
        self.assertTrue((Path(data["claude"]["path"])).exists())
        listed = self.run_json("claude-code", "list", "--all", env=env)
        names = {feature["name"] for feature in listed["features"]}
        self.assertIn("new-schema-toggle", names)

    def test_invalid_cached_claude_schema_falls_back_to_bundled_schema(self) -> None:
        data_home = self.tmp / "labkit-data"
        data_home.mkdir()
        (data_home / "claude-code-settings-schema.json").write_text('{"type":"array"}\n', encoding="utf-8")
        env = self.env.copy()
        env["LABKIT_DATA_HOME"] = str(data_home)

        listed = self.run_json("claude-code", "list", "--all", env=env)
        names = {feature["name"] for feature in listed["features"]}
        self.assertEqual(listed["schema"]["source"], "bundled")
        self.assertIn("include-co-authored-by", names)

    def test_legacy_top_level_codex_alias_still_works_but_is_hidden(self) -> None:
        help_text = self.run_lab("--no-color", "--help").stdout
        self.assertNotIn(" check ", help_text)
        data = self.run_json("list")
        self.assertEqual(data["command"], "codex list")

    def test_readme_deployed_usage_has_no_local_invocation_or_old_placeholder(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("labkit codex select", readme)
        self.assertIn("labkit claude-code select", readme)
        self.assertNotIn("./labkit", readme)
        self.assertNotIn("<feature-name>", readme)
        self.assertNotIn("codex-labkit", readme)


if __name__ == "__main__":
    unittest.main(verbosity=2)
