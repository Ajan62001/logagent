---
name: diagnose-placement-congestion
description: Root-cause Innovus placement legality / density / congestion failures from a placeDesign or checkPlace run
when_to_use: placeDesign ERRORs, checkPlace overflow / unplaced / overlap reports, density-map hotspots, "cannot legalize" or excessive routing congestion after placement
domain: any
executable: false
---

# Playbook: placement congestion / legality

`placeDesign` rarely *invents* a congestion problem — it surfaces a floorplan,
blockage, or library mismatch. Work the chain from what the placer reported
back to what the user can change.

1. **Read the placement banner.** `read_logs` with
   `pattern="Starting \"place"` and again with
   `pattern="checkPlace|Overflow|unplaced|cannot legalize"`. Note the exact
   message, the instance/region it names, and any utilization or
   row-overflow numbers printed (e.g. "Cells overflow: N", "Density: X").

2. **Distinguish legality from congestion.**
   - **Legality** (overlap, off-row, off-grid, unplaced): the placer cannot
     find a slot for specific instances. Look for `checkPlace` violations and
     instance names.
   - **Congestion** (GR/H+V overflow, hot density bins): the placer placed
     everything but routing is infeasible. Look for "H overflow", "V
     overflow", or density >= 0.85 reported by `reportDensityMap`.

3. **Corroborate with the manual.** `search_manual` for `checkPlace`,
   `reportDensityMap`, "Checking Placement", or "Adding Padding" — these
   sections enumerate the recognised violation types and the knobs (padding,
   blockages, density screens) that move the needle.

4. **Find the upstream cause.** Open the floorplan / blockage / scan-chain
   inputs the log cites. Common culprits:
   - over-tight `setPlaceMode -placeIoPins` or a too-small core box,
   - a placement blockage / partition halo that walled off a region,
   - missing well-tap or end-cap cells (`addWellTap` / `addEndCap` skipped),
   - a `setPlaceMode -density` target above what the library can absorb,
   - macro placement that pinned a wide bus into a narrow channel.

   Use `read_file` on the referenced TCL/floorplan to see the exact constraint
   with your own eyes before naming it as the cause.

5. **Conclude.** Report:
   - the single root cause (legality vs. congestion + the upstream constraint),
   - the concrete fix at the source (relax density, widen channel, drop
     blockage, add padding around the hot module, fix scan-chain order, etc.),
   - the stage to re-run from (`placeDesign` after the input is edited; no
     need to re-import unless macros moved),
   - a prevention suggestion (e.g. add a `checkPlace` gate after
     `placeDesign` in the flow, or set a `setPlaceMode -densityScreen` budget
     in CI).
