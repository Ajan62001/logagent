"""Skill tools: list_skills + run_skill.

A skill is a directory containing ``SKILL.md`` with YAML frontmatter:

    ---
    name: diagnose-missing-completion
    description: when a run ends with no completion banner
    when_to_use: crash / killed / OOM with no "Ending" line
    domain: eda                   # optional: eda | generic | any (default any)
    executable: false             # optional; gated by --allow-skill-exec
    ---
    <the step-by-step procedure the agent should follow>

Discovery is recursive (``rglob('SKILL.md')``), so skills can be grouped:
``skills/k8s/oomkill/SKILL.md``, ``skills/eda/missing-completion/SKILL.md``.
The catalog is filtered by the active domain profile — a skill tagged
``domain: eda`` is hidden in generic mode, and vice versa. Skills with no
``domain:`` (or ``domain: any``) are universal.

By default ``run_skill`` returns the procedure text for the agent to *apply*
itself (skills are diagnostic playbooks, not side-effecting). A skill that
declares ``executable: true`` runs its ``run.sh``/``run.py`` only when the
operator started logb with ``--allow-skill-exec`` (a hard-to-reverse action,
so it is opt-in).
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from .base import Tool, ToolContext, truncate

_WORD = re.compile(r"[A-Za-z0-9_]+")


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
    meta.setdefault("domain", "any")
    return meta


def _skill_dirs(cfg) -> list[Path]:
    """All directories with a SKILL.md, searched recursively."""
    d = Path(cfg.skills_dir)
    if not d.is_dir():
        return []
    return sorted({p.parent for p in d.rglob("SKILL.md") if p.is_file()})


def _domain_match(skill_domain: str, profile_name: str) -> bool:
    """A skill applies if its declared domain is the active profile, 'any',
    or empty. Case-insensitive."""
    d = (skill_domain or "any").strip().lower()
    return d in ("", "any", profile_name.lower())


def _rank(query: str, items: list[tuple[Path, dict]]) -> list[tuple[Path, dict]]:
    """Lightweight ranking for `list_skills(query=...)`. Scores skills by how
    many query tokens appear in name+description+when_to_use, weighted by
    inverse document frequency so rare tokens dominate. No external dep; the
    catalog is small enough that this is more than enough to surface the
    right 3-5 playbooks from a 50-item catalog."""
    q = [t.lower() for t in _WORD.findall(query) if len(t) > 1]
    if not q:
        return items
    import math
    haystacks = []
    for _, m in items:
        hay = " ".join(filter(None, (m.get("name"), m.get("description"),
                                       m.get("when_to_use", ""))))
        haystacks.append([t.lower() for t in _WORD.findall(hay)])
    N = len(items) or 1
    df = {}
    for toks in haystacks:
        for t in set(toks):
            df[t] = df.get(t, 0) + 1
    scored = []
    for (path, meta), toks in zip(items, haystacks):
        tf = {}
        for t in toks:
            tf[t] = tf.get(t, 0) + 1
        s = 0.0
        for t in q:
            if t in tf:
                s += tf[t] * math.log(1 + (N - df.get(t, 0) + 0.5)
                                       / (df.get(t, 0) + 0.5))
        scored.append((s, path, meta))
    scored.sort(key=lambda x: x[0], reverse=True)
    # Keep only positive scores when the user provided a query — listing
    # zero-match skills back to a targeted query just adds noise.
    return [(p, m) for s, p, m in scored if s > 0] or items


def _list_skills(args: dict, ctx: ToolContext) -> str:
    dirs = _skill_dirs(ctx.cfg)
    if not dirs:
        return f"No skills found in {ctx.cfg.skills_dir!r}."
    profile_name = getattr(ctx.profile, "name", "eda")
    pairs: list[tuple[Path, dict]] = []
    hidden = 0
    for d in dirs:
        m = _parse_skill(d / "SKILL.md")
        if _domain_match(m.get("domain", "any"), profile_name):
            pairs.append((d, m))
        else:
            hidden += 1

    query = (args.get("query") or "").strip()
    if query:
        pairs = _rank(query, pairs)
        top = int(args.get("k", 8))
        pairs = pairs[:top]

    if not pairs:
        return (f"No skills match profile {profile_name!r}"
                + (f" / query {query!r}" if query else "")
                + (f" ({hidden} skill(s) hidden for other domains)."
                   if hidden else "."))
    out = []
    for _, m in pairs:
        line = f"- {m['name']}: {m['description']}"
        if m.get("when_to_use"):
            line += f"  (use when: {m['when_to_use']})"
        if m.get("domain", "any") not in ("", "any"):
            line += f"  [domain: {m['domain']}]"
        out.append(line)
    hidden_note = (f"\n[{hidden} skill(s) hidden — declared for other domains.]"
                   if hidden else "")
    header = (f"Available skills (profile={profile_name}"
              + (f", query={query!r}" if query else "") + "):")
    return f"{header}\n" + "\n".join(out) + hidden_note


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
    description=(
        "List diagnostic skills (playbooks) with when-to-use hints. Skills "
        "tagged for another domain are hidden automatically. Pass `query` to "
        "rank the catalog by relevance to the user's problem (useful when "
        "the catalog is large); omit it to see everything for this profile."),
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string",
                      "description": "Optional: rank by relevance to this "
                      "phrase (e.g. the user's failure description)."},
            "k": {"type": "integer",
                  "description": "When query is set, return the top K hits "
                  "(default 8)."},
        },
    },
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
