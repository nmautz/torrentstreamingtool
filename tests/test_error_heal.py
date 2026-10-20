"""An item put in `error` by a qBittorrent error state comes back once qBit is
working on the torrent again (20.25.16).

    python tests/test_error_heal.py      (or `make test`)

`library_download_monitor` sets `status: error` with no message when qBit reports
`error` / `missingFiles`, and only ever looks at `downloading` items afterwards.
On 2026-10-20 a full disk did that to Family Guy; qBit finished and seeded it once
there was room, and the library still called it failed. The function under test is
lifted out of main.py by name, with a fake library, qBit and broadcast.
"""
import ast, asyncio, contextlib, copy, sys, types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
import _backend_src                       # noqa: E402
src = _backend_src.read()
tree = ast.parse(src)

WANT = {"_heal_qbit_error_items"}
CONSTS = {"_QBIT_ALIVE_STATES"}

LIB = {"items": []}
QB = {"tors": []}
SENT = []
LOGS = []


class LibraryUnchanged(Exception):
    pass


@contextlib.asynccontextmanager
async def mutate_library():
    work = copy.deepcopy(LIB)
    try:
        yield work
    except LibraryUnchanged:
        return
    LIB.clear(); LIB.update(work)


async def qbit_info_all():
    return QB["tors"]


async def broadcast(ev, data):
    SENT.append((ev, data))


ns = {
    "LibraryUnchanged": LibraryUnchanged, "mutate_library": mutate_library,
    "qbit_info_all": qbit_info_all, "broadcast": broadcast,
    "state": types.SimpleNamespace(downloading_count=0, downloading_count_visible=0),
    "log": types.SimpleNamespace(info=lambda *a: LOGS.append(a)),
}
pieces, found = [], set()
for node in tree.body:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in WANT:
        pieces.append(ast.get_source_segment(src, node)); found.add(node.name)
    elif isinstance(node, (ast.Assign, ast.AnnAssign)):
        for t in (node.targets if isinstance(node, ast.Assign) else [node.target]):
            if isinstance(t, ast.Name) and t.id in CONSTS:
                pieces.append(ast.get_source_segment(src, node)); found.add(t.id)
missing = (WANT | CONSTS) - found
assert not missing, f"not found in main.py: {missing}"
exec("\n\n".join(pieces), ns)
heal = ns["_heal_qbit_error_items"]

fails = []


def check(name, ok):
    print(("  ok    " if ok else "  FAIL  ") + name)
    if not ok:
        fails.append(name)


def item(iid, h, error="", status="error", admin_only=False):
    return {"id": iid, "title": iid, "status": status, "error": error,
            "torrent_hash": h, "admin_only": admin_only}


def run():
    return asyncio.run(heal(copy.deepcopy(LIB)))


def status(iid):
    return next(i for i in LIB["items"] if i["id"] == iid)["status"]


print("a qBit error that has cleared")
LIB["items"] = [item("fg", "AAA"), item("stopped", "BBB"), item("still", "CCC"),
                item("fake", "DDD", error="contains no playable video"),
                item("nohash", ""), item("gone", "EEE"), item("ok", "FFF", status="ready"),
                item("locked", "GGG", admin_only=True)]
QB["tors"] = [{"hash": "aaa", "state": "uploading"}, {"hash": "bbb", "state": "stoppedDL"},
              {"hash": "ccc", "state": "missingFiles"}, {"hash": "ddd", "state": "uploading"},
              {"hash": "fff", "state": "uploading"}, {"hash": "ggg", "state": "stalledDL"}]
n = run()
check("the seeding torrent's item goes back to downloading", status("fg") == "downloading")
check("so does one qBit is fetching again", status("locked") == "downloading")
check("two healed", n == 2)
check("a stopped torrent is left alone", status("stopped") == "error")
check("a torrent still in error is left alone", status("still") == "error")
check("an error with a message is a verdict, not a qBit state", status("fake") == "error")
check("an item with no torrent is left alone", status("nohash") == "error")
check("an item qBit no longer has is left alone", status("gone") == "error")
check("a ready item is untouched", status("ok") == "ready")
check("both counted as downloading", ns["state"].downloading_count == 2)
check("only the visible one in the visible count", ns["state"].downloading_count_visible == 1)
check("each is broadcast", sorted(d["item_id"] for _, d in SENT) == ["fg", "locked"])

print("nothing to heal")
before = copy.deepcopy(LIB)
QB["tors"] = [{"hash": "bbb", "state": "stoppedDL"}]
check("returns 0 and writes nothing", run() == 0 and LIB == before)
QB["tors"] = None
check("qBit unreachable: returns 0", run() == 0)

print()
if fails:
    print(f"{len(fails)} FAILED"); sys.exit(1)
print("all passed")
