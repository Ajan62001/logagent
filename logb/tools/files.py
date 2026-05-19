"""read_file — open a file referenced inside a log line.

EDA logs constantly print paths (the .tcl that errored, a constraints file, a
generated report, a stack-trace source). Following them is the whole point, so
absolute paths are allowed by default. Two guards always apply:

  * SENSITIVE_PATTERNS (creds/keys/secrets) — refused unconditionally.
  * cfg.restrict_to_roots — when True, reads cannot escape allowed roots.

Output is 1-indexed line-numbered, byte-capped.
"""

from __future__ import annotations

from pathlib import Path

from ..config import is_sensitive
from .base import Tool, ToolContext, truncate


def _read_file(args: dict, ctx: ToolContext) -> str:
    raw = (args.get("path") or "").strip()
    if not raw:
        return "ERROR: `path` is required."
    p = Path(raw)
    if not p.is_absolute():
        p = (Path(ctx.cfg.project_root) / p)
    try:
        p = p.resolve()
    except OSError as e:
        return f"ERROR: bad path {raw!r}: {e}"

    if is_sensitive(p):
        return (f"REFUSED: {p} matches a sensitive-file pattern "
                f"(credentials/keys). Not opened.")
    if ctx.cfg.restrict_to_roots:
        roots = ctx.cfg.allowed_roots()
        if not any(str(p).startswith(str(r)) for r in roots):
            return (f"REFUSED: {p} is outside allowed roots and "
                    f"restrict_to_roots is on.")
    if not p.is_file():
        return f"ERROR: not a file: {p}"

    try:
        lines = p.read_text(errors="replace").splitlines()
    except OSError as e:
        return f"ERROR reading {p}: {e}"

    start = max(1, int(args.get("start", 1)))
    end = int(args.get("end", start + 199))
    sel = lines[start - 1:end]
    numbered = "\n".join(f"{start + i:>7}: {ln}" for i, ln in enumerate(sel))
    header = f"# {p}  ({len(lines)} lines, showing {start}-{min(end, len(lines))})\n"
    return truncate(header + (numbered or "(empty range)"),
                    ctx.cfg.tool_result_char_budget)


READ_FILE = Tool(
    name="read_file",
    description=(
        "Open a file referenced in a log line (the failing .tcl/.sdc, a "
        "report, a stack-trace source, a config). Absolute paths printed in "
        "logs are allowed. Returns 1-indexed line-numbered content; use "
        "`start`/`end` to window large files. Credential/key files are refused."),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path (absolute or project-relative)."},
            "start": {"type": "integer", "description": "First line (1-indexed)."},
            "end": {"type": "integer", "description": "Last line."},
        },
        "required": ["path"],
    },
    run=_read_file,
)
