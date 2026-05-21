---
name: diagnose-antenna-violations
description: Triage Innovus antenna / floating-area / dangling-wire violations and decide between auto-fix, diode insertion, and a real routing problem
when_to_use: verifyProcessAntenna reports antenna-ratio violations, "maximum floating area", "unconnected metal segment", or NanoRoute warns "antenna fix iterations exceeded"
domain: any
executable: false
---

# Playbook: antenna violations

Antenna violations are *manufacturing* concerns, not connectivity bugs. Most
are mechanical to fix and shouldn't trigger re-routing of the design — but
the wrong fix (re-route everything) wastes hours. Identify which knob applies
before pulling it.

1. **Confirm the violation type.** `read_logs` with
   `pattern="antenna|verifyProcessAntenna|floating|dangling"`. Three
   distinct categories share this section:
   - *Process antenna* — pin/net ratio exceeds the LEF-declared max for the
     routing layer (gate damage during fab).
   - *Maximum floating area* — an unconnected metal segment whose area
     exceeds the LEF-declared max (no discharge path).
   - *Dangling wire* — a wire stub that's neither connected nor terminated
     in a diode (subset of floating area).
   Note the count per category and the dominant layer.

2. **Manual cross-reference.** `search_manual` for "Verifying Process
   Antennas", "verifyProcessAntenna", "Verifying Maximum Floating Area
   Violations", "Types of Connectivity Violations Reported", or
   "setNanoRouteMode -drouteFixAntenna". The manual section "Verifying
   Process Antennas" enumerates exactly which violations the engine can
   auto-fix.

3. **Check whether auto-fix was enabled.** Search the log for
   `setNanoRouteMode -drouteFixAntenna` / `-routeAntennaCellName` /
   `-routeInsertAntennaDiode`. If it was *not* enabled, the violations are
   probably a config oversight, not a real defect — most antennas are
   fixed by jumping a higher layer or inserting a diode, both of which
   NanoRoute does on demand.

4. **Pick the right remediation per category.**
   - Process antenna with auto-fix off → enable `-drouteFixAntenna 1`,
     re-route incrementally. No design change.
   - Process antenna *with* auto-fix on, still failing → diode pool
     (`-routeAntennaCellName`) doesn't contain the needed cell, or the
     net is so long no number of diodes fits. Add a wider antenna-fix
     cell list, or shorten the offending net via `ecoChangeCell`.
   - Floating area → trim with `editTrimAntenna` or extend to ground via
     `editAddVia`. Manual section: "Trimming Antennas on Selected
     Stripes".
   - Dangling wire → was created by an ECO `editDelete`; investigate the
     ECO that orphaned it before deleting blindly.

5. **Open a representative report.** If `verifyProcessAntenna` produced a
   report file, `read_file` for the worst-ratio violations. Verify the
   PIN is on a real gate input (not a PG pin — PG pins are exempt from
   antenna checking and a warning there is benign).

6. **Conclude.** Report:
   - category + count + dominant layer,
   - whether auto-fix was even enabled,
   - the smallest fix that closes it (one `setNanoRouteMode` change vs.
     widening the diode pool vs. local ECO re-route),
   - re-run point: `routeDesign -incremental` after re-enabling auto-fix
     is usually enough; full re-route only if a metal-layer pair changed,
   - prevention: bake `-drouteFixAntenna` into the standard flow; add
     `verifyProcessAntenna` as a gate immediately after `routeDesign`.
