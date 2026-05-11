# Lab Kit CLI

Power up Codex CLI and Claude Code with Lab Kit CLI!

Lab Kit CLI is for people who use coding agents every day and want easier access to the experimental, preview, and locally gated controls already present in their installed tools. Instead of digging through config files or guessing environment variables, run one command, review the available controls, and choose what should be active.

It supports Codex CLI and Claude Code. It backs up files before editing. It does not auto-enable anything.

Lab Kit cannot grant account access, paid-plan access, model access, or server-side rollouts. It only toggles controls your local CLI already knows about.

## Quick Start

```bash
curl -fsSL https://raw.githubusercontent.com/jethros-projects/lab-kit-cli/main/install.sh | sh
```

Then run it from anywhere:

```bash
lab-kit codex select
lab-kit claude-code select
```

The installer adds `~/.local/bin` to your shell profile when needed. Open a new terminal after install, or run this once in the current terminal:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

The selector lets you pick multiple active or inactive controls, then apply them together. Nothing changes until you confirm.

## Why Use It

- Turn on experimental agent controls without hand-editing config files.
- See Codex and Claude Code controls grouped by area.
- Enable or disable multiple controls in one guided flow.
- Preview changes with `--dry-run`.
- Use `--json` from scripts and other agents.
- Keep backups of config/settings before every write.
- Verify Codex runtime evidence for context-window changes.

## Core Commands

```bash
lab-kit codex check
lab-kit codex list
lab-kit codex select
lab-kit codex enable <control-id> [control-id...]
lab-kit codex disable <control-id> [control-id...]
lab-kit codex verify

lab-kit claude-code check
lab-kit claude-code list
lab-kit claude-code select
lab-kit claude-code enable <control-id> [control-id...]
lab-kit claude-code disable <control-id> [control-id...]
```

Use `list` to find control ids. Use `select` for the polished interactive flow.

## Safety Model

Read-only commands:

```bash
lab-kit codex check
lab-kit codex list
lab-kit codex discover
lab-kit codex verify
lab-kit claude-code check
lab-kit claude-code list
```

Write commands:

```bash
lab-kit codex select
lab-kit codex enable <control-id>
lab-kit codex disable <control-id>
lab-kit claude-code select
lab-kit claude-code enable <control-id>
lab-kit claude-code disable <control-id>
```

Write commands back up the target file before editing it. After enabling controls, start a fresh Codex CLI or Claude Code session so the new config/settings are loaded.

## Agent-Friendly

Lab Kit works well for humans and for agents.

```bash
lab-kit codex list --json
lab-kit codex enable --dry-run --json <control-id>
lab-kit codex verify --strict

lab-kit claude-code list --json
lab-kit claude-code enable --dry-run --json <control-id>
```

JSON mode disables color and progress output. Runtime errors are emitted as JSON on stderr.

## Testing

Run the hermetic E2E suite:

```bash
tests/run_e2e.sh
```

Those tests execute the real `lab-kit` CLI against isolated temp homes and fake `codex` / `claude` binaries. That keeps CI deterministic and prevents tests from editing your real `~/.codex` or `~/.claude` files.

Run optional live smoke tests against real installed binaries:

```bash
LAB_KIT_LIVE_E2E=1 tests/run_e2e.sh
```

Live smoke tests are read-only or dry-run only.

## What It Touches

Codex:

- `~/.codex/config.toml`
- local model-catalog override used for the `1m-context` control
- recent Codex session logs for runtime verification

Claude Code:

- `~/.claude/settings.json`
- `.claude/settings.json` with `--scope project`
- `.claude/settings.local.json` with `--scope local`
- documented Claude Code `env` settings under the selected scope

If your binaries live somewhere unusual:

```bash
lab-kit --codex-bin /path/to/codex codex check
lab-kit --claude-bin /path/to/claude claude-code check
```

## Limits

Lab Kit is a local control surface, not an entitlement bypass.

Some controls are only useful when your installed CLI build, account, model, or rollout already supports them. Lab Kit can make local switches easy to inspect and edit; it cannot force a remote service to expose a feature.

## Repository Map

- `lab-kit`: main CLI.
- `install.sh`: installer for the `lab-kit` executable.
- `scripts/render_readme.py`: local README renderer.
- `README.html`: generated local preview of the README.
- `README.md`: product and usage guide.
