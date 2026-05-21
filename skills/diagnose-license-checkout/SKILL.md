---
name: diagnose-license-checkout
description: Diagnose Innovus / Cadence license failures — checkout denied, queued, suite mismatch, FLEXlm / Cadence License Manager errors
when_to_use: log ends with "Unable to check out a license", "License queued", FLEXlm -1/-15/-21/-97 errors, "feature not found", "INCREMENT line", "suite license mismatch", or the run dies before any design work begins
domain: any
executable: false
---

# Playbook: license checkout failure

Licensing failures are *binary* — the run did zero design work. The cost of
diagnosis is low; the cost of guessing wrong is making the user re-do the
LM-admin loop. Read the exact FLEXlm code; it tells you everything.

1. **Locate the license attempt.** `read_logs` with
   `pattern="license|FLEXlm|FlexNet|LM_LICENSE|CDS_LIC|Unable to check"`.
   Capture the very first license-related message and any FLEXlm error
   number (e.g. `-1`, `-15`, `-21`, `-97`, `-25`).

2. **Decode the FLEXlm/CDS error.** Map by number:
   - `-1` / `-15` — license server connection failed (server down,
     `CDS_LIC_FILE` / `LM_LICENSE_FILE` points nowhere, firewall).
   - `-5` — no such feature in license file (wrong product or wrong
     version requested).
   - `-21` — license file does not support this version (version mismatch
     with the binary).
   - `-97` — checkout exceeded the MAX users; queued or denied.
   - `-25` — license server doesn't support this version of vendor daemon.
   - `INCREMENT line not found` — that exact feature is not in the issued
     license at all.
   Also note "suite mismatch" between Innovus packages — e.g.
   `Innovus_Block` vs. `Innovus_Implementation_System` — the command set
   the script used isn't in the suite that was checked out.

3. **Cross-reference the manual.** `search_manual` for "Licensing
   Information", "Product Packages and Options", "Setting and Changing the
   License Check-Out Order", "Limiting the Multi-CPU License Search to
   Specific Products", or "Releasing Licenses Before the Session Ends".
   The "Product Packages and Options" table is the source of truth for
   which suite contains which commands.

4. **Inspect the env / setup script.** `read_file` the wrapper script /
   shell init that launched the run. Check:
   - `CDS_LIC_FILE` / `LM_LICENSE_FILE` is set and points to a reachable
     `port@host` (or readable file),
   - the binary version (`innovus -version`) matches the license version,
   - if `setMultiCpuUsage` is in the script, an extra suite license is
     required — see if it's listed in the suite the user actually owns,
   - no `-stylus` flag against a license without Stylus entitlement.

5. **Identify which command triggered checkout.** Some Innovus commands
   pull a *different* suite (CTS / SI / Power Integrity). Find the last
   successful command in the log and the next command attempted — the
   suite gap is usually right there.

6. **Conclude.** Report:
   - the FLEXlm/CDS code and what it means in plain English,
   - whether the cause is environment (`CDS_LIC_FILE`), entitlement
     (feature not licensed), capacity (all seats in use), or version
     mismatch,
   - the fix at the level the user can act on (set the env var, contact
     LM-admin, queue with `setLicenseQueueing`, downgrade to a suite that
     covers the command),
   - re-run: nothing to revert — re-launch from scratch once the license
     issue is resolved,
   - prevention: add a license sanity-check (`lmstat -a -c $CDS_LIC_FILE`)
     to the flow's preflight; or `setLicenseQueueing 1` to avoid silent
     denials during peak hours.
