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

**Jackett category search** — the `Category[]` query parameter is omitted entirely when `INDEXER_CATEGORIES` is `"0"`. Passing `Category[]=0` to Jackett returns zero results because Jackett treats `0` as an unknown category ID rather than "all". The correct behavior for "all categories" is to send no `Category[]` parameter at all.

### `setup.py` — one-time configurator

Runs under system Python (no venv). Steps in order:
1. Creates `.venv`, installs `requirements.txt`
2. Detects VLC / qBittorrent / Mullvad / Jackett at platform-specific default paths; saves found paths to `.env` as `_VLC_BIN`, `_QBIT_BIN`, etc.
3. Interactive prompts (Enter = accept default) for all service URLs and passwords
4. Merges settings into `qBittorrent.ini` directly (parses existing ini, injects `Preferences` and `BitTorrent` keys, writes back preserving other sections)
5. Writes `.env`; creates the download directory

### `run.py` — launcher

Runs under system Python but immediately `os.execv`s itself into `.venv/bin/python` so psutil and other venv packages are available for the rest of execution.

**venv detection** uses `Path(sys.prefix).resolve() != VENV.resolve()` — **not** a comparison of executable paths. On Homebrew/pyenv macOS, both `python3` and `.venv/bin/python` can resolve through symlinks to the same underlying binary, making path comparison always equal and exec never firing. `sys.prefix` is always set to the venv root when running inside a venv, so it's the correct signal.

**psutil pre-flight check**: after the venv relaunch, `run.py` imports `psutil` immediately and exits with a clear message if it's missing. This catches the case where the venv was created but `pip install` never completed (e.g. because `setup.py` crashed earlier).

Service start order: VLC → qBittorrent → Jackett → Mullvad check → uvicorn.

**Jackett local vs remote**: parses `INDEXER_URL` hostname. If it's `localhost`/`127.0.0.1`/`::1`, tries to find and launch a local binary. If it's any other host, only checks reachability via TCP — never attempts a local launch.

**VLC**: always launched with `--extraintf=http` flags rather than editing VLC's preferences file. If VLC is already running without HTTP, `run.py` kills it and restarts.

`port_open(port, host="127.0.0.1")` and `wait_for_port(port, timeout, label, host="127.0.0.1")` take an explicit host so remote Jackett checks use the correct address.

### Library system

Persistent storage in `library.json` at the project root (created automatically). Structure: `{"profiles": [...], "items": [...]}`. Accessed via `asyncio.Lock` — never read/write raw from multiple coroutines.

**Profiles** — up to 6, no passwords, Netflix-style picker on first load. Profile ID stored in `localStorage`. ID is passed as a query param or request body field on every library API call; the server is stateless w.r.t. which profile is "active".

**Library items** — added via `/api/library/download`. Download runs in a background `asyncio.Task` (`library_download_pipeline`) which adds the magnet to qBit, waits for the torrent to appear, then resolves the file path. `library_download_monitor` polls qBit every 10 s and marks items `ready` when qBit state transitions to `uploading`/`stalledUP`/etc. Items are never auto-deleted on stop — only on explicit DELETE request.

**Watch progress** — `vlc_progress_tracker` polls VLC's `/requests/status.json` every 15 s while `state.library_item_id` is set, saving `{position_sec, duration_sec, completed}` per profile per item. Completed threshold is >92%. Resume via `/api/library/{id}/play` with `seek_to=null` — backend reads saved position and issues a VLC `seek` command 3 s after playback starts (to let VLC open the file first).

**Sequential download** — `qbit_streaming_mode` calls only `setSequentialDownload` (pieces arrive in order 0 → N). First/last piece priority (`toggleFirstLastPiecePrio`) is a separate, independent flag and is explicitly **not** set — it causes qBittorrent to fetch out-of-order chunks early, which breaks piece-order streaming.

**VLC track IDs** — `GET /api/vlc/tracks` must use the actual ES (elementary stream) ID as each track's `id` — the number N extracted from the `"Stream N"` key in VLC's JSON status. VLC's `audio_track` and `subtitle_track` commands accept ES IDs, and the `<audiotrack>`/`<subtitletrack>` values in the XML status are also ES IDs. Using sequential per-type counters (1, 2, 3…) instead will send the wrong ID, causing commands to silently do nothing and the "current" highlight in the dropdown to never match.

### Frontend (`static/index.html`)

Vanilla JS, Tailwind CDN, no build step. A single `EventSource('/api/events')` drives all real-time UI. SSE event types:
- `state` — full snapshot pushed every 2 s by `stat_broadcaster`
- `vpn_status` — pushed immediately on VPN connect/disconnect
- `stream_status` — pushed by the stream pipeline at each phase transition
- `library_update` — pushed when a download status changes; triggers library refresh in the UI
- `progress_saved` — pushed every 15 s while a library item is playing

**Two tabs**: Search (stream-now, with a Download button that opens a metadata modal) and Library (shows items grouped by series, with Resume/Play/Delete actions and per-profile watch progress bars).

**Profile picker**: full-screen overlay shown on first load (or when no valid profile in localStorage). Profiles stored in `library.json` on the server; selected profile ID stored in `localStorage.streamlink_profile`.

The VPN-disconnected overlay is a fixed full-screen div toggled by CSS `hidden` class. Play buttons disable optimistically on click and re-enable if the API call fails.
