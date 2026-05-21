---
name: diagnose-oom-runtime
description: Diagnose Innovus OOM / runaway-memory / multi-CPU thrash / killed-by-OS events — runs that died not for a design reason but for a runtime resource reason
when_to_use: log ends with "Killed", "Out of memory", "std::bad_alloc", "exit code 137", sudden truncation with no final banner and no FATAL, or runtime-summary shows memory grew unbounded across stages
domain: any
executable: false
---

# Playbook: OOM / runtime resource failure

A killed run is *not* a design bug — it's a configuration choice that
exceeded the host. Find which stage's memory footprint blew past the host,
and pick the smallest knob that brings it back inside. Don't change the
design.

1. **Confirm it's a resource death, not a tool crash.** `read_logs` for
   the tail with `pattern="Killed|Out of memory|bad_alloc|killed by signal|exit code 137|SIGKILL"`.
   If the tail has no FATAL and no normal "Ending" banner, but the OS log
   or the wrapper script shows the process was killed, this is the
   pattern. A core dump or `Segmentation fault` is a *different* failure
   class — that one belongs to [[diagnose-missing-completion]].

2. **Reconstruct the memory curve.** Innovus prints periodic resource
   summaries — `read_logs` with `pattern="Memory|MEM|CPU|elapsed|peak"`
   and read the values in order. The diagnostic is the *shape*:
   - monotone climb across stages → a stage's data structures aren't
     being released (saveDesign without close, or held-open ILMs),
   - sharp jump at one stage → that stage's algorithm exceeded the host
     (e.g. detail route on too-fine an extraction corner set),
   - flat then sudden jump near the end → tracing/reporting expansion
     (e.g. `report_timing -all_paths` on a giant fanout).

3. **Find the multi-CPU configuration.** `read_logs` for
   `pattern="setMultiCpuUsage|setDistributedMode|threads"`. Multi-thread
   and superthread are not free — each worker copies design state. A
   `setMultiCpuUsage -localCpu N` that's too high for the host will OOM
   even if the single-thread footprint would fit.

4. **Manual cross-reference.** `search_manual` for "Memory and Run Time
   Control", "Accelerating the Design Process By Using Multiple-CPU
   Processing", "Running Distributed Processing", "Running Multi-
   Threading", "Running Superthreading", or "Controlling the Level of
   Usage Information in the Log File". The "Memory and Run Time Control"
   section enumerates the knobs available without changing the design.

5. **Match the spike to a stage and a cause.**
   - PostRoute extraction OOM → switch to a lighter QRC corner or fewer
     RC corners for the failing view.
   - SI / glitch OOM → reduce the aggressor set, disable statistical SI,
     run SI per-view.
   - Multi-CPU OOM → drop `-localCpu`, or move to distributed mode where
     workers are separate hosts.
   - Reporting OOM → narrow the `report_timing` scope; don't `-all_paths`
     a million-flop design.
   - Save/restore OOM → save in incremental mode or split partitions.

6. **Conclude.** Report:
   - which stage owned the spike and the peak vs. host RAM,
   - which class (per-stage algorithm / multi-CPU / reporting /
     extraction / save),
   - the fix — *configuration* only (lower thread count, narrower
     report, lighter QRC corner), not a design change,
   - re-run point: re-launch with the smaller configuration; nothing to
     revert in the database,
   - prevention: add a memory-budget guard in the flow (a wrapper that
     trips at host_RAM × 0.8 and downshifts threads) and pre-flight
     report scope at flow start.
