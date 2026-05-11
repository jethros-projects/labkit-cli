#!/usr/bin/env python3
"""Command-line parser and routing for Lab Kit."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from . import ui
from .claude import (
    cmd_claude_check,
    cmd_claude_code_check,
    cmd_claude_code_disable,
    cmd_claude_code_discover,
    cmd_claude_code_enable,
    cmd_claude_code_info,
    cmd_claude_code_list,
    cmd_claude_code_select,
    cmd_claude_disable,
    cmd_claude_discover,
    cmd_claude_enable,
    cmd_claude_info,
    cmd_claude_list,
    cmd_claude_select,
)
from .codex import (
    cmd_check,
    cmd_codex_check,
    cmd_codex_disable,
    cmd_codex_discover,
    cmd_codex_enable,
    cmd_codex_info,
    cmd_codex_list,
    cmd_codex_select,
    cmd_codex_status,
    cmd_codex_verify,
    cmd_disable,
    cmd_discover,
    cmd_enable,
    cmd_info,
    cmd_list,
    cmd_select,
    cmd_status,
    cmd_verify,
)
from .metadata import package_version
from .models import CliError
from .refresh import cmd_update_features
from .self_update import cmd_self_update

RISK_CHOICES = ["low", "medium", "high", "internal"]


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
    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
        metavar="{codex,claude-code,update,upgrade,update-features}",
    )

    def command_parser(subparsers_: argparse._SubParsersAction, name: str, **kwargs: Any) -> argparse.ArgumentParser:
        hidden = kwargs.get("help") is argparse.SUPPRESS
        kwargs.setdefault("formatter_class", help_formatter)
        parser_ = polish_help(subparsers_.add_parser(name, **kwargs))
        if hidden:
            subparsers_._choices_actions = [action for action in subparsers_._choices_actions if action.dest != name]
        return parser_

    def add_json(parser_: argparse.ArgumentParser) -> None:
        parser_.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="Emit machine-readable JSON.")

    def add_marking_filters(parser_: argparse.ArgumentParser) -> None:
        parser_.add_argument("--risk", choices=RISK_CHOICES, help="Focus on controls at this risk level or higher.")
        parser_.add_argument(
            "--include-internal",
            action="store_true",
            help="Compatibility flag; internal controls are already visible and marked.",
        )

    def add_self_update_options(parser_: argparse.ArgumentParser, command: str) -> None:
        parser_.add_argument("--ref", help="Branch, tag, or commit to install. Defaults to LABKIT_REF, REF, or main.")
        parser_.add_argument("--archive-url", help="Install from an explicit tar.gz archive URL or local archive path.")
        parser_.add_argument("--install-dir", help="Directory that contains the labkit executable. Defaults to the current install.")
        parser_.add_argument("--sha256", help="Expected SHA256 for the downloaded archive.")
        parser_.add_argument("--dry-run", action="store_true", help="Show the update plan without downloading or writing files.")
        parser_.add_argument("--repo-owner", help=argparse.SUPPRESS)
        parser_.add_argument("--repo-name", help=argparse.SUPPRESS)
        add_json(parser_)
        parser_.set_defaults(func=cmd_self_update, self_update_command=command)

    def add_codex_commands(parent: argparse.ArgumentParser) -> None:
        codex_subparsers = parent.add_subparsers(
            dest="codex_command",
            required=True,
            metavar="{check,list,info,discover,select,enable,disable,verify}",
        )
        c_check = command_parser(codex_subparsers, "check", help="Check Codex CLI installation and state.")
        add_json(c_check)
        c_check.set_defaults(func=cmd_codex_check)
        c_doctor = command_parser(codex_subparsers, "doctor", help=argparse.SUPPRESS)
        add_json(c_doctor)
        c_doctor.set_defaults(func=cmd_codex_check)
        c_list = command_parser(codex_subparsers, "list", help="Show Codex controls grouped by area.")
        c_list.add_argument("--all", action="store_true", help="Include every available registry flag, not just curated controls.")
        c_list.add_argument("--details", action="store_true", help="Show dependencies, limitations, and verification notes inline.")
        add_marking_filters(c_list)
        add_json(c_list)
        c_list.set_defaults(func=cmd_codex_list)
        c_info = command_parser(codex_subparsers, "info", help="Explain one Codex control's metadata and verification.")
        add_json(c_info)
        c_info.add_argument("feature", metavar="control-id")
        c_info._positionals.title = "Arguments"
        c_info.set_defaults(func=cmd_codex_info)
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
        add_marking_filters(c_select)
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

    def add_claude_code_commands(parent: argparse.ArgumentParser, alias: str) -> None:
        claude_subparsers = parent.add_subparsers(
            dest=f"{alias.replace('-', '_')}_command",
            required=True,
            metavar="{check,list,info,discover,select,enable,disable}",
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
        cc_list.add_argument("--details", action="store_true", help="Show dependencies, limitations, and verification notes inline.")
        add_marking_filters(cc_list)
        add_json(cc_list)
        cc_list.set_defaults(func=cmd_claude_code_list if alias == "claude-code" else cmd_claude_list)
        cc_info = command_parser(claude_subparsers, "info", help="Explain one Claude Code control's metadata and verification.")
        add_claude_scope(cc_info)
        add_json(cc_info)
        cc_info.add_argument("feature", metavar="control-id")
        cc_info._positionals.title = "Arguments"
        cc_info.set_defaults(func=cmd_claude_code_info if alias == "claude-code" else cmd_claude_info)
        cc_discover = command_parser(claude_subparsers, "discover", help="Show curated, schema, and settings-file Claude Code keys.")
        add_claude_scope(cc_discover)
        add_json(cc_discover)
        cc_discover.set_defaults(func=cmd_claude_code_discover if alias == "claude-code" else cmd_claude_discover)
        cc_select = command_parser(claude_subparsers, "select", help="Open the interactive Claude Code checklist.")
        add_claude_scope(cc_select)
        cc_select.add_argument("--all", action="store_true", help="Include official schema keys in the selector.")
        add_marking_filters(cc_select)
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

    update = command_parser(subparsers, "update", help="Update Lab Kit CLI in place from GitHub.")
    add_self_update_options(update, "update")
    upgrade = command_parser(subparsers, "upgrade", help="Alias for `update`.")
    add_self_update_options(upgrade, "upgrade")

    check = command_parser(subparsers, "check", help=argparse.SUPPRESS)
    add_json(check)
    check.set_defaults(func=cmd_check)
    doctor = command_parser(subparsers, "doctor", help=argparse.SUPPRESS)
    add_json(doctor)
    doctor.set_defaults(func=cmd_check)
    list_parser = command_parser(subparsers, "list", help=argparse.SUPPRESS)
    list_parser.add_argument("--all", action="store_true", help="Include every available registry flag, not just curated controls.")
    list_parser.add_argument("--details", action="store_true", help="Show dependencies, limitations, and verification notes inline.")
    add_marking_filters(list_parser)
    add_json(list_parser)
    list_parser.set_defaults(func=cmd_list)
    info = command_parser(subparsers, "info", help=argparse.SUPPRESS)
    add_json(info)
    info.add_argument("feature", metavar="control-id")
    info._positionals.title = "Arguments"
    info.set_defaults(func=cmd_info)
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
    add_marking_filters(select)
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
    parser = build_parser()
    args = parser.parse_args(argv)
    json_output = bool(getattr(args, "json", False))
    ui.configure(color_enabled=not args.no_color and not json_output, json_output=json_output, progress_enabled=not args.no_progress)
    try:
        result = args.func(args)
        return int(result or 0)
    except CliError as exc:
        if ui.JSON_OUTPUT:
            print(json.dumps({"ok": False, "error": {"type": "CliError", "message": str(exc)}}, sort_keys=True), file=sys.stderr)
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        if ui.JSON_OUTPUT:
            print(
                json.dumps({"ok": False, "error": {"type": "KeyboardInterrupt", "message": "interrupted"}}, sort_keys=True),
                file=sys.stderr,
            )
        else:
            print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
