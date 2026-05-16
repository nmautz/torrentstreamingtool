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
| `state` | every 2 s; on any state change | full `state_snapshot()` ([main.py:229](../main.py#L229)) |
| `vpn_status` | VPN connect/disconnect transition | `{secure, status}` |
| `stream_status` | stream pipeline phase transition | `{status, message, progress?, downloaded_mb?, total_mb?, dl_speed_bps?, ul_speed_bps?}` |
| `library_progress` | per-download stats, ~every 5 s while downloading | `{item_id, speed_bps, downloaded_bytes, total_bytes, progress_pct, eta_secs}` |
| `library_update` | library item status changed | `{item_id, status, message?}` |
| `progress_saved` | every 15 s while a library item is playing | `{item_id, profile_id, file_path, episode_name, position_sec, duration_sec, pct}` |
| `analysis_status` | Smart Skip job progress | `{series_key, job: {status, stage, current, total, message, episode_name?, …}}` |

## State

| Method | Path | Notes |
|--------|------|-------|
| GET | `/api/state` | Current full snapshot |

## Search

| Method | Path | Notes |
|--------|------|-------|
| GET | `/api/search?q=…&limit=30` | Calls Jackett `/api/v2.0/indexers/all/results`, sorts by seeders. `INDEXER_CATEGORIES` can be overridden in library.json admin overrides; `0` = no category filter |

## Stream-now (transient, auto-deleted on stop)

| Method | Path | Notes |
|--------|------|-------|
| POST | `/api/stream/prepare` | `{magnet, title}` → adds magnet, waits for metadata + file list, returns `{hash, files[]}`. Stores hash in `state.prepare_hash` |
| DELETE | `/api/stream/cancel?hash=…` | Deletes a torrent added by `/stream/prepare` (e.g. user dismissed the picker) |
| POST | `/api/stream` | `{magnet, title, file_index?, torrent_hash?}` → 202; spawns `stream_pipeline` task. `torrent_hash` reuses the `/prepare` torrent |
| POST | `/api/stop` | Cancel pipeline, delete torrent + files (unless saved to library), VLC `pl_stop`, minimize VLC |
| POST | `/api/retry` | Kill VLC, relaunch with HTTP interface, replay current file + remainder of playlist |
| POST | `/api/stream/save-to-library` | `{title, series, season, episode, save_path}` → adopt the active stream into the library. Restores all file priorities to 1 so the full torrent continues. Sets `library_item_id` so `/api/stop` won't delete files |

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
| GET | `/api/library?profile_id=…` | List items. Filters out `admin_only` items unless admin OR `profile.elevated`. Adds `resume` hint per item |
| GET | `/api/library/{id}/files?profile_id=…` | Per-file list with progress |
| POST | `/api/library/prepare` | `{magnet, title}` → file list for the precision-selection picker (no `state.prepare_hash` side effect) |
| POST | `/api/library/download` | `{magnet, title, series, season, episode, save_path, torrent_hash, selected_file_indices[]}` |
| POST | `/api/library/upload` | multipart: `files[]`, `title`, `series`, `season`, `episode`, `save_path` — direct upload of local video files |
| DELETE | `/api/library/{id}?delete_file=true` | Remove item; optionally also delete files from disk via qBit |
| POST | `/api/library/{id}/play` | `{profile_id, files[], seek_first_to?}` → start VLC playback (resolves resume + applies resume_mode) |
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
| POST | `/api/vlc/prev` | Previous episode in series order. Uses `library_playlist` then `item.files` |
| POST | `/api/vlc/next` | Next episode in series order |
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
| POST | `/api/library/{id}/offline-prepare` | `{file_path}` → ffprobes the source. Fast path returns `{ready:true, video_url, subs[], codec_info, duration_sec}` for already-Safari-compatible MP4s (uses the existing `/download` URL). Otherwise spawns an ffmpeg remux/transcode job and returns `{ready:false, job_id, operation:"remux"\|"transcode", subs[]}`. `video_url` is meant to be set as `<video>.src`; the file is served with HTTP Range support |
| GET | `/api/library/offline-job/{job_id}` | Poll a prepare job — `{status:"pending"\|"processing"\|"done"\|"error", operation, progress (0-1), error, video_url?}` (progress is approximated from output-file growth; `-progress` flag is awkward to consume) |
| GET | `/api/library/offline-cache/{name}` | Serves a prepared MP4 from `.offline_cache/`. The name is sha256(path \| mtime \| size)[:24]+`.mp4`. Range-aware (`FileResponse`). Path-traversal–rejected |
| GET | `/api/library/{id}/subtitle?file=…` | Returns a sidecar `.srt`/`.vtt` next to a video file as `text/vtt` (SRT auto-converted by `_srt_to_vtt`). Filename only — no path traversal. Wired straight into `<track src=…>` by the local player |
| GET | `/api/library/{id}/skip-data?file_path=…` | Read-only intro/credits times for one file (or full map when `file_path` is omitted). Same shape as the admin editor but no auth — any profile that can play the item can read its skip data |
| POST | `/api/library/{id}/prep-all` | Pre-runs remux/transcode for every video file in an item so subsequent device-side Play taps a cached MP4 and starts streaming immediately. Returns `{files:[{file_path,name,status,job_id?,progress?}], total, ready, processing, errored, needs_prep, missing}` with one row per file. Coalesces with any in-flight jobs |
| GET | `/api/library/{id}/prep-status` | Same shape as `prep-all` but never starts new work — the UI polls this every 3 s while a prep is in progress, and seeds `prepFileState` so per-row Prep buttons reflect "Stream Ready" |
| GET | `/api/offline-active` | Global view of every active job: `{active, total_jobs, items:[{item_id, title, processing, progress, eta_secs, operation}]}`. Drives the persistent `#globalPrepBar` indicator so the user can see preprocessing is running even after a page reload or when the originating card is off-screen. Polled at 3 s while jobs exist, 8 s while idle, paused when the tab is hidden |

Per-file `status` values: `ready_native` (fast-path Safari MP4, no work needed), `cached` (already in `.offline_cache/`), `pending`/`processing` (job running, includes `progress` 0-1 + `operation`), `done` (job just finished), `error`, `needs_prep`, `missing` (file not on disk). The frontend collapses `ready_native`/`cached`/`done` into the single "Stream Ready" UI state.

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

## Admin HTTPS redirect

`admin_https_redirect` middleware ([main.py:1772](../main.py#L1772)) redirects any HTTP request to `/admin*` or `/api/admin/*` to HTTPS via 301 (assumes the HTTPS process is running on port 443). The HTTPS process is launched by `run.py` if `cert.pem`/`key.pem` exist (generated by `setup.py`).
