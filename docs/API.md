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
| `state` | every 2 s; on any state change | full `state_snapshot()` ([main.py:292](../main.py#L292)). Includes `library_item_id` and the full ordered `library_playlist` (used by the TV→device Handoff to reconstruct the remaining tail), alongside `library_current_file` / `library_current_index` / `is_library_playback`, `jackett_ok` (last known indexer HTTP reachability), `download_idle_open` / `download_idle_configured` (idle/night download window state — see Download scheduling), and `sys_status` (host CPU/RAM/GPU/network health + `overall` ok\|degraded\|overloaded — drives the "host busy" perf banner) |
| `vpn_status` | VPN connect/disconnect transition | `{secure, status}` |
| `jackett_status` | Jackett HTTP reachability transition (from `jackett_health_monitor`, ~20 s poll) | `{ok, url}` |
| `stream_status` | stream pipeline phase transition | `{status, message, progress?, downloaded_mb?, total_mb?, dl_speed_bps?, ul_speed_bps?}` |
| `library_progress` | per-download stats, ~every 5 s while downloading | `{item_id, speed_bps, downloaded_bytes, total_bytes, progress_pct, eta_secs, download_mode, paused}` — `paused: true` ⇒ the scheduler is holding this item (idle window closed); the UI shows "Waiting for idle window" |
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
| GET | `/api/library?profile_id=…` | List items. Filters out `admin_only` items unless admin OR `profile.elevated`. Adds `resume` hint, `hidden: bool` (per-profile visibility), `skip_status ∈ {ok, partial, failed, pending, none}` (Smart Skip availability summary — skip-moded files excluded), `download_mode ∈ {now, idle}` (drives the card's Pause/Resume control), and `download_partial: bool` (any file was skipped → "⊘ Partial" badge) per item |
| GET | `/api/library/{id}/files?profile_id=…` | Per-file list with progress + per-file `mode ∈ {now, high, idle, skip}` (effective download schedule), `dl_pct` (download %, `null` if unknown), and `complete` (file fully on disk → playable now). `complete`/`dl_pct` come from live qBit per-file progress for **any** torrent-backed item (ready included), matched by full path with a basename fallback — so a finished partial download doesn't mislabel absent files as complete. Response top-level adds `has_torrent`, `download_mode`, `idle_open`, `idle_configured` (see Download scheduling below) |
| POST | `/api/library/prepare` | `{magnet, title}` → file list for the precision-selection picker (no `state.prepare_hash` side effect) |
| POST | `/api/library/download` | `{magnet, title, series, season, episode, save_path, torrent_hash, selected_file_indices[], default_visible_profiles[], download_mode}` — `default_visible_profiles` optional (if non-empty, only those profile IDs see the item by default). `download_mode ∈ {now, idle}` (default `now`); `idle` only downloads during the idle/night window |
| POST | `/api/library/upload` | multipart: `files[]`, `title`, `series`, `season`, `episode`, `save_path` — direct upload of local video files |
| DELETE | `/api/library/{id}?delete_file=true` | Remove item; optionally also delete files from disk via qBit |
| POST | `/api/library/{id}/play` | `{profile_id, files[], seek_first_to?}` → returns **202** immediately. State flips to `buffering`, a `state` SSE event fires, and the VLC `in_play`/`in_enqueue` work runs in a background task (`state.library_play_task`) which broadcasts `playing` when VLC has accepted the first track. Re-issuing Play / prev / next / stop while a prior handoff is in flight cancels it |
| POST | `/api/library/{id}/queue-play` | `?profile_id=…&file_path=…` — auto-play when download (or specific file) completes. Routes the boost through the download model (specific file → `high`; whole-item → `mode=now` + qBit `topPrio`) so the scheduler keeps it |
| DELETE | `/api/library/{id}/queue-play` | Cancel pending auto-play |
| POST | `/api/library/{id}/download-schedule` | `{mode: "now"\|"idle", reset_files?: bool}` — item-level download schedule. `idle` = Pause (download only during the idle/night window, auto-resuming there); `now` = Resume (download immediately). Sweeps per-file `now`/`high`↔`idle` overrides too, leaving explicit `skip` alone. `reset_files: true` clears **all** per-file overrides so every file (incl. skipped) inherits `mode` — the episode picker's whole-torrent "All Now / All Idle". Reconciles qBit immediately (`_apply_item_schedule`) |
| POST | `/api/library/{id}/file-schedule` | `{file_paths[], mode: "now"\|"high"\|"idle"\|"skip"}` — set the download schedule for specific files (or a whole folder, by passing its files): `now` (normal), `high` (download now, first), `idle` (only during the idle/night window), `skip` (never). Reconciles qBit immediately. Works on a **finished** item too: re-enabling a skipped file (or moving idle→now) flips the item back to `downloading` so it actually fetches (`_apply_item_schedule`) |
| POST | `/api/library/{id}/progress` | `{profile_id, file_path, position_sec, duration_sec}` — manual progress save (most progress comes from the tracker task) |
| POST | `/api/library/{id}/mark-watched` | `{profile_id, watched, file_paths[], season?}` — mass mark watched/unwatched |
| GET | `/api/library/{id}/download?file_path=…` | Browser-side file download (single file) |
| POST | `/api/library/{id}/download-zip` | `{file_paths[]}` → streamed ZIP (uses `os.pipe` + thread; ZIP_STORED — no compression) |
| GET | `/api/library/{id}/metadata?refresh=0\|1` | Cached TMDb show metadata (auto-fetches on first call when an API key is configured). Always returns `{enabled, img_base, metadata}`. `enabled=false` when no TMDb key is set — UI falls back to filename parsing |
| POST | `/api/library/{id}/metadata/refresh` | Admin-only. `{tmdb_id?, kind?}` — force a re-fetch; optional `{tmdb_id, kind:"tv"\|"movie"}` overrides the auto-match for items that grabbed the wrong show |

### Download scheduling

Per-item download scheduling persists in `library.json → item.download` (`{mode, files}`) and is applied to qBittorrent by the `download_scheduler_loop` background task + `_apply_item_schedule` (on demand from the endpoints) — the **single writer** of scheduled items' qBit file priorities + torrent pause/resume (see [BACKEND.md](BACKEND.md) and [GOTCHAS.md](GOTCHAS.md)). Schedule changes work in the **ready** state too: re-enabling a skipped file reactivates the item. The "idle/night window" reuses the admin prep schedules (Overnight Stream Prep window OR Idle Auto-Prep idleness, via `_download_idle_open`). `download-schedule` / `file-schedule` (above) write the model and reconcile immediately. `state_snapshot` exposes `download_idle_open` (window open right now) and `download_idle_configured` (any admin prep window enabled — the UI warns when picking idle-only with none configured). A complete file in a still-downloading torrent (`files[].complete`) is playable to VLC now via `/api/library/{id}/play` with that single file.

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

## AI Subtitles (speech-to-text, whisper.cpp)

Generated subs are sidecar `<stem>.<lang>.ai.srt` files next to the source —
picked up by VLC and the on-device HLS player through the existing sidecar
plumbing. Trigger = no usable text subtitle (none, image-only, or none matching
the admin default language). See [STT.md](STT.md). All return **503** when
whisper.cpp / its model isn't installed (`state.stt_available` false).

| Method | Path | Notes |
|--------|------|-------|
| POST | `/api/library/{id}/generate-subtitles` | `{file_path, translate?}` → interactive STT for a library file (on-device context). `{status:"processing"\|"cached"\|"error", job_id?, progress?}`. Writes sidecars; poll `/api/stt-job/{id}` |
| POST | `/api/subtitles/generate` | `{translate?}` → interactive STT for the file VLC is currently playing. On completion the sidecar is loaded into VLC + selected. Same response shape |
| GET | `/api/stt-job/{job_id}` | Poll an STT job — `{status:"pending"\|"processing"\|"paused"\|"done"\|"error", progress (0-1), error, tracks[]}`. On `done` also `subs[]` (the file's full sidecar list incl. generated tracks, each `{name,lang,ai,url}`) |

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
| GET | `/api/library/offline-job/{job_id}` | Poll a prep job — `{status:"pending"\|"processing"\|"done"\|"error", operation:"hls", progress (0-1), error}`. On `done` it also carries the bundle fields: `master_url, duration_sec, videos[], audios[], subtitles[], skipped_image_subs[], bundle_size_bytes, subs[]` (on-disk sidecars incl. any generated `.ai.srt`). Progress is parsed from ffmpeg `-progress pipe:1` |
| GET | `/api/library/offline-cache/{cache_key}/{filename}` | Serves one file from an HLS bundle dir — `master.m3u8`, per-rendition `*.m3u8`, `init_*.mp4`, `seg_*.m4s`, `sub_*.vtt`, `meta.json`. `cache_key` is sha256(VERSION \| path \| mtime \| size)[:24] (24 hex); both segments regex-validated (`_CACHE_KEY_RE` / `_BUNDLE_FILE_RE`). Range-aware, correct HLS MIME types |
| GET | `/api/library/{id}/subtitle?file=…` | Returns a sidecar `.srt`/`.vtt` next to a video file as `text/vtt` (SRT auto-converted by `_srt_to_vtt`). Filename only — no path traversal. Wired straight into `<track src=…>` by the local player |
| GET | `/api/library/{id}/skip-data?file_path=…` | Read-only intro/credits times for one file (or full map when `file_path` is omitted). Same shape as the admin editor but no auth — any profile that can play the item can read its skip data |
| POST | `/api/library/{id}/prep-all` | Pre-runs remux/transcode for every video file in an item so subsequent device-side Play taps a cached MP4 and starts streaming immediately. Returns `{files:[{file_path,name,status,job_id?,progress?}], total, ready, processing, paused, errored, needs_prep, missing}` with one row per file. Coalesces with any in-flight jobs |
| GET | `/api/library/{id}/prep-status` | Same shape as `prep-all` but never starts new work — the UI polls this every 3 s while a prep is in progress, and seeds `prepFileState` so per-row Prep buttons reflect "Stream Ready" |
| GET | `/api/offline-active?profile_id=` | Global view of every active job: `{active, paused, total_jobs, processing_jobs, pending_jobs, paused_jobs, items:[{item_id, title, processing, progress, eta_secs, operation}]}`. Active includes `paused` jobs so the bar (and its Resume button) stays visible while the queue is held. Drives the persistent `#globalPrepBar` indicator so the user can see prep is running/paused even after a page reload or when the originating card is off-screen. Polled at 3 s while jobs exist, 8 s while idle, paused when the tab is hidden. **`profile_id`** scopes title visibility: if any active item is `admin_only` and the requester is not admin or elevated, **every** entry's `title` is replaced with `"Library content"` and `item_id` is blanked (all-or-nothing redaction — selectively hiding only the restricted entries would itself reveal which one is hidden). Counts/progress/ETA are identical for every caller |
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
| GET | `/api/settings/system-volume-default` | `{system_volume_default}` — host OS volume (0-100, default 70) restored when a YouTube play stops. **Global.** See [YOUTUBE.md](YOUTUBE.md) |
| POST | `/api/settings/system-volume-default` | `{system_volume_default: 0-100}` — stores in `library.json → settings.system_volume_default`. Does NOT change the OS volume immediately, only at the next YouTube Stop |
| GET | `/api/settings/youtube-start-volume` | `{youtube_start_volume}` — host OS volume (0-100, default 30) pre-set the moment a YouTube play starts (before the kiosk loads, before audio). **Global.** See [YOUTUBE.md](YOUTUBE.md) |
| POST | `/api/settings/youtube-start-volume` | `{youtube_start_volume: 0-100}` — stores in `library.json → settings.youtube_start_volume`. Does NOT change the OS volume immediately, only at the next YouTube play |
| GET | `/api/settings/host-volume` | `{host_volume}` — current host OS mixer volume (0-100), or `null` if the platform helper failed (pycaw missing on Windows, no `pactl`/`amixer` on Linux) |
| POST | `/api/settings/host-volume` | `{host_volume: 0-100}` — **immediately** pushes to the host OS mixer via pycaw / `osascript` / `pactl`/`amixer`. Not persisted in `library.json` — the OS owns its own mixer state |

## Admin

All require admin auth.

| Method | Path | Notes |
|--------|------|-------|
| GET | `/api/admin/status` | `{enabled}` — is `ADMIN_PASSWORD` set? |
| POST | `/api/admin/login` | `{password}` → `{token}` |
| POST | `/api/admin/logout` | Invalidates the bearer token |
| GET | `/api/admin/settings` | Returns `INDEXER_URL`, `INDEXER_API_KEY`, current `indexer_categories` override, `tmdb_api_key`, and `tmdb_api_key_source ∈ {admin\|env\|unset}` |
| POST | `/api/admin/settings` | `{indexer_categories?, tmdb_api_key?}` — both saved as `library.json` → `settings.admin_overrides.*` (admin override beats `.env`). Empty `tmdb_api_key` clears the override |
| GET | `/api/admin/library` | All items including admin-only; includes `series_key`, `files_with_skip`, `files_failed` (count of files marked `analysis.source == "failed"`), `skip_status` (item-level summary), and `analysis_job` for each |
| GET | `/api/admin/indexers` | List configured Jackett indexers |
| GET | `/api/admin/indexers/available` | List all Jackett-known indexers (configured + available) |
| GET | `/api/admin/indexers/{id}/config` | Indexer config schema for setup form |
| POST | `/api/admin/indexers/{id}/config` | Persist indexer config (POSTs through to Jackett) |
| DELETE | `/api/admin/indexers/{id}` | Remove indexer from Jackett |
| POST | `/api/library/{id}/visibility` | `{profile_id, hidden: bool}` — toggle per-profile visibility. `hidden=true` moves the item to the user's hidden tab; `hidden=false` restores it to the main list. Distinct from `admin_only` (admin content lock) |
| POST | `/api/library/{id}/admin-lock` | `{admin_only}` |
| GET | `/api/admin/library/{id}/skip-data` | Per-file intro/credits times for the editor. Failed files also carry `error_code` and `error` so the editor can render the failure reason |
| PATCH | `/api/admin/library/{id}/skip-data` | `{file_path, intro_start?, intro_end?, credits_start?}` — manual override; sets `analysis.source="manual"` |
| POST | `/api/admin/library/{id}/analyze` | Force re-run of series analysis |
| GET | `/api/admin/analyzer-status` | `{available, ffmpeg, fpcalc}` |
| GET | `/api/admin/analyzer-log?limit=N` | In-memory Smart Skip event ring buffer (200-deep, resets on restart). Returns `{entries:[{ts, level, series_key, item_id, file_path, error_code, message}], available, ffmpeg, fpcalc}` — drives the Fingerprint Log panel under the Smart Skip admin tab |
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
| POST | `/api/admin/overnight-prep` | `{enabled, start:"HH:MM", end:"HH:MM", timezone, on_end:"pause"\|"continue"}` → saves to `library.json → settings.overnight_prep`. Validates both HH:MM (24h), rejects an empty (start==end) window, resets the auto-prep edge flag. Drives the unified `auto_prep_loop`: on entering the window it queues a bulk HLS-prep job for every un-prepped library file; on leaving it either pauses (in-flight file finishes) or continues to completion |
| GET | `/api/admin/system-resources` | Live host health: `{cpu:{pct,status}, ram:{pct,used_gb,total_gb,status}, gpu:{util_pct,mem_pct,status}\|null, net:{up_mbps,down_mbps,status}, overall, updated_at, prep_active, prep_paused}` — each `status ∈ ok\|degraded\|overloaded`. `{}` until the first sample (~5 s after start). Sampled by `system_monitor_loop`; also rides in every `state` event as `sys_status`. Polled by the admin System Health card |
| GET | `/api/admin/idle-prep` | `{enabled, idle_minutes, idle_now, paused, active}` — idle-triggered auto stream-prep config. `idle_now` = box is idle right now; `active` = prepping while idle right now |
| POST | `/api/admin/idle-prep` | `{enabled, idle_minutes}` → saves to `library.json → settings.idle_prep`. Clamps `idle_minutes` to 1–720, resets the auto-prep edge flag. Drives the same `auto_prep_loop`: when the host has been idle (`_machine_in_use`) for `idle_minutes` it queues bulk HLS-prep for the whole un-prepped library, and on the first sign of activity calls `_pause_prep(kill=True)` — discarding the in-flight encode (restarts later, no mid-file checkpoint) |
| GET | `/api/admin/stt` | `{enabled, default_language, translate, available, languages:[{code,name}]}` — AI auto-subtitle config + whether whisper.cpp is installed + the language-picker options |
| POST | `/api/admin/stt` | `{enabled, default_language, translate}` → saves to `library.json → settings.stt`. `default_language` canonicalized to a 3-letter code ("" = any). When set, files lacking a sub in that language also get one generated. See [STT.md](STT.md) |
| GET | `/api/admin/components` | Status of installable portable deps: `{components:{ffmpeg,fpcalc,whisper,whisper_model:{label,installed,path,installable,purpose,job?}}, platform, model_sizes, stt_available, nvenc}`. `nvenc` = an NVIDIA GPU is present (UI recommends a CUDA whisper build). `job` (when present) = `{status:"pending"\|"downloading"\|"done"\|"error", progress, error}`. Polled while an install runs |
| POST | `/api/admin/components/install` | `{component:"ffmpeg"\|"fpcalc"\|"whisper"\|"whisper_model", model?, build?}` → starts a background download+install (streamed for progress), writes the path into `.env`, clears the ffmpeg-version/NVENC/STT caches. `model` (whisper_model only) ∈ base/small/medium. `build` (whisper only) ∈ `cpu`/`cuda12`/`cuda11` (CUDA = GPU; runtime auto-falls-back to CPU via `-ng` if CUDA can't init). ffmpeg/whisper binaries are **400** off-Windows (use the OS package manager). See [SETUP.md](SETUP.md) |
| GET | `/api/admin/logs` | `{log_dir, files:[{name, bytes, mtime}]}` — lists every file in `LOG_DIR` (newest first by mtime). Used by the admin System tab "Server Logs" card |
| GET | `/api/admin/logs/_bundle` | Streams a ZIP of every file in `LOG_DIR` (deflated). Filename `streamlink-logs-YYYYMMDD-HHMMSS.zip`. 404 if no log files exist |
| GET | `/api/admin/logs/{name}` | Streams a single log file as an attachment (`text/plain; charset=utf-8`). `{name}` is the basename only — slashes/`..`/absolute paths are rejected and the resolved path must stay within `LOG_DIR` |
| DELETE | `/api/admin/logs` | Clear every file in `LOG_DIR`. Active rotating handlers (`streamlink_app.log`, `hls.log`) are **truncated in-place via `handler.stream.truncate(0)`** so the live FD keeps working — deleting them would orphan the handle on Windows and silently swallow writes on POSIX. Non-active siblings (rotated `.1`/`.2`/`.3`, `streamlink.err`) are unlinked, with a write-mode truncate fallback if unlink fails (e.g. service holds an exclusive Windows handle). Returns `{ok:true, cleared:[name], errors:[{file, error}]}` |
| GET | `/api/admin/updater` | `{cfg, allowed_branches, is_git_repo, current_branch, current_commit, phase, message, busy, last_output, service_installed, ui_version}` — full state for the admin Updates tab. `cfg` is `settings.autoupdate` (`enabled, branch, dev_mode, interval_hours, auto_apply, last_check_at, last_check_status, last_applied_at, last_applied_commit, last_error`). `phase ∈ idle\|checking\|applying\|setup\|restarting\|error`. `last_output` is the last 8 KiB of `setup.py` stdout/stderr from the previous apply for diagnostics |
| GET | `/api/admin/updater/branches` | `{ok, branches:[…], allowed_branches:[…]}` — every branch on origin via a fresh `git ls-remote` (canonical main/beta/alpha first, then alphabetical), filtered to structurally-valid names. Powers the dev-mode "show all branches" picker. Kept off the polled `/api/admin/updater` payload because it costs a network round-trip; the UI fetches it only when the picker is opened. `branches:[]` (not an error) when this isn't a git checkout |
| POST | `/api/admin/updater/config` | `{enabled?, branch?, interval_hours?, auto_apply?, dev_mode?}` — partial merge into `settings.autoupdate`. `dev_mode` is applied first, then `branch` is validated via `updater.branch_allowed`: with `dev_mode=false` only `ALLOWED_BRANCHES` (main / beta / alpha) pass; with `dev_mode=true` any structurally-valid branch name passes (no leading `-`, no `..`/`//`, no whitespace). `interval_hours` clamped to 1–168 |
| POST | `/api/admin/updater/check` | Force an immediate `git fetch` + compare against `origin/<branch>` (branch from saved config). Uses the saved `dev_mode` to gate the branch. Returns `{ok, branch, local, remote, behind_by, ahead_by, has_update}` or `{ok:false, branch, error}` on failure. 409 if another update operation is in progress |
| POST | `/api/admin/updater/apply` | `{branch?, reboot:true, dev_mode?}` — run the full sequence: **git apply → setup.py → supervisor-wrapper refresh → host reboot**. `branch` accepts main/beta/alpha (or any branch when `dev_mode=true`, which the picker sends inline so it works before an explicit Save); passing one that differs from the current working tree triggers a switch (`git switch -C` + `git reset --hard`) in either direction. With `reboot=true` (default) the host comes down ~1.5 s after the response flushes; the admin UI handles the dead SSE connection as "reboot in progress" and waits for the host to come back. With `reboot=false`, steps 3 and 4 are skipped (code-only refresh — dev convenience). Returns `{ok, stage, message, commit?, service_reinstalled?, service_install_output?, reboot_pending?}` (the field is named `service_reinstalled` for legacy-toast compatibility but now means "the wrapper file write succeeded"). Phases reported via SSE: `applying → setup → refreshing-service → rebooting`. The wrapper refresh requires no admin / UAC — Windows-friendly. 409 if another operation is running |
| POST | `/api/admin/updater/switch-branch` | `{branch, dev_mode?}` — `git switch -C <branch> origin/<branch>` then `git reset --hard origin/<branch>`. Persists the new branch (+ `dev_mode` when provided) as the default. `dev_mode` gates a non-canonical target so the switch works before a separate Save. Does NOT run setup or restart — admin can follow up with `/apply` for the full refresh. 409 if another operation is running |
| POST | `/api/admin/updater/reset-hard` | No body — `git fetch` + `git reset --hard origin/<current-branch>`. Forces the working tree back onto the remote, discarding local commits + uncommitted edits to tracked files. Stays on the current branch (no switch) and does no `git clean`, so untracked/gitignored files (library.json, .env, .offline_cache/, .background/) survive. Does NOT run setup or reboot. The current branch is gated via `branch_allowed` using the saved `dev_mode` — 500 on a detached HEAD, or (dev mode off) an out-of-list branch. Returns `{ok, branch, commit}`. 409 if another operation is running |
| GET | `/api/admin/env-keys` | `{features:[{key, label, description, required, secret, present}]}` — env-key feature registry from `ENV_KEY_FEATURES` in `main.py`. `present=true` ⇒ the corresponding `Settings` attribute is non-empty (or, for `tmdb_api_key`, the admin override is set in library.json). Drives the Required API Keys card on the Updates tab |
| POST | `/api/admin/env-keys` | `{keys: {KEY: value, ...}}` — merge into `.env` (existing comments + ordering preserved). Only keys in `ENV_KEY_FEATURES` are accepted (400 otherwise). Empty value clears the entry (Settings falls back to its declared default). Re-instantiates the in-process `Settings` so changes take effect immediately, then broadcasts a fresh `state` event so every client's banner clears |

## Admin HTTPS redirect

`admin_https_redirect` middleware ([main.py:1772](../main.py#L1772)) redirects any HTTP request to `/admin*` or `/api/admin/*` to HTTPS via 301 (assumes the HTTPS process is running on port 443). The HTTPS process is launched by `run.py` if `cert.pem`/`key.pem` exist (generated by `setup.py`).
