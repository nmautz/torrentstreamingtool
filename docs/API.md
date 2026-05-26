# API Reference

All endpoints are defined in `main.py`. SSE event stream is `/api/events`.

## Conventions

- Request bodies are JSON unless marked `multipart` (file upload) or `query` (querystring only).
- Admin endpoints require either `Authorization: Bearer <token>` (from `POST /api/admin/login`), an `X-Admin-Token` header, or `?admin_token=...` querystring. Token TTL is 24 h.
- Profile-scoped endpoints take a `profile_id` field (UUID from `/api/profiles`).
- `state` snapshots come back on `/api/events` every 2 s — clients generally don't need to poll `/api/state`.

## Server-Sent Events

`GET /api/events` — opens an SSE stream. Initial payload is a `state` event. Heartbeat colon-comment every 20 s.

Event types:
| Event | When | Payload shape |
|-------|------|---------------|
| `state` | every 2 s; on any state change | full `state_snapshot()` ([main.py:292](../main.py#L292)). Includes `library_item_id` and the full ordered `library_playlist` (used by the TV→device Handoff to reconstruct the remaining tail), alongside `library_current_file` / `library_current_index` / `is_library_playback`, and `jackett_ok` (last known indexer HTTP reachability) |
| `vpn_status` | VPN connect/disconnect transition | `{secure, status}` |
| `jackett_status` | Jackett HTTP reachability transition (from `jackett_health_monitor`, ~20 s poll) | `{ok, url}` |
| `stream_status` | stream pipeline phase transition | `{status, message, progress?, downloaded_mb?, total_mb?, dl_speed_bps?, ul_speed_bps?}` |
| `library_progress` | per-download stats, ~every 5 s while downloading | `{item_id, speed_bps, downloaded_bytes, total_bytes, progress_pct, eta_secs}` |
| `library_update` | library item status changed | `{item_id, status, message?}` |
| `progress_saved` | every 15 s while a library item is playing | `{item_id, profile_id, file_path, episode_name, position_sec, duration_sec, pct}` |
| `analysis_status` | Smart Skip job progress | `{series_key, job: {status, stage, current, total, message, episode_name?, …}}` |
| `yt_command` | YouTube-on-TV: a playback command for the `/tv` kiosk page | `{action, value?, video_id?}` — `action ∈ load\|play\|pause\|playpause\|seek\|seek_to\|volume_set\|volume_step\|close`. Broadcast to all SSE clients; only `static/tv.html` acts on it. See [YOUTUBE.md](YOUTUBE.md) |

## State / Version

| Method | Path | Notes |
|--------|------|-------|
| GET | `/api/state` | Current full snapshot |
| GET | `/api/version` | `{"version": "<semver>"}` — always `no-cache`. The UI fetches this on load and force-reloads with `?_v=<ver>` if the cached page is older (see `UI_VERSION` in `main.py` and the `[data-ui-version]` badge in `index.html`) |

## Search

| Method | Path | Notes |
|--------|------|-------|
| GET | `/api/search?q=…&limit=30` | Calls Jackett `/api/v2.0/indexers/all/results`, sorts by seeders. `INDEXER_CATEGORIES` can be overridden in library.json admin overrides; `0` = no category filter |

## Stream-now (transient, auto-deleted on stop)

| Method | Path | Notes |
|--------|------|-------|
| POST | `/api/stream/prepare` | `{magnet, title}` → adds magnet, waits for metadata + file list, returns `{hash, files[]}`. Stores hash in `state.prepare_hash` |
| DELETE | `/api/stream/cancel?hash=…` | Deletes a torrent added by `/stream/prepare` (e.g. user dismissed the picker) |
| POST | `/api/stream` | `{magnet, title, file_index?, torrent_hash?}` → 202; clears `state` synchronously, kicks off `stream_pipeline`, and runs the prior-torrent qBit cleanup in a background task so the response never waits on qBit. `torrent_hash` reuses the `/prepare` torrent |
| POST | `/api/stop` | Returns **202** — clears `state` and broadcasts `idle` synchronously, then runs the qBit delete + VLC `pl_stop` + minimize in a background task so the UI flips to idle immediately even when qBit/VLC roundtrips are slow. Cancels both `state.stream_task` and `state.library_play_task` first. If a YouTube play was active, also broadcasts `yt_command:close` and hard-kills the kiosk Chrome |
| POST | `/api/retry` | Kill VLC, relaunch with HTTP interface, replay current file + remainder of playlist |
| POST | `/api/stream/save-to-library` | `{title, series, season, episode, save_path}` → adopt the active stream into the library. Restores all file priorities to 1 so the full torrent continues. Sets `library_item_id` so `/api/stop` won't delete files |

## YouTube on TV

Plays a YouTube link in a fullscreen Chrome kiosk on the host display, driven
from the dashboard. Not VPN-gated (ordinary HTTPS, not P2P). See [YOUTUBE.md](YOUTUBE.md).

| Method | Path | Notes |
|--------|------|-------|
| POST | `/api/youtube` | `{url}` → **202**. Extracts the video id (watch / `youtu.be` / shorts / live / embed / bare id; 400 if none), takes over now-playing state (`youtube_active=True`, `stream_status="playing"`), stops VLC, broadcasts `yt_command:load`, and launches the kiosk if the `/tv` page hasn't beaten in the last 6 s (else hot-swaps via the broadcast). 500 if no Chrome/Chromium found (`_find_chrome`; override with `_CHROME_BIN` in `.env`) |
| POST | `/api/youtube/control` | `{action, value?}`. Playback actions (`playpause`/`play`/`pause`/`seek`/`seek_to`) are relayed to the `/tv` page as an SSE `yt_command`. **Volume actions (`volume_set`/`volume_step`) are handled server-side** — they call `set_system_volume` and do not broadcast (the IFrame `setVolume` doesn't change the OS mixer output). `value` = ±seconds (`seek`), 0–100 % (`seek_to`), or OS volume 0–100 / ±delta (`volume_*`). 409 if no YouTube video is active, 400 on unknown action |
| POST | `/api/youtube/tv-state` | Heartbeat + playback report **from** the `/tv` page: `{video_id, title, time, duration, volume, playback}`. Stamps `state.youtube_tv_seen_at`, mirrors fields onto the reused `active_title`/`vlc_time`/`vlc_duration`/`vlc_volume`, rebroadcasts `state`. Returns `{active}` so a stale page can self-pause after Stop |
| GET | `/tv` | Serves `static/tv.html`, the host-side kiosk player (YouTube IFrame API + `yt_command` listener). Opened by the kiosk launcher with `?v=<id>` |

## Profiles

Up to 6 profiles. No passwords. Optional 4-digit PIN per profile.

| Method | Path | Notes |
|--------|------|-------|
| GET | `/api/profiles` | List all (no PIN exposed; has_pin boolean only) |
| POST | `/api/profiles` | `{name, color}` |
| DELETE | `/api/profiles/{profile_id}` | Removes profile + all its progress entries |
| POST | `/api/profiles/{id}/set-pin` | `{pin, current_pin}` — 4 digits or empty to clear. Admin token can override `current_pin` check |
| POST | `/api/profiles/{id}/verify-pin` | `{pin}` — used by the profile picker |
| POST | `/api/profiles/{id}/set-elevated` | `{elevated}` — admin only; grants view of `admin_only` items |
| POST | `/api/profiles/{id}/auto-skip` | `{auto_skip_intro?, auto_skip_credits?}` |
| POST | `/api/profiles/{id}/resume-mode` | `{resume_mode: "auto"|"prompt"|"off"}` |

## Library

| Method | Path | Notes |
|--------|------|-------|
| GET | `/api/library?profile_id=…` | List items. Filters out `admin_only` items unless admin OR `profile.elevated`. Adds `resume` hint and `hidden: bool` (per-profile visibility) per item |
| GET | `/api/library/{id}/files?profile_id=…` | Per-file list with progress |
| POST | `/api/library/prepare` | `{magnet, title}` → file list for the precision-selection picker (no `state.prepare_hash` side effect) |
| POST | `/api/library/download` | `{magnet, title, series, season, episode, save_path, torrent_hash, selected_file_indices[], default_visible_profiles[]}` — `default_visible_profiles` is optional; if non-empty, only those profile IDs see the item in the main list by default (others see it in the hidden tab) |
| POST | `/api/library/upload` | multipart: `files[]`, `title`, `series`, `season`, `episode`, `save_path` — direct upload of local video files |
| DELETE | `/api/library/{id}?delete_file=true` | Remove item; optionally also delete files from disk via qBit |
| POST | `/api/library/{id}/play` | `{profile_id, files[], seek_first_to?}` → returns **202** immediately. State flips to `buffering`, a `state` SSE event fires, and the VLC `in_play`/`in_enqueue` work runs in a background task (`state.library_play_task`) which broadcasts `playing` when VLC has accepted the first track. Re-issuing Play / prev / next / stop while a prior handoff is in flight cancels it |
| POST | `/api/library/{id}/queue-play` | `?profile_id=…&file_path=…` — auto-play when download (or specific file) completes; boosts qBit priority |
| DELETE | `/api/library/{id}/queue-play` | Cancel pending auto-play |
| POST | `/api/library/{id}/file-priority` | `{file_paths[], priority: 0|1|7}` — qBit priority for specific files |
| POST | `/api/library/{id}/progress` | `{profile_id, file_path, position_sec, duration_sec}` — manual progress save (most progress comes from the tracker task) |
| POST | `/api/library/{id}/mark-watched` | `{profile_id, watched, file_paths[], season?}` — mass mark watched/unwatched |
| GET | `/api/library/{id}/download?file_path=…` | Browser-side file download (single file) |
| POST | `/api/library/{id}/download-zip` | `{file_paths[]}` → streamed ZIP (uses `os.pipe` + thread; ZIP_STORED — no compression) |
| GET | `/api/library/{id}/metadata?refresh=0\|1` | Cached TMDb show metadata (auto-fetches on first call when an API key is configured). Always returns `{enabled, img_base, metadata}`. `enabled=false` when no TMDb key is set — UI falls back to filename parsing |
| POST | `/api/library/{id}/metadata/refresh` | Admin-only. `{tmdb_id?, kind?}` — force a re-fetch; optional `{tmdb_id, kind:"tv"\|"movie"}` overrides the auto-match for items that grabbed the wrong show |

## VLC controls

| Method | Path | Notes |
|--------|------|-------|
| POST | `/api/vlc/pause` | Toggle |
| POST | `/api/vlc/volume/set?volume=0-200` | Sets absolute volume; capped by global `settings.max_volume` |
| POST | `/api/vlc/volume/{up\|down}?step=N` | Server-side relative adjust (default ±10 %), capped by global `settings.max_volume`. UI +/- buttons send this with `step=5` so out-of-sync clients can't snap volume to a stale absolute. |
| POST | `/api/vlc/seek?delta=N` | Relative — `val=±Ns` |
| POST | `/api/vlc/seek/to?position_pct=N` | Absolute — `val=N%`. NOTE: VLC treats `val=N` (no suffix) as a 0–1 fraction. Don't confuse the two |
| POST | `/api/vlc/prev` | Previous episode in series order. Uses `library_playlist` then `item.files`. Returns **202** — VLC handoff runs in background (`state.library_play_task`); buffering state is broadcast immediately, `playing` once VLC accepts the new track |
| POST | `/api/vlc/next` | Next episode in series order. Same 202 + background-handoff pattern as `/prev` |
| GET | `/api/vlc/tracks` | `{audio[], subtitle[], current_audio, current_subtitle, time, length}` — IDs are VLC ES IDs, not 1/2/3 counters |
| POST | `/api/vlc/track/audio/{track_id}` | Switch audio. Saves as profile track-pref for the current file |
| POST | `/api/vlc/track/subtitle/{track_id}` | Switch subtitle (`-1` = off). Saves as profile track-pref |

## Subtitles (OpenSubtitles, keyless legacy REST)

| Method | Path | Notes |
|--------|------|-------|
| GET | `/api/subtitles/search?query=…&lang=…` | Search by movie hash + name. Hash matches sorted first. `lang` is OpenSubtitles 3-letter id (blank = all) |
| POST | `/api/subtitles/download` | `{download_link, lang}` — host must be `*.opensubtitles.org`. Downloads, gunzips, saves as `<stem>.<lang>.srt` next to the video, calls VLC `addsubtitle` + selects the new track (picks max ES ID after add) |

## Smart Skip

| Method | Path | Notes |
|--------|------|-------|
| POST | `/api/skip-now` | `{type: "intro"\|"credits"}` — execute the current offer. Intro = seek to `end_at+1`. Credits = `vlc_next_file` (or `pl_stop` if no next) |
| DELETE | `/api/skip-now` | Dismiss without acting. Marks `state.skip_offer_file` with a `#intro-done` / `#credits-done` suffix so it doesn't re-emit |
| POST | `/api/resume-now` | Apply the current `resume_offer` (seek to saved position) |
| DELETE | `/api/resume-now` | Dismiss; start from beginning |

## Stream to Device

The endpoint paths still carry an `offline-*` prefix for backwards
compatibility; the user-facing flow is now stream-to-device (the device's
`<video>` plays the URL directly instead of saving it to IndexedDB).

| Method | Path | Notes |
|--------|------|-------|
| POST | `/api/library/{id}/offline-prepare` | `{file_path, profile_id?, bulk?}` → ffprobes the source. If the HLS bundle is already on disk: `{ready:true, master_url, duration_sec, videos[], audios[], subtitles[], skipped_image_subs[], subs[] (on-disk sidecars), saved_tracks{audio_idx,subtitle_idx}}`. Otherwise spawns an HLS prep job: `{ready:false, needs_processing:true, job_id, operation:"hls", subs[], saved_tracks}`. `master_url` → `/api/library/offline-cache/<key>/master.m3u8`, loaded by hls.js (Chrome/FF/Edge) or Safari native. `videos[]` is the ABR ladder `[{idx,name,height,label}]` (master order, idx 0 = original). `bulk:true` ⇒ "prep for later" job that honors the global pause gate; default `false` ⇒ interactive play-on-device, which always runs (an existing paused job for the same file is promoted to interactive). **503 on macOS hosts** (`HLS_AVAILABLE` false) |
| GET | `/api/library/offline-job/{job_id}` | Poll a prep job — `{status:"pending"\|"processing"\|"done"\|"error", operation:"hls", progress (0-1), error}`. On `done` it also carries the bundle fields: `master_url, duration_sec, videos[], audios[], subtitles[], skipped_image_subs[], bundle_size_bytes`. Progress is parsed from ffmpeg `-progress pipe:1` |
| GET | `/api/library/offline-cache/{cache_key}/{filename}` | Serves one file from an HLS bundle dir — `master.m3u8`, per-rendition `*.m3u8`, `init_*.mp4`, `seg_*.m4s`, `sub_*.vtt`, `meta.json`. `cache_key` is sha256(VERSION \| path \| mtime \| size)[:24] (24 hex); both segments regex-validated (`_CACHE_KEY_RE` / `_BUNDLE_FILE_RE`). Range-aware, correct HLS MIME types |
| GET | `/api/library/{id}/subtitle?file=…` | Returns a sidecar `.srt`/`.vtt` next to a video file as `text/vtt` (SRT auto-converted by `_srt_to_vtt`). Filename only — no path traversal. Wired straight into `<track src=…>` by the local player |
| GET | `/api/library/{id}/skip-data?file_path=…` | Read-only intro/credits times for one file (or full map when `file_path` is omitted). Same shape as the admin editor but no auth — any profile that can play the item can read its skip data |
| POST | `/api/library/{id}/prep-all` | Pre-runs remux/transcode for every video file in an item so subsequent device-side Play taps a cached MP4 and starts streaming immediately. Returns `{files:[{file_path,name,status,job_id?,progress?}], total, ready, processing, paused, errored, needs_prep, missing}` with one row per file. Coalesces with any in-flight jobs |
| GET | `/api/library/{id}/prep-status` | Same shape as `prep-all` but never starts new work — the UI polls this every 3 s while a prep is in progress, and seeds `prepFileState` so per-row Prep buttons reflect "Stream Ready" |
| GET | `/api/offline-active` | Global view of every active job: `{active, paused, total_jobs, processing_jobs, pending_jobs, paused_jobs, items:[{item_id, title, processing, progress, eta_secs, operation}]}`. Active includes `paused` jobs so the bar (and its Resume button) stays visible while the queue is held. Drives the persistent `#globalPrepBar` indicator so the user can see prep is running/paused even after a page reload or when the originating card is off-screen. Polled at 3 s while jobs exist, 8 s while idle, paused when the tab is hidden |
| POST | `/api/offline-prep/pause` | `{kill}` → pauses bulk stream-prep (non-admin). `kill:false` lets the in-flight file finish, then holds the rest; `kill:true` terminates the running ffmpeg now (restarts from scratch on resume). Interactive play-on-device encodes are never killed. Returns `{ok, paused:true, killed}` |
| POST | `/api/offline-prep/resume` | Resumes bulk stream-prep (non-admin) — clears the gate and re-spawns every paused job. Returns `{ok, paused:false, resumed}` |

Per-file `status` values: `ready_native` (fast-path Safari MP4, no work needed), `cached` (already in `.offline_cache/`), `pending`/`processing` (job running, includes `progress` 0-1 + `operation`), `paused` (bulk job held at the global pause gate — re-spawned on resume), `done` (job just finished), `error`, `needs_prep`, `missing` (file not on disk). The frontend collapses `ready_native`/`cached`/`done` into the single "Stream Ready" UI state and treats `paused` as in-progress.

See [STREAMING.md](STREAMING.md) for the full client/server flow.

## Settings

| Method | Path | Notes |
|--------|------|-------|
| GET | `/api/settings/download-path` | The primary `QBIT_DOWNLOAD_PATH` from .env |
| GET | `/api/settings/library-paths` | All paths: static (`QBIT_DOWNLOAD_PATH`, `LIBRARY_PATH_2..4`) + dynamic (`library.json` → `settings.library_paths[]`) |
| POST | `/api/settings/library-paths?path=…` | Add a UI-managed path (must be an existing directory) |
| DELETE | `/api/settings/library-paths?path=…` | Remove a dynamic path (static .env paths cannot be removed via API) |
| GET | `/api/settings/disk-space` | Per-path `{total_bytes, free_bytes, free_pct}` |
| GET | `/api/settings/max-volume` | `{max_volume}` — global VLC volume cap (0-200) |
| POST | `/api/settings/max-volume` | `{max_volume: 0-200}` — immediately enforces if current VLC volume exceeds the new cap |
| GET | `/api/settings/system-volume-default` | `{system_volume_default}` — host OS volume (0-100, default 70) restored when a YouTube play stops. See [YOUTUBE.md](YOUTUBE.md) |
| POST | `/api/settings/system-volume-default` | `{system_volume_default: 0-100}` — stores in `library.json → settings.system_volume_default`. Does NOT change the OS volume immediately, only at the next YouTube Stop |

## Admin

All require admin auth.

| Method | Path | Notes |
|--------|------|-------|
| GET | `/api/admin/status` | `{enabled}` — is `ADMIN_PASSWORD` set? |
| POST | `/api/admin/login` | `{password}` → `{token}` |
| POST | `/api/admin/logout` | Invalidates the bearer token |
| GET | `/api/admin/settings` | Returns `INDEXER_URL`, `INDEXER_API_KEY`, current `indexer_categories` override, `tmdb_api_key`, and `tmdb_api_key_source ∈ {admin\|env\|unset}` |
| POST | `/api/admin/settings` | `{indexer_categories?, tmdb_api_key?}` — both saved as `library.json` → `settings.admin_overrides.*` (admin override beats `.env`). Empty `tmdb_api_key` clears the override |
| GET | `/api/admin/library` | All items including admin-only; includes `series_key`, `files_with_skip`, `analysis_job` for each |
| GET | `/api/admin/indexers` | List configured Jackett indexers |
| GET | `/api/admin/indexers/available` | List all Jackett-known indexers (configured + available) |
| GET | `/api/admin/indexers/{id}/config` | Indexer config schema for setup form |
| POST | `/api/admin/indexers/{id}/config` | Persist indexer config (POSTs through to Jackett) |
| DELETE | `/api/admin/indexers/{id}` | Remove indexer from Jackett |
| POST | `/api/library/{id}/visibility` | `{profile_id, hidden: bool}` — toggle per-profile visibility. `hidden=true` moves the item to the user's hidden tab; `hidden=false` restores it to the main list. Distinct from `admin_only` (admin content lock) |
| POST | `/api/library/{id}/admin-lock` | `{admin_only}` |
| GET | `/api/admin/library/{id}/skip-data` | Per-file intro/credits times for the editor |
| PATCH | `/api/admin/library/{id}/skip-data` | `{file_path, intro_start?, intro_end?, credits_start?}` — manual override; sets `analysis.source="manual"` |
| POST | `/api/admin/library/{id}/analyze` | Force re-run of series analysis |
| GET | `/api/admin/analyzer-status` | `{available, ffmpeg, fpcalc}` |
| GET | `/api/admin/offline-encoder` | `{nvenc_available, encoder, ffmpeg}` — which encoder offline Save Offline jobs use (h264_nvenc when an NVIDIA GPU + NVENC-built ffmpeg are present, else libx264). Result is cached for the process lifetime. |
| GET | `/api/admin/offline-cache` | `{total_bytes, cache_dir, items:[{item_id, title, file_count, total_bytes, cached_count, processing_count, pending_count, error_count, partial_count, files:[…]}], orphans:[{cache_key, kind:"cached"\|"partial", bytes, mtime}]}`. Each `files[]` entry has `{file_path, name, cache_key, bytes, status}` where `status ∈ cached \| processing \| pending \| error \| partial_stale`; processing entries add `progress, operation, encoder, job_id, started_at, eta_secs?`; error entries add `error, operation, encoder, job_id, started_at`. |
| DELETE | `/api/admin/offline-cache/{cache_key}` | Delete one cached MP4 by its 24-hex basename. 409 if a pending/processing prep job is currently writing that file |
| DELETE | `/api/admin/offline-cache/orphans` | Purge every cache file whose source is gone or has been re-encoded. Returns `{deleted_count, bytes_freed}` |
| DELETE | `/api/admin/library/{item_id}/offline-cache` | Delete every cached MP4 currently mapped to one library item. Skips files locked by an active prep job. Returns `{deleted_count, bytes_freed}` |
| GET | `/api/admin/background-video` | `{name, volume, enabled, exists, size_bytes, currently_playing}` — idle background video settings + live status |
| POST | `/api/admin/background-video` | Multipart `file` upload — replaces any existing `.background/` file. Hot-swaps on screen if bg is currently playing |
| DELETE | `/api/admin/background-video` | Removes file + settings. Stops VLC if bg was on screen |
| POST | `/api/admin/background-video/volume` | `{volume}` 0–200; capped by `settings.max_volume`. Pushed live to VLC if bg is on screen |
| POST | `/api/admin/background-video/enabled` | `{enabled}` toggle without deleting the file. When off, stops VLC if bg is on screen |
| POST | `/api/admin/shutdown` | Stop the StreamLink web server. Returns `{ok:true, message}` immediately, then asynchronously sends SIGTERM to every `uvicorn main:app` process (HTTP + HTTPS siblings). After 3 s without exit, falls back to `os._exit(0)`. qBittorrent / Jackett / VLC are not touched — they're not children of this process |
| POST | `/api/admin/reboot` | Reboot the **whole host machine** (not just the web server). Returns `{ok:true, message}` immediately, then fires `_reboot_machine()` ~0.5 s later (platform-appropriate command chain). For the box to come back the host needs auto-login + the system service (`run.py --install`). Hard reset for a wedged Jackett |
| GET | `/api/admin/scheduled-reboot` | `{enabled, time:"HH:MM", timezone, idle_minutes, last_fired, now}` — `now` is the host's current time in the configured tz |
| POST | `/api/admin/scheduled-reboot` | `{enabled, time:"HH:MM", timezone, idle_minutes}` → saves to `library.json → settings.scheduled_reboot`. Validates HH:MM (24h), clamps `idle_minutes` to 1–720, resets the internal `last_fired` guard. Drives the `scheduled_reboot_loop`: at the configured local time, reboots when idle for `idle_minutes`, else waits and re-checks until idle |
| GET | `/api/admin/overnight-prep` | `{enabled, start:"HH:MM", end:"HH:MM", timezone, on_end, now, in_window, paused}` — overnight auto stream-prep config + the host's current time in the configured tz and whether the window is open now |
| POST | `/api/admin/overnight-prep` | `{enabled, start:"HH:MM", end:"HH:MM", timezone, on_end:"pause"\|"continue"}` → saves to `library.json → settings.overnight_prep`. Validates both HH:MM (24h), rejects an empty (start==end) window, resets in-memory window membership. Drives the `overnight_prep_loop`: on entering the window it queues a bulk HLS-prep job for every un-prepped library file; on leaving it either pauses (in-flight file finishes) or continues to completion |
| GET | `/api/admin/logs` | `{log_dir, files:[{name, bytes, mtime}]}` — lists every file in `LOG_DIR` (newest first by mtime). Used by the admin System tab "Server Logs" card |
| GET | `/api/admin/logs/_bundle` | Streams a ZIP of every file in `LOG_DIR` (deflated). Filename `streamlink-logs-YYYYMMDD-HHMMSS.zip`. 404 if no log files exist |
| GET | `/api/admin/logs/{name}` | Streams a single log file as an attachment (`text/plain; charset=utf-8`). `{name}` is the basename only — slashes/`..`/absolute paths are rejected and the resolved path must stay within `LOG_DIR` |

## Admin HTTPS redirect

`admin_https_redirect` middleware ([main.py:1772](../main.py#L1772)) redirects any HTTP request to `/admin*` or `/api/admin/*` to HTTPS via 301 (assumes the HTTPS process is running on port 443). The HTTPS process is launched by `run.py` if `cert.pem`/`key.pem` exist (generated by `setup.py`).
