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
from .rag import ManualIndex
from .tools import ToolContext, build_registry


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


def _make_agent(cfg: Config, verbose: bool) -> Agent:
    client = build_client(cfg)
    registry = build_registry()
    ctx = ToolContext(cfg=cfg, manual_index=ManualIndex(cfg.manual_dir),
                       on_ask=_on_ask, on_confirm=_on_confirm)
    trace = (lambda s: print(f"\033[90m{s}\033[0m")) if verbose else None
    return Agent(client, registry, ctx, max_steps=cfg.max_steps, trace=trace)


def _print_answer(res) -> None:
    print(f"\n{res.answer}\n")
    print(f"\033[90m[{res.steps} step(s)]\033[0m")


def _cmd_ask(cfg: Config, args) -> int:
    agent = _make_agent(cfg, args.verbose)
    try:
        res = agent.ask(args.question)
    except LLMError as e:
        print(f"\033[31mLLM error:\033[0m {e}", file=sys.stderr)
        return 2
    _print_answer(res)
    return 0


def _cmd_chat(cfg: Config, args) -> int:
    agent = _make_agent(cfg, args.verbose)
    print(f"logb — {cfg.backend}:{cfg.model if cfg.backend=='ollama' else cfg.anthropic_model}"
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
            _print_answer(agent.ask(q))
        except LLMError as e:
            print(f"\033[31mLLM error:\033[0m {e}", file=sys.stderr)


def _cmd_skills(cfg: Config, _args) -> int:
    from .tools.skills import _list_skills
    print(_list_skills({}, ToolContext(cfg=cfg, manual_index=None)))
    return 0


def _cmd_doctor(cfg: Config, _args) -> int:
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
    p.add_argument("-v", "--verbose", action="store_true",
                   help="trace tool calls")

    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("ask", help="one-shot question")
    a.add_argument("question")
    a.set_defaults(func=_cmd_ask)
    sub.add_parser("chat", help="interactive REPL").set_defaults(func=_cmd_chat)
    sub.add_parser("skills", help="list skills").set_defaults(func=_cmd_skills)
    sub.add_parser("doctor", help="check environment").set_defaults(func=_cmd_doctor)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    overrides = {k: getattr(args, k) for k in (
        "log_path", "manual_dir", "skills_dir", "backend", "model",
        "max_steps", "interactive", "restrict_to_roots", "allow_skill_exec",
        "allow_shell")
        if getattr(args, k, None) is not None}
    cfg = Config.load(overrides)
    return args.func(cfg, args)


if __name__ == "__main__":
    raise SystemExit(main())
