"""Prototype: align a subtitle to a video's speech, and say how sure we are.

speech_energy(audio)  -> per-100 ms voice-band loudness (dB) via ffmpeg astats
align(cues, energy)   -> {ratio, offset, conf, fit}

Fit of a candidate (ratio r, offset o): mean (standardised, detrended) energy
under the subtitle's lines — every offset at once via FFT cross-correlation.
Confidence: how far the best offset stands above all the others, in standard
deviations. A wrong episode has no single offset where lines and speech agree.
"""
import re, subprocess
import numpy as np

STEP = 0.1
RATIOS = [1.0, 25 / 23.976, 23.976 / 25, 24 / 23.976, 23.976 / 24, 25 / 24, 24 / 25, 30 / 29.97, 29.97 / 30]


def speech_energy(src, ffmpeg="ffmpeg", stream=None):
    af = ("aresample=16000,pan=mono|c0=0.5*c0+0.5*c1,highpass=f=300,lowpass=f=3400,"
          "asetnsamples=n=1600:p=0,astats=metadata=1:reset=1,"
          "ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-")
    cmd = [ffmpeg, "-nostdin", "-loglevel", "error", "-i", str(src)]
    if stream is not None:
        cmd += ["-map", f"0:a:{stream}"]
    cmd += ["-vn", "-af", af, "-f", "null", "-"]
    out = subprocess.run(cmd, capture_output=True, text=True).stdout
    vals = []
    for m in re.finditer(r"RMS_level=(-?[\d.]+|-inf)", out):
        v = m.group(1)
        vals.append(-120.0 if v == "-inf" else max(-120.0, float(v)))
    return vals


def prep_energy(e, detrend=50):
    """Standardise, and subtract a moving average (±detrend bins) so a steady
    music bed doesn't read as 'someone is talking'."""
    x = np.asarray(e, dtype=np.float64)
    x = np.maximum(x, np.percentile(x, 5))                 # clamp digital silence
    if detrend:
        k = np.ones(2 * detrend + 1) / (2 * detrend + 1)
        pad = np.pad(x, detrend, mode="edge")
        x = x - np.convolve(pad, k, mode="valid")
    sd = x.std() or 1.0
    return (x - x.mean()) / sd


def _mask(cues, ratio, n):
    m = np.zeros(n, dtype=np.float64)
    for a, b in cues:
        i, j = int(a * ratio / STEP), int(b * ratio / STEP)
        if j < 0 or i >= n:
            continue
        m[max(i, 0):min(j, n - 1) + 1] = 1.0
    return m


def _xcorr(x, m, k):
    """score[s] = sum_i x[i + s] * m[i] for s in [-k, k]."""
    n = len(x)
    size = 1 << int(np.ceil(np.log2(2 * n + 1)))
    X = np.fft.rfft(x, size); M = np.fft.rfft(m, size)
    c = np.fft.irfft(X * np.conj(M), size)
    # c[s] for s >= 0 at index s, negative s at size + s
    return np.concatenate([c[size - k:], c[:k + 1]])


def align(cues, energy, search=120.0, ratios=RATIOS, detrend=50, prior=1.1):
    cues = sorted((c[0], c[1]) for c in cues)
    if len(energy) < 600 or len(cues) < 20:
        return None
    x = prep_energy(energy, detrend)
    n = len(x)
    k = int(search / STEP)
    res = []
    for r in ratios:
        m = _mask(cues, r, n + k)[:n]
        cnt = m.sum()
        if cnt < 50:
            continue
        sc = _xcorr(x, m, k) / cnt
        i = int(np.argmax(sc))
        mu, sd = sc.mean(), sc.std() or 1e-9
        res.append({"ratio": r, "offset": round((i - k) * STEP, 2), "conf": round(float((sc[i] - mu) / sd), 2),
                    "fit": round(float(sc[i]), 4)})
    if not res:
        return None
    best = max(res, key=lambda d: d["fit"])
    one = next((d for d in res if d["ratio"] == 1.0), None)
    if prior and one and best is not one and best["fit"] < one["fit"] * prior:
        best = one                        # a speed change must clearly win
    return best


def refine_chunks(cues, energy, ratio, offset, chunk=300.0, search=6.0, detrend=50):
    """Per-chunk offsets around the global fit — absorbs cut differences and
    residual drift. Returns [(chunk_start_in_sub_time, offset)]."""
    cues = sorted((c[0], c[1]) for c in cues)
    x = prep_energy(energy, detrend)
    n = len(x); k = int(search / STEP)
    out = []
    t0, end = cues[0][0], cues[-1][1]
    while t0 < end:
        part = [c for c in cues if t0 <= c[0] < t0 + chunk]
        if len(part) >= 8:
            m = _mask([(a * ratio + offset, b * ratio + offset) for a, b in part], 1.0, n)
            if m.sum() > 20:
                sc = _xcorr(x, m, k) / m.sum()
                i = int(np.argmax(sc))
                z = float((sc[i] - sc.mean()) / (sc.std() or 1e-9))
                out.append((t0, round(offset + (i - k) * STEP, 2), round(z, 2)))
        t0 += chunk
    return out


def gate_chunks(chunks, offset, zmin=3.0, dmin=0.5):
    """Keep a chunk's own offset only where its local fit is confident and it
    actually differs; otherwise it inherits the global offset."""
    return [(t, o if (z >= zmin and abs(o - offset) >= dmin) else offset) for t, o, z in chunks]


def apply(cues, ratio, offset, chunks=None):
    res = []
    for c in cues:
        a, b = c[0], c[1]
        o = offset
        if chunks:
            cand = [co for ct, co, *_ in chunks if ct <= a]
            o = cand[-1] if cand else chunks[0][1]
        res.append((a * ratio + o, b * ratio + o) + tuple(c[2:]))
    return res
