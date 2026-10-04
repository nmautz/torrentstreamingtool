"""Subtitle ↔ speech alignment: is this subtitle for this video, and is it in
sync? If it's off by a constant or runs at the wrong speed, by how much?

Leaf module: stdlib + numpy + the media binaries via `mediabin`. No `main`
import. numpy is optional at import time (like analyzer.py's matcher): without
it `align` returns None and the caller treats the subtitle as unverified.

HOW
1. speech_energy(): ffmpeg band-passes the audio to the voice range (300-3400 Hz)
   and reports the RMS level of every 100 ms window (`astats`). All the DSP runs
   in ffmpeg; Python only parses one number per window.
2. The energy is detrended (±2 s moving average removed) so a steady music bed
   doesn't read as speech, then standardised.
3. For each playback-speed ratio (1.0, PAL↔film, 24↔23.976, …) the subtitle's
   on-screen spans become a 0/1 mask, and FFT cross-correlation scores EVERY
   offset in ±120 s at once: mean speech energy under the lines.
4. Confidence = how many standard deviations the best offset stands above all
   the others. Right episode: one sharp peak. Wrong episode: none.

MEASURED (tests/subs_eval/, 290 downloaded candidates graded against embedded
tracks): with ACCEPT=5 and MOVE=1.0, alignment took in-sync candidates from
103 to 156 and broke 1 good one. alass, run unconditionally on the same set,
reached 132 and broke 25 — and it moved some episodes' own embedded tracks by
15-45 s, so it was not adopted.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Optional, Sequence

try:
    import numpy as np
except Exception:          # pragma: no cover - optional dependency
    np = None

import mediabin

STEP = 0.1                 # seconds per energy window
SEARCH = 120.0             # ± offset searched
DETREND = 20               # ± windows of moving average removed (2 s)
SPEED_PRIOR = 1.25         # a non-1.0 speed must beat 1.0's fit by this factor
ACCEPT = 5.0               # confidence at/above which a subtitle counts as verified
MOVE = 1.0                 # only move a verified subtitle when off by >= this (s)
RATIOS = (1.0, 25 / 23.976, 23.976 / 25, 24 / 23.976, 23.976 / 24,
          25 / 24, 24 / 25, 30 / 29.97, 29.97 / 30)


def available() -> bool:
    return np is not None and bool(mediabin.ffmpeg_bin())


def speech_energy(src: str | Path, audio_index: Optional[int] = None,
                  timeout: int = 900) -> list[float]:
    """Voice-band loudness (dBFS) of every 100 ms window of `src`'s audio.
    `audio_index` picks the Nth audio stream (the original-language one — a dub
    speaks at different moments than the subtitles were timed to)."""
    ffmpeg = mediabin.ffmpeg_bin()
    if not ffmpeg:
        return []
    af = ("aresample=16000,pan=mono|c0=0.5*c0+0.5*c1,highpass=f=300,lowpass=f=3400,"
          "asetnsamples=n=1600:p=0,astats=metadata=1:reset=1,"
          "ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-")
    cmd = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(src)]
    if audio_index is not None:
        cmd += ["-map", f"0:a:{int(audio_index)}"]
    cmd += ["-vn", "-sn", "-dn", "-af", af, "-f", "null", "-"]
    try:
        cp = mediabin.run_capture(cmd, timeout=timeout)
        out = cp.stdout or ""
    except Exception:
        return []
    vals = []
    for m in re.finditer(r"RMS_level=(-?[\d.]+|-inf)", out):
        v = m.group(1)
        vals.append(-120.0 if v == "-inf" else max(-120.0, float(v)))
    return vals


def _prep(energy: Sequence[float]):
    x = np.asarray(energy, dtype=np.float64)
    x = np.maximum(x, np.percentile(x, 5))
    if DETREND:
        k = np.ones(2 * DETREND + 1) / (2 * DETREND + 1)
        x = x - np.convolve(np.pad(x, DETREND, mode="edge"), k, mode="valid")
    sd = x.std() or 1.0
    return (x - x.mean()) / sd


def _mask(cues, ratio: float, n: int):
    m = np.zeros(n, dtype=np.float64)
    for a, b in cues:
        i, j = int(a * ratio / STEP), int(b * ratio / STEP)
        if j < 0 or i >= n:
            continue
        m[max(i, 0):min(j, n - 1) + 1] = 1.0
    return m


def _xcorr(x, m, k: int):
    """score[s] = Σ x[i+s]·m[i] for s in [-k, k], via FFT."""
    n = len(x)
    size = 1 << int(np.ceil(np.log2(2 * n + 1)))
    c = np.fft.irfft(np.fft.rfft(x, size) * np.conj(np.fft.rfft(m, size)), size)
    return np.concatenate([c[size - k:], c[:k + 1]])


def align(cues: Sequence[Sequence], energy: Sequence[float]) -> Optional[dict]:
    """Best (ratio, offset) mapping subtitle time -> video time, with confidence.

    `cues`: [(start, end, ...)] in seconds. Returns
    {ratio, offset, conf, verified, move} or None when there's too little to go
    on (numpy missing, <60 s of audio, <20 lines)."""
    if np is None or len(energy) < 600:
        return None
    spans = sorted((float(c[0]), float(c[1])) for c in cues)
    if len(spans) < 20:
        return None
    x = _prep(energy)
    n = len(x)
    k = int(SEARCH / STEP)
    res = []
    for r in RATIOS:
        m = _mask(spans, r, n + k)[:n]
        cnt = m.sum()
        if cnt < 50:
            continue
        sc = _xcorr(x, m, k) / cnt
        i = int(np.argmax(sc))
        sd = float(sc.std()) or 1e-9
        res.append({"ratio": r, "offset": round((i - k) * STEP, 2),
                    "conf": round(float((sc[i] - sc.mean()) / sd), 2), "fit": float(sc[i])})
    if not res:
        return None
    best = max(res, key=lambda d: d["fit"])
    one = next((d for d in res if d["ratio"] == 1.0), None)
    if one and best is not one and best["fit"] < one["fit"] * SPEED_PRIOR:
        best = one
    verified = best["conf"] >= ACCEPT
    move = verified and (best["ratio"] != 1.0 or abs(best["offset"]) >= MOVE)
    return {"ratio": best["ratio"], "offset": best["offset"], "conf": best["conf"],
            "verified": verified, "move": move}


def mapper(result: Optional[dict]):
    """t -> corrected t for an align() result (identity unless it says move)."""
    if not result or not result.get("move"):
        return lambda t: t
    r, o = result["ratio"], result["offset"]
    return lambda t: t * r + o
