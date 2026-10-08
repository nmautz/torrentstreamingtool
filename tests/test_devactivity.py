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
import sqlite3
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

# ── claimed profile / anonymous id ───────────────────────────────────────────
# The House account has no PIN, so no session: the query is the only thing that
# names it. Both strings below are real rows from the box.
eq("profile from query", da.claimed_profile("profile_id=b8220378-9476-4fc8-b05e-aa70410a741a"),
   "b8220378-9476-4fc8-b05e-aa70410a741a")
eq("profile among others", da.claimed_profile("profile_id=p1&device_id=dmulz3exczzfs1ftm"), "p1")
eq("no profile", da.claimed_profile("file_path=F%3A%5Cprofile_id"), "")
eq("empty profile", da.claimed_profile("profile_id="), "")
eq("malformed profile", da.claimed_profile("profile_id=a%20b%2F.."), "")
eq("no query", da.claimed_profile(""), "")
A = da.anon_id
eq("app builds are one client", A("10.0.0.2", "App/20.13.3 CFNetwork/3896.100.1.2.1 Darwin/27.0.0"),
   A("10.0.0.2", "App/20.8.13 CFNetwork/3896.100.1.2.1 Darwin/27.0.0"))
eq("curl versions are one client", A("10.0.0.2", "curl/8.7.1"), A("10.0.0.2", "curl/8.4.0"))
eq("another address is another client", A("10.0.0.2", "curl/8.7.1") == A("10.0.0.3", "curl/8.7.1"), False)
eq("another kind is another client",
   A("10.0.0.2", "curl/8.7.1") == A("10.0.0.2", "App/20.13.3 CFNetwork/3896 Darwin/27.0.0"), False)
eq("anon id shape", A("10.0.0.2", "curl/8.7.1").startswith("anon-") and len(A("", "")) == 17, True)

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

# ── devstore: the app_version column (19.7.0) ────────────────────────────────
tmp = tempfile.mkdtemp()
try:
    path = os.path.join(tmp, "old.sqlite3")
    # A store made before the column existed must gain it, keeping its rows.
    old = sqlite3.connect(path)
    old.executescript("""CREATE TABLE devices (id TEXT PRIMARY KEY, label TEXT NOT NULL DEFAULT '',
        name TEXT NOT NULL DEFAULT '', ua TEXT NOT NULL DEFAULT '', ua_summary TEXT NOT NULL DEFAULT '',
        ip TEXT NOT NULL DEFAULT '', via TEXT NOT NULL DEFAULT '', first_seen REAL NOT NULL,
        last_seen REAL NOT NULL, requests INTEGER NOT NULL DEFAULT 0,
        profile_id TEXT NOT NULL DEFAULT '', paired INTEGER NOT NULL DEFAULT 0);
        INSERT INTO devices (id, first_seen, last_seen) VALUES ('old', 1.0, 2.0);""")
    old.commit(); old.close()
    st = devstore.DeviceStore(path)
    eq("old store migrated", st.device("old")["app_version"], "")
    base = {"id": "a1", "first_seen": 1.0, "last_seen": 2.0, "requests": 1}
    st.write(devices=[{**base, "app_version": "19.6.0"}])
    eq("app version stored", st.device("a1")["app_version"], "19.6.0")
    st.write(devices=[{**base, "last_seen": 3.0}])
    eq("request without it keeps it", st.device("a1")["app_version"], "19.6.0")
    st.write(devices=[{**base, "app_version": "19.7.0"}])
    eq("newer replaces", st.device("a1")["app_version"], "19.7.0")
    st.close()

    # Anonymous rows keyed the old way (ip + raw UA) merge onto the new id, with
    # their history; a second run changes nothing; identified devices are left.
    st = devstore.DeviceStore(os.path.join(tmp, "rekey.sqlite3"))
    ua1, ua2 = "App/20.8.13 CFNetwork/1 Darwin/27", "App/20.13.3 CFNetwork/1 Darwin/27"
    mk = lambda i, ua, a, b, n, **kw: {"id": i, "ua": ua, "ua_summary": da.ua_summary(ua), "ip": "10.0.0.2",  # noqa: E731
                                       "via": "anonymous", "first_seen": a, "last_seen": b, "requests": n, **kw}
    rq = lambda i, ts, q="": {"device_id": i, "ts": ts, "method": "GET", "path": "/api/discovery",  # noqa: E731
                              "query": q, "status": 200, "kind": "other", "via": "anonymous"}
    st.write(devices=[mk("anon-old1", ua1, 10.0, 20.0, 2), mk("anon-old2", ua2, 30.0, 40.0, 3),
                      mk("phone", "Mozilla iPhone", 5.0, 50.0, 9, via="browser")],
             requests=[rq("anon-old1", 10.0), rq("anon-old1", 20.0), rq("anon-old2", 40.0),
                       rq("phone", 6.0, "profile_id=house"), rq("phone", 7.0, "profile_id=gone"),
                       rq("anon-old2", 39.0, "profile_id=nobody")],
             activities=[{"device_id": "anon-old1", "kind": "other", "start": 10.0, "end": 20.0}])
    st.set_label("anon-old1", "Probe")
    new = da.anon_id("10.0.0.2", ua1)
    eq("two rows moved", st.rekey_anonymous(da.anon_id), 2)
    eq("old rows gone", (st.device("anon-old1"), st.device("anon-old2")), (None, None))
    m = st.device(new)
    eq("merged counts and span", (m["requests"], m["first_seen"], m["last_seen"]), (5, 10.0, 40.0))
    eq("newest client string, label kept", (m["ua"], m["label"]), (ua2, "Probe"))
    eq("requests followed", len(st.requests(new)), 4)
    eq("activities followed", len(st.activities(new)), 1)
    eq("second run is a no-op", st.rekey_anonymous(da.anon_id), 0)
    eq("identified device untouched", st.device("phone")["requests"], 9)
    # The newest VALID claim names the device; an unknown id never does.
    eq("one device named", st.backfill_profiles(da.claimed_profile, {"house"}), 1)
    eq("profile filled in", st.device("phone")["profile_id"], "house")
    eq("unknown profile ignored", st.device(new)["profile_id"], "")
    eq("nothing left to name", st.backfill_profiles(da.claimed_profile, {"house"}), 0)
    st.close()
finally:
    shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    for f in _FAIL:
        print("FAIL", f)
    print("devactivity: %d passed, %d failed" % (_PASS, len(_FAIL)))
    sys.exit(1 if _FAIL else 0)
