---
name: interpret-message-code
description: Look up a Cadence/Innovus message code (e.g. IMPCORE-9001, IMPSDC-3071, TECHLIB-1321) in the manual and return the canonical explanation with citations
when_to_use: a log mentions a parenthesised message code like (IMPCORE-9001) and the agent needs the manual's authoritative meaning before deciding root cause; or another playbook needs to confirm whether a warning is a root cause or a documented downstream symptom
domain: any
executable: false
---

# Playbook: interpret a Cadence message code

Cadence's `(PREFIX-NNN)` codes are the most reliable lookup key in the
manual — they are stable across versions and uniquely identify a warning /
error class. Use them, do not paraphrase the message text and hope for the
best.

1. **Pull the code(s).** From the user's question or from another
   playbook's evidence, capture every `(PREFIX-NNN)` code mentioned.
   Common Innovus prefixes: `IMPCORE-` (core implementation),
   `IMPCTS-` (clock tree), `IMPOPT-` (optimization), `IMPSDC-` (SDC
   parsing), `IMPMMMC-` (MMMC), `TECHLIB-` (technology / LEF / lib),
   `IMPLF-` (low power), `IMPPM-` (power planning),
   `IMPNR-` / `NR-` (NanoRoute), `IMPECO-` (ECO).
   The prefix already tells you which chapter to search.

2. **Search the manual.** For each code, `search_manual` with the *exact*
   code string. The manual indexes them verbatim. If the first pass
   returns no hit, try the prefix alone plus a noun from the log line
   (e.g. `IMPCTS clock buffer`) — the code may appear in a table that
   indexes by category.

3. **If still no hit, search the message text.** Sometimes the code is
   newer than the printed manual edition. `search_manual` with the
   distinctive phrase from the message (the part that isn't a variable —
   not the instance name, but the static error wording).

4. **Read the surrounding context.** When `search_manual` returns a
   match, `read_file` on the manual page so you can see whether the
   surrounding text says this is:
   - a *root-cause* class (must be fixed at source),
   - a *cascade symptom* class (will go away once the upstream is fixed),
   - a *recoverable warning* (engine continued; no user action required
     unless quality is affected),
   - a *config gate* (the engine refuses to proceed until a setup
     command is issued).
   The manual is explicit about this distinction for most codes.

5. **Conclude.** Return a compact answer:
   - the code,
   - one or two sentences of plain-English meaning,
   - the class from step 4 (root cause / cascade / warning / gate),
   - the `manual/<file>:<line>` citation,
   - if relevant, the canonical fix command the manual recommends (only
     if the manual literally names one — do not invent).

   Format the answer so a calling playbook can quote it verbatim in its
   "Evidence" section.
