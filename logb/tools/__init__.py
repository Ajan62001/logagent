"""Tool registry assembly."""

from __future__ import annotations

from .ask import ASK_USER
from .base import Tool, ToolContext, ToolRegistry, truncate
from .files import READ_FILE
from .logs import LIST_LOGS, READ_LOGS
from .manual import SEARCH_MANUAL
from .notes import DELETE_NOTE, GET_NOTE, LIST_NOTES, SAVE_NOTE
from .plan import CREATE_PLAN, SHOW_PLAN, UPDATE_PLAN
from .profile import DETECT_PROFILE
from .shell import RUN_BASH
from .skills import LIST_SKILLS, RUN_SKILL
from .summary import LOG_SUMMARY

ALL_TOOLS = [
    CREATE_PLAN, UPDATE_PLAN, SHOW_PLAN,           # plan first, then act
    LIST_LOGS, READ_LOGS,                          # look in the logs
    LOG_SUMMARY,                                   # exact counts + distinct-code table
    DETECT_PROFILE,                                # confirm which domain this log belongs to
    SEARCH_MANUAL,                                 # refer to the manual
    LIST_SKILLS, RUN_SKILL,                        # refer to skills
    READ_FILE,                                     # files mentioned in logs
    SAVE_NOTE, GET_NOTE, LIST_NOTES, DELETE_NOTE,  # durable cross-question memory
    RUN_BASH,                                      # propose+run a shell command (approval-gated)
    ASK_USER,                                      # clarifying questions
]


def build_registry() -> ToolRegistry:
    reg = ToolRegistry()
    for t in ALL_TOOLS:
        reg.register(t)
    return reg


__all__ = ["Tool", "ToolContext", "ToolRegistry", "truncate",
           "ALL_TOOLS", "build_registry"]
