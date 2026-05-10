"""P2P StreamLink v2.0 — FastAPI backend"""

import asyncio
import base64
import json
import platform
import re
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncGenerator, Optional

import httpx
import psutil
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
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

    vlc_url: str = "http://localhost:8080"
    vlc_password: str = "vlcpassword"

    buffer_min_mb: float = 15.0
    buffer_min_pct: float = 1.0


settings = Settings()
LIBRARY_FILE = Path(__file__).parent / "library.json"
_lib_lock: asyncio.Lock  # initialised in lifespan


# ── Library Storage ───────────────────────────────────────────────────────────

def _load_lib_raw() -> dict:
    if LIBRARY_FILE.exists():
        try:
            return json.loads(LIBRARY_FILE.read_text(encoding="utf-8"))
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
    library_item_id: Optional[str] = None   # item currently playing from library
    library_profile_id: Optional[str] = None
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


async def qbit_add_magnet(magnet: str, save_path: Optional[str] = None) -> Optional[str]:
    h = extract_hash(magnet)
    data = {"urls": magnet, "savepath": save_path or settings.qbit_download_path}
    r = await qreq("POST", "/api/v2/torrents/add", data=data)
    return h if (r and r.text.strip() == "Ok.") else None


async def qbit_streaming_mode(h: str) -> None:
    """Enable sequential download and ensure first/last piece priority is ON.

    toggleFirstLastPiecePrio is a toggle — calling it on a torrent that already
    has it enabled would turn it OFF.  We read current state first to avoid that.
    """
    await qreq("POST", "/api/v2/torrents/setSequentialDownload",
                data={"hashes": h, "enable": "true"})
    info = await qbit_info(h)
    if info is not None and not info.get("f_l_piece_prio", False):
        await qreq("POST", "/api/v2/torrents/toggleFirstLastPiecePrio",
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


def largest_video(files: list) -> Optional[dict]:
    VIDEO = {".mkv", ".mp4", ".avi", ".mov", ".wmv", ".m4v", ".ts", ".m2ts", ".webm"}
    videos = [f for f in files if Path(f["name"]).suffix.lower() in VIDEO]
    pool = videos or files
    return max(pool, key=lambda f: f.get("size", 0), default=None)


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


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
        await broadcast("state", state_snapshot())
        await asyncio.sleep(2)


# ── Background Task: Library Download Monitor ─────────────────────────────────

async def library_download_monitor() -> None:
    """Poll qBit every 10 s for pending library downloads and mark them complete."""
    while True:
        await asyncio.sleep(10)
        try:
            lib = await get_library()
            pending = [it for it in lib["items"] if it.get("status") == "downloading"]
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
                qstate = info.get("state", "")
                if qstate in ("uploading", "stalledUP", "pausedUP", "queuedUP", "forcedUP"):
                    item["status"] = "ready"
                    item["size_bytes"] = info.get("size", 0)
                    changed = True
                    await broadcast("library_update", {"item_id": item["id"], "status": "ready"})
                elif qstate in ("error", "missingFiles"):
                    item["status"] = "error"
                    changed = True
                    await broadcast("library_update", {"item_id": item["id"], "status": "error"})
            if changed:
                await put_library(lib)
        except Exception:
            pass


# ── Background Task: VLC Progress Tracker ────────────────────────────────────

async def vlc_progress_tracker() -> None:
    """Save watch progress for library items playing in VLC every 15 s."""
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

            lib = await get_library()
            item = next((it for it in lib["items"] if it["id"] == state.library_item_id), None)
            if not item:
                continue

            pct = pos_sec / dur_sec
            item.setdefault("progress", {})[state.library_profile_id] = {
                "position_sec": round(pos_sec, 1),
                "duration_sec": round(dur_sec, 1),
                "completed": pct > 0.92,
                "updated_at": _now_iso(),
            }
            await put_library(lib)
            await broadcast("progress_saved", {
                "item_id": state.library_item_id,
                "profile_id": state.library_profile_id,
                "position_sec": pos_sec,
                "duration_sec": dur_sec,
                "pct": round(pct * 100, 1),
            })
        except Exception:
            pass


# ── Stream Pipeline (stream-now, torrent auto-deleted on stop) ────────────────

async def stream_pipeline(magnet: str, title: str) -> None:
    try:
        state.active_title = title
        state.stream_status = "buffering"
        await broadcast("stream_status", {"status": "buffering", "message": "Adding torrent to qBittorrent…"})

        h = await qbit_add_magnet(magnet)
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
        await broadcast("stream_status", {"status": "buffering", "message": "Sequential mode set. Buffering first pieces…"})

        while True:
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
                    "progress": pct,
                    "downloaded_mb": mb,
                    "total_mb": state.total_mb,
                    "dl_speed_bps": state.dl_speed_bps,
                    "ul_speed_bps": state.ul_speed_bps,
                })

                if mb >= settings.buffer_min_mb or pct >= settings.buffer_min_pct:
                    break

            await asyncio.sleep(1)

        files = await qbit_files(h)
        vid = largest_video(files)
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

async def library_download_pipeline(item_id: str, magnet: str) -> None:
    """Add magnet to qBit for a full download; no streaming mode, no auto-delete."""
    try:
        h = await qbit_add_magnet(magnet)
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

        # Wait for torrent to appear, then resolve the file path
        for _ in range(30):
            await asyncio.sleep(2)
            info = await qbit_info(h)
            if info:
                break

        files = await qbit_files(h)
        vid = largest_video(files)
        if vid:
            info = await qbit_info(h)
            save_path = (info or {}).get("save_path", settings.qbit_download_path)
            file_path = str(Path(save_path) / vid["name"])
            size_bytes = info.get("size", 0) if info else 0

            lib = await get_library()
            for it in lib["items"]:
                if it["id"] == item_id:
                    it["file_path"] = file_path
                    it["size_bytes"] = size_bytes
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

class StreamReq(BaseModel):
    magnet: str
    title: str


class DownloadReq(BaseModel):
    magnet: str
    title: str
    series: str = ""
    season: int = 0
    episode: int = 0


class ProfileReq(BaseModel):
    name: str
    color: str = "indigo"


class ProgressReq(BaseModel):
    profile_id: str
    position_sec: float
    duration_sec: float


class LibraryPlayReq(BaseModel):
    profile_id: str
    seek_to: Optional[float] = None  # explicit seek in seconds; None = resume from saved


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
        prof_prog = it.get("progress", {}).get(profile_id, {}) if profile_id else {}
        items.append({
            "id": it["id"],
            "title": it["title"],
            "series": it.get("series", ""),
            "season": it.get("season", 0),
            "episode": it.get("episode", 0),
            "file_path": it.get("file_path", ""),
            "size_bytes": it.get("size_bytes", 0),
            "size_human": human_size(it.get("size_bytes", 0)),
            "added_at": it.get("added_at", ""),
            "status": it.get("status", "ready"),
            "torrent_hash": it.get("torrent_hash", ""),
            "progress": prof_prog,
        })
    items.sort(key=lambda x: (
        x["series"] or "\xff" + x["title"],
        x["season"],
        x["episode"],
        x["added_at"],
    ))
    return JSONResponse({"items": items})


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
        "file_path": "",
        "size_bytes": 0,
        "added_at": _now_iso(),
        "status": "downloading",
        "torrent_hash": "",
        "progress": {},
    }
    lib["items"].append(item)
    await put_library(lib)
    asyncio.create_task(library_download_pipeline(item["id"], req.magnet))
    return JSONResponse({"ok": True, "item_id": item["id"]})


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


@app.post("/api/library/{item_id}/play")
async def play_library_item(item_id: str, req: LibraryPlayReq) -> JSONResponse:
    lib = await get_library()
    item = next((it for it in lib["items"] if it["id"] == item_id), None)
    if not item:
        raise HTTPException(404, "Item not found.")

    file_path = item.get("file_path", "")
    if not file_path or not Path(file_path).exists():
        raise HTTPException(400, f"File not found on disk: {file_path or '(no path recorded)'}")

    fp = Path(file_path)
    await vlc("in_play", input=fp.resolve().as_uri())
    state.stream_status = "playing"
    state.active_title = item["title"]
    state.active_file = fp
    state.active_hash = item.get("torrent_hash") or None
    state.library_item_id = item_id
    state.library_profile_id = req.profile_id

    # Determine seek target
    saved = item.get("progress", {}).get(req.profile_id, {})
    seek_sec = req.seek_to
    if seek_sec is None:
        pos = saved.get("position_sec", 0)
        dur = saved.get("duration_sec", 0)
        if pos > 5 and dur > 0 and (pos / dur) < 0.92:
            seek_sec = pos

    if seek_sec and seek_sec > 5:
        async def _delayed_seek(s: float) -> None:
            await asyncio.sleep(3)
            await vlc("seek", val=str(int(s)))
        asyncio.create_task(_delayed_seek(seek_sec))

    await broadcast("stream_status", {"status": "playing", "message": f"Playing: {fp.name}"})
    return JSONResponse({"ok": True, "seek_to": seek_sec})


@app.post("/api/library/{item_id}/progress")
async def update_progress(item_id: str, req: ProgressReq) -> JSONResponse:
    lib = await get_library()
    item = next((it for it in lib["items"] if it["id"] == item_id), None)
    if not item:
        raise HTTPException(404, "Item not found.")
    dur = req.duration_sec
    pct = req.position_sec / dur if dur else 0
    item.setdefault("progress", {})[req.profile_id] = {
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


@app.post("/api/stream")
async def stream_now(req: StreamReq) -> JSONResponse:
    if not state.vpn_secure:
        raise HTTPException(403, "VPN not connected — streaming blocked.")

    if state.stream_task and not state.stream_task.done():
        state.stream_task.cancel()
        if state.active_hash and not state.library_item_id:
            await qbit_delete(state.active_hash)

    state.active_hash = None
    state.active_file = None
    state.library_item_id = None
    state.library_profile_id = None
    state.stream_task = asyncio.create_task(stream_pipeline(req.magnet, req.title))
    return JSONResponse({"ok": True})


@app.post("/api/stop")
async def stop() -> JSONResponse:
    if state.stream_task and not state.stream_task.done():
        state.stream_task.cancel()
    # Only auto-delete the torrent for stream-now sessions, not library downloads
    if state.active_hash and not state.library_item_id:
        await qbit_delete(state.active_hash)
    await vlc("pl_stop")

    state.active_hash = None
    state.active_file = None
    state.active_title = None
    state.stream_status = "idle"
    state.library_item_id = None
    state.library_profile_id = None
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


@app.post("/api/vlc/volume/{direction}")
async def volume(direction: str) -> JSONResponse:
    if direction not in ("up", "down"):
        raise HTTPException(400, "direction must be 'up' or 'down'")
    await vlc("volume", val="+26" if direction == "up" else "-26")
    return JSONResponse({"ok": True})


@app.get("/api/state")
async def get_state() -> JSONResponse:
    return JSONResponse(state_snapshot())


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
