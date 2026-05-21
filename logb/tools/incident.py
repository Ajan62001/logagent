"""incident_around(line) — return a multi-line incident as one unit.

The indexer coalesces a severe line plus its continuation body (indented
or non-prefix lines that follow) into a single "incident" record. This
tool lets the agent fetch that whole block by anchor line — useful when
a stack trace, traceback, or any multi-line diagnostic follows the error
that surfaced via `read_logs(severity=...)`.

The agent can also ask for the incident "near" any non-severe line and we
return the closest enclosing incident (head_line >= line - N).
"""

from __future__ import annotations

from bisect import bisect_left

from .. import index as _idx
from .base import Tool, ToolContext, truncate
from .logs import _pick_file


def _incident_around(args: dict, ctx: ToolContext) -> str:
    line_1idx = args.get("line")
    if line_1idx is None:
        return "ERROR: `line` is required (1-indexed)."
    try:
        line = int(line_1idx) - 1
    except (TypeError, ValueError):
        return f"ERROR: `line` must be an integer, got {line_1idx!r}."
    if line < 0:
        return f"ERROR: `line` must be >= 1, got {line_1idx!r}."

    f = _pick_file(args, ctx.cfg, ctx.profile)
    if f is None or not f.is_file():
        return f"Log not found: {args.get('path')!r}. Call list_logs."
    try:
        idx = _idx.load_or_build(f, ctx.profile)
    except OSError as e:
        return f"ERROR indexing {f}: {e}"

    incidents = idx.get("incidents") or []
    if not incidents:
        return ("(no incidents recorded for this log — incidents are "
                "multi-line blocks anchored on a severe line; the index "
                "didn't detect any continuation bodies.)")

    # Find the incident whose [head, end] range contains `line`, or the
    # nearest head before `line` if none does. Incidents are stored in
    # build-time order which is also head-line ascending.
    heads = [inc[0] for inc in incidents]
    pos = bisect_left(heads, line + 1) - 1   # last head <= line
    if pos < 0:
        return (f"(no incident at or before L{line + 1}. The first "
                f"recorded incident starts at L{heads[0] + 1}.)")
    head, end, sev = incidents[pos][0], incidents[pos][1], incidents[pos][2]
    if line > end:
        # Not within this incident; report it as the nearest preceding.
        nearest_note = (f"\n(L{line + 1} is not inside any incident; "
                         f"the nearest preceding one ends at L{end + 1}.)")
    else:
        nearest_note = ""

    text = _idx.fetch_text(f, idx, set(range(head, end + 1)))
    out = [f"# incident_around L{line + 1}  (head L{head + 1}, "
           f"body ends L{end + 1}, severity={sev})"]
    for ln in range(head, end + 1):
        marker = ">>>" if ln == head else "   "
        out.append(f"{marker} {ln + 1:>6}: {text.get(ln, '')}")
    if nearest_note:
        out.append(nearest_note)
    return truncate("\n".join(out), ctx.cfg.tool_result_char_budget)


INCIDENT_AROUND = Tool(
    name="incident_around",
    description=(
        "Return a multi-line incident (a severe line plus its continuation "
        "body — typically a stack trace or traceback) as a single unit. "
        "Pass the 1-indexed `line` number of the head error OR any line "
        "within the body; returns the whole block. If the line falls "
        "outside any recorded incident, returns the nearest preceding one "
        "with a note. Useful when read_logs surfaces a severe lead line "
        "but the actual diagnostic is the indented body below it."),
    parameters={
        "type": "object",
        "properties": {
            "line": {"type": "integer",
                     "description": "1-indexed line number to look up "
                     "(head line or any body line of the incident)."},
            "path": {"type": "string",
                     "description": "Log file path or name (omit for the "
                     "only/first log)."},
        },
        "required": ["line"],
    },
    run=_incident_around,
)
