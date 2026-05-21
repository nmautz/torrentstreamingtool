# Daemon & Watchdog

Two related but distinct pieces:

- **`daemon.py`** — installs StreamLink as a system service so it starts on boot/login. Called from `setup.py` (optional) or `run.py --install`.
- **`watchdog.py`** — runs as a thread inside `run.py` (or standalone when the service runs it) and re-launches crashed VLC / qBittorrent / Jackett.

## `daemon.py`

545 lines. Generates a wrapper script `streamlink_service.py` at the repo root, then registers the OS supervisor to run it.

### Wrapper script ([daemon.py:46](../daemon.py#L46))

`streamlink_service.py` is a generated Python file that mirrors the network-facing startup of `python run.py` so the installed service and interactive launch behave identically:
1. Sets up file logging (`logs/streamlink_service.log`) — the service has no console
2. Adds the venv `site-packages` to `sys.path` so `watchdog.py` can import `psutil` and `run.py` helpers can import `zeroconf`. Handles both Windows (`Lib/site-packages`) and Unix (`lib/pythonX.Y/site-packages`) layouts
3. Calls `start_watchdog()` (monitors VLC/qBit/Jackett — and launches them when missing)
4. Calls `run.get_local_ip()` to detect the LAN IP for mDNS
5. On Windows: calls `run.setup_windows_firewall(80)` and `(443)` if certs exist (idempotent — Task Scheduler runs the wrapper elevated so `netsh add rule` succeeds)
6. Calls `run.start_mdns(lan_ip, 80, 443 if certs)` so `remote.local` resolves for LAN clients
7. If `cert.pem` + `key.pem` exist: launches a separate `uvicorn … --port 443 --ssl-certfile … --ssl-keyfile …` subprocess for the admin panel (HTTPS), logging to `logs/uvicorn_https.log`. Not supervised — if it dies, only port 443 stops; the main HTTP service continues
8. Supervises `uvicorn main:app --host 0.0.0.0 --port 80` in a restart loop
9. Restart loop logic: clean exit (rc 0) → quit; non-zero → wait 5 s and retry. **Fast-death detection**: 5 consecutive crashes in under 15 s each → give up (prevents tight crash loops eating CPU)
10. On any exit path the wrapper cleans up: terminates the HTTPS subprocess, closes the zeroconf instance

The wrapper does **not** explicitly launch VLC / qBit / Jackett at boot — the watchdog detects them as down on its first tick and starts them itself. This matches the interactive `run.py` flow once you account for the watchdog also running there.

### Per-platform install

| Platform | Mechanism | File |
|----------|-----------|------|
| macOS | launchd user agent | `~/Library/LaunchAgents/com.streamlink.dashboard.plist` |
| Linux | systemd user unit | `~/.config/systemd/user/streamlink.service` |
| Windows | Task Scheduler | task name `StreamLink` |

### macOS ([daemon.py:233](../daemon.py#L233))
- Writes plist with `KeepAlive=true`, `RunAtLoad=true`, `WorkingDirectory=HERE`, stdout/stderr → `logs/streamlink.{log,err}`
- `launchctl unload` (in case loaded), then `launchctl load -w`
- Uninstall: `launchctl unload -w` + delete plist + delete wrapper

### Linux ([daemon.py:318](../daemon.py#L318))
- Writes unit with `Type=simple`, `Restart=on-failure`, `RestartSec=5`
- `systemctl --user daemon-reload && enable && start`
- Note about `loginctl enable-linger $USER` if you want it to start without an interactive login

### Windows ([daemon.py:393](../daemon.py#L393))
- Requires admin token (for `netsh advfirewall` and to register a task under another account). If not elevated, re-launches `run.py --install` through `ShellExecuteW "runas"` (UAC prompt). The elevated console reconfigures stdout to UTF-8 (legacy code pages can't render `✓`/`⚠`) and pauses at the end so the user can read the result
- While elevated, also adds Windows Firewall rules for TCP/80 (and /443 if `cert.pem` + `key.pem` exist) so a Standard-User-context task doesn't need to add them at runtime
- **`/RU` is the console user, not `USERNAME`** — `_windows_console_user()` queries `WTSGetActiveConsoleSessionId` + `WTSQuerySessionInformationW` to find who's actually logged in at the keyboard. After UAC elevation, `os.environ['USERNAME']` is the *Admin* account, which would register a task that only fires at Admin's logon — invisible to the regular user. Falls back to PowerShell `Win32_ComputerSystem.UserName` if the WTS API call fails, then to `USERNAME` with a warning
- **No `/RL HIGHEST`** — Windows doesn't restrict ports < 1024, so the wrapper doesn't need elevation to bind 80/443. HIGHEST on a Standard-User task fails silently (no admin token to elevate to), leaving the task registered but never running. See `docs/GOTCHAS.md` for the gory detail
- `schtasks /Create /SC ONLOGON /TN StreamLink /TR "<py> <wrapper>" /RU <console_user>`
- Then `schtasks /Run /TN StreamLink` to start immediately

### Public API

- `install()` → True/False
- `uninstall()` → True/False
- `status()` → prints to stdout

Used by `setup.py` (`offer_service_install()`) and `run.py` (`--install`/`--uninstall`/`--status`).

---

## `watchdog.py`

519 lines. Two modes:

1. **Embedded thread** ([watchdog.py:490](../watchdog.py#L490)) — `start_watchdog()` returns a daemon thread. Called from `run.py` after services are up
2. **Standalone process** ([watchdog.py:513](../watchdog.py#L513)) — `python3 watchdog.py` runs the loop directly. Used by the service wrapper

### `ServiceSpec` ([watchdog.py:266](../watchdog.py#L266))

A small dataclass-like class:
- `name`, `port`, `host`
- `find_bin` — callable returning the binary path (or None)
- `build_args(bin_path)` → list[str] **OR** None (when the start command was already run inline, e.g. Windows `sc.exe start`)
- `startup_timeout`, `back_off`
- Tracks `_failures` for exponential back-off capped at 120 s

### Loop ([watchdog.py:327](../watchdog.py#L327))

Three steps each tick (default 3 s):

1. **Check VPN**: `_vpn_connected()` runs `mullvad status` with a 5 s timeout. **No Mullvad CLI → treated as VPN-down** (cannot verify → unsafe).
2. **Enforce qBit ↔ VPN invariant**:
   - VPN down + qBit alive → kill qBit immediately via `_kill_by_name("qbittorrent")` (psutil-based, falls back to `taskkill`/`pkill`)
   - VPN up + qBit dead → wait back-off, re-check VPN didn't drop during sleep, then start qBit
3. **Plain services** (VLC, Jackett): port check → if down, wait back-off, restart

`_interruptible_sleep` watches `_stop_event` so `stop_watchdog()` exits the back-off promptly.

`vlc_spec.build_args` launches VLC with `--fullscreen` plus the Smart Skip **marquee args** (`--sub-source=marq --marq-file=<repo>/.vlc_marquee.txt --marq-position=10 …`) for the on-TV auto-skip countdown popup; `_build_specs` creates that file empty first. These args are mirrored in `run.py` `start_vlc` and `main.py` `_vlc_marquee_args()` — keep all three in sync. See [GOTCHAS.md](GOTCHAS.md#smart-skip-countdown-marquee).

Transitions (DOWN/UP) are logged once; routine ticks are silent. This keeps the log readable.

### Jackett specifics ([watchdog.py:160](../watchdog.py#L160))

On Windows, Jackett's `build_args` runs `sc.exe query Jackett` + `sc.exe start Jackett` inline and returns `None`. The loop logs the result but doesn't call `_launch_bg`. When the service isn't installed it now launches the tray/console exe as a **user process** (`return [bin_path]`) instead of giving up — that's the model the watchdog can fully manage without elevation.

Jackett is only added to plain_specs when `INDEXER_URL` points at localhost. Remote Jackett is unwatched — `run.py` already warned at startup if it wasn't reachable.

**HTTP health check, not a port check.** Jackett's `ServiceSpec` is built with a `health_check` (`_jackett_alive`) that requires both an open port *and* a successful `GET {INDEXER_URL}/UI/Login` (`_http_ok`). A hung Jackett holds the port open while it stops serving — a bare port check would call that alive forever. `ServiceSpec.is_alive()` uses `health_check` when present, else the port check (VLC/qBit are unchanged).

**Force-down before restart.** The Jackett spec also sets `pre_restart=_jackett_force_down`. `ServiceSpec.start()` runs it first: on Windows `_force_stop_jackett_windows()` does `sc stop` + waits for STOPPED (hard-kill fallback); elsewhere `_kill_by_name("jackett")`. This clears a wedged Jackett so the relaunch can re-bind 9117 — `sc start` alone is a 1056 no-op on a hung service. `start()` then waits on `is_alive()` (HTTP), not just the port, so it doesn't tight-loop before Jackett's web stack is ready (`startup_timeout=40s`).

**Admin requirement.** Stopping/starting a LocalSystem `Jackett` service needs admin; a non-elevated watchdog gets access-denied and logs a clear hint. `setup.py`'s `grant_jackett_service_control()` grants the rights once so the watchdog can recover Jackett without elevation. See [GOTCHAS.md](GOTCHAS.md#controlling-the-localsystem-jackett-service-needs-admin).

**Reusable restart.** `restart_jackett()` / `jackett_healthy()` ([watchdog.py:670](../watchdog.py#L670)) expose the same force-down+launch and HTTP-liveness logic so the dashboard process (`main.py`'s `jackett_health_monitor`) can use them as a backstop when no watchdog is running.

### Building specs ([watchdog.py:431](../watchdog.py#L431))

`_build_specs()` reads ports + URLs from `.env` and returns `(plain_specs, qbit_spec)`. Always built fresh each call.

## Logging

Both daemon and watchdog write to `logs/`:
- `logs/streamlink_service.log` — wrapper / supervisor messages
- `logs/uvicorn.log` — appended uvicorn stdout/stderr
- `logs/streamlink.log` + `.err` — launchd / systemd captured I/O (macOS/Linux)
- Standalone `watchdog.py` invocation logs to stderr

## See also

- [RUNTIME.md](RUNTIME.md) — how `run.py` integrates the watchdog
- [SETUP.md](SETUP.md) — `setup.py` calls `daemon.install()`
- [GOTCHAS.md](GOTCHAS.md) — the VPN-down → kill-qBit invariant
