"""What languages a release carries, and how much choice it gives you.

Two questions about one piece of evidence — a torrent title — and the whole
point of this module is that they are answered off **one parse**:

* **Which language is this?** (`classify_audio`) Anime and foreign content mix
  variants under one show: original audio + subs (SubsPlease/Erai-raws), English
  DUBBED, Dual/Multi-Audio, and foreign dubs (ArabicDub, Korean Dub, VOSTFR,
  LATINO). A seeder sort is blind to this, so a dub household bulk-grabbing a
  season gets a language lottery. This is a **filter**: it answers "can we watch
  this", and the picker applies it before it ranks anything.
* **How many ways can you watch it?** (`track_rank`) A copy carrying two audio
  tracks and three subtitle tracks is a better thing to own than one carrying a
  single track of each, and nothing in a seeder count says so. This is a
  **tiebreak**, sorted below availability — see `_pickCmp` in static/index.html.

**They must never be parsed separately.** A second regex sweep for "does this
look multi-track" loses the context the first pass established, and the context
is the entire difficulty: Erai-raws' ``[Multiple Subtitle] [ENG][POR-BR][RUS]``
is a SUBTITLE list on an original-audio release, and a naive sweep reads it as
three audio tracks. So `lang_facts` extracts once and both readers consume it.
The three traps that shape the ladder, all of them real releases:

* a foreign tag fused to "dub" (``ArabicDub``, ``Korean Dub``) must not read as
  an English dub;
* a multi-SUB marker means the language tokens describe subtitles, not audio;
* ``MULTi`` does NOT imply English — the French scene uses it for FR+original
  (Tsundere-Raws) — while explicit ``Dual/Multi-Audio`` and bare ``DUAL`` do.

Pure and dependency-free (stdlib only), like `relquality.py` and `episodes.py`.
No I/O, no clock, no `main` import.

**Invariant, and it is not decorative:** `episodes.py`'s `_AUDIO_CH_RE` and
`main.py`'s `_strip_release_tags` also match some of these tokens, and they
*discard* them — a channel layout like "EAC3 2 0" once parsed as episode 0, and
stripping ``DUAL``/``MULTi`` is what stops a release tag polluting a TMDb title
match. They run over their own copy of the string and nothing here reads their
output. Do not merge them into this module; they are doing the opposite job.

See docs/GOTCHAS.md - "Audio-language classification" and docs/API.md.
"""

from __future__ import annotations

import re
from typing import Any

# Explicit dual/multi-AUDIO, plus bare "DUAL". All three imply English is one of
# the tracks, which is why they short-circuit the ladder below.
_AUD_DUAL_RE = re.compile(r"\b(?:dual|multi)[ ._-]?audio\b|\bdual\b", re.IGNORECASE)
# Bare "MULTi" — a multi-LANGUAGE scene tag with no promise about which
# languages. The lookahead keeps "Multi-Subs" out of it.
_AUD_MULTI_RE = re.compile(r"\bmulti\b(?![ ._-]?subs?)", re.IGNORECASE)
_AUD_DUB_RE = re.compile(r"\bdub(?:bed|s)?\b", re.IGNORECASE)
# The multi-SUBTITLE marker on its own. Split out of `_AUD_SUB_RE` so "was this
# a *multi*-sub marker, or just 'ENG SUBS'?" is answerable without re-parsing —
# `track_rank` needs that distinction and `classify_audio` does not.
_AUD_MULTI_SUB_RE = re.compile(r"\bmulti(?:ple)?[ ._-]?sub(?:title)?s?\b", re.IGNORECASE)
# Any subtitle marker. "Multiple Subtitle" is the Erai-raws batch form whose
# bracketed language list ([ENG][POR-BR]...) describes SUBS, not audio — it must
# win over those tokens.
_AUD_SUB_RE = re.compile(
    r"\bmulti(?:ple)?[ ._-]?sub(?:title)?s?\b|\bsubbed\b|\beng(?:lish)?[ ._-]?subs?\b",
    re.IGNORECASE)
# English *audio* evidence ("ENG-ITA", "[Eng-Hindi-Tam-Tel]", "AUDIO #1 ENGLISH")
# — checked only after eng-subs phrases are stripped, so "ENG SUBS" never counts.
_AUD_ENG_RE = re.compile(r"\beng(?:lish)?\b", re.IGNORECASE)
_AUD_ENG_SUB_RE = re.compile(r"\beng(?:lish)?[ ._-]?sub(?:title)?s?\b", re.IGNORECASE)
# Release groups whose language is known but untagged in the title.
_AUD_FRENCH_GROUP_RE = re.compile(r"tsundere[ ._-]?raws", re.IGNORECASE)

# Token -> display language for non-English tags. Deliberately conservative —
# only unambiguous full words / well-known scene tags (no "GER"/"SPA"/"POR"
# abbreviations that collide with title words).
_AUD_LANGS = {
    "french": "French", "truefrench": "French", "vf": "French", "vff": "French",
    "vfq": "French", "vostfr": "French subs", "subfrench": "French subs",
    "ita": "Italian", "italian": "Italian",
    "spanish": "Spanish", "castellano": "Spanish", "latino": "Latino",
    "german": "German", "hindi": "Hindi", "tamil": "Tamil", "telugu": "Telugu",
    "russian": "Russian", "rus": "Russian",
    "arabic": "Arabic", "korean": "Korean",
    "portuguese": "Portuguese", "dublado": "Portuguese",
    "polish": "Polish", "lektor": "Polish",
}


def lang_facts(title: Any) -> dict:
    """Every signal both answers are read off, extracted exactly once.

    The shared pass is the design, not an optimisation: `sub_marked` is what
    stops Erai-raws' ``[Multiple Subtitle] [ENG][POR-BR][RUS]`` language list
    being counted as three audio tracks, and it only exists here. A caller that
    re-derives any of this with its own regex has reintroduced the bug.

    Never raises — a non-string title yields an all-empty, well-formed dict.
    """
    t = re.sub(r"[._]+", " ", title if isinstance(title, str) else "").lower()
    # Split fused "arabicdub" so the foreign tag and "dub" are separate tokens.
    t = re.sub(r"\b([a-z]{3,})dub\b", r"\1 dub", t)
    toks = set(re.sub(r"[^a-z0-9]+", " ", t).split())
    return {
        "t": t,
        "toks": toks,
        # Sorted DISPLAY values, so "french" + "truefrench" is one language, not
        # two. `track_rank` counts these, and counting raw tokens would let one
        # release spell the same language three ways into a richness bonus.
        "langs": sorted({v for k, v in _AUD_LANGS.items() if k in toks}),
        "sub_marked": bool(_AUD_SUB_RE.search(t)),
        "multi_sub": bool(_AUD_MULTI_SUB_RE.search(t)),
        "eng_audio": bool(_AUD_ENG_RE.search(_AUD_ENG_SUB_RE.sub(" ", t))),
        "dual": bool(_AUD_DUAL_RE.search(t)),
        "multi": bool(_AUD_MULTI_RE.search(t)),
        "dub": bool(_AUD_DUB_RE.search(t)),
        "french_group": bool(_AUD_FRENCH_GROUP_RE.search(t)),
    }


def classify_audio(title: Any, facts: dict = None) -> tuple:
    """Classify a release title's audio -> ``(audio, lang)``:

        "dual"  - carries original *and* English audio (Dual/Multi-Audio, DUAL,
                  or an explicit English token alongside foreign ones: ENG-ITA)
        "other" - non-English language tag(s), no English audio; ``lang`` names it
        "dub"   - English dub
        "sub"   - explicitly subtitled (original audio)
        ""      - no language markers (plain English content, or an untagged
                  fansub release — original audio + English subs for anime)

    Precedence: dual > other > dub > sub, with two context rules: a foreign tag
    next to "dub" ("ArabicDub", "Korean Dub") must not read as an English dub,
    and a multi-SUB marker means the language tokens describe subtitles
    (Erai-raws "[Multiple Subtitle] [ENG][POR-BR]..." is original audio + subs,
    not an English/Portuguese release).

    Pass `facts` when you already have them — `track_rank` needs the same ones,
    and parsing twice is exactly what this module exists to prevent.
    """
    f = facts if facts is not None else lang_facts(title)
    langs = f["langs"]
    if f["dual"]:
        return "dual", ""
    if f["french_group"]:
        # Known French release group: its MULTi/Multi-Subs releases are
        # FR+original audio, no English — the sub-marker rule must not fire.
        return "other", "French"
    if langs:
        if f["sub_marked"] and not f["dub"]:
            return "sub", ""          # the language tokens are the subs list
        if f["eng_audio"]:
            return "dual", "/".join(langs)   # multi-language incl. English
        return "other", "/".join(langs)
    if f["multi"]:
        # Bare "MULTi" with no language context: multi-audio from an English-
        # facing scene (ToonsHub) — treat as dual. (Foreign MULTi carries its
        # own language tokens/group names and is caught above.)
        return "dual", ""
    if f["dub"]:
        return "dub", ""
    if f["sub_marked"]:
        return "sub", ""
    return "", ""


def track_rank(facts: dict, audio: str) -> int:
    """How much choice a release gives you, 0-3. Audio counts double.

        2  more than one audio track
        1  more than one subtitle track

    Audio is worth double because an extra audio track is a language you can
    *watch in*, where an extra subtitle track is a convenience. Two booleans
    rather than a weighted count of tags on purpose: this is a tiebreak sorted
    below availability, so it has to be arguable in one line and must never need
    calibrating.

    It is deliberately **not** a second opinion about language — `classify_audio`
    already answered that and the picker has already filtered on it. Which is
    also why a Tsundere-Raws ``MULTi`` scores 0 despite genuinely carrying two
    audio tracks: they are French and Japanese, and with no audio preference set
    (the default) a bonus for them would quietly turn richness into a back-door
    preference for French releases. Richness may reorder copies a household can
    watch; it may not decide which language they watch in.

    Never raises: anything unexpected scores 0, which is the neutral value.
    """
    if not isinstance(facts, dict):
        return 0
    langs = facts.get("langs") or []
    # "dual" already absorbs Dual-Audio, bare DUAL, bare MULTi and the ENG-ITA
    # shape — classify_audio resolved all four to it, so there is nothing to
    # re-test here. "other" with two distinct languages is a genuine two-track
    # release that simply has no English in it.
    multi_aud = (audio == "dual") or (audio == "other" and len(langs) >= 2)
    # A bare "subbed" or "ENG SUBS" is ONE subtitle track and must not score.
    # Only the explicit multi-sub marker counts, or that marker's language list.
    multi_sub = bool(facts.get("multi_sub")) or (
        bool(facts.get("sub_marked")) and len(langs) >= 2)
    return (2 if multi_aud else 0) + (1 if multi_sub else 0)


def analyse(title: Any) -> tuple:
    """``(audio, lang, tracks)`` in one call, off one parse. The shape `main`
    wants when it is enriching a search result and needs all three."""
    f = lang_facts(title)
    audio, lang = classify_audio(title, f)
    return audio, lang, track_rank(f, audio)
