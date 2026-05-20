---
name: diagnose-missing-completion
description: Trace an Innovus run that crashed or ended with no completion banner back to its first causal error
when_to_use: FATAL / core dumped / Signal 11 / run ends with no final "Ending" banner, or a cascade of stage errors
domain: eda
executable: false
---

# Playbook: missing-completion / crash root-cause

Cascade failures look worst at the tail but the cause is upstream. Work the
chain backwards.

1. **Find the terminal failure.** `read_logs` with `severity=fatal`. Note the
   FATAL/crash line, its message id, and any file path it prints.

2. **List every ERROR in order.** `read_logs` with `severity=error`. Read
   them top-to-bottom. The *first* ERROR is the prime suspect; later ERRORs
   are usually cascade symptoms of it.

3. **Identify the originating stage.** Map each ERROR to its `--- Starting
   "<stage>" ---` block. The earliest stage with an ERROR is where the run
   actually broke; everything after it is collateral.

4. **Corroborate with the manual.** `search_manual` for the first ERROR's
   message id. Confirm whether it is a root cause or a documented downstream
   symptom (the manual flags cascade symptoms explicitly).

5. **Open the referenced file.** If the originating ERROR cites a file and
   line (e.g. an SDC/TCL path + line number), `read_file` that exact window
   and confirm the defect with your own eyes. Do not assert a cause you have
   not seen in the file.

6. **Conclude.** Report the single root cause, the cascade chain
   (root → … → terminal symptom), the concrete fix at the source, the stage
   to re-run from, and a prevention/early-detection suggestion.
