"""Session persistence + structured audit trail.

Two related concerns:

  1. Session resume — `Agent.history` lives in-process. For a long-running
     debugging session that survives process restarts ("come back tomorrow
     and keep going"), the history (plus plan, plus per-turn call counts)
     gets serialized to `<project_root>/.logb-sessions/<id>.json` and can
     be reloaded with `--resume <id>`. Notes are already durable via
     `.logb-notes.json`, so they don't need to be saved here.

  2. Audit log — for production / regulated environments, every final
     answer is paired with a structured JSONL record of which tool calls
     produced it. `<project_root>/.logb-audit.jsonl` (append-only) makes
     it possible to retrospectively answer "what evidence did the agent
     have when it concluded X?"

Both are intentionally simple JSON files written to the project root —
not a database, not a service. The volume is bounded by max_steps × the
size of tool results × number of asks, and a hand-curl is the right
debugging tool for them.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

SESSION_DIR = ".logb-sessions"
AUDIT_FILE = ".logb-audit.jsonl"
SCHEMA_VERSION = 1


def _now_iso() -> str:
    # Timezone-aware ISO-8601, sortable as a string.
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def _session_dir(project_root: str | os.PathLike) -> Path:
    d = Path(project_root) / SESSION_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def new_session_id() -> str:
    """Short collision-resistant id like '2026-05-20-a3f9b1'."""
    today = _dt.date.today().isoformat()
    return f"{today}-{secrets.token_hex(3)}"


def session_path(project_root: str | os.PathLike, sid: str) -> Path:
    return _session_dir(project_root) / f"{sid}.json"


def list_sessions(project_root: str | os.PathLike) -> list[dict]:
    """Return a manifest of saved sessions, newest first. Each entry:
    {id, path, created, updated, turns, last_question}."""
    d = _session_dir(project_root)
    out: list[dict] = []
    for f in d.glob("*.json"):
        try:
            data = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        out.append({
            "id": data.get("id", f.stem),
            "path": str(f),
            "created": data.get("created", ""),
            "updated": data.get("updated", ""),
            "turns": sum(1 for h in data.get("history", [])
                          if h.get("role") == "user"),
            "last_question": next(
                (h.get("text", "")[:60]
                 for h in reversed(data.get("history", []))
                 if h.get("role") == "user"), ""),
        })
    out.sort(key=lambda x: x.get("updated", ""), reverse=True)
    return out


@dataclass
class SessionState:
    id: str
    history: list = field(default_factory=list)
    plan_tasks: list = field(default_factory=list)
    created: str = field(default_factory=_now_iso)
    updated: str = field(default_factory=_now_iso)

    def to_json(self) -> dict:
        return {
            "schema": SCHEMA_VERSION,
            "id": self.id,
            "created": self.created,
            "updated": _now_iso(),
            "history": self.history,
            "plan_tasks": self.plan_tasks,
        }

    @classmethod
    def from_json(cls, data: dict) -> "SessionState":
        if data.get("schema") != SCHEMA_VERSION:
            raise ValueError(
                f"session schema mismatch: file v{data.get('schema')}, "
                f"runtime v{SCHEMA_VERSION}")
        return cls(
            id=data["id"],
            history=list(data.get("history", [])),
            plan_tasks=list(data.get("plan_tasks", [])),
            created=data.get("created", _now_iso()),
            updated=data.get("updated", _now_iso()),
        )


def save_session(project_root: str | os.PathLike, agent) -> str:
    """Atomically write the agent's current state to disk. Returns the
    session id used (creates one if the agent doesn't have one yet)."""
    sid = getattr(agent, "session_id", None) or new_session_id()
    agent.session_id = sid
    plan = getattr(agent.ctx, "plan", None)
    plan_tasks = []
    if plan is not None and plan.tasks:
        plan_tasks = [{"idx": t.idx, "text": t.text,
                       "status": t.status, "result": t.result}
                      for t in plan.tasks]
    state = SessionState(id=sid, history=list(agent.history),
                          plan_tasks=plan_tasks)
    path = session_path(agent.ctx.cfg.project_root, sid)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state.to_json(), indent=2))
    tmp.replace(path)
    return sid


def load_session(project_root: str | os.PathLike, sid: str) -> SessionState:
    """Load a saved session. Raises FileNotFoundError if the id is unknown
    or ValueError on schema mismatch."""
    path = session_path(project_root, sid)
    if not path.is_file():
        raise FileNotFoundError(f"session not found: {sid} (expected {path})")
    return SessionState.from_json(json.loads(path.read_text()))


def apply_session(agent, state: SessionState) -> None:
    """Restore a SessionState onto a freshly-built Agent."""
    agent.session_id = state.id
    agent.history = list(state.history)
    if state.plan_tasks:
        # Lazy-attach the PlanState if the context doesn't have one yet —
        # the plan tools attach it on first call, but we may be loading
        # before any tool has run.
        from .tools.plan import PlanState, Task
        plan = getattr(agent.ctx, "plan", None)
        if plan is None:
            plan = PlanState()
            agent.ctx.plan = plan
        plan.tasks = [Task(idx=t["idx"], text=t["text"],
                            status=t["status"], result=t["result"])
                      for t in state.plan_tasks]


# --------------------------------------------------------------------------- #
#  Audit trail. One JSONL record per completed `ask()` — append-only.         #
# --------------------------------------------------------------------------- #
def audit_path(project_root: str | os.PathLike) -> Path:
    return Path(project_root) / AUDIT_FILE


def write_audit(project_root: str | os.PathLike, *,
                 session_id: str | None, question: str, answer: str,
                 steps: int, transcript: list,
                 telemetry: dict | None = None,
                 verification_passes: int = 1) -> None:
    """Append one structured record summarizing this turn. Tool results
    are referenced by name + args + result-length (not full text — the
    audit file would otherwise dwarf the actual logs)."""
    tool_calls = []
    for h in transcript:
        if h.get("role") == "tool":
            tool_calls.append({
                "name": h.get("name"),
                "result_len": len(h.get("result") or ""),
            })
        elif h.get("role") == "assistant" and h.get("tool_calls"):
            for tc in h["tool_calls"]:
                # Args inline — they're usually small. If a model passes a
                # multi-KB arg we still log it; the audit file is a debug
                # surface and the user can rotate it.
                tool_calls.append({
                    "name": tc.get("name"),
                    "args": tc.get("args", {}),
                })
    record = {
        "ts": _now_iso(),
        "session": session_id,
        "question": question[:500],
        "answer": answer[:2000],
        "steps": steps,
        "verification_passes": (telemetry or {}).get(
            "verification_passes", verification_passes),
        "tool_calls": tool_calls,
    }
    if telemetry:
        record["telemetry"] = {
            "tokens_in": telemetry.get("tokens_in", 0),
            "tokens_out": telemetry.get("tokens_out", 0),
            "latency_ms": telemetry.get("latency_ms", 0),
            "llm_calls": telemetry.get("llm_calls", 0),
        }
    try:
        with open(audit_path(project_root), "a") as f:
            f.write(json.dumps(record) + "\n")
    except OSError:
        # Audit logging is best-effort; never crash the agent for it.
        pass


def read_audit_tail(project_root: str | os.PathLike, n: int = 20) -> list[dict]:
    """Last N audit records (newest last). For inspection / debugging."""
    p = audit_path(project_root)
    if not p.is_file():
        return []
    try:
        lines = p.read_text().splitlines()
    except OSError:
        return []
    out = []
    for line in lines[-n:]:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out
