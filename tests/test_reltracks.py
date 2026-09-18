"""Unit tests for `reltracks.py`. Run with plain python, no deps:

    python tests/test_reltracks.py      (or `make test`)

Same shape as `test_relquality.py` — a list of cases and a counter, because the
module is pure and that is the whole job.

Most of what is locked here is **older than the module**. The audio classifier
lived in `main.py` until 17.3.0, where it could not be tested at all (importing
`main` needs fastapi, and `make test` runs with no deps), so the four context
rules documented in docs/GOTCHAS.md have been load-bearing and unverified for
several releases. They are now read by a second consumer (`track_rank`), which
makes the cost of breaking one of them twice what it was. Every case below is a
real indexer title shape.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import reltracks as rt           # noqa: E402

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
        _FAIL.append(name + (("  " + detail) if detail else ""))


def case(name, title, audio, lang, tracks):
    """One title, all three answers — which is also the contract that they come
    off ONE parse. `analyse` is what `main` calls."""
    eq(name, rt.analyse(title), (audio, lang, tracks))


# ── 1. The four GOTCHAS context rules ────────────────────────────────────────
# These are the traps. A regression in any of them is a user downloading a
# language nobody in the house speaks.

# 1a. Erai-raws: the bracketed list describes SUBTITLES. The classic failure is
# reading [ENG][POR-BR][RUS] as audio and calling this an English release.
case("Erai-raws multi-sub batch is original audio + subs",
     "[Erai-raws] Sousou no Frieren - 01 [1080p][Multiple Subtitle][ENG][POR-BR][RUS]",
     "sub", "", 1)
ok("...and its language list does NOT count as audio tracks",
   rt.analyse("[Erai-raws] Show - 01 [Multiple Subtitle][ENG][POR-BR][RUS]")[2] == 1)

# 1b. Tsundere-Raws: a known French group whose MULTi is FR+original, no English.
case("Tsundere-Raws MULTi is French, not dual",
     "[Tsundere-Raws] Boku no Hero - 12 VOSTFR [MULTi][1080p]", "other", "French", 0)

# 1c. A foreign tag fused to "dub" must not read as an English dub.
case("ArabicDub is not an English dub",
     "Attack.on.Titan.S01.1080p.ArabicDub.WEB-DL", "other", "Arabic", 0)
case("Korean Dub is not an English dub",
     "Some.Show.S02.720p.Korean Dub.HDTV", "other", "Korean", 0)

# 1d. "ENG SUBS" is a subtitle claim, never evidence of English audio.
case("ENG SUBS is not English audio",
     "[SubsPlease] Dandadan - 05 (1080p) [Eng Subs]", "sub", "", 0)


# ── 2. The audio classes ─────────────────────────────────────────────────────
case("plain English content has no markers at all",
     "Breaking.Bad.S05E01.1080p.BluRay.x264-GROUP", "", "", 0)
case("untagged fansub is original audio (no claim either way)",
     "[SubsPlease] Frieren - 12 (1080p) [A1B2C3D4].mkv", "", "", 0)
case("explicit Dual-Audio",
     "[Judas] Hunter x Hunter (2011) - 063 [1080p][HEVC][Dual-Audio]", "dual", "", 2)
case("bare DUAL",
     "Show.S01.1080p.BluRay.DUAL.x265-GRP", "dual", "", 2)
case("bare MULTi with no language context is English-facing multi-audio",
     "[ToonsHub] Solo Leveling S01 1080p MULTi AAC", "dual", "", 2)
case("English token beside a foreign one is dual",
     "Movie.2024.1080p.WEB-DL.ENG-ITA.x264", "dual", "Italian", 2)
case("English dub, no other language tag",
     "One.Piece.S01.1080p.Dubbed.WEB-DL", "dub", "", 0)
case("Multi-Subs is subtitles only, never audio",
     "Show.S01.1080p.BluRay.Multi-Subs.x265", "sub", "", 1)


# ── 3. Richness arithmetic ───────────────────────────────────────────────────
ok("audio outweighs subs (2 vs 1)",
   rt.analyse("Show.S01.DUAL.x265")[2] > rt.analyse("Show.S01.Multi-Subs.x265")[2])
case("both axes score 3",
     "[Judas] Show S01 [1080p][Dual-Audio][Multi-Subs]", "dual", "", 3)
case("two foreign languages with no English is a real two-track release",
     "Film.2023.1080p.BluRay.German.Spanish.x264", "other", "German/Spanish", 2)
ok("one language spelled two ways is ONE language, not a richness bonus",
   rt.analyse("Film.2023.TrueFrench.French.1080p")[2] == 0)
ok("a single sub track never scores",
   rt.analyse("Show.S01.1080p.Subbed.x264")[2] == 0)
ok("richness is 0 whenever the title says nothing",
   rt.analyse("Show.S01E01.1080p.WEB-DL.x264-GRP")[2] == 0)


# ── 4. The one-parse contract ────────────────────────────────────────────────
# Both readers must agree because they were handed the same facts. A future
# refactor that gives `track_rank` its own regex sweep breaks this silently, so
# assert the seam itself rather than only its outputs.
_t = "[Erai-raws] Show - 01 [Multiple Subtitle][ENG][RUS]"
_f = rt.lang_facts(_t)
_a, _l = rt.classify_audio(_t, _f)
eq("classify_audio(title, facts) == classify_audio(title)",
   (_a, _l), rt.classify_audio(_t))
eq("analyse is the two readers over one fact set",
   rt.analyse(_t), (_a, _l, rt.track_rank(_f, _a)))
ok("sub_marked is what separates a subs list from an audio list",
   _f["sub_marked"] and _f["multi_sub"] and not _f["dual"])


# ── 5. Never raises ──────────────────────────────────────────────────────────
for _bad in (None, 123, "", [], {}):
    ok("analyse(%r) returns a well-formed triple" % (_bad,),
       rt.analyse(_bad) == ("", "", 0))
ok("track_rank survives a non-dict", rt.track_rank(None, "dual") == 0)


# ── 6. Leaf guard ────────────────────────────────────────────────────────────
ok("reltracks is a leaf module (no main import)", "main" not in sys.modules)
ok("reltracks imports no third-party deps",
   not any(m.startswith(("fastapi", "httpx", "pydantic", "uvicorn"))
           for m in sys.modules))


# ---------------------------------------------------------------- report
if _FAIL:
    print("FAILED %d of %d" % (len(_FAIL), _PASS + len(_FAIL)))
    for _f in _FAIL:
        print("  - " + _f)
    sys.exit(1)
print("OK %d/%d" % (_PASS, _PASS))
