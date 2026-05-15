"""P2P StreamLink v2.0 — FastAPI backend"""

import asyncio
import base64
import gzip
import hashlib
import io
import json
import os
import platform
import re
import secrets
import shutil
import socket
import struct
import subprocess
import threading
import time
import uuid
import zipfile
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncGenerator, Optional
from urllib.parse import quote, unquote, urlparse

import httpx
import psutil
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

import analyzer


# ── Settings ──────────────────────────────────────────────────────────────────

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    indexer_url: str = "http://localhost:9117"
    indexer_api_key: str = ""
    indexer_categories: str = "0"

    qbit_url: str = "http://localhost:8081"
    qbit_username: str = "admin"
    qbit_password: str = "adminadmin"
    qbit_download_path: str = str(Path.home() / "Downloads" / "StreamLink")
    library_path_2: str = ""   # optional extra storage locations (LIBRARY_PATH_2 in .env)
    library_path_3: str = ""
    library_path_4: str = ""

    vlc_url: str = "http://localhost:8080"
    vlc_password: str = "vlcpassword"

    buffer_min_mb: float = 15.0
    buffer_min_pct: float = 1.0

    admin_password: str = ""   # if empty, admin panel is disabled
    jackett_password: str = ""  # Jackett UI login password for indexer management

    # OpenSubtitles legacy REST API (rest.opensubtitles.org) needs no key, only a
    # User-Agent. "TemporaryUserAgent" is OpenSubtitles' documented testing UA.
    opensubtitles_user_agent: str = "TemporaryUserAgent"


settings = Settings()
LIBRARY_FILE = Path(__file__).parent / "library.json"
_lib_lock: asyncio.Lock  # initialised in lifespan


# ── Library Storage ───────────────────────────────────────────────────────────

def _migrate_item(item: dict) -> dict:
    """Upgrade items written by older versions of the server in-place."""
    # v2.0 → v2.1: flat file_path → files list
    if not item.get("files") and item.get("file_path"):
        item["files"] = [{
            "name": Path(item["file_path"]).name,
            "path": item["file_path"],
            "size_bytes": item.get("size_bytes", 0),
            "season": item.get("season", 0),
            "episode": item.get("episode", 0),
        }]
    # v2.0 → v2.1: flat per-profile progress → per-file progress
    for prof_id, prog in list(item.get("progress", {}).items()):
        if isinstance(prog, dict) and "file_progress" not in prog and "position_sec" in prog:
            file_path = item.get("file_path", "")
            if file_path:
                item["progress"][prof_id] = {
                    "last_file": file_path,
                    "file_progress": {
                        file_path: {
                            "position_sec": prog.get("position_sec", 0),
                            "duration_sec": prog.get("duration_sec", 0),
                            "completed": prog.get("completed", False),
                            "updated_at": prog.get("updated_at", ""),
                        }
                    },
                }
    return item


def _load_lib_raw() -> dict:
    if LIBRARY_FILE.exists():
        try:
            raw = json.loads(LIBRARY_FILE.read_text(encoding="utf-8"))
            raw["items"] = [_migrate_item(it) for it in raw.get("items", [])]
            return raw
        except Exception:
            pass
    return {"profiles": [], "items": []}


def _save_lib_raw(data: dict) -> None:
    LIBRARY_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


async def get_library() -> dict:
    async with _lib_lock:
        return _load_lib_raw()


async def put_library(data: dict) -> None:
    async with _lib_lock:
        _save_lib_raw(data)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── Global State ──────────────────────────────────────────────────────────────

@dataclass
class AppState:
    vpn_secure: bool = True
    vpn_status_text: str = "Checking…"
    active_hash: Optional[str] = None
    active_title: Optional[str] = None
    active_file: Optional[Path] = None
    stream_status: str = "idle"   # idle | buffering | playing | error
    progress: float = 0.0
    downloaded_mb: float = 0.0
    total_mb: float = 0.0
    dl_speed_bps: int = 0
    ul_speed_bps: int = 0
    stream_task: Optional[asyncio.Task] = None
    library_item_id: Optional[str] = None
    library_profile_id: Optional[str] = None
    library_item_file_count: int = 0                     # total files in the playing item
    library_playlist: list = field(default_factory=list)  # ordered file paths
    library_current_file: Optional[str] = None            # path VLC is playing now
    downloading_count: int = 0                            # active library downloads
    play_when_ready_item_id: Optional[str] = None        # auto-play this item on download complete
    play_when_ready_profile_id: Optional[str] = None
    play_when_ready_file_path: Optional[str] = None      # if set, wait for this specific file
    current_audio_track: int = -1                         # last track ID sent to VLC
    current_subtitle_track: int = -1                      # last track ID sent to VLC
    track_pref_applied_file: Optional[str] = None         # path for which track prefs were last applied
    vlc_time: int = 0                                     # VLC current position (seconds)
    vlc_duration: int = 0                                 # VLC total duration (seconds)
    vlc_volume: int = 100                                 # VLC volume 0-200 (100 = normal)
    prepare_hash: Optional[str] = None                    # hash added by /stream/prepare, pending user selection
    skip_offer: Optional[dict] = None                     # {"type": "intro"|"credits", "end_at": s, "next_item_id": id?, "next_file_path": p?}
    skip_offer_file: Optional[str] = None                 # path the current skip offer corresponds to
    analysis_jobs: dict = field(default_factory=dict)     # series_key → {status, stage, current, total, message, item_ids, started_at, finished_at}
    sse_queues: list = field(default_factory=list)


state = AppState()
qbit: Optional[httpx.AsyncClient] = None
_admin_sessions: dict[str, float] = {}   # token → expiry Unix timestamp

# ── Jackett Session ───────────────────────────────────────────────────────────
_jackett_cookie: str = ""
_jackett_cookie_expiry: float = 0.0
_jackett_cookie_lock: asyncio.Lock  # initialised in lifespan


async def _jackett_login() -> str:
    """Login to Jackett and return the session cookie value."""
    async with httpx.AsyncClient(follow_redirects=True, timeout=10.0) as tmp:
        await tmp.get(f"{settings.indexer_url}/UI/Login")
        r = await tmp.post(
            f"{settings.indexer_url}/UI/Dashboard",
            data={"password": settings.jackett_password},
            follow_redirects=False,
        )
    cookie = r.cookies.get("Jackett")
    if not cookie:
        raise HTTPException(502, "Could not authenticate with Jackett — check JACKETT_PASSWORD in .env")
    return cookie


@asynccontextmanager
async def _jackett_admin():
    """Yield an httpx client authenticated to Jackett's admin API."""
    global _jackett_cookie, _jackett_cookie_expiry
    cookies: dict[str, str] = {}
    if settings.jackett_password:
        async with _jackett_cookie_lock:
            if not _jackett_cookie or time.time() >= _jackett_cookie_expiry:
                _jackett_cookie = await _jackett_login()
                _jackett_cookie_expiry = time.time() + 3600
        cookies = {"Jackett": _jackett_cookie}
    async with httpx.AsyncClient(cookies=cookies, timeout=15.0) as c:
        yield c


# ── SSE Helpers ───────────────────────────────────────────────────────────────

async def broadcast(event: str, data: dict) -> None:
    msg = f"event: {event}\ndata: {json.dumps(data)}\n\n"
    dead = []
    for q in state.sse_queues:
        try:
            q.put_nowait(msg)
        except asyncio.QueueFull:
            dead.append(q)
    for q in dead:
        state.sse_queues.remove(q)


def state_snapshot() -> dict:
    playlist = state.library_playlist
    current  = state.library_current_file
    try:
        cur_idx = playlist.index(current) if (current and current in playlist) else -1
    except (ValueError, AttributeError):
        cur_idx = -1
    return {
        "vpn_secure": state.vpn_secure,
        "vpn_status": state.vpn_status_text,
        "stream_status": state.stream_status,
        "active_title": state.active_title,
        "progress": round(state.progress, 2),
        "downloaded_mb": round(state.downloaded_mb, 2),
        "total_mb": round(state.total_mb, 2),
        "dl_speed_bps": state.dl_speed_bps,
        "ul_speed_bps": state.ul_speed_bps,
        "downloading_count": state.downloading_count,
        "vlc_time": state.vlc_time,
        "vlc_duration": state.vlc_duration,
        "vlc_volume": state.vlc_volume,
        "library_playlist_count": len(playlist),
        "library_current_index": cur_idx,
        "library_current_file": current,
        "library_item_file_count": state.library_item_file_count,
        "is_library_playback": state.library_item_id is not None,
        "play_when_ready_item_id": state.play_when_ready_item_id,
        "play_when_ready_file_path": state.play_when_ready_file_path,
        "skip_offer": state.skip_offer,
        "analysis_jobs": state.analysis_jobs,
    }


# ── Admin Auth ────────────────────────────────────────────────────────────────

def _check_admin(request: Request) -> bool:
    """Return True if the request carries a valid admin session token."""
    if not settings.admin_password:
        return False
    token: Optional[str] = None
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
    if not token:
        token = request.headers.get("x-admin-token", "").strip() or None
    if not token:
        token = request.query_params.get("admin_token", "").strip() or None
    if not token or token not in _admin_sessions:
        return False
    if time.time() > _admin_sessions[token]:
        _admin_sessions.pop(token, None)
        return False
    return True


def _require_admin(request: Request) -> None:
    if not _check_admin(request):
        raise HTTPException(401, "Admin authentication required.")


def _pin_hash(pin: str) -> str:
    return hashlib.sha256(pin.encode()).hexdigest()


# ── qBittorrent Client ────────────────────────────────────────────────────────

async def qbit_login() -> bool:
    try:
        r = await qbit.post(
            f"{settings.qbit_url}/api/v2/auth/login",
            data={"username": settings.qbit_username, "password": settings.qbit_password},
        )
        return r.text.strip() == "Ok."
    except Exception:
        return False


async def qreq(method: str, path: str, **kw) -> Optional[httpx.Response]:
    """Make an authenticated qBit API request, re-logging in on 403."""
    try:
        r = await qbit.request(method, f"{settings.qbit_url}{path}", **kw)
        if r.status_code == 403:
            await qbit_login()
            r = await qbit.request(method, f"{settings.qbit_url}{path}", **kw)
        return r
    except Exception:
        return None


async def qbit_add_magnet(
    magnet: str,
    save_path: Optional[str] = None,
    sequential: bool = False,
) -> Optional[str]:
    h = extract_hash(magnet)
    data: dict = {"urls": magnet, "savepath": save_path or settings.qbit_download_path}
    if sequential:
        # Set sequential download at add-time so it applies before any pieces download.
        # This is the qBittorrent /add API field, separate from the toggle endpoint.
        data["sequentialDownload"] = "true"
    r = await qreq("POST", "/api/v2/torrents/add", data=data)
    return h if (r and r.text.strip() == "Ok.") else None


async def qbit_set_file_priority(h: str, indices: list[int], priority: int) -> None:
    """Set qBit file priority for a list of file indices. priority 0=skip, 1=normal, 6=high."""
    if not indices:
        return
    await qreq(
        "POST", "/api/v2/torrents/filePrio",
        data={"hash": h, "id": "|".join(str(i) for i in indices), "priority": str(priority)},
    )


async def qbit_streaming_mode(h: str) -> None:
    """Ensure sequential download is on.

    The correct API endpoint is toggleSequentialDownload (not setSequentialDownload,
    which does not exist).  Because it is a toggle, we read seq_dl from torrent info
    first and only call it when sequential is currently off.  This is a belt-and-
    suspenders check — sequential=true is already passed at add-time via qbit_add_magnet.
    """
    info = await qbit_info(h)
    if info is not None and not info.get("seq_dl", False):
        await qreq("POST", "/api/v2/torrents/toggleSequentialDownload",
                   data={"hashes": h})


async def qbit_info(h: str) -> Optional[dict]:
    r = await qreq("GET", f"/api/v2/torrents/info?hashes={h}")
    if r and r.status_code == 200:
        data = r.json()
        return data[0] if data else None
    return None


async def qbit_files(h: str) -> list:
    r = await qreq("GET", f"/api/v2/torrents/files?hash={h}")
    return r.json() if (r and r.status_code == 200) else []


async def qbit_delete(h: str, delete_files: bool = True) -> None:
    await qreq("POST", "/api/v2/torrents/delete",
                data={"hashes": h, "deleteFiles": "true" if delete_files else "false"})


# ── VLC Client ────────────────────────────────────────────────────────────────

async def vlc(command: str, **params) -> None:
    try:
        async with httpx.AsyncClient() as c:
            await c.get(
                f"{settings.vlc_url}/requests/status.xml",
                auth=httpx.BasicAuth("", settings.vlc_password),
                params={"command": command, **params},
                timeout=5.0,
            )
    except Exception:
        pass


async def vlc_status() -> Optional[dict]:
    """Return VLC's current status JSON (includes 'time' and 'length' in seconds)."""
    try:
        async with httpx.AsyncClient() as c:
            r = await c.get(
                f"{settings.vlc_url}/requests/status.json",
                auth=httpx.BasicAuth("", settings.vlc_password),
                timeout=3.0,
            )
            if r.status_code == 200:
                return r.json()
    except Exception:
        pass
    return None



async def vlc_playlist_uri() -> Optional[str]:
    """Return the file:// URI of the currently active VLC playlist item."""
    try:
        async with httpx.AsyncClient() as c:
            r = await c.get(
                f"{settings.vlc_url}/requests/playlist.json",
                auth=httpx.BasicAuth("", settings.vlc_password),
                timeout=3.0,
            )
            if r.status_code == 200:
                def _find_current(node: dict) -> Optional[str]:
                    if node.get("current") == "current":
                        return node.get("uri")
                    for child in node.get("children", []):
                        found = _find_current(child)
                        if found:
                            return found
                    return None
                return _find_current(r.json())
    except Exception:
        pass
    return None


def uri_to_path(uri: str) -> str:
    """Convert a file:// URI back to an absolute file path."""
    path = unquote(urlparse(uri).path)
    if platform.system() == "Windows":
        path = path.lstrip("/")
    return path


# ── Subtitle Download (OpenSubtitles) ─────────────────────────────────────────

def _opensubtitles_hash(path: Path) -> Optional[str]:
    """Compute the OpenSubtitles movie hash: 64-bit sum of filesize + first 64 KB
    + last 64 KB, read as little-endian 8-byte ints. Returns 16-char hex."""
    try:
        fmt = "<q"
        chunk = 65536
        bsize = struct.calcsize(fmt)
        filesize = path.stat().st_size
        if filesize < chunk * 2:
            return None
        h = filesize
        with path.open("rb") as f:
            for _ in range(chunk // bsize):
                (val,) = struct.unpack(fmt, f.read(bsize))
                h = (h + val) & 0xFFFFFFFFFFFFFFFF
            f.seek(filesize - chunk, 0)
            for _ in range(chunk // bsize):
                (val,) = struct.unpack(fmt, f.read(bsize))
                h = (h + val) & 0xFFFFFFFFFFFFFFFF
        return f"{h:016x}"
    except Exception:
        return None


async def _current_playback_path() -> Optional[Path]:
    """Resolve the file VLC is actually playing, regardless of how it started."""
    uri = await vlc_playlist_uri()
    if uri and uri.startswith("file:"):
        try:
            p = Path(uri_to_path(uri))
            if p.exists():
                return p
        except Exception:
            pass
    for cand in (state.library_current_file, state.active_file):
        if cand:
            try:
                p = Path(cand)
                if p.exists():
                    return p
            except Exception:
                pass
    return None


def _trim_subtitle_result(s: dict) -> dict:
    return {
        "name": s.get("SubFileName") or s.get("MovieReleaseName") or "subtitle",
        "lang": s.get("ISO639") or "",
        "lang_name": s.get("LanguageName") or s.get("SubLanguageID") or "Unknown",
        "downloads": int(s.get("SubDownloadsCount") or 0),
        "matched_by": s.get("MatchedBy") or "",
        "release": s.get("MovieReleaseName") or "",
        "download_link": s.get("SubDownloadLink") or "",
    }


async def _opensubtitles_search(
    file_hash: Optional[str], file_size: Optional[int],
    query: str, lang: str,
) -> list[dict]:
    """Query the keyless rest.opensubtitles.org API by hash and/or text. Hash
    matches are exact; text matches are a fallback. Results are merged."""
    headers = {
        "User-Agent": settings.opensubtitles_user_agent,
        "X-User-Agent": settings.opensubtitles_user_agent,
    }
    lang_seg = f"/sublanguageid-{quote(lang)}" if lang else ""
    urls: list[str] = []
    if file_hash and file_size:
        urls.append(
            f"https://rest.opensubtitles.org/search"
            f"/moviebytesize-{file_size}/moviehash-{file_hash}{lang_seg}"
        )
    if query:
        urls.append(
            f"https://rest.opensubtitles.org/search/query-{quote(query)}{lang_seg}"
        )
    raw: list[dict] = []
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as c:
        for url in urls:
            try:
                r = await c.get(url, headers=headers)
                if r.status_code == 200 and isinstance(r.json(), list):
                    raw.extend(r.json())
            except Exception:
                pass
    # Dedup by download link, prefer hash matches, then by download count
    seen: set[str] = set()
    trimmed: list[dict] = []
    for s in raw:
        t = _trim_subtitle_result(s)
        link = t["download_link"]
        if not link or link in seen:
            continue
        seen.add(link)
        trimmed.append(t)
    trimmed.sort(
        key=lambda t: (0 if t["matched_by"] == "moviehash" else 1, -t["downloads"])
    )
    return trimmed[:40]


def _find_vlc_bin() -> Optional[str]:
    """Locate the VLC binary, checking .env first then well-known paths."""
    env_file = Path(__file__).parent / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line.startswith("_VLC_BIN="):
                val = line[9:].strip()
                if val and Path(val).exists():
                    return val
    candidates: list[str] = {
        "Darwin":  ["/Applications/VLC.app/Contents/MacOS/VLC"],
        "Windows": [
            r"C:\Program Files\VideoLAN\VLC\vlc.exe",
            r"C:\Program Files (x86)\VideoLAN\VLC\vlc.exe",
        ],
    }.get(platform.system(), ["/usr/bin/vlc"])
    for c in candidates:
        if Path(c).exists():
            return c
    return shutil.which("vlc")


def _find_vlc_hwnds_windows() -> list:
    """Return all visible top-level window handles belonging to any VLC process.

    Uses psutil for PID lookup + GetWindowThreadProcessId for matching.
    The cb variable keeps the EnumWindowsProc wrapper alive to prevent ctypes GC.
    """
    try:
        import ctypes
        from ctypes import wintypes

        vlc_pids: set = set()
        for p in psutil.process_iter(["name", "pid"]):
            if (p.info["name"] or "").lower().startswith("vlc"):
                vlc_pids.add(p.info["pid"])
        if not vlc_pids:
            return []

        user32 = ctypes.windll.user32
        found: list = []
        EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

        def _cb(hwnd, _):
            if user32.IsWindowVisible(hwnd):
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if pid.value in vlc_pids:
                    found.append(hwnd)
            return True

        cb = EnumWindowsProc(_cb)   # keep ref alive — ctypes GC pitfall
        user32.EnumWindows(cb, 0)
        return found
    except Exception:
        return []


def _vlc_focus_windows() -> None:
    """Bring the main VLC window to the foreground on Windows using ctypes."""
    hwnds = _find_vlc_hwnds_windows()
    if not hwnds:
        return
    hwnd = hwnds[0]
    try:
        import ctypes
        user32 = ctypes.windll.user32
        fg_thread = user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), None)
        my_thread = ctypes.windll.kernel32.GetCurrentThreadId()
        if fg_thread != my_thread:
            user32.AttachThreadInput(my_thread, fg_thread, True)
        user32.ShowWindow(hwnd, 9)   # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
        if fg_thread != my_thread:
            user32.AttachThreadInput(my_thread, fg_thread, False)
    except Exception:
        pass


def _vlc_minimize_windows() -> None:
    """Minimize all VLC windows on Windows using ctypes."""
    hwnds = _find_vlc_hwnds_windows()
    if not hwnds:
        return
    try:
        import ctypes
        user32 = ctypes.windll.user32
        WM_SYSCOMMAND = 0x0112
        SC_MINIMIZE    = 0xF020
        for hwnd in hwnds:
            # PostMessage puts SC_MINIMIZE into VLC's own message queue (UI-thread safe).
            user32.PostMessageW(hwnd, WM_SYSCOMMAND, SC_MINIMIZE, 0)
            # SW_FORCEMINIMIZE (11) is designed for cross-process/thread minimization.
            user32.ShowWindow(hwnd, 11)
    except Exception:
        pass


async def vlc_minimize() -> None:
    """Minimize VLC on all platforms. Best-effort; never raises."""
    system = platform.system()
    try:
        if system == "Windows":
            await asyncio.get_running_loop().run_in_executor(None, _vlc_minimize_windows)
        elif system == "Darwin":
            proc = await asyncio.create_subprocess_exec(
                "osascript", "-e",
                'tell application "VLC" to set miniaturized of every window to true',
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
        elif system == "Linux":
            if shutil.which("xdotool"):
                proc = await asyncio.create_subprocess_exec(
                    "xdotool", "search", "--name", "VLC", "windowminimize", "--sync",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await proc.wait()
            elif shutil.which("wmctrl"):
                proc = await asyncio.create_subprocess_exec(
                    "wmctrl", "-r", "VLC", "-b", "add,hidden",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await proc.wait()
    except Exception:
        pass


async def vlc_focus_and_fullscreen() -> None:
    """Bring VLC to the foreground and enable fullscreen. Best-effort; never raises."""
    await asyncio.sleep(1.5)
    system = platform.system()
    try:
        if system == "Windows":
            await asyncio.get_event_loop().run_in_executor(None, _vlc_focus_windows)
        elif system == "Darwin":
            proc = await asyncio.create_subprocess_exec(
                "osascript", "-e", 'tell application "VLC" to activate',
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
        elif system == "Linux":
            # Best-effort via wmctrl if available; silently skip if not installed
            if shutil.which("wmctrl"):
                proc = await asyncio.create_subprocess_exec(
                    "wmctrl", "-a", "VLC",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await proc.wait()
    except Exception:
        pass
    try:
        vs = await vlc_status()
        if vs and not vs.get("fullscreen"):
            await vlc("fullscreen")
    except Exception:
        pass


async def _restart_vlc_process() -> bool:
    """Kill any running VLC processes, relaunch with HTTP interface. Returns True when port opens."""
    for p in psutil.process_iter(["name"]):
        if (p.info["name"] or "").lower().startswith("vlc"):
            try:
                p.kill()
            except Exception:
                pass

    await asyncio.sleep(1.5)

    vlc_bin = _find_vlc_bin()
    if not vlc_bin:
        return False

    vlc_port = int(urlparse(settings.vlc_url).port or 8080)

    kw: dict = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if platform.system() == "Windows":
        kw["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    subprocess.Popen(
        [vlc_bin, "--extraintf=http", "--http-host=localhost",
         f"--http-port={vlc_port}", f"--http-password={settings.vlc_password}", "--no-random"],
        **kw,
    )

    deadline = asyncio.get_event_loop().time() + 15
    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.5)
        try:
            with socket.create_connection(("127.0.0.1", vlc_port), timeout=0.5):
                await asyncio.sleep(0.8)
                return True
        except OSError:
            pass
    return False


async def _retry_task(file_path: Path) -> None:
    """Background task: restart VLC and replay the current file + remainder of playlist."""
    try:
        await broadcast("stream_status", {"status": state.stream_status, "message": "Relaunching VLC…"})
        ok = await _restart_vlc_process()
        if not ok:
            state.stream_status = "error"
            await broadcast("stream_status", {"status": "error", "message": "VLC could not be relaunched."})
            return

        await vlc("in_play", input=file_path.resolve().as_uri())

        if state.library_playlist and state.library_current_file:
            try:
                cur = str(state.library_current_file)
                idx = state.library_playlist.index(cur)
                for p in state.library_playlist[idx + 1:]:
                    await vlc("in_enqueue", input=Path(p).resolve().as_uri())
            except (ValueError, Exception):
                pass

        state.stream_status = "playing"
        state.current_audio_track = -1
        state.current_subtitle_track = -1
        asyncio.create_task(vlc_focus_and_fullscreen())
        await broadcast("stream_status", {"status": "playing", "message": f"Retrying: {file_path.name}"})
    except Exception as exc:
        state.stream_status = "error"
        await broadcast("stream_status", {"status": "error", "message": f"Retry failed: {exc}"})


# ── Utilities ─────────────────────────────────────────────────────────────────

def extract_hash(magnet: str) -> Optional[str]:
    """Pull the info-hash from a magnet URI, normalising base32 → hex."""
    m = re.search(r"xt=urn:btih:([a-fA-F0-9]{40}|[A-Za-z2-7]{32})", magnet, re.I)
    if not m:
        return None
    h = m.group(1)
    if len(h) == 32:
        try:
            h = base64.b32decode(h.upper()).hex()
        except Exception:
            return None
    return h.lower()


def parse_season_episode(name: str) -> tuple[int, int]:
    """Extract (season, episode) from filenames like S01E03, s2e5, 1x03, etc."""
    m = re.search(r"[Ss](\d{1,2})[Ee](\d{1,2})", name)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.search(r"\b(\d{1,2})x(\d{2})\b", name)
    if m:
        return int(m.group(1)), int(m.group(2))
    return 0, 0


VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".mov", ".wmv", ".m4v", ".ts", ".m2ts", ".webm"}


def build_file_list(qbit_file_list: list, save_path: str) -> list[dict]:
    """Build a sorted video-file list from qBittorrent's files API response."""
    result = []
    for f in qbit_file_list:
        rel = f.get("name", "")
        if Path(rel).suffix.lower() not in VIDEO_EXTS:
            continue
        season, episode = parse_season_episode(rel)
        result.append({
            "name": Path(rel).name,
            "path": str(Path(save_path) / rel),
            "size_bytes": f.get("size", 0),
            "season": season,
            "episode": episode,
        })
    result.sort(key=lambda x: (x["season"] or 9999, x["episode"] or 9999, x["name"]))
    return result


def _file_by_index(files: list, idx: int) -> Optional[dict]:
    """Return the file dict whose 'index' field equals idx (falls back to enumerate position)."""
    for i, f in enumerate(files):
        if f.get("index", i) == idx:
            return f
    return None


def largest_video(files: list) -> Optional[dict]:
    videos = [f for f in files if Path(f["name"]).suffix.lower() in VIDEO_EXTS]
    pool = videos or files
    return max(pool, key=lambda f: f.get("size", 0), default=None)


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def _file_progress(item: dict, profile_id: str, file_path: str) -> Optional[dict]:
    """Return per-file progress dict for a given profile and path, or None."""
    prof = item.get("progress", {}).get(profile_id, {})
    return prof.get("file_progress", {}).get(file_path)


def _canonical_item_path(vlc_path: str, item: dict) -> str:
    """Return the item["files"] path that resolves to the same file as vlc_path.

    VLC receives Path(p).resolve().as_uri() which follows symlinks.  The stored
    path may not be resolved, so we compare resolved forms and return the stored
    path so progress keys always match what get_item_files and find_resume_hint
    expect.  Falls back to vlc_path if no match is found.
    """
    try:
        target = Path(vlc_path).resolve()
    except Exception:
        return vlc_path
    for f in item.get("files", []):
        stored = f.get("path", "")
        try:
            if Path(stored).resolve() == target:
                return stored
        except Exception:
            continue
    return vlc_path


def find_resume_hint(item: dict, profile_id: str) -> Optional[dict]:
    """Return the best file+position to resume for a profile, or None."""
    if not profile_id:
        return None
    files = item.get("files", [])
    if not files:
        return None
    prof = item.get("progress", {}).get(profile_id, {})
    file_progs = prof.get("file_progress", {})
    last_file = prof.get("last_file")

    # Prefer the last-watched file if it has meaningful in-progress position
    if last_file:
        fp = file_progs.get(last_file, {})
        if not fp.get("completed") and fp.get("position_sec", 0) > 5:
            dur = fp.get("duration_sec", 0)
            pos = fp.get("position_sec", 0)
            return {
                "file_path": last_file,
                "episode_name": Path(last_file).name,
                "position_sec": pos,
                "duration_sec": dur,
                "pct": round(pos / dur * 100, 1) if dur else 0,
            }

    # Walk files in order — return first that isn't completed
    for f in files:
        path = f.get("path", "")
        fp = file_progs.get(path, {})
        if not fp.get("completed"):
            pos = fp.get("position_sec", 0)
            dur = fp.get("duration_sec", 0)
            return {
                "file_path": path,
                "episode_name": f.get("name", Path(path).name),
                "position_sec": pos,
                "duration_sec": dur,
                "pct": round(pos / dur * 100, 1) if dur else 0,
            }

    # All completed — point back to first so user can rewatch
    f = files[0]
    return {
        "file_path": f.get("path", ""),
        "episode_name": f.get("name", ""),
        "position_sec": 0,
        "duration_sec": 0,
        "pct": 100,
        "all_completed": True,
    }


# ── Track Preference Helpers ──────────────────────────────────────────────────

async def _save_track_pref(
    item_id: str, profile_id: str, file_path: str,
    audio: Optional[int] = None, subtitle: Optional[int] = None,
) -> None:
    """Persist audio/subtitle track preference for a file into library.json."""
    try:
        async with _lib_lock:
            lib = _load_lib_raw()
            item = next((it for it in lib["items"] if it["id"] == item_id), None)
            if not item:
                return
            fp = (item.setdefault("progress", {})
                      .setdefault(profile_id, {})
                      .setdefault("file_progress", {})
                      .setdefault(file_path, {}))
            if audio is not None:
                fp["audio_track"] = audio
            if subtitle is not None:
                fp["subtitle_track"] = subtitle
            _save_lib_raw(lib)
    except Exception:
        pass


async def _apply_track_prefs(
    item_id: str, profile_id: str, file_path: str, delay: float = 2.0,
) -> None:
    """Apply saved audio/subtitle track prefs for a file after a short delay."""
    try:
        await asyncio.sleep(delay)
        lib = await get_library()
        item = next((it for it in lib["items"] if it["id"] == item_id), None)
        if not item:
            return
        fp = (item.get("progress", {})
                  .get(profile_id, {})
                  .get("file_progress", {})
                  .get(file_path, {}))
        audio = fp.get("audio_track")
        subtitle = fp.get("subtitle_track")
        if audio is not None:
            state.current_audio_track = audio
            await vlc("audio_track", val=str(audio))
        if subtitle is not None:
            state.current_subtitle_track = subtitle
            await vlc("subtitle_track", val=str(subtitle))
        state.track_pref_applied_file = file_path
    except Exception:
        pass


# ── Background Task: Mullvad Guard ───────────────────────────────────────────

async def vpn_guard() -> None:
    """Poll `mullvad status` every 3 s; kill qBittorrent if not connected."""
    while True:
        try:
            proc = await asyncio.create_subprocess_exec(
                "mullvad", "status",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, _ = await proc.communicate()
            text = out.decode(errors="replace")
            connected = "Connected" in text
            first_line = text.strip().splitlines()[0] if text.strip() else "Unknown"
            state.vpn_status_text = first_line

            if not connected and state.vpn_secure:
                state.vpn_secure = False
                pname = "qbittorrent.exe" if platform.system() == "Windows" else "qbittorrent"
                for p in psutil.process_iter(["name"]):
                    if (p.info["name"] or "").lower() == pname.lower():
                        try:
                            p.kill()
                        except psutil.NoSuchProcess:
                            pass
                await broadcast("vpn_status", {"secure": False, "status": first_line})

            elif connected and not state.vpn_secure:
                state.vpn_secure = True
                await broadcast("vpn_status", {"secure": True, "status": first_line})

        except FileNotFoundError:
            state.vpn_status_text = "mullvad CLI not installed"
        except Exception:
            pass

        await asyncio.sleep(3)


# ── Background Task: State Broadcaster ───────────────────────────────────────

async def stat_broadcaster() -> None:
    """Push a full state snapshot to all SSE clients every 2 s."""
    while True:
        if state.active_hash and state.stream_status in ("buffering", "playing"):
            info = await qbit_info(state.active_hash)
            if info:
                total = info.get("size", 1) or 1
                done = info.get("completed", 0)
                state.progress = done / total * 100
                state.downloaded_mb = done / 1_048_576
                state.total_mb = total / 1_048_576
                state.dl_speed_bps = info.get("dlspeed", 0)
                state.ul_speed_bps = info.get("upspeed", 0)
        if state.stream_status == "playing":
            vs = await vlc_status()
            if vs:
                state.vlc_time = int(vs.get("time", 0))
                state.vlc_duration = int(vs.get("length", 0))
                state.vlc_volume = round(int(vs.get("volume", 256)) / 256 * 100)
        await broadcast("state", state_snapshot())
        await asyncio.sleep(2)


async def _auto_play_item(item: dict, profile_id: str, file_path: str = "") -> None:
    """Trigger playback for a library item (or specific file) that just finished downloading."""
    try:
        all_paths = [f["path"] for f in item.get("files", [])]
        if file_path and file_path in all_paths:
            start_idx = all_paths.index(file_path)
            playlist = all_paths[start_idx:]
        else:
            hint = find_resume_hint(item, profile_id)
            if hint and hint.get("file_path") and not hint.get("all_completed"):
                try:
                    start_idx = all_paths.index(hint["file_path"])
                    playlist = all_paths[start_idx:]
                except ValueError:
                    playlist = all_paths
            else:
                playlist = all_paths

        if not playlist:
            return

        # Wait up to 10 s for files to appear on disk after qBit marks complete
        for _ in range(10):
            if all(Path(p).exists() for p in playlist):
                break
            await asyncio.sleep(1)

        first = Path(playlist[0])
        await vlc("in_play", input=first.resolve().as_uri())
        for p in playlist[1:]:
            await vlc("in_enqueue", input=Path(p).resolve().as_uri())
        asyncio.create_task(vlc_focus_and_fullscreen())

        state.stream_status = "playing"
        state.active_title = item["title"]
        state.active_file = first
        state.current_audio_track = -1
        state.current_subtitle_track = -1
        state.active_hash = item.get("torrent_hash") or None
        state.library_item_id = item["id"]
        state.library_profile_id = profile_id
        state.library_item_file_count = len(item.get("files", []))
        state.library_playlist = playlist
        state.library_current_file = playlist[0]

        await broadcast("stream_status", {"status": "playing", "message": f"Auto-playing: {first.name}"})
    except Exception:
        pass


# ── Background Task: Library Download Monitor ─────────────────────────────────

async def library_download_monitor() -> None:
    """Poll qBit every 5 s for pending library downloads and mark them complete."""
    while True:
        await asyncio.sleep(5)
        try:
            lib = await get_library()
            pending = [it for it in lib["items"] if it.get("status") == "downloading"]
            state.downloading_count = len(pending)
            if not pending:
                continue
            changed = False
            for item in pending:
                h = item.get("torrent_hash")
                if not h:
                    continue
                info = await qbit_info(h)
                if not info:
                    continue

                # Refresh file list now that sizes are final
                qfiles = await qbit_files(h)
                save_path = info.get("save_path", settings.qbit_download_path)
                new_files = build_file_list(qfiles, save_path)
                if new_files:
                    item["files"] = new_files
                    item["size_bytes"] = sum(f["size_bytes"] for f in new_files)

                qstate = info.get("state", "")
                if qstate in ("uploading", "stalledUP", "pausedUP", "queuedUP", "forcedUP"):
                    item["status"] = "ready"
                    state.downloading_count = max(0, state.downloading_count - 1)
                    changed = True
                    await broadcast("library_update", {"item_id": item["id"], "status": "ready"})
                    _schedule_series_analysis_if_eligible(item, lib)
                    if state.play_when_ready_item_id == item["id"]:
                        pwr_profile = state.play_when_ready_profile_id
                        pwr_fp = state.play_when_ready_file_path
                        state.play_when_ready_item_id = None
                        state.play_when_ready_profile_id = None
                        state.play_when_ready_file_path = None
                        asyncio.create_task(_auto_play_item(item, pwr_profile or "", pwr_fp or ""))
                elif qstate in ("error", "missingFiles"):
                    item["status"] = "error"
                    state.downloading_count = max(0, state.downloading_count - 1)
                    changed = True
                    await broadcast("library_update", {"item_id": item["id"], "status": "error"})
                else:
                    # Still downloading — push live stats to the UI
                    eta = info.get("eta", 8640000)
                    await broadcast("library_progress", {
                        "item_id": item["id"],
                        "speed_bps": info.get("dlspeed", 0),
                        "downloaded_bytes": info.get("completed", 0),
                        "total_bytes": info.get("size", 0),
                        "progress_pct": round(info.get("completed", 0) / max(info.get("size", 1), 1) * 100, 1),
                        "eta_secs": eta if eta < 8640000 else -1,
                    })
                    changed = True  # file list updated
                    # Check if a specific queued file finished (even while torrent is still going)
                    if (state.play_when_ready_item_id == item["id"]
                            and state.play_when_ready_file_path and qfiles):
                        pwr_path = state.play_when_ready_file_path
                        for qf in qfiles:
                            full = str(Path(save_path) / qf.get("name", ""))
                            if full == pwr_path and qf.get("progress", 0) >= 1.0:
                                pwr_profile = state.play_when_ready_profile_id
                                state.play_when_ready_item_id = None
                                state.play_when_ready_profile_id = None
                                state.play_when_ready_file_path = None
                                asyncio.create_task(_auto_play_item(item, pwr_profile or "", pwr_path))
                                break

            if changed:
                await put_library(lib)
        except Exception:
            pass


# ── Smart Skip helpers ────────────────────────────────────────────────────────

def _series_key(item: dict) -> str:
    """Group items by series for cross-episode fingerprinting.

    Items with a non-empty series field group together; everything else is
    grouped by item ID (a single-item bucket) so movies and one-offs still
    get the credits fallback.
    """
    s = (item.get("series") or "").strip()
    return f"series:{s.lower()}" if s else f"item:{item.get('id', '')}"


def _items_for_series_key(lib: dict, key: str) -> list[dict]:
    return [it for it in lib["items"] if _series_key(it) == key]


def _skip_settings_for_profile(lib: dict, profile_id: str) -> dict:
    """Return {auto_skip_intro, auto_skip_credits} for a profile (defaults False)."""
    if not profile_id:
        return {"auto_skip_intro": False, "auto_skip_credits": False}
    prof = next((p for p in lib.get("profiles", []) if p["id"] == profile_id), None)
    if not prof:
        return {"auto_skip_intro": False, "auto_skip_credits": False}
    return {
        "auto_skip_intro":   bool(prof.get("auto_skip_intro", False)),
        "auto_skip_credits": bool(prof.get("auto_skip_credits", False)),
    }


def _next_file_in_item(item: dict, current_path: str) -> Optional[str]:
    """Return the next file path in this item after current_path, or None."""
    files = item.get("files", [])
    paths = [f.get("path", "") for f in files]
    try:
        idx = paths.index(current_path)
    except ValueError:
        return None
    return paths[idx + 1] if idx + 1 < len(paths) else None


def _find_file_meta(item: dict, file_path: str) -> Optional[dict]:
    """Return the per-file analysis dict (intro/credits_start) if present."""
    skip_data = item.get("skip_data", {})
    return skip_data.get(file_path)


async def _set_analysis_status(series_key: str, **patch) -> None:
    """Update state.analysis_jobs[series_key] and broadcast the change."""
    job = state.analysis_jobs.setdefault(series_key, {})
    job.update(patch)
    await broadcast("analysis_status", {"series_key": series_key, "job": job})


async def _run_series_analysis(series_key: str) -> None:
    """Background task: analyze a series, save results, broadcast progress."""
    if not analyzer.is_available():
        await _set_analysis_status(
            series_key, status="failed",
            stage="error", message="ffmpeg/fpcalc not available",
            current=0, total=0, finished_at=_now_iso(),
        )
        return

    lock = analyzer.lock_for_series(series_key)
    async with lock:
        lib = await get_library()
        items = _items_for_series_key(lib, series_key)
        ready_items = [it for it in items if it.get("status") == "ready"]
        item_ids = [it["id"] for it in ready_items]
        if not ready_items:
            return

        # Start
        await _set_analysis_status(
            series_key, status="running",
            stage="starting", current=0, total=0,
            message="Preparing analysis…",
            item_ids=item_ids,
            started_at=_now_iso(),
            finished_at=None,
        )

        async def _on_progress(**kw):
            await _set_analysis_status(series_key, status="running", **kw)

        try:
            results = await analyzer.analyze_series(ready_items, progress_cb=_on_progress)
        except Exception as exc:
            await _set_analysis_status(
                series_key, status="failed",
                stage="error", message=f"Analysis failed: {exc}",
                finished_at=_now_iso(),
            )
            return

        if not results:
            await _set_analysis_status(
                series_key, status="failed",
                stage="error", message="No analyzable episodes found",
                finished_at=_now_iso(),
            )
            return

        # Persist results back into library.json under each item
        files_updated = 0
        lib = await get_library()
        for it in lib["items"]:
            if _series_key(it) != series_key:
                continue
            skip_data = it.setdefault("skip_data", {})
            changed = False
            for f in it.get("files", []):
                p = f.get("path", "")
                if p in results:
                    skip_data[p] = results[p]
                    files_updated += 1
                    changed = True
            if changed:
                await broadcast("library_update", {"item_id": it["id"], "status": it.get("status", "ready")})
        await put_library(lib)

        await _set_analysis_status(
            series_key, status="complete",
            stage="done", message=f"Updated {files_updated} file(s)",
            current=files_updated, total=files_updated,
            finished_at=_now_iso(),
        )


def _schedule_series_analysis_if_eligible(item: dict, lib: dict) -> None:
    """When an item flips to 'ready', kick off series analysis if 2+ ready episodes
    exist in the same series and no analysis has been run yet for this series.
    Single-item buckets still run (to populate the credits fallback)."""
    if not analyzer.is_available():
        return
    key = _series_key(item)
    peers = [it for it in _items_for_series_key(lib, key) if it.get("status") == "ready"]
    # Avoid re-analyzing if every file in every peer already has skip_data
    needs_run = False
    for peer in peers:
        sk = peer.get("skip_data", {})
        for f in peer.get("files", []):
            if f.get("path", "") not in sk:
                needs_run = True
                break
        if needs_run:
            break
    if needs_run:
        asyncio.create_task(_run_series_analysis(key))


# Pre-roll: show the skip button this many seconds before the range starts so
# the user has visual time to react when entering the window.
SKIP_PREROLL_SEC = 2.0


async def _maybe_emit_skip_offer(
    item: dict, file_path: str, meta: Optional[dict],
    prefs: dict, pos_sec: float, dur_sec: float,
) -> None:
    """Set or clear state.skip_offer based on current playback position.

    Auto-skip behavior: if the profile has auto_skip_* enabled, this helper
    issues the seek/advance directly and does NOT show the offer in the UI.
    """
    if not meta:
        await _clear_skip_offer(file_path)
        return

    # Intro window: position within [start - PREROLL, end]
    intro = meta.get("intro")
    if intro and intro.get("end", 0) > intro.get("start", 0):
        start = float(intro.get("start", 0))
        end   = float(intro.get("end",   0))
        if (start - SKIP_PREROLL_SEC) <= pos_sec < end:
            if prefs.get("auto_skip_intro") and end - pos_sec > 1.0:
                # Only auto-skip if the offer hasn't already been auto-handled
                if state.skip_offer_file != f"{file_path}#intro-done":
                    state.skip_offer_file = f"{file_path}#intro-done"
                    state.skip_offer = None
                    await vlc("seek", val=str(int(end) + 1))
                    await broadcast("state", state_snapshot())
                return
            offer = {"type": "intro", "end_at": round(end, 1), "file_path": file_path}
            if state.skip_offer != offer:
                state.skip_offer = offer
                state.skip_offer_file = file_path
                await broadcast("state", state_snapshot())
            return
        elif pos_sec >= end and state.skip_offer and state.skip_offer.get("type") == "intro":
            await _clear_skip_offer(file_path)

    # Credits window: position past credits_start
    credits_start = meta.get("credits_start")
    if credits_start and pos_sec >= float(credits_start) - SKIP_PREROLL_SEC and pos_sec < dur_sec - 1:
        next_path = _next_file_in_item(item, file_path)
        next_exists = bool(next_path) and Path(next_path).exists()
        if prefs.get("auto_skip_credits") and (pos_sec >= float(credits_start)):
            # Only auto-skip once per file
            done_marker = f"{file_path}#credits-done"
            if state.skip_offer_file != done_marker:
                state.skip_offer_file = done_marker
                state.skip_offer = None
                await broadcast("state", state_snapshot())
                if next_exists:
                    await vlc_next_file(file_path, item)
                else:
                    await vlc("pl_stop")
            return
        offer = {
            "type": "credits",
            "credits_start": round(float(credits_start), 1),
            "file_path": file_path,
            "has_next": next_exists,
            "next_file_path": next_path if next_exists else None,
        }
        if state.skip_offer != offer:
            state.skip_offer = offer
            state.skip_offer_file = file_path
            await broadcast("state", state_snapshot())
        return

    # Outside any window — clear if one was set for this file
    await _clear_skip_offer(file_path)


async def _clear_skip_offer(file_path: str) -> None:
    if state.skip_offer is not None and state.skip_offer.get("file_path") == file_path:
        state.skip_offer = None
        # don't reset skip_offer_file — it carries the done marker
        await broadcast("state", state_snapshot())
    elif state.skip_offer is not None:
        # Different file (e.g. user advanced manually) — drop the offer
        state.skip_offer = None
        await broadcast("state", state_snapshot())


async def vlc_next_file(current_file: str, item: dict) -> None:
    """Internal helper: advance VLC to the next file in this item's playlist."""
    next_path = _next_file_in_item(item, current_file)
    if not next_path or not Path(next_path).exists():
        return
    all_paths = [f.get("path", "") for f in item.get("files", [])]
    try:
        idx = all_paths.index(next_path)
        new_tail = all_paths[idx:]
    except ValueError:
        new_tail = [next_path]
    state.library_playlist = new_tail
    state.library_current_file = next_path
    state.current_audio_track = -1
    state.current_subtitle_track = -1
    state.track_pref_applied_file = next_path
    await vlc("in_play", input=Path(next_path).resolve().as_uri())
    for p in new_tail[1:]:
        await vlc("in_enqueue", input=Path(p).resolve().as_uri())
    if state.library_item_id and state.library_profile_id:
        asyncio.create_task(_apply_track_prefs(
            state.library_item_id, state.library_profile_id, next_path, delay=2.0,
        ))


# ── Background Task: VLC Progress Tracker ────────────────────────────────────

async def vlc_progress_tracker() -> None:
    """Save watch progress + manage Smart Skip offers. Two cadences:

    - skip-offer detection runs every 2 s while a library item is playing
    - progress save runs every 15 s
    """
    last_progress_save = 0.0
    while True:
        await asyncio.sleep(2)
        if not state.library_item_id or not state.library_profile_id:
            # Clear any stale skip offer when playback ends
            if state.skip_offer is not None:
                state.skip_offer = None
                state.skip_offer_file = None
                await broadcast("state", state_snapshot())
            continue
        try:
            vs = await vlc_status()
            if not vs:
                continue
            pos_sec = float(vs.get("time", 0))
            dur_sec = float(vs.get("length", 0))
            if dur_sec < 10:
                continue

            # Resolve which file is playing and look up its skip metadata
            current_uri = await vlc_playlist_uri()
            if current_uri and current_uri.startswith("file://"):
                state.library_current_file = uri_to_path(current_uri)
            cur_file = state.library_current_file
            if cur_file:
                lib_q = await get_library()
                item_q = next((it for it in lib_q["items"] if it["id"] == state.library_item_id), None)
                if item_q:
                    cur_file = _canonical_item_path(cur_file, item_q)
                    state.library_current_file = cur_file
                    meta = _find_file_meta(item_q, cur_file)
                    prefs = _skip_settings_for_profile(lib_q, state.library_profile_id)
                    await _maybe_emit_skip_offer(item_q, cur_file, meta, prefs, pos_sec, dur_sec)

            now = asyncio.get_event_loop().time()
            if now - last_progress_save < 15:
                continue
            last_progress_save = now

            current_file = state.library_current_file
            if not current_file:
                continue

            # Apply saved track prefs when VLC advances to a new episode
            if (current_file != state.track_pref_applied_file and
                    state.library_item_id and state.library_profile_id):
                state.track_pref_applied_file = current_file
                asyncio.create_task(_apply_track_prefs(
                    state.library_item_id, state.library_profile_id, current_file, delay=2.0,
                ))

            lib = await get_library()
            item = next((it for it in lib["items"] if it["id"] == state.library_item_id), None)
            if not item:
                continue

            pct = pos_sec / dur_sec
            prof_prog = item.setdefault("progress", {}).setdefault(state.library_profile_id, {})
            prof_prog["last_file"] = current_file
            prof_prog.setdefault("file_progress", {})[current_file] = {
                "position_sec": round(pos_sec, 1),
                "duration_sec": round(dur_sec, 1),
                "completed": pct > 0.92,
                "updated_at": _now_iso(),
            }
            await put_library(lib)

            await broadcast("progress_saved", {
                "item_id": state.library_item_id,
                "profile_id": state.library_profile_id,
                "file_path": current_file,
                "episode_name": Path(current_file).name,
                "position_sec": pos_sec,
                "duration_sec": dur_sec,
                "pct": round(pct * 100, 1),
            })
        except Exception:
            pass


# ── Stream Pipeline (stream-now, torrent auto-deleted on stop) ────────────────

async def stream_pipeline(
    magnet: str,
    title: str,
    file_index: Optional[int] = None,
    torrent_hash: Optional[str] = None,
) -> None:
    try:
        state.active_title = title
        state.stream_status = "buffering"
        state.current_audio_track = -1
        state.current_subtitle_track = -1
        await broadcast("stream_status", {"status": "buffering", "message": "Adding torrent to qBittorrent…"})

        if torrent_hash:
            # Torrent already added by /stream/prepare — use existing hash directly
            h = torrent_hash
            state.active_hash = h
        else:
            h = await qbit_add_magnet(magnet, sequential=True)
            if not h:
                raise RuntimeError("qBittorrent rejected the magnet (is it running on port 8081?)")
            state.active_hash = h
            for _ in range(30):
                await asyncio.sleep(1)
                if await qbit_info(h):
                    break
            else:
                raise RuntimeError("Torrent did not appear in qBittorrent after 30 s.")

        await qbit_streaming_mode(h)

        if file_index is not None:
            # Skip all files except the selected one
            all_files = await qbit_files(h)
            skip_ids = [f.get("index", i) for i, f in enumerate(all_files)
                        if f.get("index", i) != file_index]
            if skip_ids:
                await qbit_set_file_priority(h, skip_ids, 0)
            await broadcast("stream_status", {
                "status": "buffering", "message": "File selected. Buffering first pieces…",
            })
        else:
            await broadcast("stream_status", {
                "status": "buffering", "message": "Sequential mode set. Buffering first pieces…",
            })

        # Buffer loop — track per-file progress when a specific file is selected
        while True:
            if file_index is not None:
                file_list = await qbit_files(h)
                target = _file_by_index(file_list, file_index)
                info = await qbit_info(h)
                if target and info:
                    f_size = target.get("size", 1) or 1
                    f_prog = target.get("progress", 0)
                    mb = f_size * f_prog / 1_048_576
                    pct = f_prog * 100
                    total_mb = f_size / 1_048_576
                    state.progress = pct
                    state.downloaded_mb = mb
                    state.total_mb = total_mb
                    state.dl_speed_bps = info.get("dlspeed", 0)
                    state.ul_speed_bps = info.get("upspeed", 0)
                    await broadcast("stream_status", {
                        "status": "buffering",
                        "message": f"Buffering {mb:.1f} MB / {total_mb:.1f} MB ({pct:.1f}%)",
                        "progress": pct, "downloaded_mb": mb, "total_mb": total_mb,
                        "dl_speed_bps": state.dl_speed_bps, "ul_speed_bps": state.ul_speed_bps,
                    })
                    if mb >= settings.buffer_min_mb or pct >= settings.buffer_min_pct:
                        break
            else:
                info = await qbit_info(h)
                if info:
                    total = info.get("size", 1) or 1
                    done = info.get("completed", 0)
                    pct = done / total * 100
                    mb = done / 1_048_576
                    state.progress = pct
                    state.downloaded_mb = mb
                    state.total_mb = total / 1_048_576
                    state.dl_speed_bps = info.get("dlspeed", 0)
                    state.ul_speed_bps = info.get("upspeed", 0)
                    await broadcast("stream_status", {
                        "status": "buffering",
                        "message": f"Buffering {mb:.1f} MB / {state.total_mb:.1f} MB ({pct:.1f}%)",
                        "progress": pct, "downloaded_mb": mb, "total_mb": state.total_mb,
                        "dl_speed_bps": state.dl_speed_bps, "ul_speed_bps": state.ul_speed_bps,
                    })
                    if mb >= settings.buffer_min_mb or pct >= settings.buffer_min_pct:
                        break
            await asyncio.sleep(1)

        # Resolve the file to play
        files = await qbit_files(h)
        vid = _file_by_index(files, file_index) if file_index is not None else largest_video(files)
        if not vid:
            raise RuntimeError("No recognisable video file found in torrent.")

        info = await qbit_info(h)
        save_path = (info or {}).get("save_path", settings.qbit_download_path)
        file_path = Path(save_path) / vid["name"]
        state.active_file = file_path

        await vlc("in_play", input=file_path.resolve().as_uri())
        asyncio.create_task(vlc_focus_and_fullscreen())
        state.stream_status = "playing"
        await broadcast("stream_status", {"status": "playing", "message": f"Playing: {file_path.name}"})

    except asyncio.CancelledError:
        pass
    except Exception as e:
        state.stream_status = "error"
        await broadcast("stream_status", {"status": "error", "message": str(e)})


# ── Library Download Pipeline (keep file, no auto-delete) ─────────────────────

async def library_download_pipeline(
    item_id: str,
    magnet: str,
    save_path: str = "",
    torrent_hash: str = "",
    selected_file_indices: Optional[list[int]] = None,
) -> None:
    """Add magnet to qBit for a full download; no streaming mode, never auto-deleted."""
    try:
        if torrent_hash:
            h = torrent_hash
        else:
            h = await qbit_add_magnet(magnet, save_path=save_path or None)
            if not h:
                lib = await get_library()
                for it in lib["items"]:
                    if it["id"] == item_id:
                        it["status"] = "error"
                        break
                await put_library(lib)
                await broadcast("library_update", {"item_id": item_id, "status": "error"})
                return

        lib = await get_library()
        for it in lib["items"]:
            if it["id"] == item_id:
                it["torrent_hash"] = h
                break
        await put_library(lib)

        # Wait for torrent metadata to appear, then build the file list
        for _ in range(30):
            await asyncio.sleep(2)
            info = await qbit_info(h)
            if info:
                break

        if info := await qbit_info(h):
            save_path = info.get("save_path", settings.qbit_download_path)
            qfiles = await qbit_files(h)

            if selected_file_indices:
                # Skip non-selected files to avoid downloading them
                selected_set = set(selected_file_indices)
                skip_ids = [
                    f.get("index", i) for i, f in enumerate(qfiles)
                    if f.get("index", i) not in selected_set
                ]
                if skip_ids:
                    await qbit_set_file_priority(h, skip_ids, 0)
                sel_qfiles = [f for i, f in enumerate(qfiles) if f.get("index", i) in selected_set]
                files = build_file_list(sel_qfiles, save_path)
            else:
                files = build_file_list(qfiles, save_path)

            lib = await get_library()
            for it in lib["items"]:
                if it["id"] == item_id:
                    it["files"] = files
                    it["size_bytes"] = info.get("size", 0)
                    break
            await put_library(lib)

        await broadcast("library_update", {"item_id": item_id, "status": "downloading"})

    except Exception as exc:
        await broadcast("library_update", {"item_id": item_id, "status": "error", "message": str(exc)})


# ── FastAPI App ───────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator:
    global qbit, _lib_lock, _jackett_cookie_lock
    _lib_lock = asyncio.Lock()
    _jackett_cookie_lock = asyncio.Lock()
    qbit = httpx.AsyncClient(timeout=10.0)
    await qbit_login()
    Path(settings.qbit_download_path).mkdir(parents=True, exist_ok=True)

    guard       = asyncio.create_task(vpn_guard())
    broadcaster = asyncio.create_task(stat_broadcaster())
    dl_monitor  = asyncio.create_task(library_download_monitor())
    vlc_tracker = asyncio.create_task(vlc_progress_tracker())

    yield

    for t in (guard, broadcaster, dl_monitor, vlc_tracker):
        t.cancel()
    if state.stream_task and not state.stream_task.done():
        state.stream_task.cancel()
    await qbit.aclose()


app = FastAPI(title="P2P StreamLink", version="2.0", lifespan=lifespan)


@app.middleware("http")
async def admin_https_redirect(request: Request, call_next):
    """Redirect /admin and /api/admin/* to HTTPS when accessed over plain HTTP."""
    path = request.url.path
    if (path == "/admin" or path.startswith("/admin/") or path.startswith("/api/admin")):
        if request.url.scheme == "http":
            host = request.url.hostname
            qs   = ("?" + request.url.query) if request.url.query else ""
            return RedirectResponse(f"https://{host}{path}{qs}", status_code=301)
    return await call_next(request)


# ── Request Models ────────────────────────────────────────────────────────────

class StreamPrepareReq(BaseModel):
    magnet: str
    title: str


class StreamReq(BaseModel):
    magnet: str
    title: str
    file_index: Optional[int] = None     # specific file to stream (None = largest video)
    torrent_hash: Optional[str] = None   # hash from /stream/prepare (skips re-adding)


class DownloadReq(BaseModel):
    magnet: str
    title: str
    series: str = ""
    season: int = 0
    episode: int = 0
    save_path: str = ""
    torrent_hash: str = ""          # pre-added hash from /api/library/prepare
    selected_file_indices: list[int] = []  # if non-empty, skip all other files


class ProfileReq(BaseModel):
    name: str
    color: str = "indigo"


class ProgressReq(BaseModel):
    profile_id: str
    file_path: str
    position_sec: float
    duration_sec: float


class LibraryPlayReq(BaseModel):
    profile_id: str
    files: list[str] = []         # ordered list of absolute paths to play as a playlist
    seek_first_to: Optional[float] = None  # seek into the first file (seconds)


class MarkWatchedReq(BaseModel):
    profile_id: str
    watched: bool
    file_paths: list[str] = []    # specific paths; empty = all files in item
    season: Optional[int] = None  # if set (and file_paths empty), filter to this season


class AdminLoginReq(BaseModel):
    password: str


class AdminItemLockReq(BaseModel):
    admin_only: bool


class AdminSettingsReq(BaseModel):
    indexer_categories: Optional[str] = None


class ProfilePinReq(BaseModel):
    pin: str          # 4 digits to set, "" to clear
    current_pin: str = ""  # required when changing an existing PIN without admin token


class ProfileElevatedReq(BaseModel):
    elevated: bool    # whether this profile can view admin-only library items


class ProfileAutoSkipReq(BaseModel):
    auto_skip_intro: Optional[bool] = None
    auto_skip_credits: Optional[bool] = None


class SkipNowReq(BaseModel):
    type: str         # "intro" or "credits"


class AdminSkipDataReq(BaseModel):
    file_path: str
    intro_start: Optional[float] = None    # null = clear intro
    intro_end:   Optional[float] = None
    credits_start: Optional[float] = None  # null = clear credits


# ── Routes: Profiles ─────────────────────────────────────────────────────────

@app.get("/api/profiles")
async def list_profiles() -> JSONResponse:
    lib = await get_library()
    profiles = [
        {
            "id": p["id"],
            "name": p["name"],
            "color": p.get("color", "indigo"),
            "has_pin": bool(p.get("pin_hash", "")),
            "elevated": bool(p.get("elevated", False)),
            "auto_skip_intro":   bool(p.get("auto_skip_intro", False)),
            "auto_skip_credits": bool(p.get("auto_skip_credits", False)),
        }
        for p in lib["profiles"]
    ]
    return JSONResponse({"profiles": profiles})


@app.post("/api/profiles")
async def create_profile(req: ProfileReq) -> JSONResponse:
    lib = await get_library()
    if len(lib["profiles"]) >= 6:
        raise HTTPException(400, "Maximum 6 profiles reached.")
    profile = {"id": str(uuid.uuid4()), "name": req.name.strip()[:30], "color": req.color}
    lib["profiles"].append(profile)
    await put_library(lib)
    return JSONResponse({"profile": profile})


@app.delete("/api/profiles/{profile_id}")
async def delete_profile(profile_id: str) -> JSONResponse:
    lib = await get_library()
    lib["profiles"] = [p for p in lib["profiles"] if p["id"] != profile_id]
    for item in lib["items"]:
        item.get("progress", {}).pop(profile_id, None)
    await put_library(lib)
    return JSONResponse({"ok": True})


# ── Routes: Library ───────────────────────────────────────────────────────────

@app.get("/api/library")
async def list_library(request: Request, profile_id: str = "") -> JSONResponse:
    is_admin = _check_admin(request)
    lib = await get_library()
    elevated_ids = {p["id"] for p in lib["profiles"] if p.get("elevated")}
    is_elevated  = bool(profile_id) and profile_id in elevated_ids
    items = []
    for it in lib["items"]:
        if it.get("admin_only") and not is_admin and not is_elevated:
            continue
        files = it.get("files", [])
        resume = find_resume_hint(it, profile_id) if profile_id else None
        items.append({
            "id": it["id"],
            "title": it["title"],
            "series": it.get("series", ""),
            "season": it.get("season", 0),
            "episode": it.get("episode", 0),
            "file_count": len(files),
            "size_bytes": it.get("size_bytes", 0),
            "size_human": human_size(it.get("size_bytes", 0)),
            "added_at": it.get("added_at", ""),
            "status": it.get("status", "ready"),
            "torrent_hash": it.get("torrent_hash", ""),
            "resume": resume,
        })
    items.sort(key=lambda x: (
        x["series"] or "\xff" + x["title"],
        x["season"],
        x["episode"],
        x["added_at"],
    ))
    return JSONResponse({"items": items})


@app.get("/api/library/{item_id}/files")
async def get_item_files(item_id: str, profile_id: str = "") -> JSONResponse:
    """Return the video file list for a library item with per-profile progress."""
    lib = await get_library()
    item = next((it for it in lib["items"] if it["id"] == item_id), None)
    if not item:
        raise HTTPException(404, "Item not found.")

    file_progs = (
        item.get("progress", {}).get(profile_id, {}).get("file_progress", {})
        if profile_id else {}
    )

    out = []
    for f in item.get("files", []):
        path = f.get("path", "")
        fp = file_progs.get(path)
        if fp:
            dur = fp.get("duration_sec", 0)
            pos = fp.get("position_sec", 0)
            progress = {
                "position_sec": pos,
                "duration_sec": dur,
                "completed": fp.get("completed", False),
                "pct": round(pos / dur * 100, 1) if dur else 0,
            }
        else:
            progress = None
        out.append({
            "name": f.get("name", Path(path).name),
            "path": path,
            "size_bytes": f.get("size_bytes", 0),
            "size_human": human_size(f.get("size_bytes", 0)),
            "season": f.get("season", 0),
            "episode": f.get("episode", 0),
            "progress": progress,
        })
    return JSONResponse({"files": out, "item_status": item.get("status", "ready")})


@app.post("/api/library/download")
async def library_download(req: DownloadReq) -> JSONResponse:
    if not state.vpn_secure:
        raise HTTPException(403, "VPN not connected — download blocked.")
    lib = await get_library()
    item: dict = {
        "id": str(uuid.uuid4()),
        "title": req.title,
        "series": req.series,
        "season": req.season,
        "episode": req.episode,
        "files": [],
        "size_bytes": 0,
        "added_at": _now_iso(),
        "status": "downloading",
        "torrent_hash": "",
        "progress": {},
    }
    lib["items"].append(item)
    await put_library(lib)
    state.downloading_count += 1
    save_path = req.save_path.strip() or settings.qbit_download_path
    asyncio.create_task(library_download_pipeline(
        item["id"], req.magnet, save_path,
        torrent_hash=req.torrent_hash,
        selected_file_indices=req.selected_file_indices or None,
    ))
    return JSONResponse({"ok": True, "item_id": item["id"],
                         "default_save_path": settings.qbit_download_path})


@app.delete("/api/library/{item_id}")
async def delete_library_item(item_id: str, delete_file: bool = True) -> JSONResponse:
    lib = await get_library()
    item = next((it for it in lib["items"] if it["id"] == item_id), None)
    if not item:
        raise HTTPException(404, "Item not found.")
    h = item.get("torrent_hash")
    if h:
        await qbit_delete(h, delete_files=delete_file)
    lib["items"] = [it for it in lib["items"] if it["id"] != item_id]
    await put_library(lib)
    return JSONResponse({"ok": True})


@app.post("/api/library/{item_id}/queue-play")
async def queue_play(item_id: str, profile_id: str = "", file_path: str = "") -> JSONResponse:
    """Queue an in-progress download to auto-play when it (or a specific file) finishes."""
    if not profile_id:
        raise HTTPException(400, "profile_id required.")
    lib = await get_library()
    item = next((it for it in lib["items"] if it["id"] == item_id), None)
    if not item:
        raise HTTPException(404, "Item not found.")
    if item.get("status") != "downloading":
        raise HTTPException(400, "Item is not currently downloading.")
    state.play_when_ready_item_id = item_id
    state.play_when_ready_profile_id = profile_id
    state.play_when_ready_file_path = file_path or None
    h = item.get("torrent_hash")
    if h:
        if file_path:
            # Boost the specific file to max priority
            qfiles = await qbit_files(h)
            info = await qbit_info(h)
            if qfiles and info:
                sp = info.get("save_path", settings.qbit_download_path)
                idx = [qf.get("index", i) for i, qf in enumerate(qfiles)
                       if str(Path(sp) / qf.get("name", "")) == file_path]
                if idx:
                    await qbit_set_file_priority(h, idx, 7)
        else:
            await qreq("POST", "/api/v2/torrents/topPrio", data={"hashes": h})
    return JSONResponse({"ok": True})


@app.delete("/api/library/{item_id}/queue-play")
async def cancel_queue_play(item_id: str) -> JSONResponse:
    """Cancel a pending Play When Ready for this item."""
    if state.play_when_ready_item_id == item_id:
        state.play_when_ready_item_id = None
        state.play_when_ready_profile_id = None
    return JSONResponse({"ok": True})


class FilePriorityReq(BaseModel):
    file_paths: list[str]
    priority: int = 7   # 7=max, 1=normal, 0=skip


@app.post("/api/library/{item_id}/file-priority")
async def set_file_priority_for_item(item_id: str, req: FilePriorityReq) -> JSONResponse:
    """Set qBit download priority for specific files within a library item's torrent."""
    lib = await get_library()
    item = next((it for it in lib["items"] if it["id"] == item_id), None)
    if not item:
        raise HTTPException(404, "Item not found.")
    h = item.get("torrent_hash")
    if not h:
        raise HTTPException(400, "Item has no associated torrent.")
    qfiles = await qbit_files(h)
    info = await qbit_info(h)
    if not qfiles or not info:
        raise HTTPException(400, "Torrent not found in qBittorrent.")
    sp = info.get("save_path", settings.qbit_download_path)
    target_set = set(req.file_paths)
    indices = [
        qf.get("index", i) for i, qf in enumerate(qfiles)
        if str(Path(sp) / qf.get("name", "")) in target_set
    ]
    if not indices:
        raise HTTPException(400, "No matching files found in this torrent.")
    await qbit_set_file_priority(h, indices, req.priority)
    return JSONResponse({"ok": True, "updated": len(indices)})


@app.post("/api/library/{item_id}/play")
async def play_library_item(item_id: str, req: LibraryPlayReq) -> JSONResponse:
    lib = await get_library()
    item = next((it for it in lib["items"] if it["id"] == item_id), None)
    if not item:
        raise HTTPException(404, "Item not found.")

    # Resolve playlist — if caller passed explicit files use those, else auto-select
    playlist = req.files
    if not playlist:
        hint = find_resume_hint(item, req.profile_id)
        if hint and hint.get("file_path"):
            # Build playlist starting from the resume file
            all_paths = [f["path"] for f in item.get("files", [])]
            try:
                start_idx = all_paths.index(hint["file_path"])
                playlist = all_paths[start_idx:]
            except ValueError:
                playlist = all_paths
        else:
            playlist = [f["path"] for f in item.get("files", [])]

    if not playlist:
        raise HTTPException(400, "No video files available for this item.")

    # Filter to files that are on disk — partial downloads may not have all files yet
    existing = [p for p in playlist if Path(p).exists()]
    if not existing:
        raise HTTPException(400, f"File(s) not yet downloaded: {', '.join(Path(p).name for p in playlist[:3])}")
    playlist = existing

    # Build VLC playlist: play first, enqueue the rest
    first = Path(playlist[0])
    await vlc("in_play", input=first.resolve().as_uri())
    for p in playlist[1:]:
        await vlc("in_enqueue", input=Path(p).resolve().as_uri())
    asyncio.create_task(vlc_focus_and_fullscreen())

    # Update app state
    state.stream_status = "playing"
    state.active_title = item["title"]
    state.active_file = first
    state.current_audio_track = -1
    state.current_subtitle_track = -1
    state.track_pref_applied_file = playlist[0]  # mark as applied so tracker doesn't double-apply
    state.active_hash = item.get("torrent_hash") or None
    state.library_item_id = item_id
    state.library_profile_id = req.profile_id
    state.library_item_file_count = len(item.get("files", []))
    state.library_playlist = playlist
    state.library_current_file = playlist[0]
    state.skip_offer = None
    state.skip_offer_file = None

    # Seek into the first file after VLC has had time to open it
    seek_sec = req.seek_first_to
    if seek_sec is None:
        hint = find_resume_hint(item, req.profile_id)
        if hint and hint.get("position_sec", 0) > 5 and not hint.get("all_completed"):
            seek_sec = hint["position_sec"]

    if seek_sec and seek_sec > 5:
        async def _delayed_seek(s: float) -> None:
            await asyncio.sleep(3)
            await vlc("seek", val=str(int(s)))
        asyncio.create_task(_delayed_seek(seek_sec))

    # Apply saved track prefs for the first file (after VLC opens it)
    asyncio.create_task(_apply_track_prefs(item_id, req.profile_id, playlist[0], delay=3.5))

    await broadcast("stream_status", {"status": "playing", "message": f"Playing: {first.name}"})
    return JSONResponse({"ok": True, "playlist_count": len(playlist), "seek_to": seek_sec})


@app.post("/api/library/{item_id}/progress")
async def update_progress(item_id: str, req: ProgressReq) -> JSONResponse:
    lib = await get_library()
    item = next((it for it in lib["items"] if it["id"] == item_id), None)
    if not item:
        raise HTTPException(404, "Item not found.")
    dur = req.duration_sec
    pct = req.position_sec / dur if dur else 0
    prof_prog = item.setdefault("progress", {}).setdefault(req.profile_id, {})
    prof_prog["last_file"] = req.file_path
    prof_prog.setdefault("file_progress", {})[req.file_path] = {
        "position_sec": round(req.position_sec, 1),
        "duration_sec": round(req.duration_sec, 1),
        "completed": pct > 0.92,
        "updated_at": _now_iso(),
    }
    await put_library(lib)
    return JSONResponse({"ok": True})


@app.post("/api/library/{item_id}/mark-watched")
async def mark_watched(item_id: str, req: MarkWatchedReq) -> JSONResponse:
    """Mark or unmark episodes as watched for a profile."""
    lib = await get_library()
    item = next((it for it in lib["items"] if it["id"] == item_id), None)
    if not item:
        raise HTTPException(404, "Item not found.")

    files = item.get("files", [])
    if req.file_paths:
        targets = {p for p in req.file_paths}
        target_files = [f for f in files if f.get("path", "") in targets]
    elif req.season is not None:
        target_files = [f for f in files if f.get("season", 0) == req.season]
    else:
        target_files = files

    prof_prog = item.setdefault("progress", {}).setdefault(req.profile_id, {})
    file_prog = prof_prog.setdefault("file_progress", {})

    for f in target_files:
        path = f.get("path", "")
        if not path:
            continue
        existing = file_prog.get(path, {})
        if req.watched:
            file_prog[path] = {
                "position_sec": existing.get("duration_sec", 0),
                "duration_sec": existing.get("duration_sec", 0),
                "completed": True,
                "updated_at": _now_iso(),
                **{k: v for k, v in existing.items()
                   if k in ("audio_track", "subtitle_track")},
            }
        else:
            file_prog[path] = {
                "position_sec": 0,
                "duration_sec": existing.get("duration_sec", 0),
                "completed": False,
                "updated_at": _now_iso(),
                **{k: v for k, v in existing.items()
                   if k in ("audio_track", "subtitle_track")},
            }

    await put_library(lib)
    await broadcast("library_update", {"item_id": item_id, "status": item.get("status", "ready")})
    return JSONResponse({"ok": True, "updated": len(target_files)})


# ── Routes: Search & Stream ───────────────────────────────────────────────────

@app.get("/api/search")
async def search(q: str, limit: int = 30) -> JSONResponse:
    if not q.strip():
        return JSONResponse({"results": []})

    lib = await get_library()
    cats_override = lib.get("settings", {}).get("admin_overrides", {}).get("indexer_categories")
    cats = (cats_override if cats_override is not None else settings.indexer_categories).strip()
    params: dict = {"apikey": settings.indexer_api_key, "Query": q}
    if cats and cats != "0":
        params["Category[]"] = cats

    try:
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.get(
                f"{settings.indexer_url}/api/v2.0/indexers/all/results",
                params=params,
            )
        items = r.json().get("Results", [])
    except Exception as e:
        raise HTTPException(502, f"Indexer unreachable: {e}")

    results = []
    for it in items[: limit * 2]:
        mag = it.get("MagnetUri") or it.get("Link", "")
        if not mag:
            continue
        results.append({
            "title": it.get("Title", "Unknown"),
            "size": it.get("Size", 0),
            "size_human": human_size(it.get("Size", 0)),
            "seeders": it.get("Seeders", 0),
            "peers": it.get("Peers", 0),
            "magnet": mag,
            "tracker": it.get("Tracker", ""),
        })

    results.sort(key=lambda x: x["seeders"], reverse=True)
    return JSONResponse({"results": results[:limit]})


@app.post("/api/stream/prepare")
async def stream_prepare(req: StreamPrepareReq) -> JSONResponse:
    """Add magnet to qBit, wait for file metadata, return file list for the picker UI.

    The caller is responsible for either proceeding with /api/stream (using the returned
    hash) or cancelling with DELETE /api/stream/cancel?hash=... if the user dismisses
    the picker.  The torrent is stored in state.prepare_hash so /api/stop also cleans it up.
    """
    if not state.vpn_secure:
        raise HTTPException(403, "VPN not connected — streaming blocked.")

    # Clean up any previous prepare torrent that the user didn't explicitly cancel
    if state.prepare_hash:
        await qbit_delete(state.prepare_hash, delete_files=True)
        state.prepare_hash = None

    h = await qbit_add_magnet(req.magnet)
    if not h:
        raise HTTPException(500, "qBittorrent rejected the magnet.")

    state.prepare_hash = h

    # Wait for torrent to appear (metadata download)
    for _ in range(30):
        await asyncio.sleep(1)
        if await qbit_info(h):
            break
    else:
        await qbit_delete(h, delete_files=True)
        state.prepare_hash = None
        raise HTTPException(504, "Torrent metadata timed out — check connectivity and try again.")

    # Wait for file list (may need another moment after info appears)
    for _ in range(30):
        files = await qbit_files(h)
        if files:
            break
        await asyncio.sleep(1)
    else:
        await qbit_delete(h, delete_files=True)
        state.prepare_hash = None
        raise HTTPException(504, "Could not fetch file list — torrent may have no seeds.")

    result = [
        {
            "index": f.get("index", i),
            "name": f.get("name", ""),
            "size_bytes": f.get("size", 0),
            "size_human": human_size(f.get("size", 0)),
        }
        for i, f in enumerate(files)
    ]
    return JSONResponse({"hash": h, "files": result})


@app.delete("/api/stream/cancel")
async def stream_cancel(hash: str) -> JSONResponse:
    """Delete a torrent that was added by /stream/prepare but never started streaming."""
    await qbit_delete(hash, delete_files=True)
    if state.prepare_hash == hash:
        state.prepare_hash = None
    return JSONResponse({"ok": True})


@app.post("/api/library/prepare")
async def library_prepare(req: StreamPrepareReq) -> JSONResponse:
    """Fetch the file list for a torrent so the library file-picker UI can show checkboxes.

    Unlike /stream/prepare this does NOT touch state.prepare_hash — the caller is
    responsible for either completing the download (passing the returned hash to
    /api/library/download) or cancelling with DELETE /api/stream/cancel?hash=...
    """
    if not state.vpn_secure:
        raise HTTPException(403, "VPN not connected — download blocked.")

    h = await qbit_add_magnet(req.magnet)
    if not h:
        raise HTTPException(500, "qBittorrent rejected the magnet.")

    for _ in range(30):
        await asyncio.sleep(1)
        if await qbit_info(h):
            break
    else:
        await qbit_delete(h, delete_files=True)
        raise HTTPException(504, "Torrent metadata timed out — check connectivity and try again.")

    for _ in range(30):
        files = await qbit_files(h)
        if files:
            break
        await asyncio.sleep(1)
    else:
        await qbit_delete(h, delete_files=True)
        raise HTTPException(504, "Could not fetch file list — torrent may have no seeds.")

    result = [
        {
            "index": f.get("index", i),
            "name": f.get("name", ""),
            "size_bytes": f.get("size", 0),
            "size_human": human_size(f.get("size", 0)),
        }
        for i, f in enumerate(files)
    ]
    return JSONResponse({"hash": h, "files": result})


@app.post("/api/library/upload")
async def upload_to_library(
    files: list[UploadFile] = File(...),
    title: str = Form(""),
    series: str = Form(""),
    season: int = Form(0),
    episode: int = Form(0),
    save_path: str = Form(""),
) -> JSONResponse:
    """Accept one or more local video files and add them directly to the library."""
    if not files:
        raise HTTPException(400, "No files provided.")
    dest_dir = Path(save_path.strip() or settings.qbit_download_path)
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        raise HTTPException(500, f"Cannot create save directory: {e}")

    saved_files = []
    for upload in files:
        filename = Path(upload.filename or "upload").name
        if not filename or Path(filename).suffix.lower() not in VIDEO_EXTS:
            continue
        dest = dest_dir / filename
        stem, suffix = Path(filename).stem, Path(filename).suffix
        counter = 1
        while dest.exists():
            dest = dest_dir / f"{stem}_{counter}{suffix}"
            counter += 1
        with open(dest, "wb") as f:
            while chunk := await upload.read(1024 * 1024):
                f.write(chunk)
        s, ep = parse_season_episode(filename)
        saved_files.append({
            "name": dest.name,
            "path": str(dest),
            "size_bytes": dest.stat().st_size,
            "season": s,
            "episode": ep,
        })

    if not saved_files:
        raise HTTPException(400, "No supported video files were uploaded.")

    item: dict = {
        "id": str(uuid.uuid4()),
        "title": title.strip() or (Path(saved_files[0]["name"]).stem if len(saved_files) == 1 else "Uploaded Files"),
        "series": series.strip(),
        "season": season,
        "episode": episode,
        "files": saved_files,
        "size_bytes": sum(f["size_bytes"] for f in saved_files),
        "added_at": _now_iso(),
        "status": "ready",
        "torrent_hash": "",
        "progress": {},
    }
    lib = await get_library()
    lib["items"].append(item)
    await put_library(lib)
    await broadcast("library_update", {"item_id": item["id"], "status": "ready"})
    return JSONResponse({"ok": True, "item_id": item["id"], "file_count": len(saved_files)})


@app.post("/api/stream")
async def stream_now(req: StreamReq) -> JSONResponse:
    if not state.vpn_secure:
        raise HTTPException(403, "VPN not connected — streaming blocked.")

    if state.stream_task and not state.stream_task.done():
        state.stream_task.cancel()

    # Delete old torrent whether the previous task was still running or already done
    if state.active_hash and not state.library_item_id:
        await qbit_delete(state.active_hash)

    # Clean up orphaned prepare torrent that isn't the one being started now
    if state.prepare_hash and state.prepare_hash != req.torrent_hash:
        await qbit_delete(state.prepare_hash, delete_files=True)
        state.prepare_hash = None
    elif req.torrent_hash and state.prepare_hash == req.torrent_hash:
        state.prepare_hash = None

    state.active_hash = None
    state.active_file = None
    state.library_item_id = None
    state.library_profile_id = None
    state.library_item_file_count = 0
    state.library_playlist = []
    state.library_current_file = None
    state.stream_task = asyncio.create_task(
        stream_pipeline(req.magnet, req.title, req.file_index, req.torrent_hash)
    )
    return JSONResponse({"ok": True})


class SaveToLibraryReq(BaseModel):
    title: str = ""
    series: str = ""
    season: int = 0
    episode: int = 0
    save_path: str = ""


@app.post("/api/stream/save-to-library")
async def save_stream_to_library(req: SaveToLibraryReq) -> JSONResponse:
    """Adopt the currently streaming torrent into the persistent library.

    Restores all file priorities to 1 so the full torrent continues downloading,
    then creates a library entry and sets state.library_item_id so /api/stop
    will no longer auto-delete the torrent.
    """
    if not state.active_hash:
        raise HTTPException(400, "No active stream.")
    if state.library_item_id:
        raise HTTPException(400, "Stream is already saved to library.")

    h = state.active_hash

    # Restore all file priorities (streaming mode may have skipped some)
    all_files = await qbit_files(h)
    if all_files:
        all_ids = [f.get("index", i) for i, f in enumerate(all_files)]
        await qbit_set_file_priority(h, all_ids, 1)

    info = await qbit_info(h)
    save_path = req.save_path.strip() or (info.get("save_path") if info else None) or settings.qbit_download_path
    files = build_file_list(all_files, save_path) if all_files else []

    item: dict = {
        "id": str(uuid.uuid4()),
        "title": req.title.strip() or state.active_title or "Unknown",
        "series": req.series,
        "season": req.season,
        "episode": req.episode,
        "files": files,
        "size_bytes": info.get("size", 0) if info else 0,
        "added_at": _now_iso(),
        "status": "downloading",
        "torrent_hash": h,
        "progress": {},
    }

    lib = await get_library()
    lib["items"].append(item)
    await put_library(lib)
    state.downloading_count += 1
    state.library_item_id = item["id"]  # prevents auto-delete on stop

    await broadcast("library_update", {"item_id": item["id"], "status": "downloading"})
    return JSONResponse({"ok": True, "item_id": item["id"],
                         "default_save_path": settings.qbit_download_path})


@app.post("/api/stop")
async def stop() -> JSONResponse:
    if state.stream_task and not state.stream_task.done():
        state.stream_task.cancel()
    if state.active_hash and not state.library_item_id:
        await qbit_delete(state.active_hash)
    if state.prepare_hash:
        await qbit_delete(state.prepare_hash, delete_files=True)
        state.prepare_hash = None
    await vlc("pl_stop")
    asyncio.create_task(vlc_minimize())

    state.active_hash = None
    state.active_file = None
    state.active_title = None
    state.stream_status = "idle"
    state.library_item_id = None
    state.library_profile_id = None
    state.library_item_file_count = 0
    state.library_playlist = []
    state.library_current_file = None
    state.progress = 0.0
    state.downloaded_mb = 0.0
    state.total_mb = 0.0
    state.dl_speed_bps = 0
    state.ul_speed_bps = 0
    state.skip_offer = None
    state.skip_offer_file = None

    await broadcast("stream_status", {"status": "idle", "message": "Stopped."})
    return JSONResponse({"ok": True})


@app.post("/api/retry")
async def retry_playback() -> JSONResponse:
    """Relaunch VLC and replay the current file (handles VLC freezes / crashes)."""
    file_path: Optional[Path] = None
    if state.library_current_file:
        file_path = Path(state.library_current_file)
    elif state.active_file:
        file_path = state.active_file
    if not file_path:
        raise HTTPException(400, "Nothing to retry.")
    asyncio.create_task(_retry_task(file_path))
    return JSONResponse({"ok": True})


@app.post("/api/vlc/pause")
async def pause() -> JSONResponse:
    await vlc("pl_pause")
    return JSONResponse({"ok": True})


@app.post("/api/vlc/volume/set")
async def volume_set(volume: int) -> JSONResponse:
    # volume is 0-200 (100 = normal); VLC uses 0-512 (256 = 100%)
    raw = max(0, min(512, round(volume / 100 * 256)))
    await vlc("volume", val=str(raw))
    state.vlc_volume = max(0, min(200, volume))
    return JSONResponse({"ok": True})


@app.post("/api/vlc/volume/{direction}")
async def volume(direction: str) -> JSONResponse:
    if direction not in ("up", "down"):
        raise HTTPException(400, "direction must be 'up' or 'down'")
    await vlc("volume", val="+26" if direction == "up" else "-26")
    return JSONResponse({"ok": True})


@app.post("/api/vlc/seek")
async def seek(delta: float) -> JSONResponse:
    """Seek relative to current position.  delta is seconds; negative = rewind."""
    sign = "+" if delta >= 0 else ""
    await vlc("seek", val=f"{sign}{int(delta)}s")
    return JSONResponse({"ok": True})


@app.post("/api/vlc/seek/to")
async def seek_to(position_pct: float) -> JSONResponse:
    """Seek to an absolute position (0–100 %)."""
    pct = max(0.0, min(100.0, position_pct))
    await vlc("seek", val=f"{pct:.2f}%")
    return JSONResponse({"ok": True})


async def _item_all_paths() -> list[str]:
    """Return the full sorted file path list for the currently playing library item."""
    if not state.library_item_id:
        return []
    lib = await get_library()
    item = next((it for it in lib["items"] if it["id"] == state.library_item_id), None)
    return [f["path"] for f in item.get("files", [])] if item else []


def _find_in_paths(current: str, paths: list[str]) -> int:
    """Return the index of current in paths, trying exact match then resolved match."""
    try:
        return paths.index(current)
    except ValueError:
        pass
    try:
        resolved = Path(current).resolve()
        for i, p in enumerate(paths):
            try:
                if Path(p).resolve() == resolved:
                    return i
            except Exception:
                continue
    except Exception:
        pass
    return -1


@app.post("/api/vlc/prev")
async def vlc_prev() -> JSONResponse:
    """Jump to the previous episode in series order, regardless of how playback was started."""
    current = state.library_current_file
    if not current:
        raise HTTPException(400, "No active playback.")

    # Try the active playlist first, then fall back to the item's full file list
    prev_file: Optional[str] = None
    new_tail: list[str] = []

    idx = _find_in_paths(current, state.library_playlist)
    if idx > 0:
        prev_file = state.library_playlist[idx - 1]
        new_tail  = state.library_playlist[idx - 1:]
    else:
        all_paths = await _item_all_paths()
        item_idx  = _find_in_paths(current, all_paths)
        if item_idx > 0:
            prev_file = all_paths[item_idx - 1]
            new_tail  = all_paths[item_idx - 1:]

    if not prev_file:
        raise HTTPException(400, "Already at first episode.")
    if not Path(prev_file).exists():
        raise HTTPException(400, f"File not found: {Path(prev_file).name}")

    state.library_playlist    = new_tail
    state.library_current_file = prev_file
    state.current_audio_track = -1
    state.current_subtitle_track = -1
    state.track_pref_applied_file = prev_file
    state.skip_offer = None
    state.skip_offer_file = None
    await vlc("in_play", input=Path(prev_file).resolve().as_uri())
    for p in new_tail[1:]:
        await vlc("in_enqueue", input=Path(p).resolve().as_uri())
    if state.library_item_id and state.library_profile_id:
        asyncio.create_task(_apply_track_prefs(
            state.library_item_id, state.library_profile_id, prev_file, delay=2.0,
        ))
    await broadcast("stream_status", {"status": "playing", "message": f"Playing: {Path(prev_file).name}"})
    return JSONResponse({"ok": True})


@app.post("/api/vlc/next")
async def vlc_next() -> JSONResponse:
    """Jump to the next episode in series order, regardless of how playback was started."""
    current = state.library_current_file
    if not current:
        raise HTTPException(400, "No active playback.")

    # Try the active playlist first, then fall back to the item's full file list
    next_file: Optional[str] = None
    new_tail: list[str] = []

    idx = _find_in_paths(current, state.library_playlist)
    if 0 <= idx < len(state.library_playlist) - 1:
        next_file = state.library_playlist[idx + 1]
        new_tail  = state.library_playlist[idx + 1:]
    else:
        all_paths = await _item_all_paths()
        item_idx  = _find_in_paths(current, all_paths)
        if 0 <= item_idx < len(all_paths) - 1:
            next_file = all_paths[item_idx + 1]
            new_tail  = all_paths[item_idx + 1:]

    if not next_file:
        raise HTTPException(400, "Already at last episode.")
    if not Path(next_file).exists():
        raise HTTPException(400, f"File not found: {Path(next_file).name}")

    state.library_playlist    = new_tail
    state.library_current_file = next_file
    state.current_audio_track = -1
    state.current_subtitle_track = -1
    state.track_pref_applied_file = next_file
    state.skip_offer = None
    state.skip_offer_file = None
    await vlc("in_play", input=Path(next_file).resolve().as_uri())
    for p in new_tail[1:]:
        await vlc("in_enqueue", input=Path(p).resolve().as_uri())
    if state.library_item_id and state.library_profile_id:
        asyncio.create_task(_apply_track_prefs(
            state.library_item_id, state.library_profile_id, next_file, delay=2.0,
        ))
    await broadcast("stream_status", {"status": "playing", "message": f"Playing: {Path(next_file).name}"})
    return JSONResponse({"ok": True})


@app.get("/api/vlc/tracks")
async def get_tracks() -> JSONResponse:
    """Return available audio/subtitle tracks and which are currently selected.

    Track IDs are the actual ES (elementary stream) IDs from VLC — the number N
    in each 'Stream N' key.  VLC's audio_track / subtitle_track commands accept
    these same ES IDs, so sending the wrong sequential counter silently fails.
    The <audiotrack> / <subtitletrack> XML values are also ES IDs, so they must
    be compared against the same ES IDs for the 'current' highlight to work.
    """
    vs = await vlc_status()

    audio: list[dict] = []
    subtitle: list[dict] = [{"id": -1, "label": "Off", "language": ""}]
    audio_n = 1   # display-only counter for fallback labels
    sub_n   = 1

    if vs:
        cat = vs.get("information", {}).get("category", {})
        # Sort numerically by stream index so we process in file order
        stream_keys = sorted(
            (k for k in cat if k.startswith("Stream")),
            key=lambda k: int(k.split()[-1]) if k.split()[-1].isdigit() else 999,
        )
        for key in stream_keys:
            try:
                es_id = int(key.split()[-1])   # the actual ES ID VLC uses
            except (ValueError, IndexError):
                continue
            s     = cat[key]
            typ   = s.get("Type", "")
            lang  = s.get("Language", s.get("language", ""))
            codec = s.get("Codec", s.get("codec", ""))
            if typ == "Audio":
                label = lang or codec or f"Track {audio_n}"
                audio.append({"id": es_id, "label": label, "language": lang, "codec": codec})
                audio_n += 1
            elif typ == "Subtitle":
                label = lang or codec or f"Track {sub_n}"
                subtitle.append({"id": es_id, "label": label, "language": lang, "codec": codec})
                sub_n += 1

    # VLC 3.x does not expose current track selection in its HTTP API
    # (no <audiotrack>/<subtitletrack> in status.xml). We track it ourselves.
    return JSONResponse({
        "audio":    audio,
        "subtitle": subtitle,
        "current_audio":    state.current_audio_track,
        "current_subtitle": state.current_subtitle_track,
        "time":   vs.get("time",   0) if vs else 0,
        "length": vs.get("length", 0) if vs else 0,
    })


@app.post("/api/vlc/track/audio/{track_id}")
async def set_audio_track(track_id: int) -> JSONResponse:
    state.current_audio_track = track_id
    await vlc("audio_track", val=str(track_id))
    if state.library_item_id and state.library_profile_id and state.library_current_file:
        asyncio.create_task(_save_track_pref(
            state.library_item_id, state.library_profile_id,
            state.library_current_file, audio=track_id,
        ))
    return JSONResponse({"ok": True})


@app.post("/api/vlc/track/subtitle/{track_id}")
async def set_subtitle_track(track_id: int) -> JSONResponse:
    state.current_subtitle_track = track_id
    await vlc("subtitle_track", val=str(track_id))
    if state.library_item_id and state.library_profile_id and state.library_current_file:
        asyncio.create_task(_save_track_pref(
            state.library_item_id, state.library_profile_id,
            state.library_current_file, subtitle=track_id,
        ))
    return JSONResponse({"ok": True})


@app.get("/api/subtitles/search")
async def search_subtitles(query: str = "", lang: str = "") -> JSONResponse:
    """Find subtitles for the file VLC is playing — by movie hash (exact) and by
    name (fallback). `query` overrides the auto-derived name; `lang` is an
    optional 3-letter OpenSubtitles language id (blank = all languages)."""
    video = await _current_playback_path()
    file_hash: Optional[str] = None
    file_size: Optional[int] = None
    file_name = ""
    if video:
        file_name = video.name
        file_size = video.stat().st_size
        file_hash = await asyncio.to_thread(_opensubtitles_hash, video)
    q = query.strip() or (video.stem if video else "")
    if not file_hash and not q:
        raise HTTPException(409, "Nothing is playing and no search query was given.")
    results = await _opensubtitles_search(file_hash, file_size, q, lang.strip())
    return JSONResponse({"file": file_name, "hash": file_hash, "results": results})


class SubtitleDownloadReq(BaseModel):
    download_link: str
    lang: str = ""


@app.post("/api/subtitles/download")
async def download_subtitle(req: SubtitleDownloadReq) -> JSONResponse:
    """Download a chosen subtitle, save it next to the playing video, and load it
    into VLC as a new (and selected) subtitle track."""
    link = req.download_link.strip()
    host = (urlparse(link).hostname or "").lower()
    if not (host == "opensubtitles.org" or host.endswith(".opensubtitles.org")):
        raise HTTPException(400, "Invalid subtitle source.")
    video = await _current_playback_path()
    if not video:
        raise HTTPException(409, "No file is currently playing.")

    headers = {"User-Agent": settings.opensubtitles_user_agent}
    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as c:
            r = await c.get(link, headers=headers)
        r.raise_for_status()
        data = r.content
        if data[:2] == b"\x1f\x8b":   # gzip magic — OpenSubtitles serves .gz
            data = gzip.decompress(data)
    except Exception as e:
        raise HTTPException(502, f"Subtitle download failed: {e}")

    lang = re.sub(r"[^a-zA-Z]", "", req.lang)[:5].lower() or "sub"
    dest = video.with_name(f"{video.stem}.{lang}.srt")
    n = 2
    while dest.exists():
        dest = video.with_name(f"{video.stem}.{lang}.{n}.srt")
        n += 1
    try:
        dest.write_bytes(data)
    except Exception as e:
        raise HTTPException(500, f"Could not save subtitle file: {e}")

    # Load into VLC, then select the newly added subtitle track.
    await vlc("addsubtitle", val=str(dest.resolve()))
    await asyncio.sleep(0.6)
    new_id: Optional[int] = None
    vs = await vlc_status()
    if vs:
        cat = vs.get("information", {}).get("category", {})
        sub_ids = [
            int(k.split()[-1])
            for k, v in cat.items()
            if k.startswith("Stream") and k.split()[-1].isdigit()
            and v.get("Type") == "Subtitle"
        ]
        if sub_ids:
            new_id = max(sub_ids)
            state.current_subtitle_track = new_id
            await vlc("subtitle_track", val=str(new_id))
            if state.library_item_id and state.library_profile_id and state.library_current_file:
                asyncio.create_task(_save_track_pref(
                    state.library_item_id, state.library_profile_id,
                    state.library_current_file, subtitle=new_id,
                ))
    return JSONResponse({"ok": True, "saved": dest.name, "subtitle_track": new_id})


@app.get("/api/state")
async def get_state() -> JSONResponse:
    return JSONResponse(state_snapshot())


async def _all_library_paths() -> list[dict]:
    """All configured library paths: static (.env) + dynamic (UI-added via library.json)."""
    lib = await get_library()
    dynamic: list[str] = lib.get("settings", {}).get("library_paths", [])
    seen: set[str] = set()
    result = []
    for raw, is_static in [
        (settings.qbit_download_path, True),
        (settings.library_path_2, True),
        (settings.library_path_3, True),
        (settings.library_path_4, True),
        *((p, False) for p in dynamic),
    ]:
        p = (raw or "").strip()
        if p and p not in seen:
            seen.add(p)
            result.append({"path": p, "label": Path(p).name or p, "static": is_static})
    return result


@app.get("/api/settings/download-path")
async def get_download_path() -> JSONResponse:
    return JSONResponse({"path": settings.qbit_download_path})


@app.get("/api/settings/library-paths")
async def get_library_paths_api() -> JSONResponse:
    return JSONResponse({"paths": await _all_library_paths()})


@app.post("/api/settings/library-paths")
async def add_library_path(path: str) -> JSONResponse:
    p = path.strip()
    if not p:
        raise HTTPException(400, "Path cannot be empty.")
    if not Path(p).is_dir():
        raise HTTPException(400, f"Directory does not exist: {p}")
    lib = await get_library()
    existing = [info["path"] for info in await _all_library_paths()]
    if p in existing:
        raise HTTPException(400, "Path is already configured.")
    lib.setdefault("settings", {}).setdefault("library_paths", []).append(p)
    await put_library(lib)
    return JSONResponse({"ok": True})


@app.delete("/api/settings/library-paths")
async def remove_library_path(path: str) -> JSONResponse:
    lib = await get_library()
    paths: list = lib.get("settings", {}).get("library_paths", [])
    if path not in paths:
        raise HTTPException(404, "Path not found in UI-configured paths (static .env paths cannot be removed here).")
    paths.remove(path)
    await put_library(lib)
    return JSONResponse({"ok": True})


@app.get("/api/settings/disk-space")
async def get_disk_space() -> JSONResponse:
    disks = []
    for info in await _all_library_paths():
        try:
            usage = shutil.disk_usage(info["path"])
            disks.append({
                "path":        info["path"],
                "label":       info["label"],
                "total_bytes": usage.total,
                "free_bytes":  usage.free,
                "total_human": human_size(usage.total),
                "free_human":  human_size(usage.free),
                "free_pct":    round(usage.free / usage.total * 100, 1) if usage.total else 0,
            })
        except Exception as e:
            disks.append({"path": info["path"], "label": info["label"], "error": str(e)})
    primary = disks[0] if disks else {}
    return JSONResponse({**primary, "disks": disks})


@app.get("/api/library/{item_id}/download")
async def download_library_file(item_id: str, file_path: str = "") -> FileResponse:
    """Stream a library file to the browser for download.

    For single-file items the path can be omitted; for multi-file items pass
    the absolute file_path as a query parameter.
    """
    lib = await get_library()
    item = next((it for it in lib["items"] if it["id"] == item_id), None)
    if not item:
        raise HTTPException(404, "Item not found.")

    files = item.get("files", [])
    if not files:
        raise HTTPException(404, "No files associated with this item.")

    if file_path:
        target = next((f for f in files if f["path"] == file_path), None)
        if not target:
            raise HTTPException(404, "File not found in this item.")
        path = Path(target["path"])
    else:
        if len(files) != 1:
            raise HTTPException(400, "Multiple files — pass file_path query parameter.")
        path = Path(files[0]["path"])

    if not path.exists():
        raise HTTPException(404, f"File not on disk: {path.name}")

    return FileResponse(
        path=str(path),
        filename=path.name,
        media_type="application/octet-stream",
    )


class ZipDownloadReq(BaseModel):
    file_paths: list[str] = []   # empty = all files in item


@app.post("/api/library/{item_id}/download-zip")
async def download_library_zip(item_id: str, req: ZipDownloadReq) -> StreamingResponse:
    """Stream a ZIP of selected (or all) library files to the browser.

    Uses a pipe so the ZIP is streamed without buffering entire video files in memory.
    ZIP_STORED is used — video files are already compressed.
    """
    lib = await get_library()
    item = next((it for it in lib["items"] if it["id"] == item_id), None)
    if not item:
        raise HTTPException(404, "Item not found.")

    all_files = item.get("files", [])
    if req.file_paths:
        valid_paths = {f["path"] for f in all_files}
        targets = [p for p in req.file_paths if p in valid_paths and Path(p).exists()]
    else:
        targets = [f["path"] for f in all_files if Path(f["path"]).exists()]

    if not targets:
        raise HTTPException(404, "No files available for download.")

    zip_name = re.sub(r'[\\/*?:"<>|]', "_", item["title"]) + ".zip"

    r_fd, w_fd = os.pipe()

    def _write_zip() -> None:
        try:
            with os.fdopen(w_fd, "wb") as wf, zipfile.ZipFile(wf, "w", zipfile.ZIP_STORED) as zf:
                for path_str in targets:
                    p = Path(path_str)
                    zf.write(str(p), p.name)
        except Exception:
            pass  # reader will get EOF; any partial data is discarded by the browser

    threading.Thread(target=_write_zip, daemon=True).start()

    async def _read_pipe() -> AsyncGenerator[bytes, None]:
        rf = os.fdopen(r_fd, "rb")
        try:
            while True:
                chunk = await asyncio.to_thread(rf.read, 65536)
                if not chunk:
                    break
                yield chunk
        finally:
            rf.close()

    return StreamingResponse(
        _read_pipe(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{zip_name}"'},
    )


@app.get("/api/events")
async def events(request: Request) -> StreamingResponse:
    q: asyncio.Queue = asyncio.Queue(maxsize=100)
    state.sse_queues.append(q)

    async def stream() -> AsyncGenerator[str, None]:
        yield f"event: state\ndata: {json.dumps(state_snapshot())}\n\n"
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    yield await asyncio.wait_for(q.get(), timeout=20.0)
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
        finally:
            if q in state.sse_queues:
                state.sse_queues.remove(q)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Routes: Admin Panel ───────────────────────────────────────────────────────

@app.get("/admin", include_in_schema=False)
async def admin_page() -> FileResponse:
    return FileResponse(str(Path(__file__).parent / "static" / "admin.html"))


@app.get("/api/admin/status")
async def admin_status() -> JSONResponse:
    return JSONResponse({"enabled": bool(settings.admin_password)})


@app.post("/api/admin/login")
async def admin_login(req: AdminLoginReq) -> JSONResponse:
    if not settings.admin_password:
        raise HTTPException(503, "Admin panel is not configured (set ADMIN_PASSWORD in .env).")
    if req.password != settings.admin_password:
        raise HTTPException(401, "Incorrect admin password.")
    token = secrets.token_hex(32)
    _admin_sessions[token] = time.time() + 86400   # 24-hour session
    return JSONResponse({"ok": True, "token": token})


@app.post("/api/admin/logout")
async def admin_logout(request: Request) -> JSONResponse:
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    _admin_sessions.pop(token, None)
    return JSONResponse({"ok": True})


@app.get("/api/admin/indexers")
async def admin_list_indexers(request: Request) -> JSONResponse:
    _require_admin(request)
    try:
        async with _jackett_admin() as c:
            r = await c.get(
                f"{settings.indexer_url}/api/v2.0/indexers",
                params={"configured": "true"},
            )
        return JSONResponse({"indexers": r.json() if r.status_code == 200 else []})
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"Could not reach Jackett: {e}")


@app.get("/api/admin/indexers/available")
async def admin_list_available_indexers(request: Request) -> JSONResponse:
    _require_admin(request)
    try:
        async with _jackett_admin() as c:
            r = await c.get(
                f"{settings.indexer_url}/api/v2.0/indexers",
                params={"configured": "false"},
            )
        return JSONResponse({"indexers": r.json() if r.status_code == 200 else []})
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"Could not reach Jackett: {e}")


@app.get("/api/admin/indexers/{indexer_id}/config")
async def admin_get_indexer_config(indexer_id: str, request: Request) -> JSONResponse:
    _require_admin(request)
    try:
        async with _jackett_admin() as c:
            r = await c.get(f"{settings.indexer_url}/api/v2.0/indexers/{indexer_id}/config")
        if r.status_code != 200:
            raise HTTPException(502, f"Jackett returned {r.status_code}")
        return JSONResponse({"config": r.json()})
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"Could not reach Jackett: {e}")


@app.post("/api/admin/indexers/{indexer_id}/config")
async def admin_save_indexer_config(indexer_id: str, request: Request) -> JSONResponse:
    _require_admin(request)
    body = await request.json()
    try:
        async with _jackett_admin() as c:
            r = await c.post(
                f"{settings.indexer_url}/api/v2.0/indexers/{indexer_id}/config",
                json=body,
            )
        if r.status_code >= 300:
            raise HTTPException(502, f"Jackett returned {r.status_code}")
        return JSONResponse({"ok": True})
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"Could not reach Jackett: {e}")


@app.delete("/api/admin/indexers/{indexer_id}")
async def admin_delete_indexer(indexer_id: str, request: Request) -> JSONResponse:
    _require_admin(request)
    try:
        async with _jackett_admin() as c:
            r = await c.delete(f"{settings.indexer_url}/api/v2.0/indexers/{indexer_id}")
        return JSONResponse({"ok": r.status_code < 300})
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"Could not reach Jackett: {e}")


@app.get("/api/admin/settings")
async def admin_get_settings(request: Request) -> JSONResponse:
    _require_admin(request)
    lib = await get_library()
    overrides = lib.get("settings", {}).get("admin_overrides", {})
    return JSONResponse({
        "indexer_url": settings.indexer_url,
        "indexer_api_key": settings.indexer_api_key,
        "indexer_categories": overrides.get("indexer_categories", settings.indexer_categories),
    })


@app.post("/api/admin/settings")
async def admin_update_settings(request: Request, req: AdminSettingsReq) -> JSONResponse:
    _require_admin(request)
    lib = await get_library()
    overrides = lib.setdefault("settings", {}).setdefault("admin_overrides", {})
    if req.indexer_categories is not None:
        overrides["indexer_categories"] = req.indexer_categories.strip()
    await put_library(lib)
    return JSONResponse({"ok": True})


@app.post("/api/library/{item_id}/admin-lock")
async def admin_lock_item(item_id: str, request: Request, req: AdminItemLockReq) -> JSONResponse:
    _require_admin(request)
    lib = await get_library()
    item = next((it for it in lib["items"] if it["id"] == item_id), None)
    if not item:
        raise HTTPException(404, "Item not found.")
    item["admin_only"] = req.admin_only
    await put_library(lib)
    return JSONResponse({"ok": True})


@app.get("/api/admin/library")
async def admin_list_library(request: Request) -> JSONResponse:
    """Return ALL library items including admin-only ones (admin auth required)."""
    _require_admin(request)
    lib = await get_library()
    items = []
    for it in lib["items"]:
        files = it.get("files", [])
        series_key = _series_key(it)
        skip_data = it.get("skip_data", {})
        files_with_skip = sum(1 for f in files if f.get("path", "") in skip_data)
        items.append({
            "id": it["id"],
            "title": it["title"],
            "series": it.get("series", ""),
            "season": it.get("season", 0),
            "episode": it.get("episode", 0),
            "file_count": len(files),
            "size_human": human_size(it.get("size_bytes", 0)),
            "status": it.get("status", "ready"),
            "admin_only": it.get("admin_only", False),
            "series_key": series_key,
            "files_with_skip": files_with_skip,
            "analysis_job": state.analysis_jobs.get(series_key),
        })
    items.sort(key=lambda x: (x["series"] or "\xff" + x["title"], x["season"], x["episode"]))
    return JSONResponse({"items": items, "jobs": state.analysis_jobs})


# ── Routes: Profile PINs ──────────────────────────────────────────────────────

@app.post("/api/profiles/{profile_id}/set-pin")
async def set_profile_pin(profile_id: str, request: Request, req: ProfilePinReq) -> JSONResponse:
    """Set or clear a profile PIN.

    Admin token: always allowed.
    No token: allowed if the profile has no existing PIN (first-time set).
              If the profile already has a PIN, `current_pin` must be provided and correct.
    """
    is_admin = _check_admin(request)
    pin = req.pin.strip()
    if pin and (len(pin) != 4 or not pin.isdigit()):
        raise HTTPException(400, "PIN must be exactly 4 digits, or empty to clear.")
    lib = await get_library()
    profile = next((p for p in lib["profiles"] if p["id"] == profile_id), None)
    if not profile:
        raise HTTPException(404, "Profile not found.")
    if not is_admin:
        existing = profile.get("pin_hash", "")
        if existing:
            current = req.current_pin.strip()
            if not current or _pin_hash(current) != existing:
                raise HTTPException(403, "Current PIN is incorrect.")
    if pin:
        profile["pin_hash"] = _pin_hash(pin)
    else:
        profile.pop("pin_hash", None)
    await put_library(lib)
    return JSONResponse({"ok": True, "has_pin": bool(pin)})


@app.post("/api/profiles/{profile_id}/set-elevated")
async def set_profile_elevated(profile_id: str, request: Request, req: ProfileElevatedReq) -> JSONResponse:
    """Grant or revoke access to admin-only library items for a profile (admin only)."""
    _require_admin(request)
    lib = await get_library()
    profile = next((p for p in lib["profiles"] if p["id"] == profile_id), None)
    if not profile:
        raise HTTPException(404, "Profile not found.")
    if req.elevated:
        profile["elevated"] = True
    else:
        profile.pop("elevated", None)
    await put_library(lib)
    return JSONResponse({"ok": True, "elevated": req.elevated})


@app.post("/api/profiles/{profile_id}/verify-pin")
async def verify_profile_pin(profile_id: str, req: ProfilePinReq) -> JSONResponse:
    lib = await get_library()
    profile = next((p for p in lib["profiles"] if p["id"] == profile_id), None)
    if not profile:
        raise HTTPException(404, "Profile not found.")
    stored = profile.get("pin_hash", "")
    if not stored:
        return JSONResponse({"ok": True})   # no PIN set — always pass
    if _pin_hash(req.pin.strip()) != stored:
        raise HTTPException(403, "Incorrect PIN.")
    return JSONResponse({"ok": True})


# ── Routes: Smart Skip ────────────────────────────────────────────────────────

@app.post("/api/profiles/{profile_id}/auto-skip")
async def set_profile_auto_skip(profile_id: str, req: ProfileAutoSkipReq) -> JSONResponse:
    """Toggle the per-profile auto-skip preferences for intro and credits."""
    lib = await get_library()
    profile = next((p for p in lib["profiles"] if p["id"] == profile_id), None)
    if not profile:
        raise HTTPException(404, "Profile not found.")
    if req.auto_skip_intro is not None:
        if req.auto_skip_intro:
            profile["auto_skip_intro"] = True
        else:
            profile.pop("auto_skip_intro", None)
    if req.auto_skip_credits is not None:
        if req.auto_skip_credits:
            profile["auto_skip_credits"] = True
        else:
            profile.pop("auto_skip_credits", None)
    await put_library(lib)
    return JSONResponse({
        "ok": True,
        "auto_skip_intro":   bool(profile.get("auto_skip_intro", False)),
        "auto_skip_credits": bool(profile.get("auto_skip_credits", False)),
    })


@app.post("/api/skip-now")
async def skip_now(req: SkipNowReq) -> JSONResponse:
    """Execute the current Smart Skip offer (called by the client's Skip button)."""
    offer = state.skip_offer
    if not offer or offer.get("type") != req.type:
        raise HTTPException(400, "No matching skip offer is active.")

    if req.type == "intro":
        end_at = float(offer.get("end_at", 0))
        if end_at <= 0:
            raise HTTPException(400, "Invalid intro end position.")
        await vlc("seek", val=str(int(end_at) + 1))
        # Mark this file's intro as handled so the offer doesn't re-show
        if state.skip_offer_file:
            state.skip_offer_file = f"{state.skip_offer_file}#intro-done"
        state.skip_offer = None
        await broadcast("state", state_snapshot())
        return JSONResponse({"ok": True, "action": "seek"})

    if req.type == "credits":
        next_path = offer.get("next_file_path")
        cur_file = offer.get("file_path", "")
        if next_path and Path(next_path).exists():
            lib = await get_library()
            item = next((it for it in lib["items"] if it["id"] == state.library_item_id), None)
            if not item:
                raise HTTPException(404, "Item not found.")
            await vlc_next_file(cur_file, item)
            state.skip_offer = None
            state.skip_offer_file = f"{cur_file}#credits-done"
            await broadcast("state", state_snapshot())
            return JSONResponse({"ok": True, "action": "next_episode"})
        # No next file — just stop playback
        await vlc("pl_stop")
        state.skip_offer = None
        state.skip_offer_file = f"{cur_file}#credits-done"
        await broadcast("state", state_snapshot())
        return JSONResponse({"ok": True, "action": "stop"})

    raise HTTPException(400, "Unknown skip type.")


@app.delete("/api/skip-now")
async def dismiss_skip_offer() -> JSONResponse:
    """Dismiss the current Smart Skip offer without acting on it.

    Marks the offer as handled for the current file so it doesn't re-show on
    the next progress tick. The user can still hit Next/Stop manually.
    """
    if state.skip_offer_file and not state.skip_offer_file.endswith("#dismissed"):
        offer_type = (state.skip_offer or {}).get("type", "intro")
        state.skip_offer_file = f"{state.skip_offer_file}#{offer_type}-done"
    state.skip_offer = None
    await broadcast("state", state_snapshot())
    return JSONResponse({"ok": True})


@app.get("/api/admin/library/{item_id}/skip-data")
async def admin_get_skip_data(item_id: str, request: Request) -> JSONResponse:
    """Return per-file intro/credits times for an item (admin manual editor)."""
    _require_admin(request)
    lib = await get_library()
    item = next((it for it in lib["items"] if it["id"] == item_id), None)
    if not item:
        raise HTTPException(404, "Item not found.")
    skip_data = item.get("skip_data", {})
    files_out = []
    for f in item.get("files", []):
        path = f.get("path", "")
        entry = skip_data.get(path) or {}
        intro = entry.get("intro") or {}
        files_out.append({
            "name": f.get("name", Path(path).name),
            "path": path,
            "intro_start":   intro.get("start"),
            "intro_end":     intro.get("end"),
            "credits_start": entry.get("credits_start"),
            "source":        (entry.get("analysis") or {}).get("source", ""),
        })
    return JSONResponse({"files": files_out, "series_key": _series_key(item)})


@app.patch("/api/admin/library/{item_id}/skip-data")
async def admin_set_skip_data(item_id: str, request: Request, req: AdminSkipDataReq) -> JSONResponse:
    """Manual override: set intro/credits times for one file in an item."""
    _require_admin(request)
    lib = await get_library()
    item = next((it for it in lib["items"] if it["id"] == item_id), None)
    if not item:
        raise HTTPException(404, "Item not found.")
    paths = {f.get("path", "") for f in item.get("files", [])}
    if req.file_path not in paths:
        raise HTTPException(400, "file_path is not in this item.")
    skip_data = item.setdefault("skip_data", {})
    entry = skip_data.setdefault(req.file_path, {})
    if req.intro_start is not None and req.intro_end is not None and req.intro_end > req.intro_start:
        entry["intro"] = {"start": round(req.intro_start, 1), "end": round(req.intro_end, 1)}
    elif req.intro_start is None and req.intro_end is None:
        entry.pop("intro", None)
    if req.credits_start is not None:
        entry["credits_start"] = round(req.credits_start, 1) if req.credits_start > 0 else None
        if entry["credits_start"] is None:
            entry.pop("credits_start", None)
    entry["analysis"] = {"version": analyzer.ANALYZER_VERSION, "source": "manual"}
    await put_library(lib)
    return JSONResponse({"ok": True})


@app.post("/api/admin/library/{item_id}/analyze")
async def admin_analyze_series(item_id: str, request: Request) -> JSONResponse:
    """Force a (re-)analysis of the series this item belongs to."""
    _require_admin(request)
    if not analyzer.is_available():
        raise HTTPException(503, "ffmpeg/fpcalc not available on this host.")
    lib = await get_library()
    item = next((it for it in lib["items"] if it["id"] == item_id), None)
    if not item:
        raise HTTPException(404, "Item not found.")
    key = _series_key(item)
    asyncio.create_task(_run_series_analysis(key))
    return JSONResponse({"ok": True, "series_key": key})


@app.get("/api/admin/analyzer-status")
async def admin_analyzer_status(request: Request) -> JSONResponse:
    _require_admin(request)
    return JSONResponse({
        "available": analyzer.is_available(),
        "ffmpeg":    analyzer.ffmpeg_bin(),
        "fpcalc":    analyzer.fpcalc_bin(),
    })


# Static files must be mounted last so API routes take priority
app.mount("/", StaticFiles(directory="static", html=True), name="static")
