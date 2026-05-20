"""Streaming sidecar index for huge log files.

One bounded-memory pass builds a `<log>.<profile>.logbidx` JSON sidecar,
cached and invalidated by (size, mtime). It lets read_logs answer the CENSUS
instantly and window the file via `seek()` instead of `f.read().splitlines()`
— the latter peaks at multiple GB for a 2 GB log and was re-run on every call.

The severity/code/stage regexes come from the active Profile, so the same
streaming pass works for EDA logs, generic app logs, or any future domain
without code changes here. The profile name is in the cache filename so
switching modes on the same log doesn't reuse a stale taxonomy.

What the index stores (all from a single pass, bounded):
  * total            — line count
  * grid             — a byte offset every GRID lines (seek anchors)
  * severe           — [lineno, offset, "error"|"fatal"] for the first
                        SEVERE_CAP severe lines (counts below are EXACT)
  * stages           — [lineno, offset] for the first STAGE_CAP stage banners
                        (empty when the profile has no stage_rx)
  * n_err / n_fat    — exact ERROR / FATAL counts (never capped)

Query-time RAM is O(window): we seek to the nearest grid anchor and read
only the lines actually needed.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

from .profiles import EDA, Profile

INDEX_VERSION = 4    # bumped: profile name is part of the cache filename now
GRID = 2000          # byte-offset anchor every GRID lines
SEVERE_CAP = 4000    # store offsets for at most this many severe lines
STAGE_CAP = 2000     # ...and this many stage-banner lines
CODES_CAP = 4000     # store at most this many distinct message codes


def index_path(log: Path, profile: Profile = EDA) -> Path:
    """Sidecar next to the log if its dir is writable, else a cache dir.
    The profile name is part of the suffix so different domains never share a
    cache (a generic-mode index would have empty stages/codes for an EDA log
    and lie about the data — separate files keep both correct)."""
    suffix = f".{profile.name}.logbidx"
    side = log.with_suffix(log.suffix + suffix)
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
    return cache / f"{h}.{profile.name}.logbidx"


def build(log: Path, profile: Profile = EDA) -> dict:
    """Single streaming pass. Bounded memory regardless of file size. The
    severity/code/stage patterns are taken from the profile — pass GENERIC
    for app/system logs, EDA for place-and-route flows."""
    st = log.stat()
    b_fatal = profile.severity_bytes["fatal"]
    b_error = profile.severity_bytes["error"]
    b_warn = profile.severity_bytes["warn"]
    b_stage = profile.stage_rx
    b_code = profile.code_rx
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
            if b_fatal.search(raw):
                sev = "fatal"
                n_fat += 1
                if len(severe) < SEVERE_CAP:
                    severe.append([total, offset, "fatal"])
            elif b_error.search(raw):
                sev = "error"
                n_err += 1
                if len(severe) < SEVERE_CAP:
                    severe.append([total, offset, "error"])
            elif b_warn.search(raw):
                sev = "warn"
                n_warn += 1
            if sev and b_code is not None:
                m = b_code.search(raw)
                if m:
                    # The code regex may have several alternatives, each
                    # with its own capture group. Pick whichever one fired.
                    code_b = next((g for g in m.groups() if g is not None),
                                  None)
                    code = (code_b.decode("ascii", "replace")
                            if code_b else "(uncoded)")
                else:
                    code = "(uncoded)"
                c = codes.get(code)
                if c is None:
                    if len(codes) < CODES_CAP:
                        codes[code] = [sev, 1, total, offset]
                else:
                    c[1] += 1
            if b_stage is not None and b_stage.search(raw) and len(stages) < STAGE_CAP:
                stages.append([total, offset])
            offset += len(raw)
            total += 1
    if not grid:
        grid = [0]
    return {
        "v": INDEX_VERSION, "profile": profile.name,
        "size": st.st_size, "mtime": st.st_mtime,
        "grid_step": GRID, "total": total, "grid": grid,
        "severe": severe, "stages": stages,
        "codes": [[k, v[0], v[1], v[2], v[3]] for k, v in codes.items()],
        "n_err": n_err, "n_fat": n_fat, "n_warn": n_warn,
        "severe_capped": n_err + n_fat > len(severe),
        "stages_capped": len(stages) >= STAGE_CAP,
        "codes_capped": len(codes) >= CODES_CAP,
    }


def load_or_build(log: Path, profile: Profile = EDA) -> dict:
    """Return a valid index, rebuilding only if the log changed OR the cache
    was written under a different profile."""
    ip = index_path(log, profile)
    st = log.stat()
    try:
        idx = json.loads(ip.read_text())
        if (idx.get("v") == INDEX_VERSION
                and idx.get("profile") == profile.name
                and idx.get("size") == st.st_size
                and idx.get("mtime") == st.st_mtime):
            return idx
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    idx = build(log, profile)
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
