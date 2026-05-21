---
name: diagnose-cts-skew
description: Trace Innovus clock-tree synthesis problems (excessive skew, insertion delay, CCOpt failures) back to constraint or library cause
when_to_use: ccopt_design / clockDesign ERRORs, skew/insertion-delay above target in CTS reports, "no buffer/inverter found", unbalanced sinks, CCOpt property warnings
domain: any
executable: false
---

# Playbook: CTS skew / insertion delay

A bad clock tree is almost always a bad *spec* — wrong buffer list, wrong
clock definition in the SDC, or a property the engineer didn't realise was
acting as a hard constraint. Confirm the spec before blaming the engine.

1. **Locate the CTS phase.** `read_logs` with
   `pattern="Starting \"ccopt|Starting \"cts|clockDesign|ccopt_design"`.
   Capture the start banner and the first WARNING/ERROR after it.

2. **Pull the CTS summary.** `read_logs` with
   `pattern="Target Skew|Insertion Delay|Max Skew|Max Trans|sinks|CCOpt"` —
   the engine prints a per-clock table with achieved vs. target. Note which
   clock and which metric missed, and by how much.

3. **Check the clock spec.** `search_manual` for "Concepts and Clock Tree
   Specification", "CCOpt Property System", or the exact property name the
   log warned about. The manual section "CCOpt Properties" (syntax chapter)
   lists every knob the engine honours — many warnings reference a property
   the user set in a CCOpt spec or via `set_ccopt_property`.

4. **Verify the inputs.** Open the CTS spec / library list that the log
   cites with `read_file`. Common upstream causes:
   - buffer/inverter list too narrow (no cell with the needed drive),
   - missing `create_clock` / wrong period in the SDC,
   - a `set_ccopt_property target_skew` tighter than the technology can hit,
   - `route_type` for clock nets not configured (non-default rule missing),
   - clock gating cells excluded from the cell list,
   - a useful-skew constraint conflicting with a `set_clock_groups`.

   For SDC problems, `sdc_lint` can flag obvious issues before you read the
   file.

5. **Cross-check the report.** If the run produced `clockTreeReport` /
   `report_ccopt_skew_groups` output, `read_file` it and confirm the sinks
   the engine is balancing match the design intent (no stray gen-clock
   sinks, no missing skew group).

6. **Conclude.** Report:
   - the offending clock and metric (skew / insertion / transition),
   - the root cause (spec/library/SDC), with the exact line you saw it on,
   - the fix (widen buffer list, fix `create_clock`, relax target, add
     `set_ccopt_property`),
   - the stage to re-run from (`ccopt_design` — no need to re-place),
   - a suggestion to add a CCOpt summary check to the flow so the next miss
     is caught at-stage, not at PostRoute.
