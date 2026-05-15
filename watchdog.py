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
        r = subprocess.run(["sc", "start", "Jackett"], capture_output=True, text=True, timeout=10)
    except Exception:
        return False
    out = (r.stdout + r.stderr).upper()
    if r.returncode == 0:
        return True
    if "1056" in out or "ALREADY" in out:
        return True
    return False


def _build_jackett_args(bin_path: str) -> list[str]:
    """Return the launch args for the Jackett binary.

    On Windows the canonical Jackett service is launched via `sc start` first
    (the service serves port 9117); the tray exe is then started so the user
    has a visible icon. `--NoRestart` is only valid for `JackettConsole.exe`.
    """
    if SYSTEM == "Windows":
        _start_jackett_service_windows()
        if Path(bin_path).name.lower() == "jacketttray.exe":
            return [bin_path]
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
    ):
        self.name            = name
        self.port            = port
        self.host            = host
        self.find_bin        = find_bin
        self.build_args      = build_args
        self.startup_timeout = startup_timeout
        self.back_off        = back_off
        self._failures       = 0
        self._MAX_BACK_OFF   = 120.0

    def is_alive(self) -> bool:
        return _port_open(self.port, self.host, timeout=0.5)

    def start(self) -> bool:
        bin_path = self.find_bin()
        if not bin_path:
            log.warning("%s binary not found — cannot start", self.name)
            return False
        args = self.build_args(bin_path)
        log.info("Starting %s: %s", self.name, " ".join(str(a) for a in args))
        _launch_bg(args)
        alive = _wait_port(self.port, self.host, self.startup_timeout)
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
        plain_specs.append(ServiceSpec(
            name="Jackett",
            port=jackett_port,
            host="127.0.0.1",
            find_bin=find_jackett,
            build_args=_build_jackett_args,
            startup_timeout=25.0,
            back_off=5.0,
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


# ── Standalone entry-point (used by the daemon service) ───────────────────
if __name__ == "__main__":
    _stop_event.clear()
    plain_specs, qbit_spec = _build_specs()
    try:
        _watchdog_loop(plain_specs, qbit_spec)
    except KeyboardInterrupt:
        log.info("Watchdog interrupted by user.")
