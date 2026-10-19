"""Reading a torrent's name: its info-hash, its season/episode, its show title.

`parse_torrent_title` is what groups search results into shows; `extract_hash`
normalises a magnet's info-hash; `_strip_release_tags` cleans a release name
down to something TMDb can search for. Pure (stdlib + `episodes`).
"""
import base64
import html as _html
import re
from typing import Optional

import episodes


# ── TMDb search terms from a release name ─────────────────────────────────────

_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")


def _strip_release_tags(name: str) -> str:
    """Strip common release/quality tags from a torrent title so a TMDb search
    has a shot at matching the canonical show name."""
    if not name:
        return ""
    s = name
    s = re.sub(r"\[[^\]]*\]", " ", s)
    s = re.sub(r"\([^)]*\)", " ", s)
    s = re.sub(
        r"\b(2160p|1080p|720p|480p|4K|UHD|BluRay|BDRip|BRRip|DVDRip|WEB-?DL|WEBRip|"
        r"HDTV|HDR|HDR10|HEVC|x264|x265|H\.?264|H\.?265|AAC|AC3|DTS|FLAC|EAC3|"
        r"REMUX|REPACK|PROPER|EXTENDED|UNRATED|MULTi|DUAL|SUBBED|DUBBED|BATCH|"
        r"VOSTFR|SUBFRENCH|\d{1,2}[ -]?bit|"
        r"COMPLETE|S\d{1,2}|Season\s*\d{1,2}|E\d{1,3})\b.*",
        " ", s, flags=re.IGNORECASE,
    )
    s = re.sub(r"[._\-]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip(" -_.")
    return s


# ── Magnet info-hash, one file's season/episode ──────────────────────────────

def extract_hash(magnet: str) -> Optional[str]:
    """Pull the info-hash from a magnet URI, normalising base32 → hex."""
    m = re.search(r"xt=urn:btih:([a-fA-F0-9]{40}|[A-Za-z2-7]{32})", magnet, re.I)
    if not m:
        return None
    h = m.group(1)
    if len(h) == 32:
        try:
            h = base64.b32decode(h.upper()).hex()
        except Exception:
            return None
    return h.lower()


def parse_season_episode(name: str) -> tuple[int, int]:
    """Extract (season, episode) from one file name or relative path.

    Thin wrapper over `episodes.parse_slot` for the callers that only ever see a
    bare filename (uploads, non-torrent moves). Anything holding a whole item's
    file list should use `episodes.attribute_paths` instead — the release root
    has to be identified before a directory can be read as a season, and that
    takes every path. See docs/LIBRARY_DATA.md § Season/episode attribution.
    """
    slot = episodes.parse_slot(name)
    return slot["season"], slot["episode"]


# ── Search-result title parsing (show grouping) ──────────────────────────────
# Structural markers that separate a show's name from its season/episode/release
# info. Used by parse_torrent_title() to group flat search results into shows.
# `\d{1,3}` + `(?!\d)`: without them "S01E015" matched "E01" and left the 5
# behind, so ten different episodes of Hunter x Hunter all parsed as episode 1
# and flooded every S01E01 search.
_TT_EP_RE          = re.compile(r"[Ss](\d{1,2})[Ee](\d{1,3})(?!\d)")
# A FRACTIONAL episode code — "S01E07.5", the scene's form for a recap or
# summary special. It is not episode 7, and handing one to somebody who asked
# for episode 7 is a silent wrong answer (live: a 2160p ToonsHub Solo Leveling
# S01E07.5 at 38 seeders, which the seeder-led pick took).
#
# Two things make this fiddly. It is matched against the RAW title, because
# `norm` flattens dots to spaces and "S01E07.5" is "S01E07 5" by then. And the
# single-digit requirement is the whole discriminator: ".5" is a fraction,
# ".720p"/".1080p"/".2160p" is a resolution tag. Measured on live results: 1
# release of the first shape, 111 of the second, so a rule that cannot tell
# them apart would break a hundred titles to fix one.
#
# Only the dotted/underscored form is matched. A space-separated
# "S01E07 5" is conceivable but was not observed, and widening the
# separator class would start reading an ordinary trailing number as a
# fraction for no measured gain.
#
# It is a FLAG, not a failed match, and that is deliberate: dropping the match
# would fall through to `_TT_SEASON_RE`, which sees the "S01" and classifies a
# 300 MB recap as a whole SEASON PACK — strictly worse than the bug it fixes.
_TT_EP_FRAC_RE     = re.compile(r"[Ss]\d{1,2}[Ee]\d{1,3}[._]\d(?!\d)")
_TT_EP_X_RE        = re.compile(r"\b(\d{1,2})x(\d{2})\b")
# Multi-season packs. The trailing (?!\d) stops the second number from matching a
# resolution/year *prefix* — "S01 - 720p" must NOT read as S1-72, "S08 - 2019"
# not as S8-20. Legit forms: S01-S05, S01-05, S01-5, S1+S2+S3, S01-S02-S03.
_TT_MULTI_PLUS_RE  = re.compile(r"[Ss]\d{1,2}(?:\s*[+&]\s*[Ss]?\d{1,2})+")         # S1+S2+S3 / S01&02
_TT_MULTI_WORD_PLUS_RE = re.compile(
    r"[Ss]easons?\s*\d{1,2}(?:\s*[+&]\s*(?:[Ss]easons?\s*)?\d{1,2})+")             # Season 1 + 2
_TT_MULTI_LIST_RE  = re.compile(r"[Ss]\d{1,2}(?:\s*-\s*[Ss]?\d{1,2}(?!\d)){2,}")   # S01-S02-S03…
_TT_MULTI_RANGE_RE = re.compile(r"[Ss](\d{1,2})\s*-\s*[Ss]?(\d{1,2})(?!\d)")       # S01-S05 / S01-5
# Fansub per-episode form for sequel seasons: "[SubsPlease] Show S2 - 05" /
# "Show Season 2 - 05". A *spaced* dash + a 2+-digit second number with no S
# prefix is an episode, not a season range — real packs write "S01-S05"/"S01-05"
# tight, or "Seasons 1-5" (plural, which this pattern's singular "Season" never
# matches). Must be tested before the multiseason ranges, which would otherwise
# swallow "S2 - 05" as a bogus seasons-2-to-5 pack.
_TT_SEASON_EP_DASH_RE = re.compile(r"\b[Ss](?:eason\s*)?(\d{1,2})\s+-\s+(\d{2,4})(?!\d)")
_TT_SEASONS_WORD_RE = re.compile(r"[Ss]easons?\s*(\d{1,2})\s*-\s*(\d{1,2})(?!\d)") # Seasons 1-5
_TT_SEASON_RE      = re.compile(r"\b[Ss](\d{1,2})\b")
_TT_SEASON_WORD_RE = re.compile(r"\b[Ss]eason\s*(\d{1,2})\b")
# Ordinal season ("Sousou no Frieren 2nd Season") — the dominant fansub form for
# anime sequels. Without it, "…2nd Season - 10" reads as absolute S1E10 and the
# ordinal stays in the show name, fragmenting the group away from season 1.
_TT_ORDINAL_SEASON_RE = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)\s+[Ss]eason\b", re.IGNORECASE)
_TT_COMPLETE_SERIES_RE = re.compile(r"\bComplete\s+(?:Series|Pack|Collection)\b", re.IGNORECASE)
# Absolute-numbered anime episode ("[Grp] One Piece - 1168 [1080p]", "One Piece
# EP1168", "Naruto 121 (..."). Only consulted when NO S/E or season marker exists.
# Dash / EP are strong fansub signals (1-4 digits); a bare trailing number needs
# 3-4 digits so "Ocean's 11" (a movie) isn't read as an episode.
# Absolute-episode-range batch ("One Piece 001-574", "One Piece 1094-1099") —
# the long-running-anime bulk form. Requires a 3+-digit upper bound so year
# spans and small number pairs never match; validated further at the use site.
_TT_EP_RANGE_RE    = re.compile(r"\b(\d{1,4})\s*[-~]\s*(\d{3,4})(?!\d)")
_TT_ABS_EP_RE      = re.compile(r"\b[Ee][Pp]\s?(\d{1,4})\b")                       # EP1168
_TT_ABS_DASH_RE    = re.compile(r"\s-\s+(\d{1,4})(?!\d)")                          # Show - 121
# Size tags ("1.85GB", "700 MB", "2 GB") — stripped before structural parsing so a
# YIFY-style "- 1.85GB" can't be misread as absolute episode 1 (dots → spaces in
# norm turn "1.85GB" into "1 85GB", so match the split form too).
_TT_SIZE_RE        = re.compile(r"\b\d+(?:[ .]\d+)?\s*(?:[GMKT]i?B|MB|GB|KB|TB)\b", re.I)
_TT_ABS_TAIL_RE    = re.compile(r"\s(\d{3,4})\s*(?=[\[(]|$)")                      # Show 1168 [.. / (..
_TT_TRAIL_YEAR_RE  = re.compile(r"\s+(?:19|20)\d{2}$")
_TT_EXT_RE         = re.compile(r"\.(?:mkv|mp4|avi|mov|m4v|ts|webm)$", re.IGNORECASE)
_TT_RES_NUMS       = {480, 540, 576, 720, 1080, 1440, 2160, 4320}


def _tt_bounded_span(a: int, b: int):
    """Accept a season range only if it's plausible. Rejects the resolution/year
    bleed and absolute-ep dashes ("S4 - 37") that would otherwise read as a
    30+-season pack — real multi-season packs span a handful of seasons."""
    lo, hi = min(a, b), max(a, b)
    if hi > 50 or (hi - lo) > 30:
        return None
    return lo, hi


def parse_torrent_title(title: str) -> dict:
    """Classify a raw torrent title into a show + kind so flat search results can
    be grouped. Returns:
        show        display-cased show name ("Breaking Bad")
        show_key    normalised grouping key ("breaking bad")
        kind        "episode" | "season" | "multiseason" | "movie"
        season      episode/season kinds: the season number (0 otherwise)
        episode     episode kind: episode number (absolute-numbered anime → season 1)
        season_from/season_to  multiseason kind: inclusive season range (0 = "complete")
        ep_from/ep_to  multiseason kind only: absolute-episode-range batch
                       ("One Piece 001-574"; 0 when the pack is season-based)
        year        detected release year (or None)
    The show name is the text *before* the first structural marker, run through
    _strip_release_tags — this is what separates e.g. a "Better Call Saul S06E11
    Breaking Bad" or "Moonshiners S15E12 Braking Badly" hit out of a search for
    "breaking bad" into their own groups instead of polluting the show."""
    raw = _html.unescape(title or "")   # Jackett titles can carry &amp; etc.
    # Checked on `raw`: the next line destroys the dot this depends on.
    frac_ep = bool(_TT_EP_FRAC_RE.search(raw))
    norm = re.sub(r"[._]+", " ", raw)
    norm = re.sub(r"\s+", " ", norm).strip()
    # Drop size tags so "- 1.85GB" / "- 700 MB" aren't misread as absolute episodes.
    norm = re.sub(r"\s+", " ", _TT_SIZE_RE.sub(" ", norm)).strip()

    ym = _YEAR_RE.search(norm)
    year = int(ym.group(0)) if ym else None

    season = episode = season_from = season_to = ep_from = ep_to = 0
    abs_marker_pos = None   # where to cut the show name for an absolute-numbered ep

    ep_m    = _TT_EP_RE.search(norm) or _TT_EP_X_RE.search(norm)
    plus_m  = _TT_MULTI_PLUS_RE.search(norm) or _TT_MULTI_WORD_PLUS_RE.search(norm)
    list_m  = _TT_MULTI_LIST_RE.search(norm)
    range_m = _TT_SEASONS_WORD_RE.search(norm) or _TT_MULTI_RANGE_RE.search(norm)
    complete_series = bool(_TT_COMPLETE_SERIES_RE.search(norm))

    # "S2 - 05" / "Season 2 - 05" (spaced dash, padded episode) is a sequel-
    # season *episode*, not a range — claim it before the multiseason block.
    epdash_m = None
    if ep_m is None:
        m = _TT_SEASON_EP_DASH_RE.search(norm)
        if m:
            n = int(m.group(2))
            if not (1900 <= n <= 2099) and n not in _TT_RES_NUMS:
                epdash_m = m

    kind = None
    if epdash_m is not None:
        kind = "episode"
        season, episode = int(epdash_m.group(1)), int(epdash_m.group(2))
    if kind is None and (plus_m or list_m or range_m or complete_series):
        span = None
        if plus_m:
            nums = [int(x) for x in re.findall(r"\d{1,2}", plus_m.group(0))]
            span = _tt_bounded_span(min(nums), max(nums)) if nums else None
        elif list_m:
            nums = [int(x) for x in re.findall(r"\d{1,2}", list_m.group(0))]
            span = _tt_bounded_span(min(nums), max(nums)) if nums else None
        elif range_m:
            span = _tt_bounded_span(int(range_m.group(1)), int(range_m.group(2)))
        if span:
            kind, (season_from, season_to) = "multiseason", span
        elif complete_series:
            kind = "multiseason"          # "Complete Series" with no explicit numbers
        elif range_m:
            # Implausible span ("S01 - 720p" already blocked by (?!\d); "S4 - 37")
            # → treat as the single leading season, not a bogus pack.
            kind, season = "season", int(range_m.group(1))

    if kind is None:
        if ep_m:
            kind = "episode"
            season, episode = int(ep_m.group(1)), int(ep_m.group(2))
        else:
            sm = (_TT_SEASON_WORD_RE.search(norm) or _TT_ORDINAL_SEASON_RE.search(norm)
                  or _TT_SEASON_RE.search(norm))
            if sm:
                kind, season = "season", int(sm.group(1))
                # "<show> 2nd Season - 10" / "<show> Season 2 - 10": an absolute
                # episode number AFTER a word/ordinal season marker is that
                # season's episode (the fansub form for sequel seasons), not a
                # batch. Bare "S2 - 10" is left alone — _TT_MULTI_RANGE_RE owns
                # that ambiguity.
                if sm.re is not _TT_SEASON_RE:
                    am = (_TT_ABS_EP_RE.search(norm, sm.end())
                          or _TT_ABS_DASH_RE.search(norm, sm.end()))
                    if am:
                        n = int(am.group(1))
                        if not (1900 <= n <= 2099) and n not in _TT_RES_NUMS:
                            kind, episode = "episode", n
            else:
                # Absolute-episode-range batch ("One Piece 001-574") — must be
                # tested before the single abs-episode forms, whose " - NNN"
                # would otherwise read the range's upper bound as one episode.
                rm = _TT_EP_RANGE_RE.search(norm)
                if rm:
                    a_, b_ = int(rm.group(1)), int(rm.group(2))
                    if (b_ <= a_ or b_ < 100
                            or (1900 <= a_ <= 2099 and 1900 <= b_ <= 2099)
                            or (a_ in _TT_RES_NUMS and b_ in _TT_RES_NUMS)):
                        rm = None
                if rm is not None:
                    kind = "multiseason"     # an episodes batch is a pack
                    ep_from, ep_to = int(rm.group(1)), int(rm.group(2))
                    abs_marker_pos = rm.start()
                else:
                    am = (_TT_ABS_EP_RE.search(norm) or _TT_ABS_DASH_RE.search(norm)
                          or _TT_ABS_TAIL_RE.search(norm))
                    if am:
                        n = int(am.group(1))
                        if not (1900 <= n <= 2099) and n not in _TT_RES_NUMS:
                            kind, season, episode = "episode", 1, n
                            abs_marker_pos = am.start()   # cut before " - " / "EP" / the number
                if kind is None:
                    kind = "movie"

    # Show name = text before the earliest structural marker.
    positions = []
    for rx in (_TT_EP_RE, _TT_EP_X_RE, _TT_SEASONS_WORD_RE, _TT_MULTI_PLUS_RE,
               _TT_MULTI_WORD_PLUS_RE, _TT_MULTI_LIST_RE, _TT_MULTI_RANGE_RE,
               _TT_SEASON_WORD_RE, _TT_ORDINAL_SEASON_RE, _TT_SEASON_RE,
               _TT_COMPLETE_SERIES_RE):
        m = rx.search(norm)
        if m:
            positions.append(m.start())
    if abs_marker_pos is not None:
        positions.append(abs_marker_pos)
    cut = min(positions) if positions else len(norm)
    show = _strip_release_tags(norm[:cut]) or _strip_release_tags(norm) or norm.strip()
    # A cut inside parens ("Show (Season 1) …") leaves a dangling "(" the tag
    # stripper can't remove (it only strips balanced pairs) — trim trailing
    # opener/separator punctuation so the group key merges with the clean name.
    show = re.sub(r"[\s(\[{|:,&+\-–—]+$", "", show).strip() or show
    # Key ignores punctuation so apostrophe/colon variants of the same title
    # ("Frieren: Beyond Journey's End" / "Frieren Beyond Journeys End") land in
    # one group instead of fragmenting by scene-name styling.
    show_key = re.sub(r"\s+", " ", re.sub(r"[^\w\s]+", "", show.lower())).strip()
    # …and ignores CJK decoration when a Latin name remains ("DanDaDan ダンダダン"
    # groups with "Dandadan"; a purely-CJK title keeps its CJK key).
    ascii_key = re.sub(r"\s+", " ", re.sub(r"[^\x00-\x7f]+", " ", show_key)).strip()
    if len(re.sub(r"[^a-z0-9]", "", ascii_key)) >= 3:
        show_key = ascii_key
    show_key = _TT_EXT_RE.sub("", show_key)                       # drop a trailing .mkv etc
    show_key = _TT_TRAIL_YEAR_RE.sub("", show_key).strip()        # "inception 2010" ⇒ "inception"

    return {
        "show": show, "show_key": show_key, "kind": kind,
        "season": season, "episode": episode,
        "season_from": season_from, "season_to": season_to,
        "ep_from": ep_from, "ep_to": ep_to, "year": year,
        # "S01E07.5" — a recap/summary special, NOT episode 7. Only meaningful
        # on an `episode` kind; consumers that resolve a specific episode must
        # skip it (see `_bgIngest`), consumers that just group results need not
        # care. See `_TT_EP_FRAC_RE` for why this is a flag and not a non-match.
        "special": frac_ep and kind == "episode",
    }
