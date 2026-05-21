"""search_manual — BM25 retrieval over the manual/docs corpus (RAG)."""

from __future__ import annotations

import re

from .base import Tool, ToolContext, truncate

# Severity prefix + parenthesised code that wraps the literal message in
# logs (e.g. "**ERROR: (IMPLF-213): Ignoring MASK ..." → strip both, keep
# the natural-language template).
_SEV_PREFIX_RX = re.compile(
    r"^\s*(\*\*)?(FATAL|ERROR|ERR|WARN(?:ING)?|INFO|NOTE)\s*:?\s*", re.I)
_PAREN_CODE_RX = re.compile(r"\(\s*[A-Z][A-Z0-9_]+-\d+\s*\)\s*:?\s*")
_QUOTED_RX = re.compile(r"['\"][^'\"]{1,80}['\"]")   # 'F6', '3', 'mac_X'
_NUM_RX = re.compile(r"\b\d+(?:\.\d+)?\b")            # bare numbers
_PATH_RX = re.compile(r"\S*/\S+")                     # file paths
_HEX_ADDR_RX = re.compile(r"\b0x[0-9a-fA-F]+\b")

# Looks like an error code: ALL-CAPS-WORD + dash + digits (EDA),
# or a klog-style E0520, or a bracketed [ERROR-1234]. Any of these are
# highly specific search terms that should always be retried individually.
_CODE_LIKE_RX = re.compile(
    r"\b([A-Z][A-Z0-9_]+-\d+|[EWIF]\d{4,})\b")

# A token we'd consider real "content" — drop short stop-ish words and
# punctuation. Used to decide whether a query is essentially just a bare
# code (no semantic content the manual could match against).
_WORD = re.compile(r"[A-Za-z]{3,}")
_STOPISH = {"the", "and", "for", "with", "from", "what", "does",
            "mean", "this", "that", "error", "warning", "code"}


def _extract_message_template(line: str) -> str:
    """Strip a log line down to its natural-language template — the part
    that's identical across every instance of this error. Removes the
    severity prefix, the parenthesised code, quoted instance/macro/layer
    names, file paths, and bare numbers. The remainder is what BM25 should
    actually match against the manual.

    Example:
      '**ERROR: (IMPLF-213): Ignoring MASK value 3 in RECT on layer F6
       in macro dwc_lpddr5xphy_pclk_rptx1 because the layer has no MASK
       statement defined.'
      → 'Ignoring MASK value in RECT on layer in macro because the layer
         has no MASK statement defined.'
    """
    s = line.strip()
    s = _SEV_PREFIX_RX.sub("", s)
    s = _PAREN_CODE_RX.sub("", s)
    s = _PATH_RX.sub(" ", s)
    s = _HEX_ADDR_RX.sub(" ", s)
    s = _QUOTED_RX.sub(" ", s)
    s = _NUM_RX.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip(" :.-")
    return s


def _lookup_code_in_log(code: str, ctx: ToolContext) -> str | None:
    """If `code` appears in the configured target log, return the literal
    first-occurrence message line. Returns None if no log is configured
    or the code isn't there."""
    try:
        from .logs import _pick_file  # local import: avoid cycle
        from .. import index as _idx
    except ImportError:
        return None
    try:
        f = _pick_file({}, ctx.cfg, ctx.profile)
    except Exception:
        return None
    if f is None or not f.is_file():
        return None
    try:
        idx = _idx.load_or_build(f, ctx.profile)
    except OSError:
        return None
    entry = next((c for c in idx.get("codes", []) if c[0] == code), None)
    if entry is None:
        return None
    first_line = entry[3]
    text = _idx.fetch_text(f, idx, {first_line})
    return text.get(first_line, "").strip() or None


def _is_bare_code_query(query: str) -> bool:
    """True if the only useful token in the query is a code like
    'IMPLF-213'. Codes alone score high in BM25 against irrelevant chunks
    (e.g. 'IMPLF-40' will match any 'WIDTH 0.40' line) because the corpus
    is the User Guide, not the Messages Reference — so warn callers."""
    codes = _CODE_LIKE_RX.findall(query)
    if not codes:
        return False
    stripped = _CODE_LIKE_RX.sub(" ", query)
    content = [t.lower() for t in _WORD.findall(stripped)
               if t.lower() not in _STOPISH]
    return not content


def _expanded_queries(query: str) -> list[str]:
    """Generate a small set of related queries from a single user query.

    Strategy: keep the original; also add each code-like token as its own
    query (matches manual sections keyed on the code even when the
    surrounding prose is different). De-duplicated, original first.
    """
    out = [query]
    seen = {query.lower()}
    for m in _CODE_LIKE_RX.finditer(query):
        token = m.group(1)
        if token.lower() not in seen:
            out.append(token)
            seen.add(token.lower())
    return out


def _search_manual(args: dict, ctx: ToolContext) -> str:
    query = (args.get("query") or "").strip()
    if not query:
        return "ERROR: `query` is required."
    k = int(args.get("k", 4))
    expand = bool(args.get("expand", True))

    # Auto-promote bare-code queries: the User Guide isn't keyed by code,
    # so 'IMPLF-213' returns BM25 noise. Look the code up in the target
    # log, extract the natural-language template, and search by THAT.
    auto_note = ""
    if _is_bare_code_query(query):
        m = _CODE_LIKE_RX.search(query)
        code = m.group(1) if m else None
        if code:
            line = _lookup_code_in_log(code, ctx)
            if line:
                template = _extract_message_template(line)
                if template and len(template.split()) >= 3:
                    auto_note = (
                        f"# search_manual: auto-promoted bare code {code!r} "
                        f"to message-text search.\n"
                        f"  log line (first occurrence): {line[:200]}\n"
                        f"  derived template: {template!r}\n"
                        f"  searching the manual for this template instead "
                        f"of the bare code (the User Guide is not indexed "
                        f"by code).\n\n")
                    query = template

    queries = _expanded_queries(query) if expand else [query]

    # Search each query, union by chunk identity, keep best score per chunk.
    union: dict = {}     # id(chunk) -> (best_score, chunk, matched_query)
    for q in queries:
        for score, chunk in ctx.manual_index.search(q, k=k):
            key = id(chunk)
            prev = union.get(key)
            if prev is None or score > prev[0]:
                union[key] = (score, chunk, q)

    if not union:
        notes = (f"No manual passages matched {query!r}"
                 + (f" or its variants {queries[1:]!r}" if len(queries) > 1
                    else "")
                 + f". The manual dir ({ctx.cfg.manual_dir!r}) may be empty "
                 "or lack this topic. Do NOT fabricate an explanation — "
                 "say the manual has no entry for this.")
        return notes

    ranked = sorted(union.values(), key=lambda x: x[0], reverse=True)[:k]
    # BM25 always returns something — that's noise on irrelevant matches.
    # The threshold below which we declare "not found" depends on corpus
    # size: BM25's IDF term log((N-df+0.5)/(df+0.5)) is small when N is
    # small, so a perfect match in a 1-chunk fixture scores lower than
    # a noise match in a 1000-chunk manual. Only apply the threshold
    # when the corpus is large enough for the score to be meaningful.
    corpus_size = len(getattr(ctx.manual_index, "_chunks", []) or [])
    MANUAL_MIN_SCORE = 20.0 if corpus_size >= 50 else 0.0
    relevant = [(s, c, q) for s, c, q in ranked
                if s >= MANUAL_MIN_SCORE]
    if not relevant:
        top_score = ranked[0][0] if ranked else 0.0
        return truncate(
            auto_note
            + f"From manual: NOT FOUND. No passage matched "
            f"{query!r} above the relevance threshold (best BM25 "
            f"score {top_score:.1f} < {MANUAL_MIN_SCORE:.1f}). The "
            "indexed manual is the Innovus User Guide, which does "
            "not document every topic or code. In your final answer, "
            "write 'manual has no entry for this' — do NOT cite the "
            "below-threshold hits and do NOT fabricate an explanation.",
            ctx.cfg.tool_result_char_budget)
    blocks = []
    for rank, (score, c, matched_q) in enumerate(relevant, 1):
        match_note = (f" (matched by query={matched_q!r})"
                       if matched_q != query else "")
        loc = f"{c.source}:{c.start_line}"
        if c.page is not None:
            loc += f" (page {c.page})"
        blocks.append(
            f"## [{rank}] {loc}  ›  {c.heading}  "
            f"(score {score:.2f}{match_note})\n{c.text}")
    header = ""
    if len(queries) > 1:
        header = (f"# search_manual: tried {len(queries)} query variant(s): "
                  f"{queries}\n\n")
    # NEXT-STEPS NUDGE for bare-code searches: after we hand back the
    # promoted hit(s), enumerate the OTHER severe code prefixes in the log
    # so the model doesn't stop after one. Only fires when auto_note is
    # set (i.e. we successfully promoted a bare code).
    todo_block = ""
    if auto_note and _CODE_LIKE_RX.search(args.get("query") or ""):
        try:
            from .logs import _pick_file  # local: avoid cycle
            from .. import index as _idx
            f = _pick_file({}, ctx.cfg, ctx.profile)
            if f is not None and f.is_file():
                idx = _idx.load_or_build(f, ctx.profile)
                cur_code = _CODE_LIKE_RX.search(
                    args.get("query") or "").group(1)
                cur_prefix = cur_code.rsplit("-", 1)[0]
                seen = {cur_prefix}
                rows = []
                for c, sev, n, ln, _off in idx.get("codes", []):
                    if sev not in ("fatal", "error"):
                        continue
                    pfx = c.rsplit("-", 1)[0]
                    if pfx in seen:
                        continue
                    seen.add(pfx)
                    rows.append((c, sev, n, ln))
                if rows:
                    todo_block = ("\n\nTODO — other independent failure(s) "
                                   "in this log (different prefix "
                                   "families, not cascade of "
                                   f"{cur_code}):\n")
                    for i, (c, sev, n, ln) in enumerate(rows, 1):
                        todo_block += (
                            f"  [{i}] code_lookup(code={c!r})   "
                            f"# {sev.upper()}, {n}x, first @ L{ln + 1}\n")
                    todo_block += ("  Investigate each before producing a "
                                    "final answer.\n")
        except Exception:
            pass

    # If auto-promotion ran above, the bare-code path is already handled —
    # the message-text query usually returns real hits. The fallback
    # warning only applies when we COULDN'T auto-promote (no log, code
    # not in log, template too short).
    if not auto_note and _is_bare_code_query(query):
        header += (
            "⚠ HEURISTIC WARNING: this query is a bare error code and "
            "the target log either has no occurrence of it or the "
            "auto-extracted message text was too short to search. The "
            "indexed manual is the Innovus User Guide, NOT the Messages "
            "Reference — codes are NOT indexed verbatim here, so the "
            "matches below were ranked by token overlap and are very "
            "likely SPURIOUS. Do NOT quote them. Re-run with a query "
            "built from the literal message text (the words after the "
            f"code in the log line where {query!r} appears).\n\n")
    return truncate(auto_note + header + "\n\n".join(blocks) + todo_block,
                    ctx.cfg.tool_result_char_budget)


SEARCH_MANUAL = Tool(
    name="search_manual",
    description=(
        "Search the product manual / troubleshooting docs for guidance "
        "(error-code explanations, recommended fixes, command reference). "
        "Returns the top passages with their source file and heading so you "
        "can cite them. Use the exact error code or message tokens as the "
        "query. By default also retries with any code-shaped tokens it "
        "finds in the query (e.g. 'IMPSDC-3071') as their own search, "
        "since codes are highly specific. Pass expand=false to disable. "
        "The manual is re-scanned each call, so files added/edited mid-"
        "session are picked up automatically."),
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string",
                      "description": "Search query — error code, message text, or topic."},
            "k": {"type": "integer",
                  "description": "How many passages (default 4)."},
            "expand": {"type": "boolean",
                       "description": "When true (default), also retry "
                       "with each code-like token in the query as its "
                       "own search and union the results."},
        },
        "required": ["query"],
    },
    run=_search_manual,
)
