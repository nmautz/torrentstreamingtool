"""Release quality estimated from a torrent title, cross-checked against its size.

Before a torrent is downloaded there is almost nothing to go on: an indexer
hands over a title, a byte count and an advertised seeder count. Nothing reports
the real resolution -- that only becomes knowable once a file header exists to
read (see `dvprobe.py`). So this module answers the one question the download
race needs answered up front: **of these candidate releases, which are good?**

Two signals, in a deliberate order:

* **The name is primary.** `2160p`, `WEB-DL`, `x265`, `REMUX` are what the scene
  actually labels a release with, and they are right far more often than not.
  Everything here starts from the tokens.
* **Bytes-per-minute is a cross-check, and it only ever DEMOTES.** A release
  claiming 2160p that carries 1.4 GB across two hours is not 2160p, whatever it
  says. Run the rule the other way -- "this file is big, so promote it" -- and
  every mis-counted season pack becomes a fake 4K remux, so promotion is refused
  outright. An unknown runtime therefore costs nothing: the name simply stands.

The cross-check is **codec-aware**, which is the whole reason it can be trusted.
A 1080p HEVC encode legitimately runs at roughly 60% of the bitrate of the 1080p
h264 encode beside it; judged against one shared band it would be demoted for
being *good at its job*. Each (height, source) band is therefore scaled by the
codec before the comparison, and a lean HEVC release lands mid-band exactly
where it belongs.

The expected bands are deliberately loose. The job is catching a release that
lies about its tier, not grading encodes -- a 15% bitrate difference between two
1080p WEB-DLs is not something this module has an opinion about, and shouldn't.

Pure and dependency-free (stdlib only), like `episodes.py` and `dvprobe.py`. No
I/O, no clock, no `main` import, and **never raises** -- every entry point
returns a well-formed dict for any input at all, because it runs inside a
download-race tick where an exception would strand live torrents.

See docs/GOTCHAS.md and docs/LIBRARY_DATA.md - race.
"""

from __future__ import annotations

import re
from typing import Any

# Ladder used for both ranking and the demotion walk, highest first. 1440p is
# vanishingly rare on indexers but does appear; leaving it out would round it to
# either 2160 or 1080 and mis-band it either way.
HEIGHTS = (2160, 1440, 1080, 720, 576, 480)

SOURCES = ("remux", "bluray", "webdl", "webrip", "hdtv", "dvd", "cam", "")
CODECS = ("av1", "hevc", "h264", "mpeg2", "xvid", "")

# A source's standing at equal resolution. A rank axis, never a bitrate input
# (that is what the bands below are for).
SOURCE_RANK = {"remux": 5, "bluray": 4, "webdl": 3, "webrip": 2,
               "hdtv": 1, "dvd": 1, "cam": 0, "": 2}

# 1 Mbps sustained for 1 minute = 1e6/8 bytes/s * 60 s.
_MBPS_TO_BPM = 7.5e6

# Expected bitrate bands in **h264-equivalent Mbps**, (low, high). The codec
# multiplier below is applied to both ends at read time.
_BANDS: dict = {
    2160: {"remux": (50.0, 110.0), "bluray": (25.0, 70.0), "webdl": (12.0, 40.0),
           "webrip": (8.0, 25.0), "hdtv": (6.0, 20.0), "dvd": (4.0, 12.0),
           "cam": (2.0, 8.0), "": (8.0, 40.0)},
    1440: {"remux": (30.0, 60.0), "bluray": (14.0, 40.0), "webdl": (7.0, 22.0),
           "webrip": (5.0, 16.0), "hdtv": (4.0, 13.0), "dvd": (2.0, 7.0),
           "cam": (1.0, 5.0), "": (5.0, 22.0)},
    1080: {"remux": (20.0, 40.0), "bluray": (8.0, 25.0), "webdl": (4.0, 14.0),
           "webrip": (3.0, 10.0), "hdtv": (2.5, 9.0), "dvd": (1.0, 4.0),
           "cam": (0.6, 3.0), "": (3.0, 14.0)},
    720: {"remux": (8.0, 20.0), "bluray": (4.0, 12.0), "webdl": (2.0, 7.0),
          "webrip": (1.5, 5.0), "hdtv": (1.5, 5.0), "dvd": (0.8, 3.0),
          "cam": (0.4, 2.0), "": (1.5, 7.0)},
    576: {"remux": (3.0, 8.0), "bluray": (1.5, 6.0), "webdl": (1.0, 4.0),
          "webrip": (0.8, 3.0), "hdtv": (0.8, 3.0), "dvd": (0.4, 2.0),
          "cam": (0.3, 1.5), "": (0.8, 4.0)},
    480: {"remux": (3.0, 8.0), "bluray": (1.5, 6.0), "webdl": (1.0, 4.0),
          "webrip": (0.8, 3.0), "hdtv": (0.8, 3.0), "dvd": (0.4, 2.0),
          "cam": (0.3, 1.5), "": (0.8, 4.0)},
}

# Applied to BOTH ends of the band. `remux` is exempt -- a remux is the disc
# bitstream copied verbatim, so its codec is a property of the disc rather than
# of an encoder's efficiency, and scaling it down would demote every one of them.
# Unknown sits just under h264: an untagged release must never be demoted merely
# for being untagged.
_CODEC_MULT = {"h264": 1.00, "hevc": 0.60, "av1": 0.50,
               "mpeg2": 2.00, "xvid": 1.20, "": 0.85}

# How far below its own band's floor a release may sit and still be believed.
# Slack exists because the bands are population estimates and a good encoder
# beats them; combined with the two-sided walk in `cross_check`, a release has
# to fail its own floor AND fit a lower tier before it is demoted.
_FLOOR_SLACK = 0.45
# Above this multiple of the band ceiling the size is not evidence of quality,
# it is evidence we are dividing by the wrong amount of content.
_OVERSIZE_MULT = 3.0

_HEIGHT_RE = re.compile(r"\b(2160|1440|1080|720|576|480)\s*[pi]\b", re.I)
_UHD_RE = re.compile(r"\b(4k|uhd|2160)\b", re.I)

_RE_REMUX = re.compile(r"\bremux\b", re.I)
_RE_BLURAY = re.compile(
    r"\b(blu[-. _]?ray|bluray|bdrip|brrip|bd[-. _]?rip|bdmv|bd25|bd50)\b", re.I)
_RE_WEBDL = re.compile(r"\bweb[-. _]?dl\b", re.I)
_RE_WEBRIP = re.compile(r"\bweb[-. _]?rip\b", re.I)
_RE_WEB = re.compile(r"\bweb\b", re.I)
_RE_HDTV = re.compile(r"\b(hdtv|pdtv|sdtv|dsr|dvbrip|tvrip)\b", re.I)
_RE_DVD = re.compile(r"\b(dvd[-. _]?rip|dvd[59]|dvdr|dvdscr|ntsc|pal)\b", re.I)
# `SCREENER`/`WORKPRINT`/`CAMRIP` are unambiguous. Bare `TS`/`TC` are NOT -- see
# `_source_of`: they collide with release-group names far more often than they
# mean telesync.
_RE_CAM_HARD = re.compile(
    r"\b(camrip|hdcam|telesync|telecine|hd[-. _]?ts|hd[-. _]?tc"
    r"|workprint|screener|cam)\b", re.I)
_RE_CAM_SOFT = re.compile(r"\b(ts|tc|scr|wp)\b", re.I)

_RE_AV1 = re.compile(r"\bav1\b", re.I)
_RE_HEVC = re.compile(r"\b(x\.?265|h\.?265|hevc)\b", re.I)
_RE_H264 = re.compile(r"\b(x\.?264|h\.?264|avc)\b", re.I)
_RE_MPEG2 = re.compile(r"\bmpeg[-. _]?2\b", re.I)
_RE_XVID = re.compile(r"\b(xvid|divx)\b", re.I)

_RE_10BIT = re.compile(r"\b(10[-. _]?bit|hi10p?)\b", re.I)
_RE_HDR = re.compile(r"\b(hdr10\+?|hdr|hlg|dolby[-. _]?vision|dovi|dv)\b", re.I)
_RE_REPACK = re.compile(r"\b(repack|proper)\b", re.I)

_SOURCE_DISPLAY = {"remux": "REMUX", "bluray": "BluRay", "webdl": "WEB-DL",
                   "webrip": "WEBRip", "hdtv": "HDTV", "dvd": "DVD", "cam": "CAM"}
_CODEC_DISPLAY = {"hevc": "HEVC", "h264": "H.264", "av1": "AV1",
                  "xvid": "XviD", "mpeg2": "MPEG-2"}


def _norm(title: Any) -> str:
    r"""A title reduced to something the token regexes can match against.

    Only brackets, underscores and `+` are flattened. Dots and dashes are
    **kept**, and that is load-bearing in both directions:

    * `\b` already treats a dot as a separator, so `Show.1080p.WEB-DL` tokenizes
      correctly without touching it -- while `_` is a word character and would
      glue `Show_1080p` into one token, so it does have to go.
    * Flattening dots would split `H.265` into `H 265`, and `\bh\.?265\b` then
      matches nothing. Every dotted scene name carrying `H.264`/`H.265`/`x.265`
      would come back codec-less and be banded at the unknown multiplier.
    """
    if not isinstance(title, str):
        return ""
    return re.sub(r"[\[\]()_+]+", " ", title)[:512]


def _source_of(t: str) -> str:
    """The release source, highest-standing match first.

    The ordering is not cosmetic: a `1080p.BluRay.REMUX.AVC` title matches both
    `remux` and `bluray`, and it is a remux. Likewise `WEB-DL` must be tested
    before the bare `WEB` fallback, or every WEB-DL reads as a generic web rip.
    """
    if _RE_REMUX.search(t):
        return "remux"
    if _RE_BLURAY.search(t):
        return "bluray"
    if _RE_WEBDL.search(t):
        return "webdl"
    if _RE_WEBRIP.search(t):
        return "webrip"
    if _RE_WEB.search(t):
        # Bare `WEB` in a scene name is conventionally a WEB-DL, not a re-encode
        # off a player: `1080p.WEB.H264-GROUP` is the standard Amazon/Netflix form.
        return "webdl"
    if _RE_HDTV.search(t):
        return "hdtv"
    if _RE_DVD.search(t):
        return "dvd"
    if _RE_CAM_HARD.search(t):
        return "cam"
    # The TS footgun. `Movie.2024.1080p.WEB-DL.x264-TSuRRouNDeD` does not match
    # (no word boundary after "TS"), but a group literally named `TS` or `SCR`
    # does -- and calling a clean WEB-DL a telesync would bury it under genuine
    # junk. A bare TS/TC/SCR is therefore only believed when NOTHING else in the
    # title names a source, which is exactly the shape a real cam release has.
    if _RE_CAM_SOFT.search(t):
        return "cam"
    return ""


def _codec_of(t: str) -> str:
    if _RE_AV1.search(t):
        return "av1"
    if _RE_HEVC.search(t):
        return "hevc"
    if _RE_H264.search(t):
        return "h264"
    if _RE_MPEG2.search(t):
        return "mpeg2"
    if _RE_XVID.search(t):
        return "xvid"
    return ""


def parse(title: Any) -> dict:
    """Classify a release title into {height, source, codec, bit10, hdr, ...}.

    `height` is 0 when the title makes no resolution claim -- a real and common
    state (most fansubs, plenty of movie releases), handled downstream by
    inference rather than by guessing a default. Never raises.
    """
    t = _norm(title)
    if not t:
        return {"height": 0, "source": "", "codec": "", "bit10": False,
                "hdr": False, "repack": False, "confidence": 0.0}
    m = _HEIGHT_RE.search(t)
    if m:
        height = int(m.group(1))
    elif _UHD_RE.search(t):
        height = 2160
    else:
        height = 0
    source = _source_of(t)
    codec = _codec_of(t)
    # Confidence is "how much of this did the title actually tell us". It feeds
    # nothing automatic; it exists so the UI and the logs can be honest.
    conf = ((0.45 if height else 0.0) + (0.35 if source else 0.0)
            + (0.20 if codec else 0.0))
    return {"height": height, "source": source, "codec": codec,
            "bit10": bool(_RE_10BIT.search(t)), "hdr": bool(_RE_HDR.search(t)),
            "repack": bool(_RE_REPACK.search(t)), "confidence": round(conf, 2)}


def expected_bpm(height: int, codec: str, source: str) -> tuple:
    """The (low, high) bytes-per-minute band a release of this shape should hit.

    Codec-scaled, except for `remux` -- see `_CODEC_MULT`.
    """
    try:
        h = int(height or 0)
    except (TypeError, ValueError):
        h = 0
    if h not in _BANDS:
        # Snap to the nearest rung rather than failing: an odd height (1088p,
        # 1092p off a bad crop) should still be judged against something.
        h = min(HEIGHTS, key=lambda x: abs(x - h)) if h else 1080
    row = _BANDS[h]
    src = source if source in row else ""
    lo, hi = row[src]
    if src != "remux":
        mult = _CODEC_MULT.get(codec or "", _CODEC_MULT[""])
        lo, hi = lo * mult, hi * mult
    return lo * _MBPS_TO_BPM, hi * _MBPS_TO_BPM


def infer_height(bpm: float, codec: str, source: str) -> int:
    """Place an UNLABELLED release on the ladder by its bitrate alone.

    Only ever called when the title claims no resolution. This is inference, not
    demotion: there is no claim to contradict, so the bitrate is the only
    information available and using it beats assuming a default.
    """
    if bpm <= 0:
        return 0
    for h in HEIGHTS:
        lo, _hi = expected_bpm(h, codec, source)
        if bpm >= lo:
            return h
    return HEIGHTS[-1]


def _band_position(bpm: float, lo: float, hi: float) -> float:
    if bpm <= 0 or hi <= lo:
        return 0.5
    return max(0.0, min(1.0, (bpm - lo) / (hi - lo)))


def cross_check(parsed: dict, size_bytes: float, runtime_min: float,
                episode_count: int = 1) -> dict:
    """Test a parsed title's claim against the bytes actually on offer.

    Returns `{verdict, bpm, expected, effective_height, bpm_position, reason}`,
    where `verdict` is one of:

    * `unknown` -- no runtime, no size or no episode count. The claim stands
      untouched and `bpm_position` is a neutral 0.5. Common, and fine.
    * `inferred` -- the title claimed no resolution; one was read off the bitrate.
    * `ok` -- the bitrate is consistent with the claim.
    * `demote` -- the bitrate fails this tier's floor *and* fits a lower one.
      `effective_height` drops to that lower rung.
    * `suspect_oversize` -- far above the band's ceiling. Almost always means the
      divisor is wrong (a season pack counted as one episode), so the claim is
      left alone and **nothing is promoted**.

    `episode_count` is a required divisor for a pack and must never be guessed;
    callers pass 0 when they do not know, which routes here as `unknown`.
    """
    claimed = int(parsed.get("height") or 0)
    codec = parsed.get("codec") or ""
    source = parsed.get("source") or ""
    try:
        size = float(size_bytes or 0)
        runtime = float(runtime_min or 0)
        eps = int(episode_count or 0)
    except (TypeError, ValueError):
        size = runtime = 0.0
        eps = 0
    if size <= 0 or runtime <= 0 or eps <= 0:
        lo, hi = expected_bpm(claimed or 1080, codec, source)
        return {"verdict": "unknown", "bpm": 0.0, "expected": (lo, hi),
                "effective_height": claimed, "bpm_position": 0.5,
                "reason": "no runtime or size"}

    bpm = size / (runtime * eps)

    if not claimed:
        inferred = infer_height(bpm, codec, source)
        lo, hi = expected_bpm(inferred, codec, source)
        return {"verdict": "inferred", "bpm": bpm, "expected": (lo, hi),
                "effective_height": inferred,
                "bpm_position": _band_position(bpm, lo, hi),
                "reason": "title claims no resolution"}

    lo, hi = expected_bpm(claimed, codec, source)

    if bpm > hi * _OVERSIZE_MULT:
        return {"verdict": "suspect_oversize", "bpm": bpm, "expected": (lo, hi),
                "effective_height": claimed, "bpm_position": 1.0,
                "reason": "far above band - the episode count is probably wrong"}

    if bpm >= lo * _FLOOR_SLACK:
        return {"verdict": "ok", "bpm": bpm, "expected": (lo, hi),
                "effective_height": claimed,
                "bpm_position": _band_position(bpm, lo, hi), "reason": ""}

    # Walk DOWN: the effective tier is the highest rung below the claim whose own
    # floor this bitrate actually clears.
    effective = HEIGHTS[-1]
    for h in HEIGHTS:
        if h >= claimed:
            continue
        h_lo, _h_hi = expected_bpm(h, codec, source)
        if bpm >= h_lo * _FLOOR_SLACK:
            effective = h
            break
    e_lo, e_hi = expected_bpm(effective, codec, source)
    return {"verdict": "demote", "bpm": bpm, "expected": (lo, hi),
            "effective_height": effective,
            "bpm_position": _band_position(bpm, e_lo, e_hi),
            "reason": "bitrate too low for the claimed resolution"}


def score(title: Any, size_bytes: float = 0, runtime_min: float = 0.0,
          episode_count: int = 1, *, ceiling: int = 1080,
          seeders: int = 0) -> dict:
    """Parse + cross-check + rank a candidate release. The entry point `main` calls.

    `ceiling` is the highest tier the household wants fetched; `hq_eligible`
    marks the candidates the HQ track of a download race may aim at. A release
    DEMOTED to at-or-below the ceiling is eligible -- what matters is the
    resolution you will actually get, not the one the title advertises.

    Never raises: any failure returns a valid, bottom-ranked descriptor.
    """
    try:
        parsed = parse(title)
        cc = cross_check(parsed, size_bytes, runtime_min, episode_count)
        try:
            ceil = int(ceiling or 1080)
        except (TypeError, ValueError):
            ceil = 1080
        try:
            seeds = max(0, int(seeders or 0))
        except (TypeError, ValueError):
            seeds = 0
        eff = int(cc.get("effective_height") or 0)
        desc = {
            "title": title if isinstance(title, str) else "",
            "height": parsed["height"],
            "effective_height": eff,
            "source": parsed["source"],
            "codec": parsed["codec"],
            "bit10": parsed["bit10"],
            "hdr": parsed["hdr"],
            "repack": parsed["repack"],
            "confidence": parsed["confidence"],
            "verdict": cc["verdict"],
            "bpm": round(float(cc["bpm"]), 1),
            "bpm_position": round(float(cc["bpm_position"]), 3),
            "reason": cc["reason"],
            "seeders": seeds,
            # An unresolved height cannot be held against a release, but it
            # cannot be waved into the HQ slot either -- eligibility needs a
            # height we can actually compare with the ceiling.
            "hq_eligible": bool(eff and eff <= ceil),
        }
        desc["label"] = label(desc)
        return desc
    except Exception:       # pragma: no cover - the whole point is never to raise
        return {"title": "", "height": 0, "effective_height": 0, "source": "",
                "codec": "", "bit10": False, "hdr": False, "repack": False,
                "confidence": 0.0, "verdict": "unknown", "bpm": 0.0,
                "bpm_position": 0.5, "reason": "unparseable", "seeders": 0,
                "hq_eligible": False, "label": "Unknown"}


def rank_key(desc: dict) -> tuple:
    """Sortable, best-first under `reverse=True`.

    A tuple rather than a scalar on purpose: collapsing resolution, source and
    bitrate into one number always ends up trading them against each other in
    ways nobody intended (a generous bitrate quietly outvoting a whole
    resolution rung). Compared in order, each axis only breaks the ties above it.

    **Codec is deliberately absent.** It is already accounted for inside
    `bpm_position`, which judges an HEVC release against an HEVC band. Adding it
    here as well would punish HEVC twice for the efficiency that makes it good.
    """
    if not isinstance(desc, dict):
        return (0, 0, 0.0, 0, 0)
    return (
        int(desc.get("effective_height") or 0),
        SOURCE_RANK.get(desc.get("source") or "", 2),
        float(desc.get("bpm_position") or 0.5),
        1 if desc.get("bit10") else 0,
        int(desc.get("seeders") or 0),
    )


def is_upgrade(lq: dict, hq: dict) -> bool:
    """True when `hq` is worth keeping a second parallel download alive for.

    Strictly a **resolution** question. A better source at the same resolution
    (WEBRip to BluRay) is a real improvement, but not one worth the bandwidth of
    a parallel download plus a mid-watch file swap. A whole extra rung is.
    """
    if not isinstance(lq, dict) or not isinstance(hq, dict):
        return False
    return int(hq.get("effective_height") or 0) > int(lq.get("effective_height") or 0)


def label(desc: dict) -> str:
    """Short human label: "1080p WEB-DL HEVC", or "720p WEB-DL (listed 1080p)".

    A demoted release says so. The whole reason the cross-check exists is that
    the title was misleading, so hiding the disagreement would reproduce exactly
    the problem it was built to solve.
    """
    if not isinstance(desc, dict):
        return "Unknown"
    bits: list = []
    eff = int(desc.get("effective_height") or 0)
    if eff:
        bits.append(str(eff) + "p")
    src = _SOURCE_DISPLAY.get(desc.get("source") or "")
    if src:
        bits.append(src)
    cod = _CODEC_DISPLAY.get(desc.get("codec") or "")
    if cod:
        bits.append(cod)
    if not bits:
        return "Unknown"
    out = " ".join(bits)
    claimed = int(desc.get("height") or 0)
    if desc.get("verdict") == "demote" and claimed and claimed != eff:
        out += " (listed " + str(claimed) + "p)"
    return out
