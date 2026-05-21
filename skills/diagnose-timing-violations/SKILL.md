---
name: diagnose-timing-violations
description: Triage Innovus setup/hold WNS or TNS misses after optDesign (PreCTS, PostCTS, PostRoute) and identify the originating violation, not the symptom
when_to_use: optDesign / timeDesign reports negative WNS or non-zero violating paths; "Worst Negative Slack" line shows a regression between stages; PostRoute hold or setup TNS spikes
domain: any
executable: false
---

# Playbook: timing WNS/TNS triage

Timing reports show *every* failing endpoint, but the agent's job is to find
the *one* upstream cause that produced the cluster. Treat the report as a
shape, not a list.

1. **Find the failing stage.** `read_logs` with
   `pattern="Starting \"opt|timeDesign|Starting \"route"` and locate the
   stage that owns the WNS/TNS regression (preCTS, postCTS, postRoute,
   postRoute hold). The "After … Optimization" summary block prints
   per-mode/per-corner WNS and TNS — capture both numbers and the corner.

2. **Identify mode + corner.** `read_logs` for
   `pattern="setup|hold|WNS|TNS|view"` near that banner. A setup miss in one
   MMMC view but not others is almost always a missing or mis-derated view —
   not a real path issue. A hold miss only at the fast corner is a
   PostRoute hold-fix gap.

3. **Open the path report.** If the log names a `*.tarpt` / `report_timing`
   output, `read_file` it. Look at:
   - the launch and capture clocks (same clock? cross-clock?),
   - the *first* cell on the path (driver too weak? in a region with
     congestion-induced detour?),
   - the slack distribution: one outlier path or hundreds of similar paths.

   A single bad path = a specific net/cell. A cluster with the same launch
   clock = a CTS or clock-uncertainty issue. A cluster with the same
   start-point register = a fanout/placement issue.

4. **Corroborate with the manual.** `search_manual` for "Optimizing Timing",
   "Performing PostRoute Optimization", "Optimizing Timing in On-Chip
   Variation Analysis Mode", or the specific knob the log mentions
   (`setOptMode`, `set_interactive_constraint_modes`,
   `set_analysis_view`). The manual's "Optimizing Timing" chapter
   enumerates which optimization is allowed at which stage — many
   regressions are because hold fixing was deferred to PostRoute when it
   should have run PostCTS.

5. **Trace cross-stage regression.** Compare WNS at PreCTS → PostCTS →
   PostRoute (the log prints all three). A jump means that stage *caused*
   the violation: PostCTS jump → CTS buffering hurt a path; PostRoute jump →
   detour routing or SI; preCTS already bad → constraint/synthesis issue
   not Innovus.

6. **Conclude.** Report:
   - which stage introduced the regression (or "was bad from PreCTS" if
     upstream),
   - the offending mode/corner and a representative endpoint,
   - root cause (constraint, derate, congestion, buffer choice, hold-fix
     skipped),
   - the fix (`setOptMode -fixHoldAllowOverlap`, tighter
     `set_clock_uncertainty`, congestion relief, etc.),
   - re-run point (`optDesign -postRoute -hold` is usually enough; don't
     re-run from placement unless PreCTS was already bad).
