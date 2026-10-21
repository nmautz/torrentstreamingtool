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
import contextvars
import faulthandler
import gc
import io
import logging
import os
import re
import socket
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Optional

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
LOOP_BLOCK_SAMPLE_S   = 0.15    # loop heartbeat this stale ⇒ start sampling the loop thread's stack
                                # (2.0 until 20.25.18, 0.5 until 20.25.20: the freeze standard
                                # is 250 ms, and every block it counts must come with a name)
CHILD_LOG_CAP         = 5 * 1024 * 1024   # a helper process's own log is rolled past this
# Who else is using the disk (#96). The box's library saves went from milliseconds to
# 5-17 s while F: was busy, and nothing in our own logs could name the other user.
DISK_SLOW_MS          = 250.0   # a drive averaging this per I/O over a sample is the bottleneck (an idle HDD: 5-20 ms)
DISK_SLOW_MIN_OPS     = 5       # fewer I/Os than this in a sample is too few to call a drive slow
IO_TOP_N              = 3       # processes named per sample
IO_TOP_MIN_BPS        = 1_048_576   # a process moving less than 1 MB/s is not worth naming
DISK_SAMPLE_TIMEOUT_S = 5.0     # the sampler is skipped for this vitals line past this
DRIVE_MAP_TTL_S       = 600.0   # how long the PhysicalDriveN -> letter map is trusted
# Requests that wait on Jackett by design: a Test sits behind a Cloudflare check
# (60 s a try, tried twice, and Jackett runs them one at a time). Not "stuck"
# until they have had this long. On 2026-10-11 a Test-all that was simply
# waiting wrote a CRITICAL stall dump for a server that was idle.
PATIENT_S             = 300.0
_PATIENT_PREFIX       = "POST /api/admin/indexers/"
LOOP_BLOCK_POLL_S     = 0.05    # how often the heartbeat is checked, and the sampling interval
LOOP_LAG_TICK_S       = 0.05    # how often the loop proves it is free; a block's lag is
                                # measured to within this (0.5 until 20.25.18, 0.1 until 20.25.20)
GC_PAUSE_LOG_S        = 0.1     # a garbage collection this long is logged (GC PAUSE): it holds the
                                # GIL, so it stops the loop whichever thread it runs on (20.25.21)
LOOP_BLOCK_ONGOING_S  = 15.0    # still blocked this long ⇒ log what it is doing NOW, in case it never returns


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
    loop_lag_peak_s: float = 0.0     # worst lag since the last vitals line (`lagmax=`)
    inflight:        dict  = field(default_factory=dict)   # id -> (started_monotonic, "METHOD /path")
    requests_total:  int   = 0
    requests_slow:   int   = 0
    probe_failures:  int    = 0
    probe_last_ok:   float = 0.0     # wall clock
    probe_last_ms:   float = 0.0
    last_stall_dump: float = 0.0     # monotonic
    locks:           dict  = field(default_factory=dict)   # name -> _LockProbe
    loop_beat:       float = 0.0     # monotonic; set by _measure_loop_lag every turn
    loop_thread_id:  int   = 0
    last_block:      str   = ""      # the most recent LOOP BLOCKED report, for the stall dump
    disk_last:       dict  = field(default_factory=dict)   # the latest disk_sample(), merged into snapshot()
    block_lag_s:     float = 0.0     # worst lag since the watchdog's last report: a block's true length
    gc_peak_s:       float = 0.0     # longest collection since the last vitals line (`gcmax=`)
    gc_peak_gen:     int   = -1
    gc_slow:         list  = field(default_factory=list)   # (wall, secs, gen, collected, thread id) >= GC_PAUSE_LOG_S


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
    acc = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, _AccessTagFilter) for f in acc.filters):
        acc.addFilter(_AccessTagFilter())


# ── Device id on access lines ────────────────────────────────────────────────
# Every client reaches the box from its own LAN address once the box is its own
# Tailscale subnet router, so the peer column of access.log names nobody (85,246
# requests from 192.168.0.106 and no other address, 2026-10-04). The request
# middleware sets this to the client's device id; uvicorn writes the access line
# from inside the same request task (it gives each request a fresh context), so
# the filter below can read it back and append ` dev=<first 8>`.
_access_tag: contextvars.ContextVar[str] = contextvars.ContextVar("access_tag", default="")


def set_access_tag(device_id: str) -> None:
    """Name the device the current request came from. Empty = say nothing."""
    if device_id:
        _access_tag.set(device_id[:8])


_DEVICE_ID_OK = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_DEVICE_COOKIE = b"streamlink_device_id="


def scope_device_id(scope: dict) -> str:
    """The device id an ASGI request names: `X-Device-Id`, else the
    `streamlink_device_id` cookie (hls.js, EventSource and <img> send only the
    cookie). '' when absent or malformed."""
    did = b""
    cookie = b""
    for k, v in scope.get("headers") or ():
        if k == b"x-device-id":
            did = v.strip()
        elif k == b"cookie":
            cookie = v
    if not did and _DEVICE_COOKIE in cookie:
        for part in cookie.split(b";"):
            part = part.strip()
            if part.startswith(_DEVICE_COOKIE):
                did = part[len(_DEVICE_COOKIE):].strip()
                break
    try:
        s = did.decode("ascii")
    except UnicodeDecodeError:
        return ""
    return s if _DEVICE_ID_OK.match(s) else ""


class AccessTagMiddleware:
    """Pure ASGI, and it must be the OUTERMOST middleware (`add_middleware` last).

    Setting the tag from an `@app.middleware("http")` function does not work: each
    of those (Starlette's `BaseHTTPMiddleware`) runs the app inside it in a child
    task with a COPY of the context, so a value set inside never reaches the
    outermost task, which is the one that hands the response to uvicorn and so
    writes the access line. 20.25.0 set it from `diag_track_requests`, the
    innermost of four, and the box logged no `dev=` at all. A pure ASGI layer
    runs in uvicorn's own request task."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") == "http":
            try:
                set_access_tag(scope_device_id(scope))
            except Exception:
                pass
        await self.app(scope, receive, send)


class _AccessTagFilter(logging.Filter):
    """Appends ` dev=<id>` to a uvicorn access line. A filter on the LOGGER, not a
    formatter on a handler, so it works whichever launcher configured the file."""

    def filter(self, record: logging.LogRecord) -> bool:
        tag = _access_tag.get()
        if tag:
            try:
                record.msg = str(record.msg) + " dev=" + tag.replace("%", "")
            except Exception:
                pass
        return True


# ── Disk facts for "what is this file, really?" ──────────────────────────────

def allocated_bytes(path: Path) -> Optional[int]:
    """Bytes the file actually occupies on disk, or None when it can't be told.

    qBittorrent writes sparse files: a file it has never fetched a byte of can
    still report its full length, and only the allocation says it is empty.
    Windows: `GetCompressedFileSizeW` (it answers for sparse files too). POSIX:
    `st_blocks`. Never raises."""
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            fn = k32.GetCompressedFileSizeW
            fn.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
            fn.restype = wintypes.DWORD
            high = wintypes.DWORD(0)
            low = fn(str(path), ctypes.byref(high))
            if low == 0xFFFFFFFF and ctypes.get_last_error() != 0:
                return None
            return (high.value << 32) + low
        blocks = getattr(os.stat(path), "st_blocks", None)
        return None if blocks is None else int(blocks) * 512
    except Exception:
        return None


def file_holders(paths: list, limit: int = 8) -> list[str]:
    """Which processes have these files open, as `name pid=N [svc=X]` strings.

    Windows only (the Restart Manager, the API Explorer's "file in use" dialog
    uses); [] elsewhere, or when nothing holds them. A lock that makes
    `os.replace` fail usually clears within milliseconds, so ask straight after
    the failure. Never raises: an error comes back as one `? (...)` entry."""
    if os.name != "nt" or not paths:
        return []
    try:
        import ctypes
        from ctypes import wintypes

        class _UniqueProcess(ctypes.Structure):
            _fields_ = [("dwProcessId", wintypes.DWORD),
                        ("ProcessStartTime", wintypes.FILETIME)]

        class _ProcessInfo(ctypes.Structure):
            _fields_ = [("Process", _UniqueProcess),
                        ("strAppName", wintypes.WCHAR * 256),          # CCH_RM_MAX_APP_NAME + 1
                        ("strServiceShortName", wintypes.WCHAR * 64),  # CCH_RM_MAX_SVC_NAME + 1
                        ("ApplicationType", ctypes.c_int),
                        ("AppStatus", wintypes.ULONG),
                        ("TSSessionId", wintypes.DWORD),
                        ("bRestartable", wintypes.BOOL)]

        rm = ctypes.WinDLL("rstrtmgr")
        rm.RmStartSession.argtypes = [ctypes.POINTER(wintypes.DWORD), wintypes.DWORD,
                                      wintypes.LPWSTR]
        rm.RmRegisterResources.argtypes = [wintypes.DWORD, wintypes.UINT,
                                           ctypes.POINTER(wintypes.LPCWSTR), wintypes.UINT,
                                           ctypes.c_void_p, wintypes.UINT, ctypes.c_void_p]
        rm.RmGetList.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.UINT),
                                 ctypes.POINTER(wintypes.UINT), ctypes.POINTER(_ProcessInfo),
                                 ctypes.POINTER(wintypes.DWORD)]
        rm.RmEndSession.argtypes = [wintypes.DWORD]
        for f in (rm.RmStartSession, rm.RmRegisterResources, rm.RmGetList, rm.RmEndSession):
            f.restype = wintypes.DWORD

        session = wintypes.DWORD(0)
        key = ctypes.create_unicode_buffer(33)                         # CCH_RM_SESSION_KEY + 1
        if rm.RmStartSession(ctypes.byref(session), 0, key) != 0:
            return []
        try:
            names = (wintypes.LPCWSTR * len(paths))(*[str(p) for p in paths])
            if rm.RmRegisterResources(session, len(paths), names, 0, None, 0, None) != 0:
                return []
            infos = (_ProcessInfo * limit)()
            needed, count, reasons = wintypes.UINT(0), wintypes.UINT(limit), wintypes.DWORD(0)
            rc = rm.RmGetList(session, ctypes.byref(needed), ctypes.byref(count),
                              infos, ctypes.byref(reasons))
            if rc == 234:                                             # ERROR_MORE_DATA
                return ["%d processes (more than %d)" % (needed.value, limit)]
            if rc != 0:
                return ["? (RmGetList %d)" % rc]
            out = []
            for i in range(min(count.value, limit)):
                p = infos[i]
                s = "%s pid=%d" % (p.strAppName or "?", p.Process.dwProcessId)
                if p.strServiceShortName:
                    s += " svc=" + p.strServiceShortName
                out.append(s)
            return out
        finally:
            rm.RmEndSession(session)
    except Exception as exc:
        return ["? (%s)" % exc]


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


def _is_patient(label: str) -> bool:
    return label.startswith(_PATIENT_PREFIX) and (
        label.endswith("/test") or label.endswith("/test-all"))


def oldest_inflight() -> tuple[float, str]:
    """Age and label of the longest-running in-flight request. A request that
    waits on Jackett by design is not counted until it is past `PATIENT_S`."""
    now = time.monotonic()
    live = [(started, label) for started, label in state.inflight.values()
            if not (_is_patient(label) and now - started < PATIENT_S)]
    if not live:
        return 0.0, ""
    started, label = min(live, key=lambda v: v[0])
    return now - started, label


def child_log(log_dir: Path, name: str, cap: int = CHILD_LOG_CAP) -> str:
    """Where a helper process we launch keeps its own log (`logs/<name>.log`).

    Call it just before the launch: that is the one moment nothing holds the
    file, and on Windows a file a live process has open cannot be renamed. Over
    `cap` it is rolled to `<name>.log.1`. Never raises; a log that can't be
    rolled keeps growing until the next update archives it."""
    path = Path(log_dir) / (name + ".log")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.stat().st_size > cap:
            os.replace(path, path.with_name(path.name + ".1"))
    except OSError:
        pass
    return str(path)


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

    def set_holder(self, label: str) -> None:
        """Name the current holder and what it is doing (`update_progress [save]`).

        The task name alone is starlette's `call_next.<locals>.coro` for every
        request, which named nobody in the 2026-10-18 lock holds (#96). The label
        shows in the LOCK line, the vitals anomaly and the stall dump. A no-op
        when the lock is not held.
        """
        if self._probe.acquired_at:
            self._probe.holder = label

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
        holder = p.holder
        p.max_held_s = max(p.max_held_s, held)
        p.acquired_at = 0.0
        p.holder = ""
        self._lock.release()
        if held >= LOCK_HELD_WARN_S:
            log.warning("LOCK '%s' held %.1fs by %s — every waiter was blocked that long",
                        p.name, held, holder or "?")


# ── Stack dumps ──────────────────────────────────────────────────────────────

def await_chain(task: "asyncio.Task", limit: int = 40) -> tuple[list, Any]:
    """Every frame of a task's await chain, outermost first, and what the last one awaits.

    `Task.get_stack()` gives a suspended coroutine ONE frame (a suspended
    coroutine's frame has no `f_back`), so a request parked inside
    `mutate_library` read as starlette's `coro` and nothing more (#96). This
    follows `cr_await` / `gi_yieldfrom` / `ag_await` down instead. The leaf has
    no frame: usually a Future's iterator (the last frame says which sleep, lock
    or `to_thread`). An `async with` on an
    `@asynccontextmanager` ends the chain at its `async_generator_asend`, which
    exposes no frame; `InstrumentedLock.set_holder` covers the library's one.
    """
    frames: list = []
    obj: Any = task.get_coro()
    while obj is not None and len(frames) < limit:
        f = (getattr(obj, "cr_frame", None) or getattr(obj, "gi_frame", None)
             or getattr(obj, "ag_frame", None))
        if f is None:
            break
        frames.append(f)
        nxt = getattr(obj, "cr_await", None)
        if nxt is None:
            nxt = getattr(obj, "gi_yieldfrom", None)
        if nxt is None:
            nxt = getattr(obj, "ag_await", None)
        obj = nxt
    return frames, obj



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

    if state.last_block:
        buf.write("\n=== last loop block (sampled while it was happening) ===\n")
        buf.write(state.last_block + "\n")

    buf.write("\n=== asyncio tasks ===\n")
    try:
        for t in asyncio.all_tasks():
            buf.write(f"\n--- task {t.get_name()} done={t.done()} ---\n")
            try:
                frames, leaf = await_chain(t)
                for frame in frames:
                    buf.write("".join(traceback.format_stack(frame, limit=1)))
                if leaf is not None:
                    buf.write(f"  waiting on: {type(leaf).__name__}\n")
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


# ── Disk: who else is using it ───────────────────────────────────────────────

_disk_prev: dict = {}                       # last raw counters, for the next delta
_disk_busy = threading.Lock()               # one sample at a time, even after a timeout
_drive_map: dict = {"t": None, "map": {}}
# psutil's read_time / write_time are milliseconds everywhere EXCEPT Windows,
# where psutil/arch/windows/disk.c divides the 100 ns DISK_PERFORMANCE counters
# by 10,000,000 under a "convert to ms" comment: whole SECONDS (seen on the box,
# 20.25.4: a hard disk at "0.0 ms" per I/O). If psutil ever fixes it, every drive
# reads 1000x slow and the anomaly fires on an idle box: loud, not silent.
_DISK_TIME_UNIT_MS = 1000.0 if sys.platform == "win32" else 1.0  # t: monotonic of the last lookup, None = never
_disk_pool = None                           # a private one-thread executor (see disk_sample_async)


def _windows_drive_map() -> dict:
    """`PhysicalDrive1` -> `F:` (`C:+D:` when one disk holds several volumes).

    psutil names disks by physical number on Windows; the volume's letter is what
    a person knows. IOCTL_STORAGE_GET_DEVICE_NUMBER on the volume (`F:`) answers it with
    no admin rights. A volume that spans disks (dynamic/Storage Spaces) has no one
    number: it is left out and its disk keeps the physical name.
    """
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                                wintypes.HANDLE]
    k32.DeviceIoControl.restype = wintypes.BOOL
    k32.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID,
                                    wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
                                    ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]

    class _DeviceNumber(ctypes.Structure):
        _fields_ = [("DeviceType", wintypes.DWORD), ("DeviceNumber", wintypes.ULONG),
                    ("PartitionNumber", wintypes.ULONG)]

    invalid = wintypes.HANDLE(-1).value
    found: dict = {}
    for part in psutil.disk_partitions(all=False):
        if "cdrom" in (part.opts or "") or not part.fstype:
            continue
        letter = part.device.rstrip("\\/")            # 'F:'
        # 0 access + share read/write: enough for this IOCTL, and never blocks a writer.
        h = k32.CreateFileW("\\\\.\\" + letter, 0, 0x1 | 0x2, None, 3, 0, None)
        if not h or h == invalid:
            continue
        try:
            dn, got = _DeviceNumber(), wintypes.DWORD(0)
            if k32.DeviceIoControl(h, 0x2D1080, None, 0, ctypes.byref(dn),
                                   ctypes.sizeof(dn), ctypes.byref(got), None):
                found.setdefault(f"PhysicalDrive{dn.DeviceNumber}", []).append(letter)
        finally:
            k32.CloseHandle(h)
    return {k: "+".join(v) for k, v in found.items()}


def _drive_names() -> dict:
    # `t` starts as None, not 0: Windows' monotonic clock counts from boot, so a
    # 0 start meant no lookup for the first 10 minutes after a reboot (20.25.3).
    now = time.monotonic()
    last = _drive_map["t"]
    if last is None or now - last >= DRIVE_MAP_TTL_S:
        _drive_map["t"] = now
        old = _drive_map["map"]
        try:
            new = _windows_drive_map() if sys.platform == "win32" else {}
        except Exception as exc:
            log.warning("disk sampler: drive letters unavailable (%s: %s); "
                        "showing physical names", type(exc).__name__, exc)
            new = {}
        if sys.platform == "win32" and (last is None or new != old):
            log.info("disk sampler: drives %s", ", ".join(
                f"{k}={v}" for k, v in sorted(new.items())) or "(no letter found)")
        _drive_map["map"] = new
    return _drive_map["map"]


def _skip_disk(name: str) -> bool:
    # Linux lists partitions, loop and RAM devices next to the disks.
    return name.startswith(("loop", "ram", "zram")) or (
        sys.platform.startswith("linux") and name[-1:].isdigit()
        and not name.startswith("nvme") and not name.startswith("mmcblk"))


def disk_sample() -> dict:
    """Each drive's average time per I/O and MB/s since the last call, and the
    processes that moved the most bytes. Blocking (it walks every process): run it
    off the loop, see `disk_sample_async`.

    The first call only primes the counters and returns {}.

    Per-process bytes come from `io_counters()`. On Windows they count ALL reads and
    writes the process issued, cached ones and network ones included, so they name
    who is busy, not exact disk traffic. A process we may not query (another
    account's service, a protected one) is counted in `io_denied`, so a top list
    that is blind to the culprit says so.
    """
    if psutil is None or not _disk_busy.acquire(blocking=False):
        return {}
    try:
        now = time.monotonic()
        try:
            disks = psutil.disk_io_counters(perdisk=True) or {}
        except Exception:
            disks = {}
        procs: dict = {}
        denied = 0
        for p in psutil.process_iter(["name"]):
            try:
                io = p.io_counters()
            except psutil.AccessDenied:
                denied += 1
                continue
            except Exception:                       # gone, zombie, unsupported
                continue
            procs[p.pid] = ((p.info.get("name") or "?"), io.read_bytes, io.write_bytes)

        out: dict = {}
        t0 = _disk_prev.get("t")
        dt = (now - t0) if t0 else 0.0
        if dt > 0:
            names = _drive_names()
            rows = []
            for k, c in disks.items():
                o = _disk_prev["disks"].get(k)
                if o is None or _skip_disk(k):
                    continue
                ops = max(0, (c.read_count - o.read_count) + (c.write_count - o.write_count))
                if not ops:
                    continue
                ticks = max(0, (c.read_time - o.read_time) + (c.write_time - o.write_time))
                mbs = max(0, (c.read_bytes - o.read_bytes) + (c.write_bytes - o.write_bytes)) / dt / 1_048_576
                row = {"disk": names.get(k, k), "ms": ticks * _DISK_TIME_UNIT_MS / ops,
                       "mbs": mbs, "ops": ops}
                if not ticks and _DISK_TIME_UNIT_MS > 1.0:
                    # Under one tick (a whole second on Windows) across all its
                    # I/Os: all we know is the average is below tick / ops.
                    row["ms"], row["under"] = _DISK_TIME_UNIT_MS / ops, True
                rows.append(row)
            rows.sort(key=lambda r: r["ms"], reverse=True)
            top = []
            for pid, (name, r, w) in procs.items():
                o = _disk_prev["procs"].get(pid)
                if not o or o[0] != name:
                    continue
                dr, dw = max(0, r - o[1]) / dt, max(0, w - o[2]) / dt
                if dr + dw >= IO_TOP_MIN_BPS:
                    top.append((dr + dw, name, dr, dw))
            top.sort(reverse=True)
            out = {"disks": rows[:4],
                   "io_top": [{"name": n, "r_mbs": dr / 1_048_576, "w_mbs": dw / 1_048_576}
                              for _, n, dr, dw in top[:IO_TOP_N]],
                   "io_denied": denied}
        _disk_prev.update(t=now, disks=disks, procs=procs)
        state.disk_last = out
        return out
    finally:
        _disk_busy.release()


async def disk_sample_async() -> None:
    """`disk_sample()` on its own thread, bounded by `DISK_SAMPLE_TIMEOUT_S`.

    Its own thread, not `to_thread`: a saturated default pool is one of the
    things vitals exists to report, and must not also silence this. On a timeout
    the previous sample is dropped rather than shown as current.
    """
    global _disk_pool
    if psutil is None:
        return
    if _disk_pool is None:
        from concurrent.futures import ThreadPoolExecutor
        _disk_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="diag-disk")
    loop = asyncio.get_running_loop()
    try:
        await asyncio.wait_for(loop.run_in_executor(_disk_pool, disk_sample),
                               DISK_SAMPLE_TIMEOUT_S)
    except asyncio.TimeoutError:
        state.disk_last = {"error": "timeout"}


def _ms(v: float) -> str:
    """`0.4` under 10 ms (an SSD), `850` above it."""
    return f"{v:.1f}" if v < 10 else f"{v:.0f}"


def _disk_fields(s: dict) -> str:
    """` dsk=F:850ms/22.0MBps,C:4ms/0.1MBps io=MsMpEng.exe:85.0r+0.0w io_denied=12`."""
    if s.get("disk_error"):
        return f" dsk={s['disk_error']}"
    if "disks" not in s:
        return ""
    # A letter already ends in ':' (`F:850ms`); a physical name gets one (`PhysicalDrive0:4ms`).
    dsk = ",".join(f"{d['disk'].rstrip(':')}:{'<' if d.get('under') else ''}"
                   f"{_ms(d['ms'])}ms/{d['mbs']:.1f}MBps" for d in s["disks"]) or "-"
    io = ",".join(f"{p['name']}:{p['r_mbs']:.1f}r+{p['w_mbs']:.1f}w" for p in s["io_top"]) or "-"
    return f" dsk={dsk} io={io} io_denied={s.get('io_denied', 0)}"


def snapshot() -> dict:
    """One flat dict of everything worth knowing about process health."""
    loop = asyncio.get_event_loop()
    threads_n, maxw, queued = _threadpool_stats(loop)
    age, label = oldest_inflight()
    lib = state.locks.get("library")
    snap = {
        "uptime_s":    time.monotonic() - state.started_at,
        "loop_lag_s":  state.loop_lag_s,
        "loop_lag_peak_s": state.loop_lag_peak_s,
        "gc_peak_s":   state.gc_peak_s,
        "gc_peak_gen": state.gc_peak_gen,
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
    dk = state.disk_last
    if dk.get("error"):
        snap["disk_error"] = dk["error"]
    elif dk:
        snap.update(dk)
    return snap


def snapshot_line(s: Optional[dict] = None) -> str:
    s = s if s is not None else snapshot()
    return (f"up={s['uptime_s']:.0f}s lag={s['loop_lag_s']:.2f}s "
            f"lagmax={s.get('loop_lag_peak_s', 0.0):.2f}s "
            f"gcmax={s.get('gc_peak_s', 0.0):.2f}s/g{max(s.get('gc_peak_gen', -1), 0)} "
            f"tasks={s['tasks']} "
            f"tp={s['tp_threads']}/{s['tp_max']}+q{s['tp_queued']} "
            f"inflight={s['inflight']} oldest={s['oldest_req_s']:.1f}s "
            f"lock_held={s['lock_held_s']:.1f}s waiters={s['lock_waiters']} "
            f"probe={s['probe_ms']:.0f}ms fails={s['probe_fails']} "
            f"rss={s.get('rss_mb', 0):.0f}MB thr={s.get('threads', 0)} "
            f"fds={s.get('fds', 0)} conns={s.get('conns', 0)}"
            + _disk_fields(s))


def _anomalies(s: dict) -> list[str]:
    """Which thresholds this sample breaches. Empty ⇒ healthy."""
    out = []
    # The worst turn since the last line, not this instant's: `lag` alone is one
    # tick in 300, so a 1-3 s freeze between samples passed without a word.
    worst = max(s["loop_lag_s"], s.get("loop_lag_peak_s", 0.0))
    if worst >= LOOP_LAG_WARN_S:
        out.append(f"event loop {worst:.1f}s behind")
    if s["lock_held_s"] >= LOCK_HELD_WARN_S:
        out.append(f"library lock held {s['lock_held_s']:.1f}s by {s['lock_holder'] or '?'} "
                   f"({s['lock_waiters']} waiting)")
    if s["tp_max"] and s["tp_queued"] > 0             and s["tp_threads"] >= s["tp_max"] * THREADPOOL_WARN_FRAC:
        out.append(f"thread pool saturated {s['tp_threads']}/{s['tp_max']} "
                   f"with {s['tp_queued']} queued — to_thread calls are waiting")
    if s["oldest_req_s"] >= REQUEST_STUCK_S:
        out.append(f"request stuck {s['oldest_req_s']:.0f}s: {s['oldest_req']}")
    slow = [d for d in s.get("disks", ())
            if d["ms"] >= DISK_SLOW_MS and d["ops"] >= DISK_SLOW_MIN_OPS and not d.get("under")]
    if slow:
        busiest = ", ".join(f"{p['name']} {p['r_mbs']:.1f} MB/s read + {p['w_mbs']:.1f} write"
                            for p in s.get("io_top", ())) or "no process over 1 MB/s"
        blind = f" ({s['io_denied']} processes unreadable)" if s.get("io_denied") else ""
        out.append("disk " + ", ".join(f"{d['disk']} {d['ms']:.0f} ms per I/O ({d['mbs']:.1f} MB/s)"
                                       for d in slow)
                   + f"; busiest: {busiest}{blind}")
    return out


async def _measure_loop_lag() -> None:
    """Continuously measure how late a `LOOP_LAG_TICK_S` sleep actually returns.

    Lag is the single clearest indicator that something synchronous is blocking
    the loop. Cheap enough to run permanently.
    """
    state.loop_thread_id = threading.get_ident()
    state.loop_beat = time.monotonic()
    start_loop_watchdog()
    start_gc_watch()
    while True:
        t0 = time.monotonic()
        await asyncio.sleep(LOOP_LAG_TICK_S)
        state.loop_beat = time.monotonic()
        lag = max(0.0, (state.loop_beat - t0) - LOOP_LAG_TICK_S)
        state.loop_lag_s = lag
        state.loop_lag_max_s = max(state.loop_lag_max_s, lag)
        state.loop_lag_peak_s = max(state.loop_lag_peak_s, lag)
        state.block_lag_s = max(state.block_lag_s, lag)


# ── Loop-block sampler ───────────────────────────────────────────────────────
#
# The stall dump is written BY the event loop, so it can only run once the loop
# is free again — by which point whatever blocked it has returned and its frame
# is gone. Every dump from the 18-35 s freezes of September 2026 shows the
# aftermath and never the culprit. This thread watches the heartbeat
# `_measure_loop_lag` leaves and, while it is stale, samples the loop thread's
# own stack from outside. Stdlib only: `sys._current_frames()`.
#
# One limit: a C call that holds the GIL for the whole block stops this thread
# too. The samples then land only after it releases, and name what ran next.

_watchdog_started = False
_gc_starts: dict = {}


def _gc_callback(phase: str, info: dict) -> None:
    """Time every collection. A full (gen 2) pass walks every live container,
    and with a few parsed copies of a 7.6 MB library alive that is millions: it
    runs on whichever thread happened to allocate, holding the GIL throughout,
    so the loop stops and the sampler names whatever innocent line it was on."""
    tid = threading.get_ident()
    if phase == "start":
        _gc_starts[tid] = time.perf_counter()
        return
    t0 = _gc_starts.pop(tid, None)
    if t0 is None:
        return
    d = time.perf_counter() - t0
    gen = int(info.get("generation", -1))
    if d > state.gc_peak_s:
        state.gc_peak_s, state.gc_peak_gen = d, gen
    if d >= GC_PAUSE_LOG_S and len(state.gc_slow) < 200:
        state.gc_slow.append((time.time(), d, gen, int(info.get("collected", 0)), tid))


def start_gc_watch() -> None:
    if _gc_callback not in gc.callbacks:
        gc.callbacks.append(_gc_callback)


def drain_gc_pauses() -> list[str]:
    """The slow collections since the last call, as log lines (pure formatting)."""
    rows, state.gc_slow = state.gc_slow, []
    names = {th.ident: th.name for th in threading.enumerate()}
    out = []
    for wall, d, gen, collected, tid in rows:
        who = "event loop" if tid == state.loop_thread_id else names.get(tid, f"thread {tid}")
        out.append(f"GC PAUSE {d:.2f}s gen{gen} at {time.strftime('%H:%M:%S', time.localtime(wall))} "
                   f"on {who}: {collected} collected")
    return out
_IDLE_FRAMES = ("select", "_poll", "GetQueuedCompletionStatus", "poll",
                "wait", "acquire", "get")


def start_loop_watchdog() -> None:
    global _watchdog_started
    if _watchdog_started:
        return
    _watchdog_started = True
    threading.Thread(target=_loop_watchdog, name="diag-loop-watchdog",
                     daemon=True).start()


def _stack_key(frame: Any, limit: int = 18) -> str:
    """One sampled stack as text, innermost last. Frames from the stdlib's
    asyncio plumbing above our code are dropped: they are the same every time."""
    lines = traceback.format_stack(frame, limit=limit)
    keep = [ln for ln in lines if "asyncio" not in ln.split("\n", 1)[0]
            or ln is lines[-1]]
    return "".join(keep or lines).rstrip()


def _busy_threads(frames: dict, skip: set) -> list[str]:
    """The other threads that are running Python rather than parked in a wait:
    when the loop thread is itself idle-looking, one of these holds the GIL."""
    out = []
    for th in threading.enumerate():
        if th.ident in skip:
            continue
        fr = frames.get(th.ident or -1)
        if fr is None or fr.f_code.co_name in _IDLE_FRAMES:
            continue
        out.append(f"[{th.name}] " + _stack_key(fr, limit=8))
    return out


def summarize_block(blocked_s: float, samples: list[str], others: list[str],
                    ongoing: bool = False) -> str:
    """Pure: the report for one block — the stacks seen most often first."""
    from collections import Counter
    head = (f"LOOP BLOCKED {'(still) ' if ongoing else ''}{blocked_s:.2f}s — "
            f"{len(samples)} sample(s) of the event-loop thread")
    parts = [head]
    for stack, n in Counter(samples).most_common(3):
        parts.append(f"--- seen in {n}/{len(samples)} samples ---\n{stack}")
    if others:
        parts.append("--- other threads running Python meanwhile ---")
        for stack, n in Counter(others).most_common(3):
            parts.append(f"({n}x) {stack}")
    return "\n".join(parts)


def _loop_watchdog() -> None:
    samples: list[str] = []
    others: list[str] = []
    blocked_since = 0.0
    told_ongoing = False
    me = threading.get_ident()
    while True:
        time.sleep(LOOP_BLOCK_POLL_S)
        try:
            now = time.monotonic()
            stale = now - state.loop_beat if state.loop_beat else 0.0
            if stale >= LOOP_BLOCK_SAMPLE_S:
                if not blocked_since:
                    blocked_since = state.loop_beat
                frames = sys._current_frames()
                fr = frames.get(state.loop_thread_id)
                if fr is not None:
                    samples.append(_stack_key(fr))
                    # Always: a loop thread that looks busy may be waiting for the GIL
                    # at whatever line it was on (20.25.21).
                    others.extend(_busy_threads(frames, {me, state.loop_thread_id}))
                if not told_ongoing and stale >= LOOP_BLOCK_ONGOING_S:
                    told_ongoing = True
                    log.warning("%s", summarize_block(stale, samples, others, ongoing=True))
                continue
            if samples:
                # The loop's own measure of the turn, when it has one: this thread
                # can itself wake late under GIL contention, which made 0.23 s
                # blocks read 0.48 s on 20.25.20.
                took = state.block_lag_s if state.block_lag_s > 0 else now - blocked_since
                report = summarize_block(took, samples, others)
                state.last_block = time.strftime("%Y-%m-%d %H:%M:%S ") + report
                log.warning("%s", report)
            samples, others, blocked_since, told_ongoing = [], [], 0.0, False
            state.block_lag_s = 0.0
        except Exception as exc:                     # never let the watchdog die
            log.error("loop watchdog sample failed: %s: %s", type(exc).__name__, exc)
            samples, others, blocked_since, told_ongoing = [], [], 0.0, False


def _is_power_of_two(n: int) -> bool:
    return n > 0 and (n & (n - 1)) == 0


async def vitals_loop() -> None:
    """Sample health every `VITALS_INTERVAL_S`; escalate anomalies to the app log.

    Failures of the sampler ITSELF are reported at ERROR with a backoff, never
    swallowed — see the handler below for why that distinction matters.
    """
    consecutive_failures = 0
    while True:
        await asyncio.sleep(VITALS_INTERVAL_S)
        try:
            await disk_sample_async()
            s = snapshot()
            line = snapshot_line(s)
            state.loop_lag_peak_s = 0.0      # `lagmax=` covers one line each
            state.gc_peak_s, state.gc_peak_gen = 0.0, -1
            for gl in drain_gc_pauses():
                log.warning("%s", gl)
            if _vitals_log:
                _vitals_log.info(line)
            bad = _anomalies(s)
            if bad:
                log.warning("VITALS ANOMALY: %s | %s", "; ".join(bad), line)
                if s["loop_lag_s"] >= LOOP_LAG_WARN_S * 5 or \
                   s["oldest_req_s"] >= REQUEST_STUCK_S * 2:
                    dump_stalled_state("vitals anomaly: " + "; ".join(bad))
            if consecutive_failures:
                log.warning("VITALS recovered after %d failed sample(s)",
                            consecutive_failures)
                consecutive_failures = 0
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Never DEBUG. A diagnostics loop that dies quietly is worse than no
            # diagnostics at all: vitals.log simply stops growing, which looks
            # exactly like a healthy idle box. Shout on the first failure, then
            # back off geometrically so a permanently broken sampler doesn't
            # itself become the noise problem this module exists to remove.
            consecutive_failures += 1
            if _is_power_of_two(consecutive_failures):
                log.error("VITALS SAMPLE FAILED (%d in a row) — health "
                          "monitoring is degraded: %s: %s", consecutive_failures,
                          type(exc).__name__, exc, exc_info=True)


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
