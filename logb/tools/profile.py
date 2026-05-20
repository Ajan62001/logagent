"""detect_profile — sniff the target log to identify its domain.

A lightweight tool the agent can call once at session start (especially in
'auto' mode) to confirm which Profile is active and which severity tokens
actually appear in this log. Useful when the user pointed us at an
unfamiliar log family and we don't know whether to read it as EDA or
generic. Read-only; no side effects.
"""

from __future__ import annotations

from pathlib import Path

from ..profiles import EDA, GENERIC, PROFILES, detect
from .base import Tool, ToolContext, truncate
from .logs import _pick_file


def _detect_profile(args: dict, ctx: ToolContext) -> str:
    f = _pick_file(args, ctx.cfg, ctx.profile)
    if f is None or not f.is_file():
        return (f"Log not found: {args.get('path')!r}. "
                f"Call list_logs to see available files.")
    try:
        with open(f, "rb") as fh:
            head = fh.read(8192)
    except OSError as e:
        return f"ERROR reading {f}: {e}"

    detected = detect(head)
    out = [f"# detect_profile: {f}",
           f"Active profile:   {ctx.profile.name}",
           f"Detected profile: {detected.name}"]
    if detected.name != ctx.profile.name:
        out.append("⚠ The detected profile differs from the active one. "
                   "Relaunch with --mode <name> to switch — the index/severity "
                   "regexes will not match this log family correctly otherwise.")

    # Show which severity tokens from the active profile actually fire in the
    # head sample, so the model knows the vocabulary present in *this* file.
    hits = []
    for sev, rx in ctx.profile.severity_bytes.items():
        n = len(rx.findall(head))
        if n:
            hits.append(f"{sev}={n}")
    out.append("Severity hits in first 8 KB (active profile): "
               + (", ".join(hits) if hits else "none"))

    # And what the detected profile says, if different — helps justify a switch.
    if detected.name != ctx.profile.name:
        alt = []
        for sev, rx in detected.severity_bytes.items():
            n = len(rx.findall(head))
            if n:
                alt.append(f"{sev}={n}")
        out.append("Severity hits in first 8 KB (detected profile): "
                   + (", ".join(alt) if alt else "none"))

    out.append(f"Profiles available: {', '.join(sorted(PROFILES))}")
    return truncate("\n".join(out), ctx.cfg.tool_result_char_budget)


DETECT_PROFILE = Tool(
    name="detect_profile",
    description=(
        "Sniff a log file's first 8 KB to identify its domain (EDA "
        "vs generic app/system log) and report which severity tokens actually "
        "appear. Use when the user pointed you at an unfamiliar log and you "
        "want to verify the active profile matches it before drawing "
        "conclusions."),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string",
                     "description": "Log file path or name (omit for the "
                     "only/first log)."},
        },
    },
    run=_detect_profile,
)
