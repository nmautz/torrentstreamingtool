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

import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import time
import webbrowser
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

# ── Color output ──────────────────────────────────────────────────────────
_TTY = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()
def _c(n): return f"\033[{n}m" if _TTY else ""
RESET = _c(0); BOLD = _c(1)
RED = _c(91); GRN = _c(92); YLW = _c(93); BLU = _c(94); CYN = _c(96)

def ok(t):   print(f"  {GRN}✓{RESET}  {t}")
def warn(t): print(f"  {YLW}⚠{RESET}  {t}")
def fail(t): print(f"  {RED}✗{RESET}  {t}")
def info(t): print(f"  {BLU}→{RESET}  {t}")


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
        "Windows": [r"C:\Program Files\Jackett\Jackett.exe"],
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

    info(f"Starting VLC (Lua HTTP on port {vlc_port}) …")
    launch_bg([
        vlc_bin,
        "--extraintf=http",
        f"--http-host=localhost",
        f"--http-port={vlc_port}",
        f"--http-password={vlc_pwd}",
        "--no-random",
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


def start_jackett() -> bool:
    indexer_url  = e("INDEXER_URL", "http://localhost:9117")
    jackett_port = extract_port(indexer_url, 9117)

    # Parse the host so we can distinguish remote from local
    m = re.search(r"https?://([^:/]+)", indexer_url)
    jackett_host = m.group(1) if m else "127.0.0.1"
    is_local     = jackett_host in ("localhost", "127.0.0.1", "::1")

    if port_open(jackett_port, jackett_host):
        prefix = "Remote Jackett" if not is_local else "Jackett"
        ok(f"{prefix} reachable at {indexer_url}")
        return True

    if not is_local:
        # Remote instance — we can't launch it from here
        warn(f"Jackett not reachable at {indexer_url}")
        warn("Start Jackett on the remote machine, then retry")
        return False

    # Local instance — try to launch it
    jackett_bin = find_jackett()
    if not jackett_bin:
        warn("Jackett not found locally — search will be unavailable")
        info("Download: https://github.com/Jackett/Jackett/releases")
        return False

    info("Starting Jackett …")
    launch_bg([jackett_bin, "--NoRestart"])
    return wait_for_port(jackett_port, 25.0, "Jackett", jackett_host)


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

    try:
        import psutil
        buckets: list[list[str]] = [[], [], []]
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
                if not _is_lan(ip):
                    continue
                if ip.startswith("192.168."):
                    buckets[0].append(ip)
                elif ip.startswith("10."):
                    buckets[1].append(ip)
                else:
                    buckets[2].append(ip)
        for bucket in buckets:
            if bucket:
                return bucket[0]
    except Exception:
        pass

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        if _is_lan(ip):
            return ip
    except Exception:
        pass

    return ""


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


# ── mDNS ──────────────────────────────────────────────────────────────────
def start_mdns(lan_ip: str, port: int):
    """Register remote.local via mDNS so LAN devices can reach the dashboard by name."""
    try:
        from zeroconf import ServiceInfo, Zeroconf
        import socket as _socket
        zc = Zeroconf()
        info = ServiceInfo(
            "_http._tcp.local.",
            "StreamLink._http._tcp.local.",
            addresses=[_socket.inet_aton(lan_ip)],
            port=port,
            properties={},
            server="remote.local.",
        )
        zc.register_service(info)
        ok("mDNS: http://remote.local registered")
        return zc
    except ImportError:
        warn("zeroconf not installed — run python3 setup.py to update dependencies")
        return None
    except Exception as exc:
        warn(f"mDNS registration failed: {exc}")
        return None


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
            print("Usage: python3 run.py [--install | --uninstall | --status]")
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
    qbit_ok    = start_qbittorrent()
    _          = start_jackett()          # optional; don't block on failure
    mullvad_ok = check_mullvad()
    print()

    if not vlc_ok or not qbit_ok:
        fail("Required services could not start. Fix the errors above and try again.")

    if not mullvad_ok:
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

    # ── Start watchdog (7.2) ──────────────────────────────────────────────
    try:
        from watchdog import start_watchdog
        start_watchdog()
        ok("Watchdog started — VLC, qBittorrent, and Jackett will be auto-restarted if they crash")
    except ImportError:
        warn("watchdog.py not found — auto-restart disabled")
    print()

    # ── Launch dashboard ──────────────────────────────────────────────────
    PORT          = 80
    local_url     = "http://127.0.0.1"
    mdns_url      = "http://remote.local"
    lan_ip        = get_local_ip()
    lan_url       = f"http://{lan_ip}" if lan_ip else ""
    ssid          = get_wifi_ssid()
    uvicorn_bin   = VENV / ("Scripts/uvicorn.exe" if SYSTEM == "Windows" else "bin/uvicorn")

    print(f"{BOLD}  Dashboard{RESET}")
    ok(f"Local  →  {CYN}{local_url}{RESET}")
    if lan_url:
        ssid_note = f"  {BLU}({ssid}){RESET}" if ssid else ""
        ok(f"Phone  →  {CYN}{mdns_url}{RESET}  or  {CYN}{lan_url}{RESET}{ssid_note}")
        if ssid:
            print(f"          {BLU}Connect your phone to Wi-Fi: {BOLD}{ssid}{RESET}")
    else:
        warn("Could not detect LAN IP — phone access may not work")

    if PORT < 1024 and SYSTEM in ("Darwin", "Linux"):
        try:
            if os.geteuid() != 0:
                warn(f"Port {PORT} is privileged — if startup fails, retry with: sudo python3 run.py")
        except AttributeError:
            pass

    info("Press Ctrl+C to stop")
    print()

    # Register mDNS hostname (remote.local)
    print(f"{BOLD}  mDNS{RESET}")
    _zc = start_mdns(lan_ip, PORT) if lan_ip else None
    print()

    # Give browser a moment then open
    def _open_browser():
        time.sleep(1.5)
        try:
            webbrowser.open(local_url)
        except Exception:
            pass

    import threading
    threading.Thread(target=_open_browser, daemon=True).start()

    try:
        subprocess.run(
            [
                str(uvicorn_bin), "main:app",
                "--host", "0.0.0.0",
                "--port", str(PORT),
                "--log-level", "warning",
            ],
            cwd=str(HERE),
        )
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
