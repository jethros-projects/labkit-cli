# Contributing

Thanks for helping keep Lab Kit CLI sharp.

## Local Setup

```bash
git clone https://github.com/jethros-projects/labkit-cli.git
cd labkit-cli
python3 -m pip install -e ".[dev]"
```

Run the CLI from the checkout:

```bash
labkit --version
labkit codex list --json
labkit claude-code list --json
```

## Tests And Checks

Run the hermetic end-to-end tests before opening a PR:

```bash
tests/run_e2e.sh
```

Run the development checks used by CI:

```bash
ruff check .
black --check .
mypy lab_kit
```

Shell changes should pass ShellCheck:

```bash
shellcheck install.sh tests/run_e2e.sh
```

## Feature Coverage

Codex feature coverage should stay dynamic. Prefer improving `lab_kit/data/codex_feature_metadata.json` for titles, descriptions, grouping, smart markings, dependencies, limitations, verification steps, and source links instead of adding new hardcoded behavior for normal registry flags.

Claude Code coverage should stay curated plus schema-driven. Add polished controls to the curated list when Lab Kit can safely enable/disable them. Put docs-backed dependency and verification notes in `lab_kit/data/claude_feature_metadata.json`. Let `labkit claude-code list --all` and `labkit claude-code discover` surface schema-only or settings-file-only keys.

Leaked or research-preview Claude Code features need extra caution. Prefer official docs, SchemaStore, changelog, or live CLI evidence. If a control only appears in public leak analysis, keep it visible, mark it `risk_level: internal` or `risk_level: high`, make it non-selectable/reference-only, and add clear notes plus blocking limitations. Do not add write support for unsafe bypasses, internal employee paths, or names that appear build-gated or server-gated.

When documenting a control, distinguish local verification from runtime proof. Lab Kit can usually prove the file value and upstream key discovery; account entitlements, provider IAM, managed policy precedence, and live tool behavior need a real Codex CLI or Claude Code session.

Refresh local feature knowledge with:

```bash
labkit update-features
```

## Pull Request Notes

- Keep runtime dependencies at zero unless there is a very strong reason.
- Preserve existing command names and JSON fields when possible.
- Add or update e2e tests for user-visible behavior.
- Update README details when install, safety, or coverage behavior changes.
