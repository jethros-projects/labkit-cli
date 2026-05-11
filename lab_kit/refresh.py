"""Refresh cached feature knowledge."""

from __future__ import annotations

import argparse
import os
from datetime import datetime
from typing import Any

from . import ui
from .claude import schema_env_properties, schema_properties, validate_claude_schema
from .codex import binary_report_data, inspect_binary, registry_data
from .models import CLAUDE_SCHEMA_URL
from .ui import banner, emit_json, kv, section, status_line
from .utils import (
    cached_claude_schema_path,
    cached_codex_registry_path,
    die,
    fetch_json_url,
    optional_codex_bin,
    user_data_home,
    write_cached_json,
)


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
        ok, reason = validate_claude_schema(schema)
        if not ok:
            die(f"Fetched Claude Code schema is invalid: {reason}")
        path = cached_claude_schema_path()
        write_cached_json(path, schema)
        results["claude"] = {
            "ok": True,
            "path": str(path),
            "schema_url": schema_url,
            "schema_validated": True,
            "top_level_settings": len(schema_properties(schema)),
            "env_settings": len(schema_env_properties(schema)),
        }

    if ui.JSON_OUTPUT:
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
        kv("schema validated", claude["schema_validated"])
        kv("top-level settings", claude["top_level_settings"])
        kv("env settings", claude["env_settings"])
