"""P2P StreamLink v2.0 — FastAPI backend"""

import asyncio
import base64
import json
import platform
import re
import shutil
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncGenerator, Optional
from urllib.parse import unquote, urlparse

import httpx
import psutil
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    vlc_time: int = 0                                     # VLC current position (seconds)
    vlc_duration: int = 0                                 # VLC total duration (seconds)
    vlc_volume: int = 100                                 # VLC volume 0-200 (100 = normal)
    prepare_hash: Optional[str] = None                    # hash added by /stream/prepare, pending user selection
    sse_queues: list = field(default_factory=list)


state = AppState()
qbit: Optional[httpx.AsyncClient] = None


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
    }


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


# ── Background Task: VLC Progress Tracker ────────────────────────────────────

async def vlc_progress_tracker() -> None:
    """Save per-episode watch progress for library items playing in VLC, every 15 s."""
    while True:
        await asyncio.sleep(15)
        if not state.library_item_id or not state.library_profile_id:
            continue
        try:
            vs = await vlc_status()
            if not vs:
                continue
            pos_sec = float(vs.get("time", 0))
            dur_sec = float(vs.get("length", 0))
            if dur_sec < 10:
                continue

            # Detect which playlist item VLC is currently playing
            current_uri = await vlc_playlist_uri()
            if current_uri and current_uri.startswith("file://"):
                state.library_current_file = uri_to_path(current_uri)

            current_file = state.library_current_file
            if not current_file:
                continue

            lib = await get_library()
            item = next((it for it in lib["items"] if it["id"] == state.library_item_id), None)
            if not item:
                continue

            # Normalize to the stored item path so progress keys always match
            # what get_item_files and find_resume_hint look up by.
            current_file = _canonical_item_path(current_file, item)
            state.library_current_file = current_file

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
    global qbit, _lib_lock
    _lib_lock = asyncio.Lock()
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


# ── Routes: Profiles ─────────────────────────────────────────────────────────

@app.get("/api/profiles")
async def list_profiles() -> JSONResponse:
    lib = await get_library()
    return JSONResponse({"profiles": lib["profiles"]})


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
async def list_library(profile_id: str = "") -> JSONResponse:
    lib = await get_library()
    items = []
    for it in lib["items"]:
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

    # Update app state
    state.stream_status = "playing"
    state.active_title = item["title"]
    state.active_file = first
    state.current_audio_track = -1
    state.current_subtitle_track = -1
    state.active_hash = item.get("torrent_hash") or None
    state.library_item_id = item_id
    state.library_profile_id = req.profile_id
    state.library_item_file_count = len(item.get("files", []))
    state.library_playlist = playlist
    state.library_current_file = playlist[0]

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


# ── Routes: Search & Stream ───────────────────────────────────────────────────

@app.get("/api/search")
async def search(q: str, limit: int = 30) -> JSONResponse:
    if not q.strip():
        return JSONResponse({"results": []})

    params: dict = {"apikey": settings.indexer_api_key, "Query": q}
    cats = settings.indexer_categories.strip()
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

    await broadcast("stream_status", {"status": "idle", "message": "Stopped."})
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
    await vlc("in_play", input=Path(prev_file).resolve().as_uri())
    for p in new_tail[1:]:
        await vlc("in_enqueue", input=Path(p).resolve().as_uri())
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
    await vlc("in_play", input=Path(next_file).resolve().as_uri())
    for p in new_tail[1:]:
        await vlc("in_enqueue", input=Path(p).resolve().as_uri())
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
    return JSONResponse({"ok": True})


@app.post("/api/vlc/track/subtitle/{track_id}")
async def set_subtitle_track(track_id: int) -> JSONResponse:
    state.current_subtitle_track = track_id
    await vlc("subtitle_track", val=str(track_id))
    return JSONResponse({"ok": True})


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


# Static files must be mounted last so API routes take priority
app.mount("/", StaticFiles(directory="static", html=True), name="static")
