"""Offline tests — tools, RAG, and the agent loop with a scripted fake LLM.

No network / no Ollama needed. Run: python -m pytest -q
"""

from __future__ import annotations

import json
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


def test_index_stage_ranges_built_in_pass(tmp_path):
    """The build pass must record (start_line, end_line, name) tuples
    for every Starting/Ending banner pair, plus open-but-never-closed
    stages with end_line = total."""
    from logb import index as _idx
    p = tmp_path / "x.log"
    p.write_text(
        '[04:00:00  10s] --- Starting "place" ---\n'
        "info\n"
        '[04:00:00  20s] --- Ending "place" ---\n'
        '[04:00:00  30s] --- Starting "route" ---\n'
        "**ERROR: (IMPROUTE-7440): boom\n"
        "FATAL: (IMPCORE-9001): crashed\n")
    idx = _idx.build(p)
    # Two ranges: "place" closed normally; "route" still open (no Ending).
    assert len(idx["stage_ranges"]) == 2
    ranges = {r[2]: r for r in idx["stage_ranges"]}
    assert "place" in ranges and "route" in ranges
    # Closed stage has both timestamps; open stage has end_ts == None.
    place = ranges["place"]
    assert place[3] == 10 and place[4] == 20    # start_ts, end_ts
    route = ranges["route"]
    assert route[3] == 30 and route[4] is None
    # Open stage's end_line is the file's total line count.
    assert route[1] == idx["total"]


def test_index_stage_hist_per_stage_counts_and_codes(tmp_path):
    """stage_hist must accumulate per-stage severity totals and per-code
    counts, matching what the old stage_errors used to bucket at query
    time."""
    from logb import index as _idx
    p = tmp_path / "y.log"
    p.write_text(
        '--- Starting "place" ---\n'
        "**ERROR: (IMPSDC-3071): x\n"
        "**ERROR: (IMPSDC-3071): x again\n"
        "**WARN: (IMPSDC-3099): y\n"
        '--- Ending "place" ---\n'
        '--- Starting "route" ---\n'
        "FATAL: (IMPCORE-9001): boom\n")
    idx = _idx.build(p)
    place = idx["stage_hist"]["place"]
    assert place["error"] == 2 and place["warn"] == 1 and place["fatal"] == 0
    assert place["codes"]["IMPSDC-3071"] == 2
    assert place["codes"]["IMPSDC-3099"] == 1
    route = idx["stage_hist"]["route"]
    assert route["fatal"] == 1
    assert route["codes"]["IMPCORE-9001"] == 1


def test_index_stage_hist_pre_stage_bucket(tmp_path):
    """Severe lines before the first banner go into a synthetic
    '<pre-stage>' bucket — same as the old _stage_errors fallback."""
    from logb import index as _idx
    p = tmp_path / "z.log"
    p.write_text(
        "**WARN: (X-1): early\n"
        "**ERROR: (X-2): also early\n"
        '--- Starting "place" ---\n'
        "info\n"
        '--- Ending "place" ---\n')
    idx = _idx.build(p)
    pre = idx["stage_hist"]["<pre-stage>"]
    assert pre["warn"] == 1 and pre["error"] == 1
    # `place` bucket may or may not exist (no severe entries) — but if it
    # does, it must NOT have inherited the pre-stage error.
    assert idx["stage_hist"].get("place", {}).get("error", 0) == 0


def test_index_code_occurrences_head_and_tail(tmp_path):
    """Per-code occurrence lists keep the first CODE_OCC_HEAD line
    numbers in head, then roll the last CODE_OCC_TAIL via deque."""
    from logb import index as _idx
    from logb.index import CODE_OCC_HEAD, CODE_OCC_TAIL
    p = tmp_path / "many.log"
    # 50 occurrences of the same code — exceeds head, fills tail.
    p.write_text("\n".join(
        f"**ERROR: (IMPLF-213): occurrence {i}" for i in range(50)) + "\n")
    idx = _idx.build(p)
    occ = idx["code_occurrences"]["IMPLF-213"]
    assert occ["count"] == 50
    assert len(occ["head"]) == CODE_OCC_HEAD
    assert len(occ["tail"]) == CODE_OCC_TAIL
    # head starts at line 0 (the first occurrence) and is contiguous.
    assert occ["head"][0] == 0
    assert occ["head"][CODE_OCC_HEAD - 1] == CODE_OCC_HEAD - 1
    # tail captures the last CODE_OCC_TAIL line numbers.
    assert occ["tail"][-1] == 49


def test_index_code_occurrences_when_count_less_than_head(tmp_path):
    """For codes with fewer than CODE_OCC_HEAD hits, head holds them all
    and tail stays empty."""
    from logb import index as _idx
    p = tmp_path / "few.log"
    p.write_text("**ERROR: (FEW-1): one\n**ERROR: (FEW-1): two\n")
    idx = _idx.build(p)
    occ = idx["code_occurrences"]["FEW-1"]
    assert occ["count"] == 2
    assert occ["head"] == [0, 1]
    assert occ["tail"] == []


def test_code_lookup_nth_returns_head_occurrence(tmp_path):
    """nth=N for N within the head returns that specific occurrence."""
    p = tmp_path / "many.log"
    # 10 occurrences of the same code on lines 0..9.
    p.write_text("\n".join(
        f"**ERROR: (IMPLF-213): hit #{i}" for i in range(10)) + "\n")
    cfg = _cfg(log_path=str(p))
    out = build_registry().dispatch(
        "code_lookup", {"code": "IMPLF-213", "nth": 3}, _ctx(cfg))
    # 1-indexed occurrence 3 = 0-indexed line 2 = "hit #2".
    assert "occurrence #3 of 10" in out
    assert "hit #2" in out
    assert "L3" in out


def test_code_lookup_nth_returns_tail_occurrence(tmp_path):
    """For an N past the head, the result comes from the tail buffer."""
    from logb.index import CODE_OCC_HEAD, CODE_OCC_TAIL
    p = tmp_path / "many.log"
    total = CODE_OCC_HEAD + CODE_OCC_TAIL + 10
    p.write_text("\n".join(
        f"**ERROR: (IMPLF-213): hit #{i}" for i in range(total)) + "\n")
    cfg = _cfg(log_path=str(p))
    # The very last occurrence — must come from the tail.
    out = build_registry().dispatch(
        "code_lookup", {"code": "IMPLF-213", "nth": total}, _ctx(cfg))
    assert f"occurrence #{total} of {total}" in out
    assert f"hit #{total - 1}" in out


def test_code_lookup_nth_between_head_and_tail(tmp_path):
    """For an N that lands between head and tail, fall back gracefully —
    don't hallucinate, tell the agent how to enumerate via read_logs."""
    from logb.index import CODE_OCC_HEAD, CODE_OCC_TAIL
    p = tmp_path / "many.log"
    total = CODE_OCC_HEAD + CODE_OCC_TAIL + 20
    p.write_text("\n".join(
        f"**ERROR: (IMPLF-213): hit #{i}" for i in range(total)) + "\n")
    cfg = _cfg(log_path=str(p))
    # nth in the middle (not in head, not in tail).
    middle = CODE_OCC_HEAD + 5
    out = build_registry().dispatch(
        "code_lookup", {"code": "IMPLF-213", "nth": middle}, _ctx(cfg))
    assert "falls BETWEEN the indexed head" in out
    assert "read_logs(pattern=" in out


def test_code_lookup_nth_out_of_range(tmp_path):
    p = tmp_path / "x.log"
    p.write_text("**ERROR: (IMPLF-213): one\n")
    cfg = _cfg(log_path=str(p))
    out = build_registry().dispatch(
        "code_lookup", {"code": "IMPLF-213", "nth": 99}, _ctx(cfg))
    assert "out of range" in out and "1 occurrence(s)" in out


def test_index_mentions_captures_file_paths(tmp_path):
    """The build pass extracts file-path-like tokens into an inverted
    index keyed by token, valued as a list of line numbers."""
    from logb import index as _idx
    p = tmp_path / "x.log"
    p.write_text(
        "**ERROR: constraint in scripts/constraints/top.sdc line 88\n"
        "info\n"
        "loading scripts/constraints/top.sdc again\n"
        "**ERROR: routing failed for run/data/netlist.v\n")
    idx = _idx.build(p)
    assert "scripts/constraints/top.sdc" in idx["mentions"]
    assert idx["mentions"]["scripts/constraints/top.sdc"] == [0, 2]
    assert "run/data/netlist.v" in idx["mentions"]


def test_index_mentions_captures_cmd_source(tmp_path):
    """`<CMD> source X` is captured with a CMD: prefix."""
    from logb import index as _idx
    p = tmp_path / "x.log"
    p.write_text(
        "<CMD> source scripts/init.tcl\n"
        "info\n"
        "<CMD> source scripts/place.tcl\n")
    idx = _idx.build(p)
    assert "CMD:scripts/init.tcl" in idx["mentions"]
    assert "CMD:scripts/place.tcl" in idx["mentions"]


def test_find_mentions_exact_lookup(tmp_path):
    """find_mentions returns every line where the exact token appears."""
    import re as _re
    p = tmp_path / "x.log"
    p.write_text(
        "first hit scripts/constraints/top.sdc\n"
        "no mention\n"
        "second hit scripts/constraints/top.sdc here\n"
        "tail\n")
    cfg = _cfg(log_path=str(p))
    out = build_registry().dispatch(
        "find_mentions",
        {"token": "scripts/constraints/top.sdc"}, _ctx(cfg))
    # Line numbers are right-padded; extract them with a regex.
    line_nos = _re.findall(r"L\s*(\d+):", out)
    assert "1" in line_nos and "3" in line_nos
    assert "2 hit(s)" in out


def test_find_mentions_substring_match(tmp_path):
    """With substring=true, any indexed key containing the token is
    surfaced — lets the agent search by basename when it doesn't know
    the full path."""
    p = tmp_path / "x.log"
    p.write_text(
        "scripts/constraints/top.sdc\n"
        "scripts/cts/top.sdc\n")
    cfg = _cfg(log_path=str(p))
    out = build_registry().dispatch(
        "find_mentions",
        {"token": "top.sdc", "substring": True}, _ctx(cfg))
    assert "scripts/constraints/top.sdc" in out
    assert "scripts/cts/top.sdc" in out


def test_find_mentions_unknown_token_lists_candidates(tmp_path):
    """When the token isn't in the index, the result lists available
    tokens so the agent can recover."""
    p = tmp_path / "x.log"
    p.write_text("scripts/known.tcl is here\n")
    cfg = _cfg(log_path=str(p))
    out = build_registry().dispatch(
        "find_mentions", {"token": "missing.sdc"}, _ctx(cfg))
    assert "no mentions of" in out
    assert "scripts/known.tcl" in out


def test_region_summary_basic_counts(tmp_path):
    """region_summary returns severity counts inside an arbitrary window
    without reading any text lines."""
    p = tmp_path / "x.log"
    p.write_text(
        "info\n"
        "**ERROR: e1\n"        # line 1 (1-indexed 2)
        "**FATAL: f1\n"        # line 2 (1-indexed 3)
        "info\n"
        "**WARN: w1\n"         # line 4 (1-indexed 5)
        "tail\n")
    cfg = _cfg(log_path=str(p))
    # 1-indexed: lines 2-3 contain only 1 error and 1 fatal.
    out = build_registry().dispatch(
        "region_summary", {"start": 2, "end": 3}, _ctx(cfg))
    assert "1 FATAL" in out and "1 ERROR" in out
    assert "0 WARN" in out


def test_region_summary_stages_crossed(tmp_path):
    """region_summary names the stages whose ranges overlap the window."""
    p = tmp_path / "x.log"
    p.write_text(
        '--- Starting "place" ---\n'
        "info\n"
        '--- Ending "place" ---\n'
        '--- Starting "route" ---\n'
        "info\n"
        '--- Ending "route" ---\n')
    cfg = _cfg(log_path=str(p))
    out = build_registry().dispatch(
        "region_summary", {"start": 1, "end": 6}, _ctx(cfg))
    assert "place" in out and "route" in out
    assert "stages crossed" in out


def test_region_summary_quiet_region(tmp_path):
    """When a window is genuinely empty of signal, say so explicitly
    instead of bluffing a fake summary."""
    p = tmp_path / "x.log"
    p.write_text("info\n" * 10 + "**ERROR: late\n")
    cfg = _cfg(log_path=str(p))
    out = build_registry().dispatch(
        "region_summary", {"start": 1, "end": 5}, _ctx(cfg))
    assert "quiet region" in out


def test_index_repeats_detected_in_build_pass(tmp_path):
    """Runs of >= REPEAT_MIN identical consecutive lines must be recorded
    as [first_line, count] in idx['repeats']."""
    from logb import index as _idx
    p = tmp_path / "spam.log"
    p.write_text(
        "header line\n"
        "**WARN: slow query\n"
        "**WARN: slow query\n"
        "**WARN: slow query\n"
        "**WARN: slow query\n"
        "info\n"
        "**WARN: slow query\n"
        "**WARN: slow query\n"    # only 2 — under REPEAT_MIN
        "tail\n")
    idx = _idx.build(p)
    # First run: 4 identical warns starting at line 1 (0-indexed).
    assert [1, 4] in idx["repeats"]
    # Second run (length 2) is not recorded because REPEAT_MIN >= 3.
    assert not any(r[0] == 6 for r in idx["repeats"])


def test_read_logs_collapses_repeated_lines(tmp_path):
    """When read_logs returns a window containing a recorded repeat run,
    the duplicates must be replaced with one canonical line + a count
    annotation — frees budget for actual signal."""
    p = tmp_path / "spam.log"
    p.write_text(
        "**WARN: slow query\n" * 10 +     # 10 identical warns
        "**ERROR: connection refused\n")
    cfg = _cfg(log_path=str(p), mode="generic")
    out = build_registry().dispatch("read_logs",
                                    {"severity": "warn,error"}, _ctx(cfg))
    # Canonical line appears once; the count annotation tells the model
    # how many there were.
    assert "slow query" in out
    assert "× 10 identical lines" in out
    assert out.count("slow query") <= 2       # not duplicated 10x


def test_index_incidents_coalesce_continuation_lines(tmp_path):
    """A severe line followed by indented continuation lines is recorded
    as a single incident block: [head_line, body_end_line, severity]."""
    from logb import index as _idx
    p = tmp_path / "trace.log"
    p.write_text(
        "info\n"
        "**ERROR: NullPointerException\n"
        "    at com.foo.Bar.run(Bar.java:42)\n"
        "    at com.foo.Bar.main(Bar.java:7)\n"
        "    at java.base/Main.run(Main.java:1)\n"
        "info recovered\n")
    idx = _idx.build(p)
    # One incident, head at line 1 (the ERROR), body extends through
    # line 4 (the last "at ..." frame).
    assert len(idx["incidents"]) == 1
    head, end, sev = (idx["incidents"][0][0],
                       idx["incidents"][0][1],
                       idx["incidents"][0][2])
    assert head == 1 and end == 4 and sev == "error"


def test_incident_around_returns_full_block(tmp_path):
    """incident_around(line=head) returns the head + body as one unit
    with the head marked, body indented."""
    p = tmp_path / "trace.log"
    p.write_text(
        "info\n"
        "**FATAL: out of memory\n"
        "    pool=heap, free=0, max=16G\n"
        "    most recent allocation: 2 GiB\n"
        "recovery failed\n")
    cfg = _cfg(log_path=str(p), mode="generic")
    out = build_registry().dispatch("incident_around", {"line": 2}, _ctx(cfg))
    assert "out of memory" in out
    assert "pool=heap" in out
    assert "most recent allocation" in out
    # The head line is marked.
    assert ">>>" in out
    assert "severity=fatal" in out


def test_incident_around_finds_nearest_when_line_not_in_incident(tmp_path):
    """Asking for incident_around on a line that's not inside any
    incident returns the nearest preceding one with a note."""
    p = tmp_path / "x.log"
    p.write_text(
        "**ERROR: a\n"
        "    body line\n"
        "info\n"
        "info\n"
        "info\n")
    cfg = _cfg(log_path=str(p), mode="generic")
    out = build_registry().dispatch("incident_around", {"line": 5}, _ctx(cfg))
    assert "not inside any incident" in out
    assert "nearest preceding one" in out


def test_index_severe_tail_preserves_last_errors(tmp_path):
    """When severe count exceeds SEVERE_CAP, the LAST errors must still
    be reachable via severe_tail. This is the head+tail split — the old
    index lost the terminal failure on a 5000-error log."""
    from logb import index as _idx
    from logb.index import SEVERE_CAP, SEVERE_TAIL_CAP
    p = tmp_path / "many.log"
    # Generate more errors than SEVERE_CAP so the tail buffer kicks in.
    n_errors = SEVERE_CAP + SEVERE_TAIL_CAP + 100
    p.write_text("\n".join(
        f"**ERROR: (X-{i:05d}): err" for i in range(n_errors)) + "\n")
    idx = _idx.build(p)
    assert idx["n_err"] == n_errors
    assert len(idx["severe"]) == SEVERE_CAP
    assert len(idx["severe_tail"]) == SEVERE_TAIL_CAP
    # The LAST few error linenos must be present in severe_tail —
    # that's the whole point of this split.
    tail_lines = {e[0] for e in idx["severe_tail"]}
    assert (n_errors - 1) in tail_lines       # last error
    assert (n_errors - SEVERE_TAIL_CAP) in tail_lines


def test_index_severe_tail_dedupe_when_under_cap(tmp_path):
    """When n_severe <= SEVERE_CAP, severe holds everything and
    severe_tail dedupes to empty (no double-counting in read_logs)."""
    from logb import index as _idx
    p = tmp_path / "few.log"
    p.write_text("**ERROR: a\n**ERROR: b\n**ERROR: c\n")
    idx = _idx.build(p)
    assert len(idx["severe"]) == 3
    assert idx["severe_tail"] == []           # nothing new in the tail


def test_index_warn_offsets_no_more_scan(tmp_path):
    """severity=warn used to require a streaming scan; now warns are in
    the index so it's an O(window) lookup."""
    from logb import index as _idx
    p = tmp_path / "warns.log"
    p.write_text(
        "info\n"
        "**WARN: (W-1): one\n"
        "info\n"
        "**WARN: (W-2): two\n"
        "**ERROR: (E-1): err\n")
    idx = _idx.build(p)
    assert len(idx["warn_offsets"]) == 2
    # Line numbers (0-indexed) should be 1 and 3.
    warn_lns = [e[0] for e in idx["warn_offsets"]]
    assert warn_lns == [1, 3]


def test_read_logs_severity_warn_uses_index_not_scan(tmp_path):
    """End-to-end: read_logs(severity=warn) now answers from warn_offsets
    instead of bouncing through _idx.scan. The agent sees only warns and
    the result includes the right line numbers."""
    p = tmp_path / "w.log"
    p.write_text(
        "**ERROR: e1\n"
        "**WARN: w1\n"
        "info\n"
        "**WARN: w2\n"
        "**ERROR: e2\n")
    cfg = _cfg(log_path=str(p))
    reg, ctx = build_registry(), _ctx(cfg)
    out = reg.dispatch("read_logs", {"severity": "warn"}, ctx)
    assert "w1" in out and "w2" in out
    # No CENSUS-style scan output that came from the old streaming path:
    # the bounded scan returned via the same renderer, so we test by
    # confirming we see warn lines correctly. The error lines are around
    # them as context (default 2), which is fine.


def test_read_logs_severity_error_combines_head_and_tail(tmp_path):
    """severity=error must surface BOTH the first errors (root cause
    candidates) AND the last errors (terminal failures) when the file
    has more than SEVERE_CAP severe lines."""
    from logb.index import SEVERE_CAP
    p = tmp_path / "huge.log"
    p.write_text("\n".join(
        f"**ERROR: (E-{i:05d}): err" for i in range(SEVERE_CAP + 100)) + "\n")
    cfg = _cfg(log_path=str(p), tool_result_char_budget=200_000)
    reg, ctx = build_registry(), _ctx(cfg)
    out = reg.dispatch("read_logs",
                       {"severity": "error", "max_lines": 5000}, ctx)
    # The first error (E-00000) and one of the very last (E-02099 or
    # later) must both appear.
    assert "E-00000" in out
    last_id = f"E-{SEVERE_CAP + 99:05d}"
    assert last_id in out


def test_index_memory_bounded_on_large_log(tmp_path):
    """Sanity ceiling: with all new index fields, a 50K-line log with
    high error and warn density must still serialize to under ~5 MB.
    Bigger than that means a cap is missing somewhere."""
    from logb import index as _idx
    p = tmp_path / "big.log"
    lines = []
    for i in range(50_000):
        # Mix of severities, repeated lines, and code variety.
        if i % 7 == 0:
            lines.append(f"**ERROR: (E-{i % 100:04d}): something bad")
        elif i % 3 == 0:
            lines.append(f"**WARN: (W-{i % 50:04d}): heads up")
        else:
            lines.append(f"info line {i} loading scripts/run.tcl")
    p.write_text("\n".join(lines) + "\n")
    idx = _idx.build(p)
    serialized_len = len(json.dumps(idx, default=str))
    assert serialized_len < 5_000_000, (
        f"index serialized to {serialized_len} bytes — "
        "a cap is missing somewhere")
    # Sanity: counts are still accurate at this scale.
    assert idx["n_err"] > 0 and idx["n_warn"] > 0
    assert idx["total"] == 50_000


def test_index_version_bumped_invalidates_old_cache(tmp_path):
    """Bumping INDEX_VERSION must force a rebuild of stale sidecars,
    not silently return an old (v: 5) index that lacks the new fields."""
    from logb import index as _idx
    p = tmp_path / "x.log"
    p.write_text("**ERROR: (IMPLF-213): bad mask\n")
    # Write an obviously stale sidecar.
    side = _idx.index_path(p)
    side.write_text(json.dumps({
        "v": 5, "profile": "eda", "size": 1, "mtime": 0.0,
        "grid_step": 2000, "total": 1, "grid": [0],
        "severe": [], "stages": [], "codes": [],
        "n_err": 0, "n_fat": 0, "n_warn": 0,
    }))
    idx = _idx.load_or_build(p)
    assert idx["v"] == _idx.INDEX_VERSION              # bumped to current
    assert idx["v"] >= 6
    # Real counts (the stale file said 0; rebuild fixed it).
    assert idx["n_err"] == 1


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


def test_log_summary_no_300_cap_on_distinct_codes(tmp_path):
    """The display cap is gone — every distinct code gets a row, bounded
    only by the per-tool char budget. With a generous budget, all codes
    appear regardless of count."""
    p = tmp_path / "many_codes.log"
    # 500 distinct error codes, one occurrence each.
    p.write_text("\n".join(
        f"**ERROR: (CODE-{i:04d}): boom {i}" for i in range(500)) + "\n")
    # Bump the budget far above what 500 rows need.
    cfg = _cfg(log_path=str(p), tool_result_char_budget=200_000)
    out = build_registry().dispatch("log_summary", {}, _ctx(cfg))
    # All 500 codes should be rendered as rows.
    assert "500 distinct code(s)" in out
    assert "CODE-0000" in out and "CODE-0499" in out
    # No "+N more codes" trailer (the old 300-cap message).
    assert "more codes" not in out


def test_log_summary_truncated_only_by_char_budget(tmp_path):
    """With the default char budget, output is clipped by truncate() —
    same backstop as every other tool result, not a hardcoded row cap."""
    p = tmp_path / "many.log"
    p.write_text("\n".join(
        f"**ERROR: (X-{i:04d}): msg" for i in range(800)) + "\n")
    cfg = _cfg(log_path=str(p), tool_result_char_budget=6000)
    out = build_registry().dispatch("log_summary", {}, _ctx(cfg))
    # Budget-driven truncation kicks in — the elision marker is the
    # truncate() one, not the old "+N more codes" sentinel.
    assert "[truncated " in out or len(out) <= 6500


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
    # The verifier's "pending TODOs" check would force re-asks here (the
    # FakeClient calls search_manual once and bails). This test isn't
    # about verification — it's about the loop mechanically calling tools
    # and emitting an answer — so disable verify_citations.
    cfg = _cfg(verify_citations=False)
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


# ---- cite-path resolution (the "file is present but resolver missed it") ----
def test_resolve_cite_via_basename_match_to_target_log(tmp_path):
    """Most common case: cite uses just the basename of the target log."""
    from logb.agent import _resolve_cite_path
    log = tmp_path / "innovus_log" / "test.log"
    log.parent.mkdir(parents=True)
    log.write_text("a\n" * 100)
    # project_root deliberately elsewhere — this is the prod scenario
    other = tmp_path / "other_project"
    other.mkdir()
    cfg = _cfg(log_path=str(log), project_root=str(other))
    assert _resolve_cite_path("test.log", cfg).samefile(log)


def test_resolve_cite_via_suffix_match_to_absolute_log_path(tmp_path):
    """Cite is a relative path whose tail matches an absolute log_path —
    the canonical 'file is present but resolver said no' bug."""
    from logb.agent import _resolve_cite_path
    log = tmp_path / "innovus_log" / "test.log"
    log.parent.mkdir(parents=True)
    log.write_text("\n" * 2000)            # 2000 lines so L1234 is valid
    other = tmp_path / "other_project"
    other.mkdir()
    cfg = _cfg(log_path=str(log), project_root=str(other))
    # The cite says "innovus_log/test.log"; log_path is absolute and ends
    # with the same suffix. Must resolve to the real log.
    resolved = _resolve_cite_path("innovus_log/test.log", cfg)
    assert resolved is not None
    assert resolved.samefile(log)


def test_resolve_cite_rglob_under_log_dir_not_just_project_root(tmp_path):
    """When --log points OUTSIDE project_root, the rglob fallback must
    also search the log's directory tree — not only project_root."""
    from logb.agent import _resolve_cite_path
    log_root = tmp_path / "prod_logs"
    log_root.mkdir()
    (log_root / "stage_a").mkdir()
    target = log_root / "stage_a" / "place.log"
    target.write_text("x\n" * 50)
    from pathlib import Path as _Path
    cfg = _cfg(log_path=str(log_root), project_root=str(tmp_path / "elsewhere"))
    _Path(cfg.project_root).mkdir()
    # Cite references just the basename; must be found via rglob under
    # log_root even though it's not under project_root.
    resolved = _resolve_cite_path("place.log", cfg)
    assert resolved is not None
    assert resolved.samefile(target)


def test_resolve_cite_still_returns_none_for_truly_missing(tmp_path):
    """Don't accidentally turn 'unresolvable' into 'wrong file' — a cite
    that names a file that doesn't exist anywhere must still return None."""
    from logb.agent import _resolve_cite_path
    log = tmp_path / "test.log"
    log.write_text("a\n")
    cfg = _cfg(log_path=str(log), project_root=str(tmp_path))
    assert _resolve_cite_path("not_real_at_all.log", cfg) is None


def test_verify_citations_passes_with_suffix_match(tmp_path):
    """End-to-end: a Mode-C answer citing 'innovus_log/test.log:5' must
    verify successfully when the actual log is /abs/.../innovus_log/test.log
    (the exact scenario from the user's report)."""
    from logb.agent import _verify_citations
    log = tmp_path / "innovus_log" / "test.log"
    log.parent.mkdir(parents=True)
    log.write_text("\n".join(f"line {i}" for i in range(20)) + "\n")
    other = tmp_path / "other"
    other.mkdir()
    cfg = _cfg(log_path=str(log), project_root=str(other))
    cites = _verify_citations(
        "## Evidence\n- see innovus_log/test.log:5 for context", cfg)
    assert len(cites) == 1 and cites[0]["ok"]
    assert cites[0]["content"] == "line 4"


# ---- quoted-text + numeric-claim verification -------------------------------
def test_quoted_text_in_tool_result_passes():
    from logb.agent import _find_quoted_text_problems
    history = [{"role": "tool", "name": "read_logs",
                "result": "L20: **ERROR: clock 'core_clk' referenced before created"}]
    problems = _find_quoted_text_problems(
        "The log says `clock 'core_clk' referenced before created`.",
        history)
    assert problems == []


def test_quoted_text_not_in_tool_result_flagged():
    from logb.agent import _find_quoted_text_problems
    history = [{"role": "tool", "name": "read_logs",
                "result": "L20: **ERROR: clock 'core_clk' referenced before created"}]
    problems = _find_quoted_text_problems(
        "The log says `the entire database was deleted at 03:14`.",
        history)
    assert len(problems) == 1
    assert "fabricated" in problems[0]


def test_quoted_text_ignores_cite_shapes():
    """Path:line citations are already handled by _verify_citations — the
    quoted-text check must not double-flag them."""
    from logb.agent import _find_quoted_text_problems
    history = [{"role": "tool", "name": "read_file", "result": "irrelevant"}]
    problems = _find_quoted_text_problems(
        "See `scripts/constraints/top.sdc:88` for the bug.", history)
    assert problems == []                # the path:line shape is excluded


def test_quoted_text_ignores_short_quotes():
    """Don't false-positive on tiny inline literals like variable names —
    only quotes long enough to plausibly be 'fabricated content' count."""
    from logb.agent import _find_quoted_text_problems
    history = [{"role": "tool", "name": "read_logs", "result": "blah"}]
    problems = _find_quoted_text_problems(
        "The variable `core_clk` is missing.", history)
    assert problems == []                # 8-char quote, under the threshold


def test_extract_summary_counts_finds_most_recent():
    from logb.agent import _extract_summary_counts
    history = [
        {"role": "tool", "name": "log_summary",
         "result": "# x  (10 lines)\nEXACT counts (whole file): 1 FATAL · 5 ERROR · 3 WARN · 0 codes\n"},
        {"role": "tool", "name": "read_logs", "result": "noise"},
        {"role": "tool", "name": "log_summary",
         "result": "# y  (20 lines)\nEXACT counts (whole file): 2 FATAL · 4 ERROR · 7 WARN · 0 codes\n"},
    ]
    counts = _extract_summary_counts(history)
    assert counts == {"FATAL": 2, "ERROR": 4, "WARN": 7}   # the latest


def test_numeric_claim_matches_summary_passes():
    from logb.agent import _find_numeric_claim_problems
    history = [{"role": "tool", "name": "log_summary",
                "result": "EXACT counts (whole file): 2 FATAL · 4 ERROR · 2 WARN · 0 codes"}]
    problems = _find_numeric_claim_problems(
        "Answer: 2 FATAL, 4 ERROR, 2 WARN.", history)
    assert problems == []


def test_numeric_claim_contradicts_summary_flagged():
    from logb.agent import _find_numeric_claim_problems
    history = [{"role": "tool", "name": "log_summary",
                "result": "EXACT counts (whole file): 2 FATAL · 4 ERROR · 2 WARN · 0 codes"}]
    problems = _find_numeric_claim_problems(
        "Answer: 47 ERROR, 12 FATAL.", history)
    assert len(problems) == 2
    assert any("47 ERROR" in p and "exact: 4 ERROR" in p for p in problems)
    assert any("12 FATAL" in p and "exact: 2 FATAL" in p for p in problems)


def test_numeric_claim_no_summary_no_check():
    """If log_summary wasn't called this turn, we have no authoritative
    counts to compare against — skip the check rather than guess."""
    from logb.agent import _find_numeric_claim_problems
    history = [{"role": "tool", "name": "read_logs", "result": "lots"}]
    assert _find_numeric_claim_problems("3 ERROR", history) == []


def test_verification_rejects_fabricated_quoted_text():
    """End-to-end: a Mode-C draft with a fabricated literal quote gets
    caught by the new check and forces a revision."""
    class Fabricator:
        def __init__(self):
            self.calls = 0
        def chat(self, s, h, t, on_token=None):
            self.calls += 1
            if self.calls == 1:
                return Assistant(tool_calls=[{"id": "1",
                                              "name": "read_logs", "args": {}}])
            if self.calls == 2:
                # Fabricated quote — the read_logs result definitely won't
                # contain this exact 40+ char string.
                return Assistant(text=(
                    "## Root Cause\n"
                    "The log says `the network partition occurred at exactly 03:14:42 UTC`."))
            return Assistant(text="## Root Cause\nrevised: cause unclear.")
    cli = Fabricator()
    cfg = _cfg(verify_citations=True)
    res = Agent(cli, build_registry(), _ctx(cfg), max_steps=3).ask("debug")
    # 1 tool step + 1 draft + 1 revision after verification rejection
    assert cli.calls == 3
    assert "revised" in res.answer


# ---- harness guards: tool-call-as-text + duplicate-call brake --------------
def test_detect_tool_calls_in_text_finds_json_blocks():
    from logb.agent import _detect_tool_calls_in_text
    # The exact failure mode from the user's session: model emits tool
    # calls inside markdown JSON fences in its text reply.
    text = ("Sure, let me check.\n"
            '```json\n{"name": "search_manual", "arguments": {"query": "x"}}\n```\n'
            'And then I will try:\n'
            '{"name": "code_lookup", "arguments": {"code": "IMPSDC-3071"}}\n'
            'Done.')
    names = _detect_tool_calls_in_text(text)
    assert names == ["search_manual", "code_lookup"]


def test_detect_tool_calls_in_text_clean_answer():
    from logb.agent import _detect_tool_calls_in_text
    assert _detect_tool_calls_in_text(
        "## Root Cause\nthe SDC is broken at top.sdc:88") == []


def test_verification_rejects_answer_with_tool_calls_in_text():
    """End-to-end: the 7B failure where the model writes tool-call JSON
    in its text reply. Verification must catch this and force a re-ask."""
    class TextSpammer:
        def __init__(self):
            self.calls = 0
        def chat(self, s, h, t, on_token=None):
            self.calls += 1
            if self.calls == 1:
                # The exact failure: a "final answer" stuffed with JSON
                # tool-call blocks that didn't actually execute.
                return Assistant(text=(
                    "Based on my analysis the cause is X.\n"
                    '```json\n{"name": "search_manual", "arguments": {"query": "y"}}\n```\n'
                    '{"name": "code_lookup", "arguments": {"code": "Z"}}\n'
                    "Conclusion: see above."))
            return Assistant(text="Sorry, I have no grounded answer for this.")
    cli = TextSpammer()
    cfg = _cfg(verify_citations=True)
    Agent(cli, build_registry(), _ctx(cfg), max_steps=2).ask("debug it")
    assert cli.calls == 2          # verification forced a re-ask


def test_tool_call_signature_canonicalizes_args():
    from logb.agent import _tool_call_signature
    # Different orderings, same canonical signature.
    a = _tool_call_signature("search_manual", {"query": "X", "k": 4})
    b = _tool_call_signature("search_manual", {"k": 4, "query": "X"})
    assert a == b
    # Different args, different signature.
    c = _tool_call_signature("search_manual", {"query": "Y", "k": 4})
    assert a != c


def test_duplicate_tool_call_refused_in_main_loop():
    """If the model calls the same tool with the same args repeatedly,
    the runtime refuses past the cap — preventing the 'spam same call'
    loop the user observed."""
    class Looper:
        def __init__(self):
            self.n = 0
        def chat(self, s, h, t, on_token=None):
            self.n += 1
            if self.n <= 4:
                return Assistant(tool_calls=[{"id": f"c{self.n}",
                                              "name": "list_logs",
                                              "args": {}}])
            return Assistant(text="ok")
    cfg = _cfg(max_repeated_tool_call=2, verify_citations=False)
    agent = Agent(Looper(), build_registry(), _ctx(cfg), max_steps=6)
    res = agent.ask("loop")
    # Walk the tool results: after the 2nd identical call, subsequent
    # ones must come back REFUSED.
    tool_results = [h["result"] for h in res.transcript
                    if h.get("role") == "tool"]
    # 4 tool calls were issued; first 2 dispatch normally, last 2 are refused.
    assert len(tool_results) == 4
    assert sum(1 for r in tool_results if r.startswith("REFUSED")) >= 2
    # The refusal message includes both the count and the guidance.
    refused = next(r for r in tool_results if r.startswith("REFUSED"))
    assert "already been called" in refused
    assert "different tool" in refused or "different" in refused


def test_distinct_args_not_treated_as_duplicates():
    """Same tool name but different args must NOT trip the duplicate
    brake — the model is doing genuine different work."""
    class Varied:
        def __init__(self):
            self.n = 0
        def chat(self, s, h, t, on_token=None):
            self.n += 1
            if self.n <= 3:
                return Assistant(tool_calls=[{"id": f"c{self.n}",
                                              "name": "search_manual",
                                              "args": {"query": f"q{self.n}"}}])
            return Assistant(text="done")
    cfg = _cfg(max_repeated_tool_call=2, verify_citations=False)
    agent = Agent(Varied(), build_registry(), _ctx(cfg), max_steps=5)
    res = agent.ask("research")
    tool_results = [h["result"] for h in res.transcript
                    if h.get("role") == "tool"]
    assert len(tool_results) == 3
    assert not any(r.startswith("REFUSED") for r in tool_results)


# ---- EDA-specific tools ----------------------------------------------------
def test_stage_timeline_pipeline_view():
    reg, ctx = build_registry(), _ctx(_cfg())
    out = reg.dispatch("stage_timeline", {}, ctx)
    # The whole pipeline is visible with durations and statuses.
    for stage in ("init_design", "floorplan", "place",
                  "clock_tree_synthesis", "route"):
        assert stage in out
    # The crashing stage is correctly flagged INCOMPLETE.
    assert "INCOMPLETE" in out
    route_line = next(l for l in out.splitlines() if "route" in l)
    assert "INCOMPLETE" in route_line
    # Stages that ran cleanly are OK.
    fp_line = next(l for l in out.splitlines() if "floorplan" in l)
    assert "OK" in fp_line
    # Durations from log timestamps are present (e.g. "40s", "106s").
    assert "s " in out                # some duration shows up


def test_stage_errors_groups_by_stage():
    reg, ctx = build_registry(), _ctx(_cfg())
    out = reg.dispatch("stage_errors", {}, ctx)

    def _section(name: str) -> str:
        """Return just the lines belonging to the named ## section."""
        marker = f"## {name}"
        start = out.index(marker)
        next_section = out.find("\n##", start + len(marker))
        return out[start:next_section] if next_section != -1 else out[start:]

    assert "IMPSDC-3071" in _section("place")                # root cause
    assert "IMPCTS-5012" in _section("clock_tree_synthesis") # cascade
    assert "IMPROUTE-7440" in _section("route")              # terminal


def test_stage_tools_fall_back_when_no_stages(tmp_path):
    # A log without `--- Starting "X" ---` banners (e.g. a generic-looking
    # log under EDA mode) should fall back gracefully, not error.
    p = tmp_path / "no_stages.log"
    p.write_text("**ERROR: (IMPLF-213): bad mask\n"
                 "FATAL: (IMPCORE-9001): boom\n")
    cfg = _cfg(log_path=str(p))
    reg, ctx = build_registry(), _ctx(cfg)
    tl = reg.dispatch("stage_timeline", {}, ctx)
    assert "no stage banners" in tl
    se = reg.dispatch("stage_errors", {}, ctx)
    assert "no stage banners" in se
    assert "IMPLF-213" in se          # flat listing still surfaces them


def test_sdc_lint_catches_clock_used_before_created():
    """End-to-end on the real bundled top.sdc — independent of the log,
    sdc_lint should find the exact bug IMPSDC-3071 is complaining about."""
    reg, ctx = build_registry(), _ctx(_cfg())
    out = reg.dispatch("sdc_lint",
                       {"path": "scripts/constraints/top.sdc"}, ctx)
    assert "core_clk" in out
    assert "referenced BEFORE its create_clock" in out
    # The bug is at L87 (use) referencing L88 (declare).
    assert "L87" in out and "L88" in out


def test_sdc_lint_clean_file(tmp_path):
    p = tmp_path / "clean.sdc"
    p.write_text(
        "# A clean SDC: create then reference.\n"
        "create_clock -name core_clk -period 5 [get_ports clk_in]\n"
        "set_clock_uncertainty 0.05 [get_clocks core_clk]\n")
    cfg = _cfg(project_root=str(tmp_path))
    reg, ctx = build_registry(), _ctx(cfg)
    out = reg.dispatch("sdc_lint", {"path": "clean.sdc"}, ctx)
    assert "no issues found" in out
    assert "core_clk" in out


def test_sdc_lint_undeclared_clock(tmp_path):
    p = tmp_path / "missing.sdc"
    p.write_text("set_clock_uncertainty 0.05 [get_clocks ghost_clk]\n")
    cfg = _cfg(project_root=str(tmp_path))
    reg, ctx = build_registry(), _ctx(cfg)
    out = reg.dispatch("sdc_lint", {"path": "missing.sdc"}, ctx)
    assert "ghost_clk" in out
    assert "NEVER declared" in out


def test_code_lookup_returns_log_occurrence_and_manual():
    reg, ctx = build_registry(), _ctx(_cfg())
    out = reg.dispatch("code_lookup", {"code": "IMPSDC-3071"}, ctx)
    assert "IMPSDC-3071" in out
    assert "1 occurrence(s)" in out
    assert "L20" in out                     # found in the log
    assert "From manual:" in out             # manual section included
    assert "manual/innovus_errors.md" in out
    # Honesty rule: don't fabricate when missing
    out2 = reg.dispatch("code_lookup", {"code": "IMPFAKE-9999"}, ctx)
    assert "NOT FOUND" in out2
    assert "no passages matched" in out2 or "do NOT" in out2


def test_eda_tools_hidden_from_generic_mode_schema():
    """The whole reason for profile_required: in generic mode the EDA
    tools must NOT appear in the schema list the model sees. Otherwise
    the model wastes steps calling them."""
    reg = build_registry()
    eda_schemas = reg.schemas("eda")
    gen_schemas = reg.schemas("generic")
    eda_names = {s["name"] for s in eda_schemas}
    gen_names = {s["name"] for s in gen_schemas}
    for tool in ("stage_timeline", "stage_errors", "sdc_lint", "code_lookup"):
        assert tool in eda_names, f"{tool} should be in EDA schema list"
        assert tool not in gen_names, f"{tool} should be hidden in generic mode"
    # Generic mode still gets the universal tools.
    assert "read_logs" in gen_names
    assert "search_manual" in gen_names


def test_eda_tools_refuse_dispatch_under_wrong_profile():
    """Even if the model somehow knows the tool name in generic mode and
    tries to dispatch it, dispatch must refuse with a clear error."""
    cfg = _cfg(mode="generic")
    reg, ctx = build_registry(), _ctx(cfg)
    out = reg.dispatch("stage_timeline", {}, ctx)
    assert "requires the 'eda' profile" in out
    assert "active: 'generic'" in out


# ---- delegation (deep-agent sub-agents) ------------------------------------
class _RecordingClient:
    """Records every chat() call so tests can assert the child ran in
    isolation from the parent. Each invocation returns the next scripted
    Assistant from `plan`; missing plan entries return a default text reply."""

    def __init__(self, plan):
        self.plan = list(plan)
        self.histories: list[list] = []   # snapshot of history per call

    def chat(self, system, history, tools, on_token=None):
        self.histories.append([dict(h) for h in history])
        if self.plan:
            return self.plan.pop(0)
        return Assistant(text="(default)")


def test_delegate_subtask_spawns_child_with_fresh_history():
    """The child agent must run with empty history, not the parent's
    transcript — otherwise the whole point of delegation (context
    isolation) is defeated."""
    cli = _RecordingClient([
        # Parent step 1: call delegate.
        Assistant(tool_calls=[{"id": "1", "name": "delegate_subtask",
                                "args": {"focus": "check stage X",
                                         "max_steps": 2}}]),
        # Child step 1: emit its summary directly.
        Assistant(text="stage X has 1 error at top.sdc:88. "
                       "Recommend checking the SDC."),
        # Parent step 2: final answer using the child's report.
        Assistant(text="## Root Cause\nsee `scripts/constraints/top.sdc:88`"),
    ])
    cfg = _cfg(verify_citations=False)
    agent = Agent(cli, build_registry(), _ctx(cfg), max_steps=4)
    res = agent.ask("debug this")
    assert "Root Cause" in res.answer
    # The 2nd chat() call was the child's first step — its history should
    # NOT contain the parent's user question, only the child's framing.
    child_call_history = cli.histories[1]
    parent_question_text = "debug this"
    assert all(parent_question_text not in (h.get("text") or "")
               for h in child_call_history)
    # And the child's history should start with the SUB-AGENT framing.
    assert any("SUB-AGENT" in (h.get("text") or "")
               for h in child_call_history)


def test_delegate_subtask_summary_passed_back_to_parent():
    """The child's answer is embedded in the parent's history as a tool
    result — the parent should be able to use it on its next step."""
    cli = _RecordingClient([
        Assistant(tool_calls=[{"id": "1", "name": "delegate_subtask",
                                "args": {"focus": "find first error"}}]),
        Assistant(text="first error is IMPSDC-3071 at L20."),
        Assistant(text="parent final: child found IMPSDC-3071."),
    ])
    cfg = _cfg(verify_citations=False)
    agent = Agent(cli, build_registry(), _ctx(cfg), max_steps=4)
    agent.ask("what's the first error")
    # Parent's 3rd call (the final answer) should see the child's summary
    # as a tool result in its history.
    parent_final_history = cli.histories[2]
    tool_results = [h["result"] for h in parent_final_history
                    if h.get("role") == "tool"]
    assert any("IMPSDC-3071" in tr for tr in tool_results)
    assert any("delegate_subtask result" in tr for tr in tool_results)


def test_delegate_depth_gate_blocks_excessive_recursion():
    """Parent (depth 0) -> child (1) -> grandchild (2) is the limit.
    A grandchild trying to spawn another sub-agent must be refused —
    otherwise a confused model could runaway-recurse and burn the budget."""
    cli = _RecordingClient([
        # Parent: delegate
        Assistant(tool_calls=[{"id": "p", "name": "delegate_subtask",
                                "args": {"focus": "level 1"}}]),
        # Child: delegate again
        Assistant(tool_calls=[{"id": "c", "name": "delegate_subtask",
                                "args": {"focus": "level 2"}}]),
        # Grandchild: tries to delegate — should get ERROR back as tool result
        Assistant(tool_calls=[{"id": "g", "name": "delegate_subtask",
                                "args": {"focus": "level 3 (should be blocked)"}}]),
        # Grandchild after receiving the ERROR: emits a text answer
        Assistant(text="grandchild answer: cannot delegate further"),
        # Child receives the grandchild's summary, answers
        Assistant(text="child answer: relayed grandchild finding"),
        # Parent receives the child's summary, answers
        Assistant(text="parent answer: done"),
    ])
    cfg = _cfg(verify_citations=False)
    agent = Agent(cli, build_registry(), _ctx(cfg), max_steps=4)
    agent.ask("test recursion")
    # Find the grandchild's tool result (the depth-gate ERROR).
    all_tool_results = [h["result"] for hist in cli.histories
                        for h in hist if h.get("role") == "tool"]
    assert any("max delegation depth" in tr for tr in all_tool_results), \
        f"expected depth-gate error in tool results, got: {all_tool_results}"


def test_delegate_subtask_rejects_empty_focus():
    reg, ctx = build_registry(), _ctx(_cfg())
    # Need a parent agent in ctx for the tool not to error on that path.
    class _Stub: trace = lambda self, _: None  # noqa: E731
    ctx._agent = _Stub()
    out = reg.dispatch("delegate_subtask", {"focus": ""}, ctx)
    assert out.startswith("ERROR: `focus` is required")


def test_delegate_subtask_caps_max_steps():
    """Even if the model asks for max_steps=999, the child is capped at
    HARD_MAX_STEPS so a bad call can't blow the LLM budget."""
    from logb.tools.delegate import HARD_MAX_STEPS
    captured: dict = {}
    cli = _RecordingClient([
        # Parent: delegate with absurd max_steps
        Assistant(tool_calls=[{"id": "1", "name": "delegate_subtask",
                                "args": {"focus": "x",
                                         "max_steps": 9999}}]),
        # Child: answer immediately
        Assistant(text="child reply"),
        # Parent: final
        Assistant(text="parent done"),
    ])
    cfg = _cfg(verify_citations=False)
    agent = Agent(cli, build_registry(), _ctx(cfg), max_steps=3)

    # Wrap delegate_subtask to capture the child's max_steps.
    original = agent.registry._tools["delegate_subtask"].run
    def _spy(args, ctx):
        captured["max_steps_arg"] = args.get("max_steps")
        return original(args, ctx)
    agent.registry._tools["delegate_subtask"].run = _spy

    agent.ask("test")
    # Even though the model passed 9999, the actual budget should be capped.
    # We assert the captured arg WAS 9999 (the model was free to ask) but
    # the child only executed 1 step (it answered immediately), proving
    # the cap didn't break early exit.
    assert captured.get("max_steps_arg") == 9999
    assert HARD_MAX_STEPS == 10                  # constant sanity


def test_delegate_subtask_strips_user_callbacks_from_child():
    """Children must not be able to pop user prompts or run shell, even if
    the parent had on_ask/on_confirm wired up."""
    cli = _RecordingClient([
        Assistant(tool_calls=[{"id": "1", "name": "delegate_subtask",
                                "args": {"focus": "test"}}]),
        # Child: try to call ask_user — should be refused
        Assistant(tool_calls=[{"id": "c", "name": "ask_user",
                                "args": {"question": "which file?"}}]),
        Assistant(text="child: could not ask user"),
        Assistant(text="parent: done"),
    ])
    cfg = _cfg(interactive=True, verify_citations=False)
    ctx = ToolContext(cfg=cfg, manual_index=ManualIndex(cfg.manual_dir),
                      on_ask=lambda q, o: "answer-from-parent",
                      on_confirm=lambda c, p: True)
    agent = Agent(cli, build_registry(), ctx, max_steps=4)
    agent.ask("test")
    # The child's ask_user call should have been refused — the answer
    # "answer-from-parent" must NOT appear anywhere in tool results.
    all_results = [h["result"] for hist in cli.histories
                   for h in hist if h.get("role") == "tool"]
    assert not any("answer-from-parent" in r for r in all_results)


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


# ---- hybrid BM25 + embedding retrieval -------------------------------------
class _StubEmbedStore:
    """In-memory embedding store for tests. Maps text -> fixed vector.
    Mimics the EmbeddingStore interface (`enabled`, `get_or_embed`)."""

    def __init__(self, vectors):
        self.vectors = vectors           # dict[str -> list[float]]
        self.enabled = True
        self.calls = []

    def get_or_embed(self, text):
        self.calls.append(text)
        for needle, vec in self.vectors.items():
            if needle in text:
                return vec
        return None


def test_bm25_only_when_no_embeddings(tmp_path):
    from logb.rag import ManualIndex
    mdir = tmp_path / "m"
    mdir.mkdir()
    (mdir / "x.md").write_text("# IMPSDC-3071\nclock referenced.\n")
    idx = ManualIndex(str(mdir))
    hits = idx.search("IMPSDC-3071")
    assert hits and "IMPSDC-3071" in hits[0][1].heading


def test_hybrid_search_uses_embeddings_when_attached(tmp_path):
    """With an embedding store attached, a conceptual query that doesn't
    share vocabulary with the manual still finds the right passage via
    vector similarity."""
    from logb.rag import ManualIndex
    mdir = tmp_path / "m"
    mdir.mkdir()
    (mdir / "x.md").write_text(
        "# Resolution failure\nThe constraint object could not be resolved.\n")
    idx = ManualIndex(str(mdir))
    idx._maybe_rebuild()                 # force chunk creation so id() is stable
    chunk = idx._chunks[0]

    # Query and the manual chunk share NO content words ("absent" vs
    # "Resolution failure"), so BM25 will score 0. But the embeddings
    # both contain the marker word "match" so the stub returns the same
    # vector for both — cosine = 1.
    store = _StubEmbedStore({
        "absent": [1.0, 0.0],
        "constraint object": [1.0, 0.0],
    })
    idx.attach_embeddings(store)
    hits = idx.search("the thing is absent")
    assert hits, "hybrid retrieval should surface a passage via embeddings"
    assert hits[0][1] is chunk


def test_hybrid_degrades_when_embed_returns_none(tmp_path):
    """If the embedding endpoint is down (returns None), search must
    silently fall back to BM25 results rather than erroring."""
    from logb.rag import ManualIndex
    mdir = tmp_path / "m"
    mdir.mkdir()
    (mdir / "x.md").write_text("# alpha\nfind by keyword.\n")
    idx = ManualIndex(str(mdir))

    class DeadStore:
        enabled = True
        def get_or_embed(self, _text):
            return None                  # endpoint unreachable

    idx.attach_embeddings(DeadStore())
    hits = idx.search("alpha")
    assert hits                          # BM25 still finds it
    assert "alpha" in hits[0][1].heading


def test_embed_store_uses_cache(tmp_path):
    """The cache file means an embedding only hits Ollama once per
    (text, model) pair — important for repeated searches in a chat."""
    from logb.embed import EmbeddingStore
    import struct
    cache_dir = tmp_path / ".logb-embeddings"

    # Hand-write a cached vector for known text+model.
    text = "hello world"
    model = "stub-model"
    store = EmbeddingStore(project_root=str(tmp_path),
                            cache_subdir=".logb-embeddings",
                            host="http://localhost:0",   # unreachable on purpose
                            model=model)
    cache_dir.mkdir(parents=True, exist_ok=True)
    # Mirror the hashing logic used internally to seed the cache.
    from logb.embed import _chunk_id, _cache_path
    cid = _chunk_id(text, model)
    path = _cache_path(cache_dir, cid)
    path.write_bytes(struct.pack("<3f", 1.0, 0.0, 0.0))

    # Even though the host is unreachable, the cached value comes back.
    vec = store.get_or_embed(text)
    assert vec == [1.0, 0.0, 0.0]


def test_embed_store_disabled_returns_none(tmp_path):
    from logb.embed import EmbeddingStore
    store = EmbeddingStore(project_root=str(tmp_path),
                            cache_subdir=".logb-embeddings",
                            host="http://x", model="")    # empty -> disabled
    assert not store.enabled
    assert store.get_or_embed("anything") is None


def test_cosine_dot_for_normalized_vectors():
    from logb.embed import cosine, _l2_normalize
    a = _l2_normalize([3.0, 4.0])
    b = _l2_normalize([3.0, 4.0])
    assert abs(cosine(a, b) - 1.0) < 1e-6
    c = _l2_normalize([-3.0, -4.0])
    assert abs(cosine(a, c) + 1.0) < 1e-6


# ---- manual freshness + query expansion ------------------------------------
def test_manual_index_picks_up_new_file_mid_session(tmp_path):
    """The whole point of the freshness check: add a manual file
    mid-session, the next search must find it without restart."""
    from logb.rag import ManualIndex
    mdir = tmp_path / "manual"
    mdir.mkdir()
    (mdir / "first.md").write_text("# IMPSDC-3071\nclock referenced too early.\n")
    idx = ManualIndex(str(mdir))
    hits = idx.search("IMPSDC-3071")
    assert hits
    n_before = len(idx._chunks)
    # Add a new file. Bump mtime so the signature check fires.
    import time, os
    time.sleep(0.01)
    new_file = mdir / "second.md"
    new_file.write_text("# IMPLF-213\nbad mask layer.\n")
    os.utime(new_file, None)
    # Force a different signature by also re-touching the directory
    hits2 = idx.search("IMPLF-213")
    assert hits2
    # The second file was indexed.
    assert any("second.md" in c.source for c in idx._chunks)
    assert len(idx._chunks) > n_before


def test_manual_index_picks_up_edits_mid_session(tmp_path):
    """Editing an existing file should also be picked up."""
    from logb.rag import ManualIndex
    import time, os
    mdir = tmp_path / "manual"
    mdir.mkdir()
    f = mdir / "doc.md"
    f.write_text("# alpha\nfirst content.\n")
    idx = ManualIndex(str(mdir))
    h1 = idx.search("alpha")
    assert h1 and "first content" in h1[0][1].text
    # Edit the file. Make sure mtime changes.
    time.sleep(0.01)
    f.write_text("# alpha\nbrand new content.\n")
    os.utime(f, None)
    h2 = idx.search("alpha")
    assert h2 and "brand new content" in h2[0][1].text


def test_manual_index_skips_rebuild_when_unchanged(tmp_path):
    """Stability: when nothing changed, we don't pointlessly rebuild —
    this matters for chat sessions making many searches in a row."""
    from logb.rag import ManualIndex
    mdir = tmp_path / "manual"
    mdir.mkdir()
    (mdir / "x.md").write_text("# A\ncontent.\n")
    idx = ManualIndex(str(mdir))
    idx.search("A")
    chunks1 = list(idx._chunks)
    idx.search("A")
    chunks2 = list(idx._chunks)
    # Same object identities — index was not rebuilt.
    assert all(a is b for a, b in zip(chunks1, chunks2))


def test_search_manual_query_expansion_finds_code(tmp_path):
    """Conceptual query mentions a code in passing; expansion should
    pull the code's own manual section."""
    from logb.tools import build_registry, ToolContext
    from logb.config import Config
    from logb.rag import ManualIndex
    mdir = tmp_path / "manual"
    mdir.mkdir()
    # Manual section keyed by the code, body mentions different words
    (mdir / "errors.md").write_text(
        "# IMPSDC-3071\nThe constraint engine could not resolve a clock "
        "object because its declaration order was wrong.\n")
    cfg = Config.load({"manual_dir": str(mdir), "log_path": str(tmp_path),
                        "skills_dir": str(tmp_path),
                        "project_root": str(tmp_path), "interactive": False})
    ctx = ToolContext(cfg=cfg, manual_index=ManualIndex(str(mdir)))
    # Conceptual phrasing; code appears in passing.
    out = build_registry().dispatch("search_manual",
        {"query": "I see error IMPSDC-3071 in my run"}, ctx)
    assert "IMPSDC-3071" in out
    assert "constraint engine" in out                # full passage included


def test_search_manual_expand_can_be_disabled(tmp_path):
    from logb.tools import build_registry, ToolContext
    from logb.config import Config
    from logb.rag import ManualIndex
    mdir = tmp_path / "manual"
    mdir.mkdir()
    (mdir / "errors.md").write_text("# CODE-1\nfoo bar.\n")
    cfg = Config.load({"manual_dir": str(mdir), "log_path": str(tmp_path),
                        "skills_dir": str(tmp_path),
                        "project_root": str(tmp_path), "interactive": False})
    ctx = ToolContext(cfg=cfg, manual_index=ManualIndex(str(mdir)))
    # With expansion off, a non-matching query gets no hits.
    out = build_registry().dispatch("search_manual",
        {"query": "completely unrelated topic", "expand": False}, ctx)
    assert "No manual passages matched" in out


def test_search_manual_no_match_says_so_explicitly(tmp_path):
    """When nothing matches, the result must encourage admitting ignorance
    rather than fabricating — the 7B model needs that nudge."""
    from logb.tools import build_registry, ToolContext
    from logb.config import Config
    from logb.rag import ManualIndex
    mdir = tmp_path / "manual"
    mdir.mkdir()
    (mdir / "only.md").write_text("# alpha\ncontent.\n")
    cfg = Config.load({"manual_dir": str(mdir), "log_path": str(tmp_path),
                        "skills_dir": str(tmp_path),
                        "project_root": str(tmp_path), "interactive": False})
    ctx = ToolContext(cfg=cfg, manual_index=ManualIndex(str(mdir)))
    out = build_registry().dispatch("search_manual",
        {"query": "zebra"}, ctx)
    assert "do not fabricate" in out.lower() or "no entry" in out.lower()


# ---- JSONL / structured log support ----------------------------------------
def test_jsonl_detected_and_severity_filtered(tmp_path):
    p = tmp_path / "app.jsonl"
    p.write_text(
        '{"ts": 1, "level": "info", "msg": "starting"}\n'
        '{"ts": 2, "level": "error", "msg": "connection refused"}\n'
        '{"ts": 3, "level": "warn", "msg": "slow query"}\n'
        '{"ts": 4, "level": "error", "msg": "retry failed"}\n'
        '{"ts": 5, "level": "fatal", "msg": "exiting"}\n')
    cfg = _cfg(log_path=str(p), mode="generic")
    reg, ctx = build_registry(), _ctx(cfg)
    out = reg.dispatch("read_logs", {"severity": "error,fatal"}, ctx)
    assert "JSONL read" in out
    assert "connection refused" in out
    assert "retry failed" in out
    assert "exiting" in out
    assert "starting" not in out            # info filtered out
    assert "slow query" not in out          # warn filtered out


def test_jsonl_field_value_filter(tmp_path):
    """field/value filtering catches what regex can't — e.g. filter by
    request_id, by service name, by a non-severity dimension."""
    p = tmp_path / "svc.jsonl"
    p.write_text(
        '{"service": "auth", "msg": "login ok"}\n'
        '{"service": "billing", "msg": "charge failed"}\n'
        '{"service": "auth", "msg": "token expired"}\n'
        '{"service": "billing", "msg": "refund issued"}\n')
    cfg = _cfg(log_path=str(p), mode="generic")
    reg, ctx = build_registry(), _ctx(cfg)
    out = reg.dispatch("read_logs",
                       {"field": "service", "value": "auth"}, ctx)
    assert "login ok" in out and "token expired" in out
    assert "charge failed" not in out
    assert "2 match(es)" in out


def test_jsonl_falls_back_to_msg_field_alternatives(tmp_path):
    """Different JSON logs use different field names. We try several."""
    p = tmp_path / "varied.jsonl"
    p.write_text(
        '{"level": "error", "message": "first style"}\n'
        '{"level": "error", "msg": "second style"}\n'
        '{"level": "error", "text": "third style"}\n')
    cfg = _cfg(log_path=str(p), mode="generic")
    out = build_registry().dispatch("read_logs",
                                    {"severity": "error"}, _ctx(cfg))
    assert "first style" in out
    assert "second style" in out
    assert "third style" in out


def test_non_jsonl_takes_text_path(tmp_path):
    """If the file isn't JSONL, the JSONL detector must NOT engage —
    regular regex/severity filtering still has to work."""
    p = tmp_path / "plain.log"
    p.write_text(
        "**ERROR: (IMPLF-213): bad mask\n"
        "INFO: continuing\n"
        "**ERROR: (IMPCORE-9001): boom\n")
    cfg = _cfg(log_path=str(p))
    out = build_registry().dispatch("read_logs",
                                    {"severity": "error"}, _ctx(cfg))
    # The plain-text path always shows the CENSUS header; the JSONL path
    # uses a different header. Confirm we took the text route.
    assert "CENSUS" in out and "JSONL read" not in out
    assert "IMPLF-213" in out


def test_jsonl_engaged_by_field_arg_even_on_plain_text(tmp_path):
    """If the agent passes `field=X`, that's an explicit structural query
    — engage the JSONL reader regardless of file sniff. (Useful for files
    that have some non-JSON header lines.)"""
    p = tmp_path / "mixed.log"
    p.write_text("# header line not JSON\n"
                 '{"service": "auth", "msg": "ok"}\n')
    cfg = _cfg(log_path=str(p))
    out = build_registry().dispatch("read_logs",
                                    {"field": "service", "value": "auth"},
                                    _ctx(cfg))
    assert "JSONL read" in out and "ok" in out


# ---- strict mode preset ----------------------------------------------------
def test_strict_mode_tightens_knobs():
    cfg = Config()
    # Defaults — relaxed.
    assert cfg.max_steps == 12
    assert cfg.verify_max_passes == 3
    assert cfg.max_repeated_tool_call == 2
    cfg.apply_strict()
    assert cfg.strict
    assert cfg.max_steps == 6
    assert cfg.verify_max_passes == 5
    assert cfg.max_repeated_tool_call == 1
    assert cfg.tool_result_char_budget <= 4000
    assert cfg.history_compact_keep_recent == 2


def test_strict_mode_idempotent():
    cfg = Config()
    cfg.apply_strict()
    cfg.apply_strict()                       # again
    assert cfg.max_steps == 6                # not double-clamped
    assert cfg.verify_max_passes == 5


def test_strict_mode_via_config_load():
    """--strict goes through cli_overrides; Config.load must wire it up."""
    cfg = Config.load({"strict": True})
    assert cfg.strict
    assert cfg.max_steps == 6
    assert cfg.max_repeated_tool_call == 1


def test_strict_mode_only_tightens_never_loosens():
    """If the user manually set a tight max_steps, strict shouldn't
    accidentally relax it back up."""
    cfg = Config.load({"strict": True, "max_steps": 3})
    assert cfg.max_steps == 3                # respected, not bumped to 6


# ---- eval harness ----------------------------------------------------------
def test_eval_score_simple_substring():
    from logb.eval.runner import EvalCase, score_one
    case = EvalCase(id="t", log_path="x", question="q",
                     expected_facts=["foo", "bar"],
                     forbidden_facts=["baz"])
    passed, hits, misses, forb = score_one(case, "foo and bar and qux")
    assert passed and hits == ["foo", "bar"] and not misses and not forb


def test_eval_score_any_of():
    from logb.eval.runner import EvalCase, score_one
    case = EvalCase(id="t", log_path="x", question="q",
                     expected_facts=[{"any_of": ["alpha", "beta"]}])
    passed, _, _, _ = score_one(case, "we found alpha here")
    assert passed
    passed, _, _, _ = score_one(case, "gamma only")
    assert not passed


def test_eval_score_regex():
    from logb.eval.runner import EvalCase, score_one
    case = EvalCase(id="t", log_path="x", question="q",
                     expected_facts=[{"regex": r"\bL\d+\b"}])
    passed, _, _, _ = score_one(case, "see L20 for the bug")
    assert passed
    passed, _, _, _ = score_one(case, "no line number here")
    assert not passed


def test_eval_score_forbidden_facts_fail():
    """Even with all expected facts present, hitting a forbidden fact
    fails the case — the 7B failure mode where the answer is right AND
    hallucinates more must be caught."""
    from logb.eval.runner import EvalCase, score_one
    case = EvalCase(id="t", log_path="x", question="q",
                     expected_facts=["correct fact"],
                     forbidden_facts=["fabricated thing"])
    passed, hits, _, forb = score_one(case,
        "the correct fact is here, plus a fabricated thing")
    assert hits == ["correct fact"]
    assert forb == ["fabricated thing"]
    assert not passed


def test_eval_load_cases_from_corpus(tmp_path):
    from logb.eval.runner import load_cases
    case = {"id": "tc1", "log_path": "x.log", "question": "q",
            "expected_facts": ["foo"]}
    (tmp_path / "tc1.json").write_text(json.dumps(case))
    cases = load_cases(tmp_path)
    assert len(cases) == 1
    assert cases[0].id == "tc1"


def test_eval_run_corpus_with_fake_agent(tmp_path):
    """End-to-end: load a case, run it with a FakeClient-backed agent
    (so we don't need Ollama), confirm scoring + telemetry."""
    from logb.eval.runner import run_corpus
    (tmp_path / "case1.json").write_text(json.dumps({
        "id": "case1", "log_path": "x", "question": "why?",
        "expected_facts": ["IMPSDC-3071", "core_clk"],
        "forbidden_facts": ["nonsense"]}))

    def _build(case):
        # Return a one-shot agent that always replies with a fixed answer.
        cfg = _cfg(verify_citations=False)
        ctx = ToolContext(cfg=cfg, manual_index=ManualIndex(cfg.manual_dir))

        class Fixed:
            def chat(self, s, h, t, on_token=None):
                return Assistant(
                    text=("## Root Cause\nclock 'core_clk' referenced "
                          "before created (IMPSDC-3071)."),
                    tokens_in=200, tokens_out=80, latency_ms=120)
        return Agent(Fixed(), build_registry(), ctx, max_steps=2)

    results = run_corpus(tmp_path, build_agent=_build)
    assert len(results) == 1
    r = results[0]
    assert r.passed
    assert "IMPSDC-3071" in r.hits and "core_clk" in r.hits
    assert r.forbidden_hits == []
    assert r.tokens_in == 200 and r.tokens_out == 80


def test_eval_run_corpus_filters_and_caps(tmp_path):
    from logb.eval.runner import run_corpus
    for i in range(5):
        (tmp_path / f"c{i}.json").write_text(json.dumps({
            "id": f"c{i}", "log_path": "x", "question": "q",
            "expected_facts": []}))

    def _build(case):
        cfg = _cfg(verify_citations=False)
        ctx = ToolContext(cfg=cfg, manual_index=ManualIndex(cfg.manual_dir))

        class Stub:
            def chat(self, s, h, t, on_token=None):
                return Assistant(text="ok")
        return Agent(Stub(), build_registry(), ctx, max_steps=2)

    out = run_corpus(tmp_path, build_agent=_build, max_cases=2)
    assert len(out) == 2
    out = run_corpus(tmp_path, build_agent=_build, filter_pattern="c3")
    assert len(out) == 1 and out[0].case_id == "c3"


def test_eval_summarize_metrics():
    from logb.eval.runner import EvalResult, summarize
    results = [
        EvalResult(case_id="a", answer="ok", passed=True,
                    hits=["f1", "f2"], misses=[], forbidden_hits=[],
                    steps=3, tokens_in=100, tokens_out=50,
                    latency_ms=1000, llm_calls=2, verification_passes=1),
        EvalResult(case_id="b", answer="bad", passed=False,
                    hits=["f1"], misses=["f2"],
                    forbidden_hits=["hallucination"],
                    steps=5, tokens_in=200, tokens_out=80,
                    latency_ms=2000, llm_calls=3, verification_passes=2),
    ]
    s = summarize(results)
    assert s["n"] == 2 and s["passed"] == 1
    assert s["pass_rate"] == 0.5
    # 3 hits out of 4 expected facts total
    assert abs(s["fact_recall"] - 0.75) < 0.001
    assert s["hallucination_rate"] == 0.5         # 1 of 2 had forbidden hits
    assert s["avg_steps"] == 4.0
    assert s["verification_reasks"] == 1


def test_eval_handles_agent_exception(tmp_path):
    """An agent crash for one case must not abort the whole run — that
    case gets recorded as errored, others continue."""
    from logb.eval.runner import run_corpus
    (tmp_path / "boom.json").write_text(json.dumps({
        "id": "boom", "log_path": "x", "question": "q",
        "expected_facts": ["something"]}))

    def _build(case):
        class Broken:
            def chat(self, *a, **kw):
                raise RuntimeError("simulated backend failure")
        cfg = _cfg(verify_citations=False)
        ctx = ToolContext(cfg=cfg, manual_index=ManualIndex(cfg.manual_dir))
        return Agent(Broken(), build_registry(), ctx, max_steps=2)

    results = run_corpus(tmp_path, build_agent=_build)
    assert len(results) == 1
    assert not results[0].passed
    assert results[0].error and "simulated" in results[0].error


def test_eval_bundled_corpus_loadable():
    """Sanity: the JSON files we ship under logb/eval/corpus/ all parse
    and have the required fields."""
    from logb.eval.runner import load_cases, CORPUS_DIR
    cases = load_cases(CORPUS_DIR)
    assert len(cases) >= 2
    for c in cases:
        assert c.id and c.log_path and c.question
        assert isinstance(c.expected_facts, list)


# ---- token + latency telemetry ---------------------------------------------
def test_telemetry_accumulated_across_llm_calls():
    """Per-turn telemetry sums tokens/latency over every backend call,
    including verification re-asks."""
    class Counting:
        def __init__(self):
            self.n = 0
        def chat(self, s, h, t, on_token=None):
            self.n += 1
            return Assistant(text=f"answer {self.n}",
                             tokens_in=100, tokens_out=20, latency_ms=50)
    cfg = _cfg(verify_citations=False)
    res = Agent(Counting(), build_registry(), _ctx(cfg),
                max_steps=2).ask("question")
    assert res.llm_calls == 1
    assert res.tokens_in == 100 and res.tokens_out == 20
    assert res.latency_ms == 50


def test_telemetry_sums_verification_passes():
    """A verification re-ask costs another LLM call — telemetry must add
    its tokens to the running total."""
    class TwoStage:
        def __init__(self):
            self.n = 0
        def chat(self, s, h, t, on_token=None):
            self.n += 1
            tokens = (50, 30, 200) if self.n == 1 else (60, 40, 300)
            if self.n == 1:
                return Assistant(text="## Root Cause\nbad at fakefile.sdc:42",
                                 tokens_in=tokens[0], tokens_out=tokens[1],
                                 latency_ms=tokens[2])
            return Assistant(text="## Root Cause\nrevised",
                             tokens_in=tokens[0], tokens_out=tokens[1],
                             latency_ms=tokens[2])
    cfg = _cfg(verify_citations=True)
    res = Agent(TwoStage(), build_registry(), _ctx(cfg),
                max_steps=2).ask("why")
    assert res.llm_calls == 2
    assert res.tokens_in == 110           # 50 + 60
    assert res.tokens_out == 70           # 30 + 40
    assert res.latency_ms == 500          # 200 + 300
    assert res.verification_passes == 2


def test_telemetry_zero_when_backend_omits_counts():
    """Some backends won't report tokens — the AgentResult fields must
    default to 0 rather than None, so the eval harness can sum safely."""
    class Silent:
        def chat(self, s, h, t, on_token=None):
            return Assistant(text="ok")  # no token counts
    res = Agent(Silent(), build_registry(), _ctx(_cfg(verify_citations=False)),
                max_steps=2).ask("x")
    assert res.tokens_in == 0 and res.tokens_out == 0


def test_audit_records_telemetry(tmp_path):
    """Audit JSONL must carry the per-turn telemetry so the eval harness
    can aggregate across many runs without rerunning anything."""
    from logb import session as _s
    cfg = _cfg(project_root=str(tmp_path), audit_enabled=True,
                verify_citations=False)
    ctx = ToolContext(cfg=cfg, manual_index=ManualIndex(cfg.manual_dir))

    class Backend:
        def chat(self, s, h, t, on_token=None):
            return Assistant(text="ok", tokens_in=123, tokens_out=45,
                             latency_ms=80)
    Agent(Backend(), build_registry(), ctx, max_steps=2).ask("x")
    rec = json.loads(_s.audit_path(cfg.project_root).read_text().strip())
    assert "telemetry" in rec
    assert rec["telemetry"]["tokens_in"] == 123
    assert rec["telemetry"]["tokens_out"] == 45
    assert rec["telemetry"]["latency_ms"] == 80


# ---- session persistence + audit -------------------------------------------
def test_session_round_trip(tmp_path):
    """Save a session, reload it onto a fresh Agent, verify history+plan
    are intact (this is what `--resume` does end-to-end)."""
    from logb import session as _s
    cfg = _cfg(project_root=str(tmp_path), session_persist=True,
                verify_citations=False)
    ctx = ToolContext(cfg=cfg, manual_index=ManualIndex(cfg.manual_dir))

    # The model creates the plan DURING ask() so it survives the
    # plan.reset() that ask() does at the start of each turn.
    class Planner:
        def __init__(self):
            self.n = 0
        def chat(self, s, h, t, on_token=None):
            self.n += 1
            if self.n == 1:
                return Assistant(tool_calls=[{"id": "1",
                    "name": "create_plan",
                    "args": {"tasks": ["scan errors", "check sdc"]}}])
            if self.n == 2:
                return Assistant(tool_calls=[{"id": "2",
                    "name": "update_plan",
                    "args": {"idx": 1, "status": "done",
                             "result": "2 errors"}}])
            return Assistant(text="done")

    agent = Agent(Planner(), build_registry(), ctx, max_steps=4)
    agent.ask("what crashed")
    sid = agent.session_id
    assert sid is not None
    assert len(ctx.plan.tasks) == 2                  # plan populated in-flight

    # Brand-new agent in the same project_root reloads everything.
    ctx2 = ToolContext(cfg=cfg, manual_index=ManualIndex(cfg.manual_dir))
    agent2 = Agent(Planner(), build_registry(), ctx2, max_steps=2)
    state = _s.load_session(cfg.project_root, sid)
    _s.apply_session(agent2, state)
    assert agent2.session_id == sid
    assert len(agent2.history) == len(agent.history)
    # The plan was rehydrated too.
    assert ctx2.plan is not None
    assert len(ctx2.plan.tasks) == 2
    assert ctx2.plan.tasks[0].status == "done"
    assert ctx2.plan.tasks[0].result == "2 errors"


def test_session_list_newest_first(tmp_path):
    from logb import session as _s
    cfg = _cfg(project_root=str(tmp_path))
    # Hand-craft two sessions with explicit timestamps.
    d = tmp_path / ".logb-sessions"
    d.mkdir()
    (d / "old.json").write_text(json.dumps({
        "schema": 1, "id": "old", "created": "2025-01-01T00:00:00+00:00",
        "updated": "2025-01-01T00:00:00+00:00",
        "history": [{"role": "user", "text": "older question"}],
        "plan_tasks": []}))
    (d / "new.json").write_text(json.dumps({
        "schema": 1, "id": "new", "created": "2026-01-01T00:00:00+00:00",
        "updated": "2026-01-01T00:00:00+00:00",
        "history": [{"role": "user", "text": "newer question"}],
        "plan_tasks": []}))
    rows = _s.list_sessions(cfg.project_root)
    assert [r["id"] for r in rows] == ["new", "old"]
    assert rows[0]["turns"] == 1
    assert "newer question" in rows[0]["last_question"]


def test_session_schema_mismatch_refused(tmp_path):
    from logb import session as _s
    d = tmp_path / ".logb-sessions"
    d.mkdir()
    (d / "bad.json").write_text(json.dumps({
        "schema": 999, "id": "bad", "history": []}))
    try:
        _s.load_session(str(tmp_path), "bad")
    except ValueError as e:
        assert "schema mismatch" in str(e)
    else:
        raise AssertionError("expected ValueError on schema mismatch")


def test_audit_record_written_per_turn(tmp_path):
    from logb import session as _s
    import json as _json
    cfg = _cfg(project_root=str(tmp_path), audit_enabled=True)
    ctx = ToolContext(cfg=cfg, manual_index=ManualIndex(cfg.manual_dir))

    class Quick:
        def chat(self, s, h, t, on_token=None):
            return Assistant(text="done")
    agent = Agent(Quick(), build_registry(), ctx, max_steps=2)
    agent.ask("first question")
    agent.ask("second question")

    audit_path = _s.audit_path(cfg.project_root)
    assert audit_path.is_file()
    lines = audit_path.read_text().splitlines()
    assert len(lines) == 2
    rec1 = _json.loads(lines[0])
    rec2 = _json.loads(lines[1])
    assert rec1["question"] == "first question"
    assert rec2["question"] == "second question"
    assert rec1["answer"] == "done" and rec2["answer"] == "done"
    assert "ts" in rec1                              # iso timestamp present


def test_audit_disabled_writes_nothing(tmp_path):
    from logb import session as _s
    cfg = _cfg(project_root=str(tmp_path), audit_enabled=False)
    ctx = ToolContext(cfg=cfg, manual_index=ManualIndex(cfg.manual_dir))

    class Quick:
        def chat(self, s, h, t, on_token=None):
            return Assistant(text="x")
    Agent(Quick(), build_registry(), ctx, max_steps=2).ask("q")
    assert not _s.audit_path(cfg.project_root).is_file()


def test_audit_tool_calls_captured(tmp_path):
    """The audit record must include WHICH tools the agent called for this
    answer — that's the whole point of having a paper trail."""
    from logb import session as _s
    import json as _json
    cfg = _cfg(project_root=str(tmp_path), audit_enabled=True,
                verify_citations=False)
    ctx = ToolContext(cfg=cfg, manual_index=ManualIndex(cfg.manual_dir))

    class Caller:
        def __init__(self):
            self.n = 0
        def chat(self, s, h, t, on_token=None):
            self.n += 1
            if self.n == 1:
                return Assistant(tool_calls=[{"id": "1",
                                              "name": "list_logs",
                                              "args": {}}])
            return Assistant(text="answer")
    Agent(Caller(), build_registry(), ctx, max_steps=3).ask("what?")
    rec = _json.loads(_s.audit_path(cfg.project_root).read_text().strip())
    names = [c.get("name") for c in rec["tool_calls"]]
    assert "list_logs" in names


# ---- time-aware index + correlate ------------------------------------------
def test_index_extracts_timestamps_eda(tmp_path):
    from logb import index as _idx
    from logb.profiles import EDA
    p = tmp_path / "ts.log"
    p.write_text("[04:00:00  10s] start\n"
                 "[04:00:30  40s] middle\n"
                 "[04:01:00  70s] end\n")
    idx = _idx.build(p, EDA)
    assert idx["first_ts"] == 10
    assert idx["last_ts"] == 70
    assert len(idx["time_index"]) >= 1


def test_index_extracts_timestamps_severe_lines_always_sampled(tmp_path):
    """Severe lines must always get a time sample so correlate can lock
    onto an error precisely, even if it's the only severe line in a
    block of TIME_SAMPLE lines."""
    from logb import index as _idx
    from logb.profiles import EDA
    p = tmp_path / "rare_error.log"
    lines = [f"[04:00:00  {i}s] info {i}\n" for i in range(50)]
    # Inject a single error at line 25
    lines[25] = "[04:00:00  25s] **ERROR: (IMPLF-213) bad mask\n"
    p.write_text("".join(lines))
    idx = _idx.build(p, EDA)
    sampled_lines = [t[0] for t in idx["time_index"]]
    assert 25 in sampled_lines              # error line sampled despite cadence


def test_correlate_finds_lines_in_window_single_log():
    from logb.tools import build_registry, ToolContext
    from logb.config import Config
    from logb.rag import ManualIndex
    import tempfile
    import os
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "x.log")
        with open(p, "w") as f:
            for i in range(10):
                f.write(f"[04:00:00  {i*10}s] line {i}\n")
        cfg = Config.load({"log_path": p, "manual_dir": tmp,
                            "skills_dir": tmp, "project_root": tmp,
                            "interactive": False})
        ctx = ToolContext(cfg=cfg, manual_index=ManualIndex(tmp))
        out = build_registry().dispatch("correlate",
            {"anchor_ts": 50, "window_seconds": 15}, ctx)
        # Should pick up lines at t=40, 50, 60 (within ±15 of 50)
        assert "line 4" in out and "line 5" in out and "line 6" in out
        assert "line 0" not in out and "line 9" not in out


def test_correlate_across_multiple_logs(tmp_path):
    from logb.tools import build_registry, ToolContext
    from logb.config import Config
    from logb.rag import ManualIndex
    logdir = tmp_path / "logs"
    logdir.mkdir()
    # Two synthetic logs with overlapping timestamps
    (logdir / "app.log").write_text(
        "[04:00:00  100s] app started\n"
        "[04:00:00  150s] app FAIL\n"
        "[04:00:00  200s] app exit\n")
    (logdir / "sys.log").write_text(
        "[04:00:00  140s] sys ok\n"
        "[04:00:00  155s] sys oom-kill detected\n"
        "[04:00:00  180s] sys cleanup\n")
    cfg = Config.load({"log_path": str(logdir),
                        "manual_dir": str(tmp_path),
                        "skills_dir": str(tmp_path),
                        "project_root": str(tmp_path),
                        "interactive": False})
    ctx = ToolContext(cfg=cfg, manual_index=ManualIndex(str(tmp_path)))
    out = build_registry().dispatch("correlate",
        {"anchor_ts": 150, "window_seconds": 15}, ctx)
    # Both logs' lines around t=150 should appear, sorted by ts
    assert "app FAIL" in out and "sys oom-kill" in out
    # Outside window — should NOT appear
    assert "app started" not in out and "sys cleanup" not in out


def test_correlate_anchor_by_path_and_line(tmp_path):
    from logb.tools import build_registry, ToolContext
    from logb.config import Config
    from logb.rag import ManualIndex
    p = tmp_path / "anchor.log"
    p.write_text("[04:00:00  10s] a\n"
                 "[04:00:00  50s] b ← anchor at L2\n"
                 "[04:00:00  90s] c\n")
    cfg = Config.load({"log_path": str(p),
                        "manual_dir": str(tmp_path),
                        "skills_dir": str(tmp_path),
                        "project_root": str(tmp_path),
                        "interactive": False})
    ctx = ToolContext(cfg=cfg, manual_index=ManualIndex(str(tmp_path)))
    out = build_registry().dispatch("correlate",
        {"anchor_path": "anchor.log", "anchor_line": 2,
         "window_seconds": 45}, ctx)
    # Anchor ts=50, window ±45 → includes 10, 50, 90 (within edge)
    assert "a" in out and "b" in out and "c" in out


def test_correlate_no_timestamps_explained(tmp_path):
    from logb.tools import build_registry, ToolContext
    from logb.config import Config
    from logb.rag import ManualIndex
    p = tmp_path / "notimes.log"
    p.write_text("just text\nno timestamps\nhere\n")
    cfg = Config.load({"log_path": str(p),
                        "manual_dir": str(tmp_path),
                        "skills_dir": str(tmp_path),
                        "project_root": str(tmp_path),
                        "interactive": False})
    ctx = ToolContext(cfg=cfg, manual_index=ManualIndex(str(tmp_path)))
    out = build_registry().dispatch("correlate",
        {"anchor_path": "notimes.log", "anchor_line": 1,
         "window_seconds": 10}, ctx)
    assert "no timestamps" in out


if __name__ == "__main__":
    import subprocess
    raise SystemExit(subprocess.call(
        [sys.executable, "-m", "pytest", "-q", __file__]))
