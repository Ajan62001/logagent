---
name: diagnose-import-lef-def
description: Diagnose Innovus design-import failures — LEF / DEF / Verilog / library load errors at init_design or read_mmmc
when_to_use: init_design / loadConfig / read_verilog / read_lef / defIn ERRORs at the start of the run; "unsupported LEF syntax", "missing macro", "undriven net", "no such cell in library", or import aborts before the Starting "place" banner
domain: any
executable: false
---

# Playbook: import / data-prep failures

Import errors are cheap to triage *and* cheap to fix — the run never reached
optimization, so there is no cascade. The bug is in one of: LEF, DEF, Verilog
netlist, library, or the import script's order. Find which file, find which
line.

1. **Confirm the run never started physical work.** `read_logs` with
   `pattern="Starting \"place|Starting \"opt|Starting \"route"` — if none of
   these appear, the failure is at import. `read_logs` with
   `pattern="init_design|loadConfig|read_verilog|read_lef|defIn|read_mmmc"`
   to find the last import command that ran.

2. **Capture the first ERROR after import begins.** `read_logs` with
   `severity=error` and read top-to-bottom. The *first* error is the cause;
   subsequent ones are almost always "cannot continue because step N
   failed". Note its message id and any file path / line number it cites.

3. **Classify the input.**
   - *LEF* — "unsupported LEF syntax", "MACRO not found", "missing SITE",
     "PIN has no LAYER".
   - *DEF* — "version mismatch", "COMPONENTS count mismatch",
     "unconnected pin in DEF", "row site not in LEF".
   - *Verilog* — "module not found", "undriven net", "port mismatch",
     "blackbox <name>".
   - *Library / .lib* — "no library set for view", ".lib parsing failed",
     "function not defined for pin".
   - *Tech / captable / QRC* — "cap table layer mismatch", "captable
     missing for corner".

4. **Manual cross-reference.** `search_manual` for the specific section:
   "Preparing Physical Libraries", "Unsupported LEF and DEF Syntax",
   "Preparing the Design Netlist", "Preparing Timing Libraries", "The
   init_design Import Flow", or "Verifying Data before Importing a Design".
   The "Unsupported LEF and DEF Syntax" section in particular lists every
   construct Innovus rejects — match the warning text to the entry.

5. **Open the offending file at the offending line.** `read_file` with the
   line cited by the error. Confirm the construct with your eyes — do not
   recommend "fix the LEF" without naming the exact statement.

6. **Conclude.** Report:
   - which input file is wrong (LEF / DEF / Verilog / lib / tech),
   - the specific construct or missing entity (cell name, port name, layer,
     syntax keyword),
   - the fix (regenerate the LEF, edit the Verilog port list, swap the
     `.lib` for the right corner, fix the `init_design` `-mmmc_file`
     path),
   - re-run point: re-`init_design`; nothing else has run, so nothing
     downstream needs reverting,
   - prevention: add a `check_design -all` / `verify_*` gate at the end of
     import so future broken inputs fail at second 1, not minute 30.
