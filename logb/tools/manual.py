"""search_manual — BM25 retrieval over the manual/docs corpus (RAG)."""

from __future__ import annotations

from .base import Tool, ToolContext, truncate


def _search_manual(args: dict, ctx: ToolContext) -> str:
    query = (args.get("query") or "").strip()
    if not query:
        return "ERROR: `query` is required."
    k = int(args.get("k", 4))
    hits = ctx.manual_index.search(query, k=k)
    if not hits:
        return (f"No manual passages matched {query!r}. The manual dir "
                f"({ctx.cfg.manual_dir!r}) may be empty or lack this topic.")
    blocks = []
    for rank, (score, c) in enumerate(hits, 1):
        blocks.append(
            f"## [{rank}] {c.source}  ›  {c.heading}  (score {score:.2f})\n{c.text}")
    return truncate("\n\n".join(blocks), ctx.cfg.tool_result_char_budget)


SEARCH_MANUAL = Tool(
    name="search_manual",
    description=(
        "Search the product manual / troubleshooting docs for guidance "
        "(error-code explanations, recommended fixes, command reference). "
        "Returns the top passages with their source file and heading so you "
        "can cite them. Use the exact error code or message tokens as the query."),
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string",
                      "description": "Search query — error code, message text, or topic."},
            "k": {"type": "integer", "description": "How many passages (default 4)."},
        },
        "required": ["query"],
    },
    run=_search_manual,
)
