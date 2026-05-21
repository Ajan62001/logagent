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
      step so you can see what's done and what's next.

      THE PLAN IS A LIVE WORK-QUEUE, NOT A ONE-SHOT LIST:
        - Many tools (log_summary, code_lookup, search_manual) emit
          `[N] <tool>(args)` TODO lines in their results. These are
          AUTO-APPENDED to your plan as pending tasks — you do not
          need to manually call update_plan(add_tasks=...) for them.
        - When YOU read a tool result and realise you need MORE info
          (a referenced file you haven't read, a related code you
          haven't looked up, a hypothesis to confirm), CALL
          update_plan(add_tasks=['<the new action>']) to register it.
          Then execute it. Do not skip work just because the plan
          didn't list it up front.
        - Before emitting the final answer you MUST have zero pending
          tasks. The verifier checks this. If the plan still has open
          tasks, work them down — don't bail out with an incomplete
          investigation.

      Finally emit the four-section template (## Root Cause /
      ## Evidence / ## Fix / ## Suggestions to Improve) — the ONLY
      mode that produces it.

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

Tool-call protocol (CRITICAL — failing this guarantees a wrong answer):
- To call a tool, emit it through the function-calling protocol. Your
  client will marshal the call and return the result as a tool message.
- NEVER write a tool-call JSON object like
  `{"name": "search_manual", "arguments": {...}}`
  inside your TEXT reply. Text written that way is just prose; the tool
  does NOT execute. Any content you derive from such a "call" is
  hallucinated. The runtime now detects this pattern and rejects answers
  that contain it.
- Your text reply is for: (a) the FINAL ANSWER for the user, or
  (b) a brief justification before tool calls. Nothing else.

Repeat-call discipline (CRITICAL): if you've already called a tool with
specific arguments and got a result THIS TURN, do NOT call the same tool
with the same arguments again — the result will not change and the
runtime will refuse the call. Either:
  - use the prior result that's still in your history, or
  - change the arguments, or
  - call a different tool, or
  - emit your final answer if you have enough information.

Delegation (`delegate_subtask`): when a plan step is itself multi-tool work
that would clutter your history (e.g. "investigate everything about the
CTS stage", "cross-check each error against the manual"), call
delegate_subtask(focus="<one-line goal>", max_steps=N). The sub-agent runs
silently with its own history, calls whatever tools it needs, and returns
ONE summary back to you. Recursion depth is capped at 2 — don't try to
delegate from inside a delegation more than once. Sub-agents cannot ask
the user or run shell. Don't delegate for simple single-tool steps; the
overhead isn't worth it.

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
    # Per-turn accumulated telemetry. Token counts and latency_ms may be
    # None if the backend didn't report them (rare; both Ollama and
    # Anthropic do). The eval harness consumes these to score efficiency.
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms: int = 0
    llm_calls: int = 0
    verification_passes: int = 1


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
# A JSON object that *looks like* a tool call written in the response text
# instead of being emitted through the function-calling protocol. The model
# sometimes does this when confused — e.g. emits dozens of {"name": "X",
# "arguments": {...}} blocks in markdown ```json fences. Those blocks DO
# NOT execute; they're just text. We detect them so verification can
# reject the answer and force the model to use the real protocol.
_TOOL_CALL_IN_TEXT_RX = re.compile(
    r'\{\s*"name"\s*:\s*"(?P<name>[A-Za-z_][A-Za-z0-9_]*)"\s*,\s*'
    r'"arguments"\s*:\s*\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}\s*\}',
    re.DOTALL,
)


def _detect_tool_calls_in_text(text: str) -> list[str]:
    """Names of tool-call blocks the model emitted as text (not protocol).
    Returns the list of names (with duplicates) — empty list means clean."""
    return [m.group("name") for m in _TOOL_CALL_IN_TEXT_RX.finditer(text)]


_DEDUP_IGNORE_ARGS = {
    # When the agent already has a target log configured, an explicit
    # `path` argument that names that same log is a no-op — drop it
    # from the dedup signature so the model can't bypass the cap by
    # appending it.
    "path", "file",
}


def _tool_call_signature(name: str, args: dict) -> str:
    """Canonical (name, args) string for dup detection within a turn.

    Args whose name is in _DEDUP_IGNORE_ARGS (e.g. `path=` for a tool
    that already defaults to the target log) are stripped so the model
    can't escape the duplicate cap by appending a redundant kwarg.
    """
    args = {k: v for k, v in (args or {}).items()
            if k not in _DEDUP_IGNORE_ARGS}
    try:
        argstr = json.dumps(args, sort_keys=True, default=str)
    except (TypeError, ValueError):
        argstr = repr(args)
    return f"{name}:{argstr}"


# --------------------------------------------------------------------------- #
#  Quoted-text + numeric-claim verification.                                  #
#                                                                             #
#  The _path:line_ cite checker catches a specific shape; the model can       #
#  still slip in confabulations like:                                         #
#    - a backtick-quoted literal that doesn't appear in any tool result, OR   #
#    - a count ("47 ERROR, 2 FATAL") that contradicts log_summary.            #
#                                                                             #
#  These two checks below close that gap. Both are conservative — they only   #
#  flag claims whose source IS structurally present in the transcript, so     #
#  they don't false-positive on novel paraphrasing.                            #
# --------------------------------------------------------------------------- #

# Backtick-quoted text of substantial length. Cite forms like `top.sdc:88` are
# handled by _CITE_RX and excluded here, since they're meant to be checked
# differently (file existence, not transcript presence).
_QUOTED_TEXT_RX = re.compile(r"`([^`\n]{20,})`")
_CITE_SHAPE_RX = re.compile(r"^[\w./\\\-]+\.[\w]{1,8}[:][\d]+\b")

# "47 ERROR", "2 FATAL", "310 WARN" / "WARNING" — the kind of count the model
# emits in Mode A or when summarizing.
_NUMERIC_CLAIM_RX = re.compile(
    r"\b(\d+)\s+(FATAL|ERROR|WARN(?:ING)?)\b", re.I)

# Parse log_summary output to extract the authoritative counts. Matches the
# header line "EXACT counts (whole file): 2 FATAL · 4 ERROR · 2 WARN ..."
_SUMMARY_COUNT_RX = re.compile(
    r"(\d+)\s+(FATAL|ERROR|WARN(?:ING)?)", re.I)


def _collect_tool_haystack(history: list) -> str:
    """Concatenate all tool-result text so we can substring-check claims."""
    return "\n".join(h.get("result", "") for h in history
                     if h.get("role") == "tool")


def _find_quoted_text_problems(answer: str, history: list) -> list[str]:
    """Flag any backtick-quoted substring (≥20 chars) in the answer that
    doesn't appear in any tool result. Catches the 'paraphrase as literal
    quote' failure mode where the model fabricates a quote that resembles
    what a tool would say."""
    if not history:
        return []
    haystack = _collect_tool_haystack(history)
    if not haystack:
        return []
    seen: set[str] = set()
    problems: list[str] = []
    for m in _QUOTED_TEXT_RX.finditer(answer):
        quoted = m.group(1).strip()
        if not quoted or quoted in seen:
            continue
        seen.add(quoted)
        # Skip cite-shaped quotes (path:line) — handled by _verify_citations.
        if _CITE_SHAPE_RX.match(quoted):
            continue
        if quoted in haystack:
            continue
        # Fall back to checking a 40-char prefix — handles small paraphrases
        # of long literals (the model may have wrapped/truncated mid-quote).
        prefix = quoted[:40]
        if len(prefix) >= 20 and prefix in haystack:
            continue
        # Definitively absent from the transcript. Real failure.
        display = quoted if len(quoted) <= 60 else quoted[:60] + "…"
        problems.append(
            f"  - quoted text `{display}` does NOT appear in any tool "
            "result this turn — the quote is fabricated or paraphrased "
            "rather than literal.")
    return problems


def _extract_summary_counts(history: list) -> dict[str, int] | None:
    """The most recent log_summary tool result's exact counts, or None."""
    for h in reversed(history):
        if (h.get("role") == "tool"
                and h.get("name") == "log_summary"):
            result = h.get("result", "")
            # Look for the header line with counts. Avoid matching the
            # code table rows (which are per-code, not whole-file).
            header_match = re.search(
                r"EXACT counts[^\n]*?:\s*(.+)", result)
            if not header_match:
                return None
            counts: dict[str, int] = {}
            for n, sev in _SUMMARY_COUNT_RX.findall(header_match.group(1)):
                k = sev.upper().replace("WARNING", "WARN")
                counts[k] = int(n)
            return counts
    return None


def _find_numeric_claim_problems(answer: str, history: list) -> list[str]:
    """If the answer asserts 'N ERROR' / 'N FATAL' / 'N WARN' counts and a
    log_summary call this turn returned different counts, flag the
    contradiction. Mode-A answers especially benefit — they should
    parrot log_summary verbatim and otherwise are wrong."""
    counts = _extract_summary_counts(history)
    if not counts:
        return []
    seen: set[tuple[int, str]] = set()
    problems: list[str] = []
    for m in _NUMERIC_CLAIM_RX.finditer(answer):
        n = int(m.group(1))
        sev = m.group(2).upper().replace("WARNING", "WARN")
        if (n, sev) in seen:
            continue
        seen.add((n, sev))
        expected = counts.get(sev)
        if expected is None:
            continue
        if n != expected:
            problems.append(
                f"  - numeric claim '{n} {sev}' contradicts log_summary "
                f"(exact: {expected} {sev}). The answer must quote the "
                "log_summary counts verbatim.")
    return problems


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
    """Resolve a `path:line` cite back to a real file.

    The model writes cites in many shapes:
      • the bare basename of the target log (`test.log:1234`)
      • a relative path whose suffix matches the target log
        (`innovus_log/test.log:1234` when log_path is
         `/abs/path/innovus_log/test.log`)
      • a path relative to `project_root`
      • a path relative to the manual / skills dir
      • an absolute path

    We try all of these and ALSO search by basename under both
    `project_root` AND the directory containing the target log — that
    second root matters when the user runs logb with --log pointing
    far outside the project tree (the common case for prod logs).
    """
    p = Path(raw)
    # 1. Absolute path that already exists.
    if p.is_absolute() and p.is_file():
        return p

    log_path = Path(cfg.log_path)
    log_is_file = log_path.is_file()
    log_is_dir = log_path.is_dir()

    # 2. The most common case: the cite refers to the TARGET log itself.
    #    Accept by basename match or by suffix match so abbreviated paths
    #    (`innovus_log/test.log` when log is `/abs/.../innovus_log/test.log`)
    #    resolve to log_path directly.
    if log_is_file:
        if p.name == log_path.name:
            return log_path
        raw_norm = raw.replace("\\", "/")
        log_norm = str(log_path).replace("\\", "/")
        if log_norm.endswith("/" + raw_norm) or log_norm.endswith(raw_norm):
            return log_path

    # 3. Standard candidate locations.
    candidates: list[Path] = []
    candidates.append(Path(cfg.project_root) / raw)
    if log_is_file:
        candidates.append(log_path.parent / raw)
    elif log_is_dir:
        candidates.append(log_path / raw)
        # Also try without the leading dir component, in case the cite
        # already includes the log_path's tail.
        try:
            stripped = Path(raw).relative_to(log_path.name)
            candidates.append(log_path / stripped)
        except ValueError:
            pass
    candidates.append(Path(cfg.manual_dir) / raw)
    candidates.append(Path(cfg.skills_dir) / raw)
    candidates.append(p)             # bare relative-to-cwd

    seen: set = set()
    for c in candidates:
        try:
            key = str(c.resolve())
        except OSError:
            key = str(c)
        if key in seen:
            continue
        seen.add(key)
        try:
            if c.is_file():
                return c
        except OSError:
            continue

    # 4. Basename rglob under BOTH project_root and the log directory.
    #    The log dir matters when --log points outside project_root
    #    (common in prod), which the old single-root rglob missed.
    name = p.name
    search_roots = [Path(cfg.project_root)]
    log_root = log_path.parent if log_is_file else (
        log_path if log_is_dir else None)
    if log_root is not None and log_root not in search_roots:
        search_roots.append(log_root)
    for root in search_roots:
        try:
            if not root.is_dir():
                continue
            for found in root.rglob(name):
                if found.is_file():
                    return found
        except OSError:
            continue
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


# `[N] code_lookup(code='IMPLF-213')` style TODO items emitted by
# log_summary / code_lookup / search_manual. We extract these and check
# they were actually executed before letting a Mode-C answer through.
_TODO_CALL_RX = re.compile(
    r"\[\d+\]\s+(\w+)\(([^)]*)\)")


def _normalise_call(tool: str, args_str: str) -> str:
    """Canonicalise a (tool, args) pair so we can match plan tasks against
    tool calls regardless of quote style or whitespace."""
    norm = re.sub(r"['\"]", "", args_str).replace(" ", "")
    return f"{tool}({norm})"


_CODE_LOOKUP_LINE_RX = re.compile(
    r"In log \(([^)]+)\):\s*(\d+)\s*occurrence\(s\),\s*severity=(\w+),"
    r"\s*first at L(\d+)\s*\n\s*>\s*(.*)")
# Pull the top manual hit out of a code_lookup result. The location may
# be plain  `file`,  `file:LINE`, or  `file:LINE (page N)` — the new
# chunker emits the richer forms; the regex stays permissive so older
# tool results still parse.
_CODE_LOOKUP_MANUAL_RX = re.compile(
    r"\[1\]\s+(\S+?)(?::(\d+))?(?:\s+\(page (\d+)\))?\s+>\s+"
    r"([^\n(]+?)\s+\(score ([\d.]+)\)\s*\n"
    r"\s+(.{20,400})")


def _extract_evidence_for_investigated_codes(
        history: list[dict], turn_start: int) -> str:
    """For every code_lookup(code=X) called this turn, return a ready-
    made Markdown Evidence bullet block — literal log line PLUS the top
    manual hit (heading + snippet) — that the model can paste verbatim.

    The model's persistent failure mode on small local models is
    fabricating quotes and dropping manual citations from Evidence. By
    pre-extracting both halves directly out of the tool result and
    pasting them into the verification feedback, the model just has to
    copy instead of recall.
    """
    by_code: dict[str, str] = {}
    investigated_order: list[str] = []
    for h in history[turn_start:]:
        if h.get("role") == "assistant":
            for tc in h.get("tool_calls", []) or []:
                if tc.get("name") == "code_lookup":
                    code = str((tc.get("args") or {}).get("code") or "")
                    code = code.strip().upper()
                    if code and code not in investigated_order:
                        investigated_order.append(code)
        elif h.get("role") == "tool" and h.get("name") == "code_lookup":
            result = h.get("result") or ""
            m_hdr = re.search(r"# code_lookup:\s*(\S+)",
                              result.split("\n", 1)[0])
            code = m_hdr.group(1).strip().upper() if m_hdr else None
            if not code:
                continue
            m_log = _CODE_LOOKUP_LINE_RX.search(result)
            m_man = _CODE_LOOKUP_MANUAL_RX.search(result)
            manual_not_found = "From manual: NOT FOUND" in result
            if not m_log:
                continue
            fname, _count, _sev, lineno, msg = m_log.groups()
            msg = msg.strip()
            if len(msg) > 200:
                msg = msg[:200]
            bullet = f"- `{fname}:{lineno}` → {msg}"
            if m_man and not manual_not_found:
                src, m_line, m_page, heading, _score, snippet = m_man.groups()
                snippet = snippet.strip().replace("\n", " ")
                if len(snippet) > 220:
                    snippet = snippet[:220].rstrip() + "…"
                from os.path import basename
                loc = f"`{basename(src)}"
                if m_line:
                    loc += f":{m_line}"
                loc += "`"
                if m_page:
                    loc += f" (page {m_page})"
                bullet += (f"\n  Manual: {loc} > {heading.strip()} — "
                           f"{snippet}")
            elif manual_not_found:
                bullet += f"\n  Manual: no entry for {code}"
            by_code[code] = bullet
    if not by_code:
        return ""
    bullets = [by_code[c] for c in investigated_order if c in by_code]
    if not bullets:
        return ""
    return "\n".join(bullets)


def _sync_plan_from_tool(plan, tc: dict, result: str) -> None:
    """After a tool dispatches, keep the plan in sync:
      1. If the just-executed call was already a pending plan task, mark
         it done (the result text becomes its summary).
      2. Auto-append every `[N] <tool>(args)` TODO line emitted by the
         tool as a new pending plan task (deduped against the plan).
    This makes the plan the live work-queue: tools push new work into it,
    the model works it down, and the verifier checks it before answering.
    """
    if plan is None:
        return
    # Imported here to avoid a top-level cycle with logb.tools.plan.
    from .tools.plan import MAX_TASKS, MAX_TEXT_LEN, Task

    name = tc.get("name", "") or ""
    args = tc.get("args") or {}
    args_repr = ",".join(f"{k}={v}" for k, v in args.items())
    just_done = _normalise_call(name, args_repr)

    existing_norm: set[str] = set()
    for t in plan.tasks:
        m = _TODO_CALL_RX.search(t.text) or re.match(
            r"\s*(\w+)\((.*)\)", t.text)
        if m:
            existing_norm.add(_normalise_call(m.group(1), m.group(2)))
        # Mark a matching pending task as done.
        if t.status == "pending" and m:
            cand = _normalise_call(m.group(1), m.group(2))
            if cand == just_done:
                t.status = "done"
                if result:
                    snippet = result.strip().splitlines()
                    t.result = (snippet[0][:200] if snippet
                                else "(executed)")[:200]

    if not result:
        return
    proposed: list[str] = []
    for m in _TODO_CALL_RX.finditer(result):
        norm = _normalise_call(m.group(1), m.group(2))
        if norm in existing_norm:
            continue
        existing_norm.add(norm)
        # Store the human-readable form so the plan render reads naturally.
        proposed.append(f"{m.group(1)}({m.group(2).strip()})")

    if not proposed:
        return
    room = MAX_TASKS - len(plan.tasks)
    if room <= 0:
        return
    start = max((t.idx for t in plan.tasks), default=0) + 1
    for i, text in enumerate(proposed[:room]):
        plan.tasks.append(Task(idx=start + i, text=text[:MAX_TEXT_LEN]))


def _extract_pending_todos(history: list[dict],
                            turn_start: int) -> list[tuple[str, str]]:
    """Pull every `[N] <tool>(...)` line out of tool results in this turn,
    then subtract the calls the model has already made. The remainder is
    work the model is told to do but hasn't. Returns (tool_name, args_str)
    pairs sorted by first appearance."""
    proposed: list[tuple[str, str]] = []
    seen_proposed: set[tuple[str, str]] = set()
    executed: set[tuple[str, str]] = set()
    for h in history[turn_start:]:
        if h.get("role") == "tool":
            for m in _TODO_CALL_RX.finditer(h.get("result", "") or ""):
                tool = m.group(1)
                args_str = m.group(2).strip()
                # Normalise: code='X' -> code=X (strip quote variants).
                norm = re.sub(r"['\"]", "", args_str).replace(" ", "")
                key = (tool, norm)
                if key not in seen_proposed:
                    seen_proposed.add(key)
                    proposed.append(key)
        elif h.get("role") == "assistant":
            for tc in h.get("tool_calls", []) or []:
                tname = tc.get("name", "")
                args = tc.get("args") or {}
                args_str = ",".join(f"{k}={v}" for k, v in args.items())
                executed.add((tname, re.sub(r"['\"]", "",
                                              args_str).replace(" ", "")))
    return [p for p in proposed if p not in executed]


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
        self.session_id: str | None = None  # set by save_session / load_session
        # Back-reference so delegate_subtask can find this Agent (its client
        # and registry) from inside a tool dispatch without a circular import.
        ctx._agent = self
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
    def _find_problems(self, answer: str, cfg, tools_used: set[str]) -> list[str]:
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

        # 4. Tool calls written as text instead of through the function-
        #    calling protocol. These do NOT execute — anything the model
        #    claims to derive from them is fabricated.
        text_calls = _detect_tool_calls_in_text(answer)
        if text_calls:
            from collections import Counter
            counts = Counter(text_calls)
            top = ", ".join(f"{n} (x{c})" if c > 1 else n
                            for n, c in counts.most_common(5))
            problems.append(
                f"  - {len(text_calls)} tool-call JSON block(s) appear "
                f"in the TEXT content of the answer ({top}). These did "
                "NOT execute — tool calls must go through the function-"
                "calling protocol, not be written as text. Any content "
                "the answer derives from these is ungrounded.")

        # 5. Backtick-quoted literals in the answer must appear in some
        #    tool result. Otherwise the model fabricated a "direct quote".
        problems.extend(_find_quoted_text_problems(answer, self.history))

        # 6. Numeric counts ("47 ERROR") must match log_summary if the
        #    model called it. Catches off-by-N counting from windowed
        #    read_logs output instead of the exact whole-file census.
        problems.extend(_find_numeric_claim_problems(answer, self.history))

        # 7b. Coverage: every code investigated via code_lookup(code=X)
        #     in this turn must appear in the ## Evidence section of the
        #     Mode-C answer — not just name-dropped in a dismissive
        #     "further investigation needed" tail. The investigation is
        #     wasted if the synthesis doesn't reflect it.
        if _is_mode_c(answer):
            turn_start = getattr(self, "_current_turn_start", 0)
            investigated: set[str] = set()
            for h in self.history[turn_start:]:
                if h.get("role") != "assistant":
                    continue
                for tc in h.get("tool_calls", []) or []:
                    if tc.get("name") == "code_lookup":
                        code = (tc.get("args") or {}).get("code", "")
                        code = str(code).strip().upper()
                        if code:
                            investigated.add(code)
            # Slice out the Evidence section (## Evidence … next ##).
            ev_match = re.search(
                r"##\s*Evidence\s*\n(.*?)(?:\n##|\Z)", answer, re.S | re.I)
            evidence = ev_match.group(1) if ev_match else ""
            missing_evidence = [c for c in sorted(investigated)
                                 if c not in evidence]
            if missing_evidence:
                problems.append(
                    f"  - {len(missing_evidence)} code(s) were "
                    "investigated via code_lookup but do NOT appear in "
                    "the ## Evidence section: "
                    f"{', '.join(missing_evidence)}. Each investigated "
                    "code needs its own Evidence bullet: a `file:line` "
                    "from the log + what the manual passage says. Name-"
                    "dropping them in a 'further investigation needed' "
                    "sentence is NOT coverage. If multiple codes share "
                    "one root cause, the Evidence section should list "
                    "each code's bullet and then the Root Cause section "
                    "can tie them together. Re-write to cover ALL "
                    "investigated codes properly.")
            # Even when codes ARE named in Evidence, each one should be
            # backed EITHER by a manual reference OR by an explicit
            # "manual has no entry" admission. Only check codes whose
            # code_lookup actually surfaced a manual hit (i.e. it didn't
            # come back NOT FOUND) — for those, the Evidence must
            # include the manual citation, not just the log line.
            elif investigated:
                # Which codes had a real manual hit in their lookup?
                codes_with_manual_hit: set[str] = set()
                for h in self.history[turn_start:]:
                    if (h.get("role") == "tool"
                            and h.get("name") == "code_lookup"):
                        result = h.get("result") or ""
                        m_hdr = re.search(
                            r"# code_lookup:\s*(\S+)",
                            result.split("\n", 1)[0])
                        if not m_hdr:
                            continue
                        c = m_hdr.group(1).strip().upper()
                        if ("manual/" in result
                                and "NOT FOUND" not in result.split(
                                    "From manual", 1)[-1][:200]):
                            codes_with_manual_hit.add(c)
                missing_manual = [c for c in sorted(codes_with_manual_hit)
                                   if c not in evidence
                                   or "manual" not in
                                   evidence.split(c, 1)[-1][:600].lower()]
                # Coarser fallback: if NO manual ref at all but at least
                # one code had a hit, flag it.
                if codes_with_manual_hit and "manual" not in evidence.lower():
                    problems.append(
                        "  - Evidence cites log lines but contains NO "
                        "manual reference, even though code_lookup "
                        "returned a manual passage for "
                        f"{', '.join(sorted(codes_with_manual_hit))}. "
                        "Pair each log line with its manual passage "
                        "(format: `manual/<file>` > <heading> — "
                        "<snippet>). For any code whose code_lookup "
                        "said 'From manual: NOT FOUND', the Evidence "
                        "bullet should explicitly state 'manual has no "
                        "entry for <code>'.")

        # 7. Pending work in the plan. The plan is the live work-queue:
        #    tools auto-add TODOs into it; the model adds tasks via
        #    update_plan(add_tasks=...). A Mode-C final answer is only
        #    allowed when every plan task is done or skipped — anything
        #    pending or in_progress means the investigation is incomplete.
        if _is_mode_c(answer):
            turn_start = getattr(self, "_current_turn_start", 0)
            plan = getattr(self.ctx, "plan", None)
            pending_plan = []
            if plan is not None:
                pending_plan = [t for t in plan.tasks
                                if t.status in ("pending", "in_progress")]
            # Fallback for sessions without a plan: scan tool results
            # directly so the check still fires even if the model never
            # called create_plan.
            pending_raw = _extract_pending_todos(self.history, turn_start)
            if pending_plan:
                preview = "; ".join(f"#{t.idx} {t.text}"
                                     for t in pending_plan[:5])
                more = (f" (+{len(pending_plan) - 5} more)"
                        if len(pending_plan) > 5 else "")
                problems.append(
                    f"  - {len(pending_plan)} plan task(s) are not done "
                    f"({preview}{more}). Every pending/in-progress task "
                    "is required work before the final Mode-C answer. "
                    "Execute the next pending task (or call "
                    "update_plan(idx=N, status='skipped', result='<why>') "
                    "if it turned out to be unnecessary). DO NOT emit a "
                    "final answer while work remains.")
            elif pending_raw:
                preview = "; ".join(f"{t}({a})"
                                     for t, a in pending_raw[:5])
                more = (f" (+{len(pending_raw) - 5} more)"
                        if len(pending_raw) > 5 else "")
                problems.append(
                    f"  - {len(pending_raw)} TODO item(s) from prior "
                    f"tool outputs were NOT executed this turn ({preview}"
                    f"{more}). Each `[N] <tool>(...)` line is a REQUIRED "
                    "next step. Independent code prefixes are independent "
                    "failures and must each be investigated. Call the "
                    "missing tool(s) now, then re-emit the answer.")
        return problems

    def _maybe_verify_and_revise(self, answer: str, step: int,
                                  turn_start: int) -> str:
        cfg = self.ctx.cfg
        # Stash turn_start so _find_problems can scope its TODO scan to
        # this turn only (a prior turn's TODO list shouldn't gate this
        # turn's answer).
        self._current_turn_start = turn_start
        if not getattr(cfg, "verify_citations", True):
            return answer

        max_passes = max(1, int(getattr(cfg, "verify_max_passes", 3)))
        tools = self.registry.schemas(self.ctx.profile.name)

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
            #
            # For multi-code Mode-C investigations the persistent failure
            # mode on small local models is FABRICATING the literal log
            # line in the Evidence bullet. We pre-extract every real
            # log-line+citation from the code_lookup results in this
            # turn and paste them into the feedback as ready-to-use
            # Markdown — the model just has to copy them, not recall.
            evidence_paste = _extract_evidence_for_investigated_codes(
                self.history, turn_start)
            evidence_block = ""
            if evidence_paste:
                evidence_block = (
                    "\n\nUSE THESE EXACT EVIDENCE BULLETS — copy them "
                    "verbatim into the ## Evidence section (one bullet "
                    "per investigated code, IN ORDER). Do NOT paraphrase "
                    "and do NOT invent line numbers; the literal log "
                    "line + line number for every code you investigated "
                    "is already pulled below from the cached tool "
                    "results:\n\n"
                    + evidence_paste
                    + "\n\nIf you reword any of these or invent a line "
                    "number, verification will fail again.")
            feedback = (
                f"Pre-answer verification FAILED (pass {attempt}/"
                f"{max_passes}). Do NOT return the draft as-is. "
                "Problems found:\n"
                + "\n".join(problems)
                + evidence_block
                + "\n\nFix instructions:\n"
                "  - NEVER write tool-call JSON like {\"name\": \"X\", "
                "\"arguments\": {...}} in your TEXT reply — that does NOT "
                "execute. To call a tool, use the function-calling "
                "protocol (the client will marshal it). The text reply is "
                "for the FINAL ANSWER ONLY.\n"
                "  - Do NOT re-call a tool with the same arguments you "
                "already used this turn — the result will not change. "
                "If a previous result didn't help, change the args or "
                "pick a different tool.\n"
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

            # AUTO-EXECUTE missing tool TODOs before re-asking the model.
            # Small models often fixate on codes they already investigated
            # and keep re-calling those instead of the one that's missing.
            # Run the missing calls ourselves and inject the results into
            # history — then the synthesiser sees ALL the data it needs.
            auto_executed = self._auto_execute_missing_todos(turn_start)
            if auto_executed:
                self.trace(f"  [auto-executed {len(auto_executed)} pending "
                           f"TODO(s): {', '.join(auto_executed)}]")

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
            self._accumulate_telemetry(resp)
            self._turn_verification_passes = attempt + 1

            inner_steps = 0
            inner_call_counts: dict[str, int] = {}
            while resp.wants_tools and inner_steps < 4:
                self.history.append({"role": "assistant",
                                     "text": resp.text,
                                     "tool_calls": resp.tool_calls})
                for tc in resp.tool_calls:
                    argstr = ", ".join(f"{k}={v!r}"
                                       for k, v in (tc.get("args") or {}).items())
                    self.trace(f"  → {tc['name']}({argstr}) [revision]")
                    result = self._dispatch_with_dedup(tc, inner_call_counts)
                    self.history.append({"role": "tool", "id": tc["id"],
                                         "name": tc["name"], "result": result})
                    _sync_plan_from_tool(getattr(self.ctx, "plan", None),
                                          tc, result)
                inner_steps += 1
                try:
                    resp = self.client.chat(self.system, self.history,
                                            tools, on_token=self.on_token)
                except TypeError:
                    resp = self.client.chat(self.system, self.history, tools)
                self._accumulate_telemetry(resp)

            # If the inner loop burned its budget on tool calls and the
            # model never produced text, force ONE final tools=[] call so
            # it MUST synthesise rather than spinning. This is the
            # difference between "empty revision" (the prior failure
            # mode) and getting any answer at all.
            revised = (resp.text or "").strip()
            if not revised:
                self.trace("  [verification: inner loop ended without "
                           "text — forcing synthesis with tools disabled]")
                self.history.append({
                    "role": "user",
                    "text": ("You have called enough tools. The data is "
                             "in history above. Now WRITE THE ANSWER. "
                             "Emit the four-section Mode-C template "
                             "(## Root Cause / ## Evidence / ## Fix / "
                             "## Suggestions to Improve). One Evidence "
                             "bullet per investigated code. Do NOT call "
                             "any more tools.")})
                try:
                    resp = self.client.chat(self.system, self.history, [],
                                            on_token=self.on_token)
                except TypeError:
                    resp = self.client.chat(self.system, self.history, [])
                self._accumulate_telemetry(resp)
                revised = (resp.text or "").strip()
            if not revised:
                # Truly empty — let the next pass try (or fall through).
                self.trace("  [verification: empty revision]")
                continue
            answer = revised
        return answer

    def _auto_execute_missing_todos(self, turn_start: int) -> list[str]:
        """Run any `[N] tool(args)` TODO line that's emitted by a prior
        tool result this turn but hasn't been executed yet. Returns the
        list of human-readable call signatures that were executed.

        This is the safety net for small models that read a TODO list,
        say "yes I will investigate X next", then re-call codes they
        already had. We just do it for them.
        """
        pending = _extract_pending_todos(self.history, turn_start)
        if not pending:
            return []
        executed: list[str] = []
        # Mirror the agent loop's plan-sync and dispatch path so the
        # plan tasks get marked done and the tool results land in
        # history exactly as if the model had called them.
        for i, (tool, args_str) in enumerate(pending):
            # Parse the args back into a dict. Accept the simple
            # `k=v[,k=v]` form the TODO emitter uses.
            args: dict = {}
            for pair in args_str.split(","):
                pair = pair.strip()
                if "=" not in pair:
                    continue
                k, _, v = pair.partition("=")
                v = v.strip().strip("'\"")
                args[k.strip()] = v
            tc = {"id": f"auto-{i}", "name": tool, "args": args}
            # Synthetic assistant turn so the plan sync can mark the
            # pending task done by argument match.
            self.history.append({"role": "assistant", "text": "",
                                  "tool_calls": [tc]})
            self.trace(f"  → {tool}({args_str}) [auto-executed]")
            result = self.registry.dispatch(tool, args, self.ctx)
            self.history.append({"role": "tool", "id": tc["id"],
                                  "name": tool, "result": result})
            _sync_plan_from_tool(getattr(self.ctx, "plan", None),
                                  tc, result)
            executed.append(f"{tool}({args_str})")
        return executed

    def _persist_turn(self, question: str, answer: str, steps: int,
                       turn_start: int) -> None:
        """Append an audit record and (when enabled) save the session."""
        cfg = self.ctx.cfg
        try:
            from . import session as _session   # local: avoid import cycle
            transcript_since_turn = self.history[turn_start:]
            telemetry = {
                "tokens_in": getattr(self, "_turn_tokens_in", 0),
                "tokens_out": getattr(self, "_turn_tokens_out", 0),
                "latency_ms": getattr(self, "_turn_latency_ms", 0),
                "llm_calls": getattr(self, "_turn_llm_calls", 0),
                "verification_passes":
                    getattr(self, "_turn_verification_passes", 1),
            }
            if getattr(cfg, "audit_enabled", True):
                _session.write_audit(
                    cfg.project_root, session_id=self.session_id,
                    question=question, answer=answer, steps=steps,
                    transcript=transcript_since_turn,
                    telemetry=telemetry)
            if getattr(cfg, "session_persist", False):
                _session.save_session(cfg.project_root, self)
        except Exception:                       # noqa: BLE001
            # Persistence is best-effort. A broken audit path must not
            # take down the agent loop.
            pass

    def _dispatch_with_dedup(self, tc: dict,
                              call_counts: dict) -> str:
        """Dispatch a tool call but refuse if the same (name, args) was
        already called too many times in this turn. Catches loops where the
        model keeps re-calling the same tool expecting a different result."""
        sig = _tool_call_signature(tc["name"], tc.get("args") or {})
        call_counts[sig] = call_counts.get(sig, 0) + 1
        cap = max(1, int(getattr(self.ctx.cfg,
                                  "max_repeated_tool_call", 2)))
        if call_counts[sig] > cap:
            return (f"REFUSED: {tc['name']} with these exact arguments has "
                    f"already been called {call_counts[sig] - 1} time(s) in "
                    "this turn. The result will not change. Pick a "
                    "different tool, change the arguments, or emit your "
                    "final answer now.")
        return self.registry.dispatch(tc["name"], tc.get("args") or {},
                                       self.ctx)

    def _accumulate_telemetry(self, resp: Assistant) -> None:
        """Sum LLM-call telemetry across a single ask() turn."""
        self._turn_tokens_in += resp.tokens_in or 0
        self._turn_tokens_out += resp.tokens_out or 0
        self._turn_latency_ms += resp.latency_ms or 0
        self._turn_llm_calls += 1

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
        call_counts: dict[str, int] = {}  # (name, args) signature -> count
        # Telemetry accumulators for this turn.
        self._turn_tokens_in = 0
        self._turn_tokens_out = 0
        self._turn_latency_ms = 0
        self._turn_llm_calls = 0
        self._turn_verification_passes = 1
        # Filter tool schemas by the active profile so domain-specific tools
        # (e.g. EDA stage_timeline) don't appear in generic-mode sessions.
        tools = self.registry.schemas(self.ctx.profile.name)

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
            self._accumulate_telemetry(resp)

            if not resp.wants_tools or last:
                answer = resp.text.strip() or "(no answer produced)"
                answer = self._maybe_verify_and_revise(answer, step, turn_start)
                self.history.append({"role": "assistant", "text": answer})
                # Audit + persist on every completed turn. Best-effort —
                # never let an IO error in the audit path crash the loop.
                self._persist_turn(question, answer, step, turn_start)
                return AgentResult(
                    answer=answer, steps=step,
                    transcript=list(self.history),
                    tokens_in=self._turn_tokens_in,
                    tokens_out=self._turn_tokens_out,
                    latency_ms=self._turn_latency_ms,
                    llm_calls=self._turn_llm_calls,
                    verification_passes=self._turn_verification_passes,
                )

            # Cap parallel tool calls in one response (strict mode sets
            # this to 1). Small models that emit 5 dispatches don't read
            # the intermediate results — forcing serial calls means each
            # subsequent call is informed by the previous result.
            tool_calls = resp.tool_calls
            cap = int(getattr(self.ctx.cfg,
                              "max_tool_calls_per_response", 0) or 0)
            dropped = 0
            if cap > 0 and len(tool_calls) > cap:
                dropped = len(tool_calls) - cap
                tool_calls = tool_calls[:cap]
                self.trace(f"  [strict: capped {dropped} parallel tool "
                           f"call(s); kept first {cap}]")
            self.history.append({"role": "assistant", "text": resp.text,
                                  "tool_calls": tool_calls})
            for tc in tool_calls:
                argstr = ", ".join(f"{k}={v!r}"
                                    for k, v in (tc.get("args") or {}).items())
                self.trace(f"  → {tc['name']}({argstr})")
                result = self._dispatch_with_dedup(tc, call_counts)
                self.history.append({"role": "tool", "id": tc["id"],
                                     "name": tc["name"], "result": result})
                _sync_plan_from_tool(getattr(self.ctx, "plan", None),
                                      tc, result)
            # Proactive auto-execute (strict mode): after the model's
            # explicit calls land, also run any pending `[N] tool(args)`
            # TODOs from prior tool results that the model hasn't
            # touched. This breaks the "weak model fixates on first
            # code, ignores TECHLIB-1366" loop without waiting for
            # verification to catch it.
            if getattr(self.ctx.cfg, "auto_execute_todos", False):
                auto = self._auto_execute_missing_todos(turn_start)
                if auto:
                    self.trace(f"  [strict: auto-executed "
                               f"{len(auto)} pending TODO(s)]")

        # Unreachable: the last-step branch always returns.
        return AgentResult("(loop exhausted)", self.max_steps, list(self.history))
