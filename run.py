#!/usr/bin/env python3
"""
StreamLink Launcher — starts all services then the dashboard.
    python3 run.py              # normal interactive launch
    python3 run.py --install    # register as a system service (7.1)
    python3 run.py --uninstall  # remove the system service
    python3 run.py --status     # show service registration status

This script auto-relaunches itself inside the virtualenv so psutil
and other venv packages are available without manual activation.
"""
from __future__ import annotations

import asyncio
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

HERE   = Path(__file__).parent
VENV   = HERE / ".venv"
SYSTEM = platform.system()

# ── Auto-relaunch inside venv (transparent to the user) ───────────────────
# Use sys.prefix rather than comparing resolved executable paths: two different
# Python invocations can resolve to the same binary yet have different site-packages
# (system vs. venv). sys.prefix is always set to the venv root when inside one.
_VENV_PY = VENV / ("Scripts/python.exe" if SYSTEM == "Windows" else "bin/python")
if _VENV_PY.exists() and Path(sys.prefix).resolve() != VENV.resolve():
    os.execv(str(_VENV_PY), [str(_VENV_PY)] + sys.argv)

# ── Verbose flag (consume early so existing arg handling is unaffected) ──
VERBOSE = any(a in ("-v", "--verbose") for a in sys.argv[1:])
if VERBOSE:
    sys.argv = [sys.argv[0]] + [a for a in sys.argv[1:] if a not in ("-v", "--verbose")]

# ── Color output ──────────────────────────────────────────────────────────
_TTY = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()
def _c(n): return f"\033[{n}m" if _TTY else ""
RESET = _c(0); BOLD = _c(1)
RED = _c(91); GRN = _c(92); YLW = _c(93); BLU = _c(94); CYN = _c(96); MAG = _c(95)

def ok(t):   print(f"  {GRN}✓{RESET}  {t}")
def warn(t): print(f"  {YLW}⚠{RESET}  {t}")
def fail(t): print(f"  {RED}✗{RESET}  {t}")
def info(t): print(f"  {BLU}→{RESET}  {t}")
def vlog(t):
    if VERBOSE:
        print(f"  {MAG}[v]{RESET} {t}")


def vrun(cmd, **kwargs) -> subprocess.CompletedProcess:
    """subprocess.run + verbose dump of the command and its output."""
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    if VERBOSE:
        vlog(f"$ {' '.join(str(c) for c in cmd)}")
    proc = subprocess.run(cmd, **kwargs)
    if VERBOSE:
        vlog(f"  exit={proc.returncode}")
        for line in (proc.stdout or "").strip().splitlines():
            vlog(f"  stdout: {line}")
        for line in (proc.stderr or "").strip().splitlines():
            vlog(f"  stderr: {line}")
    return proc


# ── Load .env ─────────────────────────────────────────────────────────────
def load_env(path: Path) -> dict:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip()
    return env


_ENV = load_env(HERE / ".env")


def e(key: str, default: str = "") -> str:
    return _ENV.get(key, os.environ.get(key, default))


# ── Network helpers ────────────────────────────────────────────────────────
def port_open(port: int, host: str = "127.0.0.1") -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.8):
            return True
    except OSError:
        return False


def wait_for_port(port: int, timeout: float, label: str, host: str = "127.0.0.1") -> bool:
    print(f"  {BOLD}Waiting for {label}{RESET}", end="", flush=True)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if port_open(port, host):
            elapsed = timeout - (deadline - time.monotonic())
            print(f"  {GRN}ready{RESET} ({elapsed:.1f}s)")
            return True
        print(".", end="", flush=True)
        time.sleep(0.5)
    print(f"  {RED}timed out{RESET}")
    return False


def http_ok(url: str, timeout: float = 4.0) -> bool:
    """True if `url` returns any HTTP response within `timeout`.

    Stronger than a port check for Jackett: a hung Jackett keeps its listener
    socket open while no longer serving, so a bare port-open would wrongly
    report it reachable. Any HTTP status proves the web stack is answering.
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


def extract_port(url: str, default: int) -> int:
    m = re.search(r":(\d+)", url)
    return int(m.group(1)) if m else default


# ── Process helpers ────────────────────────────────────────────────────────
def kill_by_name(name: str) -> int:
    """Kill all processes whose name starts with `name`. Returns count killed."""
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


def is_running(name: str) -> bool:
    import psutil
    name_l = name.lower()
    for p in psutil.process_iter(["name"]):
        if (p.info["name"] or "").lower().startswith(name_l):
            return True
    return False


def launch_bg(args: list[str]) -> None:
    """Start a process detached from this terminal."""
    kw: dict = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if SYSTEM == "Windows":
        kw["creationflags"] = (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        )
    else:
        kw["start_new_session"] = True
    subprocess.Popen(args, **kw)


# ── Executable discovery ───────────────────────────────────────────────────
def find_vlc() -> str | None:
    saved = e("_VLC_BIN")
    if saved and Path(saved).exists():
        return saved
    candidates = {
        "Darwin":  ["/Applications/VLC.app/Contents/MacOS/VLC", "vlc"],
        "Windows": [r"C:\Program Files\VideoLAN\VLC\vlc.exe",
                    r"C:\Program Files (x86)\VideoLAN\VLC\vlc.exe"],
    }.get(SYSTEM, ["/usr/bin/vlc", "vlc"])
    for c in candidates:
        p = Path(c)
        if p.exists():
            return str(p)
        found = shutil.which(c)
        if found:
            return found
    return None


def find_qbit() -> str | None:
    saved = e("_QBIT_BIN")
    if saved and Path(saved).exists():
        return saved
    candidates = {
        "Darwin":  ["/Applications/qbittorrent.app/Contents/MacOS/qbittorrent", "qbittorrent"],
        "Windows": [r"C:\Program Files\qBittorrent\qbittorrent.exe"],
    }.get(SYSTEM, ["/usr/bin/qbittorrent", "qbittorrent"])
    for c in candidates:
        p = Path(c)
        if p.exists():
            return str(p)
        found = shutil.which(c)
        if found:
            return found
    return None


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


def find_jackett() -> str | None:
    saved = e("_JACKETT_BIN")
    if saved and Path(saved).exists():
        return saved
    candidates = {
        "Darwin":  [
            "/Applications/Jackett/JackettConsole",
            "/opt/homebrew/opt/jackett/bin/jackett",
            str(Path.home() / "Downloads/Jackett/jackett"),
            "jackett",
        ],
        "Windows": _windows_jackett_candidates(),
    }.get(SYSTEM, [str(Path.home() / "Downloads/Jackett/jackett"), "jackett"])
    for c in candidates:
        p = Path(c)
        if p.exists():
            return str(p)
        found = shutil.which(c)
        if found:
            return found
    return None


def find_mullvad() -> str | None:
    saved = e("_MULLVAD_BIN")
    if saved and Path(saved).exists():
        return saved
    candidates = {
        "Darwin":  ["/Applications/Mullvad VPN.app/Contents/Resources/mullvad", "mullvad"],
        "Windows": [r"C:\Program Files\Mullvad VPN\resources\mullvad.exe", "mullvad"],
    }.get(SYSTEM, ["mullvad"])
    for c in candidates:
        p = Path(c)
        if p.exists():
            return str(p)
        found = shutil.which(c)
        if found:
            return found
    return None


# ── Service launchers ──────────────────────────────────────────────────────
def start_vlc() -> bool:
    vlc_port = extract_port(e("VLC_URL", "http://localhost:8080"), 8080)
    vlc_pwd  = e("VLC_PASSWORD", "vlcpassword")

    if port_open(vlc_port):
        ok(f"VLC HTTP interface already on port {vlc_port}")
        return True

    vlc_bin = find_vlc()
    if not vlc_bin:
        fail("VLC not found — install from https://videolan.org/vlc/ and re-run setup.py")
        return False

    # If VLC is running without HTTP we need to restart it
    if is_running("VLC") or is_running("vlc"):
        info("VLC running without HTTP interface — restarting with HTTP enabled…")
        kill_by_name("vlc")
        kill_by_name("VLC")
        time.sleep(1.5)

    # Smart Skip countdown popup: VLC reads this file via a marq sub-source and
    # renders it bottom-right on the TV. main.py writes "Skipping … in N" here.
    # Create it empty so marq has something to open at launch. Keep the args in
    # sync with main.py's _vlc_marquee_args() and watchdog.py's vlc_spec.
    # A lone space, not "" — marq's getline() treats an empty file as EOF and
    # logs a read error every refresh tick. A space reads fine and shows nothing.
    marquee_file = HERE / ".vlc_marquee.txt"
    try:
        marquee_file.write_text(" ", encoding="utf-8")
    except OSError:
        pass

    info(f"Starting VLC (Lua HTTP on port {vlc_port}) …")
    launch_bg([
        vlc_bin,
        "--extraintf=http",
        f"--http-host=localhost",
        f"--http-port={vlc_port}",
        f"--http-password={vlc_pwd}",
        "--no-random",
        # Mirror watchdog.py: come up fullscreen so the idle background video
        # (and any subsequent playback) covers the screen reliably, even at
        # boot when the post-play HTTP fullscreen toggle can race the desktop.
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
    ])
    return wait_for_port(vlc_port, 20.0, "VLC")


def start_qbittorrent() -> bool:
    qbit_port = extract_port(e("QBIT_URL", "http://localhost:8081"), 8081)

    if port_open(qbit_port):
        ok(f"qBittorrent Web UI already on port {qbit_port}")
        return True

    qbit_bin = find_qbit()
    if not qbit_bin:
        fail("qBittorrent not found — install from https://qbittorrent.org/ and re-run setup.py")
        return False

    info("Starting qBittorrent …")
    launch_bg([qbit_bin])
    return wait_for_port(qbit_port, 30.0, "qBittorrent Web UI")


def _start_jackett_service_windows() -> bool:
    """Best-effort: start the 'Jackett' Windows service.

    Returns True if the call succeeded (or the service is already running).
    `sc start` returns 1056 (ALREADY_RUNNING) and 1060 (SERVICE_NOT_INSTALLED)
    — the former counts as success, the latter falls through to launching
    the tray exe as a regular process.
    """
    try:
        r = vrun(["sc.exe", "start", "Jackett"], timeout=10)
    except Exception as exc:
        vlog(f"sc.exe start raised: {exc}")
        return False
    out = (r.stdout + r.stderr).upper()
    if r.returncode == 0:
        return True
    if "1056" in out or "ALREADY" in out:   # already running
        return True
    return False


def _force_stop_jackett_local() -> None:
    """Force a hung local Jackett down so the relaunch below can re-bind 9117.

    On Windows `sc.exe start` is a no-op (1056) on a wedged service, so we stop
    it (waiting for STOPPED, hard-killing if it won't die). Elsewhere we kill
    the process directly.
    """
    if SYSTEM == "Windows":
        vrun(["sc.exe", "stop", "Jackett"], timeout=10)
        for _ in range(12):
            q = vrun(["sc.exe", "query", "Jackett"])
            if q.returncode != 0 or "STOPPED" in (q.stdout or ""):
                return
            time.sleep(1)
        warn("Jackett service did not stop — hard-killing the process")
        kill_by_name("jackett")
    else:
        kill_by_name("jackett")
    time.sleep(1)


def start_jackett() -> bool:
    indexer_url  = e("INDEXER_URL", "http://localhost:9117")
    jackett_port = extract_port(indexer_url, 9117)

    # Parse the host so we can distinguish remote from local
    m = re.search(r"https?://([^:/]+)", indexer_url)
    jackett_host = m.group(1) if m else "127.0.0.1"
    is_local     = jackett_host in ("localhost", "127.0.0.1", "::1")

    # "Reachable" means actually serving HTTP, not just an open port — a hung
    # Jackett keeps the port bound but stops answering, and that's the failure
    # mode the watchdog must be able to recover from.
    base = indexer_url.rstrip("/")
    if http_ok(f"{base}/UI/Login"):
        prefix = "Remote Jackett" if not is_local else "Jackett"
        ok(f"{prefix} reachable at {indexer_url}")
        return True

    if not is_local:
        # Remote instance — we can't launch it from here
        warn(f"Jackett not reachable at {indexer_url}")
        warn("Start Jackett on the remote machine, then retry")
        return False

    # Local instance that isn't serving. If the port is still held open it's a
    # hung process — clear it first so the launch below can take the port.
    if port_open(jackett_port, jackett_host):
        warn("Jackett port is open but not serving — forcing a restart")
        _force_stop_jackett_local()

    # Local instance — try to launch it
    jackett_bin = find_jackett()
    if not jackett_bin:
        warn("Jackett not found locally — search will be unavailable")
        info("Download: https://github.com/Jackett/Jackett/releases")
        return False
    vlog(f"Jackett binary resolved to: {jackett_bin}")

    if SYSTEM == "Windows":
        # The Windows installer registers a "Jackett" service that actually
        # serves port 9117. Start that — it's idempotent. The tray exe is
        # purely cosmetic and launched once if not already running.
        q = vrun(["sc.exe", "query", "Jackett"])
        service_installed = q.returncode == 0
        vlog(f"Jackett service installed: {service_installed}")
        if VERBOSE and service_installed:
            vrun(["sc.exe", "qc", "Jackett"])      # show binPath
            try:
                ns = subprocess.run(["netstat", "-ano", "-p", "TCP"],
                                    capture_output=True, text=True, timeout=5)
                hits = [l for l in ns.stdout.splitlines() if ":9117" in l]
                if hits:
                    vlog("Port 9117 bindings:")
                    for l in hits:
                        vlog(f"  {l.strip()}")
                else:
                    vlog("Nothing currently listening on port 9117.")
            except Exception as exc:
                vlog(f"netstat failed: {exc}")
        if service_installed and _start_jackett_service_windows():
            info("Started Jackett Windows service")
            if VERBOSE:
                _diagnose_jackett_service_state()
        elif not service_installed:
            warn("Jackett Windows service is not installed.")
            info("Re-run 'python setup.py' from an Administrator PowerShell to install it,")
            info("or open the Jackett tray icon → 'Start background service'.")
        tray_running = is_running("JackettTray")
        vlog(f"JackettTray running: {tray_running}")
        if Path(jackett_bin).name.lower() == "jacketttray.exe" and not tray_running:
            info("Launching JackettTray.exe …")
            launch_bg([jackett_bin])
    else:
        info("Starting Jackett …")
        launch_bg([jackett_bin, "--NoRestart"])
    return wait_for_port(jackett_port, 25.0, "Jackett", jackett_host)


def _diagnose_jackett_service_state() -> None:
    """Poll `sc query Jackett` for a few seconds and report crash exit code.

    The service can register, accept `sc start`, transition to START_PENDING,
    and then crash — visible only on a follow-up query (state back to STOPPED
    with a non-zero WIN32_EXIT_CODE / SERVICE_EXIT_CODE). Also tail Jackett's
    own log to surface the actual error.
    """
    vlog("Polling service state for 8s after start …")
    for i in range(8):
        time.sleep(1)
        r = subprocess.run(["sc.exe", "query", "Jackett"], capture_output=True, text=True)
        state = ""
        for line in r.stdout.splitlines():
            if "STATE" in line and ":" in line:
                state = line.split(":", 1)[1].strip()
                break
        vlog(f"  t+{i+1}s: STATE = {state}")
        if "STOPPED" in state:
            for line in r.stdout.splitlines():
                ls = line.strip()
                if ls.startswith(("WIN32_EXIT_CODE", "SERVICE_EXIT_CODE")):
                    vlog(f"  {ls}")
            break
        if "RUNNING" in state:
            return
    # The service runs as LocalSystem, so its data folder is under
    # C:\Windows\System32\config\systemprofile — NOT the interactive user's
    # AppData. Search both, plus ProgramData (the installer default).
    sysprofile = Path(r"C:\Windows\System32\config\systemprofile")
    candidates = [
        Path(r"C:\ProgramData\Jackett") / "log.txt",
        sysprofile / "AppData" / "Roaming" / "Jackett" / "log.txt",
        sysprofile / "AppData" / "Local"   / "Jackett" / "log.txt",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Jackett" / "log.txt",
        Path(os.environ.get("APPDATA", ""))     / "Jackett" / "log.txt",
    ]
    # Locate Jackett's config dir(s) — knowing which one the service uses
    # matters because LocalSystem has its own AppData under
    # C:\Windows\System32\config\systemprofile, not the interactive user.
    vlog("Searching for Jackett ServerConfig.json …")
    cfg_candidates = [
        Path(r"C:\ProgramData\Jackett"),
        sysprofile / "AppData" / "Roaming" / "Jackett",
        sysprofile / "AppData" / "Local"   / "Jackett",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Jackett",
        Path(os.environ.get("APPDATA", ""))     / "Jackett",
    ]
    for d in cfg_candidates:
        cfg = d / "ServerConfig.json"
        vlog(f"  {'FOUND' if cfg.exists() else '   - '} {cfg}")

    vlog("Looking for Jackett log file …")
    found = False
    for p in candidates:
        try:
            if p.exists():
                vlog(f"  FOUND {p}")
                lines = p.read_text(encoding="utf-8", errors="replace").splitlines()[-40:]
                vlog(f"  Last {len(lines)} lines:")
                for line in lines:
                    vlog(f"    | {line}")
                found = True
                break
            else:
                vlog(f"     -  {p}")
        except Exception as exc:
            vlog(f"  Could not read {p}: {exc}")
    if not found:
        vlog("No Jackett log.txt found in any standard location.")

    # Pull the most recent Service Control Manager error from the event log
    vlog("Querying Application/System event log for recent Jackett errors …")
    ps_cmd = (
        "Get-WinEvent -FilterHashtable @{LogName='System'; ProviderName='Service Control Manager'} "
        "-MaxEvents 50 | Where-Object { $_.Message -match 'Jackett' } | "
        "Select-Object -First 5 | Format-List TimeCreated, LevelDisplayName, Message"
    )
    try:
        ev = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_cmd],
            capture_output=True, text=True, timeout=15,
        )
        out = (ev.stdout or "").strip()
        if out:
            for line in out.splitlines():
                vlog(f"  evt: {line}")
        else:
            vlog("  (no Jackett-related events in last 50 SCM entries)")
    except Exception as exc:
        vlog(f"  event log query failed: {exc}")


def check_mullvad() -> bool:
    mullvad_bin = find_mullvad()
    if not mullvad_bin:
        warn("Mullvad CLI not found — VPN kill-switch disabled")
        return False
    try:
        result = subprocess.run(
            [mullvad_bin, "status"],
            capture_output=True, text=True, timeout=5,
        )
        first = result.stdout.strip().splitlines()[0] if result.stdout.strip() else "Unknown"
        if "Connected" in result.stdout:
            ok(f"Mullvad: {first}")
            return True
        else:
            warn(f"Mullvad: {first}")
            warn("Connect to Mullvad VPN before streaming (required for kill-switch guard)")
            return False
    except Exception as exc:
        warn(f"Mullvad check failed: {exc}")
        return False


# ── Network info ───────────────────────────────────────────────────────────
def get_local_ip() -> str:
    """Return the LAN IP phones should use to connect."""
    _VPN_STARTS = ("utun", "tun", "tap", "wg", "ppp", "lo")
    _VPN_SUBS   = ("mullvad", "wireguard", "vpn", "virtual",
                   "vmware", "vbox", "hyper-v", "loopback")

    def _is_lan(ip: str) -> bool:
        return (ip.startswith("192.168.") or
                ip.startswith("10.") or
                bool(re.match(r"^172\.(1[6-9]|2\d|3[01])\.", ip)))

    # Subnets used by virtual adapters whose friendly names don't always reveal
    # them on Windows (e.g. VirtualBox Host-Only shows up as "Ethernet 2").
    # These IPs route only to the owning host — never advertise on mDNS.
    _VIRTUAL_PREFIXES = (
        "192.168.56.",   # VirtualBox host-only default
        "192.168.99.",   # Docker Machine default
        "192.168.137.",  # Windows ICS / Mobile Hotspot default
        "169.254.",      # APIPA / link-local
    )

    def _is_virtual(ip: str) -> bool:
        return any(ip.startswith(p) for p in _VIRTUAL_PREFIXES)

    # Step 1: enumerate physical interfaces, dropping VPN and virtual adapters
    # by name AND by IP subnet. This yields the set of IPs we'd ever want to
    # advertise on mDNS.
    candidates: list[tuple[int, str]] = []  # (priority, ip)
    try:
        import psutil
        for iface, addrs in psutil.net_if_addrs().items():
            n = iface.lower()
            if any(n.startswith(p) for p in _VPN_STARTS):
                continue
            if any(p in n for p in _VPN_SUBS):
                continue
            for addr in addrs:
                if addr.family != socket.AF_INET:
                    continue
                ip = addr.address
                if not _is_lan(ip) or _is_virtual(ip):
                    continue
                if ip.startswith("192.168."):
                    candidates.append((0, ip))
                elif ip.startswith("10."):
                    candidates.append((1, ip))
                else:
                    candidates.append((2, ip))
    except Exception:
        pass

    if not candidates:
        return ""

    candidate_ips = {ip for _, ip in candidates}

    # Step 2: prefer whichever candidate the OS routing table picks. The UDP
    # connect() doesn't send a packet but sets the source IP to whatever
    # interface would reach the destination — useful on multi-NIC hosts. We
    # gate it through `candidate_ips` so an active VPN (e.g. Mullvad capturing
    # the default route) can never win.
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        if ip in candidate_ips:
            return ip
    except Exception:
        pass

    candidates.sort()
    return candidates[0][1]


def get_wifi_ssid() -> str:
    """Best-effort: return the current Wi-Fi SSID or empty string."""
    try:
        if SYSTEM == "Darwin":
            airport = (
                "/System/Library/PrivateFrameworks/Apple80211.framework"
                "/Versions/Current/Resources/airport"
            )
            r = subprocess.run([airport, "-I"], capture_output=True, text=True, timeout=5)
            for line in r.stdout.splitlines():
                line = line.strip()
                if re.match(r"^SSID\s*:", line) and "BSSID" not in line:
                    return line.split(":", 1)[1].strip()
            for iface in ("en0", "en1", "en2"):
                r = subprocess.run(
                    ["networksetup", "-getairportnetwork", iface],
                    capture_output=True, text=True, timeout=5,
                )
                if "Current Wi-Fi Network:" in r.stdout:
                    return r.stdout.split("Current Wi-Fi Network:", 1)[1].strip()
        elif SYSTEM == "Linux":
            r = subprocess.run(["iwgetid", "-r"], capture_output=True, text=True, timeout=5)
            if r.stdout.strip():
                return r.stdout.strip()
        elif SYSTEM == "Windows":
            r = subprocess.run(
                ["netsh", "wlan", "show", "interfaces"],
                capture_output=True, text=True, timeout=5,
            )
            for line in r.stdout.splitlines():
                if "SSID" in line and "BSSID" not in line:
                    return line.split(":", 1)[1].strip()
    except Exception:
        pass
    return ""


# ── Windows Firewall ──────────────────────────────────────────────────────
def setup_windows_firewall(port: int) -> None:
    """Add inbound firewall rules for the dashboard port and mDNS (idempotent)."""
    rules = [
        (f"StreamLink HTTP {port}", "TCP", str(port)),
        ("StreamLink mDNS",         "UDP", "5353"),
    ]
    for name, proto, lport in rules:
        check = subprocess.run(
            ["netsh", "advfirewall", "firewall", "show", "rule", f"name={name}"],
            capture_output=True, text=True,
        )
        if check.returncode == 0 and name in check.stdout:
            ok(f"Firewall rule already exists: {name}")
            continue
        result = subprocess.run(
            [
                "netsh", "advfirewall", "firewall", "add", "rule",
                f"name={name}", f"protocol={proto}", "dir=in",
                f"localport={lport}", "action=allow",
            ],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            ok(f"Firewall: allowed inbound {proto} port {lport} ({name})")
        else:
            warn(f"Firewall rule failed for {name} — run as Administrator to add it")
            warn(f"  netsh advfirewall firewall add rule name=\"{name}\" protocol={proto} dir=in localport={lport} action=allow")


# ── mDNS ──────────────────────────────────────────────────────────────────
def start_mdns(lan_ip: str, http_port: int, https_port: int = 0):
    """Register remote.local via mDNS (HTTP, and optionally HTTPS) for LAN access."""
    try:
        from zeroconf import ServiceInfo, Zeroconf
        import socket as _socket
        zc = Zeroconf(interfaces=[lan_ip])
        addr = [_socket.inet_aton(lan_ip)]

        http_info = ServiceInfo(
            "_http._tcp.local.",
            "StreamLink._http._tcp.local.",
            addresses=addr,
            port=http_port,
            properties={},
            server="remote.local.",
        )
        zc.register_service(http_info)
        ok(f"mDNS: http://remote.local registered")

        if https_port:
            https_info = ServiceInfo(
                "_https._tcp.local.",
                "StreamLink Admin._https._tcp.local.",
                addresses=addr,
                port=https_port,
                properties={},
                server="remote.local.",
            )
            zc.register_service(https_info)
            ok(f"mDNS: https://remote.local registered (admin panel)")

        return zc
    except ImportError:
        warn("zeroconf not installed — run python3 setup.py to update dependencies")
        return None
    except Exception as exc:
        warn(f"mDNS registration failed: {exc}")
        return None


def start_mdns_resilient(http_port: int, https_port: int = 0,
                         poll_interval: float = 5.0, watch_interval: float = 30.0):
    """Register remote.local via mDNS, resilient to a late or changing LAN IP.

    A one-shot ``start_mdns()`` registers whatever ``get_local_ip()`` returns
    *right now*. That's fine interactively (the network is already up), but the
    installed launchd/systemd service starts at login *before* Wi-Fi has
    associated — ``get_local_ip()`` returns "" and the registration is silently
    skipped, so ``remote.local`` never resolves until the next manual relaunch
    even though uvicorn (bound to ``0.0.0.0``) becomes reachable by IP the
    moment the network comes up. That's why "remote.local works on first
    install but not after a reboot." See docs/GOTCHAS.md.

    This runs a daemon thread that waits for a LAN IP, registers, then
    re-registers if the IP later changes (DHCP lease, network switch). It polls
    every ``poll_interval`` until registered, then every ``watch_interval`` to
    watch for changes. Returns a handle with ``.close()`` for shutdown cleanup;
    safe to call even when zeroconf is missing.
    """
    import threading

    class _MdnsKeepalive:
        def __init__(self) -> None:
            self._zc = None
            self._ip = ""
            self._stop = threading.Event()
            self._thread = threading.Thread(
                target=self._run, name="mdns-keepalive", daemon=True)
            self._thread.start()

        def _run(self) -> None:
            while not self._stop.is_set():
                ip = get_local_ip()
                if ip and ip != self._ip:
                    if self._zc is not None:
                        try:
                            self._zc.close()
                        except Exception:
                            pass
                        self._zc = None
                    zc = start_mdns(ip, http_port, https_port)
                    if zc is not None:
                        self._zc = zc
                        self._ip = ip
                # poll quickly until registered, then just watch for IP changes
                self._stop.wait(watch_interval if self._zc is not None else poll_interval)

        def close(self) -> None:
            self._stop.set()
            if self._zc is not None:
                try:
                    self._zc.close()
                except Exception:
                    pass

    return _MdnsKeepalive()


# ── Main ───────────────────────────────────────────────────────────────────
def main():
    # ── Handle --install / --uninstall / --status flags (7.1) ────────────
    if len(sys.argv) > 1:
        flag = sys.argv[1].lstrip("-").lower()
        if flag in ("install", "uninstall", "status"):
            # These may be called before the venv is active; ensure it is.
            if not VENV.exists():
                print(f"\n  Run python3 setup.py first.")
                sys.exit(1)
            from daemon import install, uninstall, status
            if flag == "install":
                sys.exit(0 if install() else 1)
            elif flag == "uninstall":
                sys.exit(0 if uninstall() else 1)
            else:
                status()
                sys.exit(0)
        else:
            print(f"Unknown flag: {sys.argv[1]}")
            print("Usage: python3 run.py [--install | --uninstall | --status] [-v|--verbose]")
            sys.exit(1)

    # Pre-flight checks
    if not VENV.exists():
        print(f"\n  {RED}✗{RESET}  Virtual environment not found.")
        print(f"  Run {BOLD}python3 setup.py{RESET} first.")
        sys.exit(1)

    if not (HERE / ".env").exists():
        print(f"\n  {RED}✗{RESET}  .env not found.")
        print(f"  Run {BOLD}python3 setup.py{RESET} first.")
        sys.exit(1)

    try:
        import psutil  # noqa: F401
    except ModuleNotFoundError:
        print(f"\n  {RED}✗{RESET}  Python packages not installed in the virtualenv.")
        print(f"  The venv exists but pip install did not complete.")
        print(f"  Run {BOLD}python3 setup.py{RESET} again to finish installation.")
        sys.exit(1)

    print(f"\n{BOLD}{CYN}  StreamLink v2.0 — Starting up{RESET}\n")

    # ── Services ──────────────────────────────────────────────────────────
    print(f"{BOLD}  Services{RESET}")
    vlc_ok     = start_vlc()
    mullvad_ok = check_mullvad()
    _          = start_jackett()          # optional; don't block on failure

    if not mullvad_ok:
        # No-stdin context (system service / piped invocation) → continue
        # without prompting. The watchdog gates qBit on VPN status, so
        # silently proceeding is safe — qBit won't actually launch until
        # Mullvad reconnects.
        interactive = bool(getattr(sys.stdin, "isatty", lambda: False)())
        if interactive:
            print(f"  {YLW}Continue anyway? VPN kill-switch will be inactive. [y/N]{RESET} ", end="")
            try:
                answer = input().strip().lower()
            except (EOFError, KeyboardInterrupt):
                answer = "n"
            if not answer.startswith("y"):
                print()
                info("Connect Mullvad and re-run.")
                sys.exit(0)
            print()
        else:
            info("Mullvad disconnected and stdin is non-interactive — continuing; watchdog will start qBit once VPN reconnects.")
        info("qBittorrent will not start — watchdog will launch it once Mullvad connects")
        qbit_ok = True   # intentionally skipped; watchdog gates it
    else:
        qbit_ok = start_qbittorrent()
    print()

    if not vlc_ok or not qbit_ok:
        fail("Required services could not start. Fix the errors above and try again.")

    # ── Start watchdog (7.2) ──────────────────────────────────────────────
    try:
        from watchdog import start_watchdog
        if VERBOSE:
            import logging as _logging
            _logging.getLogger("streamlink.watchdog").setLevel(_logging.DEBUG)
            _logging.getLogger().setLevel(_logging.DEBUG)
        start_watchdog()
        ok("Watchdog started — VLC, qBittorrent, and Jackett will be auto-restarted if they crash")
    except ImportError:
        warn("watchdog.py not found — auto-restart disabled")
    print()

    # ── Launch dashboard ──────────────────────────────────────────────────
    PORT          = 80
    ADMIN_PORT    = 443
    CERT          = HERE / "cert.pem"
    KEY           = HERE / "key.pem"
    local_url     = "http://127.0.0.1"
    mdns_url      = "http://remote.local"
    lan_ip        = get_local_ip()
    lan_url       = f"http://{lan_ip}" if lan_ip else ""
    ssid          = get_wifi_ssid()
    uvicorn_bin   = VENV / ("Scripts/uvicorn.exe" if SYSTEM == "Windows" else "bin/uvicorn")
    has_cert      = CERT.exists() and KEY.exists()

    print(f"{BOLD}  Dashboard{RESET}")
    ok(f"Local  →  {CYN}{local_url}{RESET}")
    if lan_url:
        ssid_note = f"  {BLU}({ssid}){RESET}" if ssid else ""
        ok(f"Phone  →  {CYN}{mdns_url}{RESET}  or  {CYN}{lan_url}{RESET}{ssid_note}")
        if ssid:
            print(f"          {BLU}Connect your phone to Wi-Fi: {BOLD}{ssid}{RESET}")
    else:
        warn("Could not detect LAN IP — phone access may not work")

    if has_cert:
        ok(f"Admin  →  {CYN}https://127.0.0.1/admin{RESET}  or  {CYN}https://remote.local/admin{RESET}")
    else:
        warn("No SSL cert found — admin panel served over HTTP only. Run setup.py to generate cert.")

    for port in (PORT, ADMIN_PORT if has_cert else None):
        if port and port < 1024 and SYSTEM in ("Darwin", "Linux"):
            try:
                if os.geteuid() != 0:
                    warn(f"Port {port} is privileged — if startup fails, retry with: sudo python3 run.py")
                    break
            except AttributeError:
                break

    info("Press Ctrl+C to stop")
    print()

    # ── Windows: open firewall for port 80, 443, and mDNS ────────────────
    if SYSTEM == "Windows":
        print(f"{BOLD}  Firewall{RESET}")
        setup_windows_firewall(PORT)
        if has_cert:
            setup_windows_firewall(ADMIN_PORT)
        print()

    # Register mDNS hostname (remote.local) for HTTP + HTTPS. Resilient: waits
    # for the LAN IP and re-registers if it changes, so it works even when
    # launched before Wi-Fi is up. See start_mdns_resilient.
    print(f"{BOLD}  mDNS{RESET}")
    _zc = start_mdns_resilient(PORT, ADMIN_PORT if has_cert else 0)
    print()

    # ── Launch dashboard (single process so HTTP + HTTPS share AppState) ──
    # Both servers run in the same asyncio event loop, so module-level state
    # in main.py is shared. The HTTPS server uses lifespan="off" to avoid
    # re-running startup hooks (qBit login, background tasks) a second time.
    import uvicorn as _uvicorn
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))

    async def _launch():
        http_cfg = _uvicorn.Config(
            "main:app",
            host="0.0.0.0",
            port=PORT,
            log_level="warning",
        )
        http_srv = _uvicorn.Server(http_cfg)
        http_srv.install_signal_handlers = lambda: None

        coros = [http_srv.serve()]

        if has_cert:
            https_cfg = _uvicorn.Config(
                "main:app",
                host="0.0.0.0",
                port=ADMIN_PORT,
                ssl_certfile=str(CERT),
                ssl_keyfile=str(KEY),
                log_level="warning",
                lifespan="off",
            )
            https_srv = _uvicorn.Server(https_cfg)
            https_srv.install_signal_handlers = lambda: None
            coros.append(https_srv.serve())

        await asyncio.gather(*coros, return_exceptions=True)

    try:
        asyncio.run(_launch())
    except KeyboardInterrupt:
        print(f"\n  {GRN}✓{RESET}  StreamLink stopped.")
    finally:
        if _zc:
            try:
                _zc.close()
            except Exception:
                pass


if __name__ == "__main__":
    main()
