# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# First-time setup (creates .venv, installs deps, writes qBit ini, generates .env)
python3 setup.py

# Start all services + dashboard (auto-relaunches inside .venv)
python3 run.py

# Shortcuts
make setup
make run

# Run the dashboard directly (requires .venv active and services already running)
.venv/bin/uvicorn main:app --host 127.0.0.1 --port 7000 --reload
```

`setup.py` and `run.py` must be run with the **system** Python (not the venv) — they include `from __future__ import annotations` for Python 3.9 compatibility. `main.py` runs inside the venv and can use 3.10+ syntax freely.

## Architecture

### Service topology

```
Browser ←SSE/HTTP→ FastAPI (main.py :7000)
                       ├── httpx → qBittorrent Web UI (:8081)
                       ├── httpx → VLC Lua HTTP (:8080)
                       └── httpx → Jackett (:9117, may be remote)
```

VLC, qBittorrent, and the dashboard all run on the same machine. Jackett can be remote — `INDEXER_URL` accepts any `http://host:port`.

### `main.py` internals

**Settings** — `pydantic-settings` reads `.env` into a `Settings` singleton. All service URLs, credentials, and buffer thresholds come from here.

**AppState** — a module-level `@dataclass` holding all mutable runtime state: VPN status, active torrent hash, stream pipeline status, progress figures, and the list of SSE queues. There is no database; state is in-memory only.

**SSE broadcast** — each `/api/events` connection gets its own `asyncio.Queue`. `broadcast(event, data)` fans out to all queues. The two background tasks (`vpn_guard`, `stat_broadcaster`) push events this way; the stream pipeline task does too.

**Stream pipeline** — `/api/stream` immediately returns 202 and creates an `asyncio.Task` stored in `state.stream_task`. The task:
1. Adds the magnet to qBittorrent
2. Waits up to 30 s for the torrent to appear
3. Sets sequential download + first/last piece priority
4. Polls `qbit_info()` with `asyncio.sleep(1)` (never `time.sleep`) until `BUFFER_MIN_MB` or `BUFFER_MIN_PCT` is crossed
5. Resolves the video file path with `Path.as_uri()` (handles Windows `file:///C:/…` and macOS `file:///…` automatically)
6. Sends `in_play` to VLC's HTTP interface

Cancelling the task (on `/api/stop` or new Play) also triggers qBit deletion.

**VPN guard** — `asyncio.create_subprocess_exec("mullvad", "status")` runs every 3 s. On disconnect → kills qbittorrent process via `psutil`, sets `state.vpn_secure = False`, broadcasts `vpn_status` event. `/api/stream` returns 403 while `vpn_secure` is False.

**qBittorrent auth** — `setup.py` writes `WebUI\LocalHostAuth=false` to the qBit ini, so localhost requests never need a session cookie. `qreq()` still calls login on startup and retries on 403 for safety.

### `setup.py` — one-time configurator

Runs under system Python (no venv). Steps in order:
1. Creates `.venv`, installs `requirements.txt`
2. Detects VLC / qBittorrent / Mullvad / Jackett at platform-specific default paths; saves found paths to `.env` as `_VLC_BIN`, `_QBIT_BIN`, etc.
3. Interactive prompts (Enter = accept default) for all service URLs and passwords
4. Merges settings into `qBittorrent.ini` directly (parses existing ini, injects `Preferences` and `BitTorrent` keys, writes back preserving other sections)
5. Writes `.env`; creates the download directory

### `run.py` — launcher

Runs under system Python but immediately `os.execv`s itself into `.venv/bin/python` so psutil and other venv packages are available for the rest of execution.

Service start order: VLC → qBittorrent → Jackett → Mullvad check → uvicorn.

**Jackett local vs remote**: parses `INDEXER_URL` hostname. If it's `localhost`/`127.0.0.1`/`::1`, tries to find and launch a local binary. If it's any other host, only checks reachability via TCP — never attempts a local launch.

**VLC**: always launched with `--extraintf=http` flags rather than editing VLC's preferences file. If VLC is already running without HTTP, `run.py` kills it and restarts.

`port_open(port, host="127.0.0.1")` and `wait_for_port(port, timeout, label, host="127.0.0.1")` take an explicit host so remote Jackett checks use the correct address.

### Frontend (`static/index.html`)

Vanilla JS, Tailwind CDN, no build step. A single `EventSource('/api/events')` drives all real-time UI. Three event types:
- `state` — full snapshot pushed every 2 s by `stat_broadcaster`
- `vpn_status` — pushed immediately on VPN connect/disconnect
- `stream_status` — pushed by the stream pipeline at each phase transition

The VPN-disconnected overlay is a fixed full-screen div toggled by CSS `hidden` class. Play buttons disable optimistically on click and re-enable if the API call fails.
