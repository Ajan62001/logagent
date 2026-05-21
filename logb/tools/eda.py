"""EDA-specific tools — only registered when the active profile is `eda`.

These are domain-specialized helpers that leverage Innovus / PrimeTime /
VCS conventions (stage banners, `(CODE-NNN)` message codes, SDC/TCL files)
to give the agent shortcuts a generic profile can't offer:

  * stage_timeline   — pipeline view: which stages ran, how long, status
  * stage_errors     — first error per stage; makes the "earliest error
                       is the root cause" rule concrete and machine-checkable
  * sdc_lint         — light static check on a .sdc file (clocks referenced
                       before create_clock — exactly the bug pattern in the
                       bundled sample log)
  * code_lookup      — given an EDA code (e.g. IMPSDC-3071), return its log
                       occurrence + matching manual passage in one call,
                       saving a round-trip
"""

from __future__ import annotations

import re
from pathlib import Path

from .. import index as _idx
from .base import Tool, ToolContext, truncate
from .logs import _pick_file


def _stage_rows(idx: dict) -> list[dict]:
    """Build display rows directly from the index's pre-parsed
    `stage_ranges` and `stage_hist`. No log re-read, no banner re-parse."""
    ranges = idx.get("stage_ranges") or []
    hist = idx.get("stage_hist") or {}
    out: list[dict] = []
    end_marker = idx.get("total", 0)
    for entry in ranges:
        # stage_ranges shape: [start_line, end_line, name, start_ts, end_ts]
        if len(entry) >= 5:
            start_line, end_line, name, start_ts, end_ts = entry[:5]
        else:                                # back-compat (no ts captured)
            start_line, end_line, name = entry[0], entry[1], entry[2]
            start_ts = end_ts = None
        bucket = hist.get(name, {})
        errs = bucket.get("error", 0)
        fats = bucket.get("fatal", 0)
        incomplete = end_line == end_marker and end_ts is None
        if incomplete:
            status = f"FATAL" if fats else (
                f"ERROR" if errs else "INCOMPLETE")
            if status != "INCOMPLETE":
                # Match the original phrasing: explicit INCOMPLETE marker
                # when the run died inside the stage.
                status = "INCOMPLETE"
        elif fats:
            status = "FATAL"
        elif errs:
            status = "ERROR"
        else:
            status = "OK"
        dur = (end_ts - start_ts) if (start_ts is not None
                                       and end_ts is not None) else None
        out.append({
            "name": name, "start_line": start_line,
            "end_line": None if incomplete else end_line,
            "duration_s": dur, "errors": errs, "fatals": fats,
            "status": status,
        })
    return out


# --------------------------------------------------------------------------- #
#  stage_timeline                                                             #
# --------------------------------------------------------------------------- #
def _stage_timeline(args: dict, ctx: ToolContext) -> str:
    f = _pick_file(args, ctx.cfg, ctx.profile)
    if f is None or not f.is_file():
        return f"Log not found: {args.get('path')!r}. Call list_logs."
    try:
        idx = _idx.load_or_build(f, ctx.profile)
    except OSError as e:
        return f"ERROR indexing {f}: {e}"

    rows = _stage_rows(idx)
    if not rows:
        return ("(no stage banners detected — this log doesn't follow the "
                "Innovus `--- Starting \"<stage>\" ---` convention.)")

    out = [f"# Stage timeline for {f}  ({idx['total']} lines, "
           f"{idx['n_fat']} FATAL · {idx['n_err']} ERROR · "
           f"{idx.get('n_warn', 0)} WARN)"]
    out.append(f"{'STAGE':<22} {'START':>7} {'END':>7} "
               f"{'DUR':>7} {'ERRS':>5} {'FATS':>5} STATUS")
    for r in rows:
        sl = f"L{r['start_line'] + 1}" if r['start_line'] is not None else "—"
        el = f"L{r['end_line'] + 1}" if r['end_line'] is not None else "—"
        dur = f"{r['duration_s']}s" if r['duration_s'] is not None else "—"
        out.append(f"{r['name']:<22} {sl:>7} {el:>7} "
                   f"{dur:>7} {r['errors']:>5} {r['fatals']:>5} {r['status']}")
    return truncate("\n".join(out), ctx.cfg.tool_result_char_budget)


# --------------------------------------------------------------------------- #
#  stage_errors                                                               #
# --------------------------------------------------------------------------- #
def _stage_errors(args: dict, ctx: ToolContext) -> str:
    """Group the indexed severe lines by which stage they fell inside.
    Reads `stage_ranges` + `stage_hist` directly — no per-call re-bucketing,
    no second pass over `severe`."""
    f = _pick_file(args, ctx.cfg, ctx.profile)
    if f is None or not f.is_file():
        return f"Log not found: {args.get('path')!r}. Call list_logs."
    try:
        idx = _idx.load_or_build(f, ctx.profile)
    except OSError as e:
        return f"ERROR indexing {f}: {e}"

    severe = idx.get("severe") or []
    ranges = idx.get("stage_ranges") or []
    hist = idx.get("stage_hist") or {}
    if not severe:
        return f"# {f}\n(no severe lines in this log — nothing to group.)"
    if not ranges:
        # No stage banners — fall back to a flat list of first-N severe.
        n = min(int(args.get("max", 10)), 50)
        first = severe[:n]
        text = _idx.fetch_text(f, idx, {ln for ln, _, _ in first})
        out = [f"# stage_errors (no stage banners in this log; flat listing)"]
        for ln, _off, sev in first:
            out.append(f"  L{ln + 1} [{sev}] {text.get(ln, '').strip()[:140]}")
        return truncate("\n".join(out), ctx.cfg.tool_result_char_budget)

    # Per-stage first error/fatal line, by walking severe once. Keeps the
    # old display semantics (warn-only stages are not surfaced — the tool
    # is `stage_errors`, not `stage_warnings`). hist still gives accurate
    # error+fatal counts; we only need severe for the first-line preview.
    first_ef_by_stage: dict[str, tuple[int, str]] = {}
    pre_first: tuple[int, str] | None = None
    for ln, _off, sev in severe:
        owner = None
        for entry in ranges:
            start, end, name = entry[0], entry[1], entry[2]
            if start < ln < end:
                owner = name
                break
        if owner is None:
            if pre_first is None:
                pre_first = (ln, sev)
        elif owner not in first_ef_by_stage:
            first_ef_by_stage[owner] = (ln, sev)

    first_lines = {ln for ln, _ in first_ef_by_stage.values()}
    if pre_first is not None:
        first_lines.add(pre_first[0])
    text = _idx.fetch_text(f, idx, first_lines) if first_lines else {}

    out = [f"# stage_errors for {f}"]
    for entry in ranges:
        start, end, name = entry[0], entry[1], entry[2]
        bucket = hist.get(name, {})
        n_ef = bucket.get("error", 0) + bucket.get("fatal", 0)
        if not n_ef:
            continue
        first = first_ef_by_stage.get(name)
        if first is None:
            continue
        first_ln, first_sev = first
        line_text = text.get(first_ln, "").strip()[:160]
        out.append(f"\n## {name}  (L{start + 1}-{end + 1}) — "
                   f"{n_ef} severe line(s), first at L{first_ln + 1}")
        out.append(f"  [{first_sev}] {line_text}")
    if pre_first is not None:
        ln, sev = pre_first
        pre_bucket = hist.get("<pre-stage>", {})
        n_pre = pre_bucket.get("error", 0) + pre_bucket.get("fatal", 0)
        if n_pre:
            out.append(f"\n## <pre-stage>  — {n_pre} severe line(s) "
                       "before any stage banner")
    return truncate("\n".join(out), ctx.cfg.tool_result_char_budget)


# --------------------------------------------------------------------------- #
#  sdc_lint                                                                   #
# --------------------------------------------------------------------------- #
_SDC_CREATE_CLOCK_NAMED = re.compile(r"\bcreate_clock\b[^#\n]*?-name\s+(\w+)")
_SDC_CREATE_CLOCK_PORT = re.compile(
    r"\bcreate_clock\b(?![^#\n]*?-name\b)[^#\n]*?\[get_ports\s+(\w+)\s*\]")
_SDC_GET_CLOCKS = re.compile(r"\bget_clocks\s+(\w+)")


def _sdc_lint(args: dict, ctx: ToolContext) -> str:
    raw = (args.get("path") or "").strip()
    if not raw:
        return ("ERROR: `path` is required (path to a .sdc file mentioned in "
                "the log).")
    # Reuse the agent's cite-path resolver so this works whether the model
    # passed "top.sdc", "scripts/constraints/top.sdc", or the absolute path.
    from ..agent import _resolve_cite_path
    resolved = _resolve_cite_path(raw, ctx.cfg)
    if resolved is None or not resolved.is_file():
        return f"ERROR: SDC file not found: {raw!r}."

    declared: dict[str, int] = {}     # clock name -> line declared
    referenced: list[tuple[str, int]] = []
    try:
        text = resolved.read_text(errors="replace")
    except OSError as e:
        return f"ERROR reading {resolved}: {e}"

    for i, raw_line in enumerate(text.splitlines(), 1):
        # Strip comments. Tcl line continuation (`\` at EOL) is rare in
        # SDCs; we don't model it. Lint stays line-local.
        line = raw_line.split("#", 1)[0]
        if not line.strip():
            continue
        for m in _SDC_CREATE_CLOCK_NAMED.finditer(line):
            declared.setdefault(m.group(1), i)
        for m in _SDC_CREATE_CLOCK_PORT.finditer(line):
            declared.setdefault(m.group(1), i)
        for m in _SDC_GET_CLOCKS.finditer(line):
            referenced.append((m.group(1), i))

    issues: list[str] = []
    for name, ln in referenced:
        decl = declared.get(name)
        if decl is None:
            issues.append(
                f"L{ln}: get_clocks {name!r} — clock NEVER declared "
                "with create_clock anywhere in this file.")
        elif decl > ln:
            issues.append(
                f"L{ln}: get_clocks {name!r} — referenced BEFORE its "
                f"create_clock at L{decl}. This is the canonical "
                "\"clock referenced before created\" bug.")

    out = [f"# sdc_lint {resolved}",
           f"declared clocks: {sorted(declared.keys()) or '(none)'}",
           f"get_clocks references: {len(referenced)}"]
    if issues:
        out.append("")
        out.append("ISSUES:")
        out.extend(f"  - {p}" for p in issues)
    else:
        out.append("(no issues found by sdc_lint)")
    return truncate("\n".join(out), ctx.cfg.tool_result_char_budget)


# --------------------------------------------------------------------------- #
#  code_lookup                                                                #
# --------------------------------------------------------------------------- #
def _code_lookup(args: dict, ctx: ToolContext) -> str:
    code = (args.get("code") or "").strip().upper()
    if not code:
        return ("ERROR: `code` is required (e.g. 'IMPSDC-3071'). Get codes "
                "from log_summary's distinct-code table.")
    nth = args.get("nth")
    try:
        nth = int(nth) if nth is not None else None
    except (TypeError, ValueError):
        return f"ERROR: `nth` must be an integer, got {args.get('nth')!r}."

    f = _pick_file(args, ctx.cfg, ctx.profile)
    out: list[str] = [f"# code_lookup: {code}"
                       + (f"  (nth={nth})" if nth is not None else "")]

    if f is None or not f.is_file():
        out.append("(no target log to search for occurrences)")
    else:
        try:
            idx = _idx.load_or_build(f, ctx.profile)
        except OSError as e:
            return f"ERROR indexing {f}: {e}"
        log_entry = next((c for c in idx.get("codes", []) if c[0] == code),
                          None)
        if log_entry is None:
            out.append(f"In log ({f.name}): NOT FOUND. The log's "
                       "distinct-code table does not include this code "
                       "— either it never appeared or the indexer didn't "
                       "extract it.")
        else:
            _code, sev, count, first_line, _off = log_entry
            if nth is None:
                # Default behavior — first occurrence.
                text = _idx.fetch_text(f, idx, {first_line})
                out.append(f"In log ({f.name}): {count} occurrence(s), "
                           f"severity={sev}, first at L{first_line + 1}")
                out.append(f"  > {text.get(first_line, '').strip()[:200]}")
            else:
                # nth-occurrence lookup against the indexed head/tail.
                occ = (idx.get("code_occurrences") or {}).get(code, {})
                head = occ.get("head") or []
                tail = occ.get("tail") or []
                target_line = None
                position_note = None
                if nth < 1 or nth > count:
                    out.append(f"In log ({f.name}): nth={nth} is out of "
                               f"range — code has {count} occurrence(s).")
                elif nth <= len(head):
                    target_line = head[nth - 1]
                    position_note = f"head[{nth - 1}]"
                elif nth > count - len(tail):
                    # Tail position. tail[-1] is the last occurrence.
                    tail_idx = nth - (count - len(tail)) - 1
                    target_line = tail[tail_idx]
                    position_note = f"tail[{tail_idx}]"
                else:
                    out.append(f"In log ({f.name}): occurrence #{nth} of "
                               f"{count} falls BETWEEN the indexed head "
                               f"({len(head)}) and tail ({len(tail)}) "
                               "samples. Use "
                               f"read_logs(pattern='{code}', max_lines="
                               f"{count}) to enumerate every hit.")
                if target_line is not None:
                    text = _idx.fetch_text(f, idx, {target_line})
                    out.append(f"In log ({f.name}): occurrence #{nth} of "
                               f"{count} ({position_note}), severity={sev}, "
                               f"at L{target_line + 1}")
                    out.append(f"  > {text.get(target_line, '').strip()[:200]}")

    # Manual side. The User Guide is not keyed by code, so searching the
    # bare code returns BM25 noise. If we found the code in the log we
    # have its literal message line — derive a natural-language template
    # from it and search by that instead. This is the same auto-promote
    # behavior search_manual now does, but here we already have the line
    # without a second log fetch.
    if ctx.manual_index is not None:
        from .manual import _extract_message_template  # local: avoid cycle
        template = ""
        line_for_search = None
        if f is not None and f.is_file() and 'log_entry' in dir():
            pass  # placeholder — log_entry resolved above
        # Re-derive the first-occurrence line cheaply (already fetched above
        # into `text` for default path; nth path has its own `text`).
        try:
            log_entry = next((c for c in idx.get("codes", [])
                               if c[0] == code), None)
        except (NameError, AttributeError):
            log_entry = None
        if log_entry is not None:
            first_line = log_entry[3]
            txt = _idx.fetch_text(f, idx, {first_line})
            raw = txt.get(first_line, "").strip()
            if raw:
                template = _extract_message_template(raw)
                line_for_search = raw

        query = template if template and len(template.split()) >= 3 else code
        hits = ctx.manual_index.search(query, k=2)
        # BM25 always returns something — that's noise on irrelevant
        # matches. Threshold is corpus-size-aware: a real manual (1000+
        # chunks) needs a higher floor than a tiny test fixture.
        corpus_size = len(
            getattr(ctx.manual_index, "_chunks", []) or [])
        MANUAL_MIN_SCORE = 20.0 if corpus_size >= 50 else 0.0
        relevant = [(s, c) for s, c in hits if s >= MANUAL_MIN_SCORE]

        out.append("")
        if not relevant:
            top_score = hits[0][0] if hits else 0.0
            out.append(
                f"From manual: NOT FOUND. No passage in the manual is "
                f"relevant to this error (best BM25 score "
                f"{top_score:.1f} < threshold {MANUAL_MIN_SCORE:.1f} — "
                "token overlap, not an actual explanation). The "
                "indexed manual is the Innovus User Guide, which does "
                "not document every message code. In your final "
                "answer, write: 'manual has no entry for "
                f"{code}' — do NOT cite or paraphrase the low-score "
                "BM25 hits.")
        else:
            if line_for_search and query != code:
                out.append(f"From manual (searched by message template "
                           f"{query!r}, derived from the log line):")
            else:
                out.append(f"From manual (searched by code {code!r}):")
            for rank, (score, chunk) in enumerate(relevant, 1):
                loc = f"{chunk.source}:{chunk.start_line}"
                if chunk.page is not None:
                    loc += f" (page {chunk.page})"
                out.append(f"  [{rank}] {loc} > "
                           f"{chunk.heading} (score {score:.2f})")
                snippet = chunk.text.strip().replace("\n", " ")[:300]
                out.append(f"      {snippet}")

    # NEXT-STEPS NUDGE: list every OTHER severe code-prefix in this log so
    # the model doesn't stop after just this one. Cascade rule applies
    # WITHIN a prefix; across prefixes each is independent and needs its
    # own code_lookup.
    try:
        all_codes = idx.get("codes", []) if 'idx' in dir() else []
    except Exception:
        all_codes = []
    cur_prefix = code.rsplit("-", 1)[0] if "-" in code else code
    others = []
    seen_prefixes = {cur_prefix}
    for c, sev, n, ln, _off in all_codes:
        if sev not in ("fatal", "error"):
            continue
        pfx = c.rsplit("-", 1)[0]
        if pfx in seen_prefixes:
            continue
        seen_prefixes.add(pfx)
        others.append((c, sev, n, ln))
    if others:
        out.append("")
        out.append("TODO — other independent failure(s) in this log "
                   "(different prefix families, not cascade of this one):")
        for i, (c, sev, n, ln) in enumerate(others, 1):
            out.append(f"  [{i}] code_lookup(code={c!r})   "
                       f"# {sev.upper()}, {n} occurrence(s), first @ L{ln + 1}")
        out.append("  Investigate each one before producing a final answer.")
    return truncate("\n".join(out), ctx.cfg.tool_result_char_budget)


# --------------------------------------------------------------------------- #
#  Tool descriptors                                                           #
# --------------------------------------------------------------------------- #
STAGE_TIMELINE = Tool(
    name="stage_timeline",
    description=(
        "EDA-only. Render the pipeline as a table: each stage with its "
        "start/end line, duration (in wallclock seconds from log "
        "timestamps), error/fatal counts inside it, and OK/ERROR/FATAL/"
        "INCOMPLETE status. An INCOMPLETE row marks the stage where the "
        "run died — usually the most important line in the table. Built "
        "on the cached index; nearly free to call."),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string",
                     "description": "Log file path or name (omit for the "
                     "target log)."},
        },
    },
    run=_stage_timeline,
    profile_required="eda",
)

STAGE_ERRORS = Tool(
    name="stage_errors",
    description=(
        "EDA-only. Group every severe line in the log by which stage it "
        "occurred in. Shows the first error per stage so you can apply "
        "the cascade rule (earliest error in earliest stage is the root "
        "cause) without manual book-keeping. Falls back to a flat first-N "
        "listing if the log has no stage banners."),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string",
                     "description": "Log file path or name (omit for the "
                     "target log)."},
            "max": {"type": "integer",
                    "description": "When no stage banners exist, cap the "
                    "flat listing at this many lines (default 10, max 50)."},
        },
    },
    run=_stage_errors,
    profile_required="eda",
)

SDC_LINT = Tool(
    name="sdc_lint",
    description=(
        "EDA-only. Run a light static check on an .sdc file: find clocks "
        "referenced via get_clocks before their create_clock declaration "
        "(the canonical \"clock referenced before created\" bug), and "
        "clocks referenced that are never declared. Pass the SDC file "
        "path from a log error (the agent will resolve it under "
        "project_root)."),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string",
                     "description": "Path to the .sdc file (relative to "
                     "project_root works; the resolver tries common "
                     "locations)."},
        },
        "required": ["path"],
    },
    run=_sdc_lint,
    profile_required="eda",
)

CODE_LOOKUP = Tool(
    name="code_lookup",
    description=(
        "EDA-only convenience: given a message code like 'IMPSDC-3071', "
        "return its occurrence in the log (line + text + severity) AND "
        "the top matching manual passage in a single tool call. By "
        "default returns the FIRST occurrence; pass `nth=N` to fetch a "
        "different occurrence — works for any N in the first "
        "CODE_OCC_HEAD (16) or last CODE_OCC_TAIL (16) hits of a code. "
        "For occurrences between head and tail (only matters when a "
        "code fires >32 times), the result tells the agent to fall "
        "back to read_logs(pattern=<code>). If the manual has no match, "
        "the result says so explicitly — do not fabricate an "
        "explanation."),
    parameters={
        "type": "object",
        "properties": {
            "code": {"type": "string",
                     "description": "The EDA code (e.g. 'IMPSDC-3071')."},
            "nth": {"type": "integer",
                    "description": "Optional: 1-indexed occurrence "
                    "number. Default 1 (first). Use the latest "
                    "occurrence count from log_summary or this tool's "
                    "own output to pick valid N."},
            "path": {"type": "string",
                     "description": "Log file path or name (omit for the "
                     "target log)."},
        },
        "required": ["code"],
    },
    run=_code_lookup,
    profile_required="eda",
)
