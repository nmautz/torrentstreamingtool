# Runtime (`run.py`)

885 lines. The launcher. Run with **system Python** (`python3 run.py`); it auto-relaunches inside `.venv/bin/python` via `os.execv`.

## Venv relaunch ([run.py:33](../run.py#L33))

```python
if _VENV_PY.exists() and Path(sys.prefix).resolve() != VENV.resolve():
    os.execv(str(_VENV_PY), [str(_VENV_PY)] + sys.argv)
```

**Important**: this uses `sys.prefix`, not `sys.executable`. On Homebrew/pyenv macOS, system python3 and `.venv/bin/python` often resolve through symlinks to the same physical binary — so comparing executable paths would always say "already in venv" and the exec would never fire. `sys.prefix` is reliably set to the venv root inside a venv, so it's the correct signal.

## CLI flags ([run.py:701](../run.py#L701))

- `python3 run.py` — normal interactive launch
- `python3 run.py --install` / `--uninstall` / `--status` — proxies to `daemon.py`
- `-v` / `--verbose` — verbose logging (Jackett service diagnostics, etc.)

## Pre-flight ([run.py:722](../run.py#L722))

- `.venv` must exist (run setup.py if not)
- `.env` must exist
- `psutil` must import — catches the case where venv exists but `pip install` never finished

## Service launchers

### `start_vlc()`
- Port-open check first; if port 8080 is already serving, skip
- If a non-HTTP VLC is running, kill it, sleep 1.5 s
- `launch_bg([vlc_bin, --extraintf=http, --http-host=localhost, --http-port=N, --http-password=PWD, --no-random, --fullscreen, <marquee args>])`
- Always launches with HTTP flags rather than editing VLC prefs
- The `<marquee args>` (`--sub-source=marq --marq-file=<repo>/.vlc_marquee.txt --marq-position=10 …`) enable the Smart Skip on-TV countdown popup. Creates the file empty first. Mirrored in `watchdog.py` `vlc_spec` and `main.py` `_vlc_marquee_args()` — keep all three in sync. See [ANALYZER.md](ANALYZER.md#how-the-popup-reaches-the-tv) and [GOTCHAS.md](GOTCHAS.md#smart-skip-countdown-marquee).

### `start_qbittorrent()`
- Port-open check; if 8081 responds, skip
- Otherwise `launch_bg([qbit_bin])` and wait up to 30 s for port

### `start_jackett()` ([run.py:331](../run.py#L331))
- Parses `INDEXER_URL` for hostname; sets `is_local = host in (localhost, 127.0.0.1, ::1)`
- **Reachability is an HTTP check, not a port check.** `http_ok(f"{INDEXER_URL}/UI/Login")` decides "already reachable" — a hung Jackett keeps the port open while it stops serving, so a port check would wrongly skip the relaunch. See [GOTCHAS.md](GOTCHAS.md#port-open-is-not-a-jackett-health-check).
- **Remote**: never tries to launch — HTTP reachability check only. If unreachable, warns
- **Local**: if the port is open but it isn't serving (hung), `_force_stop_jackett_local()` clears it (Windows `sc stop` + wait, hard-kill fallback; else `kill_by_name`) so the relaunch can re-bind. Then finds the binary. On Windows: prefers starting the `Jackett` service via `sc.exe start Jackett` (handles 1056 = already running). If service isn't installed, prints how to fix. Otherwise launches `JackettTray.exe` (Windows) or `jackett --NoRestart` (others)
- `_diagnose_jackett_service_state()` (verbose mode) polls service state for 8 s, dumps the Jackett log file from any of 5 LocalSystem-AppData candidates, and pulls SCM events via PowerShell. Used to debug the case where the service registers, starts, then crashes immediately

### `check_mullvad()`
- Runs `mullvad status`; returns True if "Connected" in output
- If not connected, asks for confirmation before continuing (VPN kill-switch will be inactive)

## Watchdog ([run.py:766](../run.py#L766))

After all services are up, `start_watchdog()` is called. It returns a daemon thread that monitors VLC/qBit/Jackett and restarts crashed ones — see [DAEMON_WATCHDOG.md](DAEMON_WATCHDOG.md).

## Dashboard launch ([run.py:779](../run.py#L779))

- **Port 80** for HTTP (the main dashboard)
- **Port 443** for HTTPS (admin panel) — only if `cert.pem` + `key.pem` exist
- Uvicorn binds to `0.0.0.0` so mobile devices on the same LAN can connect
- The HTTPS process is launched separately (also via uvicorn) as a subprocess; the main `run.py` foreground process is the HTTP uvicorn
- Both pointed at `main:app`. Same FastAPI app served over both — `admin_https_redirect` middleware ensures admin routes go through 443
- No browser auto-open. `run.py` is a server launcher — the URL is printed for the operator, but `webbrowser.open` is intentionally not called so headless / service installs don't try to pop a UI on a box that may have no display.

## LAN detection ([run.py:516](../run.py#L516))

`get_local_ip()` returns the LAN IP the phone should use:
1. Enumerates physical interfaces via `psutil.net_if_addrs()`
2. Drops interfaces by **name** (`utun*`, `tun*`, `tap*`, `wg*`, `ppp*`, `lo`, anything containing `mullvad`/`wireguard`/`vpn`/`vmware`/`vbox`/`hyper-v`/`virtual`/`loopback`)
3. Drops by **subnet** (`192.168.56.*` VirtualBox, `192.168.99.*` Docker Machine, `192.168.137.*` Windows ICS, `169.254.*` APIPA)
4. Prefers `192.168.*` → `10.*` → `172.16-31.*`
5. Uses a `connect()` to `8.8.8.8:80` (no packet sent) to learn which interface the OS routing table would pick — but only accepts the result if it's already in the candidate set. This means an active VPN that captured the default route can never win.

`get_wifi_ssid()` is best-effort: `airport -I` (macOS, with `networksetup` fallback), `iwgetid -r` (Linux), `netsh wlan show interfaces` (Windows).

## mDNS ([run.py:734](../run.py#L734))

Uses `zeroconf`. `start_mdns(lan_ip, http_port, https_port)` registers `_http._tcp.local. → remote.local` on the given LAN IP (port 80), plus a separate `_https._tcp.local.` entry for port 443 if SSL certs exist.

**Both `run.py` and the installed service call `start_mdns_resilient()`, not `start_mdns()` directly.** It spawns a daemon thread that polls `get_local_ip()` until a LAN IP appears, registers, then re-registers if the IP later changes (DHCP lease / network switch). It polls every 5 s until registered, then every 30 s to watch for changes. This exists because the service starts at boot **before Wi-Fi is up** — a one-shot registration would see no IP and silently skip, so `remote.local` would never resolve until a manual relaunch even though the dashboard is reachable by IP. See [GOTCHAS.md](GOTCHAS.md#remotelocal-doesnt-resolve-after-a-reboot). Returns a handle with `.close()`, called on Ctrl+C / service shutdown.

## Windows Firewall ([run.py:629](../run.py#L629))

Adds inbound rules for the HTTP port, HTTPS port (if certs), and UDP 5353 (mDNS). Idempotent — checks `netsh advfirewall firewall show rule name=…` before adding. Requires Administrator; warns if not elevated.

## Privileged ports

Port 80 / 443 require root on macOS/Linux. If `os.geteuid() != 0`, warns about needing `sudo python3 run.py`.

## Shutdown

On Ctrl+C: terminates the HTTPS subprocess (5 s timeout), closes zeroconf, exits. VLC, qBittorrent, and Jackett keep running — only the dashboard stops.
