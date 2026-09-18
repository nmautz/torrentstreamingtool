"""Subtitle search: building OpenSubtitles queries, deciding which results are
really this episode, ranking them, and reading/re-timing subtitle files.

Pure (stdlib only, no `main` import). `main.py` does the HTTP; `subsync.py`
does the audio alignment. Every rule here was measured on the eval kit in
tests/subs_eval/ (52 library episodes graded against their own embedded
English tracks) — see docs/GOTCHAS.md § "Subtitle search" before changing one.

THE LEGACY API IS UNFORGIVING ABOUT URLS
rest.opensubtitles.org canonicalises every search path and answers anything
non-canonical with a 302 to the host `_` — which a client sees as a connection
error, i.e. silently zero results. Canonical means: path params in alphabetical
order, `imdbid` zero-padded to 7 digits, `query` lower-case with `+` for spaces
and no apostrophes. The pre-17.7 code searched by the raw filename stem (upper
case, brackets) and so found nothing for 52/52 eval targets.

WHY METADATA FILTERING IS NOT ENOUGH ON ITS OWN
A title search for "Death Note" returns the 2015 drama, "Death Note: New
Generation" and "Death of the Pastor's Wife"; the IMDb search is exact on the
show, but uploaders mislabel episodes ("CG R2 - 01" = Code Geass season 2 filed
as S1E1) and DVD vs broadcast order differ (Futurama). So the filter here removes
what metadata CAN rule out, the ranking prefers what is likely to be in sync, and
subsync.py verifies the pick against the episode's actual speech.
"""
from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import quote

LEGACY_API = "https://rest.opensubtitles.org/search"
USER_AGENT = "TemporaryUserAgent"

# ── queries ───────────────────────────────────────────────────────────────────


def norm_title(t: str) -> str:
    """What the legacy API accepts as `query-…`: lower case, no apostrophes, no
    punctuation, single spaces."""
    t = re.sub(r"[【】\[\]()]", " ", t or "")
    t = t.lower().replace("'", "").replace("’", "")
    t = re.sub(r"[^\w\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def imdb_num(imdb) -> int:
    """'tt0994314' / '0994314' / 994314 -> 994314 (0 when absent)."""
    s = str(imdb or "").strip().lower().removeprefix("tt")
    return int(s) if s.isdigit() else 0


def legacy_url(**params) -> str:
    """Canonical legacy search URL. Params: episode, imdbid, moviebytesize,
    moviehash, query, season, sublanguageid. Empty values are dropped."""
    segs = []
    for k in sorted(params):
        v = params[k]
        if v in (None, "", 0) and k not in ("season",):
            continue
        if v in (None, ""):
            continue
        if k == "imdbid":
            v = f"{imdb_num(v):07d}"
        elif k == "query":
            v = quote(norm_title(str(v)), safe="").replace("%20", "+")
            if not v:
                continue
        else:
            v = quote(str(v).lower(), safe="")
        segs.append(f"{k}-{v}")
    return f"{LEGACY_API}/" + "/".join(segs)


def search_urls(target: dict, lang: str = "") -> list[str]:
    """Every search worth making for `target`, most exact first.

    target keys: kind ('tv'|'movie'), imdb, title, season, episode, abs_no,
    anime, hash, size. `lang` is an OpenSubtitles 3-letter id or '' for all.
    """
    tv = target.get("kind") != "movie"
    s, e = target.get("season"), target.get("episode")
    urls = []
    if target.get("hash") and target.get("size"):
        urls.append(legacy_url(moviebytesize=target["size"], moviehash=target["hash"], sublanguageid=lang))
    if target.get("imdb"):
        if tv and s is not None and e:
            urls.append(legacy_url(imdbid=target["imdb"], season=s, episode=e, sublanguageid=lang))
        elif not tv:
            urls.append(legacy_url(imdbid=target["imdb"], sublanguageid=lang))
    if target.get("title"):
        if tv and s is not None and e:
            urls.append(legacy_url(query=target["title"], season=s, episode=e, sublanguageid=lang))
        elif not tv:
            urls.append(legacy_url(query=target["title"], sublanguageid=lang))
    if tv and target.get("anime") and target.get("abs_no") and target.get("abs_no") != e and target.get("title"):
        urls.append(legacy_url(query=target["title"], episode=target["abs_no"], sublanguageid=lang))
    seen, out = set(), []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def free_text_url(query: str, lang: str = "") -> Optional[str]:
    """The manual 'search for anything' box — canonicalised so it actually works."""
    q = norm_title(query)
    return legacy_url(query=q, sublanguageid=lang) if q else None


# ── filtering + ranking ──────────────────────────────────────────────────────


def _norm(s: str) -> str:
    s = (s or "").lower().replace("'", "").replace("’", "")
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def _show_name(r: dict) -> str:
    m = re.match(r'\s*"([^"]+)"', r.get("MovieName") or "")
    return m.group(1) if m else (r.get("MovieName") or "")


def is_match(target: dict, r: dict) -> bool:
    """Metadata says this result is `target` (right show AND episode)."""
    if str(r.get("SubForeignPartsOnly")) == "1":
        return False                                    # forced-only: not dialogue
    if int(r.get("SubSumCD") or 1) > 1:
        return False                                    # one CD of a split movie
    if r.get("MatchedBy") == "moviehash":
        return True                                     # byte-exact file match
    imdb = imdb_num(target.get("imdb"))
    names = {_norm(x) for x in [target.get("title") or "", *(target.get("aka") or [])] if x}
    if target.get("kind") == "movie":
        if imdb:
            return imdb_num(r.get("IDMovieImdb")) == imdb
        return _norm(r.get("MovieName")) in names
    if imdb:
        if imdb_num(r.get("SeriesIMDBParent")) != imdb:
            return False
    elif _norm(_show_name(r)) not in names:
        return False
    try:
        s, e = int(r.get("SeriesSeason") or 0), int(r.get("SeriesEpisode") or 0)
    except ValueError:
        return False
    if s == target.get("season") and e == target.get("episode"):
        return True
    # Anime filed by absolute number: "Hunter x Hunter - 45" is S1E45 on the site
    # whatever TMDb's season split says. Only season 0/1 — a real S2E4 is never E4.
    ab = target.get("abs_no")
    return bool(target.get("anime") and ab and e == ab and s in (0, 1))


def last_ts(r: dict) -> Optional[float]:
    """The subtitle's last timestamp in seconds (SubLastTS: 'HH:MM:SS', or a
    frame count for MicroDVD)."""
    v = str(r.get("SubLastTS") or "")
    m = re.fullmatch(r"(\d+):(\d\d):(\d\d)", v)
    if m:
        return int(m[1]) * 3600 + int(m[2]) * 60 + int(m[3])
    if v.isdigit():
        fps = float(r.get("MovieFPS") or 0) or 23.976
        return int(v) / fps
    return None


_SRC = [("bluray", r"blu-?ray|bdrip|brrip|\bbd\b|bdremux|\bbd\d|\bbd720|\bbd1080"),
        ("web", r"web-?dl|webrip|\bweb\b|amzn|\bnf\b|netflix|hmax|dsnp|hulu|atvp|\bcr\b"),
        ("hdtv", r"hdtv|pdtv|\bdsr\b|tvrip"),
        ("dvd", r"dvd")]


def source(name: str) -> str:
    s = (name or "").lower()
    for label, rx in _SRC:
        if re.search(rx, s):
            return label
    return ""


_STOPTOK = {"eng", "en", "english", "srt", "ass", "ssa", "sub", "hi", "sdh", "cc", "forced", "subs",
            "subtitles", "complete", "season", "episode", "the", "and", "of", "mkv", "mp4", "avi"}


def _rel_tokens(name: str, title: str) -> set:
    s = re.sub(r"\.(srt|ass|ssa|sub|vtt|mkv|mp4|avi)$", "", (name or "").lower())
    toks = set(re.findall(r"[a-z0-9]+", s)) - set(re.findall(r"[a-z0-9]+", (title or "").lower()))
    return {t for t in toks if t not in _STOPTOK
            and not re.fullmatch(r"s\d+e\d+|s\d+|e\d+|\d{1,3}|\d+x\d+", t)}


def release_similarity(file_name: str, title: str, r: dict) -> float:
    """Share of the video file's release tokens (group, source, service,
    resolution, codec) that the subtitle's release name also carries."""
    a = _rel_tokens(file_name, title)
    b = _rel_tokens(r.get("MovieReleaseName") or "", title) | _rel_tokens(r.get("SubFileName") or "", title)
    return len(a & b) / len(a) if a and b else 0.0


def score(target: dict, r: dict) -> float:
    """Likelihood that `r` is in sync with `target`'s file. Weights from the eval
    kit's per-feature tables (tests/subs_eval/features.py):
      * ends > 30 s after the video: 96% wrong        -> heavy penalty
      * same source family (web/web): 66% in sync, 2% wrong; Blu-ray file with
        a web sub: 91% wrong
      * MicroDVD `.sub`: 91% wrong; 25 fps (PAL) timing: 67% wrong
      * download count barely predicts anything      -> tiebreak only
    """
    if r.get("MatchedBy") == "moviehash":
        return 100.0
    s = 0.0
    lt, d = last_ts(r), target.get("duration")
    if lt and d:
        over = lt - d
        if over > 30:
            s -= 10
        elif over > 2:
            s -= 3
        elif over < -240:
            s -= 2
    fs = source(target.get("file_name") or "")
    ss = source(r.get("MovieReleaseName") or r.get("SubFileName") or "")
    if fs and ss:
        s += 2 if fs == ss else -2
    s += 3 * release_similarity(target.get("file_name") or "", target.get("title") or "", r)
    if (r.get("SubFormat") or "").lower() == "sub":
        s -= 4
    try:
        fps = float(r.get("MovieFPS") or 0)
    except ValueError:
        fps = 0.0
    if fps and abs(fps - 25) < 0.01:
        s -= 2
    if str(r.get("SubAutoTranslation")) == "1":
        s -= 3
    if str(r.get("SubBad")) not in ("0", "", "None"):
        s -= 3
    if str(r.get("SubFromTrusted")) == "1":
        s += 0.5
    s += 0.25 * math.log10(1 + int(r.get("SubDownloadsCnt") or 0))
    return round(s, 3)


def rank(target: dict, results: Iterable[dict], matched_only: bool = True) -> list[dict]:
    """Dedupe (same file id / same content hash), filter, score, sort.
    Each returned result gains `_score` and `_match`."""
    seen, out = set(), []
    for r in results:
        key = r.get("IDSubtitleFile") or r.get("SubHash") or r.get("SubDownloadLink")
        h = r.get("SubHash")
        if not key or key in seen or (h and h in seen):
            continue
        seen.add(key)
        if h:
            seen.add(h)
        m = is_match(target, r)
        if matched_only and not m:
            continue
        out.append(dict(r, _score=score(target, r), _match=m))
    out.sort(key=lambda r: (not r["_match"], -r["_score"]))
    return out


# ── subtitle files ────────────────────────────────────────────────────────────


def decode(b: bytes) -> str:
    if b.startswith(b"\xef\xbb\xbf"):
        b = b[3:]
    if b.startswith((b"\xff\xfe", b"\xfe\xff")):
        return b.decode("utf-16", "replace")
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return b.decode("cp1252", "replace")


def sniff_format(text: str) -> str:
    """'ass' | 'vtt' | 'sub' (MicroDVD) | 'srt' | '' — by CONTENT. OpenSubtitles
    serves ASS as often as SRT for anime; the pre-17.7 code saved everything as
    `.srt`, so half its anime downloads were ASS text in a file named .srt."""
    head = text[:4000]
    if re.search(r"(?im)^\[script info\]|^\[events\]", head):
        return "ass"
    if head.lstrip().startswith("WEBVTT"):
        return "vtt"
    if re.match(r"\s*\{\d+\}\{\d+\}", head):
        return "sub"
    if "-->" in head:
        return "srt"
    return ""


_TS = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})")


def _ts(s: str) -> Optional[float]:
    m = _TS.search(s)
    if not m:
        return None
    h, mi, se, frac = m.groups()
    return int(h or 0) * 3600 + int(mi) * 60 + int(se) + int(frac.ljust(3, "0")[:3]) / 1000


def _clean(t: str) -> str:
    t = re.sub(r"\{[^}]*\}", "", t)
    t = re.sub(r"<[^>]+>", "", t)
    t = t.replace("\\N", " ").replace("\\n", " ").replace("\\h", " ")
    return re.sub(r"\s+", " ", t).strip()


_SIGN_STYLE = re.compile(r"sign|song|^op|^ed|karaoke|title", re.I)


def parse_cues(text: str, fps: float = 23.976) -> list[tuple[float, float, str]]:
    """[(start, end, text)] for SRT, VTT, ASS/SSA and MicroDVD. ASS lines in
    sign/song styles are dropped (they're not speech) unless that would drop most
    of the file — some groups name their dialogue style oddly."""
    fmt = sniff_format(text)
    cues: list[tuple[float, float, str]] = []
    if fmt == "ass":
        cols, dropped = None, []
        for line in text.splitlines():
            low = line.lower()
            if low.startswith("format:") and cols is None and "start" in low:
                cols = [x.strip().lower() for x in line.split(":", 1)[1].split(",")]
            elif line.startswith("Dialogue:") and cols:
                row = dict(zip(cols, line.split(":", 1)[1].split(",", len(cols) - 1)))
                a, b = _ts(row.get("start", "")), _ts(row.get("end", ""))
                txt = _clean(row.get("text", ""))
                if a is None or b is None or not txt:
                    continue
                style = (row.get("style") or "").strip()
                if _SIGN_STYLE.search(style) and "default" not in style.lower():
                    dropped.append((a, b, txt))
                else:
                    cues.append((a, b, txt))
        if len(cues) < len(dropped):
            cues += dropped
    elif fmt == "sub":
        for m in re.finditer(r"(?m)^\{(\d+)\}\{(\d+)\}(.*)$", text):
            a, b, t = int(m.group(1)), int(m.group(2)), m.group(3)
            if a == 1 and b == 1:
                try:
                    fps = float(t)
                except ValueError:
                    pass
                continue
            cues.append((a / fps, b / fps, _clean(t.replace("|", " "))))
    elif fmt in ("srt", "vtt"):
        for block in re.split(r"\r?\n\s*\r?\n", text):
            m = re.search(r"([\d:.,]+)\s*-->\s*([\d:.,]+)", block)
            if not m:
                continue
            a, b = _ts(m.group(1)), _ts(m.group(2))
            if a is None or b is None:
                continue
            txt = _clean(" ".join(block[m.end():].splitlines()))
            if txt:
                cues.append((a, b, txt))
    cues = [(a, max(a + 0.05, b), t) for a, b, t in cues if b >= 0]
    cues.sort()
    return cues


def _fmt_srt(x: float) -> str:
    x = max(0.0, x)
    ms = int(round(x * 1000))
    return f"{ms // 3600000:02}:{ms // 60000 % 60:02}:{ms // 1000 % 60:02},{ms % 1000:03}"


def _fmt_vtt(x: float) -> str:
    return _fmt_srt(x).replace(",", ".")


def _fmt_ass(x: float) -> str:
    x = max(0.0, x)
    cs = int(round(x * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02}:{cs // 100 % 60:02}.{cs % 100:02}"


def retime(text: str, fn) -> tuple[str, str]:
    """Rewrite every timestamp through `fn(seconds) -> seconds`, keeping the
    file's own format and styling (an ASS file stays ASS — positioning, fonts
    and karaoke intact). MicroDVD is converted to SRT, since frame numbers can't
    carry a sub-frame correction. Returns (new_text, format)."""
    fmt = sniff_format(text)
    if fmt == "ass":
        def dia(m):
            parts = m.group(2).split(",", 2)
            if len(parts) < 3:
                return m.group(0)
            a, b = _ts(parts[1]), _ts(parts[2].split(",", 1)[0])
            if a is None or b is None:
                return m.group(0)
            rest = parts[2].split(",", 1)[1] if "," in parts[2] else ""
            return f"{m.group(1)}{parts[0]},{_fmt_ass(fn(a))},{_fmt_ass(fn(b))},{rest}"
        return re.sub(r"(?m)^((?:Dialogue|Comment):\s*)(.*)$", dia, text), "ass"
    if fmt in ("srt", "vtt"):
        f = _fmt_srt if fmt == "srt" else _fmt_vtt

        def arrow(m):
            a, b = _ts(m.group(1)), _ts(m.group(2))
            if a is None or b is None:
                return m.group(0)
            return f"{f(fn(a))} --> {f(fn(b))}{m.group(3)}"
        return re.sub(r"([\d:.,]+)\s*-->\s*([\d:.,]+)([^\n]*)", arrow, text), fmt
    if fmt == "sub":
        cues = parse_cues(text)
        return to_srt([(fn(a), fn(b), t) for a, b, t in cues]), "srt"
    return text, fmt


def to_srt(cues) -> str:
    return "\n".join(f"{i}\n{_fmt_srt(a)} --> {_fmt_srt(b)}\n{t}\n" for i, (a, b, t) in enumerate(cues, 1))


def sidecar_name(video: Path, lang: str, fmt: str, tag: str = "opensubs") -> Path:
    """`<stem>.<lang>.<tag>.<ext>` next to the video; `.2`, `.3`… if taken.
    The `opensubs` tag marks a file we downloaded (vs one that shipped with the
    release). Not `os`: that is a real ISO 639-1 code (Ossetian), and
    `_parse_sub_lang` reads filename tokens as languages."""
    ext = {"ass": "ass", "vtt": "vtt"}.get(fmt, "srt")
    safe = re.sub(r"[^a-z]", "", (lang or "").lower())[:5] or "und"
    dest = video.with_name(f"{video.stem}.{safe}.{tag}.{ext}")
    n = 2
    while dest.exists():
        dest = video.with_name(f"{video.stem}.{safe}.{tag}.{n}.{ext}")
        n += 1
    return dest
