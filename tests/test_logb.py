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


# ---- PDF manual support ----------------------------------------------------
def _make_pdf(path, lines):
    """Write a minimal valid PDF whose page content is `lines` (uncompressed,
    so the pure-stdlib extractor handles it with no external tool)."""
    show = b"BT /F1 12 Tf 72 720 Td\n" + b"\n".join(
        b"(" + ln.encode("latin-1") + b") Tj T*" for ln in lines) + b"\nET"
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(show), show),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    buf = bytearray(b"%PDF-1.4\n")
    offs = []
    for i, body in enumerate(objs, 1):
        offs.append(len(buf))
        buf += b"%d 0 obj\n%s\nendobj\n" % (i, body)
    xref = len(buf)
    buf += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for o in offs:
        buf += b"%010d 00000 n \n" % o
    buf += (b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF"
            % (len(objs) + 1, xref))
    path.write_bytes(bytes(buf))


def test_pdf_stdlib_extractor_reads_text(tmp_path):
    # Pure-stdlib path: no poppler / no pypdf needed — deterministic anywhere.
    from logb import rag
    p = tmp_path / "ref.pdf"
    _make_pdf(p, ["IMPSDC-3071: object referenced before it is created",
                  "Fix: move create_clock above any get_clocks usage"])
    text = rag._pdf_via_stdlib(p)
    assert text and "IMPSDC-3071" in text and "create_clock" in text
    # and the top-level extractor agrees (whichever backend it picks)
    assert "IMPSDC-3071" in (rag._extract_pdf_text(p) or "")


def test_manual_index_searches_pdf_end_to_end(tmp_path):
    p = tmp_path / "innovus_manual.pdf"
    _make_pdf(p, ["IMPCORE-9001: NanoRoute terminated abnormally",
                  "Cause: empty clock topology from a failed SDC",
                  "Fix: correct the SDC and re-run from place"])
    out = build_registry().dispatch(
        "search_manual", {"query": "IMPCORE-9001 NanoRoute"},
        _ctx(_cfg(manual_dir=str(tmp_path))))
    assert "IMPCORE-9001" in out
    assert "innovus_manual.pdf" in out          # cited by its real filename


def test_manual_index_warns_on_unreadable_pdf(tmp_path, capsys):
    (tmp_path / "broken.pdf").write_bytes(b"%PDF-1.4\nnot really a pdf\n")
    from logb.rag import ManualIndex
    ManualIndex(str(tmp_path)).search("anything")
    assert "could not extract text from PDF" in capsys.readouterr().err


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


# ---- generic profile -------------------------------------------------------
def test_profile_detect_eda_vs_generic():
    from logb.profiles import EDA, GENERIC, detect
    eda_head = b'--- Starting "place" ---\n**ERROR: (IMPLF-213): bad mask\n'
    k8s_head = (b"E0520 14:23:01.123 1 controller.go:42] sync failed: x\n"
                b"W0520 14:23:02.456 1 controller.go:43] retrying\n")
    py_head = (b'Traceback (most recent call last):\n'
               b'  File "app.py", line 12, in <module>\n'
               b'    foo()\nValueError: bad\n')
    nginx_head = (b'2024/05/20 14:23:01 [error] 1234#0: *5 upstream timed out\n'
                  b'2024/05/20 14:23:02 [warn] 1234#0: ssl session reused\n')
    assert detect(eda_head).name == "eda"
    assert detect(k8s_head).name == "generic"
    assert detect(py_head).name == "generic"
    assert detect(nginx_head).name == "generic"


def test_generic_profile_matches_broad_vocabulary():
    from logb.profiles import GENERIC
    assert GENERIC.severity["fatal"].search("[FATAL] db connection lost")
    assert GENERIC.severity["fatal"].search("CRITICAL: out of memory")
    assert GENERIC.severity["fatal"].search("Traceback (most recent call last):")
    assert GENERIC.severity["error"].search("[ERROR] connection refused")
    assert GENERIC.severity["error"].search("E0520 controller.go:42] failed")
    assert GENERIC.severity["error"].search("SEVERE: NullPointerException")
    assert GENERIC.severity["warn"].search("[WARNING] disk usage 90%")
    assert GENERIC.severity["warn"].search("W0520 cache miss")
    # And the EDA "**ERROR" form still matches (broader vocabulary, not narrower)
    assert GENERIC.severity["error"].search("ERROR: x")


def test_generic_profile_no_stages_codeless_log(tmp_path):
    # Codeless severity lines collapse into a single "(uncoded)" entry —
    # same shape EDA mode produces for severity lines without a code.
    from logb import index as _idx
    from logb.profiles import GENERIC
    p = tmp_path / "app.log"
    p.write_text(
        '--- Starting "place" ---\n'         # would be a stage in EDA, not in generic
        "[ERROR] connection refused\n"
        "INFO: retrying\n"
        "[ERROR] connection refused again\n"
        "CRITICAL: giving up\n"
        "[WARN] cache stale\n")
    idx = _idx.build(p, GENERIC)
    assert (idx["n_fat"], idx["n_err"], idx["n_warn"]) == (1, 2, 1)
    codes = {c[0]: c for c in idx["codes"]}
    assert list(codes) == ["(uncoded)"]  # everything bucketed as uncoded
    assert codes["(uncoded)"][2] == 4    # 1 fatal + 2 error + 1 warn
    assert idx["stages"] == []           # no stage_rx -> no stages
    assert idx["profile"] == "generic"


def test_generic_profile_extracts_eda_style_codes(tmp_path):
    # The whole point: generic mode should still surface codes if the log
    # happens to have them. Verifies that pointing --mode generic at an
    # EDA log doesn't regress the code table.
    from logb import index as _idx
    from logb.profiles import GENERIC
    p = tmp_path / "innovus.log"
    p.write_text(
        "**ERROR: (IMPSDC-3071): clock referenced before created\n"
        "**ERROR: (IMPSDC-3071): again\n"
        "**ERROR: (IMPLF-213): bad mask\n"
        "FATAL: (IMPCORE-9001): boom\n")
    idx = _idx.build(p, GENERIC)
    codes = {c[0]: c for c in idx["codes"]}
    assert "IMPSDC-3071" in codes and codes["IMPSDC-3071"][2] == 2
    assert "IMPLF-213" in codes and codes["IMPCORE-9001"] in codes.values() \
           or "IMPCORE-9001" in codes


def test_generic_profile_extracts_bracket_and_klog_codes(tmp_path):
    from logb import index as _idx
    from logb.profiles import GENERIC
    p = tmp_path / "mix.log"
    p.write_text(
        "[ERROR-1234] connection refused\n"
        "[ERROR-1234] again\n"
        "E0520 controller.go:42] sync failed\n"
        "W0520 controller.go:43] retrying\n"
        "[FATAL-9001] giving up\n")
    idx = _idx.build(p, GENERIC)
    codes = {c[0]: c for c in idx["codes"]}
    assert "ERROR-1234" in codes and codes["ERROR-1234"][2] == 2
    assert "E0520" in codes                    # klog-style error code
    assert "FATAL-9001" in codes


def test_log_summary_generic_mode_with_eda_log(tmp_path):
    # End-to-end: log_summary in generic mode against an EDA log produces
    # the same kind of distinct-code table EDA mode would.
    p = tmp_path / "innovus.log"
    p.write_text(
        "**ERROR: (IMPSDC-3071): clock\n" * 5
        + "**ERROR: (IMPLF-213): mask\n" * 2
        + "FATAL: (IMPCORE-9001): boom\n")
    cfg = _cfg(log_path=str(p), mode="generic")
    out = build_registry().dispatch("log_summary", {}, _ctx(cfg))
    assert "1 FATAL" in out and "7 ERROR" in out
    assert "IMPSDC-3071" in out and "5" in out      # exact count
    assert "IMPLF-213" in out and "IMPCORE-9001" in out
    assert "3 distinct code(s)" in out


def test_index_cache_keyed_by_profile(tmp_path):
    from logb import index as _idx
    from logb.profiles import EDA, GENERIC
    p = tmp_path / "x.log"
    p.write_text("**ERROR: (IMPLF-213): bad mask\n[ERROR] also bad\n")
    eda_path = _idx.index_path(p, EDA)
    gen_path = _idx.index_path(p, GENERIC)
    assert eda_path != gen_path
    assert "eda" in eda_path.name and "generic" in gen_path.name


def test_read_logs_generic_finds_k8s_severity(tmp_path):
    p = tmp_path / "k8s.log"
    p.write_text(
        "I0520 14:23:00 starting up\n"
        "E0520 14:23:01 controller.go:42] sync failed: connection refused\n"
        "W0520 14:23:02 controller.go:43] retrying\n"
        "E0520 14:23:03 controller.go:44] still failing\n"
        "CRITICAL pod evicted\n")
    cfg = _cfg(log_path=str(p), mode="generic")
    reg = build_registry()
    out = reg.dispatch("read_logs", {"severity": "error,fatal"}, _ctx(cfg))
    assert "sync failed" in out and "still failing" in out
    assert "pod evicted" in out                    # CRITICAL counts as fatal
    assert "CENSUS: 1 FATAL + 2 ERROR + 1 WARN" in out


def test_log_summary_generic_mode_no_codes(tmp_path):
    p = tmp_path / "app.log"
    p.write_text("[ERROR] x\n" * 3 + "[FATAL] y\n" + "[WARN] z\n" * 2)
    cfg = _cfg(log_path=str(p), mode="generic")
    out = build_registry().dispatch("log_summary", {}, _ctx(cfg))
    assert "1 FATAL" in out and "3 ERROR" in out and "2 WARN" in out
    # No domain-specific message codes in generic mode -> no code table rows
    assert "distinct code(s)" in out


def test_detect_profile_tool(tmp_path):
    p = tmp_path / "app.log"
    p.write_text("[ERROR] connection refused\nE0520 controller.go:42] x\n")
    cfg = _cfg(log_path=str(p), mode="generic")
    out = build_registry().dispatch("detect_profile", {}, _ctx(cfg))
    assert "Active profile:   generic" in out
    assert "Detected profile: generic" in out


def test_detect_profile_warns_on_mismatch(tmp_path):
    # EDA mode active but the log is generic -> the tool should flag it.
    p = tmp_path / "app.log"
    p.write_text("[ERROR] connection refused\n[WARN] slow\n")
    cfg = _cfg(log_path=str(p), mode="eda")
    out = build_registry().dispatch("detect_profile", {}, _ctx(cfg))
    assert "Active profile:   eda" in out
    assert "Detected profile: generic" in out
    assert "differs from the active one" in out


def test_skills_nested_directory_discovery(tmp_path):
    # rglob: skills can live one or more levels deep.
    root = tmp_path / "skills"
    (root / "eda" / "deep").mkdir(parents=True)
    (root / "k8s" / "crashloop").mkdir(parents=True)
    (root / "eda" / "deep" / "SKILL.md").write_text(
        "---\nname: eda-deep\ndescription: x\ndomain: eda\n---\nbody\n")
    (root / "k8s" / "crashloop" / "SKILL.md").write_text(
        "---\nname: k8s-crashloop\ndescription: y\ndomain: generic\n"
        "when_to_use: pod restarts every 30s\n---\nbody\n")
    cfg = _cfg(skills_dir=str(root), mode="generic")
    out = build_registry().dispatch("list_skills", {}, _ctx(cfg))
    assert "k8s-crashloop" in out
    assert "eda-deep" not in out                  # filtered out by domain
    assert "1 skill(s) hidden" in out


def test_skills_domain_filter_hides_other_profile(tmp_path):
    root = tmp_path / "skills"
    (root / "a").mkdir(parents=True)
    (root / "b").mkdir(parents=True)
    (root / "a" / "SKILL.md").write_text(
        "---\nname: only-eda\ndescription: x\ndomain: eda\n---\nbody\n")
    (root / "b" / "SKILL.md").write_text(
        "---\nname: universal\ndescription: y\n---\nbody\n")  # no domain -> any
    # In eda mode, both visible.
    cfg = _cfg(skills_dir=str(root), mode="eda")
    out = build_registry().dispatch("list_skills", {}, _ctx(cfg))
    assert "only-eda" in out and "universal" in out
    # In generic mode, only the universal one.
    cfg = _cfg(skills_dir=str(root), mode="generic")
    out = build_registry().dispatch("list_skills", {}, _ctx(cfg))
    assert "universal" in out
    assert "only-eda" not in out


def test_skills_query_ranks_by_relevance(tmp_path):
    root = tmp_path / "skills"
    for n, desc, when in [
        ("a-oom", "diagnose container OOMKilled", "exit 137 / memory limit"),
        ("b-probe", "liveness probe restarts", "probe timeout in 30s"),
        ("c-disk", "disk pressure eviction", "PVC full, node pressure"),
    ]:
        (root / n).mkdir(parents=True)
        (root / n / "SKILL.md").write_text(
            f"---\nname: {n}\ndescription: {desc}\nwhen_to_use: {when}\n"
            f"---\nbody\n")
    cfg = _cfg(skills_dir=str(root), mode="generic")
    out = build_registry().dispatch(
        "list_skills", {"query": "container OOMKilled exit 137"}, _ctx(cfg))
    lines = [l for l in out.splitlines() if l.startswith("- ")]
    assert lines and "a-oom" in lines[0]      # top hit


def test_agent_session_context_includes_profile_guidance():
    cfg = _cfg(mode="generic")
    agent = Agent(FakeClient(), build_registry(),
                  ToolContext(cfg=cfg, manual_index=ManualIndex(cfg.manual_dir)),
                  max_steps=8)
    assert "Active profile: generic" in agent.system
    assert "PROFILE GUIDANCE" in agent.system
    assert "no eda conventions" in agent.system.lower()


def test_config_default_mode_is_eda():
    # Back-compat: existing users get EDA without setting anything.
    assert Config().mode == "eda"


def test_generic_profile_overrides_fix_with_next_steps():
    # In generic mode the agent doesn't have a domain manual; prescribing a
    # specific code/config fix would be hallucination. The prompt must steer
    # it away from a 'Fix' section toward investigative 'Next Steps'.
    cfg = _cfg(mode="generic")
    agent = Agent(FakeClient(), build_registry(),
                  ToolContext(cfg=cfg, manual_index=ManualIndex(cfg.manual_dir)),
                  max_steps=8)
    sys = agent.system
    assert "Likely Causes & Next Steps" in sys
    assert "Do NOT" in sys and "hallucination" in sys.lower()
    assert "investigate and explain" in sys.lower()


# ---- notes / durable memory ------------------------------------------------
def test_notes_save_and_get_roundtrip(tmp_path):
    cfg = _cfg(project_root=str(tmp_path))
    reg, ctx = build_registry(), _ctx(cfg)
    assert "Saved note" in reg.dispatch(
        "save_note", {"key": "root_cause",
                      "value": "top.sdc:88 uses core_clk before create_clock"},
        ctx)
    out = reg.dispatch("get_note", {"key": "root_cause"}, ctx)
    assert "top.sdc:88" in out and "# note: root_cause" in out


def test_notes_persist_across_agent_instances(tmp_path):
    # The whole point of notes is surviving process restart. Simulate that by
    # building a fresh ToolContext (the on-disk file is the only handoff).
    cfg = _cfg(project_root=str(tmp_path))
    build_registry().dispatch(
        "save_note", {"key": "open_q", "value": "is the SDC the only failure?"},
        _ctx(cfg))
    # Simulate restart: brand-new context, same cfg/project_root.
    fresh_ctx = ToolContext(cfg=cfg, manual_index=ManualIndex(cfg.manual_dir))
    out = build_registry().dispatch("get_note", {"key": "open_q"}, fresh_ctx)
    assert "SDC the only failure" in out
    # And the file is where we said it would be.
    assert (tmp_path / ".logb-notes.json").is_file()


def test_notes_list_and_delete(tmp_path):
    cfg = _cfg(project_root=str(tmp_path))
    reg, ctx = build_registry(), _ctx(cfg)
    reg.dispatch("save_note", {"key": "a", "value": "first finding"}, ctx)
    reg.dispatch("save_note", {"key": "b", "value": "second\nmultiline"}, ctx)
    out = reg.dispatch("list_notes", {}, ctx)
    assert "saved notes (2)" in out
    assert "- a" in out and "- b" in out
    assert "..." in out                              # multiline preview ellipsis
    reg.dispatch("delete_note", {"key": "a"}, ctx)
    out = reg.dispatch("list_notes", {}, ctx)
    assert "saved notes (1)" in out and "- a" not in out


def test_notes_overwrite_same_key(tmp_path):
    cfg = _cfg(project_root=str(tmp_path))
    reg, ctx = build_registry(), _ctx(cfg)
    reg.dispatch("save_note", {"key": "k", "value": "old"}, ctx)
    reg.dispatch("save_note", {"key": "k", "value": "new"}, ctx)
    assert "new" in reg.dispatch("get_note", {"key": "k"}, ctx)
    assert "old" not in reg.dispatch("get_note", {"key": "k"}, ctx)


def test_notes_rejects_bad_key(tmp_path):
    cfg = _cfg(project_root=str(tmp_path))
    reg, ctx = build_registry(), _ctx(cfg)
    out = reg.dispatch("save_note",
                       {"key": "has spaces", "value": "x"}, ctx)
    assert out.startswith("ERROR: invalid key")
    out = reg.dispatch("save_note", {"key": "", "value": "x"}, ctx)
    assert out.startswith("ERROR: `key` is required")


def test_notes_caps_value_length(tmp_path):
    from logb.tools.notes import MAX_VALUE_LEN
    cfg = _cfg(project_root=str(tmp_path))
    reg, ctx = build_registry(), _ctx(cfg)
    out = reg.dispatch("save_note",
                       {"key": "huge", "value": "x" * (MAX_VALUE_LEN + 1)}, ctx)
    assert out.startswith("ERROR: value too long")
    assert "synthesis, not raw" in out               # the guidance is surfaced


def test_notes_caps_total_keys(tmp_path):
    from logb.tools.notes import MAX_KEYS
    cfg = _cfg(project_root=str(tmp_path))
    reg, ctx = build_registry(), _ctx(cfg)
    for i in range(MAX_KEYS):
        reg.dispatch("save_note", {"key": f"k{i}", "value": "v"}, ctx)
    out = reg.dispatch("save_note", {"key": "overflow", "value": "v"}, ctx)
    assert "capped at" in out and "delete_note" in out
    # ...but updating an existing key still works at the cap.
    assert "Saved note" in reg.dispatch(
        "save_note", {"key": "k0", "value": "updated"}, ctx)


def test_notes_get_missing_lists_available(tmp_path):
    cfg = _cfg(project_root=str(tmp_path))
    reg, ctx = build_registry(), _ctx(cfg)
    reg.dispatch("save_note", {"key": "alpha", "value": "v"}, ctx)
    out = reg.dispatch("get_note", {"key": "beta"}, ctx)
    assert "not found" in out and "alpha" in out


def test_notes_empty_list_explains_purpose(tmp_path):
    cfg = _cfg(project_root=str(tmp_path))
    out = build_registry().dispatch("list_notes", {}, _ctx(cfg))
    assert "no notes saved yet" in out
    assert "save_note" in out                        # nudges the model toward it


def test_notes_survive_corrupt_file(tmp_path):
    # A garbage notes file shouldn't crash anything; just start fresh.
    cfg = _cfg(project_root=str(tmp_path))
    (tmp_path / ".logb-notes.json").write_text("{not json")
    reg, ctx = build_registry(), _ctx(cfg)
    assert "no notes saved yet" in reg.dispatch("list_notes", {}, ctx)
    assert "Saved" in reg.dispatch(
        "save_note", {"key": "x", "value": "ok"}, ctx)


def test_eda_profile_keeps_fix_template():
    # EDA mode still produces the prescriptive Fix section (manual + skills
    # back it up). Regression guard for the base template.
    cfg = _cfg(mode="eda")
    agent = Agent(FakeClient(), build_registry(),
                  ToolContext(cfg=cfg, manual_index=ManualIndex(cfg.manual_dir)),
                  max_steps=8)
    assert "## Fix" in agent.system
    assert "concrete, ordered steps" in agent.system


# ---- planning --------------------------------------------------------------
def test_create_plan_sets_tasks():
    reg, ctx = build_registry(), _ctx(_cfg())
    out = reg.dispatch("create_plan",
                       {"tasks": ["log_summary for counts",
                                  "read_logs severity=error,fatal",
                                  "search_manual for first error code"]},
                       ctx)
    assert "3 task(s)" in out
    assert "[ ] 1." in out and "[ ] 2." in out and "[ ] 3." in out
    assert ctx.plan.tasks[0].text == "log_summary for counts"


def test_create_plan_rejects_empty():
    reg, ctx = build_registry(), _ctx(_cfg())
    out = reg.dispatch("create_plan", {"tasks": []}, ctx)
    assert out.startswith("ERROR")
    out = reg.dispatch("create_plan", {}, ctx)
    assert out.startswith("ERROR")


def test_create_plan_caps_size():
    from logb.tools.plan import MAX_TASKS
    reg, ctx = build_registry(), _ctx(_cfg())
    out = reg.dispatch("create_plan",
                       {"tasks": [f"task {i}" for i in range(MAX_TASKS + 1)]},
                       ctx)
    assert "too many tasks" in out


def test_update_plan_status_and_result():
    reg, ctx = build_registry(), _ctx(_cfg())
    reg.dispatch("create_plan",
                 {"tasks": ["a", "b", "c"]}, ctx)
    out = reg.dispatch("update_plan",
                       {"idx": 1, "status": "done",
                        "result": "got counts: 2 FATAL, 4 ERROR"},
                       ctx)
    assert "status=done" in out
    assert ctx.plan.tasks[0].status == "done"
    assert "got counts" in ctx.plan.tasks[0].result
    show = reg.dispatch("show_plan", {}, ctx)
    assert "[✓] 1." in show and "[ ] 2." in show


def test_update_plan_add_tasks():
    reg, ctx = build_registry(), _ctx(_cfg())
    reg.dispatch("create_plan", {"tasks": ["a"]}, ctx)
    out = reg.dispatch("update_plan",
                       {"add_tasks": ["b", "c"]}, ctx)
    assert "Added 2 task(s)" in out
    assert [t.text for t in ctx.plan.tasks] == ["a", "b", "c"]
    assert [t.idx for t in ctx.plan.tasks] == [1, 2, 3]


def test_update_plan_unknown_idx():
    reg, ctx = build_registry(), _ctx(_cfg())
    reg.dispatch("create_plan", {"tasks": ["a"]}, ctx)
    out = reg.dispatch("update_plan",
                       {"idx": 99, "status": "done"}, ctx)
    assert "no task with idx 99" in out


def test_update_plan_requires_existing_plan():
    reg, ctx = build_registry(), _ctx(_cfg())
    out = reg.dispatch("update_plan", {"idx": 1, "status": "done"}, ctx)
    assert "no plan exists" in out


def test_show_plan_empty_says_so():
    reg, ctx = build_registry(), _ctx(_cfg())
    out = reg.dispatch("show_plan", {}, ctx)
    assert "no plan set" in out


def test_plan_visible_in_system_prompt():
    """The whole point: the plan must appear in the system context so the
    model sees it on every step, even after history compaction."""
    from logb.agent import _build_system_prompt
    ctx = _ctx(_cfg())
    build_registry().dispatch("create_plan",
                              {"tasks": ["scan errors", "open the SDC"]}, ctx)
    build_registry().dispatch("update_plan",
                              {"idx": 1, "status": "done",
                               "result": "2 errors found"}, ctx)
    sys_prompt = _build_system_prompt(ctx)
    assert "CURRENT PLAN" in sys_prompt
    assert "1 done / 2 total" in sys_prompt
    assert "scan errors" in sys_prompt and "open the SDC" in sys_prompt
    assert "[✓] 1." in sys_prompt and "[ ] 2." in sys_prompt
    assert "2 errors found" in sys_prompt          # the recorded result


def test_plan_resets_between_asks():
    """Plans are per-ask: a stale plan from question 1 must not leak into
    question 2's strategy. Durable cross-question state goes in notes."""
    cfg = _cfg()
    ctx = _ctx(cfg)

    class Quick:
        def __init__(self):
            self.n = 0
        def chat(self, s, h, t, on_token=None):
            self.n += 1
            return Assistant(text="ok")

    agent = Agent(Quick(), build_registry(), ctx, max_steps=2)
    # Pre-populate a plan as if a previous turn ran.
    build_registry().dispatch("create_plan",
                              {"tasks": ["leftover task"]}, ctx)
    assert len(ctx.plan.tasks) == 1
    agent.ask("new question")
    assert ctx.plan.tasks == []                    # cleared at ask() start


def test_plan_disappears_from_prompt_when_empty():
    from logb.agent import _build_system_prompt
    ctx = _ctx(_cfg())
    sys_prompt = _build_system_prompt(ctx)
    assert "CURRENT PLAN" not in sys_prompt        # no noise when no plan


# ---- streaming -------------------------------------------------------------
class StreamingFakeClient:
    """Plan-replaying fake that also drives `on_token` like a real backend."""

    def __init__(self, plan):
        self.plan = plan
        self.i = 0
        self.token_calls: list[str] = []

    def chat(self, system, history, tools, on_token=None):
        a = self.plan[self.i]
        self.i += 1
        if on_token and a.text:
            # Simulate a streaming backend chunking the text into a few pieces.
            for piece in (a.text[:5], a.text[5:15], a.text[15:]):
                if piece:
                    on_token(piece)
        return a


def test_streaming_callback_receives_tokens():
    tokens: list[str] = []
    cfg = _cfg()
    plan = [Assistant(text="## Root Cause\nthe SDC is broken at top.sdc:88.")]
    agent = Agent(StreamingFakeClient(plan), build_registry(), _ctx(cfg),
                  max_steps=2, on_token=tokens.append)
    res = agent.ask("why?")
    assert "the SDC is broken" in res.answer
    # The chunks emitted by the fake are concatenated into the same answer.
    assert "".join(tokens).startswith("## Root Cause")
    assert "the SDC is broken" in "".join(tokens)


def test_non_streaming_path_still_works():
    # FakeClient (no `on_token` kwarg) must keep working — back-compat for
    # any external test or harness built on the old chat signature.
    res = Agent(FakeClient(), build_registry(),
                _ctx(_cfg(verify_citations=False)), max_steps=8).ask("crash?")
    assert "IMPSDC-3071" in res.answer


# ---- history compaction ----------------------------------------------------
def test_compact_threshold_defaults_from_num_ctx():
    from logb.agent import Agent as _A
    cfg = _cfg(num_ctx=4096)
    agent = _A(FakeClient(), build_registry(), _ctx(cfg), max_steps=4)
    assert agent._compact_threshold() == int(4096 * 2.5)


def test_compact_tool_result_keeps_head_and_tail():
    from logb.agent import _compact_tool_result
    raw = "HEADER LINE\n" + ("filler\n" * 500) + "FOOTER LINE\n"
    out = _compact_tool_result(raw, 400)
    assert out.startswith("[compacted")
    assert "HEADER LINE" in out and "FOOTER LINE" in out
    assert len(out) < len(raw) // 4                  # actually compressed
    # Idempotent — re-compacting doesn't keep stacking markers.
    again = _compact_tool_result(out, 400)
    assert again == out


def test_history_compaction_triggers_on_long_chat():
    from logb.agent import _history_bytes
    cfg = _cfg(num_ctx=1024, history_compact_keep_recent=1,
               history_compact_budget=200, verify_citations=False)
    # Build a client that returns one tool call per step, then a final answer.
    class Many:
        def __init__(self):
            self.n = 0
        def chat(self, s, h, t, on_token=None):
            self.n += 1
            if self.n <= 6:
                return Assistant(tool_calls=[{"id": f"c{self.n}",
                                              "name": "list_logs", "args": {}}])
            return Assistant(text="done")
    agent = Agent(Many(), build_registry(), _ctx(cfg), max_steps=10)
    # Stuff history with fat tool results to force compaction to engage.
    agent.history = [
        {"role": "tool", "id": f"x{i}", "name": "read_logs",
         "result": "x" * 3000} for i in range(6)
    ]
    before = _history_bytes(agent.history)
    agent._compact_history_if_needed()
    after = _history_bytes(agent.history)
    assert after < before // 2          # at least halved
    # The most recent tool result is preserved at full size.
    assert len(agent.history[-1]["result"]) == 3000
    # The older ones are now compacted.
    assert "[compacted" in agent.history[0]["result"]


def test_compaction_skipped_under_threshold():
    cfg = _cfg(num_ctx=8192, verify_citations=False)
    agent = Agent(FakeClient(), build_registry(), _ctx(cfg), max_steps=4)
    agent.history = [
        {"role": "tool", "id": "1", "name": "read_logs", "result": "short"}]
    agent._compact_history_if_needed()
    assert agent.history[0]["result"] == "short"     # untouched


# ---- citation verification -------------------------------------------------
def test_extract_cites_dedupes_and_finds_paths():
    from logb.agent import _extract_cites
    txt = ("see `top.sdc:88` and also top.sdc:88 (same), "
           "plus manual/foo.md:12 and logs/run.log:4213.")
    cites = _extract_cites(txt)
    assert ("top.sdc", 88) in cites
    assert ("manual/foo.md", 12) in cites
    assert ("logs/run.log", 4213) in cites
    assert len(cites) == 3                            # deduped


def test_extract_cites_accepts_prose_form():
    # The model often writes "<path> line N" or "<path> on line N" instead
    # of the path:line shape the prompt asks for. Verification must catch
    # those too — otherwise a Mode-C answer with prose-form cites would
    # slip through with no checking at all.
    from logb.agent import _extract_cites
    txt = ("see `top.sdc` line 88 for the bug, and `manual/foo.md` on "
           "line 12 explains it. The actual route is at "
           "`scripts/cts.tcl`:3.")
    cites = _extract_cites(txt)
    assert ("top.sdc", 88) in cites
    assert ("manual/foo.md", 12) in cites
    assert ("scripts/cts.tcl", 3) in cites


def test_verify_citations_finds_real_file():
    from logb.agent import _verify_citations
    cfg = _cfg()
    cites = _verify_citations(
        "## Evidence\nsee scripts/constraints/top.sdc:88 for the bug",
        cfg)
    assert len(cites) == 1
    assert cites[0]["ok"]
    assert "core_clk" in cites[0]["content"]          # actual line 88 content


def test_verify_citations_flags_bad_file():
    from logb.agent import _verify_citations
    cfg = _cfg()
    cites = _verify_citations(
        "## Evidence\nsee nonexistent/path/to.sdc:1 for the bug", cfg)
    assert cites and not cites[0]["ok"]
    assert cites[0]["reason"] == "file not found"


def test_verify_citations_flags_line_out_of_range():
    from logb.agent import _verify_citations
    cfg = _cfg()
    cites = _verify_citations(
        "## Evidence\nsee scripts/constraints/top.sdc:99999 for the bug",
        cfg)
    assert cites and not cites[0]["ok"]
    assert cites[0]["reason"] == "line out of range"


def test_verification_reasks_when_cites_are_bad():
    """End-to-end: the agent emits a draft with a bad cite, verification
    fires, a revision LLM call is made WITH TOOLS AVAILABLE (so the model
    can actually re-look-up evidence), and the revised answer is returned."""
    class TwoStage:
        def __init__(self):
            self.calls = []
        def chat(self, s, h, t, on_token=None):
            self.calls.append({"tools": bool(t), "len": len(h)})
            if len(self.calls) == 1:                  # first answer (draft)
                return Assistant(
                    text="## Root Cause\nthe SDC is broken at fakefile.sdc:42.")
            # Second call: verification feedback in history -> emit revised
            return Assistant(
                text="## Root Cause\nrevised: cannot pin to a single file:line.")
    cli = TwoStage()
    cfg = _cfg(verify_citations=True)
    res = Agent(cli, build_registry(), _ctx(cfg), max_steps=2).ask("why?")
    assert len(cli.calls) == 2                        # one draft, one revise
    assert "revised" in res.answer
    # The revision call had tools available — the previous design that
    # withheld them was the bug that produced uncorroborated revisions.
    assert cli.calls[1]["tools"] is True
    assert cli.calls[1]["len"] > cli.calls[0]["len"]  # feedback added


def test_verification_skipped_when_cites_all_resolve():
    class OneShot:
        def __init__(self):
            self.calls = 0
        def chat(self, s, h, t, on_token=None):
            self.calls += 1
            return Assistant(
                text="## Root Cause\nsee scripts/constraints/top.sdc:88")
    cli = OneShot()
    cfg = _cfg(verify_citations=True)
    Agent(cli, build_registry(), _ctx(cfg), max_steps=2).ask("why?")
    assert cli.calls == 1                             # no re-ask needed


def test_verification_opt_out():
    class Drafter:
        def __init__(self):
            self.calls = 0
        def chat(self, s, h, t, on_token=None):
            self.calls += 1
            return Assistant(text="## Root Cause\nfakefile.sdc:42 broke it.")
    cli = Drafter()
    cfg = _cfg(verify_citations=False)
    Agent(cli, build_registry(), _ctx(cfg), max_steps=2).ask("why?")
    assert cli.calls == 1                             # opt-out honored


def test_verify_manual_refs_resolves_real_file(tmp_path):
    from logb.agent import _verify_manual_refs
    mdir = tmp_path / "manual"
    mdir.mkdir()
    (mdir / "innovus_errors.md").write_text("## IMPSDC-3071\nfoo")
    cfg = _cfg(manual_dir=str(mdir))
    answer = "see manual/innovus_errors.md for the explanation."
    out = _verify_manual_refs(answer, cfg)
    assert len(out) == 1 and out[0]["ok"]


def test_verify_manual_refs_flags_hallucinated_path(tmp_path):
    from logb.agent import _verify_manual_refs
    mdir = tmp_path / "manual"
    mdir.mkdir()
    cfg = _cfg(manual_dir=str(mdir))
    # The 7B failure mode: invented techlib/impex/impex_4022.txt path that
    # has no backing file under the manual dir.
    answer = ("per manual:techlib/impex/impex_4022.txt the cause is X; "
              "also see manual/implf/implf_213.txt")
    out = _verify_manual_refs(answer, cfg)
    assert len(out) == 2 and not any(o["ok"] for o in out)
    assert "no such file" in out[0]["reason"]


def test_claims_manual_without_calling_it():
    from logb.agent import _claims_manual_without_calling_it
    # Triggers — claim language present, no search_manual in tools_used.
    assert _claims_manual_without_calling_it(
        "I will reference the manual to explain.", set())
    assert _claims_manual_without_calling_it(
        "According to the manual, X is required.", set())
    assert _claims_manual_without_calling_it(
        "Per the manual, see section 3.", {"read_logs"})
    # Does NOT trigger — search_manual was actually called.
    assert not _claims_manual_without_calling_it(
        "According to the manual, X is required.", {"search_manual"})
    # Does NOT trigger — no manual-claim language.
    assert not _claims_manual_without_calling_it(
        "## Root Cause\nthe SDC is broken.", set())


def test_reasks_when_manual_claimed_without_tool_call():
    """Canonical 7B failure: 'I will reference the manual' followed by
    fabricated content, with no actual search_manual call. The agent must
    detect that and force a revision."""
    class Hallucinator:
        def __init__(self):
            self.calls = 0
        def chat(self, s, h, t, on_token=None):
            self.calls += 1
            if self.calls == 1:
                # Mode C answer that claims to consult the manual but
                # makes zero tool calls in this turn.
                return Assistant(
                    text="## Root Cause\nper the manual, the SDC is bad.")
            return Assistant(text="## Root Cause\nthe SDC is bad (no manual cite).")
    cli = Hallucinator()
    cfg = _cfg(verify_citations=True)
    res = Agent(cli, build_registry(), _ctx(cfg), max_steps=2).ask("why?")
    assert cli.calls == 2                           # verification re-asked
    assert "no manual cite" in res.answer            # revised answer wins


def test_verification_loops_until_clean_or_budget():
    """Multi-pass verification: keep re-asking while problems remain, up
    to verify_max_passes. Previously verification only ran once, so a
    revised answer with NEW hallucinations slipped through unchecked."""
    class Persistent:
        """Returns a hallucinated answer on every call until pass N."""
        def __init__(self, clean_after):
            self.clean_after = clean_after
            self.calls = 0
        def chat(self, s, h, t, on_token=None):
            self.calls += 1
            if self.calls < self.clean_after:
                # Different made-up cite each time — exercises the loop.
                return Assistant(
                    text=f"## Root Cause\nbroken at fakefile{self.calls}.sdc:42.")
            return Assistant(
                text="## Root Cause\nthe SDC is broken; no exact cite available.")
    cli = Persistent(clean_after=3)
    cfg = _cfg(verify_citations=True, verify_max_passes=4)
    res = Agent(cli, build_registry(), _ctx(cfg), max_steps=2).ask("why?")
    # 1 draft + 2 revisions until clean = 3 calls total
    assert cli.calls == 3
    assert "no exact cite" in res.answer


def test_verification_surfaces_problems_when_budget_exhausted():
    """If the model keeps hallucinating past verify_max_passes, the agent
    must NOT silently return a still-broken answer — it surfaces the
    residual problems so the user sees the verifier didn't trust it."""
    class Stubborn:
        def __init__(self):
            self.calls = 0
        def chat(self, s, h, t, on_token=None):
            self.calls += 1
            return Assistant(text="## Root Cause\nbroken at imaginary.sdc:42.")
    cli = Stubborn()
    cfg = _cfg(verify_citations=True, verify_max_passes=2)
    res = Agent(cli, build_registry(), _ctx(cfg), max_steps=2).ask("why?")
    assert cli.calls == 2          # draft + one revision; both bad
    assert "could not be fully verified" in res.answer
    assert "imaginary.sdc" in res.answer  # the residual problem is named
    assert "stronger model" in res.answer  # nudge toward Anthropic


def test_no_reask_when_manual_actually_called():
    """If search_manual was invoked in this turn, the 'claims manual' check
    must NOT fire — the model legitimately consulted the manual."""
    class Honest:
        def __init__(self):
            self.calls = 0
        def chat(self, s, h, t, on_token=None):
            self.calls += 1
            if self.calls == 1:
                return Assistant(tool_calls=[{"id": "1",
                                              "name": "search_manual",
                                              "args": {"query": "IMPSDC-3071"}}])
            return Assistant(
                text="## Root Cause\nper the manual, see "
                     "manual/innovus_errors.md for IMPSDC-3071.")
    cli = Honest()
    cfg = _cfg(verify_citations=True)
    res = Agent(cli, build_registry(), _ctx(cfg), max_steps=4).ask("why?")
    # Two LLM calls (decide tool, then answer) — but NOT a verification
    # re-ask. The bundled manual/innovus_errors.md exists, so manual_refs
    # also passes.
    assert cli.calls == 2
    assert "per the manual" in res.answer


def test_verification_skipped_outside_mode_c():
    class Counter:
        def __init__(self):
            self.calls = 0
        def chat(self, s, h, t, on_token=None):
            self.calls += 1
            # Mode A answer (no ## Root Cause header) — verification must not
            # fire even if it mentions a path:line that doesn't exist.
            return Assistant(text="3 ERROR, 0 FATAL, 5 WARN at nope.log:1.")
    cli = Counter()
    cfg = _cfg(verify_citations=True)
    Agent(cli, build_registry(), _ctx(cfg), max_steps=2).ask("how many?")
    assert cli.calls == 1                             # no re-ask outside Mode C


if __name__ == "__main__":
    import subprocess
    raise SystemExit(subprocess.call(
        [sys.executable, "-m", "pytest", "-q", __file__]))
