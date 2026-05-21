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
    """A retrieval chunk with full provenance.

    `heading` is the FULL breadcrumb (Chapter > Section > Subsection),
    built from the heading stack at chunk emit time. `start_line` is the
    1-indexed line in the source file (for markdown) or in the extracted
    text (for PDFs — combined with `page` to point back at the printed
    page). Citations can render as `source:start_line (page N) > heading`.
    """
    source: str          # file path
    heading: str         # full breadcrumb, ' > '-joined
    text: str
    tokens: list[str]
    start_line: int = 1  # 1-indexed line where the chunk begins
    page: int | None = None  # 1-indexed page (PDF only); None for text


def _join_breadcrumb(stack: list[str]) -> str:
    """Render the active heading stack as a ' > '-joined path. Empty
    entries are skipped so a missing mid-level doesn't produce
    ' >  > '."""
    parts = [s.strip() for s in stack if s and s.strip()]
    return " > ".join(parts) if parts else ""


def _chunk_markdown(path: Path) -> list[Chunk]:
    """Split a markdown file by H1/H2/H3 headings and ~1.2k-char blocks,
    tracking the full heading stack and the 1-indexed start line so each
    chunk knows exactly where in the source it came from."""
    raw = path.read_text(errors="replace")
    chunks: list[Chunk] = []
    heading_stack: list[str] = ["", "", ""]
    buf: list[str] = []
    buf_start_line = 1     # 1-indexed line where the current buffer began

    def flush(start_line: int):
        body = "\n".join(buf).strip()
        if body:
            crumb = _join_breadcrumb(heading_stack)
            chunks.append(Chunk(
                source=str(path),
                heading=crumb or "(no heading)",
                text=body,
                tokens=_tokenize(crumb + " " + body),
                start_line=start_line,
            ))
        buf.clear()

    for lineno, line in enumerate(raw.splitlines(), start=1):
        m = re.match(r"^(#{1,6})\s+(.*)", line)
        if m:
            flush(buf_start_line)
            buf_start_line = lineno
            depth = min(len(m.group(1)), 3)   # cap at H3 for the stack
            heading_stack[depth - 1] = m.group(2).strip()
            # Reset deeper levels — a new H2 invalidates the prior H3.
            for d in range(depth, 3):
                heading_stack[d] = ""
        else:
            if not buf:
                buf_start_line = lineno
            buf.append(line)
            if sum(len(x) for x in buf) > 1200:
                flush(buf_start_line)
                buf_start_line = lineno + 1
    flush(buf_start_line)
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


def _heading_level(line: str) -> int:
    """Classify a candidate heading line into a coarse level:
      1 — chapter-like  (ALL CAPS prose, or '1 Section' numbered)
      2 — section-like  ('1.2 Subsection', EDA codes)
      3 — subsection    (Title Case multi-word)
      0 — not a heading
    PDFs lack markup so this is heuristic. Aggressive filters reject
    technical noise that the EDA User Guide is full of: table rows
    ('VIA12 VIA 41 0'), code samples ('Met1 = …'), measurements
    ('WIDTH 0.40'), and variable names ('SCREAMING_SNAKE')."""
    s = line.strip()
    if not s or len(s) > 80:
        return 0
    # EDA error codes used as section heads.
    if re.match(r"^[A-Z][A-Z0-9]+-\d+\b", s):
        return 2
    # Reject code/config-like lines.
    if any(ch in s for ch in "=;{}[]<>"):
        return 0
    # Reject lines ending with bare numbers (table-row noise).
    if re.search(r"\s\d+(?:\.\d+)?(?:\s+\d+(?:\.\d+)?)*$", s):
        return 0
    # Reject lines containing decimal numbers (measurements).
    if re.search(r"\b\d+\.\d+\b", s):
        return 0
    letters = sum(1 for c in s if c.isalpha())
    digits = sum(1 for c in s if c.isdigit())
    if letters < 4 or digits > letters // 2:
        return 0
    # NOTE: deliberately NO "ALL CAPS = L1" rule. EDA PDFs are full of
    # ALL CAPS technical content (LEF keywords, cell names, command
    # names like 'BUFFD4BWP', 'METAL1 FILL') that would all be
    # misclassified as chapter headings. The real chapter titles in
    # the Innovus User Guide are Title Case, not ALL CAPS, so we let
    # those fall through to the Title Case rule below.
    #
    # Numbered '1' / '1.2' / '1.2.3' prefix → level by dot count. Rare
    # in this PDF but precise when it fires.
    m = re.match(r"^(\d+(?:\.\d+)*)\s+[A-Z]", s)
    if m:
        dots = m.group(1).count(".")
        return 1 if dots == 0 else (2 if dots == 1 else 3)
    # Title Case run, each word starts with Upper+letter, no trailing
    # period — section heading. This is the primary signal for PDFs.
    if re.match(r"^[A-Z][a-zA-Z][\w&-]*(\s+[A-Z][a-zA-Z][\w&-]*){0,7}$", s) \
            and not s.endswith("."):
        return 2
    return 0


def _looks_like_heading(line: str) -> bool:
    """Back-compat shim — kept for any external caller; new code uses
    _heading_level directly to also get the level."""
    return _heading_level(line) > 0


def _chunk_pdf(path: Path, text: str) -> list[Chunk]:
    """Heading-aware ~1.2k-char chunks over extracted PDF text.

    Tracks:
      • a 3-level heading stack so each chunk carries the full breadcrumb
        (e.g. 'DESIGN IMPLEMENTATION > Placing the Design > Adding Filler')
      • the 1-indexed line in the extracted text where the chunk starts
      • the page number, by counting form-feed (\\f) page breaks
        emitted by `pdftotext` (pypdf/stdlib fallbacks may not emit
        these — page stays None for those).
    """
    chunks: list[Chunk] = []
    heading_stack: list[str] = ["", "", ""]
    buf: list[str] = []
    buf_start_line = 1
    buf_start_page = 1
    page = 1

    def flush(start_line: int, start_page: int):
        body = "\n".join(buf).strip()
        if body:
            crumb = _join_breadcrumb(heading_stack)
            chunks.append(Chunk(
                source=str(path),
                heading=crumb or "(no heading)",
                text=body,
                tokens=_tokenize(crumb + " " + body),
                start_line=start_line,
                page=start_page,
            ))
        buf.clear()

    # Use split('\n'), NOT splitlines() — str.splitlines() consumes form
    # feed as a line separator and discards it, which would defeat the
    # page counter. We want the literal \f character preserved so we
    # can count page breaks emitted by pdftotext.
    for lineno, line in enumerate(text.split("\n"), start=1):
        ff_count = line.count("\f")
        if ff_count:
            page += ff_count
            line = line.replace("\f", "")
            if not line.strip():
                continue
        lvl = _heading_level(line)
        if lvl > 0:
            flush(buf_start_line, buf_start_page)
            buf_start_line = lineno + 1
            buf_start_page = page
            heading_stack[lvl - 1] = line.strip()
            for d in range(lvl, 3):
                heading_stack[d] = ""
        else:
            if not buf:
                buf_start_line = lineno
                buf_start_page = page
            buf.append(line)
            if sum(len(x) for x in buf) > 1200:
                flush(buf_start_line, buf_start_page)
                buf_start_line = lineno + 1
                buf_start_page = page
    flush(buf_start_line, buf_start_page)
    return chunks


class ManualIndex:
    """Lazy BM25 index over a manual directory, with optional hybrid
    embedding retrieval.

    Tracks a coarse signature (file paths + mtimes) of the manual tree at
    build time. `_maybe_rebuild()` rechecks on each search; if anything
    changed, the index is rebuilt transparently. This lets the user edit
    or add manual files mid-session without restarting logb.

    When an embedding store is attached (via `attach_embeddings`), each
    chunk's vector is fetched lazily on first search. The store handles
    caching to disk. Search then merges BM25 and cosine results into a
    single ranked list using normalized scores. With no store, behavior
    is exactly the BM25-only version.
    """

    def __init__(self, manual_dir: str):
        self.dir = Path(manual_dir)
        self._chunks: list[Chunk] = []
        self._df: dict[str, int] = {}
        self._avglen = 0.0
        self._built = False
        self._signature: tuple = ()       # (path, mtime, size) tuples
        self._embeddings: dict = {}       # id(chunk) -> vector
        self._embed_store = None          # logb.embed.EmbeddingStore | None

    def attach_embeddings(self, store) -> None:
        """Hook an EmbeddingStore in. Re-embedding happens lazily."""
        self._embed_store = store
        self._embeddings.clear()        # force re-embed under the new model

    def _current_signature(self) -> tuple:
        """Cheap fingerprint of the manual tree. Same -> reuse cache."""
        if not self.dir.is_dir():
            return ()
        out = []
        try:
            for p in self.dir.rglob("*"):
                if not p.is_file() or p.suffix.lower() not in _DOC_EXT:
                    continue
                st = p.stat()
                out.append((str(p), st.st_mtime, st.st_size))
        except OSError:
            return ()
        return tuple(sorted(out))

    def _maybe_rebuild(self) -> None:
        """Rebuild iff the manual tree has changed since last build."""
        sig = self._current_signature()
        if self._built and sig == self._signature:
            return
        self._signature = sig
        self._build()

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
        self._maybe_rebuild()
        if not self._chunks:
            return []
        bm25_scored = self._bm25_search(query)
        if self._embed_store is None or not self._embed_store.enabled:
            bm25_scored.sort(key=lambda x: x[0], reverse=True)
            return bm25_scored[:k]
        return self._hybrid_search(query, bm25_scored, k)

    def _bm25_search(self, query: str) -> list[tuple[float, Chunk]]:
        q = [t for t in _tokenize(query) if len(t) > 1]
        N = len(self._chunks)
        k1, b = 1.5, 0.75
        out: list[tuple[float, Chunk]] = []
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
                out.append((s, c))
        return out

    def _hybrid_search(self, query: str,
                        bm25: list[tuple[float, Chunk]],
                        k: int) -> list[tuple[float, Chunk]]:
        """Min-max-normalize BM25 and cosine scores into [0,1] then
        weighted-sum (0.5/0.5). Either component can be empty (e.g. embed
        endpoint down): degrade to whichever still has signal."""
        from .embed import cosine
        q_vec = self._embed_store.get_or_embed(query)
        cos: list[tuple[float, Chunk]] = []
        if q_vec is not None:
            for c in self._chunks:
                cid = id(c)
                vec = self._embeddings.get(cid)
                if vec is None:
                    vec = self._embed_store.get_or_embed(
                        f"{c.heading}\n{c.text}")
                    if vec is None:
                        continue
                    self._embeddings[cid] = vec
                score = cosine(q_vec, vec)
                if score > 0:
                    cos.append((score, c))

        def _normalize(rows):
            if not rows:
                return {}
            lo = min(s for s, _ in rows)
            hi = max(s for s, _ in rows)
            span = (hi - lo) or 1.0
            return {id(c): (s - lo) / span for s, c in rows}

        bm_norm = _normalize(bm25)
        cos_norm = _normalize(cos)
        if not bm_norm and not cos_norm:
            return []
        ids = set(bm_norm) | set(cos_norm)
        by_id = {id(c): c for c in self._chunks}
        merged = [(0.5 * bm_norm.get(i, 0.0) + 0.5 * cos_norm.get(i, 0.0),
                    by_id[i]) for i in ids if i in by_id]
        merged.sort(key=lambda x: x[0], reverse=True)
        return merged[:k]
