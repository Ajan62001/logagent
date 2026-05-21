"""find_mentions(token) — line numbers where a path/identifier appears.

The indexer extracts file-path-like tokens and `<CMD> source ...` script
references during the build pass into an inverted index. This tool
returns every line where a given token (or any token containing the
caller's substring) was seen — much faster than `read_logs(pattern=...)`
because there's no streaming scan.

Two query modes:
  • exact (default): the token must match a key in the index.
  • substring (set `substring=true`): list every indexed key that
    *contains* the caller's string, with its line numbers. Useful when
    the agent only has the file's basename ("top.sdc") and wants to
    find it under any path ("scripts/constraints/top.sdc").
"""

from __future__ import annotations

from .. import index as _idx
from .base import Tool, ToolContext, truncate
from .logs import _pick_file


def _find_mentions(args: dict, ctx: ToolContext) -> str:
    token = (args.get("token") or "").strip()
    if not token:
        return ("ERROR: `token` is required (e.g. 'top.sdc', "
                "'scripts/constraints/top.sdc', or 'CMD:scripts/place.tcl').")
    substring = bool(args.get("substring", False))
    max_lines = min(int(args.get("max_lines") or 50), 200)

    f = _pick_file(args, ctx.cfg, ctx.profile)
    if f is None or not f.is_file():
        return f"Log not found: {args.get('path')!r}. Call list_logs."
    try:
        idx = _idx.load_or_build(f, ctx.profile)
    except OSError as e:
        return f"ERROR indexing {f}: {e}"

    mentions = idx.get("mentions") or {}
    if not mentions:
        return ("(no mentions indexed — this log doesn't reference any "
                "file-path-shaped tokens that the indexer recognized.)")

    if substring:
        keys = [k for k in mentions if token in k]
    else:
        keys = [token] if token in mentions else []
    if not keys:
        # Provide a list of available tokens so the agent can recover.
        sample = sorted(mentions.keys())[:20]
        return (f"(no mentions of {token!r}.{' Tried substring match.' if substring else ' Re-call with substring=true to fuzzy-match.'} "
                f"Available tokens (up to 20): {sample})")

    keys.sort()
    out = [f"# find_mentions({token!r}, substring={substring}) — "
           f"{len(keys)} token(s) matched in {f}"]
    # Collect target lines for context fetch — first 3 per token, max
    # max_lines total. This keeps the output budget bounded on logs
    # that mention a popular file dozens of times.
    fetch_lines: set[int] = set()
    flat: list[tuple[str, list[int]]] = []
    total_shown = 0
    for k in keys:
        lns = mentions[k][:]
        if total_shown >= max_lines:
            break
        preview = lns[:max(1, max_lines - total_shown)]
        flat.append((k, preview))
        fetch_lines.update(preview[:3])
        total_shown += len(preview)
    text = _idx.fetch_text(f, idx, fetch_lines) if fetch_lines else {}
    for k, lns in flat:
        out.append(f"\n## {k}  ({len(mentions[k])} hit(s))")
        for ln in lns:
            preview = text.get(ln, "").strip()[:120]
            out.append(f"  L{ln + 1:>6}: {preview}")
    return truncate("\n".join(out), ctx.cfg.tool_result_char_budget)


FIND_MENTIONS = Tool(
    name="find_mentions",
    description=(
        "Return every line where a file path / script reference / "
        "identifier appears in the log. Faster than read_logs(pattern=...) "
        "because hits come from a pre-built inverted index. Pass the "
        "full token (e.g. 'scripts/constraints/top.sdc') or a basename "
        "with substring=true to fuzzy-match. `<CMD> source` invocations "
        "are indexed with a 'CMD:' prefix (e.g. 'CMD:scripts/place.tcl')."),
    parameters={
        "type": "object",
        "properties": {
            "token": {"type": "string",
                      "description": "Path / identifier to look up "
                      "(e.g. 'top.sdc', 'CMD:scripts/place.tcl')."},
            "substring": {"type": "boolean",
                          "description": "When true, match any indexed "
                          "key containing `token`; otherwise require an "
                          "exact key match."},
            "max_lines": {"type": "integer",
                          "description": "Cap on total hits returned "
                          "(default 50, max 200)."},
            "path": {"type": "string",
                     "description": "Log file path or name (omit for the "
                     "only/first log)."},
        },
        "required": ["token"],
    },
    run=_find_mentions,
)
