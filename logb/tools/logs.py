"""Log tools: list_logs + read_logs (grep / severity / head / tail / context).

read_logs returns 1-indexed, line-numbered output so the agent can cite exact
locations ("line 4213") and follow any file paths printed in those lines via
the read_file tool.
"""

from __future__ import annotations

import json as _json
import re
from pathlib import Path

from .. import index as _idx
from ..profiles import EDA, Profile
from .base import Tool, ToolContext, truncate

# Fields a JSON log line commonly uses for severity. Order matters: most
# specific first. Stops on the first match.
_JSON_LEVEL_FIELDS = ("level", "severity", "lvl", "log.level", "loglevel")
_JSON_MSG_FIELDS = ("msg", "message", "text", "event")


def _looks_like_jsonl(log: Path) -> bool:
    """Cheap sniff: read the first non-empty line and see if it parses
    as a JSON object. Cached on the Path via an attribute would be nicer
    but a one-shot read is bounded enough for any sane log."""
    try:
        with open(log, "rb") as f:
            for _ in range(5):
                line = f.readline()
                if not line:
                    return False
                line = line.strip()
                if not line:
                    continue
                if not line.startswith(b"{"):
                    return False
                try:
                    obj = _json.loads(line)
                except _json.JSONDecodeError:
                    return False
                return isinstance(obj, dict)
    except OSError:
        return False
    return False


def _jsonl_severity(obj: dict) -> str:
    """Normalize whatever the JSON record calls 'level' into fatal/error/
    warn (or empty if neither). Handles common spellings."""
    for k in _JSON_LEVEL_FIELDS:
        v = obj.get(k)
        if isinstance(v, (str, int)):
            s = str(v).lower()
            if s in ("fatal", "critical", "crit", "emerg", "emergency",
                     "panic", "0", "1", "2"):
                return "fatal"
            if s in ("error", "err", "severe", "exception", "3"):
                return "error"
            if s in ("warn", "warning", "warning ", "4"):
                return "warn"
            return ""
    return ""


def _jsonl_msg(obj: dict) -> str:
    """Best-effort extraction of the user-visible message from a JSON line."""
    for k in _JSON_MSG_FIELDS:
        v = obj.get(k)
        if isinstance(v, str):
            return v
    # Fall back: a short flattened key=value summary so the agent has
    # *something* to cite. Skips noisy top-level fields.
    skip = {"@timestamp", "ts", "time", "level", "severity", "lvl"}
    parts = []
    for k, v in obj.items():
        if k in skip or isinstance(v, (dict, list)):
            continue
        parts.append(f"{k}={v!r}"[:60])
        if sum(len(p) for p in parts) > 160:
            break
    return " ".join(parts)


def _read_jsonl(args: dict, ctx: ToolContext, f: Path) -> str:
    """JSONL-aware filter. Parses each line as JSON and filters by `field`
    if given (e.g. `field=level`, `value=error`), or by severity using
    _jsonl_severity. Returns the matching lines with their JSON message."""
    field = args.get("field")
    value = args.get("value")
    severity_request = (args.get("severity") or "").lower()
    max_lines = min(int(args.get("max_lines") or 200), 1000)

    want_sev: set = set()
    if severity_request:
        want_sev = {t for t in re.split(r"[,\s/|]+", severity_request)
                    if t in {"fatal", "error", "warn"}}

    out = []
    n_total = n_match = 0
    try:
        with open(f, "rb") as fh:
            for i, raw in enumerate(fh, 1):
                n_total += 1
                line = raw.strip()
                if not line:
                    continue
                try:
                    obj = _json.loads(line)
                except _json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                # Filter logic.
                if field is not None:
                    if str(obj.get(field, "")) != str(value):
                        continue
                if want_sev:
                    if _jsonl_severity(obj) not in want_sev:
                        continue
                n_match += 1
                msg = _jsonl_msg(obj)
                sev = _jsonl_severity(obj) or "?"
                out.append(f"  L{i} [{sev}] {msg[:200]}")
                if len(out) >= max_lines:
                    break
    except OSError as e:
        return f"ERROR reading {f}: {e}"

    header = (f"# JSONL read: {f}  ({n_total} lines scanned, "
              f"{n_match} match(es))")
    if field is not None:
        header += f"  filter: {field}={value!r}"
    if want_sev:
        header += f"  severity in {sorted(want_sev)}"
    body = "\n".join(out) if out else "(no matching JSON lines)"
    return truncate(header + "\n" + body, ctx.cfg.tool_result_char_budget)


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

    # JSONL path: if the file looks like JSONL OR the caller passed a
    # field-based filter, use the structured reader. Field-based filtering
    # is impossible via regex, so the agent gets the right tool here.
    if args.get("field") is not None or _looks_like_jsonl(f):
        return _read_jsonl(args, ctx, f)

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
    elif rx:
        # Arbitrary regex: bounded streaming scan with early-stop.
        sel, _ = _idx.scan(f, rx, sev_res, ctx_lines, max_lines + 1)
    elif sev_res:
        # Index-only path for ANY severity (including warn). Combines
        # severe head + severe_tail + warn_offsets so the agent gets
        # both root-cause-region and terminal-region severe lines without
        # a full re-scan, AND warn queries are now O(window) instead of
        # streaming the whole file.
        want = set(sev_names)
        hits: list[int] = []
        for ln, _off, s in idx.get("severe", []):
            if s in want:
                hits.append(ln)
        for ln, _off, s in idx.get("severe_tail", []):
            if s in want:
                hits.append(ln)
        if "warn" in want:
            for ln, _off in idx.get("warn_offsets", []):
                hits.append(ln)
        hits.sort()
        keep: set[int] = set()
        _with_ctx(hits, keep)
        sel = sorted(keep)
    else:
        # No filter: a *triage view*, NOT the tail. The terminal FATAL is
        # usually near the end but the root cause is the FIRST severe event
        # (cascade symptoms come after it). Built entirely from the index.
        # We now also surface the LAST severe lines via severe_tail —
        # that's the actual terminal failure on capped-large logs and
        # more useful than the file's literal tail (often cleanup noise).
        severe_head = [e[0] for e in idx.get("severe", [])]
        severe_tail = [e[0] for e in idx.get("severe_tail", [])]
        fatal = ([e[0] for e in idx.get("severe", []) if e[2] == "fatal"]
                 + [e[0] for e in idx.get("severe_tail", []) if e[2] == "fatal"])
        stages = [s[0] for s in idx["stages"]]
        n_sev = idx["n_err"] + idx["n_fat"]
        keep = set()
        if n_sev:
            first_n = max(1, max_lines // 3)
            _with_ctx(severe_head[:first_n], keep)   # root-cause candidates
            _with_ctx(fatal, keep)                    # terminal failure(s)
            keep.update(stages)                       # stage timeline
            # Last severe lines (when available) — more useful than the
            # file's last K lines, which are usually shutdown noise.
            if severe_tail:
                last_n = max(1, max_lines // 4)
                _with_ctx(severe_tail[-last_n:], keep)
            else:
                keep.update(range(max(0, total - max_lines // 4), total))
            note = (f"\n[triage view: {n_sev} severe line(s), "
                    f"{idx['n_fat']} fatal; showing the FIRST errors (likely "
                    f"root cause), all fatals, the stage map, and the last "
                    f"severe lines. The cause is usually the earliest error, "
                    f"not the last. Use severity=/pattern= to scan everything.]")
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

    # Repeated-line collapse: if the indexer recorded a run of identical
    # consecutive lines (length >= REPEAT_MIN), and any of those lines
    # are in our selection, replace them with one canonical line + a
    # `[× N repeats from L_first..L_last]` annotation. Frees budget for
    # signal on logs that spam the same warning hundreds of times.
    repeats = idx.get("repeats") or []
    # Build a map: first_line -> (count, last_line) for fast lookup.
    repeat_map = {first: (count, first + count - 1)
                  for first, count in repeats}
    # Build the inverse map: any line in a repeat run -> first_line.
    in_repeat: dict[int, int] = {}
    for first, (count, last) in repeat_map.items():
        for ln in range(first, last + 1):
            in_repeat[ln] = first
    out: list[str] = []
    prev: int | None = None
    skip_repeat: int | None = None  # first_line of a repeat we're inside
    for i in sel:
        if prev is not None and i != prev + 1:
            out.append("   ──")
            skip_repeat = None
        first = in_repeat.get(i)
        if first is not None:
            count, last = repeat_map[first]
            if skip_repeat == first:
                # Already emitted the canonical line for this run; skip
                # the duplicates entirely.
                prev = i
                continue
            # Emit the first line of the run with the count annotation.
            out.append(f"{i + 1:>7}: {text.get(i, '')}")
            if count >= 2:
                out.append(f"        [× {count} identical lines, "
                           f"L{first + 1}..L{last + 1}]")
            skip_repeat = first
        else:
            out.append(f"{i + 1:>7}: {text.get(i, '')}")
            skip_repeat = None
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
        "just the end of the file.\n"
        "\n"
        "JSONL support: if the log is JSON-per-line (e.g. structured app "
        "logs, klog), the tool switches to a structured reader. Pass "
        "`field` + `value` for exact field matching (`field=level "
        "value=error`), or just `severity` to filter by the JSON record's "
        "level field. Regex still works on the raw text fallback path."),
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
            "field": {"type": "string",
                      "description": "JSONL only: filter by exact match on "
                      "this JSON field (e.g. 'level', 'service', 'request_id')."},
            "value": {"type": "string",
                      "description": "JSONL only: required value when `field` is set."},
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
