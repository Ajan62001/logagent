---
name: diagnose-si-crosstalk
description: Diagnose Innovus signal-integrity / crosstalk failures — SI delta-delay impact, glitch / noise violations, SI-aware timing degradation
when_to_use: timeDesign or signoff-timing reports show large SI delta-delay vs. nominal, "SI noise violation", "glitch", or PostRoute WNS regresses sharply once SI analysis is enabled
domain: any
executable: false
---

# Playbook: SI / crosstalk triage

SI problems show up as a *delta* between SI-on and SI-off timing or as
glitch markers, never as a hard failure. The triage question is "is this a
real coupling issue, an analysis-setup issue, or a missing-fix-stage
issue?" — those have three different fixes.

1. **Locate the SI run.** `read_logs` with
   `pattern="SI|crosstalk|delta delay|noise|glitch|setSIMode|setAnalysisMode -checkType si"`.
   Confirm SI analysis was actually enabled (`setSIMode -analysisType`)
   and on which views.

2. **Quantify the impact.** `read_logs` for the SI-on vs. SI-off WNS/TNS
   summary (the engine prints both when SI is enabled). Note:
   - the delta WNS,
   - whether the regression is on setup, hold, or both,
   - which clocks/modes are affected,
   - any explicit `glitch` count from `report_noise` / `verify_glitch`.

3. **Classify the cause.**
   - *Real aggressor coupling* — a small set of nets contributes most of
     the delta; victim and aggressor are adjacent on a critical path.
     Fix at routing: spacing, shielding, layer swap.
   - *Analysis setup issue* — SI delta is improbably large (e.g. 30% of
     period) or uniform across uncorrelated paths. Likely missing
     coupling cap data, wrong RC corner, or `setSIMode -analysisType
     statistical` left on. The design is probably fine; the analysis
     isn't.
   - *Glitch / noise* — `verify_glitch` reports above-threshold glitches
     on victim pins, usually clock or async control. Fix at cell choice
     (stronger driver) or shielding.
   - *Fix-stage skipped* — `optDesign -postRoute -si` or `-incremental
     -si` not run after detail route. Fix is to re-run the SI-aware
     PostRoute step, not to redesign anything.

4. **Manual cross-reference.** `search_manual` for "Analyzing and
   Repairing Crosstalk", "Inputs for SI Analysis", "Setting Up Innovus
   for SI Analysis", "Preventing Crosstalk Violations", "Fixing Crosstalk
   Violations", "Optimizing SI Slew and SI Glitches in PostRoute
   Optimization", or "Performing XILM-Based SI Analysis and Fixing". The
   "Inputs for SI Analysis" section enumerates the data the analyzer
   needs — most "uniform large delta" cases are an input gap there.

5. **Inspect a worst aggressor.** If the log names a victim net + top
   aggressor list (or a `report_noise` artifact), `read_file` it. Confirm
   the aggressors are physically adjacent (same layer, parallel run) and
   switching simultaneously with the victim's launch. If they're on
   different layers or in different clock domains, the analyzer is
   reporting a false coupling and the inputs are suspect.

6. **Conclude.** Report:
   - delta WNS/TNS, dominant mode/corner, glitch count if any,
   - which of the four causes the evidence supports,
   - the fix (spacing/shield/layer ECO, fix the SI inputs, re-run
     SI-aware PostRoute opt, add coupling-cap extraction),
   - re-run point: SI-aware `optDesign -incremental -si` is usually
     enough; do not re-route the whole design,
   - prevention: gate the flow on SI-on WNS at PostRoute so a future
     regression is caught at-stage, and verify the SI input set
     (coupling caps, RC corner) at flow start.
