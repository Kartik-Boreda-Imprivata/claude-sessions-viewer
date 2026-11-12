# claude-sessions-viewer

Single-file Python script that reports token usage, API-equivalent cost and tool calls for Claude Code sessions. Standard library only, Python 3.8+.

## Usage

```bash
python3 claude_session_report.py                          # table of every session in the config dir
python3 claude_session_report.py --tui                    # same table, interactive
python3 claude_session_report.py --config-dir ~/.claude-work
python3 claude_session_report.py SESSION_ID [SESSION_ID ...]
python3 claude_session_report.py /path/to/session.jsonl
python3 claude_session_report.py --detail                 # full per-session report, then the table
python3 claude_session_report.py --compare SESSION_ID SESSION_ID_2 [... up to 6]
```

With no arguments it reads every top-level transcript at `<config>/projects/*/*.jsonl`, oldest first. Transcripts with no usage or tool records are skipped and counted in a note on stderr.

## Table

One row per session: `#`, session (its name if renamed, else the first 8 characters of the ID), project, subagent count, total / input / output / cache-write / cache-read tokens (K, M, B), cost and tool calls, plus a Total row when there is more than one session. Project and Session shrink to fit the terminal (140 columns when piped). A `*` on a cost means regional rates were used (see Cost). `Cost` shows `n/a` for models without a price, and the total then ends in `+`.

## Interactive view

`--tui` opens the same table with the standard-library `curses`, so it needs a real terminal and no install.

| Key | Action |
|-----|--------|
| up / down, `k` / `j`, PgUp / PgDn | move the selection |
| Enter | full report for the selected session; Enter or `q` goes back, up / down scroll |
| `s` | cycle sort: order, tokens, cost, tools |
| `r` | reverse the sort |
| `q` | quit |

## Comparison

Off by default. `--compare` takes 2 to 6 session IDs or paths, prints the full report for each, then the table with a Total row. It cannot be combined with positional sessions, and any other count is rejected.

## Config directory

Resolved in this order:

1. `-c` / `--config-dir DIR`
2. `$CLAUDE_CONFIG_DIR`
3. `~/.claude`

A session ID is looked up under `<config>/projects/`. A transcript path is used as given.

## Output per session

Input, output, cache-write and cache-read tokens, total tokens, cost, and a count of each tool called.

- **Name:** if the session was renamed, the header reads `Name (<id>.jsonl)`; the latest rename wins.
- **Subagents:** transcripts under `<project>/<session-id>/subagents/` are added into their parent session's totals, and a `Subagents: N` line shows how many.

## Cost

Uses a recorded cost from the transcript when there is one, unless the session has subagents; the recorded figure comes from the main transcript alone, so those sessions are estimated from the combined usage. Otherwise it estimates from the [Amazon Bedrock Anthropic prices](https://aws.amazon.com/bedrock/pricing/) hard-coded in `RATES` (price data of `PRICING_DATE`). The estimate is API-equivalent, not what a Pro/Max subscription charges, and shows "unavailable" for models not in `RATES`. Update the rates and the date together.

How the estimate is built:

- **Each response counts once.** Claude Code writes one transcript line per content block, each repeating the response's usage, so usage is de-duplicated by message ID (this alone roughly halved the old totals).
- **Cache writes** are split into 5-minute and 1-hour writes, which are priced differently.
- **Models are matched exactly** after stripping the vendor prefix and date/version suffix, so `claude-sonnet-5-5` no longer falls back to Sonnet 5 rates.
- **Global vs regional:** rates are the Bedrock *Global Cross-region* prices. If the transcript shows a geo or in-region ID (`us.anthropic.*`, `eu.anthropic.*`, `anthropic.*`, also read from the `cost-state` record) the session is priced at the *Geo and In-region* tier, 10% higher. The cost line says which tier was used.
- **Not modelled:** batch, priority/flex/reserved tiers, Haiku 5.5 long-context rates, and Claude 4 / 4.1 / 3.x models (they show "unavailable").
- **Not the same as `/cost`:** Claude Code's own `cost-state` total uses a stale Sonnet 5.5 cache-read rate and no regional premium, and can restart when a session is resumed, so it will not match.

## Exit codes

`0` success, `1` some named sessions failed, `2` nothing to report or the projects directory is missing.

## Limits

- Subagents are only rolled into their parent, never listed on their own.
- Subagent support follows the `<session-id>/subagents/` layout; it was checked against a made-up fixture because none of the local sessions have subagent files.
- Lines over 64 MiB and malformed JSON lines are skipped with a warning.
