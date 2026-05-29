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

# ── Verbose flag (consume early so the rest of argv stays clean) ──────────
VERBOSE = any(a in ("-v", "--verbose") for a in sys.argv[1:])
if VERBOSE:
    sys.argv = [sys.argv[0]] + [a for a in sys.argv[1:] if a not in ("-v", "--verbose")]

# ── stdout/stderr encoding (Python 3.13 + Windows + piped output crash) ──
# When setup.py runs as a subprocess on Windows (auto-updater calling it, or any
# `subprocess.run([...setup.py])` invocation), Python defaults stdout/stderr to
# the host's legacy ANSI code page — usually cp1252 in en-US. cp1252 can't
# encode the Unicode box-drawing characters / ✓ ✗ → ⚠ symbols this script prints,
# so the very first banner raises UnicodeEncodeError and the whole process
# exits with rc=1 before doing anything useful. Force UTF-8 with errors="replace"
# so misencoded glyphs degrade to ? instead of killing the process. Idempotent.
for _stream in (sys.stdout, sys.stderr):
    try:
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Whether this is an auto-updater-driven re-run. The updater (see updater.py)
# sets this env var; when it's True, setup.py:
#   - forces reuse_env=True when an existing .env is found (no prompt)
#   - skips offer_service_install() — the updater does its own daemon.uninstall()
#     + install() afterwards
#   - treats `pip install` failures as warnings (transient network glitches
#     shouldn't kill an update — the next setup re-run will catch up)
#   - never tries to install OS-level apps (winget/brew casks) — those run only
#     during the initial interactive setup
AUTOUPDATE = os.environ.get("STREAMLINK_AUTOUPDATE", "").strip() == "1"

# ── Color output (disabled on non-TTY) ────────────────────────────────────
_TTY = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()
def _c(n): return f"\033[{n}m" if _TTY else ""
RESET = _c(0); BOLD = _c(1)
RED = _c(91); GRN = _c(92); YLW = _c(93); BLU = _c(94); CYN = _c(96); MAG = _c(95)

def header(t): print(f"\n{BOLD}{BLU}━━  {t}  ━━{RESET}")
def ok(t):     print(f"  {GRN}✓{RESET}  {t}")
def warn(t):   print(f"  {YLW}⚠{RESET}  {t}")
def fail(t):   print(f"  {RED}✗{RESET}  {t}"); sys.exit(1)
def note(t):   print(f"     {CYN}{t}{RESET}")
def vlog(t):
    if VERBOSE:
        print(f"     {MAG}[v]{RESET} {t}")


def vrun(cmd, **kwargs) -> subprocess.CompletedProcess:
    """subprocess.run + verbose dump of the command and its output."""
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    if VERBOSE:
        vlog(f"$ {' '.join(str(c) for c in cmd)}")
    proc = subprocess.run(cmd, **kwargs)
    if VERBOSE:
        vlog(f"  exit={proc.returncode}")
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        if out:
            for line in out.splitlines():
                vlog(f"  stdout: {line}")
        if err:
            for line in err.splitlines():
                vlog(f"  stderr: {line}")
    return proc


# Whether we have an interactive stdin to read from. Task Scheduler / launchd
# / systemd run setup.py without a console — sys.stdin.isatty() is False and
# input() can block indefinitely (it doesn't always cleanly EOF on Windows
# when the parent is a service), so we short-circuit to defaults up front.
_STDIN_INTERACTIVE = bool(getattr(sys.stdin, "isatty", lambda: False)())


def ask(prompt, default="", secret=False, show_default=None):
    # show_default lets a caller display a different hint than the real default
    # value — used to mask stored secrets in the brackets while still returning
    # the stored value when the user just presses Enter.
    shown = show_default if show_default is not None else default
    hint = f"{CYN}{shown}{RESET}"
    if not _STDIN_INTERACTIVE:
        print(f"  {BOLD}{prompt}{RESET} [{hint}]: (no stdin — using default)")
        return default
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
    if not _STDIN_INTERACTIVE:
        answer = "yes" if default else "no"
        print(f"  {BOLD}{prompt}{RESET} [{choices}]: (no stdin — defaulting to {answer})")
        return default
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

    Preferred entry-point is `JackettTray.exe` — launching it puts the icon
    in the notification area AND auto-starts the Jackett Windows service that
    actually serves port 9117. `JackettConsole.exe` / `jackett.exe` are
    fallbacks for setups where the tray app isn't installed.
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


def whisper_candidates() -> list[str]:
    portable = _portable_matches("whisper", "whisper-cli.exe", "whisper-cli",
                                 "main.exe", "whisper.exe")
    if SYSTEM == "Darwin":
        return portable + ["/opt/homebrew/bin/whisper-cli", "/usr/local/bin/whisper-cli",
                           "whisper-cli", "whisper-cpp"]
    if SYSTEM == "Windows":
        return portable + ["whisper-cli.exe", "whisper-cli"]
    return portable + ["/usr/bin/whisper-cli", "/usr/local/bin/whisper-cli",
                       "whisper-cli", "whisper-cpp"]


def whisper_model_candidates() -> list[str]:
    """Locate a downloaded GGML model under ./tools/whisper/ (any ggml-*.bin)."""
    base = TOOLS_DIR / "whisper"
    if not base.exists():
        return []
    return [str(p) for p in base.rglob("ggml-*.bin") if p.is_file()]


def whisper_install_hint() -> str:
    if SYSTEM == "Darwin":
        return "brew install whisper-cpp  (then re-run setup.py to download the model)"
    if SYSTEM == "Windows":
        return "let setup.py download the portable whisper.cpp build, or grab it from https://github.com/ggml-org/whisper.cpp/releases"
    return "build whisper.cpp (https://github.com/ggml-org/whisper.cpp) so `whisper-cli` is on PATH"


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

# whisper.cpp — portable Windows build (CPU) + a multilingual GGML model used by
# the auto-subtitle (STT) feature. The model MUST be multilingual (not *.en) so
# whisper's translate task can emit English from foreign audio. ggml-base is the
# size/quality sweet spot for overnight bulk prep; admins can swap in a larger
# model by dropping it in tools/whisper/ and pointing _WHISPER_MODEL at it.
#
# The Windows binary zip's ASSET NAME is stable but the release TAG is not — a
# hardcoded tag 404s the moment a new release lands. So we resolve the URL from
# the GitHub releases API at install time and only fall back to a pinned
# known-good release if the API is unreachable.
#
# Three builds ship per release: the CPU build (`whisper-bin-x64.zip`) and two
# CUDA/cuBLAS builds (`whisper-cublas-12.x.x-bin-x64.zip`, `…-11.8.0-…`) for
# NVIDIA hosts. The CUDA builds bundle the CUDA runtime DLLs but still need a
# compatible NVIDIA driver; whisper.cpp auto-offloads to the GPU when run, and
# we force-fall-back to CPU at runtime (`-ng`) if CUDA init fails. CPU is the
# portable default; the GPU build is opt-in via the admin Components panel.
WHISPER_RELEASES_API     = "https://api.github.com/repos/ggml-org/whisper.cpp/releases/latest"
WHISPER_FALLBACK_VERSION = "1.8.4"   # known-good tag that publishes all three builds
WHISPER_MODEL_NAME = "ggml-base.bin"
WHISPER_MODEL_URL  = f"https://huggingface.co/ggerganov/whisper.cpp/resolve/main/{WHISPER_MODEL_NAME}"

# build key → (pinned fallback asset name, matcher against live release asset names)
_WHISPER_BUILDS = {
    "cpu":    ("whisper-bin-x64.zip",
               lambda n: n == "whisper-bin-x64.zip"),
    "cuda12": ("whisper-cublas-12.4.0-bin-x64.zip",
               lambda n: n.startswith("whisper-cublas-12") and n.endswith("-bin-x64.zip")),
    "cuda11": ("whisper-cublas-11.8.0-bin-x64.zip",
               lambda n: n.startswith("whisper-cublas-11") and n.endswith("-bin-x64.zip")),
}


def _resolve_whisper_win_url(build: str = "cpu") -> str:
    """Return the download URL for the requested whisper.cpp Windows build
    (`cpu` | `cuda12` | `cuda11`) from the GitHub releases API, or the pinned
    fallback if the API can't be reached / the asset isn't found."""
    fallback_asset, matcher = _WHISPER_BUILDS.get(build, _WHISPER_BUILDS["cpu"])
    fallback = (f"https://github.com/ggml-org/whisper.cpp/releases/download/"
                f"v{WHISPER_FALLBACK_VERSION}/{fallback_asset}")
    import urllib.request, json as _json
    try:
        req = urllib.request.Request(
            WHISPER_RELEASES_API,
            headers={"User-Agent": "StreamLink-setup",
                     "Accept": "application/vnd.github+json"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = _json.loads(resp.read().decode("utf-8", "replace"))
        for asset in data.get("assets", []):
            name = asset.get("name", "")
            if matcher(name) and asset.get("browser_download_url"):
                return asset["browser_download_url"]
        warn(f"whisper {build} build not found in the latest release; using pinned fallback.")
    except Exception as e:
        warn(f"Could not query whisper.cpp releases ({e}); using pinned fallback.")
    return fallback


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


def _download_whisper_model() -> Optional[str]:
    """Download the multilingual GGML model into tools/whisper/models/. Returns
    the saved path or None. Shared across platforms — the model is portable."""
    model_dir = TOOLS_DIR / "whisper" / "models"
    model_dir.mkdir(parents=True, exist_ok=True)
    dest = model_dir / WHISPER_MODEL_NAME
    if dest.exists() and dest.stat().st_size > 1_000_000:
        ok(f"whisper model already present → {dest}")
        return str(dest)
    note(f"Downloading whisper model {WHISPER_MODEL_NAME} (~148 MB) …")
    if _download_with_progress(WHISPER_MODEL_URL, dest):
        ok(f"whisper model → {dest}")
        return str(dest)
    warn("whisper model download failed.")
    return None


def _portable_install_whisper_windows() -> dict:
    """Download the portable whisper.cpp Windows build + model under ./tools/whisper/.

    Returns {"whisper": path or None, "whisper_model": path or None}.
    """
    result: dict = {"whisper": None, "whisper_model": None}
    TOOLS_DIR.mkdir(exist_ok=True)
    tmp_zip = TOOLS_DIR / "_dl_whisper.zip"
    wh_dir = TOOLS_DIR / "whisper"
    if _download_with_progress(_resolve_whisper_win_url(), tmp_zip):
        if _extract_archive(tmp_zip, wh_dir):
            wh_path = (_find_in_tree(wh_dir, ["whisper-cli.exe"])
                       or _find_in_tree(wh_dir, ["main.exe"])
                       or _find_in_tree(wh_dir, ["whisper.exe"]))
            if wh_path:
                result["whisper"] = wh_path
                ok(f"whisper.cpp → {wh_path}")
            else:
                warn("whisper-cli.exe not found inside the extracted archive.")
        tmp_zip.unlink(missing_ok=True)
    result["whisper_model"] = _download_whisper_model()
    return result


# ── Auto-install whisper.cpp + model (auto-subtitle / STT dep) ─────────────
def install_stt_deps(tools: dict) -> dict:
    """Offer to install whisper.cpp + a multilingual GGML model for the
    auto-subtitle (speech-to-text) feature. No-op if both are already detected.

    whisper.cpp generates a sidecar .srt for sources that ship no usable text
    subtitle. It's optional — declining only disables auto/AI subtitles; every
    other feature works without it.
    """
    if tools.get("whisper") and tools.get("whisper_model"):
        return tools

    header("Auto-Subtitle Dependencies (whisper.cpp + model)")
    note("Used to transcribe audio into subtitles when a file has none. Optional.")
    missing = []
    if not tools.get("whisper"):       missing.append("whisper.cpp")
    if not tools.get("whisper_model"): missing.append("GGML model")
    warn(f"Missing: {', '.join(missing)}")

    refreshed = dict(tools)
    if SYSTEM == "Windows":
        if not ask_bool("Download portable whisper.cpp + base model into ./tools/ (~180 MB)?",
                        default=True):
            note(f"Skipped. Enable later with: {whisper_install_hint()}")
            return tools
        portable = _portable_install_whisper_windows()
        if portable.get("whisper"):       refreshed["whisper"] = portable["whisper"]
        if portable.get("whisper_model"): refreshed["whisper_model"] = portable["whisper_model"]
    elif SYSTEM == "Darwin":
        brew = find_exe("brew", "/opt/homebrew/bin/brew", "/usr/local/bin/brew")
        if brew and not refreshed.get("whisper"):
            if ask_bool(f"Run `{brew} install whisper-cpp` now?", default=True):
                try:
                    subprocess.run([brew, "install", "whisper-cpp"], check=True)
                except subprocess.CalledProcessError as e:
                    warn(f"brew install failed (exit {e.returncode})")
            refreshed["whisper"] = find_exe(*whisper_candidates())
        elif not brew:
            warn("Homebrew not found — install whisper.cpp manually, then re-run setup.py.")
            note(f"Hint: {whisper_install_hint()}")
        if ask_bool("Download the multilingual whisper model (~148 MB) now?", default=True):
            refreshed["whisper_model"] = _download_whisper_model()
    else:
        # Linux: no reliable prebuilt; the model is still portable.
        if not refreshed.get("whisper"):
            warn("No prebuilt whisper.cpp for Linux — build it so `whisper-cli` is on PATH.")
            note(f"Hint: {whisper_install_hint()}")
        if ask_bool("Download the multilingual whisper model (~148 MB) now?", default=True):
            refreshed["whisper_model"] = _download_whisper_model()

    if refreshed.get("whisper") and refreshed.get("whisper_model"):
        ok("Auto-subtitles ready.")
    else:
        note("Auto-subtitles will stay disabled until both the binary and model are present.")
    return refreshed


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


# ── Jackett Windows service install ───────────────────────────────────────
def _is_windows_admin() -> bool:
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _jackett_service_installed() -> bool:
    try:
        r = vrun(["sc.exe", "query", "Jackett"], timeout=5)
    except Exception as exc:
        vlog(f"sc.exe query Jackett raised: {exc}")
        return False
    return r.returncode == 0


def _find_jackett_install_dir() -> Path | None:
    """Return the directory that contains Jackett's executables."""
    if SYSTEM != "Windows":
        return None
    candidates = _windows_jackett_candidates()
    vlog(f"Checking {len(candidates)} Jackett candidate paths …")
    for c in candidates:
        exists = Path(c).exists()
        if VERBOSE:
            vlog(f"  {'FOUND' if exists else '   - '} {c}")
        if exists:
            return Path(c).parent
    return None


def _jackett_service_sddl() -> str | None:
    """Return the Jackett service's security descriptor (SDDL), or None."""
    try:
        r = vrun(["sc.exe", "sdshow", "Jackett"], timeout=10)
    except Exception as exc:
        vlog(f"sc.exe sdshow raised: {exc}")
        return None
    out = (r.stdout or "").strip()
    for line in out.splitlines():
        line = line.strip()
        if line.startswith(("D:", "O:")) or "(A;" in line:
            return line
    return out or None


def grant_jackett_service_control() -> None:
    """Let the current (non-admin) user start/stop the Jackett service and set
    SCM crash-recovery, so the watchdog can recover a hung Jackett WITHOUT a UAC
    prompt or a reboot.

    Without this, a LocalSystem Jackett service can only be controlled by an
    administrator — so a non-elevated StreamLink watchdog can detect a hung
    Jackett but not restart it, and the only cure is a reboot. We additively
    grant Authenticated Users SERVICE_START + SERVICE_STOP via `sc sdset`, and
    set restart-on-failure actions. Idempotent and best-effort — never fatal.
    """
    if SYSTEM != "Windows" or not _jackett_service_installed():
        return

    header("Jackett Auto-Recovery Permissions")

    GRANT_ACE = "(A;;RPWP;;;AU)"   # Authenticated Users: SERVICE_START + SERVICE_STOP
    sddl = _jackett_service_sddl()
    vlog(f"Jackett SDDL: {sddl}")
    already = bool(sddl and GRANT_ACE in sddl)

    new_sddl: str | None = None
    if sddl and not already:
        if "S:" in sddl:
            d, s = sddl.split("S:", 1)
            new_sddl = f"{d}{GRANT_ACE}S:{s}"
        else:
            new_sddl = f"{sddl}{GRANT_ACE}"

    failure_args = ["reset=", "86400", "actions=",
                    "restart/5000/restart/5000/restart/5000"]

    if already:
        ok("This account can already start/stop the Jackett service.")
        # Refresh crash-recovery actions when we have the rights to do so.
        if _is_windows_admin():
            vrun(["sc.exe", "failure", "Jackett", *failure_args])
        return

    if _is_windows_admin():
        if new_sddl:
            r = vrun(["sc.exe", "sdset", "Jackett", new_sddl])
            if r.returncode == 0:
                ok("Granted this account permission to start/stop the Jackett service.")
            else:
                warn(f"sc.exe sdset Jackett failed (exit {r.returncode}).")
        r = vrun(["sc.exe", "failure", "Jackett", *failure_args])
        if r.returncode == 0:
            ok("Configured Jackett crash-recovery (auto-restart on failure).")
        return

    # Not admin → apply both in a single elevated shell (one UAC prompt).
    failure_cmd = "sc failure Jackett reset= 86400 actions= restart/5000/restart/5000/restart/5000"
    parts = []
    if new_sddl:
        parts.append(f'sc sdset Jackett "{new_sddl}"')
    parts.append(failure_cmd)
    chained = " & ".join(parts)
    note("Granting the watchdog permission to restart Jackett without a reboot.")
    info("Requesting elevation — accept the UAC prompt …")
    try:
        import ctypes
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", "cmd.exe", f"/c {chained}", None, 0)
        if rc <= 32:
            warn(f"Elevation declined (code {rc}).")
            note("Run these once in an elevated PowerShell to enable auto-recovery:")
            if new_sddl:
                note(f'  sc sdset Jackett "{new_sddl}"')
            note(f"  {failure_cmd}")
            return
        ok("Done — the watchdog can now restart a hung Jackett without admin or a reboot.")
    except Exception as exc:
        warn(f"Could not request elevation: {exc}")


def install_jackett_service() -> None:
    """Install and start the 'Jackett' Windows service so it runs as a
    background service from boot — the same action as the tray's
    'Start background service' menu item, just automated.

    Strategy: try `JackettConsole.exe --Install` first (canonical Jackett
    installer), fall back to `sc create` pointing at `JackettService.exe`
    if the console exe isn't present.
    """
    if SYSTEM != "Windows":
        return

    header("Jackett Windows Service")

    if _jackett_service_installed():
        ok("Jackett service already installed.")
        try:
            vrun(["sc.exe", "start", "Jackett"], timeout=10)
        except Exception as exc:
            vlog(f"sc.exe start raised: {exc}")
        grant_jackett_service_control()
        return

    jackett_dir = _find_jackett_install_dir()
    if not jackett_dir:
        warn("Jackett install directory not found — cannot install the service.")
        note("Re-run setup.py after Jackett finishes installing, or open the")
        note("Jackett tray icon → 'Start background service'.")
        return

    console = jackett_dir / "JackettConsole.exe"
    service = jackett_dir / "JackettService.exe"

    note(f"Installing 'Jackett' as a Windows service from {jackett_dir}")
    if VERBOSE:
        try:
            files = sorted(p.name for p in jackett_dir.iterdir() if p.is_file())
            vlog(f"Files in {jackett_dir}:")
            for f in files:
                vlog(f"  {f}")
        except Exception as exc:
            vlog(f"Could not list {jackett_dir}: {exc}")
        vlog(f"JackettConsole.exe present: {console.exists()}")
        vlog(f"JackettService.exe present: {service.exists()}")
        vlog(f"Running as admin: {_is_windows_admin()}")

    # ── Elevate if we're not admin ────────────────────────────────────────
    if not _is_windows_admin():
        target = console if console.exists() else service
        if not target.exists():
            warn(f"Neither JackettConsole.exe nor JackettService.exe under {jackett_dir}")
            note("Open the Jackett tray icon → 'Start background service' manually.")
            return
        warn("Administrator rights are required for service install.")
        info("Requesting elevation — accept the UAC prompt …")
        try:
            import ctypes
            rc = ctypes.windll.shell32.ShellExecuteW(
                None, "runas", str(target), "--Install", str(jackett_dir), 1
            )
            if rc <= 32:
                warn(f"Elevation declined or failed (ShellExecute code {rc}).")
                note("Open the Jackett tray icon → 'Start background service'.")
                return
        except Exception as exc:
            warn(f"Could not request elevation: {exc}")
            return
        # ShellExecuteW returns immediately; poll until the service shows up.
        import time as _time
        for _ in range(30):   # up to ~15 s
            _time.sleep(0.5)
            if _jackett_service_installed():
                break
    else:
        # We have admin — run --Install in-process, falling back to sc create.
        installed = False
        if console.exists():
            proc = vrun([str(console), "--Install"], cwd=str(jackett_dir))
            if proc.returncode == 0:
                installed = True
            else:
                warn(f"JackettConsole.exe --Install failed (exit {proc.returncode})")
        if not installed and service.exists():
            note("Falling back to: JackettService.exe --install …")
            proc = vrun([str(service), "--install"], cwd=str(jackett_dir))
            if proc.returncode == 0:
                installed = True
            else:
                warn(f"JackettService.exe --install failed (exit {proc.returncode})")
        if not installed and service.exists():
            note("Falling back to: sc.exe create Jackett …")
            proc = vrun([
                "sc.exe", "create", "Jackett",
                f"binPath={service}",
                "start=auto",
                "DisplayName=Jackett",
            ])
            if proc.returncode == 0:
                installed = True
            else:
                warn(f"sc.exe create Jackett failed (exit {proc.returncode})")
        if not installed:
            warn("Could not register the service automatically.")
            note(f"Inspect {jackett_dir} — verify Jackett exes are present.")
            return

    if _jackett_service_installed():
        ok("Jackett service installed.")
        try:
            r = vrun(["sc.exe", "start", "Jackett"], timeout=10)
            if r.returncode == 0 or "1056" in (r.stdout + r.stderr):
                ok("Jackett service started.")
            else:
                warn(f"sc.exe start Jackett returned {r.returncode}")
        except Exception as exc:
            vlog(f"sc.exe start raised: {exc}")
        grant_jackett_service_control()
    else:
        warn("Jackett service didn't appear — install may have been declined.")
        note("Open the Jackett tray icon → 'Start background service' manually.")


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

    # On Windows, warn loudly if this Python is installed per-user — a venv
    # built from it can only be executed by the same user. Anyone else
    # (including a scheduled-task account) gets "Access is denied" when the
    # venv launcher tries to re-exec the base python, and the service
    # silently fails. See docs/GOTCHAS.md "Microsoft Store Python".
    if SYSTEM == "Windows":
        exe = Path(sys.executable).resolve()
        s   = str(exe).lower()
        per_user_markers = (
            r"\appdata\local\microsoft\windowsapps",
            r"\appdata\local\programs\python",
            r"\appdata\local\packages\pythonsoftwarefoundation",
        )
        if any(m in s for m in per_user_markers):
            warn(f"Python is installed per-user ({exe}).")
            warn("Other Windows accounts (including the scheduled task that")
            warn("runs the service for non-admin users) will NOT be able to")
            warn("execute the venv. Install Python from python.org with")
            warn("'Install Python for all users' checked, then delete .venv")
            warn("and re-run this setup. See docs/GOTCHAS.md for details.")
            if not ask_bool("Continue anyway?", default=False):
                sys.exit(1)


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

    # In auto-update mode, treat pip failures as warnings — a transient network
    # glitch (or a PyPI hiccup) shouldn't kill the whole update. If deps are
    # actually missing the new code will fail at import time on next boot,
    # which the operator can debug separately. Interactive setup keeps the
    # original fail-loud behaviour.
    def _pip_step(cmd: list, label: str) -> bool:
        try:
            subprocess.run(cmd, check=True)
            return True
        except subprocess.CalledProcessError as exc:
            if AUTOUPDATE:
                warn(f"{label} failed (rc={exc.returncode}) — continuing in auto-update mode.")
                return False
            raise

    # Use 'python -m pip' to upgrade pip — calling pip.exe directly fails on Windows
    pip_ok = _pip_step(
        [str(python), "-m", "pip", "install", "-q", "--upgrade", "pip"],
        "pip self-upgrade",
    )
    reqs_ok = _pip_step(
        [str(pip), "install", "-q", "-r", str(HERE / "requirements.txt")],
        "requirements.txt install",
    )
    if pip_ok and reqs_ok:
        ok("All Python packages installed")
    else:
        warn("Some dependency steps were skipped; existing venv contents will be used.")


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
        ("whisper", "whisper.cpp",   whisper_candidates(), "https://github.com/ggml-org/whisper.cpp/releases"),
    ]

    for key, label, candidates, url in checks:
        path = find_exe(*candidates)
        tools[key] = path
        if path:
            ok(f"{label}: {path}")
        else:
            warn(f"{label} not found — download: {url}")

    # whisper.cpp model is a data file, not an exe — detect it separately.
    model_path = next(iter(whisper_model_candidates()), None)
    tools["whisper_model"] = model_path
    if model_path:
        ok(f"whisper model: {model_path}")

    if not tools.get("ffmpeg") or not tools.get("fpcalc"):
        note(f"Smart Skip (intro/credits auto-detection) needs both ffmpeg and fpcalc.")
        note(f"Install: {chromaprint_install_hint()}")

    return tools


# ── Step 4: Interactive configuration ─────────────────────────────────────
def gather_config(existing: dict | None = None) -> dict:
    header("Configuration")
    prev = existing or {}
    if prev:
        note("Current values are pre-filled — press Enter to keep each one.")
        note("Reviewing every value here doubles as a quick .env health check.")
    else:
        note("Press Enter to accept the default shown in brackets.")
    print()

    default_dl = str(Path.home() / "Downloads" / "StreamLink")

    cfg: dict[str, str] = {}

    # Plain field: default to the stored value, falling back to the factory default.
    def ask_field(label, key, factory=""):
        return ask(label, prev.get(key, factory))

    # Secret field: same defaulting, but never echo the stored value to the
    # terminal — show a masked placeholder so re-running setup can't leak it.
    def ask_secret(label, key, factory=""):
        cur = prev.get(key, factory)
        if key in prev:
            shown = "•••••• (Enter = keep)" if cur else "(currently blank)"
        else:
            shown = factory
        return ask(label, cur, secret=True, show_default=shown)

    print(f"  {BOLD}Jackett (indexer){RESET}")
    cfg["INDEXER_URL"]        = ask_field("URL",     "INDEXER_URL", "http://localhost:9117")
    cfg["INDEXER_API_KEY"]    = ask_field("API key", "INDEXER_API_KEY", "")
    cfg["JACKETT_PASSWORD"]   = ask_secret("Admin password (for indexer management, leave blank if none)", "JACKETT_PASSWORD", "")
    cfg["INDEXER_CATEGORIES"] = ask_field("Categories (0=all, 2000=Movies, 5000=TV)", "INDEXER_CATEGORIES", "0")
    print()
    print(f"  {BOLD}qBittorrent{RESET}")
    cfg["QBIT_URL"]           = ask_field("Web UI URL", "QBIT_URL", "http://localhost:8081")
    cfg["QBIT_USERNAME"]      = ask_field("Username",   "QBIT_USERNAME", "admin")
    cfg["QBIT_PASSWORD"]      = ask_secret("Password",  "QBIT_PASSWORD", "adminadmin")
    cfg["QBIT_DOWNLOAD_PATH"] = ask_field("Download folder", "QBIT_DOWNLOAD_PATH", default_dl)
    print()
    print(f"  {BOLD}VLC{RESET}")
    cfg["VLC_URL"]            = ask_field("HTTP URL",          "VLC_URL", "http://localhost:8080")
    cfg["VLC_PASSWORD"]       = ask_secret("Lua HTTP password", "VLC_PASSWORD", "vlcpassword")
    print()
    print(f"  {BOLD}Buffer thresholds (stream starts when either is met){RESET}")
    cfg["BUFFER_MIN_MB"]      = ask_field("Min MB", "BUFFER_MIN_MB", "15.0")
    cfg["BUFFER_MIN_PCT"]     = ask_field("Min %",  "BUFFER_MIN_PCT", "1.0")
    print()
    print(f"  {BOLD}Admin panel{RESET}")
    cfg["ADMIN_PASSWORD"]     = ask_secret("Admin password (leave blank to disable)", "ADMIN_PASSWORD", "")

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
               "ffmpeg": "_FFMPEG_BIN", "fpcalc": "_FPCALC_BIN",
               "whisper": "_WHISPER_BIN", "whisper_model": "_WHISPER_MODEL"}
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
               "ffmpeg": "_FFMPEG_BIN", "fpcalc": "_FPCALC_BIN",
               "whisper": "_WHISPER_BIN", "whisper_model": "_WHISPER_MODEL"}
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
    if AUTOUPDATE:
        # Skip OS-level app installs in the auto-updater context — winget/brew
        # need an interactive desktop and can hang or fail from a service
        # account. The admin chose these on first setup; we don't add or
        # remove them just because new code was pulled.
        note("Auto-update mode — skipping winget/brew app installs.")
    else:
        tools = install_core_deps(tools)
        install_jackett_service()
        tools = install_smart_skip_deps(tools)
        tools = install_stt_deps(tools)

    existing = parse_existing_env() if ENV.exists() else {}
    reuse_env = False
    if existing:
        if AUTOUPDATE:
            # Auto-updater path: never prompt, never re-prompt for values. The
            # admin's existing .env is authoritative; any new env keys
            # introduced by the new code are surfaced via the dashboard's
            # missing-env-keys banner so the admin fills them in post-update.
            reuse_env = True
            header("Existing .env detected")
            note(f"Found {ENV} — reusing in auto-update mode (no prompts).")
        else:
            header("Existing .env detected")
            note(f"Found {ENV}")
            note("Re-prompting pre-fills each current value (press Enter to keep it).")
            reuse_env = ask_bool("Reuse existing .env without re-prompting?", default=True)

    if reuse_env:
        cfg = existing
        merge_tool_paths(tools)
        ok("Skipped interactive configuration")
    else:
        cfg = gather_config(existing)
        configure_qbittorrent(cfg)
        write_env(cfg, tools)

    if cfg.get("QBIT_DOWNLOAD_PATH"):
        ensure_download_dir(cfg)
    generate_ssl_cert()

    if AUTOUPDATE:
        # The auto-updater calls daemon.uninstall() + daemon.install() itself
        # right after this script exits — don't duplicate the work here (and
        # don't risk a Windows UAC elevation prompt blowing up from a service
        # context, which has no interactive desktop).
        service_installed = False
        note("Auto-update mode — skipping service install (updater handles it).")
    else:
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
