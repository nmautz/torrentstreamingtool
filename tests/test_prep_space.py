"""Automatic prep keeps its disk I/O off the event loop and stops before a disk fills.

    python tests/test_prep_space.py      (or `make test`)

Two incidents (20.25.12):

  * 2026-10-19: `_enqueue_library_prep` stat-ed every library file on the event
    loop (the source, its cache key, two signatures, `master.m3u8`). On a busy disk
    the server froze eight times in two hours, for up to 173 s, while the TV played.
  * 2026-10-20: auto-prep never looked at free space. It filled C: to 0 bytes under
    a pack that was still downloading, then failed ~60 encodes a minute.

main.py can't be imported without the whole dependency tree, so - like
tests/test_prep_gate.py - the functions under test are lifted out of its source by
name, and the event-loop rule is checked on the AST of the two async functions.
"""
import ast, shutil, sys, tempfile, types
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
import _backend_src                       # noqa: E402
src = _backend_src.read()
tree = ast.parse(src)

WANT = {"_prep_space_ok", "_disk_free_or_none", "_prep_room_sync", "_prep_candidates_sync"}
CONSTS = {"PREP_FREE_RESERVE_BYTES", "PREP_BUNDLE_FACTOR"}
# Never CALLED inside the async sweep: each one touches the disk. Passing one to
# asyncio.to_thread is a reference, not a call, and is what they should do.
SYNC_IO = {"exists", "stat", "is_file", "is_dir", "iterdir", "disk_usage",
           "_offline_cache_key", "_offline_cache_dir", "_bundle_unbuildable",
           "_prep_unreadable_wait", "_file_sig", "_file_sig_for",
           "_prep_candidates_sync", "_bundle_dir_built_sync", "_disk_free_or_none",
           "_prep_room_sync", "_needs_validation", "_validation_targets_sync", "_make_verdict"}
ON_LOOP = {"_enqueue_library_prep", "_maybe_start_prep_job",
           # #73: the auto-validator's whole-library stat sweep, every 30 s
           "background_maintenance_loop", "_run_auto_validation"}

GB = 1024 ** 3
UNBUILDABLE, UNREADABLE = set(), set()
ns = {
    "Optional": Optional, "Path": Path, "shutil": shutil,
    "VIDEO_EXTS": {".mkv", ".mp4"},
    "OFFLINE_CACHE_DIRNAME": ".streamlink_cache",
    "_norm_path": lambda p: str(p),
    "_effective_prep_mode": lambda cfg, p: cfg.get(p, "idle"),
    "_effective_prep_priority": lambda cfg, p: "high" if p.endswith("E02.mkv") else "mid",
    "_PREP_PRIO_NUM": {"low": 0, "mid": 1, "high": 2},
    "_offline_cache_key_for": lambda name, size: f"k-{name}-{size}",
    "_bundle_unbuildable": lambda f, key: f["path"] in UNBUILDABLE,
    "_prep_unreadable_wait": lambda p: str(p) in UNREADABLE,
}
pieces, found, bodies = [], set(), {}
for node in tree.body:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        if node.name in WANT:
            pieces.append(ast.get_source_segment(src, node)); found.add(node.name)
        if node.name in ON_LOOP:
            bodies[node.name] = node
    elif isinstance(node, (ast.Assign, ast.AnnAssign)):
        for t in (node.targets if isinstance(node, ast.Assign) else [node.target]):
            if isinstance(t, ast.Name) and t.id in CONSTS:
                pieces.append(ast.get_source_segment(src, node)); found.add(t.id)
missing = (WANT | CONSTS) - found
assert not missing, f"not found in main.py: {missing}"
assert set(bodies) == ON_LOOP, f"not found in main.py: {ON_LOOP - set(bodies)}"
exec("\n\n".join(pieces), ns)

fails = []


def check(name, ok):
    print(("  ok    " if ok else "  FAIL  ") + name)
    if not ok:
        fails.append(name)


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

print("room for a bundle")
ok = ns["_prep_space_ok"]
reserve = ns["PREP_FREE_RESERVE_BYTES"]
check("0 bytes free refuses", ok(0, GB) is False)
check("exactly the reserve plus the bundle is enough",
      ok(reserve + int(GB * ns["PREP_BUNDLE_FACTOR"]), GB) is True)
check("a byte less is not", ok(reserve + int(GB * ns["PREP_BUNDLE_FACTOR"]) - 1, GB) is False)
check("a big film needs more than an episode", ok(reserve + 2 * GB, GB) and not ok(reserve + 2 * GB, 27 * GB))
check("an unmeasurable disk is not refused", ok(None, 100 * GB) is True)
check("disk_usage failing reads as unmeasurable",
      ns["_disk_free_or_none"]("/no/such/dir/at/all") is None)

print("which files the sweep considers")
with tempfile.TemporaryDirectory() as tmp:
    d = Path(tmp)
    paths = []
    for n in ("E01.mkv", "E02.mkv", "E03.mkv", "E04.mkv", "E05.mkv", "E06.mkv", "notes.txt"):
        (d / n).write_bytes(b"\0" * 10)
        paths.append(str(d / n))
    missing_path = str(d / "E07.mkv")
    built_dir = d / ".streamlink_cache" / "k-E01.mkv-10"
    built_dir.mkdir(parents=True)
    (built_dir / "master.m3u8").write_text("#EXTM3U\n")
    UNBUILDABLE.add(paths[3]); UNREADABLE.add(paths[4])
    item = {"files": [{"path": p} for p in paths + [missing_path]]}
    cfg = {paths[5]: "never"}
    rows = ns["_prep_candidates_sync"](item, {paths[2]}, cfg)
    got = {Path(r[0]).name: r for r in rows}
    check("only E01 and E02 are left (E03 incomplete, E04 unbuildable, E05 unreadable, "
          "E06 never, the .txt not a video, E07 not on disk)", sorted(got) == ["E01.mkv", "E02.mkv"])
    check("E01's bundle is seen as built", got["E01.mkv"][5] is True)
    check("E02's is not", got["E02.mkv"][5] is False)
    check("E02 carries its own priority", got["E02.mkv"][1] == 2 and got["E01.mkv"][1] == 1)
    check("each row carries the source size and its disk", got["E02.mkv"][2] == 10
          and got["E02.mkv"][3] == (d / "E02.mkv").stat().st_dev)
    free, size = ns["_prep_room_sync"](d / "E02.mkv")
    check("the pre-encode gate measures the source's disk", size == 10 and isinstance(free, int))
    check("a vanished source measures as size 0, not an error",
          ns["_prep_room_sync"](Path(missing_path))[1] == 0)

print()
if fails:
    print(f"{len(fails)} FAILED"); sys.exit(1)
print("all passed")
