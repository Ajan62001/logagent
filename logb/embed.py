"""Optional Ollama-served embeddings for hybrid manual retrieval.

BM25 (in rag.py) is fast and precise for keyword-shaped queries — error
codes, exact message tokens. It misses conceptual queries that don't share
vocabulary with the manual (e.g. "what does this kind of failure usually
mean"). Vector retrieval over a small local embedding model handles those.

Design constraints carried over from the rest of logb:

  * Optional. If `cfg.embedding_model` is empty or the Ollama endpoint
    returns an error, search degrades to BM25-only with no warning to
    the user. The manual tool must always work.
  * Local. Uses Ollama's `/api/embeddings`. No cloud dep, no extra Python
    package required.
  * Cached. Per-chunk embeddings hash by (text, model) and live in
    `<project_root>/.logb-embeddings/<hash>.bin` as raw float32 arrays.
    Rebuilding the manual index is amortized; live searches are cheap.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import struct
import urllib.error
import urllib.request
from pathlib import Path


def _chunk_id(text: str, model: str) -> str:
    h = hashlib.sha1((model + "\n" + text).encode("utf-8")).hexdigest()
    return h[:16]


def _cache_path(cache_dir: Path, chunk_id: str) -> Path:
    return cache_dir / f"{chunk_id}.bin"


def _save_vec(path: Path, vec: list[float]) -> None:
    """Pack as little-endian float32. Stable across runs and machines."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        f.write(struct.pack(f"<{len(vec)}f", *vec))


def _load_vec(path: Path) -> list[float] | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if not data or len(data) % 4 != 0:
        return None
    n = len(data) // 4
    return list(struct.unpack(f"<{n}f", data))


def _l2_normalize(vec: list[float]) -> list[float]:
    s = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / s for x in vec]


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    # If both are L2-normalized the dot product IS the cosine.
    return sum(x * y for x, y in zip(a, b))


def embed_text(text: str, host: str, model: str,
                timeout: int = 60) -> list[float] | None:
    """One-shot embed via Ollama. Returns None on any error so the caller
    can degrade gracefully. We L2-normalize so cosine reduces to dot."""
    if not model:
        return None
    payload = json.dumps({"model": model, "prompt": text}).encode()
    req = urllib.request.Request(
        f"{host.rstrip('/')}/api/embeddings",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
    except (urllib.error.HTTPError, urllib.error.URLError,
            json.JSONDecodeError, OSError):
        return None
    vec = data.get("embedding")
    if not isinstance(vec, list) or not vec:
        return None
    try:
        return _l2_normalize([float(x) for x in vec])
    except (TypeError, ValueError):
        return None


class EmbeddingStore:
    """Read-through cache of per-chunk embeddings under a project dir."""

    def __init__(self, project_root: str | os.PathLike,
                  cache_subdir: str, host: str, model: str):
        self.cache_dir = Path(project_root) / cache_subdir
        self.host = host
        self.model = model
        self.enabled = bool(model)

    def get_or_embed(self, text: str) -> list[float] | None:
        """Return an embedding for text. Hits the cache first; on miss
        calls Ollama and persists. Returns None if disabled or any
        network step fails."""
        if not self.enabled:
            return None
        cid = _chunk_id(text, self.model)
        path = _cache_path(self.cache_dir, cid)
        cached = _load_vec(path)
        if cached is not None:
            return cached
        vec = embed_text(text, self.host, self.model)
        if vec is None:
            return None
        try:
            _save_vec(path, vec)
        except OSError:
            pass  # cache is best-effort
        return vec
