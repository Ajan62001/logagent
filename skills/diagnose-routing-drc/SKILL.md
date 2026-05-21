---
name: diagnose-routing-drc
description: Root-cause Innovus NanoRoute / verify_drc violations (shorts, spacing, antenna, via, metal-density) and decide whether it is a router, constraint, or LEF issue
when_to_use: routeDesign / detailRoute ends with non-zero DRC; verify_drc / verifyConnectivity / verifyProcessAntenna report markers; "shorts" or "opens" reported in PostRoute
domain: any
executable: false
---

# Playbook: routing DRC / connectivity

NanoRoute will run to completion even when it can't close DRCs — the
violations live in `verify_drc` and `verifyConnectivity`, not in the route
banner. Always finish with verify, not with the router's exit status.

1. **Confirm the router ran.** `read_logs` with
   `pattern="Starting \"route|routeDesign|detailRoute"`. Capture exit
   summary: number of DRCs, shorts, opens, antenna violations.

2. **Run the violation census.** `read_logs` with
   `pattern="verify_drc|verifyConnectivity|verifyProcessAntenna|Total Viol"`
   to read each verify command's tail. Note: the *type* (Short / Spacing /
   MinArea / MinStep / EndOfLine / Antenna / Open / Unconnected), the
   *layer*, and the *count*. The distribution tells you the cause more than
   the marker location does:
   - Many Shorts on one layer → routing-resource exhaustion (congestion).
   - MinStep / EndOfLine concentrated near pins → access / pin-LEF issue.
   - Antenna only → fix with `setNanoRouteMode -drouteFixAntenna`, not a
     real defect.
   - Opens reported by `verifyConnectivity` → ECO/wiring edit, not a routing
     failure.

3. **Map to the manual.** `search_manual` for the exact verify command
   (`verify_drc`, `verifyProcessAntenna`, `verifyConnectivity`) and for
   "Identifying and Viewing Violations". The "Verifying DRC" section
   explains which violations are routing's responsibility vs. which require
   floorplan or LEF changes. `verifyConnectivity` "Opens" do **not** mean
   routing failed — see the section on debugging opens interactively.

4. **Inspect a representative marker.** Pick the highest-count violation
   type. `read_logs` with a `pattern=` for its message id to get a few
   coordinate lines, then if the violation report file is named in the log
   use `read_file` on it. Confirm the layer/cell — a Short between
   `M3` over a macro pin is a pin-access problem, not a routing-track
   problem.

5. **Check upstream candidates.**
   - Congestion-driven shorts → re-place with more padding (see
     [[diagnose-placement-congestion]]), not re-route.
   - Antennas → add diodes / enable `-drouteFixAntenna`.
   - Pin-access MinStep → LEF/pin geometry, escalate to library, not router.
   - Metal density → `addMetalFill` was skipped; re-run chip-finishing.
   - Opens → an ECO edit removed a wire; re-route the affected nets.

6. **Conclude.** Report:
   - dominant violation type + layer + count,
   - root cause (router knob / congestion / LEF / chip-finish skipped),
   - the exact fix (one `setNanoRouteMode`, one `addMetalFill`, one ECO
     re-route, etc.),
   - re-run point (`routeDesign -incremental` is usually right; full
     re-route only for catastrophic short clusters),
   - prevention: add the matching `verify_*` to the flow as a gate after
     routing so the next run can't hide DRCs.
