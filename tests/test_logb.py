"""Offline tests — tools, RAG, and the agent loop with a scripted fake LLM.

No network / no Ollama needed. Run: python -m pytest -q
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from logb.agent import Agent
from logb.config import Config, is_sensitive
from logb.llm import Assistant
from logb.rag import ManualIndex
from logb.tools import ToolContext, build_registry

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _cfg(**kw) -> Config:
    base = dict(log_path=os.path.join(ROOT, "logs", "sample_innovus.log"),
                manual_dir=os.path.join(ROOT, "manual"),
                skills_dir=os.path.join(ROOT, "skills"),
                project_root=ROOT, interactive=False)
    base.update(kw)
    return Config.load(base)


def _ctx(cfg):
    return ToolContext(cfg=cfg, manual_index=ManualIndex(cfg.manual_dir))


# ---- tools -----------------------------------------------------------------
def test_read_logs_severity_finds_root_error():
    reg, ctx = build_registry(), _ctx(_cfg())
    out = reg.dispatch("read_logs", {"severity": "error"}, ctx)
    assert "IMPSDC-3071" in out          # the root-cause ERROR
    assert "top.sdc line 88" in out
    assert out.lstrip()[0].isdigit() or ":" in out.splitlines()[1]  # line-numbered


def test_read_logs_default_is_triage_not_tail():
    # No filter must surface the FIRST error (root cause), not only the tail.
    reg, ctx = build_registry(), _ctx(_cfg())
    out = reg.dispatch("read_logs", {}, ctx)
    assert "triage view" in out
    assert "IMPSDC-3071" in out          # earliest ERROR (root cause)
    assert "IMPCORE-9001" in out         # terminal FATAL too
    # the root-cause line is far from the end, yet it is present in the
    # numbered body (a "<spaces><n>: ..." line, not the census summary)
    rc_line = next(l for l in out.splitlines()
                   if "IMPSDC-3071" in l and l.lstrip()[:1].isdigit())
    assert int(rc_line.split(":")[0].strip()) < 25


def test_census_surfaces_buried_errors_on_tail_call(tmp_path):
    # Regression: errors early, only warnings at the tail. A tail call must
    # still report the census AND warn the errors are not in the window.
    p = tmp_path / "buried.log"
    body = (["**ERROR: (IMPLF-213): bad mask"] * 3
            + [f"info line {i}" for i in range(50)]
            + ["**WARN: (TECHLIB-1483): timing missing"] * 5)
    p.write_text("\n".join(body) + "\n")
    cfg = _cfg(log_path=str(p))
    out = build_registry().dispatch("read_logs", {"tail": 4}, _ctx(cfg))
    assert "CENSUS: 0 FATAL + 3 ERROR" in out
    assert "NOT in the lines below" in out          # the buried-error warning
    assert "IMPLF-213" in out                        # the actual error text
    assert "WARNING is not the root cause" in out


def test_census_clean_file_says_warnings_only(tmp_path):
    p = tmp_path / "warnonly.log"
    p.write_text("**WARN: x\nstuff\n**WARN: y\n")
    out = build_registry().dispatch("read_logs", {}, _ctx(_cfg(log_path=str(p))))
    assert "0 ERROR/FATAL" in out and "warnings only" in out


def test_read_logs_fatal_and_listing():
    reg, ctx = build_registry(), _ctx(_cfg())
    assert "FATAL" in reg.dispatch("read_logs", {"severity": "fatal"}, ctx)
    assert "sample_innovus.log" in reg.dispatch("list_logs", {}, ctx)


def test_read_logs_combined_severity_value():
    # The model routinely passes "error,fatal"; it must filter, not fall through.
    reg, ctx = build_registry(), _ctx(_cfg())
    out = reg.dispatch("read_logs", {"severity": "error,fatal"}, ctx)
    assert "IMPSDC-3071" in out and "IMPCORE-9001" in out
    assert "triage view" not in out      # took the real filter path, not fallback


def test_search_manual_ranks_error_code():
    reg, ctx = build_registry(), _ctx(_cfg())
    out = reg.dispatch("search_manual", {"query": "IMPSDC-3071 clock referenced"}, ctx)
    assert "IMPSDC-3071" in out and "create_clock" in out


def test_read_file_follows_referenced_sdc():
    reg, ctx = build_registry(), _ctx(_cfg())
    out = reg.dispatch("read_file",
                        {"path": "scripts/constraints/top.sdc",
                         "start": 86, "end": 90}, ctx)
    assert "set_clock_uncertainty 0.05 [get_clocks core_clk]" in out
    assert "88:" in out  # the buggy line is numbered


def test_read_file_refuses_sensitive():
    reg, ctx = build_registry(), _ctx(_cfg())
    out = reg.dispatch("read_file", {"path": "/home/u/.ssh/id_rsa"}, ctx)
    assert out.startswith("REFUSED")
    assert is_sensitive("/x/.aws/credentials")


def test_restrict_roots_blocks_escape():
    reg, ctx = build_registry(), _ctx(_cfg(restrict_to_roots=True))
    out = reg.dispatch("read_file", {"path": "/etc/hostname"}, ctx)
    assert out.startswith("REFUSED")


def test_skills_list_and_load():
    reg, ctx = build_registry(), _ctx(_cfg())
    assert "diagnose-missing-completion" in reg.dispatch("list_skills", {}, ctx)
    body = reg.dispatch("run_skill", {"name": "diagnose-missing-completion"}, ctx)
    assert "first causal error" in body or "cascade" in body.lower()


def test_index_counts_warnings_and_codes(tmp_path):
    p = tmp_path / "mix.log"
    p.write_text(
        "**ERROR: (IMPLF-213): bad mask\n"
        "**ERROR: (IMPLF-213): bad mask again\n"
        "info\n"
        "**WARN: (TECHLIB-1321): attr missing\n"
        "**WARN: (TECHLIB-1321): attr missing 2\n"
        "**WARN: (IMPSYC-881): beta feature\n"
        "FATAL: (IMPCORE-9001): boom\n")
    from logb import index as _idx
    idx = _idx.build(p)
    assert (idx["n_fat"], idx["n_err"], idx["n_warn"]) == (1, 2, 3)
    codes = {c[0]: c for c in idx["codes"]}
    assert codes["IMPLF-213"][1] == "error" and codes["IMPLF-213"][2] == 2
    assert codes["TECHLIB-1321"][1] == "warn" and codes["TECHLIB-1321"][2] == 2
    assert codes["IMPCORE-9001"][1] == "fatal"


def test_log_summary_exact_counts_and_unique_codes(tmp_path):
    p = tmp_path / "s.log"
    p.write_text(
        "**ERROR: (IMPLF-213): m\n" * 50          # 50 of the same code
        + "**ERROR: (IMPIMEX-4022): db modified\n"
        + "**WARN: (TECHLIB-1321): x\n" * 4
        + "info line\n")
    cfg = _cfg(log_path=str(p))
    out = build_registry().dispatch("log_summary", {}, _ctx(cfg))
    assert "51 ERROR" in out and "4 WARN" in out and "0 FATAL" in out
    assert "3 distinct code(s)" in out                 # the 3 unique codes
    assert "IMPLF-213" in out and "50" in out          # exact repeat count
    assert "IMPIMEX-4022" in out and "TECHLIB-1321" in out


def test_census_reports_exact_warn_count(tmp_path):
    p = tmp_path / "w.log"
    p.write_text("**ERROR: (E-1): a\n" + "**WARN: (W-1): b\n" * 7 + "ok\n")
    out = build_registry().dispatch("read_logs", {"tail": 1},
                                    _ctx(_cfg(log_path=str(p))))
    assert "1 ERROR + 7 WARN" in out      # warn count is exact, not eyeballed


def test_run_bash_disabled_by_default():
    reg, ctx = build_registry(), _ctx(_cfg())  # allow_shell defaults False
    out = reg.dispatch("run_bash", {"command": "echo hi", "purpose": "x"}, ctx)
    assert out.startswith("REFUSED: shell execution is disabled")


def test_run_bash_hard_deny_blocks_even_with_approval():
    cfg = _cfg(allow_shell=True)
    ctx = _ctx(cfg)
    ctx.on_confirm = lambda c, p: True          # operator says yes...
    out = build_registry().dispatch(
        "run_bash", {"command": "rm -rf /", "purpose": "cleanup"}, ctx)
    assert out.startswith("REFUSED (hard block")  # ...still refused
    assert "never run" in out


def test_run_bash_needs_operator_approval():
    cfg = _cfg(allow_shell=True, interactive=True)
    ctx = _ctx(cfg)
    ctx.on_confirm = lambda c, p: False         # operator declines
    out = build_registry().dispatch(
        "run_bash", {"command": "echo SHOULD_NOT_RUN", "purpose": "test"}, ctx)
    assert out.startswith("DENIED by operator")
    assert "SHOULD_NOT_RUN" not in out          # genuinely did not execute


def test_run_bash_runs_when_approved():
    cfg = _cfg(allow_shell=True, interactive=True)
    ctx = _ctx(cfg)
    ctx.on_confirm = lambda c, p: True
    out = build_registry().dispatch(
        "run_bash", {"command": "echo HELLO_FROM_SHELL", "purpose": "demo"}, ctx)
    assert "HELLO_FROM_SHELL" in out and "[exit 0]" in out


def test_run_bash_presets_logb_log_env_var():
    # The weak model must not need to retype the path; $LOGB_LOG is preset.
    cfg = _cfg(allow_shell=True, interactive=True)
    ctx = _ctx(cfg)
    ctx.on_confirm = lambda c, p: True
    out = build_registry().dispatch(
        "run_bash",
        {"command": 'wc -l < "$LOGB_LOG"', "purpose": "count log lines"}, ctx)
    assert "[exit 0]" in out
    assert "32" in out                       # sample_innovus.log has 32 lines


def test_run_bash_non_interactive_denied():
    cfg = _cfg(allow_shell=True, interactive=False)
    out = build_registry().dispatch(
        "run_bash", {"command": "echo x", "purpose": "y"}, _ctx(cfg))
    assert out.startswith("DENIED") and "non-interactive" in out


def test_ask_user_non_interactive_sentinel():
    reg, ctx = build_registry(), _ctx(_cfg(interactive=False))
    out = reg.dispatch("ask_user", {"question": "which log?"}, ctx)
    assert "non-interactive" in out


def test_unknown_tool_is_graceful():
    reg, ctx = build_registry(), _ctx(_cfg())
    assert reg.dispatch("nope", {}, ctx).startswith("ERROR: unknown tool")


# ---- agent loop with a scripted fake client --------------------------------
class FakeClient:
    """Replays a fixed plan: read errors -> check manual -> open sdc -> answer."""

    def __init__(self):
        self.plan = [
            Assistant(tool_calls=[{"id": "1", "name": "read_logs",
                                   "args": {"severity": "error"}}]),
            Assistant(tool_calls=[{"id": "2", "name": "search_manual",
                                   "args": {"query": "IMPSDC-3071"}}]),
            Assistant(tool_calls=[{"id": "3", "name": "read_file",
                                   "args": {"path": "scripts/constraints/top.sdc",
                                            "start": 86, "end": 90}}]),
            Assistant(text="## Root Cause\ntop.sdc:88 uses core_clk before "
                           "create_clock on line 89 (IMPSDC-3071)."),
        ]
        self.i = 0

    def chat(self, system, history, tools):
        a = self.plan[self.i]
        self.i += 1
        return a


def test_agent_injects_real_log_path_into_prompt():
    # Regression: the model invented 'log.txt' because it never knew the path.
    cfg = _cfg()
    agent = Agent(FakeClient(), build_registry(), _ctx(cfg), max_steps=8)
    assert "SESSION CONTEXT" in agent.system
    assert "sample_innovus.log" in agent.system      # the concrete target
    assert "never invent placeholders" in agent.system


def test_agent_loop_runs_tools_then_answers():
    cfg = _cfg()
    agent = Agent(FakeClient(), build_registry(), _ctx(cfg), max_steps=8)
    res = agent.ask("Why did the run crash?")
    assert "IMPSDC-3071" in res.answer
    assert res.steps == 4
    # tool results made it into history
    assert any(h["role"] == "tool" and "top.sdc" in h["result"]
               for h in res.transcript)


def test_agent_forces_answer_on_last_step():
    class Loopy:
        def chat(self, s, h, t):
            if t:  # tools offered -> keep calling a tool
                return Assistant(tool_calls=[{"id": "x", "name": "list_logs",
                                              "args": {}}])
            return Assistant(text="forced final answer")  # last step: no tools
    agent = Agent(Loopy(), build_registry(), _ctx(_cfg()), max_steps=3)
    res = agent.ask("loop forever?")
    assert res.answer == "forced final answer"
    assert res.steps == 3


if __name__ == "__main__":
    import subprocess
    raise SystemExit(subprocess.call(
        [sys.executable, "-m", "pytest", "-q", __file__]))
