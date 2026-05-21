---
name: diagnose-floorplan
description: Diagnose Innovus floorplan setup failures — bad rows / sites, FinFET grid mismatch, partition halo / pin spillover, macro placement infeasibility
when_to_use: floorPlan / createRow / defIn / partition ERRORs, "row site not in LEF", "FinFET grid violation", "partition pin not assignable", "macro overlaps row", or downstream stages (placement/routing) fail in a way that traces back to floorplan geometry
domain: any
executable: false
---

# Playbook: floorplan / partition setup

A bad floorplan rarely fails the floorplan stage itself — it fails *later*,
during placement or routing, in ways that look like the placer's fault. The
work is to recognise the floorplan as the upstream cause and fix it once,
not chase the symptoms.

1. **Locate the floorplan creation.** `read_logs` with
   `pattern="floorPlan|createRow|init_design.*floorplan|defIn|partition|specifyPartition|definePartition|setPartition"`.
   Capture the row spec, die/core boundary, and any partition definitions.

2. **Classify the symptom.**
   - *Row / site geometry* — "row site not in LEF", "row off-grid", "site
     row mismatch", "no SITE for this CORE". Cause: row site name in
     `addRing`/`createRow` doesn't match a SITE in the LEF, or row pitch
     conflicts with FinFET fin pitch.
   - *FinFET grid* — "FinFET grid violation", "instance not on fin grid",
     "implant minimum width". Cause: cell heights mixed across implant
     boundaries without correct fillers.
   - *Macro placement* — "macro overlaps row", "macro outside core",
     "blockage covers row", placement complains later about un-placeable
     instances near a macro edge. Cause: macro halo too large, or macro
     position not snapped to manufacturing grid.
   - *Partition* — "partition pin not assignable", "pin layer not allowed
     on boundary", or ILM export warning. Cause: partition boundary cuts
     through a routing channel the pins want to use, or pin-layer
     constraint excludes every legal layer.

3. **Manual cross-reference.** `search_manual` for "Floorplanning the
   Design", "Common Floorplanning Sequence", "Module Constraint Types",
   "Creating and Editing Rows", "Using Vertical Rows", "Using Multiple-
   height Rows", "FinFET Technology", "Hierarchical Floorplan
   Considerations", or "Hierarchical Partitioning Flow and Capabilities".
   The "Floorplanning the Design" chapter lists the recognised row /
   site / multi-height options; FinFET has its own subsection.

4. **Open the floorplan inputs.** `read_file` the floorplan TCL, the
   `.fp` or restored DEF, and (if hierarchical) the partition definition.
   Verify against the LEF SITE definitions — the cheapest mistake to make
   is a SITE name typo that lets the row be created but produces
   downstream "off-site" complaints.

5. **Cross-check the downstream complaint.** If a *later* stage flagged
   this (placement / routing), match the failing instance coordinates
   against the floorplan: is it near a macro halo? On the wrong row
   class? Inside a partition's "do not touch" region? The geometry
   correspondence is the proof.

6. **Conclude.** Report:
   - which class (row geometry / FinFET / macro / partition),
   - the exact `floorPlan` / `createRow` / `specifyPartition` line that
     is wrong,
   - the fix (rename the SITE, change row pitch, shrink the macro halo,
     widen the partition channel, swap pin-layer constraint),
   - re-run point: re-`floorPlan` or `defIn`, then re-`placeDesign`; do
     not re-run import unless macros or pins changed,
   - prevention: add a `checkFPlan` / `check_design -all` gate after the
     floorplan stage so geometry bugs fail at minute 1, not at hour 3.
