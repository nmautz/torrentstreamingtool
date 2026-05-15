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


def _windows_jackett_candidates() -> list[str]:
    """All the places the Jackett Windows installer (and winget) may drop it.

    Jackett ships both `JackettConsole.exe` (the `--NoRestart` console runner
    run.py launches) and `jackett.exe` (the tray app); installs land in
    Program Files when elevated and under the user profile otherwise.
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
            out.append(str(base / "JackettConsole.exe"))
            out.append(str(base / "jackett.exe"))
    return out


def jackett_candidates() -> list[str]:
    if SYSTEM == "Darwin":
        return [
            "/Applications/Jackett/JackettConsole",
            "/opt/homebrew/opt/jackett/bin/jackett",
            str(Path.home() / "Downloads/Jackett/jackett"),
            "jackett",
        ]
    if SYSTEM == "Windows":
        return _windows_jackett_candidates()
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


def _portable_matches(subdir: str, *exe_names: str) -> list[str]:
    """Resolve already-extracted portable binaries under ./tools/<subdir>/.

    The portable archives extract into version-stamped subdirectories
    (e.g. tools/ffmpeg/ffmpeg-8.1.1-essentials_build/bin/ffmpeg.exe), so a
    plain candidate path can't match — walk the tree instead. Listed first
    so a re-run of setup.py reuses the download instead of fetching it again.
    """
    base = TOOLS_DIR / subdir
    if not base.exists():
        return []
    out: list[str] = []
    for name in exe_names:
        out.extend(str(p) for p in base.rglob(name) if p.is_file())
    return out


def ffmpeg_candidates() -> list[str]:
    portable = _portable_matches("ffmpeg", "ffmpeg.exe", "ffmpeg")
    if SYSTEM == "Darwin":
        return portable + ["/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg", "ffmpeg"]
    if SYSTEM == "Windows":
        return portable + [r"C:\ffmpeg\bin\ffmpeg.exe", "ffmpeg"]
    return portable + ["/usr/bin/ffmpeg", "/usr/local/bin/ffmpeg", "ffmpeg"]


def fpcalc_candidates() -> list[str]:
    portable = _portable_matches("chromaprint", "fpcalc.exe", "fpcalc")
    if SYSTEM == "Darwin":
        return portable + ["/opt/homebrew/bin/fpcalc", "/usr/local/bin/fpcalc", "fpcalc"]
    if SYSTEM == "Windows":
        return portable + [r"C:\chromaprint\fpcalc.exe", "fpcalc"]
    return portable + ["/usr/bin/fpcalc", "/usr/local/bin/fpcalc", "fpcalc"]


def chromaprint_install_hint() -> str:
    if SYSTEM == "Darwin":
        return "brew install ffmpeg chromaprint"
    if SYSTEM == "Linux":
        return "sudo apt install ffmpeg libchromaprint-tools  (or: dnf install ffmpeg chromaprint-tools)"
    if SYSTEM == "Windows":
        return "winget install Gyan.FFmpeg ; winget install Chromaprint  (or download fpcalc.exe from https://acoustid.org/chromaprint)"
    return "install ffmpeg and chromaprint via your package manager"


# ── Portable binary download (Windows fallback) ───────────────────────────
TOOLS_DIR = HERE / "tools"
FFMPEG_WIN_URL  = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
FPCALC_WIN_URL  = "https://github.com/acoustid/chromaprint/releases/download/v1.5.1/chromaprint-fpcalc-1.5.1-windows-x86_64.zip"
FPCALC_LINUX_URL = "https://github.com/acoustid/chromaprint/releases/download/v1.5.1/chromaprint-fpcalc-1.5.1-linux-x86_64.tar.gz"
FPCALC_MAC_URL   = "https://github.com/acoustid/chromaprint/releases/download/v1.5.1/chromaprint-fpcalc-1.5.1-macos-x86_64.tar.gz"


def _download_with_progress(url: str, dest: Path) -> bool:
    """Download a URL to dest with a simple progress bar. Returns True on success."""
    import urllib.request
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        note(f"Downloading {url.rsplit('/', 1)[-1]} …")
        last_pct = [-1]
        def _hook(blocks, blocksize, total):
            if total <= 0 or not _TTY:
                return
            done = blocks * blocksize
            pct = min(100, int(done / total * 100))
            if pct != last_pct[0]:
                last_pct[0] = pct
                bar = "█" * (pct // 4) + "░" * (25 - pct // 4)
                sys.stdout.write(f"\r     [{bar}] {pct}%")
                sys.stdout.flush()
        urllib.request.urlretrieve(url, dest, reporthook=_hook)
        if _TTY:
            sys.stdout.write("\r" + " " * 50 + "\r")
            sys.stdout.flush()
        return True
    except Exception as e:
        warn(f"Download failed: {e}")
        return False


def _extract_archive(archive: Path, dest_dir: Path) -> bool:
    """Extract .zip or .tar.gz to dest_dir. Returns True on success."""
    import zipfile, tarfile
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        if archive.suffix == ".zip":
            with zipfile.ZipFile(archive) as zf:
                zf.extractall(dest_dir)
        elif archive.name.endswith(".tar.gz") or archive.suffix == ".gz":
            with tarfile.open(archive, "r:gz") as tf:
                tf.extractall(dest_dir)
        else:
            warn(f"Unknown archive type: {archive.name}")
            return False
        return True
    except Exception as e:
        warn(f"Extraction failed: {e}")
        return False


def _find_in_tree(root: Path, names: list[str]) -> Optional[str]:
    """Walk a directory tree and return the first matching exe path."""
    for p in root.rglob("*"):
        if p.is_file() and p.name.lower() in [n.lower() for n in names]:
            return str(p)
    return None


def _portable_install_windows() -> dict:
    """Download ffmpeg + fpcalc zips and extract under ./tools/.

    Returns {"ffmpeg": path or None, "fpcalc": path or None}.
    """
    result: dict = {"ffmpeg": None, "fpcalc": None}
    TOOLS_DIR.mkdir(exist_ok=True)
    tmp_zip = TOOLS_DIR / "_dl.zip"

    # ── ffmpeg ─────────────────────────────────────────────────────────────
    ff_dir = TOOLS_DIR / "ffmpeg"
    if _download_with_progress(FFMPEG_WIN_URL, tmp_zip):
        if _extract_archive(tmp_zip, ff_dir):
            ff_path = _find_in_tree(ff_dir, ["ffmpeg.exe"])
            if ff_path:
                result["ffmpeg"] = ff_path
                ok(f"ffmpeg.exe → {ff_path}")
            else:
                warn("ffmpeg.exe not found inside the extracted archive.")
        tmp_zip.unlink(missing_ok=True)

    # ── fpcalc ─────────────────────────────────────────────────────────────
    fp_dir = TOOLS_DIR / "chromaprint"
    if _download_with_progress(FPCALC_WIN_URL, tmp_zip):
        if _extract_archive(tmp_zip, fp_dir):
            fp_path = _find_in_tree(fp_dir, ["fpcalc.exe"])
            if fp_path:
                result["fpcalc"] = fp_path
                ok(f"fpcalc.exe → {fp_path}")
            else:
                warn("fpcalc.exe not found inside the extracted archive.")
        tmp_zip.unlink(missing_ok=True)

    return result


def _portable_install_unix(system: str) -> dict:
    """Linux/macOS fallback when no package manager is available. fpcalc only —
    ffmpeg static builds are too platform-fragmented to download reliably."""
    result: dict = {"ffmpeg": None, "fpcalc": None}
    TOOLS_DIR.mkdir(exist_ok=True)
    tmp_tar = TOOLS_DIR / "_dl.tar.gz"

    url = FPCALC_LINUX_URL if system == "Linux" else FPCALC_MAC_URL
    fp_dir = TOOLS_DIR / "chromaprint"
    if _download_with_progress(url, tmp_tar):
        if _extract_archive(tmp_tar, fp_dir):
            fp_path = _find_in_tree(fp_dir, ["fpcalc"])
            if fp_path:
                # Ensure it's executable on Unix
                try:
                    os.chmod(fp_path, 0o755)
                except OSError:
                    pass
                result["fpcalc"] = fp_path
                ok(f"fpcalc → {fp_path}")
        tmp_tar.unlink(missing_ok=True)

    return result


# ── Auto-install ffmpeg + chromaprint (Smart Skip deps) ────────────────────
def install_smart_skip_deps(tools: dict) -> dict:
    """Offer to install ffmpeg/fpcalc via the host's package manager.

    Returns an updated tools dict with refreshed ffmpeg/fpcalc paths if the
    install succeeded.  No-op if both are already detected.
    """
    if tools.get("ffmpeg") and tools.get("fpcalc"):
        return tools

    header("Smart Skip Dependencies (ffmpeg + chromaprint)")
    missing = []
    if not tools.get("ffmpeg"): missing.append("ffmpeg")
    if not tools.get("fpcalc"): missing.append("fpcalc")
    warn(f"Missing: {', '.join(missing)}")

    if SYSTEM == "Darwin":
        brew = find_exe("brew", "/opt/homebrew/bin/brew", "/usr/local/bin/brew")
        if not brew:
            warn("Homebrew not found — install it from https://brew.sh, then re-run setup.py")
            note(f"Or install manually: {chromaprint_install_hint()}")
            return tools
        if not ask_bool(f"Run `{brew} install ffmpeg chromaprint` now?", default=True):
            note(f"Skipped. Install later with: {chromaprint_install_hint()}")
            return tools
        note("Running brew install (this may take a few minutes) …")
        try:
            subprocess.run([brew, "install", "ffmpeg", "chromaprint"], check=True)
        except subprocess.CalledProcessError as e:
            warn(f"brew install failed (exit {e.returncode})")
            note(f"Install manually: {chromaprint_install_hint()}")
            return tools

    elif SYSTEM == "Linux":
        # Detect the package manager and propose the matching command
        apt = find_exe("apt", "apt-get")
        dnf = find_exe("dnf", "yum")
        pacman = find_exe("pacman")
        if apt:
            cmd = ["sudo", apt, "install", "-y", "ffmpeg", "libchromaprint-tools"]
        elif dnf:
            cmd = ["sudo", dnf, "install", "-y", "ffmpeg", "chromaprint-tools"]
        elif pacman:
            cmd = ["sudo", pacman, "-S", "--noconfirm", "ffmpeg", "chromaprint"]
        else:
            warn("No supported package manager found (apt/dnf/pacman).")
            note(f"Install manually: {chromaprint_install_hint()}")
            return tools
        if not ask_bool(f"Run `{' '.join(cmd)}` now?", default=True):
            note(f"Skipped. Install later with: {chromaprint_install_hint()}")
            return tools
        note("Running package install (you may be prompted for your sudo password) …")
        try:
            subprocess.run(cmd, check=True)
        except subprocess.CalledProcessError as e:
            warn(f"Install failed (exit {e.returncode})")
            note(f"Install manually: {chromaprint_install_hint()}")
            return tools

    elif SYSTEM == "Windows":
        # Skip winget entirely — it's flaky on stock Windows installs.  Just
        # download the official portable zips for ffmpeg + chromaprint.
        if not ask_bool("Download portable ffmpeg.exe + fpcalc.exe into ./tools/ (~85 MB total)?",
                        default=True):
            note(f"Skipped. Install later with: {chromaprint_install_hint()}")
            return tools
        portable = _portable_install_windows()
        refreshed = dict(tools)
        if portable.get("ffmpeg"): refreshed["ffmpeg"] = portable["ffmpeg"]
        if portable.get("fpcalc"): refreshed["fpcalc"] = portable["fpcalc"]
        if not refreshed.get("ffmpeg") or not refreshed.get("fpcalc"):
            warn("Portable install did not produce both binaries.")
            note(f"Install manually if Smart Skip is needed: {chromaprint_install_hint()}")
        return refreshed
    else:
        warn(f"Auto-install not implemented for {SYSTEM}.")
        note(f"Install manually: {chromaprint_install_hint()}")
        return tools

    # Re-detect after install (package-manager path on Darwin / Linux)
    refreshed = dict(tools)
    refreshed["ffmpeg"] = find_exe(*ffmpeg_candidates())
    refreshed["fpcalc"] = find_exe(*fpcalc_candidates())
    if refreshed["ffmpeg"]:
        ok(f"ffmpeg: {refreshed['ffmpeg']}")
    else:
        warn("ffmpeg still not found after install — check PATH")
    if refreshed["fpcalc"]:
        ok(f"fpcalc: {refreshed['fpcalc']}")
    else:
        warn("fpcalc still not found after install — check PATH")

    # Last-resort: portable fpcalc download on Unix if package manager didn't
    # provide it (some distros lack a libchromaprint-tools package).
    if not refreshed.get("fpcalc") and SYSTEM in ("Linux", "Darwin"):
        if ask_bool("Download portable fpcalc into ./tools/ as a fallback?", default=True):
            portable = _portable_install_unix(SYSTEM)
            if portable.get("fpcalc"):
                refreshed["fpcalc"] = portable["fpcalc"]
    return refreshed


# ── Auto-install core applications (VLC, qBittorrent, Jackett, Mullvad) ────
_CORE_LABELS = {
    "vlc":     "VLC",
    "qbit":    "qBittorrent",
    "jackett": "Jackett",
    "mullvad": "Mullvad VPN",
}
_WINGET_IDS = {
    "vlc":     "VideoLAN.VLC",
    "qbit":    "qBittorrent.qBittorrent",
    "jackett": "Jackett.Jackett",
    "mullvad": "MullvadVPN.MullvadVPN",
}
_BREW_CASKS = {
    "vlc":     "vlc",
    "qbit":    "qbittorrent",
    "jackett": "jackett",
    "mullvad": "mullvad-vpn",
}
_CORE_RESCAN = {
    "vlc":     vlc_candidates,
    "qbit":    qbit_candidates,
    "jackett": jackett_candidates,
    "mullvad": mullvad_candidates,
}
# winget exit codes that mean "nothing to do" rather than a real failure.
# Listed in both unsigned and signed-32-bit forms because subprocess can
# surface either on Windows.
_WINGET_OK_CODES = {
    0,
    0x8A15002B, 0x8A15002B - 0x100000000,   # UPDATE_NOT_APPLICABLE (already current)
    0x8A150061, 0x8A150061 - 0x100000000,   # PACKAGE_ALREADY_INSTALLED
}


def install_core_deps(tools: dict) -> dict:
    """Install the core applications that weren't detected.

    Windows uses winget (the documented target for full automation); macOS
    uses Homebrew casks; Linux prints a package-manager hint because desktop
    app packaging varies too much to automate safely.  Returns an updated
    tools dict with refreshed paths for anything that got installed.
    """
    core_keys = ["vlc", "qbit", "jackett", "mullvad"]
    missing = [k for k in core_keys if not tools.get(k)]
    if not missing:
        return tools

    header("Core Applications")
    warn(f"Missing: {', '.join(_CORE_LABELS[k] for k in missing)}")

    if SYSTEM == "Windows":
        winget = find_exe("winget")
        if not winget:
            warn("winget not found — install 'App Installer' from the Microsoft Store, then re-run setup.py")
            for k in missing:
                note(f"Or install {_CORE_LABELS[k]} manually.")
            return tools
        if not ask_bool(
            f"Install {', '.join(_CORE_LABELS[k] for k in missing)} via winget now?",
            default=True,
        ):
            note("Skipped core application install.")
            return tools
        for k in missing:
            pkg = _WINGET_IDS[k]
            note(f"winget install {pkg} …")
            proc = subprocess.run(
                [winget, "install", "--id", pkg, "-e", "--silent",
                 "--accept-package-agreements", "--accept-source-agreements"],
            )
            if proc.returncode in _WINGET_OK_CODES:
                ok(f"{_CORE_LABELS[k]} installed")
            else:
                warn(f"winget install {pkg} failed (exit {proc.returncode})")

    elif SYSTEM == "Darwin":
        brew = find_exe("brew", "/opt/homebrew/bin/brew", "/usr/local/bin/brew")
        if not brew:
            warn("Homebrew not found — install it from https://brew.sh, then re-run setup.py")
            return tools
        casks = [_BREW_CASKS[k] for k in missing]
        if not ask_bool(f"Run `{brew} install --cask {' '.join(casks)}` now?", default=True):
            note("Skipped core application install.")
            return tools
        for k in missing:
            cask = _BREW_CASKS[k]
            note(f"brew install --cask {cask} …")
            try:
                subprocess.run([brew, "install", "--cask", cask], check=True)
                ok(f"{_CORE_LABELS[k]} installed")
            except subprocess.CalledProcessError as exc:
                warn(f"brew install --cask {cask} failed (exit {exc.returncode})")

    else:  # Linux
        warn("Automatic core-app install isn't supported on Linux.")
        note("Install via your package manager, for example:")
        note("  sudo apt install vlc qbittorrent")
        note("  Mullvad: https://mullvad.net/download/vpn/linux")
        note("  Jackett: https://github.com/Jackett/Jackett/releases")
        return tools

    # Re-detect anything that was missing (a new shell/PATH may be needed)
    refreshed = dict(tools)
    for k in missing:
        path = find_exe(*_CORE_RESCAN[k]())
        refreshed[k] = path
        if path:
            ok(f"{_CORE_LABELS[k]}: {path}")
        else:
            warn(f"{_CORE_LABELS[k]} still not found — a reboot or new shell may be needed for PATH changes")
    return refreshed


# ── Register StreamLink as a system service ───────────────────────────────
def offer_service_install() -> bool:
    """Offer to register StreamLink as a system service so it starts on boot.

    Delegates to daemon.install(); the installed service runs the watchdog,
    which starts VLC / Jackett / the dashboard on its own and starts
    qBittorrent only once Mullvad VPN reports Connected.
    """
    header("Startup Automation")
    note("Register StreamLink as a system service so it starts automatically on")
    note("boot/login. The service runs the watchdog — which starts VLC, Jackett,")
    note("and the dashboard, and starts qBittorrent only once Mullvad VPN connects.")
    default = SYSTEM == "Windows"
    if not ask_bool("Install StreamLink as a system service now?", default=default):
        note("Skipped. Install later with: python3 run.py --install")
        return False
    try:
        import daemon as _daemon
    except Exception as exc:
        warn(f"Could not import daemon.py: {exc}")
        note("Install later with: python3 run.py --install")
        return False
    try:
        return bool(_daemon.install())
    except Exception as exc:
        warn(f"Service install failed: {exc}")
        note("Install later with: python3 run.py --install")
        return False


# ── Step 1: Python version ─────────────────────────────────────────────────
def check_python():
    header("Python")
    vi = sys.version_info
    if vi < (3, 9):
        fail(f"Python 3.9+ required, found {vi.major}.{vi.minor}")
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

    python = VENV / ("Scripts/python.exe" if SYSTEM == "Windows" else "bin/python")
    pip    = VENV / ("Scripts/pip.exe"    if SYSTEM == "Windows" else "bin/pip")
    note("Installing dependencies (may take a moment) …")
    # Use 'python -m pip' to upgrade pip — calling pip.exe directly fails on Windows
    subprocess.run([str(python), "-m", "pip", "install", "-q", "--upgrade", "pip"], check=True)
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
        ("ffmpeg",  "ffmpeg",        ffmpeg_candidates(),  "https://ffmpeg.org/download.html"),
        ("fpcalc",  "fpcalc (chromaprint)", fpcalc_candidates(), "https://acoustid.org/chromaprint"),
    ]

    for key, label, candidates, url in checks:
        path = find_exe(*candidates)
        tools[key] = path
        if path:
            ok(f"{label}: {path}")
        else:
            warn(f"{label} not found — download: {url}")

    if not tools.get("ffmpeg") or not tools.get("fpcalc"):
        note(f"Smart Skip (intro/credits auto-detection) needs both ffmpeg and fpcalc.")
        note(f"Install: {chromaprint_install_hint()}")

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
    cfg["JACKETT_PASSWORD"]   = ask("Admin password (for indexer management, leave blank if none)", "", secret=True)
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
    print()
    print(f"  {BOLD}Admin panel{RESET}")
    cfg["ADMIN_PASSWORD"]     = ask("Admin password (leave blank to disable)", "", secret=True)

    # Warn if qBittorrent and VLC are configured on the same port
    import re as _re
    def _port(url: str, default: int) -> int:
        m = _re.search(r":(\d+)", url)
        return int(m.group(1)) if m else default
    if _port(cfg["QBIT_URL"], 8081) == _port(cfg["VLC_URL"], 8080):
        warn("qBittorrent and VLC are both on the same port — one of them will fail to bind.")
        warn("Edit QBIT_URL or VLC_URL so they use different ports.")

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


# ── Step 5b: Generate SSL certificate ────────────────────────────────────
def generate_ssl_cert() -> bool:
    """Generate a self-signed CA + server cert for https://remote.local.

    Returns True if certs were created or already exist; False on error.
    cert.pem / key.pem are read by uvicorn.
    ca.pem should be added to the system/browser trust store.
    """
    header("SSL Certificate (HTTPS admin panel)")
    cert = HERE / "cert.pem"
    key  = HERE / "key.pem"
    ca   = HERE / "ca.pem"

    if cert.exists() and key.exists() and ca.exists():
        ok("SSL certs already exist — skipping generation")
        return True

    try:
        import ipaddress as _ip
        from datetime import datetime as _dt, timedelta as _td, timezone as _tz
        from cryptography import x509 as _x509
        from cryptography.x509.oid import NameOID as _OID
        from cryptography.hazmat.primitives import hashes as _hashes, serialization as _ser
        from cryptography.hazmat.primitives.asymmetric import rsa as _rsa
    except ImportError:
        warn("cryptography package not found — cannot generate SSL cert.")
        warn("Run: pip install cryptography  (or re-run setup.py to install it)")
        return False

    note("Generating self-signed CA and server certificate…")
    now = _dt.now(_tz.utc)
    expire = now + _td(days=3650)

    # CA
    ca_key = _rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = _x509.Name([_x509.NameAttribute(_OID.COMMON_NAME, "StreamLink Local CA")])
    ca_cert = (
        _x509.CertificateBuilder()
        .subject_name(ca_name).issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(_x509.random_serial_number())
        .not_valid_before(now).not_valid_after(expire)
        .add_extension(_x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(ca_key, _hashes.SHA256())
    )

    # Server cert
    srv_key = _rsa.generate_private_key(public_exponent=65537, key_size=2048)
    san = _x509.SubjectAlternativeName([
        _x509.DNSName("remote.local"),
        _x509.DNSName("localhost"),
        _x509.IPAddress(_ip.IPv4Address("127.0.0.1")),
    ])
    srv_cert = (
        _x509.CertificateBuilder()
        .subject_name(_x509.Name([_x509.NameAttribute(_OID.COMMON_NAME, "remote.local")]))
        .issuer_name(ca_name)
        .public_key(srv_key.public_key())
        .serial_number(_x509.random_serial_number())
        .not_valid_before(now).not_valid_after(expire)
        .add_extension(san, critical=False)
        .sign(ca_key, _hashes.SHA256())
    )

    cert.write_bytes(srv_cert.public_bytes(_ser.Encoding.PEM))
    key.write_bytes(srv_key.private_bytes(
        _ser.Encoding.PEM, _ser.PrivateFormat.TraditionalOpenSSL, _ser.NoEncryption()))
    ca.write_bytes(ca_cert.public_bytes(_ser.Encoding.PEM))

    ok(f"cert.pem  →  {cert}")
    ok(f"key.pem   →  {key}")
    ok(f"ca.pem    →  {ca}")
    print()

    if SYSTEM == "Darwin":
        note("To make browsers trust the cert (no warning), run:")
        note(f'  sudo security add-trusted-cert -d -r trustRoot \\')
        note(f'       -k /Library/Keychains/System.keychain "{ca}"')
    elif SYSTEM == "Linux":
        note("To trust the cert system-wide:")
        note(f'  sudo cp "{ca}" /usr/local/share/ca-certificates/streamlink-ca.crt')
        note(f'  sudo update-ca-certificates')
    elif SYSTEM == "Windows":
        note("To trust the cert (run in an elevated PowerShell):")
        note(f'  Import-Certificate -FilePath "{ca}" -CertStoreLocation Cert:\\LocalMachine\\Root')

    return True


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
               "jackett": "_JACKETT_BIN", "mullvad": "_MULLVAD_BIN",
               "ffmpeg": "_FFMPEG_BIN", "fpcalc": "_FPCALC_BIN"}
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


# ── .env parsing for reuse path ───────────────────────────────────────────
def parse_existing_env() -> dict:
    """Read the existing .env into a flat dict of key/value strings."""
    cfg: dict[str, str] = {}
    if not ENV.exists():
        return cfg
    for line in ENV.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        cfg[k.strip()] = v.strip()
    return cfg


def merge_tool_paths(tools: dict) -> None:
    """When reusing .env, refresh just the auto-detected _*_BIN entries.

    Existing user settings stay untouched; only the binary paths are kept
    current so newly-installed tools (e.g. ffmpeg/fpcalc) are picked up
    without forcing a full re-prompt.
    """
    if not ENV.exists():
        return
    lines = ENV.read_text(encoding="utf-8", errors="replace").splitlines()
    mapping = {"vlc": "_VLC_BIN", "qbit": "_QBIT_BIN",
               "jackett": "_JACKETT_BIN", "mullvad": "_MULLVAD_BIN",
               "ffmpeg": "_FFMPEG_BIN", "fpcalc": "_FPCALC_BIN"}
    desired = {env_key: tools[key] for key, env_key in mapping.items() if tools.get(key)}

    out: list[str] = []
    seen: set[str] = set()
    for raw in lines:
        stripped = raw.strip()
        if "=" in stripped and not stripped.startswith("#"):
            k = stripped.split("=", 1)[0].strip()
            if k in desired:
                out.append(f"{k}={desired[k]}")
                seen.add(k)
                continue
            if k in mapping.values() and k not in desired:
                # Path no longer detected — drop the stale entry
                continue
        out.append(raw)

    # Append any newly-detected bins not already present
    new_keys = [k for k in desired if k not in seen]
    if new_keys:
        if out and out[-1].strip():
            out.append("")
        out.append("# Auto-detected binary paths (refreshed)")
        for k in new_keys:
            out.append(f"{k}={desired[k]}")

    ENV.write_text("\n".join(out) + "\n", encoding="utf-8")
    if new_keys:
        ok(f"Refreshed tool paths in .env: {', '.join(new_keys)}")
    else:
        ok("Tool paths in .env are up to date")


# ── Main ───────────────────────────────────────────────────────────────────
def main():
    print(f"\n{BOLD}{CYN}  ┌──────────────────────────────┐")
    print(f"  │   StreamLink v2.0  Setup   │")
    print(f"  └──────────────────────────────┘{RESET}")

    check_python()
    setup_venv()
    tools = detect_tools()
    tools = install_core_deps(tools)
    tools = install_smart_skip_deps(tools)

    reuse_env = False
    if ENV.exists():
        header("Existing .env detected")
        note(f"Found {ENV}")
        reuse_env = ask_bool("Reuse existing .env without re-prompting?", default=True)

    if reuse_env:
        cfg = parse_existing_env()
        merge_tool_paths(tools)
        ok("Skipped interactive configuration")
    else:
        cfg = gather_config()
        configure_qbittorrent(cfg)
        write_env(cfg, tools)

    if cfg.get("QBIT_DOWNLOAD_PATH"):
        ensure_download_dir(cfg)
    generate_ssl_cert()

    service_installed = offer_service_install()

    header("Done")
    ok("Setup complete!")
    print()

    missing = [k for k, v in tools.items() if not v]
    if missing:
        warn(f"Still needs manual install: {', '.join(missing)}")
    if not tools.get("mullvad"):
        note("VPN guard will be inactive until Mullvad CLI is in PATH.")

    print()
    if service_installed:
        note(f"StreamLink is registered as a system service and is starting now.")
        note(f"Manage it with: {BOLD}python3 run.py --status{RESET} / {BOLD}--uninstall{RESET}")
    else:
        note(f"Start everything:   {BOLD}python3 run.py{RESET}")
    print()


if __name__ == "__main__":
    main()
