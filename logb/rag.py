"""Manual / docs retrieval — pure-stdlib BM25 over a folder of docs.

Indexes ``.md/.markdown/.txt/.rst`` directly and ``.pdf`` after text
extraction. No embedding model is required (only ``qwen2.5`` is pulled
locally, an instruct model, not an embedder). BM25 keyword ranking over
heading-aware chunks is deterministic, dependency-free, and good enough to
surface the right troubleshooting section for log-error queries. The index is
built lazily on first search and cached for the process lifetime.

PDF text extraction has no *required* third-party dependency. It tries, in
order: the ``pdftotext`` CLI (poppler) → the ``pypdf`` module if it happens
to be importable → a pure-stdlib ``zlib`` fallback that pulls text-showing
operators out of the content streams. A PDF that none of these can read is
skipped with a warning rather than indexed as binary garbage.
"""

from __future__ import annotations

import math
import re
import shutil
import subprocess
import sys
import zlib
from dataclasses import dataclass
from pathlib import Path

_WORD = re.compile(r"[A-Za-z0-9_]+")
_TEXT_EXT = {".md", ".markdown", ".txt", ".rst"}
_PDF_EXT = {".pdf"}
_DOC_EXT = _TEXT_EXT | _PDF_EXT


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


# --------------------------------------------------------------------------- #
#  PDF text extraction — layered, zero *required* third-party dependency.      #
# --------------------------------------------------------------------------- #
def _pdf_via_pdftotext(path: Path) -> str | None:
    """Best quality. poppler's `pdftotext` is an external program, not a
    Python dependency, so the pure-stdlib promise (pyproject deps == []) holds."""
    exe = shutil.which("pdftotext")
    if not exe:
        return None
    try:
        r = subprocess.run([exe, "-q", "-enc", "UTF-8", "-eol", "unix",
                            str(path), "-"], capture_output=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.decode("utf-8", "replace")
    return None


def _pdf_via_pypdf(path: Path) -> str | None:
    """Used only if pypdf is already importable; never a hard requirement."""
    try:
        import pypdf  # noqa: PLC0415  (optional, intentionally lazy)
    except Exception:
        return None
    try:
        reader = pypdf.PdfReader(str(path))
        text = "\n".join((pg.extract_text() or "") for pg in reader.pages)
    except Exception:
        return None
    return text or None


_PDF_STREAM = re.compile(rb"stream\r?\n(.*?)\r?\n?endstream", re.S)
_PDF_LIT = re.compile(rb"\((?:\\.|[^\\()])*\)", re.S)
_PDF_SHOW = re.compile(rb"(\((?:\\.|[^\\()])*\)|\[[^\]]*\])\s*(TJ|Tj|'|\")")
_PDF_NL_OP = re.compile(rb"T\*|\b(Td|TD|ET)\b")
_PDF_ESC = {b"n": b"\n", b"r": b"\r", b"t": b"\t", b"b": b"\b",
            b"f": b"\f", b"(": b"(", b")": b")", b"\\": b"\\"}


def _pdf_unescape(s: bytes) -> str:
    out, i = bytearray(), 0
    while i < len(s):
        c = s[i:i + 1]
        if c == b"\\" and i + 1 < len(s):
            nxt = s[i + 1:i + 2]
            if nxt in _PDF_ESC:
                out += _PDF_ESC[nxt]
                i += 2
                continue
            if nxt.isdigit():  # octal escape \ddd
                j = i + 1
                while j < len(s) and j < i + 4 and s[j:j + 1].isdigit():
                    j += 1
                out.append(int(s[i + 1:j], 8) & 0xFF)
                i = j
                continue
            out += nxt
            i += 2
            continue
        out += c
        i += 1
    return out.decode("latin-1", "replace")


def _pdf_via_stdlib(path: Path) -> str | None:
    """Last-resort pure-stdlib fallback: inflate FlateDecode streams with
    zlib and pull operands of the text-showing operators (Tj/TJ/'/"). Lower
    fidelity than poppler — no layout — but enough for BM25 keyword search,
    and it depends on nothing outside the standard library."""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    parts: list[str] = []
    for m in _PDF_STREAM.finditer(data):
        body = m.group(1)
        try:
            body = zlib.decompress(body)
        except zlib.error:
            try:
                body = zlib.decompressobj().decompress(body)
            except zlib.error:
                pass  # uncompressed (or a filter we can't undo) — try as-is
        for sm in _PDF_SHOW.finditer(body):
            tok = sm.group(1)
            if tok[:1] == b"[":  # TJ array: concatenate its literal pieces
                parts.append("".join(_pdf_unescape(p[1:-1])
                                     for p in _PDF_LIT.findall(tok)))
            else:                # (literal) Tj / ' / "
                parts.append(_pdf_unescape(tok[1:-1]))
            tail = body[sm.end():sm.end() + 8]
            if _PDF_NL_OP.search(tail) or sm.group(2) in (b"'", b'"'):
                parts.append("\n")
    text = "".join(parts).strip()
    return text or None


def _extract_pdf_text(path: Path) -> str | None:
    for fn in (_pdf_via_pdftotext, _pdf_via_pypdf, _pdf_via_stdlib):
        text = fn(path)
        if text and text.strip():
            return text
    return None


def _looks_like_heading(line: str) -> bool:
    """PDFs carry no markdown headings; promote short lines that read like
    section titles (an EDA error code, or an ALL-CAPS / Title-Case header) so
    citations get a meaningful breadcrumb instead of just the file stem."""
    s = line.strip()
    if not s or len(s) > 80:
        return False
    if re.match(r"^[A-Z][A-Z0-9]+-\d+\b", s):          # IMPSDC-3071 ...
        return True
    letters = [c for c in s if c.isalpha()]
    if letters and s == s.upper() and len(letters) >= 3:  # ALL CAPS header
        return True
    return bool(re.match(r"^(\d+(\.\d+)*\s+)?[A-Z][\w/&-]*"
                         r"(\s+[A-Z0-9][\w/&-]*){0,7}$", s)) and not s.endswith(".")


def _chunk_pdf(path: Path, text: str) -> list[Chunk]:
    """Heading-aware ~1.2k-char chunks over extracted PDF text."""
    chunks: list[Chunk] = []
    heading = path.stem
    buf: list[str] = []

    def flush():
        body = "\n".join(buf).strip()
        if body:
            chunks.append(Chunk(str(path), heading, body,
                                _tokenize(heading + " " + body)))
        buf.clear()

    for line in text.splitlines():
        if _looks_like_heading(line):
            flush()
            heading = line.strip()
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
        skipped: list[str] = []
        if self.dir.is_dir():
            for p in sorted(self.dir.rglob("*")):
                if not p.is_file():
                    continue
                ext = p.suffix.lower()
                if ext in _TEXT_EXT:
                    self._chunks.extend(_chunk_markdown(p))
                elif ext in _PDF_EXT:
                    text = _extract_pdf_text(p)
                    if text:
                        self._chunks.extend(_chunk_pdf(p, text))
                    else:
                        skipped.append(str(p))
        if skipped:
            print("[logb] WARNING: could not extract text from PDF(s): "
                  + ", ".join(skipped) + "\n  Install poppler's `pdftotext` "
                  "(or `pip install pypdf`) so the manual is searchable.",
                  file=sys.stderr)
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
