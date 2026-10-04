"""A stalled part-download rescued by a race: what gets deleted, and when.

    python tests/test_race_rescue.py      (or `make test`)

`_rescue_stalled_download` adds replacement releases BESIDE a torrent that has
stopped part-way and lets the race engine decide. This drives the real
`_reconcile_item_race` from main.py, tick by tick, against a fake qBittorrent
and a fake clock, and checks the one thing that matters: the stuck torrent's
bytes are never thrown away on a guess.

main.py can't be imported without the whole dependency tree, so - like
tests/test_packslice.py - the functions under test are lifted out of its source
by name and given the handful of globals they touch. The lift asserts every name
was found, so a rename in main.py fails this loudly.
"""
import ast, asyncio, io, logging, os, sys, types
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import racerules, relquality, stallrule      # noqa: E402

src = io.open(ROOT / "main.py", encoding="utf-8").read()
tree = ast.parse(src)

WANT = {"_reconcile_item_race", "_race_promote", "_race_drop", "_race_settle",
        "_race_active", "_race_entry_done", "_race_bank_attempt", "_release_key"}
CONSTS = {"_RACE_TAG", "_RACE_CULL_RATIO", "_RACE_META_GRACE", "_RACE_HQ_DEAD_SECS",
          "_RACE_HQ_MAX_ETA", "_RACE_MISS_TICKS", "_RACE_PAUSED_STATES",
          "_RACE_CHALLENGER_IDLE", "_RACE_ARMING_SECS", "_race_entry_progress", "_race_leader",
          "_race_should_cull"}


class Clock:
    t = 1_800_000_000.0
CLOCK = Clock()


class FakeDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime.fromtimestamp(CLOCK.t, tz or timezone.utc)


def _now_iso():
    return datetime.fromtimestamp(CLOCK.t, timezone.utc).isoformat()


def _parse_iso_dt(s):
    try:
        return datetime.fromisoformat(s) if s else None
    except ValueError:
        return None


class Qbit:
    """Just enough qBittorrent: a dict of torrents, and a record of deletions."""
    def __init__(self):
        self.t, self.deleted = {}, []

    def by_hash(self):
        return {h: dict(v) for h, v in self.t.items()}


QB = Qbit()
LIB = {"items": []}
EVENTS = []


class _Mutate:
    async def __aenter__(self):
        return LIB

    async def __aexit__(self, *a):
        return False


async def _qbit_delete_reaped(h):
    QB.deleted.append(h)
    QB.t.pop(h, None)


async def _qbit_files(h):
    return [{"name": "ep.mkv", "size": QB.t[h]["size"]}] if h in QB.t else []


async def _noop(*a, **k):
    return None


async def _race_event(item_id, event, **kw):
    EVENTS.append((event, kw.get("reason", "")))


async def _broadcast(*a, **k):
    return None


ns = {
    "Optional": Optional, "datetime": FakeDatetime, "timezone": timezone,
    "time": types.SimpleNamespace(time=lambda: CLOCK.t),
    "re": __import__("re"),
    "racerules": racerules, "relquality": relquality, "stallrule": stallrule,
    "log": logging.getLogger("test"), "_now_iso": _now_iso, "_parse_iso_dt": _parse_iso_dt,
    "state": types.SimpleNamespace(vpn_secure=True),
    "settings": types.SimpleNamespace(qbit_download_path="/dl"),
    "_race_item_is_playing": lambda item: False,
    "_qbit_delete_reaped": _qbit_delete_reaped, "qbit_files": _qbit_files,
    "qbit_resume": _noop, "qbit_pause": _noop, "qbit_add_magnet": _noop,
    "build_file_list": lambda qfiles, sp: [{"name": q["name"], "size_bytes": q["size"]}
                                           for q in qfiles],
    "mutate_library": lambda: _Mutate(), "broadcast": _broadcast,
    "_race_event": _race_event, "_missing_torrent_ticks": {},
}
pieces, found = [], set()
for node in tree.body:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in WANT:
        pieces.append(ast.get_source_segment(src, node)); found.add(node.name)
    elif isinstance(node, ast.Assign):
        for t in node.targets:
            if isinstance(t, ast.Name) and t.id in CONSTS:
                pieces.append(ast.get_source_segment(src, node)); found.add(t.id)
missing = (WANT | CONSTS) - found
assert not missing, f"not found in main.py: {missing}"
exec("\n\n".join(pieces), ns)
reconcile = ns["_reconcile_item_race"]

MB = 1024 ** 2
fails = []


def check(label, cond):
    print(("  ok   " if cond else "  FAIL ") + label)
    if not cond:
        fails.append(label)


def entry(h, title, role="challenger"):
    return {"hash": h, "title": title, "magnet": "magnet:?xt=urn:btih:" + h,
            "save_path": "/dl", "role": role, "quality": {}, "label": "", "status": "live",
            "added_at": _now_iso(), "first_bytes_at": "", "completed": 0, "total": 0,
            "rate_ewma": 0.0, "samples": 0, "last_sample_at": 0.0, "under_ratio_ticks": 0,
            "miss_ticks": 0, "meta_ok": False, "file_count": 0, "drop_reason": "",
            "dropped_at": ""}


def setup(inc_done, inc_size, challengers):
    """A rescue race as `_race_start(rescue=True)` leaves it: the stuck incumbent
    plus `challengers` = [(hash, size)], everything freshly added."""
    QB.t.clear(); QB.deleted.clear(); EVENTS.clear()
    QB.t["old"] = {"hash": "old", "state": "stalledDL", "completed": inc_done,
                   "size": inc_size, "save_path": "/dl"}
    for h, size in challengers:
        QB.t[h] = {"hash": h, "state": "downloading", "completed": 0, "size": size,
                   "save_path": "/dl"}
    item = {"id": "it", "title": "Show S01E01 720p-OLD", "status": "downloading",
            "torrent_hash": "old", "files": [{"path": "/dl/old.mkv"}], "size_bytes": inc_size,
            "race": {"v": 1, "state": "racing", "rescue": True, "entries":
                     [entry("old", "Show S01E01 720p-OLD", "primary")]
                     + [entry(h, "Show S01E01 1080p-" + h.upper()) for h, _ in challengers]}}
    LIB["items"] = [dict(item)]
    return item


def run(item, secs, rates):
    """Tick every 5 s for `secs`; `rates` = {hash: bytes/sec} for torrents that move."""
    async def go():
        t_end = CLOCK.t + secs
        while CLOCK.t < t_end:
            CLOCK.t += 5
            for h, bps in rates.items():
                if h in QB.t:
                    q = QB.t[h]
                    q["completed"] = min(q["size"], q["completed"] + int(bps * 5))
            if (item.get("race") or {}).get("state") not in ("racing", "upgrading"):
                break
            await reconcile(item, QB.by_hash())
    asyncio.run(go())


# ── A. The box's own case: 83 % and stuck, past KEEP_PROGRESS so it can't be culled.
print("A. stuck at 83% - a slow replacement finishes first")
it = setup(337 * MB, 404 * MB, [("new", 640 * MB)])
run(it, 20 * 60, {"new": 0.25 * MB})                 # too slow to count as a 'leader'
check("the old torrent is untouched while the replacement is still coming",
      "old" not in QB.deleted and it["torrent_hash"] == "old")
check("...and the race is still on", it["race"]["state"] == "racing")
run(it, 40 * 60, {"new": 0.25 * MB})
check("the finished replacement became the item's torrent", it["torrent_hash"] == "new")
check("only THEN was the stuck one deleted", QB.deleted == ["old"])
check("the item took the replacement's title", it["title"].endswith("1080p-NEW"))
check("the race settled", it["race"]["state"] == "settled")
check("the abandoned release is banked so it is never picked again",
      any(a.get("title", "").endswith("720p-OLD") for a in it.get("download_attempts") or []))

# ── B. Under KEEP_PROGRESS, a plainly-moving replacement takes over without waiting.
print("B. stuck at 41% - a fast replacement takes over early")
it = setup(145 * MB, 355 * MB, [("new", 800 * MB)])
run(it, 80, {"new": 2 * MB})
check("not inside the grace period, however fast", it["torrent_hash"] == "old"
      and "old" not in QB.deleted)
run(it, 4 * 60, {"new": 2 * MB})
check("promoted once it has plainly won", it["torrent_hash"] == "new")
check("the stuck one was deleted exactly once", QB.deleted == ["old"])
check("...before the replacement had even finished",
      ("culled", "outpaced") in EVENTS)

# ── C. Every replacement is dead too. Nothing of the original may be lost.
print("C. the replacements never fetch a byte")
it = setup(337 * MB, 404 * MB, [("d1", 640 * MB), ("d2", 700 * MB)])
run(it, 5 * 60, {})
check("five idle minutes: still waiting on them", it["race"]["state"] == "racing")
run(it, 8 * 60, {})
check("both dead replacements were dropped", sorted(QB.deleted) == ["d1", "d2"])
check("the stuck original was NOT deleted", "old" in QB.t and it["torrent_hash"] == "old")
check("the race ended, freeing its slot and un-suppressing the stall clock",
      it["race"]["state"] in ("settled", "exhausted"))
check("the dead ones are banked, so the next rescue tries different releases",
      len([a for a in it.get("download_attempts") or [] if a.get("raced")]) == 2)

# ── D. The old seeder comes back and wins. The complete copy must be the one kept.
print("D. the original's seeder returns and it finishes first")
it = setup(337 * MB, 404 * MB, [("new", 640 * MB)])
run(it, 3 * 60, {"new": 0.1 * MB})
QB.t["old"]["state"] = "downloading"
run(it, 5 * 60, {"new": 0.1 * MB, "old": 1 * MB})
check("the original is still the item's torrent", it["torrent_hash"] == "old")
check("the replacement was dropped instead", QB.deleted == ["new"])
check("the race settled", it["race"]["state"] == "settled")

# ── E. A replacement that starts and then stalls itself is dropped, not kept for ever.
print("E. a replacement that stalls part-way is dropped too")
it = setup(337 * MB, 404 * MB, [("new", 640 * MB)])
run(it, 2 * 60, {"new": 0.5 * MB})
check("it was moving", QB.t["new"]["completed"] > 0)
QB.t["new"]["state"] = "stalledDL"
run(it, 15 * 60, {})
check("dropped after ten idle minutes", QB.deleted == ["new"])
check("the original survives", it["torrent_hash"] == "old" and "old" in QB.t)

# ── F. Paused / queued time is not held against a challenger.
print("F. a queued replacement is not judged for being queued")
it = setup(337 * MB, 404 * MB, [("new", 640 * MB)])
QB.t["new"]["state"] = "queuedDL"
run(it, 30 * 60, {})
check("still in the race after 30 queued minutes", "new" not in QB.deleted)

# ── G. A race that has been claimed but not populated yet must not be settled.
print("G. a race still being populated is left alone")
it = setup(337 * MB, 404 * MB, [])
it["race"]["arming"] = True
it["race"]["started_at"] = _now_iso()
run(it, 60, {})
check("a one-entry race mid-claim is NOT settled", it["race"]["state"] == "racing")
before = dict(it["race"]["entries"][0])
check("...and not mutated, so the monitor's merge-back can't clobber the append",
      it["race"]["entries"][0] == before and it["race"].get("arming") is True)
run(it, 120, {})
check("but a claim abandoned by a crash is cleaned up after the window",
      it["race"]["state"] == "settled")

print()
print("FAILED: " + "; ".join(fails) if fails else "all checks passed")
sys.exit(1 if fails else 0)
