"""Shared filesystem, process, and parsing helpers."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

from .models import ClaudePaths, CliError, Paths
from .ui import spinner


def die(message: str) -> None:
    raise CliError(message)


def user_data_home() -> Path:
    return Path(os.environ.get("LABKIT_DATA_HOME", "~/.local/share/labkit")).expanduser()


def cached_claude_schema_path() -> Path:
    return user_data_home() / "claude-code-settings-schema.json"


def cached_codex_registry_path() -> Path:
    return user_data_home() / "codex-features.json"


def resolve_paths() -> Paths:
    codex_home = Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()
    return Paths(codex_home, codex_home / "config.toml", codex_home / "model-catalog-1m.json")


def cli_codex_bin() -> Path | None:
    found = shutil.which("codex")
    return Path(found) if found else None


def preferred_codex_bin(explicit: str | None, *, required: bool = True) -> Path | None:
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file() or not os.access(path, os.X_OK):
            die(f"Codex binary is not executable: {path}")
        return path
    env_bin = os.environ.get("CODEX_BIN")
    if env_bin:
        return preferred_codex_bin(env_bin, required=required)
    found_path = cli_codex_bin()
    if not found_path:
        if required:
            die("Could not find Codex CLI on PATH. Pass --codex-bin /path/to/codex.")
        return None
    return found_path


def require_codex_bin(explicit: str | None) -> Path:
    path = preferred_codex_bin(explicit, required=True)
    assert path is not None
    return path


def optional_codex_bin(explicit: str | None) -> Path | None:
    return preferred_codex_bin(explicit, required=False)


def claude_bin() -> Path | None:
    found = shutil.which("claude")
    return Path(found) if found else None


def preferred_claude_bin(explicit: str | None) -> Path:
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file() or not os.access(path, os.X_OK):
            die(f"Claude Code binary is not executable: {path}")
        return path
    env_bin = os.environ.get("CLAUDE_BIN")
    if env_bin:
        return preferred_claude_bin(env_bin)
    found_path = claude_bin()
    if not found_path:
        die("Could not find Claude Code on PATH. Pass --claude-bin /path/to/claude.")
    assert found_path is not None
    return found_path


def resolve_claude_paths(scope: str) -> ClaudePaths:
    home = Path(os.environ.get("CLAUDE_HOME", "~/.claude")).expanduser()
    if scope == "user":
        return ClaudePaths(home=home, settings=home / "settings.json", scope=scope)
    if scope == "project":
        return ClaudePaths(home=home, settings=Path.cwd() / ".claude" / "settings.json", scope=scope)
    if scope == "local":
        return ClaudePaths(home=home, settings=Path.cwd() / ".claude" / "settings.local.json", scope=scope)
    die(f"Unknown Claude settings scope: {scope}")
    raise AssertionError("unreachable")


def run_text(command: list[str], env: dict[str, str] | None = None, label: str | None = None) -> tuple[int, str, str]:
    with spinner(label):
        result = subprocess.run(
            command,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    return result.returncode, result.stdout, result.stderr


def run_json(command: list[str], env: dict[str, str] | None = None, label: str | None = None) -> dict[str, Any]:
    code, stdout, stderr = run_text(command, env=env, label=label)
    if code != 0:
        die(stderr.strip() or stdout.strip() or f"Command failed: {' '.join(command)}")
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as exc:
        die(f"Command did not return JSON: {' '.join(command)}\n{exc}")
    raise AssertionError("unreachable")


def fetch_json_url(url: str) -> dict[str, Any]:
    request = Request(url, headers={"User-Agent": "labkit-cli"})
    try:
        with urlopen(request, timeout=20) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (OSError, URLError, json.JSONDecodeError) as exc:
        die(f"Could not fetch JSON from {url}: {exc}")
    if not isinstance(data, dict):
        die(f"Expected JSON object from {url}")
    return data


def write_cached_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def read_config(paths: Paths) -> str:
    return paths.config.read_text(encoding="utf-8") if paths.config.exists() else ""


def write_config(paths: Paths, text: str) -> None:
    paths.codex_home.mkdir(parents=True, exist_ok=True)
    tmp = paths.config.with_suffix(paths.config.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(paths.config)


def backup_config(paths: Paths) -> Path | None:
    if not paths.config.exists():
        return None
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    backup = paths.config.with_name(f"{paths.config.name}.backup.{stamp}")
    shutil.copy2(paths.config, backup)
    return backup


def read_json_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        die(f"Invalid JSON in {path}: {exc}")
    if not isinstance(data, dict):
        die(f"Expected JSON object in {path}")
    return data


def write_json_file(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def backup_file(path: Path) -> Path | None:
    if not path.exists():
        return None
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    backup = path.with_name(f"{path.name}.backup.{stamp}")
    shutil.copy2(path, backup)
    return backup


def get_nested(data: dict[str, Any], dotted_key: str) -> Any:
    target: Any = data
    for part in dotted_key.split("."):
        if not isinstance(target, dict) or part not in target:
            return None
        target = target[part]
    return target


def value_matches(actual: Any, expected: Any) -> bool:
    if isinstance(expected, str):
        expected_text = expected.strip().lower()
        if isinstance(actual, bool):
            actual_text = "true" if actual else "false"
            if expected_text in {"0", "1"}:
                actual_text = "1" if actual else "0"
        else:
            actual_text = str(actual).strip().lower()
        return actual_text == expected_text
    return actual == expected


def set_nested(data: dict[str, Any], dotted_key: str, value: Any) -> None:
    target: dict[str, Any] = data
    parts = dotted_key.split(".")
    for part in parts[:-1]:
        child = target.get(part)
        if not isinstance(child, dict):
            child = {}
            target[part] = child
        target = child
    target[parts[-1]] = value


def parse_toml(text: str) -> dict[str, Any]:
    try:
        import tomllib  # type: ignore[import-not-found]
    except ModuleNotFoundError:
        try:
            import tomli as tomllib  # type: ignore[import-not-found,no-redef]
        except ModuleNotFoundError:
            tomllib = None  # type: ignore[assignment]
    if tomllib is not None:
        try:
            data = tomllib.loads(text)
            return data if isinstance(data, dict) else {}
        except Exception:
            pass
    return parse_toml_light(text)


def parse_toml_light(text: str) -> dict[str, Any]:
    data: dict[str, Any] = {}
    current: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = [part.strip() for part in line.strip("[]").split(".") if part.strip()]
            continue
        match = re.match(r"([A-Za-z0-9_.-]+)\s*=\s*(.+)$", line)
        if not match:
            continue
        key, raw_value = match.groups()
        target = data
        for part in current:
            child = target.setdefault(part, {})
            if not isinstance(child, dict):
                child = {}
                target[part] = child
            target = child
        target[key] = parse_toml_value(raw_value)
    return data


def parse_toml_value(raw_value: str) -> Any:
    value = raw_value.split("#", 1)[0].strip()
    if value == "true":
        return True
    if value == "false":
        return False
    if re.match(r"^-?\d+$", value):
        return int(value)
    if value.startswith('"') and value.endswith('"'):
        return value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return value


def int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def format_toml_assignment(key: str, value: Any) -> str:
    if isinstance(value, bool):
        rendered = "true" if value else "false"
    elif isinstance(value, int):
        rendered = str(value)
    elif isinstance(value, str):
        rendered = '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    else:
        raise TypeError(f"unsupported value for {key}: {type(value).__name__}")
    return f"{key} = {rendered}\n"


def set_top_level(text: str, values: dict[str, Any]) -> str:
    lines = text.splitlines(keepends=True)
    first_table = next((i for i, line in enumerate(lines) if re.match(r"^\s*\[", line)), len(lines))
    top, rest = lines[:first_table], lines[first_table:]
    schema: list[str] = []
    while top and top[0].startswith("#:schema"):
        schema.append(top.pop(0))
    assignment = re.compile(r"^\s*([A-Za-z0-9_.-]+)\s*=")
    kept = []
    for line in top:
        match = assignment.match(line)
        if not (match and match.group(1) in values):
            kept.append(line)
    block = [format_toml_assignment(key, value) for key, value in values.items()]
    output = schema + block
    if kept and kept[0].strip():
        output.append("\n")
    output.extend(kept)
    if rest and output and output[-1].strip():
        output.append("\n")
    output.extend(rest)
    return "".join(output)


def remove_top_level_keys(text: str, keys: set[str]) -> str:
    lines = text.splitlines(keepends=True)
    first_table = next((i for i, line in enumerate(lines) if re.match(r"^\s*\[", line)), len(lines))
    top, rest = lines[:first_table], lines[first_table:]
    assignment = re.compile(r"^\s*([A-Za-z0-9_.-]+)\s*=")
    kept = []
    for line in top:
        match = assignment.match(line)
        if not (match and match.group(1) in keys):
            kept.append(line)
    return "".join(kept + rest)


def set_table_values(text: str, table: str, values: dict[str, Any]) -> str:
    lines = text.splitlines(keepends=True)
    header_re = re.compile(r"^\s*\[" + re.escape(table) + r"\]\s*(?:#.*)?$")
    any_header_re = re.compile(r"^\s*\[[^\]]+\]\s*(?:#.*)?$")
    start = next((i for i, line in enumerate(lines) if header_re.match(line)), None)
    if start is None:
        prefix = "" if not lines or lines[-1].endswith("\n") else "\n"
        block = [f"{prefix}\n[{table}]\n"]
        block.extend(format_toml_assignment(key, value) for key, value in values.items())
        return "".join(lines + block)
    end = next((i for i in range(start + 1, len(lines)) if any_header_re.match(lines[i])), len(lines))
    assignment = re.compile(r"^\s*([A-Za-z0-9_.-]+)\s*=")
    section = [lines[start]]
    for line in lines[start + 1 : end]:
        match = assignment.match(line)
        if not (match and match.group(1) in values):
            section.append(line)
    while section and not section[-1].strip():
        section.pop()
    section.extend(format_toml_assignment(key, value) for key, value in values.items())
    if end < len(lines) and section and section[-1].strip():
        section.append("\n")
    return "".join(lines[:start] + section + lines[end:])
