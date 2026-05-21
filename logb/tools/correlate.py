"""correlate — interleave lines from multiple logs around a time window.

The most common real production debugging question is "what was happening
in the N seconds around the crash" — and the answer almost always spans
multiple files (app log + system log + audit + metrics export). With just
read_logs the agent has to call it per-file and stitch in its head,
which weak models do poorly and which loses ordering across files.

This tool uses the profile's timestamp regex (stored as `time_index`
in each log's cached sidecar) to convert a per-file line number into a
moment in time, then for every other configured log finds the lines
within ±window_seconds of that moment, and returns them sorted by
timestamp with the source file prefixed.

Anchor specification:
    anchor_path + anchor_line  — line in a specific log file
                                  (timestamp resolved via that log's index)
    anchor_ts                  — explicit numeric timestamp (use when the
                                  agent already has a known moment)
"""

from __future__ import annotations

from bisect import bisect_left
from pathlib import Path

from .. import index as _idx
from .base import Tool, ToolContext, truncate
from .logs import _pick_file, _resolve_logs


def _ts_at_line(idx: dict, line: int) -> int | None:
    """Interpolate the timestamp at a given line from the sparse time
    samples. Returns None if the log has no timestamps."""
    ti = idx.get("time_index") or []
    if not ti:
        return None
    if line <= ti[0][0]:
        return ti[0][1]
    if line >= ti[-1][0]:
        return ti[-1][1]
    # Find the surrounding samples (line, ts) and linearly interpolate.
    keys = [t[0] for t in ti]
    i = bisect_left(keys, line)
    if ti[i][0] == line:
        return ti[i][1]
    lo_ln, lo_t = ti[i - 1]
    hi_ln, hi_t = ti[i]
    span = hi_ln - lo_ln
    if span <= 0:
        return lo_t
    return lo_t + (hi_t - lo_t) * (line - lo_ln) // span


def _lines_in_window(log: Path, idx: dict, lo_ts: int, hi_ts: int,
                      max_lines: int) -> list[tuple[int, int, str]]:
    """Return [(ts, line, text), ...] from `log` whose timestamp falls in
    [lo_ts, hi_ts]. Built on the time_index samples; we expand each
    sample into the surrounding lines proportionally so we don't miss
    untimestamped lines that lie between sampled ones."""
    ti = idx.get("time_index") or []
    if not ti:
        return []
    # Identify the sample range that overlaps the window.
    # Pad by one sample on each side to catch lines whose timestamps
    # weren't explicitly sampled.
    keys = [t[1] for t in ti]
    lo_i = max(0, bisect_left(keys, lo_ts) - 1)
    hi_i = min(len(ti), bisect_left(keys, hi_ts) + 1)
    if lo_i >= hi_i:
        return []
    start_line = ti[lo_i][0]
    end_line = ti[hi_i - 1][0]
    # Read every line between start and end via the index's seekable
    # fetch. We then re-derive each line's timestamp by interpolation
    # for sorting.
    wanted = set(range(start_line, end_line + 1))
    text_map = _idx.fetch_text(log, idx, wanted)
    out: list[tuple[int, int, str]] = []
    for ln in sorted(wanted):
        if ln not in text_map:
            continue
        ts = _ts_at_line(idx, ln)
        if ts is None:
            continue
        if lo_ts <= ts <= hi_ts:
            out.append((ts, ln, text_map[ln]))
        if len(out) >= max_lines:
            break
    return out


def _correlate(args: dict, ctx: ToolContext) -> str:
    window = int(args.get("window_seconds") or 30)
    if window < 1 or window > 3600:
        return ("ERROR: window_seconds must be between 1 and 3600 "
                f"(got {window}).")
    max_lines = min(int(args.get("max_lines") or 200), 500)

    # Resolve the anchor.
    anchor_ts = args.get("anchor_ts")
    if anchor_ts is None:
        anchor_path = args.get("anchor_path")
        anchor_line = args.get("anchor_line")
        if not anchor_path or anchor_line is None:
            return ("ERROR: provide either `anchor_ts` (numeric seconds), "
                    "or both `anchor_path` and `anchor_line`.")
        f = _pick_file({"path": anchor_path}, ctx.cfg, ctx.profile)
        if f is None or not f.is_file():
            return f"ERROR: anchor log not found: {anchor_path!r}."
        try:
            idx = _idx.load_or_build(f, ctx.profile)
        except OSError as e:
            return f"ERROR indexing anchor log: {e}"
        anchor_ts = _ts_at_line(idx, int(anchor_line) - 1)  # CLI is 1-indexed
        if anchor_ts is None:
            return (f"ERROR: log {f.name} has no timestamps for the "
                    "active profile — correlate needs a timestamp regex "
                    "(check profile.timestamp_rx). Use anchor_ts directly "
                    "if you have a number.")
    try:
        anchor_ts = int(anchor_ts)
    except (TypeError, ValueError):
        return f"ERROR: anchor_ts must be numeric, got {anchor_ts!r}."

    lo, hi = anchor_ts - window, anchor_ts + window

    # Which logs to correlate across. By default: every log discovered
    # under cfg.log_path. The agent can scope by passing `paths=[...]`.
    requested = args.get("paths")
    if requested:
        logs = []
        for p in requested:
            f = _pick_file({"path": p}, ctx.cfg, ctx.profile)
            if f is not None and f.is_file():
                logs.append(f)
    else:
        logs = _resolve_logs(ctx.cfg, ctx.profile)
    if not logs:
        return "ERROR: no logs to correlate across."

    rows: list[tuple[int, str, int, str]] = []  # (ts, log_name, line, text)
    skipped: list[str] = []
    for log in logs:
        try:
            idx = _idx.load_or_build(log, ctx.profile)
        except OSError as e:
            skipped.append(f"{log.name}: {e}")
            continue
        if not (idx.get("time_index") or []):
            skipped.append(f"{log.name}: no timestamps")
            continue
        for ts, ln, text in _lines_in_window(
                log, idx, lo, hi, max_lines):
            rows.append((ts, log.name, ln, text))

    rows.sort(key=lambda r: (r[0], r[1], r[2]))
    if len(rows) > max_lines:
        rows = rows[:max_lines]

    out = [f"# correlate window=[{lo}..{hi}] (anchor_ts={anchor_ts}, "
           f"±{window}s)  · {len(logs)} log(s) checked"]
    if skipped:
        out.append("[skipped: " + "; ".join(skipped[:5])
                   + ("..." if len(skipped) > 5 else "") + "]")
    if not rows:
        out.append("(no timestamped lines in the window)")
        return "\n".join(out)

    for ts, log_name, ln, text in rows:
        out.append(f"  t={ts:>6}  {log_name}:L{ln + 1}  {text[:180]}")
    return truncate("\n".join(out), ctx.cfg.tool_result_char_budget)


CORRELATE = Tool(
    name="correlate",
    description=(
        "Interleave lines from one or more logs that occurred within "
        "±window_seconds of an anchor moment. Sorted by timestamp, "
        "log-name labeled. Use to answer 'what was happening around the "
        "crash' — especially across multiple log files (app + system + "
        "audit). Pass either anchor_ts (numeric seconds) OR "
        "anchor_path+anchor_line (a specific line in a known log) to "
        "fix the moment in time. Only logs whose profile timestamp "
        "regex matches will appear; logs without timestamps are skipped "
        "with a note."),
    parameters={
        "type": "object",
        "properties": {
            "anchor_path": {"type": "string",
                             "description": "Anchor log path/name."},
            "anchor_line": {"type": "integer",
                             "description": "Anchor line number (1-indexed) "
                             "in anchor_path."},
            "anchor_ts": {"type": "integer",
                          "description": "Numeric anchor timestamp "
                          "(seconds, profile-defined epoch); skip "
                          "anchor_path/anchor_line if used."},
            "window_seconds": {"type": "integer",
                                "description": "Half-window size in "
                                "seconds (default 30, max 3600)."},
            "max_lines": {"type": "integer",
                          "description": "Cap on returned lines (default 200, max 500)."},
            "paths": {"type": "array", "items": {"type": "string"},
                      "description": "Restrict to these log files (default: "
                      "all logs under cfg.log_path)."},
        },
    },
    run=_correlate,
)
