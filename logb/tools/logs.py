"""Log tools: list_logs + read_logs (grep / severity / head / tail / context).

read_logs returns 1-indexed, line-numbered output so the agent can cite exact
locations ("line 4213") and follow any file paths printed in those lines via
the read_file tool.
"""

from __future__ import annotations

import re
from pathlib import Path

from .. import index as _idx
from ..profiles import EDA, Profile
from .base import Tool, ToolContext, truncate


def _resolve_logs(cfg, profile: Profile = EDA) -> list[Path]:
    p = Path(cfg.log_path)
    if p.is_dir():
        exts = profile.log_extensions
        return sorted(f for f in p.rglob("*")
                      if f.is_file() and f.suffix.lower() in exts)
    return [p] if p.is_file() else []


def _list_logs(args: dict, ctx: ToolContext) -> str:
    logs = _resolve_logs(ctx.cfg, ctx.profile)
    if not logs:
        return f"No log files found at {ctx.cfg.log_path!r}."
    lines = []
    for f in logs:
        try:
            kb = f.stat().st_size / 1024
            n = _idx.load_or_build(f, ctx.profile)["total"]   # cached
        except OSError:
            n, kb = -1, -1
        lines.append(f"{f}  ({n} lines, {kb:.0f} KB)")
    return "Available logs:\n" + "\n".join(lines)


def _pick_file(args: dict, cfg, profile: Profile = EDA) -> Path | None:
    want = args.get("path")
    logs = _resolve_logs(cfg, profile)
    if want:
        wp = Path(want)
        for f in logs:
            if f == wp or f.name == wp.name or str(f).endswith(want):
                return f
        return wp if wp.is_file() else None
    return logs[0] if logs else None


def _read_logs(args: dict, ctx: ToolContext) -> str:
    f = _pick_file(args, ctx.cfg, ctx.profile)
    if f is None or not f.is_file():
        return (f"Log not found: {args.get('path')!r}. "
                f"Call list_logs to see available files.")

    pattern = args.get("pattern")
    severity = (args.get("severity") or "").lower()
    head = args.get("head")
    tail = args.get("tail")
    ctx_lines = int(args.get("context", 2))
    max_lines = int(args.get("max_lines", 200))

    sev_map = ctx.profile.severity
    rx = re.compile(pattern, re.I) if pattern else None
    # Tolerant severity parsing: accept "error", "error,fatal", "ERROR fatal",
    # "warn" etc. (The model routinely passes combined values; silently
    # ignoring them is how real errors get buried.)
    sev_names = [t for t in re.split(r"[,\s/|]+", severity) if t in sev_map]
    sev_res = [sev_map[t] for t in sev_names]

    try:
        idx = _idx.load_or_build(f, ctx.profile)   # one cached streaming pass
    except OSError as e:
        return f"ERROR indexing {f}: {e}"
    total = idx["total"]
    note = ""

    def _with_ctx(idxs, keep):
        for i in idxs:
            keep.update(range(max(0, i - ctx_lines),
                              min(total, i + ctx_lines + 1)))

    if head:
        sel = list(range(min(int(head), total)))
    elif tail:
        sel = list(range(max(0, total - int(tail)), total))
    elif rx or ("warn" in sev_names):
        # Arbitrary regex (or a 'warn' request — warn lines aren't indexed):
        # bounded streaming scan with early-stop, never a full materialize.
        sel, _ = _idx.scan(f, rx, sev_res, ctx_lines, max_lines + 1)
    elif sev_res:
        # error/fatal only => answer straight from the index, no file scan.
        want = set(sev_names)
        hits = [ln for ln, off, s in idx["severe"] if s in want]
        keep: set[int] = set()
        _with_ctx(hits, keep)
        sel = sorted(keep)
    else:
        # No filter: a *triage view*, NOT the tail. The terminal FATAL is
        # usually near the end but the root cause is the FIRST severe event
        # (cascade symptoms come after it). Built entirely from the index.
        severe = [e[0] for e in idx["severe"]]
        fatal = [e[0] for e in idx["severe"] if e[2] == "fatal"]
        stages = [s[0] for s in idx["stages"]]
        n_sev = idx["n_err"] + idx["n_fat"]
        keep = set()
        if n_sev:
            first_n = max(1, max_lines // 3)
            _with_ctx(severe[:first_n], keep)   # root-cause candidates
            _with_ctx(fatal, keep)              # terminal failure(s)
            keep.update(stages)                 # stage timeline
            keep.update(range(max(0, total - max_lines // 4), total))  # tail
            note = (f"\n[triage view: {n_sev} severe line(s), "
                    f"{idx['n_fat']} fatal; showing the FIRST errors (likely "
                    f"root cause), all fatals, the stage map, and the tail. "
                    f"The cause is usually the earliest error, not the last. "
                    f"Use severity=/pattern= to scan everything.]")
        else:
            keep.update(range(max(0, total - max_lines), total))
            note = "\n[no severe lines found; showing the tail.]"
        sel = sorted(keep)

    if len(sel) > max_lines:
        sel = sel[:max_lines]
        clipped = f"\n[... {len(sel)} of more matching lines; refine pattern/max_lines ...]"
    else:
        clipped = ""

    # Read text for exactly the selected lines (+ census preview lines) via
    # grid-anchored seeks — O(window) RAM, never the whole file.
    n_err, n_fat, n_warn = idx["n_err"], idx["n_fat"], idx.get("n_warn", 0)
    err_lns = [e[0] for e in idx["severe"] if e[2] == "error"]
    fat_lns = [e[0] for e in idx["severe"] if e[2] == "fatal"]
    firsts = sorted(set(fat_lns[:3] + err_lns[:5]))[:6]
    text = _idx.fetch_text(f, idx, set(sel) | set(firsts))

    # --- always-on ERROR/FATAL census (model-independent safety net) -------
    # Whatever window the model asked for, it must SEE that errors exist and
    # where the first ones are — counts are EXACT even on a 2 GB file.
    sel_set = set(sel)
    if n_err or n_fat:
        listing = "; ".join(
            f"L{i + 1} {text.get(i, '').strip()[:80]}" for i in firsts)
        census = (f"\n⚠ CENSUS: {n_fat} FATAL + {n_err} ERROR + {n_warn} WARN "
                  f"line(s) in this {total}-line file (EXACT whole-file "
                  f"counts; call log_summary for the distinct-code table). "
                  f"First: {listing}")
        seen = sum(1 for e in idx["severe"] if e[0] in sel_set)
        unseen = (n_err + n_fat) - seen
        if unseen > 0:
            census += (f"\n⚠ {unseen} of them are NOT in the lines below "
                       f"— you have not seen the actual errors yet. Re-call "
                       f"read_logs(severity=\"error\") (and \"fatal\") on the "
                       f"WHOLE file before stating any root cause. A WARNING "
                       f"is not the root cause while ERROR/FATAL lines exist.")
    else:
        census = (f"\n✓ CENSUS: 0 ERROR/FATAL lines in this {total}-line file "
                  f"(warnings only — {n_warn} WARN, the worst severity here "
                  f"is WARN; call log_summary for the distinct-code table).")

    out, prev = [], None
    for i in sel:
        if prev is not None and i != prev + 1:
            out.append("   ──")
        out.append(f"{i + 1:>7}: {text.get(i, '')}")
        prev = i
    body = "\n".join(out) if out else "(no matching lines)"
    header = f"# {f}  ({total} lines total){census}{note}\n"
    return truncate(header + body + clipped, ctx.cfg.tool_result_char_budget)


LIST_LOGS = Tool(
    name="list_logs",
    description="List the log files available for analysis (path, line count, size).",
    parameters={"type": "object", "properties": {}},
    run=_list_logs,
)

READ_LOGS = Tool(
    name="read_logs",
    description=(
        "Read lines from a log file with optional filtering. Returns "
        "1-indexed, line-numbered text you can cite. Use `pattern` (regex) "
        "and/or `severity` to scan the WHOLE file for errors; `tail`/`head` "
        "to bound; `context` for surrounding lines around matches. With no "
        "filter it returns a triage view (the FIRST errors — usually the "
        "root cause — plus every FATAL, the stage map, and the tail), not "
        "just the end of the file."),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string",
                     "description": "Log file path or name (omit to use the only/first log)."},
            "pattern": {"type": "string",
                        "description": "Case-insensitive regex to match lines."},
            "severity": {"type": "string",
                         "description": "Keep only lines at these severities. "
                         "One or more of fatal/error/warn, comma-separated "
                         "(e.g. \"error\" or \"error,fatal\"). Scans the whole file."},
            "head": {"type": "integer", "description": "First N lines."},
            "tail": {"type": "integer", "description": "Last N lines."},
            "context": {"type": "integer",
                        "description": "Lines of context around each match (default 2)."},
            "max_lines": {"type": "integer",
                          "description": "Cap on returned lines (default 200)."},
        },
    },
    run=_read_logs,
)
