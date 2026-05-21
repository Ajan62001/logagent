---
name: diagnose-mmmc-setup
description: Diagnose Innovus Multi-Mode Multi-Corner (MMMC) view configuration errors — missing/extra views, mode/corner mismatches, unloaded SDC or libraries
when_to_use: init_design / create_constraint_mode / create_analysis_view ERRORs, "no active view", "view not found", "library set missing", "scenario not enabled", or per-view WNS/TNS that look impossible (e.g. only typical corner missing)
domain: any
executable: false
---

# Playbook: MMMC view / scenario setup

Most "the tool ignored my constraints" or "only one corner is failing" reports
trace back to the MMMC config — the design has the right libraries but the
wrong *combination* of mode × corner × delay-corner enabled.

1. **Find the MMMC init.** `read_logs` with
   `pattern="create_constraint_mode|create_delay_corner|create_analysis_view|set_analysis_view|create_library_set|create_rc_corner"`.
   Capture every `create_*` and `set_analysis_view` call in order.

2. **Reconstruct the view matrix.** From those calls, list:
   - the constraint modes (each maps to one SDC),
   - the library sets (each maps to one .lib timing library),
   - the RC corners (each maps to one captable / QRC tech file),
   - the delay corners (= library_set × RC corner),
   - the analysis views (= constraint_mode × delay_corner),
   - which views were activated by `set_analysis_view -setup …` and
     `-hold …`.

   The bug is almost always either a missing pair in the matrix or a view
   that exists but was not activated.

3. **Search the manual.** `search_manual` for "Configuring the Setup for
   Multi-Mode Multi-Corner Analysis", "create_analysis_view",
   "set_analysis_view", or "Optimizing Timing in On-Chip Variation Analysis
   Mode". The MMMC chapter shows the canonical order and the failure modes
   the engine prints (e.g. "active setup views must include all corners
   intended for optimization").

4. **Open the view-definition file.** `read_file` the MMMC TCL the log
   sourced. Look for:
   - an SDC referenced by `create_constraint_mode` whose file path is wrong
     or whose `create_clock` differs from another mode,
   - a `create_library_set` missing the slow/fast `.lib` variant,
   - `set_analysis_view -setup` listing only one of N analysis views,
   - OCV derates set in only one mode (`set_timing_derate -early/-late`
     scoped wrong),
   - `set_interactive_constraint_modes` left enabled from a prior session.

5. **Sanity-check with the timing reports.** If the log produced
   `report_analysis_views` or per-view timing summaries, `read_file`. The
   tell is: the failing path uses a clock that only exists in one
   constraint mode, or the failing corner is one the user did *not* list in
   `set_analysis_view -hold`.

6. **Conclude.** Report:
   - which slot in the matrix is wrong (missing / extra / not activated),
   - the exact `create_*` or `set_analysis_view` line responsible,
   - the fix (add the missing library set, activate the view, repath the
     SDC),
   - re-run point (`init_design` to reload MMMC if libraries changed;
     `set_analysis_view` alone if just activation changed),
   - prevention: add a `report_analysis_views` print after MMMC setup so
     the matrix is visible at the top of every log.
