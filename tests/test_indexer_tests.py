"""The indexer Test and the warm-up from main.py, replaying the box on 2026-10-11.

    python tests/test_indexer_tests.py      (or `make test`)

Five hours after a reboot the first thing to use FlareSolverr was a Test-all.
1337x got no answer in the 60 s Jackett allows and was listed as failing; alone
it then passed in 48 s and 13 s. So a Test that timed out is tried once more,
and one Test pass runs in the background after a start.

main.py can't be imported without its whole dependency tree, so the functions
are lifted out of its source (as tests/test_retry_candidates.py does). No
Jackett, no clock: time moves only when a stand-in says so.
"""

import ast
import asyncio
import io
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import ixwait            # noqa: E402

WANTED = ("_test_one_indexer", "_test_health_row", "_run_indexer_tests", "indexer_warmup")
import _backend_src                       # noqa: E402
src = _backend_src.read()
fns = {n.name: n for n in ast.parse(src).body
       if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in WANTED}

TIMEOUT = ("Exception (1337x): Exception: System.Threading.Tasks.TaskCanceledException: "
           "The request was canceled due to the configured HttpClient.Timeout of 60 seconds elapsing.\n"
           " ---> System.TimeoutException: The operation was canceled.\n"
           "   at FlareSolverrSharp.Solvers.FlareSolverr.<>c__DisplayClass12_0.MoveNext()")
REFUSED = ("Exception (torrentproject2): Received an unexpected EOF or 0 bytes from the transport stream.\n"
           " ---> System.Net.Http.HttpRequestException: The SSL connection could not be established")

_PASS = 0
_FAIL = []


def ok(name, cond, detail=""):
    global _PASS
    if cond:
        _PASS += 1
    else:
        _FAIL.append("%s%s" % (name, ("\n     " + detail) if detail else ""))


def eq(name, got, want):
    ok(name, got == want, "got %r, want %r" % (got, want))


class ReadTimeout(Exception):
    pass


class Resp:
    def __init__(self, status, error=""):
        self.status_code, self._error, self.text = status, error, error

    def json(self):
        return {"error": self._error} if self._error else {}


def world(script, configured=None, health=None, jackett_ok=True,
          solver_installed=False, solver_up=None, list_fails=0):
    """A fresh namespace with the lifted functions. `script[id]` is the list of
    answers that indexer gives, each `(seconds, error | "" | exception)`."""
    clock = [1000.0]
    posts = []
    logs = []
    state = SimpleNamespace(indexer_health=list(health or []), jackett_ok=jackett_ok)
    solver_up = list(solver_up or [])
    left = {"list_fails": list_fails}

    class Client:
        async def get(self, url, params=None):
            return SimpleNamespace(status_code=200, json=lambda: [
                {"id": i, "name": i.upper()} for i in (configured or sorted(script))])

        async def post(self, url):
            ixid = url.split("/indexers/")[1].split("/")[0]
            posts.append(ixid)
            secs, what = script[ixid].pop(0)
            clock[0] += secs
            if isinstance(what, Exception):
                raise what
            return Resp(500, what) if what else Resp(204)

    @asynccontextmanager
    async def admin(timeout=15.0):
        if left["list_fails"] > 0:
            left["list_fails"] -= 1
            raise ConnectionError("All connection attempts failed")
        yield Client()

    async def sleep(secs):
        clock[0] += secs

    async def apply(h):
        state.indexer_health = h

    async def solver_running():
        return solver_up.pop(0) if solver_up else True

    def logline(fmt, *a):
        logs.append(fmt % a if a else fmt)

    ns = {
        "ixwait": ixwait,
        "asyncio": SimpleNamespace(sleep=sleep, gather=asyncio.gather,
                                   CancelledError=asyncio.CancelledError),
        "time": SimpleNamespace(monotonic=lambda: clock[0]),
        "settings": SimpleNamespace(indexer_url="http://jackett"),
        "state": state,
        "log": SimpleNamespace(info=logline, warning=logline),
        "_IX_PACE": ixwait.Pace(),
        "_jackett_admin": admin,
        "_apply_indexer_health": apply,
        "_flaresolverr_bin_path": lambda: "flaresolverr.exe" if solver_installed else "",
        "_flaresolverr_running": solver_running,
    }
    for name in WANTED:
        exec(ast.get_source_segment(src, fns[name]), ns)
    return SimpleNamespace(ns=ns, state=state, posts=posts, logs=logs, clock=clock)


def run(coro):
    return asyncio.run(coro)


eq("every function was found in main.py", sorted(fns), sorted(WANTED))

# ── one Test ──────────────────────────────────────────────────────────────────
w = world({"1337x": [(60.0, TIMEOUT), (48.1, "")]})
async def _one(w, ixid="1337x"):
    async with w.ns["_jackett_admin"]() as c:
        return await w.ns["_test_one_indexer"](c, ixid)
res = run(_one(w))
eq("the box's morning: timed out, then passed", (res["ok"], res["error"]), (True, ""))
eq("it was tried twice", w.posts, ["1337x", "1337x"])
ok("the result says it took a second try", res["retried"] is True)
eq("the time is the try that passed", res["secs"], 48.1)
ok("the indexer is now known to be slow", w.ns["_IX_PACE"].is_slow("1337x"))
ok("the retry is in the log", any("trying once more" in l for l in w.logs), repr(w.logs))

w = world({"1337x": [(60.0, TIMEOUT), (60.0, TIMEOUT)]})
res = run(_one(w))
ok("timing out twice is failing", res["ok"] is False)
ok("and says twice", res["error"].startswith("Timed out twice. "), res["error"])
ok("with Jackett's reason", "HttpClient.Timeout of 60 seconds" in res["error"], res["error"])
eq("it is not tried a third time", len(w.posts), 2)

w = world({"torrentproject2": [(0.4, REFUSED)]})
res = run(_one(w, "torrentproject2"))
ok("a refusal is failing at once", res["ok"] is False and not res["retried"])
eq("and is asked once", len(w.posts), 1)
ok("its reason is one line", "\n" not in res["error"] and "SSL connection" in res["error"], res["error"])

w = world({"1337x": [(120.0, ReadTimeout())]})
res = run(_one(w))
eq("our own wait running out is not retried", len(w.posts), 1)
eq("and names the wait a Test gets", res["error"], "no answer from Jackett in 120 s")

w = world({"yts": [(0.5, "")]})
res = run(_one(w, "yts"))
eq("a quick pass is one try", (res["ok"], res["retried"], len(w.posts)), (True, False, 1))
ok("and is not marked slow", not w.ns["_IX_PACE"].is_slow("yts"))

# ── Test-all ──────────────────────────────────────────────────────────────────
w = world({"1337x": [(60.0, TIMEOUT), (48.1, "")], "eztv": [(22.7, "")], "yts": [(0.5, "")]})
health = run(w.ns["_run_indexer_tests"]())
eq("every indexer has a row, in Jackett's order", [h["id"] for h in health], ["1337x", "eztv", "yts"])
eq("none is failing", [h["ok"] for h in health], [True, True, True])
eq("rows carry Jackett's names", [h["name"] for h in health], ["1337X", "EZTV", "YTS"])
eq("the slow ones are marked slow", [bool(h.get("slow")) for h in health], [True, True, False])
ok("the retried one says so", health[0].get("retried") is True and "retried" not in health[2])
ok("the list became the health list", w.state.indexer_health is health)

# ── the warm-up ───────────────────────────────────────────────────────────────
w = world({"1337x": [(60.0, TIMEOUT), (48.1, "")], "yts": [(0.5, "")]})
t0 = w.clock[0]
run(w.ns["indexer_warmup"]())
eq("the warm-up tests every indexer", sorted(set(w.posts)), ["1337x", "yts"])
eq("and leaves a healthy list behind", [h["ok"] for h in w.state.indexer_health], [True, True])
ok("it waited for the services first", w.clock[0] - t0 >= ixwait.WARM_AFTER)
ok("the log says what it found",
   any("2 of 2 responding" in l and "1337x 48 s" in l for l in w.logs), repr(w.logs))

w = world({"1337x": [(13.0, "")]},
          health=[{"id": "1337x", "name": "1337x", "ok": True, "error": "", "results": 80}])
run(w.ns["indexer_warmup"]())
eq("a search got there first: no warm-up", w.posts, [])

w = world({"1337x": [(13.0, "")]}, solver_installed=True, solver_up=[False, False, True])
run(w.ns["indexer_warmup"]())
eq("it waits for FlareSolverr to be up", w.posts, ["1337x"])
ok("which took two more looks", w.clock[0] - 1000.0 >= ixwait.WARM_AFTER + 60)

w = world({"1337x": [(13.0, "")]}, list_fails=2)
run(w.ns["indexer_warmup"]())
eq("Jackett not ready yet: it tries again", w.posts, ["1337x"])

w = world({"1337x": [(13.0, "")]}, jackett_ok=False)
run(w.ns["indexer_warmup"]())
eq("Jackett never up: no test", w.posts, [])
ok("and it gives up, quietly", any("skipped" in l for l in w.logs)
   and w.clock[0] - 1000.0 < ixwait.WARM_AFTER + ixwait.WARM_GIVE_UP + 60, repr(w.logs))
eq("with no health list invented", w.state.indexer_health, [])

w = world({"1337x": [(60.0, TIMEOUT), (60.0, TIMEOUT)], "yts": [(0.5, "")]})
run(w.ns["indexer_warmup"]())
ok("a real failure is reported, not hidden",
   any("failing: 1337x" in l for l in w.logs), repr(w.logs))

print("indexer tests: %d passed, %d failed" % (_PASS, len(_FAIL)))
for f in _FAIL:
    print("  FAIL " + f)
sys.exit(1 if _FAIL else 0)
