"""delegate_subtask — spawn a focused sub-agent for a bounded investigation.

The deep-agent pattern: when the parent agent faces a complex multi-part
task, it delegates one focused part to a child agent. The child runs its
own ReAct loop with **fresh, isolated history** (no parent context bloat),
works within a **small step budget**, and returns a **summary string** to
the parent — not the full transcript. The parent gets ~200 bytes of result
in exchange for what might have been 6+ tool calls of internal work.

Why not just let the parent do everything itself? Three reasons:

  1. Context bloat. A deep dive ("look at every error in stage X, cross-
     reference each with the manual, check the referenced files") fills
     the parent's history with tool results that aren't needed for the
     final answer. Compaction helps but doesn't compose well with the
     parent's overall plan.

  2. Focus. The sub-agent gets a one-line goal and only that. It can't
     drift onto "while I'm here, let me also..." tangents.

  3. Cost predictability. Each delegation is hard-capped at max_steps
     (default 6, max 10). The parent doesn't have to babysit.

Safety:

  • Recursion is gated by `_delegation_depth` on the ToolContext. Default
    cap is MAX_DEPTH=2, so the chain is parent → child → grandchild at
    most; grandchildren cannot delegate further.
  • Children inherit the parent's profile / tools / manual_index, BUT
    NOT `on_ask` or `on_confirm` — sub-agents can't pop user prompts or
    run shell commands. Those need a human at the keyboard.
  • Children share the parent's notes file (durable knowledge is global).
  • Children don't stream tokens (would interleave confusingly with the
    parent's stream); they run silently and return the summary.
"""

from __future__ import annotations

from .base import Tool, ToolContext, truncate

MAX_DEPTH = 2          # parent=0, child=1, grandchild=2; deeper = refused
DEFAULT_MAX_STEPS = 6  # per-child step cap; small to keep cost bounded
HARD_MAX_STEPS = 10    # ceiling regardless of what the model asks for


def _delegate_subtask(args: dict, ctx: ToolContext) -> str:
    focus = (args.get("focus") or "").strip()
    if not focus:
        return ("ERROR: `focus` is required — a single concrete goal for "
                "the sub-agent (e.g. 'investigate the CTS stage errors').")
    if len(focus) > 280:
        return (f"ERROR: focus too long ({len(focus)} chars). Keep it to "
                "one sentence; the sub-agent doesn't need a manifesto.")

    # Depth gate — prevents runaway recursion.
    depth = getattr(ctx, "_delegation_depth", 0)
    if depth >= MAX_DEPTH:
        return (f"ERROR: max delegation depth ({MAX_DEPTH}) reached. "
                "Investigate this yourself rather than delegating further.")

    # Budget. Honor the caller's request but cap at HARD_MAX_STEPS so a
    # malformed call can't blow the LLM budget.
    requested = args.get("max_steps") or DEFAULT_MAX_STEPS
    try:
        max_steps = max(1, min(int(requested), HARD_MAX_STEPS))
    except (TypeError, ValueError):
        max_steps = DEFAULT_MAX_STEPS

    parent = getattr(ctx, "_agent", None)
    if parent is None:
        return ("ERROR: no parent agent in context. delegate_subtask can "
                "only be called from within an agent loop.")

    # Build the child's context: same shared resources (manual index,
    # profile, notes file via cfg.project_root) but stripped of any
    # human-interactive callbacks and with the recursion depth bumped.
    child_ctx = ToolContext(
        cfg=ctx.cfg,
        manual_index=ctx.manual_index,
        on_ask=None,
        on_confirm=None,
        profile=ctx.profile,
    )
    child_ctx._delegation_depth = depth + 1

    # Frame the goal so the child knows it's a sub-investigation and must
    # return a short summary, NOT the full Mode-C template.
    framing = (
        f"[SUB-AGENT — depth {depth + 1} of {MAX_DEPTH}]\n"
        f"Focus: {focus}\n\n"
        "You are a focused sub-investigation spawned by a parent agent. "
        "Stay strictly on the focus above — do NOT broaden scope. Your "
        "output will be embedded back into the parent's history as a "
        "tool result, so it must be TIGHT:\n"
        "  - 1-3 sentences of finding(s)\n"
        "  - cited evidence as `file:line` where applicable\n"
        "  - one recommended next step for the parent to take\n"
        "Do NOT emit the four-section Root Cause / Evidence / Fix / "
        "Suggestions template — that belongs to the parent. Do NOT "
        "include meta-commentary like \"sub-agent here\" or "
        "\"hope this helps\". Just the finding.")

    # Spawn the child agent. Local import dodges the agent <-> tools cycle.
    from ..agent import Agent          # noqa: PLC0415
    parent.trace(f"  ↪ delegate(focus={focus!r}, max_steps={max_steps}, "
                 f"depth={depth + 1})")
    child = Agent(
        client=parent.client,
        registry=parent.registry,
        ctx=child_ctx,
        max_steps=max_steps,
        trace=lambda s: parent.trace(f"    {s}"),  # nested trace indent
        on_token=None,                              # don't stream child tokens
    )
    try:
        result = child.ask(framing)
    except Exception as e:                          # noqa: BLE001
        return f"ERROR: sub-agent crashed: {type(e).__name__}: {e}"

    # Reporting summary back to the parent. We deliberately keep this
    # compact — that's the whole point of delegation.
    tool_names = sorted({h["name"] for h in result.transcript
                          if h.get("role") == "tool"})
    answer = (result.answer or "").strip() or "(no answer produced)"
    summary = (
        f"[delegate_subtask result]\n"
        f"focus: {focus}\n"
        f"steps used: {result.steps}/{max_steps}\n"
        f"tools called: {', '.join(tool_names) or '(none)'}\n"
        f"---\n{answer}")
    parent.trace(f"  ↩ delegate done ({result.steps} steps, "
                 f"{len(answer)} chars returned)")
    return truncate(summary, ctx.cfg.tool_result_char_budget)


DELEGATE_SUBTASK = Tool(
    name="delegate_subtask",
    description=(
        "Spawn a focused sub-agent for a bounded investigation. Use when "
        "a step in your plan is itself multi-tool work that would clutter "
        "your history (e.g. 'investigate everything about the CTS stage', "
        "'cross-check each error against the manual'). The sub-agent has "
        "the same tools and access to the same logs/manual/notes, but "
        "runs in fresh history and returns ONE summary back to you — not "
        "the full transcript. Max delegation depth is 2 (you can delegate, "
        "and your delegate can delegate once, but no further). Sub-agents "
        "cannot ask the user or run shell commands; for those, do the "
        "work yourself."),
    parameters={
        "type": "object",
        "properties": {
            "focus": {"type": "string",
                      "description": "One-sentence concrete goal for the "
                      "sub-agent (e.g. 'find all errors in the routing "
                      "stage and report the first one with its file/line')."},
            "max_steps": {"type": "integer",
                          "description": f"Child step budget; default "
                          f"{DEFAULT_MAX_STEPS}, cap {HARD_MAX_STEPS}."},
        },
        "required": ["focus"],
    },
    run=_delegate_subtask,
)
