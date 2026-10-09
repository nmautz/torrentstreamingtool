"""HDR sources in an SDR bundle: tone-map the picture, and say so in the header.

A prepped bundle is 8-bit H.264. An HDR10 or HLG source used to reach it as
`scale` -> `yuv420p` with nothing else done, which leaves two things wrong:

  * the picture is still PQ / BT.2020 code values, so it looks flat and washed out
    on anything that reads it as SDR;
  * ffmpeg carries the source's labels across. The init segment says BT.2020 / PQ
    (`colr`) and holds the mastering-display and light-level boxes (`mdcv`,
    `clli`), on a stream that is neither 10-bit nor HEVC. A phone plays that. A
    Chromecast fetched the first video segment and asked for nothing more
    (Project Hail Mary, 2026-10-09), and those labels were the one thing that
    bundle had which none of the 42 others it had played did.

This module decides what to do about a source (`mode`), builds the filter chain
for one rung (`chain`), names the output's colour (`out_tags`), and removes HDR
boxes from an init segment after the encode (`scrub_init`), because whether the
muxer still writes them depends on the ffmpeg version and cannot be left to it.

Leaf module: stdlib only, no `main` import. Tests in `tests/test_hdrmap.py`.
"""
from __future__ import annotations

import struct
from typing import Optional

# ffprobe `color_transfer` values that mean "this is HDR".
HDR_TRANSFERS = {"smpte2084": "HDR10", "arib-std-b67": "HLG"}

TONEMAP = "tonemap"   # zscale + tonemap: a real SDR picture
RETAG = "retag"       # picture untouched, header corrected: the fallback

# What the chain is told when the source names its transfer but not the rest.
# Every HDR10 and HLG release is BT.2020 non-constant-luminance, and zscale stops
# with "no path between colorspaces" on a frame whose matrix is unspecified.
_DEFAULT_PRIMARIES = "bt2020"
_DEFAULT_MATRIX = "bt2020nc"
_KNOWN_PRIMARIES = {"bt2020", "bt709", "smpte431", "smpte432"}
_KNOWN_MATRICES = {"bt2020nc", "bt2020c", "bt709"}

# Sample entries whose children hold the colour boxes.
_VISUAL_ENTRIES = {b"avc1", b"avc3", b"hvc1", b"hev1", b"dvh1", b"dvhe", b"encv"}
_VISUAL_ENTRY_HEAD = 86          # box header (8) + VisualSampleEntry fields (78)
_HDR_BOXES = {b"mdcv", b"clli"}
_CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl"}


def transfer(video: Optional[dict]) -> str:
    """The source's HDR transfer function, or "" when it is not HDR."""
    t = ((video or {}).get("color_transfer") or "").lower()
    return t if t in HDR_TRANSFERS else ""


def mode(video: Optional[dict], can_tonemap: bool) -> str:
    """"" for an SDR source, else TONEMAP when this ffmpeg can, else RETAG."""
    if not transfer(video):
        return ""
    return TONEMAP if can_tonemap else RETAG


def chain(video: Optional[dict], how: str, scale_h: int = 0) -> str:
    """The `-filter:v` value for one encoded rung of an HDR source.

    A down-rung is scaled FIRST: tone mapping works in 32-bit float RGB and costs
    by the pixel, so mapping 4K and then throwing most of it away is the slow way
    round. `sidedata=mode=delete` drops the per-frame HDR metadata, which the
    encoder would otherwise hand to the muxer.
    """
    v = video or {}
    parts = []
    if scale_h:
        parts.append("scale=-2:%d" % int(scale_h))
    if how == TONEMAP:
        pri = (v.get("color_primaries") or "").lower()
        mat = (v.get("color_space") or "").lower()
        parts += [
            # State the input first: zscale reads it off the frame, and a source
            # that tags only its transfer leaves the rest unspecified.
            "setparams=color_primaries=%s:color_trc=%s:colorspace=%s" % (
                pri if pri in _KNOWN_PRIMARIES else _DEFAULT_PRIMARIES,
                transfer(v) or "smpte2084",
                mat if mat in _KNOWN_MATRICES else _DEFAULT_MATRIX),
            "zscale=t=linear:npl=100",
            "format=gbrpf32le",
            "zscale=p=bt709",
            "tonemap=tonemap=hable:desat=0",
            "zscale=t=bt709:m=bt709:r=tv",
            "format=yuv420p",
        ]
    else:
        parts.append("setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709")
    parts.append("sidedata=mode=delete")
    return ",".join(parts)


def out_tags(i: int) -> list:
    """Output options that label video stream `i` BT.709, whatever the encoder
    would have inherited."""
    return ["-color_primaries:v:%d" % i, "bt709",
            "-color_trc:v:%d" % i, "bt709",
            "-colorspace:v:%d" % i, "bt709"]


def _boxes(buf: bytes, pos: int, end: int):
    """Yield (type, start, body_start, stop) for each box in buf[pos:end]."""
    while pos + 8 <= end:
        size, typ = struct.unpack(">I4s", buf[pos:pos + 8])
        head = 8
        if size == 1:
            if pos + 16 > end:
                return
            size = struct.unpack(">Q", buf[pos + 8:pos + 16])[0]
            head = 16
        elif size == 0:
            size = end - pos
        if size < head or pos + size > end:
            return
        yield typ, pos, pos + head, pos + size
        pos += size


def _entry_children(buf: bytes):
    """Yield (type, start, body_start, stop) for the child boxes of every visual
    sample entry in an init segment."""
    def walk(pos, end):
        for typ, start, body, stop in _boxes(buf, pos, end):
            if typ in _CONTAINERS:
                yield from walk(body, stop)
            elif typ == b"stsd":
                for etyp, estart, _ebody, estop in _boxes(buf, body + 8, stop):
                    if etyp in _VISUAL_ENTRIES:
                        yield from _boxes(buf, estart + _VISUAL_ENTRY_HEAD, estop)
    yield from walk(0, len(buf))


def init_marks(data: bytes) -> dict:
    """What an init segment claims about its colour:
    {"colr": (primaries, transfer, matrix) | None, "hdr_boxes": ["mdcv", ...]}."""
    colr = None
    found = []
    for typ, _start, body, stop in _entry_children(data):
        if typ == b"colr" and data[body:body + 4] == b"nclx" and stop - body >= 10:
            colr = struct.unpack(">HHH", data[body + 4:body + 10])
        elif typ in _HDR_BOXES:
            found.append(typ.decode("ascii"))
    return {"colr": colr, "hdr_boxes": found}


def claims_hdr(marks: dict) -> bool:
    """True when a header still tells a player the stream is HDR."""
    colr = marks.get("colr")
    return bool(marks.get("hdr_boxes")) or bool(colr and colr[1] in (16, 18))


def scrub_init(data: bytes) -> tuple:
    """Turn every `mdcv` / `clli` box in an init segment into a `free` box.

    Same length, so no size or offset anywhere else has to change, and a parser
    skips a `free` box by definition. Returns (data, [names removed]); the data is
    the same object when there was nothing to remove.
    """
    hits = [(start, typ) for typ, start, _body, _stop in _entry_children(data)
            if typ in _HDR_BOXES]
    if not hits:
        return data, []
    out = bytearray(data)
    for start, _typ in hits:
        out[start + 4:start + 8] = b"free"
    return bytes(out), [typ.decode("ascii") for _start, typ in hits]
