"""Unit tests for `relquality.py`. Run with plain python, no deps:

    python tests/test_relquality.py      (or `make test`)

There is no test runner in this repo and this file deliberately does not add
one: `relquality` is pure, so a list of cases and a counter is the whole job.
Every case below is either a real indexer title or a bitrate arithmetic check
that a future tweak to the bands could otherwise break silently.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import relquality as rq          # noqa: E402

_PASS = 0
_FAIL = []


def eq(name, got, want):
    global _PASS
    if got == want:
        _PASS += 1
    else:
        _FAIL.append("%s\n     got:  %r\n     want: %r" % (name, got, want))


def ok(name, cond, detail=""):
    global _PASS
    if cond:
        _PASS += 1
    else:
        _FAIL.append("%s%s" % (name, ("\n     " + detail) if detail else ""))


GB = 1000 ** 3
MB = 1000 ** 2


# 1. Parse table - real indexer titles. (title, height, source, codec)
PARSE_CASES = [
    ("Show.S04E02.2160p.WEB-DL.DDP5.1.HDR.H.265-NTb", 2160, "webdl", "hevc"),
    ("Movie.2019.1080p.BluRay.REMUX.AVC.DTS-HD.MA-FGT", 1080, "remux", "h264"),
    ("Movie.2019.1080p.BRRip.x264-YIFY", 1080, "bluray", "h264"),
    ("[SubsPlease] Show - 12 (1080p) [ABCD1234].mkv", 1080, "", ""),
    ("Show S01 COMPLETE 720p HDTV x264-GROUP", 720, "hdtv", "h264"),
    ("Movie.2024.HDTS.1080p.x264", 1080, "cam", "h264"),
    ("Movie.2160p.UHD.BluRay.x265.10bit.HDR-TERMINAL", 2160, "bluray", "hevc"),
    ("Show.S01E01.WEBRip.XviD-FUM", 0, "webrip", "xvid"),
    ("Movie 4K WEB DL AV1", 2160, "webdl", "av1"),
    ("Movie.2020.DVDRip.XviD", 0, "dvd", "xvid"),
    ("Movie.2021.720p.WEB.h264-GROUP", 720, "webdl", "h264"),
    ("Movie.2018.1080p.WEB-DL.DD5.1.H264-FGT", 1080, "webdl", "h264"),
    ("Show.S02E05.480p.x264-mSD", 480, "", "h264"),
    ("Movie.2022.2160p.WEB-DL.DV.HDR10.HEVC-GROUP", 2160, "webdl", "hevc"),
    ("Show.S03E04.1080p.AMZN.WEB-DL.DDP2.0.H.264-NTb", 1080, "webdl", "h264"),
    ("Movie.1998.576p.PAL.DVD.MPEG-2", 576, "dvd", "mpeg2"),
    ("Some.Film.2023.1440p.WEB-DL.x265", 1440, "webdl", "hevc"),
]
for _title, _h, _s, _c in PARSE_CASES:
    _p = rq.parse(_title)
    eq("parse height  %r" % _title, _p["height"], _h)
    eq("parse source  %r" % _title, _p["source"], _s)
    eq("parse codec   %r" % _title, _p["codec"], _c)

eq("10bit flag", rq.parse("Movie.2160p.BluRay.x265.10bit-X")["bit10"], True)
eq("hdr flag", rq.parse("Movie.2160p.WEB-DL.HDR10.HEVC-X")["hdr"], True)
eq("repack flag", rq.parse("Show.S01E01.REPACK.1080p.WEB-DL-X")["repack"], True)


# 2. The TS footgun. A group called TSuRRouNDeD is not a telesync, and a bare
#    TS beside a real source token is the group name, not the source.
eq("TS group is not cam",
   rq.parse("Movie.2024.1080p.WEB-DL.x264-TSuRRouNDeD")["source"], "webdl")
eq("HDTS really is cam", rq.parse("Movie.2024.HDTS.x264")["source"], "cam")
eq("bare TS with no other source IS cam",
   rq.parse("Movie.2024.TS.XviD")["source"], "cam")
eq("bare TS beside WEB-DL is the group, not cam",
   rq.parse("Movie.2024.1080p.WEB-DL.x264-TS")["source"], "webdl")


# 3. REMUX outranks the BluRay token that always sits beside it.
eq("remux beats bluray",
   rq.parse("Movie.2019.1080p.BluRay.REMUX.AVC-FGT")["source"], "remux")


# 4. THE HEADLINE TEST. An efficient HEVC encode must not be demoted for being
#    efficient -- both of these are honest 1080p WEB-DLs.
_hevc = rq.score("Movie.2023.1080p.WEB-DL.x265-GRP", 3.5 * GB, 90, 1)
_h264 = rq.score("Movie.2023.1080p.WEB-DL.x264-GRP", 6.0 * GB, 90, 1)
eq("test_hevc_not_punished (hevc verdict)", _hevc["verdict"], "ok")
eq("test_hevc_not_punished (h264 verdict)", _h264["verdict"], "ok")
eq("test_hevc_not_punished (hevc keeps 1080)", _hevc["effective_height"], 1080)
eq("test_hevc_not_punished (h264 keeps 1080)", _h264["effective_height"], 1080)


# 5. Demotion fires on a release lying about its tier.
_liar = rq.score("Movie.2024.2160p.WEB-DL.x264", 1.4 * GB, 120, 1)
eq("demotion fires (verdict)", _liar["verdict"], "demote")
ok("demotion lands below 1080", _liar["effective_height"] <= 720,
   "effective_height=%s" % _liar["effective_height"])
ok("demotion is visible in the label", "listed 2160p" in _liar["label"],
   _liar["label"])


# 6. ...and does not over-fire on a lean but legitimate encode.
_lean = rq.score("Show.S01E01.1080p.WEB-DL.x265-GRP", 900 * MB, 45, 1)
eq("lean hevc episode is ok", _lean["verdict"], "ok")
eq("lean hevc keeps its tier", _lean["effective_height"], 1080)


# 7. Unknown runtime never demotes - the name stands.
_nort = rq.score("Movie.2024.2160p.WEB-DL.x264", 1.4 * GB, 0, 1)
eq("no runtime -> unknown", _nort["verdict"], "unknown")
eq("no runtime keeps the claim", _nort["effective_height"], 2160)
eq("no runtime is bitrate-neutral", _nort["bpm_position"], 0.5)
eq("no size -> unknown",
   rq.score("Movie.1080p.WEB-DL.x264", 0, 90, 1)["verdict"], "unknown")


# 8. Season packs. The divisor is the whole ballgame.
_pack_ok = rq.score("Show.S01.COMPLETE.1080p.WEB-DL.x264-GRP", 22 * GB, 45, 10)
eq("season pack with the right count is ok", _pack_ok["verdict"], "ok")
_pack_bad = rq.score("Show.S01.COMPLETE.1080p.WEB-DL.x264-GRP", 22 * GB, 45, 1)
eq("season pack counted as one episode is suspect, not promoted",
   _pack_bad["verdict"], "suspect_oversize")
eq("suspect_oversize NEVER promotes", _pack_bad["effective_height"], 1080)


# 9. An unknown episode count must route to `unknown`, never be treated as 1.
eq("episode_count 0 -> unknown",
   rq.score("Show.S01.1080p.WEB-DL.x264", 22 * GB, 45, 0)["verdict"], "unknown")


# 10. Ceiling semantics. Deliberately locked down: what matters is the
#     resolution you actually GET, so a demoted 2160p IS eligible under 1080p.
eq("2160p is not hq_eligible under a 1080 ceiling",
   rq.score("M.2160p.WEB-DL.x265", 0, 0, 1, ceiling=1080)["hq_eligible"], False)
eq("1080p is hq_eligible under a 1080 ceiling",
   rq.score("M.1080p.WEB-DL.x265", 0, 0, 1, ceiling=1080)["hq_eligible"], True)
eq("a 2160p DEMOTED to 720p is hq_eligible under a 1080 ceiling",
   rq.score("Movie.2024.2160p.WEB-DL.x264", 1.4 * GB, 120, 1,
            ceiling=1080)["hq_eligible"], True)
eq("2160p is hq_eligible under a 2160 ceiling",
   rq.score("M.2160p.WEB-DL.x265", 0, 0, 1, ceiling=2160)["hq_eligible"], True)


# 11. Ordering.
def rk(title, size=0, rt=0, ec=1, seeders=0):
    return rq.rank_key(rq.score(title, size, rt, ec, seeders=seeders))


ok("1080p BluRay > 1080p WEBRip",
   rk("M.1080p.BluRay.x264") > rk("M.1080p.WEBRip.x264"))
ok("1080p WEBRip > 720p HDTV",
   rk("M.1080p.WEBRip.x264") > rk("M.720p.HDTV.x264"))
ok("720p HDTV > 720p CAM",
   rk("M.720p.HDTV.x264") > rk("M.720p.CAM.x264"))
ok("a demoted 2160p sorts below a genuine 1080p",
   rk("Movie.2024.2160p.WEB-DL.x264", 1.4 * GB, 120)
   < rk("Movie.2024.1080p.WEB-DL.x264", 5 * GB, 120))
ok("seeders only break an otherwise exact tie",
   rk("M.1080p.WEB-DL.x264", seeders=500) > rk("M.1080p.WEB-DL.x264", seeders=5))


# 12. is_upgrade - strictly a resolution question.
def sc(title, size=0, rt=0, ec=1):
    return rq.score(title, size, rt, ec)


ok("720p HDTV -> 1080p WEB-DL is an upgrade",
   rq.is_upgrade(sc("M.720p.HDTV.x264"), sc("M.1080p.WEB-DL.x264")))
ok("1080p -> 1080p is not an upgrade",
   not rq.is_upgrade(sc("M.1080p.WEB-DL.x264"), sc("M.1080p.WEB-DL.x265")))
ok("1080p BluRay -> 1080p WEBRip is not an upgrade",
   not rq.is_upgrade(sc("M.1080p.BluRay.x264"), sc("M.1080p.WEBRip.x264")))
ok("a demoted 2160p -> a real 1080p is an upgrade",
   rq.is_upgrade(sc("Movie.2024.2160p.WEB-DL.x264", 1.4 * GB, 120),
                 sc("Movie.2024.1080p.WEB-DL.x264", 5 * GB, 120)))
ok("is_upgrade tolerates junk", rq.is_upgrade(None, None) is False)


# 13. Robustness. This runs inside a download-race tick, where raising would
#     strand live torrents, so every entry point must survive anything.
for _junk in ("", None, 12345, [], {}, "x" * 4000, "2160p" * 50,
              "ダンダダン 1080p", "-", "()[]__..",
              "Movie.2024.1080p" + chr(0) + ".x264"):
    try:
        _d = rq.score(_junk, 1 * GB, 45, 1)
        ok("score(%.20r) returns a dict" % (_junk,), isinstance(_d, dict))
        ok("score(%.20r) has a label" % (_junk,), isinstance(_d.get("label"), str))
        rq.rank_key(_d)
        rq.label(_d)
        _PASS += 1
    except Exception as _exc:
        _FAIL.append("score(%r) raised %r" % (_junk, _exc))

try:
    rq.parse(None)
    rq.expected_bpm("nonsense", None, None)
    rq.expected_bpm(1092, "hevc", "webdl")      # odd height snaps to a rung
    rq.cross_check({}, "x", "y", "z")
    rq.infer_height(-1, "", "")
    rq.rank_key("not a dict")
    rq.label(None)
    _PASS += 1
except Exception as _exc:
    _FAIL.append("a helper raised on junk input: %r" % (_exc,))


# 14. Leaf guard - relquality must never drag `main` (or any dep) in with it.
ok("relquality is a leaf module (no main import)", "main" not in sys.modules)
ok("relquality imports no third-party deps",
   not any(m.startswith(("fastapi", "httpx", "pydantic", "uvicorn"))
           for m in sys.modules))


# ---------------------------------------------------------------- report
if _FAIL:
    print("FAILED %d of %d" % (len(_FAIL), _PASS + len(_FAIL)))
    for _f in _FAIL:
        print("  - " + _f)
    sys.exit(1)
print("OK %d/%d" % (_PASS, _PASS))
