"""create_plan / update_plan / show_plan — explicit task planning.

Adds a planning layer on top of the ReAct loop. Instead of the model
deciding tools one step at a time and easily losing the thread, it commits
to an ordered list of tasks up front via `create_plan`, then marks each
done (or skipped, with a reason) as it goes. The current plan is rendered
into the system prompt on every step (see `agent._session_context`), so
the model always sees what's pending vs. what's already completed —
including across history compaction, which would otherwise erase that
state.

Plan state lives on the `ToolContext` (in-memory) and is **reset at the
start of each `ask()`**. It is intentionally NOT persistent across chat
turns — a new user question gets a fresh strategy. Cross-question state
that should survive belongs in `save_note`, which is durable.

Why a tool, not just a prompt convention? The tool result is auditable and
the schema is enforced. A free-text plan in the model's reasoning is easy
to skip; a tool call leaves a trace the verifier (and the user, via `-v`)
can inspect.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .base import Tool, ToolContext, truncate

MAX_TASKS = 20                 # cap to keep the plan readable in-prompt
MAX_TEXT_LEN = 160             # per-task description
MAX_RESULT_LEN = 240           # per-task result summary
STATUSES = ("pending", "in_progress", "done", "skipped")


@dataclass
class Task:
    idx: int
    text: str
    status: str = "pending"
    result: str = ""


@dataclass
class PlanState:
    tasks: list = field(default_factory=list)

    def reset(self) -> None:
        self.tasks.clear()

    def render(self) -> str:
        """Compact human-readable rendering for the system prompt."""
        if not self.tasks:
            return "(no plan set)"
        rows = []
        for t in self.tasks:
            mark = {"pending": "[ ]", "in_progress": "[~]",
                    "done": "[✓]", "skipped": "[—]"}.get(t.status, "[?]")
            line = f"  {mark} {t.idx}. {t.text}"
            if t.result:
                line += f"\n        → {t.result}"
            rows.append(line)
        return "\n".join(rows)

    def summary_counts(self) -> dict:
        out = {s: 0 for s in STATUSES}
        for t in self.tasks:
            out[t.status] = out.get(t.status, 0) + 1
        return out


def _get_plan(ctx: ToolContext) -> PlanState:
    """Plan state lazily attached to the ToolContext."""
    plan = getattr(ctx, "plan", None)
    if plan is None:
        plan = PlanState()
        ctx.plan = plan
    return plan


def _create_plan(args: dict, ctx: ToolContext) -> str:
    tasks = args.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        return "ERROR: `tasks` must be a non-empty list of task descriptions."
    if len(tasks) > MAX_TASKS:
        return (f"ERROR: too many tasks ({len(tasks)} > {MAX_TASKS}). "
                "Keep the plan tight — 3-8 tasks is usually right; split a "
                "big task into a follow-up plan after the first half lands.")
    cleaned: list[Task] = []
    for i, raw in enumerate(tasks, 1):
        text = str(raw).strip()
        if not text:
            return f"ERROR: task #{i} is empty."
        cleaned.append(Task(idx=i, text=text[:MAX_TEXT_LEN]))
    plan = _get_plan(ctx)
    plan.tasks = cleaned
    return (f"Plan set with {len(cleaned)} task(s):\n{plan.render()}\n\n"
            "Now execute the first task, then call update_plan with its "
            "outcome before moving on.")


def _update_plan(args: dict, ctx: ToolContext) -> str:
    plan = _get_plan(ctx)
    if not plan.tasks:
        return ("ERROR: no plan exists. Call create_plan(tasks=[...]) first.")

    # Mode 1: append new tasks discovered mid-execution.
    add = args.get("add_tasks")
    if isinstance(add, list) and add:
        if len(plan.tasks) + len(add) > MAX_TASKS:
            return (f"ERROR: adding {len(add)} would exceed the {MAX_TASKS}-"
                    f"task cap (currently {len(plan.tasks)}).")
        start = max(t.idx for t in plan.tasks) + 1
        for i, raw in enumerate(add):
            text = str(raw).strip()
            if text:
                plan.tasks.append(Task(idx=start + i,
                                       text=text[:MAX_TEXT_LEN]))
        return f"Added {len(add)} task(s).\n{plan.render()}"

    # Mode 2: update one existing task's status / result.
    idx = args.get("idx")
    if idx is None:
        return ("ERROR: provide either `idx` (to update a task) or "
                "`add_tasks` (to append new ones).")
    try:
        idx = int(idx)
    except (TypeError, ValueError):
        return f"ERROR: idx must be an integer, got {idx!r}."
    task = next((t for t in plan.tasks if t.idx == idx), None)
    if task is None:
        return (f"ERROR: no task with idx {idx}. Existing: "
                f"{[t.idx for t in plan.tasks]}")

    status = args.get("status")
    if status:
        if status not in STATUSES:
            return f"ERROR: status must be one of {STATUSES}, got {status!r}."
        task.status = status
    result = args.get("result")
    if result:
        task.result = str(result).strip()[:MAX_RESULT_LEN]
    counts = plan.summary_counts()
    return (f"Task {idx} updated: status={task.status}"
            + (f", result recorded ({len(task.result)} chars)"
               if result else "")
            + f"\nPlan now: {counts['done']} done, {counts['pending']} "
            f"pending, {counts['in_progress']} in progress, "
            f"{counts['skipped']} skipped.")


def _show_plan(_args: dict, ctx: ToolContext) -> str:
    plan = _get_plan(ctx)
    if not plan.tasks:
        return ("(no plan set — call create_plan(tasks=[...]) before "
                "any non-trivial investigation.)")
    counts = plan.summary_counts()
    header = (f"Current plan ({counts['done']} done / {len(plan.tasks)} total"
              + (f", {counts['pending']} pending" if counts['pending']
                 else "")
              + "):")
    return truncate(f"{header}\n{plan.render()}",
                    ctx.cfg.tool_result_char_budget)


CREATE_PLAN = Tool(
    name="create_plan",
    description=(
        "Lay out an ordered list of investigation tasks BEFORE you start "
        "calling read_logs / search_manual / read_file. Each task is one "
        "concrete action (e.g. 'log_summary to get exact counts', "
        "'search_manual for IMPSDC-3071', 'read_file top.sdc around line "
        "88'). The plan is visible in your system prompt on every step, "
        "so it persists through context compaction. Required at the start "
        "of any root-cause / debug question (Mode C); optional for simple "
        "count or informational questions. Calling create_plan again "
        "REPLACES the existing plan — only do that on a real strategy "
        "pivot, not after every step (use update_plan for normal progress)."),
    parameters={
        "type": "object",
        "properties": {
            "tasks": {"type": "array", "items": {"type": "string"},
                      "description": "Ordered task descriptions, "
                      f"3-8 typical, {MAX_TASKS} max."},
        },
        "required": ["tasks"],
    },
    run=_create_plan,
)

UPDATE_PLAN = Tool(
    name="update_plan",
    description=(
        "Update the plan after a tool call. Two modes: (a) `idx=N, "
        "status='done'|'in_progress'|'skipped', result='<one-line "
        "summary>'` marks one task; (b) `add_tasks=['task A', 'task B']` "
        "appends new tasks discovered mid-investigation. Mark tasks "
        "'skipped' (not 'done') when a task turned out to be unnecessary, "
        "and put the reason in `result` so the user can audit your "
        "decisions."),
    parameters={
        "type": "object",
        "properties": {
            "idx": {"type": "integer",
                    "description": "Task number to update (1-indexed)."},
            "status": {"type": "string",
                       "enum": list(STATUSES),
                       "description": "New status for that task."},
            "result": {"type": "string",
                       "description": "One-line summary of what the task "
                       f"produced (max {MAX_RESULT_LEN} chars)."},
            "add_tasks": {"type": "array", "items": {"type": "string"},
                          "description": "Append these new tasks to the "
                          "plan instead of updating an existing one."},
        },
    },
    run=_update_plan,
)

SHOW_PLAN = Tool(
    name="show_plan",
    description=(
        "Print the current plan with each task's status (✓ done, ~ in "
        "progress, [ ] pending, — skipped) and any recorded result. The "
        "plan is also embedded in your system prompt on every turn, so "
        "you usually don't need this — call it only if you're unsure "
        "what state things are in (e.g. after a long compaction)."),
    parameters={"type": "object", "properties": {}},
    run=_show_plan,
)
