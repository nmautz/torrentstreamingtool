"""Matching a playing file to the library touches no disk in the usual case, and
the playback paths keep their disk calls off the event loop (20.25.17), as does
every async caller of the episode-group homes (20.25.19), the bundle manifest
and the library's own load and save (20.26.1).

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
           "_safe_log_path", "_log_dir_entries_sync", "_log_files_sync",
           "rmtree", "read_text", "_dir_size_bytes"}
ON_LOOP = {"_resolve_bundle_dir", "_vlc_wait_until_ready", "_sync_state_from_vlc",
           "_play_prep_chain", "_vlc_relaunch_playlist", "_library_play_launch",
           # 20.25.18: the log listing held the loop 3.1 s, and the freeze watch
           # reads these every 30 min
           "admin_list_logs", "admin_download_logs_bundle",
           "admin_download_prior_logs_bundle", "admin_download_log",
           # 20.30.13: a stray-folder delete held the loop 0.74 s (rmtree); the
           # sidecar-sub read and the resync listing are the same disk
           "admin_cleanup_delete_stray", "_sub_to_vtt", "_hls_resync_bundles"}

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

print("the ffmpeg capability probes start no process on the loop (20.25.20)")
# CreateProcess runs on the loop thread under asyncio on Windows; the first
# ffmpeg start after a boot held it 0.6 s in _has_nvenc.
PROBES = {"_probe_nvenc", "_has_cuda_scale", "_has_tonemap", "_gpu_can_hwdecode",
          "_sample_gpu",               # 20.25.23: every 5 s, 0.14 s on the loop
          "vpn_guard",                 # 20.25.24: `mullvad status` every 3 s, 0.16 s
          "_ffmpeg_version"}           # 20.26.2: 0.67 s at start-up
seen = set()
for n in tree.body:
    if isinstance(n, ast.AsyncFunctionDef) and n.name in PROBES:
        seen.add(n.name)
        calls = _direct_calls(n)
        check(f"{n.name} runs ffmpeg through _probe_run",
              "create_subprocess_exec" not in calls and "_probe_run" in calls)
check("every probe was found", seen == PROBES)

print("start-up gives the loop its turns (20.25.22)")
life = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "lifespan")
life_src = ast.get_source_segment(src, life)
check("lifespan shortens the GIL switch interval",
      "sys.setswitchinterval(GIL_SWITCH_INTERVAL_S)" in life_src)
check("lifespan reads the endpoints' source on a worker thread, before serving",
      "await asyncio.to_thread(_warm_endpoint_sources_sync, app)" in life_src
      and life_src.index("_warm_endpoint_sources_sync") < life_src.index("yield"))

check("a missing sniffio is stubbed, so httpcore's per-request import never searches the disk",
      'sys.modules.setdefault("sniffio", None)' in src
      and src.index('sys.modules.setdefault("sniffio", None)') < src.index("\napp = FastAPI("))

print("a phone downloading ten episodes doesn't stall the box (20.26.1)")
# meta.json and the client-log pending file were read on the loop: 4-7 s each
# with qBit writing to the same disk on 2026-10-22.
NEVER_ON_LOOP = {"_read_meta", "_client_pending_read", "_client_pending_write",
                 "_client_devices", "_enumerate_bundle_files", "_dir_size_bytes",
                 "_bundle_manifest_open_sync",
                 # 20.26.2: 0.25-0.5 s each under load
                 "_needs_bundle_check", "_any_bundle_check_needed", "_file_sig",
                 "_prep_status_rows_sync", "_bundle_audit_locate_sync",
                 # 20.26.3: numpy cross-correlation, 0.67 s
                 "align", "parse_cues",
                 # 20.30.2: a stat / mkdir / failed import with F: busy, 0.4-0.6 s
                 "_prep_out_dir_sync", "_tz_lookup_sync"}
for n in ast.walk(tree):
    if isinstance(n, ast.AsyncFunctionDef):
        bad = sorted(NEVER_ON_LOOP & set(_direct_calls(n)))
        if bad:
            check(f"{n.name} calls {bad[0]} on the loop", False)
check("no async function reads a bundle's meta.json or the client-log state on the loop",
      True)
oj = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "_run_offline_job")
check("a finished prep is moved into place on a thread",
      "rename" not in _direct_calls(oj) and "_swap_into_place" in ast.get_source_segment(src, oj))
# The library's load and save run while `_lib_lock` is held. On the shared pool a
# lock holder queued behind four bundle listings for 118 s with 20 waiting.
check("library loads and saves never go through the shared pool",
      "to_thread(_load_lib_raw" not in src and "to_thread(_save_lib_raw" not in src)
check("they run on the library's own thread",
      src.count("lib_io(_load_with_edits, ") >= 3 and src.count("lib_io(_save_lib_raw, ") >= 3)
check("lifespan widens the shared pool before anything uses it",
      "set_default_executor(" in life_src
      and life_src.index("set_default_executor(") < life_src.index("to_thread("))
check("lifespan moves log writes to their thread before anything else logs much",
      "diag.defer_log_writes()" in life_src
      and life_src.index("diag.defer_log_writes()") < life_src.index("_warm_endpoint_sources_sync"))
check("a phone that drops a log upload is not a 500 (its traceback was 0.31 s)",
      "except ClientDisconnect:" in ast.get_source_segment(src, next(
          n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "diag_client_log")))
check("lifespan freezes the start-up heap just before serving",
      "diag.freeze_startup_heap()" in life_src
      and life_src.index("diag.freeze_startup_heap()") < life_src.index("yield")
      and life_src.index("diag.freeze_startup_heap()") > life_src.index("start_power_listener"))

import os, re, tempfile                   # noqa: E402
bns = {"os": os, "re": re, "Path": Path}
bpieces = []
for node in tree.body:
    if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "_BUNDLE_FILE_RE"
                                            for t in node.targets):
        bpieces.append(ast.get_source_segment(src, node))
    if isinstance(node, ast.FunctionDef) and node.name in ("_enumerate_bundle_files",
                                                           "_dir_size_bytes"):
        bpieces.append(ast.get_source_segment(src, node))
exec("\n\n".join(bpieces), bns)
with tempfile.TemporaryDirectory() as td:
    b = Path(td) / "bundle"
    b.mkdir()
    for name, size in (("seg_video_00002.m4s", 30), ("master.m3u8", 5),
                       ("seg_video_00001.m4s", 20), ("meta.json", 7), ("bad name!.txt", 3)):
        (b / name).write_bytes(b"x" * size)
    (b / "sub").mkdir()
    (b / "sub" / "deep.vtt").write_bytes(b"y" * 11)
    files_, total_ = bns["_enumerate_bundle_files"](b)
    check("the bundle listing has every servable file, sorted, with sizes",
          files_ == [{"name": "master.m3u8", "size": 5}, {"name": "meta.json", "size": 7},
                     {"name": "seg_video_00001.m4s", "size": 20},
                     {"name": "seg_video_00002.m4s", "size": 30}] and total_ == 62)
    check("the cache size counts every file, subfolders included",
          bns["_dir_size_bytes"](b) == 5 + 7 + 20 + 30 + 3 + 11)
    check("a missing folder is 0, not an error", bns["_dir_size_bytes"](b / "nope") == 0)
print("the storage breakdown lists each folder once (20.26.6)")
sns = {"os": os, "Path": Path, "VIDEO_EXTS": {".mkv", ".mp4"},
       "_file_evicted": lambda f: bool(f.get("evicted")),
       "_item_display_names": lambda it: [it.get("title", "")]}
seg = next(ast.get_source_segment(src, n) for n in tree.body
           if isinstance(n, ast.FunctionDef) and n.name == "_storage_breakdown_sync")
exec(seg, sns)
with tempfile.TemporaryDirectory() as td:
    d = Path(td) / "Show" / "Season 01"
    d.mkdir(parents=True)
    for name, size in (("Show.S01E01.mkv", 100), ("Show.S01E01.en.srt", 7),
                       ("Show.S01E01-poster.jpg", 5), ("Show.S01E02.mkv", 50),
                       ("Show.S01E10.mkv", 9), ("other.txt", 3)):
        (d / name).write_bytes(b"x" * size)
    lib = {"items": [{"id": "a", "title": "Show", "files": [
        {"path": str(d / "Show.S01E01.mkv")}, {"path": str(d / "Show.S01E02.mkv")},
        {"path": str(d / "Show.S01E03.mkv"), "size_bytes": 999},     # not on disk
        {"path": str(d / "Show.S01E10.mkv"), "evicted": True}]}]}
    real_stat = os.stat
    stats = []
    os.stat = lambda *a, **k: (stats.append(a[0]), real_stat(*a, **k))[1]
    try:
        out = sns["_storage_breakdown_sync"](lib, {str(d / "Show.S01E02.mkv"): 40})
    finally:
        os.stat = real_stat
    rows = {Path(f["path"]).name: f for f in out["items"][0]["files"]}
    check("sizes, sidecars and bundle bytes are counted as before",
          rows["Show.S01E01.mkv"]["source_bytes"] == 100
          and rows["Show.S01E01.mkv"]["extra_bytes"] == 12
          and rows["Show.S01E02.mkv"]["total_bytes"] == 90
          and "Show.S01E03.mkv" not in rows and "Show.S01E10.mkv" not in rows
          and out["total_bytes"] == 202)
    check("a file in the listing is not stat-ed again",
          not any("S01E01" in str(x) or "S01E02" in str(x) for x in stats))

for fn in ("_enumerate_bundle_files", "_dir_size_bytes", "_storage_breakdown_sync"):
    body = next(ast.get_source_segment(src, n) for n in tree.body
                if isinstance(n, ast.FunctionDef) and n.name == fn)
    check(f"{fn} lists with os.scandir (size from the directory read)",
          "os.scandir(" in body and ".iterdir()" not in body and ".rglob(" not in body)

print("where ffmpeg is, is remembered (20.26.8)")
sys.path.insert(0, str(ROOT))
import mediabin                           # noqa: E402
_looks = []
_clock = [1000.0]
_real = (mediabin._env_bin, mediabin.shutil.which, mediabin.time.monotonic)
mediabin._env_bin = lambda k: (_looks.append(k), "C:/tools/ffmpeg.exe" if "FFMPEG" in k else None)[1]
mediabin.shutil.which = lambda name: (_looks.append(name), None)[1]
mediabin.time.monotonic = lambda: _clock[0]
mediabin._bin_memo.clear()
try:
    a = mediabin.ffmpeg_bin(); b = mediabin.ffmpeg_bin()
    check("a found binary is looked up once", a == b == "C:/tools/ffmpeg.exe" and _looks == ["_FFMPEG_BIN"])
    _clock[0] += mediabin._BIN_TTL_FOUND_S + 1
    _gate = __import__("threading").Event()
    _env = mediabin._env_bin
    mediabin._env_bin = lambda k: (_gate.wait(5), _env(k))[1]
    c = mediabin.ffmpeg_bin()
    d = mediabin.ffmpeg_bin()                       # while the lookup is still running
    check("after its TTL the old answer comes back at once (20.30.1)",
          c == d == "C:/tools/ffmpeg.exe" and _looks.count("_FFMPEG_BIN") == 1)
    _gate.set(); mediabin._bin_last_refresh.join(5)
    mediabin._env_bin = _env
    check("and it is looked up again on a thread, once",
          _looks.count("_FFMPEG_BIN") == 2 and not mediabin._bin_refreshing)
    _looks.clear()
    mediabin.fpcalc_bin(); mediabin.fpcalc_bin()
    check("a missing one is remembered too", len(_looks) == 2)      # env + PATH, once
    _clock[0] += mediabin._BIN_TTL_MISSING_S + 1
    mediabin.fpcalc_bin(); mediabin._bin_last_refresh.join(5)
    check("but re-checked sooner, so an install is noticed",
          len(_looks) == 4 and mediabin._BIN_TTL_MISSING_S < mediabin._BIN_TTL_FOUND_S)
    mediabin._bin_memo.clear(); _looks.clear()
    got = mediabin.warm_bins()
    check("warm_bins looks every binary up", set(got) == {"ffmpeg", "ffprobe", "fpcalc"}
          and "_FFMPEG_BIN" in _looks and "_FPCALC_BIN" in _looks)
finally:
    mediabin._env_bin, mediabin.shutil.which, mediabin.time.monotonic = _real
    mediabin._bin_memo.clear()
fb = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_ffprobe_bin")
check("main's ffprobe lookup goes through the memo",
      "mediabin._memo_bin(" in ast.get_source_segment(src, fb))
_life = ast.get_source_segment(src, next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef)
                                         and n.name == "lifespan"))
check("the lifespan finds every binary on a thread, before serving",
      "asyncio.to_thread(_warm_media_bins_sync)" in _life
      and _life.index("_warm_media_bins_sync") < _life.index("yield"))
check("and starts the NVENC check", "_start_nvenc_probe()" in _life
      and _life.index("_start_nvenc_probe()") < _life.index("yield"))

print("one NVENC check, shared (20.30.1)")
import asyncio as _aio                    # noqa: E402
nv = {"asyncio": _aio, "Optional": __import__("typing").Optional, "_nvenc_probe": {},
      "_nvenc_task": None}
nv_runs = []


async def _fake_probe():
    nv_runs.append(1)
    await _aio.sleep(0.1)
    nv["_nvenc_probe"]["result"] = True
    return True

exec("\n\n".join(ast.get_source_segment(src, n) for n in tree.body
                  if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                  and n.name in ("_has_nvenc", "_start_nvenc_probe")), nv)
nv["_probe_nvenc"] = _fake_probe


async def _nv_case():
    nv["_start_nvenc_probe"]()                    # the lifespan
    gone = _aio.ensure_future(nv["_has_nvenc"]())
    await _aio.sleep(0.01)
    gone.cancel()                                 # a caller that gives up
    res = await _aio.gather(*(nv["_has_nvenc"]() for _ in range(3)))
    after = (await nv["_has_nvenc"](), len(nv_runs))
    nv["_nvenc_probe"].clear()                    # the ffmpeg-changed reset
    again = await nv["_has_nvenc"]()
    return res, after, again

res, after, again = _aio.run(_nv_case())
check("callers while it runs share the one check, and one leaving doesn't stop it",
      res == [True] * 3 and after == (True, 1))
check("a reset starts a new one", again is True and len(nv_runs) == 2)

print("profile sessions are written off the loop (20.30.1)")
import threading as _th, time, types as _ty    # noqa: E402
_writes = []
_wgate = _th.Event()


class _SlowFile:
    def write_text(self, blob, encoding=None):
        _wgate.wait(5)
        _writes.append(json.loads(blob))

import json                               # noqa: E402
sns = {"json": json, "threading": _th, "Optional": __import__("typing").Optional,
       "atexit": _ty.SimpleNamespace(register=lambda f: f),
       "log": _ty.SimpleNamespace(warning=lambda *a: None),
       "PROFILE_SESSIONS_FILE": _SlowFile(), "_profile_sessions": {}}
ssrc = _backend_src.read()
stree = ast.parse(ssrc)
spieces = []
for n in stree.body:
    if isinstance(n, ast.FunctionDef) and n.name in (
            "_write_profile_sessions", "_take_pending_sessions", "_sessions_writer_main",
            "_flush_profile_sessions", "_save_profile_sessions"):
        decos = "".join("@" + ast.get_source_segment(ssrc, d) + "\n" for d in n.decorator_list)
        spieces.append(decos + ast.get_source_segment(ssrc, n))
    elif isinstance(n, (ast.Assign, ast.AnnAssign)) and any(
            getattr(t, "id", "").startswith("_sessions_")
            for t in (n.targets if isinstance(n, ast.Assign) else [n.target])):
        spieces.append(ast.get_source_segment(ssrc, n))
exec("\n\n".join(spieces), sns)
t0 = time.monotonic()
for i in range(5):
    sns["_profile_sessions"][f"t{i}"] = {"profile_id": "p", "expires": 1}
    sns["_save_profile_sessions"]()
check("saving returns at once while the disk is slow", time.monotonic() - t0 < 0.1)
_wgate.set()
deadline = time.monotonic() + 5
while (sns["_sessions_pending"] is not None or not _writes or len(_writes[-1]) < 5) \
        and time.monotonic() < deadline:
    time.sleep(0.01)
check("the newest copy is what lands, and a burst is not five writes",
      bool(_writes) and len(_writes[-1]) == 5 and len(_writes) <= 2)
_wgate.clear()
sns["_profile_sessions"]["late"] = {"profile_id": "p", "expires": 1}
with sns["_sessions_cv"]:
    sns["_sessions_pending"] = json.dumps(sns["_profile_sessions"])   # not yet taken
_wgate.set()
sns["_flush_profile_sessions"]()
check("a copy still waiting at exit is written", any("late" in w for w in _writes))
print("the on-demand and prep paths touch no disk on the loop (20.30.2)")


def _afn(name):
    return next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == name)


asp = _afn("_assert_source_present")
check("_assert_source_present checks the file on a thread",
      "exists" not in _direct_calls(asp) and "asyncio.to_thread(" in ast.get_source_segment(src, asp))
for fn in ("stream_ondemand", "make_clip", "generate_subtitles", "library_subtitle_fetch"):
    body = ast.get_source_segment(src, _afn(fn))
    check(f"{fn} awaits it", "await _assert_source_present(" in body)
ons = _afn("_od_new_session")
check("_od_new_session makes its folder on a thread", "mkdir" not in _direct_calls(ons))
op = _afn("offline_prepare")
check("offline_prepare stats and makes folders on threads",
      not ({"exists", "mkdir", "_offline_cache_dir"} & set(_direct_calls(op))))

print("a time zone is looked up once (20.30.2)")
tz_imports = []


class _NoTz(Exception):
    pass


def _fake_zoneinfo(name):
    tz_imports.append(name)
    raise _NoTz(name)

tns = {"asyncio": _aio, "datetime": __import__("datetime").datetime,
       "log": _ty.SimpleNamespace(warning=lambda *a: None)}
exec("\n\n".join(ast.get_source_segment(src, n) for n in tree.body
                  if (isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                      and n.name in ("_tz_lookup_sync", "_now_in_tz", "_now_in_tz_async"))
                  or (isinstance(n, ast.AnnAssign) and getattr(n.target, "id", "") == "_TZ_MEMO")),
     tns)
_zi = _ty.ModuleType("zoneinfo"); _zi.ZoneInfo = _fake_zoneinfo
_real_zi = sys.modules.get("zoneinfo"); sys.modules["zoneinfo"] = _zi
try:
    for _ in range(3):
        _aio.run(tns["_now_in_tz_async"]("America/Los_Angeles"))
    tns["_now_in_tz"]("America/Los_Angeles")
finally:
    if _real_zi is not None:
        sys.modules["zoneinfo"] = _real_zi
check("a name that does not resolve is tried once, then local time",
      tz_imports == ["America/Los_Angeles"])
rb = ast.get_source_segment(src, _afn("scheduled_reboot_loop"))
check("the reboot loop asks through the thread-safe path",
      "_now_in_tz_async(" in rb and "_now_in_tz(" not in rb.replace("_now_in_tz_async(", ""))
req = (ROOT / "requirements.txt").read_text()
check("tzdata is installed on Windows", 'tzdata>=' in req and 'sys_platform == "win32"' in
      next(l for l in req.splitlines() if l.startswith("tzdata")))

save_src = next(ast.get_source_segment(ssrc, n) for n in stree.body
                if isinstance(n, ast.FunctionDef) and n.name == "_save_profile_sessions")
check("_save_profile_sessions itself touches no file", "write_text" not in save_src)

import asyncio, time                      # noqa: E402
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

# 20.30.2 slid `_prep_out_dir_sync` in between `@app.post(".../offline-prepare")`
# and its handler, so the route served the helper: every prep and resume got a
# 422 for a missing `out_dir`. A route is never a private helper.
routed = [n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
          and any(ast.get_source_segment(src, d).startswith(("app.get(", "app.post(", "app.put(",
                                                              "app.delete(", "app.patch("))
                  for d in n.decorator_list)]
check("no route is bound to a private helper", not [n for n in routed if n.startswith("_")])
check("offline-prepare is served by offline_prepare", "offline_prepare" in routed)

# The page answers `metadata_update` by re-reading the series' files, and that
# read spawns the section fetch again. Announcing an unchanged result looped at
# 11 events a second (20.30.5).
ssf = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_spawn_section_fetch")
ssf_src = ast.get_source_segment(src, ssf)
check("the section fetch announces only what it stored",
      "wrote=wrote" in ssf_src and "if wrote:" in ssf_src
      and "if await _fetch_section_metadata(" not in ssf_src)
fsm = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "_fetch_section_metadata")
fsm_src = ast.get_source_segment(src, fsm)
check("and it says so only after its write",
      fsm_src.index("wrote.append(True)") > fsm_src.index("async with mutate_library()"))

print("the NVDEC probe reads seconds, not the whole file, and runs once (20.30.6)")
# A codec the GPU can't decode fails on every frame, and ffmpeg read the whole
# episode before giving up: the first on-demand play of it waited 46 s.
import asyncio                            # noqa: E402
hw_names = {"HWDEC_PROBE_READ_S", "HWDEC_PROBE_TIMEOUT_S", "_HWDEC_FAIL_MARKS"}
hw_src = [ast.get_source_segment(src, n) for n in tree.body
          if (isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name)
              and n.targets[0].id in hw_names)
          or (isinstance(n, ast.AsyncFunctionDef) and n.name == "_gpu_can_hwdecode")]
check("the probe and its limits were found", len(hw_src) == 4)
hw_runs = []


async def _fake_probe_run(cmd, keep="stdout", kw=None, timeout=120.0):
    hw_runs.append((cmd, keep, timeout))
    await asyncio.sleep(0.05)
    return hw_ns["_next"]


async def _yes():
    return True

hw_ns = {"asyncio": asyncio, "Path": Path, "_hwdec_probe": {}, "_hwdec_inflight": {},
         "_has_nvenc": _yes, "_probe_run": _fake_probe_run, "_FFMPEG_SUBPROCESS_KW": {},
         "analyzer": types.SimpleNamespace(ffmpeg_bin=lambda: "ffmpeg"),
         "print": lambda *a, **k: None, "_next": (0, b"")}
exec("\n\n".join(hw_src), hw_ns)
gch = hw_ns["_gpu_can_hwdecode"]


async def _three():
    return await asyncio.gather(*(gch(Path("a.mkv"), "h264", "yuv420p10le") for _ in range(3)))

hw_ns["_next"] = (69, b"[h264] hwaccel initialisation returned error.")
got = asyncio.run(_three())
check("three plays at once run one probe", len(hw_runs) == 1 and got == [False] * 3)
cmd = hw_runs[0][0]
check("it reads only the first seconds of the source",
      "-t" in cmd and cmd.index("-t") < cmd.index("-i")
      and cmd[cmd.index("-t") + 1] == str(hw_ns["HWDEC_PROBE_READ_S"]))
check("and gives up well before the default 120 s", hw_runs[0][2] <= 30)
check("the answer is kept", asyncio.run(gch(Path("b.mkv"), "h264", "yuv420p10le")) is False
      and len(hw_runs) == 1)
hw_ns["_next"] = (0, b"Failed setup for format cuda: hwaccel initialisation returned error")
check("an hwaccel set-up error is a no even when ffmpeg exits 0",
      asyncio.run(gch(Path("c.mkv"), "av1", "yuv420p")) is False)
hw_ns["_next"] = (0, b"")
check("a clean decode is a yes", asyncio.run(gch(Path("d.mkv"), "hevc", "yuv420p10le")) is True)
check("and nothing is left in flight", hw_ns["_hwdec_inflight"] == {})

print("a slow on-demand start says where its time went (20.30.6)")
sod = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "stream_ondemand")
sod_src = ast.get_source_segment(src, sod)
stages = [m for m in ("lib+source", "ffprobe", "session", "prep-enqueue", "subs")]
check("every stage is marked, in order",
      all(f'_mark("{m}")' in sod_src for m in stages)
      and [sod_src.index(f'_mark("{m}")') for m in stages]
      == sorted(sod_src.index(f'_mark("{m}")') for m in stages))
check("and the line is written past the threshold",
      "OD_START_SLOW_S" in sod_src and "slow start" in sod_src)

print("the series page asks qBit once for all its members (20.30.7)")
# South Park is 54 items: 108 qBit round trips and 54 thread hops in turn, 0.35 s
# alone and 5-8 s with 20 readers.
qv = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "_qbit_view")
qv_src = ast.get_source_segment(src, qv)
qv_calls = []


async def _fake_qreq(method, url, **kw):
    qv_calls.append(url)
    return types.SimpleNamespace(status_code=200, json=lambda: [
        {"hash": "aaa", "state": "uploading"}, {"hash": "bbb", "state": "stalledUP"}])


async def _fake_qfiles(h):
    qv_calls.append("files:" + h)
    await asyncio.sleep(0.01)
    return [{"name": h + ".mkv", "progress": 1.0}]

qv_ns = {"asyncio": asyncio, "qreq": _fake_qreq, "qbit_files": _fake_qfiles,
         "QBIT_VIEW_PARALLEL": 8, "Optional": None}
exec(qv_src, qv_ns)
view = asyncio.run(qv_ns["_qbit_view"](["AAA", "bbb", "", "bbb", "ccc"]))
check("one torrents/info call names every hash once",
      [u for u in qv_calls if "torrents/info" in u] == ["/api/v2/torrents/info?hashes=AAA|bbb|ccc"])
check("each file list is fetched once", sorted(u for u in qv_calls if u.startswith("files:"))
      == ["files:AAA", "files:bbb", "files:ccc"])
check("a hash matches whatever its case", view["AAA"][0] == {"hash": "aaa", "state": "uploading"})
check("a torrent qBit does not know reads as no info", view["ccc"][0] is None)
check("no hashes, no calls", asyncio.run(qv_ns["_qbit_view"](["", ""])) == {})


async def _down_qreq(method, url, **kw):
    qv_calls.append(url)
    return None

qv_calls.clear()
qv_ns["qreq"] = _down_qreq
down = asyncio.run(qv_ns["_qbit_view"](["aaa", "bbb"]))
check("qBit unreachable: no file lists asked for, every member reads as unknown (20.30.9)",
      len(qv_calls) == 1 and down == {"aaa": (None, []), "bbb": (None, [])})
gsf = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "get_series_files")
gsf_src = ast.get_source_segment(src, gsf)
check("the series page builds every member from one view",
      "await _qbit_view(" in gsf_src and "_build_item_files(it, profile_id, qview, prepped)" in gsf_src
      and gsf_src.count("_prepped_paths_sync") == 1)
bif = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "_build_item_files")
bif_src = ast.get_source_segment(src, bif)
check("and a member given the view asks qBit nothing itself",
      bif_src.index("if qview is not None:") < bif_src.index("await qbit_info(")
      and "if prepped is None:" in bif_src)

print("ffprobe is started before anyone waits on it (20.30.7)")
life = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "lifespan")
life_src = ast.get_source_segment(src, life)
check("lifespan warms ffprobe in the background, before serving",
      "asyncio.ensure_future(_warm_ffprobe())" in life_src
      and life_src.index("_warm_ffprobe()") < life_src.index("yield"))
wf = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "_warm_ffprobe")
check("on a worker thread", "_probe_run" in _direct_calls(wf)
      and "create_subprocess_exec" not in _direct_calls(wf))
check("an on-demand start ffprobe could not answer is not called 'no video'",
      sod_src.index("if not info:") < sod_src.index('if not info.get("video"):')
      and "HTTPException(503" in sod_src)

print("a qBit that is not listening costs one refused connect, not one per call (20.30.8)")
qr = [ast.get_source_segment(src, n) for n in tree.body
      if (isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name)
          and n.targets[0].id in ("QBIT_DOWN_HOLD_S", "_qbit_down_until"))
      or (isinstance(n, ast.AsyncFunctionDef) and n.name in ("qreq", "qbit_login"))]
check("qreq, qbit_login and their hold were found", len(qr) == 4)


class _ConnErr(Exception):
    pass


class _ConnTimeout(Exception):
    pass

qr_calls = []
qr_clock = [100.0]


async def _qr_request(method, url, **kw):
    qr_calls.append(url)
    if qr_ns["_refuse"]:
        raise _ConnErr("refused")
    return types.SimpleNamespace(status_code=200, text="Ok.")


async def _qr_post(url, **kw):
    return await _qr_request("POST", url, **kw)

qr_ns = {"httpx": types.SimpleNamespace(ConnectError=_ConnErr, ConnectTimeout=_ConnTimeout,
                                        Response=object),
         "time": types.SimpleNamespace(monotonic=lambda: qr_clock[0]),
         "runtime": types.SimpleNamespace(qbit=types.SimpleNamespace(request=_qr_request, post=_qr_post)),
         "settings": types.SimpleNamespace(qbit_url="http://q"), "Optional": __import__("typing").Optional,
         "_refuse": True}
exec("\n\n".join(qr), qr_ns)
check("a refused connect answers None", asyncio.run(qr_ns["qreq"]("GET", "/a")) is None
      and len(qr_calls) == 1)
check("the next calls answer None without connecting",
      asyncio.run(qr_ns["qreq"]("GET", "/b")) is None and len(qr_calls) == 1)
qr_ns["_refuse"] = False
qr_clock[0] += qr_ns["QBIT_DOWN_HOLD_S"] + 0.1
r = asyncio.run(qr_ns["qreq"]("GET", "/c"))
check("after the hold it tries again", r is not None and r.status_code == 200 and len(qr_calls) == 2)
qr_ns["settings"].qbit_username = qr_ns["settings"].qbit_password = "x"
qr_ns["_refuse"] = True
qr_clock[0] += qr_ns["QBIT_DOWN_HOLD_S"] + 0.1
check("a refused login starts the hold too (20.30.12)",
      asyncio.run(qr_ns["qbit_login"]()) is False and len(qr_calls) == 3
      and asyncio.run(qr_ns["qreq"]("GET", "/d")) is None and len(qr_calls) == 3)
check("and a login inside the hold connects to nothing",
      asyncio.run(qr_ns["qbit_login"]()) is False and len(qr_calls) == 3)
ss = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "setup_status")
ss_src = ast.get_source_segment(src, ss)
check("the setup checklist asks qBit through qreq with a short wait, and logs in only on a 403",
      "qbit_login()" not in ss_src and 'qreq("GET", "/api/v2/app/version")' in ss_src
      and "SETUP_QBIT_WAIT_S" in ss_src)

print("an on-demand play of a file set to never builds no bundle (20.30.8)")
check("the background prep is skipped for never",
      sod_src.index('!= "never"') < sod_src.index("await _maybe_start_prep_job(src, item_id)"))

print("the anime table is parsed before anything can ask for it (20.30.9)")
# A background warm-up lost the race to the kiosk's first episode list after a
# boot, which parsed 1.7 MB of XML on the loop (0.20 s).
check("lifespan awaits the parse before it starts any task",
      "await _anime_map_warm()" in life_src
      and life_src.index("await _anime_map_warm()") < life_src.index("asyncio.create_task(")
      and "_spawn_bg(_anime_map_warm())" not in life_src)

print("a slow series page says where its time went (20.30.10)")
gsf = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "get_series_files")
gsf_src = ast.get_source_segment(src, gsf)
ser_stages = ["lib", "qbit", "prepped", "build", "sections", "ep-homes", "nudge"]
check("every stage is marked, in order",
      all(f'_mark("{m}")' in gsf_src for m in ser_stages)
      and [gsf_src.index(f'_mark("{m}")') for m in ser_stages]
      == sorted(gsf_src.index(f'_mark("{m}")') for m in ser_stages))
check("and the line is written past the threshold", "SERIES_SLOW_S" in gsf_src)
check("the qBit client gives up a connect long before a read",
      "runtime.qbit = _http_client(timeout=httpx.Timeout(10.0, connect=QBIT_CONNECT_TIMEOUT_S))" in life_src)

print()
if fails:
    print(f"{len(fails)} FAILED"); sys.exit(1)
print("all passed")
