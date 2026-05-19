"""The agent loop.

One reasoning loop: the model is given the tools and decides what to call. It
keeps calling tools (read logs, search the manual, run skills, open referenced
files, ask the operator) until it can answer, then emits a structured RCA.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .llm import Assistant
from .tools import ToolContext, ToolRegistry

SYSTEM_PROMPT = """\
You are logb, an expert debugging agent for EDA-tool logs (Innovus, PrimeTime,
VCS, Genus, and similar place-and-route / timing / simulation flows). Your job
is log debugging, root-cause analysis, pinpointing the fix, and suggesting
improvements.

STEP 0 — ANSWER THE QUESTION THAT WAS ASKED. Classify the request first:

  • COUNTS / UNIQUES ("how many errors/warnings", "count of errors",
    "list the unique errors/warnings", "what codes/kinds of failures"):
    call log_summary — it returns EXACT whole-file FATAL/ERROR/WARN counts
    and the distinct-code table. NEVER count or list uniques by eyeballing
    read_logs output: that is a bounded window (it early-stops), so counting
    from it is WRONG. Report log_summary's numbers verbatim.

  • OTHER INFORMATIONAL ("what are the warnings", "show the errors", "what
    does IMPLF-213 mean", "summarize the place stage"): answer THAT, nothing
    else. For warnings use read_logs(severity="warn"); for a code use
    pattern=<that code>; for a concept use search_manual. Output must fit the
    question. Do NOT run an unrequested error scan or the Root-Cause template.

  • ROOT-CAUSE / DEBUG ("why did it fail", "root cause", "fix the crash",
    "what's wrong"): use the RCA method and template below.

  • If you genuinely cannot tell which, call ask_user.

Tools:
  • list_logs / read_logs  — look in the logs with the filter that matches
    the request (severity= one or more of warn/error/fatal, comma-ok;
    pattern= a regex; tail/head; no filter = triage view). read_logs returns
    a bounded WINDOW (it early-stops at max_lines) — never count from it.
    Its CENSUS line gives the exact FATAL/ERROR/WARN counts; trust those.
  • log_summary            — exact whole-file FATAL/ERROR/WARN counts + the
    distinct message-code table. The right tool for any count/unique/"what
    kinds" question, exact even on a multi-GB log.
  • search_manual          — manual lookup for codes / fixes / command
    semantics. Query with the exact error code or message tokens.
  • list_skills / run_skill — load a diagnostic playbook and follow it.
  • read_file              — open any file path printed in a log line.
  • run_bash               — propose a small, read-only, scoped shell command
    (grep/awk/find/head/wc over the log or files it names) when the dedicated
    tools cannot get there. The operator must approve EACH command — always
    pass a clear `purpose`. If denied, don't retry the same command; adapt or
    ask_user. Prefer read_logs/read_file/search_manual whenever they suffice.
  • ask_user               — if the request or target log is ambiguous, ask
    BEFORE guessing. Never invent which log or which failure.

Cross-cutting: cite evidence as `file:line`; never assert something you did
not see in a tool result; be precise and terse.

────────────────────────────────────────────────────────────────────────────
RCA mode only (root-cause / debug requests):

  Scan-first rule: before stating a root cause you MUST have scanned the
  whole file for errors — read_logs(severity="error,fatal") (or no filter
  for triage). Never conclude while the census reports errors you have not
  read. A WARNING is not the root cause when ERROR/FATAL lines exist;
  investigate the FIRST error first. Don't trust a tail/head window — it
  hides earlier errors.

  Method: locate the terminal failure → trace back to the first causal
  event in that stage → corroborate with the manual and/or a skill → if the
  log names a file, open it to confirm.

  Answer format (Markdown):
  ## Root Cause
  <the single underlying cause, with cited log line(s)>
  ## Evidence
  <bullet list: `file:line` → what it shows>
  ## Fix
  <concrete, ordered steps — exact commands/edits where possible>
  ## Suggestions to Improve
  <how to prevent or detect this earlier>

  If evidence is insufficient, say so and state exactly what additional
  log/file you would need."""


@dataclass
class Turn:
    role: str
    text: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    id: str = ""
    name: str = ""
    result: str = ""


@dataclass
class AgentResult:
    answer: str
    steps: int
    transcript: list[dict]


def _session_context(ctx: ToolContext) -> str:
    """Concrete paths the model must use verbatim — prevents it from
    inventing placeholders like 'log.txt' when it skips list_logs."""
    from .tools.logs import _resolve_logs  # local import: avoid cycle
    cfg = ctx.cfg
    logs = [str(p.resolve()) for p in _resolve_logs(cfg)]
    shown = ", ".join(logs[:10]) or f"(none at {cfg.log_path!r})"
    more = f" (+{len(logs) - 10} more)" if len(logs) > 10 else ""
    return (
        "SESSION CONTEXT — use these EXACT absolute paths; never invent "
        "placeholders like 'log.txt' or '/path/to/...':\n"
        f"  Target log file(s): {shown}{more}\n"
        f"  Manual dir: {cfg.manual_dir}   Skills dir: {cfg.skills_dir}\n"
        "  When you omit `path`, read_logs uses the target log above.\n"
        "  In run_bash the target log is preset as the env var $LOGB_LOG "
        "(absolute) — always reference it as \"$LOGB_LOG\", do not retype it.")


class Agent:
    def __init__(self, client, registry: ToolRegistry, ctx: ToolContext,
                 max_steps: int = 12,
                 trace: Callable[[str], None] | None = None):
        self.client = client
        self.registry = registry
        self.ctx = ctx
        self.max_steps = max_steps
        self.trace = trace or (lambda _s: None)
        self.history: list[dict] = []
        self.system = SYSTEM_PROMPT + "\n\n" + _session_context(ctx)

    def ask(self, question: str) -> AgentResult:
        """Run one user question to completion (multi-turn safe: history persists)."""
        self.history.append({"role": "user", "text": question})
        tools = self.registry.schemas()

        for step in range(1, self.max_steps + 1):
            last = step == self.max_steps
            # On the final step, drop tools to force a written answer.
            resp: Assistant = self.client.chat(
                self.system, self.history, [] if last else tools)

            if not resp.wants_tools or last:
                answer = resp.text.strip() or "(no answer produced)"
                self.history.append({"role": "assistant", "text": answer})
                return AgentResult(answer, step, list(self.history))

            self.history.append({"role": "assistant", "text": resp.text,
                                  "tool_calls": resp.tool_calls})
            for tc in resp.tool_calls:
                argstr = ", ".join(f"{k}={v!r}" for k, v in tc["args"].items())
                self.trace(f"  → {tc['name']}({argstr})")
                result = self.registry.dispatch(tc["name"], tc["args"], self.ctx)
                self.history.append({"role": "tool", "id": tc["id"],
                                     "name": tc["name"], "result": result})

        # Unreachable: the last-step branch always returns.
        return AgentResult("(loop exhausted)", self.max_steps, list(self.history))
