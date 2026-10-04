"""Structural integrity check for a prepped HLS bundle.

WHAT THIS CATCHES
-----------------
A bundle encoded from a source that was not all there yet. qBittorrent writes
pieces into a sparse file, so a half-fetched episode is a full-length file with
holes in it. ffmpeg reads straight through those holes without erroring: the
video filter chain, running CFR, duplicates the last good frame to fill the
timestamp gap, and `aresample=async=1` pads the audio with digital silence. The
encode exits 0 and the bundle *looks* built.

What lands on disk is a picture frozen on one frame over silence, for as long as
the hole lasted. That is the "counter runs, frame is stuck" failure — and because
`_offline_cache_key` is `version|name|size` and a sparse file already reports its
FINAL size, the wreck is stamped with exactly the key the finished file resolves
to, so nothing ever rebuilds it.

Measured on the real case that prompted this (Hacks S03E03, prepped seven minutes
before its torrent finished) the signature is unmistakable:

    video, healthy 10.4 s segment   3.1 – 8.1 MB
    video, frozen  10.4 s segment   440,977 B — and byte-identical, segment after
                                    segment, because it is the same held frame
    audio, healthy  6.0 s segment   120 – 135 KB
    audio, silent   6.0 s segment   ~3.0 KB (6-byte AAC frames = digital silence)

So the check is pure arithmetic on segment sizes — no ffmpeg, no decode, a few
hundred `stat` calls per bundle. See docs/STREAMING.md § Bundle integrity.

WHAT IT DELIBERATELY DOES NOT CATCH
-----------------------------------
Subtle corruption: a few bad macroblocks, a brief glitch, drifting A/V sync.
Those need a real decode and are the source validator's job
(`_validate_one_file` in main.py). This module answers one question cheaply and
without false alarms: *is a long stretch of this bundle dead?*

Leaf module — stdlib only, no `main` import. Tests in tests/test_bundlecheck.py.
"""

from __future__ import annotations

from typing import Iterable, NamedTuple, Optional


# ── Thresholds ───────────────────────────────────────────────────────────────
#
# Every one of these is set to make a FALSE POSITIVE much more expensive than a
# miss. Flagging a good bundle costs a pointless re-encode of a whole episode
# and, if it kept happening, a file that can never be prepped; missing a bad one
# costs what we have today, which the background sweep will find on its next
# pass anyway.

# A segment is "dead" when it is smaller than this fraction of its rendition's
# reference size. The real gap is 7-40x (0.03-0.14); 0.20 sits far below any
# healthy segment measured and far above any dead one.
DEAD_FRACTION = 0.20

# The reference "healthy size" is this percentile of the rendition's own segment
# sizes. NOT the median: on the case that prompted this, 48% of the bundle was
# dead, which drags a median down into the wreckage and hides it. p90 survives a
# bundle that is mostly damaged, and being per-rendition makes the test
# scale-free — a legitimately static video (a still image over music) has a
# uniformly small rung, so its p90 is small too and nothing trips.
REF_PERCENTILE = 0.90

# A dead run must last this long to count. Real content genuinely goes black and
# silent — a fade, a beat before a cold open — but not for a quarter of a minute.
MIN_RUN_SECS = 15.0

# ...and the bundle is only called damaged once the dead runs total this much.
MIN_TOTAL_SECS = 20.0

# A frozen picture ALONE (no matching dead audio) has to last this long before it
# counts. A held production card over music at the end of an episode is real, and
# by size it is indistinguishable from a held frame; a full minute of one is not.
# Every window of the case that prompted this was either longer than this or had
# dead audio under it, so nothing is lost by being strict here.
FROZEN_ALONE_SECS = 60.0

# A run of byte-IDENTICAL segment sizes is the frozen-frame signature: the same
# held picture re-encoded to the same bytes, over and over. VBR content does not
# repeat an exact byte count four times running. This is a second, independent
# way in — it fires even where a rendition is so damaged that the percentile
# reference is itself suspect.
IDENTICAL_RUN = 4

# ...but only when those identical segments are also well under the reference, so
# a genuinely constant-bitrate rendition can't trip it.
IDENTICAL_FRACTION = 0.50

# The last segment of a rendition is routinely a short tail (a 1.2 s remainder
# after the final full segment) and is legitimately tiny. Never judge it.
SKIP_TRAILING = 1


class Segment(NamedTuple):
    """One entry of a media playlist, paired with the size it is on disk.

    `size` is -1 when the file is missing — which is itself damage, and is
    treated as dead.
    """
    name: str
    duration: float
    size: int


class Span(NamedTuple):
    """A contiguous dead stretch of one rendition, in playback seconds."""
    rendition: str
    start: float
    end: float
    reason: str          # "dead" (size collapse) | "frozen" (identical run)

    @property
    def seconds(self) -> float:
        return max(0.0, self.end - self.start)


class Verdict(NamedTuple):
    """The answer for a whole bundle."""
    damaged: bool
    spans: list            # list[Span], worst rendition first
    dead_secs: float       # total damaged playback time (union across renditions)
    total_secs: float      # the bundle's duration, as its playlists describe it
    detail: str            # one human-readable line for a log or the admin card

    def as_dict(self) -> dict:
        """JSON-safe form for `library.json` / the admin API."""
        return {
            "damaged":    self.damaged,
            "dead_secs":  round(self.dead_secs, 1),
            "total_secs": round(self.total_secs, 1),
            "detail":     self.detail,
            "spans": [
                {"rendition": s.rendition, "start": round(s.start, 1),
                 "end": round(s.end, 1), "reason": s.reason}
                for s in self.spans
            ],
        }


def parse_media_playlist(text: str) -> list:
    """`(name, duration)` for every segment in an HLS media playlist.

    Deliberately minimal: `#EXTINF:<secs>,` lines each followed by a URI line.
    Comments and every other tag are skipped, and a URI with no preceding EXTINF
    is ignored (that is a malformed playlist, not a segment).
    """
    out = []
    dur: Optional[float] = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXTINF:"):
            try:
                dur = float(line[len("#EXTINF:"):].split(",")[0])
            except ValueError:
                dur = None
        elif line.startswith("#"):
            continue
        elif dur is not None:
            out.append((line, dur))
            dur = None
    return out


def _percentile(values: list, q: float) -> float:
    """The `q` quantile of `values` by nearest rank. Empty ⇒ 0.0."""
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = int(round(q * (len(ordered) - 1)))
    return float(ordered[max(0, min(len(ordered) - 1, idx))])


def _runs(flags: list) -> list:
    """Contiguous `[start, end]` index runs where `flags[i]` is true."""
    out = []
    for i, on in enumerate(flags):
        if not on:
            continue
        if out and out[-1][1] == i - 1:
            out[-1][1] = i
        else:
            out.append([i, i])
    return out


def scan_rendition(name: str, segs: Iterable) -> list:
    """Dead spans in one rendition. `segs` is an iterable of `Segment`.

    Two independent detectors, both scale-free, both scored against this
    rendition's own sizes so nothing needs to know its bitrate:

      1. **size collapse** — a run of segments under `DEAD_FRACTION` of the p90.
      2. **frozen frame**  — a run of byte-identical sizes under
         `IDENTICAL_FRACTION` of the p90.

    A run has to last `MIN_RUN_SECS` either way.
    """
    segments = list(segs)
    if len(segments) <= SKIP_TRAILING + 1:
        return []          # too short to say anything useful about

    # Start offsets, so a span can be reported in playback seconds.
    starts = []
    acc = 0.0
    for s in segments:
        starts.append(acc)
        acc += max(0.0, s.duration)

    judged = segments[:len(segments) - SKIP_TRAILING]
    sizes = [s.size for s in judged]
    ref = _percentile([z for z in sizes if z > 0], REF_PERCENTILE)
    if ref <= 0:
        return []          # nothing to compare against (every segment missing)

    dead_flags = [(z < 0) or (z < ref * DEAD_FRACTION) for z in sizes]

    # Identical-size runs, scored separately so a frozen stretch is caught even
    # where the collapse test's reference is dragged down by the damage itself.
    frozen_flags = [False] * len(sizes)
    for a, b in _runs([True] * len(sizes)):   # one pass over the whole rendition
        i = a
        while i <= b:
            j = i
            while j + 1 <= b and sizes[j + 1] == sizes[i]:
                j += 1
            if (j - i + 1) >= IDENTICAL_RUN and 0 <= sizes[i] < ref * IDENTICAL_FRACTION:
                for k in range(i, j + 1):
                    frozen_flags[k] = True
            i = j + 1

    spans = []
    for flags, reason in ((dead_flags, "dead"), (frozen_flags, "frozen")):
        for a, b in _runs(flags):
            start = starts[a]
            end = starts[b] + max(0.0, judged[b].duration)
            if (end - start) >= MIN_RUN_SECS:
                spans.append(Span(name, start, end, reason))

    # A frozen run is always also a dead run, so drop the duplicate report and
    # keep the more specific label.
    frozen = [s for s in spans if s.reason == "frozen"]
    kept = list(frozen)
    for s in spans:
        if s.reason != "dead":
            continue
        if any(f.start <= s.start and s.end <= f.end for f in frozen):
            continue
        kept.append(s)
    return sorted(kept, key=lambda s: s.start)


def _merge(spans: list) -> list:
    """`spans` flattened to non-overlapping `[start, end]` intervals."""
    merged = []
    for s in sorted(spans, key=lambda x: x.start):
        if merged and s.start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], s.end)
        else:
            merged.append([s.start, s.end])
    return merged


def _intersect(a: list, b: list) -> list:
    """The overlap of two merged interval lists."""
    out = []
    i = j = 0
    while i < len(a) and j < len(b):
        lo, hi = max(a[i][0], b[j][0]), min(a[i][1], b[j][1])
        if hi > lo:
            out.append([lo, hi])
        if a[i][1] < b[j][1]:
            i += 1
        else:
            j += 1
    return out


def _secs(intervals: list) -> float:
    return sum(b - a for a, b in intervals)


def _is_audio(name: str) -> bool:
    return name.startswith("audio")


def scan_bundle(renditions: dict) -> Verdict:
    """Judge a whole bundle.

    `renditions` maps a rendition name (`"video"`, `"video_480"`, `"audio_0"`, …)
    to its list of `Segment`. The caller does the file IO — this stays pure so it
    can be unit-tested without a bundle on disk.

    **A dead video rung on its own is not a verdict.** An episode's credits roll,
    or a held production card, encodes to almost nothing and by size alone reads
    exactly like a frozen frame. What makes the real failure unmistakable is that
    it kills BOTH sides at once: the hole in the source takes the interleaved
    audio with it, so the picture freezes over digital silence. So the primary
    test is the *overlap* between dead video and dead audio — which no credits
    roll (audio playing) and no quiet passage (picture moving) can produce.

    The one standalone signal kept is a long run of byte-identical segments,
    which needs `FROZEN_ALONE_SECS` before it counts on its own. That is what
    covers a bundle with no audio rendition at all.
    """
    spans = []
    total = 0.0
    saw_audio = False
    for name in sorted(renditions):
        segs = list(renditions[name])
        total = max(total, sum(max(0.0, s.duration) for s in segs))
        saw_audio = saw_audio or _is_audio(name)
        spans.extend(scan_rendition(name, segs))

    vid = _merge([s for s in spans if not _is_audio(s.rendition)])
    aud = _merge([s for s in spans if _is_audio(s.rendition)])
    agreed = _intersect(vid, aud) if (vid and aud) else []
    frozen = _merge([s for s in spans if s.reason == "frozen"])

    both_dead = _secs(agreed) >= MIN_TOTAL_SECS
    long_frozen = _secs(frozen) >= FROZEN_ALONE_SECS
    damaged = both_dead or long_frozen

    if damaged:
        dead_iv = _merge([s for s in spans
                          if any(lo < s.end and s.start < hi
                                 for lo, hi in (agreed if both_dead else frozen))])
        dead = _secs(dead_iv)
    else:
        dead = 0.0

    if not renditions:
        detail = "no renditions to check"
    elif damaged:
        worst = max(spans, key=lambda s: s.seconds)
        pct = (dead / total * 100.0) if total > 0 else 0.0
        why = "picture frozen over silence" if both_dead else "picture frozen"
        detail = (f"{_clock(dead)} of {_clock(total)} dead ({pct:.0f}%) — {why}; "
                  f"worst {worst.rendition} {_clock(worst.start)}-{_clock(worst.end)}")
    elif not saw_audio and not spans:
        detail = "clean"
    else:
        detail = "clean"
    return Verdict(damaged,
                   sorted(spans, key=lambda s: -s.seconds) if damaged else [],
                   dead, total, detail)


def _clock(secs: float) -> str:
    """`m:ss`, the form the spans read best in."""
    s = int(max(0.0, secs))
    return f"{s // 60}:{s % 60:02d}"
