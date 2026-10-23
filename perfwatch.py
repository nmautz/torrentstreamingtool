"""Which requests a person waits on, and how long they took (20.26.11, #104).

Leaf module: stdlib only, imports nothing else in this repo. `diagnostics.py`
feeds it every finished request; the freeze watch judges what it reports.

The freeze standard catches the server not answering at all. This is the other
half: a request that is answered, but slowly enough to notice. The user set the
bar (2026-10-22): an interactive request of 500 ms or more is worth a line, the
same endpoint doing it three times in a window is a finding, and one of 2 s or
more is a finding on its own.

"Interactive" is the point. A video segment, the live-update stream, a log
upload and a phone's bundle download are slow by nature or by size, and judging
them by the same bar would bury the requests that make a screen wait. `classify`
sorts each route into a kind; only `ui` is judged. The others are still counted,
so their timings show in the summary.
"""
from __future__ import annotations

import threading
from typing import Optional

SLOW_UI_S = 0.5          # an interactive request this slow gets a SLOW UI line
HARD_UI_S = 2.0          # ... and one this slow is a finding on its own
SAMPLES_PER_ROUTE = 600  # per summary period; enough for a stable p95

# Route templates (FastAPI's `route.path`) by kind. A rule ending in "/" is a
# prefix; any other rule matches itself and anything under it.
_MEDIA = ("/api/library/offline-cache/", "/StreamLinkBundles/", "/api/library/ondemand/",
          "/api/metadata/img/", "/api/ambient/img/", "/api/library/clip/", "(static)")
_STREAM = ("/api/events",)
_UPLOAD = ("/api/diag/client-log",)
# Waits on someone else's server (Jackett's indexers, TMDb, OpenSubtitles): the
# box can't make those faster, so they are reported, not judged.
_EXTERNAL = ("/api/search", "/api/subtitles/search", "/api/library/{item_id}/subtitles/search",
             "/api/admin/idle-screen/geocode", "/api/ambient/weather",
             "/api/library/{item_id}/metadata/search", "/api/tmdb/search",
             "/api/admin/indexers/{indexer_id}/test", "/api/admin/indexers/test-all")
# A phone downloading a bundle: a background wait, timed end to end elsewhere.
_BACKGROUND = ("/api/library/{item_id}/bundle-manifest",)
_BULK = ("/api/admin/logs", "/api/admin/updater/", "/api/library/{item_id}/download-zip",
         "/api/admin/client-logs", "/api/admin/analyzer-log")


def _matches(tmpl: str, rules: tuple) -> bool:
    for r in rules:
        if r.endswith("/") and tmpl.startswith(r):
            return True
        if tmpl == r or (not r.endswith("/") and tmpl.startswith(r + "/")):
            return True
    return False


def classify(method: str, tmpl: str) -> str:
    """`ui` | `media` | `stream` | `upload` | `external` | `background` | `bulk` | `probe`."""
    if tmpl == "/healthz":                  # the server's own self-probe, every 10 s
        return "probe"
    if _matches(tmpl, _STREAM):
        return "stream"
    if _matches(tmpl, _MEDIA):
        return "media"
    if _matches(tmpl, _UPLOAD):
        return "upload"
    if _matches(tmpl, _BULK):
        return "bulk"
    if _matches(tmpl, _EXTERNAL):
        return "external"
    if _matches(tmpl, _BACKGROUND):
        return "background"
    return "ui"


def slow_line(method: str, tmpl: str, dur: float, status: int, cost: Optional[dict]) -> str:
    """`SLOW UI 0.62s GET /api/library/{item_id}/files status=200 lock=0.40s lib=0.05s pool=0.00s other=0.17s`.

    `other` is what is left: the handler's own work, disk calls it makes on its
    own threads, network round trips (qBit, TMDb)."""
    c = cost or {}
    lock, lib, pool = c.get("lock", 0.0), c.get("lib", 0.0), c.get("pool", 0.0)
    other = max(0.0, dur - lock - lib - pool)
    return (f"SLOW UI {dur:.2f}s {method} {tmpl} status={status} "
            f"lock={lock:.2f}s lib={lib:.2f}s pool={pool:.2f}s other={other:.2f}s")


def _pct(sorted_vals: list, q: float) -> float:
    if not sorted_vals:
        return 0.0
    i = min(len(sorted_vals) - 1, max(0, int(round(q * (len(sorted_vals) - 1)))))
    return sorted_vals[i]


class Stats:
    """Per-route durations for one summary period. Thread-safe, bounded."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rows: dict = {}           # (kind, method tmpl) -> [count, samples, max]

    def record(self, kind: str, method: str, tmpl: str, dur: float) -> None:
        key = (kind, f"{method} {tmpl}")
        with self._lock:
            row = self._rows.get(key)
            if row is None:
                row = self._rows[key] = [0, [], 0.0]
            row[0] += 1
            if len(row[1]) < SAMPLES_PER_ROUTE:
                row[1].append(dur)
            else:                       # keep a spread, not just the first N
                row[1][row[0] % SAMPLES_PER_ROUTE] = dur
            if dur > row[2]:
                row[2] = dur

    def drain(self) -> list:
        """`[{kind, route, n, p50, p95, max}]` since the last drain, slowest p95 first."""
        with self._lock:
            rows, self._rows = self._rows, {}
        out = []
        for (kind, route), (n, samples, mx) in rows.items():
            s = sorted(samples)
            out.append({"kind": kind, "route": route, "n": n,
                        "p50": _pct(s, 0.50), "p95": _pct(s, 0.95), "max": mx})
        out.sort(key=lambda r: r["p95"], reverse=True)
        return out


def summary_line(rows: list, minutes: float, top: int = 8) -> Optional[str]:
    """`REQ STATS 5m ui n=412: GET /api/library n=40 p50=30ms p95=210ms max=800ms; ...`
    for the slowest interactive routes, plus one total per other kind."""
    if not rows:
        return None
    ui = [r for r in rows if r["kind"] == "ui"]
    parts = [f"{r['route']} n={r['n']} p50={r['p50'] * 1000:.0f}ms "
             f"p95={r['p95'] * 1000:.0f}ms max={r['max'] * 1000:.0f}ms" for r in ui[:top]]
    others = {}
    for r in rows:
        if r["kind"] != "ui":
            o = others.setdefault(r["kind"], [0, 0.0])
            o[0] += r["n"]
            o[1] = max(o[1], r["max"])
    tail = " | ".join(f"{k} n={n} max={mx * 1000:.0f}ms" for k, (n, mx) in sorted(others.items()))
    head = f"REQ STATS {minutes:.0f}m ui n={sum(r['n'] for r in ui)}: "
    return head + ("; ".join(parts) or "-") + (f" | {tail}" if tail else "")
