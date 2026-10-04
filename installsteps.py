"""Everything the graphical installer decides, with no window attached.

`installer.py` is the Tk wizard; this is what it asks. Kept apart so the parts
that can be wrong without anyone noticing (which Python is safe to build a venv
from, what a `.env` rewrite keeps, whether a folder unpacked from a ZIP became a
real clone, whether Jackett accepted the key) run under `tests/test_installsteps.py`
on any machine, not only on a fresh Windows box.

Leaf module: stdlib only, no `main` / `run` / `setup` import, and it must run
under the **system** Python (the venv doesn't exist yet when it is first used).
See docs/INSTALLER.md.
"""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO_URL = "https://github.com/nmautz/torrentstreamingtool.git"
REPO_NAME = "torrentstreamingtool"
DEFAULT_BRANCH = "main"

# Factory defaults. tests/test_installsteps.py reads setup.py's gather_config()
# and fails if these drift from it. ADMIN_PASSWORD is blank there too: the
# wizard makes the person choose one instead of shipping a known password.
DEFAULTS = {
    "INDEXER_URL":        "http://localhost:9117",
    "INDEXER_API_KEY":    "",
    "JACKETT_PASSWORD":   "",
    "INDEXER_CATEGORIES": "0",
    "QBIT_URL":           "http://localhost:8081",
    "QBIT_USERNAME":      "admin",
    "QBIT_PASSWORD":      "adminadmin",
    "QBIT_DOWNLOAD_PATH": str(Path.home() / "Downloads" / "StreamLink"),
    "VLC_URL":            "http://localhost:8080",
    "VLC_PASSWORD":       "vlcpassword",
    "BUFFER_MIN_MB":      "15.0",
    "BUFFER_MIN_PCT":     "1.0",
    "ADMIN_PASSWORD":     "",
}

# Handed to setup.py as SL_<KEY>. The Jackett key and password are found or
# typed AFTER setup has installed Jackett, and written to .env directly.
INSTALL_KEYS = [k for k in DEFAULTS if k not in ("INDEXER_API_KEY", "JACKETT_PASSWORD")]

VPN_MODES = ("mullvad", "generic", "off")


# ── Child processes ───────────────────────────────────────────────────────
def child_env(base: dict | None = None) -> dict:
    """Environment for setup.py / run.py started from the wizard.

    Both print `✓` and box-drawing characters. Through a pipe on Windows,
    Python would otherwise encode them in the ANSI code page: run.py dies with
    UnicodeEncodeError, and setup.py (which forces UTF-8 itself) sends bytes
    the reader can't decode. Pin both ends to UTF-8.
    """
    env = dict(os.environ if base is None else base)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    return env


_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
# winget's spinner and progress bar: redrawn in place with \r, hundreds of times.
_NOISE = re.compile(r"^[\s\-\\|/█▒░]*(\d+(\.\d+)?\s*[KMG]?B\s*/\s*\d+(\.\d+)?\s*[KMG]?B)?[\s\d%]*$")


def clean_line(raw: str) -> str:
    """One line of child output as it should appear in the install log.

    Strips colour codes, keeps only what a `\\r`-redrawn line ended on, and
    returns "" for a line that is nothing but a spinner or a progress bar.
    """
    text = _ANSI.sub("", raw).rstrip("\r\n")
    if "\r" in text:
        parts = [p for p in text.split("\r") if p.strip()]
        text = parts[-1] if parts else ""
    if text.strip() and _NOISE.match(text):
        return ""
    return text.rstrip()


# ── Which Python ──────────────────────────────────────────────────────────
_PER_USER_MARKERS = (
    "\\appdata\\local\\microsoft\\windowsapps",
    "\\appdata\\local\\programs\\python",
    "\\appdata\\local\\packages\\pythonsoftwarefoundation",
)


def is_per_user_python(exe: str) -> bool:
    """True for a Windows Python only its installing user can execute.

    Same markers as setup.py's check_python(), which refuses to continue when
    there is nobody to ask. install.bat avoids these; this is for a wizard
    started by hand with the wrong interpreter.
    """
    s = str(exe).replace("/", "\\").lower()
    return any(m in s for m in _PER_USER_MARKERS)


def session_user() -> str:
    """The account signed in to this desktop session ("" if unknown).

    When a standard user approves the admin prompt with ANOTHER account's
    password, the wizard runs as that other account: `Path.home()`, the
    download folder and qBittorrent.ini would all land in the wrong profile.
    The process's own name can't show that; the session's owner can.
    """
    if os.name != "nt":
        return ""
    try:
        import ctypes
        from ctypes import wintypes

        sid = wintypes.DWORD()
        if not ctypes.windll.kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(sid)):
            return ""
        buf = ctypes.c_void_p()
        n = wintypes.DWORD()
        WTS_USER_NAME = 5
        if not ctypes.windll.wtsapi32.WTSQuerySessionInformationW(
                0, sid.value, WTS_USER_NAME, ctypes.byref(buf), ctypes.byref(n)):
            return ""
        try:
            return ctypes.wstring_at(buf.value) if buf.value else ""
        finally:
            ctypes.windll.wtsapi32.WTSFreeMemory(buf)
    except Exception:
        return ""


def wrong_account(process_user: str, desktop_user: str) -> bool:
    """True when the wizard is running as someone other than who is signed in."""
    if not process_user or not desktop_user:
        return False
    return process_user.strip().lower() != desktop_user.strip().lower()


# ── .env ──────────────────────────────────────────────────────────────────
def read_env(path: Path) -> dict:
    env: dict[str, str] = {}
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return env
    for raw in text.splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip()
    return env


def set_env_keys(path: Path, updates: dict) -> None:
    """Replace or append KEY=value lines, leaving every other line as it was."""
    path = Path(path)
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        lines = []
    todo = dict(updates)
    out = []
    for line in lines:
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            k = s.split("=", 1)[0].strip()
            if k in todo:
                out.append(f"{k}={todo.pop(k)}")
                continue
        out.append(line)
    for k, v in todo.items():
        out.append(f"{k}={v}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def port_of(url: str, default: int) -> int:
    m = re.search(r":(\d+)", url or "")
    return int(m.group(1)) if m else default


def is_local_url(url: str) -> bool:
    m = re.search(r"https?://\[?([^\]:/]+)", url or "")
    return (m.group(1).lower() if m else "") in ("localhost", "127.0.0.1", "::1")


def validate_settings(v: dict) -> str | None:
    """Why these settings can't be installed, or None. First problem only."""
    pw = v.get("ADMIN_PASSWORD", "")
    if not pw.strip():
        return ("Choose an admin password. It protects the settings panel, which "
                "anyone on your network can reach.")
    for key, label in (("ADMIN_PASSWORD", "admin password"),
                       ("QBIT_PASSWORD", "qBittorrent password"),
                       ("VLC_PASSWORD", "VLC password"),
                       ("QBIT_USERNAME", "qBittorrent username")):
        val = v.get(key, "")
        if not val:
            return f"The {label} can't be blank."
        # .env has no quoting we can rely on: these would be read back changed.
        if val != val.strip() or "\n" in val or " #" in val or val[0] in "\"'#":
            return (f"The {label} can't start or end with a space, start with a "
                    "quote or #, or contain ' #'.")
    folder = v.get("QBIT_DOWNLOAD_PATH", "").strip()
    if not folder:
        return "Choose a download folder."
    if not (os.path.isabs(folder) or re.match(r"^[A-Za-z]:[\\/]", folder)):
        return "The download folder must be a full path, like D:\\StreamLink."
    for key in ("INDEXER_URL", "QBIT_URL", "VLC_URL"):
        if not re.match(r"^https?://[^\s/]+", v.get(key, "")):
            return f"{key.replace('_', ' ')} must start with http:// or https://"
    if port_of(v["QBIT_URL"], 8081) == port_of(v["VLC_URL"], 8080) \
            and is_local_url(v["QBIT_URL"]) and is_local_url(v["VLC_URL"]):
        return "qBittorrent and VLC are set to the same port. Give them different ports."
    for key, label in (("BUFFER_MIN_MB", "Buffer (MB)"), ("BUFFER_MIN_PCT", "Buffer (%)")):
        try:
            if float(v.get(key, "")) < 0:
                raise ValueError
        except ValueError:
            return f"{label} must be a number."
    return None


def setup_env(values: dict, vpn_mode: str, base: dict | None = None) -> dict:
    """The environment that makes setup.py apply the wizard's choices."""
    env = child_env(base)
    env["STREAMLINK_WIZARD"] = "1"
    env["STREAMLINK_VPN_MODE"] = vpn_mode if vpn_mode in VPN_MODES else "mullvad"
    # The wizard registers the service itself, last. See installer.py.
    env["STREAMLINK_INSTALL_SERVICE"] = "0"
    for key in INSTALL_KEYS:
        if key in values:
            env["SL_" + key] = values[key]
    return env


# ── A folder that came from a ZIP ─────────────────────────────────────────
def zip_branch(folder_name: str) -> str:
    """The branch a GitHub ZIP was made from, read off its folder name.

    GitHub names the folder `<repo>-<branch>`; Windows adds " (1)" to a second
    download. Anything else says nothing, and "" means "use the default".
    """
    name = re.sub(r"\s*\(\d+\)$", "", folder_name.strip())
    prefix = REPO_NAME + "-"
    if name.lower().startswith(prefix) and len(name) > len(prefix):
        return name[len(prefix):]
    return ""


def find_git(environ: dict | None = None) -> str | None:
    import shutil
    found = shutil.which("git")
    if found:
        return found
    env = os.environ if environ is None else environ
    # A Git that winget installed a minute ago is not on this process's PATH.
    for root in (env.get("ProgramFiles"), env.get("ProgramW6432"),
                 env.get("ProgramFiles(x86)"), env.get("LOCALAPPDATA")):
        if not root:
            continue
        for rel in ("Git/cmd/git.exe", "Programs/Git/cmd/git.exe"):
            p = Path(root) / rel
            if p.exists():
                return str(p)
    return None


WINGET_GIT = ["install", "-e", "--id", "Git.Git", "--scope", "machine", "--silent",
              "--accept-package-agreements", "--accept-source-agreements"]


def _git(git: str, root: Path, *args: str, timeout: float = 600.0):
    kw: dict = {}
    if os.name == "nt":
        kw["creationflags"] = subprocess.CREATE_NO_WINDOW
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"       # never wait on a hidden credential prompt
    try:
        p = subprocess.run([git, *args], cwd=str(root), capture_output=True,
                           encoding="utf-8", errors="replace", timeout=timeout,
                           stdin=subprocess.DEVNULL, env=env, **kw)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)
    return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()


# Files the installer cannot work without. A branch that lacks them predates
# it: switching this folder to that branch would delete the wizard mid-run and
# hand setup.py a seam it doesn't have.
_INSTALLER_FILES = ("installer.py", "installsteps.py")


def ensure_clone(root: Path, git: str, log, url: str = REPO_URL,
                 branch: str = "") -> str:
    """Make `root` a git clone so Admin → Updates works. Returns what happened:

    "already"   it was a clone; untouched
    "cloned"    it was a plain folder (a ZIP); now tracks origin/<branch>
    "skipped"   left as a plain folder, and `log` was told why

    Never raises, and never fails the install: a box without updates still
    works, it only has to be updated by hand.
    """
    root = Path(root)
    if (root / ".git").exists():
        return "already"

    want = branch or zip_branch(root.name) or DEFAULT_BRANCH
    log(f"This folder is not a git clone (it came from a ZIP). Connecting it to {url} …")
    steps_undo = root / ".git"

    def give_up(why: str) -> str:
        log(f"Could not set up updates: {why}")
        log("StreamLink will still install. To update later, download a new ZIP.")
        # A half-made .git would make the updater think it can work.
        if steps_undo.exists():
            import shutil
            shutil.rmtree(steps_undo, ignore_errors=True)
        return "skipped"

    rc, out = _git(git, root, "init", "-q")
    if rc:
        return give_up(out or "git init failed")
    rc, out = _git(git, root, "remote", "add", "origin", url)
    if rc:
        return give_up(out or "git remote add failed")

    tried = [want] if want == DEFAULT_BRANCH else [want, DEFAULT_BRANCH]
    chosen = ""
    for b in tried:
        rc, out = _git(git, root, "fetch", "-q", "origin", b)
        if rc:
            log(f"  branch '{b}' could not be fetched.")
            continue
        missing = [f for f in _INSTALLER_FILES
                   if _git(git, root, "cat-file", "-e", f"FETCH_HEAD:{f}")[0]]
        if missing:
            log(f"  branch '{b}' does not have the installer yet.")
            continue
        chosen = b
        break
    if not chosen:
        return give_up("no branch on the server contains this installer "
                       "(or there is no internet connection)")

    # FETCH_HEAD, not origin/<b>: a single-branch fetch only writes the
    # remote-tracking ref opportunistically. Pin it ourselves.
    _git(git, root, "update-ref", f"refs/remotes/origin/{chosen}", "FETCH_HEAD")
    rc, out = _git(git, root, "checkout", "-q", "-f", "-B", chosen, f"origin/{chosen}")
    if rc:
        return give_up(out or "git checkout failed")
    _git(git, root, "branch", "-q", f"--set-upstream-to=origin/{chosen}", chosen)
    # The wizard runs elevated, so .git is owned by Administrators; the service
    # runs unelevated and git would refuse the folder as "dubious ownership".
    _git(git, root, "config", "--global", "--add", "safe.directory",
         str(root.resolve()).replace("\\", "/"))
    log(f"Connected to branch '{chosen}'. Updates will come from Admin → Updates.")
    return "cloned"


# ── Things other apps wrote ───────────────────────────────────────────────
def jackett_config_paths(environ: dict | None = None) -> list[Path]:
    """Where Jackett keeps ServerConfig.json, most likely first."""
    env = os.environ if environ is None else environ
    out: list[Path] = []
    if env.get("ProgramData"):                     # the Windows service
        out.append(Path(env["ProgramData"]) / "Jackett" / "ServerConfig.json")
    if env.get("APPDATA"):                         # run from the tray, per user
        out.append(Path(env["APPDATA"]) / "Jackett" / "ServerConfig.json")
    home = Path(env.get("HOME") or Path.home())
    out.append(home / ".config" / "Jackett" / "ServerConfig.json")
    out.append(home / "Library" / "Application Support" / "Jackett" / "ServerConfig.json")
    return out


def read_jackett_key(paths: list[Path]) -> str:
    """Jackett's API key, from the file Jackett writes on its first start.

    The key exists as soon as Jackett has run once; no indexer is needed and
    nobody has to copy it out of a web page.
    """
    for p in paths:
        try:
            data = json.loads(Path(p).read_text(encoding="utf-8-sig", errors="replace"))
        except (OSError, ValueError):
            continue
        key = str((data or {}).get("APIKey") or "").strip()
        if re.fullmatch(r"[A-Za-z0-9]{16,64}", key):
            return key
    return ""


def vlcrc_path(environ: dict | None = None) -> Path:
    env = os.environ if environ is None else environ
    if env.get("APPDATA"):
        return Path(env["APPDATA"]) / "vlc" / "vlcrc"
    home = Path(env.get("HOME") or Path.home())
    mac = home / "Library" / "Preferences" / "org.videolan.vlc" / "vlcrc"
    return mac if mac.parent.exists() else home / ".config" / "vlc" / "vlcrc"


def vlc_first_run_done(vlcrc_text: str) -> bool:
    """Has VLC's first-run privacy dialog been answered?

    Until it is, VLC shows the dialog on every launch and its web control
    doesn't come up behind it. Answering it writes `qt-privacy-ask=0`; the
    commented default (`#qt-privacy-ask=1`) means it is still waiting.
    """
    for raw in vlcrc_text.splitlines():
        line = raw.strip()
        if line.startswith("qt-privacy-ask="):
            return line.split("=", 1)[1].strip() == "0"
    return False


def mullvad_app(cli_path: str, environ: dict | None = None) -> str:
    """The Mullvad window's exe, next to the CLI that setup.py detected."""
    env = os.environ if environ is None else environ
    cands = []
    if cli_path:
        cli = Path(cli_path)
        cands += [cli.parent.parent / "Mullvad VPN.exe", cli.parent / "Mullvad VPN.exe"]
    for root in (env.get("ProgramFiles"), env.get("ProgramW6432")):
        if root:
            cands.append(Path(root) / "Mullvad VPN" / "Mullvad VPN.exe")
    cands.append(Path("/Applications/Mullvad VPN.app"))
    for c in cands:
        if c.exists():
            return str(c)
    return ""


def lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("192.0.2.1", 9))        # no packet is sent; picks a route
            ip = s.getsockname()[0]
        finally:
            s.close()
        return "" if ip.startswith("127.") else ip
    except OSError:
        return ""


def port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.8) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# ── Does the key work ─────────────────────────────────────────────────────
def _http_get(url: str, timeout: float = 10.0):
    """(status, body). status 0 = could not connect, body = why."""
    req = urllib.request.Request(url, headers={"User-Agent": "StreamLink-Installer"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(400_000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            body = e.read(400_000).decode("utf-8", "replace")
        except Exception:
            body = ""
        return e.code, body
    except Exception as e:                       # URLError, timeout, bad URL
        return 0, str(getattr(e, "reason", e))


def parse_indexers(status: int, body: str):
    """Read Jackett's answer to `t=indexers&configured=true`.

    Returns (state, count): "ok" with how many indexers are set up, "badkey",
    or "down" (not reachable / not Jackett).
    """
    if status == 0:
        return "down", 0
    m = re.search(r'<error\b[^>]*description="([^"]*)"', body)
    if status in (401, 403) or (m and "api key" in m.group(1).lower()):
        return "badkey", 0
    if "<indexers" not in body:
        return "down", 0
    return "ok", len(re.findall(r"<indexer\b", body))


def check_jackett(url: str, key: str):
    q = urllib.parse.urlencode({"apikey": key, "t": "indexers", "configured": "true"})
    status, body = _http_get(f"{url.rstrip('/')}/api/v2.0/indexers/all/results/torznab/api?{q}")
    return parse_indexers(status, body)


def looks_like_tmdb_v4(key: str) -> bool:
    """TMDb shows two credentials; the long 'Read Access Token' is the wrong one."""
    k = key.strip()
    return k.startswith("eyJ") or len(k) > 60


def check_tmdb(key: str) -> str:
    """"ok", "badkey" or "down"."""
    q = urllib.parse.urlencode({"api_key": key.strip()})
    status, _ = _http_get(f"https://api.themoviedb.org/3/configuration?{q}")
    if status == 200:
        return "ok"
    return "badkey" if status in (401, 403, 404) else "down"


# ── Checks that need run.py (so they run under the venv's Python) ─────────
# Each prints one `RESULT:<word>` line; everything else is run.py's own output.

VPN_DRIVER = """
import sys, run
print("RESULT:" + ("ok" if run.check_vpn() else "down"))
"""

JACKETT_DRIVER = """
import sys, run
print("RESULT:" + ("ok" if run.start_jackett() else "down"))
"""

# qBittorrent is started only if the VPN check passes, and stopped again if
# this check is what started it: the wizard runs elevated, and an elevated
# qBittorrent is one the unelevated watchdog could not kill when the VPN drops.
QBIT_DRIVER = """
import re, sys, time, urllib.parse, urllib.request
import run

url = run.e("QBIT_URL", "http://localhost:8081").rstrip("/")
port = run.extract_port(url, 8081)
m = re.search(r"https?://([^:/]+)", url)
local = (m.group(1) if m else "localhost") in ("localhost", "127.0.0.1", "::1")
started = False
try:
    if local and not run.port_open(port):
        if not run.check_vpn():
            print("RESULT:vpn"); sys.exit(0)
        started = True
        if not run.start_qbittorrent():
            print("RESULT:nostart"); sys.exit(0)
    data = urllib.parse.urlencode({
        "username": run.e("QBIT_USERNAME", "admin"),
        "password": run.e("QBIT_PASSWORD", "adminadmin")}).encode()
    answer = ""
    for _ in range(8):
        try:
            req = urllib.request.Request(url + "/api/v2/auth/login", data=data,
                                         headers={"Referer": url})
            answer = urllib.request.urlopen(req, timeout=5).read().decode("utf-8", "replace").strip()
            break
        except Exception as exc:
            answer = "ERR " + str(exc)
            time.sleep(1)
    if answer == "Ok.":
        print("RESULT:ok")
    elif answer.startswith("ERR"):
        print(answer); print("RESULT:noweb")
    else:
        print("RESULT:login")
finally:
    if started:
        run.kill_by_name("qbittorrent")
"""


def result_of(output: str) -> str:
    """The `RESULT:` word a driver printed ("" if it crashed before printing)."""
    for line in reversed(output.splitlines()):
        if line.startswith("RESULT:"):
            return line[len("RESULT:"):].strip()
    return ""


def last_detail(output: str) -> str:
    """The last thing run.py said, for the line under a failed check."""
    for raw in reversed(output.splitlines()):
        line = clean_line(raw).strip()
        if line and not line.startswith("RESULT:"):
            return re.sub(r"^[✓⚠✗→]\s*", "", line)
    return ""


def venv_python(root: Path) -> Path | None:
    cand = Path(root) / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    return cand if cand.exists() else None


def run_driver(root: Path, driver: str, timeout: float = 150.0) -> str:
    """Run one of the drivers above; returns its whole output."""
    py = venv_python(root)
    if not py:
        return "RESULT:novenv"
    kw: dict = {}
    if os.name == "nt":
        kw["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        p = subprocess.run([str(py), "-c", driver], cwd=str(root), env=child_env(),
                           capture_output=True, encoding="utf-8", errors="replace",
                           timeout=timeout, stdin=subprocess.DEVNULL, **kw)
        return (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return "RESULT:timeout"
    except OSError as exc:
        return f"{exc}\nRESULT:crash"
