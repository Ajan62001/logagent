---
name: diagnose-scan-chain
description: Diagnose Innovus scan-chain / DFT failures — chain reorder errors, broken stitching, scan-enable not propagated, length imbalance, post-ECO scan integrity
when_to_use: scanReorder / optimizeScanChain ERRORs, "scan chain broken", "SI/SO pin mismatch", "scan group not balanced", chain length mismatch between RTL and post-place netlist, or a post-ECO run reports scan-integrity violations
domain: any
executable: false
---

# Playbook: scan chain / DFT integrity

Scan-chain problems are easy to *see* (the tool prints a count) but easy to
fix the *wrong* way — re-ordering will silently re-stitch a chain that the
test pattern set already targets, breaking ATPG coverage. Confirm the
*intent* before touching the chain.

1. **Locate the scan stage.** `read_logs` with
   `pattern="scan|scanReorder|optimizeScanChain|defIn.*SCANDEF|writeScanDef|specifyScanChain"`.
   Note whether a SCANDEF was loaded and whether `scanReorder` ran
   automatically inside `placeDesign` or as an explicit step.

2. **Capture the chain census.** `read_logs` for
   `pattern="chain|chains|scan length|SI |SO |scan enable"`. The engine
   prints chain count, chain lengths, and any unstitched cells. Note:
   - declared chain count (from SCANDEF) vs. found chain count,
   - chain length min/max/mean — large variance signals a reorder issue,
   - any "scan flop not in chain" / "orphan scan cell" warning,
   - whether `set_dont_touch` or partition boundaries excluded cells from
     reorder.

3. **Classify the symptom.**
   - *SCANDEF missing or stale* — chain count doesn't match RTL; cells
     present in netlist but absent from SCANDEF. Fix: regenerate SCANDEF
     from the synth output that produced this netlist.
   - *Reorder broke ATPG-targeted order* — chain count correct but order
     changed and ATPG pattern set was generated against the old order.
     Fix: lock the chain (`setScanReorderMode -reorderMode false` or
     `set_dont_touch` on chain cells) and re-place.
   - *Stitching gap* — SI/SO pin not connected, scan-enable not driven
     into a region. Fix: ECO-stitch the missing segment; do not re-place.
   - *Length imbalance* — one chain is 10× another. Pure performance
     issue; fix only if scan-shift dominates test time.
   - *Partition / hierarchical boundary* — chain crosses a partition that
     was committed; reorder cannot cross it. Fix at the partition spec or
     reorder per-partition.

4. **Manual cross-reference.** `search_manual` for "Optimizing and
   Reordering Scan Chains", "SCANDEF", `scanReorder`,
   "specifyScanChain", or "Editing Pins". The scan-chain section
   describes which constructs are reordered and which are preserved.

5. **Open the SCANDEF and reorder report.** `read_file` the SCANDEF that
   was loaded; verify it lists the cells the netlist actually contains.
   If the run produced a reorder-report artifact, read it for the diff
   of old vs. new chain order — that's the input ATPG needs to update
   its patterns.

6. **Conclude.** Report:
   - declared vs. found chain count + length min/max,
   - which class of symptom and which evidence,
   - the fix (regenerate SCANDEF, lock chain order, ECO-stitch the gap,
     widen the partition pin layer, etc.),
   - re-run point: re-`scanReorder` only if intent was to reorder;
     otherwise re-place with reorder disabled. Note explicitly whether
     ATPG patterns must be regenerated,
   - prevention: add a post-place scan-integrity check
     (`reportScanChain`) and a SCANDEF-vs-netlist consistency gate at
     import.
