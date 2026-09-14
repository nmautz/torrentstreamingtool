"""Smart Skip — audio fingerprinting for intro/credits detection.

Uses ffmpeg (audio decode) + chromaprint/fpcalc (fingerprinting) to find audio
segments that repeat across episodes of a series. The repeating segment near
the start of each file is the intro; the one near the end (if present) is the
credits/outro. There is NO fallback: credits come only from a confirmed
cross-episode match, never from a black-frame detector or a percentage-of-runtime
guess (both existed once and fabricated wrong outros - see docs/GOTCHAS.md).

Blocking subprocess work (ffmpeg/fpcalc/ffprobe) runs off the loop on a
DEDICATED thread pool (`_FP_EXECUTOR` via `_fp_thread`), not the shared default
asyncio executor — see `_FP_EXECUTOR` for why that isolation matters. The
orchestrator runs one series at a time per series (a per-series asyncio.Lock
prevents re-entry) and bounds cross-series concurrency with main.py's
`_analysis_gate`.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Optional

import refiner
# Binary discovery + low-priority spawning live in mediabin so refiner can use them
# without importing analyzer (analyzer imports refiner). Re-exported below so the ~38
# `analyzer.ffmpeg_bin()` / `analyzer.fpcalc_bin()` call sites in main.py keep working.
from mediabin import (  # noqa: F401  (re-exported for main.py)
    _LOWPRIO_KW, _lp, _env_bin, ffmpeg_bin, ffprobe_bin, fpcalc_bin,
)

# numpy powers the vectorized matcher fast path (~50-100x the pure-Python
# loop). Optional: a venv that predates the dependency still analyzes via the
# pure-Python fallback, just slower.
try:
    import numpy as _np
    _POP16 = _np.array([bin(i).count("1") for i in range(1 << 16)], dtype=_np.uint8)
except Exception:           # pragma: no cover - numpy missing
    _np = None
    _POP16 = None

ANALYZER_VERSION = 9

# Per-file failure codes recorded in skip_data[path].analysis when fingerprinting
# could not produce usable skip points. The user-facing UI shows a "Skip
# unavailable" chip for any of these; the admin editor + analyzer log surface
# the message verbatim. Keep codes stable — clients may filter on them.
ERR_NO_BINARY    = "no_binary"      # ffmpeg or fpcalc not installed on host
ERR_FILE_MISSING = "file_missing"   # video file is not on disk
ERR_NO_DURATION  = "no_duration"    # ffprobe could not read duration
ERR_FP_EMPTY     = "fp_empty"       # fpcalc returned no fingerprint (codec/corruption)
ERR_NO_SKIP      = "no_skip_points" # fingerprinting ran but produced no usable match
ERR_EXCEPTION    = "exception"      # raised inside analyze_series — message carries detail

# Run analyzer subprocesses (ffmpeg decode, fpcalc, ffprobe) at lowered OS
# priority so a Smart-Skip pass never starves the StreamLink server. The server
# raises itself to HIGH at startup and children inherit that, so without this an
# analysis would run at HIGH and lag the controls/UI — exactly what we're trying
# to avoid. Windows: BELOW_NORMAL_PRIORITY_CLASS via creationflags. POSIX:
# prepend `nice -n 10` (no-op when `nice` isn't on PATH). Mirrors main.py's prep.
# (_LOWPRIO_KW and _lp now live in mediabin — imported above.)

# Chromaprint's raw fingerprint emission rate. Each frame is one 32-bit integer.
#
# This was 7.8 for a long time and that was simply WRONG — the old comment claimed
# "8192 samples @ 11025 Hz", which works out to 1.35 frames/s, so the number never
# followed from its own arithmetic. The real pipeline resamples to 11025 Hz and hops
# 4096/3 ≈ 1365.33 samples per chroma frame → 11025 / 1365.33 = 8.0775 frames/s.
#
# Measured directly against fpcalc 1.5.1 (the build setup.py installs) on synthetic
# clips of exactly known length, 30 s → 360 s:
#
#     frames = 8.07677 * seconds - 21.43     (7 points, essentially exact)
#
# A 7.8 divisor therefore inflated EVERY reported timestamp by 8.0768/7.8 = +3.55% of
# its own value. That is a scale error, not an offset, so it grew with the boundary:
# an intro START at ~12 s was off by +0.4 s (invisible) while the intro END at ~105 s
# was off by +3.7 s, and a credits offset 480 s into the tail window was off by +17 s.
# That asymmetry — start looks right, end runs long — was the reported "Smart Skip
# skips too far past the intro" bug. See docs/GOTCHAS.md.
FP_FRAMES_PER_SEC = 8.0768

# Chromaprint's classifier needs a 16-chroma-frame window, and the first FFT frame
# needs to fill, so a fingerprint is short of `rate * seconds` by a CONSTANT ~21.4
# frames regardless of window length. Any self-calibration must be affine — a naive
# `len(fp) / window_sec` reads 7.37 at a 30 s window and 8.02 at 360 s, and would
# under-correct by 8.8% on short windows.
FP_FRAME_LEADIN = 21.43

# Accept a self-calibrated rate only inside this band; anything else means the
# fingerprint was truncated (partial file, decode error) and the constant is safer.
FP_RATE_SANITY = (7.5, 8.6)

# Search windows
INTRO_SEARCH_SECS = 360       # look for intro in first 6 minutes
OUTRO_SEARCH_SECS = 600       # look for outro/credits in last 10 minutes
MIN_INTRO_SEC     = 15        # smallest segment we'll call an intro
MAX_INTRO_SEC     = 180       # cap to avoid runaway matches
MIN_OUTRO_SEC     = 15
MAX_OUTRO_SEC     = 180

# Matching thresholds
FRAME_HAMMING_MAX = 6         # ≤6 bits differ = "same" frame (32-bit hash)
MIN_MATCH_FRAMES  = int(MIN_INTRO_SEC * FP_FRAMES_PER_SEC)

# Outro acceptance. Credits time comes ONLY from a confirmed cross-episode
# fingerprint match — there is no fabricated fallback (no flat-% guess, no
# black-frame detector). A matched tail run is only accepted as credits when it
# both starts late enough AND runs to ~the end of the file. This rejects a
# recurring NON-credits cue (a stinger/gag) that some shows repeat near the end:
# real credits run to the end, a recurring gag is followed by more content.
MIN_CREDITS_PCT      = 0.75   # credits may not start before 75% of runtime
OUTRO_END_MARGIN_SEC = 120    # matched run must reach within 2 min of the end
# Cluster consensus: members of a real-credits cluster all match the SAME anchor
# region, so their anchor-side offsets agree. An outlier that matched the anchor
# on a different region (a recurring gag, not the credits) is pruned when its
# anchor offset deviates by more than this from the cluster median.
CLUSTER_OFFSET_TOL_FRAMES = int(8.0 * FP_FRAMES_PER_SEC)

# Gap tolerance. Each chromaprint frame is computed over a ~2.4 s audio window
# (hopped every ~0.128 s), so ONE second of audio that differs between episodes
# — a title card voiceover, an episode-specific sound under the theme — smears
# across ~20 consecutive frames. Requiring a perfectly consecutive run (the old
# behaviour) truncated or killed real intro matches. The matcher now bridges
# mismatch gaps up to MATCH_GAP_FRAMES (~4 s) as long as the merged run is
# mostly matching frames (MATCH_MIN_RATIO). Random cross-episode frame matches
# are ~0.03% likely, so a long mostly-matching run is never a false positive.
MATCH_GAP_FRAMES = int(4.0 * FP_FRAMES_PER_SEC)
MATCH_MIN_RATIO  = 0.6

# Terminal-segment trim (see _trim_weak_edges). Gap bridging stays exactly as tuned —
# these only discard a run's OUTERMOST segment when it is both too short to be evidence
# and separated by a real gap, which is the "silence then a brief coincidence" tail that
# inflated intro ends.
MATCH_EDGE_MIN_FRAMES = int(1.5 * FP_FRAMES_PER_SEC)
MATCH_EDGE_GAP_FRAMES = int(2.0 * FP_FRAMES_PER_SEC)

# Cluster consensus trim. Fraction of members ignored at EACH edge when computing the
# agreed anchor window, so one short match can't truncate the whole season. int(n * q)
# is 0 for n < 5, making small clusters identical to a hard intersection.
CLUSTER_TRIM_Q = 0.2

# Stationary-audio guard. Silence, drones, and sustained tones make chromaprint
# emit runs of near-identical hash frames — degenerate regions that "match"
# anything similar for tens of seconds (observed: a 22 s consecutive bogus run
# between two different sine sweeps). A frame only counts as match evidence
# when it's *informative* — ≥ this many bits changed vs its own predecessor —
# in BOTH episodes; uninformative stretches are treated as gaps instead.
MIN_FRAME_DELTA_BITS = 2

# Head/tail fingerprinting decodes audio with ffmpeg/fpcalc at below-normal
# priority; two at a time roughly halves the wall-clock of the (I/O-heavy)
# fingerprinting stage without saturating the host.
FP_CONCURRENCY = 2

# Dedicated thread pool for the analyzer's blocking subprocess waits
# (ffmpeg / fpcalc / ffprobe). Deliberately SEPARATE from asyncio's default loop
# executor: `asyncio.to_thread` shares one bounded pool process-wide, and
# main.py's get_library() / put_library() ride that same pool. Each fingerprint
# parks its worker thread for the whole decode (head = 6 min, tail = 10 min of
# audio → seconds of wall-clock per call). Fingerprint several series at once and
# enough of those blocking waits pile into the default pool that library reads/
# writes — which every hot loop (progress tracker, download monitor) and HTTP
# handler depend on — queue behind the decodes. The event loop stays alive
# (the host and RDP look healthy) but the dashboard freezes: the exact
# "fingerprinting many shows stalls the server" report. Routing analyzer
# subprocess work through its OWN pool keeps the default pool free for library
# I/O. Created lazily, and only ever from the event-loop thread, so no lock.
_FP_EXECUTOR: Optional[concurrent.futures.ThreadPoolExecutor] = None


def _fp_executor() -> concurrent.futures.ThreadPoolExecutor:
    global _FP_EXECUTOR
    if _FP_EXECUTOR is None:
        _FP_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
            max_workers=FP_CONCURRENCY * 4, thread_name_prefix="smartskip-fp",
        )
    return _FP_EXECUTOR


async def run_offloaded(fn, *args):
    """Public entry to the analyzer's dedicated subprocess thread pool.

    For callers OUTSIDE this module (the shot-scan worker in main.py) that need to run a
    blocking media subprocess. It must not go through `asyncio.to_thread`: that is the
    event loop's default pool, which `get_library()`/`put_library()` also ride, and a
    multi-minute decode parked there stalls the whole dashboard. See docs/GOTCHAS.md.
    """
    return await _fp_thread(fn, *args)


async def _fp_thread(fn, *args):
    """Run a blocking analyzer subprocess call off the loop on the analyzer's OWN
    thread pool — never the shared default pool get_library()/put_library() use.
    See `_FP_EXECUTOR` for why the isolation matters."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_fp_executor(), fn, *args)

# Overall-progress spans per stage: (start_fraction, end_fraction). Emitted as a
# monotonic `progress` float so progress bars never restart at stage boundaries
# (the matching stages' `total` is a growing estimate — per-stage current/total
# jumps backward; `progress` never does).
_STAGE_SPAN = {
    "starting":        (0.00, 0.00),
    "fingerprinting":  (0.00, 0.55),
    "matching-intros": (0.55, 0.75),
    "matching-outros": (0.75, 0.95),
    "finalizing":      (0.95, 1.00),
    "done":            (1.00, 1.00),
}


def is_available() -> bool:
    return bool(fpcalc_bin() and ffmpeg_bin())


# ── Fingerprinting ───────────────────────────────────────────────────────────

def _fpcalc_raw(file_path: str, length_sec: int, start_sec: float = 0.0) -> list[int]:
    """Run fpcalc and return the raw fingerprint as a list of 32-bit ints.

    Uses -raw to get the integer sequence directly. Roughly FP_FRAMES_PER_SEC integers
    per second of audio — but the relation is affine, so convert with `_rate_for` /
    `frames_to_seconds` rather than dividing by the constant.

    `start_sec` is a FLOAT and is passed to ffmpeg at millisecond precision. Callers must
    convert the resulting frame offsets back against the same value they passed in — a
    re-derived base silently shifts every timestamp (that bug made credits_start late by
    frac(duration) on every file).

    fpcalc has a -ts flag for seeking; for chunks that don't start at 0 we
    pre-decode with ffmpeg and pipe WAV to stdin (fpcalc accepts `-` as a
    pseudo-path on most builds, but piping via ffmpeg is more portable).
    """
    binp = fpcalc_bin()
    if not binp:
        return []
    if start_sec <= 0:
        proc = subprocess.run(
            _lp([binp, "-raw", "-length", str(length_sec), file_path]),
            capture_output=True, text=True, timeout=120,
            encoding="utf-8", errors="replace",   # never the locale code page (see GOTCHAS.md)
            **_LOWPRIO_KW,
        )
    else:
        ff = ffmpeg_bin()
        if not ff:
            return []
        # Pipe a mono 11025 Hz WAV chunk through ffmpeg → fpcalc on stdin
        ff_proc = subprocess.Popen(
            _lp([ff, "-loglevel", "error", "-ss", f"{start_sec:.3f}", "-t", str(length_sec),
                 "-i", file_path, "-ac", "1", "-ar", "11025", "-f", "wav", "pipe:1"]),
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, **_LOWPRIO_KW,
        )
        proc = subprocess.run(
            _lp([binp, "-raw", "-length", str(length_sec), "-"]),
            input=ff_proc.stdout.read() if ff_proc.stdout else b"",
            capture_output=True, timeout=120, **_LOWPRIO_KW,
        )
        ff_proc.wait(timeout=10)
        proc = subprocess.CompletedProcess(
            proc.args, proc.returncode,
            stdout=proc.stdout.decode("utf-8", errors="replace") if proc.stdout else "",
            stderr=proc.stderr.decode("utf-8", errors="replace") if proc.stderr else "",
        )

    if proc.returncode != 0:
        return []
    fp_line = ""
    for line in proc.stdout.splitlines():
        if line.startswith("FINGERPRINT="):
            fp_line = line[len("FINGERPRINT="):].strip()
            break
    if not fp_line:
        return []
    try:
        return [int(x) for x in fp_line.split(",") if x]
    except ValueError:
        return []


def _media_duration(file_path: str) -> Optional[float]:
    """Return duration in seconds via ffprobe (shipped with ffmpeg)."""
    ff = ffmpeg_bin()
    if not ff:
        return None
    # mediabin.ffprobe_bin() swaps only the FILENAME of the ffmpeg path. A blanket
    # str.replace rewrites EVERY "ffmpeg" segment of the bundled Windows path
    # ("tools/ffmpeg/ffmpeg-8.1.1-essentials_build/bin/ffmpeg.exe") and yields a path
    # that never exists - which silently demoted every Windows host to the brittle
    # stderr branch below, whose re.search then raised on a None stderr and crashed the
    # whole series. See docs/GOTCHAS.md.
    ffprobe = ffprobe_bin()
    if not ffprobe:
        # Fall back to parsing ffmpeg's stderr
        proc = subprocess.run(
            _lp([ff, "-i", file_path]), capture_output=True, text=True, timeout=15,
            encoding="utf-8", errors="replace",   # ffmpeg echoes the path + stream titles
            **_LOWPRIO_KW,
        )
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", proc.stderr or "")
        if m:
            h, mn, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
            return h * 3600 + mn * 60 + s
        return None
    proc = subprocess.run(
        _lp([ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", file_path]),
        capture_output=True, text=True, timeout=15,
        encoding="utf-8", errors="replace",   # never the locale code page (see GOTCHAS.md)
        **_LOWPRIO_KW,
    )
    try:
        return float(proc.stdout.strip())
    except (ValueError, TypeError):
        return None


# ── Matching algorithm ───────────────────────────────────────────────────────

def _popcount32(x: int) -> int:
    return bin(x & 0xFFFFFFFF).count("1")


def _find_longest_match(fp_a: list[int], fp_b: list[int],
                        min_frames: int, max_frames: int) -> Optional[tuple[int, int, int]]:
    """Find the longest run of approximately-matching frames between two fingerprints.

    Returns (offset_a, offset_b, length_frames) — the start positions in each
    fingerprint and how many frames matched. Returns None if no run >= min_frames.

    Uses a sliding alignment: for each offset d = -W..W between the two
    sequences, find the longest run where Hamming distance per frame is
    <= FRAME_HAMMING_MAX, bridging brief mismatch gaps (MATCH_GAP_FRAMES /
    MATCH_MIN_RATIO — see the constants block for why gaps are essential).

    Two implementations with identical coordinates/semantics: a numpy
    vectorized fast path (fractions of a second per pair) and a pure-Python
    fallback (tens of seconds per pair, strict consecutive runs only) for
    hosts whose venv predates the numpy dependency.

    Either way this is CPU work that holds the GIL (numpy releases it only
    inside individual ops) — `_run_match` dispatches it to a separate
    low-priority *process* so the event loop never starves (the GIL convoy
    effect made a worker thread freeze the dashboard — the classic "UI laggy
    but RDP fine" report). See `_match_executor`.
    """
    if not fp_a or not fp_b:
        return None
    if _np is not None:
        return _find_longest_match_np(fp_a, fp_b, min_frames, max_frames)
    return _find_longest_match_py(fp_a, fp_b, min_frames, max_frames)


def _best_gap_run(match: "_np.ndarray", min_frames: int) -> Optional[tuple[int, int]]:
    """Longest run of True in a boolean match array, bridging False gaps of up
    to MATCH_GAP_FRAMES when the merged span stays >= MATCH_MIN_RATIO matched.
    Returns (start, span_length) or None. Runs start/end on true matches by
    construction, so the reported window never includes leading/trailing noise."""
    m = match
    d = _np.diff(m.astype(_np.int8))
    starts = _np.flatnonzero(d == 1) + 1
    ends = _np.flatnonzero(d == -1) + 1
    if m[0]:
        starts = _np.concatenate(([0], starts))
    if m[-1]:
        ends = _np.concatenate((ends, [m.size]))
    best: Optional[tuple[int, int]] = None
    k, n = 0, int(starts.size)
    while k < n:
        s, e = int(starts[k]), int(ends[k])
        matched = e - s
        j = k + 1
        segs = [(s, e)]
        while j < n and int(starts[j]) - e <= MATCH_GAP_FRAMES:
            matched += int(ends[j]) - int(starts[j])
            e = int(ends[j])
            segs.append((int(starts[j]), e))
            j += 1
        s, e, matched = _trim_weak_edges(segs, matched)
        span = e - s
        if span >= min_frames and matched / span >= MATCH_MIN_RATIO:
            if best is None or span > best[1]:
                best = (s, span)
        k = j
    return best


def _trim_weak_edges(segs: list[tuple[int, int]], matched: int) -> tuple[int, int, int]:
    """Drop flimsy terminal segments from a gap-bridged run.

    Gap bridging is essential (see MATCH_GAP_FRAMES — a 1 s blip inside a theme corrupts
    ~20 frames, and a strict matcher truncates real intros), but it cuts both ways at the
    ENDS of a run: post-intro silence is correctly refused as match evidence by
    _informative_mask, then bridged over anyway, and whatever coincidentally matches
    within 4 s on the far side — a shared sting, a stock transition — drags the reported
    boundary out with it. The run still legitimately "ends on a true match", so the
    existing construction can't catch it.

    So walk inward from each end and drop any TERMINAL segment that is both short and
    separated by a real gap. Interior structure is untouched, which is what keeps a
    "theme, 2 s voiceover, 30 s more theme" match intact.
    """
    while len(segs) > 1:
        gap = segs[1][0] - segs[0][1]
        if segs[0][1] - segs[0][0] < MATCH_EDGE_MIN_FRAMES and gap >= MATCH_EDGE_GAP_FRAMES:
            matched -= segs[0][1] - segs[0][0]
            segs.pop(0)
            continue
        break
    while len(segs) > 1:
        gap = segs[-1][0] - segs[-2][1]
        if segs[-1][1] - segs[-1][0] < MATCH_EDGE_MIN_FRAMES and gap >= MATCH_EDGE_GAP_FRAMES:
            matched -= segs[-1][1] - segs[-1][0]
            segs.pop()
            continue
        break
    return segs[0][0], segs[-1][1], matched


def _informative_mask(fp: "_np.ndarray") -> "_np.ndarray":
    """Boolean mask of frames that changed >= MIN_FRAME_DELTA_BITS vs their
    predecessor — i.e. frames carrying actual audio structure rather than a
    repeated hash from stationary audio. Frame 0 has no predecessor → False."""
    out = _np.empty(fp.size, dtype=bool)
    out[0] = False
    d = fp[1:] ^ fp[:-1]
    out[1:] = (_POP16[d & 0xFFFF] + _POP16[d >> _np.uint32(16)]) >= MIN_FRAME_DELTA_BITS
    return out


def _find_longest_match_np(fp_a: list[int], fp_b: list[int],
                           min_frames: int, max_frames: int) -> Optional[tuple[int, int, int]]:
    """numpy fast path: per shift, Hamming distances come from a vectorized
    XOR + 16-bit popcount LUT; shifts that can't possibly beat the current best
    (too few matching frames in total) are rejected before run analysis."""
    a = _np.asarray(fp_a, dtype=_np.uint32)
    b = _np.asarray(fp_b, dtype=_np.uint32)
    max_shift = int(min(a.size, b.size)) - min_frames
    if max_shift < 1:
        return None
    info_a = _informative_mask(a)
    info_b = _informative_mask(b)
    best: Optional[tuple[int, int, int]] = None
    best_len = min_frames - 1
    for shift in range(-max_shift, max_shift + 1):
        if shift >= 0:
            i0, j0 = shift, 0
        else:
            i0, j0 = 0, -shift
        ln = min(int(a.size) - i0, int(b.size) - j0)
        if ln < min_frames:
            continue
        x = a[i0:i0 + ln] ^ b[j0:j0 + ln]
        hd = _POP16[x & 0xFFFF] + _POP16[x >> _np.uint32(16)]
        match = (hd <= FRAME_HAMMING_MAX) & info_a[i0:i0 + ln] & info_b[j0:j0 + ln]
        # A span must be >= MATCH_MIN_RATIO matched, so fewer matches than
        # ratio * (best span + 1) can't produce a new best — skip the RLE.
        if int(match.sum()) < MATCH_MIN_RATIO * max(min_frames, best_len + 1):
            continue
        run = _best_gap_run(match, min_frames)
        if run is None:
            continue
        s, span = run
        if span > max_frames:
            span = max_frames
        if span > best_len:
            best_len = span
            best = (i0 + s, j0 + s, span)
    return best


def _find_longest_match_py(fp_a: list[int], fp_b: list[int],
                           min_frames: int, max_frames: int) -> Optional[tuple[int, int, int]]:
    """Pure-Python fallback (no gap bridging — strict consecutive runs).
    O(N^2): ~10-20 s per pair for a 6-minute head. Only used when numpy is
    missing from the venv."""
    best: Optional[tuple[int, int, int]] = None

    # Search alignments within ±half the shorter fingerprint
    max_shift = min(len(fp_a), len(fp_b)) - min_frames
    if max_shift < 1:
        return None

    for shift in range(-max_shift, max_shift + 1):
        if shift >= 0:
            i_start, j_start = shift, 0
        else:
            i_start, j_start = 0, -shift
        run_len = 0
        run_i = i_start
        run_j = j_start
        i, j = i_start, j_start
        while i < len(fp_a) and j < len(fp_b):
            if _popcount32(fp_a[i] ^ fp_b[j]) <= FRAME_HAMMING_MAX:
                run_len += 1
                if run_len >= min_frames and (best is None or run_len > best[2]):
                    # Capture the running match window
                    best = (i - run_len + 1, j - run_len + 1, run_len)
            else:
                run_len = 0
                run_i = i + 1
                run_j = j + 1
            i += 1
            j += 1
            if run_len >= max_frames:
                break

    return best


def _match_worker_init() -> None:
    """Drop this matching worker to BELOW_NORMAL OS priority.

    The StreamLink server raises itself to HIGH at startup and spawned children
    inherit that, so without this the matching process would run at HIGH and lag
    the controls/UI — the exact thing moving it off-process is meant to fix.
    Mirrors `_LOWPRIO_KW` (which only works for `creationflags`-spawned
    subprocesses, not ProcessPoolExecutor workers). Best-effort: a host without
    psutil / nice privileges just runs at normal priority."""
    try:
        import psutil
        p = psutil.Process()
        if os.name == "nt":
            p.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
        else:
            p.nice(10)
    except Exception:
        try:
            if os.name == "posix":
                os.nice(10)
        except Exception:
            pass


def _new_match_executor() -> Optional[concurrent.futures.ProcessPoolExecutor]:
    """A single-worker, low-priority process pool for the CPU-bound matcher.

    One worker is enough — the orchestrator awaits matches one at a time, so we
    only need the work *off the server process*, not parallelism (which would
    just peg more cores). Returns None if the host can't create a process pool
    (restricted multiprocessing — e.g. no /dev/shm); callers fall back to a
    thread, accepting the GIL hit rather than failing analysis outright."""
    try:
        return concurrent.futures.ProcessPoolExecutor(
            max_workers=1, initializer=_match_worker_init,
        )
    except Exception:
        return None


async def _run_match(executor: Optional[concurrent.futures.ProcessPoolExecutor],
                     fp_a: list[int], fp_b: list[int],
                     min_frames: int, max_frames: int) -> Optional[tuple[int, int, int]]:
    """Run `_find_longest_match` off the event loop. Prefers a separate process
    (no GIL contention); falls back to a thread if the pool is missing/broken."""
    if executor is not None:
        try:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(
                executor, _find_longest_match, fp_a, fp_b, min_frames, max_frames,
            )
        except Exception:
            # Broken pool (worker died, platform limit) — degrade to a thread so
            # the analysis still completes. Worse for UI latency, but correct.
            pass
    return await asyncio.to_thread(_find_longest_match, fp_a, fp_b, min_frames, max_frames)


def _intersect_match(matches: list[tuple[int, int, int]],
                     min_frames: int) -> Optional[tuple[int, int]]:
    """Anchor-side window agreed by the cluster. Each tuple is (offset_in_anchor, _, length).

    A hard intersection (max of starts, min of ends) degrades badly as a cluster grows:
    ONE member whose match runs 5 s short truncates the reported intro for all 24
    episodes, and over a full season that's near-certain. So trim the worst
    CLUSTER_TRIM_Q at each edge before intersecting.

    For n < 5 this is EXACTLY the old hard intersection (k == 0), so small clusters —
    the ones with the least evidence — behave bit-identically to before.
    """
    if not matches:
        return None
    starts = sorted(m[0] for m in matches)
    ends   = sorted(m[0] + m[2] for m in matches)
    n = len(starts)
    k = int(n * CLUSTER_TRIM_Q)
    s = starts[n - 1 - k]
    e = ends[k]
    if e - s >= min_frames:
        return (s, e - s)
    return None


def _medoid_window(matches: dict) -> Optional[tuple[int, int]]:
    """Fallback anchor window when the intersection collapses: the REAL member interval
    with the greatest total overlap against all the others.

    Replaces taking the median offset and the median length independently, which could
    synthesise an (offset, length) pair that corresponds to no actual match at all and
    whose end could run past the anchor's fingerprint.
    """
    ivs = [(m[0], m[0] + m[2]) for m in matches.values()]
    if not ivs:
        return None

    def overlap(p, q):
        return max(0, min(p[1], q[1]) - max(p[0], q[0]))

    best = max(ivs, key=lambda p: sum(overlap(p, q) for q in ivs))
    return (best[0], best[1] - best[0])


def _project(anchor_win: tuple[int, int], m: tuple[int, int, int],
             fp_len: int, min_frames: int) -> Optional[tuple[int, int]]:
    """Map the anchor-side consensus window into one member's own frame coordinates.

    A pair match is a constant shift: ep_frame = anchor_frame + (offset_in_ep -
    offset_in_anchor). Without this, every NON-anchor episode kept its raw pairwise
    length — so a single pair that bridged 4 s of silence into a following shared cue
    became that episode's intro end directly, and episodes sharing one opening reported
    wildly different durations (measured: a 77 s spread across 101 Attack on Titan
    episodes for a fixed 90 s OP).

    Clamped twice: to the member's own matched evidence, then to its fingerprint. Through
    the intersection path both clamps are provably no-ops; they earn their keep on the
    medoid path, which deliberately breaks that invariant.
    """
    a_s, a_len = anchor_win
    a, b, length = m
    d = b - a
    s, e = a_s + d, a_s + a_len + d
    s = max(s, b)
    e = min(e, b + length)
    s = max(0, s)
    if fp_len:
        e = min(e, fp_len)
    if e - s < min_frames:
        return None
    return (s, e - s)


def _resolve_offset_in_cluster(idx: int, cluster: dict,
                               fp_lens: Optional[list[int]] = None) -> Optional[tuple[int, int]]:
    """Return (offset_frames, length_frames) of the shared segment in episode idx
    within the given cluster, or None if idx isn't a member.

    Every member — anchor included — is now expressed through the SAME consensus window,
    so an intro detected across a cluster has one agreed duration instead of one per
    pair. A member the projection can't place keeps its raw pairwise match rather than
    losing its skip point: a missing "skip intro" button is a worse regression than a
    slightly loose one.
    """
    anchor_win = cluster.get("anchor_range") or _medoid_window(cluster["matches"])
    if anchor_win is None:
        return None
    fp_len = 0
    if fp_lens and 0 <= idx < len(fp_lens):
        fp_len = fp_lens[idx]

    if idx == cluster["anchor_idx"]:
        s, ln = anchor_win
        if fp_len:
            ln = min(ln, max(0, fp_len - s))
        return (s, ln) if ln > 0 else None

    m = cluster["matches"].get(idx)
    if m is None:
        return None
    return _project(anchor_win, m, fp_len, MIN_MATCH_FRAMES) or (m[1], m[2])


def _filter_cluster_consensus(cluster: dict) -> None:
    """Prune cluster members that matched the anchor on a *different* region.

    Real shared intro/credits make every member match the SAME stretch of the
    anchor, so their anchor-side offsets agree. An outlier episode whose real
    intro/credits is absent can still pairwise-match the anchor on some OTHER
    recurring audio — a stinger, a repeated gag, a transition sting — landing at
    a different anchor offset. Left in, that member gets a bogus skip point (the
    reported "credits kick in early, cut off the end" bug).

    Mutates `cluster` in place: drop members whose `offset_in_anchor` deviates
    from the cluster median by more than CLUSTER_OFFSET_TOL_FRAMES, then recompute
    `anchor_range` over the survivors. The median member always survives, so a
    genuine cluster is left intact; an outlier simply disappears from the offset
    map and gets no skip point.
    """
    matches = cluster["matches"]
    if len(matches) <= 1:
        return  # nothing to compare against — a lone pairwise match stays
    anchor_offsets = sorted(m[0] for m in matches.values())
    median = anchor_offsets[len(anchor_offsets) // 2]
    survivors = {
        idx: m for idx, m in matches.items()
        if abs(m[0] - median) <= CLUSTER_OFFSET_TOL_FRAMES
    }
    if len(survivors) == len(matches):
        return  # consensus already unanimous
    cluster["matches"] = survivors
    cluster["anchor_range"] = (
        _intersect_match(list(survivors.values()), MIN_MATCH_FRAMES)
        if survivors else None
    )


def _build_ep_offset_map(clusters: list[dict],
                         fp_lens: Optional[list[int]] = None) -> dict[int, tuple[int, int]]:
    out: dict[int, tuple[int, int]] = {}
    for cluster in clusters:
        for idx in (cluster["anchor_idx"], *cluster["matches"].keys()):
            pos = _resolve_offset_in_cluster(idx, cluster, fp_lens)
            if pos is not None:
                out[idx] = pos
    return out


# ── Public API ───────────────────────────────────────────────────────────────

def _rate_for(fp_len: int, window_sec: Optional[float]) -> float:
    """Frames-per-second for one fingerprint, calibrated against the window we asked for.

    We know exactly how many seconds of audio fpcalc was given and how many frames came
    back, so the rate is recoverable per file rather than assumed. The relation is
    AFFINE (see FP_FRAME_LEADIN) — dividing frames by seconds under-corrects badly on
    short windows. Falls back to the constant whenever the result is implausible, which
    is the truncated-fingerprint case.

    This exists so a chromaprint build with different internals can't silently reintroduce
    the +3.55% scale error the hard-coded 7.8 caused.
    """
    if not fp_len or not window_sec or window_sec <= 30:
        return FP_FRAMES_PER_SEC
    rate = (fp_len + FP_FRAME_LEADIN) / window_sec
    return rate if FP_RATE_SANITY[0] <= rate <= FP_RATE_SANITY[1] else FP_FRAMES_PER_SEC


def frames_to_seconds(frames: int, rate: Optional[float] = None) -> float:
    """Convert a fingerprint frame index to seconds from the start of that fingerprint.

    A frame index is the START of its analysis window, not a point — frame k summarises
    roughly [k/rate, k/rate + 2.23 s]. Do NOT add window compensation here: a frame only
    matches cross-episode while its window is mostly shared audio, so the last matching
    frame already sits ~1 s BEFORE the true boundary. Compensating would push boundaries
    later, which is the wrong direction for the over-skip bug.
    """
    return frames / (rate or FP_FRAMES_PER_SEC)


def _failed_entry(error_code: str, error: str) -> dict:
    """Build a per-file skip_data entry that records a fingerprinting failure.

    The shape matches a successful entry so the rest of the system (admin
    editor, runtime skip-offer lookup, fingerprint-version migration) can read
    `analysis.source` uniformly. Callers that show user-facing chips should
    check for `analysis.source == "failed"`.
    """
    return {
        "intro": None,
        "credits_start": None,
        "analysis": {
            "version":    ANALYZER_VERSION,
            "source":     "failed",
            "error_code": error_code,
            "error":      error,
        },
    }


async def _build_clusters_async(
    fps: list[list[int]],
    min_frames: int,
    max_frames: int,
    episodes: list[dict],
    progress_cb,
    stage_label: str,
    executor: Optional[concurrent.futures.ProcessPoolExecutor] = None,
) -> list[dict]:
    """Greedy clustering of episodes that share a fingerprint segment.

    Picks the first un-clustered episode as anchor, pairwise-matches every
    remaining episode against it, then recurses on the unmatched leftovers.
    An anchor that matches nothing is treated as a singleton (special/OVA/
    episode with a unique opening) and is silently dropped.

    Returns a list of clusters, each:
        anchor_idx:   episode index of the cluster anchor
        matches:      dict[ep_idx -> (offset_in_anchor, offset_in_ep, length_frames)]
        anchor_range: Optional[(offset_frames, length_frames)] — intersection
                      of anchor-side windows across this cluster's matches,
                      or None if intersection collapsed below min_frames
    """
    remaining = [i for i in range(len(fps)) if fps[i]]
    clusters: list[dict] = []
    pairs_done = 0
    total_estimate = max(1, len(remaining) - 1)

    while len(remaining) >= 2:
        anchor = remaining[0]
        rest   = remaining[1:]
        total_estimate = max(total_estimate, pairs_done + len(rest))
        matches: dict[int, tuple[int, int, int]] = {}
        unmatched: list[int] = []
        for i in rest:
            pairs_done += 1
            ep_name = Path(episodes[i]["path"]).name if 0 <= i < len(episodes) else ""
            try:
                await progress_cb(
                    stage=stage_label,
                    current=pairs_done,
                    total=total_estimate,
                    message=f"Matching {pairs_done} of {total_estimate}",
                    episode_name=ep_name,
                )
            except Exception:
                pass
            m = await _run_match(executor, fps[anchor], fps[i], min_frames, max_frames)
            if m:
                matches[i] = m
            else:
                unmatched.append(i)

        if matches:
            anchor_range = _intersect_match(list(matches.values()), min_frames)
            clusters.append({
                "anchor_idx":   anchor,
                "matches":      matches,
                "anchor_range": anchor_range,
            })
        # else: anchor was a singleton — drop it, don't form a one-episode cluster

        remaining = unmatched

    return clusters


# Boundary refinement radii. The fingerprint finds where episodes STOP SHARING audio,
# which is several seconds short of where the intro actually ends — the matcher
# legitimately refuses the theme's fade-out, where the mix differs per episode. Measured
# on real episodes the residual is ~6 s, so the intro-end radius has to be wider than
# that or the correct candidate falls outside the window and nothing snaps.
#
# NOTE the direction: before the FP_FRAMES_PER_SEC fix the fingerprint OVER-shot the
# intro end; now it UNDER-shoots. The bias is forward for that reason, and it is a soft
# preference rather than a one-sided window precisely because that sign has moved once.
REFINE_INTRO_END_RADIUS   = 10.0
REFINE_INTRO_START_RADIUS = 4.0
REFINE_MIN_CONF           = 0.45


def _refine_one(path: str, intro: Optional[dict], credits_start: Optional[float],
                dur: Optional[float]) -> tuple[Optional[dict], Optional[float], str, Optional[dict]]:
    """Pin a coarse fingerprint boundary to a chapter marker or a silence edge.

    Returns (intro, credits_start, method, refine_block). Never raises and never returns
    a boundary it did not find evidence for: with no chapters and no silence in range the
    inputs come back unchanged with method "fingerprint".
    """
    audio_base = {
        "intro_start":   intro["start"] if intro else None,
        "intro_end":     intro["end"] if intro else None,
        "credits_start": credits_start,
    }
    prov: dict = {}
    method = "fingerprint"
    try:
        chapters = refiner.probe_chapters(path)
    except Exception:
        chapters = []

    # 1. A named, plausible intro chapter is authoritative — it was written from the
    #    master, not inferred. But it still loses to a confirmed audio match it flatly
    #    contradicts: a disagreement that large means they are describing different
    #    things, and the fingerprint is the one with cross-episode evidence behind it.
    ch_intro = refiner.chapter_intro(chapters) if chapters else None
    if ch_intro and intro and abs(ch_intro["end"] - intro["end"]) > refiner.CHAPTER_VETO_SEC:
        ch_intro = None
    if ch_intro:
        intro = ch_intro
        method = "chapters"
        prov["intro_start"] = {"t": intro["start"], "conf": 1.0, "src": "chapter"}
        prov["intro_end"]   = {"t": intro["end"],   "conf": 1.0, "src": "chapter"}
    elif intro:
        # 2. No usable chapter — snap the audio boundary onto the silence that separates
        #    the theme from the first line of dialogue.
        cuts = list(refiner.chapter_cuts(chapters)) if chapters else []
        win_start = max(0.0, intro["start"] - 12.0)
        win_len = (intro["end"] + REFINE_INTRO_END_RADIUS + 4.0) - win_start
        runs = refiner.merge_silence(refiner.detect_silence(path, win_start, win_len))
        if not runs:
            runs = refiner.merge_silence(refiner.detect_silence(
                path, win_start, win_len,
                noise_db=refiner.SILENCE_DB_LOOSE, min_sec=refiner.SILENCE_MIN_SEC_LOOSE))
        cuts += refiner.silence_cuts(runs)

        # An intro END must never land on a silence START — that is the instant the theme
        # stopped, not the instant content resumes, and snapping there parks the viewer
        # in several seconds of dead air.
        t, conf, src = refiner.snap_boundary(
            intro["end"], cuts, radius=REFINE_INTRO_END_RADIUS, bias="fwd",
            prefer=("chapter", "silence_end"), allow=("chapter", "silence_end"),
            min_conf=REFINE_MIN_CONF,
            floor=intro["start"] + MIN_INTRO_SEC, ceil=intro["end"] + REFINE_INTRO_END_RADIUS,
        )
        if conf > 0:
            intro = dict(intro, end=round(t, 1))
            prov["intro_end"] = {"t": round(t, 1), "conf": conf, "src": src}
            method = "fingerprint+silence"

        # The intro START moves symmetrically: the theme begins where the silence before
        # it ends. Tighter radius and a higher bar, because a start error eats content.
        t, conf, src = refiner.snap_boundary(
            intro["start"], cuts, radius=REFINE_INTRO_START_RADIUS, bias="back",
            prefer=("chapter", "silence_end"), allow=("chapter", "silence_end"),
            min_conf=0.55, floor=0.0, ceil=intro["end"] - MIN_INTRO_SEC,
        )
        if conf > 0:
            intro = dict(intro, start=round(t, 1))
            prov["intro_start"] = {"t": round(t, 1), "conf": conf, "src": src}

    # 3. Credits. A named chapter is used when present; otherwise the fingerprint value
    #    stands untouched. Measured against chapter ground truth, the corrected
    #    fingerprint already lands within ~0.1 s, so there is nothing to gain from
    #    snapping it — and moving credits EARLY truncates real content, which is the
    #    most user-hostile error available here.
    if chapters and dur:
        cc = refiner.chapter_credits(chapters, dur, MIN_CREDITS_PCT, OUTRO_END_MARGIN_SEC)
        if cc is not None and (credits_start is None
                               or abs(cc - credits_start) <= refiner.CHAPTER_VETO_SEC):
            credits_start = cc
            prov["credits_start"] = {"t": cc, "conf": 1.0, "src": "chapter"}
            if method == "fingerprint":
                method = "chapters"

    if not prov:
        return intro, credits_start, method, None
    return intro, credits_start, method, {"version": REFINER_VERSION, "audio": audio_base, **prov}


REFINER_VERSION = 1


async def analyze_series(items: list[dict], progress_cb=None) -> dict:
    """Analyze a series of items, returning per-file intro/credits ranges.

    Input: a list of library items belonging to the same series, each with a
    "files" list of dicts containing "path".

    Output: a dict keyed by absolute file path:
        { path: {"intro": {"start": s, "end": s}, "credits_start": s,
                 "analysis": {"version": N, "source": "auto"}} }

    Only paths that exist on disk and have at least one peer episode in the
    same series are analyzed. Movies (1 file, no peers) get the 92% credits
    heuristic only.

    progress_cb is an optional async callable invoked with kwargs:
        stage:    "fingerprinting" | "matching-intros" | "matching-outros" | "finalizing"
        current:  int (1-based item being processed)
        total:    int (total items in this stage)
        message:  short human-readable string
        episode_name: optional basename of file being processed
        progress: float 0..1 — overall run fraction across ALL stages,
                  guaranteed monotonic (use this for progress bars; the
                  per-stage current/total resets at stage boundaries and the
                  matching stages' total is a growing estimate)
    """
    last_progress = 0.0

    async def _emit(**kw):
        nonlocal last_progress
        lo, hi = _STAGE_SPAN.get(kw.get("stage", ""), (0.0, 1.0))
        total = kw.get("total") or 0
        cur = min(kw.get("current") or 0, total) if total else 0
        frac = (cur / total) if total else 0.0
        last_progress = max(last_progress, lo + (hi - lo) * frac)
        kw["progress"] = round(last_progress, 4)
        if progress_cb is None:
            return
        try:
            await progress_cb(**kw)
        except Exception:
            pass

    # Collect every file path the caller asked us to analyze. The result map
    # always carries an entry per path — successes get intro/credits, failures
    # carry an `analysis.source == "failed"` marker so the UI can flag the file
    # as un-skippable and the admin can see why.
    all_paths: list[str] = []
    seen: set[str] = set()
    for item in items:
        for f in item.get("files", []):
            p = f.get("path", "")
            if p and p not in seen:
                seen.add(p)
                all_paths.append(p)

    if not all_paths:
        return {}

    # Missing binaries: every file fails the same way. We still return an entry
    # per file so the user-facing "Skip unavailable" chip shows up consistently
    # and the admin log records the host-level diagnostic.
    if not is_available():
        ff_ok = bool(ffmpeg_bin())
        fp_ok = bool(fpcalc_bin())
        missing = [n for n, ok in (("ffmpeg", ff_ok), ("fpcalc", fp_ok)) if not ok]
        err = (
            f"Required binary not installed on the host: {', '.join(missing)}. "
            "Smart Skip cannot fingerprint audio without it — re-run setup.py "
            "after installing the dependency."
        )
        return {p: _failed_entry(ERR_NO_BINARY, err) for p in all_paths}

    # Files we'll actually fingerprint (exist on disk). Missing files get a
    # failure entry up-front and are excluded from the clustering input.
    result: dict = {}
    episodes: list[dict] = []
    for p in all_paths:
        if not Path(p).exists():
            result[p] = _failed_entry(
                ERR_FILE_MISSING,
                "Video file is not present on disk — Smart Skip could not fingerprint it.",
            )
        else:
            episodes.append({"path": p})

    if len(episodes) < 2:
        # No peers — nothing to fingerprint against. Credits time is derived ONLY
        # from a confirmed cross-episode match (there is no fabricated fallback),
        # so a lone file (e.g. a single movie) gets no skip points and is recorded
        # as a per-file failure that drives the "Skip unavailable" chip.
        for idx, ep in enumerate(episodes, start=1):
            await _emit(stage="finalizing", current=idx, total=len(episodes),
                        message="No peer episodes to fingerprint against",
                        episode_name=Path(ep["path"]).name)
            dur = await _fp_thread(_media_duration, ep["path"])
            if dur:
                result[ep["path"]] = _failed_entry(
                    ERR_NO_SKIP,
                    "No peer episodes to fingerprint against — Smart Skip needs at "
                    "least two episodes that share an intro/credits sequence.",
                )
            else:
                result[ep["path"]] = _failed_entry(
                    ERR_NO_DURATION,
                    "ffprobe could not determine the media duration — the container may be "
                    "unsupported or the file may be partially written.",
                )
        return result

    # Compute fingerprints for the head and tail of each episode, FP_CONCURRENCY
    # at a time (the ffmpeg/fpcalc decodes are subprocess work that releases the
    # GIL, so a small parallel window roughly halves stage wall-clock). Results
    # land in index order; the progress counter advances per *completed* episode.
    # Track per-episode failures so files where fpcalc returned nothing are
    # recorded as failures instead of silently disappearing from the result.
    head_fps: list[list[int]] = []
    tail_fps: list[list[int]] = []
    durations: list[Optional[float]] = []
    # Per-episode frame rate and tail time base. Both used to be implicit constants and
    # both were wrong: the rate was a hard-coded 7.8 (see FP_FRAMES_PER_SEC) and the tail
    # base was re-derived as a float AFTER seeking with an int, so credits_start was late
    # by frac(duration) on top of the scale error.
    head_rates: list[float] = []
    tail_rates: list[float] = []
    tail_starts: list[float] = []
    fp_errors: dict[int, tuple[str, str]] = {}   # ep idx → (code, message)
    total_eps = len(episodes)
    fp_sem = asyncio.Semaphore(FP_CONCURRENCY)
    fp_done = 0

    async def _fingerprint_one(ep: dict):
        nonlocal fp_done
        async with fp_sem:
            dur = await _fp_thread(_media_duration, ep["path"])
            head = await _fp_thread(_fpcalc_raw, ep["path"], INTRO_SEARCH_SECS, 0)
            # Seek and convert-back must share ONE time base. Keep the float and pass it
            # through; never re-derive it at the other end.
            tail_start = 0.0
            if dur and dur > OUTRO_SEARCH_SECS + 60:
                tail_start = float(dur - OUTRO_SEARCH_SECS)
                tail = await _fp_thread(_fpcalc_raw, ep["path"], OUTRO_SEARCH_SECS, tail_start)
            else:
                tail = []
        fp_done += 1
        await _emit(stage="fingerprinting", current=fp_done, total=total_eps,
                    message=f"Fingerprinting episode {fp_done} of {total_eps}",
                    episode_name=Path(ep["path"]).name)
        # The head window is whatever fpcalc actually got: the -length cap, or the file
        # if it is shorter. The tail window is exactly OUTRO_SEARCH_SECS by construction.
        head_window = min(float(INTRO_SEARCH_SECS), dur) if dur else float(INTRO_SEARCH_SECS)
        return (dur, head, tail, tail_start,
                _rate_for(len(head), head_window),
                _rate_for(len(tail), float(OUTRO_SEARCH_SECS)))

    await _emit(stage="fingerprinting", current=0, total=total_eps,
                message=f"Fingerprinting {total_eps} episode(s)")
    fp_results = await asyncio.gather(*(_fingerprint_one(ep) for ep in episodes))
    for idx, (dur, head, tail, t_start, h_rate, t_rate) in enumerate(fp_results):
        durations.append(dur)
        head_fps.append(head)
        tail_fps.append(tail)
        tail_starts.append(t_start)
        head_rates.append(h_rate)
        tail_rates.append(t_rate)
        if not head:
            fp_errors[idx] = (
                ERR_FP_EMPTY,
                "fpcalc produced no fingerprint for the head of this file "
                "(unsupported audio codec, silent track, or corrupted container).",
            )

    # ── Cluster intros and outros independently.
    #
    # Greedy clustering: pick the first un-clustered episode as anchor, match
    # every other un-clustered episode against it, then recurse on whatever's
    # left. This naturally handles three failure modes the old single-anchor
    # approach couldn't:
    #   • Specials/OVAs mixed into the torrent — they match nothing, form no
    #     cluster, and get no false intro skip.
    #   • Mid-season intro changes — eps with the new opening drop out of the
    #     first cluster and form their own on the second pass.
    #   • Episode 0 being a special — first pass finds an empty cluster and
    #     moves on; the real intro group still gets detected from ep 1+.
    # The pairwise matcher is pure-Python CPU work; run it in a separate
    # low-priority process so it never starves the server's event loop (a worker
    # *thread* wouldn't — the GIL convoy effect freezes the dashboard). One pool
    # serves both stages; tear it down in finally so the worker never lingers.
    executor = _new_match_executor()
    try:
        max_intro_frames = int(MAX_INTRO_SEC * FP_FRAMES_PER_SEC)
        intro_clusters = await _build_clusters_async(
            head_fps, MIN_MATCH_FRAMES, max_intro_frames,
            episodes, _emit, "matching-intros", executor,
        )
        for c in intro_clusters:
            _filter_cluster_consensus(c)
        intro_by_ep = _build_ep_offset_map(intro_clusters, [len(f) for f in head_fps])

        max_outro_frames = int(MAX_OUTRO_SEC * FP_FRAMES_PER_SEC)
        outro_clusters = await _build_clusters_async(
            tail_fps, MIN_MATCH_FRAMES, max_outro_frames,
            episodes, _emit, "matching-outros", executor,
        )
        for c in outro_clusters:
            _filter_cluster_consensus(c)
        outro_by_ep = _build_ep_offset_map(outro_clusters, [len(f) for f in tail_fps])
    finally:
        if executor is not None:
            executor.shutdown(wait=False)

    # ── Build per-episode result ─────────────────────────────────────────────
    # `result` already carries per-path failure entries for missing files. Now
    # add per-episode entries — success for files that produced intro/credits,
    # failure for files that produced nothing usable.
    # Episodes the fingerprint could not give credits to. Some shows use a different
    # song over every credit roll (Hacks, WandaVision, One Tree Hill S8), so the tail
    # matcher correctly finds nothing and the whole series gets no credits skip. Those
    # get a second, song-independent pass below.
    no_credits: list[int] = []
    got_credits = 0

    for idx, ep in enumerate(episodes):
        path = ep["path"]
        dur = durations[idx]
        ep_name = Path(path).name

        await _emit(stage="finalizing", current=idx + 1, total=total_eps,
                    message=f"Finalizing episode {idx + 1} of {total_eps}",
                    episode_name=ep_name)

        intro: Optional[dict] = None
        if idx in intro_by_ep:
            s_fr, l_fr = intro_by_ep[idx]
            intro = {
                "start": round(frames_to_seconds(s_fr, head_rates[idx]), 1),
                "end":   round(frames_to_seconds(s_fr + l_fr, head_rates[idx]), 1),
            }

        # Credits time comes ONLY from a confirmed cross-episode fingerprint
        # match — no fabricated fallback. The matched tail run is accepted as
        # credits only when it both starts late enough (MIN_CREDITS_PCT) and runs
        # to ~the end of the file (within OUTRO_END_MARGIN_SEC). A recurring
        # non-credits cue near the end (a stinger/gag) is followed by more
        # content, so its run ends well before the file end → rejected → no skip.
        credits_start: Optional[float] = None
        if idx in outro_by_ep and dur:
            offset_fr, length_fr = outro_by_ep[idx]
            tail_start = tail_starts[idx]
            cs = tail_start + frames_to_seconds(offset_fr, tail_rates[idx])
            ce = tail_start + frames_to_seconds(offset_fr + length_fr, tail_rates[idx])
            if cs >= dur * MIN_CREDITS_PCT and ce >= dur - OUTRO_END_MARGIN_SEC:
                credits_start = round(cs, 1)
        if credits_start is None and dur:
            no_credits.append(idx)
        elif credits_start is not None:
            got_credits += 1

        # Pin the coarse boundaries to something physical (chapter marker / silence).
        # Off-loop: this spawns ffprobe + ffmpeg. Never allowed to fail the episode.
        method, refine_block = "fingerprint", None
        if intro or credits_start is not None:
            try:
                intro, credits_start, method, refine_block = await _fp_thread(
                    _refine_one, path, intro, credits_start, dur)
            except Exception:
                method, refine_block = "fingerprint", None

        if intro or credits_start is not None:
            result[path] = {
                "intro": intro,
                "credits_start": credits_start,
                **({"refine": refine_block} if refine_block else {}),
                "analysis": {
                    "version": ANALYZER_VERSION,
                    "method":  method,
                    "source": "auto",
                },
            }
        else:
            # Nothing usable — record why so the admin can see it and the user
            # gets the "Skip unavailable" chip. fpcalc emptiness is the most
            # specific cause; missing duration is the next; otherwise the matcher
            # ran on usable input but found no shared intro and no qualifying
            # credits run (credits time is fingerprint-only — no fallback).
            if idx in fp_errors:
                code, msg = fp_errors[idx]
            elif not dur:
                code, msg = ERR_NO_DURATION, (
                    "ffprobe could not determine the media duration."
                )
            else:
                code, msg = ERR_NO_SKIP, (
                    "Fingerprinting completed but no shared intro and no credits "
                    "transition could be located in this file."
                )
            result[path] = _failed_entry(code, msg)

    # ── Subtitle credits pass ────────────────────────────────────────────────
    # Only for episodes the fingerprint gave no credits. "Have the credits started?" is
    # really "has the dialogue ended for good?", and a subtitle track answers that
    # directly, for free, without decoding anything.
    #
    # This replaced an audio-only structural detector that could not work on the shows it
    # was built for. Those shows run a song across the seam from the last scene into the
    # credit roll, so there is NO acoustic event at the boundary: the detector took the
    # last gap-free block of sound and landed deep inside real content — measured at 19 s
    # on Hacks S05E05 and 69 s on S03E02. Its cross-episode consensus check endorsed the
    # error rather than catching it, because every episode was wrong in the same
    # direction, and agreement only ever proves precision, not accuracy.
    #
    # No consensus requirement here, deliberately. The signal is self-validating per
    # episode (dialogue stops, only music or nothing follows), and requiring peers would
    # reject exactly the files this is for — single-episode library items, which is how
    # most of the rotating-theme content in the wild is filed.
    if no_credits:
        for n, idx in enumerate(no_credits, 1):
            dur = durations[idx]
            if not dur:
                continue
            path = episodes[idx]["path"]
            # `no_credits` was built from the FINGERPRINT result, before refinement ran —
            # and refinement can supply credits from a chapter marker, which is exact and
            # outranks anything measured here. Re-check the entry rather than the list, or
            # a chapter-derived time gets overwritten by a ~2 s estimate (observed moving
            # one file 106.9 s).
            if (result.get(path) or {}).get("credits_start") is not None:
                continue
            await _emit(stage="finalizing", current=total_eps, total=total_eps,
                        message=f"Looking for credit rolls — {n} of {len(no_credits)}",
                        episode_name=Path(path).name)
            try:
                found = await _fp_thread(refiner.credits_from_subtitles,
                                         path, dur, MIN_CREDITS_PCT)
            except Exception:
                found = None
            if not found:
                continue
            cs, src = found
            entry = result.get(path) or {}
            prev = entry.get("analysis") or {}
            if prev.get("source") == "failed" and not entry.get("intro"):
                # Nothing at all before: promote to a credits-only success rather than
                # leaving the "Skip unavailable" chip up.
                entry = {"intro": None, "analysis": {"version": ANALYZER_VERSION,
                                                     "source": "auto"}}
            entry["credits_start"] = cs
            ana = entry.setdefault("analysis", {})
            ana["version"] = ANALYZER_VERSION
            ana["source"] = "auto"
            base = ana.get("method") or "fingerprint"
            ana["method"] = src if base == "fingerprint" else f"{base}+{src}"
            ana.pop("error_code", None)
            ana.pop("error", None)
            ref = entry.setdefault("refine", {"version": REFINER_VERSION,
                                              "audio": {"credits_start": None}})
            ref["credits_start"] = {"t": cs, "conf": 0.8, "src": src}
            result[path] = entry

    return result


# Per-series locks live in the parent process; analyzer just exposes a helper
_series_locks: dict[str, asyncio.Lock] = {}


def lock_for_series(key: str) -> asyncio.Lock:
    """Return a per-series lock so concurrent analyze calls for the same
    series serialize. Different series can run in parallel."""
    lock = _series_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _series_locks[key] = lock
    return lock
