"""Domain profiles — taxonomy of severity, codes, and stages per log family.

A Profile captures everything log-domain-specific in one place: the severity
regexes (str + bytes), the optional message-code and stage-banner patterns,
the file extensions that count as a log, and a prompt addendum the agent gets
on top of the universal base contract. Other modules read from a Profile
instance rather than hard-coding regexes — adding a new domain is a matter of
declaring another Profile, not editing tool/agent code.

Two built-ins:

  EDA      — Innovus/PrimeTime/VCS/Genus style: `**ERROR`/`FATAL`,
             `(CODE-NNN)` message codes, `--- Starting "<stage>" ---` banners.

  GENERIC  — broader vocabulary covering app/system logs: FATAL/CRITICAL/
             EMERG(ENCY), ERROR/SEVERE/EXCEPTION, klog-style E0520/W0520,
             bracketed `[ERROR]`, syslog priorities `<0..7>`, Python
             tracebacks. No assumed code or stage format.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Profile:
    name: str
    log_extensions: frozenset            # suffixes that look like log files
    severity: dict                       # {name: re.Pattern[str]}
    severity_bytes: dict                 # {name: re.Pattern[bytes]}
    code_rx: object                      # re.Pattern[bytes] | None
    stage_rx: object                     # re.Pattern[bytes] | None
    detect_signatures: tuple             # (re.Pattern[bytes], ...) — auto-detect
    prompt_extras: str                   # appended to the base system prompt


# --------------------------------------------------------------------------- #
#  EDA profile — matches today's hard-coded behavior 1:1.
# --------------------------------------------------------------------------- #
_EDA_SEV = {
    "fatal": re.compile(r"\b(FATAL|PANIC|ABORT|core dumped|Segmentation fault)\b", re.I),
    "error": re.compile(r"\b(ERROR|ERR|\*\*ERROR|FAIL(ED|URE)?)\b", re.I),
    "warn":  re.compile(r"\b(WARN(ING)?|\*\*WARN)\b", re.I),
}
_EDA_SEV_B = {
    "fatal": re.compile(rb"\b(FATAL|PANIC|ABORT|core dumped|Segmentation fault)\b", re.I),
    "error": re.compile(rb"\b(ERROR|ERR|\*\*ERROR|FAIL(ED|URE)?)\b", re.I),
    "warn":  re.compile(rb"\b(WARN(ING)?|\*\*WARN)\b", re.I),
}

EDA = Profile(
    name="eda",
    log_extensions=frozenset({".log", ".rpt", ".txt", ".out", ""}),
    severity=_EDA_SEV,
    severity_bytes=_EDA_SEV_B,
    code_rx=re.compile(rb"\(([A-Z][A-Z0-9]+-\d+)\)"),
    stage_rx=re.compile(rb'--- (Starting|Ending) "'),
    detect_signatures=(
        re.compile(rb'--- (Starting|Ending) "'),
        re.compile(rb"\([A-Z][A-Z0-9]+-\d+\)"),
        re.compile(rb"\b(innovus|primetime|genus|nanoroute|encounter)\b", re.I),
    ),
    prompt_extras=(
        "Domain: EDA place-and-route / timing / simulation flows "
        "(Innovus, PrimeTime, VCS, Genus, ...).\n"
        "- Stage banners look like `--- Starting \"<stage>\" ---` and "
        "`--- Ending \"<stage>\" ---`.\n"
        "- Message codes look like `(IMPLF-213)`, `(IMPCORE-9001)`, "
        "`(TECHLIB-1321)`. Search the manual by the exact code.\n"
        "- Cascade rule: when ERRORs exist, the FIRST one is the prime "
        "suspect; later ones are usually downstream symptoms.\n"
        "- Example Mode A: Q: \"how many errors?\" → A: \"47 ERROR, 2 FATAL, "
        "310 WARN (log_summary, exact whole-file).\"\n"
        "- Example Mode B: Q: \"what does IMPCTS-5012 mean?\" → A: a 1-2 "
        "sentence explanation from the manual with a `manual/...:line` cite."
    ),
)


# --------------------------------------------------------------------------- #
#  Generic profile — application / system / build logs without EDA conventions.
# --------------------------------------------------------------------------- #
_GEN_SEV = {
    "fatal": re.compile(
        r"\b(FATAL|CRITICAL|CRIT|EMERG(ENCY)?|PANIC|ABORT|"
        r"core dumped|Segmentation fault)\b"
        r"|Traceback \(most recent call last\)"
        r"|\[FATAL\]|\[CRITICAL\]|^<[0-2]>", re.I | re.M),
    "error": re.compile(
        r"\b(ERROR|ERR|SEVERE|EXCEPTION|FAIL(ED|URE)?)\b"
        r"|\[ERROR\]|\bE\d{4}\b|^<3>", re.I | re.M),
    "warn": re.compile(
        r"\b(WARN(ING)?)\b|\[WARN(ING)?\]|\bW\d{4}\b|^<4>", re.I | re.M),
}
_GEN_SEV_B = {
    "fatal": re.compile(
        rb"\b(FATAL|CRITICAL|CRIT|EMERG(ENCY)?|PANIC|ABORT|"
        rb"core dumped|Segmentation fault)\b"
        rb"|Traceback \(most recent call last\)"
        rb"|\[FATAL\]|\[CRITICAL\]|^<[0-2]>", re.I | re.M),
    "error": re.compile(
        rb"\b(ERROR|ERR|SEVERE|EXCEPTION|FAIL(ED|URE)?)\b"
        rb"|\[ERROR\]|\bE\d{4}\b|^<3>", re.I | re.M),
    "warn": re.compile(
        rb"\b(WARN(ING)?)\b|\[WARN(ING)?\]|\bW\d{4}\b|^<4>", re.I | re.M),
}

GENERIC = Profile(
    name="generic",
    log_extensions=frozenset({".log", ".txt", ".out", ".err",
                              ".json", ".jsonl", ".ndjson", ""}),
    severity=_GEN_SEV,
    severity_bytes=_GEN_SEV_B,
    # Catch any reasonable "code"-ish token so log_summary's distinct-code
    # table is populated for EDA logs, build logs, k8s logs, syslog, etc.
    # The alternation order matters: most-specific (paren/bracket-wrapped
    # CODE-NNN) first, then bare CODE-NNN, then klog-style E0520/W0520.
    # index.py picks whichever capture group is non-None per match.
    code_rx=re.compile(
        rb"\(([A-Z][A-Z0-9_]+-\d+)\)"          # (IMPSDC-3071), (ERR-42)
        rb"|\[([A-Z][A-Z0-9_]+-\d+)\]"         # [ERROR-1234], [LOG-100]
        rb"|\b([A-Z][A-Z0-9_]+-\d{2,})\b"      # IMPSDC-3071 (bare)
        rb"|\b([EWIF]\d{4,})\b"                # klog: E0520, W0520, I0520
    ),
    stage_rx=None,
    detect_signatures=(),     # generic is the fallback — never auto-matched first
    prompt_extras=(
        "Domain: application / system / build / runtime logs (no EDA "
        "conventions assumed).\n"
        "- Severity vocabulary is broad: FATAL/CRITICAL/EMERG, "
        "ERROR/SEVERE/EXCEPTION, WARN/WARNING, klog-style E0520/W0520, "
        "bracketed `[ERROR]`, syslog priorities `<0..7>`. The CENSUS counts "
        "all of these.\n"
        "- There is NO standardized message code or stage map — cite by "
        "`file:line` and the literal message text. Do not invent codes.\n"
        "- For exceptions / tracebacks the FIRST one is usually the cause; "
        "subsequent ones may be retries or cascade.\n"
        "- Example Mode A: Q: \"how many errors?\" → A: \"12 ERROR, 1 FATAL, "
        "84 WARN (log_summary, exact whole-file).\"\n"
        "- Example Mode B: Q: \"what is connection refused\" → search_manual "
        "if a manual exists, else cite the raw log line.\n"
        "\n"
        "ROOT-CAUSE MODE OVERRIDE for this profile:\n"
        "  You DO NOT have an authoritative manual for this log's domain. "
        "Prescribing exact code edits or commands as a 'Fix' would be "
        "hallucination. Investigate and explain — do NOT prescribe.\n"
        "  Use this 4-section template INSTEAD of the base one (same first "
        "two sections, different last two):\n"
        "    ## Root Cause\n"
        "    <what the log shows is going wrong, with cited file:line>\n"
        "    ## Evidence\n"
        "    <bullet list: `file:line` → what it shows>\n"
        "    ## Likely Causes & Next Steps\n"
        "    <ordered list of plausible underlying causes ranked by what "
        "the evidence supports, each paired with a concrete next "
        "investigation step (which file/config to check, which command to "
        "run, which metric to look at). Do NOT write 'apply this patch' "
        "or invent specific edits unless the log or a search_manual hit "
        "explicitly names them.>\n"
        "    ## Suggestions to Improve\n"
        "    <how to detect this class of failure earlier — monitoring, "
        "log-level changes, healthchecks, alert rules>\n"
        "  Rule of thumb: if you would have to guess at code/config you "
        "have not read, put it under 'Next Steps' as 'check X', not under "
        "a 'Fix' as 'change X to Y'."
    ),
)


PROFILES = {EDA.name: EDA, GENERIC.name: GENERIC}


def detect(head: bytes) -> Profile:
    """Sniff the first chunk of a log and pick a profile. Specific profiles
    are checked first; GENERIC is the fallback (its detect_signatures is empty)."""
    for prof in (EDA,):
        if any(rx.search(head) for rx in prof.detect_signatures):
            return prof
    return GENERIC


def resolve(mode: str, log_paths=None) -> Profile:
    """Map a mode string ('eda' | 'generic' | 'auto') to a Profile. With
    'auto', sniff the first readable log; falls back to GENERIC if none read."""
    mode = (mode or "").lower()
    if mode == "auto":
        for p in (log_paths or []):
            try:
                with open(p, "rb") as f:
                    head = f.read(8192)
                return detect(head)
            except OSError:
                continue
        return GENERIC
    return PROFILES.get(mode, EDA)
