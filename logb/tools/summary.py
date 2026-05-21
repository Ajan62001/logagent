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
    f = _pick_file(args, ctx.cfg, ctx.profile)
    if f is None or not f.is_file():
        return (f"Log not found: {args.get('path')!r}. "
                f"Call list_logs to see available files.")
    try:
        idx = _idx.load_or_build(f, ctx.profile)
    except OSError as e:
        return f"ERROR indexing {f}: {e}"

    total = idx["total"]
    n_fat, n_err, n_warn = idx["n_fat"], idx["n_err"], idx.get("n_warn", 0)
    codes = idx.get("codes", [])

    only = (args.get("severity") or "").lower()
    wanted = {s for s in ("fatal", "error", "warn") if not only or s in only}

    rows = [c for c in codes if c[1] in wanted]
    rows.sort(key=lambda c: (_RANK.get(c[1], 9), -c[2], c[0]))

    # One example line per code, via a direct seek to the offset stored
    # in the index. Each seek is microseconds, so we read examples for
    # ALL rows — no display cap. The final truncate() (driven by
    # cfg.tool_result_char_budget) is the only ceiling on output size,
    # which keeps the model's context bounded without us pre-clipping
    # rows the user explicitly asked to see.
    examples: dict[int, str] = {}
    if rows:
        with open(f, "rb") as fh:
            for _code, _sev, _n, _ln, off in rows:
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
    for code, sev, n, ln, off in rows:
        ex = examples.get(off, "").strip()[:110]
        lines.append(f"{code:<16} {sev:<5} {n:>7}  L{ln + 1:<6} {ex}")

    # NEXT-STEPS NUDGE: small local models stop after one search_manual.
    # Enumerate every distinct error/fatal code as an explicit TODO list so
    # the next iteration of the agent loop has a concrete remaining
    # checklist instead of an open-ended "what now". One code per line,
    # grouped by prefix (cascade rule applies within a prefix family).
    severe = [c for c in rows if c[1] in ("fatal", "error")]
    todo = ""
    if severe:
        # Group by prefix (everything before the last '-NNN'). Within a
        # prefix, only the first code needs investigation (cascade rule);
        # across prefixes, each one is independent.
        by_prefix: dict[str, list] = {}
        for code, sev, n, ln, off in severe:
            pfx = code.rsplit("-", 1)[0]
            by_prefix.setdefault(pfx, []).append((code, sev, n, ln))
        todo_lines = [
            "",
            "TODO — investigate each distinct prefix below as a SEPARATE "
            "failure (cascade rule applies WITHIN a prefix, not across):"]
        for i, (pfx, items) in enumerate(by_prefix.items(), 1):
            primary = items[0]   # first code in the prefix family
            code, sev, n, ln = primary
            rest = (f" [+{len(items) - 1} more in {pfx}-* family: "
                    f"{', '.join(c[0] for c in items[1:])}]"
                    if len(items) > 1 else "")
            todo_lines.append(
                f"  [{i}] code_lookup(code={code!r})   "
                f"# {sev.upper()}, {n} occurrence(s), first @ L{ln + 1}{rest}")
        todo_lines.append(
            "  Do EVERY [N] above before emitting a final answer. "
            "code_lookup auto-derives the right manual query from the log "
            "line, so you do NOT need to chain read_logs + search_manual "
            "manually. If a [N] returns no manual hit, say so honestly — "
            "do NOT fabricate.")
        todo = "\n" + "\n".join(todo_lines)
    return truncate(head + "\n".join(lines) + todo,
                    ctx.cfg.tool_result_char_budget)


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
