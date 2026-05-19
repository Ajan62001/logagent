"""run_bash — let the agent propose a small shell command, gated by approval.

Defense in depth (every layer must pass):
  1. cfg.allow_shell must be True   (operator opt-in: --allow-shell)
  2. the command must not match HARD_DENY  (catastrophic/system — refused
     BEFORE the operator is asked; approval cannot override these)
  3. an interactive operator must explicitly approve THIS command
  4. non-interactive / no operator  => denied (never auto-runs)

Output (stdout+stderr+exit) is captured, truncated, and returned to the agent.
Intended for read-only diagnostics (grep/awk/find/head/wc over logs &
referenced files). The dedicated read_logs/read_file tools are still preferred
when they suffice.
"""

from __future__ import annotations

import os
import re
import subprocess

from .base import Tool, ToolContext, truncate
from .logs import _resolve_logs

# Refused unconditionally — even with operator approval. Narrow on purpose:
# the operator decides everything else; this only blocks irreversible
# system-level destruction / exfiltration patterns.
HARD_DENY = [
    (re.compile(r"\brm\s+-[rf]{1,2}[a-z]*\s+(/|~|\$HOME|/\*)(\s|$)", re.I),
     "recursive delete of / or home"),
    (re.compile(r"\bmkfs\b|\bfdisk\b|\bparted\b", re.I), "filesystem/partition op"),
    (re.compile(r"\bdd\b[^|]*\bof=/dev/", re.I), "dd to a raw device"),
    (re.compile(r">\s*/dev/(sd|nvme|hd|mmcblk|vd)", re.I), "write to a raw device"),
    (re.compile(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;", ), "fork bomb"),
    (re.compile(r"\b(shutdown|reboot|halt|poweroff)\b|\binit\s+[06]\b", re.I),
     "power/runlevel change"),
    (re.compile(r"\b(curl|wget)\b.*\|\s*(sudo\s+)?(ba)?sh\b", re.I),
     "pipe-download-to-shell"),
    (re.compile(r"\bchmod\s+-R\s+0*777\s+/(\s|$)", re.I), "chmod 777 /"),
    (re.compile(r"\bmkfs|\bshred\b\s+/dev|\bwipefs\b", re.I), "disk wipe"),
]


def _run_bash(args: dict, ctx: ToolContext) -> str:
    cmd = (args.get("command") or "").strip()
    purpose = (args.get("purpose") or "").strip()
    if not cmd:
        return "ERROR: `command` is required."

    cfg = ctx.cfg
    if not getattr(cfg, "allow_shell", False):
        return ("REFUSED: shell execution is disabled. Use read_logs / "
                "read_file / search_manual instead. (The operator can enable "
                "it by starting logb with --allow-shell.)")

    for rx, why in HARD_DENY:
        if rx.search(cmd):
            return (f"REFUSED (hard block: {why}). This command is never run, "
                    f"even with approval. Propose a safe, scoped alternative.")

    if not cfg.interactive or ctx.on_confirm is None:
        return ("DENIED: shell needs an operator to approve each command and "
                "this session is non-interactive. Proceed without it.")

    if not ctx.on_confirm(cmd, purpose):
        return ("DENIED by operator. Do not retry the same command — either "
                "ask_user what to do instead or continue with other tools.")

    # Hand the resolved log path(s) to the shell so a weak model never has to
    # retype a long path: it can just use "$LOGB_LOG".
    logs = [str(p.resolve()) for p in _resolve_logs(cfg)]
    env = dict(os.environ)
    if logs:
        env["LOGB_LOG"] = logs[0]
        env["LOGB_LOGS"] = "\n".join(logs)
        env["LOGB_LOGDIR"] = os.path.dirname(logs[0])

    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                            timeout=getattr(cfg, "shell_timeout", 60),
                            cwd=cfg.project_root, env=env)
    except subprocess.TimeoutExpired:
        return f"ERROR: command timed out ({getattr(cfg,'shell_timeout',60)}s)."
    except Exception as e:  # noqa: BLE001
        return f"ERROR launching command: {type(e).__name__}: {e}"

    out = (f"$ {cmd}\n[exit {r.returncode}]\n"
           f"--- stdout ---\n{r.stdout or '(empty)'}\n"
           f"--- stderr ---\n{r.stderr or '(empty)'}")
    return truncate(out, cfg.tool_result_char_budget)


RUN_BASH = Tool(
    name="run_bash",
    description=(
        "Propose a small shell command to run for diagnostics (e.g. grep / "
        "awk / find / head / wc over the log or files it references). The "
        "operator must approve each command before it runs; commands are run "
        "from the project root with a timeout. Prefer read_logs / read_file / "
        "search_manual when they already answer the need. The target log is "
        "preset in the shell as $LOGB_LOG (absolute path) — reference it as "
        "\"$LOGB_LOG\" instead of typing a path. Always give a clear "
        "`purpose` — the operator sees it when deciding whether to allow it. "
        "Keep commands read-only and narrowly scoped."),
    parameters={
        "type": "object",
        "properties": {
            "command": {"type": "string",
                        "description": "The bash command (single line, scoped, read-only)."},
            "purpose": {"type": "string",
                        "description": "Why you need it — shown to the operator for approval."},
        },
        "required": ["command", "purpose"],
    },
    run=_run_bash,
)
