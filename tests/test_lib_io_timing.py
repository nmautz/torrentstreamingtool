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
        "LibraryUnchanged", "get_library", "put_library", "lib_io"}
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
       "_lock_failure_desc": lambda e: "", "_lock_holders_note": lambda: ""}
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
            "mutate_library", "_library_txn", "_note_slow_txn", "LibraryUnchanged"}]
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

print()
if fails:
    print(f"{len(fails)} FAILED"); sys.exit(1)
print("all passed")
