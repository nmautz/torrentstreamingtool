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
sudo .venv/bin/uvicorn main:app --host 0.0.0.0 --port 80 --reload
```

`setup.py` and `run.py` must be run with the **system** Python (not the venv) — they include `from __future__ import annotations` for Python 3.9 compatibility. `main.py` runs inside the venv and can use 3.10+ syntax freely.

## Architecture

### Service topology

```
Browser ←SSE/HTTP→ FastAPI (main.py :80, mDNS: remote.local)
                       ├── httpx → qBittorrent Web UI (:8081)
                       ├── httpx → VLC Lua HTTP (:8080)
                       └── httpx → Jackett (:9117, may be remote)
```

VLC, qBittorrent, and the dashboard all run on the same machine. Jackett can be remote — `INDEXER_URL` accepts any `http://host:port`.

### `main.py` internals

**Settings** — `pydantic-settings` reads `.env` into a `Settings` singleton. All service URLs, credentials, and buffer thresholds come from here.

**AppState** — a module-level `@dataclass` holding all mutable runtime state: VPN status, active torrent hash, stream pipeline status, progress figures, VLC playback position (`vlc_time`/`vlc_duration` in seconds), track selection, and the list of SSE queues. There is no database; state is in-memory only.

**SSE broadcast** — each `/api/events` connection gets its own `asyncio.Queue`. `broadcast(event, data)` fans out to all queues. The background tasks (`vpn_guard`, `stat_broadcaster`, `vlc_progress_tracker`) push events this way; the stream pipeline task does too.

**`stat_broadcaster`** — runs every 2 s. When `stream_status` is `buffering` or `playing`, polls qBit for download progress. Additionally, when `stream_status == "playing"`, calls `vlc_status()` to update `state.vlc_time`/`state.vlc_duration` (VLC playback position). These are included in every `state` snapshot and used by the frontend seek bar.

**Stream pipeline** — `/api/stream` immediately returns 202 and creates an `asyncio.Task` stored in `state.stream_task`. The task:
1. Adds the magnet to qBittorrent with `sequentialDownload=true` at add-time
2. Waits up to 30 s for the torrent to appear
3. Calls `qbit_streaming_mode()` to verify sequential is on (belt-and-suspenders)
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

**VLC 3.x current track limitation** — VLC 3.x does not include `<audiotrack>` or `<subtitletrack>` in its XML status response. Current track selection is tracked server-side in `state.current_audio_track` / `state.current_subtitle_track` (set by the `POST /api/vlc/track/*` endpoints, reset to -1 on new playback).

**Absolute seek** — `POST /api/vlc/seek/to?position_pct=N` sends `val=N%` to VLC's `seek` command (percentage-based absolute position). The relative-seek endpoint `POST /api/vlc/seek?delta=N` uses `val=+Ns` / `val=-Ns` format. Do not confuse the two — VLC treats `val=N` (no suffix) as a fraction (0.0–1.0), not seconds.

### Smart Skip (`analyzer.py`)

Audio-fingerprint-driven intro/credits detection for library items. Requires `ffmpeg` and `fpcalc` (chromaprint) on the host; `setup.py` detects both and prints a platform-specific install hint if missing — does NOT auto-install. `pyacoustid` is a pip dependency, but the actual fingerprinting calls shell out to `fpcalc -raw` for speed. If either binary is missing, `analyzer.is_available()` returns False and the feature degrades to manual entry only (the admin editor still works).

**Series grouping** — `_series_key(item)` returns `series:<lowercased series field>` when set, otherwise `item:<id>` (movies and one-offs are their own bucket). Cross-episode matching only works within a non-empty series bucket; single-item buckets get the credits fallback only.

**Algorithm** — chromaprint emits ~7.8 32-bit hash frames per second. The intro detector slices the first 6 min of each episode; the outro detector the last 10 min. For each pair (anchor, other), `_find_longest_match` does a sliding alignment within ±half-the-shorter-fingerprint and counts consecutive frames with Hamming distance ≤ 6 bits. The longest run ≥ `MIN_MATCH_FRAMES` (~15 s) per pair is kept. `_intersect_match` takes the intersection across all pairs anchored on episode 0 — that's the canonical intro/outro range. Per-non-anchor episodes use the pairwise match position in their own fingerprint, so cold opens of varying length still align correctly.

**Credits fallback chain** — when no repeated outro fingerprint is found: (1) ffmpeg `blackdetect` between 85% and end-of-runtime, take the first ≥0.5 s black segment as the credits start; (2) if no usable black frame, default `credits_start = duration × 0.92` (matches the existing completion threshold so progress and credits agree).

**Trigger** — `library_download_monitor` calls `_schedule_series_analysis_if_eligible` whenever an item flips to `ready`. It only fires if at least one file in the series bucket still lacks `skip_data` — prevents infinite re-runs. Per-series `asyncio.Lock` in `analyzer.lock_for_series` serializes analyses for the same series; different series run in parallel. Admins force re-runs via `POST /api/admin/library/{item_id}/analyze`.

**Storage** — under each item: `skip_data: { <file_path>: { intro: {start, end}|null, credits_start: N|null, analysis: {version, source} } }`. `source` is `auto`, `auto-blackframe`, `auto-fallback`, or `manual`. Per-profile prefs `auto_skip_intro` and `auto_skip_credits` live on the profile object (defaults False).

**Runtime detection** — `vlc_progress_tracker` runs the skip-window check every 2 s; the progress-save half of the same task still runs every 15 s, guarded by a timestamp. When VLC position enters `[intro.start - 2s, intro.end]` or crosses `credits_start - 2s`, `_maybe_emit_skip_offer` either auto-executes the skip (if the profile pref is on) or sets `state.skip_offer = {type, end_at?, file_path, has_next, next_file_path}` and broadcasts a fresh state snapshot. Reconnecting mobile clients see the offer immediately because it's part of every `state` SSE event.

**Skip execution** — `POST /api/skip-now {type: "intro"|"credits"}` calls VLC seek for intro, or `vlc_next_file` for credits (or `pl_stop` if no next episode exists). After executing, `state.skip_offer_file` gets a `#intro-done` / `#credits-done` suffix so the same offer doesn't re-emit on the next tick. `DELETE /api/skip-now` dismisses without acting, also setting the done marker.

**Frontend** — fixed-position amber skip tile at the bottom of the viewport, ≥64 px tall, full-width. Positioned via `bottom: env(safe-area-inset-bottom) + offset` so it respects iOS home indicator. Renders whenever `state.skip_offer` is non-null, both in normal and fullscreen mode. The profile-settings modal (gear icon next to nav avatar) toggles per-profile auto-skip prefs.

**Admin panel** — `Smart Skip` tab in `/admin` lists library items with `Analyze` (runs `POST /api/admin/library/{id}/analyze`) and `Edit` buttons. Edit opens an inline per-file editor with three numeric inputs (intro start, intro end, credits start) — values in seconds, blank to clear. Manual edits record `analysis.source = "manual"`.

### Frontend (`static/index.html`)

Vanilla JS, Tailwind CDN, no build step. **The project follows the Metro UI design language (flat tiles, typography-focused, high contrast) throughout** A single `EventSource('/api/events')` drives all real-time UI. SSE event types:
- `state` — full snapshot pushed every 2 s by `stat_broadcaster`
- `vpn_status` — pushed immediately on VPN connect/disconnect
- `stream_status` — pushed by the stream pipeline at each phase transition
- `library_update` — pushed when a download status changes; triggers library refresh in the UI
- `progress_saved` — pushed every 15 s while a library item is playing

**Two tabs**: Search (stream-now, with a Download button that opens a metadata modal) and Library (shows items grouped by series, with Resume/Play/Delete actions and per-profile watch progress bars).

**Profile picker**: full-screen overlay shown on first load (or when no valid profile in localStorage). Profiles stored in `library.json` on the server; selected profile ID stored in `localStorage.streamlink_profile`.

**Alert / toast** — `showAlert(msg, type)` writes to two places simultaneously: `#alert` (an inline div inside `#searchTab`) and `#globalToast` (a fixed-position element below the navbar, always visible regardless of active tab). This means errors from library playback are visible even when the Search tab is hidden. `hideAlert()` clears both. Auto-dismiss fires after 4 s (non-error) or 7 s (error).

**Seek bar** — the player footer seek bar (`#seekBarWrapper`) responds to click/tap. When `stream_status === "playing"` and `vlc_duration > 0`, the bar shows VLC playback position (from `vlc_time`/`vlc_duration` in the state snapshot) and clicking calls `POST /api/vlc/seek/to`. Otherwise it shows download progress from `state.progress`. `renderPlayer()` handles both modes.

**Episode picker** — each row has a per-episode ▶ button (calls `epPlayFrom(filePath)`) that selects that episode and all following ones in order, then plays immediately without needing to use the "Play Selected" button. `epPlayFrom` slices `epFiles` from the tapped index forward, respects resume position on the first file, and closes the modal.

The VPN-disconnected overlay is a fixed full-screen div toggled by CSS `hidden` class. Play buttons disable optimistically on click and re-enable if the API call fails.

**`run.py` startup output** — binds uvicorn to `0.0.0.0` (not `127.0.0.1`) so mobile devices on the same LAN can connect. On startup, prints both the localhost URL and the LAN URL with the current Wi-Fi SSID (detected via `airport -I` on macOS, with `networksetup` fallback).
