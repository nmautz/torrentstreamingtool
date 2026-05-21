# Backend (`main.py`)

3615 lines. Everything server-side lives here except Smart Skip (`analyzer.py`).

## Section map (in source order)

| Lines | Section |
|------:|---------|
| 1–35    | Imports |
| 41–73   | `Settings` (pydantic-settings reading `.env`) |
| 77–134  | Library storage helpers: `_migrate_item`, `get_library`, `put_library`, `_lib_lock` |
| 138–177 | `AppState` dataclass + module globals (`state`, `qbit`, `_admin_sessions`) |
| 179–212 | Jackett session cookie helpers (`_jackett_login`, `_jackett_admin`) |
| 215–260 | SSE broadcast + `state_snapshot()` |
| 263–291 | Admin auth (`_check_admin`, `_require_admin`, `_pin_hash`) |
| 294–373 | qBittorrent client: `qreq`, `qbit_add_magnet`, `qbit_streaming_mode`, `qbit_info`, `qbit_files`, `qbit_delete`, `qbit_set_file_priority` |
| 376–448 | VLC client: `vlc()`, `vlc_status`, `vlc_playlist_uri`, `uri_to_path` |
| 450–552 | OpenSubtitles: `_opensubtitles_hash`, `_current_playback_path`, `_opensubtitles_search` |
| 555–773 | VLC window control (Windows ctypes / macOS osascript / Linux xdotool): focus, fullscreen, minimize. Windows focus path first minimizes all non-VLC top-level windows so the player owns the screen on TV playback. |
| 776–847 | VLC restart + `_retry_task` |
| 793–940 | Utilities: `extract_hash`, `parse_season_episode`, `build_file_list`, `find_resume_hint` |
| 942–992 | Track preference save/apply |
| 995–1032 | `vpn_guard` (3 s `mullvad status` loop) |
| 1035–1058 | `stat_broadcaster` (2 s state push loop) |
| 1060–1107 | `_auto_play_item` (called when a queued item finishes) |
| 1112–1187 | `library_download_monitor` (5 s poll for downloading items) |
| 1190–1347 | Smart Skip orchestration: `_series_key`, `_run_series_analysis`, `_schedule_series_analysis_if_eligible` |
| 1350–1458 | Skip offer detection: `_maybe_emit_skip_offer`, `vlc_next_file` |
| 1461–1552 | `vlc_progress_tracker` (2 s skip detection, 15 s progress save) |
| 1555–1671 | `stream_pipeline` (stream-now buffer loop + handoff to VLC) |
| 1675–1741 | `library_download_pipeline` (add magnet, populate file list) |
| 1746–1781 | FastAPI `lifespan` + HTTPS redirect middleware |
| 1786–1877 | Pydantic request models |
| 1881–1919 | Routes: Profiles CRUD |
| 1922–2267 | Routes: Library — list, files, download, play, progress, mark-watched, queue-play, file-priority, save-to-library |
| 2270–2418 | Routes: Search, `/api/stream/prepare`, `/api/library/prepare` |
| 2421–2482 | Route: `/api/library/upload` (multipart) |
| 2485–2622 | Routes: `/api/stream`, `/api/stop`, `/api/retry` |
| 2624–2796 | Routes: VLC controls — pause, volume, seek, prev/next |
| 2799–2874 | Routes: VLC tracks (audio/subtitle) |
| 2877–2958 | Routes: subtitle search/download (OpenSubtitles) |
| 2961–3041 | Routes: state, library paths, disk space |
| 3044–3136 | Routes: library file download (single file + ZIP stream) |
| 3139–3162 | Route: `/api/events` (SSE) |
| 3167–3336 | Routes: admin panel (login, indexers, settings, library, content lock) |
| 3339–3398 | Routes: profile PINs (set/verify, elevated flag) |
| 3401–3535 | Routes: profile prefs (auto-skip, resume mode) + global max-volume setting + skip-now/resume-now |
| 3538–3611 | Routes: admin Smart Skip (skip-data CRUD, force analyze, analyzer status) |
| 3615    | `app.mount("/", StaticFiles…)` — must be last so API routes win |

## AppState fields ([main.py:138](../main.py#L138))

| Field | What it means |
|-------|---------------|
| `vpn_secure`, `vpn_status_text` | Set by `vpn_guard`; gates `/api/stream` and `/api/library/download` |
| `jackett_ok` | Last known Jackett HTTP reachability; set by `jackett_health_monitor`, published in `state_snapshot()` |
| `active_hash`, `active_title`, `active_file` | The current torrent (stream-now OR library playback) |
| `stream_status` | `idle` \| `buffering` \| `playing` \| `error` |
| `progress`, `downloaded_mb`, `total_mb`, `dl_speed_bps`, `ul_speed_bps` | Live torrent stats from qBit |
| `stream_task` | The asyncio.Task running `stream_pipeline` — cancel on stop/replay |
| `library_play_task` | The asyncio.Task running the VLC handoff for the active library play / prev / next. Cancelled by `/api/stop` and by any subsequent Play / prev / next so slow VLC roundtrips can't race a newer action |
| `library_item_id`, `library_profile_id` | Non-None when active playback is a library item — prevents auto-delete on stop |
| `library_item_file_count`, `library_playlist`, `library_current_file` | Multi-episode playlist state |
| `downloading_count` | Driving the navbar download badge |
| `play_when_ready_*` | "Auto-play when this item (or file) finishes downloading" |
| `current_audio_track`, `current_subtitle_track` | VLC ES IDs — VLC 3.x doesn't expose these in status, so we track them server-side. Reset to -1 on new playback |
| `track_pref_applied_file` | Last file for which saved audio/sub track prefs were applied (avoid double-applying) |
| `vlc_time`, `vlc_duration`, `vlc_volume` | Sampled by `stat_broadcaster` every 2 s |
| `prepare_hash` | Torrent added by `/api/stream/prepare` pending user file selection — also cleaned up by `/api/stop` |
| `skip_offer`, `skip_offer_file` | Current intro/credits skip offer (or `#intro-done` / `#credits-done` marker) |
| `skip_countdown`, `skip_countdown_task` | Active auto-skip countdown `{type, file_path, n}` + its coroutine handle. Drives the on-TV marquee popup; see [ANALYZER.md](ANALYZER.md#auto-skip-countdown-on-tv-marquee) |
| `resume_offer` | When `resume_mode="prompt"`, a `{position_sec, file_path}` dict |
| `analysis_jobs` | `{series_key → {status, stage, current, total, message, …}}` |
| `sse_queues` | One `asyncio.Queue` per connected client |

## Background tasks

Started in `lifespan` ([main.py:1746](../main.py#L1746)); all run forever until app shutdown.

- **`vpn_guard`** (every 3 s) — runs `mullvad status` via `asyncio.create_subprocess_exec`. On disconnect: kills `qbittorrent` via psutil, sets `vpn_secure=False`, broadcasts `vpn_status`. The kill switch is enforced redundantly by `watchdog.py` at the process level.
- **`jackett_health_monitor`** (every 20 s) — `GET {INDEXER_URL}/UI/Login` (any HTTP status = serving, since a hung Jackett keeps the port open but stops answering). Updates `state.jackett_ok` and broadcasts `jackett_status` on transitions. As a backstop, if a *local* Jackett stays unreachable for ~2 min it calls `watchdog.restart_jackett()` via `asyncio.to_thread` — primary recovery is the process watchdog (which acts within seconds), so this only fires when no watchdog is running. See [DAEMON_WATCHDOG.md](DAEMON_WATCHDOG.md#jackett-specifics-watchdogpy160) and [GOTCHAS.md](GOTCHAS.md#port-open-is-not-a-jackett-health-check).
- **`stat_broadcaster`** (every 2 s) — when `stream_status in ("buffering","playing")` polls `qbit_info(active_hash)`. When `playing`, also polls `vlc_status()` for `time`/`length`/`volume`. Always broadcasts `state` with `state_snapshot()`.
- **`library_download_monitor`** (every 5 s) — for each item with `status="downloading"`, polls qBit; flips to `ready` on `uploading`/`stalledUP`/`pausedUP`/`queuedUP`/`forcedUP`, to `error` on `error`/`missingFiles`. Pushes a per-item `library_progress` event with speed/ETA. Also handles `play_when_ready_*`: auto-plays the item (or a specific file once its progress reaches 1.0) when the trigger fires.
- **`vlc_progress_tracker`** (every 2 s skip check; 15 s save) — runs only while `library_item_id` is set. Reads current VLC position, finds the matching `skip_data` entry, calls `_maybe_emit_skip_offer` (auto-skip or set `state.skip_offer`). Every 15 s, also writes `position_sec`/`duration_sec`/`completed` into `library.json` for the active profile and broadcasts `progress_saved`.
- **`background_video_loop`** (every 3 s) — if `settings.background_video` is configured + enabled, `stream_status` is not `buffering`, and VLC reports anything other than `playing`/`paused`, plays the bg file via `_play_background_video()`. Sets `state.background_playing=True`. Naturally replaced by any user `vlc("in_play", …)` — that branch in the `vlc()` helper restores `state.vlc_volume` from `state.user_volume_before_bg` and clears `background_playing`. See [ADMIN.md §5](ADMIN.md) and [LIBRARY_DATA.md](LIBRARY_DATA.md).

## Pipelines

### `stream_pipeline` ([main.py:1557](../main.py#L1557))
1. Set `stream_status="buffering"`, broadcast.
2. Add magnet to qBit (sequential=true at add-time) **unless** a `torrent_hash` was passed from `/api/stream/prepare`.
3. Wait up to 30 s for the torrent to appear in qBit.
4. `qbit_streaming_mode(h)` — belt-and-suspenders sequential check.
5. If `file_index` is set, zero out priority for all other files.
6. Buffer loop (1 s sleep): poll `qbit_info` and `qbit_files`. Push `stream_status` with progress every tick. Break when `BUFFER_MIN_MB` or `BUFFER_MIN_PCT` is crossed.
7. Resolve the video file (`largest_video` or `_file_by_index`), build `file_path` from `info["save_path"]`.
8. `vlc("in_play", input=file_path.resolve().as_uri())`. Spawn `vlc_focus_and_fullscreen` task. Set `stream_status="playing"`.

Cancellation: on new `/api/stream` or `/api/stop`, `state.stream_task.cancel()` is called. If `library_item_id is None`, the torrent is deleted by `qbit_delete(active_hash)`.

### `library_download_pipeline` ([main.py:1675](../main.py#L1675))
- Adds magnet WITHOUT sequential mode and WITHOUT auto-delete.
- Waits for metadata, populates `item["files"]` via `build_file_list(qfiles, save_path)`.
- Honours `selected_file_indices` (skip non-selected files via `qbit_set_file_priority`).
- The item stays `status="downloading"` until `library_download_monitor` flips it.

### `_library_play_launch` / `_vlc_relaunch_playlist`
- Background handoff used by `/api/library/{id}/play` and by `/api/vlc/prev`/`next`. The route handlers update `state` synchronously, broadcast `stream_status="buffering"` + a full `state` event, return 202, then create one of these tasks to run the VLC `in_play` + `in_enqueue` HTTP roundtrips and broadcast `playing` when done. Tracked on `state.library_play_task` so `/api/stop` and a subsequent Play can cancel an in-flight handoff (otherwise a slow VLC roundtrip could keep firing `in_play` after the user has already moved on).
- Why: VLC's HTTP API is slow over flaky links; awaiting it inside the request kept the Play response stalled for seconds and starved the SSE-driven UI of any "I'm doing something" signal. Returning 202 + broadcasting buffering lets the client paint loading state immediately. See [GOTCHAS.md §Slow-network Play](GOTCHAS.md#slow-network-play-must-be-non-blocking).

## Stream-to-Device prep

End of `main.py` (just before `app.mount`) has the prep subsystem that powers
Stream-to-Device. The function names still say `offline_*` for backwards
compatibility, but the live consumer is the browser `<video>` issuing HTTP
Range requests against the cached MP4 — nothing is downloaded to the device
anymore.

- `_ffprobe_codec(path)` shells out to ffprobe (located alongside `analyzer.ffmpeg_bin()`) for video/audio codec + duration.
- `_safari_compatible(info, ext)` — true for `.mp4`/`.m4v`/`.mov` containers with `h264`/`hevc` video and `aac`/`mp3` audio (direct play).
- `_can_remux(info)` — true for compatible codecs in any container (no re-encode, just rewrap to MP4).
- `_offline_jobs: dict[job_id → {src, out, status, operation, progress, error}]` is a process-local job table. Keyed by random `secrets.token_hex(8)`. There is no persistence — restarting the dashboard discards all in-flight jobs (the cached MP4s on disk survive).
- `_run_offline_job(job_id)` runs the actual ffmpeg call. Remux args use `-c copy -bsf:a aac_adtstoasc -movflags +faststart`; transcode args use `libx264 veryfast crf 23` + `aac 160k`. Progress is approximated by `tmp.stat().st_size / src.stat().st_size` because consuming `-progress` is awkward.
- `OFFLINE_CACHE = repo/.offline_cache/` — sha256-keyed MP4s. The cache key is `sha256(path | mtime | size)[:24]`, so a re-encoded source invalidates the cache entry. There is **no automatic eviction**; users can clean it manually.
- Sidecar subtitles: `_list_sidecar_subs(src, item_id)` finds `.srt`/`.vtt` whose stem matches the video stem (or `<stem>.<lang>`). The subtitle endpoint `/api/library/{id}/subtitle` converts SRT → VTT inline via `_srt_to_vtt`.

The fast-path of `/offline-prepare` returns the existing `/api/library/{id}/download` URL — no new file is created. Only when remux/transcode is needed do we hit `OFFLINE_CACHE`.

## qBittorrent client notes

- **Auth**: `qbit_login` calls `/api/v2/auth/login`. Because `setup.py` writes `WebUI\LocalHostAuth=false` to qBit's ini, localhost requests don't actually need a cookie — but `qreq` still retries on 403.
- **`qbit_streaming_mode`** uses `toggleSequentialDownload` (the only endpoint that exists; there is no `setSequentialDownload`). It's a toggle, so we read `seq_dl` first and only call it when sequential is currently off. Sequential is already set at add-time via the `/torrents/add` form.
- **DO NOT call `toggleFirstLastPiecePrio`** — that fetches the last piece early, which defeats piece-order streaming. We deliberately do not enable it.

## VLC client notes

- All calls go to `<vlc_url>/requests/status.xml` (or `status.json`, `playlist.json`).
- `vlc("in_play", input=uri)` is the play command. `in_enqueue` adds to playlist.
- Volume scale: VLC uses 0–512 (256 = 100 %). Our API uses 0–200 (100 = normal). Conversion: `raw = volume / 100 * 256`.
- Pre-roll volume: when sending `in_play`, we send `volume` first so VLC's default doesn't blast for half a second.
- VLC 3.x quirk: `audiotrack` / `subtitletrack` are not in the XML response. We track them ourselves in `state.current_audio_track` / `current_subtitle_track`, reset on each new playback.

## See also

- [API.md](API.md) — every endpoint with method, path, request shape, response
- [GOTCHAS.md](GOTCHAS.md) — VLC ES ID quirks, sequential-download trap, etc.
- [ANALYZER.md](ANALYZER.md) — Smart Skip details
