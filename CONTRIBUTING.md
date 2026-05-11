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

Codex feature coverage should stay dynamic. Prefer improving `lab_kit/data/codex_feature_metadata.json` for titles, descriptions, and grouping instead of adding new hardcoded behavior for normal registry flags.

Claude Code coverage should stay curated plus schema-driven. Add polished controls to the curated list when Lab Kit can safely enable/disable them. Let `labkit claude-code list --all` and `labkit claude-code discover` surface schema-only or settings-file-only keys.

Refresh local feature knowledge with:

```bash
labkit update-features
```

## Pull Request Notes

- Keep runtime dependencies at zero unless there is a very strong reason.
- Preserve existing command names and JSON fields when possible.
- Add or update e2e tests for user-visible behavior.
- Update README details when install, safety, or coverage behavior changes.
