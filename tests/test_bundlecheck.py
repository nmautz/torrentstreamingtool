"""Unit tests for `bundlecheck.py` - is a long stretch of this bundle dead?

    python tests/test_bundlecheck.py    (or `make test`)

No bundle on disk, no ffmpeg: every case is a list of (duration, size) pairs.

The numbers are not invented. The first block replays Hacks S03E03 and S03E04
as they were measured off the live box - the two bundles prepped while their
torrent was still downloading (built 18:09:01 and 18:12:03 against a torrent
that finished at 18:15:58). Their five clean siblings from the same pack, prepped
minutes later against the finished file, must stay clean. The rest pin the
false-positive direction, which is the one that costs a pointless re-encode of a
whole episode: a short fade to black, a credits roll, a genuinely low-bitrate
rendition, and the short tail segment every rendition ends with.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bundlecheck as bc        # noqa: E402

_PASS = 0
_FAIL = []


def ok(name, cond, detail=""):
    global _PASS
    if cond:
        _PASS += 1
    else:
        _FAIL.append("%s%s" % (name, ("\n     " + detail) if detail else ""))


def segs(dur, sizes):
    """A rendition: uniform `dur` per segment, one entry per size."""
    return [bc.Segment("seg_%05d.m4s" % i, dur, z) for i, z in enumerate(sizes)]


def spans_of(verdict):
    return [(s.rendition, int(s.start), int(s.end), s.reason) for s in verdict.spans]


# ── The real failure, as measured ────────────────────────────────────────────
#
# Hacks S03E03, video rung: 10.427 s segments. Healthy 3.1-8.1 MB. The frozen
# stretch is 440,977 B, byte-identical segment after segment - one held frame.
# Segments 8-43 = 1:23 to 7:39, which is exactly where playback froze.

VID_HEALTHY = [5920731, 3861765, 3951410, 5399303, 4394859, 4258199, 4699880, 3169638,
               3927615, 8117711, 7394699, 4789083, 4581986, 3289553, 6457226]


def vid_span(n, held):
    """`n` segments of one held frame - the same bytes, `n` times over."""
    return [held] * n


def vid_ok(n):
    return [VID_HEALTHY[i % len(VID_HEALTHY)] for i in range(n)]


# The six windows, at the segment indices they occupy in the real bundle. Each
# holds a DIFFERENT frame, so each run is internally identical but they differ
# from one another - exactly as measured (440977, 309260, 300198, 210116, ...).
e03_video = (vid_ok(8) + vid_span(36, 440977) +       #  1:23 - 7:39
             vid_ok(11) + vid_span(13, 309260) +      #  9:33 - 11:49
             vid_ok(7) + vid_span(7, 300198) +        # 13:02 - 14:15
             vid_ok(6) + vid_span(17, 250752) +       # 15:17 - 18:14
             vid_ok(25) + vid_span(11, 210116) +      # 22:35 - 24:30
             vid_ok(24) + vid_span(9, 136286) +       # 28:41 - 30:14
             vid_ok(7) + [63403])

# ...and its audio rendition: 6.016 s segments, healthy 120-135 KB, silent 3.0 KB
# (6-byte AAC frames). Six dead windows, 918 s of 1896 s.
e03_audio = ([121950] * 14 + [3011] * 62 + [128000] * 20 + [3021] * 21 +
             [130000] * 14 + [3028] * 10 + [125000] * 13 + [3011] * 28 +
             [131000] * 44 + [3011] * 18 + [127000] * 43 + [3021] * 14 +
             [133000] * 14 + [1200])

e03 = bc.scan_bundle({"video": segs(10.427083, e03_video),
                      "audio_0": segs(6.016, e03_audio)})
ok("S03E03: damaged", e03.damaged, e03.detail)
ok("S03E03: reported as frozen over silence",
   "silence" in e03.detail, e03.detail)
ok("S03E03: a span starts at 1:2x",
   any(abs(st - 84) <= 8 for _, st, _, _ in spans_of(e03)), str(spans_of(e03)))
ok("S03E03: both rungs are represented",
   {"video", "audio_0"} <= {r for r, _, _, _ in spans_of(e03)}, str(spans_of(e03)))
# Dead time is the OVERLAP, not the sum: the same hole killed both rungs.
ok("S03E03: overlap is not double-counted",
   e03.dead_secs < 1100, "dead=%.0f" % e03.dead_secs)
ok("S03E03: and is most of the episode",
   e03.dead_secs > 800, "dead=%.0f" % e03.dead_secs)

# The frozen-frame detector on its own - a bundle whose audio rung is somehow
# fine still gets caught by 6 minutes of one held picture.
vonly = bc.scan_bundle({"video": segs(10.427083, e03_video)})
ok("S03E03 video alone: the held frame is enough", vonly.damaged, vonly.detail)
ok("S03E03 video alone: found as frozen",
   any(r == "frozen" for _, _, _, r in spans_of(vonly)), str(spans_of(vonly)))

# Hacks S03E04 - the same bug, milder (15% dead). It must not slip through for
# being less broken than its neighbour. Its windows are shorter, so this is the
# case that proves the video/audio agreement path carries its weight: no single
# frozen run here reaches FROZEN_ALONE_SECS.
e04_audio = ([124000] * 17 + [3011] * 10 + [124000] * 15 + [3015] * 11 +
             [124000] * 23 + [3011] * 7 + [124000] * 34 + [3011] * 10 +
             [124000] * 117 + [3020] * 14 + [124000] * 81 + [900])
e04_video = ([4100000] * 10 + [210000] * 6 + [4100000] * 9 + [211000] * 6 +
             [4100000] * 13 + [209000] * 4 + [4100000] * 20 + [213000] * 6 +
             [4100000] * 67 + [208000] * 8 + [4100000] * 46 + [90000])
e04 = bc.scan_bundle({"video": segs(10.427083, e04_video),
                      "audio_0": segs(6.016, e04_audio)})
ok("S03E04: damaged", e04.damaged, e04.detail)
ok("S03E04: several windows", len(e04.spans) >= 5, str(spans_of(e04)))

# The five clean siblings from the same pack, prepped after the torrent finished.
for name, n in (("E01", 363), ("E02", 330), ("E05", 292), ("E06", 342), ("E07", 345)):
    # Real segments vary one to the next; a flat list would be a softer test.
    aud = [120000 + (i * 7919) % 15000 for i in range(n - 1)] + [1100]
    vid = [3000000 + (i * 104729) % 4000000 for i in range(n // 2)] + [400000]
    c = bc.scan_bundle({"video": segs(10.427083, vid), "audio_0": segs(6.016, aud)})
    ok("S03%s: clean" % name, not c.damaged, c.detail)


# ── The expensive direction: a good bundle must never be flagged ─────────────

# A healthy rendition with ordinary VBR swing.
healthy = [3000000 + (i * 104729) % 4000000 for i in range(180)] + [400000]
ok("clean: ordinary VBR is not damaged",
   not bc.scan_bundle({"video": segs(10.427083, healthy)}).damaged)

# A fade to black and a beat of silence - real, and shorter than MIN_RUN_SECS.
fade = healthy[:40] + [120000] + healthy[41:]
ok("clean: one dark segment is not damage",
   not bc.scan_bundle({"video": segs(10.427083, fade)}).damaged)

# A credits roll: five minutes of near-black that encodes to almost nothing. By
# SIZE this is indistinguishable from a frozen frame - the only thing separating
# them is that the credits music is still playing. This is the case that cost the
# first cut of this module a false positive on every episode with end credits.
credits = healthy[:150] + [280000 + (i * 1013) % 90000 for i in range(30)] + [90000]
cred_aud = [118000 + (i * 6203) % 12000 for i in range(315)] + [1100]
cr = bc.scan_bundle({"video": segs(10.427083, credits),
                     "audio_0": segs(6.016, cred_aud)})
ok("clean: a credits roll is not damage", not cr.damaged, cr.detail)

# The mirror: a long quiet passage with the picture still moving.
quiet_aud = ([120000] * 100 + [3000] * 8 + [120000] * 207) + [1100]
q = bc.scan_bundle({"video": segs(10.427083, healthy),
                    "audio_0": segs(6.016, quiet_aud)})
ok("clean: a quiet passage under moving picture is not damage", not q.damaged, q.detail)

# A whole rendition that is legitimately tiny (a still image over music). The
# reference is per-rendition, so nothing here is "small" relative to anything.
still = [41000 + (i * 97) % 2000 for i in range(180)] + [9000]
ok("clean: a uniformly low-bitrate rendition is not damage",
   not bc.scan_bundle({"video": segs(10.427083, still)}).damaged)

# A CBR-ish rendition whose sizes genuinely repeat must not read as frozen.
cbr = [250000] * 180 + [40000]
ok("clean: constant sizes near the reference are not frozen",
   not bc.scan_bundle({"video": segs(10.427083, cbr)}).damaged,
   bc.scan_bundle({"video": segs(10.427083, cbr)}).detail)

# The short tail segment every rendition ends with is legitimately tiny.
ok("clean: the trailing short segment is never judged",
   not bc.scan_bundle({"audio_0": segs(6.016, [124000] * 300 + [90])}).damaged)

# Degenerate inputs must not raise or flag.
ok("empty bundle is not damaged", not bc.scan_bundle({}).damaged)
ok("empty rendition is not damaged", not bc.scan_bundle({"video": []}).damaged)
ok("one segment is not damaged", not bc.scan_bundle({"video": segs(6.0, [10])}).damaged)


# ── Missing files count as dead ──────────────────────────────────────────────

gone_aud = [124000] * 20 + [-1] * 20 + [124000] * 270 + [1100]
gone_vid = [4100000] * 11 + [-1] * 12 + [4100000] * 155 + [400000]
g = bc.scan_bundle({"video": segs(10.427083, gone_vid),
                    "audio_0": segs(6.016, gone_aud)})
ok("missing segments are damage", g.damaged, g.detail)


# ── Playlist parsing ─────────────────────────────────────────────────────────

PL = """#EXTM3U
#EXT-X-VERSION:7
#EXT-X-TARGETDURATION:10
#EXT-X-MAP:URI="init_video.mp4"
#EXTINF:10.427083,
seg_video_00000.m4s
#EXTINF:10.427083,
seg_video_00001.m4s
#EXTINF:8.341667,
seg_video_00002.m4s
#EXT-X-ENDLIST
"""
parsed = bc.parse_media_playlist(PL)
ok("playlist: three segments", len(parsed) == 3, str(parsed))
ok("playlist: name and duration", parsed[0] == ("seg_video_00000.m4s", 10.427083),
   str(parsed[0]))
ok("playlist: the short tail is read", abs(parsed[2][1] - 8.341667) < 1e-6)
ok("playlist: EXT-X-MAP is not a segment",
   all("init_" not in n for n, _ in parsed), str(parsed))
ok("playlist: empty input", bc.parse_media_playlist("") == [])
ok("playlist: a URI with no EXTINF is ignored",
   bc.parse_media_playlist("#EXTM3U\nstray.m4s\n") == [])


# ── The verdict is JSON-safe for library.json / the admin API ────────────────

d = e03.as_dict()
ok("as_dict: shape", set(d) == {"damaged", "dead_secs", "total_secs", "detail", "spans"},
   str(sorted(d)))
ok("as_dict: spans carry rendition + reason",
   bool(d["spans"]) and set(d["spans"][0]) == {"rendition", "start", "end", "reason"},
   str(d["spans"][:1]))
import json                     # noqa: E402
ok("as_dict: serialises", isinstance(json.dumps(d), str))


print("%d passed, %d failed" % (_PASS, len(_FAIL)))
for f in _FAIL:
    print("  FAIL", f)
sys.exit(1 if _FAIL else 0)
