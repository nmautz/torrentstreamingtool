"""What is each device doing? Rules that turn raw requests into a readable history.

The admin Devices tab has two views of one device: the RAW view is every request
it sent (method, path, status, time), and the SIMPLE view is what a person would
say it was doing -- "Watching Frieren · S01E12", "Searched 'dune'", "Deleted 3
episodes". This module is the translation, and nothing else:

  * `classify(method, path, query)` -> `(kind, ref)`: which activity a request
    belongs to, and the thing it is about (an item id, a bundle key, a search
    term). Resolving a ref to a NAME needs the library, so `main.py` does that.
  * `extends(...)`: consecutive requests of the same kind about the same thing
    collapse into one activity row with a start, an end and a count. A phone
    streaming an episode sends a segment every few seconds; the simple view must
    show one line, not six hundred.
  * `redact_query(...)`: tokens, PINs and passwords never reach storage. Several
    endpoints take a token in the query string (`?device_token=`), and the raw
    view is precisely where one would otherwise sit in plain text forever.
  * `ua_summary(...)`, `presence(...)`: labels for the device list.

Kinds in `NOISE` (polling, the event stream, page assets) are recorded raw but are
never a device's "current activity": a dashboard left open polls forever, and
that must not hide the episode the same phone was streaming a minute ago.

Pure: stdlib only, no `main` import. Tests in `tests/test_devactivity.py`.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode

# Consecutive same-activity requests further apart than this start a new row.
# Five minutes rides out a paused episode and a phone locking between segments;
# longer and two separate evenings of the same show merge into one line.
MERGE_GAP_SEC = 300

# Presence windows for the device list.
ACTIVE_SEC = 120        # "Active now": a request in the last 2 minutes, or an open event stream
RECENT_SEC = 30 * 60    # "Recently active": within half an hour

# Kinds that are real but never "what the device is doing".
NOISE = frozenset({"poll", "asset", "open"})

_SECRET_KEY = re.compile(r"(token|password|passwd|pass|pin|secret|key|auth|sig)", re.I)

_ITEM = r"(?P<item>[A-Za-z0-9_-]{4,64})"

# (method or None for any, compiled path regex, kind). First match wins, so the
# specific rules sit above the general ones. `item`, `bundle`, `od`, `series`
# and `profile` named groups become the ref.
_RULES = [
    (None, r"^/healthz$", "poll"),
    (None, r"^/api/(state|events|version|player-manifest|setup-status|offline-active|pair/status)$", "poll"),
    ("GET", r"^/api/playback/sessions$", "poll"),
    (None, r"^/api/diag/", "diag"),
    (None, r"^/api/library/offline-cache/(?P<bundle>[0-9a-f]{8,64})/", "stream"),
    (None, r"^/api/library/ondemand/(?P<od>[^/]+)/close$", "stop"),
    (None, r"^/api/library/ondemand/(?P<od>[^/]+)/", "stream"),
    ("POST", r"^/api/playback/session$", "watch"),
    ("POST", r"^/api/playback/session/[^/]+/yield$", "handoff"),
    ("POST", r"^/api/playback/pull$", "handoff"),
    ("POST", r"^/api/library/" + _ITEM + r"/progress$", "watch"),
    ("POST", r"^/api/library/" + _ITEM + r"/(local-tracks|shuffle-pref)$", "watch"),
    ("GET", r"^/api/library/" + _ITEM + r"/(bundle-manifest|download|download-zip)$", "save"),
    ("POST", r"^/api/library/" + _ITEM + r"/download-zip$", "save"),
    (None, r"^/api/sync/", "sync"),
    ("GET", r"^/api/(search|tmdb/search|tmdb/lookup|subtitles/search)$", "search"),
    ("GET", r"^/api/tmdb/(explore|genres|watch)$", "browse"),
    ("POST", r"^/api/library/watched-purge$", "delete"),
    ("POST", r"^/api/library/bulk-delete$", "delete"),
    ("DELETE", r"^/api/library/" + _ITEM + r"$", "delete"),
    ("POST", r"^/api/library/" + _ITEM + r"/delete-files$", "delete"),
    ("POST", r"^/api/library/" + _ITEM + r"/mark-watched$", "mark"),
    ("POST", r"^/api/library/" + _ITEM + r"/(play|queue-play|stream-file|stream-ondemand)$", "play"),
    ("POST", r"^/api/library/(play-now|stream-now|shuffle|unshuffle)$", "play"),
    (None, r"^/api/(vlc|youtube|skip-now|resume-now|stop|window-control|tv-local|trailer)(/|$)", "tv"),
    ("POST", r"^/api/(stream|stream/prepare|stream/race|stream/save-to-library|library/download|library/pack-fetch|torrent/inspect|retry)$", "download"),
    ("POST", r"^/api/library/" + _ITEM + r"/(download-schedule|file-schedule|recheck)$", "download"),
    ("POST", r"^/api/library/(prepare|upload)$", "download"),
    ("POST", r"^/api/profiles/(?P<profile>[^/]+)/verify-pin$", "sign-in"),
    ("POST", r"^/api/admin/login$", "sign-in"),
    (None, r"^/api/pair$", "pair"),
    (None, r"^/api/admin(/|$)", "admin"),
    ("GET", r"^/api/library/series/(?P<series>[^/]+)$", "browse"),
    ("GET", r"^/api/library/" + _ITEM + r"/(files|metadata|subs|skip-data|saved-tracks|prep-status)$", "browse"),
    ("GET", r"^/api/library(/groups|/group/.*|/coverage)?$", "browse"),
    ("GET", r"^/(|index\.html|tv|admin)$", "open"),
    ("GET", r"^/(static|vendor|icons|fonts)/", "asset"),
    ("GET", r"^/(favicon\.ico|manifest\.json|apple-touch-icon.*|robots\.txt)$", "asset"),
    ("GET", r"^/api/metadata/img/", "asset"),
]
_RULES = [(m, re.compile(p), k) for (m, p, k) in _RULES]


def classify(method: str, path: str, query: str = "") -> tuple:
    """`(kind, ref)` for one request. `ref` is a small dict naming what it's
    about -- `{"item": id}`, `{"bundle": key}`, `{"od": key}`, `{"series": key}`,
    `{"q": term}` -- or `{}`."""
    method = (method or "GET").upper()
    path = path or "/"
    for m, rx, kind in _RULES:
        if m and m != method:
            continue
        hit = rx.match(path)
        if not hit:
            continue
        ref = {k: v for k, v in hit.groupdict().items() if v}
        if kind == "search":
            q = _query_param(query, ("q", "query", "title"))
            if q:
                ref["q"] = q[:120]
        return kind, ref
    if path.startswith("/api/"):
        return "other", {"what": "/".join(path.split("/")[2:4])}
    return "asset", {}


def _query_param(query: str, names) -> str:
    try:
        pairs = parse_qsl(query or "", keep_blank_values=False)
    except ValueError:
        return ""
    for k, v in pairs:
        if k in names and v.strip():
            return v.strip()
    return ""


def redact_query(query: str) -> str:
    """The query string with every secret-looking parameter's VALUE replaced."""
    if not query:
        return ""
    try:
        pairs = parse_qsl(query, keep_blank_values=True)
    except ValueError:
        return "(unparseable)"
    return urlencode([(k, "***" if _SECRET_KEY.search(k) else v) for k, v in pairs], safe="*")[:500]


def ua_summary(ua: str) -> str:
    """A short "Platform · Client" label from a User-Agent string."""
    ua = ua or ""
    low = ua.lower()
    if not ua:
        return "Unknown client"
    if "applecoremedia" in low or "avplayer" in low or low.startswith("appletv"):
        return "iOS media player"
    if "streamlink" in low and ("darwin" in low or "cfnetwork" in low):
        return "iOS app (native)"
    if "cfnetwork" in low:
        return "iOS app (native)"
    if low.startswith("vlc") or "libvlc" in low:
        return "VLC"
    if low.startswith(("curl/", "python-", "httpx", "wget/", "go-http")):
        return ua.split("/")[0]
    plat = ("iPhone" if "iphone" in low else "iPad" if "ipad" in low
            else "Android" if "android" in low else "Windows" if "windows" in low
            else "Mac" if "macintosh" in low or "mac os x" in low
            else "Linux" if "linux" in low or "x11" in low
            else "CrOS" if "cros" in low else "")
    client = ("Edge" if "edg/" in low else "Firefox" if "firefox/" in low
              else "Chrome" if ("chrome/" in low or "crios/" in low)
              else "Safari" if "safari/" in low else "")
    if plat in ("iPhone", "iPad") and not client:
        client = "App"          # WKWebView: no "Safari/" token
    return " · ".join(x for x in (plat, client) if x) or ua[:40]


def extends(prev, kind: str, key: str, ts: float, gap: float = MERGE_GAP_SEC) -> bool:
    """Does a request of `kind` about `key` at `ts` continue activity `prev`?

    `prev` is `{kind, key, end}` or None. Same kind, same subject, close in time.
    "stream" and "watch" are one activity: the segments and the heartbeat of the
    same episode are the same thing seen from two sides.
    """
    if not prev:
        return False
    if ts - float(prev.get("end") or 0) > gap:
        return False
    a, b = _canon(prev.get("kind")), _canon(kind)
    if a != b:
        return False
    # An unnamed request (a heartbeat before the title is known) continues
    # whatever named activity of the same kind is running.
    return not key or not prev.get("key") or prev.get("key") == key


def _canon(kind):
    return "watch" if kind in ("stream", "watch") else kind


def canonical_kind(kind: str) -> str:
    return _canon(kind)


_VERB = {
    "watch":    "Watching",
    "stop":     "Stopped",
    "handoff":  "Moved playback",
    "save":     "Saved to device",
    "sync":     "Synced watch progress",
    "search":   "Searched",
    "browse":   "Browsing",
    "delete":   "Deleted",
    "mark":     "Marked watched/unwatched",
    "play":     "Played on the TV",
    "tv":       "Controlling the TV",
    "download": "Downloading",
    "sign-in":  "Signed in",
    "pair":     "Paired the app",
    "admin":    "Admin panel",
    "diag":     "Sent diagnostics",
    "open":     "Opened the dashboard",
    "poll":     "Dashboard open",
    "asset":    "Loading the page",
    "other":    "Other",
}


def describe(kind: str, subject: str = "", count: int = 1) -> str:
    """One human line for an activity: "Watching Frieren · S01E12"."""
    kind = _canon(kind)
    verb = _VERB.get(kind, kind.title())
    if kind == "search" and subject:
        return f"{verb} “{subject}”"
    if kind == "browse" and not subject:
        return "Browsing the library"
    if subject:
        return f"{verb} {subject}"
    return verb


def presence(last_seen: float, now: float, connected: int = 0) -> str:
    """"active" | "recent" | "idle" for the device list."""
    if connected > 0 or (last_seen and now - last_seen <= ACTIVE_SEC):
        return "active"
    if last_seen and now - last_seen <= RECENT_SEC:
        return "recent"
    return "idle"
