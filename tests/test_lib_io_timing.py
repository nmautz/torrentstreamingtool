"""Where a slow library.json transaction spends its time (#96).

    python tests/test_lib_io_timing.py      (or `make test`)

On 2026-10-18 every progress save on the box held the library lock 5-17 s while
the F: drive was busy, and the logs could not say whether the write, the fsync,
the replace or the snapshot was slow, nor which handler held the lock (the task
name is starlette's `coro` for every request). This drives the real
`_write_durable`, `_save_lib_raw`, `_load_lib_raw` and `mutate_library` from
main.py against a fake clock, and the real `InstrumentedLock` / `await_chain`
from diagnostics.py.

main.py can't be imported without the whole dependency tree, so - like
tests/test_delete_locked.py - the functions under test are lifted out of its
source by name. The lift asserts every name was found.
"""
import ast, asyncio, io, json, logging, marshal, os, sys, tempfile, threading, time, types
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import diagnostics as diag  # noqa: E402  (stdlib only)

import _backend_src                       # noqa: E402
src = _backend_src.read()
tree = ast.parse(src)

WANT = {"_write_durable", "_steps_note", "_note_slow_lib_io", "_save_lib_raw",
        "_save_lib_raw_inner", "_lib_file_key",
        "_load_lib_raw", "mutate_library", "_library_txn", "_note_slow_txn",
        "LibraryUnchanged", "get_library", "put_library", "lib_io",
        "_apply_edits", "_edits_saved", "_load_with_edits", "_loads_with_edits"}
CONSTS = {"_LIB_IO_SLOW_S", "_SAVE_RETRY_DELAYS", "_LIB_POOL"}

fails = []


def check(name, ok, detail=""):
    print(("  ok   " if ok else "  FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        fails.append(name)


class Clock:
    """A monotonic clock the test moves by hand; shared with worker threads."""
    def __init__(self):
        self.t = 1000.0
        self._lk = threading.Lock()

    def monotonic(self):
        with self._lk:
            return self.t

    def advance(self, s):
        with self._lk:
            self.t += s


CLOCK = Clock()


class Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, rec):
        self.lines.append((rec.levelname, rec.getMessage()))

    def take(self):
        out, self.lines = self.lines, []
        return out


CAP = Capture()
log = logging.getLogger("test-lib-io")
log.addHandler(CAP)
log.setLevel(logging.DEBUG)
log.propagate = False
diag.log.addHandler(CAP)

TMP = Path(tempfile.mkdtemp())
SLOW = {"fsync": 0.0, "close": 0.0, "migrate": 0.0, "snapshot": 0.0}
REPLACE_FAILS = [0]


def _fsync(fd):
    os.fsync(fd)
    CLOCK.advance(SLOW["fsync"])


def _replace(a, b):
    if REPLACE_FAILS[0]:
        REPLACE_FAILS[0] -= 1
        raise PermissionError(5, "Access is denied", str(a), None, str(b))
    os.replace(a, b)


fake_os = types.SimpleNamespace(fsync=_fsync, replace=_replace)
fake_time = types.SimpleNamespace(monotonic=CLOCK.monotonic,
                                  sleep=lambda s: CLOCK.advance(s))


MIGRATED = [0]


def _migrate_item(it):
    CLOCK.advance(SLOW["migrate"])       # one item in LIB
    MIGRATED[0] += 1
    return it


def _snapshot(data, blob):
    CLOCK.advance(SLOW["snapshot"])


ns = {
    "Optional": Optional, "Path": Path, "os": fake_os, "time": fake_time, "json": json,
    "sys": sys, "asyncio": asyncio, "asynccontextmanager": asynccontextmanager,
    "log": log, "LIBRARY_FILE": TMP / "library.json",
    "_maybe_snapshot_library": _snapshot,
    "_lock_failure_desc": lambda exc: "[WinError 5] replace",
    "_lock_holders_note": lambda: "",
    "_parse_library_bytes": lambda b: json.loads(b) if b else None,
    "_recover_library": lambda: None,
    "_migrate_item": _migrate_item,
    "_fold_item_hides": lambda raw: None,
    "marshal": marshal, "_LIB_CACHE": None, "_LIB_GEN": 0, "diag": diag,
    "_DEFERRED": [],
}

pieces, found = [], set()
for node in tree.body:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name in WANT:
        seg = ast.get_source_segment(src, node)
        decos = "".join("@" + ast.get_source_segment(src, d) + "\n" for d in node.decorator_list)
        pieces.append(decos + seg); found.add(node.name)
    elif isinstance(node, ast.Assign):
        for t in node.targets:
            if isinstance(t, ast.Name) and t.id in CONSTS:
                pieces.append(ast.get_source_segment(src, node)); found.add(t.id)
missing = (WANT | CONSTS) - found
assert not missing, f"not found in main.py: {missing}"
exec("\n\n".join(pieces), ns)
# `open` inside _write_durable is the builtin; the real file goes to TMP.


def reset():
    for k in SLOW:
        SLOW[k] = 0.0
    REPLACE_FAILS[0] = 0
    CAP.take()


def warnings(lines):
    return [m for lvl, m in lines if lvl == "WARNING"]


LIB = {"profiles": [{"id": "p"}], "items": [{"id": "a", "progress": {}}]}

print("_write_durable")
reset()
steps = {}
ns["_write_durable"](TMP / "x.json", b"{}", steps)
check("writes the file", (TMP / "x.json").read_bytes() == b"{}")
check("no temp file left", not (TMP / "x.json.tmp").exists())
check("times every step", list(steps) == ["open", "write", "fsync", "close", "replace"],
      str(steps))
SLOW["fsync"] = 2.5
ns["_write_durable"](TMP / "x.json", b"{}", steps)
check("accumulates across calls (a retry is not free)", abs(steps["fsync"] - 2.5) < 1e-9,
      str(steps))
ns["_write_durable"](TMP / "x.json", b"{}")
check("steps are optional", True)

print("_save_lib_raw")
reset()
ns["_save_lib_raw"](LIB)
check("a fast save logs nothing", warnings(CAP.take()) == [])
check("and wrote the library", json.loads(ns["LIBRARY_FILE"].read_text()) == LIB)

reset()
SLOW["fsync"] = 6.8
ns["_save_lib_raw"](LIB)
w = warnings(CAP.take())
check("a slow save logs one line", len(w) == 1, str(w))
check("naming the slow step", w and "fsync 6.80s" in w[0], str(w))
check("with every step in order", w and w[0].index("dumps") < w[0].index("open")
      < w[0].index("fsync") < w[0].index("replace") < w[0].index("snapshot"), str(w))
check("and the size", w and "MB)" in w[0], str(w))

reset()
SLOW["snapshot"] = 3.0
ns["_save_lib_raw"](LIB)
w = warnings(CAP.take())
check("a slow snapshot is named too", w and "snapshot 3.00s" in w[0], str(w))

reset()
REPLACE_FAILS[0] = 2
ns["_save_lib_raw"](LIB)
lines = CAP.take()
check("a retried save still succeeds", any("succeeded on attempt 3" in m for _, m in lines),
      str(lines))
check("retry waits are counted, not hidden",
      not warnings(lines) or "retry_wait" in warnings(lines)[0], str(lines))

reset()
REPLACE_FAILS[0] = 99
try:
    ns["_save_lib_raw"](LIB)
    check("a save out of retries raises", False)
except PermissionError:
    errs = [m for lvl, m in CAP.take() if lvl == "ERROR"]
    check("a save out of retries raises", True)
    check("and its error line carries the steps", errs and "retry_wait" in errs[0], str(errs))

print("_load_lib_raw")
reset()
ns["LIBRARY_FILE"].write_text(json.dumps(LIB))
got = ns["_load_lib_raw"]()
check("loads the library", got["items"] == LIB["items"])
check("a fast load logs nothing", warnings(CAP.take()) == [])
SLOW["migrate"] = 4.0
ns["_LIB_CACHE"] = None                  # a cold load; the cache is checked below
ns["_load_lib_raw"]()
w = warnings(CAP.take())
check("a slow load names the step", w and "load took" in w[0] and "migrate 4.00s" in w[0],
      str(w))

print("the parsed-library cache")
reset()
LIBF = ns["LIBRARY_FILE"]
LIBF.write_text(json.dumps(LIB))
ns["_LIB_CACHE"] = None
MIGRATED[0] = 0
a = ns["_load_lib_raw"]()
b = ns["_load_lib_raw"]()
check("an unchanged file is parsed once", MIGRATED[0] == 1, str(MIGRATED))
check("a hit returns the same library", b == a == {**LIB, "items": LIB["items"]})
b["items"][0]["progress"]["mine"] = 1
check("each load is its own copy", "mine" not in ns["_load_lib_raw"]()["items"][0]["progress"])
LIBF.write_text(json.dumps({"profiles": [], "items": [{"id": "other", "progress": {}}]}))
check("a file written by someone else is read again",
      ns["_load_lib_raw"]()["items"][0]["id"] == "other")
ns["_save_lib_raw"](LIB)
n0 = MIGRATED[0]
got = ns["_load_lib_raw"]()
check("a save is read back, not served from the cache",
      got["items"] == LIB["items"] and MIGRATED[0] == n0 + 1, str(MIGRATED))
ns["_load_lib_raw"]()
check("and is cached after that", MIGRATED[0] == n0 + 1, str(MIGRATED))
# A save that runs while a load is between its stat and its store: the load must
# not keep what it parsed. The fake migrate step is where the save lands.
real_migrate = ns["_migrate_item"]
ns["_LIB_CACHE"] = None
ns["_migrate_item"] = lambda it: (ns.__setitem__("_LIB_GEN", ns["_LIB_GEN"] + 2), it)[1]
ns["_load_lib_raw"]()
ns["_migrate_item"] = real_migrate
check("a load overlapped by a save keeps nothing", ns["_LIB_CACHE"] is None)
REPLACE_FAILS[0] = 99
ns["_load_lib_raw"]()
try:
    ns["_save_lib_raw"](LIB)
except PermissionError:
    pass
CAP.take()
check("a failed save still drops the cache", ns["_LIB_CACHE"] is None)
REPLACE_FAILS[0] = 0

print("mutate_library")
SEEN_HOLDER = []


def slow_save(data):
    SEEN_HOLDER.append(LOCK._probe.holder)
    CLOCK.advance(SLOW["fsync"])
    ns["LIBRARY_FILE"].write_text(json.dumps(data))


ns["_save_lib_raw"] = slow_save
ns["_load_lib_raw"] = lambda: json.loads(ns["LIBRARY_FILE"].read_text())


async def update_progress():
    async with ns["mutate_library"]() as lib:
        SEEN_HOLDER.append(LOCK._probe.holder)
        lib["items"][0]["progress"]["p"] = 42


async def no_op_tick():
    async with ns["mutate_library"]() as lib:
        raise ns["LibraryUnchanged"]()


async def bad_request():
    async with ns["mutate_library"]() as lib:
        CLOCK.advance(1.5)
        raise ValueError("404")


async def reader():
    return await ns["get_library"]()


async def run(coro_fn):
    global LOCK
    LOCK = diag.InstrumentedLock(asyncio.Lock(), "library-test")
    ns["runtime"] = types.SimpleNamespace(_lib_lock=LOCK)   # backend/runtime.py
    return await coro_fn()

reset()
asyncio.run(run(update_progress))
check("commits the change",
      json.loads(ns["LIBRARY_FILE"].read_text())["items"][0]["progress"]["p"] == 42)
check("labels the lock with the caller and step",
      SEEN_HOLDER == ["update_progress [body]", "update_progress [save]"], str(SEEN_HOLDER))
check("a fast transaction logs nothing", warnings(CAP.take()) == [])

reset()
SEEN_HOLDER.clear()
SLOW["fsync"] = 7.0
asyncio.run(run(update_progress))
w = warnings(CAP.take())
check("a slow transaction logs who and where",
      w and "library txn by update_progress held the lock 7.00s (saved;" in w[0]
      and "save 7.00s" in w[0], str(w))

reset()
SLOW["fsync"] = 7.0
asyncio.run(run(no_op_tick))
check("LibraryUnchanged skips the save (and its time)", warnings(CAP.take()) == [])

reset()
try:
    asyncio.run(run(bad_request))
    check("a body exception propagates", False)
except ValueError:
    w = warnings(CAP.take())
    check("a body exception propagates", True)
    check("and a slow aborted transaction says so",
          w and "(aborted;" in w[0] and "body 1.50s" in w[0], str(w))

reset()
SEEN_HOLDER.clear()
ns["_load_lib_raw"] = lambda: (SEEN_HOLDER.append(LOCK._probe.holder), LIB)[1]
asyncio.run(run(reader))
check("get_library labels the lock with its caller",
      SEEN_HOLDER == ["reader [get_library]"], str(SEEN_HOLDER))

print("diagnostics")
reset()


async def lock_line():
    lk = diag.InstrumentedLock(asyncio.Lock(), "named")
    old = diag.LOCK_HELD_WARN_S
    diag.LOCK_HELD_WARN_S = 0.0
    try:
        async with lk:
            lk.set_holder("update_progress [save]")
    finally:
        diag.LOCK_HELD_WARN_S = old
    lk.set_holder("ignored")      # not held: a no-op
    return lk

lk = asyncio.run(lock_line())
w = warnings(CAP.take())
check("the LOCK line names the holder", w and "by update_progress [save]" in w[0], str(w))
check("set_holder on a free lock does nothing", lk._probe.holder == "")


async def inner_wait(ev):
    await ev.wait()


async def middle(ev):
    await inner_wait(ev)


async def chain_case():
    ev = asyncio.Event()
    t = asyncio.create_task(middle(ev), name="req")
    await asyncio.sleep(0)
    frames, leaf = diag.await_chain(t)
    names = [f.f_code.co_name for f in frames]
    ev.set()
    await t
    return names, leaf

names, leaf = asyncio.run(chain_case())
check("await_chain follows the whole chain, outermost first",
      names[:3] == ["middle", "inner_wait", "wait"], str(names))
check("and stops at the frameless leaf it waits on",
      leaf is not None and not hasattr(leaf, "cr_frame"), repr(leaf))

print("a read does not wait for a save (20.30.1)")
# Real clock, real lock, real threads: a writer stuck in its fsync, readers meanwhile.
rns = {"Optional": Optional, "Path": Path, "time": time, "json": json, "sys": sys,
       "asyncio": asyncio, "asynccontextmanager": asynccontextmanager, "marshal": marshal,
       "log": log, "diag": diag, "LIBRARY_FILE": TMP / "reads.json",
       "_SAVE_RETRY_DELAYS": (), "_LIB_CACHE": None, "_LIB_GEN": 0, "_LIB_LAST": None,
       "_LIB_POOL": None, "_LIB_IO_SLOW_S": 99.0,
       "_parse_library_bytes": lambda b: json.loads(b) if b else None,
       "_recover_library": lambda: None, "_migrate_item": lambda it: it,
       "_fold_item_hides": lambda raw: None, "_note_slow_lib_io": lambda *a: None,
       "_lock_failure_desc": lambda e: "", "_lock_holders_note": lambda: "",
       "_DEFERRED": [], "_DEFERRED_SEQ": 0, "_DEFERRED_FLUSH_S": 0.1,
       "_DEFERRED_MAX": 20000, "_deferred_task": None}
FSYNC = threading.Event()
SAVE_FAILS = [False]


def _inner(data):
    FSYNC.wait(5)
    if SAVE_FAILS[0]:
        raise OSError("disk gone")
    rns["LIBRARY_FILE"].write_text(json.dumps(data))


rns["_save_lib_raw_inner"] = _inner
rpieces = [ast.get_source_segment(src, n) for n in tree.body
           if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name in
           {"_lib_file_key", "_load_lib_raw", "_save_lib_raw", "get_library", "lib_io",
            "mutate_library", "_library_txn", "_note_slow_txn", "LibraryUnchanged",
            "_pending_edits", "_apply_edits", "_edits_saved", "_load_with_edits",
            "_loads_with_edits", "defer_edit", "_flush_deferred_later", "flush_deferred"}]
rns["_library_txn"] = None
exec("\n\n".join(p if not p.startswith("async def _library_txn") else "@asynccontextmanager\n" + p
                  for p in rpieces), rns)
rns["LIBRARY_FILE"].write_text(json.dumps({"items": [], "v": 1}))


async def reads_case():
    rns["runtime"] = types.SimpleNamespace(_lib_lock=diag.InstrumentedLock(asyncio.Lock(), "reads"))
    out = {}

    async def writer(v):
        async with rns["mutate_library"]() as lib:
            lib["v"] = v

    FSYNC.clear()
    w = asyncio.ensure_future(writer(2))
    await asyncio.sleep(0.2)                                # the writer is in its fsync
    t0 = time.monotonic()
    out["during"] = (await rns["get_library"]())["v"]
    out["during_s"] = time.monotonic() - t0
    FSYNC.set()
    await w
    out["after_last"] = rns["_LIB_LAST"]
    FSYNC.clear()
    w = asyncio.ensure_future(writer(3))
    await asyncio.sleep(0.2)
    out["during2"] = (await rns["get_library"]())["v"]     # the writer loaded after v=2
    FSYNC.set()
    await w
    out["free"] = (await rns["get_library"]())["v"]
    SAVE_FAILS[0] = True
    FSYNC.set()
    try:
        await writer(4)
    except OSError:
        pass
    SAVE_FAILS[0] = False
    out["failed_last"] = marshal.loads(rns["_LIB_LAST"])["v"] if rns["_LIB_LAST"] else None
    rns["_LIB_LAST"] = marshal.dumps({"v": "stale"})
    out["unlocked"] = (await rns["get_library"]())["v"]   # lock free: the file, not the copy
    return out

r = asyncio.run(reads_case())
check("a read while a save is in its fsync gets the last finished write, at once",
      r["during"] == 1 and r["during_s"] < 0.15, str(r))
check("a finished save clears the copy (its write is not in it)", r["after_last"] is None, str(r))
check("the next writer's load makes it current again", r["during2"] == 2, str(r))
check("with the lock free a read waits for nobody and reads the file", r["free"] == 3, str(r))
check("a failed save leaves the copy (the file did not change)", r["failed_last"] == 3, str(r))
check("the copy is used only while the lock is taken", r["unlocked"] == 3, str(r))
retire = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef)
              and n.name == "_retire_ai_subtitles")
check("no caller writes back what get_library returned",
      "put_library(lib)" not in ast.get_source_segment(src, retire))


print("progress doesn't wait for the disk (20.30.3)")
SAVES = [0]
_inner_reads = rns["_save_lib_raw_inner"]


def _counting_inner(data):
    SAVES[0] += 1
    _inner_reads(data)


rns["_save_lib_raw_inner"] = _counting_inner


def _pos(v):
    """An edit like update_progress's: pure, skips a missing item."""
    def fn(lib):
        it = next((i for i in lib["items"] if i["id"] == "a"), None)
        if it is not None:
            it["pos"] = v
    return fn


def _disk_pos():
    return next((i.get("pos") for i in json.loads(rns["LIBRARY_FILE"].read_text())["items"]
                 if i["id"] == "a"), None)


async def deferred_case():
    rns["runtime"] = types.SimpleNamespace(_lib_lock=diag.InstrumentedLock(asyncio.Lock(), "defer"))
    rns["LIBRARY_FILE"].write_text(json.dumps({"items": [{"id": "a"}], "v": 1}))
    rns["_LIB_CACHE"], rns["_LIB_LAST"] = None, None
    out = {}
    FSYNC.set()
    SAVE_FAILS[0] = False
    SAVES[0] = 0

    rns["defer_edit"](_pos(10))
    out["read_free"] = (await rns["get_library"]())["items"][0].get("pos")
    out["disk_before"] = _disk_pos()
    await asyncio.sleep(0.3)
    out["disk_after"], out["pending_after"], out["saves1"] = _disk_pos(), len(rns["_DEFERRED"]), SAVES[0]

    # A save stuck in its fsync: the POST still returns at once, a read sees it.
    FSYNC.clear()

    async def writer(body=None):
        async with rns["mutate_library"]() as lib:
            if body:
                body(lib)

    seen = {}
    w = asyncio.ensure_future(writer(lambda lib: seen.update(v=lib["items"][0].get("pos"))))
    await asyncio.sleep(0.05)
    t0 = time.monotonic()
    rns["defer_edit"](_pos(20))
    out["defer_s"] = time.monotonic() - t0
    t0 = time.monotonic()
    out["read_locked"] = (await rns["get_library"]())["items"][0].get("pos")
    out["read_locked_s"] = time.monotonic() - t0
    FSYNC.set()
    await w
    out["pending_mid"] = len(rns["_DEFERRED"])      # 20 arrived after that load
    await asyncio.sleep(0.3)
    out["disk_mid"], out["pending_mid2"] = _disk_pos(), len(rns["_DEFERRED"])

    # Another writer's transaction carries the pending edit, and its body sees it.
    SAVES[0] = 0
    rns["defer_edit"](_pos(30))
    await writer(lambda lib: seen.update(w2=lib["items"][0].get("pos")))
    out["carried"], out["pending_c"] = _disk_pos(), len(rns["_DEFERRED"])
    await asyncio.sleep(0.3)
    out["saves_c"] = SAVES[0]                         # the flusher found nothing left

    # A writer that decides not to write keeps the edit pending.
    rns["defer_edit"](_pos(40))

    async def unchanged():
        async with rns["mutate_library"]():
            raise rns["LibraryUnchanged"]()
    await unchanged()
    out["unchanged_pending"] = len(rns["_DEFERRED"])
    await asyncio.sleep(0.3)
    out["after_unchanged"] = _disk_pos()

    # A save that fails keeps it; the next pass saves it.
    SAVE_FAILS[0] = True
    rns["defer_edit"](_pos(50))
    await asyncio.sleep(0.25)
    out["failed_pending"], out["failed_disk"] = len(rns["_DEFERRED"]), _disk_pos()
    out["failed_read"] = (await rns["get_library"]())["items"][0].get("pos")
    SAVE_FAILS[0] = False
    await asyncio.sleep(0.3)
    out["recovered"], out["pending_end"] = _disk_pos(), len(rns["_DEFERRED"])

    # An edit for an item that is gone is skipped, not an error.
    rns["defer_edit"](lambda lib: next(i for i in lib["items"] if i["id"] == "gone"))
    rns["defer_edit"](_pos(60))
    await rns["flush_deferred"]()
    out["after_bad"], out["pending_bad"] = _disk_pos(), len(rns["_DEFERRED"])
    SAVES[0] = 0
    await rns["flush_deferred"]()
    out["empty_flush_saves"] = SAVES[0]
    out["seen"] = seen
    return out


r = asyncio.run(deferred_case())
check("a POST is seen by the next read before it is on disk",
      r["read_free"] == 10 and r["disk_before"] is None, str(r))
check("and is on disk within the flush delay, nothing left pending",
      r["disk_after"] == 10 and r["pending_after"] == 0 and r["saves1"] == 1, str(r))
check("accepting one never waits for a save in its fsync", r["defer_s"] < 0.01, str(r))
check("a read during that save sees it at once",
      r["read_locked"] == 20 and r["read_locked_s"] < 0.15, str(r))
check("an edit that came after a writer's load outlives that save, then lands",
      r["pending_mid"] == 1 and r["disk_mid"] == 20 and r["pending_mid2"] == 0, str(r))
check("another writer's save carries it, and its body sees it",
      r["carried"] == 30 and r["pending_c"] == 0 and r["seen"].get("w2") == 30
      and r["saves_c"] == 1, str(r))
check("a writer that writes nothing leaves it pending for the flusher",
      r["unchanged_pending"] == 1 and r["after_unchanged"] == 40, str(r))
check("a failed save keeps it (still read), and the next pass saves it",
      r["failed_pending"] == 1 and r["failed_disk"] == 40 and r["failed_read"] == 50
      and r["recovered"] == 50 and r["pending_end"] == 0, str(r))
check("one broken edit never blocks the rest",
      r["after_bad"] == 60 and r["pending_bad"] == 0, str(r))
check("a flush with nothing pending writes nothing", r["empty_flush_saves"] == 0, str(r))

up = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "update_progress")
up_src = ast.get_source_segment(src, up)
check("update_progress defers instead of opening a transaction",
      "defer_edit(" in up_src and "mutate_library" not in up_src and "await" not in up_src)
ap = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_apply_progress")
ap_src = ast.get_source_segment(src, ap)
check("its edit takes the POST's clock, not the time it is applied",
      "now_iso" in [a.arg for a in ap.args.args] and "_now_iso()" not in ap_src)
check("no load-then-save by hand skips the pending edits",
      "lib_io(_load_lib_raw)" not in src
      and src.count("lib_io(_load_with_edits, edits)")
      == sum(ln.strip() == "_edits_saved(edits)" for ln in src.splitlines()))
# The real edit, with the real watch rule: applied to a read and again by the
# save (or twice, when a save wrote the file and then raised) it must not move.
import watchrule                          # noqa: E402
import copy as _copy                      # noqa: E402
pns = {"Optional": Optional, "datetime": __import__("datetime").datetime,
       "timezone": __import__("datetime").timezone, "watchrule": watchrule,
       "_credits_start_for": lambda item, fp: None}
for n in tree.body:
    if isinstance(n, ast.FunctionDef) and n.name in {"_apply_progress", "_watch_state", "_parse_iso_dt"}:
        exec(ast.get_source_segment(src, n), pns)
    elif isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "_TRACK_PREF_KEYS"
                                           for t in n.targets):
        exec(ast.get_source_segment(src, n), pns)
base = {"items": [{"id": "x", "progress": {"p": {"file_progress": {"a.mkv": {
    "position_sec": 100, "duration_sec": 1400, "played_sec": 90, "completed": False,
    "updated_at": "2026-10-22T20:00:00+00:00", "subtitle_sel": {"k": 1}}}}}}]}
E = [(i, (lambda pos, at: (lambda lib: pns["_apply_progress"]("x", "p", "a.mkv", pos, 1400.0, at, lib)))(
    pos, at)) for i, (pos, at) in enumerate(((110.0, "2026-10-22T20:00:10+00:00"),
                                             (120.0, "2026-10-22T20:00:20+00:00"),
                                             (125.0, "2026-10-22T20:00:20+00:00")), 1)]
once = rns["_apply_edits"](_copy.deepcopy(base), tuple(E))
twice = rns["_apply_edits"](_copy.deepcopy(once), tuple(E))
fp1 = once["items"][0]["progress"]["p"]["file_progress"]["a.mkv"]
check("three POSTs land in order, track picks kept",
      fp1["position_sec"] == 125.0 and fp1["played_sec"] > 90 and fp1["subtitle_sel"] == {"k": 1}, str(fp1))
check("applying them again changes nothing", once == twice, str(twice))
pns["_apply_progress"]("gone", "p", "a.mkv", 1.0, 2.0, "2026-10-22T20:00:30+00:00", once)
check("an item that is gone is skipped", once == twice)
life = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "lifespan")
life_src = ast.get_source_segment(src, life)
check("a clean stop saves what is pending",
      "await flush_deferred()" in life_src[life_src.index("    yield"):])

print()
if fails:
    print(f"{len(fails)} FAILED"); sys.exit(1)
print("all passed")
