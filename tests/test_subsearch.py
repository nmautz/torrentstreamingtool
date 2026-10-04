"""Unit tests for `subsearch.py` (and the numpy-free parts of `subsync.py`).
Run with plain python, no deps:

    python tests/test_subsearch.py      (or `make test`)

Every result dict below is the shape rest.opensubtitles.org actually returns
(trimmed to the fields used), taken from real responses captured by the eval
kit in tests/subs_eval/. The URL cases are the ones that silently returned
nothing before 17.7.0 — the legacy API 302s any non-canonical path to host `_`.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import subsearch as ss           # noqa: E402

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
        _FAIL.append("%s %s" % (name, detail))


API = ss.LEGACY_API

# ── URLs: canonical or nothing ──────────────────────────────────────────────
eq("imdb zero-padded to 7, params alphabetical",
   ss.legacy_url(sublanguageid="eng", season=1, imdbid="tt0994314", episode=1),
   API + "/episode-1/imdbid-0994314/season-1/sublanguageid-eng")
eq("8-digit imdb left alone",
   ss.legacy_url(imdbid="tt11815682", sublanguageid="eng"),
   API + "/imdbid-11815682/sublanguageid-eng")
eq("query lower-cased, + for spaces, apostrophe dropped",
   ss.legacy_url(query="Widow's Bay", season=1, episode=2, sublanguageid="eng"),
   API + "/episode-2/query-widows+bay/season-1/sublanguageid-eng")
eq("brackets/punctuation from an anime title",
   ss.norm_title("【OSHI NO KO】"), "oshi no ko")
eq("empty language dropped (all languages)",
   ss.legacy_url(imdbid=338564, sublanguageid=""), API + "/imdbid-0338564")
eq("free text canonicalised",
   ss.free_text_url("[Erai-raws] Hunter x Hunter (2011) - 045 [1080p]", "eng"),
   API + "/query-erai+raws+hunter+x+hunter+2011+045+1080p/sublanguageid-eng")

t_tv = {"kind": "tv", "imdb": "tt0877057", "title": "Death Note", "season": 1, "episode": 4,
        "abs_no": 4, "anime": True, "file_name": "Death Note - 01x04 - Pursuit.mkv", "duration": 1369}
urls = ss.search_urls(t_tv, "eng")
eq("tv: imdb then title; abs==episode adds nothing",
   urls, [API + "/episode-4/imdbid-0877057/season-1/sublanguageid-eng",
          API + "/episode-4/query-death+note/season-1/sublanguageid-eng"])
t_hxh = dict(t_tv, title="Hunter x Hunter", imdb="tt2098220", season=2, episode=3, abs_no=61)
ok("anime with a different absolute number also searches by it",
   API + "/episode-61/query-hunter+x+hunter/sublanguageid-eng" in ss.search_urls(t_hxh, "eng"))
eq("hash search goes first when we have one",
   ss.search_urls(dict(t_tv, hash="8e245d9679d31e12", size=12345), "eng")[0],
   API + "/moviebytesize-12345/moviehash-8e245d9679d31e12/sublanguageid-eng")
eq("movie: imdb only, no season/episode",
   ss.search_urls({"kind": "movie", "imdb": "tt0338564", "title": "Infernal Affairs"}, "eng")[0],
   API + "/imdbid-0338564/sublanguageid-eng")


# ── filtering ───────────────────────────────────────────────────────────────
def res(**kw):
    base = {"MatchedBy": "imdbid", "SeriesIMDBParent": "877057", "IDMovieImdb": "885243",
            "SeriesSeason": "1", "SeriesEpisode": "4", "MovieName": '"Death Note" Pursuit',
            "SubFormat": "srt", "SubDownloadsCnt": "1000", "SubLastTS": "00:22:30",
            "MovieReleaseName": "Death Note 04", "SubFileName": "Death Note 04.srt",
            "IDSubtitleFile": "1", "SubHash": "a"}
    base.update(kw)
    return base


ok("right show + episode", ss.is_match(t_tv, res()))
ok("wrong episode", not ss.is_match(t_tv, res(SeriesEpisode="5")))
ok("other show sharing the words (Death Note 2015 drama)",
   not ss.is_match(t_tv, res(SeriesIMDBParent="4623604")))
ok("forced-only subs are not dialogue", not ss.is_match(t_tv, res(SubForeignPartsOnly="1")))
ok("anime absolute number filed as S1",
   ss.is_match(t_hxh, res(SeriesIMDBParent="2098220", SeriesSeason="1", SeriesEpisode="61")))
ok("…but a real S2E4 is never absolute episode 4 (Vinland Saga S02E04 vs S01E04)",
   not ss.is_match(dict(t_tv, imdb="tt10233448", season=1, episode=4, abs_no=4),
                   res(SeriesIMDBParent="10233448", SeriesSeason="2", SeriesEpisode="4")))
t_noimdb = {"kind": "tv", "imdb": None, "title": "Chihayafuru", "aka": [], "season": 1, "episode": 1}
ok("no IMDb: exact show name required — 'Chihayafuru: Full Circle' is another show",
   not ss.is_match(t_noimdb, res(SeriesIMDBParent="37541615", MovieName='"Chihayafuru: Full Circle" By Chance',
                                 SeriesEpisode="1")))
ok("no IMDb: same show name passes",
   ss.is_match(t_noimdb, res(SeriesIMDBParent="2150751", MovieName='"Chihayafuru" Saku ya kono hana',
                             SeriesEpisode="1")))
t_mv = {"kind": "movie", "imdb": "tt0338564", "title": "Infernal Affairs"}
ok("movie sequel rejected by IMDb id",
   not ss.is_match(t_mv, res(IDMovieImdb="369060", SeriesIMDBParent="0", MovieName="Infernal Affairs II")))
ok("split-CD movie rejected", not ss.is_match(t_mv, res(IDMovieImdb="338564", SubSumCD="2")))

# ── ranking ─────────────────────────────────────────────────────────────────
t_web = {"kind": "tv", "imdb": "tt11815682", "title": "Hacks", "season": 3, "episode": 6, "duration": 1800,
         "file_name": "Hacks.S03E06.1080p.HMAX.WEB-DL.DDP5.1.x264-NTb.mkv"}
same_rel = res(SeriesIMDBParent="11815682", SeriesSeason="3", SeriesEpisode="6",
               MovieReleaseName="Hacks.S03E06.HMAX.WEB-DL.DDP5.1.x264-NTb", SubDownloadsCnt="50",
               SubLastTS="00:29:30", IDSubtitleFile="10", SubHash="h10")
popular_dvd = res(SeriesIMDBParent="11815682", SeriesSeason="3", SeriesEpisode="6",
                  MovieReleaseName="Hacks.S03E06.DVDRip", SubDownloadsCnt="90000",
                  SubLastTS="00:29:30", IDSubtitleFile="11", SubHash="h11")
too_long = res(SeriesIMDBParent="11815682", SeriesSeason="3", SeriesEpisode="6",
               MovieReleaseName="Hacks.S03E06.WEB", SubLastTS="00:31:30", IDSubtitleFile="12", SubHash="h12")
r = ss.rank(t_web, [popular_dvd, too_long, same_rel])
eq("same release beats more downloads; runs-past-the-end last",
   [x["IDSubtitleFile"] for x in r], ["10", "11", "12"])
eq("duplicate content (same SubHash) listed once",
   len(ss.rank(t_web, [same_rel, dict(same_rel, IDSubtitleFile="99")])), 1)
ok("MicroDVD .sub and PAL penalised",
   ss.score(t_web, dict(same_rel, SubFormat="sub")) < ss.score(t_web, same_rel)
   and ss.score(t_web, dict(same_rel, MovieFPS="25.000")) < ss.score(t_web, same_rel))
eq("hash match always first", ss.rank(t_web, [same_rel, dict(popular_dvd, MatchedBy="moviehash")])[0]["IDSubtitleFile"], "11")

# ── files ───────────────────────────────────────────────────────────────────
SRT = "1\r\n00:00:01,500 --> 00:00:03,000\r\nHello there.\r\n\r\n2\r\n00:01:00,000 --> 00:01:02,250\r\n<i>General</i> Kenobi!\r\n"
ASS = ("[Script Info]\nTitle: x\n\n[V4+ Styles]\nFormat: Name, Fontname\nStyle: Default,Arial\n\n[Events]\n"
       "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
       "Dialogue: 0,0:00:01.50,0:00:03.00,Default,,0,0,0,,{\\i1}Hello{\\i0}, there\n"
       "Dialogue: 0,0:00:05.00,0:00:06.00,Signs,,0,0,0,,{\\pos(10,10)}CAFE\n"
       "Dialogue: 0,0:01:00.00,0:01:02.25,Default,,0,0,0,,General\\NKenobi!\n")
MDVD = "{1}{1}25.000\n{25}{75}Hello there.\n{1500}{1556}General|Kenobi!\n"
eq("sniff srt", ss.sniff_format(SRT), "srt")
eq("sniff ass", ss.sniff_format(ASS), "ass")
eq("sniff microdvd", ss.sniff_format(MDVD), "sub")
eq("sniff vtt", ss.sniff_format("WEBVTT\n\n00:01.000 --> 00:02.000\nx\n"), "vtt")
eq("parse srt", ss.parse_cues(SRT), [(1.5, 3.0, "Hello there."), (60.0, 62.25, "General Kenobi!")])
eq("parse ass drops sign styles, keeps dialogue",
   ss.parse_cues(ASS), [(1.5, 3.0, "Hello, there"), (60.0, 62.25, "General Kenobi!")])
eq("parse microdvd uses its own fps header",
   ss.parse_cues(MDVD), [(1.0, 3.0, "Hello there."), (60.0, 62.24, "General Kenobi!")])
eq("utf-8 BOM stripped", ss.decode(b"\xef\xbb\xbfHi"), "Hi")
eq("cp1252 fallback", ss.decode("caf\xe9".encode("cp1252")), "caf\xe9")

shift = lambda t: t * 1.0 + 2.0      # noqa: E731
srt2, f = ss.retime(SRT, shift)
eq("retime srt keeps format and text", (f, ss.parse_cues(srt2)),
   ("srt", [(3.5, 5.0, "Hello there."), (62.0, 64.25, "General Kenobi!")]))
ok("retime srt keeps the italics markup", "<i>General</i>" in srt2)
ass2, f = ss.retime(ASS, shift)
eq("retime ass stays ass", f, "ass")
ok("retime ass keeps override tags + styles",
   "{\\pos(10,10)}CAFE" in ass2 and "Style: Default,Arial" in ass2 and "0:00:07.00,0:00:08.00,Signs" in ass2)
eq("retime ass shifts dialogue", ss.parse_cues(ass2)[0][:2], (3.5, 5.0))
srt3, f = ss.retime(MDVD, shift)
eq("retime microdvd converts to srt", (f, ss.parse_cues(srt3)[0][:2]), ("srt", (3.0, 5.0)))
ok("retime never produces negative times", "-" not in ss.retime(SRT, lambda t: t - 10)[0].split("-->")[0])

# ── subsync without numpy / audio: pure bits only ────────────────────────────
import subsync                    # noqa: E402
eq("mapper identity when not moving", subsync.mapper({"move": False, "ratio": 2, "offset": 5})(10.0), 10.0)
eq("mapper applies ratio + offset", subsync.mapper({"move": True, "ratio": 1.0, "offset": -3.5})(10.0), 6.5)
if subsync.np is not None:
    import random
    rnd = random.Random(7)
    cues, t = [], 5.0
    while t < 1300:
        d = rnd.uniform(1.0, 4.0)
        cues.append((t, t + d))
        t += d + rnd.uniform(0.5, 6.0)
    # synthetic speech energy: loud under the true lines (shifted +7.3 s), noise elsewhere
    n = 13500
    e = [-50.0 + rnd.uniform(-3, 3) for _ in range(n)]
    for a, b in cues:
        for i in range(int((a + 7.3) / 0.1), min(n, int((b + 7.3) / 0.1) + 1)):
            e[i] = -25.0 + rnd.uniform(-3, 3)
    r = subsync.align(cues, e)
    ok("align finds a +7.3 s shift", r and r["ratio"] == 1.0 and abs(r["offset"] - 7.3) <= 0.1, r)
    ok("…verified and marked to move", r and r["verified"] and r["move"], r)
    rw = subsync.align([(b, b + 1.5) for b in sorted(rnd.uniform(0, 1300) for _ in range(300))], e)
    ok("unrelated timing is not verified", rw and not rw["verified"], rw)
    pal = [(a / (25 / 23.976), b / (25 / 23.976)) for a, b in cues]
    e2 = [-50.0 + rnd.uniform(-3, 3) for _ in range(n)]
    for a, b in cues:
        for i in range(int(a / 0.1), min(n, int(b / 0.1) + 1)):
            e2[i] = -25.0 + rnd.uniform(-3, 3)
    rp = subsync.align(pal, e2)
    ok("PAL-timed subs get the 25/23.976 speed fix", rp and abs(rp["ratio"] - 25 / 23.976) < 1e-6 and rp["move"], rp)
    ok("too little to go on -> None", subsync.align(cues[:5], e) is None)

print(f"{_PASS} passed, {len(_FAIL)} failed")
for f_ in _FAIL:
    print("FAIL:", f_)
sys.exit(1 if _FAIL else 0)
