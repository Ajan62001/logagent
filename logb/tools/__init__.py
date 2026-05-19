"""Tool registry assembly."""

from __future__ import annotations

from .ask import ASK_USER
from .base import Tool, ToolContext, ToolRegistry, truncate
from .files import READ_FILE
from .logs import LIST_LOGS, READ_LOGS
from .manual import SEARCH_MANUAL
from .shell import RUN_BASH
from .skills import LIST_SKILLS, RUN_SKILL
from .summary import LOG_SUMMARY

ALL_TOOLS = [
    LIST_LOGS, READ_LOGS,      # look in the logs
    LOG_SUMMARY,               # exact counts + distinct-code table
    SEARCH_MANUAL,             # refer to the manual
    LIST_SKILLS, RUN_SKILL,    # refer to skills
    READ_FILE,                 # files mentioned in logs
    RUN_BASH,                  # propose+run a shell command (approval-gated)
    ASK_USER,                  # clarifying questions
]


def build_registry() -> ToolRegistry:
    reg = ToolRegistry()
    for t in ALL_TOOLS:
        reg.register(t)
    return reg


__all__ = ["Tool", "ToolContext", "ToolRegistry", "truncate",
           "ALL_TOOLS", "build_registry"]
