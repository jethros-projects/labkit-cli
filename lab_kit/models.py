"""Shared data models for Lab Kit."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

APP_NAME = "Lab Kit CLI"
CLAUDE_SCHEMA_URL = "https://json.schemastore.org/claude-code-settings.json"
TARGET_MODEL = "gpt-5.5"
CATALOG_CONTEXT_WINDOW = 1_052_632
MODEL_CONTEXT_WINDOW = 1_000_000
AUTO_COMPACT_TOKEN_LIMIT = 800_000

RISK_LEVELS = ("low", "medium", "high", "internal")
STABILITY_LEVELS = ("stable", "experimental", "beta", "internal")
VERIFICATION_MODES = ("runtime", "config", "manual", "none")
RISK_ORDER = {name: index for index, name in enumerate(RISK_LEVELS)}


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
    risk_level: str = "low"
    stability: str = "stable"
    recommended: bool = True
    verification_mode: str = "none"
    notes: str = ""
    tags: tuple[str, ...] = field(default_factory=tuple)
    dependencies: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    limitations: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    verification: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    sources: tuple[dict[str, Any], ...] = field(default_factory=tuple)


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
