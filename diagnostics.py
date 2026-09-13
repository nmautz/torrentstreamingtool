"""Runtime diagnostics: prove the server is reachable, and record why it wasn't.

Born out of the 2026-09-13 outage, where the dashboard and TV UI went dead for
~50 minutes and **the logs could not say why**. The process was demonstrably
healthy the whole time — a flat ~168 lines/min of VLC/qBittorrent polling, no
traceback, no stall — because everything that would have identified the fault
was either switched off or never collected:

* `run.py` starts both uvicorn servers with ``log_level="warning"``, which
  suppresses uvicorn's access log outright. **Nothing recorded inbound requests
  to port 80** — the port the dashboard and the TV UI actually use. The only
  inbound traffic visible at all was an accident of plumbing: the HTTPS proxy
  on :443 forwards to ``127.0.0.1:80`` with httpx, and httpx's own INFO logging
  captured those hops. Plain-HTTP clients left no trace whatsoever.
* Nothing ever checked, from *inside* the process, that the socket still
  answered. An accepting server and a wedged one look identical in a log that
  only contains outbound polls.
* When it mattered, no one had a snapshot of the event loop, the thread pool,
  or the library lock — the three things that can starve request handling while
  leaving the background pollers untouched.

This module is the answer to "improve the logging so we catch it next time". It
is deliberately a leaf: pure stdlib plus optional `psutil`, no imports from
`main`, so it cannot participate in an import cycle and can be unit-tested on
its own. `main.py` owns all the wiring; see `docs/DIAGNOSTICS.md`.

Design rules, learned from the outage:

1. **Separate files.** Access logging goes to `logs/access.log` and vitals to
   `logs/vitals.log`, never into the app log. Volume is exactly why the old
   logs were unreadable; burying a real event under poll chatter is how you get
   an undiagnosable incident.
2. **Anomalies escalate.** A normal vitals sample is written only to its own
   file. One that breaches a threshold is *also* logged at WARNING to the app
   log, so the onset of trouble appears in the file an operator opens first.
3. **Capture beats inference.** When the self-probe fails, dump every asyncio
   task stack and every thread stack to `logs/stall_<ts>.txt`. That artifact is
   what this outage lacked; nothing reconstructs it after the fact.
4. **Never take the server down to observe it.** Every probe is best-effort and
   swallows its own errors. Diagnostics must not become the outage.
"""

from __future__ import annotations

import asyncio
import faulthandler
import io
import logging
import os
import socket
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Callable, Optional

try:
    import psutil
except Exception:                                    # pragma: no cover
    psutil = None                                    # type: ignore[assignment]

log = logging.getLogger("streamlink.diag")

# ── Thresholds ───────────────────────────────────────────────────────────────
# Chosen to be quiet on a healthy box and loud at the first sign of the failure
# mode we couldn't diagnose. A breach never changes behaviour — it only raises
# the log level — so erring toward sensitive costs nothing but a WARNING line.
LOOP_LAG_WARN_S       = 1.0     # event loop this far behind schedule ⇒ something blocking
LOCK_HELD_WARN_S      = 5.0     # library lock held this long ⇒ every request that needs it is stuck
REQUEST_SLOW_S        = 5.0     # individual request taking this long ⇒ log it
REQUEST_STUCK_S       = 30.0    # in-flight this long ⇒ vitals anomaly
PROBE_TIMEOUT_S       = 5.0     # self-probe considered failed past this
PROBE_INTERVAL_S      = 30.0    # how often to prove we're still accepting
VITALS_INTERVAL_S     = 30.0    # how often to sample
THREADPOOL_WARN_FRAC  = 0.85    # default executor this saturated ⇒ to_thread calls are queuing
STALL_DUMP_COOLDOWN_S = 300.0   # min gap between stack dumps, so a long outage writes a few not thousands


# ── Shared mutable state ─────────────────────────────────────────────────────

@dataclass
class _LockProbe:
    """Instrumentation for a single asyncio.Lock (the library lock).

    `main.py` holds `_lib_lock` across `asyncio.to_thread(_load_lib_raw)`, so a
    saturated thread pool means the lock is held while waiting for a worker —
    which stalls every request that touches the library while leaving the VLC
    and qBittorrent pollers (pure async httpx, no lock) running at full cadence.
    That asymmetry is precisely the 2026-09-13 signature, and it is invisible
    unless someone is watching the lock itself.
    """
    name:        str
    acquired_at: float = 0.0     # monotonic; 0 ⇒ not held
    holder:      str   = ""      # best-effort task name of the current holder
    waiters:     int   = 0
    max_held_s:  float = 0.0
    total_waits: int   = 0

    def held_for(self) -> float:
        return 0.0 if not self.acquired_at else time.monotonic() - self.acquired_at


@dataclass
class DiagState:
    """Everything the vitals sampler reads. One instance, owned by this module."""
    started_at:      float = field(default_factory=time.monotonic)
    loop_lag_s:      float = 0.0
    loop_lag_max_s:  float = 0.0
    inflight:        dict  = field(default_factory=dict)   # id -> (started_monotonic, "METHOD /path")
    requests_total:  int   = 0
    requests_slow:   int   = 0
    probe_failures:  int    = 0
    probe_last_ok:   float = 0.0     # wall clock
    probe_last_ms:   float = 0.0
    last_stall_dump: float = 0.0     # monotonic
    locks:           dict  = field(default_factory=dict)   # name -> _LockProbe


state = DiagState()

_vitals_log: Optional[logging.Logger] = None
_log_dir:    Optional[Path] = None


# ── Setup ────────────────────────────────────────────────────────────────────

def _dedicated_logger(name: str, filename: str, log_dir: Path,
                      max_bytes: int, backups: int) -> logging.Logger:
    """A rotating file logger that does NOT propagate.

    No propagation is the point: access and vitals lines must never reach the
    app log. Mixing them back in would recreate the 168-lines/min haystack that
    made the original incident unreadable.
    """
    lg = logging.getLogger(name)
    lg.setLevel(logging.INFO)
    lg.propagate = False
    if lg.handlers:                       # idempotent across reloads
        return lg
    fh = RotatingFileHandler(log_dir / filename, maxBytes=max_bytes,
                             backupCount=backups, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(message)s",
                                      datefmt="%Y-%m-%d %H:%M:%S"))
    lg.addHandler(fh)
    return lg


def init(log_dir: Path) -> None:
    """Create the dedicated log files. Call once, early, before serving.

    Sized deliberately larger than the app log: an access log is only useful if
    it still covers the window someone asks about hours later. 8 MB x 5 is a
    few days of normal traffic and costs nothing on a media box.
    """
    global _vitals_log, _log_dir
    _log_dir = log_dir
    log_dir.mkdir(parents=True, exist_ok=True)
    # NOTE: access.log is owned exclusively by uvicorn's own dictConfig (see
    # `uvicorn_log_config`). Opening a second RotatingFileHandler on it here
    # would give one file two rotating handles, and on Windows — the primary
    # target — rotation renames the file while the other handle still holds it,
    # which fails with PermissionError and can lose the log outright. One file,
    # one handler.
    _vitals_log = _dedicated_logger("streamlink.vitals", "vitals.log", log_dir,
                                    max_bytes=4_000_000, backups=3)


def attach_access_log(log_dir: Path) -> None:
    """Attach the access/error file handlers to uvicorn's loggers directly.

    Belt and braces for `uvicorn_log_config`, and the thing that actually makes
    access logging reliable. The log config lives in the *launcher*
    (`daemon.py`'s generated `streamlink_service.py`, or `run.py`), and a
    launcher can easily be out of date — `streamlink_service.py` is only
    rewritten when the service wrapper is refreshed, which on 2026-09-13
    silently failed and left the box with no access log at all despite the
    update reporting success.

    `main.py` is never stale: uvicorn imports it fresh on every boot, and both
    the :80 app server and the :443 proxy share one process, so one logger
    covers both. Calling this from main.py means the single most important
    diagnostic cannot be switched off by an old launcher.

    Two things make it safe to run alongside `uvicorn_log_config`:

    * It skips a file that already has a handler, so the same log never ends up
      with two `RotatingFileHandler`s — on Windows the second one's rotation
      renames the file out from under the first and raises `PermissionError`.
    * It re-asserts INFO on the uvicorn loggers. uvicorn applies
      `log_level="warning"` to them when it configures logging, which is what
      suppressed access lines in the first place; this runs afterwards (uvicorn
      configures logging, *then* imports the app), so INFO wins.
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    for logger_name, filename, max_bytes, backups in (
        ("uvicorn.access", "access.log",  8_000_000, 5),
        ("uvicorn.error",  "uvicorn.log", 2_000_000, 3),
    ):
        lg = logging.getLogger(logger_name)
        target = str((log_dir / filename).resolve())
        already = any(
            getattr(h, "baseFilename", None)
            and os.path.normcase(h.baseFilename) == os.path.normcase(target)
            for h in lg.handlers
        )
        if not already:
            try:
                fh = RotatingFileHandler(log_dir / filename, maxBytes=max_bytes,
                                         backupCount=backups, encoding="utf-8")
                fh.setFormatter(logging.Formatter(
                    "%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
                lg.addHandler(fh)
            except OSError as exc:
                log.warning("Could not attach %s handler: %s", filename, exc)
                continue
        lg.setLevel(logging.INFO)
        lg.propagate = False


def quiet_noisy_loggers() -> None:
    """Silence httpx's per-request INFO chatter.

    Every VLC/qBittorrent/Jackett poll was logged at INFO, which is where the
    ~168 lines/min came from — the reason a real event couldn't be spotted
    without grep. WARNING keeps genuine client failures while dropping the
    "200 OK" flood. Inbound traffic is now recorded properly in access.log, so
    nothing is lost by muting the accidental proxy-hop visibility this provided.
    """
    for name in ("httpx", "httpcore", "urllib3"):
        logging.getLogger(name).setLevel(logging.WARNING)


def install_exception_hooks() -> None:
    """Route every last-resort exception into the app log.

    Previously an exception on a non-asyncio thread, or at interpreter exit,
    went to stderr — which on the service lands in a file nobody correlates.
    """
    prev_excepthook = sys.excepthook

    def _hook(exc_type, exc, tb):
        try:
            log.critical("UNCAUGHT EXCEPTION\n%s",
                         "".join(traceback.format_exception(exc_type, exc, tb)))
        except Exception:
            pass
        prev_excepthook(exc_type, exc, tb)

    sys.excepthook = _hook

    def _thread_hook(args):
        try:
            log.critical("UNCAUGHT THREAD EXCEPTION in %s\n%s",
                         getattr(args.thread, "name", "?"),
                         "".join(traceback.format_exception(
                             args.exc_type, args.exc_value, args.exc_traceback)))
        except Exception:
            pass

    threading.excepthook = _thread_hook


def install_loop_exception_handler(loop: asyncio.AbstractEventLoop) -> None:
    """Log asyncio's 'Exception was never retrieved' / callback errors properly.

    The 2026-09-13 log is full of `_ProactorBasePipeTransport._call_connection_lost`
    ConnectionResetError noise, which is benign Windows client-disconnect churn
    (WinError 10054) and was drowning the file at ERROR level. Demote exactly
    that case to DEBUG and log everything else loudly — the goal is that an
    ERROR in the app log means something.
    """
    def _handler(_loop, context):
        exc = context.get("exception")
        msg = context.get("message", "")
        if isinstance(exc, ConnectionResetError) or "connection_lost" in msg:
            log.debug("asyncio transport reset (benign): %s", msg)
            return
        try:
            log.error("asyncio loop exception: %s\n%s", msg,
                      "".join(traceback.format_exception(
                          type(exc), exc, exc.__traceback__)) if exc else "")
        except Exception:
            pass

    loop.set_exception_handler(_handler)


# ── Access logging ───────────────────────────────────────────────────────────

def uvicorn_log_config(log_dir: Path) -> dict:
    """A dictConfig for uvicorn that actually writes an access log.

    Replaces the `{"version": 1, "disable_existing_loggers": False}` stub in
    `run.py`, which configured no handlers at all and — combined with
    `log_level="warning"` — meant **zero** inbound request logging. Both the
    :80 app server and the :443 proxy use this, so every request is recorded
    regardless of scheme.
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "access":  {"format": "%(asctime)s %(message)s",
                        "datefmt": "%Y-%m-%d %H:%M:%S"},
            "default": {"format": "%(asctime)s %(levelname)s %(message)s",
                        "datefmt": "%Y-%m-%d %H:%M:%S"},
        },
        "handlers": {
            "access_file": {
                "class": "logging.handlers.RotatingFileHandler",
                "filename": str(log_dir / "access.log"),
                "maxBytes": 8_000_000, "backupCount": 5,
                "encoding": "utf-8", "formatter": "access",
            },
            # Its OWN file, deliberately not streamlink_app.log: main.py's
            # _init_logging() already holds a RotatingFileHandler on that one,
            # and two rotating handles on a single file break rotation on
            # Windows. This also keeps uvicorn's startup/bind failures — the
            # ones that decide whether the server is reachable at all —
            # somewhere obvious rather than mixed into the app log.
            "error_file": {
                "class": "logging.handlers.RotatingFileHandler",
                "filename": str(log_dir / "uvicorn.log"),
                "maxBytes": 2_000_000, "backupCount": 3,
                "encoding": "utf-8", "formatter": "default",
            },
        },
        "loggers": {
            # INFO is required here — uvicorn emits access lines at INFO, which
            # is exactly what `log_level="warning"` was suppressing.
            "uvicorn.access": {"handlers": ["access_file"], "level": "INFO",
                               "propagate": False},
            "uvicorn.error":  {"handlers": ["error_file"], "level": "INFO",
                               "propagate": False},
        },
    }


def note_request_start(req_id: int, method: str, path: str) -> None:
    state.inflight[req_id] = (time.monotonic(), f"{method} {path}")
    state.requests_total += 1


def note_request_end(req_id: int, status: int) -> None:
    """Close out a request, logging it if it was pathologically slow.

    A slow request is the earliest visible symptom of the starvation modes this
    module exists to catch, and it names the endpoint — which a vitals sample
    can't.
    """
    started, label = state.inflight.pop(req_id, (0.0, ""))
    if not started:
        return
    dur = time.monotonic() - started
    if dur >= REQUEST_SLOW_S:
        state.requests_slow += 1
        log.warning("SLOW REQUEST %.1fs status=%s %s", dur, status, label)


def oldest_inflight() -> tuple[float, str]:
    """Age and label of the longest-running in-flight request."""
    if not state.inflight:
        return 0.0, ""
    now = time.monotonic()
    started, label = min(state.inflight.values(), key=lambda v: v[0])
    return now - started, label


# ── Lock instrumentation ─────────────────────────────────────────────────────

def lock_probe(name: str) -> _LockProbe:
    return state.locks.setdefault(name, _LockProbe(name=name))


class InstrumentedLock:
    """Wraps an `asyncio.Lock` to record contention, holder and hold time.

    Drop-in for `async with lock:`. The overhead is two clock reads per
    acquisition — irrelevant next to the file I/O the library lock guards, and
    the only way to see the "lock held while the thread pool starves" failure
    that leaves background polling untouched.
    """

    def __init__(self, lock: asyncio.Lock, name: str) -> None:
        self._lock = lock
        self._probe = lock_probe(name)

    # Expose the underlying lock for anything that introspects it.
    @property
    def raw(self) -> asyncio.Lock:
        return self._lock

    def locked(self) -> bool:
        return self._lock.locked()

    async def __aenter__(self):
        p = self._probe
        p.waiters += 1
        p.total_waits += 1
        try:
            await self._lock.acquire()
        finally:
            p.waiters -= 1
        p.acquired_at = time.monotonic()
        try:
            task = asyncio.current_task()
            p.holder = task.get_name() if task else "?"
        except Exception:
            p.holder = "?"
        return self

    async def __aexit__(self, *exc) -> None:
        p = self._probe
        held = p.held_for()
        p.max_held_s = max(p.max_held_s, held)
        p.acquired_at = 0.0
        p.holder = ""
        self._lock.release()
        if held >= LOCK_HELD_WARN_S:
            log.warning("LOCK '%s' held %.1fs — every waiter was blocked that long",
                        p.name, held)


# ── Stack dumps ──────────────────────────────────────────────────────────────

def dump_stalled_state(reason: str) -> Optional[Path]:
    """Write every asyncio task stack + every thread stack to `logs/stall_<ts>.txt`.

    This is the artifact the 2026-09-13 incident lacked. A wedged server is
    trivially diagnosable *if* you know what each task was awaiting, and
    impossible to diagnose from poll logs alone. Rate-limited so a long outage
    produces a handful of dumps, not thousands.
    """
    now = time.monotonic()
    if now - state.last_stall_dump < STALL_DUMP_COOLDOWN_S:
        return None
    state.last_stall_dump = now
    if _log_dir is None:
        return None

    path = _log_dir / f"stall_{time.strftime('%Y%m%d-%H%M%S')}.txt"
    buf = io.StringIO()
    buf.write(f"=== STALL DUMP === {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
    buf.write(f"reason: {reason}\n")
    buf.write(f"{snapshot_line()}\n\n")

    buf.write("=== in-flight requests ===\n")
    now_m = time.monotonic()
    for started, label in sorted(state.inflight.values()):
        buf.write(f"  {now_m - started:8.1f}s  {label}\n")
    if not state.inflight:
        buf.write("  (none)\n")

    buf.write("\n=== locks ===\n")
    for p in state.locks.values():
        buf.write(f"  {p.name}: held={p.held_for():.1f}s holder={p.holder or '-'} "
                  f"waiters={p.waiters} max_held={p.max_held_s:.1f}s\n")

    buf.write("\n=== asyncio tasks ===\n")
    try:
        for t in asyncio.all_tasks():
            buf.write(f"\n--- task {t.get_name()} done={t.done()} ---\n")
            try:
                for frame in t.get_stack(limit=25):
                    buf.write("".join(traceback.format_stack(frame, limit=1)))
            except Exception as exc:
                buf.write(f"  <stack unavailable: {exc}>\n")
    except Exception as exc:
        buf.write(f"  <task enumeration failed: {exc}>\n")

    buf.write("\n=== threads ===\n")
    try:
        frames = sys._current_frames()
        for th in threading.enumerate():
            buf.write(f"\n--- thread {th.name} (id={th.ident}) ---\n")
            fr = frames.get(th.ident or -1)
            if fr is not None:
                buf.write("".join(traceback.format_stack(fr, limit=25)))
    except Exception as exc:
        buf.write(f"  <thread enumeration failed: {exc}>\n")

    try:
        path.write_text(buf.getvalue(), encoding="utf-8")
        log.critical("STALL DUMP written to %s (reason: %s)", path.name, reason)
        return path
    except OSError as exc:
        log.error("Could not write stall dump: %s", exc)
        return None


def enable_faulthandler(log_dir: Path) -> None:
    """Keep a native-level fault log, so a hard interpreter crash leaves a trace.

    A segfault or an abort in a C extension (ffmpeg bindings, psutil) kills the
    process with nothing in the Python logs at all — the "no traceback anywhere"
    case. `faulthandler` writes the C-level stack before the process dies.
    """
    try:
        fh = open(log_dir / "faulthandler.log", "a", encoding="utf-8")
        faulthandler.enable(file=fh, all_threads=True)
    except Exception as exc:                          # pragma: no cover
        log.debug("faulthandler unavailable: %s", exc)


# ── Vitals ───────────────────────────────────────────────────────────────────

def _threadpool_stats(loop: asyncio.AbstractEventLoop) -> tuple[int, int, int]:
    """(threads_spawned, max_workers, queued) for the loop's default executor.

    `main.py` makes 100+ `asyncio.to_thread` calls; the default executor caps at
    `min(32, cpu_count + 4)`. Saturating it doesn't stop the event loop — pure
    async pollers keep running — it stalls exactly the handlers that touch files
    or the library. That divergence is why the box looked healthy on
    2026-09-13 while serving nobody.

    `ThreadPoolExecutor` exposes no "busy" counter, and inventing one from the
    fields it does have is guesswork. What it does expose is honest and
    sufficient: it only spawns a worker when no idle one is available, so
    `threads == max_workers` together with a non-empty queue is a real
    saturation signal — work is waiting with nothing left to run it.
    """
    ex = getattr(loop, "_default_executor", None)
    if ex is None:
        return 0, 0, 0
    try:
        threads = len(getattr(ex, "_threads", ()) or ())
        maxw    = getattr(ex, "_max_workers", 0) or 0
        wq      = getattr(ex, "_work_queue", None)
        queued  = wq.qsize() if wq is not None else 0
        return threads, maxw, queued
    except Exception:
        return 0, 0, 0


def _proc_stats() -> dict:
    if psutil is None:
        return {}
    try:
        p = psutil.Process(os.getpid())
        with p.oneshot():
            out = {
                "rss_mb":  p.memory_info().rss / 1_048_576,
                "threads": p.num_threads(),
                "fds":     p.num_handles() if hasattr(p, "num_handles") else p.num_fds(),
            }
        try:
            out["conns"] = len(p.net_connections(kind="inet"))
        except Exception:
            out["conns"] = -1          # needs privileges on some platforms
        return out
    except Exception:
        return {}


def snapshot() -> dict:
    """One flat dict of everything worth knowing about process health."""
    loop = asyncio.get_event_loop()
    threads_n, maxw, queued = _threadpool_stats(loop)
    age, label = oldest_inflight()
    lib = state.locks.get("library")
    snap = {
        "uptime_s":    time.monotonic() - state.started_at,
        "loop_lag_s":  state.loop_lag_s,
        "tasks":       len(asyncio.all_tasks(loop)) if loop.is_running() else 0,
        "tp_threads":  threads_n,
        "tp_max":      maxw,
        "tp_queued":   queued,
        "inflight":    len(state.inflight),
        "oldest_req_s": age,
        "oldest_req":  label,
        "req_total":   state.requests_total,
        "req_slow":    state.requests_slow,
        "lock_held_s": lib.held_for() if lib else 0.0,
        "lock_waiters": lib.waiters if lib else 0,
        "lock_holder": lib.holder if lib else "",
        "probe_ms":    state.probe_last_ms,
        "probe_fails": state.probe_failures,
    }
    snap.update(_proc_stats())
    return snap


def snapshot_line() -> str:
    s = snapshot()
    return (f"up={s['uptime_s']:.0f}s lag={s['loop_lag_s']:.2f}s tasks={s['tasks']} "
            f"tp={s['tp_threads']}/{s['tp_max']}+q{s['tp_queued']} "
            f"inflight={s['inflight']} oldest={s['oldest_req_s']:.1f}s "
            f"lock_held={s['lock_held_s']:.1f}s waiters={s['lock_waiters']} "
            f"probe={s['probe_ms']:.0f}ms fails={s['probe_fails']} "
            f"rss={s.get('rss_mb', 0):.0f}MB thr={s.get('threads', 0)} "
            f"fds={s.get('fds', 0)} conns={s.get('conns', 0)}")


def _anomalies(s: dict) -> list[str]:
    """Which thresholds this sample breaches. Empty ⇒ healthy."""
    out = []
    if s["loop_lag_s"] >= LOOP_LAG_WARN_S:
        out.append(f"event loop {s['loop_lag_s']:.1f}s behind")
    if s["lock_held_s"] >= LOCK_HELD_WARN_S:
        out.append(f"library lock held {s['lock_held_s']:.1f}s by {s['lock_holder'] or '?'} "
                   f"({s['lock_waiters']} waiting)")
    if s["tp_max"] and s["tp_queued"] > 0             and s["tp_threads"] >= s["tp_max"] * THREADPOOL_WARN_FRAC:
        out.append(f"thread pool saturated {s['tp_threads']}/{s['tp_max']} "
                   f"with {s['tp_queued']} queued — to_thread calls are waiting")
    if s["oldest_req_s"] >= REQUEST_STUCK_S:
        out.append(f"request stuck {s['oldest_req_s']:.0f}s: {s['oldest_req']}")
    return out


async def _measure_loop_lag() -> None:
    """Continuously measure how late a 0.5s sleep actually returns.

    Lag is the single clearest indicator that something synchronous is blocking
    the loop. Cheap enough to run permanently.
    """
    while True:
        t0 = time.monotonic()
        await asyncio.sleep(0.5)
        lag = max(0.0, (time.monotonic() - t0) - 0.5)
        state.loop_lag_s = lag
        state.loop_lag_max_s = max(state.loop_lag_max_s, lag)


async def vitals_loop() -> None:
    """Sample health every `VITALS_INTERVAL_S`; escalate anomalies to the app log."""
    while True:
        await asyncio.sleep(VITALS_INTERVAL_S)
        try:
            s = snapshot()
            line = snapshot_line()
            if _vitals_log:
                _vitals_log.info(line)
            bad = _anomalies(s)
            if bad:
                log.warning("VITALS ANOMALY: %s | %s", "; ".join(bad), line)
                if s["loop_lag_s"] >= LOOP_LAG_WARN_S * 5 or \
                   s["oldest_req_s"] >= REQUEST_STUCK_S * 2:
                    dump_stalled_state("vitals anomaly: " + "; ".join(bad))
        except Exception as exc:
            log.debug("vitals sample failed: %s", exc)


# ── Self-probe ───────────────────────────────────────────────────────────────

async def _probe_once(host: str, port: int, path: str) -> tuple[bool, float, str]:
    """Raw HTTP GET over a plain socket, off the event loop.

    Deliberately *not* httpx: the point is to test the listening socket with as
    few of our own moving parts as possible. If this fails while the loop is
    otherwise fine, the server has stopped accepting — the exact condition that
    was invisible on 2026-09-13.
    """
    t0 = time.monotonic()

    def _blocking() -> tuple[bool, str]:
        try:
            with socket.create_connection((host, port), timeout=PROBE_TIMEOUT_S) as sk:
                sk.settimeout(PROBE_TIMEOUT_S)
                req = (f"GET {path} HTTP/1.1\r\nHost: {host}\r\n"
                       f"User-Agent: streamlink-selfprobe\r\nConnection: close\r\n\r\n")
                sk.sendall(req.encode("ascii"))
                data = sk.recv(256)
            if not data:
                return False, "empty response"
            first = data.split(b"\r\n", 1)[0].decode("latin-1", "replace")
            return (b" 200 " in data or b" 3" in data[:16]), first
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"

    try:
        ok, detail = await asyncio.wait_for(
            asyncio.to_thread(_blocking), timeout=PROBE_TIMEOUT_S * 2)
    except asyncio.TimeoutError:
        # Timing out here is itself diagnostic: either the socket is wedged or
        # the thread pool is so saturated the probe never even started.
        ok, detail = False, f"probe timed out after {PROBE_TIMEOUT_S * 2:.0f}s"
    except Exception as exc:
        ok, detail = False, f"{type(exc).__name__}: {exc}"
    return ok, (time.monotonic() - t0) * 1000.0, detail


async def self_probe_loop(host: str = "127.0.0.1", port: int = 80,
                          path: str = "/healthz") -> None:
    """Prove, from inside the process, that the server still answers.

    This is the check whose absence made the 2026-09-13 outage undiagnosable: a
    server that has stopped accepting connections looks exactly like a healthy
    one in a log full of outbound polls. Two consecutive failures trigger a
    stall dump, so the evidence is captured while the fault is live rather than
    reconstructed afterwards.
    """
    consecutive = 0
    while True:
        await asyncio.sleep(PROBE_INTERVAL_S)
        try:
            ok, ms, detail = await _probe_once(host, port, path)
            state.probe_last_ms = ms
            if ok:
                if consecutive:
                    log.warning("SELF-PROBE recovered after %d failure(s) (%.0fms)",
                                consecutive, ms)
                consecutive = 0
                state.probe_last_ok = time.time()
                continue

            consecutive += 1
            state.probe_failures += 1
            log.critical("SELF-PROBE FAILED (%d in a row) on %s:%s%s — %s | %s",
                         consecutive, host, port, path, detail, snapshot_line())
            if consecutive >= 2:
                dump_stalled_state(f"self-probe failed {consecutive}x: {detail}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.debug("self-probe loop error: %s", exc)
