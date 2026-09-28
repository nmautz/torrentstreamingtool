"""Unit tests for `devactivity.py` and `devstore.py`. Run with plain python, no deps:

    python tests/test_devactivity.py      (or `make test`)

Two modules, one file: the rules that turn a device's raw requests into a
readable history, and the SQLite store that keeps both. The classification cases
are real paths from main.py's route table; the redaction cases are the query
strings that actually carry a secret today (`?device_token=`, `?profile_token=`).
"""

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import devactivity as da         # noqa: E402
import devstore                  # noqa: E402

_PASS = 0
_FAIL = []


def eq(name, got, want):
    global _PASS
    if got == want:
        _PASS += 1
    else:
        _FAIL.append("%s\n     got:  %r\n     want: %r" % (name, got, want))


# ── classify ─────────────────────────────────────────────────────────────────
C = da.classify
eq("segment", C("GET", "/api/library/offline-cache/0123456789abcdef01234567/seg_video_00042.m4s"),
   ("stream", {"bundle": "0123456789abcdef01234567"}))
eq("master playlist", C("GET", "/api/library/offline-cache/0123456789abcdef01234567/master.m3u8")[0], "stream")
eq("ondemand segment", C("GET", "/api/library/ondemand/abc123/seg_1.ts"), ("stream", {"od": "abc123"}))
eq("ondemand close is a stop, not a stream", C("POST", "/api/library/ondemand/abc123/close")[0], "stop")
eq("heartbeat", C("POST", "/api/playback/session"), ("watch", {}))
eq("sessions list is polling", C("GET", "/api/playback/sessions")[0], "poll")
eq("progress", C("POST", "/api/library/item1234/progress"), ("watch", {"item": "item1234"}))
eq("search with q", C("GET", "/api/search", "q=dune+part+two&cat=all"), ("search", {"q": "dune part two"}))
eq("search without q", C("GET", "/api/search", ""), ("search", {}))
eq("tmdb search", C("GET", "/api/tmdb/search", "query=frieren")[1], {"q": "frieren"})
eq("show page", C("GET", "/api/library/item1234/files"), ("browse", {"item": "item1234"}))
eq("series page", C("GET", "/api/library/series/series%3Afrieren"), ("browse", {"series": "series%3Afrieren"}))
eq("library grid", C("GET", "/api/library"), ("browse", {}))
eq("delete item", C("DELETE", "/api/library/item1234"), ("delete", {"item": "item1234"}))
eq("GET item is not a delete", C("GET", "/api/library/item1234")[0] != "delete", True)
eq("delete files", C("POST", "/api/library/item1234/delete-files")[0], "delete")
eq("watched purge", C("POST", "/api/library/watched-purge")[0], "delete")
eq("watched purge preview is not a delete", C("POST", "/api/library/watched-purge/preview")[0] != "delete", True)
eq("save to phone", C("GET", "/api/library/item1234/bundle-manifest"), ("save", {"item": "item1234"}))
eq("play on TV", C("POST", "/api/library/item1234/play"), ("play", {"item": "item1234"}))
eq("vlc pause", C("POST", "/api/vlc/pause")[0], "tv")
eq("youtube", C("POST", "/api/youtube")[0], "tv")
eq("add torrent", C("POST", "/api/stream")[0], "download")
eq("pin", C("POST", "/api/profiles/p1/verify-pin"), ("sign-in", {"profile": "p1"}))
eq("admin login", C("POST", "/api/admin/login")[0], "sign-in")
eq("admin anything", C("GET", "/api/admin/devices")[0], "admin")
eq("state poll", C("GET", "/api/state")[0], "poll")
eq("events", C("GET", "/api/events")[0], "poll")
eq("healthz", C("GET", "/healthz")[0], "poll")
eq("dashboard doc", C("GET", "/")[0], "open")
eq("vendor asset", C("GET", "/vendor/hls.min.js")[0], "asset")
eq("poster", C("GET", "/api/metadata/img/w342/abc.jpg")[0], "asset")
eq("sync", C("POST", "/api/sync/progress")[0], "sync")
eq("unknown api", C("GET", "/api/settings/disk-space"), ("other", {"what": "settings/disk-space"}))
eq("client log", C("POST", "/api/diag/client-log")[0], "diag")
eq("method is case-insensitive", C("delete", "/api/library/item1234")[0], "delete")
eq("long search term clipped", len(C("GET", "/api/search", "q=" + "x" * 300)[1]["q"]), 120)

# ── redact_query ─────────────────────────────────────────────────────────────
eq("device token", da.redact_query("device_token=SECRET&x=1"), "device_token=***&x=1")
eq("profile token", "SECRET" not in da.redact_query("profile_token=SECRET"), True)
eq("password", "hunter2" not in da.redact_query("password=hunter2"), True)
eq("pin", "1234" not in da.redact_query("pin=1234"), True)
eq("api key", "abc" not in da.redact_query("apikey=abc"), True)
eq("harmless kept", da.redact_query("q=dune&page=2"), "q=dune&page=2")
eq("empty", da.redact_query(""), "")

# ── ua_summary ───────────────────────────────────────────────────────────────
U = da.ua_summary
eq("iphone safari", U("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1"), "iPhone · Safari")
eq("iphone wkwebview", U("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148"), "iPhone · App")
eq("avplayer", U("AppleCoreMedia/1.0.0.22A3354 (iPhone; U; CPU OS 18_0 like Mac OS X; en_us)"), "iOS media player")
eq("cfnetwork", U("App/1 CFNetwork/1568.100.1 Darwin/24.0.0"), "iOS app (native)")
eq("windows chrome", U("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36"), "Windows · Chrome")
eq("windows edge", U("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36 Edg/129.0"), "Windows · Edge")
eq("mac firefox", U("Mozilla/5.0 (Macintosh; Intel Mac OS X 14.0; rv:130.0) Gecko/20100101 Firefox/130.0"), "Mac · Firefox")
eq("android chrome", U("Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Mobile Safari/537.36"), "Android · Chrome")
eq("curl", U("curl/8.4.0"), "curl")
eq("empty", U(""), "Unknown client")

# ── extends ──────────────────────────────────────────────────────────────────
prev = {"kind": "stream", "key": "b:k1", "end": 1000.0}
eq("same stream continues", da.extends(prev, "stream", "b:k1", 1010), True)
eq("heartbeat continues a stream", da.extends(prev, "watch", "b:k1", 1010), True)
eq("unnamed heartbeat continues", da.extends(prev, "watch", "", 1010), True)
eq("other episode breaks", da.extends(prev, "stream", "b:k2", 1010), False)
eq("gap breaks", da.extends(prev, "stream", "b:k1", 1000 + da.MERGE_GAP_SEC + 1), False)
eq("other kind breaks", da.extends(prev, "search", "", 1010), False)
eq("no prev", da.extends(None, "stream", "b:k1", 1010), False)

# ── describe / presence ──────────────────────────────────────────────────────
eq("watch", da.describe("stream", "Frieren · S01E12"), "Watching Frieren · S01E12")
eq("search", da.describe("search", "dune"), "Searched “dune”")
eq("browse library", da.describe("browse"), "Browsing the library")
eq("browse show", da.describe("browse", "Frieren"), "Browsing Frieren")
eq("active by time", da.presence(1000, 1000 + da.ACTIVE_SEC), "active")
eq("active by stream", da.presence(0, 10 ** 9, connected=1), "active")
eq("recent", da.presence(1000, 1000 + da.ACTIVE_SEC + 1), "recent")
eq("idle", da.presence(1000, 1000 + da.RECENT_SEC + 1), "idle")
eq("never seen", da.presence(0, 1000), "idle")

# ── devstore ─────────────────────────────────────────────────────────────────
tmp = tempfile.mkdtemp()
try:
    st = devstore.DeviceStore(os.path.join(tmp, "sub", "a.sqlite3"))
    dev = {"id": "d1", "name": "Phone", "ua": "UA", "ua_summary": "iPhone · App", "ip": "10.0.0.2",
           "via": "cookie", "first_seen": 100.0, "last_seen": 200.0, "requests": 3, "profile_id": "p1"}
    reqs = [{"device_id": "d1", "ts": 100.0 + i, "method": "GET", "path": f"/p{i}", "query": "",
             "status": 200, "ms": 1.5, "ip": "10.0.0.2", "ua": "UA", "profile_id": "p1",
             "kind": "browse", "via": "cookie"} for i in range(3)]
    ids = st.write(requests=reqs, devices=[dev],
                   activities=[{"device_id": "d1", "kind": "browse", "key": "", "subject": "",
                                "start": 100.0, "end": 102.0, "count": 3}],
                   tokens=[("th1", "d1", 100.0)])
    eq("activity id returned", len(ids) == 1 and ids[0] > 0, True)
    st.write(devices=[{**dev, "name": "", "first_seen": 300.0, "last_seen": 400.0, "requests": 2}],
             activities=[{"id": ids[0], "device_id": "d1", "kind": "browse", "start": 100.0,
                          "end": 400.0, "count": 5, "subject": "Frieren"}])
    d = st.device("d1")
    eq("requests summed", d["requests"], 5)
    eq("first_seen kept", d["first_seen"], 100.0)
    eq("last_seen advanced", d["last_seen"], 400.0)
    eq("empty name does not erase", d["name"], "Phone")
    eq("activity extended in place", [(a["count"], a["subject"]) for a in st.activities("d1")], [(5, "Frieren")])
    eq("raw newest first", [r["path"] for r in st.requests("d1")], ["/p2", "/p1", "/p0"])
    eq("raw paged", [r["path"] for r in st.requests("d1", before=102.0)], ["/p1", "/p0"])
    eq("raw search", [r["path"] for r in st.requests("d1", q="p1")], ["/p1"])
    eq("token map", st.token_map(), {"th1": "d1"})
    eq("label", st.set_label("d1", "Mum's iPad") and st.device("d1")["label"], "Mum's iPad")
    eq("label unknown device", st.set_label("nope", "x"), False)
    eq("last activity", st.last_activities(("poll",))["d1"]["subject"], "Frieren")
    eq("last activity excludes noise", st.last_activities(("browse",)), {})
    # retention: devices never go; raw by age, then by count
    pr = st.prune(now=100.0 + 1.5 * 86400, raw_days=1, act_days=10)
    eq("raw pruned by age", pr["requests"], 3)
    eq("device survives prune", st.device("d1") is not None, True)
    st.write(requests=[{**reqs[0], "ts": 1000.0 + i, "path": f"/n{i}"} for i in range(10)])
    st.prune(now=1010.0, raw_days=30, max_raw_rows=4)
    eq("raw capped by count, newest kept", [r["path"] for r in st.requests("d1")], ["/n9", "/n8", "/n7", "/n6"])
    eq("stats", st.stats()["devices"], 1)
    eq("forget", st.forget("d1") > 0 and st.device("d1") is None and st.requests("d1") == [], True)
    st.close()
finally:
    shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    for f in _FAIL:
        print("FAIL", f)
    print("devactivity: %d passed, %d failed" % (_PASS, len(_FAIL)))
    sys.exit(1 if _FAIL else 0)
