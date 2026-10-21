"""Matching a playing file to the library touches no disk in the usual case, and
the playback paths keep their disk calls off the event loop (20.25.17).

    python tests/test_loop_paths.py      (or `make test`)

`Path.resolve()` opens the file on Windows. `_canonical_item_path` resolved EVERY
file of the item on the event loop every 2 s of VLC playback (Family Guy: 425
files), `_find_in_paths` did the same on each Next/Prev press because VLC's
`C:/...` never equals the library's `C:\\...` as a string, and a play queued its
whole tail through `vlc_file_uri` (a resolve each) on the loop.

main.py can't be imported without the whole dependency tree, so the functions
are lifted out of its source by name, with `ntpath` standing in for `os.path` so
the Windows comparison rules apply on any host.
"""
import ast, ntpath, sys, types
from pathlib import Path, PureWindowsPath

ROOT = Path(__file__).resolve().parent.parent
import _backend_src                       # noqa: E402
src = _backend_src.read()
tree = ast.parse(src)

WANT = {"_norm_path", "_canonical_item_path", "_find_in_paths", "_is_background_path",
        "_match_vlc_file_sync"}
# Never CALLED directly inside these async functions: each touches the disk.
SYNC_IO = {"exists", "stat", "is_file", "is_dir", "iterdir", "resolve", "vlc_file_uri",
           "_od_max_seg_on_disk", "_offline_cache_dir", "_bundle_dir_built_sync",
           "_prep_chain_wants_sync", "_match_vlc_file_sync", "_vlc_file_uris_sync"}
ON_LOOP = {"_resolve_bundle_dir", "_vlc_wait_until_ready", "_sync_state_from_vlc",
           "_play_prep_chain", "_vlc_relaunch_playlist", "_library_play_launch"}

RESOLVED = []


class FakePath(PureWindowsPath):
    """A Windows path whose resolve() is counted, and maps one 8.3 short name."""
    def resolve(self):
        RESOLVED.append(str(self))
        s = str(self).replace("FAMILY~1.MKV", "Family Guy - S01E01 - Death Has a Shadow.mkv")
        return PureWindowsPath(s)


fake_os = types.SimpleNamespace(path=ntpath)
ns = {"os": fake_os, "Path": FakePath, "Optional": __import__("typing").Optional,
      "state": types.SimpleNamespace(background_video_path="")}
pieces, found, bodies = [], set(), {}
for node in tree.body:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        if node.name in WANT:
            pieces.append(ast.get_source_segment(src, node)); found.add(node.name)
        if node.name in ON_LOOP:
            bodies[node.name] = node
missing = WANT - found
assert not missing, f"not found in main.py: {missing}"
assert set(bodies) == ON_LOOP, f"not found in main.py: {ON_LOOP - set(bodies)}"
exec("\n\n".join(pieces), ns)


def _resolved_key(p):
    try:
        return ns["_norm_path"](str(FakePath(p).resolve()))
    except Exception:
        return ""


ns["_resolved_key"] = _resolved_key

fails = []


def check(name, ok):
    print(("  ok    " if ok else "  FAIL  ") + name)
    if not ok:
        fails.append(name)


D = "C:\\Users\\Nathan\\Downloads\\StreamLink\\Family Guy (1999)\\Season 01\\"
files = [D + f"Family Guy - S01E{n:02d}.mkv" for n in range(2, 427)]
files.insert(0, D + "Family Guy - S01E01 - Death Has a Shadow.mkv")
item = {"files": [{"path": p} for p in files]}
vlc_form = files[300].replace("\\", "/")          # what uri_to_path gives on Windows

print("matching the playing file")
RESOLVED.clear()
check("VLC's forward-slash path finds its library entry",
      ns["_canonical_item_path"](vlc_form, item) == files[300])
check("without resolving anything", RESOLVED == [])
check("case does not matter on Windows",
      ns["_canonical_item_path"](files[5].upper(), item) == files[5] and RESOLVED == [])
RESOLVED.clear()
short = D + "FAMILY~1.MKV"
check("an 8.3 short name still matches, by resolving",
      ns["_canonical_item_path"](short, item) == files[0] and RESOLVED)
check("a file not in the item comes back as given",
      ns["_canonical_item_path"]("D:/other.mkv", item) == "D:/other.mkv")

print("Next / Prev")
RESOLVED.clear()
check("finds the position from VLC's path", ns["_find_in_paths"](vlc_form, files) == 300)
check("without resolving the playlist", RESOLVED == [])
check("exact match still first", ns["_find_in_paths"](files[7], files) == 7)
check("not found is -1", ns["_find_in_paths"]("D:/x.mkv", files[:3]) == -1)

print("the background video")
ns["state"].background_video_path = "C:\\bg\\loop.mp4"
RESOLVED.clear()
check("is recognised from VLC's path without resolving",
      ns["_is_background_path"]("C:/bg/loop.mp4") and RESOLVED == [])
check("a real episode is not it", not ns["_is_background_path"](vlc_form))

print("re-adopting what VLC plays after a restart")
lib = {"items": [{"id": "fg", "files": item["files"]}]}
RESOLVED.clear()
is_bg, it, p = ns["_match_vlc_file_sync"](lib, vlc_form, "C:\\bg\\loop.mp4")
check("finds the item and its stored path", not is_bg and it["id"] == "fg" and p == files[300])
check("by string, with nothing resolved", RESOLVED == [])
check("the bg video is reported as such",
      ns["_match_vlc_file_sync"](lib, "C:/bg/loop.mp4", "C:\\bg\\loop.mp4")[0] is True)

print("no disk I/O on the event loop")
for name, fn in sorted(bodies.items()):
    bad = []
    for n in ast.walk(fn):
        if isinstance(n, ast.Call):
            f = n.func
            called = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
            if called in SYNC_IO:
                bad.append(f"{called}() at line {n.lineno}")
    check(f"{name} calls none of them directly" + (f" ({', '.join(bad)})" if bad else ""),
          not bad)

print()
if fails:
    print(f"{len(fails)} FAILED"); sys.exit(1)
print("all passed")
