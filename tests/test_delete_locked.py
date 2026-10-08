"""Deleting files qBittorrent still has open: the torrent is stopped, then started.

    python tests/test_delete_locked.py      (or `make test`)

On Windows an unlink fails (WinError 32) while another process holds the file,
and a seeding torrent holds every file it has served. Dropping the file to
priority 0 does not close it; stopping the torrent does. This drives the real
`_delete_files_now` and the source-eviction run (`_evict_paths`) from main.py
against a fake qBittorrent whose running torrents lock their files, the way
Windows does.

main.py can't be imported without the whole dependency tree, so - like
tests/test_race_rescue.py - the functions under test are lifted out of its
source by name. The lift asserts every name was found.
"""
import ast, asyncio, io, logging, os, stat, sys, tempfile, types
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
src = io.open(ROOT / "main.py", encoding="utf-8").read()
tree = ast.parse(src)

WANT = {"_delete_files_now", "_qbit_let_go", "_unlink_resilient", "_item_all_torrent_hashes",
        "_evict_paths", "_evict_one_source"}
CONSTS = {"_RACE_PAUSED_STATES", "_QBIT_IDLE_STATES"}


class Qbit:
    def __init__(self):
        self.state, self.calls, self.other = {}, [], set()

    def locks(self, path) -> bool:
        """Open in a running torrent, or in some other program entirely."""
        if str(path) in self.other:
            return True
        return any(st not in ("stoppedUP", "pausedUP", "stoppedDL", "pausedDL")
                   for st in self.state.values())


QB = Qbit()
LIB = {"items": []}


class LockedPath(type(Path())):
    def unlink(self, *a, **k):
        if QB.locks(self):
            raise PermissionError(32, "The process cannot access the file because "
                                      "it is being used by another process")
        return super().unlink(*a, **k)


class _Mutate:
    async def __aenter__(self):
        return LIB

    async def __aexit__(self, *a):
        return False


async def _qbit_info(h):
    return {"state": QB.state[h]} if h in QB.state else None


async def _qbit_pause(h):
    QB.calls.append(("stop", h))
    QB.state[h] = "stoppedUP"


async def _qbit_resume(h):
    QB.calls.append(("start", h))
    QB.state[h] = "uploading"


async def _apply_item_schedule(item, lib):
    return False


async def _no_sleep(_):
    return None


class HTTPException(Exception):
    pass


ns = {
    "Optional": Optional, "Path": LockedPath, "os": os, "stat": stat,
    "asyncio": asyncio, "log": logging.getLogger("test"),
    "HTTPException": HTTPException, "mutate_library": lambda: _Mutate(),
    "qbit_info": _qbit_info, "qbit_pause": _qbit_pause, "qbit_resume": _qbit_resume,
    "_apply_item_schedule": _apply_item_schedule,
    "_bundle_dir_for_file": lambda f: None, "_file_evicted": lambda f: False,
    "_delete_cache_artifacts": lambda *a: 0,
    "_file_holders": lambda paths: {p: ["vlc.exe"] for p in paths},
    "_invalidate_bundle_index": lambda: None,
    "_invalidate_offline_cache_inventory": lambda: None,
    # source eviction
    "OFFLINE_CACHE_DIRNAME": ".offline_cache", "hls_log": logging.getLogger("test"),
    "state": types.SimpleNamespace(library_current_file=None, source_eviction_stop=False),
    "_is_compressing": lambda p: False, "_offline_cache_path_active": lambda k: False,
    "_norm_path": lambda p: p, "_od_sessions": {}, "_bundle_playable_sync": lambda b: True,
    "_now_iso": lambda: "now", "_bundle_index_register": lambda k, b: None,
    "human_size": lambda n: f"{n} B", "_file_sig": lambda p: "sig",
    "_bundle_key_for_file": lambda f: "k-" + Path(f["path"]).stem,
}


async def _get_library():
    return LIB


async def _machine_in_use(_):
    return False


ns.update({"get_library": _get_library, "_machine_in_use": _machine_in_use})
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
ns["asyncio"] = type("A", (), {"to_thread": staticmethod(asyncio.to_thread),
                               "sleep": staticmethod(_no_sleep)})
logging.disable(logging.CRITICAL)

fails = []


def check(label, cond):
    print(("  ok   " if cond else "  FAIL ") + label)
    if not cond:
        fails.append(label)


def setup(tmp, n, state):
    paths = []
    for i in range(n):
        p = Path(tmp) / f"ep{i:02d}.mkv"
        p.write_bytes(b"x" * 100)
        paths.append(str(p))
    QB.state, QB.calls, QB.other = ({"aaa": state} if state else {}), [], set()
    LIB["items"] = [{"id": "it", "torrent_hash": "AAA", "status": "ready",
                     "files": [{"path": p} for p in paths],
                     "download": {"mode": "now", "files": {}}}]
    return paths


def run(paths):
    return asyncio.run(ns["_delete_files_now"]("it", paths))


with tempfile.TemporaryDirectory() as tmp:
    print("a seeding torrent holds every file")
    paths = setup(tmp, 5, "uploading")
    res = run(paths)
    check("all five deleted", res["deleted"] == 5 and res["freed_bytes"] == 500)
    check("nothing reported failed", res["failed"] == [])
    check("files are gone", not any(os.path.exists(p) for p in paths))
    check("stopped once, started once", QB.calls == [("stop", "aaa"), ("start", "aaa")])
    check("files stay marked skip",
          all(LIB["items"][0]["download"]["files"][p] == "skip" for p in paths))

    print("nothing is locked")
    paths = setup(tmp, 3, "stoppedUP")
    res = run(paths)
    check("all three deleted", res["deleted"] == 3)
    check("the torrent is not touched", QB.calls == [])

    print("a torrent that was already stopped is not started")
    paths = setup(tmp, 2, "stoppedUP")
    QB.other.add(paths[0])
    res = run(paths)
    check("the free file went", res["deleted"] == 1 and not os.path.exists(paths[1]))
    check("the held one is reported with its holder",
          len(res["failed"]) == 1 and "vlc.exe" in res["failed"][0]["reason"])
    check("no stop, no start", QB.calls == [])
    check("the held file's skip mark is rolled back",
          paths[0] not in LIB["items"][0]["download"]["files"])

    print("held by qBittorrent and by something else")
    paths = setup(tmp, 4, "uploading")
    QB.other.add(paths[2])
    res = run(paths)
    check("three deleted, one failed", res["deleted"] == 3 and len(res["failed"]) == 1)
    check("the torrent is started again even so", QB.calls[-1] == ("start", "aaa"))

    print("qBittorrent does not know the torrent")
    paths = setup(tmp, 2, None)
    res = run(paths)
    check("plain delete still works", res["deleted"] == 2 and QB.calls == [])

    print("source eviction of a seeding pack")
    paths = setup(tmp, 4, "uploading")
    for p in paths:
        b = Path(tmp) / ".offline_cache" / ("k-" + Path(p).stem)
        b.mkdir(parents=True, exist_ok=True)
        (b / "master.m3u8").write_text("#EXTM3U")
    se = {}
    asyncio.run(ns["_evict_paths"](paths, se, manual=True))
    files = LIB["items"][0]["files"]
    check("all four sources reclaimed", se.get("deleted") == 4 and se.get("bytes_freed") == 400)
    check("sources gone, bundles kept",
          not any(os.path.exists(p) for p in paths)
          and all((Path(tmp) / ".offline_cache" / ("k-" + Path(p).stem) / "master.m3u8").exists()
                  for p in paths))
    check("every file carries its eviction record",
          all((f.get("bundle") or {}).get("source_evicted") for f in files))
    check("stopped once for the whole run, started once",
          QB.calls == [("stop", "aaa"), ("start", "aaa")])

    print("eviction when something else holds a source")
    paths = setup(tmp, 2, "stoppedUP")
    QB.other.add(paths[1])
    se = {}
    asyncio.run(ns["_evict_paths"](paths, se, manual=True))
    files = LIB["items"][0]["files"]
    check("the free one is reclaimed", se.get("deleted") == 1)
    check("the held one keeps its source and has no record",
          os.path.exists(paths[1]) and "bundle" not in files[1])
    check("the torrent is not touched", QB.calls == [])

print()
if fails:
    print(f"{len(fails)} FAILED"); sys.exit(1)
print("all passed")
