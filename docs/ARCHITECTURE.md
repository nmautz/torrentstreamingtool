# Architecture

High-level system overview. Read this first if you're new to the project.

## What it is

P2P StreamLink — a local web dashboard that searches Jackett indexers, buffers magnet links through qBittorrent, and streams the file into VLC. Mullvad VPN is enforced as a kill-switch. There is also a persistent **library** (downloaded items kept around), per-profile watch history, intro/credits Smart Skip, subtitle download, and an admin panel.

## Service topology

```
Browser  ─SSE/HTTP→  FastAPI (main.py, port 80)
                        ├── httpx → qBittorrent Web UI (port 8081)
                        ├── httpx → VLC Lua HTTP        (port 8080)
                        └── httpx → Jackett             (port 9117, can be remote)
            ↑
            └── mDNS:  remote.local (advertised on LAN)
            └── HTTPS: cert.pem / key.pem  (port 443, admin panel uses this)
```

All four services (VLC, qBittorrent, Jackett, dashboard) run on the same host except Jackett, which `run.py` can talk to remotely (`INDEXER_URL` parsed for hostname). VLC, qBittorrent, and Mullvad must always be local.

## Process model

- **One Python process** runs FastAPI/uvicorn (the dashboard). All state is in-memory in `AppState` ([main.py:138](../main.py#L138)).
- **External processes**: VLC, qBittorrent, Jackett — launched by `run.py` and supervised by `watchdog.py`. They keep running after the dashboard is stopped.
- **VPN guard task** runs `mullvad status` every 3 s inside the dashboard process and kills `qbittorrent` via psutil on VPN drop.
- **Persistence**: a single `library.json` file at the repo root. No database.

## Code map (where things live)

| File | Lines | Purpose |
|------|-------|---------|
| `main.py` | 3600 | FastAPI app, all routes, background tasks, AppState, qBit/VLC clients |
| `run.py` | 880 | Launcher — finds and starts VLC/qBit/Jackett, then uvicorn |
| `setup.py` | 1145 | First-time configurator — venv, deps, qBit ini, .env, SSL certs |
| `daemon.py` | 545 | launchd / systemd / Task Scheduler service installer |
| `watchdog.py` | 519 | Background thread (or standalone process) that restarts crashed deps |
| `analyzer.py` | 540 | Smart Skip — chromaprint fingerprinting + ffmpeg blackdetect |
| `stt.py` | — | AI subtitles — whisper.cpp wrapper (audio extract → transcribe/translate). See [STT.md](STT.md) |
| `static/index.html` | 3608 | Main UI — vanilla JS, Tailwind CDN, SSE-driven |
| `static/admin.html` | 990 | Admin panel — indexer management, content lock, Smart Skip editor |
| `static/tv.html` | — | YouTube-on-TV kiosk page (host display): IFrame Player API + `yt_command` SSE listener + state heartbeat. See [YOUTUBE.md](YOUTUBE.md) |
| `library.json` | — | Persisted state: profiles, library items, watch progress, skip data |
| `.env` | — | Settings + auto-detected binary paths (`_VLC_BIN`, `_QBIT_BIN`, etc.) |

## Lifecycle

1. **`setup.py`** runs once with system Python. Creates `.venv`, installs deps, detects/installs VLC/qBittorrent/Jackett/Mullvad and ffmpeg/fpcalc, writes `qBittorrent.ini` directly, generates self-signed cert for HTTPS admin, writes `.env`. Optionally registers a system service via `daemon.py`.
2. **`run.py`** runs with system Python, immediately `os.execv`s into `.venv/bin/python`. Starts VLC (with `--extraintf=http`), qBittorrent, Jackett (local only), checks Mullvad. Starts `watchdog` thread. Launches two uvicorn servers in the same `asyncio.run()` event loop: port 80 serves the canonical `main:app`, port 443 (if certs exist) serves `https_proxy:app` — a thin reverse proxy that forwards every request to `127.0.0.1:80`. Single AppState regardless of port/host. Registers `remote.local` via zeroconf.
3. **`main.py` lifespan** opens an httpx client to qBittorrent, logs in, then launches four background tasks: `vpn_guard`, `stat_broadcaster`, `library_download_monitor`, `vlc_progress_tracker`.
4. **Browser connects** → `EventSource('/api/events')` opens. Backend immediately yields a `state` snapshot, then pushes events as state changes.

## Data flow examples

### Stream-now (Search tab → Play)
1. Browser POST `/api/stream` `{magnet, title}` → returns 202 immediately.
2. `stream_pipeline` task: adds magnet to qBit with `sequentialDownload=true`, waits for torrent metadata (up to 30 s), polls every 1 s until `BUFFER_MIN_MB` or `BUFFER_MIN_PCT`, resolves the largest video file path, sends `in_play` to VLC's Lua HTTP, optionally fullscreens VLC.
3. `stat_broadcaster` keeps pushing `state` snapshots every 2 s with download progress and VLC playback position.
4. On `/api/stop` (or new Play): cancel the task, delete torrent + files via qBit, `pl_stop` VLC.

### YouTube on TV (Search tab → Play on TV)
1. Browser POST `/api/youtube` `{url}` → 202. Backend extracts the video id, sets `youtube_active`, stops VLC, broadcasts `yt_command:load`, and launches a fullscreen Chrome kiosk at `/tv?v=<id>` (or hot-swaps if the page is already open).
2. `static/tv.html` plays the video via the YouTube IFrame API and reports position/title/volume back via `POST /api/youtube/tv-state` every 1 s; the backend mirrors those onto the reused `vlc_*` display fields and rebroadcasts `state`.
3. The dashboard's existing footer/fullscreen controls route to `POST /api/youtube/control` (→ SSE `yt_command` → IFrame API) whenever `app.youtube_active`.
4. `/api/stop` broadcasts `yt_command:close` and hard-kills the kiosk. Full detail in [YOUTUBE.md](YOUTUBE.md).

### Library download → Play
1. Browser POST `/api/library/download` → creates an item in `library.json` with `status="downloading"`, kicks off `library_download_pipeline`.
2. Pipeline adds magnet (NO sequential mode), waits for metadata, populates `item["files"]`. Does NOT auto-delete on stop.
3. `library_download_monitor` polls every 5 s; flips `status="ready"` when qBit reports `uploading`/`stalledUP`/etc., triggers Smart Skip analysis if eligible.
4. Browser POST `/api/library/{id}/play` → resolves resume position, sends `in_play` + `in_enqueue` to VLC, sets `state.library_item_id`/`library_profile_id`.
5. `vlc_progress_tracker` polls VLC every 2 s for skip-offer logic, saves progress every 15 s.

## Key invariants

- **VPN gating**: `/api/stream` and `/api/library/download` return 403 if `state.vpn_secure` is False. `watchdog.py` enforces the same at the process level: qBit is killed on VPN drop and not restarted until VPN reconnects.
- **No sequential download for library items**: only stream-now uses sequential. Library items download normally so all files arrive complete.
- **Track IDs are VLC ES IDs**, not sequential 1/2/3 counters. See [GOTCHAS.md](GOTCHAS.md).
- **`state.library_item_id is not None`** means the active playback is a library item — `/api/stop` will NOT delete the torrent. See [main.py:2580](../main.py#L2580) (`stop` handler).

## See also

- [BACKEND.md](BACKEND.md) — main.py internals
- [FRONTEND.md](FRONTEND.md) — index.html / admin.html structure
- [API.md](API.md) — full endpoint reference
- [LIBRARY_DATA.md](LIBRARY_DATA.md) — library.json schema
- [SETUP.md](SETUP.md) — first-time configuration
- [RUNTIME.md](RUNTIME.md) — `run.py` launcher details
- [DAEMON_WATCHDOG.md](DAEMON_WATCHDOG.md) — service install and process supervision
- [ANALYZER.md](ANALYZER.md) — Smart Skip
- [GOTCHAS.md](GOTCHAS.md) — non-obvious behaviours / footguns
