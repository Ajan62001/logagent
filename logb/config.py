"""Configuration resolution.

Precedence (low -> high): built-in defaults  ->  ./logb.json  ->  CLI flags.

Kept deliberately small: Python 3.10 has no ``tomllib``, so config is plain
JSON (optional) plus CLI arguments. No third-party dependency.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path

# Files we refuse to open no matter what — credentials & secrets. This is a
# hard floor that even log-referenced absolute paths cannot bypass.
SENSITIVE_PATTERNS = (
    ".ssh/", "id_rsa", "id_ed25519", ".pem", ".key", ".pfx", ".p12",
    "shadow", ".aws/", ".netrc", ".env", "credentials", "secret",
    ".gnupg/", ".docker/config.json", ".kube/config", ".npmrc", ".pypirc",
)


@dataclass
class Config:
    # --- where things live ---
    log_path: str = "logs"          # a log file OR a directory of logs
    manual_dir: str = "manual"      # docs corpus for RAG
    skills_dir: str = "skills"      # folder-based skills
    project_root: str = "."         # root used for relative-path resolution

    # --- domain profile ---
    mode: str = "eda"               # "eda" | "generic" | "auto"

    # --- LLM backend ---
    backend: str = "ollama"         # "ollama" | "anthropic"
    model: str = "qwen2.5:7b-instruct"
    ollama_host: str = "http://localhost:11434"
    anthropic_model: str = "claude-opus-4-7"
    temperature: float = 0.0        # deterministic by default (RCA reproducibility)
    num_ctx: int = 8192             # Ollama context window
    max_tokens: int = 2048          # response cap (Anthropic)

    # --- agent loop ---
    max_steps: int = 12             # tool-call rounds before forced wrap-up
    interactive: bool = True        # allow ask_user to block on stdin
    tool_result_char_budget: int = 6000  # truncate fat tool outputs

    # --- long-chat hygiene ---
    history_compact_threshold: int = 0   # bytes; 0 = auto (num_ctx * 2.5)
    history_compact_keep_recent: int = 4 # keep this many newest tool results full
    history_compact_budget: int = 400    # head+tail size for compacted result

    # --- answer verification ---
    verify_citations: bool = True   # in Mode C, check `path:line` cites resolve
    verify_max_passes: int = 3      # how many verification iterations (incl. draft)
    stream: bool = True             # live-print model tokens to the terminal

    # --- safety ---
    restrict_to_roots: bool = False  # if True, read_file cannot escape roots.
    #   Default False because following absolute paths printed in logs is the
    #   whole point; SENSITIVE_PATTERNS is always enforced regardless.
    allow_skill_exec: bool = False   # run_skill executes scripts only if True
    allow_shell: bool = False        # run_bash enabled only if True
    shell_timeout: int = 60          # run_bash per-command timeout (s)

    extra_read_roots: list[str] = field(default_factory=list)

    # ----- resolution helpers -----
    @classmethod
    def load(cls, cli_overrides: dict | None = None) -> "Config":
        cfg = cls()
        jpath = Path("logb.json")
        if jpath.is_file():
            try:
                data = json.loads(jpath.read_text())
                for k, v in data.items():
                    if hasattr(cfg, k):
                        setattr(cfg, k, v)
            except (json.JSONDecodeError, OSError) as e:
                print(f"[logb] ignoring bad logb.json: {e}")
        for k, v in (cli_overrides or {}).items():
            if v is not None and hasattr(cfg, k):
                setattr(cfg, k, v)
        return cfg

    def allowed_roots(self) -> list[Path]:
        roots = [
            Path(self.project_root).resolve(),
            Path(self.manual_dir).resolve(),
            Path(self.skills_dir).resolve(),
        ]
        p = Path(self.log_path).resolve()
        roots.append(p if p.is_dir() else p.parent)
        roots += [Path(r).resolve() for r in self.extra_read_roots]
        return roots

    def as_dict(self) -> dict:
        return asdict(self)


def is_sensitive(path: str | os.PathLike) -> bool:
    s = str(path).replace("\\", "/").lower()
    return any(pat in s for pat in SENSITIVE_PATTERNS)
