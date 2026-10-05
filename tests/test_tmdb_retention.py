"""Nothing from TMDb is kept past six months: the pass that enforces it.

    python tests/test_tmdb_retention.py      (or `make test`)

TMDb's API terms forbid caching anything from the API for longer than 6 months.
`tmdbcache.py` holds the rule (tested in tests/test_tmdbcache.py); this drives
the real `_tmdb_retention_pass` from main.py against a fake library and a fake
TMDb, and checks the two things that matter: old metadata does go, and a library
is never blanked by one bad pass that a retry would have refreshed.

main.py can't be imported without the whole dependency tree, so - like
tests/test_race_rescue.py - the functions under test are lifted out of its
source by name. The lift asserts every name was found, so a rename in main.py
fails this loudly.
"""
import ast, asyncio, copy, io, logging, sys, time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import tmdbcache      # noqa: E402

src = io.open(ROOT / "main.py", encoding="utf-8").read()
tree = ast.parse(src)

WANT = {"_tmdb_retention_due", "_tmdb_expire_items", "_tmdb_retention_pass"}
CONSTS = {"_TMDB_KEEP_GAP"}

LIB = {"items": []}
EVENTS, CALLS = [], []


class TMDb:
    """up: answers. down: fails every call. `gone`: ids it answers 'no such entry' for."""
    up, key, gone = True, "k", set()
T = TMDb()


class _Mutate:
    async def __aenter__(self):
        return LIB

    async def __aexit__(self, *a):
        return False


async def get_library():
    return copy.deepcopy(LIB)


async def _tmdb_effective_key():
    return T.key


def _iso(days_ago):
    return datetime.fromtimestamp(time.time() - days_ago * tmdbcache.DAY,
                                  timezone.utc).isoformat(timespec="seconds")


async def _fetch_item_metadata(item_id, refresh=False):
    """What the real one promises for `refresh=True`: a new copy of the same
    binding, or None with nothing written."""
    CALLS.append(item_id)
    it = next(i for i in LIB["items"] if i["id"] == item_id)
    if not T.up:
        NS["_tmdb_fail_seq"] += 1
        return None
    if item_id in T.gone:
        return None
    m = it["metadata"]
    it["metadata"] = {"source": m["source"], "tmdb_id": m["tmdb_id"],
                      "tmdb_kind": m["tmdb_kind"], "title": "Fresh", "fetched_at": _iso(0)}
    return it["metadata"]


async def broadcast(event, data):
    EVENTS.append((event, data["item_id"]))


class _Disk:
    def prune(self):
        return 0


async def _nosleep(_):
    return None


class _Asyncio:
    sleep = staticmethod(_nosleep)
    to_thread = staticmethod(asyncio.to_thread)


_LOG = logging.getLogger("tmdb_retention_test")
_LOG.addHandler(logging.NullHandler())
_LOG.propagate = False

NS = {
    "tmdbcache": tmdbcache, "asyncio": _Asyncio, "log": _LOG,
    "get_library": get_library, "mutate_library": _Mutate,
    "_tmdb_effective_key": _tmdb_effective_key,
    "_fetch_item_metadata": _fetch_item_metadata, "broadcast": broadcast,
    "_tmdb_bg": lambda coro: asyncio.get_event_loop().create_task(coro),
    "_tmdb_disk": _Disk(), "_tmdb_img_sweep": lambda: 0, "_tmdb_fail_seq": 0,
}
found = set()
for node in tree.body:
    name = getattr(node, "name", None)
    if isinstance(node, ast.Assign) and len(node.targets) == 1:
        name = getattr(node.targets[0], "id", None)
        if name not in CONSTS:
            continue
    elif name not in WANT:
        continue
    exec(compile(ast.Module([node], []), "main.py", "exec"), NS)
    found.add(name)
assert found == WANT | CONSTS, "main.py no longer has: %s" % sorted((WANT | CONSTS) - found)

_PASS, _FAIL = 0, []


def eq(name, got, want):
    global _PASS
    if got == want:
        _PASS += 1
    else:
        _FAIL.append("%s: got %r, want %r" % (name, got, want))


def item(iid, days, source="tmdb", **extra):
    return {"id": iid, "metadata": dict({"source": source, "tmdb_id": 100 + len(iid),
                                         "tmdb_kind": "tv", "title": "Old " + iid,
                                         "fetched_at": _iso(days)}, **extra)}


def setup(*items, up=True, key="k", gone=()):
    LIB["items"] = list(items)
    T.up, T.key, T.gone = up, key, set(gone)
    del EVENTS[:], CALLS[:]


def meta(iid):
    return next(i for i in LIB["items"] if i["id"] == iid)["metadata"]


async def run(expire):
    r = await NS["_tmdb_retention_pass"](expire)
    await asyncio.sleep(0)      # let the broadcasts land
    return r


async def main():
    # Normal running: everything is refreshed long before the limit.
    setup(item("young", 10), item("due", 160), item("over", 200, source="manual"))
    eq("nothing to retry", await run(False), False)
    eq("the young one is left alone", meta("young")["title"], "Old young")
    eq("the due one is refreshed", meta("due")["title"], "Fresh")
    eq("the old one is refreshed", meta("over")["title"], "Fresh")
    eq("its binding is still pinned", meta("over")["source"], "manual")
    eq("over the limit goes first", CALLS, ["over", "due"])
    eq("clients are told", sorted(EVENTS), [("metadata_update", "due"), ("metadata_update", "over")])

    # TMDb down. The first failed pass removes nothing and asks to be re-run.
    setup(item("due", 160), item("over", 200), up=False)
    eq("asks for a retry", await run(False), True)
    eq("nothing removed on one failure", meta("over")["title"], "Old over")
    eq("stops asking once TMDb fails", CALLS, ["over"])

    # Still down on the retry: now the limit is enforced.
    eq("no further retry", await run(True), False)
    eq("content is gone", "title" in meta("over"), False)
    eq("the binding stays", (meta("over")["tmdb_id"], meta("over")["expired"]), (104, True))
    eq("under the limit is kept", meta("due")["title"], "Old due")

    # TMDb back: the stub fills in from the same binding.
    T.up = True
    del CALLS[:]
    eq("pass is clean", await run(False), False)
    eq("stub refilled", meta("over")["title"], "Fresh")
    eq("same entry", meta("over")["tmdb_id"], 104)
    eq("no stub left", meta("over").get("expired"), None)

    # Down, but nothing is over the limit: no hurry, and nothing to remove.
    setup(item("due", 160), up=False)
    eq("no retry when nothing is over", await run(True), False)
    eq("kept", meta("due")["title"], "Old due")

    # No API key behaves like TMDb being down: one grace pass, then gone.
    setup(item("over", 200), key="")
    eq("no key: retry first", await run(False), True)
    eq("no key: nothing asked", CALLS, [])
    eq("no key: kept for now", meta("over")["title"], "Old over")
    eq("no key: then done", await run(True), False)
    eq("no key: removed", meta("over").get("expired"), True)

    # TMDb answers, and no longer has the entry: nothing to wait for.
    setup(item("over", 200), item("due", 160), gone={"over", "due"})
    eq("an answer is not a failure", await run(False), False)
    eq("the over one is emptied at once", meta("over").get("expired"), True)
    eq("the due one still has its 30 days", meta("due")["title"], "Old due")

    # A hand-entered item is the user's own and never touched...
    custom = {"id": "c", "metadata": {"source": "custom", "title": "Home video",
                                      "fetched_at": _iso(900)}}
    # ...except for sections TMDb resolved inside it.
    mixed = {"id": "m", "metadata": {"source": "custom", "title": "Mine", "fetched_at": _iso(900),
                                     "sections": {"spin": {"source": "tmdb", "title": "Spin"},
                                                  "x": {"source": "none", "title": "Extras"}}}}
    setup(custom, mixed, up=False)
    eq("custom: no retry", await run(False), False)
    eq("custom untouched", meta("c")["title"], "Home video")
    eq("custom title kept", meta("m")["title"], "Mine")
    eq("its TMDb section dropped", meta("m")["sections"], {"x": {"source": "none", "title": "Extras"}})
    eq("custom: TMDb never asked", CALLS, [])

    # No stamp at all cannot show it is under six months.
    nostamp = item("n", 0)
    del nostamp["metadata"]["fetched_at"]
    setup(nostamp)
    await run(False)
    eq("unstamped metadata is refreshed", meta("n")["title"], "Fresh")


asyncio.run(main())
print("tmdb_retention: %d passed, %d failed" % (_PASS, len(_FAIL)))
for f in _FAIL:
    print("  FAIL " + f)
sys.exit(1 if _FAIL else 0)
