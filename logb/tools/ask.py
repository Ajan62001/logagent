"""ask_user — pause and ask the operator a clarifying question.

The agent calls this when the request is ambiguous, a target is unspecified,
or it needs a decision. The CLI front-end (ctx.on_ask) prints the question and
blocks on stdin. In non-interactive mode the tool returns a sentinel telling
the agent to proceed under explicitly stated assumptions instead of hanging.
"""

from __future__ import annotations

from .base import Tool, ToolContext


def _ask_user(args: dict, ctx: ToolContext) -> str:
    question = (args.get("question") or "").strip()
    if not question:
        return "ERROR: `question` is required."
    options = args.get("options") or []

    if not ctx.cfg.interactive or ctx.on_ask is None:
        return ("[non-interactive: no operator available] Proceed with your "
                "best assumption. State the assumption explicitly in your "
                "final answer and flag what would change if it is wrong.")
    answer = ctx.on_ask(question, options)
    return f"Operator answered: {answer}" if answer.strip() else \
        "[operator gave no answer] Proceed with your best assumption and state it."


ASK_USER = Tool(
    name="ask_user",
    description=(
        "Ask the operator a clarifying question when the request is "
        "ambiguous, the target log/run is unspecified, or you need a "
        "decision before continuing. Prefer this over guessing on anything "
        "that would change the root-cause conclusion."),
    parameters={
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "The clarifying question."},
            "options": {"type": "array", "items": {"type": "string"},
                        "description": "Optional suggested answers."},
        },
        "required": ["question"],
    },
    run=_ask_user,
)
