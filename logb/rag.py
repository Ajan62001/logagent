"""Manual / docs retrieval — pure-stdlib BM25 over a folder of .md/.txt files.

No embedding model is required (only ``qwen2.5`` is pulled locally, an
instruct model, not an embedder). BM25 keyword ranking over heading-aware
chunks is deterministic, dependency-free, and good enough to surface the
right troubleshooting section for log-error queries. The index is built
lazily on first search and cached for the process lifetime.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path

_WORD = re.compile(r"[A-Za-z0-9_]+")
_DOC_EXT = {".md", ".markdown", ".txt", ".rst"}


def _tokenize(text: str) -> list[str]:
    return [w.lower() for w in _WORD.findall(text)]


@dataclass
class Chunk:
    source: str       # file path
    heading: str      # nearest markdown heading (breadcrumb)
    text: str
    tokens: list[str]


def _chunk_markdown(path: Path) -> list[Chunk]:
    """Split on markdown headings; group plain text into ~1.2k-char blocks."""
    raw = path.read_text(errors="replace")
    chunks: list[Chunk] = []
    heading = path.stem
    buf: list[str] = []

    def flush():
        body = "\n".join(buf).strip()
        if body:
            chunks.append(Chunk(str(path), heading, body, _tokenize(heading + " " + body)))
        buf.clear()

    for line in raw.splitlines():
        m = re.match(r"^(#{1,6})\s+(.*)", line)
        if m:
            flush()
            heading = m.group(2).strip()
        else:
            buf.append(line)
            if sum(len(x) for x in buf) > 1200:
                flush()
    flush()
    return chunks


class ManualIndex:
    """Lazy BM25 index over a manual directory."""

    def __init__(self, manual_dir: str):
        self.dir = Path(manual_dir)
        self._chunks: list[Chunk] = []
        self._df: dict[str, int] = {}
        self._avglen = 0.0
        self._built = False

    def _build(self) -> None:
        self._chunks.clear()
        if self.dir.is_dir():
            for p in sorted(self.dir.rglob("*")):
                if p.is_file() and p.suffix.lower() in _DOC_EXT:
                    self._chunks.extend(_chunk_markdown(p))
        self._df = {}
        for c in self._chunks:
            for t in set(c.tokens):
                self._df[t] = self._df.get(t, 0) + 1
        self._avglen = (sum(len(c.tokens) for c in self._chunks)
                        / len(self._chunks)) if self._chunks else 0.0
        self._built = True

    def search(self, query: str, k: int = 5) -> list[tuple[float, Chunk]]:
        if not self._built:
            self._build()
        if not self._chunks:
            return []
        q = [t for t in _tokenize(query) if len(t) > 1]
        N = len(self._chunks)
        k1, b = 1.5, 0.75
        scored: list[tuple[float, Chunk]] = []
        for c in self._chunks:
            tf: dict[str, int] = {}
            for t in c.tokens:
                tf[t] = tf.get(t, 0) + 1
            dl = len(c.tokens) or 1
            s = 0.0
            for t in q:
                if t not in tf:
                    continue
                idf = math.log(1 + (N - self._df.get(t, 0) + 0.5)
                               / (self._df.get(t, 0) + 0.5))
                f = tf[t]
                s += idf * (f * (k1 + 1)) / (
                    f + k1 * (1 - b + b * dl / (self._avglen or 1)))
            if s > 0:
                scored.append((s, c))
        scored.sort(key=lambda x: x[0], reverse=True)
        return scored[:k]
