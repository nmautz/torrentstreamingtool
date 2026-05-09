"""P2P StreamLink v2.0 — FastAPI backend"""

import asyncio
import base64
import json
import platform
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
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


async def qbit_add_magnet(magnet: str) -> Optional[str]:
    h = extract_hash(magnet)
    r = await qreq(
        "POST", "/api/v2/torrents/add",
        data={"urls": magnet, "savepath": settings.qbit_download_path},
    )
    return h if (r and r.text.strip() == "Ok.") else None


async def qbit_streaming_mode(h: str) -> None:
    await qreq("POST", "/api/v2/torrents/setSequentialDownload",
                data={"hashes": h, "enable": "true"})
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


async def qbit_delete(h: str) -> None:
    await qreq("POST", "/api/v2/torrents/delete",
                data={"hashes": h, "deleteFiles": "true"})


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
        pass  # VLC may not be open yet; non-fatal


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


# ── Stream Pipeline ───────────────────────────────────────────────────────────

async def stream_pipeline(magnet: str, title: str) -> None:
    """
    Full stream-now pipeline.  Runs as a detached asyncio Task so it never
    blocks the FastAPI event loop.  All progress is pushed via SSE.
    """
    try:
        state.active_title = title
        state.stream_status = "buffering"
        await broadcast("stream_status", {"status": "buffering", "message": "Adding torrent to qBittorrent…"})

        # ① Add magnet
        h = await qbit_add_magnet(magnet)
        if not h:
            raise RuntimeError("qBittorrent rejected the magnet (is it running on port 8081?)")
        state.active_hash = h

        # ② Wait up to 30 s for torrent to appear in qBit's list
        for _ in range(30):
            await asyncio.sleep(1)
            if await qbit_info(h):
                break
        else:
            raise RuntimeError("Torrent did not appear in qBittorrent after 30 s.")

        # ③ Force streaming download order
        await qbit_streaming_mode(h)
        await broadcast("stream_status", {"status": "buffering", "message": "Sequential mode set. Buffering first pieces…"})

        # ④ Dynamic buffer check — asyncio.sleep is non-blocking
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

        # ⑤ Locate the largest video file in the torrent
        files = await qbit_files(h)
        vid = largest_video(files)
        if not vid:
            raise RuntimeError("No recognisable video file found in torrent.")

        info = await qbit_info(h)
        save_path = (info or {}).get("save_path", settings.qbit_download_path)
        # vid["name"] may include a sub-directory for multi-file torrents
        file_path = Path(save_path) / vid["name"]
        state.active_file = file_path

        # ⑥ Hand off to VLC — pathlib.as_uri() handles Windows C:\ and Unix /
        await vlc("in_play", input=file_path.resolve().as_uri())
        state.stream_status = "playing"
        await broadcast("stream_status", {
            "status": "playing",
            "message": f"Playing: {file_path.name}",
        })

    except asyncio.CancelledError:
        pass
    except Exception as e:
        state.stream_status = "error"
        await broadcast("stream_status", {"status": "error", "message": str(e)})


# ── FastAPI App ───────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator:
    global qbit
    qbit = httpx.AsyncClient(timeout=10.0)
    await qbit_login()
    Path(settings.qbit_download_path).mkdir(parents=True, exist_ok=True)

    guard = asyncio.create_task(vpn_guard())
    broadcaster = asyncio.create_task(stat_broadcaster())

    yield

    guard.cancel()
    broadcaster.cancel()
    if state.stream_task and not state.stream_task.done():
        state.stream_task.cancel()
    await qbit.aclose()


app = FastAPI(title="P2P StreamLink", version="2.0", lifespan=lifespan)


# ── Request Models ────────────────────────────────────────────────────────────

class StreamReq(BaseModel):
    magnet: str
    title: str


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/api/search")
async def search(q: str, limit: int = 30) -> JSONResponse:
    if not q.strip():
        return JSONResponse({"results": []})

    try:
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.get(
                f"{settings.indexer_url}/api/v2.0/indexers/all/results",
                params={
                    "apikey": settings.indexer_api_key,
                    "Query": q,
                    "Category[]": settings.indexer_categories,
                },
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
        if state.active_hash:
            await qbit_delete(state.active_hash)

    state.active_hash = None
    state.active_file = None
    state.stream_task = asyncio.create_task(stream_pipeline(req.magnet, req.title))
    return JSONResponse({"ok": True})


@app.post("/api/stop")
async def stop() -> JSONResponse:
    if state.stream_task and not state.stream_task.done():
        state.stream_task.cancel()
    if state.active_hash:
        await qbit_delete(state.active_hash)
    await vlc("pl_stop")

    state.active_hash = None
    state.active_file = None
    state.active_title = None
    state.stream_status = "idle"
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
    # VLC volume range 0–512 where 256 = 100%.  +26/-26 ≈ 10% steps.
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
