---
name: diagnose-cpf-low-power
description: Diagnose Innovus low-power (CPF or IEEE-1801/UPF) intent errors — missing isolation / level-shifter / power-switch insertion, domain crossing violations, retention failures
when_to_use: read_power_intent / commitCPF / loadCPF / read_upf ERRORs, "isolation cell required", "level shifter missing", "domain crossing not protected", "power switch network not built", or verifyPowerDomain warnings
domain: any
executable: false
---

# Playbook: CPF / IEEE-1801 power intent

Low-power errors are *intent* problems, not *engine* problems. The tool is
telling you the power intent file disagrees with the netlist or with the
library — fix the intent, do not "force" the insertion.

1. **Locate the intent load.** `read_logs` with
   `pattern="read_power_intent|loadCPF|commitCPF|read_upf|commit_upf"`. Find
   the first ERROR/WARNING after that load.

2. **Classify the error.**
   - *Missing cell class* — "no isolation cell defined", "no level shifter
     defined", "no always-on buffer". Cause: the CPF/UPF didn't name a cell
     list of that class, or the library lacks one with that purpose.
   - *Unprotected crossing* — "domain crossing not isolated", "missing
     level shifter from D1 to D2". Cause: power-domain definitions don't
     cover the offending net, or `create_isolation_rule` was scoped wrong.
   - *Switch / retention build failure* — "power switch network has no
     enable", "retention register pair not found". Cause: the always-on
     net or the save/restore signals don't exist in the netlist.
   - *Verify warning at PostRoute* — `verify_power_domain` reports
     domain-crossing issues after routing. Cause: ECO added a path that
     bypassed the isolation.

3. **Manual cross-reference.** `search_manual` for "Support for the Common
   Power Format (CPF)", "Support for IEEE1801", "Power Domain Shutdown and
   Scaling", "Power Shutdown Techniques", or the specific CPF command
   (`create_power_domain`, `create_isolation_rule`,
   `create_state_retention_rule`, `create_power_switch_rule`). The "Flow
   Special Handling for Low Power" section enumerates which commands must
   precede which.

4. **Open the intent file.** `read_file` the .cpf / .upf the log named.
   Verify:
   - every `create_power_domain` has a `-instances` or `-boundary_ports`
     scope that actually contains the cells you expect,
   - the always-on net used by retention/switch rules exists as a real net
     in the Verilog,
   - the cell list for isolation / level-shifter matches real library cells
     (case-sensitive, footprint matters),
   - rule scopes (`-from`, `-to`) are not swapped — a frequent typo.

5. **Cross-check the netlist.** If the message names an instance or
   crossing, `read_file` the Verilog and confirm the offending hierarchy
   exists. A domain crossing the tool *invented* is almost always a real
   crossing the user forgot to declare.

6. **Conclude.** Report:
   - the error class (missing class / unprotected crossing / switch-net
     build / post-route regression),
   - the exact intent rule that is wrong or missing,
   - the fix (add the `create_*_rule`, list the right cell footprint,
     update the always-on net, re-scope `-from`/`-to`),
   - re-run point: re-`commit_power_intent` and the next physical step
     (placement adds isolation/level-shifter cells; you do *not* need to
     redo MMMC unless library sets changed),
   - prevention: add `verify_power_domain` / `check_power_intent` as gates
     after import and after every ECO.
