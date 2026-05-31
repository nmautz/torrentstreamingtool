#!/usr/bin/env python3
"""
StreamLink Watchdog (7.2)
=========================
Monitors VLC, qBittorrent, and Jackett.  When a service disappears it
waits a short back-off, then re-launches it — BUT qBittorrent is special:

  • If Mullvad VPN is CONNECTED    → qBit is kept alive (restarted if it dies)
  • If Mullvad VPN is DISCONNECTED → qBit is killed immediately and will NOT
    be restarted until the VPN comes back up

This mirrors and reinforces the kill-switch guard already in main.py's
vpn_guard task, but at the process level so it works even when the
dashboard itself is not running (e.g. when launched as a daemon service).

Usage (embedded — called from run.py):
    from watchdog import start_watchdog
    start_watchdog()       # starts a daemon thread; returns immediately

Usage (standalone — called by the launchd / systemd / Task Scheduler
daemon service):
    python3 watchdog.py
"""
from __future__ import annotations

import json
import logging
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE   = Path(__file__).parent
SYSTEM = platform.system()

# Night mode (dynamic-range compressor). Mirrors main.py's NIGHT_MODE_ARGS and
# run.py's copy — change one, change all three. VLC has no runtime HTTP command
# for audio filters, so the compressor is a launch arg gated on the persisted
# library.json setting. See docs/GOTCHAS.md.
NIGHT_MODE_ARGS = [
    "--audio-filter=compressor",
    "--compressor-rms-peak=0.00",
    "--compressor-attack=25.0",
    "--compressor-release=250.0",
    "--compressor-threshold=-24.0",
    "--compressor-ratio=8.0",
    "--compressor-knee=3.0",
    "--compressor-makeup-gain=12.0",
]


def night_mode_args() -> list:
    """Return NIGHT_MODE_ARGS when library.json → settings.vlc_night_mode is on."""
    try:
        data = json.loads((HERE / "library.json").read_text(encoding="utf-8"))
        if (data.get("settings", {}) or {}).get("vlc_night_mode"):
            return list(NIGHT_MODE_ARGS)
    except Exception:
        pass
    return []

# ── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  [watchdog] %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("streamlink.watchdog")


# ── .env loader ────────────────────────────────────────────────────────────
def _load_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip()
    return env


_ENV = _load_env(HERE / ".env")


def _e(key: str, default: str = "") -> str:
    return _ENV.get(key, os.environ.get(key, default))


# ── Port helpers ───────────────────────────────────────────────────────────
def _port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.8) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _extract_port(url: str, default: int) -> int:
    m = re.search(r":(\d+)", url)
    return int(m.group(1)) if m else default


def _wait_port(port: int, host: str, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _port_open(port, host):
            return True
        time.sleep(0.5)
    return False


def _http_ok(url: str, timeout: float = 4.0) -> bool:
    """True if `url` returns *any* HTTP response within `timeout`.

    Used instead of a bare TCP check for Jackett. A hung Jackett can keep its
    listener socket bound (so the port still "connects") while it has stopped
    serving requests — a port-open check would call that "alive" forever and
    never restart it. Any HTTP status, even 401/404, proves the web stack is
    answering; a refused connection, reset, or read timeout means it's wedged.
    Uses stdlib urllib so this works in the standalone `python3 watchdog.py`
    path where the venv (and httpx) may not be importable.
    """
    import urllib.request
    import urllib.error
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            resp.read(1)
            return True
    except urllib.error.HTTPError:
        return True
    except Exception:
        return False


# ── Executable finders (mirrors run.py) ────────────────────────────────────
def _find(saved_key: str, candidates_by_os: dict, fallback: list[str]) -> str | None:
    saved = _e(saved_key)
    if saved and Path(saved).exists():
        return saved
    for c in candidates_by_os.get(SYSTEM, fallback):
        p = Path(c)
        if p.exists():
            return str(p)
        found = shutil.which(c)
        if found:
            return found
    return None


def find_vlc() -> str | None:
    return _find("_VLC_BIN", {
        "Darwin":  ["/Applications/VLC.app/Contents/MacOS/VLC", "vlc"],
        "Windows": [r"C:\Program Files\VideoLAN\VLC\vlc.exe",
                    r"C:\Program Files (x86)\VideoLAN\VLC\vlc.exe"],
    }, ["/usr/bin/vlc", "vlc"])


def find_qbit() -> str | None:
    return _find("_QBIT_BIN", {
        "Darwin":  ["/Applications/qbittorrent.app/Contents/MacOS/qbittorrent", "qbittorrent"],
        "Windows": [r"C:\Program Files\qBittorrent\qbittorrent.exe"],
    }, ["/usr/bin/qbittorrent", "qbittorrent"])


def _windows_jackett_candidates() -> list[str]:
    """Every location the Jackett Windows installer / winget may use.

    `JackettTray.exe` first — it shows the tray icon and starts the service.
    """
    roots = [
        os.environ.get("ProgramFiles", r"C:\Program Files"),
        os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
        os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")),
        os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming")),
        os.environ.get("ProgramData", r"C:\ProgramData"),
    ]
    out: list[str] = []
    for r in roots:
        for sub in ("Jackett", os.path.join("Programs", "Jackett")):
            base = Path(r) / sub
            out.append(str(base / "JackettTray.exe"))
            out.append(str(base / "JackettConsole.exe"))
            out.append(str(base / "jackett.exe"))
    return out


def _start_jackett_service_windows() -> bool:
    """Best-effort: start the 'Jackett' Windows service. Returns True on
    success or if it's already running; False if the service isn't installed."""
    try:
        r = subprocess.run(["sc.exe", "start", "Jackett"], capture_output=True, text=True, timeout=10)
    except Exception:
        return False
    out = (r.stdout + r.stderr).upper()
    if r.returncode == 0:
        return True
    if "1056" in out or "ALREADY" in out:
        return True
    return False


def _force_stop_jackett_windows() -> None:
    """Force a hung/zombie Jackett down so it can be cleanly restarted.

    Handles both Windows install models:
      • LocalSystem **service** — `sc.exe start` is a no-op (1056 ALREADY_RUNNING)
        on a wedged service, which is why a hung Jackett previously needed a full
        reboot. We stop it and wait for STOPPED.
      • **Tray / user process** (no service) — there's nothing to `sc stop`, so we
        kill the process directly.

    Stopping a LocalSystem service (or killing its process) needs admin rights;
    if StreamLink isn't elevated this logs a clear hint instead of silently
    failing — that's the difference between "auto-recovered" and "needs reboot".
    """
    q = subprocess.run(["sc.exe", "query", "Jackett"], capture_output=True, text=True)
    service_exists = q.returncode == 0

    if service_exists:
        try:
            r = subprocess.run(["sc.exe", "stop", "Jackett"],
                               capture_output=True, text=True, timeout=10)
            out = (r.stdout or "") + (r.stderr or "")
            log.info("sc.exe stop Jackett → exit=%d", r.returncode)
            if r.returncode == 5 or "Access is denied" in out:
                log.warning(
                    "Access denied stopping the Jackett service — StreamLink is not "
                    "elevated. Run it as Administrator (or grant this account service "
                    "start/stop rights), otherwise a hung Jackett can only be cleared "
                    "by a reboot."
                )
        except Exception as exc:
            log.warning("sc.exe stop Jackett failed: %s", exc)
        for _ in range(12):
            qq = subprocess.run(["sc.exe", "query", "Jackett"], capture_output=True, text=True)
            if "STOPPED" in (qq.stdout or ""):
                return
            if _stop_event.wait(1.0):
                return
        log.warning("Jackett service did not stop in time — hard-killing the process.")

    # No service, or the service refused to stop: kill the process directly.
    killed = _kill_by_name("jackett")
    if killed == 0:
        log.warning(
            "No Jackett process could be killed — it may be a LocalSystem service "
            "this account can't terminate. Re-launch StreamLink as Administrator."
        )
    _stop_event.wait(1.0)


def _build_jackett_args(bin_path: str):
    """Return the launch command for Jackett — or None when the start was
    handled synchronously here (Windows path)."""
    if SYSTEM == "Windows":
        # Check the service first so we can log it explicitly.
        q = subprocess.run(["sc.exe", "query", "Jackett"], capture_output=True, text=True)
        log.debug("sc.exe query Jackett → exit=%d", q.returncode)
        if q.stdout:
            for line in q.stdout.strip().splitlines():
                log.debug("  stdout: %s", line)
        if q.stderr:
            for line in q.stderr.strip().splitlines():
                log.debug("  stderr: %s", line)
        if q.returncode != 0:
            # No LocalSystem service — launch Jackett as a user process (the
            # tray/console exe). This is the model the watchdog can fully manage
            # without elevation, so we start it rather than giving up.
            log.warning(
                "Jackett Windows service not installed — launching %s as a user "
                "process instead.", bin_path
            )
            return [bin_path]
        r = subprocess.run(["sc.exe", "start", "Jackett"], capture_output=True, text=True, timeout=10)
        out = (r.stdout or "") + (r.stderr or "")
        log.info("sc.exe start Jackett → exit=%d", r.returncode)
        if r.stdout:
            for line in r.stdout.strip().splitlines():
                log.info("  stdout: %s", line)
        if r.stderr:
            for line in r.stderr.strip().splitlines():
                log.info("  stderr: %s", line)
        if r.returncode == 5 or "Access is denied" in out:
            log.warning(
                "Access denied starting the Jackett service — StreamLink is not "
                "elevated. Run it as Administrator (or grant this account service "
                "start/stop rights) so the watchdog can recover Jackett without a reboot."
            )
        return None
    return [bin_path, "--NoRestart"]


def find_jackett() -> str | None:
    return _find("_JACKETT_BIN", {
        "Darwin":  [
            "/Applications/Jackett/JackettConsole",
            "/opt/homebrew/opt/jackett/bin/jackett",
            str(Path.home() / "Downloads/Jackett/jackett"),
            "jackett",
        ],
        "Windows": _windows_jackett_candidates(),
    }, [str(Path.home() / "Downloads/Jackett/jackett"), "jackett"])


def find_mullvad() -> str | None:
    return _find("_MULLVAD_BIN", {
        "Darwin":  ["/Applications/Mullvad VPN.app/Contents/Resources/mullvad", "mullvad"],
        "Windows": [r"C:\Program Files\Mullvad VPN\resources\mullvad.exe", "mullvad"],
    }, ["mullvad"])


# ── Background launcher (detached) ─────────────────────────────────────────
def _launch_bg(args: list[str]) -> None:
    kw: dict = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if SYSTEM == "Windows":
        kw["creationflags"] = (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        )
    else:
        kw["start_new_session"] = True
    subprocess.Popen(args, **kw)


# ── Kill by process name ────────────────────────────────────────────────────
def _kill_by_name(name: str) -> int:
    """Kill all processes whose name starts with `name`. Returns count killed."""
    try:
        import psutil
        killed = 0
        name_l = name.lower()
        for p in psutil.process_iter(["name", "pid"]):
            pname = (p.info["name"] or "").lower()
            if pname.startswith(name_l):
                try:
                    p.kill()
                    killed += 1
                except Exception:
                    pass
        return killed
    except ImportError:
        # psutil unavailable — fall back to OS tools
        if SYSTEM == "Windows":
            subprocess.run(["taskkill", "/F", "/IM", f"{name}.exe"], capture_output=True)
        else:
            subprocess.run(["pkill", "-f", name], capture_output=True)
        return -1  # unknown count


# ── VPN check ──────────────────────────────────────────────────────────────
def _vpn_connected() -> bool:
    """Return True if Mullvad reports 'Connected'.
    Returns False on any error or if the CLI is not found
    (absent CLI = cannot verify VPN = treat as unsafe)."""
    mullvad = find_mullvad()
    if not mullvad:
        return False
    try:
        result = subprocess.run(
            [mullvad, "status"],
            capture_output=True, text=True, timeout=5,
        )
        return "Connected" in result.stdout
    except Exception:
        return False


# ── ServiceSpec ────────────────────────────────────────────────────────────
class ServiceSpec:
    """Everything the watchdog needs to check and (re)start one service."""

    def __init__(
        self,
        name: str,
        port: int,
        host: str,
        find_bin: callable,
        build_args: callable,        # (bin_path: str) -> list[str]
        startup_timeout: float = 30.0,
        back_off: float = 5.0,
        health_check: callable = None,   # () -> bool; overrides the port check
        pre_restart: callable = None,    # () -> None; run before (re)launching
    ):
        self.name            = name
        self.port            = port
        self.host            = host
        self.find_bin        = find_bin
        self.build_args      = build_args
        self.startup_timeout = startup_timeout
        self.back_off        = back_off
        self.health_check    = health_check
        self.pre_restart     = pre_restart
        self._failures       = 0
        self._MAX_BACK_OFF   = 120.0

    def is_alive(self) -> bool:
        # When a service supplies a real health check (e.g. an HTTP probe), use
        # it — a bare TCP port-open can't tell a serving process from a hung one
        # that still holds the socket. Falls back to the port check otherwise.
        if self.health_check is not None:
            return self.health_check()
        return _port_open(self.port, self.host, timeout=0.5)

    def _wait_until_alive(self, timeout: float) -> bool:
        """Poll is_alive() until True or timeout — honours the stop event so a
        long Jackett startup wait doesn't block a clean shutdown."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.is_alive():
                return True
            if _stop_event.wait(0.5):
                return False
        return False

    def start(self) -> bool:
        # A hung service can keep its port bound, so simply launching a second
        # copy would fail to bind. pre_restart forces the old one down first.
        if self.pre_restart is not None:
            try:
                self.pre_restart()
            except Exception as exc:
                log.warning("%s pre-restart hook failed: %s", self.name, exc)
        bin_path = self.find_bin()
        if not bin_path:
            log.warning("%s binary not found — cannot start", self.name)
            return False
        args = self.build_args(bin_path)
        if args is None:
            # build_args ran the start command itself (e.g. sc.exe with
            # logging on Windows); nothing left to launch in the background.
            log.info("Starting %s (handled inline)", self.name)
        else:
            log.info("Starting %s: %s", self.name, " ".join(str(a) for a in args))
            _launch_bg(args)
        # Wait on the real liveness check, not just the port — for Jackett this
        # means we wait until it actually serves HTTP, which prevents a tight
        # restart loop where the port opens before the web stack is ready.
        alive = self._wait_until_alive(self.startup_timeout)
        if alive:
            log.info("%s is up on port %d", self.name, self.port)
            self._failures = 0
        else:
            self._failures += 1
            log.warning(
                "%s did not come up within %.0fs (attempt %d)",
                self.name, self.startup_timeout, self._failures,
            )
        return alive

    def current_back_off(self) -> float:
        """Exponential back-off capped at _MAX_BACK_OFF seconds."""
        return min(self.back_off * (2 ** self._failures), self._MAX_BACK_OFF)


# ── Main watchdog loop ─────────────────────────────────────────────────────
_POLL_INTERVAL = 3.0   # seconds between full-cycle checks
_stop_event    = threading.Event()


def _watchdog_loop(
    plain_specs: list[ServiceSpec],   # VLC, Jackett — always kept alive
    qbit_spec: ServiceSpec,           # qBittorrent — VPN-gated
) -> None:
    all_names = [s.name for s in plain_specs] + [qbit_spec.name]
    log.info("Watchdog started — monitoring: %s", ", ".join(all_names))
    log.info(
        "qBittorrent is VPN-gated: killed immediately on VPN drop, "
        "restarted only when VPN reconnects."
    )

    # Track previous liveness so we only log on transitions, not every tick.
    prev_alive: dict[str, bool] = {s.name: True for s in plain_specs}
    prev_alive[qbit_spec.name] = True
    vpn_was_connected: bool | None = None   # None = not yet checked

    while not _stop_event.is_set():

        # ── Step 1: Check VPN — gates all qBit decisions ─────────────────
        vpn_ok = _vpn_connected()

        if vpn_ok != vpn_was_connected:
            if vpn_ok:
                log.info("Mullvad VPN connected.")
            else:
                log.warning("Mullvad VPN disconnected.")
            vpn_was_connected = vpn_ok

        # ── Step 2: Enforce qBit ↔ VPN invariant ─────────────────────────
        qbit_alive = qbit_spec.is_alive()

        if not vpn_ok:
            # VPN down → qBit must not run, period.
            if qbit_alive:
                killed = _kill_by_name("qbittorrent")
                log.warning(
                    "Mullvad VPN is DOWN — killed %s qBittorrent process(es). "
                    "qBit will not restart until VPN reconnects.",
                    killed if killed >= 0 else "all",
                )
            # No restart attempt while VPN is down; fall through to plain services.

        else:
            # VPN up → qBit should be running; restart it if it's not.
            if not qbit_alive:
                if prev_alive.get(qbit_spec.name, True):
                    # First tick we noticed it was down
                    log.warning(
                        "qBittorrent is DOWN on port %d — VPN is up, will restart after back-off.",
                        qbit_spec.port,
                    )
                back_off = qbit_spec.current_back_off()
                if back_off > _POLL_INTERVAL:
                    log.info("qBittorrent back-off: %.0fs", back_off)
                # Sleep in small increments so we can honour stop_event quickly
                _interruptible_sleep(back_off)
                if not _stop_event.is_set() and not qbit_spec.is_alive():
                    # Re-check VPN hasn't dropped during the back-off sleep
                    if _vpn_connected():
                        qbit_spec.start()
                    else:
                        log.warning(
                            "VPN dropped during qBittorrent back-off — aborting restart."
                        )
            elif not prev_alive.get(qbit_spec.name, True):
                log.info("qBittorrent recovered.")

        prev_alive[qbit_spec.name] = qbit_spec.is_alive()

        # ── Step 3: Plain services (VLC, Jackett) — always keep alive ─────
        for spec in plain_specs:
            if _stop_event.is_set():
                break

            alive = spec.is_alive()

            if not alive:
                if prev_alive.get(spec.name, True):
                    log.warning(
                        "%s is DOWN on port %d — will restart after back-off.",
                        spec.name, spec.port,
                    )
                back_off = spec.current_back_off()
                if back_off > _POLL_INTERVAL:
                    log.info("%s back-off: %.0fs", spec.name, back_off)
                _interruptible_sleep(back_off)
                if not _stop_event.is_set() and not spec.is_alive():
                    spec.start()
            elif not prev_alive.get(spec.name, True):
                log.info("%s recovered.", spec.name)

            prev_alive[spec.name] = spec.is_alive()

        _stop_event.wait(timeout=_POLL_INTERVAL)

    log.info("Watchdog stopped.")


def _interruptible_sleep(seconds: float) -> None:
    """Sleep for `seconds` but wake immediately if stop_event is set."""
    _stop_event.wait(timeout=seconds)


# ── Build specs from .env ──────────────────────────────────────────────────
def _build_specs() -> tuple[list[ServiceSpec], ServiceSpec]:
    """Returns (plain_specs, qbit_spec)."""
    vlc_url     = _e("VLC_URL",      "http://localhost:8080")
    qbit_url    = _e("QBIT_URL",     "http://localhost:8081")
    jackett_url = _e("INDEXER_URL",  "http://localhost:9117")
    vlc_pwd     = _e("VLC_PASSWORD", "vlcpassword")

    vlc_port  = _extract_port(vlc_url,  8080)
    qbit_port = _extract_port(qbit_url, 8081)

    m = re.search(r"https?://([^:/]+)", jackett_url)
    jackett_host     = m.group(1) if m else "127.0.0.1"
    jackett_port     = _extract_port(jackett_url, 9117)
    jackett_is_local = jackett_host in ("localhost", "127.0.0.1", "::1")

    # Smart Skip countdown popup: VLC reads this file via a marq sub-source and
    # renders it bottom-right on the TV. main.py writes "Skipping … in N" here.
    # Keep these args in sync with main.py's _vlc_marquee_args() and run.py.
    # A lone space, not "" — marq's getline() treats an empty file as EOF and
    # logs a read error every refresh tick. A space reads fine and shows nothing.
    marquee_file = HERE / ".vlc_marquee.txt"
    try:
        marquee_file.write_text(" ", encoding="utf-8")
    except OSError:
        pass

    vlc_spec = ServiceSpec(
        name="VLC",
        port=vlc_port,
        host="127.0.0.1",
        find_bin=find_vlc,
        build_args=lambda b: [
            b,
            "--extraintf=http",
            "--http-host=localhost",
            f"--http-port={vlc_port}",
            f"--http-password={vlc_pwd}",
            "--no-random",
            # Start fullscreen so the idle background video covers the screen
            # on boot. Critical for the system-service path: at login, the
            # post-`in_play` HTTP fullscreen toggle is unreliable because the
            # desktop and VLC window aren't ready, so we ask VLC to come up
            # fullscreen from the start instead of toggling later.
            "--fullscreen",
            # Bottom-right opaque countdown marquee (text only — no subtitle box).
            "--sub-source=marq",
            f"--marq-file={marquee_file}",
            "--marq-refresh=200",
            "--marq-position=10",
            "--marq-x=50", "--marq-y=50",
            "--marq-size=48",
            "--marq-color=16777215",
            "--marq-opacity=255",
            "--marq-timeout=0",
            # Night-mode compressor (empty list when the setting is off). Read at
            # each (re)launch so a crash-recovery relaunch honours the toggle.
            *night_mode_args(),
        ],
        startup_timeout=20.0,
        back_off=5.0,
    )

    qbit_spec = ServiceSpec(
        name="qBittorrent",
        port=qbit_port,
        host="127.0.0.1",
        find_bin=find_qbit,
        build_args=lambda b: [b],
        startup_timeout=30.0,
        back_off=5.0,
    )

    plain_specs: list[ServiceSpec] = [vlc_spec]

    if jackett_is_local:
        jackett_base = jackett_url.rstrip("/")

        def _jackett_alive() -> bool:
            # Port must be open AND Jackett must actually answer HTTP. /UI/Login
            # is served without auth, so any HTTP response means it's healthy.
            if not _port_open(jackett_port, jackett_host, timeout=0.5):
                return False
            return _http_ok(f"{jackett_base}/UI/Login", timeout=4.0)

        def _jackett_force_down() -> None:
            # Clear a hung/zombie Jackett before relaunching so the port frees.
            if SYSTEM == "Windows":
                _force_stop_jackett_windows()
            else:
                _kill_by_name("jackett")
                _stop_event.wait(1.0)

        plain_specs.append(ServiceSpec(
            name="Jackett",
            port=jackett_port,
            host=jackett_host,
            find_bin=find_jackett,
            build_args=_build_jackett_args,
            startup_timeout=40.0,   # mono/.NET cold start can be slow to serve
            back_off=5.0,
            health_check=_jackett_alive,
            pre_restart=_jackett_force_down,
        ))

    return plain_specs, qbit_spec


# ── Public API ─────────────────────────────────────────────────────────────
def start_watchdog() -> threading.Thread:
    """
    Start the watchdog in a daemon thread.
    Call this from run.py after all services have been launched.
    Returns the thread (callers can ignore it).
    """
    plain_specs, qbit_spec = _build_specs()
    thread = threading.Thread(
        target=_watchdog_loop,
        args=(plain_specs, qbit_spec),
        daemon=True,
        name="streamlink-watchdog",
    )
    thread.start()
    return thread


def stop_watchdog() -> None:
    """Signal the watchdog loop to exit (used in tests / clean shutdown)."""
    _stop_event.set()


def jackett_healthy() -> bool:
    """True if a local Jackett is actually serving HTTP (not just port-open).

    Returns True for a remote Jackett (we can't manage it, so don't report it as
    locally unhealthy). Safe to call from the dashboard process.
    """
    plain_specs, _ = _build_specs()
    for spec in plain_specs:
        if spec.name == "Jackett":
            return spec.is_alive()
    return True   # Jackett isn't locally managed (remote URL) — nothing to report


def restart_jackett() -> bool:
    """Force a hung/stopped local Jackett down and start it again.

    Reuses the same force-down + launch + HTTP-readiness wait the watchdog uses.
    Intended as a backstop the dashboard can call when its own health monitor
    sees Jackett wedged and no watchdog has recovered it. No-op (returns False)
    for a remote Jackett.
    """
    plain_specs, _ = _build_specs()
    for spec in plain_specs:
        if spec.name == "Jackett":
            log.warning("Manual Jackett restart requested.")
            return spec.start()
    log.info("restart_jackett: Jackett is remote / not locally managed — skipping.")
    return False


# ── Standalone entry-point (used by the daemon service) ───────────────────
if __name__ == "__main__":
    _stop_event.clear()
    plain_specs, qbit_spec = _build_specs()
    try:
        _watchdog_loop(plain_specs, qbit_spec)
    except KeyboardInterrupt:
        log.info("Watchdog interrupted by user.")
