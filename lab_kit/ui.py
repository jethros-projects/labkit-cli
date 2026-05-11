"""Terminal UI helpers for Lab Kit."""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import textwrap
import threading
import time
from contextlib import contextmanager
from typing import Any

from .models import APP_NAME, RISK_ORDER, CliError, Feature

COLOR_ENABLED = True
JSON_OUTPUT = False
PROGRESS_ENABLED = True
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


class Style:
    RESET = "0"
    BOLD = "1"
    DIM = "2"
    RED = "31"
    GREEN = "32"
    YELLOW = "33"
    BLUE = "34"
    MAGENTA = "35"
    CYAN = "36"
    GRAY = "90"


def configure(*, color_enabled: bool, json_output: bool, progress_enabled: bool) -> None:
    global COLOR_ENABLED, JSON_OUTPUT, PROGRESS_ENABLED
    COLOR_ENABLED = color_enabled
    JSON_OUTPUT = json_output
    PROGRESS_ENABLED = progress_enabled


def color_supported(stream: Any = sys.stdout) -> bool:
    if not COLOR_ENABLED:
        return False
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("CLICOLOR_FORCE") == "1":
        return True
    return bool(getattr(stream, "isatty", lambda: False)()) and os.environ.get("TERM", "dumb") != "dumb"


def paint(text: Any, *codes: str, stream: Any = sys.stdout) -> str:
    rendered = str(text)
    if not codes or not color_supported(stream):
        return rendered
    return f"\033[{';'.join(codes)}m{rendered}\033[0m"


def visible_len(text: str) -> int:
    return len(ANSI_RE.sub("", text))


def pad(text: str, width: int) -> str:
    return text + (" " * max(0, width - visible_len(text)))


def clip(text: str, width: int) -> str:
    if visible_len(text) <= width:
        return text
    if width <= 1:
        return text[:width]
    return text[: width - 1] + "."


def terminal_width(default: int = 96) -> int:
    return max(60, shutil.get_terminal_size((default, 20)).columns)


def muted(text: Any) -> str:
    return paint(text, Style.GRAY)


def strong(text: Any) -> str:
    return paint(text, Style.BOLD)


def accent(text: Any) -> str:
    return paint(text, Style.YELLOW)


def success(text: Any) -> str:
    return paint(text, Style.GREEN)


def warning(text: Any) -> str:
    return paint(text, Style.YELLOW)


def failure(text: Any) -> str:
    return paint(text, Style.RED)


def badge(label: str, tone: str = "info") -> str:
    colors = {
        "ok": Style.GREEN,
        "on": Style.GREEN,
        "low": Style.GREEN,
        "stable": Style.GREEN,
        "warn": Style.YELLOW,
        "medium": Style.YELLOW,
        "experimental": Style.YELLOW,
        "beta": Style.YELLOW,
        "partial": Style.YELLOW,
        "fail": Style.RED,
        "high": Style.RED,
        "off": Style.RED,
        "manual": Style.YELLOW,
        "internal": Style.MAGENTA,
        "info": Style.YELLOW,
    }
    return paint(f"[{label}]", colors.get(tone, Style.YELLOW))


def state_badge(state: str) -> str:
    return badge(state, state)


def risk_badge(risk_level: str) -> str:
    return badge(risk_level, risk_level)


def stability_badge(stability: str) -> str:
    return badge(stability, stability)


def state_text(state: str) -> str:
    if state == "on":
        return success(state)
    if state == "off":
        return failure(state)
    if state == "partial":
        return warning(state)
    return warning(state)


def banner(subtitle: str | None = None) -> None:
    say(strong(accent(APP_NAME)))
    if subtitle:
        say(muted(subtitle))
    say(muted("-" * min(terminal_width(), 88)))


def section(title: str) -> None:
    say("")
    say(strong(accent(title)))


def kv(label: str, value: Any) -> None:
    say(f"  {muted(pad(label + ':', 24))} {value}")


def status_line(tone: str, title: str, detail: str | None = None) -> None:
    line = f"  {badge(tone, tone)} {strong(title)}"
    if detail:
        line += f" {muted('- ' + detail)}"
    say(line)


def wrap_lines(text: str, indent: str = "      ", extra_indent: str | None = None, width_offset: int = 0) -> list[str]:
    width = max(48, terminal_width() - len(indent) - width_offset)
    return textwrap.wrap(
        text,
        width=width,
        initial_indent=indent,
        subsequent_indent=extra_indent or indent,
        break_long_words=False,
        break_on_hyphens=False,
    )


def say_wrapped(text: str, indent: str = "      ") -> None:
    for line in wrap_lines(text, indent=indent):
        say(line)


def ask(prompt: str, *styles: str) -> str:
    value = input(paint(prompt, *styles))
    if not sys.stdin.isatty():
        say("")
    return value


def say(message: str = "") -> None:
    print(message)


def emit_json(data: Any) -> None:
    print(json.dumps(data, indent=2, sort_keys=True))


def spinner_supported() -> bool:
    if not PROGRESS_ENABLED:
        return False
    if JSON_OUTPUT:
        return False
    if os.environ.get("CI"):
        return False
    return bool(getattr(sys.stderr, "isatty", lambda: False)()) and os.environ.get("TERM", "dumb") != "dumb"


@contextmanager
def spinner(label: str | None):
    if not label or not spinner_supported():
        yield
        return

    stop = threading.Event()
    frames = "|/-\\"

    def animate() -> None:
        index = 0
        while not stop.is_set():
            frame = paint(frames[index % len(frames)], Style.YELLOW, stream=sys.stderr)
            sys.stderr.write(f"\r{frame} {label}")
            sys.stderr.flush()
            index += 1
            time.sleep(0.08)
        sys.stderr.write("\r" + (" " * (visible_len(label) + 8)) + "\r")
        sys.stderr.flush()

    thread = threading.Thread(target=animate, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join()


FEATURE_NAME_WIDTH = 32
FEATURE_SOURCE_WIDTH = 18


def feature_table_header(numbered: bool) -> None:
    prefix = "#" if numbered else ""
    columns = (
        muted(pad(prefix, 4)),
        muted(pad("control", FEATURE_NAME_WIDTH)),
        muted(pad("state", 8)),
        muted(pad("risk", 12)),
        muted(pad("stability", 16)),
        muted(pad("source", FEATURE_SOURCE_WIDTH)),
        muted("type"),
    )
    say(f"  {columns[0]}  {columns[1]} {columns[2]} {columns[3]} {columns[4]} {columns[5]} {columns[6]}")


def print_rows(rows: list[tuple[str, ...]], indent: str = "  ") -> None:
    widths = [max(visible_len(row[i]) for row in rows) for i in range(len(rows[0]))]
    for row in rows:
        say(indent + "  ".join(pad(value, widths[i]) for i, value in enumerate(row)).rstrip())


def grouped_features(features: list[Feature] | None = None) -> dict[str, list[Feature]]:
    groups: dict[str, list[Feature]] = {}
    for feature in features or []:
        groups.setdefault(feature.cluster, []).append(feature)
    return groups


def feature_line_prefix(number: int | None, feature: Feature) -> str:
    if number is None:
        return " " * 4
    if feature.selectable:
        return f"{number:>3}."
    return "  --"


def metadata_line(item: dict[str, Any]) -> str:
    label = str(item.get("label") or item.get("kind") or item.get("type") or "").strip()
    detail = str(item.get("detail") or item.get("url") or "").strip()
    if label and detail:
        return f"{label}: {detail}"
    return label or detail


def render_metadata_items(title: str, items: tuple[dict[str, Any], ...], indent: str = "        ") -> None:
    if not items:
        return
    say(f"{indent}{muted(title)}")
    for item in items:
        line = metadata_line(item)
        if not line:
            continue
        command = item.get("command")
        if command:
            line = f"{line} ({command})"
        say_wrapped(f"- {line}", indent=f"{indent}  ")


def render_feature_details(feature: Feature, indent: str = "        ") -> None:
    render_metadata_items("Dependencies", feature.dependencies, indent=indent)
    render_metadata_items("Limitations", feature.limitations, indent=indent)
    render_metadata_items("Verification", feature.verification, indent=indent)
    render_metadata_items("Sources", feature.sources, indent=indent)


def feature_needs_warning(feature: Feature) -> bool:
    return RISK_ORDER.get(feature.risk_level, 0) >= RISK_ORDER["medium"]


def render_feature_warning(feature: Feature, indent: str = "        ") -> None:
    if feature_needs_warning(feature) and feature.notes:
        say_wrapped(f"Warning: {feature.notes}", indent=indent)


def render_feature(feature: Feature, state: str, source: str, number: int | None = None, details: bool = False) -> None:
    prefix = feature_line_prefix(number, feature)
    name = pad(strong(feature.title), FEATURE_NAME_WIDTH)
    state_label = pad(state_badge(state), 8)
    risk_label = pad(risk_badge(feature.risk_level), 12)
    stability_label = pad(stability_badge(feature.stability), 16)
    source_label = muted(pad(clip(source, FEATURE_SOURCE_WIDTH), FEATURE_SOURCE_WIDTH))
    type_label = muted(feature.stage)
    say(f"  {muted(prefix)}  {name} {state_label} {risk_label} {stability_label} {source_label} {type_label}")
    say_wrapped(f"{feature.name}: {feature.description}", indent="        ")
    render_feature_warning(feature)
    if details:
        render_feature_details(feature)


def render_feature_catalog(
    features: list[Feature],
    state_getter: Any,
    numbered: bool = False,
    details: bool = False,
) -> None:
    number = 1
    for cluster, group in grouped_features(features).items():
        section(cluster)
        feature_table_header(numbered)
        for feature in group:
            state, source = state_getter(feature)
            shown_number = number if numbered else None
            render_feature(feature, state, source, shown_number, details=details)
            if numbered and feature.selectable:
                number += 1


def feature_info_data(feature: Feature, state: str, source: str) -> dict[str, Any]:
    return {
        "name": feature.name,
        "title": feature.title,
        "cluster": feature.cluster,
        "stage": feature.stage,
        "kind": feature.kind,
        "selectable": feature.selectable,
        "status": state,
        "source": source,
        "description": feature.description,
        "key": feature.key,
        "value": feature.value,
        "inactive_value": feature.inactive_value,
        "registry_keys": list(feature.registry_keys),
        "risk_level": feature.risk_level,
        "stability": feature.stability,
        "recommended": feature.recommended,
        "verification": feature.verification_mode,
        "notes": feature.notes,
        "tags": list(feature.tags),
        "dependencies": list(feature.dependencies),
        "limitations": list(feature.limitations),
        "verification_steps": list(feature.verification),
        "sources": list(feature.sources),
    }


def render_feature_info(feature: Feature, state: str, source: str) -> None:
    banner(feature.title)
    kv("control id", feature.name)
    if feature.key:
        kv("writes", feature.key)
    if feature.registry_keys:
        kv("registry keys", ", ".join(feature.registry_keys))
    kv("state", f"{state} from {source}")
    kv("stage", feature.stage)
    kv("risk", risk_badge(feature.risk_level))
    kv("stability", stability_badge(feature.stability))
    kv("recommended", "yes" if feature.recommended else "no")
    kv("verification", feature.verification_mode)
    if feature.tags:
        kv("tags", ", ".join(feature.tags))
    say("")
    say_wrapped(feature.description, indent="  ")
    if feature.notes:
        section("Notes")
        say_wrapped(feature.notes, indent="  ")

    section("Dependencies")
    if feature.dependencies:
        for item in feature.dependencies:
            say_wrapped(f"- {metadata_line(item)}", indent="  ")
    else:
        status_line("info", "No extra dependencies recorded", "Lab Kit only knows the control surface")

    section("Limitations")
    if feature.limitations:
        for item in feature.limitations:
            say_wrapped(f"- {metadata_line(item)}", indent="  ")
    else:
        status_line("info", "No specific limitations recorded")

    section("Verification")
    if feature.verification:
        for item in feature.verification:
            line = metadata_line(item)
            if item.get("command"):
                line = f"{line} ({item['command']})"
            say_wrapped(f"- {line}", indent="  ")
    else:
        status_line("warn", "No automated verification recorded")

    if feature.sources:
        section("Sources")
        for item in feature.sources:
            label = str(item.get("label") or item.get("type") or "source")
            url = item.get("url")
            checked = item.get("checked_at")
            detail = f"{url}" if url else metadata_line(item)
            if checked:
                detail = f"{detail} (checked {checked})"
            kv(label, detail)


def feature_data_for(feature: Feature, state: str, source: str, index: int | None = None) -> dict[str, Any]:
    data = feature_info_data(feature, state, source)
    data["index"] = index
    return data


def feature_catalog_data_for(features: list[Feature], state_getter: Any) -> list[dict[str, Any]]:
    data: list[dict[str, Any]] = []
    index = 1
    for feature in features:
        state, source = state_getter(feature)
        data.append(feature_data_for(feature, state, source, index if feature.selectable else None))
        if feature.selectable:
            index += 1
    return data


def selected_features_from_tokens(tokens: list[str], selectable: list[Feature]) -> list[Feature]:
    by_number = {str(index): feature for index, feature in enumerate(selectable, start=1)}
    by_name = {feature.name: feature for feature in selectable}
    chosen: list[Feature] = []
    seen: set[str] = set()
    for token in tokens:
        feature = by_number.get(token) or by_name.get(token)
        if not feature:
            raise CliError(f"Unknown selection: {token}")
        if not feature.selectable:
            raise CliError(f"{feature.name} is reference-only and cannot be selected.")
        if feature.name not in seen:
            chosen.append(feature)
            seen.add(feature.name)
    return chosen


def target_enabled_from_text(text: str) -> str:
    target = text.strip().lower()
    aliases = {
        "active": "active",
        "activate": "active",
        "enable": "active",
        "enabled": "active",
        "on": "active",
        "inactive": "inactive",
        "deactivate": "inactive",
        "disable": "inactive",
        "disabled": "inactive",
        "off": "inactive",
        "toggle": "toggle",
        "flip": "toggle",
    }
    if target not in aliases:
        raise CliError("Choose one target state: active, inactive, or toggle.")
    return aliases[target]


def state_is_active(state: str) -> bool:
    return state in {"on", "partial"}


def preview_enabled_for_state(state: str, target: str) -> bool:
    return not state_is_active(state) if target == "toggle" else target == "active"


def run_selection_tui(
    features: list[Feature],
    state_getter: Any,
    planner: Any,
) -> list[Any] | None:
    import curses

    rows: list[tuple[str, str, Feature | None]] = []
    states = {feature.name: state_getter(feature) for feature in features}
    for cluster, group in grouped_features(features).items():
        selectable_group = [feature for feature in group if feature.selectable]
        if not selectable_group:
            continue
        rows.append(("header", cluster, None))
        for feature in selectable_group:
            rows.append(("item", "", feature))

    item_indexes = [index for index, row in enumerate(rows) if row[0] == "item"]
    if not item_indexes:
        raise CliError("No selectable features found.")

    def next_item(index: int, direction: int) -> int:
        position = item_indexes.index(index)
        return item_indexes[(position + direction) % len(item_indexes)]

    def draw_text(stdscr: Any, y: int, x: int, text: str, attr: int = 0) -> None:
        height, width = stdscr.getmaxyx()
        if y < 0 or y >= height or x >= width:
            return
        try:
            stdscr.addnstr(y, x, text, max(0, width - x - 1), attr)
        except curses.error:
            pass

    def app(stdscr: Any) -> list[Any] | None:
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_YELLOW, -1)
            curses.init_pair(2, curses.COLOR_BLACK, curses.COLOR_YELLOW)
            curses.init_pair(3, curses.COLOR_GREEN, -1)
            curses.init_pair(4, curses.COLOR_RED, -1)
            curses.init_pair(5, curses.COLOR_RED, -1)
            curses.init_pair(6, curses.COLOR_YELLOW, -1)
            curses.init_pair(7, curses.COLOR_WHITE, -1)
            curses.init_pair(8, curses.COLOR_BLACK, curses.COLOR_WHITE)
            curses.init_pair(9, curses.COLOR_MAGENTA, -1)

        cursor = item_indexes[0]
        current_enabled = {feature.name: state_is_active(states[feature.name][0]) for feature in features}
        desired_enabled = dict(current_enabled)
        top = 0
        notice = ""

        while True:
            stdscr.erase()
            height, width = stdscr.getmaxyx()
            visible_height = max(4, height - 7)

            if cursor < top:
                top = cursor
            if cursor >= top + visible_height:
                top = cursor - visible_height + 1

            accent_attr = curses.color_pair(1) if curses.has_colors() else curses.A_BOLD
            active_attr = curses.color_pair(3) if curses.has_colors() else curses.A_BOLD
            inactive_attr = curses.color_pair(4) if curses.has_colors() else curses.A_BOLD
            muted_attr = curses.color_pair(7) if curses.has_colors() else 0
            header_attr = curses.A_BOLD | (curses.color_pair(6) if curses.has_colors() else 0)
            cursor_attr = curses.color_pair(8) if curses.has_colors() else curses.A_REVERSE
            high_attr = curses.color_pair(4) if curses.has_colors() else curses.A_BOLD
            internal_attr = curses.color_pair(9) if curses.has_colors() else curses.A_BOLD

            draw_text(stdscr, 0, 0, APP_NAME, curses.A_BOLD | accent_attr)
            draw_text(stdscr, 1, 0, "Space toggles a control on/off. a marks active, i/d marks inactive. Enter applies. q cancels.")
            draw_text(stdscr, 2, 0, "Risk and stability badges are visible; marked internal/reference rows remain read-only.", accent_attr)
            draw_text(stdscr, 3, 0, "-" * max(0, width - 1))

            for screen_y, row_index in enumerate(range(top, min(len(rows), top + visible_height)), start=4):
                kind, label, feature = rows[row_index]
                if kind == "header":
                    draw_text(stdscr, screen_y, 0, label, header_attr)
                    continue

                assert feature is not None
                state, source = states[feature.name]
                checked = desired_enabled[feature.name]
                current = current_enabled[feature.name]
                change = f" -> {'active' if checked else 'inactive'}" if checked != current else ""
                marker = "[x]" if checked else "[ ]"
                pointer = ">" if row_index == cursor else " "
                base_attr = cursor_attr if row_index == cursor else 0
                if checked != current and row_index != cursor:
                    base_attr |= accent_attr
                risk_attr = (
                    internal_attr if feature.risk_level == "internal" else high_attr if feature.risk_level == "high" else accent_attr
                )
                draw_text(stdscr, screen_y, 0, f"{pointer} {marker} ", base_attr)
                draw_text(stdscr, screen_y, 8, f"{state:<7}", active_attr if state == "on" else inactive_attr)
                draw_text(stdscr, screen_y, 16, f"{feature.risk_level:<8}", risk_attr)
                draw_text(stdscr, screen_y, 25, f"{feature.stability:<12}", muted_attr)
                title = f"{feature.title:<30}"
                draw_text(stdscr, screen_y, 38, f"{title} {feature.name}{change}", base_attr)
                if width > 112:
                    draw_text(stdscr, screen_y, min(112, width - 1), source, muted_attr)

            footer_y = height - 2
            changed_count = sum(1 for feature in features if desired_enabled[feature.name] != current_enabled[feature.name])
            draw_text(stdscr, footer_y, 0, f"{changed_count} pending change{'s' if changed_count != 1 else ''}")
            if notice:
                attr = curses.color_pair(5) if curses.has_colors() else curses.A_BOLD
                draw_text(stdscr, footer_y + 1, 0, notice, attr)
            else:
                draw_text(stdscr, footer_y + 1, 0, "Tip: use --risk high to focus the list on high-risk/internal controls.")

            key = stdscr.getch()
            notice = ""
            if key in (ord("q"), 27):
                return None
            if key in (curses.KEY_UP, ord("k")):
                cursor = next_item(cursor, -1)
                continue
            if key in (curses.KEY_DOWN, ord("j")):
                cursor = next_item(cursor, 1)
                continue
            if key in (ord("a"), ord("A")):
                feature = rows[cursor][2]
                assert feature is not None
                desired_enabled[feature.name] = True
                continue
            if key in (ord("i"), ord("I"), ord("d"), ord("D")):
                feature = rows[cursor][2]
                assert feature is not None
                desired_enabled[feature.name] = False
                continue
            if key in (ord("t"), ord("T"), ord(" ")):
                feature = rows[cursor][2]
                assert feature is not None
                desired_enabled[feature.name] = not desired_enabled[feature.name]
                continue
            if key in (10, 13, curses.KEY_ENTER):
                changed = [feature for feature in features if desired_enabled[feature.name] != current_enabled[feature.name]]
                if not changed:
                    notice = "No changes to apply."
                    continue
                changes: list[Any] = []
                try:
                    for feature in changed:
                        changes.extend(planner([feature], "active" if desired_enabled[feature.name] else "inactive"))
                    return changes
                except CliError as exc:
                    notice = str(exc)
                    continue

    return curses.wrapper(app)
