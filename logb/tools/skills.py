"""Skill tools: list_skills + run_skill.

A skill is a directory under ``skills/`` containing ``SKILL.md`` with YAML
frontmatter:

    ---
    name: diagnose-missing-completion
    description: when a run ends with no completion banner
    when_to_use: crash / killed / OOM with no "Ending" line
    executable: false        # optional; if true and exec is enabled, run run.sh/run.py
    ---
    <the step-by-step procedure the agent should follow>

By default ``run_skill`` returns the procedure text for the agent to *apply*
itself (skills are diagnostic playbooks, not side-effecting). A skill that
declares ``executable: true`` runs its ``run.sh``/``run.py`` only when the
operator started logb with ``--allow-skill-exec`` (a hard-to-reverse action,
so it is opt-in).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from .base import Tool, ToolContext, truncate


def _parse_skill(md: Path) -> dict:
    text = md.read_text(errors="replace")
    meta: dict = {"body": text}
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            fm, body = text[3:end], text[end + 4:]
            for line in fm.splitlines():
                if ":" in line:
                    k, _, v = line.partition(":")
                    meta[k.strip()] = v.strip()
            meta["body"] = body.strip()
    meta.setdefault("name", md.parent.name)
    meta.setdefault("description", "(no description)")
    return meta


def _skill_dirs(cfg) -> list[Path]:
    d = Path(cfg.skills_dir)
    if not d.is_dir():
        return []
    return sorted(p for p in d.iterdir()
                  if p.is_dir() and (p / "SKILL.md").is_file())


def _list_skills(args: dict, ctx: ToolContext) -> str:
    dirs = _skill_dirs(ctx.cfg)
    if not dirs:
        return f"No skills found in {ctx.cfg.skills_dir!r}."
    out = []
    for d in dirs:
        m = _parse_skill(d / "SKILL.md")
        line = f"- {m['name']}: {m['description']}"
        if m.get("when_to_use"):
            line += f"  (use when: {m['when_to_use']})"
        out.append(line)
    return "Available skills:\n" + "\n".join(out)


def _run_skill(args: dict, ctx: ToolContext) -> str:
    name = (args.get("name") or "").strip()
    if not name:
        return "ERROR: `name` is required (call list_skills first)."
    for d in _skill_dirs(ctx.cfg):
        m = _parse_skill(d / "SKILL.md")
        if m["name"] == name or d.name == name:
            executable = str(m.get("executable", "")).lower() == "true"
            if executable and ctx.cfg.allow_skill_exec:
                script = next((d / s for s in ("run.sh", "run.py")
                               if (d / s).is_file()), None)
                if script:
                    cmd = ([sys.executable, str(script)] if script.suffix == ".py"
                           else ["bash", str(script)])
                    cmd += [str(a) for a in (args.get("args") or [])]
                    try:
                        r = subprocess.run(cmd, capture_output=True, text=True,
                                            timeout=120, cwd=d)
                        out = (f"[ran {script.name}, exit {r.returncode}]\n"
                               f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}")
                        return truncate(out, ctx.cfg.tool_result_char_budget)
                    except subprocess.TimeoutExpired:
                        return f"ERROR: skill {name!r} timed out (120s)."
            note = ""
            if executable and not ctx.cfg.allow_skill_exec:
                note = ("\n\n[note: this skill is executable but exec is "
                        "disabled — follow the procedure manually]")
            return (f"# Skill: {m['name']}\n{m['description']}\n\n"
                    f"{m['body']}{note}")
    return f"ERROR: no skill named {name!r}. Call list_skills."


LIST_SKILLS = Tool(
    name="list_skills",
    description="List available diagnostic skills (playbooks) with when-to-use hints.",
    parameters={"type": "object", "properties": {}},
    run=_list_skills,
)

RUN_SKILL = Tool(
    name="run_skill",
    description=(
        "Load a skill: returns its step-by-step procedure for you to apply "
        "(or runs its script if the skill is executable and exec is enabled). "
        "Call list_skills first to discover names."),
    parameters={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Skill name."},
            "args": {"type": "array", "items": {"type": "string"},
                     "description": "Args for an executable skill (optional)."},
        },
        "required": ["name"],
    },
    run=_run_skill,
)
