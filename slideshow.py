"""Idle-screen slideshow: which photos the TV shows when nothing is playing (20.28.0).

Most people never upload a background video. The slideshow needs no setup: the
admin ticks themes (and, for one of them, types a city), and the box keeps a
rotating set of photos fresh by itself. Every source answers without an API
key; a Pexels key is an optional upgrade for the city theme, used only when set.

This is the pure half: the season, the request URLs, reading each source's
answer into one photo shape, judging a downloaded image, and choosing the order
the TV shows them in. The engine in main.py (`_ambient_*`) does the fetching,
the cache and the screen; `static/index.html` (`_amb*`) draws it.

Leaf module: stdlib only, imports nothing else in this repo. Tests in
tests/test_slideshow.py.

A photo is a dict:
    id      stable and source-prefixed ("commons:File:X.jpg", "bing:OHR.X", ...)
    theme   one of THEME_IDS
    src     where to download it from (for `library`: the local URL to show)
    title   what it shows, one line, may be ""
    credit  who made it and under what licence. Shown on screen, never dropped:
            Commons' licences require it, and so do Pexels' and TMDb's terms.
    month   1-12 when the photo was taken, 0 if unknown (the city theme's season)
    date    "YYYY-MM-DD" for a photo-of-the-day source, else ""
"""

import hashlib
import html
import random
import re
from datetime import date, timedelta
from typing import Optional
from urllib.parse import quote, urlencode

# ── Themes ────────────────────────────────────────────────────────────────────

THEMES = (
    {"id": "library", "label": "Your library",
     "blurb": "Backdrops of the films and shows on this box. Works offline."},
    {"id": "daily", "label": "Photo of the day",
     "blurb": "Bing's daily photo for your region, and Wikimedia Commons' picture of the day."},
    {"id": "city", "label": "My city this season",
     "blurb": "Photos taken near your city at this time of year. Needs a city."},
    {"id": "nature", "label": "Seasonal nature",
     "blurb": "Featured landscape photos of the current season."},
    {"id": "space", "label": "Space",
     "blurb": "Featured astronomy photos and James Webb telescope images."},
)
THEME_IDS = tuple(t["id"] for t in THEMES)
# A fresh install shows these. `city` joins once a city is set.
DEFAULT_THEMES = ("library", "daily", "nature", "space")

PER_THEME = 40          # photos kept per downloaded theme
MIN_CITY = 12           # fewer in-season city photos than this → widen the window
INTERVALS = (15, 30, 60, 120, 300)   # seconds a photo stays up (admin picks)
DEFAULT_INTERVAL = 30

# Wikimedia blocks generic User-Agents on the API and on thumbnails alike, and
# asks for a contact. Sent on every request the slideshow makes.
USER_AGENT = ("StreamLink/20 (self-hosted media box idle slideshow; "
              "https://github.com/nmautz/torrentstreamingtool)")

# ── Season ────────────────────────────────────────────────────────────────────

_NORTH = {12: "winter", 1: "winter", 2: "winter", 3: "spring", 4: "spring",
          5: "spring", 6: "summer", 7: "summer", 8: "summer", 9: "autumn",
          10: "autumn", 11: "autumn"}
_FLIP = {"winter": "summer", "summer": "winter", "spring": "autumn", "autumn": "spring"}


def season(d: date, lat: Optional[float] = None) -> str:
    """Meteorological season at latitude `lat` on `d`. "" in the tropics
    (|lat| < 15), where a season is not something a photo shows. No latitude
    is read as the northern hemisphere."""
    if lat is not None and abs(lat) < 15:
        return ""
    s = _NORTH[d.month]
    return _FLIP[s] if (lat is not None and lat < 0) else s


def month_gap(a: int, b: int) -> int:
    """Months between two calendar months, round the year (Dec→Jan is 1)."""
    g = abs(a - b) % 12
    return min(g, 12 - g)


# ── Text ──────────────────────────────────────────────────────────────────────

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def clean_text(s: str, cap: int = 120) -> str:
    """Commons' metadata is HTML (an artist is a link, a title can be a div).
    Strip it to one plain line of at most `cap` characters."""
    s = html.unescape(_TAG.sub(" ", s or ""))
    s = _WS.sub(" ", s).strip()
    if len(s) > cap:
        s = s[:cap - 1].rstrip() + "…"
    return s


def _title_from_file(title: str) -> str:
    """'File:Bridge of the Gods October 2019 001.jpg' → 'Bridge of the Gods October 2019'."""
    t = title.split(":", 1)[-1]
    t = re.sub(r"\.[A-Za-z0-9]{2,4}$", "", t)
    t = re.sub(r"[_]+", " ", t)
    t = re.sub(r"\s*[-(]?\s*\d{1,3}\)?$", "", t)       # a trailing sequence number (not a year)
    t = re.sub(r"^\d{1,4}\s+", "", t)                    # leading ones
    t = re.sub(r"\s+(photo|photograph|image) by .*$", "", t, flags=re.I)
    return clean_text(t)


_STAMP = re.compile(r"^\d{4}[-_.]?\d{2}[-_.]?\d{2}(?:[ ._T-]+\d{2}[-_.:]?\d{2}(?:[-_.:]?\d{2})?)?[\s_.-]*")
_SOURCE_TAIL = re.compile(r"\s+-\s+(?:flickr|panoramio)\b.*$|\s*\(\d{6,}\)$", re.I)


def tidy_title(t: str) -> str:
    """A caption a person would write, or "". Commons titles are often a
    camera's: a leading timestamp goes ('2012-03-21 23-20-56-circumpolaire' →
    'circumpolaire'), and one that is mostly a catalogue number
    ('Altenburg-310053-PSD') is dropped: the credit line still shows."""
    t = _SOURCE_TAIL.sub("", _STAMP.sub("", t or "")).strip(" -_.")
    t = re.sub(r"^\d{1,3}\s+(?=\D)", "", t)              # '002 Jabiru …' (a shoot's order)
    t = re.sub(r"[\s,_-]+\d{1,4}$", "", t) if re.search(r"\D[\s,_-]+\d{1,3}$", t) else t
    chars = [ch for ch in t if not ch.isspace()]
    if not chars or sum(ch.isdigit() for ch in chars) / len(chars) > 0.25:
        return ""
    return t[:1].upper() + t[1:]


# Words in a title that mark a photo nobody wants on the wall: an indoor shot,
# a specimen, a sign. Cheap and incomplete by design; the admin's
# "never show this" covers the rest.
_NOT_SCENIC = re.compile(
    r"\b(interior|inside|indoor|exhibit|exhibition|specimen|museum|aquarium|"
    r"plaque|signage|sign|logo|menu|portrait|selfie|detail|mugshot|"
    r"diagram|map|chart|poster|screenshot|"
    r"restaurant|cafe|café|bar|pub|tavern|shop|store|storefront|bakery|brewery|"
    r"hotel|lobby|office|classroom|kitchen|bathroom|restroom|parking)\b", re.I)


def scenic(title: str) -> bool:
    return not _NOT_SCENIC.search(title or "")


# ── Shape ─────────────────────────────────────────────────────────────────────

def shape_ok(w: int, h: int, min_w: int = 1280) -> bool:
    """Fills a TV: wide enough, landscape, and not a panorama strip."""
    if not w or not h or w < min_w:
        return False
    r = w / h
    return 1.25 <= r <= 2.4


def image_size(data: bytes) -> Optional[tuple]:
    """(width, height) of a JPEG or PNG from its header, else None. Used on the
    downloaded bytes, so a source that lies about its size, or answers with an
    HTML error page, is caught before the TV shows it."""
    if len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        return None
    i, n = 2, len(data)
    while i + 3 < n:
        if data[i] != 0xFF:
            return None
        m = data[i + 1]
        if m == 0xFF:                      # fill byte
            i += 1
            continue
        if m in (0xD8, 0x01) or 0xD0 <= m <= 0xD7:
            i += 2
            continue
        seg = int.from_bytes(data[i + 2:i + 4], "big")
        if m in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            if i + 9 > n:
                return None
            h = int.from_bytes(data[i + 5:i + 7], "big")
            w = int.from_bytes(data[i + 7:i + 9], "big")
            return w, h
        if seg < 2:
            return None
        i += 2 + seg
    return None


def file_name(photo_id: str) -> str:
    """The cache file a photo is stored under. Content-neutral (the id, not the
    bytes), so a photo already on disk is never downloaded again."""
    return hashlib.sha1(photo_id.encode("utf-8")).hexdigest()[:24] + ".jpg"


FILE_RE = re.compile(r"^[0-9a-f]{24}\.jpg$")

# ── Wikimedia Commons ─────────────────────────────────────────────────────────

COMMONS_API = "https://commons.wikimedia.org/w/api.php"
_COMMONS_META = "DateTimeOriginal|Artist|LicenseShortName|ObjectName"


def commons_search_url(search: str, limit: int = 50) -> str:
    """A File-namespace search returning a 1920-wide thumbnail and the metadata
    a credit needs. `gsrsort=random`: relevance order is alphabetical in
    practice, so every refresh would otherwise show the same A-to-C slice.
    1920 is one of Wikimedia's standard thumbnail widths (non-standard widths
    are refused)."""
    return COMMONS_API + "?" + urlencode({
        "action": "query", "format": "json", "generator": "search",
        "gsrnamespace": 6, "gsrlimit": limit, "gsrsort": "random",
        "gsrsearch": search, "prop": "imageinfo",
        "iiprop": "url|size|extmetadata", "iiurlwidth": 1920,
        "iiextmetadatafilter": _COMMONS_META,
    })


# Commons search terms, measured against Seattle and Hood River, OR (2026-10-22).
# A bare radius search returns flags and office lobbies; Quality Images alone
# (its bar is technical, not scenic) was a third restaurant interiors. Quality
# plus scenery words is the best and deepest, and its capture dates find this
# time of year (about one photo in five). The season's words find more of it
# but with no quality bar (plant close-ups, a bike race called "Fall Freeride"),
# so they only top up. A quality bar and an OR list together return nothing.
SEASON_TERMS = {"autumn": 'autumn OR foliage OR "fall colors" OR "fall color"',
                "winter": "winter OR snow OR snowfall",
                "spring": "spring OR blossom OR blossoms", "summer": "summer"}
SCENIC_TERMS = ("skyline OR view OR park OR sunset OR sunrise OR lake OR river OR bridge "
                "OR mountain OR waterfront OR beach OR garden OR panorama")


def city_searches(lat: float, lon: float, seas: str = "") -> list:
    """`[(search, seasonal)]` for the city theme, tried in order until enough
    photos turn up. `seasonal` marks a search whose words name the season, so
    a photo from it with no capture date still counts as this time of year.
    A small town has few photos, so the radius grows before the bar drops to
    Featured pictures of the wider area."""
    at = f"{lat:.4f},{lon:.4f}"
    out = [(f"nearcoord:{r}km,{at} incategory:Quality_images filew:>1919 {SCENIC_TERMS}", False)
           for r in (15, 40, 80)]
    if seas:
        out += [(f"nearcoord:{r}km,{at} filew:>1919 {SEASON_TERMS[seas]}", True) for r in (25, 60)]
    out.append((f"nearcoord:150km,{at} incategory:Featured_pictures_on_Wikimedia_Commons", False))
    return out


def nature_search(seas: str) -> str:
    """Featured pictures that mention the season (several hundred each; a
    narrower phrase such as "spring blossom" found five). In the tropics,
    featured landscapes."""
    if not seas:
        return "incategory:Featured_pictures_of_landscapes filew:>1919"
    return f"incategory:Featured_pictures_on_Wikimedia_Commons {seas} filew:>1919"


# About 260 photos between them. NASA's APOD API was the first choice and was
# dropped (2026-10-22): every date answered with the NASA logo as its "image",
# and the shared DEMO_KEY allows 10 requests an hour. See docs/EXTERNAL_SERVICES.md.
SPACE_SEARCHES = ("incategory:Featured_pictures_of_astronomy",
                  "incategory:Quality_images_of_astronomy",
                  "incategory:Images_by_the_James_Webb_Space_Telescope filew:>1919")


def potd_url(days: list) -> str:
    """Commons' picture of the day for each date in `days` (≤ 50), one request:
    the images used by each day's `Template:Potd/YYYY-MM-DD`."""
    titles = "|".join(f"Template:Potd/{d.isoformat()}" for d in days[:50])
    return COMMONS_API + "?" + urlencode({
        "action": "query", "format": "json", "generator": "images",
        "titles": titles, "gimlimit": 100, "prop": "imageinfo",
        "iiprop": "url|size|extmetadata", "iiurlwidth": 1920,
        "iiextmetadatafilter": _COMMONS_META,
    })


_MONTH_RE = re.compile(r"(\d{4})[-:](\d{2})")


def parse_commons(js: dict, theme: str) -> list:
    """Photos from a Commons imageinfo answer. Keeps only what fills a TV and
    reads as scenery; the credit is the author and the licence, as the licence
    requires."""
    out = []
    pages = ((js or {}).get("query") or {}).get("pages") or {}
    for p in pages.values():
        infos = p.get("imageinfo") or []
        if not infos:
            continue
        ii = infos[0]
        src = ii.get("thumburl") or ""
        w, h = int(ii.get("width") or 0), int(ii.get("height") or 0)
        if not src or not shape_ok(w, h, min_w=1600):
            continue
        meta = ii.get("extmetadata") or {}
        mv = lambda k: (meta.get(k) or {}).get("value") or ""   # noqa: E731
        raw_title = clean_text(mv("ObjectName")) or _title_from_file(p.get("title", ""))
        if not scenic(raw_title) or not scenic(p.get("title", "")):
            continue
        title = tidy_title(raw_title)
        m = _MONTH_RE.search(mv("DateTimeOriginal"))
        month = int(m.group(2)) if m and 1 <= int(m.group(2)) <= 12 else 0
        artist = clean_text(mv("Artist"), 60) or "Unknown author"
        lic = clean_text(mv("LicenseShortName"), 30)
        credit = " · ".join(x for x in (artist, lic, "Wikimedia Commons") if x)
        out.append({"id": "commons:" + p.get("title", ""), "theme": theme, "src": src,
                    "title": title, "credit": credit, "month": month, "date": ""})
    out.sort(key=lambda x: x["id"])
    return out


# ── Bing photo of the day ─────────────────────────────────────────────────────

# Country → Bing market. The market picks which photo runs that day and the
# language of its caption; it says nothing about where the photo was taken.
_BING_MKT = {"US": "en-US", "GB": "en-GB", "IE": "en-GB", "CA": "en-CA",
             "AU": "en-AU", "NZ": "en-NZ", "IN": "en-IN", "DE": "de-DE",
             "AT": "de-DE", "CH": "de-DE", "FR": "fr-FR", "BE": "fr-FR",
             "ES": "es-ES", "IT": "it-IT", "JP": "ja-JP", "CN": "zh-CN",
             "BR": "pt-BR"}


def bing_market(country_code: str) -> str:
    return _BING_MKT.get((country_code or "").upper(), "en-US")


def bing_urls(mkt: str) -> list:
    """Bing keeps about 15 days: idx 0..7 and idx 7..14 (n is capped at 8)."""
    base = "https://www.bing.com/HPImageArchive.aspx?format=js&n=8&mkt=" + quote(mkt)
    return [base + "&idx=0", base + "&idx=7"]


def parse_bing(js: dict) -> list:
    out = []
    for im in (js or {}).get("images") or []:
        base = im.get("urlbase") or ""
        sd = im.get("startdate") or ""
        if not base.startswith("/th?id=OHR.") or len(sd) != 8:
            continue
        cr = im.get("copyright") or ""
        title, _, who = cr.partition(" (©")
        who = who.rstrip(")").strip()
        credit = (f"© {who} · Bing" if who else "Bing")
        # The OHR name without the market suffix: one photo, one id, whatever
        # market it was fetched through.
        pid = base[len("/th?id="):].split("_")[0]
        out.append({"id": "bing:" + pid, "theme": "daily",
                    "src": "https://www.bing.com" + base + "_1920x1080.jpg",
                    "title": clean_text(title), "credit": clean_text(credit, 80),
                    "month": 0, "date": f"{sd[:4]}-{sd[4:6]}-{sd[6:]}"})
    return out


# ── Pexels (optional key) ─────────────────────────────────────────────────────

def pexels_query(city: dict, seas: str) -> str:
    """'Portland Oregon autumn'. The state/region keeps Portland, Maine out."""
    parts = [city.get("name") or "", city.get("admin1") or ""]
    if seas:
        parts.append(seas)
    return " ".join(p for p in parts if p).strip()


def pexels_url(query: str, per_page: int = 60) -> str:
    return "https://api.pexels.com/v1/search?" + urlencode({
        "query": query, "orientation": "landscape", "size": "large",
        "per_page": per_page})


def parse_pexels(js: dict, theme: str = "city") -> list:
    out = []
    for p in (js or {}).get("photos") or []:
        src = ((p.get("src") or {}).get("original") or "")
        w, h = int(p.get("width") or 0), int(p.get("height") or 0)
        if not src or not shape_ok(w, h, min_w=1600):
            continue
        who = clean_text(p.get("photographer") or "", 60)
        out.append({"id": f"pexels:{p.get('id')}", "theme": theme,
                    "src": src + "?auto=compress&cs=tinysrgb&w=1920",
                    "title": clean_text(p.get("alt") or ""),
                    "credit": (f"Photo by {who} on Pexels" if who else "Pexels"),
                    "month": 0, "date": ""})
    return out


# ── Your library (TMDb backdrops) ─────────────────────────────────────────────

def library_photos(rows: list, img_base: str) -> list:
    """`rows` = [(item_id, title, backdrop_path)] for items every viewer may see
    (the engine leaves out admin-only ones). Not downloaded: they are shown
    through the TMDb artwork proxy, which already caches them and applies
    TMDb's six-month rule, so this adds no second store of TMDb data."""
    out, seen = [], set()
    for item_id, title, bp in rows:
        fn = (bp or "").lstrip("/")
        if not fn or "/" in fn or fn in seen:
            continue
        seen.add(fn)
        out.append({"id": f"library:{item_id}", "theme": "library",
                    "src": f"{img_base}/original/{fn}", "title": clean_text(title),
                    "credit": "Image: TMDb", "month": 0, "date": ""})
    return out


# ── Weather (Open-Meteo, no key) ──────────────────────────────────────────────

# Where people read °F. Everywhere else reads °C.
_FAHRENHEIT = {"US", "PR", "GU", "VI", "AS", "MP", "UM", "BS", "BZ", "KY", "PW", "FM", "MH", "LR"}


def temp_unit(setting: str, country_code: str) -> str:
    """"F" or "C". `setting` is the admin's "auto" | "f" | "c"."""
    if setting in ("f", "c"):
        return setting.upper()
    return "F" if (country_code or "").upper() in _FAHRENHEIT else "C"


def weather_url(lat: float, lon: float, unit: str) -> str:
    """Now (temperature, condition, day or night) and today (high, low, the
    likeliest chance of rain), in the city's own time zone."""
    return "https://api.open-meteo.com/v1/forecast?" + urlencode({
        "latitude": f"{lat:.4f}", "longitude": f"{lon:.4f}",
        "current": "temperature_2m,weather_code,is_day",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
        "timezone": "auto", "forecast_days": 1,
        "temperature_unit": "fahrenheit" if unit == "F" else "celsius"})


# WMO weather interpretation codes → (label, icon). The icon names are the
# `wx-*` symbols in static/index.html's sprite.
_WMO = {
    0: ("Clear", "sun"), 1: ("Mostly clear", "sun"), 2: ("Partly cloudy", "partly"),
    3: ("Cloudy", "cloud"), 45: ("Fog", "fog"), 48: ("Freezing fog", "fog"),
    51: ("Light drizzle", "rain"), 53: ("Drizzle", "rain"), 55: ("Heavy drizzle", "rain"),
    56: ("Freezing drizzle", "rain"), 57: ("Freezing drizzle", "rain"),
    61: ("Light rain", "rain"), 63: ("Rain", "rain"), 65: ("Heavy rain", "rain"),
    66: ("Freezing rain", "rain"), 67: ("Freezing rain", "rain"),
    71: ("Light snow", "snow"), 73: ("Snow", "snow"), 75: ("Heavy snow", "snow"),
    77: ("Snow grains", "snow"), 80: ("Showers", "rain"), 81: ("Showers", "rain"),
    82: ("Heavy showers", "rain"), 85: ("Snow showers", "snow"), 86: ("Snow showers", "snow"),
    95: ("Thunderstorm", "storm"), 96: ("Thunderstorm, hail", "storm"),
    99: ("Thunderstorm, hail", "storm"),
}


def condition(code, is_day: bool = True) -> tuple:
    """(label, icon) for a WMO code. A clear sky by day is "Sunny"; by night the
    sun becomes the moon and a part-cloudy sky just a cloud."""
    label, icon = _WMO.get(code if isinstance(code, int) else -1, ("", "cloud"))
    if not is_day:
        icon = {"sun": "moon", "partly": "cloud"}.get(icon, icon)
    elif code == 0:
        label = "Sunny"
    return label, icon


def parse_weather(js: dict, unit: str) -> Optional[dict]:
    """`{temp, unit, label, icon, hi, lo, rain}`, rounded for a TV, or None
    when the answer lacks the current temperature. A missing daily value is
    None (the page leaves it out), never a made-up 0."""
    cur = (js or {}).get("current") or {}
    daily = (js or {}).get("daily") or {}
    t = cur.get("temperature_2m")
    if not isinstance(t, (int, float)):
        return None

    def first(k):
        v = daily.get(k) or []
        return round(v[0]) if v and isinstance(v[0], (int, float)) else None

    code = cur.get("weather_code")
    label, icon = condition(code, bool(cur.get("is_day", 1)))
    return {"temp": round(t), "unit": unit, "label": label, "icon": icon,
            "hi": first("temperature_2m_max"), "lo": first("temperature_2m_min"),
            "rain": first("precipitation_probability_max")}


# ── Choosing ──────────────────────────────────────────────────────────────────

def pick_city(photos: list, today: date, seas: str, cap: int = PER_THEME) -> list:
    """This time of year first. A photo within a month of today's month counts;
    if that leaves fewer than MIN_CITY, two months; then any photo with a known
    month, then the rest. In the tropics (no season) every photo is equal."""
    photos = diverse(photos)
    if not seas:
        return photos[:cap]
    m = today.month
    near = [p for p in photos if p["month"] and month_gap(p["month"], m) <= 1]
    if len(near) < MIN_CITY:
        near += [p for p in photos if p["month"] and month_gap(p["month"], m) == 2]
    rest = [p for p in photos if p not in near]
    rest.sort(key=lambda p: (0 if p["month"] else 1, month_gap(p["month"] or m, m)))
    return (near + rest)[:cap]


def series_key(title: str) -> str:
    """Photos of one shoot share a caption but for a number ('Fall 2007 023',
    'Fall 2007 024'). The caption with its digits and punctuation gone."""
    return " ".join(re.sub(r"[\d\W_]+", " ", (title or "").lower()).split()[:5])


def diverse(photos: list, per: int = 2) -> list:
    """At most `per` photos of one shoot, order kept. Untitled photos are each
    their own."""
    seen: dict = {}
    out = []
    for p in photos:
        k = series_key(p.get("title", ""))
        if k:
            seen[k] = seen.get(k, 0) + 1
            if seen[k] > per:
                continue
        out.append(p)
    return out


def merge_dated(old: list, new: list, cap: int = PER_THEME) -> list:
    """A photo-of-the-day pool grows a day at a time: keep what was fetched
    before, add today's, newest first, `cap` at most. Undated entries (Commons'
    picture of the day, which arrives without its date) are this fetch's only,
    and hold up to half the cap, so a month of Bing can't crowd them out."""
    by_id = {p["id"]: p for p in old}
    by_id.update({p["id"]: p for p in new})
    undated = [p for p in new if not p.get("date")][:cap // 2]
    dated = sorted((p for p in by_id.values() if p.get("date")),
                   key=lambda p: p["date"], reverse=True)
    return dated[:cap - len(undated)] + undated


def playlist(pools: dict, themes: list, hidden, seed: int) -> list:
    """The order the TV shows them in: each theme shuffled (a new order every
    day, through `seed`), then dealt round-robin so the screen mixes themes
    instead of showing forty of one and then forty of the next. Hidden photos
    and duplicates are dropped."""
    rng = random.Random(seed)
    hidden = set(hidden or ())
    decks = []
    for t in themes:
        deck = [p for p in pools.get(t) or [] if p["id"] not in hidden]
        rng.shuffle(deck)
        if deck:
            decks.append(deck)
    out, seen = [], set()
    while decks:
        for deck in list(decks):
            p = deck.pop()
            if p["id"] not in seen:
                seen.add(p["id"])
                out.append(p)
            if not deck:
                decks.remove(deck)
    return out


def day_seed(d: date) -> int:
    return d.toordinal()


def recent_days(today: date, n: int) -> list:
    return [today - timedelta(days=i) for i in range(n)]


def signature(themes: list, city: Optional[dict], seas: str, pexels: bool) -> str:
    """What a pool was built for. A change of any of these (a new city, the
    season turning, a Pexels key added) makes the next refresh start over
    instead of waiting for the daily one."""
    c = city or {}
    return "|".join([",".join(sorted(themes)), f"{c.get('lat', '')}", f"{c.get('lon', '')}",
                     seas, "pexels" if pexels else ""])
