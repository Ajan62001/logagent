"""The agent loop.

One reasoning loop: the model is given the tools and decides what to call. It
keeps calling tools (read logs, search the manual, run skills, open referenced
files, ask the operator) until it can answer, then emits a structured RCA.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .llm import Assistant
from .tools import ToolContext, ToolRegistry

BASE_PROMPT = """\
You are logb, an expert debugging agent for log files. You analyze logs, find
the root cause of failures, and suggest fixes. The active domain profile is
attached below — read its conventions before answering.

╔══════════════════════════════════════════════════════════════════════════╗
║ STEP 0 — CLASSIFY THE REQUEST, THEN ANSWER ONLY WHAT WAS ASKED.            ║
║ Pick EXACTLY ONE mode below and obey only that mode's contract. The        ║
║ four-section Root-Cause report (## Root Cause / ## Evidence / ## Fix /     ║
║ ## Suggestions) belongs to ROOT-CAUSE mode ALONE. Emitting it for a count  ║
║ or an informational question is a WRONG answer even if the facts in it     ║
║ are correct — the shape must match the question. Do not pad a one-line     ║
║ answer into the template to look thorough.                                 ║
╚══════════════════════════════════════════════════════════════════════════╝

MODE A — COUNT / UNIQUES  ("how many errors/warnings", "count of errors",
  "list the unique errors/warnings", "what kinds of failures")
    → call log_summary; report its EXACT counts / distinct-code table
      verbatim. NEVER count or list uniques by eyeballing read_logs (it is a
      bounded, early-stopping window — counting from it is WRONG).
      The answer is just that number/list. No scan, no RCA template.

MODE B — INFORMATIONAL  ("what are the warnings", "show the errors", "what
  does <CODE> mean", "summarize the <stage>")
    → fetch exactly that and answer it plainly:
        warnings  → read_logs(severity="warn")
        a code    → read_logs(pattern="<CODE>") and/or search_manual
        a concept → search_manual
      The answer is only that information, cited `file:line`. Do NOT run an
      unrequested error scan. Do NOT emit the Root-Cause template.

MODE C — ROOT-CAUSE / DEBUG  ("why did it fail", "root cause", "fix the
  crash", "what's wrong", "debug this run")
    → BEFORE any other tool call, you MUST call create_plan(tasks=[...])
      with the ordered steps you intend to take (3-8 concrete actions like
      "log_summary to get exact counts", "read_logs(severity=error,fatal)",
      "search_manual for the first error code", "read_file the referenced
      script/SDC"). Then execute the plan one task at a time, calling
      update_plan(idx=N, status='done', result='<one line>') after each
      tool result. The plan is rendered in your system context on every
      step so you can see what's done and what's next. Finally emit the
      four-section template (## Root Cause / ## Evidence / ## Fix /
      ## Suggestions to Improve) — the ONLY mode that produces it.

If you genuinely cannot tell A vs B vs C, call ask_user — do NOT default to C.

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

Durable memory (`save_note` / `get_note` / `list_notes` / `delete_note`):
the conversation history is volatile — it gets truncated once the context
window fills, and it vanishes on process restart. You MUST call save_note
at the end of any Mode-C answer for at least these keys: `root_cause` (one
sentence), `first_error` (file:line of the earliest error you found), and
`exact_counts` (the n_fatal/n_error/n_warn from log_summary). Save your
*synthesis*, not raw tool output. At the START of any follow-up question
(turn 2 onward in chat), call list_notes first to see what's already
known.

Re-query discipline for follow-ups: a question like "any other errors",
"what else", "anything I missed", "are there more X" is a NEW factual
request, NOT a request to summarize what you already said. You MUST call
log_summary or read_logs(severity=...) again to ground the answer in the
log — replying from memory alone is a WRONG answer. Only skip the re-query
if a relevant `save_note` already covers it (e.g. you saved
`exact_counts` and the user asks for counts again).

Citation format (strict): write evidence as `path:line` with a literal
colon — `top.sdc:88` not `top.sdc line 88`. The verifier checks each
`path:line` against the real file; an answer with un-cited claims or
prose-form cites is treated as un-verifiable.

────────────────────────────────────────────────────────────────────────────
ROOT-CAUSE MODE ONLY (Mode C). If you classified the request as Mode A or
Mode B, IGNORE this entire section — it does not apply and its template must
not appear in your answer.

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
    """Concrete paths + active profile the model must use verbatim — prevents
    it from inventing placeholders like 'log.txt' when it skips list_logs,
    and tells it which severity vocabulary applies to this log family."""
    from .tools.logs import _resolve_logs  # local import: avoid cycle
    cfg = ctx.cfg
    profile = ctx.profile
    logs = [str(p.resolve()) for p in _resolve_logs(cfg, profile)]
    shown = ", ".join(logs[:10]) or f"(none at {cfg.log_path!r})"
    more = f" (+{len(logs) - 10} more)" if len(logs) > 10 else ""
    plan = getattr(ctx, "plan", None)
    plan_block = ""
    if plan is not None and plan.tasks:
        counts = plan.summary_counts()
        plan_block = (
            f"\nCURRENT PLAN ({counts['done']} done / "
            f"{len(plan.tasks)} total):\n"
            f"{plan.render()}\n"
            "Work the next pending task; call update_plan after each "
            "tool result so this stays accurate.")
    return (
        "SESSION CONTEXT — use these EXACT absolute paths; never invent "
        "placeholders like 'log.txt' or '/path/to/...':\n"
        f"  Active profile: {profile.name}\n"
        f"  Target log file(s): {shown}{more}\n"
        f"  Manual dir: {cfg.manual_dir}   Skills dir: {cfg.skills_dir}\n"
        "  When you omit `path`, read_logs uses the target log above.\n"
        "  In run_bash the target log is preset as the env var $LOGB_LOG "
        "(absolute) — always reference it as \"$LOGB_LOG\", do not retype it.\n"
        "\nPROFILE GUIDANCE:\n" + profile.prompt_extras
        + plan_block)


def _build_system_prompt(ctx: ToolContext) -> str:
    return BASE_PROMPT + "\n\n" + _session_context(ctx)


# Back-compat alias: callers (including tests) still import SYSTEM_PROMPT.
SYSTEM_PROMPT = BASE_PROMPT


# --------------------------------------------------------------------------- #
#  Long-chat hygiene: compact old tool results so the conversation history     #
#  doesn't blow past num_ctx and get silently truncated by the backend.        #
# --------------------------------------------------------------------------- #
def _history_bytes(history: list[dict]) -> int:
    n = 0
    for h in history:
        n += len(h.get("text", "") or "")
        n += len(h.get("result", "") or "")
        for tc in h.get("tool_calls", []) or []:
            n += len(json.dumps(tc.get("args", {}), default=str))
    return n


def _compact_tool_result(result: str, budget: int) -> str:
    """Replace a fat tool result with a head+tail slice and an elision marker.
    Re-fetching is cheap — the model can re-call the same tool if it needs
    the full output again."""
    if len(result) <= budget or result.startswith("[compacted "):
        return result
    head = result[: int(budget * 0.6)]
    tail = result[-int(budget * 0.3):]
    elided = len(result) - len(head) - len(tail)
    return (f"[compacted {elided} chars — re-call the tool to re-fetch]\n"
            f"{head}\n... [middle elided] ...\n{tail}")


# --------------------------------------------------------------------------- #
#  Citation verification: parse `path:line` references from the model's       #
#  Mode-C answer and confirm each cited line actually exists. Catches the     #
#  most common hallucination class (cite-drift) before the answer is shown.   #
# --------------------------------------------------------------------------- #
_CITE_RX = re.compile(
    # Accept both `path:line` and `path line N` / `path on line N`, with
    # the path optionally wrapped in backticks. The model routinely writes
    # the prose form ("top.sdc line 88") even when the prompt says
    # path:line, so the regex must match both or verification misses
    # everything in the answer.
    r"`?([A-Za-z0-9_./\\-]+\.[A-Za-z0-9]{1,8})`?"
    r"(?:\s*(?:on\s+)?line\s+|:)(\d+)\b",
    re.I,
)

# Manual references the model emits in prose: "manual:foo/bar.txt",
# "manual/foo/bar.md", "see the manual section impl/213". These don't carry a
# line number and won't be caught by _CITE_RX, but they're the most common
# vehicle for confabulated "I checked the docs" claims.
_MANUAL_REF_RX = re.compile(
    r"\bmanual[:/]([A-Za-z0-9_./\\-]+\.[A-Za-z0-9]{1,8})\b",
    re.I,
)

# Sentences that *claim* the manual was consulted. If we see this language
# but the agent never invoked search_manual in this turn, the answer is
# uncorroborated — it's the 7B failure mode (verbal "I'll check the manual"
# followed by 100% fabricated content).
_MANUAL_CLAIM_RX = re.compile(
    r"\b(?:reference|referenc(?:ing|ed)|consult(?:ing|ed)?|"
    r"check(?:ing|ed)?|cite(?:s|d)?|cit(?:ing|ed)|per|"
    r"according to|from)\s+(?:the\s+)?manual\b",
    re.I,
)


def _extract_cites(text: str) -> list[tuple[str, int]]:
    seen: set[tuple[str, int]] = set()
    out: list[tuple[str, int]] = []
    for m in _CITE_RX.finditer(text):
        path, line = m.group(1), int(m.group(2))
        if (path, line) not in seen:
            seen.add((path, line))
            out.append((path, line))
    return out


def _resolve_cite_path(raw: str, cfg) -> Path | None:
    p = Path(raw)
    if p.is_absolute() and p.is_file():
        return p
    candidates = [
        Path(cfg.project_root) / raw,
        Path(cfg.log_path).parent / raw if Path(cfg.log_path).is_file()
        else Path(cfg.log_path) / raw,
        Path(cfg.manual_dir) / raw,
        Path(cfg.skills_dir) / raw,
        p,
    ]
    for c in candidates:
        try:
            if c.is_file():
                return c
        except OSError:
            continue
    # Last resort: search by basename under project_root (cheap because
    # rglob is lazy and we cap at the first hit).
    root = Path(cfg.project_root)
    if root.is_dir():
        try:
            for found in root.rglob(p.name):
                if found.is_file():
                    return found
        except OSError:
            pass
    return None


def _read_line(path: Path, line: int) -> str | None:
    if line < 1:
        return None
    try:
        with open(path, "rb") as f:
            for i, raw in enumerate(f, 1):
                if i == line:
                    return raw.decode("utf-8", "replace").rstrip("\n")
                if i > line:
                    return None
    except OSError:
        return None
    return None


def _verify_citations(answer: str, cfg) -> list[dict]:
    """Return one row per cite: {path, line, ok, reason?, content?}."""
    results: list[dict] = []
    for path, line in _extract_cites(answer):
        resolved = _resolve_cite_path(path, cfg)
        if resolved is None:
            results.append({"path": path, "line": line, "ok": False,
                            "reason": "file not found"})
            continue
        content = _read_line(resolved, line)
        if content is None:
            results.append({"path": path, "line": line, "ok": False,
                            "reason": "line out of range"})
            continue
        results.append({"path": path, "line": line, "ok": True,
                        "resolved": str(resolved), "content": content})
    return results


def _verify_manual_refs(answer: str, cfg) -> list[dict]:
    """Check that any `manual:foo.txt` or `manual/foo.md` reference in the
    answer points at a real file under manual_dir. Catches confabulated
    manual paths the 7B model invents when it skips search_manual."""
    manual_dir = Path(cfg.manual_dir)
    seen: set[str] = set()
    out: list[dict] = []
    for m in _MANUAL_REF_RX.finditer(answer):
        rel = m.group(1)
        if rel in seen:
            continue
        seen.add(rel)
        cand = manual_dir / rel
        if cand.is_file():
            out.append({"ref": rel, "ok": True, "resolved": str(cand)})
        else:
            out.append({"ref": rel, "ok": False,
                        "reason": f"no such file under {manual_dir!s}"})
    return out


def _tools_used_in_turn(history: list[dict], turn_start: int) -> set[str]:
    """Names of tools dispatched since the user's most recent question."""
    return {h["name"] for h in history[turn_start:] if h.get("role") == "tool"}


def _claims_manual_without_calling_it(answer: str, tools_used: set[str]) -> bool:
    if "search_manual" in tools_used:
        return False
    return bool(_MANUAL_CLAIM_RX.search(answer))


def _is_mode_c(answer: str) -> bool:
    return "## Root Cause" in answer or "## Evidence" in answer


class Agent:
    def __init__(self, client, registry: ToolRegistry, ctx: ToolContext,
                 max_steps: int = 12,
                 trace: Callable[[str], None] | None = None,
                 on_token: Callable[[str], None] | None = None):
        self.client = client
        self.registry = registry
        self.ctx = ctx
        self.max_steps = max_steps
        self.trace = trace or (lambda _s: None)
        self.on_token = on_token       # live-stream callback (CLI) or None (tests)
        self.history: list[dict] = []
        self.system = _build_system_prompt(ctx)

    # ----- long-chat hygiene -----
    def _compact_threshold(self) -> int:
        cfg = self.ctx.cfg
        explicit = getattr(cfg, "history_compact_threshold", 0)
        if explicit and explicit > 0:
            return explicit
        # Default: ~2.5 bytes/token * num_ctx — keeps history under ~60% of
        # the window so the model has room for the response and the system
        # prompt without paying the context-truncation tax.
        return int(getattr(cfg, "num_ctx", 8192) * 2.5)

    def _compact_history_if_needed(self) -> None:
        threshold = self._compact_threshold()
        if _history_bytes(self.history) <= threshold:
            return
        cfg = self.ctx.cfg
        keep_recent = getattr(cfg, "history_compact_keep_recent", 4)
        budget = getattr(cfg, "history_compact_budget", 400)
        tool_idxs = [i for i, h in enumerate(self.history) if h["role"] == "tool"]
        compactable = tool_idxs[:-keep_recent] if len(tool_idxs) > keep_recent else []
        compacted = 0
        for i in compactable:
            before = len(self.history[i]["result"])
            self.history[i]["result"] = _compact_tool_result(
                self.history[i]["result"], budget)
            if len(self.history[i]["result"]) < before:
                compacted += 1
            if _history_bytes(self.history) <= threshold:
                break
        if compacted:
            self.trace(f"  [compacted {compacted} stale tool result(s) — "
                       f"history now {_history_bytes(self.history)} bytes]")

    # ----- answer verification (cites + manual claims + tool-call sanity) -----
    @staticmethod
    def _find_problems(answer: str, cfg, tools_used: set[str]) -> list[str]:
        problems: list[str] = []

        # 1. Mode-C `path:line` citations.
        if _is_mode_c(answer):
            for c in _verify_citations(answer, cfg):
                if not c["ok"]:
                    problems.append(
                        f"  - cite `{c['path']}:{c['line']}` — {c['reason']}")

        # 2. Manual file references must point at real files under
        #    manual_dir. Catches confabulated paths like
        #    `manual:techlib/impex/impex_4022.txt`.
        for m in _verify_manual_refs(answer, cfg):
            if not m["ok"]:
                problems.append(
                    f"  - manual reference `{m['ref']}` — {m['reason']}")

        # 3. "I checked the manual" without ever calling search_manual.
        if _claims_manual_without_calling_it(answer, tools_used):
            problems.append(
                "  - the answer claims to reference / consult the manual "
                "but search_manual was NOT called in this turn — the "
                "supposed manual content is uncorroborated")
        return problems

    def _maybe_verify_and_revise(self, answer: str, step: int,
                                  turn_start: int) -> str:
        cfg = self.ctx.cfg
        if not getattr(cfg, "verify_citations", True):
            return answer

        max_passes = max(1, int(getattr(cfg, "verify_max_passes", 3)))
        tools = self.registry.schemas()

        for attempt in range(1, max_passes + 1):
            tools_used = _tools_used_in_turn(self.history, turn_start)
            problems = self._find_problems(answer, cfg, tools_used)
            if not problems:
                if attempt > 1:
                    self.trace(f"  [verification: clean after pass {attempt}]")
                return answer
            if attempt >= max_passes:
                # Budget exhausted — surface the residual problems instead of
                # silently returning an answer the verifier doesn't trust.
                self.trace(f"  [verification: still {len(problems)} "
                           f"problem(s) after {attempt} pass(es); surfacing]")
                disclaimer = (
                    "\n\n---\n⚠ This answer could not be fully verified "
                    f"after {attempt} pass(es). Residual problems:\n"
                    + "\n".join(problems)
                    + "\n\nIf the model claims to quote a manual file or a "
                    "specific log line that doesn't exist here, treat that "
                    "content as unconfirmed — re-ask with a narrower "
                    "question, or switch to a stronger model "
                    "(`--backend anthropic`).")
                if self.on_token:
                    self.on_token(disclaimer)
                return answer + disclaimer

            # Build feedback for the revision pass. Critically, the re-ask
            # ALLOWS tools — telling the model "CALL search_manual" while
            # passing tools=[] is the bug that produced the bad output:
            # the model was told to look something up and simultaneously
            # forbidden from doing it.
            feedback = (
                f"Pre-answer verification FAILED (pass {attempt}/"
                f"{max_passes}). Do NOT return the draft as-is. "
                "Problems found:\n"
                + "\n".join(problems)
                + "\n\nFix instructions:\n"
                "  - If you need manual content, CALL search_manual NOW "
                "with the exact code or topic. Do NOT fabricate paths, "
                "quotes, or section names. If search_manual returns no "
                "passages, the answer MUST explicitly say: \"the manual "
                "has no entry for <X>\". Admitting ignorance is REQUIRED; "
                "inventing content is a wrong answer.\n"
                "  - Cite log evidence as `file:line` only when the line "
                "actually exists. Never cite past the log's total line "
                "count. If a claim has no verifiable source, drop it.\n"
                "  - Drop every unverifiable claim — don't paraphrase it, "
                "remove it.\n"
                "  - Reply with the corrected answer ONLY. No apology, no "
                "meta-commentary, no \"sure, here's the revised version\".")

            bad_summary = "; ".join(p.strip("- ").strip()
                                     for p in problems[:3])
            self.trace(f"  [verification pass {attempt}: {len(problems)} "
                       f"problem(s) — re-asking with tools]")
            if self.on_token:
                self.on_token(
                    f"\n\n[⚠ verification pass {attempt} failed: {bad_summary}"
                    + (f" (+{len(problems)-3} more)"
                       if len(problems) > 3 else "")
                    + " — revising"
                    + (" (tools available)" if attempt == 1 else "")
                    + "]\n\n")
            self.history.append({"role": "assistant", "text": answer})
            self.history.append({"role": "user", "text": feedback})

            # Re-ask with tools so the model can actually run search_manual
            # / read_logs to ground the revision. If the model emits tool
            # calls, dispatch them inline (mini sub-loop) before reading
            # the next answer; otherwise the text reply is the revision.
            self.system = _build_system_prompt(self.ctx)
            try:
                resp = self.client.chat(self.system, self.history, tools,
                                        on_token=self.on_token)
            except TypeError:
                resp = self.client.chat(self.system, self.history, tools)

            inner_steps = 0
            while resp.wants_tools and inner_steps < 4:
                self.history.append({"role": "assistant",
                                     "text": resp.text,
                                     "tool_calls": resp.tool_calls})
                for tc in resp.tool_calls:
                    argstr = ", ".join(f"{k}={v!r}"
                                       for k, v in tc["args"].items())
                    self.trace(f"  → {tc['name']}({argstr}) [revision]")
                    result = self.registry.dispatch(
                        tc["name"], tc["args"], self.ctx)
                    self.history.append({"role": "tool", "id": tc["id"],
                                         "name": tc["name"], "result": result})
                inner_steps += 1
                try:
                    resp = self.client.chat(self.system, self.history,
                                            tools, on_token=self.on_token)
                except TypeError:
                    resp = self.client.chat(self.system, self.history, tools)

            revised = (resp.text or "").strip()
            if not revised:
                # Empty revision — keep the prior answer, let next pass try
                # again (or fall through to the disclaimer branch).
                self.trace("  [verification: empty revision]")
                continue
            answer = revised
        return answer

    # ----- main loop -----
    def ask(self, question: str) -> AgentResult:
        """Run one user question to completion (multi-turn safe: history persists)."""
        # Fresh strategy per question: durable knowledge belongs in notes,
        # not in the plan. Avoids stale tasks bleeding into the next ask.
        plan = getattr(self.ctx, "plan", None)
        if plan is not None:
            plan.reset()
        self.history.append({"role": "user", "text": question})
        turn_start = len(self.history)   # for tracking tool calls in this turn
        tools = self.registry.schemas()

        for step in range(1, self.max_steps + 1):
            last = step == self.max_steps
            self._compact_history_if_needed()
            # Re-render the system prompt every step so the plan block
            # reflects the latest state. Cheap (it's a string format).
            self.system = _build_system_prompt(self.ctx)
            try:
                resp: Assistant = self.client.chat(
                    self.system, self.history, [] if last else tools,
                    on_token=self.on_token)
            except TypeError:
                resp = self.client.chat(
                    self.system, self.history, [] if last else tools)

            if not resp.wants_tools or last:
                answer = resp.text.strip() or "(no answer produced)"
                answer = self._maybe_verify_and_revise(answer, step, turn_start)
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
