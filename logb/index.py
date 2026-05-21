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
import re
import tempfile
from collections import deque
from pathlib import Path

from .profiles import EDA, Profile

# Innovus-style stage banner: --- Starting "X" ---  / --- Ending "X" ---
# Promoted from logb/tools/eda.py so the indexer can parse stage names
# during the build pass instead of every tool re-parsing them.
_STAGE_NAME_RX = re.compile(rb'--- (Starting|Ending) "([^"]+)"')

# File path / source-file mentions. Common extensions in EDA + general
# software logs. We deliberately don't include `.so`/`.exe` because those
# show up in noise (LD_LIBRARY_PATH spam) more than as referenced files.
_MENTION_PATH_RX = re.compile(
    rb"\b([A-Za-z0-9_./\-]+\."
    rb"(?:tcl|sdc|lef|def|v|sv|vhd|vh|lib|rpt|log|md|conf|yaml|yml|"
    rb"json|py|c|h|cpp|hpp|sh|csh)\b)")
# Innovus `<CMD> source scripts/foo.tcl` style invocation. We capture
# the line as a CMD mention (key prefixed with "CMD:") so the agent
# can ask `find_mentions(token='CMD:scripts/foo.tcl')` to see exactly
# which scripts ran and when.
_MENTION_CMD_RX = re.compile(rb"<CMD>\s+source\s+([A-Za-z0-9_./\-]+)")

INDEX_VERSION = 6    # bumped: stage_ranges/stage_hist/code_occurrences/severe_tail/warn_offsets/repeats/incidents/mentions
GRID = 2000          # byte-offset anchor every GRID lines
SEVERE_CAP = 2000    # head: offsets for first 2k severe lines (was 4000; tail moved to severe_tail)
SEVERE_TAIL_CAP = 2000  # tail: rolling deque of the last 2k severe lines
WARN_CAP = 4000      # store offsets for at most this many WARN lines (parity with severe)
STAGE_CAP = 2000     # raw stage-banner offsets (back-compat — see stage_ranges for the high-level shape)
STAGE_RANGES_CAP = 512   # parsed (start_line, end_line, name) tuples
STAGE_CODES_CAP = 64     # codes tracked per stage in stage_hist
CODES_CAP = 4000     # store at most this many distinct message codes
CODE_OCC_HEAD = 16   # per-code: first N occurrence line numbers
CODE_OCC_TAIL = 16   # per-code: last N occurrence line numbers
TIME_SAMPLE = 100    # store a (line, ts) pair every TIME_SAMPLE timestamped lines
REPEATS_CAP = 2000   # runs of identical consecutive lines (length >= REPEAT_MIN)
REPEAT_MIN = 3       # minimum run length to record as a repeat
INCIDENTS_CAP = 1000 # multi-line incidents (severe head + continuation body)
INCIDENT_MAX_BODY = 15  # max continuation lines per incident
MENTIONS_CAP = 2000  # distinct file paths / identifiers tracked
MENTION_LINES_CAP = 64  # line numbers per token


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
    b_ts = getattr(profile, "timestamp_rx", None)
    ts_decode = getattr(profile, "timestamp_to_seconds", None)
    grid: list[int] = []
    severe: list[list] = []                 # first SEVERE_CAP severe lines (head)
    # Rolling last SEVERE_TAIL_CAP severe lines. Combined with `severe`,
    # this gives the agent visibility into BOTH the first errors (likely
    # root cause) and the LAST errors (likely terminal failure) on a log
    # whose total severe count exceeds SEVERE_CAP.
    severe_tail_buf: deque = deque(maxlen=SEVERE_TAIL_CAP)
    # WARN line offsets, mirror of `severe` for warns. Eliminates the
    # streaming-scan slow path that `severity=warn` used to hit.
    warn_offsets: list[list] = []
    stages: list[list] = []
    codes: dict[str, list] = {}   # code -> [sev, count, first_lineno, first_off]
    # Per-code occurrence lists. head = first CODE_OCC_HEAD line numbers;
    # tail = rolling last CODE_OCC_TAIL (via deque). Lets the agent jump
    # to e.g. the 3rd hit of IMPSDC-3071 without a streaming scan.
    code_occ: dict[str, dict] = {}
    # Repeats: runs of identical consecutive lines, recorded once the
    # run terminates and length >= REPEAT_MIN. Free per-line cost in
    # the hot loop; only a byte-compare against the previous raw line.
    repeats: list[list] = []
    repeat_start_line: int = -1
    repeat_prev_raw: bytes = b""
    repeat_count: int = 0
    # Incidents: a severe line plus the contiguous "body" of continuation
    # lines that follow it (indented or lacking the normal line prefix).
    # Indexed lazily — we open an incident on each severe line and
    # extend it line-by-line until the body terminates.
    incidents: list[list] = []
    open_incident: list | None = None  # [head_line, body_end_line, severity]
    # Mentions inverted index: extracted file paths / sourced scripts ->
    # list of line numbers. Lets the agent answer "every line that
    # references top.sdc" without a streaming scan.
    mentions: dict[str, list] = {}
    # Stage ranges: parsed (start_line, end_line, name) per opened/closed
    # stage pair. open_stages is the current nesting; current_stage is the
    # most recent open stage's name (used to bucket severe lines into
    # stage_hist). The pre-stage bucket catches errors before any banner.
    stage_ranges: list[list] = []
    open_stages: list[tuple[str, int]] = []  # [(name, start_line), ...]
    current_stage = "<pre-stage>"
    stage_hist: dict[str, dict] = {}
    # Sparse time index: [(line_no, seconds)] sampled every TIME_SAMPLE
    # timestamped lines + every severe line (so correlate() can lock onto
    # an error precisely). The first and last timestamps are always kept.
    time_index: list[tuple[int, int]] = []
    last_ts_sampled_at = -TIME_SAMPLE
    first_ts: int | None = None
    last_ts: int | None = None
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
                severe_tail_buf.append([total, offset, "fatal"])
            elif b_error.search(raw):
                sev = "error"
                n_err += 1
                if len(severe) < SEVERE_CAP:
                    severe.append([total, offset, "error"])
                severe_tail_buf.append([total, offset, "error"])
            elif b_warn.search(raw):
                sev = "warn"
                n_warn += 1
                if len(warn_offsets) < WARN_CAP:
                    warn_offsets.append([total, offset])
            # Repeats: ignore trailing newlines so identical "lines"
            # match even with different line endings.
            line_body = raw.rstrip(b"\r\n")
            if line_body == repeat_prev_raw and line_body:
                repeat_count += 1
            else:
                # Run terminated: emit if long enough.
                if (repeat_count >= REPEAT_MIN
                        and len(repeats) < REPEATS_CAP):
                    repeats.append([repeat_start_line, repeat_count])
                repeat_prev_raw = line_body
                repeat_start_line = total
                repeat_count = 1
            # Incidents: open one on each severe line; extend on
            # subsequent continuation lines (indented, OR not starting
            # with the timestamp/severity prefix shape). Close when
            # we see a normal-shape line or when body grows past
            # INCIDENT_MAX_BODY.
            if sev is not None:
                if (open_incident is not None
                        and len(incidents) < INCIDENTS_CAP):
                    incidents.append(open_incident)
                open_incident = [total, total, sev]
            elif open_incident is not None:
                # Continuation heuristic: starts with whitespace OR
                # is short/empty OR doesn't look like a new top-level
                # log line. We can't perfectly identify a "new line"
                # without a profile-specific regex; using indentation
                # as the primary signal covers most stack-trace shapes
                # (Python "  File ...", "    foo()"; Java "    at X").
                is_continuation = (
                    line_body.startswith((b" ", b"\t"))
                    or not line_body                              # blank
                )
                head_line = open_incident[0]
                if is_continuation and (total - head_line) <= INCIDENT_MAX_BODY:
                    open_incident[1] = total
                else:
                    if len(incidents) < INCIDENTS_CAP:
                        incidents.append(open_incident)
                    open_incident = None
            code = None
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
                        code_occ[code] = {
                            "head": [total],
                            "tail": deque(maxlen=CODE_OCC_TAIL),
                            "count": 1,
                        }
                else:
                    c[1] += 1
                    occ = code_occ.get(code)
                    if occ is not None:
                        if len(occ["head"]) < CODE_OCC_HEAD:
                            occ["head"].append(total)
                        else:
                            occ["tail"].append(total)
                        occ["count"] += 1
            # Stage banner: track open/close pairs into stage_ranges and
            # update current_stage so the severe-line bucketing below
            # attributes errors to the right stage. We capture the
            # in-line timestamp on the banner so durations are available
            # without a second pass.
            if b_stage is not None and b_stage.search(raw):
                if len(stages) < STAGE_CAP:
                    stages.append([total, offset])
                m_name = _STAGE_NAME_RX.search(raw)
                if m_name is not None:
                    evt_ts = None
                    if b_ts is not None and ts_decode is not None:
                        m_t = b_ts.search(raw)
                        if m_t:
                            ts_capture = next((g for g in m_t.groups()
                                                if g is not None), None)
                            if ts_capture is not None:
                                evt_ts = ts_decode(ts_capture)
                    kind = m_name.group(1)
                    name = m_name.group(2).decode("utf-8", "replace")
                    if kind == b"Starting":
                        open_stages.append((name, total, evt_ts))
                        current_stage = name
                    else:  # Ending — pop the matching open frame
                        popped = None
                        for i in range(len(open_stages) - 1, -1, -1):
                            if open_stages[i][0] == name:
                                popped = open_stages.pop(i)
                                break
                        if popped is not None and len(stage_ranges) < STAGE_RANGES_CAP:
                            # [start_line, end_line, name, start_ts, end_ts]
                            stage_ranges.append([popped[1], total, name,
                                                  popped[2], evt_ts])
                        current_stage = (open_stages[-1][0]
                                          if open_stages else "<pre-stage>")
            # Bucket this severe line into the per-stage histogram. We
            # always record under the current_stage; severe lines that
            # fall before the first banner land in "<pre-stage>".
            if sev is not None:
                bucket = stage_hist.get(current_stage)
                if bucket is None:
                    if len(stage_hist) < STAGE_RANGES_CAP + 1:  # +1 for pre-stage
                        bucket = {"error": 0, "fatal": 0, "warn": 0,
                                  "codes": {}, "first_severe_line": total}
                        stage_hist[current_stage] = bucket
                if bucket is not None:
                    bucket[sev] = bucket.get(sev, 0) + 1
                    if code and code != "(uncoded)":
                        cmap = bucket["codes"]
                        if code in cmap:
                            cmap[code] += 1
                        elif len(cmap) < STAGE_CODES_CAP:
                            cmap[code] = 1
            # Mentions inverted index. Extract file-path-like tokens
            # plus `<CMD> source` invocations. Capped per-token and
            # capped overall so a pathological line can't blow up RAM.
            if len(mentions) < MENTIONS_CAP:
                for m_path in _MENTION_PATH_RX.finditer(raw):
                    token = m_path.group(1).decode("utf-8", "replace")
                    lst = mentions.get(token)
                    if lst is None:
                        if len(mentions) < MENTIONS_CAP:
                            mentions[token] = [total]
                    elif len(lst) < MENTION_LINES_CAP:
                        if lst[-1] != total:    # dedupe within same line
                            lst.append(total)
                m_cmd = _MENTION_CMD_RX.search(raw)
                if m_cmd is not None:
                    token = "CMD:" + m_cmd.group(1).decode("utf-8", "replace")
                    lst = mentions.get(token)
                    if lst is None:
                        if len(mentions) < MENTIONS_CAP:
                            mentions[token] = [total]
                    elif len(lst) < MENTION_LINES_CAP:
                        lst.append(total)
            # Timestamp sampling. Always record on a severe line (correlate
            # needs exact alignment to error moments) and every TIME_SAMPLE
            # lines otherwise. First and last samples are always kept.
            if b_ts is not None and ts_decode is not None:
                m = b_ts.search(raw)
                if m:
                    captured = next((g for g in m.groups()
                                      if g is not None), None)
                    if captured is not None:
                        ts = ts_decode(captured)
                        if ts is not None:
                            if first_ts is None:
                                first_ts = ts
                            last_ts = ts
                            on_severe = sev is not None
                            if (on_severe
                                    or total - last_ts_sampled_at >= TIME_SAMPLE):
                                time_index.append((total, ts))
                                last_ts_sampled_at = total
            offset += len(raw)
            total += 1
    if not grid:
        grid = [0]
    # Always cap with the final-line time if we haven't already.
    if (time_index and last_ts is not None
            and time_index[-1][0] != total - 1):
        time_index.append((total - 1, last_ts))
    # Close out any unclosed stages — the run died inside them. Their
    # end_line is the last line of the file. This is the row that gets
    # flagged INCOMPLETE in stage_timeline. end_ts is None (no banner).
    for name, start_line, start_ts in open_stages:
        if len(stage_ranges) < STAGE_RANGES_CAP:
            stage_ranges.append([start_line, total, name, start_ts, None])
    # Serialize code_occ for JSON. deques aren't JSON-native.
    code_occurrences = {
        code: {"head": occ["head"],
               "tail": list(occ["tail"]),
               "count": occ["count"]}
        for code, occ in code_occ.items()
    }
    # Flush any open repeat run and open incident at EOF.
    if repeat_count >= REPEAT_MIN and len(repeats) < REPEATS_CAP:
        repeats.append([repeat_start_line, repeat_count])
    if open_incident is not None and len(incidents) < INCIDENTS_CAP:
        incidents.append(open_incident)
    # severe_tail: dedupe entries already present in the head `severe`.
    # When n_err+n_fat <= SEVERE_CAP, severe_tail_buf overlaps with severe
    # entirely; we drop those to keep the tail purely "the lines we don't
    # already have."
    severe_head_lns = {entry[0] for entry in severe}
    severe_tail = [list(entry) for entry in severe_tail_buf
                   if entry[0] not in severe_head_lns]
    return {
        "v": INDEX_VERSION, "profile": profile.name,
        "size": st.st_size, "mtime": st.st_mtime,
        "grid_step": GRID, "total": total, "grid": grid,
        "severe": severe, "severe_tail": severe_tail,
        "warn_offsets": warn_offsets,
        "stages": stages,
        "stage_ranges": stage_ranges,
        "stage_hist": stage_hist,
        "codes": [[k, v[0], v[1], v[2], v[3]] for k, v in codes.items()],
        "code_occurrences": code_occurrences,
        "repeats": repeats,
        "incidents": incidents,
        "mentions": mentions,
        "n_err": n_err, "n_fat": n_fat, "n_warn": n_warn,
        # severe_capped is now true when even head+tail don't cover everything.
        "severe_capped": (n_err + n_fat) > (len(severe) + len(severe_tail)),
        "warn_offsets_capped": n_warn > len(warn_offsets),
        "stages_capped": len(stages) >= STAGE_CAP,
        "codes_capped": len(codes) >= CODES_CAP,
        "time_index": time_index,
        "first_ts": first_ts,
        "last_ts": last_ts,
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
