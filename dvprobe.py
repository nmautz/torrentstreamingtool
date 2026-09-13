"""Dolby Vision / HDR signalling read straight out of a media file's header.

Some releases play back with a lurid **green** cast in VLC *and* in the prepped
HLS bundle. The cause is always the same one thing: **Dolby Vision Profile 5**.

Profile 5 is single-layer and stores the picture in **IPT-PQ-c2**, not YCbCr,
with the mapping back to something displayable carried in a per-frame RPU. Its
`bl_signal_compatibility_id` is 0 — there is deliberately **no** HDR10 or SDR
fallback baked into the base layer. A decoder that ignores the RPU (VLC, and
ffmpeg without libplacebo — which is to say everything this project runs) reads
IPT chroma planes as if they were Cb/Cr and renders the characteristic green.
Every other DV profile carries a usable base layer (8.1 → HDR10, 8.2 → SDR,
8.4 → HLG, 7 → an HDR10 BL) and is therefore *not* the green case — at worst
those look flat without tone-mapping, which is a different, milder problem.

Two things make this worth a dedicated module rather than an ffprobe call:

* **No ffmpeg needed.** The answer lives in a fixed-layout 5-byte
  `DOVIDecoderConfigurationRecord` in the container header — reachable with a
  short byte read. `analyzer.ffmpeg_bin()` can legitimately be absent, and the
  probe still has to work when it is.
* **Release titles lie.** Of the DV files observed in the wild here, the only
  one that announced itself did so as a bare "DV" — while plenty of *fine*
  P8.1 releases advertise "DV" just as loudly. Title matching alone would both
  miss real P5 files and reject good ones, so the file itself is the authority.
  `title_dv_risk` exists only to rank *search results*, where there is no file
  to read yet.

Pure and dependency-free (stdlib only), like `episodes.py`. Reads at most
`HEAD_BYTES` from the front of the file (plus, for MP4 with a trailing `moov`,
the same from the back). Never writes.

See docs/GOTCHAS.md § Dolby Vision Profile 5 and docs/LIBRARY_DATA.md § video.
"""

from __future__ import annotations

import os
import re
from typing import Optional

# How much of the header to read. Matroska puts Tracks well inside the first
# few hundred KB in every sample seen; 1 MiB is slack for oddly-muxed files
# without making the probe expensive (it runs per file on download completion).
HEAD_BYTES = 1024 * 1024

# ── EBML / Matroska ──────────────────────────────────────────────────────────
_EBML_MAGIC = b"\x1a\x45\xdf\xa3"

# Only the IDs this probe cares about; everything else is skipped by size.
_ID_SEGMENT     = 0x18538067
_ID_TRACKS      = 0x1654AE6B
_ID_TRACKENTRY  = 0xAE
_ID_VIDEO       = 0xE0
_ID_COLOUR      = 0x55B0
_ID_MASTERING   = 0x55D0
_ID_BLOCKADDMAP = 0x41E4
_ID_EBML        = 0x1A45DFA3
_ID_INFO        = 0x1549A966

_ID_CODECID     = 0x86
_ID_TRACKTYPE   = 0x83
_ID_PIXELWIDTH  = 0xB0
_ID_PIXELHEIGHT = 0xBA
_ID_MATRIX      = 0x55B1
_ID_BITDEPTH    = 0x55B2
_ID_TRANSFER    = 0x55BA
_ID_PRIMARIES   = 0x55BB
_ID_ADDIDTYPE   = 0x41E7
_ID_ADDIDEXTRA  = 0x41ED

# Elements whose payload is more elements. Anything not listed is read as a leaf
# and skipped, which is what keeps this parser short.
_MASTERS = {_ID_SEGMENT, _ID_TRACKS, _ID_TRACKENTRY, _ID_VIDEO, _ID_COLOUR,
            _ID_MASTERING, _ID_BLOCKADDMAP, _ID_EBML, _ID_INFO}

# BlockAddIDType values that introduce a Dolby Vision configuration record.
# The integers spell the same FourCCs MP4 uses for its boxes ('dvcC' / 'dvvC').
_DV_ADDID_TYPES = {0x64766343, 0x64767643}

_MP4_DV_BOXES = (b"dvcC", b"dvvC")
# Sample-entry FourCCs that mean "this track is Dolby Vision" in MP4 even before
# the config box is reached.
_MP4_DV_CODECS = (b"dvh1", b"dvhe", b"dav1", b"dvav", b"dva1")


def _read_vint(buf: bytes, pos: int, strip_marker: bool) -> tuple:
    """One EBML variable-length integer → (value, next_pos).

    `strip_marker` clears the leading length-marker bit, which is right for
    *sizes* and wrong for *IDs* (an ID is conventionally quoted including it).
    """
    if pos >= len(buf):
        raise EOFError("past end")
    b0 = buf[pos]
    if b0 == 0:
        raise ValueError("invalid vint")
    length, mask = 1, 0x80
    while not (b0 & mask):
        mask >>= 1
        length += 1
    if pos + length > len(buf):
        raise EOFError("truncated vint")
    val = (b0 & (mask - 1)) if strip_marker else b0
    for i in range(1, length):
        val = (val << 8) | buf[pos + i]
    return val, pos + length


def _uint(b: bytes) -> int:
    return int.from_bytes(b, "big") if b else 0


def _walk_ebml(buf: bytes, pos: int, end: int, out: list, depth: int = 0) -> None:
    """Collect (id, payload) for every leaf element, descending into masters.

    Tolerant by design: a truncated header (we only read the first MiB) simply
    stops the walk rather than raising, so a partially-downloaded file probes to
    "unknown" instead of failing the caller.
    """
    if depth > 12:
        return
    while pos < end:
        try:
            eid, p = _read_vint(buf, pos, False)
            size, p = _read_vint(buf, p, True)
        except (EOFError, ValueError, IndexError):
            return
        # An unknown-size master (live-muxed Segment) runs to the end of what we
        # have; treating it otherwise would skip the whole file in one step.
        unknown = size >= (1 << 56) - 1
        body_end = end if unknown else min(p + size, end)
        if eid in _MASTERS:
            _walk_ebml(buf, p, body_end, out, depth + 1)
        else:
            out.append((eid, buf[p:body_end]))
        if body_end <= pos:      # no forward progress ⇒ malformed; bail
            return
        pos = body_end


def parse_dv_record(rec: bytes) -> Optional[dict]:
    """Decode a `DOVIDecoderConfigurationRecord` (identical in MKV and MP4).

        u8  dv_version_major
        u8  dv_version_minor
        u7  dv_profile
        u6  dv_level
        u1  rpu_present_flag
        u1  el_present_flag
        u1  bl_present_flag
        u4  dv_bl_signal_compatibility_id
    """
    if not rec or len(rec) < 5:
        return None
    packed = (rec[2] << 8) | rec[3]
    return {
        "profile":  packed >> 9,
        "level":   (packed >> 3) & 0x3F,
        "rpu":     (packed >> 2) & 1,
        "el":      (packed >> 1) & 1,
        "bl":       packed & 1,
        "compat":   rec[4] >> 4,
    }


def _probe_matroska(buf: bytes) -> dict:
    out: list = []
    _walk_ebml(buf, 0, len(buf), out)
    info: dict = {"container": "mkv"}
    saw_dv_type = False
    for eid, body in out:
        if eid == _ID_CODECID and "codec" not in info:
            info["codec"] = body.decode("ascii", "replace").strip("\x00")
        elif eid == _ID_PIXELWIDTH and "width" not in info:
            info["width"] = _uint(body)
        elif eid == _ID_PIXELHEIGHT and "height" not in info:
            info["height"] = _uint(body)
        elif eid == _ID_BITDEPTH and "bit_depth" not in info:
            info["bit_depth"] = _uint(body)
        elif eid == _ID_MATRIX:
            info["matrix"] = _uint(body)
        elif eid == _ID_TRANSFER:
            info["transfer"] = _uint(body)
        elif eid == _ID_PRIMARIES:
            info["primaries"] = _uint(body)
        elif eid == _ID_ADDIDTYPE and _uint(body) in _DV_ADDID_TYPES:
            saw_dv_type = True
        elif eid == _ID_ADDIDEXTRA and "dv" not in info:
            rec = parse_dv_record(body)
            if rec:
                info["dv"] = rec
    # BlockAdditionMapping is also used for non-DV things (e.g. opaque track
    # additions); only trust the record when the type said Dolby Vision.
    if not saw_dv_type:
        info.pop("dv", None)
    return info


def _probe_mp4(head: bytes, tail: bytes) -> dict:
    """MP4/MOV: locate the DV config box by FourCC.

    A full box walk buys nothing here — `dvcC`/`dvvC` appears only inside a
    video sample entry, and the 5 bytes that follow the FourCC are the record
    regardless of how deeply it is nested. `moov` is checked in the tail too,
    since a non-faststart mux puts it at the end of the file.
    """
    info: dict = {"container": "mp4"}
    for blob in (head, tail):
        if not blob:
            continue
        for box in _MP4_DV_BOXES:
            i = blob.find(box)
            if i >= 0:
                rec = parse_dv_record(blob[i + 4:i + 9])
                if rec:
                    info["dv"] = rec
                    break
        if "codec" not in info:
            for fourcc in _MP4_DV_CODECS + (b"hvc1", b"hev1", b"avc1", b"av01"):
                if blob.find(fourcc) >= 0:
                    info["codec"] = fourcc.decode("ascii")
                    break
        if "dv" in info:
            break
    return info


def probe_path(path: str) -> Optional[dict]:
    """Probe one media file. Returns None when it can't be read or understood.

    The shape is deliberately small and JSON-safe — it is persisted per file in
    `library.json` (see docs/LIBRARY_DATA.md § video):

        {"container", "codec", "width", "height", "bit_depth",
         "matrix", "transfer", "primaries",
         "dv_profile", "dv_level", "dv_compat", "dv_el",
         "green": bool}
    """
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            head = fh.read(HEAD_BYTES)
            tail = b""
            if size > HEAD_BYTES * 2:
                fh.seek(max(0, size - HEAD_BYTES))
                tail = fh.read(HEAD_BYTES)
    except OSError:
        return None
    if not head:
        return None

    if head[:4] == _EBML_MAGIC:
        raw = _probe_matroska(head)
    else:
        raw = _probe_mp4(head, tail)

    dv = raw.pop("dv", None)
    out = {k: v for k, v in raw.items() if v not in (None, "")}
    if dv:
        out["dv_profile"] = dv["profile"]
        out["dv_level"]   = dv["level"]
        out["dv_compat"]  = dv["compat"]
        out["dv_el"]      = dv["el"]
    out["green"] = is_green(out)
    return out


def is_green(info: Optional[dict]) -> bool:
    """True when this file will render with the Dolby Vision green cast.

    Narrow on purpose. Profile 5 is the only profile whose base layer is not
    directly displayable; flagging any DV at all would condemn every perfectly
    fine P8.1 release and make the warning noise. `compat` is checked too rather
    than assumed — a non-zero compatibility id means the base layer *is* usable
    even if the profile byte says 5, and the file should not be condemned on a
    technicality.
    """
    if not info:
        return False
    prof = info.get("dv_profile")
    if prof is None:
        return False
    return int(prof) == 5 and int(info.get("dv_compat", 0) or 0) == 0


def describe(info: Optional[dict]) -> str:
    """One human sentence for the UI tooltip, or "" when there's nothing to say."""
    if not is_green(info):
        return ""
    return ("This release is Dolby Vision Profile 5, which has no standard "
            "fallback — it plays with a heavy green tint in VLC and on device. "
            "Replacing it with a non-Dolby-Vision release is the fix.")


# ── Search-side heuristic ────────────────────────────────────────────────────
# There is no file to read when ranking search results, so titles are all there
# is. Kept deliberately weak and used only to BREAK TIES: it never rejects a
# release outright, because "DV" in a name is just as likely to be a healthy
# P8.1 as a P5, and the post-download probe above is what actually decides.
_DV_TITLE_RE   = re.compile(r"\b(?:DV|DoVi|DoVI|Dolby[\s._-]?Vision)\b", re.I)
# A release that also advertises an HDR10/HLG layer is announcing a compatible
# base layer (profile 8.x), which is exactly the safe case.
_DV_SAFE_RE    = re.compile(r"\bHDR10\+?\b|\bHLG\b|\bSDR\b", re.I)


def title_dv_risk(title: str) -> bool:
    """True when a release *title* suggests Dolby Vision with no compatible base
    layer. Advisory only — see the module note on why titles can't be trusted."""
    if not title or not _DV_TITLE_RE.search(title):
        return False
    return not _DV_SAFE_RE.search(title)
