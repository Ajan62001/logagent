"""save_note / get_note / list_notes / delete_note — durable agent memory.

The conversation history in `Agent.history` is volatile: it lives in-process
and silently gets truncated from the front once Ollama's `num_ctx` is full.
That's fine for short tasks but useless for "remember what you found last
question" — the answer would just be hallucinated from a half-evicted
transcript.

These tools give the agent an explicit, persistent memory layer:

  • Storage:  `<project_root>/.logb-notes.json`  (gitignore'd)
  • Lifetime: survives context truncation AND process restart
  • Discipline: the model saves its *synthesis* (root cause, key file:line,
    a hypothesis to revisit) — NOT raw tool output (which would just
    duplicate what `read_logs` can re-fetch on demand)

A note has a string key (caller-chosen) and a string value, with caps to
prevent the model from accidentally dumping a multi-MB log into the notes
file (which would defeat the purpose).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .base import Tool, ToolContext

NOTES_FILE = ".logb-notes.json"
MAX_KEY_LEN = 80
MAX_VALUE_LEN = 8192        # ~2 KB of guidance is plenty; more is hoarding
MAX_KEYS = 100              # hard cap so a runaway loop can't bloat the file
_KEY_RX = re.compile(r"^[A-Za-z0-9_.\-/]{1,80}$")


def _notes_path(cfg) -> Path:
    return Path(cfg.project_root) / NOTES_FILE


def _load(cfg) -> dict:
    p = _notes_path(cfg)
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        # A corrupt notes file shouldn't crash the agent loop — start fresh
        # in memory; next _save() will overwrite the bad file.
        return {}


def _save(cfg, notes: dict) -> str | None:
    p = _notes_path(cfg)
    try:
        p.write_text(json.dumps(notes, indent=2, sort_keys=True))
        return None
    except OSError as e:
        return f"ERROR: could not write {p}: {e}"


def _save_note(args: dict, ctx: ToolContext) -> str:
    key = (args.get("key") or "").strip()
    value = args.get("value")
    if not key:
        return "ERROR: `key` is required."
    if not _KEY_RX.match(key):
        return (f"ERROR: invalid key {key!r}. Use up to {MAX_KEY_LEN} chars: "
                "letters, digits, _ . - / only.")
    if not isinstance(value, str):
        return "ERROR: `value` must be a string."
    if len(value) > MAX_VALUE_LEN:
        return (f"ERROR: value too long ({len(value)} > {MAX_VALUE_LEN} chars). "
                "Save your synthesis, not raw tool output — re-fetch raw data "
                "with read_logs/search_manual when you need it again.")
    notes = _load(ctx.cfg)
    if key not in notes and len(notes) >= MAX_KEYS:
        return (f"ERROR: notes capped at {MAX_KEYS} keys. Call delete_note to "
                "remove stale entries first (list_notes shows what's saved).")
    notes[key] = value
    err = _save(ctx.cfg, notes)
    return err or f"Saved note {key!r} ({len(value)} chars)."


def _get_note(args: dict, ctx: ToolContext) -> str:
    key = (args.get("key") or "").strip()
    if not key:
        return "ERROR: `key` is required. Call list_notes to see what's saved."
    notes = _load(ctx.cfg)
    if key not in notes:
        avail = ", ".join(sorted(notes)) or "(none)"
        return f"Note {key!r} not found. Available keys: {avail}"
    return f"# note: {key}\n{notes[key]}"


def _list_notes(_args: dict, ctx: ToolContext) -> str:
    notes = _load(ctx.cfg)
    if not notes:
        return ("(no notes saved yet. Use save_note to record durable "
                "findings — root cause, key file:line, hypotheses.)")
    rows = []
    for k in sorted(notes):
        v = notes[k]
        first_line = v.split("\n", 1)[0][:80]
        more = "..." if (len(v) > 80 or "\n" in v) else ""
        rows.append(f"- {k}  ({len(v)} chars): {first_line}{more}")
    return f"# saved notes ({len(notes)}):\n" + "\n".join(rows)


def _delete_note(args: dict, ctx: ToolContext) -> str:
    key = (args.get("key") or "").strip()
    if not key:
        return "ERROR: `key` is required."
    notes = _load(ctx.cfg)
    if key not in notes:
        return f"Note {key!r} not found; nothing deleted."
    del notes[key]
    err = _save(ctx.cfg, notes)
    return err or f"Deleted note {key!r}."


SAVE_NOTE = Tool(
    name="save_note",
    description=(
        "Save a durable note that survives context-window truncation and "
        "process restart. Use for SYNTHESIZED findings the user (or you) "
        "may need later in this chat or a future session: a root-cause "
        "sentence, a key file:line, exact error counts, an open hypothesis. "
        "Do NOT save raw tool output — that just duplicates what read_logs / "
        "search_manual can re-fetch. Overwrites if the key already exists."),
    parameters={
        "type": "object",
        "properties": {
            "key": {"type": "string",
                    "description": "Short identifier (e.g. 'root_cause', "
                    "'first_error_line', 'open_question'). Up to 80 chars; "
                    "letters, digits, _ . - / only."},
            "value": {"type": "string",
                      "description": f"What to remember (up to {MAX_VALUE_LEN} "
                      "chars). Keep it terse — one fact per note."},
        },
        "required": ["key", "value"],
    },
    run=_save_note,
)

GET_NOTE = Tool(
    name="get_note",
    description=(
        "Retrieve a previously saved note by key. Call list_notes first if "
        "you're not sure what's stored from earlier turns or sessions."),
    parameters={
        "type": "object",
        "properties": {
            "key": {"type": "string", "description": "The note's key."},
        },
        "required": ["key"],
    },
    run=_get_note,
)

LIST_NOTES = Tool(
    name="list_notes",
    description=(
        "List every saved note (key + first-line preview). Useful at the "
        "start of a follow-up question to see what's already known without "
        "re-deriving it."),
    parameters={"type": "object", "properties": {}},
    run=_list_notes,
)

DELETE_NOTE = Tool(
    name="delete_note",
    description=(
        "Remove a saved note by key. Use when a stored finding is stale or "
        "has been superseded."),
    parameters={
        "type": "object",
        "properties": {
            "key": {"type": "string", "description": "The note's key."},
        },
        "required": ["key"],
    },
    run=_delete_note,
)
