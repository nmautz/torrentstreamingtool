#!/usr/bin/env python3
"""
StreamLink Setup — run once to configure everything automatically.
    python3 setup.py
"""
from __future__ import annotations

import hashlib
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE   = Path(__file__).parent
VENV   = HERE / ".venv"
ENV    = HERE / ".env"
SYSTEM = platform.system()

# ── Color output (disabled on non-TTY) ────────────────────────────────────
_TTY = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()
def _c(n): return f"\033[{n}m" if _TTY else ""
RESET = _c(0); BOLD = _c(1)
RED = _c(91); GRN = _c(92); YLW = _c(93); BLU = _c(94); CYN = _c(96)

def header(t): print(f"\n{BOLD}{BLU}━━  {t}  ━━{RESET}")
def ok(t):     print(f"  {GRN}✓{RESET}  {t}")
def warn(t):   print(f"  {YLW}⚠{RESET}  {t}")
def fail(t):   print(f"  {RED}✗{RESET}  {t}"); sys.exit(1)
def note(t):   print(f"     {CYN}{t}{RESET}")


def ask(prompt, default="", secret=False):
    hint = f"{CYN}{default}{RESET}"
    try:
        if secret and _TTY:
            import getpass
            v = getpass.getpass(f"  {BOLD}{prompt}{RESET} [{hint}]: ")
        else:
            v = input(f"  {BOLD}{prompt}{RESET} [{hint}]: ").strip()
        return v or default
    except (EOFError, KeyboardInterrupt):
        print(); return default


def ask_bool(prompt, default=True):
    choices = "Y/n" if default else "y/N"
    try:
        v = input(f"  {BOLD}{prompt}{RESET} [{choices}]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print(); return default
    return default if not v else v.startswith("y")


# ── Find an executable from a list of candidate paths ─────────────────────
def find_exe(*candidates) -> str | None:
    for c in candidates:
        p = Path(c)
        if p.exists():
            return str(p)
        found = shutil.which(c)
        if found:
            return found
    return None


# ── Platform-specific paths ────────────────────────────────────────────────
def qbit_ini_path() -> Path:
    if SYSTEM == "Darwin":
        return Path.home() / "Library/Application Support/qBittorrent/qBittorrent.ini"
    if SYSTEM == "Windows":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming"))
        return base / "qBittorrent/qBittorrent.ini"
    return Path.home() / ".config/qBittorrent/qBittorrent.ini"


def vlc_candidates() -> list[str]:
    if SYSTEM == "Darwin":
        return ["/Applications/VLC.app/Contents/MacOS/VLC", "vlc"]
    if SYSTEM == "Windows":
        return [
            r"C:\Program Files\VideoLAN\VLC\vlc.exe",
            r"C:\Program Files (x86)\VideoLAN\VLC\vlc.exe",
        ]
    return ["/usr/bin/vlc", "/usr/local/bin/vlc", "/snap/bin/vlc", "vlc"]


def qbit_candidates() -> list[str]:
    if SYSTEM == "Darwin":
        return ["/Applications/qbittorrent.app/Contents/MacOS/qbittorrent", "qbittorrent"]
    if SYSTEM == "Windows":
        return [r"C:\Program Files\qBittorrent\qbittorrent.exe"]
    return ["/usr/bin/qbittorrent", "qbittorrent"]


def jackett_candidates() -> list[str]:
    if SYSTEM == "Darwin":
        return [
            "/Applications/Jackett/JackettConsole",
            "/opt/homebrew/opt/jackett/bin/jackett",
            str(Path.home() / "Downloads/Jackett/jackett"),
            "jackett",
        ]
    if SYSTEM == "Windows":
        return [r"C:\Program Files\Jackett\Jackett.exe"]
    return [str(Path.home() / "Downloads/Jackett/jackett"), "/opt/jackett/jackett", "jackett"]


def mullvad_candidates() -> list[str]:
    if SYSTEM == "Darwin":
        return [
            "/Applications/Mullvad VPN.app/Contents/Resources/mullvad",
            "mullvad",
        ]
    if SYSTEM == "Windows":
        return [r"C:\Program Files\Mullvad VPN\resources\mullvad.exe", "mullvad"]
    return ["/usr/bin/mullvad", "mullvad"]


# ── Step 1: Python version ─────────────────────────────────────────────────
def check_python():
    header("Python")
    vi = sys.version_info
    if vi < (3, 10):
        fail(f"Python 3.10+ required, found {vi.major}.{vi.minor}")
    ok(f"Python {vi.major}.{vi.minor}.{vi.micro}")


# ── Step 2: Virtual environment + dependencies ────────────────────────────
def setup_venv():
    header("Virtual Environment & Dependencies")
    if VENV.exists():
        ok(f"venv already at {VENV.name}/")
    else:
        note("Creating .venv …")
        subprocess.run([sys.executable, "-m", "venv", str(VENV)], check=True)
        ok("venv created")

    pip = VENV / ("Scripts/pip.exe" if SYSTEM == "Windows" else "bin/pip")
    note("Installing dependencies (may take a moment) …")
    subprocess.run([str(pip), "install", "-q", "--upgrade", "pip"], check=True)
    subprocess.run([str(pip), "install", "-q", "-r", str(HERE / "requirements.txt")], check=True)
    ok("All Python packages installed")


# ── Step 3: Detect external tools ─────────────────────────────────────────
def detect_tools() -> dict:
    header("Detecting Installed Tools")
    tools = {}

    checks = [
        ("vlc",     "VLC",           vlc_candidates(),     "https://videolan.org/vlc/"),
        ("qbit",    "qBittorrent",   qbit_candidates(),    "https://qbittorrent.org/"),
        ("mullvad", "Mullvad CLI",   mullvad_candidates(), "https://mullvad.net/"),
        ("jackett", "Jackett",       jackett_candidates(), "https://github.com/Jackett/Jackett/releases"),
    ]

    for key, label, candidates, url in checks:
        path = find_exe(*candidates)
        tools[key] = path
        if path:
            ok(f"{label}: {path}")
        else:
            warn(f"{label} not found — download: {url}")

    return tools


# ── Step 4: Interactive configuration ─────────────────────────────────────
def gather_config() -> dict:
    header("Configuration")
    note("Press Enter to accept the default shown in brackets.")
    print()

    default_dl = str(Path.home() / "Downloads" / "StreamLink")

    cfg: dict[str, str] = {}

    print(f"  {BOLD}Jackett (indexer){RESET}")
    cfg["INDEXER_URL"]        = ask("URL",               "http://localhost:9117")
    cfg["INDEXER_API_KEY"]    = ask("API key",           "")
    cfg["INDEXER_CATEGORIES"] = ask("Categories (0=all, 2000=Movies, 5000=TV)", "0")
    print()
    print(f"  {BOLD}qBittorrent{RESET}")
    cfg["QBIT_URL"]           = ask("Web UI URL",        "http://localhost:8081")
    cfg["QBIT_USERNAME"]      = ask("Username",          "admin")
    cfg["QBIT_PASSWORD"]      = ask("Password",          "adminadmin", secret=True)
    cfg["QBIT_DOWNLOAD_PATH"] = ask("Download folder",   default_dl)
    print()
    print(f"  {BOLD}VLC{RESET}")
    cfg["VLC_URL"]            = ask("HTTP URL",          "http://localhost:8080")
    cfg["VLC_PASSWORD"]       = ask("Lua HTTP password", "vlcpassword", secret=True)
    print()
    print(f"  {BOLD}Buffer thresholds (stream starts when either is met){RESET}")
    cfg["BUFFER_MIN_MB"]      = ask("Min MB",  "15.0")
    cfg["BUFFER_MIN_PCT"]     = ask("Min %",   "1.0")

    return cfg


# ── Step 5: Write qBittorrent ini ─────────────────────────────────────────
def configure_qbittorrent(cfg: dict) -> None:
    header("Configuring qBittorrent")

    ini = qbit_ini_path()
    ini.parent.mkdir(parents=True, exist_ok=True)

    # Parse existing ini while preserving unknown sections/keys
    sections: dict[str, dict[str, str]] = {}
    order: list[str] = []          # preserve section order
    current: str | None = None

    if ini.exists():
        for raw in ini.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if line.startswith("[") and line.endswith("]"):
                current = line[1:-1]
                if current not in sections:
                    sections[current] = {}
                    order.append(current)
            elif "=" in line and current is not None:
                k, _, v = line.partition("=")
                sections[current][k] = v

    # Compute MD5 hash for qBit 4.x Password_ha1 format
    user = cfg["QBIT_USERNAME"]
    pwd  = cfg["QBIT_PASSWORD"]
    ha1  = hashlib.md5(f"{user}:qBittorrent Web UI:{pwd}".encode()).hexdigest()

    # Extract port from URL
    m = re.search(r":(\d+)", cfg["QBIT_URL"])
    port = m.group(1) if m else "8081"

    # Inject/overwrite our keys
    for section in ("Preferences", "BitTorrent"):
        if section not in sections:
            sections[section] = {}
            order.append(section)

    sections["Preferences"].update({
        r"WebUI\Enabled":        "true",
        r"WebUI\Port":           port,
        r"WebUI\Username":       user,
        r"WebUI\Password_ha1":   f"@ByteArray({ha1})",
        r"WebUI\LocalHostAuth":  "false",   # no auth needed from localhost
        r"WebUI\CSRFProtection": "false",   # allow API calls from our backend
        r"WebUI\SessionTimeout": "3600",
    })
    sections["BitTorrent"][r"Session\DefaultSavePath"] = cfg["QBIT_DOWNLOAD_PATH"]

    # Write back
    out: list[str] = []
    for sec in order:
        out.append(f"[{sec}]")
        for k, v in sections[sec].items():
            out.append(f"{k}={v}")
        out.append("")

    ini.write_text("\n".join(out), encoding="utf-8")
    ok(f"Written → {ini}")
    note(f"Web UI on port {port} | localhost auth disabled | CSRF off")

    if ini.stat().st_size > 0:
        note("Restart qBittorrent if it is already running to pick up changes.")


# ── Step 6: Write .env ────────────────────────────────────────────────────
def write_env(cfg: dict, tools: dict) -> None:
    header("Writing .env")

    lines = [
        "# Generated by setup.py — re-run setup.py to regenerate",
        "",
    ]
    for k, v in cfg.items():
        lines.append(f"{k}={v}")

    lines += ["", "# Auto-detected binary paths (used by run.py)"]
    mapping = {"vlc": "_VLC_BIN", "qbit": "_QBIT_BIN",
               "jackett": "_JACKETT_BIN", "mullvad": "_MULLVAD_BIN"}
    for key, env_key in mapping.items():
        if tools.get(key):
            lines.append(f"{env_key}={tools[key]}")

    ENV.write_text("\n".join(lines) + "\n", encoding="utf-8")
    ok(f"Written → {ENV}")


# ── Step 7: Ensure download path exists ───────────────────────────────────
def ensure_download_dir(cfg: dict) -> None:
    p = Path(cfg["QBIT_DOWNLOAD_PATH"])
    p.mkdir(parents=True, exist_ok=True)
    ok(f"Download folder ready → {p}")


# ── Main ───────────────────────────────────────────────────────────────────
def main():
    print(f"\n{BOLD}{CYN}  ┌──────────────────────────────┐")
    print(f"  │   StreamLink v2.0  Setup   │")
    print(f"  └──────────────────────────────┘{RESET}")

    check_python()
    setup_venv()
    tools = detect_tools()
    cfg   = gather_config()
    configure_qbittorrent(cfg)
    write_env(cfg, tools)
    ensure_download_dir(cfg)

    header("Done")
    ok("Setup complete!")
    print()

    missing = [k for k, v in tools.items() if not v]
    if missing:
        warn(f"Still needs manual install: {', '.join(missing)}")
    if not tools.get("mullvad"):
        note("VPN guard will be inactive until Mullvad CLI is in PATH.")

    print()
    note(f"Start everything:   {BOLD}python3 run.py{RESET}")
    print()


if __name__ == "__main__":
    main()
