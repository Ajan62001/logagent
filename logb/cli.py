"""logb CLI — interactive REPL + one-shot ask + helpers.

  logb ask "why did the run crash?" --log run.log
  logb chat --log logs/                 # interactive session (history persists)
  logb skills                           # list diagnostic playbooks
  logb doctor                           # check backend / model reachability
"""

from __future__ import annotations

import argparse
import sys

from .agent import Agent
from .config import Config
from .llm import LLMError, build_client
from .profiles import PROFILES, resolve as resolve_profile
from .rag import ManualIndex
from . import session as _session
from .tools import ToolContext, build_registry
from .tools.logs import _resolve_logs


def _on_ask(question: str, options: list) -> str:
    print(f"\n\033[33m? {question}\033[0m")
    if options:
        for i, o in enumerate(options, 1):
            print(f"  {i}. {o}")
    try:
        ans = input("\033[33m> \033[0m").strip()
    except (EOFError, KeyboardInterrupt):
        return ""
    if options and ans.isdigit() and 1 <= int(ans) <= len(options):
        return options[int(ans) - 1]
    return ans


def _on_confirm(command: str, purpose: str) -> bool:
    print("\n\033[31m┌─ shell command approval ─────────────────────────────\033[0m")
    if purpose:
        print(f"\033[31m│\033[0m purpose: {purpose}")
    print(f"\033[31m│\033[0m \033[1m$ {command}\033[0m")
    print("\033[31m└─ run this? [y/N]\033[0m", end=" ")
    try:
        return input().strip().lower() in {"y", "yes"}
    except (EOFError, KeyboardInterrupt):
        print()
        return False


def _resolve_profile_for_cfg(cfg: Config):
    """Resolve --mode auto by sniffing the first concrete log path; otherwise
    just map the mode string."""
    if (cfg.mode or "").lower() == "auto":
        # Need a log list to sniff; use the EDA-extension default for the
        # *discovery* step (it's a superset for typical log dirs).
        from .profiles import EDA
        logs = _resolve_logs(cfg, EDA)
        return resolve_profile("auto", [str(p) for p in logs])
    return resolve_profile(cfg.mode)


def _stream_token(s: str) -> None:
    sys.stdout.write(s)
    sys.stdout.flush()


def _make_manual_index(cfg: Config):
    """Build the manual index. If `embedding_model` is configured, attach
    an Ollama-backed embedding store for hybrid retrieval."""
    idx = ManualIndex(cfg.manual_dir)
    if cfg.embedding_model and cfg.backend == "ollama":
        from .embed import EmbeddingStore
        store = EmbeddingStore(
            project_root=cfg.project_root,
            cache_subdir=cfg.embedding_cache_dir,
            host=cfg.ollama_host,
            model=cfg.embedding_model,
        )
        idx.attach_embeddings(store)
    return idx


def _make_agent(cfg: Config, verbose: bool,
                  resume: str | None = None) -> Agent:
    client = build_client(cfg)
    registry = build_registry()
    profile = _resolve_profile_for_cfg(cfg)
    ctx = ToolContext(cfg=cfg, manual_index=_make_manual_index(cfg),
                       on_ask=_on_ask, on_confirm=_on_confirm,
                       profile=profile)
    trace = (lambda s: print(f"\033[90m{s}\033[0m")) if verbose else None
    on_token = _stream_token if cfg.stream else None
    agent = Agent(client, registry, ctx, max_steps=cfg.max_steps,
                   trace=trace, on_token=on_token)
    if resume:
        try:
            state = _session.load_session(cfg.project_root, resume)
            _session.apply_session(agent, state)
            print(f"\033[36m[resumed session {resume}: "
                  f"{len(state.history)} prior history entries]\033[0m")
        except (FileNotFoundError, ValueError) as e:
            print(f"\033[31m[resume failed: {e}]\033[0m")
    return agent


def _print_answer(res, streamed: bool) -> None:
    # When tokens were streamed live, the answer is already on screen — only
    # print the trailing newline + footer. Otherwise print the full body.
    if streamed:
        print()
    else:
        print(f"\n{res.answer}\n")
    # Footer with steps + telemetry. Telemetry pieces silently omit when
    # zero so the line stays useful when running with the FakeClient in
    # tests (no real backend, no token counts).
    parts = [f"{res.steps} step(s)"]
    if res.llm_calls:
        parts.append(f"{res.llm_calls} LLM call(s)")
    if res.tokens_in or res.tokens_out:
        parts.append(f"{res.tokens_in} in / {res.tokens_out} out tokens")
    if res.latency_ms:
        parts.append(f"{res.latency_ms / 1000:.1f}s")
    if res.verification_passes > 1:
        parts.append(f"verify x{res.verification_passes}")
    print(f"\033[90m[{' · '.join(parts)}]\033[0m")


def _cmd_ask(cfg: Config, args) -> int:
    agent = _make_agent(cfg, args.verbose, resume=getattr(args, "resume", None))
    try:
        res = agent.ask(args.question)
    except LLMError as e:
        print(f"\033[31mLLM error:\033[0m {e}", file=sys.stderr)
        return 2
    _print_answer(res, streamed=cfg.stream)
    if cfg.session_persist and agent.session_id:
        print(f"\033[90m[session: {agent.session_id} "
              f"({len(agent.history)} entries saved)]\033[0m")
    return 0


def _cmd_chat(cfg: Config, args) -> int:
    agent = _make_agent(cfg, args.verbose, resume=getattr(args, "resume", None))
    profile_name = agent.ctx.profile.name
    print(f"logb — {cfg.backend}:{cfg.model if cfg.backend=='ollama' else cfg.anthropic_model}"
          f"  | mode={cfg.mode} → profile={profile_name}"
          f"  | log={cfg.log_path}  manual={cfg.manual_dir}  skills={cfg.skills_dir}")
    print("Ask about a failure. Ctrl-D or 'exit' to quit.\n")
    while True:
        try:
            q = input("\033[36mlogb>\033[0m ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if q.lower() in {"exit", "quit", ":q"}:
            return 0
        if not q:
            continue
        try:
            _print_answer(agent.ask(q), streamed=cfg.stream)
        except LLMError as e:
            print(f"\033[31mLLM error:\033[0m {e}", file=sys.stderr)


def _cmd_skills(cfg: Config, _args) -> int:
    from .tools.skills import _list_skills
    profile = _resolve_profile_for_cfg(cfg)
    print(_list_skills({}, ToolContext(cfg=cfg, manual_index=None,
                                       profile=profile)))
    return 0


def _cmd_index(cfg: Config, args) -> int:
    """Build/refresh the manual RAG index for `cfg.manual_dir`.

    The index is normally built lazily on the first `search_manual` call,
    which can be slow on a multi-thousand-page PDF. Run this command
    once after editing the manual directory (or installing a new copy)
    so chat/ask sessions start with a warm cache.
    """
    import time
    from collections import Counter
    from pathlib import Path
    from .rag import ManualIndex, _DOC_EXT

    mdir = Path(cfg.manual_dir)
    if not mdir.is_dir():
        print(f"\033[31mERROR:\033[0m manual_dir {str(mdir)!r} does not "
              "exist or is not a directory.\n"
              "Set --manual <dir> or edit logb.json.", file=sys.stderr)
        return 2

    files = sorted(p for p in mdir.rglob("*")
                    if p.is_file() and p.suffix.lower() in _DOC_EXT)
    if not files:
        print(f"\033[33mWARNING:\033[0m no indexable files under "
              f"{str(mdir)!r}.\nSupported extensions: "
              f"{sorted(_DOC_EXT - {''})}.", file=sys.stderr)
        return 1

    print(f"Indexing {len(files)} file(s) under {mdir}/")
    for p in files:
        size_kb = p.stat().st_size / 1024
        print(f"  {p.suffix or '(no ext)':<6} {size_kb:>9.1f} KB  "
              f"{p.relative_to(mdir)}")

    t0 = time.perf_counter()
    idx = ManualIndex(str(mdir))
    # Trigger the build by running one cheap search.
    idx.search("warm-up", k=1)
    elapsed = time.perf_counter() - t0

    chunks = idx._chunks
    if not chunks:
        print("\n\033[31mERROR:\033[0m index built but contains zero chunks.\n"
              "  • If your manuals are PDFs, install poppler's `pdftotext` "
              "(apt: poppler-utils) or `pip install pypdf` for better "
              "extraction; the pure-stdlib fallback may have skipped them.",
              file=sys.stderr)
        return 1

    by_src = Counter(c.source for c in chunks)
    by_page = [c.page for c in chunks if c.page is not None]
    has_page = bool(by_page)
    with_headings = sum(1 for c in chunks if c.heading
                         and c.heading != "(no heading)")
    avg_len = sum(len(c.text) for c in chunks) / len(chunks)

    print(f"\nBuilt index in {elapsed:.2f}s")
    print(f"  chunks:           {len(chunks)}")
    print(f"  files indexed:    {len(by_src)}")
    print(f"  with heading:     {with_headings}  "
          f"({with_headings * 100 // len(chunks)}%)")
    print(f"  avg chunk length: {avg_len:.0f} chars")
    if has_page:
        print(f"  page coverage:    {min(by_page)}..{max(by_page)} "
              f"(PDFs only)")
    print("\nPer-file chunk counts:")
    for src, n in by_src.most_common():
        rel = Path(src).relative_to(mdir) if Path(src).is_relative_to(mdir) \
              else Path(src)
        print(f"  {n:>5}  {rel}")

    if getattr(args, "sample", 0):
        print(f"\nSample of {args.sample} chunks (sorted by start_line):")
        sample = sorted(chunks, key=lambda c: (c.source, c.start_line))
        step = max(1, len(sample) // args.sample)
        for c in sample[::step][:args.sample]:
            loc = f"{Path(c.source).name}:L{c.start_line}"
            if c.page is not None:
                loc += f" p{c.page}"
            print(f"  {loc:<40}  ›  {c.heading[:60]}")

    return 0


def _cmd_sessions(cfg: Config, _args) -> int:
    """List saved sessions in this project_root, newest first."""
    rows = _session.list_sessions(cfg.project_root)
    if not rows:
        print(f"(no saved sessions under {cfg.project_root}/.logb-sessions/)")
        return 0
    for r in rows:
        print(f"  {r['id']:<28}  {r['turns']:>3} turn(s)  "
              f"updated {r['updated']:<25}  {r['last_question']!r}")
    print("\nResume with:  logb --resume <id> chat")
    return 0


def _cmd_eval(cfg: Config, args) -> int:
    """Run the eval corpus against the configured model and print a report."""
    from .eval import run_corpus, render_report, summarize, CORPUS_DIR
    from pathlib import Path as _P
    import json as _json

    def _build_agent_for(case):
        # Per-case config: switch model/mode/log_path as the case asks.
        case_cfg = Config.load({
            "log_path": case.log_path,
            "manual_dir": case.manual_dir or cfg.manual_dir,
            "skills_dir": case.skills_dir or cfg.skills_dir,
            "project_root": case.project_root or cfg.project_root,
            "mode": case.mode,
            "backend": cfg.backend,
            "model": cfg.model,
            "anthropic_model": cfg.anthropic_model,
            "max_steps": case.max_steps,
            "interactive": False,
            "stream": False,           # eval is non-interactive; no streaming
            "audit_enabled": False,    # don't pollute the audit log
            "session_persist": False,
        })
        from .agent import Agent
        from .rag import ManualIndex
        client = build_client(case_cfg)
        registry = build_registry()
        profile = _resolve_profile_for_cfg(case_cfg)
        ctx = ToolContext(cfg=case_cfg,
                           manual_index=ManualIndex(case_cfg.manual_dir),
                           profile=profile)
        return Agent(client, registry, ctx,
                      max_steps=case_cfg.max_steps)

    corpus_dir = _P(args.corpus) if getattr(args, "corpus", None) else CORPUS_DIR

    def _on_case_done(case, result):
        mark = "\033[32m✓\033[0m" if result.passed else "\033[31m✗\033[0m"
        print(f"  {mark} {case.id:<24}  "
              f"{result.steps} step(s)  "
              f"{result.tokens_in}↑/{result.tokens_out}↓  "
              f"{result.latency_ms/1000:.1f}s")
    print(f"# eval corpus: {corpus_dir}  "
          f"(backend={cfg.backend}, model="
          f"{cfg.model if cfg.backend == 'ollama' else cfg.anthropic_model})")
    print()
    results = run_corpus(
        corpus_dir, build_agent=_build_agent_for,
        filter_pattern=getattr(args, "filter", None),
        max_cases=getattr(args, "max_cases", None),
        on_case_done=_on_case_done,
    )
    print()
    print(render_report(results))
    if getattr(args, "json", False):
        s = summarize(results)
        out = {"summary": s,
               "results": [r.to_dict() for r in results]}
        print()
        print(_json.dumps(out, indent=2))
    # Exit code reflects pass rate so eval can be wired into CI.
    return 0 if all(r.passed for r in results) else 1


def _cmd_audit(cfg: Config, args) -> int:
    """Print the last N audit records (default 20)."""
    n = int(getattr(args, "n", 20) or 20)
    rows = _session.read_audit_tail(cfg.project_root, n=n)
    if not rows:
        print(f"(no audit records under {cfg.project_root}/.logb-audit.jsonl)")
        return 0
    import json as _json
    for r in rows:
        print(_json.dumps(r, indent=2))
    return 0


def _cmd_doctor(cfg: Config, _args) -> int:
    profile = _resolve_profile_for_cfg(cfg)
    print(f"mode              : {cfg.mode}  -> profile {profile.name!r}")
    if cfg.strict:
        print(f"strict mode       : ON  (max_steps={cfg.max_steps}, "
              f"verify_max_passes={cfg.verify_max_passes}, "
              f"max_repeated_tool_call={cfg.max_repeated_tool_call})")
    print(f"backend           : {cfg.backend}")
    if cfg.backend == "ollama":
        import json
        import urllib.request
        try:
            with urllib.request.urlopen(f"{cfg.ollama_host}/api/tags", timeout=3) as r:
                names = [m["name"] for m in json.loads(r.read())["models"]]
            ok = cfg.model in names
            print(f"ollama @ {cfg.ollama_host} : reachable")
            print(f"model {cfg.model!r:30}: {'available' if ok else 'NOT PULLED'}")
            if not ok:
                print(f"  pull it:  ollama pull {cfg.model}")
                return 1
        except Exception as e:
            print(f"ollama            : UNREACHABLE ({e})")
            return 1
    else:
        import os
        print(f"ANTHROPIC_API_KEY : {'set' if os.environ.get('ANTHROPIC_API_KEY') else 'MISSING'}")
        print(f"model             : {cfg.anthropic_model}")
    print(f"log_path          : {cfg.log_path}")
    print(f"manual_dir        : {cfg.manual_dir}")
    print(f"skills_dir        : {cfg.skills_dir}")
    print(f"restrict_to_roots : {cfg.restrict_to_roots}  | allow_skill_exec: {cfg.allow_skill_exec}")
    print(f"allow_shell       : {cfg.allow_shell}  | shell_timeout: {cfg.shell_timeout}s")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="logb",
                                description="AI agent for EDA-tool log RCA.")
    p.add_argument("-l", "--log", dest="log_path", help="log file or directory")
    p.add_argument("--manual", dest="manual_dir", help="manual/docs directory")
    p.add_argument("--skills", dest="skills_dir", help="skills directory")
    p.add_argument("--mode", choices=sorted(list(PROFILES) + ["auto"]),
                   help="domain profile: eda / generic / auto (sniff)")
    p.add_argument("--backend", choices=["ollama", "anthropic"])
    p.add_argument("--model", help="ollama model id")
    p.add_argument("--max-steps", dest="max_steps", type=int)
    p.add_argument("--no-interactive", dest="interactive", action="store_false",
                   default=None, help="never block on ask_user")
    p.add_argument("--restrict-roots", dest="restrict_to_roots",
                   action="store_true", default=None,
                   help="forbid read_file outside allowed roots")
    p.add_argument("--allow-skill-exec", dest="allow_skill_exec",
                   action="store_true", default=None,
                   help="permit executable skills to run scripts")
    p.add_argument("--allow-shell", dest="allow_shell",
                   action="store_true", default=None,
                   help="enable the run_bash tool (each command still needs approval)")
    p.add_argument("--no-stream", dest="stream", action="store_false",
                   default=None,
                   help="disable token streaming (print the full answer at the end)")
    p.add_argument("--no-verify", dest="verify_citations",
                   action="store_false", default=None,
                   help="skip the Mode-C citation-verification re-ask")
    p.add_argument("--persist", dest="session_persist", action="store_true",
                   default=None,
                   help="save history to .logb-sessions/<id>.json after each turn")
    p.add_argument("--resume", metavar="ID",
                   help="resume a saved session by id (run `logb sessions` to list)")
    p.add_argument("--no-audit", dest="audit_enabled",
                   action="store_false", default=None,
                   help="don't append per-answer records to .logb-audit.jsonl")
    p.add_argument("--strict", action="store_true", default=None,
                   help="tune the harness for 7B-class local models (lower "
                   "step budget, more verification passes, zero-tolerance "
                   "duplicate-call brake, tighter tool budgets)")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="trace tool calls")

    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("ask", help="one-shot question")
    a.add_argument("question")
    a.set_defaults(func=_cmd_ask)
    sub.add_parser("chat", help="interactive REPL").set_defaults(func=_cmd_chat)
    sub.add_parser("skills", help="list skills").set_defaults(func=_cmd_skills)
    sub.add_parser("doctor", help="check environment").set_defaults(func=_cmd_doctor)
    ix = sub.add_parser("index",
                         help="build / refresh the manual RAG index")
    ix.add_argument("--sample", type=int, default=0, metavar="N",
                     help="after building, print N sample chunks "
                     "(headings + file:line) for inspection.")
    ix.set_defaults(func=_cmd_index)
    sub.add_parser("sessions",
                   help="list saved sessions").set_defaults(func=_cmd_sessions)
    audit = sub.add_parser("audit",
                            help="print the last N audit records")
    audit.add_argument("-n", type=int, default=20,
                       help="how many records to print (default 20)")
    audit.set_defaults(func=_cmd_audit)
    ev = sub.add_parser("eval",
                         help="run the eval corpus against the configured model")
    ev.add_argument("--corpus", help="path to a corpus dir (default: bundled)")
    ev.add_argument("--filter", help="only run cases whose id contains this")
    ev.add_argument("--max-cases", dest="max_cases", type=int,
                     help="cap on number of cases to run")
    ev.add_argument("--json", action="store_true",
                     help="emit a JSON report after the human-readable one")
    ev.set_defaults(func=_cmd_eval)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    overrides = {k: getattr(args, k) for k in (
        "log_path", "manual_dir", "skills_dir", "mode", "backend", "model",
        "max_steps", "interactive", "restrict_to_roots", "allow_skill_exec",
        "allow_shell", "stream", "verify_citations",
        "session_persist", "audit_enabled", "strict")
        if getattr(args, k, None) is not None}
    cfg = Config.load(overrides)
    return args.func(cfg, args)


if __name__ == "__main__":
    raise SystemExit(main())
