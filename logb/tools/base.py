"""Tool contract + registry.

A Tool is a name, a JSON-schema parameter spec (sent verbatim to the LLM as
the function signature), and a ``run(args, ctx) -> str`` callable. ``ctx``
carries shared state (config, manual index) so tools stay stateless.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass
class ToolContext:
    cfg: object                 # logb.config.Config
    manual_index: object        # logb.rag.ManualIndex
    on_ask: Callable[[str, list], str] | None = None     # ask_user front-end
    on_confirm: Callable[[str, str], bool] | None = None  # run_bash approval
    profile: object = None      # logb.profiles.Profile (resolved at session start)
    # Deep-agent state. `_agent` is a back-reference the running Agent
    # writes into the context so delegate_subtask can spawn child agents
    # without an explicit dependency injection through tool dispatch.
    # `_delegation_depth` caps recursion (parent=0, child=1, ...).
    _agent: object = None
    _delegation_depth: int = 0
    # `plan` is attached lazily by the plan tools (see tools/plan.py).
    plan: object = None

    def __post_init__(self) -> None:
        # Default to the EDA profile when the caller didn't supply one — keeps
        # older test helpers and direct ToolContext(...) constructions working
        # while every CLI/Agent path now resolves the profile explicitly.
        if self.profile is None:
            from ..profiles import resolve   # local import avoids a cycle
            mode = getattr(self.cfg, "mode", "eda")
            self.profile = resolve(mode)


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict            # JSON schema (object)
    run: Callable[[dict, ToolContext], str]
    # When set, schemas() filters this tool out unless the active profile
    # matches. Domain-specific tools (e.g. EDA stage_timeline) shouldn't
    # pollute the schema list in a generic-mode session — the model would
    # waste a step calling something irrelevant.
    profile_required: str | None = None

    def schema(self) -> dict:
        return {"name": self.name, "description": self.description,
                "parameters": self.parameters}


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def schemas(self, profile_name: str | None = None) -> list[dict]:
        out = []
        for t in self._tools.values():
            if (t.profile_required is not None
                    and profile_name is not None
                    and t.profile_required != profile_name):
                continue
            out.append(t.schema())
        return out

    def dispatch(self, name: str, args: dict, ctx: ToolContext) -> str:
        tool = self._tools.get(name)
        if tool is None:
            return (f"ERROR: unknown tool {name!r}. "
                    f"Available: {', '.join(self._tools)}")
        # Tool-level profile gate. Even if a tool was somehow invoked in a
        # session where its required profile isn't active, refuse loudly
        # rather than running domain-specific logic on the wrong log type.
        if (tool.profile_required is not None
                and getattr(ctx.profile, "name", None) != tool.profile_required):
            return (f"ERROR: tool {name!r} requires the "
                    f"{tool.profile_required!r} profile (active: "
                    f"{getattr(ctx.profile, 'name', 'unknown')!r}). "
                    "Re-launch with --mode "
                    f"{tool.profile_required} if this log is that domain.")
        try:
            return tool.run(args or {}, ctx)
        except Exception as e:  # never let a tool crash the agent loop
            return f"ERROR running {name}: {type(e).__name__}: {e}"


def truncate(text: str, budget: int) -> str:
    """Clip a fat tool result, keeping head+tail and noting the elision."""
    if len(text) <= budget:
        return text
    head = text[: int(budget * 0.7)]
    tail = text[-int(budget * 0.25):]
    cut = len(text) - len(head) - len(tail)
    return f"{head}\n\n... [truncated {cut} chars] ...\n\n{tail}"
