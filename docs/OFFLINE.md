# Handoff to Device — Offline Playback

End-to-end flow for "save an episode to the browser, play it locally even when the host is unreachable". Lives across `main.py` (offline-prepare endpoints + `.offline_cache/`), `static/sw.js` (PWA shell + read-only API cache), and `static/index.html` (IndexedDB store + `<video>`-based local player).

---

## When to use this doc

Read this when changing anything related to:
- `/api/library/{id}/offline-prepare`, `/offline-job/{id}`, `/offline-cache/{name}`, `/subtitle`, `/skip-data`
- `static/sw.js` cache strategy
- The local player UI (`#localPlayerTiny`, `#localPlayerFull`)
- `offlineSaved`, `lp`, `osm`, `pcCtx`, `outboxFlush`, or any `lp*` / `osm*` / `pc*` JS function
- IndexedDB schema (`streamlink-offline` DB, stores: `videos`, `meta`, `outbox`)

For player-style UI changes that do not touch offline logic, see [FRONTEND.md](FRONTEND.md). For backend pipeline patterns, see [BACKEND.md](BACKEND.md).

---

## Flow

### Save for offline

1. User taps the save icon on an episode-picker row → `saveForOffline(itemId, filePath, fileName, fileLabel)`.
2. Frontend opens `#offlineSaveModal` and POSTs `/api/library/{id}/offline-prepare {file_path}`.
3. Backend `_ffprobe_codec` reads codec info via ffprobe.
   - **Fast path** (`_safari_compatible`): video is `h264`/`hevc`, audio is `aac`/`mp3`, container is `.mp4`/`.m4v`/`.mov`. Returns `{ready:true, video_url}` pointing at the existing `/api/library/{id}/download` URL. No file is created.
   - **Remux** (`_can_remux`): codecs are already compatible but the container isn't MP4. ffmpeg is run with `-c copy -bsf:a aac_adtstoasc -movflags +faststart`. Output → `.offline_cache/<sha>.mp4`.
   - **Transcode**: codecs are incompatible. ffmpeg with `libx264 veryfast crf 23` + `aac 160k`. Slow.
4. While a job is in progress, the client polls `/api/library/offline-job/{job_id}` every 1.5 s for `{status, progress}`. Progress is approximated by output-file growth; an exact percent isn't important for the UI.
5. Once the server returns `video_url`, the client `fetch`s it with a `ReadableStream` reader (so it can show byte-progress) and stores the resulting `Blob` in IndexedDB.
6. The client also fetches each sidecar subtitle (`/api/library/{id}/subtitle?file=...`) and the per-file `skip_data` (`/api/library/{id}/skip-data?file_path=...`) and stores them in the same IDB record.

### Play offline

1. User taps Play on an episode → `playLibraryWithChooser(itemId, files, seekTo, label)`.
2. If `navigator.onLine === false` and the first file is saved offline → `lpPlay()` directly.
3. Else if the first file is saved offline → open `#playChooserModal` ("On TV (VLC)" vs "On This Device").
4. Else → fall through to the existing `playLibraryFiles` (VLC pipeline).
5. `lpPlay` walks the playlist to the first saved-offline file, loads it via `_lpLoadIndex`, attaches sidecar `<track>` elements, and starts playback.
6. There is one `<video id="lpVideo">` inside `#localPlayer`. The container toggles between fullscreen mode (default) and a corner tile via the `.lp-tiny` class — pure CSS, no DOM moves and no video reload. `lpMaximize`/`lpMinimize` toggle the class; `lpStop` removes both `.lp-active` and `.lp-tiny`.

### Skip-intro / credits

`lpEvaluateSkipOffer(t)` runs on every `timeupdate` and mirrors backend `_maybe_emit_skip_offer`:
- Intro window: `start - 2 ≤ t < end - 2`. Show "Skip Intro" button.
- Credits window: `t ≥ credits_start - 1`. Show "Skip Credits" / "End" depending on whether there's a next file.
- Dismissed offers add `<filePath>#intro` / `#credits` to `lp.skipDoneFor` so they don't re-emit.

### Watch progress

`#lpVideoFull`'s `timeupdate` calls `saveProgress(itemId, filePath, posSec, durSec)` at most once every 15 s (matches `vlc_progress_tracker`). Online → POST `/api/library/{id}/progress`. Offline → push to the IndexedDB `outbox` store.

The window's `online` event fires `outboxFlush()`, which drains the outbox and POSTs each entry. Successful POSTs delete the row.

### Page-shell offline boot

`static/sw.js` caches the app shell (`/`, `/index.html`, `/manifest.json`, the Tailwind CDN) on install. Read-only library APIs (`/api/library`, `/api/library/{id}/files`, `/api/library/{id}/skip-data`, `/api/library/{id}/subtitle`, `/api/profiles`) use stale-while-revalidate so the offline player has metadata it needs.

Navigation requests are network-first with a fallback to the cached `/`. When `remote.local` DNS fails, the SW serves the cached HTML — the user is dropped on the dashboard with all their saved-offline episodes visible and playable.

---

## Storage budget

iOS Safari grants ~60% of device free space to the origin via the Storage API. Big movies are fine; the 50 MB legacy quota is gone on iOS 13+.

The backend `.offline_cache/` directory has no automatic eviction. Cache keys are `sha256(path | mtime | size)[:24]` so re-encoding a source file invalidates the cache entry; deleting a library item leaves orphans that must be removed manually.

---

## Things that are **not** offline-safe

- The Search tab — depends on Jackett, which depends on the host. Entirely network-only.
- New library downloads (qBit) — still need the host (and VPN).
- Admin panel — auth + Jackett + ffmpeg jobs all require the host.
- VLC playback — needs the host.

---

## See also

- [FRONTEND.md](FRONTEND.md#handoff-to-device-offline-playback) — JS function reference for `lp*` / `osm*` / `pc*`
- [BACKEND.md](BACKEND.md#offline--handoff-to-device) — `_ffprobe_codec`, `_run_offline_job`, etc.
- [API.md](API.md#handoff-to-device-offline-playback) — endpoint signatures
- [GOTCHAS.md](GOTCHAS.md#offline--handoff-to-device) — Safari MKV, blob URL leaks, SW scope
