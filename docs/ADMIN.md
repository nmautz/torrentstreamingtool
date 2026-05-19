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

Single destructive action: **Shut Down Server**. Posts to `POST /api/admin/shutdown`, which finds every `uvicorn main:app` process (both the HTTP listener and, when SSL certs exist, the HTTPS sibling) and sends `SIGTERM`. Sibling processes are signalled first so the admin process stays alive long enough to terminate the others; a 3 s `os._exit(0)` fallback handles the case where uvicorn ignores SIGTERM. Once the HTTP uvicorn exits, `run.py`'s `finally` block at [run.py:873](../run.py#L873) cleans up the HTTPS subprocess and the mDNS responder.

Note: this only stops the StreamLink web server. qBittorrent, Jackett, and VLC keep running — they are launched separately and are not children of the FastAPI process. Use the host's process manager to stop those if needed.

## Server endpoints (admin)

All require admin auth (`_require_admin`). See [API.md](API.md#admin) for the full table.

## See also

- [FRONTEND.md](FRONTEND.md) — admin.html structure
- [API.md](API.md) — admin endpoint signatures
- [BACKEND.md](BACKEND.md) — `_check_admin`, `_jackett_admin`, `_admin_sessions`
