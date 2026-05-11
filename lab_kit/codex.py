"""Codex CLI support."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any

from . import ui
from .metadata import (
    control_id_from_key,
    filter_features_by_marking,
    load_codex_metadata,
    metadata_defaults,
    metadata_entry,
    title_from_id,
    with_feature_metadata,
)
from .models import (
    AUTO_COMPACT_TOKEN_LIMIT,
    CATALOG_CONTEXT_WINDOW,
    MODEL_CONTEXT_WINDOW,
    TARGET_MODEL,
    BinaryReport,
    Feature,
    FeatureChange,
    Paths,
    RegistryEntry,
    RuntimeEvidence,
)
from .ui import (
    Style,
    ask,
    banner,
    emit_json,
    failure,
    feature_catalog_data_for,
    feature_info_data,
    kv,
    muted,
    print_rows,
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
    success,
    target_enabled_from_text,
    warning,
)
from .utils import (
    backup_config,
    cached_codex_registry_path,
    die,
    int_or_none,
    optional_codex_bin,
    parse_toml,
    read_config,
    remove_top_level_keys,
    require_codex_bin,
    resolve_paths,
    run_json,
    run_text,
    set_table_values,
    set_top_level,
    write_config,
)

CURATED_FEATURES: list[Feature] = [
    Feature(
        "1m-context",
        "Unsupported 1M Context Override",
        "Model & Context",
        "unsupported override",
        "catalog",
        "Documents the old local catalog patch for GPT-5.5. It stays visible for audit and cleanup, but is marked high risk because current Codex runtime evidence does not prove 1M context support.",
        selectable=False,
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


def codex_default_metadata(metadata: dict[str, Any], feature: Feature) -> list[dict[str, Any]]:
    names = ["control"]
    if feature.kind == "feature":
        names.append("registry-feature")
    elif feature.kind == "catalog":
        names.append("catalog-patch")
    elif feature.kind == "top":
        names.append("top-setting")
    return metadata_defaults(metadata, *names)


def apply_codex_metadata(feature: Feature, metadata: dict[str, Any]) -> Feature:
    entry = metadata_entry(metadata, feature.key or feature.name) or metadata_entry(metadata, feature.name)
    aliases = entry.get("aliases") if isinstance(entry.get("aliases"), list) else []
    registry_keys = tuple(str(item) for item in aliases)
    if feature.key:
        registry_keys = (feature.key, *registry_keys)
    registry_keys = (*feature.registry_keys, *tuple(key for key in registry_keys if key not in feature.registry_keys))
    feature = replace(
        feature,
        name=str(entry.get("name") or feature.name),
        title=str(entry.get("title") or feature.title),
        cluster=str(entry.get("cluster") or feature.cluster),
        description=str(entry.get("description") or feature.description),
        registry_keys=tuple(key for key in registry_keys if key),
    )
    return with_feature_metadata(feature, *codex_default_metadata(metadata, feature), entry)


def codex_curated_features() -> list[Feature]:
    metadata = load_codex_metadata()
    return [apply_codex_metadata(feature, metadata) for feature in CURATED_FEATURES]


def dynamic_codex_feature(name: str, entry: RegistryEntry, metadata: dict[str, Any]) -> Feature:
    meta = metadata_entry(metadata, name) or metadata_entry(metadata, control_id_from_key(name))
    control_id = str(meta.get("name") or control_id_from_key(name))
    feature = Feature(
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
    return with_feature_metadata(feature, *codex_default_metadata(metadata, feature), meta)


def codex_features_from_registry(registry: dict[str, RegistryEntry], *, include_all: bool = False) -> list[Feature]:
    metadata = load_codex_metadata()
    curated = codex_curated_features()
    features: list[Feature] = []
    represented_registry_keys: set[str] = set()
    for feature in curated:
        keys = set(feature.registry_keys)
        if feature.key:
            keys.add(feature.key)
        represented_registry_keys.update(keys)
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


def find_model(catalog: dict[str, Any]) -> dict[str, Any] | None:
    models = catalog.get("models")
    if not isinstance(models, list):
        return None
    return next((item for item in models if model_slug(item) == TARGET_MODEL), None)


def model_slug(model: dict[str, Any]) -> str | None:
    return model.get("slug") or model.get("id") or model.get("name")


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


def same_path(left: Path, right: Path) -> bool:
    try:
        return left.expanduser().resolve() == right.expanduser().resolve()
    except OSError:
        return left.expanduser() == right.expanduser()


def unpatch_gpt55_context(paths: Paths) -> None:
    current = parse_toml(read_config(paths))
    catalog_value = current.get("model_catalog_json")
    config = remove_top_level_keys(
        read_config(paths),
        {"model_catalog_json", "model_context_window", "model_auto_compact_token_limit"},
    )
    write_config(paths, config)
    if isinstance(catalog_value, str) and same_path(Path(catalog_value), paths.catalog) and paths.catalog.exists():
        paths.catalog.unlink()


def apply_feature(feature: Feature, paths: Paths, codex_bin: Path | None, enabled: bool) -> None:
    if feature.kind == "manual":
        die(f"{feature.name} is reference-only and cannot be changed by this CLI.")
    if feature.kind == "catalog":
        if not enabled:
            unpatch_gpt55_context(paths)
            return
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


def feature_catalog_data(
    config: dict[str, Any], registry: dict[str, RegistryEntry], features: list[Feature] | None = None
) -> list[dict[str, Any]]:
    catalog = features or codex_features_from_registry(registry)
    return feature_catalog_data_for(catalog, lambda feature: feature_state(feature, config, registry))


def validate_feature_change(change: FeatureChange) -> None:
    if change.feature.kind == "manual":
        die(f"{change.feature.name} is reference-only and cannot be changed by this CLI.")


def planned_changes_for_target(
    features: list[Feature],
    target: str,
    config: dict[str, Any],
    registry: dict[str, RegistryEntry],
) -> list[FeatureChange]:
    changes: list[FeatureChange] = []
    for feature in features:
        state, source = feature_state(feature, config, registry)
        enabled = not state_is_active(state) if target == "toggle" else target == "active"
        change = FeatureChange(feature=feature, enabled=enabled, current_state=state, current_source=source)
        validate_feature_change(change)
        changes.append(change)
    return changes


def marked_features(features: list[Feature], args: argparse.Namespace) -> list[Feature]:
    return filter_features_by_marking(features, risk=getattr(args, "risk", None), include_internal=True)


def apply_feature_changes(
    command: str,
    changes: list[FeatureChange],
    paths: Paths,
    codex_bin: Path | None,
    dry_run: bool = False,
) -> None:
    for change in changes:
        validate_feature_change(change)

    if ui.JSON_OUTPUT:
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
        if change.feature.limitations:
            render_metadata_items("Limitations", change.feature.limitations, indent="    ")
        if change.feature.verification:
            render_metadata_items("Verify after change", change.feature.verification, indent="    ")

    section("Next")
    status_line("warn", "Start a fresh Codex CLI session", "new sessions pick up config changes")
    if any(change.feature.kind == "catalog" and change.enabled for change in changes):
        status_line("info", "GPT-5.5 note", "Codex reserves about 5%, so the UI should show about 950k usable tokens")


def cmd_check(args: argparse.Namespace) -> None:
    paths = resolve_paths()
    config = parse_toml(read_config(paths))
    configured_window = int_or_none(config.get("model_context_window"))
    codex_bin = optional_codex_bin(args.codex_bin)
    report = inspect_binary("Codex CLI", codex_bin)
    if ui.JSON_OUTPUT:
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
    config = parse_toml(read_config(paths))
    cli_report = inspect_binary("Codex CLI", optional_codex_bin(args.codex_bin))
    registry = codex_registry_with_cache(cli_report)
    include_all = bool(getattr(args, "all", False))
    features = marked_features(codex_features_from_registry(registry, include_all=include_all), args)
    if ui.JSON_OUTPUT:
        emit_json(
            {
                "command": "codex list",
                "mode": "all" if include_all else "marked",
                "source": binary_report_data(cli_report),
                "features": feature_catalog_data(config, registry, features),
            }
        )
        return

    banner("Codex controls. Read-only view; nothing changes from this command.")
    if not include_all:
        status_line("info", "Marked view", "all curated controls are visible; use --all for dynamic registry extras")
    render_feature_catalog(
        features, lambda feature: feature_state(feature, config, registry), numbered=True, details=bool(getattr(args, "details", False))
    )


def cmd_info(args: argparse.Namespace) -> None:
    paths = resolve_paths()
    config = parse_toml(read_config(paths))
    cli_report = inspect_binary("Codex CLI", optional_codex_bin(args.codex_bin))
    registry = codex_registry_with_cache(cli_report)
    feature = codex_feature_lookup(args.feature, registry)
    if not feature:
        die(f"Unknown control: {args.feature}. Run `labkit codex list --all`.")
    state, source = feature_state(feature, config, registry)
    if ui.JSON_OUTPUT:
        emit_json({"command": "codex info", "source": binary_report_data(cli_report), "feature": feature_info_data(feature, state, source)})
        return
    render_feature_info(feature, state, source)


def cmd_status(args: argparse.Namespace) -> None:
    cmd_list(args)


def cmd_verify(args: argparse.Namespace) -> None:
    paths = resolve_paths()
    config = parse_toml(read_config(paths))
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

    if ui.JSON_OUTPUT:
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
    if ui.JSON_OUTPUT:
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


def cmd_select(args: argparse.Namespace) -> None:
    if ui.JSON_OUTPUT:
        die("`select` is interactive. Use `list --json` plus `enable --dry-run`/`disable --dry-run` for automation.")
    paths = resolve_paths()
    codex_bin = optional_codex_bin(args.codex_bin)
    config = parse_toml(read_config(paths))
    registry = codex_registry_with_cache(inspect_binary("Codex", codex_bin))
    catalog = marked_features(codex_features_from_registry(registry, include_all=bool(getattr(args, "all", False))), args)
    selectable = [feature for feature in catalog if feature.selectable]

    if sys.stdin.isatty() and sys.stdout.isatty():
        try:
            changes = run_selection_tui(
                selectable,
                lambda feature: feature_state(feature, config, registry),
                lambda chosen, target: planned_changes_for_target(chosen, target, config, registry),
            )
            if not changes:
                status_line("info", "No changes")
                return
            apply_feature_changes("select", changes, paths, codex_bin)
            return
        except Exception as exc:
            status_line("warn", "Checklist unavailable", str(exc))

    banner("Interactive Codex control selection. Nothing changes until the final confirmation.")
    render_feature_catalog(catalog, lambda feature: feature_state(feature, config, registry), numbered=True)

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
        if feature.kind == "manual":
            die(f"{feature.name} cannot be disabled automatically by this CLI.")
        features.append(feature)

    changes = [FeatureChange(feature=feature, enabled=False) for feature in features]
    apply_feature_changes("disable", changes, paths, codex_bin, dry_run=args.dry_run)


def cmd_codex_check(args: argparse.Namespace) -> None:
    return cmd_check(args)


def cmd_codex_list(args: argparse.Namespace) -> None:
    return cmd_list(args)


def cmd_codex_info(args: argparse.Namespace) -> None:
    return cmd_info(args)


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
