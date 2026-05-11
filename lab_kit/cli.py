#!/usr/bin/env python3
"""
Lab Kit CLI

Inspect local Codex CLI and Claude Code capabilities and selectively enable
known controls. Nothing is changed unless the user chooses it.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime
from importlib import metadata as importlib_metadata
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

from lab_kit import __version__

APP_NAME = "Lab Kit CLI"
CLAUDE_SCHEMA_URL = "https://json.schemastore.org/claude-code-settings.json"
TARGET_MODEL = "gpt-5.5"
CATALOG_CONTEXT_WINDOW = 1_052_632
MODEL_CONTEXT_WINDOW = 1_000_000
AUTO_COMPACT_TOKEN_LIMIT = 800_000


@dataclass(frozen=True)
class Feature:
    name: str
    title: str
    cluster: str
    stage: str
    kind: str
    description: str
    key: str | None = None
    value: Any = True
    inactive_value: Any = False
    default_enabled: bool | None = None
    selectable: bool = True
    registry_keys: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class Paths:
    codex_home: Path
    config: Path
    catalog: Path


@dataclass(frozen=True)
class ClaudePaths:
    home: Path
    settings: Path
    scope: str


@dataclass(frozen=True)
class RegistryEntry:
    stage: str
    enabled: bool


@dataclass
class BinaryReport:
    label: str
    path: Path | None
    version: str = ""
    features: dict[str, RegistryEntry] = field(default_factory=dict)
    model_context_window: int | None = None
    model_effective_window: int | None = None
    model_effective_percent: float | None = None
    error: str | None = None


@dataclass(frozen=True)
class RuntimeEvidence:
    timestamp: str
    event_type: str
    window: int
    total_tokens: int | None
    file: Path


@dataclass(frozen=True)
class FeatureChange:
    feature: Feature
    enabled: bool
    current_state: str | None = None
    current_source: str | None = None


class CliError(RuntimeError):
    pass


COLOR_ENABLED = True
JSON_OUTPUT = False
PROGRESS_ENABLED = True
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


class Style:
    RESET = "0"
    BOLD = "1"
    DIM = "2"
    RED = "31"
    GREEN = "32"
    YELLOW = "33"
    BLUE = "34"
    MAGENTA = "35"
    CYAN = "36"
    GRAY = "90"


def color_supported(stream: Any = sys.stdout) -> bool:
    if not COLOR_ENABLED:
        return False
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("CLICOLOR_FORCE") == "1":
        return True
    return bool(getattr(stream, "isatty", lambda: False)()) and os.environ.get("TERM", "dumb") != "dumb"


def paint(text: Any, *codes: str, stream: Any = sys.stdout) -> str:
    rendered = str(text)
    if not codes or not color_supported(stream):
        return rendered
    return f"\033[{';'.join(codes)}m{rendered}\033[0m"


def visible_len(text: str) -> int:
    return len(ANSI_RE.sub("", text))


def pad(text: str, width: int) -> str:
    return text + (" " * max(0, width - visible_len(text)))


def terminal_width(default: int = 96) -> int:
    return max(60, shutil.get_terminal_size((default, 20)).columns)


def muted(text: Any) -> str:
    return paint(text, Style.GRAY)


def strong(text: Any) -> str:
    return paint(text, Style.BOLD)


def accent(text: Any) -> str:
    return paint(text, Style.YELLOW)


def success(text: Any) -> str:
    return paint(text, Style.GREEN)


def warning(text: Any) -> str:
    return paint(text, Style.YELLOW)


def failure(text: Any) -> str:
    return paint(text, Style.RED)


def badge(label: str, tone: str = "info") -> str:
    colors = {
        "ok": Style.GREEN,
        "on": Style.GREEN,
        "warn": Style.YELLOW,
        "partial": Style.YELLOW,
        "fail": Style.RED,
        "off": Style.RED,
        "manual": Style.YELLOW,
        "info": Style.YELLOW,
    }
    return paint(f"[{label}]", colors.get(tone, Style.YELLOW))


def state_badge(state: str) -> str:
    return badge(state, state)


def state_text(state: str) -> str:
    if state == "on":
        return success(state)
    if state == "off":
        return failure(state)
    if state == "partial":
        return warning(state)
    return warning(state)


def banner(subtitle: str | None = None) -> None:
    say(strong(accent(APP_NAME)))
    if subtitle:
        say(muted(subtitle))
    say(muted("-" * min(terminal_width(), 88)))


def section(title: str) -> None:
    say("")
    say(strong(accent(title)))


def kv(label: str, value: Any) -> None:
    say(f"  {muted(pad(label + ':', 24))} {value}")


def status_line(tone: str, title: str, detail: str | None = None) -> None:
    line = f"  {badge(tone, tone)} {strong(title)}"
    if detail:
        line += f" {muted('- ' + detail)}"
    say(line)


def wrap_lines(text: str, indent: str = "      ", extra_indent: str | None = None, width_offset: int = 0) -> list[str]:
    width = max(48, terminal_width() - len(indent) - width_offset)
    return textwrap.wrap(
        text,
        width=width,
        initial_indent=indent,
        subsequent_indent=extra_indent or indent,
        break_long_words=False,
        break_on_hyphens=False,
    )


def say_wrapped(text: str, indent: str = "      ") -> None:
    for line in wrap_lines(text, indent=indent):
        say(line)


def ask(prompt: str, *styles: str) -> str:
    value = input(paint(prompt, *styles))
    if not sys.stdin.isatty():
        say("")
    return value


def spinner_supported() -> bool:
    if not PROGRESS_ENABLED:
        return False
    if JSON_OUTPUT:
        return False
    if os.environ.get("CI"):
        return False
    return bool(getattr(sys.stderr, "isatty", lambda: False)()) and os.environ.get("TERM", "dumb") != "dumb"


@contextmanager
def spinner(label: str | None):
    if not label or not spinner_supported():
        yield
        return

    stop = threading.Event()
    frames = "|/-\\"

    def animate() -> None:
        index = 0
        while not stop.is_set():
            frame = paint(frames[index % len(frames)], Style.YELLOW, stream=sys.stderr)
            sys.stderr.write(f"\r{frame} {label}")
            sys.stderr.flush()
            index += 1
            time.sleep(0.08)
        sys.stderr.write("\r" + (" " * (visible_len(label) + 8)) + "\r")
        sys.stderr.flush()

    thread = threading.Thread(target=animate, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join()


CURATED_FEATURES: list[Feature] = [
    Feature(
        "1m-context",
        "1M Context",
        "Model & Context",
        "local patch",
        "catalog",
        "Configures GPT-5.5 for the large local context profile.",
    ),
    Feature(
        "web-search-live",
        "Live Web Search",
        "Model & Context",
        "config",
        "top",
        "Requests live search instead of cached search behavior.",
        key="web_search",
        value="live",
    ),
    Feature(
        "memories",
        "Memories",
        "Memory & Personalization",
        "experimental",
        "feature",
        "Enables the local memory switch when the current build exposes it.",
        key="memories",
    ),
    Feature(
        "goals",
        "Goals",
        "Workflow",
        "experimental",
        "feature",
        "Tracks a concrete thread objective until the work is complete.",
        key="goals",
    ),
    Feature(
        "codex-hooks",
        "Hooks",
        "Workflow",
        "stable/config",
        "feature",
        "Runs local lifecycle hooks when the current build gates them.",
        key="codex_hooks",
        registry_keys=("codex_hooks", "hooks"),
    ),
    Feature(
        "external-migration",
        "External Migration",
        "Workflow",
        "experimental",
        "feature",
        "Imports external agent session state where supported.",
        key="external_migration",
    ),
    Feature(
        "prevent-idle-sleep",
        "Prevent Idle Sleep",
        "Runtime",
        "experimental",
        "feature",
        "Prevents idle sleep during active work on supported macOS setups.",
        key="prevent_idle_sleep",
    ),
    Feature(
        "terminal-resize-reflow",
        "Terminal Resize Reflow",
        "Runtime",
        "experimental",
        "feature",
        "Reflows terminal output more cleanly after width changes.",
        key="terminal_resize_reflow",
    ),
    Feature(
        "apps",
        "Connectors",
        "Tools & Integrations",
        "stable",
        "feature",
        "Shows connector-backed tools and app integrations when available.",
        key="apps",
    ),
    Feature(
        "plugins",
        "Plugins",
        "Tools & Integrations",
        "stable",
        "feature",
        "Lets installed plugin bundles contribute skills, tools, and integrations.",
        key="plugins",
    ),
    Feature(
        "tool-suggest",
        "Tool Suggest",
        "Tools & Integrations",
        "stable",
        "feature",
        "Surfaces relevant tools or connectors when a task appears to need them.",
        key="tool_suggest",
    ),
    Feature(
        "browser-use",
        "Browser Use",
        "Tools & Integrations",
        "stable",
        "feature",
        "Lets agents inspect pages, click flows, and capture UI evidence.",
        key="browser_use",
    ),
    Feature(
        "computer-use",
        "Computer Use",
        "Tools & Integrations",
        "stable",
        "feature",
        "Lets agents drive supported desktop surfaces under the normal permission model.",
        key="computer_use",
    ),
    Feature(
        "multi-agent",
        "Multi-Agent",
        "Agent Runtime",
        "stable",
        "feature",
        "Lets a main agent delegate bounded work to helper agents.",
        key="multi_agent",
    ),
    Feature(
        "fast-mode",
        "Fast Mode",
        "Agent Runtime",
        "stable",
        "feature",
        "Uses lower-latency routing where available.",
        key="fast_mode",
    ),
    Feature(
        "personality",
        "Personality",
        "Agent Runtime",
        "stable",
        "feature",
        "Enables interaction-style controls exposed by the current build.",
        key="personality",
    ),
    Feature(
        "request-compression",
        "Request Compression",
        "Agent Runtime",
        "stable",
        "feature",
        "Reduces payload size for larger and tool-heavy turns.",
        key="enable_request_compression",
    ),
    Feature(
        "shell-snapshot",
        "Shell Snapshot",
        "Agent Runtime",
        "stable",
        "feature",
        "Preserves more accurate terminal context across tool calls.",
        key="shell_snapshot",
    ),
    Feature(
        "shell-tool",
        "Shell Tool",
        "Agent Runtime",
        "stable",
        "feature",
        "Keeps shell execution available when gated by a feature flag.",
        key="shell_tool",
    ),
    Feature(
        "unified-exec",
        "Unified Exec",
        "Agent Runtime",
        "stable",
        "feature",
        "Uses the newer command-execution path for terminal sessions.",
        key="unified_exec",
    ),
]

FEATURE_BY_NAME = {feature.name: feature for feature in CURATED_FEATURES}


def package_version() -> str:
    try:
        return importlib_metadata.version("labkit-cli")
    except importlib_metadata.PackageNotFoundError:
        return __version__


def package_json(filename: str) -> dict[str, Any]:
    try:
        text = resources.files("lab_kit").joinpath("data", filename).read_text(encoding="utf-8")
        data = json.loads(text)
    except (FileNotFoundError, json.JSONDecodeError, ModuleNotFoundError):
        return {}
    return data if isinstance(data, dict) else {}


def user_data_home() -> Path:
    return Path(os.environ.get("LABKIT_DATA_HOME", "~/.local/share/labkit")).expanduser()


def cached_claude_schema_path() -> Path:
    return user_data_home() / "claude-code-settings-schema.json"


def cached_codex_registry_path() -> Path:
    return user_data_home() / "codex-features.json"


def load_codex_metadata() -> dict[str, Any]:
    return package_json("codex_feature_metadata.json")


def codex_metadata_entry(metadata: dict[str, Any], key_or_name: str) -> dict[str, Any]:
    features = metadata.get("features")
    if not isinstance(features, dict):
        return {}
    entry = features.get(key_or_name)
    if isinstance(entry, dict):
        return entry
    for value in features.values():
        if not isinstance(value, dict):
            continue
        aliases = value.get("aliases") if isinstance(value.get("aliases"), list) else []
        if value.get("name") == key_or_name or key_or_name in aliases:
            return value
    return {}


def control_id_from_key(key: str) -> str:
    text = key.replace(".", "-").replace("_", "-")
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", text)
    return text.lower()


def title_from_id(name: str) -> str:
    return " ".join(part.upper() if part in {"ui", "api", "mcp"} else part.capitalize() for part in re.split(r"[-_.]+", name))


def apply_codex_metadata(feature: Feature, metadata: dict[str, Any]) -> Feature:
    entry = codex_metadata_entry(metadata, feature.key or feature.name) or codex_metadata_entry(metadata, feature.name)
    if not entry:
        return feature
    aliases = entry.get("aliases") if isinstance(entry.get("aliases"), list) else []
    registry_keys = tuple(str(item) for item in aliases)
    if feature.key:
        registry_keys = (feature.key, *registry_keys)
    registry_keys = (*feature.registry_keys, *tuple(key for key in registry_keys if key not in feature.registry_keys))
    return replace(
        feature,
        name=str(entry.get("name") or feature.name),
        title=str(entry.get("title") or feature.title),
        cluster=str(entry.get("cluster") or feature.cluster),
        description=str(entry.get("description") or feature.description),
        registry_keys=tuple(key for key in registry_keys if key),
    )


def codex_curated_features() -> list[Feature]:
    metadata = load_codex_metadata()
    return [apply_codex_metadata(feature, metadata) for feature in CURATED_FEATURES]


def dynamic_codex_feature(name: str, entry: RegistryEntry, metadata: dict[str, Any]) -> Feature:
    meta = codex_metadata_entry(metadata, name) or codex_metadata_entry(metadata, control_id_from_key(name))
    control_id = str(meta.get("name") or control_id_from_key(name))
    return Feature(
        control_id,
        str(meta.get("title") or title_from_id(control_id)),
        str(meta.get("cluster") or "Available Codex Flags"),
        entry.stage,
        "feature",
        str(meta.get("description") or f"Available in your installed Codex CLI feature registry as `{name}`."),
        key=name,
        selectable=entry.stage not in {"removed", "deprecated"},
        registry_keys=(name,),
    )


def codex_features_from_registry(registry: dict[str, RegistryEntry], *, include_all: bool = False) -> list[Feature]:
    metadata = load_codex_metadata()
    curated = codex_curated_features()
    recommended = metadata.get("recommended")
    recommended_names = set(str(item) for item in recommended) if isinstance(recommended, list) else {feature.name for feature in curated}

    features: list[Feature] = []
    represented_registry_keys: set[str] = set()
    for feature in curated:
        keys = set(feature.registry_keys)
        if feature.key:
            keys.add(feature.key)
        represented_registry_keys.update(keys)
        if include_all or feature.name in recommended_names:
            features.append(feature)

    if include_all:
        for registry_name, entry in sorted(registry.items()):
            if entry.stage in {"removed", "deprecated"}:
                continue
            if registry_name in represented_registry_keys:
                continue
            features.append(dynamic_codex_feature(registry_name, entry, metadata))

    return features


def codex_feature_lookup(name: str, registry: dict[str, RegistryEntry]) -> Feature | None:
    for feature in codex_features_from_registry(registry, include_all=True):
        names = {feature.name}
        if feature.key:
            names.add(feature.key)
            names.add(control_id_from_key(feature.key))
        names.update(feature.registry_keys)
        names.update(control_id_from_key(key) for key in feature.registry_keys)
        if name in names:
            return feature
    return None


def claude_setting(
    name: str,
    title: str,
    cluster: str,
    description: str,
    key: str,
    *,
    value: Any = True,
    inactive_value: Any = False,
    default_enabled: bool | None = None,
    kind: str = "claude_bool",
) -> Feature:
    return Feature(
        name,
        title,
        cluster,
        "settings",
        kind,
        description,
        key=key,
        value=value,
        inactive_value=inactive_value,
        default_enabled=default_enabled,
    )


def claude_env(
    name: str,
    title: str,
    cluster: str,
    description: str,
    var: str,
    *,
    value: str = "1",
    inactive_value: str = "0",
    default_enabled: bool | None = False,
) -> Feature:
    return Feature(
        name,
        title,
        cluster,
        "env",
        "claude_env",
        description,
        key=f"env.{var}",
        value=value,
        inactive_value=inactive_value,
        default_enabled=default_enabled,
    )


def claude_disable_env(
    name: str,
    title: str,
    cluster: str,
    description: str,
    var: str,
    *,
    default_enabled: bool | None = True,
) -> Feature:
    return claude_env(
        name,
        title,
        cluster,
        description,
        var,
        value="0",
        inactive_value="1",
        default_enabled=default_enabled,
    )


def claude_manual(name: str, title: str, cluster: str, description: str) -> Feature:
    return Feature(
        name,
        title,
        cluster,
        "session flag",
        "manual",
        description,
        selectable=False,
    )


CLAUDE_FEATURES: list[Feature] = [
    claude_setting(
        "auto-memory",
        "Auto Memory",
        "Memory & Context",
        "Lets Claude Code read and write project-specific memory between sessions. Disable it when you want each session to start without local memory carryover.",
        "autoMemoryEnabled",
        default_enabled=True,
    ),
    claude_env(
        "1m-context",
        "1M Context Models",
        "Memory & Context",
        "Keeps extended-context model variants visible when the account and model are entitled to them. Turning it inactive writes the documented opt-out switch.",
        "CLAUDE_CODE_DISABLE_1M_CONTEXT",
        value="0",
        inactive_value="1",
        default_enabled=True,
    ),
    claude_setting(
        "thinking",
        "Thinking By Default",
        "Memory & Context",
        "Requests extended-thinking behavior by default where the active model and Claude Code build support it.",
        "alwaysThinkingEnabled",
        default_enabled=False,
    ),
    claude_disable_env(
        "extended-thinking",
        "Extended Thinking",
        "Memory & Context",
        "Keeps extended thinking available for models and turns that support explicit reasoning budgets.",
        "CLAUDE_CODE_DISABLE_THINKING",
    ),
    claude_disable_env(
        "interleaved-thinking",
        "Interleaved Thinking",
        "Memory & Context",
        "Keeps interleaved thinking available where tool-use and reasoning can alternate inside a turn.",
        "DISABLE_INTERLEAVED_THINKING",
    ),
    claude_disable_env(
        "adaptive-thinking",
        "Adaptive Thinking",
        "Memory & Context",
        "Lets supported models choose a dynamic reasoning budget instead of forcing a fixed thinking-token budget.",
        "CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING",
    ),
    claude_setting(
        "thinking-summaries",
        "Thinking Summaries",
        "Memory & Context",
        "Shows summaries of extended-thinking blocks in interactive sessions instead of only a collapsed redaction stub.",
        "showThinkingSummaries",
        default_enabled=False,
    ),
    claude_disable_env(
        "auto-compact",
        "Auto Compact",
        "Memory & Context",
        "Keeps automatic context compaction available near the context limit. Manual `/compact` remains separate.",
        "DISABLE_AUTO_COMPACT",
    ),
    claude_disable_env(
        "compact",
        "Compaction",
        "Memory & Context",
        "Keeps both automatic compaction and the manual `/compact` command available.",
        "DISABLE_COMPACT",
    ),
    claude_env(
        "prompt-cache-1h",
        "One-Hour Prompt Cache",
        "Memory & Context",
        "Requests the one-hour prompt-cache TTL for API-key, Bedrock, Vertex, and Foundry usage. Subscription users may already receive it automatically.",
        "ENABLE_PROMPT_CACHING_1H",
    ),
    claude_disable_env(
        "prompt-caching",
        "Prompt Caching",
        "Memory & Context",
        "Keeps prompt caching enabled across models so repeated shared context can be reused.",
        "DISABLE_PROMPT_CACHING",
    ),
    claude_env(
        "prompt-cache-1h-bedrock",
        "Bedrock One-Hour Prompt Cache",
        "Memory & Context",
        "Requests the one-hour prompt-cache TTL for Bedrock usage when the backend and model support it.",
        "ENABLE_PROMPT_CACHING_1H_BEDROCK",
    ),
    claude_disable_env(
        "haiku-prompt-caching",
        "Haiku Prompt Caching",
        "Memory & Context",
        "Keeps prompt caching enabled for Haiku-family models.",
        "DISABLE_PROMPT_CACHING_HAIKU",
    ),
    claude_disable_env(
        "sonnet-prompt-caching",
        "Sonnet Prompt Caching",
        "Memory & Context",
        "Keeps prompt caching enabled for Sonnet-family models.",
        "DISABLE_PROMPT_CACHING_SONNET",
    ),
    claude_disable_env(
        "opus-prompt-caching",
        "Opus Prompt Caching",
        "Memory & Context",
        "Keeps prompt caching enabled for Opus-family models.",
        "DISABLE_PROMPT_CACHING_OPUS",
    ),
    claude_disable_env(
        "claude-md-memory",
        "CLAUDE.md Memory",
        "Memory & Context",
        "Keeps user, project, local, and auto-memory `CLAUDE.md` files eligible to load into session context.",
        "CLAUDE_CODE_DISABLE_CLAUDE_MDS",
    ),
    claude_env(
        "additional-dir-memory",
        "Additional Directory Memory",
        "Memory & Context",
        "Loads memory files from directories added with `--add-dir`, so extra workspaces can contribute their own instructions.",
        "CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD",
    ),
    claude_env(
        "attribution-header",
        "Attribution Header",
        "Memory & Context",
        "Includes Claude Code attribution in the system prompt when downstream tools need to identify generated behavior.",
        "CLAUDE_CODE_ATTRIBUTION_HEADER",
    ),
    claude_disable_env(
        "attachments",
        "Attachments",
        "Tools & Files",
        "Keeps `@` file mentions and other attachments expanding into content instead of being passed as plain text.",
        "CLAUDE_CODE_DISABLE_ATTACHMENTS",
    ),
    claude_setting(
        "respect-gitignore",
        "Respect Gitignore",
        "Tools & Files",
        "Keeps ignored files out of `@` file suggestions, matching the repository's `.gitignore` behavior.",
        "respectGitignore",
        default_enabled=True,
    ),
    claude_env(
        "glob-hidden",
        "Glob Hidden Files",
        "Tools & Files",
        "Allows the Glob tool to include dotfiles and hidden files in its results.",
        "CLAUDE_CODE_GLOB_HIDDEN",
        value="true",
        inactive_value="false",
        default_enabled=True,
    ),
    claude_env(
        "glob-ignored-files",
        "Glob Ignored Files",
        "Tools & Files",
        "Allows the Glob tool to include files ignored by `.gitignore`. `@` autocomplete has its own setting.",
        "CLAUDE_CODE_GLOB_NO_IGNORE",
        value="true",
        inactive_value="false",
        default_enabled=True,
    ),
    claude_disable_env(
        "file-checkpointing",
        "File Checkpointing",
        "Tools & Files",
        "Keeps file checkpointing available so `/rewind` can restore code changes after edits.",
        "CLAUDE_CODE_DISABLE_FILE_CHECKPOINTING",
    ),
    claude_env(
        "bundled-ripgrep",
        "Bundled Ripgrep",
        "Tools & Files",
        "Uses Claude Code's bundled `rg` for file discovery instead of requiring a system `rg` binary.",
        "USE_BUILTIN_RIPGREP",
        default_enabled=True,
    ),
    claude_env(
        "native-file-search",
        "Native File Search",
        "Tools & Files",
        "Uses Node.js file APIs for discovering commands, subagents, and output styles when bundled ripgrep is unavailable or blocked.",
        "CLAUDE_CODE_USE_NATIVE_FILE_SEARCH",
    ),
    claude_setting(
        "hooks",
        "Hooks",
        "Automation",
        "Keeps configured command, HTTP, MCP, prompt, and agent hooks active at their lifecycle events.",
        "disableAllHooks",
        value=False,
        inactive_value=True,
        default_enabled=True,
    ),
    claude_setting(
        "skill-shell-execution",
        "Skill Shell Execution",
        "Automation",
        "Allows shell blocks embedded in skills and custom commands to run when those sources are trusted.",
        "disableSkillShellExecution",
        value=False,
        inactive_value=True,
        default_enabled=True,
    ),
    claude_disable_env(
        "scheduled-tasks",
        "Scheduled Tasks",
        "Automation",
        "Keeps scheduled tasks and cron-style Claude Code task execution available.",
        "CLAUDE_CODE_DISABLE_CRON",
    ),
    claude_env(
        "tasks-print-mode",
        "Tasks In Print Mode",
        "Automation",
        "Enables task tracking in non-interactive `claude -p` sessions.",
        "CLAUDE_CODE_ENABLE_TASKS",
    ),
    claude_env(
        "resume-interrupted-turn",
        "Resume Interrupted Turn",
        "Automation",
        "Lets SDK-style sessions continue a turn that ended mid-response without resending the prompt.",
        "CLAUDE_CODE_RESUME_INTERRUPTED_TURN",
    ),
    claude_env(
        "background-plugin-refresh",
        "Background Plugin Refresh",
        "Automation",
        "Refreshes plugin state at turn boundaries after a background install completes in non-interactive mode.",
        "CLAUDE_CODE_ENABLE_BACKGROUND_PLUGIN_REFRESH",
    ),
    claude_env(
        "sync-plugin-install",
        "Synchronous Plugin Install",
        "Automation",
        "Makes non-interactive sessions wait for plugin installation before the first query so plugin tools are available immediately.",
        "CLAUDE_CODE_SYNC_PLUGIN_INSTALL",
    ),
    claude_env(
        "auto-background-tasks",
        "Auto Background Tasks",
        "Agent Runtime",
        "Forces long-running agent tasks into the background when supported, reducing how often the main session blocks.",
        "CLAUDE_AUTO_BACKGROUND_TASKS",
    ),
    claude_disable_env(
        "background-tasks",
        "Background Tasks",
        "Agent Runtime",
        "Keeps background task support, the `run_in_background` parameter, and the Ctrl+B background shortcut available.",
        "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS",
    ),
    claude_env(
        "agent-teams",
        "Agent Teams",
        "Agent Runtime",
        "Enables the experimental team-of-agents runtime when the installed Claude Code build, model, and account support it.",
        "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS",
    ),
    claude_env(
        "fork-subagents",
        "Fork Subagents",
        "Agent Runtime",
        "Enables forked subagents that inherit the current conversation instead of starting from a fresh isolated prompt.",
        "CLAUDE_CODE_FORK_SUBAGENT",
    ),
    claude_env(
        "sdk-builtins",
        "SDK Built-In Agents",
        "Agent Runtime",
        "Keeps built-in Agent SDK subagent types available. Turning it inactive gives SDK callers a blank slate.",
        "CLAUDE_AGENT_SDK_DISABLE_BUILTIN_AGENTS",
        value="0",
        inactive_value="1",
        default_enabled=True,
    ),
    claude_env(
        "sdk-mcp-prefix",
        "SDK MCP Tool Prefix",
        "Agent Runtime",
        "Keeps the `mcp__server__tool` prefix on SDK-created MCP tool names to avoid collisions.",
        "CLAUDE_AGENT_SDK_MCP_NO_PREFIX",
        value="0",
        inactive_value="1",
        default_enabled=True,
    ),
    claude_env(
        "shell-keeps-cwd",
        "Shell Keeps Project CWD",
        "Agent Runtime",
        "Returns Bash and PowerShell tool calls to the original project directory after each command.",
        "CLAUDE_BASH_MAINTAIN_PROJECT_WORKING_DIR",
    ),
    claude_env(
        "simple-mode",
        "Simple Mode",
        "Agent Runtime",
        "Starts Claude Code with a minimal system prompt and only the core file and shell tools, similar to `--bare`.",
        "CLAUDE_CODE_SIMPLE",
    ),
    claude_env(
        "simple-system-prompt",
        "Simple System Prompt",
        "Agent Runtime",
        "Uses a shorter system prompt and abbreviated tool descriptions on models that support the mode.",
        "CLAUDE_CODE_SIMPLE_SYSTEM_PROMPT",
    ),
    claude_env(
        "subprocess-env-scrub",
        "Subprocess Env Scrub",
        "Safety",
        "Strips Anthropic and cloud-provider credentials from Bash, hook, and MCP subprocess environments.",
        "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB",
    ),
    claude_env(
        "mcp-env-allowlist",
        "MCP Env Allowlist",
        "Safety",
        "Starts stdio MCP servers with a small safe environment plus the server's configured env instead of inheriting the full shell.",
        "CLAUDE_CODE_MCP_ALLOWLIST_ENV",
    ),
    claude_setting(
        "sandbox",
        "Bash Sandbox",
        "Safety",
        "Enables Claude Code's bash sandbox where supported, isolating shell commands according to sandbox policy.",
        "sandbox.enabled",
        default_enabled=False,
    ),
    claude_setting(
        "sandbox-hard-fail",
        "Sandbox Hard Fail",
        "Safety",
        "Requires the sandbox to start successfully when sandboxing is enabled, rather than silently running unsandboxed.",
        "sandbox.failIfUnavailable",
        default_enabled=False,
    ),
    claude_setting(
        "sandbox-auto-allow-bash",
        "Auto-Allow Sandboxed Bash",
        "Safety",
        "Allows sandboxed bash commands to run without extra approval when Claude Code considers the sandbox active and effective.",
        "sandbox.autoAllowBashIfSandboxed",
        default_enabled=True,
    ),
    claude_env(
        "perforce-mode",
        "Perforce Mode",
        "Safety",
        "Protects Perforce-managed files by requiring `p4 edit` before Claude Code writes files without owner-write permission.",
        "CLAUDE_CODE_PERFORCE_MODE",
    ),
    claude_setting(
        "remote-control",
        "Remote Control",
        "Connectivity",
        "Keeps Claude Code remote-control sessions available when the account, build, and policy allow them.",
        "disableRemoteControl",
        value=False,
        inactive_value=True,
        default_enabled=True,
    ),
    claude_disable_env(
        "growthbook-feature-gates",
        "Remote Feature Gates",
        "Connectivity",
        "Allows Claude Code to fetch remote feature-gate defaults. Disabling it makes the client use code defaults, which can hide account-gated features.",
        "DISABLE_GROWTHBOOK",
    ),
    claude_disable_env(
        "nonessential-traffic",
        "Nonessential Traffic",
        "Connectivity",
        "Keeps update checks, telemetry, feedback, and remote feature-gate traffic available. Turn inactive for stricter offline/privacy behavior.",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
    ),
    claude_env(
        "proxy-resolves-hosts",
        "Proxy Resolves Hosts",
        "Connectivity",
        "Lets a configured proxy perform DNS resolution instead of resolving hosts locally.",
        "CLAUDE_CODE_PROXY_RESOLVES_HOSTS",
    ),
    claude_env(
        "force-remote-bundle",
        "Force Remote Bundle",
        "Connectivity",
        "Forces `claude --remote` to bundle and upload the local repository even when GitHub access is available.",
        "CCR_FORCE_BUNDLE",
    ),
    claude_env(
        "bedrock",
        "Bedrock Provider",
        "Connectivity",
        "Routes Claude Code through Amazon Bedrock instead of the default Anthropic endpoint.",
        "CLAUDE_CODE_USE_BEDROCK",
    ),
    claude_env(
        "vertex",
        "Vertex Provider",
        "Connectivity",
        "Routes Claude Code through Google Vertex AI instead of the default Anthropic endpoint.",
        "CLAUDE_CODE_USE_VERTEX",
    ),
    claude_env(
        "foundry",
        "Foundry Provider",
        "Connectivity",
        "Routes Claude Code through Microsoft Foundry instead of the default Anthropic endpoint.",
        "CLAUDE_CODE_USE_FOUNDRY",
    ),
    claude_env(
        "mantle",
        "Mantle Provider",
        "Connectivity",
        "Routes Claude Code through the Bedrock Mantle endpoint.",
        "CLAUDE_CODE_USE_MANTLE",
    ),
    claude_env(
        "bedrock-auth",
        "Bedrock Auth",
        "Connectivity",
        "Runs the AWS authentication flow for Bedrock before provider calls when needed.",
        "CLAUDE_CODE_SKIP_BEDROCK_AUTH",
        value="0",
        inactive_value="1",
        default_enabled=True,
    ),
    claude_env(
        "vertex-auth",
        "Vertex Auth",
        "Connectivity",
        "Runs the Google authentication flow for Vertex AI before provider calls when needed.",
        "CLAUDE_CODE_SKIP_VERTEX_AUTH",
        value="0",
        inactive_value="1",
        default_enabled=True,
    ),
    claude_env(
        "foundry-auth",
        "Foundry Auth",
        "Connectivity",
        "Runs the Azure authentication flow for Foundry before provider calls when needed.",
        "CLAUDE_CODE_SKIP_FOUNDRY_AUTH",
        value="0",
        inactive_value="1",
        default_enabled=True,
    ),
    claude_env(
        "mantle-auth",
        "Mantle Auth",
        "Connectivity",
        "Runs the AWS authentication flow for Mantle before provider calls when needed.",
        "CLAUDE_CODE_SKIP_MANTLE_AUTH",
        value="0",
        inactive_value="1",
        default_enabled=True,
    ),
    claude_env(
        "claudeai-mcp-servers",
        "Claude.ai MCP Servers",
        "Tools & Integrations",
        "Keeps Claude.ai MCP servers available for logged-in Claude Code users.",
        "ENABLE_CLAUDEAI_MCP_SERVERS",
        value="true",
        inactive_value="false",
        default_enabled=True,
    ),
    claude_disable_env(
        "official-marketplace-autoinstall",
        "Official Marketplace Autoinstall",
        "Tools & Integrations",
        "Allows Claude Code to automatically add the official plugin marketplace on first run.",
        "CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL",
    ),
    claude_env(
        "plugin-cache-keep-on-failure",
        "Keep Plugin Cache On Failure",
        "Tools & Integrations",
        "Keeps an existing plugin marketplace cache when update fails, useful for offline or airgapped environments.",
        "CLAUDE_CODE_PLUGIN_KEEP_MARKETPLACE_ON_FAILURE",
    ),
    claude_env(
        "powershell-tool",
        "PowerShell Tool",
        "Tools & Integrations",
        "Enables Claude Code's PowerShell tool on platforms where PowerShell is available and supported.",
        "CLAUDE_CODE_USE_POWERSHELL_TOOL",
    ),
    claude_env(
        "gateway-model-discovery",
        "Gateway Model Discovery",
        "Tools & Integrations",
        "Populates the model picker from a compatible gateway's `/v1/models` endpoint when using `ANTHROPIC_BASE_URL`.",
        "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY",
    ),
    claude_env(
        "tool-search",
        "MCP Tool Search",
        "Tools & Integrations",
        "Defers MCP tool loading through tool search where supported, reducing upfront context pressure from large tool sets.",
        "ENABLE_TOOL_SEARCH",
        value="true",
        inactive_value="false",
        default_enabled=True,
    ),
    claude_setting(
        "away-summary",
        "Away Summary",
        "Session UX",
        "Shows a one-line recap when you return to the terminal after stepping away from an active session.",
        "awaySummaryEnabled",
        default_enabled=True,
    ),
    claude_setting(
        "auto-scroll",
        "Auto Scroll",
        "Session UX",
        "Keeps the fullscreen terminal UI following new output automatically during active sessions.",
        "autoScrollEnabled",
        default_enabled=True,
    ),
    claude_setting(
        "turn-duration",
        "Turn Duration",
        "Session UX",
        "Shows short duration messages after Claude Code responses, making long-running turns easier to scan.",
        "showTurnDuration",
        default_enabled=True,
    ),
    claude_setting(
        "terminal-progress-bar",
        "Terminal Progress Bar",
        "Session UX",
        "Shows the terminal progress bar in supported terminals such as newer iTerm2, Ghostty, and ConEmu.",
        "terminalProgressBarEnabled",
        default_enabled=True,
    ),
    claude_setting(
        "fullscreen-renderer",
        "Fullscreen Renderer",
        "Session UX",
        "Uses Claude Code's fullscreen renderer for smoother alt-screen rendering and virtualized scrollback.",
        "tui",
        value="fullscreen",
        inactive_value="default",
        default_enabled=False,
        kind="claude_value",
    ),
    claude_env(
        "no-flicker",
        "No Flicker Renderer",
        "Session UX",
        "Enables the fullscreen rendering path through the environment switch used by older and preview builds.",
        "CLAUDE_CODE_NO_FLICKER",
    ),
    claude_disable_env(
        "alternate-screen",
        "Alternate Screen",
        "Session UX",
        "Keeps fullscreen alt-screen rendering available instead of forcing the classic main-screen renderer.",
        "CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN",
    ),
    claude_disable_env(
        "virtual-scroll",
        "Virtual Scroll",
        "Session UX",
        "Keeps virtualized scrollback enabled in fullscreen mode to keep long conversations responsive.",
        "CLAUDE_CODE_DISABLE_VIRTUAL_SCROLL",
    ),
    claude_disable_env(
        "mouse",
        "Mouse Tracking",
        "Session UX",
        "Keeps mouse tracking active in fullscreen mode for scroll and pointer interactions.",
        "CLAUDE_CODE_DISABLE_MOUSE",
    ),
    claude_env(
        "native-cursor",
        "Native Cursor",
        "Session UX",
        "Shows the terminal's native cursor at the prompt instead of a drawn block cursor.",
        "CLAUDE_CODE_NATIVE_CURSOR",
    ),
    claude_env(
        "accessibility-cursor",
        "Accessibility Cursor",
        "Session UX",
        "Keeps the native terminal cursor visible for screen magnifiers and assistive tooling.",
        "CLAUDE_CODE_ACCESSIBILITY",
    ),
    claude_setting(
        "reduced-motion",
        "Reduced Motion",
        "Session UX",
        "Reduces or disables motion-heavy terminal UI effects such as spinners, shimmer, and flashes.",
        "prefersReducedMotion",
        default_enabled=False,
    ),
    claude_env(
        "prompt-suggestions",
        "Prompt Suggestions",
        "Session UX",
        "Keeps grayed-out prompt suggestions available after Claude responds.",
        "CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION",
        value="true",
        inactive_value="false",
        default_enabled=True,
    ),
    claude_setting(
        "clear-context-on-plan",
        "Clear Context On Plan",
        "Session UX",
        "Restores the clear-context option on the plan accept screen.",
        "showClearContextOnPlanAccept",
        default_enabled=False,
    ),
    claude_env(
        "hide-cwd",
        "Hide Current Directory",
        "Session UX",
        "Hides the current working directory from the startup logo for screenshares and recordings.",
        "CLAUDE_CODE_HIDE_CWD",
    ),
    claude_disable_env(
        "terminal-title",
        "Terminal Title",
        "Session UX",
        "Keeps automatic terminal title updates based on conversation context.",
        "CLAUDE_CODE_DISABLE_TERMINAL_TITLE",
    ),
    claude_env(
        "tmux-truecolor",
        "Tmux Truecolor",
        "Session UX",
        "Allows 24-bit truecolor output inside tmux when the terminal and tmux configuration support it.",
        "CLAUDE_CODE_TMUX_TRUECOLOR",
    ),
    claude_env(
        "new-init",
        "Interactive Init",
        "Project Setup",
        "Makes `/init` run an interactive setup flow before writing project files.",
        "CLAUDE_CODE_NEW_INIT",
    ),
    claude_disable_env(
        "git-instructions",
        "Git Instructions",
        "Project Setup",
        "Keeps built-in commit and pull-request workflow instructions in Claude Code's system prompt.",
        "CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS",
    ),
    claude_disable_env(
        "policy-skills",
        "Policy Skills",
        "Project Setup",
        "Loads skills from the system-wide managed skills directory when present.",
        "CLAUDE_CODE_DISABLE_POLICY_SKILLS",
    ),
    claude_env(
        "ide-auto-connect",
        "IDE Auto Connect",
        "IDE",
        "Controls automatic IDE connection attempts when Claude Code starts from a supported IDE terminal.",
        "CLAUDE_CODE_AUTO_CONNECT_IDE",
        value="true",
        inactive_value="false",
        default_enabled=False,
    ),
    claude_env(
        "ide-auto-install",
        "IDE Auto Install",
        "IDE",
        "Keeps automatic IDE extension installation enabled when Claude Code detects a supported IDE terminal.",
        "CLAUDE_CODE_IDE_SKIP_AUTO_INSTALL",
        value="0",
        inactive_value="1",
        default_enabled=True,
    ),
    claude_env(
        "ide-lockfile-validation",
        "IDE Lockfile Validation",
        "IDE",
        "Keeps IDE lockfile validation enabled before Claude Code connects to an IDE extension.",
        "CLAUDE_CODE_IDE_SKIP_VALID_CHECK",
        value="0",
        inactive_value="1",
        default_enabled=True,
    ),
    claude_disable_env(
        "fast-mode",
        "Fast Mode",
        "Model & Output",
        "Keeps the fast-mode toggle available for lower-latency responses where supported.",
        "CLAUDE_CODE_DISABLE_FAST_MODE",
    ),
    claude_setting(
        "fast-mode-per-session",
        "Fast Mode Per Session",
        "Model & Output",
        "Requires users to opt into fast mode each session instead of persisting the preference across sessions.",
        "fastModePerSessionOptIn",
        default_enabled=False,
    ),
    claude_env(
        "fine-grained-tool-streaming",
        "Fine-Grained Tool Streaming",
        "Model & Output",
        "Streams large tool inputs as Claude generates them, reducing the apparent hang before big tool calls.",
        "CLAUDE_CODE_ENABLE_FINE_GRAINED_TOOL_STREAMING",
        default_enabled=True,
    ),
    claude_disable_env(
        "streaming-fallback",
        "Streaming Fallback",
        "Model & Output",
        "Keeps non-streaming fallback available when a streaming request fails mid-stream.",
        "CLAUDE_CODE_DISABLE_NONSTREAMING_FALLBACK",
    ),
    claude_disable_env(
        "legacy-model-remap",
        "Legacy Model Remap",
        "Model & Output",
        "Allows older model aliases to remap to current versions on the Anthropic API.",
        "CLAUDE_CODE_DISABLE_LEGACY_MODEL_REMAP",
    ),
    claude_env(
        "syntax-highlighting",
        "Syntax Highlighting",
        "Model & Output",
        "Keeps syntax highlighting enabled for diffs and code output.",
        "CLAUDE_CODE_SYNTAX_HIGHLIGHT",
        value="true",
        inactive_value="false",
        default_enabled=True,
    ),
    claude_disable_env(
        "experimental-betas",
        "Experimental Betas",
        "Model & Output",
        "Keeps Claude Code beta headers available on requests that opt into preview behavior.",
        "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS",
    ),
    claude_setting(
        "stable-updates",
        "Stable Updates",
        "Maintenance",
        "Pins auto-updates to the stable channel instead of the faster-moving latest channel.",
        "autoUpdatesChannel",
        value="stable",
        inactive_value="latest",
        default_enabled=False,
        kind="claude_value",
    ),
    claude_disable_env(
        "auto-updates",
        "Auto Updates",
        "Maintenance",
        "Keeps background update checks available. Manual update commands are controlled separately.",
        "DISABLE_AUTOUPDATER",
    ),
    claude_disable_env(
        "all-updates",
        "All Updates",
        "Maintenance",
        "Keeps both automatic and manual update paths available.",
        "DISABLE_UPDATES",
    ),
    claude_env(
        "package-manager-auto-update",
        "Package Manager Auto Update",
        "Maintenance",
        "Allows Homebrew and WinGet installations to run package-manager upgrades in the background when supported.",
        "CLAUDE_CODE_PACKAGE_MANAGER_AUTO_UPDATE",
    ),
    claude_disable_env(
        "cost-warnings",
        "Cost Warnings",
        "Maintenance",
        "Keeps cost warning messages visible.",
        "DISABLE_COST_WARNINGS",
    ),
    claude_disable_env(
        "doctor-command",
        "Doctor Command",
        "Maintenance",
        "Keeps the `/doctor` diagnostics command available.",
        "DISABLE_DOCTOR_COMMAND",
    ),
    claude_disable_env(
        "bug-command",
        "Bug Command",
        "Maintenance",
        "Keeps the `/bug` command available for issue reporting flows.",
        "DISABLE_BUG_COMMAND",
    ),
    claude_disable_env(
        "installation-checks",
        "Installation Checks",
        "Maintenance",
        "Keeps startup checks that warn about broken or unsupported Claude Code installations.",
        "DISABLE_INSTALLATION_CHECKS",
    ),
    claude_disable_env(
        "install-github-app-command",
        "Install GitHub App Command",
        "Maintenance",
        "Keeps the `/install-github-app` command visible where supported.",
        "DISABLE_INSTALL_GITHUB_APP_COMMAND",
    ),
    claude_disable_env(
        "extra-usage-command",
        "Extra Usage Command",
        "Maintenance",
        "Keeps the `/extra-usage` command visible for eligible users.",
        "DISABLE_EXTRA_USAGE_COMMAND",
    ),
    claude_disable_env(
        "feedback-command",
        "Feedback Command",
        "Privacy & Diagnostics",
        "Keeps the in-product feedback command available.",
        "DISABLE_FEEDBACK_COMMAND",
    ),
    claude_disable_env(
        "feedback-survey",
        "Feedback Survey",
        "Privacy & Diagnostics",
        "Keeps end-of-session feedback surveys available.",
        "CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY",
    ),
    claude_disable_env(
        "prompt-history",
        "Prompt History",
        "Privacy & Diagnostics",
        "Keeps local transcript writes enabled. Turning it inactive prevents Claude Code from writing prompt history.",
        "CLAUDE_CODE_SKIP_PROMPT_HISTORY",
    ),
    claude_disable_env(
        "error-reporting",
        "Error Reporting",
        "Privacy & Diagnostics",
        "Keeps Sentry-style error reporting available for crashes and diagnostics.",
        "DISABLE_ERROR_REPORTING",
    ),
    claude_disable_env(
        "telemetry",
        "Telemetry",
        "Privacy & Diagnostics",
        "Keeps telemetry enabled. Turning it inactive opts out of telemetry and can affect remote experiment-gate behavior.",
        "DISABLE_TELEMETRY",
    ),
    claude_env(
        "otel-telemetry",
        "OpenTelemetry Export",
        "Privacy & Diagnostics",
        "Enables OpenTelemetry collection so metrics and logs can be exported when OTel exporters are configured.",
        "CLAUDE_CODE_ENABLE_TELEMETRY",
    ),
    claude_disable_env(
        "login-command",
        "Login Command",
        "Account",
        "Keeps `/login` available for interactive authentication.",
        "DISABLE_LOGIN_COMMAND",
    ),
    claude_disable_env(
        "logout-command",
        "Logout Command",
        "Account",
        "Keeps `/logout` available for interactive sign-out.",
        "DISABLE_LOGOUT_COMMAND",
    ),
    claude_disable_env(
        "upgrade-command",
        "Upgrade Command",
        "Account",
        "Keeps `/upgrade` available for plan upgrade flows.",
        "DISABLE_UPGRADE_COMMAND",
    ),
    claude_manual(
        "chrome-session",
        "Chrome Integration",
        "Session Flags",
        "Session-only feature: start Claude Code with `claude --chrome` to enable Chrome browser integration for web automation and testing.",
    ),
    claude_manual(
        "bare-mode",
        "Bare Mode",
        "Session Flags",
        "Session-only feature: start Claude Code with `claude --bare -p` for faster scripted calls that skip hooks, plugin sync, memory discovery, and CLAUDE.md discovery.",
    ),
    claude_manual(
        "remote-control-session",
        "Remote Control Session",
        "Session Flags",
        "Session-only feature: start Claude Code with `claude --remote-control` or `claude remote-control` to control the session from Claude surfaces when eligible.",
    ),
    claude_manual(
        "worktree-session",
        "Worktree Session",
        "Session Flags",
        "Session-only feature: start Claude Code with `claude --worktree <name>` to isolate work in a generated git worktree.",
    ),
]

CLAUDE_COPY_OVERRIDES: dict[str, tuple[str, str]] = {
    "auto-memory": ("Auto Memory", "Reads and writes project memory between sessions."),
    "1m-context": ("1M Context", "Shows extended-context models when your account and model support them."),
    "thinking": ("Thinking by Default", "Starts supported sessions with extended thinking enabled."),
    "extended-thinking": ("Extended Thinking", "Keeps explicit reasoning-budget support available."),
    "interleaved-thinking": ("Interleaved Thinking", "Allows reasoning and tool use to alternate inside a turn."),
    "adaptive-thinking": ("Adaptive Thinking", "Lets supported models choose their own reasoning budget."),
    "thinking-summaries": ("Thinking Summaries", "Shows short summaries for extended-thinking blocks."),
    "auto-compact": ("Auto Compact", "Compacts context automatically near the model limit."),
    "compact": ("Compaction", "Keeps both automatic compaction and `/compact` available."),
    "prompt-cache-1h": ("1h Prompt Cache", "Requests a one-hour prompt cache TTL when supported."),
    "prompt-caching": ("Prompt Caching", "Reuses stable shared context across turns."),
    "prompt-cache-1h-bedrock": ("1h Bedrock Cache", "Requests one-hour prompt caching for Bedrock routes."),
    "haiku-prompt-caching": ("Haiku Cache", "Keeps prompt caching enabled for Haiku models."),
    "sonnet-prompt-caching": ("Sonnet Cache", "Keeps prompt caching enabled for Sonnet models."),
    "opus-prompt-caching": ("Opus Cache", "Keeps prompt caching enabled for Opus models."),
    "claude-md-memory": ("CLAUDE.md Memory", "Loads user, project, local, and auto-memory files."),
    "additional-dir-memory": ("Extra Directory Memory", "Loads memory files from directories added with `--add-dir`."),
    "attribution-header": ("Attribution Header", "Adds Claude Code attribution metadata to the system prompt."),
    "attachments": ("Attachments", "Expands `@` file mentions and attachments into content."),
    "respect-gitignore": ("Respect Gitignore", "Keeps ignored files out of `@` file suggestions."),
    "glob-hidden": ("Glob Hidden Files", "Includes dotfiles and hidden files in Glob results."),
    "glob-ignored-files": ("Glob Ignored Files", "Includes `.gitignore`-ignored files in Glob results."),
    "file-checkpointing": ("File Checkpoints", "Keeps edit checkpoints available for `/rewind`."),
    "bundled-ripgrep": ("Bundled Ripgrep", "Uses Claude Code's bundled `rg` for file discovery."),
    "native-file-search": ("Native File Search", "Uses Node.js file search when ripgrep is unavailable."),
    "hooks": ("Hooks", "Runs configured lifecycle hooks."),
    "skill-shell-execution": ("Skill Shell Blocks", "Allows trusted skills and commands to run inline shell blocks."),
    "scheduled-tasks": ("Scheduled Tasks", "Keeps cron-style tasks and scheduled runs available."),
    "tasks-print-mode": ("Print-Mode Tasks", "Enables task tracking in non-interactive `claude -p` runs."),
    "resume-interrupted-turn": ("Resume Interrupted Turns", "Continues a turn that stopped mid-response."),
    "background-plugin-refresh": ("Background Plugin Refresh", "Refreshes plugin state between non-interactive turns."),
    "sync-plugin-install": ("Sync Plugin Install", "Waits for plugin installation before the first query."),
    "auto-background-tasks": ("Auto-Background Tasks", "Moves long-running tasks into the background when supported."),
    "background-tasks": ("Background Tasks", "Keeps background task support and Ctrl+B available."),
    "agent-teams": ("Agent Teams", "Enables the experimental team-of-agents runtime."),
    "fork-subagents": ("Forked Subagents", "Lets subagents inherit the current conversation context."),
    "sdk-builtins": ("SDK Built-In Agents", "Keeps built-in Agent SDK subagent types available."),
    "sdk-mcp-prefix": ("SDK MCP Prefixes", "Keeps `mcp__server__tool` prefixes to avoid tool-name collisions."),
    "shell-keeps-cwd": ("Shell Keeps CWD", "Returns shell tools to the original project directory after each command."),
    "simple-mode": ("Simple Mode", "Starts with a smaller prompt and core tools only."),
    "simple-system-prompt": ("Simple System Prompt", "Uses shorter system and tool instructions where supported."),
    "subprocess-env-scrub": ("Subprocess Env Scrub", "Removes provider credentials from child process environments."),
    "mcp-env-allowlist": ("MCP Env Allowlist", "Starts MCP servers with a smaller approved environment."),
    "sandbox": ("Bash Sandbox", "Runs shell commands through Claude Code's sandbox where supported."),
    "sandbox-hard-fail": ("Sandbox Hard Fail", "Stops instead of running unsandboxed when sandbox startup fails."),
    "sandbox-auto-allow-bash": ("Auto-Allow Sandboxed Bash", "Skips extra approval for commands inside an active sandbox."),
    "perforce-mode": ("Perforce Mode", "Requires `p4 edit` before writing protected Perforce files."),
    "remote-control": ("Remote Control", "Keeps Claude Code remote-control sessions available."),
    "growthbook-feature-gates": ("Remote Feature Gates", "Fetches remote feature-gate defaults instead of using local defaults only."),
    "nonessential-traffic": ("Nonessential Traffic", "Keeps update, telemetry, feedback, and gate-fetch traffic available."),
    "proxy-resolves-hosts": ("Proxy DNS", "Lets the configured proxy resolve hostnames."),
    "force-remote-bundle": ("Force Remote Bundle", "Forces `claude --remote` to upload a local repo bundle."),
    "bedrock": ("Bedrock Provider", "Routes model calls through Amazon Bedrock."),
    "vertex": ("Vertex Provider", "Routes model calls through Google Vertex AI."),
    "foundry": ("Foundry Provider", "Routes model calls through Microsoft Foundry."),
    "mantle": ("Mantle Provider", "Routes model calls through Bedrock Mantle."),
    "bedrock-auth": ("Bedrock Auth", "Runs AWS auth setup for Bedrock routes."),
    "vertex-auth": ("Vertex Auth", "Runs Google auth setup for Vertex routes."),
    "foundry-auth": ("Foundry Auth", "Runs Azure auth setup for Foundry routes."),
    "mantle-auth": ("Mantle Auth", "Runs AWS auth setup for Mantle routes."),
    "claudeai-mcp-servers": ("Claude.ai MCP Servers", "Keeps Claude.ai MCP servers available for logged-in users."),
    "official-marketplace-autoinstall": ("Marketplace Autoinstall", "Allows automatic setup of the official plugin marketplace."),
    "plugin-cache-keep-on-failure": ("Keep Plugin Cache", "Keeps the existing plugin cache when refresh fails."),
    "powershell-tool": ("PowerShell Tool", "Enables PowerShell tooling where supported."),
    "gateway-model-discovery": ("Gateway Model Discovery", "Loads model-picker entries from compatible gateway endpoints."),
    "tool-search": ("MCP Tool Search", "Defers MCP tool loading to reduce context pressure."),
    "away-summary": ("Away Summary", "Shows a short recap when you return to the terminal."),
    "auto-scroll": ("Auto Scroll", "Keeps the fullscreen UI pinned to new output."),
    "turn-duration": ("Turn Duration", "Shows how long each response took."),
    "terminal-progress-bar": ("Terminal Progress Bar", "Shows progress in supported terminal integrations."),
    "fullscreen-renderer": ("Fullscreen Renderer", "Uses Claude Code's smoother fullscreen renderer."),
    "no-flicker": ("No-Flicker Renderer", "Uses the newer fullscreen path for smoother rendering."),
    "alternate-screen": ("Alternate Screen", "Keeps alternate-screen fullscreen rendering available."),
    "virtual-scroll": ("Virtual Scroll", "Keeps long conversations responsive in fullscreen mode."),
    "mouse": ("Mouse Tracking", "Keeps mouse scrolling and pointer interactions enabled."),
    "native-cursor": ("Native Cursor", "Shows the terminal's native cursor at the prompt."),
    "accessibility-cursor": ("Accessibility Cursor", "Keeps the native cursor visible for assistive tools."),
    "reduced-motion": ("Reduced Motion", "Reduces motion-heavy terminal effects."),
    "prompt-suggestions": ("Prompt Suggestions", "Shows gray prompt suggestions after Claude responds."),
    "clear-context-on-plan": ("Clear Context on Plan", "Shows a clear-context option when accepting a plan."),
    "hide-cwd": ("Hide Current Directory", "Hides the working path in the startup logo."),
    "terminal-title": ("Terminal Title", "Keeps automatic terminal title updates enabled."),
    "tmux-truecolor": ("Tmux Truecolor", "Allows 24-bit color inside tmux."),
    "new-init": ("Interactive Init", "Uses the interactive `/init` setup flow."),
    "git-instructions": ("Git Instructions", "Keeps built-in commit and pull-request guidance in context."),
    "policy-skills": ("Policy Skills", "Loads system-managed policy skills when present."),
    "ide-auto-connect": ("IDE Auto Connect", "Connects automatically from supported IDE terminals."),
    "ide-auto-install": ("IDE Auto Install", "Installs supported IDE extensions automatically."),
    "ide-lockfile-validation": ("IDE Lockfile Validation", "Validates IDE lockfiles before connecting."),
    "fast-mode": ("Fast Mode", "Keeps the low-latency fast-mode toggle available."),
    "fast-mode-per-session": ("Fast Mode per Session", "Requires fast mode to be re-enabled each session."),
    "fine-grained-tool-streaming": ("Tool Input Streaming", "Streams large tool inputs as Claude generates them."),
    "streaming-fallback": ("Streaming Fallback", "Falls back to non-streaming mode after stream failures."),
    "legacy-model-remap": ("Legacy Model Remap", "Maps older model aliases to current versions when allowed."),
    "syntax-highlighting": ("Syntax Highlighting", "Keeps syntax highlighting for diffs and code output."),
    "experimental-betas": ("Experimental Betas", "Keeps Claude Code beta request headers available."),
    "stable-updates": ("Stable Updates", "Pins auto-updates to the stable release channel."),
    "auto-updates": ("Auto Updates", "Keeps background update checks available."),
    "all-updates": ("All Updates", "Keeps both automatic and manual update paths available."),
    "package-manager-auto-update": ("Package-Manager Updates", "Allows supported package-manager background upgrades."),
    "cost-warnings": ("Cost Warnings", "Keeps cost warnings visible."),
    "doctor-command": ("Doctor Command", "Keeps `/doctor` diagnostics available."),
    "bug-command": ("Bug Command", "Keeps `/bug` issue reporting available."),
    "installation-checks": ("Installation Checks", "Keeps startup installation warnings enabled."),
    "install-github-app-command": ("GitHub App Command", "Keeps `/install-github-app` visible where supported."),
    "extra-usage-command": ("Extra Usage Command", "Keeps `/extra-usage` visible for eligible users."),
    "feedback-command": ("Feedback Command", "Keeps the in-product feedback command available."),
    "feedback-survey": ("Feedback Survey", "Keeps end-of-session quality surveys available."),
    "prompt-history": ("Prompt History", "Keeps local prompt history and transcripts enabled."),
    "error-reporting": ("Error Reporting", "Keeps crash and diagnostic error reporting enabled."),
    "telemetry": ("Telemetry", "Keeps telemetry enabled."),
    "otel-telemetry": ("OpenTelemetry Export", "Exports metrics and logs when OTel exporters are configured."),
    "login-command": ("Login Command", "Keeps `/login` available."),
    "logout-command": ("Logout Command", "Keeps `/logout` available."),
    "upgrade-command": ("Upgrade Command", "Keeps `/upgrade` available."),
    "chrome-session": ("Chrome Session", "Start with `claude --chrome` for Chrome integration."),
    "bare-mode": ("Bare Mode", "Start with `claude --bare -p` for lean scripted runs."),
    "remote-control-session": ("Remote-Control Session", "Start with `claude --remote-control` for remote control."),
    "worktree-session": ("Worktree Session", "Start with `claude --worktree <name>` for isolated work."),
}


def apply_copy_overrides(features: list[Feature], overrides: dict[str, tuple[str, str]]) -> list[Feature]:
    polished: list[Feature] = []
    for feature in features:
        copy = overrides.get(feature.name)
        if copy:
            title, description = copy
            polished.append(replace(feature, title=title, description=description))
        else:
            polished.append(feature)
    return polished


CLAUDE_FEATURES = apply_copy_overrides(CLAUDE_FEATURES, CLAUDE_COPY_OVERRIDES)
CLAUDE_FEATURE_BY_NAME = {feature.name: feature for feature in CLAUDE_FEATURES}


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


def load_claude_schema() -> tuple[dict[str, Any], str]:
    cache = cached_claude_schema_path()
    if cache.exists():
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data, str(cache)
        except json.JSONDecodeError:
            pass
    bundled = package_json("claude-code-settings-schema.json")
    if bundled:
        return bundled, "bundled"
    return {}, "unavailable"


def write_cached_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def schema_properties(schema: dict[str, Any]) -> dict[str, Any]:
    properties = schema.get("properties")
    return properties if isinstance(properties, dict) else {}


def schema_env_properties(schema: dict[str, Any]) -> dict[str, Any]:
    env = schema_properties(schema).get("env")
    if not isinstance(env, dict):
        return {}
    properties = env.get("properties")
    return properties if isinstance(properties, dict) else {}


def claude_known_keys(features: list[Feature]) -> set[str]:
    return {feature.key for feature in features if feature.key}


def schema_default_enabled(spec: dict[str, Any], active: Any) -> bool | None:
    if "default" not in spec:
        return None
    return value_matches(spec.get("default"), active)


def schema_env_values(spec: dict[str, Any]) -> tuple[str, str] | None:
    enum = spec.get("enum")
    values = {str(item) for item in enum} if isinstance(enum, list) else set()
    if {"0", "1"}.issubset(values):
        return "1", "0"
    if {"true", "false"}.issubset({value.lower() for value in values}):
        return "true", "false"
    return None


def schema_feature_for_key(key: str, spec: dict[str, Any], *, env: bool = False) -> Feature:
    name = control_id_from_key(key)
    title = title_from_id(name.replace("claude-code-", "").replace("claude-", ""))
    description = str(spec.get("description") or f"Claude Code setting `{key}` discovered from the official JSON Schema.")
    if env:
        values = schema_env_values(spec)
        selectable = values is not None
        value, inactive = values if values else ("1", "0")
        return Feature(
            name,
            title,
            "Schema: Environment",
            "schema",
            "claude_env",
            description,
            key=f"env.{key}",
            value=value,
            inactive_value=inactive,
            default_enabled=schema_default_enabled(spec, value),
            selectable=selectable,
        )

    setting_type = spec.get("type")
    selectable = setting_type == "boolean"
    value: Any = True if selectable else spec.get("default")
    inactive: Any = False if selectable else None
    return Feature(
        name,
        title,
        "Schema: Settings",
        "schema",
        "claude_bool" if selectable else "schema_setting",
        description,
        key=key,
        value=value,
        inactive_value=inactive,
        default_enabled=schema_default_enabled(spec, value) if selectable else None,
        selectable=selectable,
    )


def schema_features(schema: dict[str, Any], curated: list[Feature]) -> list[Feature]:
    known = claude_known_keys(curated)
    generated: list[Feature] = []
    for key, spec in sorted(schema_properties(schema).items()):
        if key in {"$schema", "env"} or key in known or not isinstance(spec, dict):
            continue
        generated.append(schema_feature_for_key(key, spec))
    for key, spec in sorted(schema_env_properties(schema).items()):
        dotted = f"env.{key}"
        if dotted in known or not isinstance(spec, dict):
            continue
        generated.append(schema_feature_for_key(key, spec, env=True))
    return generated


def flatten_settings_keys(data: dict[str, Any], prefix: str = "") -> set[str]:
    keys: set[str] = set()
    for key, value in data.items():
        dotted = f"{prefix}.{key}" if prefix else str(key)
        keys.add(dotted)
        if isinstance(value, dict):
            keys.update(flatten_settings_keys(value, dotted))
    return keys


def settings_only_features(settings: dict[str, Any], known_features: list[Feature]) -> list[Feature]:
    known = claude_known_keys(known_features)
    features: list[Feature] = []
    for key in sorted(flatten_settings_keys(settings)):
        if key in known or key == "$schema":
            continue
        if any(known_key.startswith(f"{key}.") for known_key in known):
            continue
        actual = get_nested(settings, key)
        name = control_id_from_key(key.replace(".", "-"))
        selectable = isinstance(actual, bool)
        features.append(
            Feature(
                name,
                title_from_id(name),
                "Settings File",
                "settings",
                "claude_bool" if selectable else "observed_setting",
                f"Present in the selected Claude Code settings file as `{key}` but not curated by Lab Kit.",
                key=key,
                value=True if selectable else actual,
                inactive_value=False if selectable else None,
                selectable=selectable,
            )
        )
    return features


def claude_features(settings: dict[str, Any] | None = None, *, include_all: bool = False) -> list[Feature]:
    if not include_all:
        return CLAUDE_FEATURES
    schema, _source = load_claude_schema()
    features = [*CLAUDE_FEATURES, *schema_features(schema, CLAUDE_FEATURES)]
    if settings is not None:
        features.extend(settings_only_features(settings, features))
    seen: set[str] = set()
    unique: list[Feature] = []
    for feature in features:
        if feature.name in seen:
            continue
        seen.add(feature.name)
        unique.append(feature)
    return unique


def claude_feature_lookup(name: str, settings: dict[str, Any]) -> Feature | None:
    for feature in claude_features(settings, include_all=True):
        names = {feature.name}
        if feature.key:
            names.add(feature.key)
            names.add(control_id_from_key(feature.key.replace(".", "-")))
        if name in names:
            return feature
    return None


def claude_known_settings_data(settings: dict[str, Any], features: list[Feature]) -> list[dict[str, Any]]:
    rows = []
    state_getter = lambda feature: claude_feature_state(feature, settings)
    for item in feature_catalog_data_for(features, state_getter):
        rows.append(item)
    return rows


def say(message: str = "") -> None:
    print(message)


def emit_json(data: Any) -> None:
    print(json.dumps(data, indent=2, sort_keys=True))


def die(message: str) -> None:
    raise CliError(message)


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
    path = cli_codex_bin()
    if not path:
        if required:
            die("Could not find Codex CLI on PATH. Pass --codex-bin /path/to/codex.")
        return None
    return path


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
    path = claude_bin()
    if not path:
        die("Could not find Claude Code on PATH. Pass --claude-bin /path/to/claude.")
    return path


def resolve_claude_paths(scope: str) -> ClaudePaths:
    home = Path(os.environ.get("CLAUDE_HOME", "~/.claude")).expanduser()
    if scope == "user":
        return ClaudePaths(home=home, settings=home / "settings.json", scope=scope)
    if scope == "project":
        return ClaudePaths(home=home, settings=Path.cwd() / ".claude" / "settings.json", scope=scope)
    if scope == "local":
        return ClaudePaths(home=home, settings=Path.cwd() / ".claude" / "settings.local.json", scope=scope)
    die(f"Unknown Claude settings scope: {scope}")


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


def parse_features_list(text: str) -> dict[str, RegistryEntry]:
    entries: dict[str, RegistryEntry] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        name = parts[0]
        enabled_text = parts[-1].lower()
        if enabled_text not in {"true", "false"}:
            continue
        stage = " ".join(parts[1:-1])
        entries[name] = RegistryEntry(stage=stage, enabled=enabled_text == "true")
    return entries


def inspect_binary(label: str, path: Path | None) -> BinaryReport:
    report = BinaryReport(label=label, path=path)
    if not path:
        report.error = "not found"
        return report

    code, stdout, stderr = run_text([str(path), "--version"], label=f"Inspecting {label} version")
    report.version = (stdout or stderr).strip()
    if code != 0:
        report.error = stderr.strip() or stdout.strip() or "version check failed"
        return report

    code, stdout, stderr = run_text([str(path), "features", "list"], label=f"Reading {label} feature registry")
    if code == 0:
        report.features = parse_features_list(stdout)
    else:
        report.error = stderr.strip() or stdout.strip() or "features list failed"

    code, stdout, stderr = run_text([str(path), "debug", "models"], label=f"Reading {label} model catalog")
    if code == 0:
        try:
            catalog = json.loads(stdout)
            model = find_model(catalog)
            if model:
                context = int(model.get("context_window", 0))
                percent = float(model.get("effective_context_window_percent", 100))
                report.model_context_window = context
                report.model_effective_window = int(context * percent / 100)
                report.model_effective_percent = percent
        except (ValueError, TypeError, json.JSONDecodeError):
            pass
    elif not report.error:
        report.error = stderr.strip() or stdout.strip() or "debug models failed"

    return report


def inspect_claude_binary(path: Path | None) -> dict[str, Any]:
    report: dict[str, Any] = {
        "label": "Claude Code",
        "path": str(path) if path else None,
        "version": None,
        "ok": bool(path),
        "error": None,
    }
    if not path:
        report["error"] = "not found"
        return report
    code, stdout, stderr = run_text([str(path), "--version"], label="Inspecting Claude Code version")
    if code == 0:
        report["version"] = (stdout or stderr).strip()
    else:
        report["ok"] = False
        report["error"] = stderr.strip() or stdout.strip() or "version check failed"
    return report


def find_model(catalog: dict[str, Any]) -> dict[str, Any] | None:
    models = catalog.get("models")
    if not isinstance(models, list):
        return None
    return next((item for item in models if model_slug(item) == TARGET_MODEL), None)


def model_slug(model: dict[str, Any]) -> str | None:
    return model.get("slug") or model.get("id") or model.get("name")


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
            target = target.setdefault(part, {})
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
    kept = [line for line in top if not (assignment.match(line) and assignment.match(line).group(1) in values)]
    block = [format_toml_assignment(key, value) for key, value in values.items()]
    output = schema + block
    if kept and kept[0].strip():
        output.append("\n")
    output.extend(kept)
    if rest and output and output[-1].strip():
        output.append("\n")
    output.extend(rest)
    return "".join(output)


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


def registry_lookup(feature: Feature, registry: dict[str, RegistryEntry]) -> RegistryEntry | None:
    keys = feature.registry_keys or ((feature.key,) if feature.key else ())
    return next((registry[key] for key in keys if key in registry), None)


def feature_state(feature: Feature, config: dict[str, Any], registry: dict[str, RegistryEntry]) -> tuple[str, str]:
    features = config.get("features", {}) if isinstance(config.get("features"), dict) else {}
    if feature.kind == "manual":
        entry = registry_lookup(feature, registry)
        state = "on" if entry and entry.enabled else "manual"
        return state, entry.stage if entry else feature.stage
    if feature.kind == "feature" and feature.key:
        if feature.key in features:
            return ("on" if features.get(feature.key) else "off"), "config"
        entry = registry_lookup(feature, registry)
        if entry:
            return ("on" if entry.enabled else "off"), f"default/{entry.stage}"
        return "off", "not listed"
    if feature.kind == "top" and feature.key:
        return ("on" if config.get(feature.key) == feature.value else "off"), "config"
    if feature.kind == "catalog":
        window = int_or_none(config.get("model_context_window"))
        catalog_path = config.get("model_catalog_json")
        catalog_ok = isinstance(catalog_path, str) and Path(catalog_path).expanduser().exists()
        if window and window >= MODEL_CONTEXT_WINDOW and catalog_ok:
            return "on", "config"
        if window and window >= MODEL_CONTEXT_WINDOW:
            return "partial", "window without catalog"
        return "off", "config"
    return "off", feature.stage


def claude_feature_state(feature: Feature, settings: dict[str, Any]) -> tuple[str, str]:
    if feature.kind == "manual":
        return "manual", feature.stage
    if not feature.key:
        return "off", "not configurable"
    actual = get_nested(settings, feature.key)
    if feature.kind in {"schema_setting", "observed_setting"}:
        source = "settings env" if feature.key.startswith("env.") else "settings"
        return ("on" if actual is not None else "off"), source if actual is not None else "schema"
    if actual is None and feature.default_enabled is not None:
        return ("on" if feature.default_enabled else "off"), "default"
    source = "settings env" if feature.key.startswith("env.") else "settings"
    return ("on" if value_matches(actual, feature.value) else "off"), source


def apply_claude_feature(feature: Feature, settings: dict[str, Any], enabled: bool) -> None:
    if feature.kind == "manual":
        die(f"{feature.name} is reference-only and cannot be changed by this CLI.")
    if not feature.key:
        die(f"{feature.name} has no Claude Code settings key.")
    set_nested(settings, feature.key, feature.value if enabled else feature.inactive_value)


def patch_gpt55_context(paths: Paths, codex_bin: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="codex-home.") as tmp_home:
        env = os.environ.copy()
        env["CODEX_HOME"] = tmp_home
        catalog = run_json([str(codex_bin), "debug", "models"], env=env, label="Building GPT-5.5 local model catalog")

    model = find_model(catalog)
    if not model:
        die(f"Could not find {TARGET_MODEL} in the Codex model catalog.")
    model["context_window"] = CATALOG_CONTEXT_WINDOW
    model["max_context_window"] = CATALOG_CONTEXT_WINDOW

    paths.codex_home.mkdir(parents=True, exist_ok=True)
    tmp = paths.catalog.with_suffix(paths.catalog.suffix + ".tmp")
    tmp.write_text(json.dumps(catalog, indent=2) + "\n", encoding="utf-8")
    tmp.replace(paths.catalog)

    config = set_top_level(
        read_config(paths),
        {
            "model": TARGET_MODEL,
            "model_catalog_json": str(paths.catalog.resolve()),
            "model_context_window": MODEL_CONTEXT_WINDOW,
            "model_auto_compact_token_limit": AUTO_COMPACT_TOKEN_LIMIT,
        },
    )
    write_config(paths, config)


def apply_feature(feature: Feature, paths: Paths, codex_bin: Path | None, enabled: bool) -> None:
    if feature.kind == "manual":
        die(f"{feature.name} is reference-only and cannot be changed by this CLI.")
    if feature.kind == "catalog":
        if not enabled:
            die("Disabling 1m-context is not automated. Restore a backup or remove model_catalog_json manually.")
        if codex_bin is None:
            die("1m-context needs a Codex CLI binary so Lab Kit can read the model catalog.")
        patch_gpt55_context(paths, codex_bin)
        return
    if feature.kind == "top" and feature.key:
        value = feature.value if enabled else "cached"
        write_config(paths, set_top_level(read_config(paths), {feature.key: value}))
        return
    if feature.kind == "feature" and feature.key:
        write_config(paths, set_table_values(read_config(paths), "features", {feature.key: enabled}))
        return
    die(f"Cannot apply feature: {feature.name}")


FEATURE_NAME_WIDTH = 32
FEATURE_SOURCE_WIDTH = 22


def feature_table_header(numbered: bool) -> None:
    prefix = "#" if numbered else ""
    columns = (
        muted(pad(prefix, 4)),
        muted(pad("control", FEATURE_NAME_WIDTH)),
        muted(pad("state", 8)),
        muted(pad("source", FEATURE_SOURCE_WIDTH)),
        muted("type"),
    )
    say(f"  {columns[0]}  {columns[1]} {columns[2]} {columns[3]} {columns[4]}")


def print_rows(rows: list[tuple[str, ...]], indent: str = "  ") -> None:
    widths = [max(visible_len(row[i]) for row in rows) for i in range(len(rows[0]))]
    for row in rows:
        say(indent + "  ".join(pad(value, widths[i]) for i, value in enumerate(row)).rstrip())


def grouped_features(features: list[Feature] | None = None) -> dict[str, list[Feature]]:
    groups: dict[str, list[Feature]] = {}
    for feature in features or CURATED_FEATURES:
        groups.setdefault(feature.cluster, []).append(feature)
    return groups


def feature_line_prefix(number: int | None, feature: Feature) -> str:
    if number is None:
        return " " * 4
    if feature.selectable:
        return f"{number:>3}."
    return "  --"


def render_feature(feature: Feature, state: str, source: str, number: int | None = None) -> None:
    prefix = feature_line_prefix(number, feature)
    name = pad(strong(feature.title), FEATURE_NAME_WIDTH)
    state_label = pad(state_badge(state), 8)
    source_label = muted(pad(source, FEATURE_SOURCE_WIDTH))
    type_label = muted(feature.stage)
    say(f"  {muted(prefix)}  {name} {state_label} {source_label} {type_label}")
    say_wrapped(f"{feature.name}: {feature.description}", indent="        ")


def render_feature_catalog(
    config: dict[str, Any],
    registry: dict[str, RegistryEntry],
    numbered: bool = False,
    features: list[Feature] | None = None,
    state_getter: Any | None = None,
) -> None:
    number = 1
    state_getter = state_getter or (lambda feature: feature_state(feature, config, registry))
    for cluster, group in grouped_features(features).items():
        section(cluster)
        feature_table_header(numbered)
        for feature in group:
            state, source = state_getter(feature)
            shown_number = number if numbered else None
            render_feature(feature, state, source, shown_number)
            if numbered and feature.selectable:
                number += 1


def render_binary_report(report: BinaryReport, configured_window: int | None) -> None:
    tone = "ok" if report.path and not report.error else "warn"
    status_line(tone, report.label, str(report.path or "not found"))
    if report.version:
        kv("version", report.version)
    if report.features:
        kv("feature registry", f"{len(report.features)} entries")
    if report.model_effective_window:
        kv(f"{TARGET_MODEL} catalog", f"{report.model_context_window:,} tokens")
        kv(f"{TARGET_MODEL} effective", f"{report.model_effective_window:,} tokens")
        if configured_window and report.model_context_window and report.model_effective_percent:
            visible_base = min(configured_window, report.model_context_window)
            visible = int(visible_base * report.model_effective_percent / 100)
            kv(f"{TARGET_MODEL} UI estimate", f"{visible:,} usable / {configured_window:,} configured")
    if report.error:
        kv("note", warning(report.error))


def binary_report_data(report: BinaryReport, configured_window: int | None = None) -> dict[str, Any]:
    data: dict[str, Any] = {
        "label": report.label,
        "path": str(report.path) if report.path else None,
        "version": report.version or None,
        "feature_count": len(report.features),
        "error": report.error,
        "ok": bool(report.path and not report.error),
        "model": {
            "slug": TARGET_MODEL,
            "catalog_context_window": report.model_context_window,
            "effective_context_window": report.model_effective_window,
            "effective_context_percent": report.model_effective_percent,
        },
    }
    if configured_window and report.model_context_window and report.model_effective_percent:
        visible_base = min(configured_window, report.model_context_window)
        data["model"]["configured_ui_estimate"] = int(visible_base * report.model_effective_percent / 100)
        data["model"]["configured_window"] = configured_window
    return data


def feature_data(feature: Feature, config: dict[str, Any], registry: dict[str, RegistryEntry], index: int | None = None) -> dict[str, Any]:
    state, source = feature_state(feature, config, registry)
    return {
        "index": index,
        "name": feature.name,
        "title": feature.title,
        "cluster": feature.cluster,
        "stage": feature.stage,
        "kind": feature.kind,
        "selectable": feature.selectable,
        "status": state,
        "source": source,
        "description": feature.description,
        "key": feature.key,
        "value": feature.value,
    }


def feature_catalog_data(
    config: dict[str, Any],
    registry: dict[str, RegistryEntry],
    features: list[Feature] | None = None,
) -> list[dict[str, Any]]:
    data: list[dict[str, Any]] = []
    index = 1
    for feature in features or codex_features_from_registry(registry):
        item_index = index if feature.selectable else None
        data.append(feature_data(feature, config, registry, item_index))
        if feature.selectable:
            index += 1
    return data


def feature_catalog_data_for(features: list[Feature], state_getter: Any) -> list[dict[str, Any]]:
    data: list[dict[str, Any]] = []
    index = 1
    for feature in features:
        state, source = state_getter(feature)
        data.append(
            {
                "index": index if feature.selectable else None,
                "name": feature.name,
                "title": feature.title,
                "cluster": feature.cluster,
                "stage": feature.stage,
                "kind": feature.kind,
                "selectable": feature.selectable,
                "status": state,
                "source": source,
                "description": feature.description,
                "key": feature.key,
                "value": feature.value,
                "inactive_value": feature.inactive_value,
            }
        )
        if feature.selectable:
            index += 1
    return data


def registry_data(registry: dict[str, RegistryEntry], include_all: bool = False) -> list[dict[str, Any]]:
    rows = []
    for name, entry in sorted(registry.items()):
        if not include_all and entry.stage in {"removed", "deprecated"}:
            continue
        rows.append({"name": name, "stage": entry.stage, "enabled": entry.enabled})
    return rows


def registry_from_data(rows: Any) -> dict[str, RegistryEntry]:
    registry: dict[str, RegistryEntry] = {}
    if not isinstance(rows, list):
        return registry
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = row.get("name")
        stage = row.get("stage")
        enabled = row.get("enabled")
        if isinstance(name, str) and isinstance(stage, str) and isinstance(enabled, bool):
            registry[name] = RegistryEntry(stage=stage, enabled=enabled)
    return registry


def load_cached_codex_registry() -> dict[str, RegistryEntry]:
    path = cached_codex_registry_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    return registry_from_data(data.get("features"))


def codex_registry_with_cache(report: BinaryReport) -> dict[str, RegistryEntry]:
    return report.features or load_cached_codex_registry()


def evidence_data(paths: Paths, evidence: list[RuntimeEvidence], limit: int | None = None) -> list[dict[str, Any]]:
    items = evidence[-limit:] if limit else evidence
    return [
        {
            "timestamp": item.timestamp,
            "event": item.event_type,
            "window": item.window,
            "total_tokens": item.total_tokens,
            "session": relative_session_path(paths, item.file),
        }
        for item in items
    ]


def recent_session_files(paths: Paths, limit: int) -> list[Path]:
    root = paths.codex_home / "sessions"
    if not root.exists():
        return []
    files = [path for path in root.rglob("*.jsonl") if path.is_file()]
    files.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return files[:limit]


def collect_runtime_evidence(paths: Paths, file_limit: int) -> list[RuntimeEvidence]:
    evidence: list[RuntimeEvidence] = []
    for path in recent_session_files(paths, file_limit):
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    if '"model_context_window"' not in line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    payload = event.get("payload")
                    if not isinstance(payload, dict):
                        continue
                    event_type = payload.get("type")
                    if event_type not in {"task_started", "token_count"}:
                        continue
                    window = int_or_none(payload.get("model_context_window"))
                    if not window:
                        continue
                    info = payload.get("info")
                    usage = info.get("total_token_usage") if isinstance(info, dict) else None
                    total_tokens = int_or_none(usage.get("total_tokens")) if isinstance(usage, dict) else None
                    evidence.append(
                        RuntimeEvidence(
                            timestamp=str(event.get("timestamp", "")),
                            event_type=str(event_type),
                            window=window,
                            total_tokens=total_tokens,
                            file=path,
                        )
                    )
        except OSError:
            continue
    evidence.sort(key=lambda item: item.timestamp)
    return evidence


def window_counts(evidence: list[RuntimeEvidence]) -> dict[int, int]:
    counts: dict[int, int] = {}
    for item in evidence:
        counts[item.window] = counts.get(item.window, 0) + 1
    return counts


def relative_session_path(paths: Paths, path: Path) -> str:
    try:
        return str(path.relative_to(paths.codex_home))
    except ValueError:
        return str(path)


def cmd_check(args: argparse.Namespace) -> None:
    paths = resolve_paths()
    config = parse_toml_light(read_config(paths))
    configured_window = int_or_none(config.get("model_context_window"))
    codex_bin = optional_codex_bin(args.codex_bin)
    report = inspect_binary("Codex CLI", codex_bin)
    if JSON_OUTPUT:
        emit_json(
            {
                "command": "codex check",
                "ok": bool(report.path and not report.error),
                "paths": {
                    "codex_home": str(paths.codex_home),
                    "config": str(paths.config),
                    "catalog": str(paths.catalog),
                },
                "binary": binary_report_data(report, configured_window),
            }
        )
        return

    banner("Inspect Codex CLI, local config, feature registry, and model metadata.")
    section("Environment")
    kv("Codex home", paths.codex_home)
    kv("Config", paths.config)
    section("Binary")
    render_binary_report(report, configured_window)
    section("Next")
    status_line("info", "Review Codex controls", "`labkit codex list`")
    status_line("info", "Open Codex checklist", "`labkit codex select`")
    status_line("info", "Verify runtime evidence", "`labkit codex verify`")


def cmd_list(args: argparse.Namespace) -> None:
    paths = resolve_paths()
    config = parse_toml_light(read_config(paths))
    cli_report = inspect_binary("Codex CLI", optional_codex_bin(args.codex_bin))
    registry = codex_registry_with_cache(cli_report)
    include_all = bool(getattr(args, "all", False))
    features = codex_features_from_registry(registry, include_all=include_all)
    if JSON_OUTPUT:
        emit_json(
            {
                "command": "codex list",
                "mode": "all" if include_all else "recommended",
                "source": binary_report_data(cli_report),
                "features": feature_catalog_data(config, registry, features),
            }
        )
        return

    banner("Codex controls. Read-only view; nothing changes from this command.")
    if not include_all:
        status_line("info", "Recommended view", "use `labkit codex list --all` to include every current registry flag")
    render_feature_catalog(config, registry, numbered=True, features=features)


def cmd_status(args: argparse.Namespace) -> None:
    cmd_list(args)


def cmd_verify(args: argparse.Namespace) -> None:
    paths = resolve_paths()
    config = parse_toml_light(read_config(paths))
    codex_bin = require_codex_bin(args.codex_bin)
    report = inspect_binary("Codex", codex_bin)

    configured_window = int_or_none(config.get("model_context_window"))
    auto_compact = int_or_none(config.get("model_auto_compact_token_limit"))
    catalog_value = config.get("model_catalog_json")
    catalog_path = Path(catalog_value).expanduser() if isinstance(catalog_value, str) else None
    catalog_window = None
    if catalog_path and catalog_path.exists():
        try:
            catalog_window = int_or_none((find_model(json.loads(catalog_path.read_text(encoding="utf-8"))) or {}).get("context_window"))
        except (OSError, json.JSONDecodeError):
            catalog_window = None

    expected_runtime = None
    if configured_window and report.model_context_window and report.model_effective_percent:
        expected_runtime = int(min(configured_window, report.model_context_window) * report.model_effective_percent / 100)

    config_ok = (
        configured_window is not None
        and configured_window >= MODEL_CONTEXT_WINDOW
        and auto_compact is not None
        and auto_compact <= configured_window
        and catalog_path is not None
        and catalog_path.exists()
    )
    catalog_ok = bool(catalog_window and catalog_window >= CATALOG_CONTEXT_WINDOW)
    binary_ok = bool(report.model_context_window and report.model_context_window >= CATALOG_CONTEXT_WINDOW)
    evidence = collect_runtime_evidence(paths, args.files)
    latest = evidence[-1] if evidence else None
    runtime_ok = bool(expected_runtime and latest and latest.window >= expected_runtime)
    verification_ok = bool(config_ok and catalog_ok and binary_ok and runtime_ok)

    if JSON_OUTPUT:
        emit_json(
            {
                "command": "codex verify",
                "ok": verification_ok,
                "strict": args.strict,
                "inputs": {
                    "codex_binary": str(codex_bin),
                    "config": str(paths.config),
                },
                "config_layer": {
                    "ok": config_ok,
                    "model_context_window": configured_window,
                    "model_auto_compact_token_limit": auto_compact,
                    "model_catalog_json": str(catalog_path) if catalog_path else None,
                },
                "catalog_metadata": {
                    "ok": catalog_ok,
                    "model": TARGET_MODEL,
                    "context_window": catalog_window,
                },
                "binary_view": binary_report_data(report, configured_window),
                "runtime_evidence": {
                    "ok": runtime_ok,
                    "latest_window": latest.window if latest else None,
                    "expected_runtime_window": expected_runtime,
                    "scanned_windows": [{"window": window, "count": count} for window, count in sorted(window_counts(evidence).items())],
                    "events": evidence_data(paths, evidence, args.events),
                },
                "coverage": {
                    "runtime_proven": ["1m-context"] if runtime_ok else [],
                    "config_registry_checked": "other controls",
                    "manual_smoke_needed": ["browser/computer tools", "connector flows", "web search results"],
                },
            }
        )
        return 2 if args.strict and not verification_ok else 0

    banner("Layer-by-layer verification for config, catalog metadata, binary view, and runtime session logs.")
    section("Inputs")
    kv("Codex binary", codex_bin)
    kv("Config", paths.config)

    section("Config Layer")
    status_line("ok" if config_ok else "warn", "Requested settings", "read from ~/.codex/config.toml")
    kv("model_context_window", configured_window or "unset")
    kv("auto compact limit", auto_compact or "unset")
    kv("model catalog json", catalog_path or "unset")

    section("Catalog Metadata")
    status_line("ok" if catalog_ok else "warn", f"{TARGET_MODEL} context window", "read from local catalog file")
    kv("catalog context", f"{catalog_window:,}" if catalog_window else "unknown")

    section("Binary View")
    status_line("ok" if binary_ok else "warn", "Selected binary model view", str(codex_bin))
    if report.version:
        kv("version", report.version)
    if report.model_context_window:
        kv(f"{TARGET_MODEL} catalog", f"{report.model_context_window:,}")
    if expected_runtime:
        kv("expected runtime/UI", f"{expected_runtime:,}")
    if report.error:
        kv("note", warning(report.error))

    section("Runtime Evidence")
    if not evidence:
        status_line("warn", "No session evidence found", "no task_started/token_count events had model_context_window")
        return 2 if args.strict else 0

    status_line("ok" if runtime_ok else "warn", "Latest recorded runtime window", f"{latest.window:,}")
    if expected_runtime and latest.window < expected_runtime:
        kv("expected at least", f"{expected_runtime:,}")
    counts = ", ".join(f"{window:,} x{count}" for window, count in sorted(window_counts(evidence).items()))
    kv("scanned windows", counts)
    say("")
    rows = [(muted("timestamp"), muted("event"), muted("window"), muted("total_tokens"), muted("session"))]
    for item in evidence[-args.events :]:
        rows.append(
            (
                item.timestamp.replace("T", " ").replace("Z", ""),
                item.event_type,
                f"{item.window:,}",
                f"{item.total_tokens:,}" if item.total_tokens is not None else "-",
                relative_session_path(paths, item.file),
            )
        )
    print_rows(rows)
    section("Coverage Model")
    status_line("ok", "Runtime-proven here", "1m-context")
    status_line("info", "Config/registry-checked here", "other controls")
    status_line("warn", "Manual smoke still needed", "browser/computer tools, connector flows, web search results")
    return 2 if args.strict and not verification_ok else 0


def cmd_discover(args: argparse.Namespace) -> None:
    report = inspect_binary("Codex CLI", require_codex_bin(args.codex_bin))
    if not report.features:
        die(f"No feature registry available for {report.label}: {report.error or 'unknown error'}")
    if JSON_OUTPUT:
        emit_json(
            {
                "command": "codex discover",
                "source": binary_report_data(report),
                "features": registry_data(report.features, include_all=args.all),
            }
        )
        return

    banner("Raw local feature registry reported by the Codex CLI binary.")
    section("Source")
    kv("surface", report.label)
    kv("binary", report.path)
    kv("version", report.version or "unknown")
    stages: dict[str, list[tuple[str, RegistryEntry]]] = {}
    for name, entry in sorted(report.features.items()):
        if not args.all and entry.stage in {"removed", "deprecated"}:
            continue
        stages.setdefault(entry.stage, []).append((name, entry))
    for stage in sorted(stages):
        section(stage.title())
        rows = [(muted("feature"), muted("state"))]
        rows.extend((name, success("on") if entry.enabled else failure("off")) for name, entry in stages[stage])
        print_rows(rows)


def selected_features_from_tokens(tokens: list[str], selectable: list[Feature]) -> list[Feature]:
    by_number = {str(index): feature for index, feature in enumerate(selectable, start=1)}
    by_name = {feature.name: feature for feature in selectable}
    chosen: list[Feature] = []
    seen: set[str] = set()
    for token in tokens:
        feature = by_number.get(token) or by_name.get(token)
        if not feature:
            die(f"Unknown selection: {token}")
        if not feature.selectable:
            die(f"{feature.name} is reference-only and cannot be selected.")
        if feature.name not in seen:
            chosen.append(feature)
            seen.add(feature.name)
    return chosen


def target_enabled_from_text(text: str) -> str:
    target = text.strip().lower()
    aliases = {
        "active": "active",
        "activate": "active",
        "enable": "active",
        "enabled": "active",
        "on": "active",
        "inactive": "inactive",
        "deactivate": "inactive",
        "disable": "inactive",
        "disabled": "inactive",
        "off": "inactive",
        "toggle": "toggle",
        "flip": "toggle",
    }
    if target not in aliases:
        die("Choose one target state: active, inactive, or toggle.")
    return aliases[target]


def validate_feature_change(change: FeatureChange) -> None:
    if change.feature.kind == "manual":
        die(f"{change.feature.name} is reference-only and cannot be changed by this CLI.")
    if not change.enabled and change.feature.kind == "catalog":
        die(f"{change.feature.name} cannot be made inactive automatically. Restore a backup or remove model_catalog_json manually.")


def validate_claude_feature_change(change: FeatureChange) -> None:
    if change.feature.kind == "manual":
        die(f"{change.feature.name} is reference-only and cannot be changed by this CLI.")


def planned_changes_for_target(
    features: list[Feature],
    target: str,
    config: dict[str, Any],
    registry: dict[str, RegistryEntry],
    state_getter: Any | None = None,
    validator: Any | None = None,
) -> list[FeatureChange]:
    changes: list[FeatureChange] = []
    state_getter = state_getter or (lambda feature: feature_state(feature, config, registry))
    validator = validator or validate_feature_change
    for feature in features:
        state, source = state_getter(feature)
        enabled = state != "on" if target == "toggle" else target == "active"
        change = FeatureChange(feature=feature, enabled=enabled, current_state=state, current_source=source)
        validator(change)
        changes.append(change)
    return changes


def preview_enabled_for_state(state: str, target: str) -> bool:
    return state != "on" if target == "toggle" else target == "active"


def run_selection_tui(
    features: list[Feature],
    config: dict[str, Any],
    registry: dict[str, RegistryEntry],
    state_getter: Any | None = None,
    planner: Any | None = None,
) -> list[FeatureChange] | None:
    import curses

    rows: list[tuple[str, str, Feature | None]] = []
    state_getter = state_getter or (lambda feature: feature_state(feature, config, registry))
    planner = planner or (lambda chosen, target: planned_changes_for_target(chosen, target, config, registry, state_getter))
    states = {feature.name: state_getter(feature) for feature in features}
    for cluster, group in grouped_features(features).items():
        selectable_group = [feature for feature in group if feature.selectable]
        if not selectable_group:
            continue
        rows.append(("header", cluster, None))
        for feature in selectable_group:
            rows.append(("item", "", feature))

    item_indexes = [index for index, row in enumerate(rows) if row[0] == "item"]
    if not item_indexes:
        die("No selectable features found.")

    def next_item(index: int, direction: int) -> int:
        position = item_indexes.index(index)
        return item_indexes[(position + direction) % len(item_indexes)]

    def draw_text(stdscr: Any, y: int, x: int, text: str, attr: int = 0) -> None:
        height, width = stdscr.getmaxyx()
        if y < 0 or y >= height or x >= width:
            return
        try:
            stdscr.addnstr(y, x, text, max(0, width - x - 1), attr)
        except curses.error:
            pass

    def app(stdscr: Any) -> list[FeatureChange] | None:
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_YELLOW, -1)
            curses.init_pair(2, curses.COLOR_BLACK, curses.COLOR_YELLOW)
            curses.init_pair(3, curses.COLOR_GREEN, -1)
            curses.init_pair(4, curses.COLOR_RED, -1)
            curses.init_pair(5, curses.COLOR_RED, -1)
            curses.init_pair(6, curses.COLOR_YELLOW, -1)
            curses.init_pair(7, curses.COLOR_WHITE, -1)
            curses.init_pair(8, curses.COLOR_BLACK, curses.COLOR_WHITE)

        cursor = item_indexes[0]
        selected: set[str] = set()
        target = "active"
        top = 0
        notice = ""

        while True:
            stdscr.erase()
            height, width = stdscr.getmaxyx()
            visible_height = max(4, height - 7)

            if cursor < top:
                top = cursor
            if cursor >= top + visible_height:
                top = cursor - visible_height + 1

            target_line = f"Target: {'[active]' if target == 'active' else ' active '}  {'[inactive]' if target == 'inactive' else ' inactive '}  {'[toggle]' if target == 'toggle' else ' toggle '}"
            accent_attr = curses.color_pair(1) if curses.has_colors() else curses.A_BOLD
            active_attr = curses.color_pair(3) if curses.has_colors() else curses.A_BOLD
            inactive_attr = curses.color_pair(4) if curses.has_colors() else curses.A_BOLD
            muted_attr = curses.color_pair(7) if curses.has_colors() else 0
            header_attr = curses.A_BOLD | (curses.color_pair(6) if curses.has_colors() else 0)
            cursor_attr = curses.color_pair(8) if curses.has_colors() else curses.A_REVERSE

            draw_text(stdscr, 0, 0, APP_NAME, curses.A_BOLD | accent_attr)
            draw_text(stdscr, 1, 0, "Space selects. a active, i inactive, t toggle. Enter applies. q cancels.")
            draw_text(stdscr, 2, 0, target_line, accent_attr)
            draw_text(stdscr, 3, 0, "-" * max(0, width - 1))

            for screen_y, row_index in enumerate(range(top, min(len(rows), top + visible_height)), start=4):
                kind, label, feature = rows[row_index]
                if kind == "header":
                    draw_text(stdscr, screen_y, 0, label, header_attr)
                    continue

                assert feature is not None
                state, source = states[feature.name]
                checked = feature.name in selected
                enabled = preview_enabled_for_state(state, target)
                change = f" -> {'active' if enabled else 'inactive'}" if checked else ""
                marker = "[x]" if checked else "[ ]"
                pointer = ">" if row_index == cursor else " "
                base_attr = cursor_attr if row_index == cursor else 0
                if checked and row_index != cursor:
                    base_attr |= accent_attr
                draw_text(stdscr, screen_y, 0, f"{pointer} {marker} ", base_attr)
                draw_text(stdscr, screen_y, 8, f"{state:<7}", active_attr if state == "on" else inactive_attr)
                title = f"{feature.title:<30}"
                draw_text(stdscr, screen_y, 16, f"{title} {feature.name}{change}", base_attr)
                if width > 96:
                    draw_text(stdscr, screen_y, min(96, width - 1), source, muted_attr)

            footer_y = height - 2
            selected_count = len(selected)
            draw_text(stdscr, footer_y, 0, f"{selected_count} selected")
            if notice:
                attr = curses.color_pair(5) if curses.has_colors() else curses.A_BOLD
                draw_text(stdscr, footer_y + 1, 0, notice, attr)
            else:
                draw_text(stdscr, footer_y + 1, 0, "Tip: select active and inactive rows together, then use toggle.")

            key = stdscr.getch()
            notice = ""
            if key in (ord("q"), 27):
                return None
            if key in (curses.KEY_UP, ord("k")):
                cursor = next_item(cursor, -1)
                continue
            if key in (curses.KEY_DOWN, ord("j")):
                cursor = next_item(cursor, 1)
                continue
            if key in (ord("a"), ord("A")):
                target = "active"
                continue
            if key in (ord("i"), ord("I"), ord("d"), ord("D")):
                target = "inactive"
                continue
            if key in (ord("t"), ord("T")):
                target = "toggle"
                continue
            if key == ord(" "):
                feature = rows[cursor][2]
                assert feature is not None
                if feature.name in selected:
                    selected.remove(feature.name)
                else:
                    selected.add(feature.name)
                continue
            if key in (10, 13, curses.KEY_ENTER):
                if not selected:
                    notice = "Select at least one feature first."
                    continue
                chosen = [feature for feature in features if feature.name in selected]
                try:
                    return planner(chosen, target)
                except CliError as exc:
                    notice = str(exc)
                    continue

    return curses.wrapper(app)


def apply_feature_changes(
    command: str,
    changes: list[FeatureChange],
    paths: Paths,
    codex_bin: Path | None,
    dry_run: bool = False,
) -> None:
    for change in changes:
        validate_feature_change(change)

    if JSON_OUTPUT:
        backup = None
        if not dry_run:
            backup = backup_config(paths)
            for change in changes:
                apply_feature(change.feature, paths, codex_bin, enabled=change.enabled)
        emit_json(
            {
                "command": command,
                "dry_run": dry_run,
                "changed": not dry_run,
                "backup": str(backup) if backup else None,
                "codex_binary": str(codex_bin) if codex_bin else None,
                "config": str(paths.config),
                "features": [
                    {
                        "name": change.feature.name,
                        "enabled": change.enabled,
                        "target_state": "active" if change.enabled else "inactive",
                        "current_state": change.current_state,
                        "current_source": change.current_source,
                    }
                    for change in changes
                ],
                "restart_required": True,
            }
        )
        return

    if command == "enable":
        title = "Planning selected controls." if dry_run else "Applying selected controls."
    elif command == "disable":
        title = "Planning selected controls." if dry_run else "Disabling selected controls."
    else:
        title = "Planning selected controls." if dry_run else "Updating selected controls."

    banner(title)
    section("Target")
    kv("config", paths.config)
    kv("Codex binary", codex_bin)
    section("Plan")
    if dry_run:
        status_line("info", "Dry run", "no files changed")
    else:
        backup = backup_config(paths)
        if backup:
            status_line("ok", "Backed up config", str(backup))
        else:
            status_line("info", "No existing config to back up")

    section("Changes")
    for change in changes:
        verb = "enable" if change.enabled else "disable"
        if not dry_run:
            apply_feature(change.feature, paths, codex_bin, enabled=change.enabled)
        if dry_run:
            label = f"Would {verb} {change.feature.name}"
            tone = "info"
        else:
            label = f"{'Enabled' if change.enabled else 'Disabled'} {change.feature.name}"
            tone = "ok"
        detail = None
        if change.current_state:
            detail = f"was {change.current_state}, target {'active' if change.enabled else 'inactive'}"
        status_line(tone, label, detail)
        say_wrapped(change.feature.description, indent="    ")

    section("Next")
    status_line("warn", "Start a fresh Codex CLI session", "new sessions pick up config changes")
    if any(change.feature.kind == "catalog" and change.enabled for change in changes):
        status_line("info", "GPT-5.5 note", "Codex reserves about 5%, so the UI should show about 950k usable tokens")


def apply_claude_feature_changes(
    command: str,
    changes: list[FeatureChange],
    paths: ClaudePaths,
    dry_run: bool = False,
) -> None:
    for change in changes:
        validate_claude_feature_change(change)

    if JSON_OUTPUT:
        backup = None
        if not dry_run:
            settings = read_json_file(paths.settings)
            backup = backup_file(paths.settings)
            for change in changes:
                apply_claude_feature(change.feature, settings, change.enabled)
            write_json_file(paths.settings, settings)
        emit_json(
            {
                "command": f"claude-code {command}",
                "scope": paths.scope,
                "settings": str(paths.settings),
                "dry_run": dry_run,
                "changed": not dry_run,
                "backup": str(backup) if backup else None,
                "features": [
                    {
                        "name": change.feature.name,
                        "enabled": change.enabled,
                        "target_state": "active" if change.enabled else "inactive",
                        "current_state": change.current_state,
                        "current_source": change.current_source,
                    }
                    for change in changes
                ],
                "restart_required": True,
            }
        )
        return

    banner("Planning Claude Code controls." if dry_run else "Updating Claude Code controls.")
    section("Target")
    kv("scope", paths.scope)
    kv("settings", paths.settings)
    section("Plan")
    if dry_run:
        status_line("info", "Dry run", "no files changed")
    else:
        backup = backup_file(paths.settings)
        if backup:
            status_line("ok", "Backed up settings", str(backup))
        else:
            status_line("info", "No existing settings to back up")
        settings = read_json_file(paths.settings)
        for change in changes:
            apply_claude_feature(change.feature, settings, change.enabled)
        write_json_file(paths.settings, settings)

    section("Changes")
    for change in changes:
        verb = "enable" if change.enabled else "disable"
        label = f"Would {verb} {change.feature.name}" if dry_run else f"{'Enabled' if change.enabled else 'Disabled'} {change.feature.name}"
        detail = None
        if change.current_state:
            detail = f"was {change.current_state}, target {'active' if change.enabled else 'inactive'}"
        status_line("info" if dry_run else "ok", label, detail)
        say_wrapped(change.feature.description, indent="    ")
    section("Next")
    status_line("warn", "Start a fresh Claude Code session", "new sessions pick up settings changes")


def cmd_select(args: argparse.Namespace) -> None:
    if JSON_OUTPUT:
        die("`select` is interactive. Use `list --json` plus `enable --dry-run`/`disable --dry-run` for automation.")
    paths = resolve_paths()
    codex_bin = optional_codex_bin(args.codex_bin)
    config = parse_toml_light(read_config(paths))
    registry = codex_registry_with_cache(inspect_binary("Codex", codex_bin))
    catalog = codex_features_from_registry(registry, include_all=bool(getattr(args, "all", False)))
    selectable = [feature for feature in catalog if feature.selectable]

    if sys.stdin.isatty() and sys.stdout.isatty():
        try:
            changes = run_selection_tui(selectable, config, registry)
            if not changes:
                status_line("info", "No changes")
                return
            apply_feature_changes("select", changes, paths, codex_bin)
            return
        except Exception as exc:
            status_line("warn", "Checklist unavailable", str(exc))

    banner("Interactive Codex control selection. Nothing changes until the final confirmation.")
    render_feature_catalog(config, registry, numbered=True, features=catalog)

    section("Selection")
    say_wrapped("Enter feature numbers or names separated by commas/spaces. Press Enter to cancel.", indent="  ")
    raw = ask("  choose> ", Style.BOLD, Style.YELLOW).strip()
    if not raw:
        status_line("info", "No changes")
        return
    tokens = [token for token in re.split(r"[\s,]+", raw) if token]
    chosen = selected_features_from_tokens(tokens, selectable)
    say("")
    status_line("info", "Selected Codex controls", ", ".join(feature.name for feature in chosen))
    for feature in chosen:
        state, source = feature_state(feature, config, registry)
        say_wrapped(f"{feature.name}: {state} from {source}. {feature.description}", indent="    ")

    say("")
    say_wrapped("Choose target state for all selected controls: active, inactive, or toggle.", indent="  ")
    target_raw = ask("  target> ", Style.BOLD, Style.YELLOW).strip()
    if not target_raw:
        status_line("info", "No changes")
        return
    changes = planned_changes_for_target(chosen, target_enabled_from_text(target_raw), config, registry)

    say("")
    status_line("info", "Planned changes")
    for change in changes:
        say_wrapped(
            f"{change.feature.name}: {change.current_state or 'unknown'} -> {'active' if change.enabled else 'inactive'}",
            indent="    ",
        )

    confirm = ask("  type 'apply' to write> ", Style.BOLD, Style.YELLOW).strip().lower()
    if confirm not in {"apply", "yes"}:
        status_line("info", "No changes")
        return
    apply_feature_changes("select", changes, paths, codex_bin)


def cmd_enable(args: argparse.Namespace) -> None:
    codex_bin = optional_codex_bin(args.codex_bin)
    registry = codex_registry_with_cache(inspect_binary("Codex", codex_bin))
    features = []
    for name in args.features:
        feature = codex_feature_lookup(name, registry)
        if not feature:
            die(f"Unknown control: {name}. Run `labkit codex list`.")
        if not feature.selectable:
            die(f"{name} is reference-only and cannot be enabled by this CLI.")
        features.append(feature)
    enable_features(features, resolve_paths(), codex_bin, dry_run=args.dry_run)


def enable_features(features: list[Feature], paths: Paths, codex_bin: Path | None, dry_run: bool = False) -> None:
    changes = [FeatureChange(feature=feature, enabled=True) for feature in features]
    apply_feature_changes("enable", changes, paths, codex_bin, dry_run=dry_run)


def cmd_disable(args: argparse.Namespace) -> None:
    paths = resolve_paths()
    codex_bin = optional_codex_bin(args.codex_bin)
    registry = codex_registry_with_cache(inspect_binary("Codex", codex_bin))
    features = []
    for name in args.features:
        feature = codex_feature_lookup(name, registry)
        if not feature:
            die(f"Unknown control: {name}. Run `labkit codex list`.")
        if feature.kind in {"catalog", "manual"}:
            die(f"{feature.name} cannot be disabled automatically by this CLI.")
        features.append(feature)

    changes = [FeatureChange(feature=feature, enabled=False) for feature in features]
    apply_feature_changes("disable", changes, paths, codex_bin, dry_run=args.dry_run)


def cmd_codex_check(args: argparse.Namespace) -> None:
    return cmd_check(args)


def cmd_codex_list(args: argparse.Namespace) -> None:
    return cmd_list(args)


def cmd_codex_status(args: argparse.Namespace) -> None:
    return cmd_status(args)


def cmd_codex_verify(args: argparse.Namespace) -> None:
    return cmd_verify(args)


def cmd_codex_discover(args: argparse.Namespace) -> None:
    return cmd_discover(args)


def cmd_codex_select(args: argparse.Namespace) -> None:
    return cmd_select(args)


def cmd_codex_enable(args: argparse.Namespace) -> None:
    return cmd_enable(args)


def cmd_codex_disable(args: argparse.Namespace) -> None:
    return cmd_disable(args)


def cmd_update_features(args: argparse.Namespace) -> None:
    results: dict[str, Any] = {"command": "update-features", "ok": True, "data_home": str(user_data_home())}

    if not args.skip_codex:
        codex_bin = optional_codex_bin(args.codex_bin)
        if codex_bin:
            report = inspect_binary("Codex CLI", codex_bin)
            path = cached_codex_registry_path()
            write_cached_json(
                path,
                {
                    "updated_at": datetime.now().isoformat(timespec="seconds"),
                    "binary": binary_report_data(report),
                    "features": registry_data(report.features, include_all=True),
                },
            )
            results["codex"] = {"ok": bool(report.features), "path": str(path), "feature_count": len(report.features)}
        else:
            results["codex"] = {"ok": False, "path": None, "error": "codex binary not found"}

    if not args.skip_claude:
        schema_url = os.environ.get("LABKIT_CLAUDE_SCHEMA_URL", CLAUDE_SCHEMA_URL)
        schema = fetch_json_url(schema_url)
        path = cached_claude_schema_path()
        write_cached_json(path, schema)
        results["claude"] = {
            "ok": True,
            "path": str(path),
            "schema_url": schema_url,
            "top_level_settings": len(schema_properties(schema)),
            "env_settings": len(schema_env_properties(schema)),
        }

    if JSON_OUTPUT:
        emit_json(results)
        return

    banner("Refresh Lab Kit feature knowledge from local binaries and official schemas.")
    section("Data")
    kv("home", results["data_home"])
    if "codex" in results:
        codex = results["codex"]
        status_line("ok" if codex["ok"] else "warn", "Codex registry", codex.get("path") or codex.get("error"))
        if codex.get("feature_count") is not None:
            kv("features", codex["feature_count"])
    if "claude" in results:
        claude = results["claude"]
        status_line("ok", "Claude schema", claude["path"])
        kv("official schema", claude["schema_url"])
        kv("top-level settings", claude["top_level_settings"])
        kv("env settings", claude["env_settings"])


def cmd_claude_code_check(args: argparse.Namespace) -> None:
    paths = resolve_claude_paths(args.scope)
    settings = read_json_file(paths.settings)
    report = inspect_claude_binary(preferred_claude_bin(args.claude_bin))
    state_getter = lambda feature: claude_feature_state(feature, settings)
    features = feature_catalog_data_for(CLAUDE_FEATURES, state_getter)
    ok = bool(report["ok"])
    if JSON_OUTPUT:
        emit_json(
            {
                "command": "claude-code check",
                "ok": ok,
                "scope": paths.scope,
                "paths": {"home": str(paths.home), "settings": str(paths.settings)},
                "binary": report,
                "features": features,
            }
        )
        return

    banner("Inspect Claude Code binary, settings, and documented controls.")
    section("Environment")
    kv("Claude home", paths.home)
    kv("Settings scope", paths.scope)
    kv("Settings", paths.settings)
    section("Binary")
    status_line("ok" if ok else "warn", "Claude Code", str(report["path"] or "not found"))
    if report.get("version"):
        kv("version", report["version"])
    if report.get("error"):
        kv("note", warning(report["error"]))
    section("Next")
    status_line("info", "Review Claude Code controls", "`labkit claude-code list`")
    status_line("info", "Open Claude Code checklist", "`labkit claude-code select`")


def cmd_claude_code_list(args: argparse.Namespace) -> None:
    paths = resolve_claude_paths(args.scope)
    settings = read_json_file(paths.settings)
    state_getter = lambda feature: claude_feature_state(feature, settings)
    include_all = bool(getattr(args, "all", False))
    catalog = claude_features(settings, include_all=include_all)
    schema, schema_source = load_claude_schema()
    if JSON_OUTPUT:
        emit_json(
            {
                "command": "claude-code list",
                "scope": paths.scope,
                "mode": "all" if include_all else "curated",
                "settings": str(paths.settings),
                "schema": {
                    "url": CLAUDE_SCHEMA_URL,
                    "source": schema_source,
                    "top_level_settings": len(schema_properties(schema)),
                    "env_settings": len(schema_env_properties(schema)),
                },
                "features": feature_catalog_data_for(catalog, state_getter),
            }
        )
        return

    banner("Documented Claude Code controls. Read-only view; nothing changes from this command.")
    if not include_all:
        status_line("info", "Curated view", "use `labkit claude-code list --all` to include schema and settings-file keys")
    render_feature_catalog({}, {}, numbered=True, features=catalog, state_getter=state_getter)


def cmd_claude_code_discover(args: argparse.Namespace) -> None:
    paths = resolve_claude_paths(args.scope)
    settings = read_json_file(paths.settings)
    schema, schema_source = load_claude_schema()
    catalog = claude_features(settings, include_all=True)
    state_getter = lambda feature: claude_feature_state(feature, settings)

    if JSON_OUTPUT:
        emit_json(
            {
                "command": "claude-code discover",
                "scope": paths.scope,
                "settings": str(paths.settings),
                "schema": {
                    "url": CLAUDE_SCHEMA_URL,
                    "source": schema_source,
                    "top_level_settings": len(schema_properties(schema)),
                    "env_settings": len(schema_env_properties(schema)),
                },
                "features": feature_catalog_data_for(catalog, state_getter),
                "settings_keys": sorted(flatten_settings_keys(settings)),
            }
        )
        return

    banner("Claude Code curated controls plus schema and settings-file discoveries.")
    section("Source")
    kv("settings", paths.settings)
    kv("schema", schema_source)
    kv("official schema", CLAUDE_SCHEMA_URL)
    render_feature_catalog({}, {}, numbered=False, features=catalog, state_getter=state_getter)


def cmd_claude_discover(args: argparse.Namespace) -> None:
    return cmd_claude_code_discover(args)


def selected_claude_features(names: list[str], settings: dict[str, Any]) -> list[Feature]:
    features = []
    for name in names:
        feature = claude_feature_lookup(name, settings)
        if not feature:
            die(f"invalid choice: {name!r}. Run `labkit claude-code list --all`.")
        if not feature.selectable:
            die(f"invalid choice: {name!r} is reference-only and cannot be changed by this CLI.")
        features.append(feature)
    return features


def cmd_claude_code_select(args: argparse.Namespace) -> None:
    if JSON_OUTPUT:
        die(
            "`claude-code select` is interactive. Use `claude-code list --json` plus `claude-code enable --dry-run`/`claude-code disable --dry-run` for automation."
        )
    paths = resolve_claude_paths(args.scope)
    settings = read_json_file(paths.settings)
    catalog = claude_features(settings, include_all=bool(getattr(args, "all", False)))
    selectable = [feature for feature in catalog if feature.selectable]
    state_getter = lambda feature: claude_feature_state(feature, settings)
    planner = lambda chosen, target: planned_changes_for_target(
        chosen,
        target,
        {},
        {},
        state_getter=state_getter,
        validator=validate_claude_feature_change,
    )

    if sys.stdin.isatty() and sys.stdout.isatty():
        try:
            changes = run_selection_tui(selectable, {}, {}, state_getter=state_getter, planner=planner)
            if not changes:
                status_line("info", "No changes")
                return
            apply_claude_feature_changes("select", changes, paths)
            return
        except Exception as exc:
            status_line("warn", "Checklist unavailable", str(exc))

    banner("Interactive Claude Code control selection. Nothing changes until the final confirmation.")
    render_feature_catalog({}, {}, numbered=True, features=catalog, state_getter=state_getter)
    section("Selection")
    say_wrapped("Enter feature numbers or names separated by commas/spaces. Press Enter to cancel.", indent="  ")
    raw = ask("  choose> ", Style.BOLD, Style.YELLOW).strip()
    if not raw:
        status_line("info", "No changes")
        return
    chosen = selected_features_from_tokens([token for token in re.split(r"[\s,]+", raw) if token], selectable)
    say("")
    status_line("info", "Selected Claude Code controls", ", ".join(feature.name for feature in chosen))
    for feature in chosen:
        state, source = state_getter(feature)
        say_wrapped(f"{feature.name}: {state} from {source}. {feature.description}", indent="    ")
    say("")
    say_wrapped("Choose target state for all selected controls: active, inactive, or toggle.", indent="  ")
    target_raw = ask("  target> ", Style.BOLD, Style.YELLOW).strip()
    if not target_raw:
        status_line("info", "No changes")
        return
    changes = planner(chosen, target_enabled_from_text(target_raw))
    confirm = ask("  type 'apply' to write> ", Style.BOLD, Style.YELLOW).strip().lower()
    if confirm not in {"apply", "yes"}:
        status_line("info", "No changes")
        return
    apply_claude_feature_changes("select", changes, paths)


def cmd_claude_code_enable(args: argparse.Namespace) -> None:
    paths = resolve_claude_paths(args.scope)
    settings = read_json_file(paths.settings)
    state_getter = lambda feature: claude_feature_state(feature, settings)
    changes = [
        FeatureChange(feature=feature, enabled=True, current_state=state_getter(feature)[0], current_source=state_getter(feature)[1])
        for feature in selected_claude_features(args.features, settings)
    ]
    apply_claude_feature_changes("enable", changes, paths, dry_run=args.dry_run)


def cmd_claude_code_disable(args: argparse.Namespace) -> None:
    paths = resolve_claude_paths(args.scope)
    settings = read_json_file(paths.settings)
    state_getter = lambda feature: claude_feature_state(feature, settings)
    changes = [
        FeatureChange(feature=feature, enabled=False, current_state=state_getter(feature)[0], current_source=state_getter(feature)[1])
        for feature in selected_claude_features(args.features, settings)
    ]
    apply_claude_feature_changes("disable", changes, paths, dry_run=args.dry_run)


def cmd_claude_check(args: argparse.Namespace) -> None:
    return cmd_claude_code_check(args)


def cmd_claude_list(args: argparse.Namespace) -> None:
    return cmd_claude_code_list(args)


def cmd_claude_select(args: argparse.Namespace) -> None:
    return cmd_claude_code_select(args)


def cmd_claude_enable(args: argparse.Namespace) -> None:
    return cmd_claude_code_enable(args)


def cmd_claude_disable(args: argparse.Namespace) -> None:
    return cmd_claude_code_disable(args)


def help_formatter(prog: str) -> argparse.HelpFormatter:
    return argparse.HelpFormatter(prog, max_help_position=34, width=100)


def polish_help(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    parser._positionals.title = "Commands"
    parser._optionals.title = "Options"
    return parser


def build_parser() -> argparse.ArgumentParser:
    parser = polish_help(
        argparse.ArgumentParser(
            prog="labkit",
            description="Inspect and manage local Codex CLI and Claude Code controls.",
            formatter_class=help_formatter,
        )
    )
    parser.add_argument("--codex-bin", help="Path to a Codex CLI binary. Defaults to `codex` on PATH.")
    parser.add_argument("--claude-bin", help="Path to a Claude Code binary. Defaults to `claude` on PATH.")
    parser.add_argument("--no-color", action="store_true", help="Disable ANSI color output.")
    parser.add_argument("--no-progress", action="store_true", help="Disable progress spinners.")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON where supported.")
    parser.add_argument("--version", "-V", action="version", version=f"%(prog)s {package_version()}")
    subparsers = parser.add_subparsers(dest="command", required=True, metavar="{codex,claude-code,update-features}")

    def command_parser(subparsers_: argparse._SubParsersAction, name: str, **kwargs: Any) -> argparse.ArgumentParser:
        hidden = kwargs.get("help") is argparse.SUPPRESS
        kwargs.setdefault("formatter_class", help_formatter)
        parser_ = polish_help(subparsers_.add_parser(name, **kwargs))
        if hidden:
            subparsers_._choices_actions = [action for action in subparsers_._choices_actions if action.dest != name]
        return parser_

    def add_json(parser_: argparse.ArgumentParser) -> None:
        parser_.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="Emit machine-readable JSON.")

    def add_codex_commands(parent: argparse.ArgumentParser, alias_mode: bool = False) -> None:
        codex_subparsers = parent.add_subparsers(
            dest="codex_command",
            required=True,
            metavar="{check,list,discover,select,enable,disable,verify}",
        )
        c_check = command_parser(codex_subparsers, "check", help="Check Codex CLI installation and state.")
        add_json(c_check)
        c_check.set_defaults(func=cmd_codex_check)
        c_doctor = command_parser(codex_subparsers, "doctor", help=argparse.SUPPRESS)
        add_json(c_doctor)
        c_doctor.set_defaults(func=cmd_codex_check)
        c_list = command_parser(codex_subparsers, "list", help="Show Codex controls grouped by area.")
        c_list.add_argument("--all", action="store_true", help="Include every available registry flag, not just recommended controls.")
        add_json(c_list)
        c_list.set_defaults(func=cmd_codex_list)
        c_status = command_parser(codex_subparsers, "status", help=argparse.SUPPRESS)
        add_json(c_status)
        c_status.set_defaults(func=cmd_codex_status)
        c_verify = command_parser(codex_subparsers, "verify", help="Verify config, catalog, binary, and runtime evidence.")
        c_verify.add_argument("--files", type=int, default=12, help="Recent session files to scan.")
        c_verify.add_argument("--events", type=int, default=16, help="Recent runtime window events to show.")
        c_verify.add_argument("--strict", action="store_true", help="Exit non-zero when verification has warnings/failures.")
        add_json(c_verify)
        c_verify.set_defaults(func=cmd_codex_verify)
        c_discover = command_parser(codex_subparsers, "discover", help="Show raw entries from `codex features list`.")
        c_discover.add_argument("--all", action="store_true", help="Include deprecated and removed features.")
        add_json(c_discover)
        c_discover.set_defaults(func=cmd_codex_discover)
        c_select = command_parser(codex_subparsers, "select", help="Open the interactive Codex checklist.")
        c_select.add_argument("--all", action="store_true", help="Include every available registry flag in the selector.")
        add_json(c_select)
        c_select.set_defaults(func=cmd_codex_select)
        c_enable = command_parser(codex_subparsers, "enable", help="Enable explicitly named Codex controls.")
        c_enable.add_argument("--dry-run", action="store_true", help="Preview changes without writing config.")
        add_json(c_enable)
        c_enable.add_argument("features", nargs="+", metavar="control-id")
        c_enable._positionals.title = "Arguments"
        c_enable.set_defaults(func=cmd_codex_enable)
        c_disable = command_parser(codex_subparsers, "disable", help="Disable explicitly named Codex controls.")
        c_disable.add_argument("--dry-run", action="store_true", help="Preview changes without writing config.")
        add_json(c_disable)
        c_disable.add_argument("features", nargs="+", metavar="control-id")
        c_disable._positionals.title = "Arguments"
        c_disable.set_defaults(func=cmd_codex_disable)
        if alias_mode:
            return

    def add_claude_code_commands(parent: argparse.ArgumentParser, alias: str) -> None:
        claude_subparsers = parent.add_subparsers(
            dest=f"{alias.replace('-', '_')}_command",
            required=True,
            metavar="{check,list,discover,select,enable,disable}",
        )

        def add_claude_scope(parser_: argparse.ArgumentParser) -> None:
            parser_.add_argument(
                "--scope", choices=["user", "project", "local"], default="user", help="Claude Code settings scope to read/write."
            )

        cc_check = command_parser(claude_subparsers, "check", help="Check Claude Code installation and settings.")
        add_claude_scope(cc_check)
        add_json(cc_check)
        cc_check.set_defaults(func=cmd_claude_code_check if alias == "claude-code" else cmd_claude_check)
        cc_list = command_parser(claude_subparsers, "list", help="Show documented Claude Code controls.")
        add_claude_scope(cc_list)
        cc_list.add_argument("--all", action="store_true", help="Include official schema keys and settings-file discoveries.")
        add_json(cc_list)
        cc_list.set_defaults(func=cmd_claude_code_list if alias == "claude-code" else cmd_claude_list)
        cc_discover = command_parser(claude_subparsers, "discover", help="Show curated, schema, and settings-file Claude Code keys.")
        add_claude_scope(cc_discover)
        add_json(cc_discover)
        cc_discover.set_defaults(func=cmd_claude_code_discover if alias == "claude-code" else cmd_claude_discover)
        cc_select = command_parser(claude_subparsers, "select", help="Open the interactive Claude Code checklist.")
        add_claude_scope(cc_select)
        cc_select.add_argument("--all", action="store_true", help="Include official schema keys in the selector.")
        add_json(cc_select)
        cc_select.set_defaults(func=cmd_claude_code_select if alias == "claude-code" else cmd_claude_select)
        cc_enable = command_parser(claude_subparsers, "enable", help="Enable explicitly named Claude Code controls.")
        add_claude_scope(cc_enable)
        cc_enable.add_argument("--dry-run", action="store_true", help="Preview changes without writing settings.")
        add_json(cc_enable)
        cc_enable.add_argument("features", nargs="+", metavar="control-id")
        cc_enable._positionals.title = "Arguments"
        cc_enable.set_defaults(func=cmd_claude_code_enable if alias == "claude-code" else cmd_claude_enable)
        cc_disable = command_parser(claude_subparsers, "disable", help="Disable explicitly named Claude Code controls.")
        add_claude_scope(cc_disable)
        cc_disable.add_argument("--dry-run", action="store_true", help="Preview changes without writing settings.")
        add_json(cc_disable)
        cc_disable.add_argument("features", nargs="+", metavar="control-id")
        cc_disable._positionals.title = "Arguments"
        cc_disable.set_defaults(func=cmd_claude_code_disable if alias == "claude-code" else cmd_claude_disable)

    codex = command_parser(subparsers, "codex", help="Inspect and manage Codex CLI controls.")
    add_codex_commands(codex)

    update_features = command_parser(subparsers, "update-features", help="Refresh cached feature knowledge from official sources.")
    update_features.add_argument("--skip-codex", action="store_true", help="Do not refresh the Codex registry cache.")
    update_features.add_argument("--skip-claude", action="store_true", help="Do not refresh the Claude Code schema cache.")
    add_json(update_features)
    update_features.set_defaults(func=cmd_update_features)

    check = command_parser(subparsers, "check", help=argparse.SUPPRESS)
    add_json(check)
    check.set_defaults(func=cmd_check)
    doctor = command_parser(subparsers, "doctor", help=argparse.SUPPRESS)
    add_json(doctor)
    doctor.set_defaults(func=cmd_check)
    list_parser = command_parser(subparsers, "list", help=argparse.SUPPRESS)
    list_parser.add_argument("--all", action="store_true", help="Include every available registry flag, not just recommended controls.")
    add_json(list_parser)
    list_parser.set_defaults(func=cmd_list)
    status = command_parser(subparsers, "status", help=argparse.SUPPRESS)
    add_json(status)
    status.set_defaults(func=cmd_status)
    verify = command_parser(subparsers, "verify", help=argparse.SUPPRESS)
    verify.add_argument("--files", type=int, default=12, help="Recent session files to scan.")
    verify.add_argument("--events", type=int, default=16, help="Recent runtime window events to show.")
    verify.add_argument("--strict", action="store_true", help="Exit non-zero when verification has warnings/failures.")
    add_json(verify)
    verify.set_defaults(func=cmd_verify)
    discover = command_parser(subparsers, "discover", help=argparse.SUPPRESS)
    discover.add_argument("--all", action="store_true", help="Include deprecated and removed features.")
    add_json(discover)
    discover.set_defaults(func=cmd_discover)
    select = command_parser(subparsers, "select", help=argparse.SUPPRESS)
    select.add_argument("--all", action="store_true", help="Include every available registry flag in the selector.")
    add_json(select)
    select.set_defaults(func=cmd_select)
    enable = command_parser(subparsers, "enable", help=argparse.SUPPRESS)
    enable.add_argument("--dry-run", action="store_true", help="Preview changes without writing config.")
    add_json(enable)
    enable.add_argument("features", nargs="+", metavar="control-id")
    enable._positionals.title = "Arguments"
    enable.set_defaults(func=cmd_enable)
    disable = command_parser(subparsers, "disable", help=argparse.SUPPRESS)
    disable.add_argument("--dry-run", action="store_true", help="Preview changes without writing config.")
    add_json(disable)
    disable.add_argument("features", nargs="+", metavar="control-id")
    disable._positionals.title = "Arguments"
    disable.set_defaults(func=cmd_disable)

    claude_code = command_parser(subparsers, "claude-code", help="Inspect and manage Claude Code controls.")
    add_claude_code_commands(claude_code, "claude-code")
    claude = command_parser(subparsers, "claude", help=argparse.SUPPRESS)
    add_claude_code_commands(claude, "claude")
    return parser


def main(argv: list[str] | None = None) -> int:
    global COLOR_ENABLED, JSON_OUTPUT, PROGRESS_ENABLED
    parser = build_parser()
    args = parser.parse_args(argv)
    JSON_OUTPUT = bool(getattr(args, "json", False))
    PROGRESS_ENABLED = not args.no_progress
    COLOR_ENABLED = not args.no_color and not JSON_OUTPUT
    try:
        result = args.func(args)
        return int(result or 0)
    except CliError as exc:
        if JSON_OUTPUT:
            print(json.dumps({"ok": False, "error": {"type": "CliError", "message": str(exc)}}, sort_keys=True), file=sys.stderr)
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        if JSON_OUTPUT:
            print(
                json.dumps({"ok": False, "error": {"type": "KeyboardInterrupt", "message": "interrupted"}}, sort_keys=True), file=sys.stderr
            )
        else:
            print("interrupted", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
