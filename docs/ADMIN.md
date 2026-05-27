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

Controls: **Shut Down Server**, **Reboot Machine**, **Server Logs**, **Scheduled Restart**, and **Overnight Stream Prep**.

#### Shut Down Server

Posts to `POST /api/admin/shutdown`, which finds every `uvicorn main:app` process (both the HTTP listener and, when SSL certs exist, the HTTPS sibling) and sends `SIGTERM`. Sibling processes are signalled first so the admin process stays alive long enough to terminate the others; a 3 s `os._exit(0)` fallback handles the case where uvicorn ignores SIGTERM. Once the HTTP uvicorn exits, `run.py`'s `finally` block at [run.py:873](../run.py#L873) cleans up the HTTPS subprocess and the mDNS responder.

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

Auto-prepares the whole library for on-device streaming during a nightly window, when the heavy ffmpeg load won't bother anyone. Config persists under `library.json → settings.overnight_prep` (`enabled`, `start` HH:MM, `end` HH:MM, `timezone` IANA name, `on_end`). Driven by the `overnight_prep_loop` background task ([main.py](../main.py), registered in `lifespan`).

Panel controls: enable toggle, **Start/End Time** (the window may cross midnight, e.g. `23:00 → 06:00`), **Timezone** (same preset list as Scheduled Restart), **When End Time Is Reached** (`pause` ⇒ hold until the next window · `continue` ⇒ run to completion), and a host-time display.

Loop mechanics: window membership is tracked in-memory (`state.overnight_active`) so entry/exit each fire once.
1. **Entering the window** → clear any pause (`_resume_prep`, which also re-spawns previously-paused jobs) and queue a bulk HLS-prep job for every un-prepped library video file (`_enqueue_library_prep`, idempotent — `_maybe_start_prep_job` skips cached/already-queued files, so a mid-window restart re-enqueues safely).
2. **Leaving the window** → if `on_end == "pause"`, call `_pause_prep(kill=False)` (the in-flight file finishes gracefully; the rest hold until the next window); if `on_end == "continue"`, leave the queue running to completion past the window.

Saving config resets `state.overnight_active` so the new schedule is re-evaluated on the next tick. Prep load relief is a separate, user-facing concern — see [STREAMING.md § Pause / resume + overnight](STREAMING.md) for the global pause gate and the non-admin Pause/Resume control.

- `GET /api/admin/overnight-prep` → config + `now` + `in_window` + `paused`.
- `POST /api/admin/overnight-prep` → `{enabled, start, end, timezone, on_end}`. Validates both HH:MM, rejects `start == end`, resets the in-memory window guard.

### 8. Updates

Auto-updater for the dashboard itself + post-update env-key fill-in. The
underlying git/setup plumbing lives in [updater.py](../updater.py); the loop
+ endpoints are in [main.py](../main.py).

#### Auto Update card

- **Enable toggle** — flips `library.json → settings.autoupdate.enabled`. When off, the background loop is dormant.
- **Branch picker** — locked to **main / beta / alpha** by `updater.ALLOWED_BRANCHES`. Saved as `settings.autoupdate.branch`.
- **Check Every (hours)** — between 1 and 168 (clamped server-side). The loop polls every minute and runs a check when this interval has elapsed.
- **Auto-apply** — when on, a detected update triggers the full sequence (git apply → `setup.py` → service uninstall+reinstall → host reboot) but only if `_machine_in_use(300)` returns False. When off, the loop just notes the update is available; the admin uses **Apply Now** when ready.
- **Status panel** — current branch, current commit, last check + result, last applied + commit, plus a live message for the in-flight phase (`checking`, `applying`, `setup`, `reinstalling-service`, `rebooting`, `error`).
- **Buttons** — **Check Now** (`POST /api/admin/updater/check`), **Apply Now** (full sequence on the branch *currently selected in the picker*, confirm-gated — so switching back to an older branch is just "set picker → Apply Now"), **Switch Branch** (hard checkout origin/`<branch>`, no setup, no reboot), **Save** (persist config changes without applying).
- **Diagnostics** — a collapsed details panel exposes the last 8 KiB of `setup.py` stdout/stderr (and any output from the service uninstall/reinstall) so a non-zero exit can be diagnosed without SSH.

#### What Apply Now does

`POST /api/admin/updater/apply {branch, reboot:true}` runs four steps in order:

1. **git apply** — `git switch -C <target> origin/<target>` (if current ≠ target) then `git reset --hard origin/<target>`. Symmetric: works for forward, backward, and side-grade switches. `library.json`, `.env`, `.offline_cache/`, `.background/` are all gitignored and survive.
2. **setup.py** — re-run non-interactively (`stdin=DEVNULL`, all prompts fall through to stored defaults via setup.py's `_STDIN_INTERACTIVE` guard) so new deps + qBit ini + certs match the new code's expectations.
3. **Service reinstall** — `daemon.uninstall()` + `daemon.install()` on a worker thread (the daemon helpers are sync). This regenerates `streamlink_service.py` from the freshly-pulled `daemon._WRAPPER_CONTENT`, so the supervisor wrapper matches the new code. Best-effort: a failed reinstall is logged + persisted but the reboot still fires (better to come back up with a stale service than be stuck pre-reboot).
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
