"""log_summary — exact, whole-file counts and the distinct message-code table.

Answers "how many errors/warnings", "list the unique error/warning codes",
"what kinds of failures are in this log" — *exactly*, straight from the cached
index, even on a 2 GB file. This exists because read_logs with a severity
filter returns a bounded *window* (it early-stops at max_lines); counting or
listing uniques by eyeballing that window is wrong. Use this instead.
"""

from __future__ import annotations

from .. import index as _idx
from .base import Tool, ToolContext, truncate
from .logs import _pick_file

_RANK = {"fatal": 0, "error": 1, "warn": 2}


def _log_summary(args: dict, ctx: ToolContext) -> str:
    f = _pick_file(args, ctx.cfg)
    if f is None or not f.is_file():
        return (f"Log not found: {args.get('path')!r}. "
                f"Call list_logs to see available files.")
    try:
        idx = _idx.load_or_build(f)
    except OSError as e:
        return f"ERROR indexing {f}: {e}"

    total = idx["total"]
    n_fat, n_err, n_warn = idx["n_fat"], idx["n_err"], idx.get("n_warn", 0)
    codes = idx.get("codes", [])

    only = (args.get("severity") or "").lower()
    wanted = {s for s in ("fatal", "error", "warn") if not only or s in only}

    rows = [c for c in codes if c[1] in wanted]
    rows.sort(key=lambda c: (_RANK.get(c[1], 9), -c[2], c[0]))

    # One example line per code, via a direct seek (bounded by #codes shown).
    shown = rows[:300]
    examples: dict[int, str] = {}
    if shown:
        with open(f, "rb") as fh:
            for _code, _sev, _n, _ln, off in shown:
                fh.seek(off)
                examples[off] = fh.readline().decode(
                    "utf-8", "replace").rstrip("\n")

    head = (f"# {f}  ({total} lines)\n"
            f"EXACT counts (whole file): {n_fat} FATAL · {n_err} ERROR · "
            f"{n_warn} WARN · {len(codes)} distinct code(s)"
            f"{' [codes capped]' if idx.get('codes_capped') else ''}\n")
    if not rows:
        return head + "(no FATAL/ERROR/WARN message codes matched)"

    lines = [f"{'CODE':<16} {'SEV':<5} {'COUNT':>7}  FIRST   EXAMPLE"]
    for code, sev, n, ln, off in shown:
        ex = examples.get(off, "").strip()[:110]
        lines.append(f"{code:<16} {sev:<5} {n:>7}  L{ln + 1:<6} {ex}")
    if len(rows) > len(shown):
        lines.append(f"... (+{len(rows) - len(shown)} more codes; "
                      f"use severity= to narrow)")
    return truncate(head + "\n".join(lines), ctx.cfg.tool_result_char_budget)


LOG_SUMMARY = Tool(
    name="log_summary",
    description=(
        "EXACT whole-file summary: total lines, true FATAL/ERROR/WARN counts, "
        "and the table of DISTINCT message codes (code, severity, occurrence "
        "count, first line, an example). Use this for any 'how many', "
        "'count of errors/warnings', or 'list the unique errors/warnings' "
        "question — it is exact even on a multi-GB log, unlike eyeballing the "
        "windowed read_logs output. Optional `severity` narrows the table."),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string",
                     "description": "Log file path or name (omit for the only/first log)."},
            "severity": {"type": "string",
                         "description": "Optional filter: fatal/error/warn, comma-ok."},
        },
    },
    run=_log_summary,
)
