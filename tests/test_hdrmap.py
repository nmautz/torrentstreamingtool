"""Unit tests for `hdrmap.py` - HDR sources in an SDR bundle.

    python tests/test_hdrmap.py      (or `make test`)

No ffmpeg. The init segment below is built to the shape of the one a Chromecast
refused (Project Hail Mary, 2026-10-09): an `avc1` entry holding `avcC`, a `colr`
that says BT.2020 / PQ, then `clli` and `mdcv`.
"""

import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hdrmap as hm          # noqa: E402

_PASS = 0
_FAIL = []


def ok(name, cond, detail=""):
    global _PASS
    if cond:
        _PASS += 1
    else:
        _FAIL.append("%s%s" % (name, ("\n     " + detail) if detail else ""))


def eq(name, got, want):
    ok(name, got == want, "got %r, want %r" % (got, want))


def box(typ, body=b""):
    return struct.pack(">I4s", 8 + len(body), typ) + body


def init_segment(colr=(9, 16, 9), hdr=True, entry=b"avc1"):
    """A minimal fmp4 init: ftyp + moov/trak/mdia/minf/stbl/stsd/<entry>."""
    kids = box(b"avcC", b"\x01\x64\x00\x29" + b"\x00" * 20)
    if colr:
        kids += box(b"colr", b"nclx" + struct.pack(">HHH", *colr) + b"\x00")
    if hdr:
        kids += box(b"clli", b"\x03\xe8\x01\x90")
        kids += box(b"mdcv", b"\x00" * 24)
    kids += box(b"pasp", struct.pack(">II", 1, 1))
    sample = box(entry, b"\x00" * 78 + kids)
    stsd = box(b"stsd", b"\x00" * 4 + struct.pack(">I", 1) + sample)
    stbl = box(b"stbl", stsd + box(b"stts", b"\x00" * 8))
    moov = box(b"moov", box(b"mvhd", b"\x00" * 100) + box(b"trak", box(b"tkhd", b"\x00" * 84)
               + box(b"mdia", box(b"mdhd", b"\x00" * 24) + box(b"minf", stbl))))
    return box(b"ftyp", b"iso5\x00\x00\x02\x00iso5iso6mp41") + moov


PQ = {"color_transfer": "smpte2084", "color_primaries": "bt2020", "color_space": "bt2020nc"}
HLG = {"color_transfer": "arib-std-b67", "color_primaries": "bt2020", "color_space": "bt2020nc"}
SDR = {"color_transfer": "bt709", "color_primaries": "bt709", "color_space": "bt709"}


# ── which sources are HDR ────────────────────────────────────────────────────
eq("PQ is HDR", hm.transfer(PQ), "smpte2084")
eq("HLG is HDR", hm.transfer(HLG), "arib-std-b67")
eq("BT.709 is not", hm.transfer(SDR), "")
eq("an untagged source is not", hm.transfer({"color_transfer": ""}), "")
eq("a probe with no colour keys is not", hm.transfer({"codec": "hevc"}), "")
eq("no video at all is not", hm.transfer(None), "")
eq("case does not matter", hm.transfer({"color_transfer": "SMPTE2084"}), "smpte2084")
# 10-bit alone is not HDR: an SDR anime encode is yuv420p10le and BT.709.
eq("10-bit SDR is not HDR", hm.transfer({"pix_fmt": "yuv420p10le", "color_transfer": "bt709"}), "")

eq("HDR + filters -> tonemap", hm.mode(PQ, True), hm.TONEMAP)
eq("HDR, no filters -> retag", hm.mode(PQ, False), hm.RETAG)
eq("SDR -> nothing", hm.mode(SDR, True), "")
eq("SDR, no filters -> nothing", hm.mode(SDR, False), "")

# ── the filter chain ─────────────────────────────────────────────────────────
c = hm.chain(PQ, hm.TONEMAP)
ok("tonemap chain maps", "tonemap=tonemap=mobius" in c and "zscale=t=linear:npl=100" in c, c)
ok("tonemap chain ends 8-bit 4:2:0", "format=yuv420p,sidedata=mode=delete" in c, c)
ok("tonemap chain states its input", c.startswith(
    "setparams=color_primaries=bt2020:color_trc=smpte2084:colorspace=bt2020nc,"), c)
ok("source rung is not scaled", "scale=-2" not in c, c)

c = hm.chain(PQ, hm.TONEMAP, 480)
ok("a down-rung scales BEFORE it maps", c.startswith("scale=-2:480,setparams="), c)

c = hm.chain(HLG, hm.TONEMAP)
ok("HLG is read as HLG", "color_trc=arib-std-b67" in c, c)

# A source that names only its transfer: zscale would stop with "no path between
# colorspaces" on the unspecified matrix, so the chain supplies BT.2020.
c = hm.chain({"color_transfer": "smpte2084"}, hm.TONEMAP)
ok("missing primaries and matrix are filled in",
   "color_primaries=bt2020:color_trc=smpte2084:colorspace=bt2020nc" in c, c)
c = hm.chain({"color_transfer": "smpte2084", "color_primaries": "unknown",
              "color_space": "reserved"}, hm.TONEMAP)
ok("values zscale cannot read are replaced",
   "color_primaries=bt2020:" in c and "colorspace=bt2020nc" in c, c)

c = hm.chain(PQ, hm.RETAG, 720)
eq("retag chain", c, "scale=-2:720,setparams=color_primaries=bt709:color_trc=bt709:"
                     "colorspace=bt709,sidedata=mode=delete")
ok("retag does not map", "tonemap" not in c and "zscale" not in c, c)

eq("output tags name the stream", hm.out_tags(2),
   ["-color_primaries:v:2", "bt709", "-color_trc:v:2", "bt709", "-colorspace:v:2", "bt709"])

# ── what an init segment claims ──────────────────────────────────────────────
bad = init_segment()
m = hm.init_marks(bad)
eq("reads colr", m["colr"], (9, 16, 9))
eq("finds both HDR boxes", sorted(m["hdr_boxes"]), ["clli", "mdcv"])
ok("that header claims HDR", hm.claims_hdr(m))

good = init_segment(colr=(1, 1, 1), hdr=False)
m = hm.init_marks(good)
eq("BT.709 colr", m["colr"], (1, 1, 1))
eq("no HDR boxes", m["hdr_boxes"], [])
ok("that header does not", not hm.claims_hdr(m))

ok("PQ colr alone claims HDR", hm.claims_hdr(hm.init_marks(init_segment(hdr=False))))
ok("HLG colr claims HDR", hm.claims_hdr(hm.init_marks(init_segment(colr=(9, 18, 9), hdr=False))))
ok("boxes alone claim HDR", hm.claims_hdr(hm.init_marks(init_segment(colr=(1, 1, 1)))))
ok("no colr, no boxes: no claim", not hm.claims_hdr(hm.init_marks(init_segment(colr=None, hdr=False))))

# ── scrubbing ────────────────────────────────────────────────────────────────
fixed, removed = hm.scrub_init(bad)
eq("removes both", sorted(removed), ["clli", "mdcv"])
eq("length unchanged", len(fixed), len(bad))
eq("no HDR boxes left", hm.init_marks(fixed)["hdr_boxes"], [])
eq("colr untouched", hm.init_marks(fixed)["colr"], (9, 16, 9))
eq("only the two type fields changed",
   sum(1 for a, b in zip(bad, fixed) if a != b), 8)
ok("the boxes became free", fixed.count(b"free") == 2 and b"mdcv" not in fixed and b"clli" not in fixed)
ok("the rest of the entry still parses", b"pasp" in fixed and b"avcC" in fixed)

same, removed = hm.scrub_init(good)
ok("nothing to do returns the same object", same is good and removed == [])
again, removed = hm.scrub_init(fixed)
ok("scrubbing twice is a no-op", again is fixed and removed == [])

fixed, removed = hm.scrub_init(init_segment(entry=b"hvc1"))
eq("an hvc1 entry is read too", sorted(removed), ["clli", "mdcv"])

# An audio init has an `mp4a` entry: nothing in it is a visual sample entry, and
# the bytes "mdcv" appearing as DATA elsewhere must not be rewritten.
_mp4a = box(b"mp4a", b"\x00" * 28 + b"mdcv" + b"\x00" * 8)
_stsd = box(b"stsd", b"\x00" * 4 + struct.pack(">I", 1) + _mp4a)
audio = box(b"ftyp", b"iso5") + box(b"moov", box(b"trak", box(b"mdia", box(
    b"minf", box(b"stbl", _stsd)))))
same, removed = hm.scrub_init(audio)
ok("an audio init is left alone", same is audio and removed == [])

# Garbage in must not raise: a truncated or non-MP4 file is simply not scrubbed.
for junk in (b"", b"\x00" * 7, b"not an mp4 at all", bad[:60], b"\xff" * 64):
    try:
        out, removed = hm.scrub_init(junk)
        ok("junk is returned unchanged", out == junk and removed == [])
        hm.init_marks(junk)
    except Exception as exc:            # noqa: BLE001
        ok("junk does not raise", False, repr(exc))

if _FAIL:
    print("FAILED %d of %d:" % (len(_FAIL), _PASS + len(_FAIL)))
    for f in _FAIL:
        print("  - " + f)
    sys.exit(1)
print("hdrmap: %d passed" % _PASS)
