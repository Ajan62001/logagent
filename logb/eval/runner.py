"""Eval runner. Loads cases, runs each against the current backend/model,
scores the answer against expected/forbidden facts, aggregates.

Design constraints (deliberate):

  * No network mocking. The harness exercises the real agent loop with the
    real LLM backend. It is slow on purpose — that's the whole point of
    measuring real model behavior rather than the FakeClient.
  * Scoring is conservative substring matching with optional `any_of` /
    `regex` shapes for flexibility. The intent is "the answer
    demonstrably includes the right facts," not "the answer is identical
    to a reference." LLM-judge scoring is an option for later but adds
    another network dep and a new failure mode.
  * Per-case telemetry (tokens/latency/verification passes) is captured
    so model comparisons can weigh quality vs. cost.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

CORPUS_DIR = Path(__file__).parent / "corpus"


@dataclass
class EvalCase:
    id: str
    log_path: str
    question: str
    mode: str = "eda"                # 'eda' | 'generic' | 'auto'
    expected_facts: list = field(default_factory=list)
    forbidden_facts: list = field(default_factory=list)
    manual_dir: str | None = None
    skills_dir: str | None = None
    project_root: str | None = None
    max_steps: int = 8

    @classmethod
    def from_dict(cls, d: dict) -> "EvalCase":
        return cls(
            id=d["id"],
            log_path=d["log_path"],
            question=d["question"],
            mode=d.get("mode", "eda"),
            expected_facts=list(d.get("expected_facts", [])),
            forbidden_facts=list(d.get("forbidden_facts", [])),
            manual_dir=d.get("manual_dir"),
            skills_dir=d.get("skills_dir"),
            project_root=d.get("project_root"),
            max_steps=int(d.get("max_steps", 8)),
        )


@dataclass
class EvalResult:
    case_id: str
    answer: str
    passed: bool
    hits: list                       # facts that matched
    misses: list                     # facts that didn't
    forbidden_hits: list             # forbidden facts that appeared
    steps: int
    tokens_in: int
    tokens_out: int
    latency_ms: int
    llm_calls: int
    verification_passes: int
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "case_id": self.case_id,
            "passed": self.passed,
            "hits": self.hits,
            "misses": self.misses,
            "forbidden_hits": self.forbidden_hits,
            "steps": self.steps,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "latency_ms": self.latency_ms,
            "llm_calls": self.llm_calls,
            "verification_passes": self.verification_passes,
            "answer_excerpt": (self.answer or "")[:200],
            "error": self.error,
        }


# --------------------------------------------------------------------------- #
#  Fact matching. A fact can be:                                              #
#    - a str: substring (case-insensitive)                                    #
#    - {"any_of": [str, ...]}: at least one substring matches                  #
#    - {"regex": "..."}: pattern matches                                       #
#    - {"all_of": [...]}: every sub-fact matches                               #
# --------------------------------------------------------------------------- #
def _matches(answer: str, fact) -> bool:
    if isinstance(fact, str):
        return fact.lower() in answer.lower()
    if isinstance(fact, dict):
        if "any_of" in fact:
            return any(_matches(answer, f) for f in fact["any_of"])
        if "all_of" in fact:
            return all(_matches(answer, f) for f in fact["all_of"])
        if "regex" in fact:
            try:
                return bool(re.search(fact["regex"], answer, re.I))
            except re.error:
                return False
    return False


def score_one(case: EvalCase, answer: str) -> tuple[bool, list, list, list]:
    """Return (passed, hits, misses, forbidden_hits) for an answer.

    `passed` requires ALL expected_facts present AND NO forbidden_facts
    present. The strict criterion is intentional — a partial answer that
    also hallucinates is still a wrong answer."""
    hits, misses = [], []
    for fact in case.expected_facts:
        if _matches(answer, fact):
            hits.append(fact)
        else:
            misses.append(fact)
    forbidden_hits = [f for f in case.forbidden_facts if _matches(answer, f)]
    passed = not misses and not forbidden_hits
    return passed, hits, misses, forbidden_hits


# --------------------------------------------------------------------------- #
#  Loading cases.                                                             #
# --------------------------------------------------------------------------- #
def load_cases(corpus_dir: Path = CORPUS_DIR,
                filter_pattern: str | None = None) -> list[EvalCase]:
    cases: list[EvalCase] = []
    if not corpus_dir.is_dir():
        return cases
    for path in sorted(corpus_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as e:
            print(f"[eval] skipping {path}: {e}")
            continue
        case = EvalCase.from_dict(data)
        if filter_pattern and filter_pattern not in case.id:
            continue
        cases.append(case)
    return cases


# --------------------------------------------------------------------------- #
#  Running one case.                                                          #
# --------------------------------------------------------------------------- #
def _run_case(case: EvalCase, build_agent: Callable) -> EvalResult:
    """build_agent: (EvalCase) -> Agent. Injected so tests can supply a
    deterministic FakeClient instead of a live Ollama backend."""
    try:
        agent = build_agent(case)
        t0 = time.monotonic()
        res = agent.ask(case.question)
        # The result already carries telemetry; latency_ms may include
        # wall time only for the LLM. We also record total wall time as
        # an upper bound — useful for tool-heavy slow runs.
        wall_ms = int((time.monotonic() - t0) * 1000)
        passed, hits, misses, forbidden = score_one(case, res.answer)
        return EvalResult(
            case_id=case.id, answer=res.answer, passed=passed,
            hits=hits, misses=misses, forbidden_hits=forbidden,
            steps=res.steps, tokens_in=res.tokens_in,
            tokens_out=res.tokens_out,
            latency_ms=max(res.latency_ms, wall_ms),
            llm_calls=res.llm_calls,
            verification_passes=res.verification_passes,
        )
    except Exception as e:                       # noqa: BLE001
        return EvalResult(
            case_id=case.id, answer="", passed=False,
            hits=[], misses=case.expected_facts,
            forbidden_hits=[], steps=0, tokens_in=0, tokens_out=0,
            latency_ms=0, llm_calls=0, verification_passes=0,
            error=f"{type(e).__name__}: {e}",
        )


def run_corpus(corpus_dir: Path = CORPUS_DIR, *,
                build_agent: Callable,
                filter_pattern: str | None = None,
                max_cases: int | None = None,
                on_case_done: Callable | None = None) -> list[EvalResult]:
    """Run every case in the corpus and return one EvalResult per case."""
    cases = load_cases(corpus_dir, filter_pattern)
    if max_cases:
        cases = cases[:max_cases]
    results: list[EvalResult] = []
    for case in cases:
        r = _run_case(case, build_agent)
        results.append(r)
        if on_case_done is not None:
            on_case_done(case, r)
    return results


# --------------------------------------------------------------------------- #
#  Aggregating + reporting.                                                   #
# --------------------------------------------------------------------------- #
def summarize(results: list[EvalResult]) -> dict:
    n = len(results)
    if n == 0:
        return {"n": 0, "pass_rate": 0.0}
    passed = sum(1 for r in results if r.passed)
    return {
        "n": n,
        "passed": passed,
        "pass_rate": passed / n,
        "fact_recall": (
            sum(len(r.hits) for r in results)
            / max(1, sum(len(r.hits) + len(r.misses) for r in results))),
        "hallucination_rate": (
            sum(1 for r in results if r.forbidden_hits) / n),
        "avg_steps": sum(r.steps for r in results) / n,
        "avg_llm_calls": sum(r.llm_calls for r in results) / n,
        "avg_tokens_in": sum(r.tokens_in for r in results) / n,
        "avg_tokens_out": sum(r.tokens_out for r in results) / n,
        "avg_latency_s": sum(r.latency_ms for r in results) / n / 1000,
        "verification_reasks":
            sum(max(0, r.verification_passes - 1) for r in results),
        "errored": sum(1 for r in results if r.error),
    }


def render_report(results: list[EvalResult]) -> str:
    """Human-readable per-case table + summary footer."""
    out = ["# eval results",
           f"{'CASE':<24} {'STATUS':<6} {'HIT':>3}/{'TOT':<3}  "
           f"{'STEPS':>5} {'LLM':>4} {'TOK_IN':>8} {'TOK_OUT':>7} "
           f"{'TIME':>6}  NOTES"]
    for r in results:
        tot = len(r.hits) + len(r.misses)
        status = "PASS" if r.passed else "FAIL"
        notes = []
        if r.forbidden_hits:
            notes.append(f"hallucinated: {', '.join(map(str, r.forbidden_hits[:2]))}")
        if r.misses:
            notes.append(f"missed: {', '.join(map(str, r.misses[:2]))}")
        if r.verification_passes > 1:
            notes.append(f"verify x{r.verification_passes}")
        if r.error:
            notes.append(f"ERROR: {r.error[:60]}")
        out.append(
            f"{r.case_id:<24} {status:<6} {len(r.hits):>3}/{tot:<3}  "
            f"{r.steps:>5} {r.llm_calls:>4} {r.tokens_in:>8} "
            f"{r.tokens_out:>7} {r.latency_ms / 1000:>5.1f}s  "
            f"{'; '.join(notes)}")
    s = summarize(results)
    out.append("")
    out.append("# summary")
    out.append(f"  pass_rate          : {s['pass_rate']:.1%} "
                f"({s.get('passed', 0)} / {s['n']})")
    out.append(f"  fact_recall        : {s.get('fact_recall', 0):.1%}")
    out.append(f"  hallucination_rate : {s.get('hallucination_rate', 0):.1%}")
    out.append(f"  avg_steps          : {s.get('avg_steps', 0):.1f}")
    out.append(f"  avg_llm_calls      : {s.get('avg_llm_calls', 0):.1f}")
    out.append(f"  avg_tokens         : "
                f"{s.get('avg_tokens_in', 0):.0f} in / "
                f"{s.get('avg_tokens_out', 0):.0f} out")
    out.append(f"  avg_latency        : {s.get('avg_latency_s', 0):.1f}s")
    if s.get("errored", 0):
        out.append(f"  errored            : {s['errored']}")
    return "\n".join(out)
