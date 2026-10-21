"""Matching a playing file to the library touches no disk in the usual case, and
the playback paths keep their disk calls off the event loop (20.25.17), as does
every async caller of the episode-group homes (20.25.19).

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
           "_prep_chain_wants_sync", "_match_vlc_file_sync", "_vlc_file_uris_sync",
           "_safe_log_path", "_log_dir_entries_sync", "_log_files_sync"}
ON_LOOP = {"_resolve_bundle_dir", "_vlc_wait_until_ready", "_sync_state_from_vlc",
           "_play_prep_chain", "_vlc_relaunch_playlist", "_library_play_launch",
           # 20.25.18: the log listing held the loop 3.1 s, and the freeze watch
           # reads these every 30 min
           "admin_list_logs", "admin_download_logs_bundle",
           "admin_download_prior_logs_bundle", "admin_download_log"}

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

print("episode-group homes are read off the loop (20.25.19)")
# `_ep_group_homes` opens up to 25 TMDb cache files on a memo miss. Every async
# function that reaches it through the sync attribution code warms it first.
NEEDS_WARM = {"_ep_group_homes", "_ep_groups_pending", "_nudge_metadata_health",
              "_reattribute_item_files", "_resettle_files"}


def _direct_calls(fn):
    out = []

    def walk(n):
        for c in ast.iter_child_nodes(n):
            if isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue                  # a nested def may well run in a thread
            if isinstance(c, ast.Call):
                f = c.func
                out.append(f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", ""))
            walk(c)
    walk(fn)
    return out


callers = 0
for n in ast.walk(tree):
    if isinstance(n, ast.AsyncFunctionDef):
        calls = _direct_calls(n)
        if NEEDS_WARM & set(calls):
            callers += 1
            check(f"{n.name} warms the homes before {sorted(NEEDS_WARM & set(calls))[0]}",
                  "_warm_ep_homes" in calls)
check("the rule found its callers", callers >= 10)

import asyncio, time                      # noqa: E402
sys.path.insert(0, str(ROOT))
import epgroups                           # noqa: E402

READS = []


class FakeDisk:
    def get(self, key):
        READS.append(key)
        return None                       # nothing cached: homes None, memoised


hns = {"Optional": __import__("typing").Optional, "time": time, "asyncio": asyncio,
       "epgroups": epgroups, "_tmdb_disk": FakeDisk(), "_ep_homes_memo": {},
       "_ep_groups_tried": set(), "_EP_GROUPS_MAX": 24}
hpieces = []
for node in tree.body:
    if isinstance(node, ast.Assign) and any(
            getattr(t, "id", "") in ("_EP_HOMES_TTL", "_EP_HOMES_WARM") for t in node.targets):
        hpieces.append(ast.get_source_segment(src, node))
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in (
            "_ep_group_homes", "_ep_homes_fill_sync", "_warm_ep_homes"):
        hpieces.append(ast.get_source_segment(src, node))
exec("\n\n".join(hpieces), hns)

show = {"metadata": {"tmdb_kind": "tv", "tmdb_id": 1429, "all_seasons": []},
        "files": [{"season": 4, "episode": 0, "path": "x"}]}
movie = {"metadata": {"tmdb_kind": "movie", "tmdb_id": 5}, "files": [{"path": "y"}]}
check("a stuck (season, 0) file makes the show eligible", epgroups.eligible(show["files"]))
asyncio.run(hns["_warm_ep_homes"]([show, show, movie]))
warm_reads = len(READS)
check("the warm-up reads the show once, the film not at all", warm_reads == 1)
READS.clear()
hns["_ep_group_homes"](show["metadata"])
check("the sync read after it touches no disk", READS == [])
asyncio.run(hns["_warm_ep_homes"]([show]))
check("a fresh memo is not read again", READS == [])
sid = 1429
hns["_ep_homes_memo"][sid] = (time.monotonic() - hns["_EP_HOMES_WARM"] - 1, None)
asyncio.run(hns["_warm_ep_homes"]([show]))
check("a memo near its expiry is re-read by the warm-up", len(READS) == 1)

print()
if fails:
    print(f"{len(fails)} FAILED"); sys.exit(1)
print("all passed")
