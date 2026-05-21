"""region_summary(start, end) — paragraph summary of an arbitrary window.

Pure index reads. No log re-scan. Designed so the agent can investigate
a specific window of interest (e.g. "what happened between L500 and
L1000?") without dumping 500 lines into context. Useful for surveying
unfamiliar parts of a long log.

Computes, for the [start, end] window:
  • severity counts (error / fatal / warn) from `severe`, `severe_tail`,
    and `warn_offsets`
  • distinct codes seen (intersection with `code_occurrences.head`/`tail`)
  • stages crossed (from `stage_ranges` overlap with the window)
  • time span (interpolated from `time_index`)
  • top file/identifier mentions (top-K of `mentions` whose line lists
    intersect the window)
  • incidents whose head_line falls inside the window
"""

from __future__ import annotations

from bisect import bisect_left

from .. import index as _idx
from .base import Tool, ToolContext, truncate
from .logs import _pick_file


def _ts_at(idx: dict, line: int) -> int | None:
    """Interpolate a timestamp for an arbitrary line. Mirrors the helper
    in tools/correlate.py — but lighter, since we don't need precision
    here (only display)."""
    ti = idx.get("time_index") or []
    if not ti:
        return None
    if line <= ti[0][0]:
        return ti[0][1]
    if line >= ti[-1][0]:
        return ti[-1][1]
    keys = [t[0] for t in ti]
    i = bisect_left(keys, line)
    if i >= len(ti):
        return ti[-1][1]
    if ti[i][0] == line:
        return ti[i][1]
    lo_ln, lo_t = ti[i - 1]
    hi_ln, hi_t = ti[i]
    span = hi_ln - lo_ln
    if span <= 0:
        return lo_t
    return lo_t + (hi_t - lo_t) * (line - lo_ln) // span


def _region_summary(args: dict, ctx: ToolContext) -> str:
    if "start" not in args or "end" not in args:
        return ("ERROR: `start` and `end` are required (1-indexed inclusive "
                "line range).")
    try:
        start = int(args["start"]) - 1
        end = int(args["end"]) - 1
    except (TypeError, ValueError):
        return f"ERROR: start/end must be integers."
    if start < 0 or end < start:
        return f"ERROR: invalid range L{start + 1}-L{end + 1}."

    f = _pick_file(args, ctx.cfg, ctx.profile)
    if f is None or not f.is_file():
        return f"Log not found: {args.get('path')!r}. Call list_logs."
    try:
        idx = _idx.load_or_build(f, ctx.profile)
    except OSError as e:
        return f"ERROR indexing {f}: {e}"
    total = idx["total"]
    end = min(end, total - 1)
    if start >= total:
        return f"ERROR: start L{start + 1} is past EOF (total {total} lines)."

    # Severity counts in window from severe + severe_tail + warn_offsets.
    n_err = n_fat = n_warn = 0
    for ln, _off, sev in idx.get("severe") or []:
        if start <= ln <= end:
            if sev == "fatal":
                n_fat += 1
            elif sev == "error":
                n_err += 1
    for ln, _off, sev in idx.get("severe_tail") or []:
        if start <= ln <= end:
            if sev == "fatal":
                n_fat += 1
            elif sev == "error":
                n_err += 1
    for ln, _off in idx.get("warn_offsets") or []:
        if start <= ln <= end:
            n_warn += 1

    # Stages overlapping the window.
    crossed: list[str] = []
    for entry in idx.get("stage_ranges") or []:
        s, e, name = entry[0], entry[1], entry[2]
        if s <= end and e >= start:
            crossed.append(name)

    # Codes seen in the window (via code_occurrences head+tail line lists).
    seen_codes: dict[str, int] = {}
    for code, occ in (idx.get("code_occurrences") or {}).items():
        # Quick check: any head or tail line falls within the window.
        in_window = any(start <= ln <= end
                        for ln in occ.get("head", []))
        if not in_window:
            in_window = any(start <= ln <= end
                            for ln in occ.get("tail", []))
        if in_window:
            # Approximate count within the window from head+tail samples.
            hits = sum(1 for ln in occ.get("head", []) if start <= ln <= end)
            hits += sum(1 for ln in occ.get("tail", []) if start <= ln <= end)
            seen_codes[code] = hits

    # Mentions whose hit lines overlap.
    mention_hits: list[tuple[str, int]] = []
    for token, lns in (idx.get("mentions") or {}).items():
        n = sum(1 for ln in lns if start <= ln <= end)
        if n:
            mention_hits.append((token, n))
    mention_hits.sort(key=lambda x: -x[1])

    # Incidents whose head_line is in window.
    incidents_in = [inc for inc in (idx.get("incidents") or [])
                    if start <= inc[0] <= end]

    # Time span (interpolated).
    t_start = _ts_at(idx, start)
    t_end = _ts_at(idx, end)
    span = (t_end - t_start) if (t_start is not None
                                  and t_end is not None) else None

    n_lines = end - start + 1
    out = [f"# region_summary L{start + 1}..L{end + 1}  "
           f"({n_lines} line(s) of {total})"]
    out.append(f"severity counts in window : {n_fat} FATAL · {n_err} ERROR · {n_warn} WARN")
    if span is not None:
        out.append(f"time span                 : t={t_start} -> t={t_end} ({span}s)")
    if crossed:
        out.append(f"stages crossed            : {', '.join(crossed)}")
    if seen_codes:
        ranked = sorted(seen_codes.items(), key=lambda x: -x[1])[:8]
        out.append("codes (sample, approx)    : "
                   + ", ".join(f"{c}×{n}" for c, n in ranked))
    if mention_hits:
        top = mention_hits[:8]
        out.append("top mentions              : "
                   + ", ".join(f"{t}×{n}" for t, n in top))
    if incidents_in:
        out.append(f"incidents heading here    : {len(incidents_in)} "
                   f"(use incident_around to fetch)")
        for head_line, body_end, sev in incidents_in[:5]:
            out.append(f"  - L{head_line + 1} [{sev}] "
                       f"(body to L{body_end + 1})")
    if not any([n_fat, n_err, n_warn, crossed, seen_codes,
                 mention_hits, incidents_in]):
        out.append("(quiet region — no severe lines, codes, stages, or "
                   "mentions intersect this window)")
    return truncate("\n".join(out), ctx.cfg.tool_result_char_budget)


REGION_SUMMARY = Tool(
    name="region_summary",
    description=(
        "Return a paragraph summary of an arbitrary line range in the "
        "target log without reading the lines themselves. Reports "
        "severity counts in the window, stages crossed, distinct codes "
        "seen, time span, top file/identifier mentions, and incidents "
        "heading in the window. Pure index reads — near-instant. Useful "
        "to triage a region before drilling in with read_logs."),
    parameters={
        "type": "object",
        "properties": {
            "start": {"type": "integer",
                      "description": "Window start, 1-indexed inclusive."},
            "end": {"type": "integer",
                    "description": "Window end, 1-indexed inclusive."},
            "path": {"type": "string",
                     "description": "Log file path or name (omit for the "
                     "only/first log)."},
        },
        "required": ["start", "end"],
    },
    run=_region_summary,
)
