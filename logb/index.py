"""Streaming sidecar index for huge log files.

One bounded-memory pass builds a `<log>.logbidx` JSON sidecar, cached and
invalidated by (size, mtime). It lets read_logs answer the CENSUS instantly
and window the file via `seek()` instead of `f.read().splitlines()` — the
latter peaks at multiple GB for a 2 GB log and was re-run on every call.

What the index stores (all from a single pass, bounded):
  * total            — line count
  * grid             — a byte offset every GRID lines (seek anchors)
  * severe           — [lineno, offset, "error"|"fatal"] for the first
                        SEVERE_CAP severe lines (counts below are EXACT)
  * stages           — [lineno, offset] for the first STAGE_CAP stage banners
  * n_err / n_fat    — exact ERROR / FATAL counts (never capped)

Query-time RAM is O(window): we seek to the nearest grid anchor and read
only the lines actually needed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

INDEX_VERSION = 3
GRID = 2000          # byte-offset anchor every GRID lines
SEVERE_CAP = 4000    # store offsets for at most this many severe lines
STAGE_CAP = 2000     # ...and this many stage-banner lines
CODES_CAP = 4000     # store at most this many distinct message codes

# Byte-regex mirrors of logs._SEV (ASCII patterns — parity holds on the
# ASCII-ish content of EDA logs; bytes scanning is ~4x faster on a 2 GB pass).
_B_FATAL = re.compile(rb"\b(FATAL|PANIC|ABORT|core dumped|Segmentation fault)\b", re.I)
_B_ERROR = re.compile(rb"\b(ERROR|ERR|\*\*ERROR|FAIL(ED|URE)?)\b", re.I)
_B_WARN = re.compile(rb"\b(WARN(ING)?|\*\*WARN)\b", re.I)
_B_STAGE = re.compile(rb'--- (Starting|Ending) "')
# EDA message code, e.g. (IMPLF-213), (TECHLIB-1321), (IMPIMEX-4022).
_B_CODE = re.compile(rb"\(([A-Z][A-Z0-9]+-\d+)\)")


def index_path(log: Path) -> Path:
    """Sidecar next to the log if its dir is writable, else a cache dir."""
    side = log.with_suffix(log.suffix + ".logbidx")
    if os.access(log.parent, os.W_OK):
        return side
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(
        os.path.expanduser("~"), ".cache")
    cache = Path(base) / "logb"
    try:
        cache.mkdir(parents=True, exist_ok=True)
    except OSError:
        cache = Path(tempfile.gettempdir()) / "logb_idx"
        cache.mkdir(parents=True, exist_ok=True)
    h = hashlib.sha1(str(log.resolve()).encode()).hexdigest()[:16]
    return cache / f"{h}.logbidx"


def build(log: Path) -> dict:
    """Single streaming pass. Bounded memory regardless of file size."""
    st = log.stat()
    grid: list[int] = []
    severe: list[list] = []
    stages: list[list] = []
    codes: dict[str, list] = {}   # code -> [sev, count, first_lineno, first_off]
    n_err = n_fat = n_warn = total = 0
    with open(log, "rb") as f:
        offset = 0
        for raw in f:
            if total % GRID == 0:
                grid.append(offset)
            sev = None
            if _B_FATAL.search(raw):
                sev = "fatal"
                n_fat += 1
                if len(severe) < SEVERE_CAP:
                    severe.append([total, offset, "fatal"])
            elif _B_ERROR.search(raw):
                sev = "error"
                n_err += 1
                if len(severe) < SEVERE_CAP:
                    severe.append([total, offset, "error"])
            elif _B_WARN.search(raw):
                sev = "warn"
                n_warn += 1
            if sev:
                m = _B_CODE.search(raw)
                code = m.group(1).decode("ascii", "replace") if m else "(uncoded)"
                c = codes.get(code)
                if c is None:
                    if len(codes) < CODES_CAP:
                        codes[code] = [sev, 1, total, offset]
                else:
                    c[1] += 1
            if _B_STAGE.search(raw) and len(stages) < STAGE_CAP:
                stages.append([total, offset])
            offset += len(raw)
            total += 1
    if not grid:
        grid = [0]
    return {
        "v": INDEX_VERSION, "size": st.st_size, "mtime": st.st_mtime,
        "grid_step": GRID, "total": total, "grid": grid,
        "severe": severe, "stages": stages,
        "codes": [[k, v[0], v[1], v[2], v[3]] for k, v in codes.items()],
        "n_err": n_err, "n_fat": n_fat, "n_warn": n_warn,
        "severe_capped": n_err + n_fat > len(severe),
        "stages_capped": len(stages) >= STAGE_CAP,
        "codes_capped": len(codes) >= CODES_CAP,
    }


def load_or_build(log: Path) -> dict:
    """Return a valid index, rebuilding only if the log changed."""
    ip = index_path(log)
    st = log.stat()
    try:
        idx = json.loads(ip.read_text())
        if (idx.get("v") == INDEX_VERSION and idx.get("size") == st.st_size
                and idx.get("mtime") == st.st_mtime):
            return idx
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    idx = build(log)
    try:
        ip.write_text(json.dumps(idx))
    except OSError:
        pass  # read-only location: rebuild next time, still correct
    return idx


def fetch_text(log: Path, idx: dict, wanted) -> dict:
    """Map {lineno: text} for `wanted` line numbers, reading only the needed
    regions via grid-anchored seeks. RAM is O(len(wanted))."""
    want = sorted({w for w in wanted if 0 <= w < idx["total"]})
    if not want:
        return {}
    grid, step = idx["grid"], idx["grid_step"]
    out: dict[int, str] = {}
    with open(log, "rb") as f:
        i = 0
        while i < len(want):
            anchor = want[i] // step
            f.seek(grid[anchor] if anchor < len(grid) else grid[-1])
            cur = anchor * step
            while i < len(want):
                target = want[i]
                while cur < target:
                    if not f.readline():
                        break
                    cur += 1
                raw = f.readline()
                if not raw:
                    i = len(want)
                    break
                out[target] = raw.decode("utf-8", "replace").rstrip("\n")
                cur += 1
                i += 1
                # Big gap to the next wanted line => re-anchor via the grid
                # instead of reading through it.
                if i < len(want) and want[i] - target > step:
                    break
    return out


def scan(log: Path, rx, sev_res, ctx_lines: int, max_lines: int):
    """Streaming scan for an arbitrary regex (optionally severity-filtered).
    Captures matched lines + context, early-stops once `max_lines` is reached.
    O(window) RAM; worst case (no match) is one bounded streaming pass."""
    from collections import deque
    sel: set[int] = set()
    text: dict[int, str] = {}
    recent: deque = deque(maxlen=max(ctx_lines, 0))
    fwd = 0
    with open(log, "rb") as f:
        for i, raw in enumerate(f):
            t = raw.decode("utf-8", "replace").rstrip("\n")
            hit = ((rx.search(t) if rx else True)
                   and (any(r.search(t) for r in sev_res) if sev_res else True))
            if hit:
                for ln, tx in recent:
                    sel.add(ln)
                    text[ln] = tx
                sel.add(i)
                text[i] = t
                fwd = ctx_lines
            elif fwd > 0:
                sel.add(i)
                text[i] = t
                fwd -= 1
            if ctx_lines:
                recent.append((i, t))
            if len(sel) >= max_lines and fwd == 0:
                break
    return sorted(sel), text
