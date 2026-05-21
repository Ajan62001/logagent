---
name: diagnose-ir-drop
description: Triage Innovus rail-analysis / IR-drop failures — Early Rail / Voltus-style reports flagging voltage drop above budget, hot regions, insufficient decap
when_to_use: rail-analysis report exceeds IR-drop budget on VDD/VSS, hotspot maps show concentrated drop under specific instances, "voltage drop >X mV" warnings, or static IR-drop regression between runs
domain: any
executable: false
---

# Playbook: IR-drop / rail-analysis triage

IR-drop is a *grid robustness* problem (this skill), not a *grid
construction* problem (that is [[diagnose-power-pg]]). The grid exists; it
just isn't strong enough where the current actually flows. Find where the
current is going, not where the grid is missing.

1. **Locate the rail-analysis run.** `read_logs` with
   `pattern="rail|IR|voltage drop|Early Rail|Signoff Rail|Voltus|static_ir"`.
   Capture the worst per-rail IR drop and the budget the report compared
   against.

2. **Get the hotspot list.** `read_logs` for
   `pattern="worst|hotspot|top \d+|instance.*mV"` near the rail summary.
   Note the top-N instances and their absolute drop. Cluster behavior is
   the diagnostic signal:
   - One instance / one macro hot → local pin/via shortage under that block.
   - A region (many adjacent instances) → stripe pitch too wide *for the
     local current*, not for the grid in general.
   - Drop concentrated at die edge / ring → ring underweight or pad-to-ring
     resistance too high.
   - Drop at switched-domain entry → power-switch network undersized.

3. **Open the rail report file.** If the log named a
   `*.rpt` / `*.report_rail` artifact, `read_file` it. Look at the
   per-instance contribution and the resistance breakdown the analyzer
   prints (stripe R, via R, follow-pin R, pin R).

4. **Manual cross-reference.** `search_manual` for "Early Rail Analysis",
   "Signoff-Rail Analysis", "Static Power Analysis", "Adding Decoupling
   Capacitance", "Power Analysis and Reports", or "Innovus and Voltus
   Menu Differences". The Early Rail chapter enumerates the inputs (PTCF,
   activity, RC) and which weakness each input failure mimics — many
   "false" IR-drop hits are actually a missing activity file inflating
   peak current.

5. **Separate signal from cause.** Decide:
   - *Real undersize* — high current density on a stripe-thin region.
     Fix: add stripes locally, widen ring, add follow-pin layer.
   - *Insufficient decap* — drop correlates with high switching activity.
     Fix: `addDeCap` near the hot instances (`addDeCapCellCandidates`
     must be defined first).
   - *Pin-access bottleneck* — drop is at the cell PG pin, not on the
     stripe. Fix: more stripe-to-row vias, not more stripes.
   - *Switch network weakness* — drop appears only when power switch is
     on. Fix: more switches, lower switch on-R, larger always-on net.
   - *Analysis input wrong* — activity file missing, wrong corner. Fix:
     re-run rail with correct PTCF; the design is probably fine.

6. **Conclude.** Report:
   - worst rail / worst region / worst mV vs. budget,
   - cluster shape and which of the five cause classes it points to,
   - the localised fix (don't recommend "redo the PG grid" if a stripe
     pair would close it),
   - re-run point (re-`addStripe` / `addDeCap` then re-run rail; no need
     to re-place),
   - prevention: gate the flow on rail analysis at a specific stage
     (post-PG, post-route) so the next regression is caught at-stage.
