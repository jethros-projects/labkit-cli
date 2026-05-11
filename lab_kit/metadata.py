"""Metadata loading and smart-marking helpers."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from importlib import resources
from typing import Any

from lab_kit import __version__

from .models import RISK_LEVELS, RISK_ORDER, STABILITY_LEVELS, VERIFICATION_MODES, Feature

STEP_FIELDS = ("dependencies", "limitations", "sources")


def package_version() -> str:
    return __version__


def package_json(filename: str) -> dict[str, Any]:
    try:
        text = resources.files("lab_kit").joinpath("data").joinpath(filename).read_text(encoding="utf-8")
        data = json.loads(text)
    except (FileNotFoundError, json.JSONDecodeError, ModuleNotFoundError):
        return {}
    return data if isinstance(data, dict) else {}


def load_codex_metadata() -> dict[str, Any]:
    return package_json("codex_feature_metadata.json")


def load_claude_metadata() -> dict[str, Any]:
    return package_json("claude_feature_metadata.json")


def metadata_entry(metadata: dict[str, Any], key_or_name: str) -> dict[str, Any]:
    features = metadata.get("features")
    if not isinstance(features, dict):
        return {}
    entry = features.get(key_or_name)
    if isinstance(entry, dict):
        return entry
    for value in features.values():
        if not isinstance(value, dict):
            continue
        raw_aliases = value.get("aliases")
        aliases = [str(item) for item in raw_aliases] if isinstance(raw_aliases, list) else []
        if value.get("name") == key_or_name or key_or_name in aliases:
            return value
    return {}


def metadata_defaults(metadata: dict[str, Any], *names: str) -> list[dict[str, Any]]:
    defaults = metadata.get("defaults")
    if not isinstance(defaults, dict):
        return []
    entries: list[dict[str, Any]] = []
    for name in names:
        entry = defaults.get(name)
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


def metadata_cluster_entry(metadata: dict[str, Any], cluster: str) -> dict[str, Any]:
    clusters = metadata.get("clusters")
    if not isinstance(clusters, dict):
        return {}
    entry = clusters.get(cluster)
    return entry if isinstance(entry, dict) else {}


def metadata_items(value: Any) -> tuple[dict[str, Any], ...]:
    if not value:
        return ()
    values = value if isinstance(value, list) else [value]
    items: list[dict[str, Any]] = []
    for item in values:
        if isinstance(item, dict):
            items.append(dict(item))
        elif isinstance(item, str):
            items.append({"detail": item})
    return tuple(items)


def merge_metadata_items(*groups: tuple[dict[str, Any], ...]) -> tuple[dict[str, Any], ...]:
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for group in groups:
        for item in group:
            marker = json.dumps(item, sort_keys=True)
            if marker in seen:
                continue
            seen.add(marker)
            merged.append(item)
    return tuple(merged)


def _valid_choice(value: Any, allowed: tuple[str, ...], fallback: str) -> str:
    if isinstance(value, str) and value in allowed:
        return value
    return fallback


def metadata_verification_steps(entry: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    steps = metadata_items(entry.get("verification_steps"))
    legacy = entry.get("verification")
    if isinstance(legacy, list):
        steps = merge_metadata_items(steps, metadata_items(legacy))
    return steps


def with_feature_metadata(feature: Feature, *entries: dict[str, Any]) -> Feature:
    updates: dict[str, Any] = {}
    for field_name in STEP_FIELDS:
        existing = getattr(feature, field_name)
        additions = [metadata_items(entry.get(field_name)) for entry in entries if isinstance(entry, dict)]
        updates[field_name] = merge_metadata_items(existing, *additions)

    verification_additions = [metadata_verification_steps(entry) for entry in entries if isinstance(entry, dict)]
    updates["verification"] = merge_metadata_items(feature.verification, *verification_additions)

    risk_level = feature.risk_level
    stability = feature.stability
    recommended = feature.recommended
    verification_mode = feature.verification_mode
    notes = feature.notes
    tags: list[str] = list(feature.tags)
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if "risk_level" in entry:
            risk_level = _valid_choice(entry.get("risk_level"), RISK_LEVELS, risk_level)
        if "stability" in entry:
            stability = _valid_choice(entry.get("stability"), STABILITY_LEVELS, stability)
        if "recommended" in entry and isinstance(entry.get("recommended"), bool):
            recommended = bool(entry["recommended"])
        if isinstance(entry.get("verification"), str):
            verification_mode = _valid_choice(entry.get("verification"), VERIFICATION_MODES, verification_mode)
        if isinstance(entry.get("notes"), str):
            notes = entry["notes"]
        raw_tags = entry.get("tags")
        if isinstance(raw_tags, list):
            for tag in raw_tags:
                tag_text = str(tag)
                if tag_text not in tags:
                    tags.append(tag_text)

    updates.update(
        {
            "risk_level": risk_level,
            "stability": stability,
            "recommended": recommended,
            "verification_mode": verification_mode,
            "notes": notes,
            "tags": tuple(tags),
        }
    )
    return replace(feature, **updates)


def control_id_from_key(key: str) -> str:
    text = key.replace(".", "-").replace("_", "-")
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", text)
    return text.lower()


def title_from_id(name: str) -> str:
    return " ".join(part.upper() if part in {"ui", "api", "mcp"} else part.capitalize() for part in re.split(r"[-_.]+", name))


def risk_at_least(feature: Feature, minimum: str | None) -> bool:
    if not minimum:
        return True
    min_rank = RISK_ORDER.get(minimum, 0)
    return RISK_ORDER.get(feature.risk_level, 0) >= min_rank


def filter_features_by_marking(features: list[Feature], *, risk: str | None = None, include_internal: bool = True) -> list[Feature]:
    filtered = [feature for feature in features if risk_at_least(feature, risk)]
    if include_internal:
        return filtered
    return [feature for feature in filtered if feature.risk_level != "internal"]
