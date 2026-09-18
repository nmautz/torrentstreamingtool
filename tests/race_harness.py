"""Live integration harness for parallel download racing.

    python tests/race_harness.py --scenario all --password <admin password>

Unlike `test_relquality.py` / `test_race_rules.py` this one is NOT pure: it
drives a **running** StreamLink against a **running** qBittorrent over the
public HTTP API only. Nothing here reaches into `main.py`, so it exercises the
same surface the dashboard does.

What it is for: the parts of racing that cannot be unit-tested because they are
about real torrents and real cleanup - does a dead swarm actually get dropped in
120 s rather than 600, does the two-track actually swap, and above all **does
every scenario end with zero orphaned torrents**. That last assertion is the one
worth the most: almost every way this feature can go wrong ends with a torrent
running that nothing in the library points at.

Setup:

  1. Copy `tests/race_magnets.example.json` to `tests/race_magnets.json` and
     fill it in. The "healthy" slots want large, permanently well-seeded, legal
     torrents - Ubuntu / Debian ISO magnets are ideal. The "dead" slots want
     well-formed magnets with an info-hash nobody is seeding (invent one).
     The real file is gitignored; only the example is committed.
  2. Turn racing ON in Admin -> Race Download Sources (or pass --enable, which
     saves the settings this harness needs and restores them afterwards).
  3. Expect scenarios to take minutes: the grace period alone is 90 s.

Every scenario cleans up after itself (deletes the item it created, with files).
It will refuse to touch anything it did not create.
"""

import argparse
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
MAGNETS = os.path.join(HERE, "race_magnets.json")


# ── tiny HTTP helper (stdlib only, like the rest of this repo's tooling) ─────
class Client:
    def __init__(self, base, password=""):
        self.base = base.rstrip("/")
        self.token = ""
        if password:
            self.token = self.post("/api/admin/login",
                                   {"password": password})["token"]

    def _req(self, method, path, body=None, timeout=60):
        url = self.base + path
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", "Bearer " + self.token)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read().decode("utf-8", "replace")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            try:
                detail = json.loads(raw)
            except ValueError:
                detail = raw
            raise RuntimeError("%s %s -> %d %s" % (method, path, e.code, detail))

    def get(self, path, timeout=60):
        return self._req("GET", path, None, timeout)

    def post(self, path, body, timeout=300):
        return self._req("POST", path, body, timeout)

    def delete(self, path, timeout=120):
        return self._req("DELETE", path, None, timeout)


class RaceEvents(threading.Thread):
    """Collect `library_race` SSE events in the background."""

    daemon = True

    def __init__(self, base):
        super().__init__()
        self.base = base
        self.events = []
        self._stop = False

    def run(self):
        try:
            with urllib.request.urlopen(self.base + "/api/events", timeout=3600) as r:
                name = ""
                for raw in r:
                    if self._stop:
                        return
                    line = raw.decode("utf-8", "replace").strip()
                    if line.startswith("event:"):
                        name = line[6:].strip()
                    elif line.startswith("data:") and name == "library_race":
                        try:
                            self.events.append(json.loads(line[5:].strip()))
                        except ValueError:
                            pass
        except Exception:
            pass          # the harness must never die because SSE dropped

    def stop(self):
        self._stop = True

    def of(self, item_id, kind=None):
        return [e for e in self.events
                if e.get("item_id") == item_id and (kind is None or e.get("event") == kind)]


# ── assertions ───────────────────────────────────────────────────────────────
FAILS = []
PASSES = []


def check(name, cond, detail=""):
    (PASSES if cond else FAILS).append(name)
    print("   %s %s%s" % ("PASS" if cond else "FAIL", name,
                          ("  <- " + detail) if (detail and not cond) else ""))


def item(cl, item_id):
    for it in cl.get("/api/library").get("items", []):
        if it["id"] == item_id:
            return it
    return None


def wait_for(fn, timeout, label, poll=5):
    """Poll `fn` until it returns truthy. Returns the value, or None on timeout."""
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(poll)
    print("   (timed out after %ds waiting for %s)" % (timeout, label))
    return None


def start_race(cl, title, magnets, profile_id, runtime_min=0, episode_count=0):
    """Start an auto-picked download with a candidate shortlist."""
    cands = [{"magnet": m["magnet"], "title": m.get("title", "candidate"),
              "size": int(m.get("size") or 0), "seeders": int(m.get("seeders") or 0)}
             for m in magnets]
    body = {"magnet": cands[0]["magnet"], "title": title, "series": "",
            "season": 0, "episode": 0, "save_path": "", "profile_id": profile_id,
            "admin_only": False, "auto_picked": True, "candidates": cands}
    if runtime_min:
        body["runtime_min"] = runtime_min
    if episode_count:
        body["episode_count"] = episode_count
    return cl.post("/api/library/download", body)


def cleanup_item(cl, item_id):
    try:
        cl.delete("/api/library/%s?delete_file=true" % item_id)
    except Exception as exc:
        print("   (cleanup of %s failed: %s)" % (item_id, exc))


def assert_no_orphans(cl, label):
    """The single most valuable assertion in the file.

    Nearly every way racing can break - a challenger the delete path missed, a
    Stream-Now loser nothing tore down, a promotion that lost track of the
    torrent it replaced - ends the same way: a torrent running that no library
    item points at. Admin Cleanup is the authoritative view of exactly that.
    """
    inv = cl.get("/api/admin/cleanup?refresh=1", timeout=180)
    orphans = inv.get("orphan_torrents") or []
    check("%s: no orphaned torrents" % label, not orphans,
          "orphans=%s" % [o.get("name", o.get("hash")) for o in orphans])
    return inv


# ── scenarios ────────────────────────────────────────────────────────────────
def scenario_good3(cl, ev, mg, pid):
    """Three healthy candidates: two get culled, one finishes, nothing leaks."""
    r = start_race(cl, "harness good3", mg["healthy"][:3], pid)
    iid = r["item_id"]
    try:
        check("good3: server accepted the race", bool(r.get("race", {}).get("started")),
              str(r.get("race")))
        got = wait_for(lambda: (item(cl, iid) or {}).get("race"), 90, "race to appear")
        check("good3: item is racing", bool(got) and got.get("state") == "racing",
              str(got))
        if got:
            check("good3: more than one candidate live", len(got.get("entries") or []) > 1,
                  str(got.get("entries")))
        # Culling cannot start before the grace period; give it grace + margin.
        settled = wait_for(
            lambda: len(((item(cl, iid) or {}).get("race") or {}).get("entries") or []) <= 1,
            300, "the field to narrow")
        check("good3: narrowed to a single candidate", bool(settled))
        check("good3: culls were announced", bool(ev.of(iid, "culled")),
              "no library_race culled events")
    finally:
        cleanup_item(cl, iid)
        time.sleep(5)
        assert_no_orphans(cl, "good3")


def scenario_dead2good1(cl, ev, mg, pid):
    """Two dead swarms and one live one. The dead pair must go on the 120 s
    metadata timer, NOT the old 600 s stall timer."""
    cands = [mg["dead"][0], mg["dead"][1], mg["healthy"][0]]
    r = start_race(cl, "harness dead2good1", cands, pid)
    iid = r["item_id"]
    t0 = time.time()
    try:
        dropped = wait_for(
            lambda: len(ev.of(iid, "culled")) >= 2, 300, "both dead swarms to drop")
        elapsed = time.time() - t0
        check("dead2good1: both dead candidates dropped", bool(dropped))
        check("dead2good1: dropped well before the 600 s stall timer", elapsed < 420,
              "took %ds" % elapsed)
        reasons = [e.get("reason") for e in ev.of(iid, "culled")]
        check("dead2good1: dropped for no-metadata", "no-metadata" in reasons,
              str(reasons))
        it = item(cl, iid)
        check("dead2good1: the item is still downloading, not errored",
              bool(it) and it.get("status") == "downloading",
              (it or {}).get("status", "gone"))
    finally:
        cleanup_item(cl, iid)
        time.sleep(5)
        assert_no_orphans(cl, "dead2good1")


def scenario_alldead(cl, ev, mg, pid):
    """Everything dies. The race must hand back to the serial retry, which
    eventually errors the item with its own message - not hang forever."""
    r = start_race(cl, "harness alldead zzqq", mg["dead"][:3], pid)
    iid = r["item_id"]
    try:
        done = wait_for(
            lambda: (item(cl, iid) or {}).get("status") in ("error", "ready"),
            900, "the item to give up")
        it = item(cl, iid) or {}
        check("alldead: the item ends in error, not limbo", done and it.get("status") == "error",
              it.get("status", "gone"))
        check("alldead: the error explains itself", bool(it.get("error")),
              str(it.get("error")))
        check("alldead: exhaustion was announced", bool(ev.of(iid, "exhausted")))
    finally:
        cleanup_item(cl, iid)
        time.sleep(5)
        assert_no_orphans(cl, "alldead")


def scenario_streamrace(cl, ev, mg, pid):
    """/api/stream/race leaves exactly one torrent standing."""
    cands = [{"magnet": m["magnet"], "title": m.get("title", "c"),
              "size": int(m.get("size") or 0), "seeders": int(m.get("seeders") or 0)}
             for m in mg["healthy"][:3]]
    won = None
    try:
        won = cl.post("/api/stream/race",
                      {"candidates": cands, "profile_id": pid}, timeout=240)
        check("streamrace: a winner was returned", bool(won.get("hash")), str(won))
        check("streamrace: it raced more than one", int(won.get("raced") or 0) > 1,
              str(won.get("raced")))
        check("streamrace: it reports who it beat", isinstance(won.get("beaten"), list))
        check("streamrace: the winner has a file list", bool(won.get("files")))
    except RuntimeError as exc:
        check("streamrace: endpoint answered", False, str(exc))
    finally:
        # Stop clears prepare_hash + every remaining racer.
        try:
            cl.post("/api/stop", {})
        except Exception:
            pass
        time.sleep(8)
        assert_no_orphans(cl, "streamrace")


def scenario_deleteduring(cl, ev, mg, pid):
    """Delete the item mid-race. Every candidate must go with it."""
    r = start_race(cl, "harness deleteduring", mg["healthy"][:3], pid)
    iid = r["item_id"]
    wait_for(lambda: (item(cl, iid) or {}).get("race"), 90, "the race to start")
    time.sleep(45)
    cleanup_item(cl, iid)
    time.sleep(8)
    check("deleteduring: the item is gone", item(cl, iid) is None)
    assert_no_orphans(cl, "deleteduring")


SCENARIOS = {
    "good3": scenario_good3,
    "dead2good1": scenario_dead2good1,
    "alldead": scenario_alldead,
    "streamrace": scenario_streamrace,
    "deleteduring": scenario_deleteduring,
}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", default="http://127.0.0.1")
    ap.add_argument("--password", default=os.environ.get("STREAMLINK_ADMIN_PASSWORD", ""),
                    help="admin password (needed for the orphan assertions)")
    ap.add_argument("--profile", default="", help="profile id to download as")
    ap.add_argument("--scenario", default="all",
                    help="one of %s, or 'all'" % ", ".join(sorted(SCENARIOS)))
    ap.add_argument("--enable", action="store_true",
                    help="turn racing on for the run and restore the old settings after")
    args = ap.parse_args()

    if not os.path.exists(MAGNETS):
        sys.exit("Missing %s - copy race_magnets.example.json and fill it in." % MAGNETS)
    mg = json.load(open(MAGNETS, encoding="utf-8"))
    for key in ("healthy", "dead"):
        if len(mg.get(key) or []) < 3:
            sys.exit("race_magnets.json needs at least 3 entries under %r." % key)

    cl = Client(args.base, args.password)
    pid = args.profile
    if not pid:
        profiles = cl.get("/api/profiles")
        pid = (profiles[0] if isinstance(profiles, list) and profiles else {}).get("id", "")
    if not pid:
        sys.exit("No profile to download as - pass --profile.")

    restore = None
    if args.enable:
        if not args.password:
            sys.exit("--enable needs --password.")
        restore = cl.get("/api/admin/download-race")
        cl.post("/api/admin/download-race",
                {"enabled": True, "size": 3, "max_items": 2,
                 "quality_ceiling": 1080, "hq_upgrade": True})
        print("Racing enabled for this run (will restore %s afterwards).\n" % restore)

    cfg = cl.get("/api/admin/download-race") if args.password else {}
    if args.password and not cfg.get("enabled"):
        sys.exit("Racing is disabled - turn it on in Admin, or pass --enable.")

    ev = RaceEvents(args.base)
    ev.start()

    names = sorted(SCENARIOS) if args.scenario == "all" else [args.scenario]
    try:
        for n in names:
            if n not in SCENARIOS:
                sys.exit("Unknown scenario %r." % n)
            print("\n== %s ==" % n)
            try:
                SCENARIOS[n](cl, ev, mg, pid)
            except Exception as exc:
                FAILS.append(n + " (raised)")
                print("   FAIL %s raised %r" % (n, exc))
    finally:
        ev.stop()
        if restore is not None:
            try:
                cl.post("/api/admin/download-race", restore)
                print("\nRestored the previous racing settings.")
            except Exception as exc:
                print("\nCould not restore racing settings: %s" % exc)

    print("\n%d passed, %d failed" % (len(PASSES), len(FAILS)))
    if FAILS:
        for f in FAILS:
            print("  - " + f)
        sys.exit(1)


if __name__ == "__main__":
    main()
