"""Smart Skip — audio fingerprinting for intro/credits detection.

Uses ffmpeg (audio decode) + chromaprint/fpcalc (fingerprinting) to find audio
segments that repeat across episodes of a series. The repeating segment near
the start of each file is the intro; the one near the end (if present) is the
credits/outro. Falls back to ffmpeg blackdetect + a 92% heuristic when
chromaprint cannot find a clean repeating outro.

All blocking work runs in threads (asyncio.to_thread). The orchestrator runs
one series at a time, with a per-series asyncio.Lock to prevent re-entry.
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

ANALYZER_VERSION = 2

# Per-file failure codes recorded in skip_data[path].analysis when fingerprinting
# could not produce usable skip points. The user-facing UI shows a "Skip
# unavailable" chip for any of these; the admin editor + analyzer log surface
# the message verbatim. Keep codes stable — clients may filter on them.
ERR_NO_BINARY    = "no_binary"      # ffmpeg or fpcalc not installed on host
ERR_FILE_MISSING = "file_missing"   # video file is not on disk
ERR_NO_DURATION  = "no_duration"    # ffprobe could not read duration
ERR_FP_EMPTY     = "fp_empty"       # fpcalc returned no fingerprint (codec/corruption)
ERR_TOO_SHORT    = "too_short"      # file shorter than the credits-fallback threshold
ERR_NO_SKIP      = "no_skip_points" # fingerprinting ran but produced nothing usable
ERR_EXCEPTION    = "exception"      # raised inside analyze_series — message carries detail

# Run analyzer subprocesses (ffmpeg decode, fpcalc, ffprobe) at lowered OS
# priority so a Smart-Skip pass never starves the StreamLink server. The server
# raises itself to HIGH at startup and children inherit that, so without this an
# analysis would run at HIGH and lag the controls/UI — exactly what we're trying
# to avoid. Windows: BELOW_NORMAL_PRIORITY_CLASS via creationflags. POSIX:
# prepend `nice -n 10` (no-op when `nice` isn't on PATH). Mirrors main.py's prep.
_LOWPRIO_KW: dict = {}
if os.name == "nt":
    _LOWPRIO_KW["creationflags"] = 0x00004000  # BELOW_NORMAL_PRIORITY_CLASS


def _lp(cmd: list[str]) -> list[str]:
    """Prefix `nice -n 10` on POSIX (when available) so the child de-prioritizes."""
    if os.name == "posix" and shutil.which("nice"):
        return ["nice", "-n", "10", *cmd]
    return cmd

# Chromaprint emits ~7.8 fingerprint frames per second (8192 samples @ 11025 Hz).
# Each frame is one 32-bit integer.
FP_FRAMES_PER_SEC = 7.8

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
CREDITS_FALLBACK_PCT = 0.92   # if no outro found, mark credits at 92%

# Manual / template extrapolation (mode == "manual"). An admin marks an intro
# window on one episode in the on-device player; we fingerprint that exact span
# and search for it in every other episode's head. A template can be shorter
# than the 15 s auto-intro floor (a stinger / short theme), so the match floor
# is relaxed here — but never below TEMPLATE_MIN_MATCH_SEC of audio, otherwise a
# few coincidentally-similar frames would false-positive an intro.
TEMPLATE_MIN_MATCH_SEC = 8


def _env_bin(env_key: str) -> Optional[str]:
    """Read a binary path from the .env file. Falls back to PATH lookup."""
    env_file = Path(__file__).parent / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line.startswith(f"{env_key}="):
                val = line.split("=", 1)[1].strip()
                if val and Path(val).exists():
                    return val
    return shutil.which(env_key.replace("_BIN", "").replace("_", "").lower())


def fpcalc_bin() -> Optional[str]:
    return _env_bin("_FPCALC_BIN") or shutil.which("fpcalc")


def ffmpeg_bin() -> Optional[str]:
    return _env_bin("_FFMPEG_BIN") or shutil.which("ffmpeg")


def is_available() -> bool:
    return bool(fpcalc_bin() and ffmpeg_bin())


# ── Fingerprinting ───────────────────────────────────────────────────────────

def _fpcalc_raw(file_path: str, length_sec: int, start_sec: int = 0) -> list[int]:
    """Run fpcalc and return the raw fingerprint as a list of 32-bit ints.

    Uses -raw to get the integer sequence directly. The first FP_FRAMES_PER_SEC
    integers correspond to roughly the first second of audio.

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
            capture_output=True, text=True, timeout=120, **_LOWPRIO_KW,
        )
    else:
        ff = ffmpeg_bin()
        if not ff:
            return []
        # Pipe a mono 11025 Hz WAV chunk through ffmpeg → fpcalc on stdin
        ff_proc = subprocess.Popen(
            _lp([ff, "-loglevel", "error", "-ss", str(start_sec), "-t", str(length_sec),
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
    ffprobe = ff.replace("ffmpeg", "ffprobe") if "ffmpeg" in ff else None
    if not ffprobe or not Path(ffprobe).exists():
        ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        # Fall back to parsing ffmpeg's stderr
        proc = subprocess.run(
            _lp([ff, "-i", file_path]), capture_output=True, text=True, timeout=15,
            **_LOWPRIO_KW,
        )
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", proc.stderr)
        if m:
            h, mn, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
            return h * 3600 + mn * 60 + s
        return None
    proc = subprocess.run(
        _lp([ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", file_path]),
        capture_output=True, text=True, timeout=15, **_LOWPRIO_KW,
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
    sequences, scan and find the longest consecutive run where Hamming
    distance per frame <= FRAME_HAMMING_MAX. This is O(N^2) over a bounded
    window; for ~6 minutes at 7.8 fps that's ~3000^2 = 9M ops, ~1s in C and
    ~10-20s in pure Python.

    This is pure-Python CPU work — it holds the GIL for the whole run. Running
    it in a worker *thread* (asyncio.to_thread) does NOT free the event loop:
    the GIL convoy effect on multi-core hosts starves the loop thread for
    seconds at a time, freezing the dashboard even though the box is healthy
    (the classic "UI laggy but RDP fine" report). So `_run_match` dispatches it
    to a separate low-priority *process* instead — see `_match_executor`.
    """
    if not fp_a or not fp_b:
        return None

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
    """Given per-pair matches with the same anchor file, return (start, length)
    of the intersection. Each tuple is (offset_in_anchor, _, length)."""
    if not matches:
        return None
    starts = [m[0] for m in matches]
    ends   = [m[0] + m[2] for m in matches]
    s = max(starts)
    e = min(ends)
    if e - s >= min_frames:
        return (s, e - s)
    return None


def _resolve_offset_in_cluster(idx: int, cluster: dict) -> Optional[tuple[int, int]]:
    """Return (offset_frames, length_frames) of the shared segment in episode idx
    within the given cluster, or None if idx isn't a member."""
    if idx == cluster["anchor_idx"]:
        rng = cluster.get("anchor_range")
        if rng:
            return rng
        # Intersection collapsed — anchor still belongs to the cluster, so fall
        # back to the median (offset_in_anchor, length) across pair matches.
        pairs = list(cluster["matches"].values())
        if not pairs:
            return None
        offsets_a = sorted(m[0] for m in pairs)
        lengths   = sorted(m[2] for m in pairs)
        return (offsets_a[len(offsets_a) // 2], lengths[len(lengths) // 2])
    m = cluster["matches"].get(idx)
    if m is None:
        return None
    return (m[1], m[2])


def _build_ep_offset_map(clusters: list[dict]) -> dict[int, tuple[int, int]]:
    out: dict[int, tuple[int, int]] = {}
    for cluster in clusters:
        for idx in (cluster["anchor_idx"], *cluster["matches"].keys()):
            pos = _resolve_offset_in_cluster(idx, cluster)
            if pos is not None:
                out[idx] = pos
    return out


# ── Manual template extrapolation ────────────────────────────────────────────

def _template_min_frames(tmpl_len_frames: int) -> int:
    """Minimum matched-run length for a template, in frames.

    Relax the 15 s auto floor for short templates, but never accept a run
    shorter than TEMPLATE_MIN_MATCH_SEC of audio (or 60 % of the template,
    whichever is smaller) so a handful of similar frames can't false-positive.
    """
    floor = int(TEMPLATE_MIN_MATCH_SEC * FP_FRAMES_PER_SEC)
    return max(floor, min(MIN_MATCH_FRAMES, int(tmpl_len_frames * 0.6)))


def _fingerprint_templates(templates: list[dict]) -> list[dict]:
    """Compute the raw fingerprint of each template's [start, end] source window.

    Returns a parallel list of {name, id, fp, frames} — fp empty when the source
    file is missing/unreadable or the span fingerprinted to nothing (that
    template is then skipped during matching). Runs fpcalc/ffmpeg, so callers
    must invoke it via asyncio.to_thread.
    """
    out: list[dict] = []
    for t in templates:
        try:
            start = max(0.0, float(t.get("start", 0)))
            end = float(t.get("end", 0))
        except (TypeError, ValueError):
            start, end = 0.0, 0.0
        length = int(round(end - start))
        src = t.get("source_path", "")
        fp: list[int] = []
        if length >= 1 and src and Path(src).exists():
            fp = _fpcalc_raw(src, length, int(start))
        out.append({
            "name":   t.get("name", ""),
            "id":     t.get("id", ""),
            "fp":     fp,
            "frames": len(fp),
            "secs":   round(end - start, 1),
        })
    return out


async def _match_templates_to_heads(
    template_fps: list[dict],
    head_fps: list[list[int]],
    progress_cb,
    executor: Optional[concurrent.futures.ProcessPoolExecutor] = None,
    episodes: Optional[list[dict]] = None,
) -> dict[int, tuple[float, float, str]]:
    """For each episode head, find the best-matching template and align it.

    Returns dict[ep_idx -> (intro_start_sec, intro_end_sec, template_name)].
    Episodes that match no template are absent (intro stays None — they still
    get the auto credits fallback downstream).

    Alignment: `_find_longest_match(template_fp, head_fp)` returns
    (offset_in_template, offset_in_head, run_len). The template window is the
    intro, so the episode's intro start is where the template's *own* start
    lands in the head: `head_offset − template_offset` (clamped ≥0). The intro
    length is the template's real duration (end − start), independent of how
    much of it matched.
    """
    out: dict[int, tuple[float, float, str]] = {}
    usable = [t for t in template_fps if t["fp"]]
    if not usable:
        return out
    total = len(head_fps)
    for idx, head in enumerate(head_fps):
        ep_name = (Path(episodes[idx]["path"]).name
                   if episodes and 0 <= idx < len(episodes) else "")
        try:
            await progress_cb(
                stage="matching-intros", current=idx + 1, total=total,
                message=f"Matching templates {idx + 1} of {total}",
                episode_name=ep_name,
            )
        except Exception:
            pass
        if not head:
            continue
        # (run_len, start_sec, secs, name) of the best-matching template so far.
        best: Optional[tuple[int, float, float, str]] = None
        for t in usable:
            min_fr = _template_min_frames(t["frames"])
            max_fr = int(MAX_INTRO_SEC * FP_FRAMES_PER_SEC)
            m = await _run_match(executor, t["fp"], head, min_fr, max_fr)
            if not m:
                continue
            off_in_tmpl, off_in_head, run_len = m
            start_fr = max(0, off_in_head - off_in_tmpl)
            if best is None or run_len > best[0]:
                best = (run_len, frames_to_seconds(start_fr), t["secs"], t["name"])
        if best is not None:
            _, start_sec, secs, name = best
            out[idx] = (round(start_sec, 1), round(start_sec + secs, 1), name)
    return out


# ── Public API ───────────────────────────────────────────────────────────────

def frames_to_seconds(frames: int) -> float:
    return frames / FP_FRAMES_PER_SEC


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


def _detect_blackframe(file_path: str, start_at_sec: float,
                       scan_duration_sec: float = 300.0) -> Optional[float]:
    """Use ffmpeg blackdetect to find the first long black segment after start_at_sec.

    Scans at most scan_duration_sec of video (default 5 minutes) — long enough
    to catch the credits transition on virtually any show but short enough that
    a single episode finishes in seconds, not minutes. Decoding the full tail
    of a long episode here is what made the analyzer appear to hang at 100%.

    Returns absolute seconds at which the credits/black-fade begins, or None.
    """
    ff = ffmpeg_bin()
    if not ff:
        return None
    # Two-pass strategy: keyframes-only first (~1-2s per episode) for the
    # common case where credits start with a clear black fade aligned to a
    # keyframe boundary. If that misses, fall back to a full-decode pass at
    # 4 fps which is ~10x faster than realtime decode.
    proc = subprocess.run(
        _lp([ff, "-skip_frame", "nokey",
             "-ss", str(int(start_at_sec)), "-t", str(int(scan_duration_sec)),
             "-i", file_path,
             "-vf", "scale=64:-2,blackdetect=d=0.2:pix_th=0.10",
             "-an", "-sn", "-f", "null", "-"]),
        capture_output=True, text=True, timeout=120, **_LOWPRIO_KW,
    )
    matches = re.findall(r"black_start:(\d+(?:\.\d+)?)\s+black_end:(\d+(?:\.\d+)?)", proc.stderr)
    if not matches:
        proc = subprocess.run(
            _lp([ff, "-ss", str(int(start_at_sec)), "-t", str(int(scan_duration_sec)),
                 "-i", file_path,
                 "-vf", "scale=64:-2,fps=4,blackdetect=d=0.4:pix_th=0.10",
                 "-an", "-sn", "-f", "null", "-"]),
            capture_output=True, text=True, timeout=120, **_LOWPRIO_KW,
        )
    # ffmpeg prints lines like:  [blackdetect @ 0x..] black_start:120.5 black_end:122.0 black_duration:1.5
    matches = re.findall(r"black_start:(\d+(?:\.\d+)?)\s+black_end:(\d+(?:\.\d+)?)", proc.stderr)
    if not matches:
        return None
    # First substantial black segment past start_at_sec is our credits start
    for m in matches:
        bs = float(m[0]) + start_at_sec
        return bs
    return None


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


async def analyze_series(items: list[dict], progress_cb=None,
                         templates: Optional[list[dict]] = None) -> dict:
    """Analyze a series of items, returning per-file intro/credits ranges.

    Input: a list of library items belonging to the same series, each with a
    "files" list of dicts containing "path".

    Output: a dict keyed by absolute file path:
        { path: {"intro": {"start": s, "end": s}, "credits_start": s,
                 "analysis": {"version": N, "source": "auto"}} }

    Only paths that exist on disk and have at least one peer episode in the
    same series are analyzed. Movies (1 file, no peers) get the 92% credits
    heuristic only.

    `templates` selects the intro-detection strategy:
      - None  → **Automatic** mode (current behaviour): greedy-cluster the
        episode heads to discover the shared intro.
      - list  → **Manual / template** mode: each template is an admin-marked
        intro window `{name, source_path, start, end}`; we fingerprint that span
        and align it into every episode's head. Credits detection is identical
        in both modes (outro clustering → blackframe → 92 % fallback), so manual
        mode keeps automatic credit skipping. A template-applied intro carries
        `analysis.source == "template"` and a `template` name.

    progress_cb is an optional async callable invoked with kwargs:
        stage:    "fingerprinting" | "matching-intros" | "matching-outros" | "finalizing"
        current:  int (1-based item being processed)
        total:    int (total items in this stage)
        message:  short human-readable string
        episode_name: optional basename of file being processed
    """
    manual_mode = templates is not None
    async def _emit(**kw):
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
        # No peers — return fallback credits only. A duration-less or too-short
        # file is recorded as a per-file failure (no shared intro can be found,
        # and the 92 % credits heuristic only makes sense for full-length
        # content).
        for idx, ep in enumerate(episodes, start=1):
            await _emit(stage="finalizing", current=idx, total=len(episodes),
                        message="No peers — applying credits fallback",
                        episode_name=Path(ep["path"]).name)
            dur = await asyncio.to_thread(_media_duration, ep["path"])
            if dur and dur > 60:
                result[ep["path"]] = {
                    "intro": None,
                    "credits_start": round(dur * CREDITS_FALLBACK_PCT, 1),
                    "analysis": {"version": ANALYZER_VERSION, "source": "auto-fallback"},
                }
            elif dur:
                result[ep["path"]] = _failed_entry(
                    ERR_TOO_SHORT,
                    f"File duration ({dur:.0f}s) is below the 60 s minimum for credits fallback.",
                )
            else:
                result[ep["path"]] = _failed_entry(
                    ERR_NO_DURATION,
                    "ffprobe could not determine the media duration — the container may be "
                    "unsupported or the file may be partially written.",
                )
        return result

    # Compute fingerprints for the head and tail of each episode in parallel chunks.
    # Track per-episode failures so files where fpcalc returned nothing are
    # recorded as failures instead of silently disappearing from the result.
    head_fps: list[list[int]] = []
    tail_fps: list[list[int]] = []
    durations: list[Optional[float]] = []
    fp_errors: dict[int, tuple[str, str]] = {}   # ep idx → (code, message)
    total_eps = len(episodes)

    for idx, ep in enumerate(episodes, start=1):
        ep_name = Path(ep["path"]).name
        await _emit(stage="fingerprinting", current=idx, total=total_eps,
                    message=f"Fingerprinting episode {idx} of {total_eps}",
                    episode_name=ep_name)
        dur = await asyncio.to_thread(_media_duration, ep["path"])
        durations.append(dur)
        head = await asyncio.to_thread(_fpcalc_raw, ep["path"], INTRO_SEARCH_SECS, 0)
        head_fps.append(head)
        if not head:
            fp_errors[idx - 1] = (
                ERR_FP_EMPTY,
                "fpcalc produced no fingerprint for the head of this file "
                "(unsupported audio codec, silent track, or corrupted container).",
            )

        if dur and dur > OUTRO_SEARCH_SECS + 60:
            tail_start = int(dur - OUTRO_SEARCH_SECS)
            tail = await asyncio.to_thread(_fpcalc_raw, ep["path"], OUTRO_SEARCH_SECS, tail_start)
        else:
            tail = []
        tail_fps.append(tail)

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
    intro_by_ep: dict[int, tuple[int, int]] = {}          # auto mode (frames)
    manual_intro_by_ep: dict[int, tuple[float, float, str]] = {}  # manual (secs)
    try:
        if manual_mode:
            # Template extrapolation: fingerprint each marked window once, then
            # align it into every episode head. No intro clustering.
            template_fps = await asyncio.to_thread(_fingerprint_templates, templates)
            manual_intro_by_ep = await _match_templates_to_heads(
                template_fps, head_fps, _emit, executor, episodes,
            )
        else:
            max_intro_frames = int(MAX_INTRO_SEC * FP_FRAMES_PER_SEC)
            intro_clusters = await _build_clusters_async(
                head_fps, MIN_MATCH_FRAMES, max_intro_frames,
                episodes, _emit, "matching-intros", executor,
            )
            intro_by_ep = _build_ep_offset_map(intro_clusters)

        # Credits detection is shared by both modes.
        max_outro_frames = int(MAX_OUTRO_SEC * FP_FRAMES_PER_SEC)
        outro_clusters = await _build_clusters_async(
            tail_fps, MIN_MATCH_FRAMES, max_outro_frames,
            episodes, _emit, "matching-outros", executor,
        )
        outro_by_ep = _build_ep_offset_map(outro_clusters)
    finally:
        if executor is not None:
            executor.shutdown(wait=False)

    # ── Build per-episode result ─────────────────────────────────────────────
    # `result` already carries per-path failure entries for missing files. Now
    # add per-episode entries — success for files that produced intro/credits,
    # failure for files that produced nothing usable.
    for idx, ep in enumerate(episodes):
        path = ep["path"]
        dur = durations[idx]
        ep_name = Path(path).name

        await _emit(stage="finalizing", current=idx + 1, total=total_eps,
                    message=f"Finalizing episode {idx + 1} of {total_eps}",
                    episode_name=ep_name)

        intro: Optional[dict] = None
        intro_template: Optional[str] = None   # set in manual mode when a template matched
        if manual_mode:
            mi = manual_intro_by_ep.get(idx)
            if mi:
                start_s, end_s, tmpl_name = mi
                intro = {"start": start_s, "end": end_s}
                intro_template = tmpl_name
        elif idx in intro_by_ep:
            s_fr, l_fr = intro_by_ep[idx]
            intro = {
                "start": round(frames_to_seconds(s_fr), 1),
                "end":   round(frames_to_seconds(s_fr + l_fr), 1),
            }

        credits_start: Optional[float] = None
        source = "auto-fallback"
        if idx in outro_by_ep and dur:
            offset_fr, _ = outro_by_ep[idx]
            tail_start = dur - OUTRO_SEARCH_SECS
            credits_start = round(tail_start + frames_to_seconds(offset_fr), 1)
            source = "auto"
        elif dur and dur > 60:
            # Blackdetect fallback — scan only the last 5 minutes to keep this snappy
            search_at = max(dur * 0.85, dur - 300)
            black = await asyncio.to_thread(_detect_blackframe, path, search_at, 300.0)
            if black and black < dur - 5:
                credits_start = round(black, 1)
                source = "auto-blackframe"
            else:
                credits_start = round(dur * CREDITS_FALLBACK_PCT, 1)
                source = "auto-fallback"

        if intro or credits_start is not None:
            if intro and intro_template is not None:
                intro_source = "template"
            elif intro:
                intro_source = "auto"
            else:
                intro_source = source
            analysis = {"version": ANALYZER_VERSION, "source": intro_source}
            if intro_template is not None:
                analysis["template"] = intro_template
            result[path] = {
                "intro": intro,
                "credits_start": credits_start,
                "analysis": analysis,
            }
        else:
            # Nothing usable — record why so the admin can see it and the user
            # gets the "Skip unavailable" chip. fpcalc emptiness is the most
            # specific cause; missing duration is the next; otherwise we hit
            # the matcher with usable input but nothing aligned and no black
            # frame was found.
            if idx in fp_errors:
                code, msg = fp_errors[idx]
            elif not dur:
                code, msg = ERR_NO_DURATION, (
                    "ffprobe could not determine the media duration — no credits "
                    "fallback could be applied."
                )
            else:
                code, msg = ERR_NO_SKIP, (
                    "Fingerprinting completed but no shared intro and no credits "
                    "transition could be located in this file."
                )
            result[path] = _failed_entry(code, msg)

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
