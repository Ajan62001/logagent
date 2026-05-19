# logb — AI agent for EDA-tool log RCA

A tool-using agent that debugs EDA-tool logs (Innovus, PrimeTime, VCS, …),
finds the **root cause**, pinpoints the **fix**, and suggests **improvements**.

It can:

- **look in the logs** — `list_logs`, `read_logs` (grep / severity / head /
  tail / context, line-numbered for citation)
- **refer to the manual** — `search_manual` (BM25 RAG over `manual/`)
- **refer to skills** — `list_skills` / `run_skill` (folder playbooks)
- **open files mentioned in logs** — `read_file` (the failing `.tcl/.sdc`, a
  report, a stack-trace source)
- **ask clarifying questions** — `ask_user` when the request is ambiguous

Pure stdlib, **no third-party dependencies**. Default backend is **local
Ollama** (nothing leaves the machine — suited to a confidential domain);
Anthropic is a drop-in alternative.

## Setup

```bash
# default backend: Ollama (local). Pull a tool-capable model:
ollama pull qwen2.5:7b-instruct
python3 -m logb --log logs/sample_innovus.log doctor   # sanity-check env
```

For the Anthropic backend instead: `export ANTHROPIC_API_KEY=...` then add
`--backend anthropic`.

## Use

```bash
# one-shot
python3 -m logb -l logs/sample_innovus.log ask "Why did the run crash?"

# interactive (history persists across questions)
python3 -m logb -l logs/ chat

# trace every tool call
python3 -m logb -l run.log -v ask "Root-cause the FATAL"

python3 -m logb skills          # list playbooks
```

Point it at real logs with `-l <file|dir>`, docs with `--manual <dir>`,
playbooks with `--skills <dir>`. Optional `logb.json` in the cwd overrides
defaults (see `logb/config.py` for keys).

## How it works

A single agent loop: the model is given the tool schemas and decides what to
call, iterating (read logs → consult manual → run a skill → open a referenced
file → ask the operator) until it can answer, then emits a structured report:

```
User question
   │
   ▼
[model reasons] ──ambiguous?──► ask_user ──► (operator)
   │
   ├─ list_logs / read_logs   look in the logs
   ├─ search_manual           refer to the manual (RAG)
   ├─ list_skills / run_skill refer to skills
   └─ read_file               files mentioned in logs   (repeat, any order)
   │
   ▼
## Root Cause / ## Evidence / ## Fix / ## Suggestions to Improve
```

`max_steps` bounds the loop; on the final step tools are withheld to force a
written answer. Tool outputs are line-numbered and byte-budgeted so the model
can cite `file:line` and never overflow context.

## Scaling to multi-GB logs

`read_logs` never loads the file into memory. On first touch it builds a
cached sidecar index (`<log>.logbidx`, invalidated by size+mtime) in one
bounded streaming pass; afterwards it answers via `seek()` over only the
needed window. Measured on a real **2.00 GB / 25.5M-line** log:

| Operation | Time | Peak RSS |
|---|---|---|
| 1st call (builds + caches the index) | ~77 s one-time | **19 MB** |
| triage / severity=error,fatal / tail / head / CENSUS (cached) | **~0 s** | 19 MB |
| `pattern=<regex>` matching only near EOF (worst case) | ~30 s | 19 MB |

Memory is **flat (~19 MB) regardless of file size** (the old
`read().splitlines()` peaked at multiple GB and OOM'd). The only slow path is
an arbitrary `pattern=` regex that matches nothing until the end — a bounded
streaming scan; severity/triage/tail/head/CENSUS are all O(1) from the index,
which is the path the agent is steered to. The exact ERROR/FATAL census is
always instant. Delete `*.logbidx` to force a rebuild.

## Safety

- Credential/key files (`.ssh`, `.aws`, `.env`, `*.pem`, …) are **always
  refused**, even when an absolute path is printed in a log.
- `--restrict-roots` confines `read_file` to the project/log/manual/skills
  roots (off by default — following absolute paths from logs is the point).
- Skills are diagnostic playbooks (text) by default. An `executable: true`
  skill runs its script only with `--allow-skill-exec` (opt-in).

## Layout

```
logb/        agent loop, pluggable LLM transport, BM25 RAG, CLI
logb/tools/  one module per tool (logs, manual, skills, files, ask)
manual/      docs corpus (sample: Innovus error reference)
skills/      playbooks (sample: diagnose-missing-completion)
logs/        sample Innovus crash log (+ referenced scripts/ files)
tests/       offline suite (fake LLM) — `python -m pytest -q`
```

Tests run fully offline (scripted fake client) — no Ollama needed: `11 passed`.
