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

Active prep jobs are protected: any `pending`/`processing` job whose `out` path matches a cache file is skipped during deletion (the per-file endpoint returns 409; bulk endpoints just skip and continue). The inventory does not auto-refresh — use the **Refresh** button (or any delete action, which reloads after completing) to update progress bars and statuses.

Single-key deletes are atomic: clicking **Delete**/**Clear** on one row removes `<key>.mp4`, `<key>.part.mp4`, AND any terminal (`done`/`error`) job entries that target it, via `_delete_cache_artifacts` in main.py.

Endpoints:
- `GET /api/admin/offline-cache` → `{total_bytes, cache_dir, items:[…], orphans:[…]}` — see [API.md](API.md) for the per-file shape
- `DELETE /api/admin/offline-cache/{cache_key}` → `{deleted, bytes_freed}` (409 if a job is writing the file)
- `DELETE /api/admin/offline-cache/orphans` → `{deleted_count, bytes_freed}`
- `DELETE /api/admin/library/{item_id}/offline-cache` → `{deleted_count, bytes_freed}`

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

Controls: **System Health**, **Shut Down Server**, **Reboot Machine**, **Server Logs**, **Scheduled Restart**, **Overnight Stream Prep**, **Idle Auto-Prep**, **Seeding & Bandwidth**, **Auto-Generated Subtitles**, and **Optional Components**.

#### System Health

Live host load, so an operator can see at a glance whether the box is coping. A `system_monitor_loop` ([main.py](../main.py)) samples every 5 s and classifies **CPU**, **Memory**, **GPU** (via `nvidia-smi`; "Not detected" when absent), and **Network** (throughput + error/drop rate) as `ok` / `degraded` / `overloaded`, plus an **overall** badge (the worst component). The card polls `GET /api/admin/system-resources` every 4 s while the System tab is open and notes whether background prep/transcoding is running now (it runs below normal priority and is killed the instant a viewer is active — see [STT.md](STT.md) + [GOTCHAS.md](GOTCHAS.md)). The same `sys_status` rides in every `state` SSE event and drives the user-facing "host busy — performance may be reduced" banner on the dashboard.

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
- **Per-file Download** is a plain `<a download>` link to `/api/admin/logs/{name}?admin_token=…`. The token rides as a query param because anchor downloads can't set headers.
- **Download All (.zip)** hits `/api/admin/logs/_bundle?admin_token=…` and streams a ZIP of every file in `LOG_DIR`. Filename includes a host-local timestamp so multiple snapshots don't collide.
- **Clear All** (`DELETE /api/admin/logs`, confirm-gated) truncates the active rotating handlers in-place (`streamlink_app.log`, `hls.log`) and deletes the non-active siblings (rotated `.1`/`.2`/`.3`, plus `streamlink.err` written by the system service). Truncation rather than delete on the live files is deliberate: on Windows you can't `unlink` a file the running process has open for writing, and on POSIX a delete would leave logging's FD valid but disconnected — subsequent writes would vanish until restart. Falls back to a write-mode truncate if `unlink` fails (e.g. the service still holds an exclusive Windows handle on `streamlink.err`).

Path traversal is blocked server-side: `_safe_log_path` resolves the requested name against `LOG_DIR` and refuses any name containing a slash, `..`, an absolute path, or a resolved location that escapes the directory.

#### Scheduled Restart

A daily, idle-gated reboot. Config persists under `library.json → settings.scheduled_reboot` (`enabled`, `time` HH:MM, `timezone` IANA name, `idle_minutes`, plus an internal `last_fired` date). Driven by the `scheduled_reboot_loop` background task ([main.py](../main.py), registered in `lifespan`):

1. At/after the configured local time (computed via `_now_in_tz`), if it hasn't already fired today, check `_machine_in_use(idle_minutes * 60)`.
2. **Idle** → write `last_fired = today` (loop guard), then `_reboot_machine()`.
3. **In use** → wait `idle_minutes` and re-check, repeating until idle.

"In use" = live VLC playback/pause of non-background content, an active stream (`stream_status ∈ buffering|playing`), a running download (`downloading_count > 0`), or a user interaction within the window. User interactions are stamped onto `state.last_activity` by the `track_activity` middleware (mutating verbs + `/api/search`; routine GET polling is ignored).

The persisted `last_fired` date is what stops a just-rebooted machine from re-arming and looping (it comes back up past the scheduled time, sees `last_fired == today`, and stands down until tomorrow). Saving new config clears `last_fired` so a freshly-set time can arm the same day.

- `GET /api/admin/scheduled-reboot` → config + `now` (host time in the configured tz, for display).
- `POST /api/admin/scheduled-reboot` → `{enabled, time, timezone, idle_minutes}`. Validates HH:MM, clamps `idle_minutes` to 1–720, resets `last_fired`.

#### Overnight Stream Prep

Auto-prepares the whole library for on-device streaming during a nightly window, when the heavy ffmpeg load won't bother anyone. Config persists under `library.json → settings.overnight_prep` (`enabled`, `start` HH:MM, `end` HH:MM, `timezone` IANA name, `on_end`). Driven by the unified `auto_prep_loop` background task ([main.py](../main.py), registered in `lifespan`), which also serves Idle Auto-Prep below.

Panel controls: enable toggle, **Start/End Time** (the window may cross midnight, e.g. `23:00 → 06:00`), **Timezone** (same preset list as Scheduled Restart), **When End Time Is Reached** (`pause` ⇒ hold until the next window · `continue` ⇒ run to completion), and a host-time display.

Loop mechanics: an in-memory edge flag (`state.auto_prep_engaged`) tracks whether prep is currently running, so entry/exit each fire once.
1. **Entering the window** → clear any pause (`_resume_prep`, which also re-spawns previously-paused jobs) and queue a bulk HLS-prep job for every un-prepped library video file (`_enqueue_library_prep`, idempotent — `_maybe_start_prep_job` skips cached/already-queued files, so a mid-window restart re-enqueues safely).
2. **Leaving the window** → if `on_end == "pause"`, call `_pause_prep(kill=False)` (the in-flight file finishes gracefully; the rest hold until the next window); if `on_end == "continue"`, leave the queue running to completion past the window.

Saving config resets `state.auto_prep_engaged` so the new schedule is re-evaluated on the next tick. Prep load relief is a separate, user-facing concern — see [STREAMING.md § Pause / resume + auto-prep](STREAMING.md) for the global pause gate and the non-admin Pause/Resume control.

> **Doubles as the idle/night DOWNLOAD window.** The Overnight Stream Prep window **and** Idle Auto-Prep idleness also gate user-facing **idle-only library downloads** — the per-download Pause (defer to idle) control and the "Download at idle/night only" toggle in the download modal. `_download_idle_open` reuses both (so an idle-only download runs during the overnight window or whenever the box is idle), independently of whether prep itself is running. If **neither** is enabled, idle-only downloads have no window to run in and the download modal warns. See [docs/API.md § Download scheduling](API.md) and [docs/LIBRARY_DATA.md](LIBRARY_DATA.md).

- `GET /api/admin/overnight-prep` → config + `now` + `in_window` + `paused`.
- `POST /api/admin/overnight-prep` → `{enabled, start, end, timezone, on_end}`. Validates both HH:MM, rejects `start == end`, resets the auto-prep edge flag.

#### Idle Auto-Prep

The activity-gated companion to the nightly window: instead of (or alongside) a fixed time, auto-prep runs **any time the host has been idle for `idle_minutes`** and pauses — *discarding* the in-flight encode — the instant activity returns. Config persists under `library.json → settings.idle_prep` (`enabled`, `idle_minutes`). Same `auto_prep_loop` task; same shared pause gate.

Panel controls: enable toggle, **Idle Time (min)** (1–720), and a live **Status Now** readout (idle vs. in use).

Loop mechanics: "idle" reuses `_machine_in_use(idle_minutes*60)` — the same helper the Scheduled Restart uses — so the box counts as idle only when there's no live VLC playback of real content, no active stream, no running download, and no mutating HTTP interaction within the window. That window doubles as the activity detector: a fresh interaction stamps `state.last_activity`, which flips `_machine_in_use` True within a tick and triggers `_pause_prep(kill=True)` (terminate the running ffmpeg; the file restarts from scratch on the next idle stretch — HLS prep can't checkpoint). When both triggers are enabled, the overnight window runs regardless of activity, and idle-prep's activity-pause overrides overnight `on_end == "continue"`. See [STREAMING.md § Pause / resume + auto-prep](STREAMING.md) for the combined `want` decision.

Saving config resets `state.auto_prep_engaged` so the trigger is re-derived on the next tick.

- `GET /api/admin/idle-prep` → config + `idle_now` (is the box idle right now) + `paused` + `active` (prepping while idle right now).
- `POST /api/admin/idle-prep` → `{enabled, idle_minutes}`. Clamps `idle_minutes` to 1–720, resets the auto-prep edge flag.

#### Optional Components

Installs the portable dependencies the auto-updater can't fetch on its own. `setup.py` under `STREAMLINK_AUTOUPDATE=1` skips all `install_*` steps, so on an auto-updating box anything not already present (most often whisper.cpp + its model, sometimes ffmpeg/fpcalc) never downloads. This card installs them from the web instead of a terminal `setup.py` run.

Lists four components — **ffmpeg**, **fpcalc**, **whisper.cpp** (binary), **whisper model** — each with an Installed/Missing badge, its resolved path, and an Install/Reinstall button. The whisper model has a size picker (base/small/medium, all multilingual). whisper.cpp has a **build picker** — CPU, GPU · CUDA 12 (~440 MB), or GPU · CUDA 11 (~60 MB); when the `nvenc` probe reports an NVIDIA GPU the card recommends a CUDA build (much faster STT) and a CUDA build is preselected. A CUDA build auto-offloads to the GPU and falls back to CPU at runtime if the driver can't initialize CUDA (so a wrong pick degrades rather than fails). Installs stream on the host with a live progress bar.

Mechanics ([main.py](../main.py) `_run_component_install`): reuses `setup.py`'s URL/extract/detect helpers (safe to `import setup` — its prompts are gated under `__main__`), streams the download via httpx for progress, extracts into `tools/`, writes the path into `.env` (`_write_env_keys`), and clears the ffmpeg-version / NVENC / STT-availability caches so the new binary takes effect without a restart. Because the files live in `tools/`, the next auto-update's `detect_tools()` + `merge_tool_paths()` re-detect them — a one-time install persists. **ffmpeg and whisper.cpp binaries are Windows-only here** (off-Windows the button is disabled with an "OS package manager" note); fpcalc and the model install on any OS.

- `GET /api/admin/components` → per-component status + any in-flight install job.
- `POST /api/admin/components/install` → `{component, model?}`; 400 for ffmpeg/whisper off-Windows.

#### Auto-Generated Subtitles

STT (whisper.cpp) config — enable toggle, preferred default language, English-translation toggle, and an unavailable banner when whisper isn't installed. See [STT.md](STT.md). `GET`/`POST /api/admin/stt`.

### 8. Updates

Auto-updater for the dashboard itself + post-update env-key fill-in. The
underlying git/setup plumbing lives in [updater.py](../updater.py); the loop
+ endpoints are in [main.py](../main.py).

#### Auto Update card

- **Enable toggle** — flips `library.json → settings.autoupdate.enabled`. When off, the background loop is dormant.
- **Branch picker** — by default offers **main / beta / alpha** (`updater.ALLOWED_BRANCHES`). Saved as `settings.autoupdate.branch`.
- **Show all branches (developer mode)** — a checkbox under the picker. When ticked, the picker is repopulated from a live `git ls-remote` (`GET /api/admin/updater/branches`) so **any** branch on origin can be selected — handy for riding a feature branch during development. Persisted as `settings.autoupdate.dev_mode`; saved together with the branch on **Save** (and sent inline on **Switch Branch** / **Apply Now** so it takes effect before an explicit Save). With dev mode off, a saved non-canonical branch is sanitised back to `main` on read. The relaxed gate still enforces a structural guard (`updater.branch_allowed`): no leading `-`, no `..`, no whitespace, no path-y `//` — so it can't smuggle in git option injection or an HTML-unsafe name. The branch list itself is fetched only when the box is toggled (or on first open while dev mode is on), not on every 4 s poll.
- **Check Every (hours)** — between 1 and 168 (clamped server-side). The loop polls every minute and runs a check when this interval has elapsed.
- **Auto-apply** — when on, a detected update triggers the full sequence (git apply → `setup.py` → service uninstall+reinstall → host reboot) but only if `_machine_in_use(300)` returns False. When off, the loop just notes the update is available; the admin uses **Apply Now** when ready.
- **Status panel** — current branch, current commit, last check + result, last applied + commit, plus a live message for the in-flight phase (`checking`, `applying`, `setup`, `reinstalling-service`, `rebooting`, `error`).
- **Buttons** — **Check Now** (`POST /api/admin/updater/check`), **Apply Now** (full sequence on the branch *currently selected in the picker*, confirm-gated — so switching back to an older branch is just "set picker → Apply Now"), **Switch Branch** (hard checkout origin/`<branch>`, no setup, no reboot), **Reset Hard** (`POST /api/admin/updater/reset-hard` — `git fetch` + `git reset --hard origin/<current-branch>` to force the working tree back onto the remote, discarding local commits + tracked-file edits; recovery for a wedged/diverged checkout. Stays on the same branch, no `git clean` so untracked/gitignored files survive, no setup, no reboot. Confirm-gated), **Save** (persist config changes without applying).
- **Live refresh vs. in-progress edits** — the card polls `/api/admin/updater` every 4 s (`startAutoupdatePolling`). The poll refreshes only the read-only status fields; it will **not** overwrite the editable controls (branch picker, interval, auto-apply, enable toggle) once you've started editing them. An `_auFormDirty` flag is set on any edit and cleared after Save / Switch Branch (both reload to a known server state) or on a fresh open of the Updates tab. Without this guard the poll would snap your branch selection back to the saved value before you could click Switch/Apply.
- **Diagnostics** — a collapsed details panel exposes the last 8 KiB of `setup.py` stdout/stderr (and any output from the service uninstall/reinstall) so a non-zero exit can be diagnosed without SSH.

#### What Apply Now does

`POST /api/admin/updater/apply {branch, reboot:true}` runs four steps in order:

1. **git apply** — `git switch -C <target> origin/<target>` (if current ≠ target) then `git reset --hard origin/<target>`. Symmetric: works for forward, backward, and side-grade switches. `library.json`, `.env`, `.offline_cache/`, `.background/` are all gitignored and survive.
2. **setup.py** — re-run non-interactively (`stdin=DEVNULL`, all prompts fall through to stored defaults via setup.py's `_STDIN_INTERACTIVE` guard) so new deps + qBit ini + certs match the new code's expectations.
3. **Supervisor wrapper refresh** — `updater.refresh_service_wrapper()` regenerates `streamlink_service.py` from the freshly-pulled `daemon._WRAPPER_CONTENT`. **Plain file write, no admin / UAC needed** — the OS service entry (Task Scheduler / launchd / systemd) keeps pointing at the same wrapper path, so it doesn't need to be re-registered. Idempotent: skipped when the file is already byte-identical. Best-effort: a failed write is logged but the reboot still fires (the existing wrapper file still references dynamic paths to `main.py` / `watchdog.py`, so the new code runs after reboot regardless).
4. **Reboot** — `_reboot_machine(delay=1.5)`. The host comes back; the OS service starts the dashboard on the new code.

`reboot=false` (dev convenience) skips steps 3 and 4 — files refreshed in place, no service touch, no downtime. Used for one-off testing; not the auto-apply path.

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
