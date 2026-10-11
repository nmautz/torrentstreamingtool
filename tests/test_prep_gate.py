"""Automatic prep leaves alone a file qBittorrent never fetched, and backs off
from a source ffprobe cannot open.

    python tests/test_prep_gate.py      (or `make test`)

A pack with one episode selected reads 100 % in qBittorrent, because its
progress counts only what is selected. A skipped episode can still be on disk,
full-length and all zeros (South Park S05E03, S06E04). The completeness check
used to stop at that 100 % and auto-prep then tried the empty file every five
minutes. This drives the real `_incomplete_download_paths` and the unreadable
backoff from main.py against a fake qBittorrent.

main.py can't be imported without the whole dependency tree, so - like
tests/test_delete_locked.py - the functions under test are lifted out of its
source by name. The lift asserts every name was found.
"""
import ast, asyncio, io, os, sys, tempfile, types
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
src = io.open(ROOT / "main.py", encoding="utf-8").read()
tree = ast.parse(src)

WANT = {"_incomplete_download_paths", "_incomplete_paths_sync", "_torrent_fully_selected",
        "_prep_unreadable_note", "_prep_unreadable_wait", "_file_sig"}
CONSTS = {"_PREP_UNREADABLE", "PREP_UNREADABLE_FIRST_WAIT", "PREP_UNREADABLE_MAX_WAIT"}

QB = {"info": {}, "files": {}, "file_calls": 0}
CLOCK = [1000.0]


async def _qbit_info(h):
    return QB["info"].get(h)


async def _qbit_files(h):
    QB["file_calls"] += 1
    return QB["files"].get(h, [])


ns = {
    "Optional": Optional, "Path": Path, "os": os, "asyncio": asyncio,
    "time": types.SimpleNamespace(monotonic=lambda: CLOCK[0]),
    "qbit_info": _qbit_info, "qbit_files": _qbit_files,
    "_norm_path": lambda p: str(p),
    "settings": types.SimpleNamespace(qbit_download_path=""),
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

fails = []


def check(name, ok):
    print(("  ok    " if ok else "  FAIL  ") + name)
    if not ok:
        fails.append(name)


def pack(tmp, n, size=100):
    """`n` full-length files on disk, the way qBit leaves a pack."""
    paths = []
    for i in range(1, n + 1):
        p = Path(tmp) / f"E{i:02d}.mkv"
        p.write_bytes(b"\0" * size)
        paths.append(str(p))
    return paths


def incomplete(item):
    return asyncio.run(ns["_incomplete_download_paths"](item))


with tempfile.TemporaryDirectory() as tmp:
    paths = pack(tmp, 3)
    item = {"status": "ready", "torrent_hash": "AAA",
            "files": [{"path": p} for p in paths]}

    print("one episode of a pack fetched, the others skipped but on disk")
    QB["info"]["aaa"] = {"state": "uploading", "progress": 1.0, "save_path": tmp,
                         "size": 100, "total_size": 300}
    QB["files"]["aaa"] = [
        {"name": "E01.mkv", "size": 100, "progress": 0.0, "priority": 0},
        {"name": "E02.mkv", "size": 100, "progress": 1.0, "priority": 1},
        {"name": "E03.mkv", "size": 100, "progress": 0.02, "priority": 0},
    ]
    got = incomplete(item)
    check("the skipped files are incomplete", got == {paths[0], paths[2]})
    check("the fetched file is not", paths[1] not in got)

    print("a skipped file that had finished before it was skipped")
    QB["files"]["aaa"][0]["progress"] = 1.0
    check("it is complete, and may be prepped", paths[0] not in incomplete(item))

    print("the whole pack selected and finished")
    QB["info"]["aaa"].update(size=300, total_size=300)
    QB["file_calls"] = 0
    check("nothing incomplete", incomplete(item) == set())
    check("and qBittorrent is not asked for the file list", QB["file_calls"] == 0)

    print("qBittorrent does not say how much is selected")
    QB["info"]["aaa"] = {"state": "uploading", "progress": 1.0, "save_path": tmp}
    QB["files"]["aaa"][0]["progress"] = 0.0
    check("the file list is asked, never assumed", paths[0] in incomplete(item))

    print("a source ffprobe cannot open")
    note, wait = ns["_prep_unreadable_note"], ns["_prep_unreadable_wait"]
    f = paths[0]
    check("not held back before any failure", wait(f) is False)
    check("first failure waits 15 minutes", note(f) == 900 and wait(f) is True)
    CLOCK[0] += 901
    check("then it may be tried again", wait(f) is False)
    check("second failure waits 30 minutes", note(f) == 1800)
    for _ in range(12):
        w = note(f)
    check("the wait stops growing at six hours", w == 6 * 3600)
    check("another file is not held back", wait(paths[1]) is False)

    print("the file changes while it is being held back")
    Path(f).write_bytes(b"\x1a\x45\xdf\xa3" * 50)
    check("it is tried at once", wait(f) is False)
    check("and its count starts over", note(f) == 900)

print()
if fails:
    print(f"{len(fails)} FAILED"); sys.exit(1)
print("all passed")
