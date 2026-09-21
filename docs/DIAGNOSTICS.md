# Diagnostics

Runtime health instrumentation: `diagnostics.py` plus its wiring in `main.py`,
`run.py` and `daemon.py`. Read this when the server is unreachable, slow, or
"crashed" with nothing in the logs.

---

## Why this exists

On **2026-09-13** the dashboard and TV UI went dead for roughly 50 minutes and
the operator eventually power-cycled the host by hand. The logs could not say
why — and that, not the outage itself, is the problem this subsystem solves.

What the logs *did* show:

- The process was **healthy throughout**: a flat ~168 log lines/min of VLC and
  qBittorrent polling right up to the manual reboot. No stall, no traceback, no
  memory symptom. The event loop was plainly turning.
- Inbound requests arriving **over HTTPS were still being served** as late as
  14:44:11 — a `200` on `/api/admin/status`, seconds before the reboot.

What they could not show, and why:

| Blind spot | Cause |
|---|---|
| **No record of any request to port 80** — the port the dashboard and TV UI use | `run.py` and `daemon.py` both started uvicorn with `log_level="warning"` and a `log_config` stub containing **no handlers**. uvicorn emits access lines at INFO, so they were discarded. |
| Inbound HTTPS traffic visible only by accident | The :443 proxy forwards to `127.0.0.1:80` with `httpx`, and **httpx's own INFO logging** recorded those hops. Plain-HTTP clients bypass the proxy and left no trace at all. |
| No proof the server was still accepting | Nothing probed the listening socket from inside the process. A wedged server and a healthy one look identical in a log of outbound polls. |
| No view of the loop, thread pool or library lock | Never collected. These are the three things that starve request handling while leaving background pollers untouched. |
| Real events buried | ~168 lines/min of httpx `200 OK` chatter, plus a flood of benign `ConnectionResetError` (WinError 10054) logged at ERROR. |
| `print()` diagnostics unreachable | The updater and reboot paths report via `print()`, not the `streamlink` logger — no `[reboot]`/`[updater]` lines appear in `streamlink_service.log` at all. |

### The failure mode this is built to catch

`get_library()` holds `_lib_lock` **across** `asyncio.to_thread(_load_lib_raw)`.
So if the default thread pool saturates, the lock is held while waiting for a
worker, and **every request touching the library stalls** — while the VLC and
qBittorrent pollers, which are pure async `httpx` and take no lock, carry on at
full cadence.

The result is a box that looks perfectly healthy in the logs and is dead to
every client. That asymmetry is invisible unless something is watching the lock,
the pool and the socket specifically. That is what `diagnostics.py` does.

---

## Client logs from the iOS app

`client_<device>.log` in `LOG_DIR` is **not** written by the server — it is uploaded by
the app through `POST /api/diag/client-log` (☰ App → Settings → Playback → **Send log to
server**). It is newline-delimited JSON with absolute timestamps, written on the device by
`DiagLog` in `NativePlayback.swift`, and it survives app restarts, which is the whole
point: the in-memory diagnostics trail is 40 rows that die with the process and timestamps
in seconds-since-launch, so it can answer "what happened in this ten-minute test" and
nothing longer.

Read it like any other log (`/api/admin/logs/client_<device>.log`). Useful rows:

| `ev` | Means |
|---|---|
| `launch` | app start — carries the build; use these to split a long log into sessions |
| `snap` | a diagnostics trail row (every field the Monitor diagnostics panel shows) |
| `startNative` | handoff began — `reason` (`early`/`background`/`manual`), `shouldPlay`, `extWindow` |
| `stopNative` | handoff ended, with the position it ended at |
| `advance` / `ended` | auto-advance to the next episode, or the end of the playlist |
| `progress` | a progress POST **and its HTTP status** — the thing that was silent while nothing saved |
| `progress-skipped` | a POST refused by a guard, naming which field was missing or zero |
| `audio-session-failed` | `setActive` threw, with the app state at the time |

**`progress-skipped` is the row to look for first** when positions are not being saved. A
silent `guard` hid the duration-0 bug for a full day; that guard now says so.

## Log files

All under `logs/`, all listed and downloadable from **Admin → System → Server
Logs** (and included in "Download All").

| File | Written by | Contents |
|---|---|---|
| `access.log` | uvicorn (`uvicorn.access`) | **Every inbound request**, both :80 and :443. 8 MB × 5. |
| `uvicorn.log` | uvicorn (`uvicorn.error`) | Server startup / bind / shutdown errors. |
| `vitals.log` | `vitals_loop` | One health sample every 30 s. 4 MB × 3. |
| `stall_<ts>.txt` | `dump_stalled_state` | Full asyncio task + thread stack dump at the moment of a stall. |
| `faulthandler.log` | `faulthandler` | Native-level stack if the interpreter dies hard (segfault in a C extension). |
| `streamlink_app.log` | `streamlink` logger | App log. Anomalies escalate here at WARNING. |

> **One file, one handler.** `access.log` is owned exclusively by uvicorn's
> dictConfig, and `uvicorn.error` deliberately writes to `uvicorn.log` rather
> than sharing `streamlink_app.log`. Two `RotatingFileHandler`s on one file
> means rotation renames it while the other handle is open — a `PermissionError`
> on Windows, the primary target, and potentially a lost log.

---

## What runs

Three background tasks, started in `lifespan` and cancelled on shutdown.

### `_measure_loop_lag`
Sleeps 0.5 s in a loop and records how late it actually returns. Lag is the
clearest single indicator that something synchronous is blocking the loop.

### `vitals_loop` — every 30 s
Writes one compact line to `vitals.log`:

```
up=3612s lag=0.01s tasks=47 tp=12/20+q0 inflight=1 oldest=0.3s
lock_held=0.0s waiters=0 probe=4ms fails=0 rss=412MB thr=31 fds=612 conns=48
```

If the sampler *itself* throws, that is reported at **ERROR** with a traceback on
the 1st, 2nd, 4th, 8th … consecutive failure (geometric backoff), and recovery is
logged too. It is never swallowed at DEBUG: a diagnostics loop that dies quietly
just stops growing `vitals.log`, which reads exactly like a healthy idle box.

If any threshold is breached, the same line is **also** logged at WARNING to
`streamlink_app.log`, prefixed `VITALS ANOMALY` — so the onset of trouble shows
up in the file an operator opens first. Thresholds (all in `diagnostics.py`):

| Constant | Default | Meaning |
|---|---|---|
| `LOOP_LAG_WARN_S` | 1.0 s | Event loop this far behind ⇒ something is blocking it |
| `LOCK_HELD_WARN_S` | 5.0 s | Library lock held this long ⇒ every waiter is stuck |
| `REQUEST_SLOW_S` | 5.0 s | Individual request logged as `SLOW REQUEST` |
| `REQUEST_STUCK_S` | 30 s | In-flight this long ⇒ anomaly |
| `THREADPOOL_WARN_FRAC` | 0.85 | Pool this full **and** work queued ⇒ saturated |
| `STALL_DUMP_COOLDOWN_S` | 300 s | Minimum gap between stack dumps |

Thread-pool saturation is reported only when the pool is at capacity *and* the
work queue is non-empty. `ThreadPoolExecutor` exposes no "busy" counter and
inventing one is guesswork; it only spawns a worker when none is idle, so
"threads at max + queue non-empty" is a real signal.

### `self_probe_loop` — every 30 s
`GET /healthz` on `127.0.0.1:<port>` over a **raw socket** (deliberately not
httpx — the point is to test the listening socket with as few of our own moving
parts as possible). This is the check whose absence made 2026-09-13
undiagnosable.

- Success → nothing logged (the timing lands in `vitals.log` as `probe=`).
- Failure → `CRITICAL SELF-PROBE FAILED` with a full vitals snapshot.
- **Two** consecutive failures → a stall dump.

`/healthz` is deliberately trivial: it touches no lock, no disk and no library.
A `200` there means "the socket accepts and the loop is turning", which cleanly
separates a networking/accept-queue fault from library-lock or thread-pool
starvation. **If `/healthz` answers while real endpoints hang, the fault is
downstream of the server** — look at `lock_held` and `tp_*` in the vitals line.

---

## Request tracking

`diag_track_requests` is registered **first** in `main.py`, which makes it the
outermost middleware (Starlette applies them in reverse), so it times the whole
stack including the HTTPS and adapter redirects.

It matters because **uvicorn's access log only records a request when it
finishes** — a handler that never returns leaves no line at all. `diag.state.inflight`
shows it *while it is still hanging*, the vitals line counts it (`inflight=`,
`oldest=`), and the stall dump names it.

Requests slower than `REQUEST_SLOW_S` are logged as `SLOW REQUEST 7.2s status=200 GET /api/library`.

---

## Stall dumps

`dump_stalled_state(reason)` writes `logs/stall_<ts>.txt` containing:

1. The vitals snapshot and the triggering reason
2. Every in-flight request with its age
3. Every instrumented lock: held-for, holder task, waiter count
4. **Every asyncio task stack**
5. **Every thread stack**

This is the artifact the 2026-09-13 incident lacked, and nothing reconstructs it
after the fact. Rate-limited to one per `STALL_DUMP_COOLDOWN_S` so a long outage
produces a handful of dumps rather than thousands.

Triggered by: two consecutive self-probe failures, loop lag ≥ 5 s, or a request
in-flight ≥ 60 s.

---

## Noise control

- `quiet_noisy_loggers()` drops `httpx` / `httpcore` / `urllib3` to WARNING,
  removing the ~168 lines/min of poll chatter. Nothing is lost: inbound traffic
  is now recorded properly in `access.log` rather than incidentally via the
  proxy's httpx hops.
- `install_loop_exception_handler()` demotes `ConnectionResetError` /
  `connection_lost` (benign WinError 10054 client-disconnect churn, which used
  to flood the log at ERROR) to DEBUG, and logs everything else loudly. The goal
  is that **an ERROR in the app log means something**.
- `install_exception_hooks()` routes `sys.excepthook` and `threading.excepthook`
  into the app log, so an exception on a non-asyncio thread is no longer lost to
  a stderr nobody correlates.

---

## Reading an incident

1. **`access.log` — did requests arrive at all?**
   - No entries → the problem is upstream: network, firewall, mDNS, adapter, or
     the client never resolved the host. Cross-check `SELF-PROBE` lines: if the
     probe succeeded throughout, the server was accepting fine and the fault was
     outside the process.
   - Entries present but no matching completion → the handler hung; find it in
     the stall dump.
2. **`streamlink_app.log` — grep `VITALS ANOMALY`, `SELF-PROBE`, `SLOW REQUEST`, `LOCK`.**
   The first anomaly line marks the onset.
3. **`vitals.log` — scroll back to the onset** and read across: `lag`, `tp=`,
   `lock_held=`, `inflight=`. These name the starvation mode directly.
   High `lag` with `lock_held=0.0s` and spare pool threads means synchronous work
   on the loop itself, e.g. the per-call `httpx.AsyncClient` construction fixed
   in 16.3.3 (see GOTCHAS).
4. **`stall_*.txt`** — the stacks say exactly what every task was awaiting.

### Two traps
- **A `logs_old_<ts>.zip` is not evidence of an update-triggered restart.**
  `_archive_old_logs()` runs only when the updater left a `.rotate_pending`
  marker, and that marker can sit unconsumed for hours. It means "an update was
  applied at some point before this start".
- **Applying an update without the reboot leaves a split-brain process.** A
  `POST /api/admin/updater/apply` with `reboot` off swaps the git checkout *and*
  re-runs `setup.py` under the still-running interpreter: in-memory code is the
  old build while `static/*.html` and anything imported lazily come off disk from
  the new one. Reboot promptly after applying.

---

## Deploying a change to this subsystem

> **Access logging does not depend on any of this.** `main.py` calls
> `diag.attach_access_log()` at import, and uvicorn imports `main.py` fresh on
> every boot, so `access.log` works even from a stale launcher. The configs
> below remain the belt to that pair of braces; `attach_access_log` skips a file
> that already has a handler, so the two never collide.


The uvicorn log config lives in **three** places that must stay in step:

| Path | Used by |
|---|---|
| `daemon.py` `_WRAPPER_CONTENT` | **The installed service** — this is what runs on the box |
| `run.py` | Interactive `python run.py` |
| `diagnostics.uvicorn_log_config()` | The shared definition both import |

⚠️ `daemon.py` only *generates* `streamlink_service.py`. An existing install
keeps running its old wrapper until the file is refreshed — via
`updater.refresh_service_wrapper()` (the updater does this automatically) or
`python run.py --install`. Changing `daemon.py` alone changes nothing on a box
that is already installed.

---

## See also

- [GOTCHAS.md](GOTCHAS.md) — the 2026-09-13 write-up and other footguns
- [ARCHITECTURE.md](ARCHITECTURE.md) — process model and service topology
- [DAEMON_WATCHDOG.md](DAEMON_WATCHDOG.md) — the service wrapper and crash supervisor
- [ADMIN.md](ADMIN.md) — Server Logs card, scheduled restart
