---
name: diagnose-power-pg
description: Diagnose Innovus power-grid problems — addRing / addStripe / sroute / viagen failures, missing PG connections, IR-drop / verifyPowerVia issues
when_to_use: sroute / addStripe / addRing ERRORs or warnings about "no via", "no viarule", "unconnected PG", verifyPowerVia markers, or rail-analysis flags on specific stripes
domain: any
executable: false
---

# Playbook: power planning / PG routing

PG issues split cleanly into three classes — *grid not built*, *grid not
connected*, *grid not robust*. Pick the class first, then the cause.

1. **Find the PG stage.** `read_logs` with
   `pattern="addRing|addStripe|sroute|editPowerVia|setViaGenMode"`. The
   commands run in a fixed order — ring → stripe → sroute → via. The first
   one that errors is the one to fix; later ones are downstream.

2. **Identify the class.**
   - *Not built* — `addStripe` / `addRing` reports "0 stripes added",
     "no candidate layer", or skipped regions. Cause: bad layer pair,
     wrong direction, or no room in the floorplan channel.
   - *Not connected* — `sroute` warns about followpins not connecting to
     stripes, or `verifyConnectivity` lists PG opens. Cause: follow-pin
     layer absent from `-connect`, or block ring not reaching standard-cell
     rail.
   - *Not robust* — `verifyPowerVia` flags missing/insufficient vias, or
     rail-analysis (Voltus / Early Rail) reports IR drop above budget.
     Cause: via cutclass not specified or VIAGEN engine couldn't find a
     viarule for the stripe-pair.

3. **Read the manual.** `search_manual` for "Power Planning and Routing",
   "Generating Special Power Vias Using Viagen", "Trimming Redundant PG
   Stripes and Vias", or the exact failing command (`addStripe`, `sroute`,
   `verifyPowerVia`). The "Functional Overview" table in that chapter maps
   each command to its `set*Mode` setup command — most "0 stripes" errors
   are a missing `setAddStripeMode` / `setViaGenMode` setup.

4. **Open the PG script.** `read_file` the TCL block the log invoked.
   Common defects:
   - layer pair has no defined VIA in LEF → VIAGEN can't bridge,
   - `setViaGenMode -viarule_preference` lists a viarule that does not
     exist in this PDK,
   - `-over_pins 1` used but no stripe-over-pin geometry exists,
   - `sroute -connect { … }` missing one of `padPin / blockPin /
     floatingStripe / corePin`,
   - secondary PG pins (multi-VDD) not enumerated in the net list.

5. **Cross-check with rail analysis.** If a rail-analysis report is named in
   the log, `read_file` it and locate the worst IR-drop instances. If they
   cluster under one ring/stripe gap, the *not robust* diagnosis is
   confirmed.

6. **Conclude.** Report:
   - class (not built / not connected / not robust),
   - the exact PG command line that owns the failure,
   - root cause (mode setup, LEF gap, layer mismatch, missing viarule),
   - the fix (add the `set*Mode` line, add an entry to `-connect`, pick a
     valid viarule, widen the stripe pitch),
   - re-run point (re-run only the failing PG command and downstream — PG
     is incremental; do not re-place to fix a power grid).
