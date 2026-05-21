---
name: diagnose-eco-flow
description: Diagnose Innovus ECO (Engineering Change Order) failures — pre-mask ECO from Verilog/DEF/eco-file, post-mask ECO with spare cells or gate-array cells
when_to_use: ecoDesign / ecoPlace / applyEco ERRORs, "spare cell exhausted", "cannot map to GA cell", "ECO instance not found", post-ECO timing regression worse than pre-ECO, or ECO Verilog has constructs the original netlist didn't
domain: any
executable: false
---

# Playbook: ECO flow failures

ECO flows are deceptively brittle: the engine *expects* the new netlist to be
a small diff from the old one, and it *expects* a pool of pre-placed
spare/gate-array cells big enough and well-placed enough to absorb the diff.
When either expectation is wrong, the failure mode is "ECO ran but the design
got worse" — not a crash.

1. **Identify the ECO mode.** `read_logs` with
   `pattern="ecoDesign|ecoPlace|applyEco|ecoChangeCell|ECO Verilog|spareCells|gateArray|GA"`.
   The mode determines which chapter applies — pre-mask from Verilog
   (free placement), pre-mask from DEF (positions fixed), pre-mask from
   eco-file (directive list), post-mask spare-cell (only spare slots may
   change), post-mask gate-array (GA cells convert to logic), post-mask
   GA-filler. Note the exact mode and any eco-file path.

2. **Read the diff summary.** `read_logs` for
   `pattern="ECO Summary|cells added|cells deleted|nets|spare.*used|GA.*used"`.
   The engine prints the size of the diff and how many spare/GA cells it
   consumed. Compare to what was available. The cause is almost always
   one of:
   - diff bigger than the spare-cell pool (ran out of slots),
   - diff needs a cell type the pool doesn't contain,
   - new logic needs nets across regions the GA pool doesn't cover,
   - ECO Verilog renames an instance the original placement pinned by name,
   - eco-file references nets that no longer exist after a prior ECO.

3. **Open the ECO source.** `read_file` the ECO Verilog or eco-file the run
   loaded. If it's Verilog, diff it mentally against the original:
   - new instances of cells *not* in the spare-cell list → impossible by
     construction in post-mask, the cell types must already be physical,
   - port-name changes → ECO will re-route; if PostRoute, this is huge,
   - hierarchy changes → spare-cell scopes may not contain the new path.

4. **Manual cross-reference.** `search_manual` for "ECO Flows", "Pre-Mask
   ECO Changes from a New Verilog File", "Post-Mask ECO Changes from a
   New Verilog Netlist (Using Spare Cells Flow)", "Post-Mask ECO Changes
   from a New Netlist (Using Gate Array Cells Flow)", "ECO Directives",
   or "HECO Directives". Each mode has its own constraint list — match
   the failure to the named restriction.

5. **Re-run pre/post timing.** If a timing report straddles the ECO, get
   the WNS/TNS *before* and *after* from the log. ECO that worsens timing
   means the spare-cell placement is too far from the connecting fanin;
   ECO that improves nothing means the directives were no-ops (named
   instances didn't exist). Both look like "ECO didn't help" but the fix
   is different.

6. **Conclude.** Report:
   - which ECO mode the run was in and what it tried to change,
   - which class the failure belongs to (pool size, pool composition,
     placement of pool, source-file mismatch, scope mismatch),
   - the fix (regenerate spare cells with `addSpareCells` at the right
     module scope, swap the GA filler list, fix the ECO Verilog port
     names, split the ECO into smaller batches),
   - re-run point: `ecoDesign` / `ecoPlace` is usually enough; only
     re-route the changed nets,
   - prevention: spec a *minimum* spare-cell budget per module in the
     floorplan, so future ECOs can't fail by exhaustion.
