"""Boundary refinement for Smart Skip — chapters and silence.

The audio fingerprint gives a COARSE boundary: it finds where episodes stop sharing
audio, which is close to, but not the same as, where the intro ends. This module pins
that boundary to something physical — a chapter marker the ripper wrote, or the silence
between the theme and the first line of dialogue.

Leaf module: stdlib + ffmpeg/ffprobe via `mediabin`. Imports nothing else in this repo
(`analyzer` imports THIS, not the other way round).

Two rules hold everywhere in here, and both exist because the alternative silently
produces garbage rather than an error:

1. **A refinement may move a boundary, never invent one.** Every snap is bounded by a
   radius, and `snap_boundary` returns the coarse value untouched when nothing qualifies.
2. **These ffmpeg filters log at AV_LOG_INFO.** Copying the `-loglevel error` idiom used
   elsewhere for media calls yields zero candidates with rc=0 — refinement becomes a
   silent no-op with nothing in any log. See docs/GOTCHAS.md.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from math import exp
from typing import Optional

from mediabin import ffmpeg_bin, ffprobe_bin, run_capture

# ── Detection tunables ───────────────────────────────────────────────────────

# silencedetect defaults (-60 dB, 2.0 s) are far too strict for a theme-to-dialogue
# transition. Measured on real episodes, the gap after an OP is 1-5 s at around -40 dB.
SILENCE_DB      = -40.0
SILENCE_MIN_SEC = 0.20
# Retried once at this looser threshold when the strict pass finds nothing in range.
SILENCE_DB_LOOSE      = -30.0
SILENCE_MIN_SEC_LOOSE = 0.12

# Chapter plausibility. An intro chapter outside this window is not a title sequence.
# This is NOT hypothetical: a film in the test library has a DVD scene list whose first
# chapter is literally titled "Opening" and runs 0 -> 631.92 s. Without this guard the
# chapter path would skip the first 10.5 minutes of the movie.
CHAPTER_INTRO_MIN_SEC = 15.0
CHAPTER_INTRO_MAX_SEC = 180.0
CHAPTER_INTRO_LATEST  = 360.0
# A chapter that disagrees with a confirmed audio match by more than this is not
# describing the same thing. The audio wins; the chapter is discarded with a warning.
CHAPTER_VETO_SEC = 20.0

_CHAPTER_INTRO_RE   = re.compile(r"\b(intro|opening|avant|title sequence)\b|^\s*op\s*$", re.I)
_CHAPTER_CREDITS_RE = re.compile(r"\b(ending|credits|end credits|closing|outro)\b|^\s*ed\s*$", re.I)

# Anchor every parse on the filter's own log prefix. ffmpeg echoes the input path at
# info level, so a filename containing "silence_start:" would otherwise poison the parse.
#
# The prefix is "[Parsed_silencedetect_0 @ 0x...]", NOT "[silencedetect @ ...]" — a
# filter instantiated inside a filtergraph is logged by its PARSED name and index. An
# anchor on the bare filter name matches nothing, and because a no-match is
# indistinguishable from "no silence found", refinement degrades to a silent no-op with
# rc=0 and nothing in any log. Verified against real ffmpeg output. See docs/GOTCHAS.md.
_SIL_TAG = r"\[Parsed_silencedetect_\d+ @[^\]]*\]"
_RE_SIL_START = re.compile(_SIL_TAG + r"\s*silence_start:\s*(-?[\d.]+)")
_RE_SIL_END   = re.compile(_SIL_TAG + r"\s*silence_end:\s*(-?[\d.]+)"
                           r"\s*\|\s*silence_duration:\s*([\d.]+)")


@dataclass(frozen=True)
class Cut:
    """One candidate boundary, in ABSOLUTE seconds from the start of the file."""
    t: float
    kind: str        # chapter | silence_start | silence_end
    strength: float  # 0..1, how good this individual candidate is


# Prior confidence per source. A chapter marker was written by a human (or by the
# authoring tool from the master), so it outranks anything inferred from the signal.
_SRC_WEIGHT = {
    "chapter":       1.00,
    "silence_end":   0.75,
    "silence_start": 0.75,
}


# ── Chapters ─────────────────────────────────────────────────────────────────

def probe_chapters(path: str, timeout: int = 30) -> list[dict]:
    """Return [{start, end, title}] for a file, or [] when it has no chapters.

    Chapter titles in this library are routinely Japanese, so the UTF-8 decode in
    `run_capture` is load-bearing — the ANSI-code-page failure returns empty output with
    rc=0 and would read as "no chapters".
    """
    ffprobe = ffprobe_bin()
    if not ffprobe:
        return []
    try:
        proc = run_capture([
            ffprobe, "-v", "error", "-print_format", "json",
            "-show_entries", "chapter=start_time,end_time:chapter_tags=title",
            path,
        ], timeout=timeout)
        if proc.returncode != 0:
            return []
        data = json.loads(proc.stdout or "{}")
    except Exception:
        return []
    out: list[dict] = []
    for ch in data.get("chapters") or []:
        try:
            start = float(ch.get("start_time"))
            end = float(ch.get("end_time"))
        except (TypeError, ValueError):
            continue
        title = ((ch.get("tags") or {}).get("title") or "").strip()
        out.append({"start": start, "end": end, "title": title})
    return out


def chapter_intro(chapters: list[dict]) -> Optional[dict]:
    """A named intro chapter, if one is present AND plausible as a title sequence."""
    for ch in chapters:
        if not _CHAPTER_INTRO_RE.search(ch["title"]):
            continue
        dur = ch["end"] - ch["start"]
        if (CHAPTER_INTRO_MIN_SEC <= dur <= CHAPTER_INTRO_MAX_SEC
                and ch["start"] < CHAPTER_INTRO_LATEST):
            return {"start": round(ch["start"], 1), "end": round(ch["end"], 1)}
    return None


def chapter_credits(chapters: list[dict], duration: float,
                    min_pct: float, end_margin: float) -> Optional[float]:
    """A named credits chapter, gated by the SAME correctness tests a fingerprint match
    must pass: it has to start late enough and run to near the end of the file.

    A mislabelled chapter must not get a shortcut past the checks — credits are never
    fabricated in this system (docs/GOTCHAS.md).
    """
    if not duration:
        return None
    for ch in chapters:
        if not _CHAPTER_CREDITS_RE.search(ch["title"]):
            continue
        if ch["start"] >= duration * min_pct and ch["end"] >= duration - end_margin:
            return round(ch["start"], 1)
    return None


def chapter_cuts(chapters: list[dict]) -> list[Cut]:
    """Every chapter edge as a snap candidate — including generic "Chapter 4" ones.

    Generic chapters are useless as a SOURCE (they say nothing about what they contain)
    but rippers very often place one exactly on the opening boundary, which makes them
    excellent corroboration for a silence candidate.
    """
    cuts: list[Cut] = []
    for ch in chapters:
        cuts.append(Cut(ch["start"], "chapter", 1.0))
        cuts.append(Cut(ch["end"], "chapter", 1.0))
    return cuts


# ── Silence ──────────────────────────────────────────────────────────────────

def detect_silence(path: str, win_start: float, win_len: float,
                   noise_db: float = SILENCE_DB, min_sec: float = SILENCE_MIN_SEC,
                   timeout: int = 90) -> list[tuple[float, float, float]]:
    """Silence runs as (start, end, duration) in ABSOLUTE file seconds.

    `-ss` before `-i` is a fast seek but it RESETS the filter-graph timeline to zero, so
    every reported time is relative to the seek point and `win_start` has to be added
    back. Audio-only decode of a short window is milliseconds.
    """
    ff = ffmpeg_bin()
    if not ff or win_len <= 0:
        return []
    win_start = max(0.0, win_start)
    try:
        proc = run_capture([
            ff, "-hide_banner", "-nostats",
            "-loglevel", "info",          # MANDATORY: silencedetect logs at INFO (GOTCHAS)
            "-ss", f"{win_start:.3f}", "-t", f"{win_len:.3f}",
            "-i", path,
            "-map", "0:a:0?",             # trailing ? so a video-only file doesn't error
            "-vn", "-sn", "-dn",
            "-af", f"silencedetect=noise={noise_db}dB:duration={min_sec}",
            "-f", "null", "-",
        ], timeout=timeout)
    except Exception:
        return []

    runs: list[tuple[float, float, float]] = []
    pending: Optional[float] = None
    for line in (proc.stderr or "").splitlines():
        m = _RE_SIL_START.search(line)
        if m:
            pending = win_start + float(m.group(1))
            continue
        m = _RE_SIL_END.search(line)
        if m and pending is not None:
            end = win_start + float(m.group(1))
            runs.append((pending, end, float(m.group(2))))
            pending = None
    if pending is not None:
        # A silence still open at the window edge emits no end line. Synthesise one
        # rather than dropping the run — it's usually the most interesting gap.
        runs.append((pending, win_start + win_len, win_start + win_len - pending))
    return runs


# A theme rarely hands straight over to dialogue. Measured on real episodes the shape is
# `theme -> silence -> brief stinger -> silence -> content`, and the two silences are
# separated by well under a second of sound. Treating them as separate runs makes the
# nearest-candidate rule pick the FIRST silence's start, which parks the viewer in six
# seconds of dead air. Merge runs separated by less than this much sound.
SILENCE_MERGE_GAP = 1.2
# ...but only while the merged span stays mostly silent, so a merge can't walk forward
# through real content that happens to be punctuated by pauses.
SILENCE_MERGE_MIN_RATIO = 0.6
# ...and the run being merged in must be a real pause, not a breath between words.
SILENCE_MERGE_MIN_NEXT = 0.5


def merge_silence(runs: list[tuple[float, float, float]],
                  max_gap: float = SILENCE_MERGE_GAP) -> list[tuple[float, float, float]]:
    """Coalesce silence runs separated by a brief burst of sound.

    The end of a merged run is where content actually resumes — which is the boundary an
    intro skip wants to land on. Bounded by SILENCE_MERGE_MIN_RATIO so the merge stops
    at the first genuinely-noisy stretch instead of chaining through the episode.
    """
    if not runs:
        return []
    out: list[tuple[float, float, float]] = []
    cur_s, cur_e, cur_sil = runs[0]
    for s, e, d in runs[1:]:
        gap = s - cur_e
        span = e - cur_s
        # The next run must itself be a real pause. Without this the merge chains through
        # the 0.2-0.3 s breaths in quiet dialogue and walks several seconds into the
        # episode — measured on a real file, a merge ran to 91.65 s on a boundary whose
        # truth was 86.4 s.
        if (gap <= max_gap and d >= SILENCE_MERGE_MIN_NEXT and span > 0
                and (cur_sil + d) / span >= SILENCE_MERGE_MIN_RATIO):
            cur_e = e
            cur_sil += d
        else:
            out.append((cur_s, cur_e, cur_sil))
            cur_s, cur_e, cur_sil = s, e, d
    out.append((cur_s, cur_e, cur_sil))
    return out


def silence_cuts(runs: list[tuple[float, float, float]]) -> list[Cut]:
    """Both edges of every silence run, weighted by how long the silence is.

    Emit both edges rather than a midpoint: a silence START is where the outgoing audio
    stopped, a silence END is where the incoming audio begins. Those are different
    boundaries and different consumers want different ones — an intro end wants the END
    (land on the first real sound), a credits start wants the START.
    """
    cuts: list[Cut] = []
    for s, e, d in runs:
        strength = min(1.0, d / 0.6)
        cuts.append(Cut(s, "silence_start", strength))
        cuts.append(Cut(e, "silence_end", strength))
    return cuts


# ── The snapper ──────────────────────────────────────────────────────────────

def snap_boundary(coarse: float, cuts: list[Cut], *, radius: float,
                  bias: str = "none", prefer: tuple[str, ...] = (),
                  allow: tuple[str, ...] = (),
                  min_conf: float = 0.0,
                  floor: Optional[float] = None,
                  ceil: Optional[float] = None) -> tuple[float, float, str]:
    """Move `coarse` onto the best nearby physical cut.

    Returns (time, confidence, source). When nothing qualifies, returns
    `(coarse, 0.0, "audio")` — never a guess, and never a value outside
    [coarse - radius, coarse + radius].

    `bias` is a soft 30% distance discount in the named direction, NOT a one-sided
    window. The sign of the residual audio error is not reliably known and it has already
    flipped once: before the frame-rate fix the fingerprint over-shot the intro end, and
    after it the fingerprint under-shoots. A hard one-sided window would have been wrong
    in one of those two regimes.
    """
    if not cuts or radius <= 0:
        return (coarse, 0.0, "audio")

    scored: list[tuple[float, Cut]] = []
    for c in cuts:
        # `allow` is a hard kind filter, not a preference. Some kinds are simply the
        # wrong ANSWER for a given boundary however close they sit: an intro end must
        # never land on a silence_start, because that is the moment the theme stopped,
        # not the moment content resumes — snapping there parks the viewer in dead air.
        if allow and c.kind not in allow:
            continue
        dt = c.t - coarse
        if floor is not None and c.t < floor:
            continue
        if ceil is not None and c.t > ceil:
            continue
        aligned = (bias == "fwd" and dt > 0) or (bias == "back" and dt < 0)
        eff = abs(dt) * (0.7 if aligned else 1.0)
        if eff > radius:
            continue
        w = _SRC_WEIGHT.get(c.kind, 0.5)
        # Distance decays GENTLY — down to 0.5 at the radius edge, never to zero.
        # A Gaussian here was a real bug: the whole point is that the coarse boundary is
        # several seconds off, so the correct candidate is usually FAR from it. Measured
        # on Death Note, the true intro end sits 6.75 s past the fingerprint, and a
        # Gaussian scored it 0.19 — rejecting the right answer for being right.
        dist = 1.0 - 0.5 * min(1.0, eff / radius)
        score = w * (0.55 + 0.45 * c.strength) * dist
        scored.append((score, c))

    if not scored:
        return (coarse, 0.0, "audio")

    def rank(item):
        score, c = item
        pref = prefer.index(c.kind) if c.kind in prefer else len(prefer)
        return (-score, pref, abs(c.t - coarse), c.t)

    scored.sort(key=rank)
    best_score, best = scored[0]
    source = best.kind

    # Agreement bonus: two DIFFERENT kinds of evidence landing on the same instant is the
    # strongest signal available — a chapter edge that coincides with a silence is not a
    # coincidence. Record it in the provenance so a wrong snap stays diagnosable.
    for score, c in scored[1:]:
        if c.kind != best.kind and abs(c.t - best.t) <= 0.35:
            best_score = min(1.0, best_score + 0.15)
            source = f"{best.kind}+{c.kind}"
            break

    if best_score < min_conf:
        return (coarse, 0.0, "audio")
    return (round(best.t, 2), round(best_score, 3), source)
