#!/usr/bin/env python3
"""Compare token usage, API-equivalent cost, and tool calls in Claude Code sessions."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import sys
from collections import Counter, defaultdict
from pathlib import Path


# USD per million tokens, on-demand standard tier, Bedrock "Global Cross-region
# Inference" prices: input, 5-minute cache write, 1-hour cache write, cache read, output.
# Source: https://aws.amazon.com/bedrock/pricing/ - Anthropic (price data published 2026-11-09).
# Geo / in-region model IDs (us.anthropic.*, eu.anthropic.*, anthropic.*) cost
# REGIONAL_MULTIPLIER more; true for every model below, not for Claude 4/4.1/3.x (not listed).
# A session is priced regional if its transcript shows such an ID (assistant records or
# the cost-state record's modelUsage keys), otherwise global.
# Not modelled: batch, Haiku 5.5 long-context rates, priority/flex/reserved tiers.
PRICING_DATE = "2026-11-09"
REGIONAL_MULTIPLIER = 1.10
MAX_LINE_BYTES = 64 * 1024 * 1024
RATES = {
    "fable-5-2": (8.0, 10.0, 16.0, 0.20, 40.0),
    "mythos-5-2": (8.0, 10.0, 16.0, 0.20, 40.0),
    "fable-5-1": (10.0, 12.5, 20.0, 0.25, 50.0),
    "mythos-5-1": (10.0, 12.5, 20.0, 0.25, 50.0),
    "fable-5": (10.0, 12.5, 20.0, 1.00, 50.0),
    "mythos-5": (10.0, 12.5, 20.0, 1.00, 50.0),
    "opus-5-5": (4.0, 5.0, 8.0, 0.20, 20.0),
    "opus-5": (5.0, 6.25, 10.0, 0.50, 25.0),
    "opus-4-8": (5.0, 6.25, 10.0, 0.50, 25.0),
    "opus-4-7": (5.0, 6.25, 10.0, 0.50, 25.0),
    "opus-4-6": (5.0, 6.25, 10.0, 0.50, 25.0),
    "opus-4-5": (5.0, 6.25, 10.0, 0.50, 25.0),
    "sonnet-5-5": (2.0, 2.5, 4.0, 0.10, 10.0),  # cache read halved from 2026-10-07
    "sonnet-5": (2.0, 2.5, 4.0, 0.20, 10.0),
    "sonnet-4-6": (3.0, 3.75, 6.0, 0.30, 15.0),
    "sonnet-4-5": (3.0, 3.75, 6.0, 0.30, 15.0),
    "haiku-5-5": (0.10, 0.125, 0.20, 0.01, 0.50),
    "haiku-4-5": (1.0, 1.25, 2.0, 0.10, 5.0),
}


def safe_text(value: object) -> str:
    """Escape terminal control characters while preserving printable Unicode."""
    return "".join(
        char if char.isprintable() else char.encode("unicode_escape").decode("ascii")
        for char in str(value)
    )


def config_dir(override: str | None) -> Path:
    """--config-dir, else $CLAUDE_CONFIG_DIR (what Claude Code itself honours), else ~/.claude."""
    return Path(override or os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude").expanduser()


def all_sessions(config: Path) -> list[Path]:
    """Every top-level session transcript (projects/<project>/<id>.jsonl), oldest first."""
    projects = config / "projects"
    if not projects.is_dir():
        raise ValueError(f"Claude projects directory is missing: {projects}")
    # Top level only: nested */subagents/*.jsonl belong to their parent session.
    return sorted(projects.glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime)


def find_session(value: str, config: Path) -> Path:
    """Accept a transcript path or resolve a session ID under <config>/projects."""
    path = Path(value).expanduser()
    if path.is_file():
        return path
    if path.exists():
        raise ValueError(f"not a transcript file: {path}")

    projects = config / "projects"
    if not projects.is_dir():
        raise ValueError(
            f"transcript path not found and Claude projects directory is missing: {projects}"
        )
    matches = list(projects.glob(f"**/{value}.jsonl"))
    if not matches:
        raise ValueError(f"session ID not found under {projects}: {value}")
    if len(matches) > 1:
        choices = ", ".join(str(match) for match in matches)
        raise ValueError(
            f"session ID is ambiguous; pass a transcript path instead: {choices}"
        )
    return matches[0]


def is_regional(model: str) -> bool:
    """True for geo / in-region Bedrock IDs (us.anthropic.*, anthropic.*), not global.* or bare names."""
    lowered = model.lower()
    return "anthropic." in lowered and not lowered.startswith("global.")


def model_rates(
    model: str | None, regional: bool = False
) -> tuple[float, float, float, float, float] | None:
    """Rates for a model ID such as claude-sonnet-5-5 or us.anthropic.claude-sonnet-5-5-v1:0."""
    if not model:
        return None
    lowered = model.lower()
    # Exact family match after stripping the vendor prefix and date/version suffixes;
    # substring matching would let "opus-5" swallow "opus-5-5".
    name = lowered.split("claude-", 1)[-1].replace(".", "-")
    name = re.sub(r"(\[.*\]|-v\d+(:\d+)?|-\d{8})+$", "", name)
    rates = RATES.get(name)
    if rates is None:
        return None
    factor = REGIONAL_MULTIPLIER if regional else 1.0
    return tuple(rate * factor for rate in rates)


def cost_from_usage(
    usage_by_model: dict[str | None, Counter], regional: bool = False
) -> float | None:
    total = 0.0
    for model, usage in usage_by_model.items():
        if not any(usage.values()):
            continue  # e.g. "<synthetic>" placeholder messages carry no usage
        rates = model_rates(model, regional)
        if rates is None:
            return None
        input_rate, write_5m_rate, write_1h_rate, read_rate, output_rate = rates
        written = usage["cache_creation_input_tokens"]
        written_1h = min(usage["cache_1h"], written)
        total += (
            usage["input_tokens"] * input_rate
            + (written - written_1h) * write_5m_rate
            + written_1h * write_1h_rate
            + usage["cache_read_input_tokens"] * read_rate
            + usage["output_tokens"] * output_rate
        ) / 1_000_000
    return total


def scan(
    path: Path,
    tokens: Counter,
    usage_by_model: dict[str | None, Counter],
    tools: Counter,
    seen: set[str],
) -> tuple[float | None, str | None, int, bool]:
    """Add one transcript's usage and tool calls to the counters.

    `seen` holds message and tool-call ids already counted, shared across a session's files.

    Returns (recorded cost, session name if renamed, skipped-line count, regional routing seen).
    """
    recorded_cost = None
    title = None
    warnings = 0
    regional = False

    with path.open("rb") as transcript:
        line_number = 0
        while raw_line := transcript.readline(MAX_LINE_BYTES + 1):
            line_number += 1
            if len(raw_line) > MAX_LINE_BYTES:
                while raw_line and not raw_line.endswith(b"\n"):
                    raw_line = transcript.readline(MAX_LINE_BYTES + 1)
                print(
                    f"warning: {safe_text(path.name)}:{line_number}: skipped line over 64 MiB",
                    file=sys.stderr,
                )
                warnings += 1
                continue
            try:
                line = raw_line.decode("utf-8")
                record = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
                print(
                    f"warning: {safe_text(path.name)}:{line_number}: skipped malformed or deeply nested JSON",
                    file=sys.stderr,
                )
                warnings += 1
                continue
            if not isinstance(record, dict):
                continue

            # /rename appends a custom-title record each time; the last one wins.
            if record.get("type") == "custom-title" and isinstance(
                record.get("customTitle"), str
            ):
                title = record["customTitle"]

            # Transcripts name the model without its routing prefix, but the
            # cost-state record keeps the real Bedrock ID (e.g. us.anthropic.*).
            model_usage = record.get("modelUsage") if record.get("type") == "cost-state" else None
            if isinstance(model_usage, dict):
                regional = regional or any(is_regional(str(key)) for key in model_usage)

            for key in ("total_cost_usd", "cost_usd", "costUSD"):
                value = record.get(key)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    try:
                        amount = float(value)
                    except OverflowError:
                        continue
                    if math.isfinite(amount) and amount >= 0:
                        recorded_cost = amount

            message = record.get("message")
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            model = message.get("model") or record.get("model")
            if not isinstance(model, str):
                model = None
            elif is_regional(model):
                regional = True
            usage = message.get("usage")
            # Claude Code writes one record per content block of a response, each
            # repeating the whole response's usage: count each message id once.
            message_id = message.get("id")
            if isinstance(message_id, str):
                if message_id in seen:
                    usage = None
                seen.add(message_id)
            if isinstance(usage, dict):
                fields = (
                    "input_tokens",
                    "output_tokens",
                    "cache_creation_input_tokens",
                    "cache_read_input_tokens",
                )
                for key in fields:
                    value = usage.get(key, 0)
                    if (
                        isinstance(value, int)
                        and not isinstance(value, bool)
                        and value >= 0
                    ):
                        amount = int(value)
                        tokens[key] += amount
                        usage_by_model[model][key] += amount
                # cache_creation_input_tokens = 5m + 1h writes; 1h costs more, so track it.
                breakdown = usage.get("cache_creation")
                if isinstance(breakdown, dict):
                    value = breakdown.get("ephemeral_1h_input_tokens", 0)
                    if (
                        isinstance(value, int)
                        and not isinstance(value, bool)
                        and value >= 0
                    ):
                        usage_by_model[model]["cache_1h"] += value

            content = message.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        name = block.get("name")
                        block_id = block.get("id")
                        if isinstance(block_id, str):
                            if block_id in seen:
                                continue
                            seen.add(block_id)
                        if isinstance(name, str):
                            tools[name] += 1

    return recorded_cost, title, warnings, regional


def analyze(path: Path) -> dict:
    """Totals for a session: its transcript plus its subagent transcripts."""
    tokens = Counter()
    usage_by_model: dict[str | None, Counter] = defaultdict(Counter)
    tools = Counter()
    seen: set[str] = set()
    recorded_cost, title, warnings, regional = scan(
        path, tokens, usage_by_model, tools, seen
    )

    # Newer Claude Code stores subagents beside the session: <id>/subagents/**/*.jsonl.
    subagents = sorted((path.parent / path.stem / "subagents").glob("**/*.jsonl"))
    for subagent in subagents:
        try:
            _, _, skipped, sub_regional = scan(subagent, tokens, usage_by_model, tools, seen)
            warnings += skipped
            regional = regional or sub_regional
        except OSError as error:
            print(f"warning: {safe_text(subagent.name)}: {safe_text(error)}", file=sys.stderr)
            warnings += 1

    if not tokens and not tools:
        raise ValueError(
            f"no Claude Code assistant usage or tool records found in {path}"
        )
    total_tokens = sum(tokens.values())
    # A recorded cost comes from the main transcript alone, so it is only
    # trusted when there are no subagents to add; otherwise estimate from all usage.
    use_recorded = recorded_cost is not None and not subagents
    estimated_cost = (
        None
        if use_recorded or total_tokens == 0
        else cost_from_usage(usage_by_model, regional)
    )
    return {
        "path": path,
        "title": title,
        "subagents": len(subagents),
        "tokens": tokens,
        "total_tokens": total_tokens,
        "tools": tools,
        "recorded_cost": recorded_cost if use_recorded else None,
        "estimated_cost": estimated_cost,
        "warnings": warnings,
        "regional": regional,
    }


def format_cost(result: dict) -> str:
    if result["recorded_cost"] is not None:
        return f"${result['recorded_cost']:.6f} (recorded)"
    if result["estimated_cost"] is not None:
        tier = "Bedrock geo/regional" if result["regional"] else "Bedrock global"
        return f"${result['estimated_cost']:.6f} (API-equivalent estimate; {tier} rates {PRICING_DATE})"
    return "unavailable (unknown model pricing)"


HEADERS = ["#", "Session", "Project", "Sub", "Tokens", "Input", "Output", "Cache W", "Cache R", "Cost", "Tools"]
LEFT_ALIGNED = {1, 2}  # Session, Project


def cost_value(result: dict) -> float | None:
    if result["recorded_cost"] is not None:
        return result["recorded_cost"]
    return result["estimated_cost"]


def human(count: int) -> str:
    for suffix, size in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if count >= size:
            return f"{count / size:.1f}{suffix}"
    return str(count)


def project_name(path: Path) -> str:
    """Project folder name with the home-directory prefix removed (-Users-me-code-app -> code-app)."""
    name = path.parent.name
    home = re.sub(r"[/.]", "-", str(Path.home()))
    if name.startswith(home):
        return name[len(home):].lstrip("-") or "~"
    return name


def table_row(index: int, result: dict) -> list[str]:
    tokens = result["tokens"]
    cost = cost_value(result)
    if cost is None:
        cost_cell = "n/a"
    else:
        cost_cell = f"${cost:,.2f}" + ("*" if result["regional"] and result["recorded_cost"] is None else "")
    return [
        str(index),
        safe_text(result["title"] or result["path"].stem[:8]),
        safe_text(project_name(result["path"])),
        str(result["subagents"] or "-"),
        human(result["total_tokens"]),
        human(tokens["input_tokens"]),
        human(tokens["output_tokens"]),
        human(tokens["cache_creation_input_tokens"]),
        human(tokens["cache_read_input_tokens"]),
        cost_cell,
        str(sum(result["tools"].values())),
    ]


def total_row(results: list[dict]) -> list[str]:
    known = [cost_value(r) for r in results if cost_value(r) is not None]
    cost = f"${sum(known):,.2f}" + ("+" if len(known) < len(results) else "")
    total = lambda get: human(sum(get(r) for r in results))  # noqa: E731
    return [
        "",
        "Total",
        "",
        str(sum(r["subagents"] for r in results) or "-"),
        total(lambda r: r["total_tokens"]),
        total(lambda r: r["tokens"]["input_tokens"]),
        total(lambda r: r["tokens"]["output_tokens"]),
        total(lambda r: r["tokens"]["cache_creation_input_tokens"]),
        total(lambda r: r["tokens"]["cache_read_input_tokens"]),
        cost,
        total(lambda r: sum(r["tools"].values())),
    ]


def format_table(rows: list[list[str]], width: int) -> list[str]:
    """Header, rule and aligned rows; Project then Session shrink to fit `width`."""
    widths = [max(len(cell) for cell in column) for column in zip(HEADERS, *rows)]
    for column, floor in ((2, 10), (1, 10)):
        excess = sum(widths) + 2 * (len(widths) - 1) - width
        if excess > 0:
            widths[column] = max(floor, widths[column] - excess)

    def cell(column: int, text: str) -> str:
        if len(text) > widths[column]:
            keep = widths[column] - 1
            text = ("…" + text[-keep:]) if column == 2 else (text[:keep] + "…")
        return text.ljust(widths[column]) if column in LEFT_ALIGNED else text.rjust(widths[column])

    def line(values: list[str]) -> str:
        return "  ".join(cell(column, text) for column, text in enumerate(values))

    return [line(HEADERS), "  ".join("-" * w for w in widths), *(line(row) for row in rows)]


def detail_lines(index: int, result: dict) -> list[str]:
    tokens = result["tokens"]
    name = result["path"].name
    label = f"{result['title']} ({name})" if result["title"] else name
    lines = [f"Session {index}: {safe_text(label)}"]
    if result["subagents"]:
        lines.append(f"  Subagents:            {result['subagents']} (included below)")
    lines += [
        f"  Input tokens:         {tokens['input_tokens']:,}",
        f"  Output tokens:        {tokens['output_tokens']:,}",
        f"  Cache write tokens:   {tokens['cache_creation_input_tokens']:,}",
        f"  Cache read tokens:    {tokens['cache_read_input_tokens']:,}",
        f"  Total tokens:         {result['total_tokens']:,}",
        f"  Cost:                 {format_cost(result)}",
        "  Tool calls:",
    ]
    if result["tools"]:
        lines += [f"    {safe_text(n)}: {c}" for n, c in sorted(result["tools"].items())]
    else:
        lines.append("    none")
    if result["warnings"]:
        lines.append(f"  Malformed lines skipped: {result['warnings']}")
    return lines


TABLE_NOTE = f"* Bedrock geo/regional rates (+10%); other costs use global rates ({PRICING_DATE}). API-equivalent estimate."


def run_tui(results: list[dict]) -> None:
    import curses

    os.environ.setdefault("ESCDELAY", "25")
    rows = [table_row(i, r) for i, r in enumerate(results, 1)]
    sorts = [
        ("order", lambda r, i: i),
        ("name", lambda r, i: rows[i][1].lower()),
        ("tokens", lambda r, i: r["total_tokens"]),
        ("cost", lambda r, i: cost_value(r) or 0),
        ("tools", lambda r, i: sum(r["tools"].values())),
    ]

    def ui(screen) -> None:
        curses.curs_set(0)
        curses.start_color()
        curses.init_pair(1, curses.COLOR_CYAN, curses.COLOR_BLACK)  # header
        curses.init_pair(2, curses.COLOR_WHITE, curses.COLOR_BLACK)  # body
        curses.init_pair(3, curses.COLOR_GREEN, curses.COLOR_BLACK)  # total cost
        screen.bkgd(" ", curses.color_pair(2))
        header_attr = curses.color_pair(1) | curses.A_BOLD
        body_attr = curses.color_pair(2)
        total_attr = curses.color_pair(3) | curses.A_BOLD

        sort_at, descending, selected, top, detail = 0, False, 0, 0, None
        filtering, filter_text = False, ""
        while True:
            height, width = screen.getmaxyx()
            indices = [
                i for i in range(len(results)) if filter_text.lower() in rows[i][1].lower()
            ]
            order = sorted(
                indices, key=lambda i: sorts[sort_at][1](results[i], i), reverse=descending
            )
            selected = min(selected, max(0, len(order) - 1))
            screen.erase()
            if detail is None:
                table = format_table([rows[i] for i in order], width - 1)
                body = max(1, height - 4)
                top = min(max(top, selected - body + 1), selected)
                screen.addnstr(0, 0, table[0], width - 1, header_attr)
                screen.addnstr(1, 0, table[1], width - 1, body_attr)
                for y, row in enumerate(table[2 + top : 2 + top + body], 2):
                    attr = (curses.A_REVERSE if top + y - 2 == selected else 0) | body_attr
                    screen.addnstr(y, 0, row, width - 1, attr)
                known = [cost_value(results[i]) for i in order if cost_value(results[i]) is not None]
                total_cost = f"${sum(known):,.2f}" + ("+" if len(known) < len(order) else "")
                shown = f"Showing {len(order)}/{len(results)} sessions   Total cost: {total_cost}"
                screen.addnstr(height - 2, 0, shown, width - 1, total_attr)
                if filtering:
                    footer = f"Filter: {filter_text}_   (enter/esc to stop)"
                else:
                    direction = "desc" if descending else "asc"
                    filter_hint = f"  filter: '{filter_text}'" if filter_text else ""
                    footer = (
                        f"up/down move  enter detail  s sort: {sorts[sort_at][0]} ({direction})  "
                        f"r reverse  / filter{filter_hint}  q quit   {TABLE_NOTE}"
                    )
                screen.addnstr(height - 1, 0, footer, width - 1, body_attr | curses.A_DIM)
            else:
                text = detail_lines(order[selected] + 1, results[order[selected]])
                detail = min(detail, max(0, len(text) - (height - 2)))
                for y, row in enumerate(text[detail : detail + height - 2]):
                    attr = (curses.A_BOLD if y == 0 and detail == 0 else 0) | body_attr
                    screen.addnstr(y, 0, row, width - 1, attr)
                screen.addnstr(
                    height - 1, 0, "up/down scroll  q or enter: back", width - 1, body_attr | curses.A_DIM
                )
            screen.refresh()

            key = screen.getch()
            if filtering:
                if key in (10, 13, curses.KEY_ENTER, 27):
                    filtering = False
                elif key in (curses.KEY_BACKSPACE, 127, 8):
                    filter_text = filter_text[:-1]
                elif 0 <= key < 256 and chr(key).isprintable():
                    filter_text += chr(key)
                selected, top = 0, 0
                continue

            move = {curses.KEY_UP: -1, ord("k"): -1, curses.KEY_DOWN: 1, ord("j"): 1,
                    curses.KEY_PPAGE: -10, curses.KEY_NPAGE: 10}.get(key, 0)
            if detail is not None:
                if key in (ord("q"), 27, 10, 13, curses.KEY_ENTER):
                    detail = None
                else:
                    detail = max(0, detail + move)
            elif key == ord("q"):
                return
            elif key in (10, 13, curses.KEY_ENTER) and order:
                detail = 0
            elif key == ord("s"):
                sort_at, selected, top = (sort_at + 1) % len(sorts), 0, 0
            elif key == ord("r"):
                descending, selected, top = not descending, 0, 0
            elif key == ord("/"):
                filtering = True
            else:
                selected = min(max(selected + move, 0), max(0, len(order) - 1))

    curses.wrapper(ui)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compare tokens, API-equivalent cost, and tool calls in Claude Code sessions.",
        epilog=(
            "Examples: python3 claude_session_report.py                      (table of all sessions in ~/.claude)\n"
            "          python3 claude_session_report.py --tui                 (interactive table)\n"
            "          python3 claude_session_report.py --config-dir ~/.claude-work\n"
            "          python3 claude_session_report.py SESSION_ID [SESSION_ID ...] --detail\n"
            "          python3 claude_session_report.py --compare SESSION_ID SESSION_ID_2   (2-6 sessions)\n"
            "          python3 claude_session_report.py /path/to/session.jsonl /path/to/other.jsonl\n"
            "Cost estimates use API token rates and do not represent Pro/Max subscription charges."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "sessions",
        nargs="*",
        metavar="SESSION",
        help="transcript path or session ID; omit to report every session in the config dir",
    )
    parser.add_argument(
        "-c",
        "--config-dir",
        metavar="DIR",
        help="Claude config directory (default: $CLAUDE_CONFIG_DIR, else ~/.claude)",
    )
    parser.add_argument(
        "--compare",
        nargs="+",
        metavar="SESSION",
        help="report these 2-6 sessions in full, then the table "
        "(use instead of listing sessions positionally)",
    )
    parser.add_argument("--detail", action="store_true", help="print the full per-session report before the table")
    parser.add_argument("--tui", action="store_true", help="browse the table interactively (terminal only)")
    args = parser.parse_args()
    if args.compare:
        if args.sessions:
            parser.error("pass sessions either positionally or after --compare, not both")
        if not 2 <= len(args.compare) <= 6:
            parser.error(f"--compare takes 2 to 6 sessions, got {len(args.compare)}")
    if args.tui and not (sys.stdin.isatty() and sys.stdout.isatty()):
        parser.error("--tui needs an interactive terminal")
    config = config_dir(args.config_dir)

    results = []
    failures = 0
    skipped = 0
    try:
        requested = args.compare or args.sessions
        listing_all = not requested
        targets = all_sessions(config) if listing_all else requested
    except (OSError, ValueError) as error:
        print(f"error: {safe_text(error)}", file=sys.stderr)
        return 2
    for session in targets:
        try:
            results.append(analyze(session if listing_all else find_session(session, config)))
        except (OSError, UnicodeError, ValueError) as error:
            if listing_all:
                skipped += 1  # empty/unreadable transcripts are normal when scanning everything
                continue
            print(f"error: {safe_text(error)}", file=sys.stderr)
            failures += 1
    if listing_all and skipped:
        print(f"note: skipped {skipped} transcript(s) with no usage or tool records", file=sys.stderr)
    if not results:
        if listing_all:
            print(f"no sessions found under {config / 'projects'}", file=sys.stderr)
        return 2

    if args.tui:
        run_tui(results)
        return 1 if failures else 0

    if args.detail or args.compare:
        for index, result in enumerate(results, 1):
            print()
            print("\n".join(detail_lines(index, result)))
        print()
    rows = [table_row(index, result) for index, result in enumerate(results, 1)]
    if len(results) > 1:
        rows.append(total_row(results))
    print("\n".join(format_table(rows, shutil.get_terminal_size((140, 24)).columns)))
    print(TABLE_NOTE)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
