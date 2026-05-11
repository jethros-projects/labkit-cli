"""Claude Code support."""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

from . import ui
from .metadata import (
    control_id_from_key,
    filter_features_by_marking,
    load_claude_metadata,
    metadata_defaults,
    metadata_entry,
    package_json,
    title_from_id,
    with_feature_metadata,
)
from .models import CLAUDE_SCHEMA_URL, ClaudePaths, Feature, FeatureChange
from .ui import (
    Style,
    ask,
    banner,
    emit_json,
    feature_catalog_data_for,
    feature_info_data,
    kv,
    render_feature_catalog,
    render_feature_info,
    render_metadata_items,
    run_selection_tui,
    say,
    say_wrapped,
    section,
    selected_features_from_tokens,
    state_is_active,
    status_line,
    target_enabled_from_text,
    warning,
)
from .utils import (
    backup_file,
    cached_claude_schema_path,
    die,
    get_nested,
    preferred_claude_bin,
    read_json_file,
    resolve_claude_paths,
    run_text,
    set_nested,
    value_matches,
    write_json_file,
)


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


def claude_reference(name: str, title: str, cluster: str, stage: str, description: str) -> Feature:
    return Feature(
        name,
        title,
        cluster,
        stage,
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
    claude_setting(
        "agent-view",
        "Agent View",
        "Agent Runtime",
        "Keeps the research-preview background agent view and background session supervisor available.",
        "disableAgentView",
        value=False,
        inactive_value=True,
        default_enabled=True,
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
    claude_setting(
        "auto-permissions",
        "Auto Permission Mode",
        "Safety",
        "Starts Claude Code in auto permission mode, where low-risk tool calls can be approved with background safety checks.",
        "permissions.defaultMode",
        value="auto",
        inactive_value="default",
        default_enabled=False,
        kind="claude_value",
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
        "channels",
        "MCP Channels",
        "Tools & Integrations",
        "Enables research-preview MCP channel notifications from approved plugins or servers.",
        "channelsEnabled",
        default_enabled=False,
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
        "voice-dictation",
        "Voice Dictation",
        "Session UX",
        "Enables the Claude Code voice dictation UI for prompts when the local machine, account, and provider route support it.",
        "voice.enabled",
        default_enabled=False,
    ),
    claude_setting(
        "voice-tap-mode",
        "Voice Tap Mode",
        "Session UX",
        "Uses tap-to-record voice dictation instead of hold-to-record when voice dictation is enabled.",
        "voice.mode",
        value="tap",
        inactive_value="hold",
        default_enabled=False,
        kind="claude_value",
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
    claude_reference(
        "ultraplan",
        "Ultraplan",
        "Session Commands",
        "research-preview",
        "Command-only feature: run `/ultraplan <prompt>` to draft a plan in Claude Code on the web, then execute remotely or send it back to the terminal.",
    ),
    claude_reference(
        "ultrareview",
        "Ultrareview",
        "Session Commands",
        "research-preview",
        "Command-only feature: run `/ultrareview [PR]` or `claude ultrareview` for a deeper cloud review with multiple reviewer agents.",
    ),
    claude_reference(
        "autofix-pr",
        "Autofix PR",
        "Session Commands",
        "cloud-command",
        "Command-only feature: run `/autofix-pr` to start a Claude Code on the web session that watches a pull request and pushes fixes.",
    ),
    claude_reference(
        "background-session",
        "Background Session",
        "Session Commands",
        "research-preview",
        "Command-only feature: run `/background`, `/bg`, or `claude --bg <prompt>` to detach a session into Agent View.",
    ),
    claude_reference(
        "side-question",
        "Side Question",
        "Session Commands",
        "command",
        "Command-only feature: run `/btw <question>` to ask an aside without adding it to the main conversation context.",
    ),
    claude_reference(
        "context-visualizer",
        "Context Visualizer",
        "Session Commands",
        "command",
        "Command-only feature: run `/context` to inspect current context-window usage and optimization suggestions.",
    ),
    claude_reference(
        "development-channels",
        "Development Channels",
        "Session Commands",
        "dangerous-session-flag",
        "Session-only flag: `--dangerously-load-development-channels` loads channel sources outside the approved allowlist for local development.",
    ),
    claude_reference(
        "removed-auto-mode-flag",
        "Removed Auto Mode Flag",
        "Leaked / Internal",
        "removed",
        "Historical flag: `--enable-auto-mode` was removed; use `--permission-mode auto` or the Auto Permission Mode setting instead.",
    ),
    claude_reference(
        "kairos",
        "KAIROS",
        "Leaked / Internal",
        "leak-reported",
        "Leak-reported persistent assistant/daemon architecture. LabKit tracks it for awareness only because no supported public setting or command enables it.",
    ),
    claude_reference(
        "auto-dream",
        "Auto-Dream",
        "Leaked / Internal",
        "leak-reported",
        "Leak-reported KAIROS memory-consolidation cycle. It is not a supported public Claude Code control.",
    ),
    claude_reference(
        "coordinator-mode",
        "Coordinator Mode",
        "Leaked / Internal",
        "leak-reported",
        "Leak-reported multi-agent orchestration mode. Use the official Agent Teams control instead of community-reported coordinator toggles.",
    ),
    claude_reference(
        "buddy",
        "Buddy",
        "Leaked / Internal",
        "leak-reported",
        "Leak-reported terminal companion/pet experiment. It has no supported public enablement path.",
    ),
    claude_reference(
        "dream-command",
        "Dream Command",
        "Leaked / Internal",
        "leak-reported",
        "Leak-reported `/dream` command for manual KAIROS memory consolidation. It is not listed in current official command docs.",
    ),
    claude_reference(
        "pr-subscriptions",
        "PR Subscriptions",
        "Leaked / Internal",
        "leak-reported",
        "Leak-reported KAIROS pull-request subscription tooling. Use official `/autofix-pr`, Code Review, routines, or channels for supported workflows.",
    ),
    claude_reference(
        "bughunter",
        "Bug Hunter",
        "Leaked / Internal",
        "leak-reported",
        "Leak-reported automated bug-hunting command. Use official `/review`, `/security-review`, or `/ultrareview` for supported review flows.",
    ),
    claude_reference(
        "undercover-mode",
        "Undercover Mode",
        "Leaked / Internal",
        "leak-reported",
        "Leak-reported internal mode for hiding Anthropic or AI-authorship signals. LabKit intentionally keeps this reference-only.",
    ),
    claude_reference(
        "anti-distillation",
        "Anti-Distillation",
        "Leaked / Internal",
        "leak-reported",
        "Leak-reported server-side competitive-defense mechanism involving fake tool injection. It is not a user-facing feature flag.",
    ),
    claude_reference(
        "internal-ant-user-type",
        "Internal Anthropic User Type",
        "Leaked / Internal",
        "unsupported",
        "Leak-reported `USER_TYPE=ant` internal-employee path. Public reports say setting it yourself is unlikely to work and may be server-side validated.",
    ),
    claude_reference(
        "ablation-baseline",
        "Ablation Baseline",
        "Leaked / Internal",
        "unsafe-internal",
        "Leak-reported safety-ablation switch. LabKit will not write it because it is internal testing infrastructure and unsafe for normal use.",
    ),
    claude_reference(
        "command-injection-check-bypass",
        "Command Injection Check Bypass",
        "Leaked / Internal",
        "unsafe-internal",
        "Leak-reported internal testing switch for disabling command-injection checks. LabKit will not write it.",
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


def claude_default_metadata(metadata: dict[str, Any], feature: Feature) -> list[dict[str, Any]]:
    names = ["control"]
    if feature.kind == "manual":
        names.append("manual")
    elif feature.kind == "schema_setting":
        names.append("schema-setting")
    elif feature.kind == "observed_setting":
        names.append("settings-file")
    elif feature.key and feature.key.startswith("env."):
        names.append("env-setting")
    else:
        names.append("setting")
    return metadata_defaults(metadata, *names)


def metadata_cluster_entry(metadata: dict[str, Any], cluster: str) -> dict[str, Any]:
    clusters = metadata.get("clusters")
    if not isinstance(clusters, dict):
        return {}
    entry = clusters.get(cluster)
    return entry if isinstance(entry, dict) else {}


def apply_claude_metadata(features: list[Feature]) -> list[Feature]:
    metadata = load_claude_metadata()
    enriched: list[Feature] = []
    for feature in features:
        entry = metadata_entry(metadata, feature.name)
        if not entry and feature.key:
            entry = metadata_entry(metadata, feature.key)
        enriched.append(
            with_feature_metadata(
                feature, *claude_default_metadata(metadata, feature), metadata_cluster_entry(metadata, feature.cluster), entry
            )
        )
    return enriched


CLAUDE_FEATURES = apply_claude_metadata(apply_copy_overrides(CLAUDE_FEATURES, CLAUDE_COPY_OVERRIDES))


def validate_claude_schema(schema: dict[str, Any]) -> tuple[bool, str | None]:
    if not isinstance(schema, dict):
        return False, "schema is not a JSON object"
    if schema.get("type") not in {None, "object"}:
        return False, "top-level schema type is not object"
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return False, "schema has no top-level properties object"
    if "env" in properties:
        env = properties.get("env")
        if not isinstance(env, dict):
            return False, "env schema is not an object"
        env_properties = env.get("properties", {})
        if env_properties is not None and not isinstance(env_properties, dict):
            return False, "env schema properties are not an object"
    schema_uri = schema.get("$schema")
    if schema_uri is not None and not isinstance(schema_uri, str):
        return False, "$schema must be a string when present"
    return True, None


def load_claude_schema() -> tuple[dict[str, Any], str]:
    cache = cached_claude_schema_path()
    if cache.exists():
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            ok, _reason = validate_claude_schema(data) if isinstance(data, dict) else (False, "not an object")
            if ok:
                return data, str(cache)
        except json.JSONDecodeError:
            pass
    bundled = package_json("claude-code-settings-schema.json")
    ok, _reason = validate_claude_schema(bundled) if bundled else (False, "missing")
    if ok:
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
    metadata = load_claude_metadata()
    generated: list[Feature] = []
    for key, spec in sorted(schema_properties(schema).items()):
        if key in {"$schema", "env"} or key in known or not isinstance(spec, dict):
            continue
        feature = schema_feature_for_key(key, spec)
        generated.append(
            with_feature_metadata(
                feature,
                *claude_default_metadata(metadata, feature),
                *metadata_defaults(metadata, "schema-setting"),
                metadata_entry(metadata, key),
            )
        )
    for key, spec in sorted(schema_env_properties(schema).items()):
        dotted = f"env.{key}"
        if dotted in known or not isinstance(spec, dict):
            continue
        feature = schema_feature_for_key(key, spec, env=True)
        generated.append(
            with_feature_metadata(
                feature,
                *claude_default_metadata(metadata, feature),
                *metadata_defaults(metadata, "schema-setting"),
                metadata_entry(metadata, dotted),
            )
        )
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
    metadata = load_claude_metadata()
    features: list[Feature] = []
    for key in sorted(flatten_settings_keys(settings)):
        if key in known or key == "$schema":
            continue
        if any(known_key.startswith(f"{key}.") for known_key in known):
            continue
        actual = get_nested(settings, key)
        name = control_id_from_key(key.replace(".", "-"))
        selectable = isinstance(actual, bool)
        feature = Feature(
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
        features.append(
            with_feature_metadata(
                feature,
                *claude_default_metadata(metadata, feature),
                *metadata_defaults(metadata, "settings-file"),
                metadata_entry(metadata, key),
            )
        )
    return features


def claude_features(settings: dict[str, Any] | None = None, *, include_all: bool = False) -> list[Feature]:
    if not include_all:
        return list(CLAUDE_FEATURES)
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


def validate_claude_feature_change(change: FeatureChange) -> None:
    if change.feature.kind == "manual":
        die(f"{change.feature.name} is reference-only and cannot be changed by this CLI.")


def planned_changes_for_target(
    features: list[Feature],
    target: str,
    state_getter: Any,
) -> list[FeatureChange]:
    changes: list[FeatureChange] = []
    for feature in features:
        state, source = state_getter(feature)
        enabled = not state_is_active(state) if target == "toggle" else target == "active"
        change = FeatureChange(feature=feature, enabled=enabled, current_state=state, current_source=source)
        validate_claude_feature_change(change)
        changes.append(change)
    return changes


def marked_features(features: list[Feature], args: argparse.Namespace) -> list[Feature]:
    return filter_features_by_marking(features, risk=getattr(args, "risk", None), include_internal=True)


def apply_claude_feature_changes(
    command: str,
    changes: list[FeatureChange],
    paths: ClaudePaths,
    dry_run: bool = False,
) -> None:
    for change in changes:
        validate_claude_feature_change(change)

    if ui.JSON_OUTPUT:
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
                        "dependencies": list(change.feature.dependencies),
                        "limitations": list(change.feature.limitations),
                        "verification": change.feature.verification_mode,
                        "verification_steps": list(change.feature.verification),
                        "risk_level": change.feature.risk_level,
                        "stability": change.feature.stability,
                        "recommended": change.feature.recommended,
                        "notes": change.feature.notes,
                        "tags": list(change.feature.tags),
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
        if change.feature.limitations:
            render_metadata_items("Limitations", change.feature.limitations, indent="    ")
        if change.feature.verification:
            render_metadata_items("Verify after change", change.feature.verification, indent="    ")
    section("Next")
    status_line("warn", "Start a fresh Claude Code session", "new sessions pick up settings changes")


def cmd_claude_code_check(args: argparse.Namespace) -> None:
    paths = resolve_claude_paths(args.scope)
    settings = read_json_file(paths.settings)
    report = inspect_claude_binary(preferred_claude_bin(args.claude_bin))
    state_getter = lambda feature: claude_feature_state(feature, settings)
    features = feature_catalog_data_for(claude_features(settings, include_all=False), state_getter)
    ok = bool(report["ok"])
    if ui.JSON_OUTPUT:
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
    catalog = marked_features(claude_features(settings, include_all=include_all), args)
    schema, schema_source = load_claude_schema()
    if ui.JSON_OUTPUT:
        emit_json(
            {
                "command": "claude-code list",
                "scope": paths.scope,
                "mode": "all" if include_all else "marked",
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
        status_line("info", "Marked view", "all curated controls are visible; use --all for schema and settings-file keys")
    render_feature_catalog(catalog, state_getter, numbered=True, details=bool(getattr(args, "details", False)))


def cmd_claude_code_info(args: argparse.Namespace) -> None:
    paths = resolve_claude_paths(args.scope)
    settings = read_json_file(paths.settings)
    feature = claude_feature_lookup(args.feature, settings)
    if not feature:
        die(f"Unknown control: {args.feature}. Run `labkit claude-code list --all`.")
    state, source = claude_feature_state(feature, settings)
    if ui.JSON_OUTPUT:
        emit_json(
            {
                "command": "claude-code info",
                "scope": paths.scope,
                "settings": str(paths.settings),
                "feature": feature_info_data(feature, state, source),
            }
        )
        return
    render_feature_info(feature, state, source)


def cmd_claude_code_discover(args: argparse.Namespace) -> None:
    paths = resolve_claude_paths(args.scope)
    settings = read_json_file(paths.settings)
    schema, schema_source = load_claude_schema()
    catalog = claude_features(settings, include_all=True)
    state_getter = lambda feature: claude_feature_state(feature, settings)

    if ui.JSON_OUTPUT:
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
    render_feature_catalog(catalog, state_getter, numbered=False)


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
    if ui.JSON_OUTPUT:
        die(
            "`claude-code select` is interactive. Use `claude-code list --json` plus `claude-code enable --dry-run`/`claude-code disable --dry-run` for automation."
        )
    paths = resolve_claude_paths(args.scope)
    settings = read_json_file(paths.settings)
    catalog = marked_features(claude_features(settings, include_all=bool(getattr(args, "all", False))), args)
    selectable = [feature for feature in catalog if feature.selectable]
    state_getter = lambda feature: claude_feature_state(feature, settings)
    planner = lambda chosen, target: planned_changes_for_target(chosen, target, state_getter)

    if sys.stdin.isatty() and sys.stdout.isatty():
        try:
            changes = run_selection_tui(selectable, state_getter, planner)
            if not changes:
                status_line("info", "No changes")
                return
            apply_claude_feature_changes("select", changes, paths)
            return
        except Exception as exc:
            status_line("warn", "Checklist unavailable", str(exc))

    banner("Interactive Claude Code control selection. Nothing changes until the final confirmation.")
    render_feature_catalog(catalog, state_getter, numbered=True)
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


def cmd_claude_info(args: argparse.Namespace) -> None:
    return cmd_claude_code_info(args)


def cmd_claude_select(args: argparse.Namespace) -> None:
    return cmd_claude_code_select(args)


def cmd_claude_enable(args: argparse.Namespace) -> None:
    return cmd_claude_code_enable(args)


def cmd_claude_disable(args: argparse.Namespace) -> None:
    return cmd_claude_code_disable(args)
