# Lab Kit CLI

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Power up Codex CLI and Claude Code with Lab Kit CLI.

Lab Kit CLI is for people who use coding agents every day and want easier access to the experimental, preview, and locally gated controls already present in their installed tools. Instead of digging through config files or guessing environment variables, run one command, review the available controls, and choose what should be active.

It supports Codex CLI and Claude Code. It backs up files before editing. It does not auto-enable anything, and it cannot grant account access, paid-plan access, model access, or server-side rollouts.

## Quick Start

Install with the shell installer:

```bash
curl -fsSL https://raw.githubusercontent.com/jethros-projects/labkit-cli/main/install.sh | sh
```

Or install from a checkout with pip:

```bash
git clone https://github.com/jethros-projects/labkit-cli.git
cd labkit-cli
python3 -m pip install -e .
```

Then run it from anywhere:

```bash
labkit codex select
labkit claude-code select
```

The installer adds `~/.local/bin` to your shell profile when needed. Open a new terminal after install, or run this once in the current terminal:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

## Secure And Pinned Installs

Pin installs to a branch, tag, or commit with `REF`. For checksum verification, download the archive first, compute its SHA256, then pass that value to the installer:

```bash
REF=<commit-or-tag> \
LABKIT_SHA256=<sha256-of-github-archive> \
curl -fsSL https://raw.githubusercontent.com/jethros-projects/labkit-cli/main/install.sh | sh
```

Without `LABKIT_SHA256`, the installer still supports `REF`, but it will clearly note that checksum verification was skipped.

## Core Commands

```bash
labkit --version

labkit codex check
labkit codex list
labkit codex list --all
labkit codex list --details
labkit codex info <control-id>
labkit codex discover
labkit codex select
labkit codex enable <control-id> [control-id...]
labkit codex disable <control-id> [control-id...]
labkit codex verify --strict

labkit claude-code check
labkit claude-code list
labkit claude-code list --all
labkit claude-code list --details
labkit claude-code info <control-id>
labkit claude-code discover
labkit claude-code select
labkit claude-code enable <control-id> [control-id...]
labkit claude-code disable <control-id> [control-id...]

labkit update-features
```

Use `list` to see the clean recommended controls. Use `list --all` or `discover` when you want the complete surface. Use `info <control-id>` or `list --details` when you need dependencies, limitations, verification steps, and primary-source links. Use `select` for the polished interactive flow.

## 100% Coverage Approach

Codex support is primarily dynamic. Lab Kit reads the live registry from `codex features list`, maps known flags through `lab_kit/data/codex_feature_metadata.json` for friendly titles and groups, and generates sensible controls for any new registry flag it has never seen before. The default view stays recommended and calm; `--all` shows every currently available registry flag, and `discover` shows the raw registry.

Claude Code support is curated plus schema-driven. The hand-curated list remains the default because it has the best descriptions and safe enable/disable semantics. `list --all` and `discover` merge that list with the official JSON Schema from [SchemaStore](https://json.schemastore.org/claude-code-settings.json), plus any extra keys already present in the selected `settings.json`.

Claude leak and research-preview coverage is evidence-ranked. Publicly documented controls such as Agent View, voice dictation, auto permission mode, channels, Ultraplan, and Ultrareview include dependency and limitation notes. Leak-only or unsafe internal names such as KAIROS, Auto-Dream, Undercover Mode, and command-injection bypasses are hidden from the default view, shown in `--all` / `discover` / `info`, and marked reference-only so Lab Kit will not write unsupported toggles.

Run this periodically to refresh the local cache:

```bash
labkit update-features
```

That command refreshes the Codex registry cache from the installed `codex` binary and the Claude Code schema cache from the official schema URL. If Codex is not installed, Lab Kit falls back to the curated Codex controls and any previously cached registry.

## Verification Model

Lab Kit now separates three ideas that are easy to accidentally blur:

- **Configured** means Lab Kit wrote or observed the local setting.
- **Discoverable** means the installed upstream binary or official schema exposes the key.
- **Runtime-proven** means there is fresh evidence from a real Codex CLI or Claude Code session.

Use these when you want to know whether a control can actually do its job:

```bash
labkit codex info 1m-context
labkit codex verify --strict
labkit claude-code info auto-memory
labkit claude-code list --all --json
```

For Codex, `info` combines the live registry with `lab_kit/data/codex_feature_metadata.json`, including known upstream dependencies such as `enable_fanout` implying `multi_agent` and `code_mode_only` implying `code_mode`. For the `1m-context` control, `verify --strict` checks the config layer, local model catalog, Codex binary model view, and recent session logs.

For Claude Code, `info` combines curated metadata, SchemaStore keys, and settings-file discoveries. Lab Kit records where verification stops: it can prove the file value, but account entitlement, provider IAM, managed policy precedence, and live context/model behavior require Claude Code runtime checks such as `/status`, `/context`, `/memory`, `/hooks`, `/mcp`, `/permissions`, or `/model`.

For leaked Claude Code controls, `info` also records known non-working or non-public status. For example, the old `--enable-auto-mode` flag is marked removed, `USER_TYPE=ant` is marked as an internal path that should not be treated as locally enabling hidden behavior, and leak-only safety-bypass names are blocking/reference-only.

## How To Stay Up To Date

- Run `labkit update-features` after upgrading Codex CLI or Claude Code.
- Use `labkit codex list --all` to inspect new Codex registry flags as soon as your installed binary exposes them.
- Use `labkit claude-code list --all` to inspect Claude Code settings from the official schema and your actual settings file.
- Keep pinned installer usage on an explicit `REF` so automation upgrades only when you choose a new branch, tag, or commit.

## Safety Model

Read-only commands:

```bash
labkit codex check
labkit codex list
labkit codex discover
labkit codex verify
labkit claude-code check
labkit claude-code list
labkit claude-code discover
```

Write commands:

```bash
labkit codex select
labkit codex enable <control-id>
labkit codex disable <control-id>
labkit claude-code select
labkit claude-code enable <control-id>
labkit claude-code disable <control-id>
labkit update-features
```

Config write commands back up the target file before editing it. After enabling controls, start a fresh Codex CLI or Claude Code session so the new config/settings are loaded.

## Agent-Friendly

Lab Kit works well for humans and for agents.

```bash
labkit codex list --json
labkit codex list --all --json
labkit codex info <control-id> --json
labkit codex enable --dry-run --json <control-id>
labkit codex verify --strict

labkit claude-code list --json
labkit claude-code list --all --json
labkit claude-code info <control-id> --json
labkit claude-code discover --json
labkit claude-code enable --dry-run --json <control-id>
```

JSON mode disables color and progress output. Runtime errors are emitted as JSON on stderr.

## What It Touches

Codex:

- `~/.codex/config.toml`
- local model-catalog override used for the `1m-context` control
- recent Codex session logs for runtime verification
- `~/.local/share/labkit/codex-features.json` for optional registry cache

Claude Code:

- `~/.claude/settings.json`
- `.claude/settings.json` with `--scope project`
- `.claude/settings.local.json` with `--scope local`
- documented Claude Code `env` settings under the selected scope
- `~/.local/share/labkit/claude-code-settings-schema.json` for optional schema cache

If your binaries live somewhere unusual:

```bash
labkit --codex-bin /path/to/codex codex check
labkit --claude-bin /path/to/claude claude-code check
```

## Architecture Notes

- `labkit` is the executable used from source checkouts and installer installs.
- `lab_kit/cli.py` contains the packaged CLI implementation used by the `labkit` console entry point.
- `lab_kit/data/codex_feature_metadata.json` keeps Codex dynamic registry output readable and records known dependencies, limitations, verification steps, and source links without making coverage static.
- `lab_kit/data/claude-code-settings-schema.json` is the bundled Claude Code schema snapshot used when no refreshed cache exists.
- `lab_kit/data/claude_feature_metadata.json` overlays Claude Code controls with docs-backed dependency and verification metadata while preserving schema-driven coverage.
- Runtime dependencies are intentionally zero; development tools live behind the `.[dev]` extra.

## Windows Caveats

Lab Kit is Python-based and can run on Windows, but the shell installer is POSIX-oriented. On Windows, prefer `python -m pip install -e .` from a checkout, run from PowerShell or Windows Terminal, and verify that the `codex` and `claude` executables are available on `PATH`.

## Testing

Run the hermetic E2E suite:

```bash
tests/run_e2e.sh
```

Those tests execute the real `labkit` CLI against isolated temp homes and fake `codex` / `claude` binaries. That keeps CI deterministic and prevents tests from editing your real `~/.codex` or `~/.claude` files.

Run optional live smoke tests against real installed binaries:

```bash
LABKIT_LIVE_E2E=1 tests/run_e2e.sh
```

Live smoke tests are read-only or dry-run only.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for local setup, testing, and feature metadata guidance.

## License

Lab Kit CLI is released under the [MIT License](LICENSE).
