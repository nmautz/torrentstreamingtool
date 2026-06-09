# Admin Panel

`/admin` — served by `static/admin.html` ([main.py:3167](../main.py#L3167)). Disabled if `ADMIN_PASSWORD` is empty in `.env`.

## Auth flow

1. `GET /api/admin/status` returns `{enabled: bool}`. If false, the login overlay shows "Admin disabled" and the dashboard hides the admin link
2. `POST /api/admin/login {password}` → returns `{token}` (32 hex chars, `secrets.token_hex(32)`)
3. Token stored client-side in `sessionStorage.admin_token`. Sent on every request via `Authorization: Bearer <token>`
4. Server-side store: `_admin_sessions: dict[str, float]` — token → Unix-timestamp expiry. TTL is 24 h ([main.py:3184](../main.py#L3184))
5. `_check_admin(request)` accepts token from `Authorization: Bearer`, `X-Admin-Token` header, or `?admin_token=` query param. The query-param form is needed for SSE because EventSource can't set headers

## HTTPS redirect ([main.py:1772](../main.py#L1772))

`admin_https_redirect` middleware: any HTTP request to `/admin*` or `/api/admin/*` returns a 301 to the same path on `https://<host>/`. Assumes the HTTPS process is listening on port 443 — `run.py` only launches it when `cert.pem`+`key.pem` exist.

Browsers will show a warning until `ca.pem` is added to the system trust store. `setup.py` prints the platform-specific command:
- macOS: `sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain ca.pem`
- Linux: `sudo cp ca.pem /usr/local/share/ca-certificates/streamlink-ca.crt && sudo update-ca-certificates`
- Windows: `Import-Certificate -FilePath ca.pem -CertStoreLocation Cert:\\LocalMachine\\Root`

## Tabs

### 1. Indexers ([static/admin.html:95](../static/admin.html#L95))

Lists configured Jackett indexers. Each row shows the indexer name + test result + delete button. Add button opens a modal that:
1. Calls `GET /api/admin/indexers/available` for the full Jackett catalog
2. Renders a filterable list. Selecting one calls `GET /api/admin/indexers/{id}/config` for the config form schema (Jackett returns field types: text, password, checkbox, select)
3. Save POSTs back to `/api/admin/indexers/{id}/config`

Also a small form to override `INDEXER_CATEGORIES` at the top of the tab. This writes to `library.json` → `settings.admin_overrides.indexer_categories` rather than touching `.env`, so it can be changed without a restart. The `/api/search` endpoint reads this override at query time.

A second form on the same tab — **TMDb Metadata** — accepts a free [TMDb v3 API key](https://www.themoviedb.org/settings/api) used by the Netflix-style episode page to fetch show overviews, episode titles, and stills. Persists as `library.json` → `settings.admin_overrides.tmdb_api_key` (admin override beats `.env → TMDB_API_KEY`). `GET /api/admin/settings` also returns `tmdb_api_key_source ∈ {admin, env, unset}` so the UI can show where the active key came from. Empty save clears the override.

#### Jackett authentication

If `JACKETT_PASSWORD` is set in `.env`, `_jackett_admin()` ([main.py:200](../main.py#L200)) calls `/UI/Login` (POSTing `{password}`) and caches the `Jackett` session cookie for 1 hour. All admin indexer endpoints use this cookie. If the password is wrong, returns 502 with "Could not authenticate with Jackett".

### 2. Content Lock ([static/admin.html:142](../static/admin.html#L142))

Lists all library items with an "Admin only" toggle. Calls `POST /api/library/{id}/admin-lock {admin_only}`. When `admin_only=true`:
- `GET /api/library` excludes the item unless the requester is admin OR the requesting `profile_id` has `elevated=true`
- Other endpoints (`/files`, `/play`, `/download`, etc.) currently do not check `admin_only`; the gate is at list-time only

To grant a profile elevated access without making them an admin, use the Profile PINs tab.

### 3. Smart Skip ([static/admin.html:155](../static/admin.html#L155))

For each item:
- **Series key**, file count, "X/Y files have skip data"
- If an analysis job is running for the series, shows a live progress bar (driven by `analysis_status` SSE events)
- **Analyze** button → `POST /api/admin/library/{id}/analyze` — force re-run for the entire series
- **Edit** button → opens inline editor with three numeric fields per file (intro start, intro end, credits start). Empty → clear. Save calls `PATCH /api/admin/library/{id}/skip-data`. Manual edits set `analysis.source="manual"` so they survive future analyzer runs

Admin SSE: `ensureAdminSSE()` opens `/api/events?admin_token=…` so the progress bars live-update.

### 4. Offline Cache ([static/admin.html#panelOffline](../static/admin.html))

Inventory + cleanup for `.offline_cache/<sha>.mp4` (the remuxed/transcoded outputs produced by `/prep-all` and Save Offline). The directory has no automatic eviction, so this tab is the only built-in way to reclaim space.

Each per-file entry carries one of five statuses, surfaced as a coloured badge in the UI:

| Status          | Meaning |
|-----------------|---------|
| `cached`        | `<key>.mp4` is on disk and ready for offline play |
| `processing`    | ffmpeg is actively encoding now — UI shows a live progress bar + ETA |
| `pending`       | Queued behind `OFFLINE_JOB_CONCURRENCY` semaphore; ffmpeg hasn't started yet |
| `error`         | Most recent prep job failed; the ffmpeg stderr tail is rendered inline |
| `partial_stale` | A `<key>.part.mp4` is on disk with no live job (server crashed mid-encode) — safe to delete |

- **Top row** — total bytes on disk (sum of completed `.mp4` and `.part.mp4`), plus the cache directory path.
- **Per-item rows** — every library item that has any kind of state (not just completed encodes). The summary row has small chips for the count of each status. Click the title to expand the per-file list. The header row's **Delete All** removes every completed/partial file and clears every error-state job entry for that item; active jobs are skipped (cancel them from the library card if you really want to abandon them).
- **Orphan card** — appears whenever a cached or partial file's source no longer maps to any library file (re-encoded, deleted, or the library item itself removed). Each orphan row labels itself **Cached** or **Partial**. One-click **Purge All Orphans** drops them all.

Active prep jobs are protected: any `pending`/`processing` job whose `out` path matches a cache file is skipped during deletion (the per-file endpoint returns 409; bulk endpoints just skip and continue).

**Snapshot + freshness.** The recursive size walk over `.offline_cache/` is O(total segments); on a large ABR cache it's slow enough that re-walking on every tab open made the panel feel broken. `_build_offline_cache_inventory` now keeps a module-level **snapshot** (`_offline_cache_inv_snapshot`, guarded by `_offline_cache_inv_lock` so concurrent opens share one walk) with a `generated_at` epoch. `GET /api/admin/offline-cache` serves that snapshot instantly; the tab shows an **"As of …"** line built from `generated_at`. The **Refresh** button calls `?refresh=1` (→ `force=True`), which re-walks the disk and shows a *Checking…* state while it runs. Every delete (per-file, per-item, purge-orphans) and any auto-purge eviction call `_invalidate_offline_cache_inventory()` so a stale snapshot can never show a bundle that's already gone — the post-delete reload rebuilds once. The inventory still does not auto-refresh otherwise (besides the auto-purge loop, which refreshes it for free when enabled).

**Auto-Purge Orphans card** — automates the orphan cleanup so an unattended box can't fill its disk. An enable toggle + a **Purge When Cache Reaches (GB)** threshold (1–10000, default 50) persist under `library.json → settings.cache_autopurge` (`enabled`, `max_gb`). The `cache_autopurge_loop` background task ([main.py](../main.py), registered in `lifespan`) re-checks every 5 min: when enabled and the **total** `.offline_cache/` size is at/above the cap, it purges every orphan bundle — exactly the set the manual **Purge All Orphans** clears (cache/partial dirs + legacy MP4s with no live library file). Bundles backing current library files are **never** touched, and active prep jobs are skipped (`_offline_cache_path_active`), so auto-purge can only ever reclaim space that was already safe to delete. The size walk reuses `_build_offline_cache_inventory(force=True)` (offloaded to a worker thread, and refreshing the admin snapshot for free) and only runs while the feature is on. The card shows a "last run" line (from `state.cache_autopurge_last`) with the count + bytes freed. `GET`/`POST /api/admin/cache-autopurge`.

Single-key deletes are atomic: clicking **Delete**/**Clear** on one row removes `<key>.mp4`, `<key>.part.mp4`, AND any terminal (`done`/`error`) job entries that target it, via `_delete_cache_artifacts` in main.py.

Endpoints:
- `GET /api/admin/offline-cache` → `{total_bytes, cache_dir, generated_at, items:[…], orphans:[…]}` — serves the cached snapshot; `?refresh=1` forces a fresh walk. See [API.md](API.md) for the per-file shape
- `DELETE /api/admin/offline-cache/{cache_key}` → `{deleted, bytes_freed}` (409 if a job is writing the file)
- `DELETE /api/admin/offline-cache/orphans` → `{deleted_count, bytes_freed}`
- `DELETE /api/admin/library/{item_id}/offline-cache` → `{deleted_count, bytes_freed}`
- `GET /api/admin/cache-autopurge` → `{enabled, max_gb, last}` (`last` = `{at, deleted, bytes_freed, total_bytes_before}` or `null`)
- `POST /api/admin/cache-autopurge` → `{enabled, max_gb}`; `max_gb` clamped 1–10000

### 5. Background Video

Single-file uploader for an idle background video that plays on the TV in VLC whenever nothing else is. Stored at `.background/<filename>` (the directory is wiped on every upload so only one file ever exists). Settings live under `library.json → settings.background_video` and survive restarts.

UI elements:
- **Current File** — name + size of the uploaded video, and an `ON SCREEN` chip when `state.background_playing` is true. **Remove** clears the file and the settings entry.
- **Upload** — multipart POST to `/api/admin/background-video`. Accepts the same `VIDEO_EXTS` as the rest of the app (mp4/mkv/mov/m4v/avi/ts/m2ts/webm/wmv). Progress bar is XHR-driven (fetch has no upload progress API). If bg is currently playing, the new file is hot-swapped immediately.
- **Background Volume** — 0–200 slider. Independent of the regular playback volume. Capped by `settings.max_volume`. Saved on `change` (release), not `input`, so dragging only POSTs once.
- **Background Playback** toggle — disables the feature without deleting the file. When toggled off while bg is on screen, VLC is stopped.

Loop mechanics: `background_video_loop` polls every 3 s. If the video file exists, the feature is enabled, `stream_status != "buffering"`, and `vlc_status().state` is anything other than `playing`/`paused`, the loop calls `_play_background_video()`. That helper sets `state.background_playing=True`, snapshots the user's volume into `state.user_volume_before_bg` (on first transition), pushes the bg volume to VLC, then `in_play`s the file. The next user-initiated `vlc("in_play", …)` automatically restores `state.vlc_volume` from the snapshot and flips `background_playing` back to `False` — see the bg-aware branch in the `vlc()` helper.

### 6. Profile PINs ([static/admin.html:182](../static/admin.html#L182))

For each profile:
- **Set PIN** — admin overrides the usual current-PIN check
- **Clear PIN** — same
- **Elevated** toggle → `POST /api/profiles/{id}/set-elevated {elevated}` — grants view of `admin_only` items

### 7. System

Controls: **System Health**, **Shut Down Server**, **Reboot Machine**, **Server Logs**, **Scheduled Restart**, **Automatic Stream Prep**, **Auto-Prep on Play**, **Force Stream Prep**, **Validate & Repair on Prep**, **File Validator**, **VPN Kill Switch**, **Seeding & Bandwidth**, **Subtitles**, **Auto-Generated Subtitles**, and **Optional Components**.

#### System Health

Live host load, so an operator can see at a glance whether the box is coping. A `system_monitor_loop` ([main.py](../main.py)) samples every 5 s and classifies **CPU**, **Memory**, **GPU** (via `nvidia-smi`; "Not detected" when absent), and **Network** (throughput + error/drop rate) as `ok` / `degraded` / `overloaded`, plus an **overall** badge (the worst component). The card polls `GET /api/admin/system-resources` every 4 s while the System tab is open and notes whether background prep/transcoding is running now (it runs below normal priority and is killed the instant a viewer is active — see [STT.md](STT.md) + [GOTCHAS.md](GOTCHAS.md)). The same `sys_status` rides in every `state` SSE event and drives the user-facing "host busy — performance may be reduced" banner on the dashboard.

#### VPN Kill Switch

Chooses how far the Mullvad kill switch reaches when the VPN drops. A single toggle, persisted under `library.json → settings.vpn_killswitch` (`block_ui`, default `true`), mirrored into `state.vpn_block_ui` and surfaced in the `state` + `vpn_status` SSE events so every dashboard reacts live.

- **Block UI** (`block_ui: true`, the historical behaviour) — a VPN drop locks the whole dashboard behind the full-screen "VPN DISCONNECTED" overlay until the VPN returns.
- **qBit Only** (`block_ui: false`) — only qBittorrent is killed; the overlay is suppressed (the VPN pill still turns red) so a viewer can keep using the dashboard, e.g. to watch already-prepped on-device content.

**qBittorrent is killed on a VPN drop regardless of this setting** — that invariant is enforced unconditionally by `vpn_guard` (in-process) and `watchdog.py` (process level), and the P2P stream/download endpoints stay 403'd in both modes. `block_ui` governs the UI lockout only. See [GOTCHAS.md § VPN](GOTCHAS.md#vpn) and [DAEMON_WATCHDOG.md](DAEMON_WATCHDOG.md).

- `GET /api/admin/vpn-killswitch` → `{block_ui}`.
- `POST /api/admin/vpn-killswitch` → `{block_ui}`; broadcasts a `state` snapshot so the overlay appears/clears immediately.

#### Seeding & Bandwidth

Three **global** qBittorrent limits, applied live via the qBit Web API and persisted by qBit in its own config (so they survive a qBit / host restart). They apply to **every** torrent — stream-now and library alike. Read live; the card shows a "qBit offline" chip when qBittorrent isn't reachable.

- **Stop Seeding at Ratio** — an enable toggle + a **Share Ratio** number (uploaded ÷ downloaded). When on, qBit stops a torrent once its ratio is hit. The action is fixed to **pause** (`max_ratio_act = 0`) — the files stay on disk, only seeding stops. E.g. ratio `1.0` stops a 10 GB show after it has uploaded 10 GB. Backed by `max_ratio_enabled` / `max_ratio` in `app/preferences`.
- **Max Upload Speed** / **Max Download Speed** — global caps entered in **MiB/s** (`0` = unlimited). Stored as bytes/sec via `transfer/setUploadLimit` / `transfer/setDownloadLimit` (the unambiguous bytes/sec endpoints — the `app/preferences` `dl_limit`/`up_limit` fields differ in KiB-vs-bytes meaning across qBit versions, so they're avoided).

The download scheduler's `_reconcile_item_downloads` only pauses/resumes torrents in **download-phase** states, so a torrent qBit paused at its ratio (a seeding/finished state) is never auto-resumed — seeding stays stopped. See [API.md](API.md) (`GET`/`POST /api/admin/qbit-limits`) and [BACKEND.md § qBittorrent client notes](BACKEND.md).

- `GET /api/admin/qbit-limits` → `{ok, ratio_enabled, ratio, dl_limit_bytes, up_limit_bytes}` (`{ok:false}` when qBit is unreachable).
- `POST /api/admin/qbit-limits` → `{ratio_enabled, ratio, dl_limit_bytes, up_limit_bytes}`; ratio clamped 0–9998, speeds bytes/sec (0 = unlimited).

#### Shut Down Server

Posts to `POST /api/admin/shutdown`, which schedules a 3.5 s `os._exit(0)` after flushing the response. Both uvicorn servers (port 80 `main:app` + port 443 `https_proxy:app`, when certs exist) live inside the same Python process, so exiting once terminates both. The psutil walk for "uvicorn main:app" processes is a legacy path — it finds nothing in the current architecture and the `os._exit(0)` fallback is what actually performs the shutdown. The launcher's `finally` block at [run.py:1023](../run.py#L1023) cleans up the mDNS responder on the way out.

Note: this only stops the StreamLink web server. qBittorrent, Jackett, and VLC keep running — they are launched separately and are not children of the FastAPI process. Use the host's process manager to stop those if needed.

#### Reboot Machine

Posts to `POST /api/admin/reboot`, which restarts the **whole host computer**, not just the web server. This exists as a hard reset for a wedged Jackett (some hung states only clear on reboot). `_reboot_machine()` ([main.py](../main.py)) tries platform-appropriate commands in order and fires ~0.5 s after the response flushes:

| Platform | Commands tried (first that succeeds wins) |
|----------|--------------------------------------------|
| macOS    | `osascript … System Events … restart` (no sudo from a launchd user agent) → `sudo -n shutdown -r now` → `shutdown -r now` |
| Linux    | `systemctl reboot` → `sudo -n shutdown -r now` → `shutdown -r now` |
| Windows  | `shutdown /r /t 0` |

For the server to come back automatically the host needs **OS auto-login** plus the **system service** installed (`run.py --install`) — see the README. If every command fails (no reboot permission), it logs an actionable hint.

#### Server Logs

Inventory + download for the host's rotating log files in `logs/` (`streamlink_app.log`, `hls.log`, `streamlink.err`, plus any rotated `.1`/`.2`/`.3` siblings). Surfaced so operators can pull diagnostics off a remote box without needing SSH.

- **Refresh** re-reads the directory.
- **Per-file Download** is a plain `<a download>` link to `/api/admin/logs/{name}?admin_token=…`. The token rides as a query param because anchor downloads can't set headers. The server serves a **snapshot copy** (temp file, cleaned up after send) rather than the live file — `streamlink_service.log` is written by the separate service-wrapper process and grows continuously, so streaming the live file was unreliable; the copy gives a stable download.
- **Download All (.zip)** hits `/api/admin/logs/_bundle?admin_token=…` and downloads a ZIP of every file in `LOG_DIR`. Filename includes a host-local timestamp so multiple snapshots don't collide. The ZIP is built into a **seekable temp file**, not streamed through a pipe — a pipe-streamed ZIP forces data descriptors that **Windows Explorer can't extract** (see [GOTCHAS.md](GOTCHAS.md)). Members are read via their own handles so actively-written logs are captured too.
- **Clear All** (`DELETE /api/admin/logs`, confirm-gated) truncates the active rotating handlers in-place (`streamlink_app.log`, `hls.log`) and deletes the non-active siblings (rotated `.1`/`.2`/`.3`, plus `streamlink.err` written by the system service). Truncation rather than delete on the live files is deliberate: on Windows you can't `unlink` a file the running process has open for writing, and on POSIX a delete would leave logging's FD valid but disconnected — subsequent writes would vanish until restart. Falls back to a write-mode truncate if `unlink` fails (e.g. the service still holds an exclusive Windows handle on `streamlink.err`).

Path traversal is blocked server-side: `_safe_log_path` resolves the requested name against `LOG_DIR` and refuses any name containing a slash, `..`, an absolute path, or a resolved location that escapes the directory.

#### Scheduled Restart

A daily, idle-gated reboot. Config persists under `library.json → settings.scheduled_reboot` (`enabled`, `time` HH:MM, `timezone` IANA name, `idle_minutes`, plus an internal `last_fired` date). Driven by the `scheduled_reboot_loop` background task ([main.py](../main.py), registered in `lifespan`):

1. At/after the configured local time (computed via `_now_in_tz`), if it hasn't already fired today, check `_machine_in_use(idle_minutes * 60)` **and** `_prep_in_progress()`.
2. **Idle** → write `last_fired = today` (loop guard), then `_reboot_machine()`.
3. **In use / prep running** → wait `idle_minutes` and re-check, repeating until idle.

"In use" = live VLC playback/pause of non-background content, an active stream (`stream_status ∈ buffering|playing`), a running download (`downloading_count > 0`), or a user interaction within the window. User interactions are stamped onto `state.last_activity` by the `track_activity` middleware (mutating verbs + `/api/search`; routine GET polling is ignored). **In-progress stream prep also defers the reboot** via `_prep_in_progress()` — any HLS-prep or STT job actively encoding (any queue), or a user/admin-priority prep queued to start. This matters because idle prep runs exactly when the box looks idle, and HLS prep can't checkpoint, so a reboot mid-encode would discard the work. Jobs parked at the pause gate (`paused`) don't count; a soft-paused file still finishing its current encode does (it's `processing`).

The persisted `last_fired` date is what stops a just-rebooted machine from re-arming and looping (it comes back up past the scheduled time, sees `last_fired == today`, and stands down until tomorrow). Saving new config clears `last_fired` so a freshly-set time can arm the same day.

- `GET /api/admin/scheduled-reboot` → config + `now` (host time in the configured tz, for display).
- `POST /api/admin/scheduled-reboot` → `{enabled, time, timezone, idle_minutes}`. Validates HH:MM, clamps `idle_minutes` to 1–720, resets `last_fired`.

#### Automatic Stream Prep

One control for when the whole library is auto-prepped for on-device streaming. Config persists under `library.json → settings.auto_prep` (`mode`, `idle_minutes`, `on_activity`), read via `_auto_prep_cfg`, and driven by the unified `auto_prep_loop` background task ([main.py](../main.py), registered in `lifespan`). Replaces the former separate *Overnight Stream Prep* + *Idle Auto-Prep* cards — the fixed nightly time-window concept is gone; modes are purely activity-based.

Panel controls: a **When To Prep** mode chooser plus (for *When Idle*) an **Idle Time (min)** field (1–720), a **When Activity Returns** select, and a live **Status Now** readout.

- **Always** (`mode: "always"`) — prep whenever there's anything un-prepped, regardless of host activity. The loop stays engaged and re-enqueues every ~5 min so freshly-downloaded content is picked up. Background work runs at below-normal priority (see [STREAMING.md](STREAMING.md)).
- **When Idle** (`mode: "idle"`) — prep only after the box has been idle for `idle_minutes`, and stop the instant activity returns. **When Activity Returns** picks the stop kind: `hard` (`_pause_prep(kill=True)`) terminates the in-flight encode immediately (restarts from scratch later — HLS prep can't checkpoint), `soft` (`_pause_prep(kill=False)`) lets the in-flight file finish then holds the rest. "Idle" reuses `_machine_in_use(idle_minutes*60, for_prep=True)` — no live VLC playback of real content, no active stream, no running download, no mutating HTTP interaction, and no open dashboard. The cheap `_activity_kick` hook shortcuts the **hard** case so the kill lands on the request, not up to a tick later.
- **Never** (`mode: "off"`) — no auto-prep; only play-on-TV (Auto-Prep on Play) and the admin Force button prep.

Loop mechanics: an in-memory edge flag (`state.auto_prep_engaged`) tracks whether prep is currently running. A rising edge clears any pause (`_resume_prep`, which re-spawns previously-paused jobs) and queues a bulk HLS-prep job for every un-prepped library video file (`_enqueue_library_prep`, idempotent — `_maybe_start_prep_job` skips cached/already-queued files). Saving config resets `state.auto_prep_engaged` so the new mode is re-evaluated on the next tick. Prep load relief is a separate, user-facing concern — see [STREAMING.md § Pause / resume + auto-prep](STREAMING.md) for the global pause gate and the non-admin Pause/Resume control.

> **Doubles as the idle/night DOWNLOAD window.** Automatic Stream Prep also gates user-facing **idle-only library downloads** — the per-download Pause (defer to idle) control and the "Download at idle/night only" toggle in the download modal. `_download_idle_open` derives the window from `mode`: **Always** ⇒ always open, **When Idle** ⇒ open while the box is idle, **Never** ⇒ no window (the download modal warns), independently of whether prep itself is running. See [docs/API.md § Download scheduling](API.md) and [docs/LIBRARY_DATA.md](LIBRARY_DATA.md).

- `GET /api/admin/auto-prep` → config (`mode`, `idle_minutes`, `on_activity`) + `idle_now` + `paused` + `active` (prepping right now).
- `POST /api/admin/auto-prep` → `{mode, idle_minutes, on_activity}`. Validates `mode ∈ {always, idle, off}` and `on_activity ∈ {soft, hard}`, clamps `idle_minutes` to 1–720, resets the auto-prep edge flag.

#### Auto-Prep on Play

Play-driven on-device prep, separate from the Automatic Stream Prep modes above. When enabled (**on by default**), every VLC library play (`POST /api/library/{id}/play`) immediately HLS-preps the playing episode for on-device, then the rest of the playlist **one episode at a time** (`_maybe_start_play_prep` → `_play_prep_chain`, started on `state.play_prep_task`). If the viewer resumes the current episode with **under 5 minutes left** (`PLAY_PREP_TAIL_SECS = 300`, judged from the saved `duration_sec` vs the resume seek), that episode is skipped and the chain starts at the next one. Each new play cancels the prior chain (so only the current series' tail is prepped); the in-flight ffmpeg of a cancelled chain keeps running.

Crucially these jobs are queued as **`interactive`**, so — unlike Automatic Stream Prep's bulk jobs — they **ignore the global pause gate and are never killed by the activity-kill** (`_pause_prep`/`_activity_kick` only touch `bulk`). They run regardless of the Automatic Stream Prep mode or whether the box is in use, and they **preempt** any in-flight bulk encode so the watched series is prioritised. Config persists under `library.json → settings.play_prep` (`enabled`). Not available on macOS hosts (no HLS). See [STREAMING.md § Auto-prep on play](STREAMING.md).

Panel control: a single enable/disable toggle (saves immediately on click).

- `GET /api/admin/play-prep` → `{enabled}`.
- `POST /api/admin/play-prep` → `{enabled}`.

#### Validate & Repair on Prep

Folds the **File Validator** (below) into automatic / bulk stream prep, so the unattended idle/overnight prep run can also heal corrupt sources. A three-way mode (`library.json → settings.prep_validate.mode`, read via `_prep_validate_cfg`, **default `off`**):

- **`off`** — never validate during prep (current behaviour; manual File Validator only).
- **`before`** — before building a file's HLS bundle, deep-decode the source (`_validate_one_file(deep=True)`); if damaged, **remux-repair** it in place (`_repair_one_file(reencode=False)` — lossless, no lossy re-encode), then prep the *healed* file. Because a repair rewrites the file its `_offline_cache_key` changes, so `_run_offline_job` re-points `out`/`tmp_dir` at the new key (and short-circuits if that bundle already exists) before encoding.
- **`after`** — build the bundle first, then deep-decode the source in the post-prep hook block. A repair purges the just-built bundle (keyed on the old path/mtime/size) so the file re-preps from the healed source on the next idle cycle.

Only **bulk** jobs honour the setting (`job["queue"] == "bulk"` — idle auto-prep + the per-row/per-item Prep buttons); **interactive** play-on-device preps are never validated so playback isn't delayed. The work runs inside the single prep concurrency slot (one ffmpeg at a time) via the shared helper `_prep_validate_repair`, which stashes the live ffmpeg in the prep job's own `_proc` slot — so `_pause_prep(kill=True)`, **Stop Now**, and the activity-kick terminate an in-flight scan/repair with no extra plumbing. The validator/repair `ffmpeg` decodes on the **GPU when NVENC is present** (`_decode_hwaccel_args` → transparent `-hwaccel cuda`, CPU fallback); the optional repair re-encode uses `h264_nvenc`. Not available on macOS hosts (no HLS prep to ride on).

Panel control: three buttons (**Don't Validate** / **Repair Before Prep** / **Repair After Prep**); the active mode is highlighted and saves immediately on click.

- `GET /api/admin/prep-validate` → `{mode}`.
- `POST /api/admin/prep-validate` → `{mode}` ∈ `off`/`before`/`after` (400 on an unknown mode).

#### Force Stream Prep

On-demand prep that **viewers and host activity cannot stop** — the deliberate opposite of Automatic Stream Prep (which the non-admin Pause control and the activity-kill can halt). The card preps the **whole library** or **one selected item** (a scope dropdown — "Whole library" plus every library item) for on-device streaming, immediately, and runs to completion no matter who's watching or how busy the box is. Only the admin's two Stop buttons halt it.

Mechanics: force-prep jobs are queued on a dedicated **`"admin"` prep queue** (`_enqueue_admin_prep` → `_start_admin_prep_job`). Like interactive (play-on-device) prep, admin jobs **ignore the bulk pause gate** (`state.prep_paused`) and **survive the activity-kill** (`_pause_prep` / `_activity_kick` only touch `bulk`), and they **preempt** any in-flight bulk encode (`_preempt_running_bulk`). Bulk prep defers to them via `_priority_hls_pending` (which now counts both `interactive` and `admin`). The one thing that can stop them is the admin Stop control, gated on `state.admin_prep_stop`:

- **Stop (finish file)** — `hard=false`: the in-flight encode runs to completion (and is cached); every queued admin job cancels at the gate. `_stop_admin_prep` sets `admin_prep_stop`, marking pending jobs `"cancelled"`.
- **Stop Now** — `hard=true`: additionally `terminate()`s the running ffmpeg via `job["_proc"]` (tagged `_admin_stopped` so the non-zero rc is treated as an intentional cancel, partial bundle dropped), then cancels the queued rest.

A stopped batch is **not** auto-resumed (unlike the bulk pause/resume); pressing **Force Prep** again clears `admin_prep_stop` and starts a fresh batch. Already-cached files are skipped. Disabled on macOS hosts (no HLS — `hls_available:false`). Status (counts + aggregate progress) polls `GET /api/admin/force-prep` every 3 s while the System tab is open. See [STREAMING.md § Force-prep (admin)](STREAMING.md).

- `GET /api/admin/force-prep` → `{hls_available, active, stopped, total, processing, pending, progress}`.
- `POST /api/admin/force-prep` → `{item_id?}` (None/"" ⇒ whole library); returns `{ok, queued, …status}`. 409 on macOS.
- `POST /api/admin/force-prep/stop` → `{hard}`; returns `{ok, cancelled, killed, …status}`.

#### Optional Components

Installs the portable dependencies the auto-updater can't fetch on its own. `setup.py` under `STREAMLINK_AUTOUPDATE=1` skips all `install_*` steps, so on an auto-updating box anything not already present (most often whisper.cpp + its model, sometimes ffmpeg/fpcalc) never downloads. This card installs them from the web instead of a terminal `setup.py` run.

Lists four components — **ffmpeg**, **fpcalc**, **whisper.cpp** (binary), **whisper model** — each with an Installed/Missing badge, its resolved path, and an Install/Reinstall button. The whisper model has a size picker (base/small/medium, all multilingual). whisper.cpp has a **build picker** — CPU, GPU · CUDA 12 (~440 MB), or GPU · CUDA 11 (~60 MB); when the `nvenc` probe reports an NVIDIA GPU the card recommends a CUDA build (much faster STT) and a CUDA build is preselected. A CUDA build auto-offloads to the GPU and falls back to CPU at runtime if the driver can't initialize CUDA (so a wrong pick degrades rather than fails). Installs stream on the host with a live progress bar.

Mechanics ([main.py](../main.py) `_run_component_install`): reuses `setup.py`'s URL/extract/detect helpers (safe to `import setup` — its prompts are gated under `__main__`), streams the download via httpx for progress, extracts into `tools/`, writes the path into `.env` (`_write_env_keys`), and clears the ffmpeg-version / NVENC / STT-availability caches so the new binary takes effect without a restart. Because the files live in `tools/`, the next auto-update's `detect_tools()` + `merge_tool_paths()` re-detect them — a one-time install persists. **ffmpeg and whisper.cpp binaries are Windows-only here** (off-Windows the button is disabled with an "OS package manager" note); fpcalc and the model install on any OS.

- `GET /api/admin/components` → per-component status + any in-flight install job.
- `POST /api/admin/components/install` → `{component, model?}`; 400 for ffmpeg/whisper off-Windows.

#### File Validator

Decodes the actual **source files** with ffmpeg to find ones that are damaged or corrupt. **This is deliberately separate from the qBittorrent piece recheck** (the episode-picker "Recheck hashes" button / `POST /api/library/{id}/recheck`): a torrent recheck only proves the bytes on disk match the torrent's hashes, so it happily seeds — and validates — a perfect copy of a file that was *already* damaged at the source (bad encode, truncated rip, bit-rot before it was added). The validator opens and decodes the media itself, so it catches a bad source even when the download was byte-perfect.

Two modes:
- **Deep** (default) — runs each library video through `ffmpeg -v error -xerror -i … -map 0:v? -map 0:a? -f null -` and flags any file that emits a decode error. Reads the whole file, so it catches mid-file corruption a header probe misses; the slow option.
- **Quick** — only ffprobes the container (must open, have a decodable video stream, and report a non-zero duration). Fast, but only catches files that won't open at all.

Scope is **Whole library** or a single item (a dropdown of every library item). ffmpeg runs at **below-normal priority** (`_FFMPEG_SUBPROCESS_KW` / `nice`) and **one file at a time** so a scan can't starve playback or the dashboard. **Stop** sets a flag the loop checks between files and terminates the in-flight decode; a cancelled file is never reported as damaged. Works on **every OS** — it's a plain decode, not HLS, so unlike stream prep it is *not* macOS-gated (it only needs ffmpeg installed; the card shows an "ffmpeg isn't installed" notice otherwise).

The card polls `GET /api/admin/validate-files` every 2 s while a scan runs (it self-stops polling once the run finishes), showing live `scanned/total` + the current filename + a running damaged count. On completion it lists every **damaged** file (with the ffmpeg/ffprobe error tail) and any **missing** file (in the library but no longer on disk). State lives in-memory on `state.file_validation` (`running`, `deep`, `scope`, `total`, `scanned`, `current_name`, `damaged[]`, `missing[]`, `started_at`, `finished_at`, `stopped`, `error`) — not persisted, so it resets on restart.

- `GET /api/admin/validate-files` → current/last run snapshot + `available` (ffmpeg present).
- `POST /api/admin/validate-files` → `{scope?, deep?}`; starts a scan (409 if one is running, 503 if ffmpeg is missing).
- `POST /api/admin/validate-files/stop` → halts the running scan.

**Repair.** Once a scan finds damaged files, a **Repair Damaged** control is revealed. It tries to fix each file in two staged attempts, each **validated with the same deep decode** before it's trusted:

1. **Remux** (lossless, fast) — `ffmpeg -err_detect ignore_err -fflags +genpts+igndts -i src -map 0 -ignore_unknown -c copy` to a temp file. Fixes the common breakage: broken index / moov atom, bad or missing PTS, a few recoverable stream errors. No quality loss. `-map 0` keeps **every** stream, so embedded subtitles and attachments survive.
2. **Re-encode** (lossy, slow, opt-in via the **Allow lossy re-encode** checkbox) — decode with error concealment and re-encode the video (libx264 CRF 20 + AAC) so genuinely corrupt frames are dropped/concealed instead of aborting playback. It maps video + audio **plus embedded subtitles** (`-map 0:s?`) and, in MKV, attachments (`-map 0:t?`, ASS fonts), so re-encoding a damaged file no longer strips its baked-in subs — copied as-is for MKV (PGS/VOBSUB included), `mov_text` for MP4/MOV. Sidecar `.srt`s (online-downloaded and `Subs/`-folder ones) are untouched either way: repair replaces the source in place, keeping the same stem they're keyed on.

A candidate is run back through the deep decode; the original is **atomically replaced only when the repair decodes clean** (`os.replace`), so a failed repair never touches the source. On success the file's **stale HLS bundle is purged** (the bundle is keyed on path+mtime+size, so the rewritten file would orphan it anyway — `_repair_one_file` stashes the old key *before* the replace and `rmtree`s that dir, then invalidates the offline-cache inventory snapshot) so on-device playback re-preps from the repaired source. Repaired/failed files are listed (failures keep the ffmpeg reason); **Stop** halts between files and kills the in-flight ffmpeg — a file already replaced stays repaired.

> **Torrent-backed caveat.** Repair rewrites the file in place, so a torrent-backed copy will stop matching its pieces and **seeding halts**. Repair is aimed at uploaded / non-torrent content; for a torrent file, re-downloading the bad pieces (the episode-picker **Recheck hashes** / `POST /api/library/{id}/recheck`) is the better fix. The UI flags repaired files that were torrent-backed.

- `GET /api/admin/repair-files` → current/last repair run snapshot.
- `POST /api/admin/repair-files` → `{paths?, reencode?}`; `paths` defaults to the last scan's damaged list. 409 if a repair *or* a validation scan is running, 503 if ffmpeg is missing, 400 if there's nothing to repair.
- `POST /api/admin/repair-files/stop` → halts the running repair.

#### Subtitles

The unified subtitle policy. Controls: **Default Subtitle Language** (the *one* preferred language — drives online search, automatic track selection on playback, *and* AI generation; defaults to English when never configured, "Any" available), **Subtitles On By Default** (whether playback starts with subs on — off out-of-the-box; each viewer can override in their own Profile Settings), **Auto-Search Online** (when subs are on and no preferred-language track is embedded, fetch one from OpenSubtitles on play, falling back to AI subs — on out-of-the-box), **Upgrade To Real Subs When Found** (swap an auto-applied AI sub for a real preferred-language one once it finishes downloading, then toast — on out-of-the-box; see [STT.md](STT.md)), and **Assume A Lone Subtitle Is Correct** (treat a single discovered sub as the preferred language even when its filename has no tag — on out-of-the-box). Persists to `library.json → settings.subtitles`. `GET`/`POST /api/admin/subtitles`. The per-viewer override is `POST /api/profiles/{id}/subtitles`. A subtitle pick is also remembered **per profile, per series** and re-applied on later episodes. StreamLink sends VLC an **explicit** on/off every play so subtitles can't sneak on — see [GOTCHAS.md](GOTCHAS.md).

#### Auto-Generated Subtitles

STT (whisper.cpp) config — enable toggle, English-translation toggle, and an unavailable banner when whisper isn't installed. The **target language is the unified one set in the Subtitles card** (no separate picker here anymore). See [STT.md](STT.md). `GET`/`POST /api/admin/stt`.

### 8. Updates

Auto-updater for the dashboard itself + post-update env-key fill-in. The
underlying git/setup plumbing lives in [updater.py](../updater.py); the loop
+ endpoints are in [main.py](../main.py).

#### Auto Update card

- **Enable toggle** — flips `library.json → settings.autoupdate.enabled`. When off, the background loop is dormant.
- **Branch picker** — by default offers **main / beta / alpha** (`updater.ALLOWED_BRANCHES`). Saved as `settings.autoupdate.branch`. **Inline branch risk warnings** (`_renderBranchWarning`) update live as the pick changes: **alpha** shows a severe red warning (may break StreamLink, manual reinstall to recover), **beta** a milder amber one (issues may be present), **main** none.
- **Show all branches (developer mode)** — a checkbox under the picker. When ticked, the picker is repopulated from a live `git ls-remote` (`GET /api/admin/updater/branches`) so **any** branch on origin can be selected — handy for riding a feature branch during development. Ticking it also surfaces a red **data-loss / reinstall** warning (`#auDevWarn`): not all branches are functional and switching to one may corrupt data and require a full manual reinstall. Persisted as `settings.autoupdate.dev_mode`; saved together with the branch on **Save** (and sent inline on **Switch Branch** / **Apply Now** so it takes effect before an explicit Save). With dev mode off, a saved non-canonical branch is sanitised back to `main` on read. The relaxed gate still enforces a structural guard (`updater.branch_allowed`): no leading `-`, no `..`, no whitespace, no path-y `//` — so it can't smuggle in git option injection or an HTML-unsafe name. The branch list itself is fetched only when the box is toggled (or on first open while dev mode is on), not on every 4 s poll.
- **Pin To Commit (developer mode only)** — a SHA field + **Pin Commit** button revealed by the dev-mode toggle (`#auCommitPin`, gated client- and server-side on `dev_mode`). Enter a 7–40 hex commit id and it hard-checks out that exact build as a **detached HEAD** (`POST /api/admin/updater/switch-commit` → `updater.switch_commit`) — for reproducing a regression on a specific commit. The SHA is structurally validated (`updater._looks_like_commit`: hex, 7–40 chars) so it can't smuggle in a refspec or git option injection. **While detached, the auto-updater is disabled**: `updater_loop` checks `updater.is_detached_head()` (git's `rev-parse --abbrev-ref HEAD` returns the literal `HEAD`) and stands down each tick — there's no branch to track, and auto-update would silently yank the admin off their pinned build. `GET /api/admin/updater` returns `detached_head: bool`, which drives the amber **"Pinned to a commit (detached HEAD)"** notice (`#auDetachedWarn`). To re-enable auto-update, the admin selects a branch and clicks **Switch Branch** (or **Apply Now**), which reattaches via `git switch -C`. **Reset Hard** refuses to run on a detached HEAD (it needs a branch to reset onto).
- **Check Every (hours)** — between 1 and 168 (clamped server-side). The loop polls every minute and runs a check when this interval has elapsed.
- **Auto-apply** — when on, a detected update triggers the full sequence (git apply → `setup.py` → service uninstall+reinstall → host reboot) but only if `_machine_in_use(300)` returns False. When off, the loop just notes the update is available; the admin uses **Apply Now** when ready.
- **Status panel** — current branch, current commit, last check + result, last applied + commit, plus a live message for the in-flight phase (`checking`, `applying`, `setup`, `reinstalling-service`, `rebooting`, `error`).
- **Buttons** — **Check Now** (`POST /api/admin/updater/check`), **Apply Now** (full sequence on the branch *currently selected in the picker*, confirm-gated — so switching back to an older branch is just "set picker → Apply Now"), **Switch Branch** (hard checkout origin/`<branch>`, no setup, no reboot), **Reset Hard** (`POST /api/admin/updater/reset-hard` — `git fetch` + `git reset --hard origin/<current-branch>` to force the working tree back onto the remote, discarding local commits + tracked-file edits; recovery for a wedged/diverged checkout. Stays on the same branch, no `git clean` so untracked/gitignored files survive, no setup, no reboot. Confirm-gated), **Save** (persist config changes without applying).
- **Live refresh vs. in-progress edits** — the card polls `/api/admin/updater` every 4 s (`startAutoupdatePolling`). The poll refreshes only the read-only status fields; it will **not** overwrite the editable controls (branch picker, interval, auto-apply, enable toggle) once you've started editing them. An `_auFormDirty` flag is set on any edit and cleared after Save / Switch Branch (both reload to a known server state) or on a fresh open of the Updates tab. Without this guard the poll would snap your branch selection back to the saved value before you could click Switch/Apply.
- **Diagnostics** — a collapsed details panel exposes the last 8 KiB of `setup.py` stdout/stderr (and any output from the service uninstall/reinstall) so a non-zero exit can be diagnosed without SSH.

#### Changelog card

A second card on the Updates tab renders `CHANGELOG.md` (newest first) so the admin can review what each release changed before — or after — applying an update. `GET /api/admin/updater/changelog` returns the raw markdown (`{ok, markdown}`, capped at 200 KiB); `loadChangelog()` fetches it **once** on first tab open (not on the 4 s poll) and `_renderChangelogMd()` does a lightweight client-side render — `## [x.y.z]` headings, `-` bullets, inline `**bold**` / `` `code` `` — and **drops** the repo-relative `[text](link)` targets (useless in a browser) while keeping the link text. A **Refresh** button re-fetches.

#### What Apply Now does

`POST /api/admin/updater/apply {branch, reboot:true}` runs four steps in order:

1. **git apply** — `git switch -C <target> origin/<target>` (if current ≠ target) then `git reset --hard origin/<target>`. Symmetric: works for forward, backward, and side-grade switches. `library.json`, `.env`, `.offline_cache/`, `.background/` are all gitignored and survive.
2. **setup.py** — re-run non-interactively (`stdin=DEVNULL`, all prompts fall through to stored defaults via setup.py's `_STDIN_INTERACTIVE` guard) so new deps + qBit ini + certs match the new code's expectations.
3. **Supervisor wrapper refresh** — `updater.refresh_service_wrapper()` regenerates `streamlink_service.py` from the freshly-pulled `daemon._WRAPPER_CONTENT`. **Plain file write, no admin / UAC needed** — the OS service entry (Task Scheduler / launchd / systemd) keeps pointing at the same wrapper path, so it doesn't need to be re-registered. Idempotent: skipped when the file is already byte-identical. Best-effort: a failed write is logged but the reboot still fires (the existing wrapper file still references dynamic paths to `main.py` / `watchdog.py`, so the new code runs after reboot regardless).
4. **Reboot** — `_reboot_machine(delay=1.5)`. The host comes back; the OS service starts the dashboard on the new code.

`reboot=false` (dev convenience) skips steps 3 and 4 — files refreshed in place, no service touch, no downtime. Used for one-off testing; not the auto-apply path.

After a successful `setup.py` step (both the reboot and `reboot=false` paths), the updater writes a `logs/.rotate_pending` marker. On the **next process start**, `_init_logging()` consumes it — before any handler opens a file — and rolls the previous version's logs into `logs/logs_old_<timestamp>.zip`, then clears the originals so the new version starts with empty logs. This is deliberately deferred to startup rather than done inline: the live process holds `streamlink_app.log` / `hls.log` open, and on Windows you can't archive + clear an open log (see [GOTCHAS.md](GOTCHAS.md)). The zip is best-effort per file (an open file is still captured by read; one another process holds open is left in place). Each process start also logs a `StreamLink v<x.y.z> starting — branch=… commit=…` banner so a fresh/rotated log identifies the build that produced it. The `logs_old_*.zip` archives appear in the log inventory + Download All bundle alongside the live logs.

Warnings the UI surfaces inline:
- **System service not installed** — without a supervisor (launchd / systemd / Task Scheduler), the dashboard won't come back after the reboot. Admin is told to run `python run.py --install`.
- **Not a git repository** — running from a tarball / unzipped archive instead of a clone. The updater is disabled; the buttons are greyed out.

> **Auto-login is required for the auto-apply path.** A host reboot ends at the login screen unless the OS is configured to log in automatically; user-level launchd / systemd / Task Scheduler entries don't run before login, so the dashboard would stay down. See `README` for the per-OS auto-login steps and [GOTCHAS.md](GOTCHAS.md).

#### Required API Keys card

Renders the `ENV_KEY_FEATURES` registry from `main.py`. Each row shows:
- The feature name + a one-line description + the literal env key (e.g. `TMDB_API_KEY`).
- A status badge — **Set** (green), **Missing · Required** (red, also drives the user-side banner), or **Optional** (grey).
- An input field, type=password for secrets. Empty fields are skipped on save so a partial form-fill never clears existing keys.

Saving writes through `POST /api/admin/env-keys`, which merges the changes into `.env` (preserves existing comments + key order) and re-instantiates the `Settings` object in-process. Changes take effect immediately; no restart needed for most keys.

The same registry feeds `state_snapshot()` → `missing_env_keys`, which drives the sticky banner in [static/index.html](../static/index.html) (`renderServerAttention`). Required-key gaps banner everyone; optional gaps only show up on this admin card.

## Server endpoints (admin)

All require admin auth (`_require_admin`). See [API.md](API.md#admin) for the full table.

## See also

- [FRONTEND.md](FRONTEND.md) — admin.html structure
- [API.md](API.md) — admin endpoint signatures
- [BACKEND.md](BACKEND.md) — `_check_admin`, `_jackett_admin`, `_admin_sessions`
