# Innovus Error & Message Reference (excerpt)

## IMPSDC-3071: object referenced before it is created

**Severity:** ERROR. Emitted while reading an SDC constraint file when a
command references a clock (or other object) that has not yet been created in
the current SDC scope.

**Typical cause:** A `get_clocks`, `set_clock_uncertainty`,
`set_clock_groups`, or `set_input_delay -clock` line appears *above* the
`create_clock` / `create_generated_clock` that defines it. SDC is read
top-to-bottom; forward references are not resolved.

**Fix:** Move all `create_clock` / `create_generated_clock` definitions so
they execute before any command that consumes the clock. Re-run from the
stage that reads the SDC (usually `place`). Validate with
`report_clocks` immediately after sourcing the SDC.

**Downstream impact:** When the clock fails to create, every command that
needs it degrades: endpoints become unconstrained (IMPSDC-3099), CTS finds no
clocks (IMPCTS-5012), and routing has an empty clock topology
(IMPROUTE-7440), which can abort NanoRoute (IMPCORE-9001).

## IMPSDC-3099: timing endpoints have no constraint

**Severity:** WARN. A large number of endpoints are unconstrained. Almost
always a *symptom* of an earlier SDC failure (see IMPSDC-3071), not an
independent problem. Fix the upstream SDC error; this clears on its own.

## IMPCTS-5012: no clock definitions found; skipping CTS

**Severity:** ERROR. Clock Tree Synthesis ran with zero clocks defined. This
is a cascade symptom of a failed/empty SDC clock setup. Do not "fix" CTS —
fix the SDC so clocks exist before `place`/`cts`.

## IMPROUTE-7440: clock nets not routed / clock tree is empty

**Severity:** ERROR. NanoRoute found no clock tree to route because CTS was
skipped. Cascade symptom. Resolve the originating SDC error.

## IMPCORE-9001: NanoRoute terminated abnormally

**Severity:** FATAL. NanoRoute aborted (often SIGSEGV / core dump) on an
unconstrained or empty clock topology. This is the terminal symptom of the
IMPSDC-3071 → IMPCTS-5012 → IMPROUTE-7440 chain. There is no route-side fix;
correct the SDC and re-run the flow from `place`.

## IMPSYN-1023 / IMPFP-221 (informational warnings)

Library voltage mismatch and floorplan aspect-ratio advisories. Not fatal and
unrelated to clock/SDC failures; review separately for QoR but they do not
cause a crash.

## Recommended SDC hygiene

1. One `clocks.sdc` sourced first, defining every `create_clock` /
   `create_generated_clock` up front.
2. Add `report_clocks > clocks.rpt` right after sourcing SDC and gate the
   flow on a non-empty clock list.
3. Lint constraints with `read_sdc -echo` or a pre-flow SDC checker so
   forward references are caught before `place`.
