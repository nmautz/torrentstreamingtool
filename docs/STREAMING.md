# Stream to Device

End-to-end flow for "play any library episode in this device's browser via
HTTP-range streaming from the host". Lives across `main.py` (`/offline-prepare`
endpoint + `.offline_cache/`) and `static/index.html` (the per-row Prep button
and the `<video>`-based local player).

This file replaced the earlier `docs/OFFLINE.md` after Milestone 13 retired the
download-to-device "Handoff" feature. The IndexedDB/service-worker history is
captured at the bottom of this doc for reference; the live system is much
smaller and simpler.

---

## When to use this doc

Read this when changing anything related to:

- `/api/library/{id}/offline-prepare`, `/offline-job/{id}`, `/offline-cache/{name}`
- `/api/library/{id}/subtitle` / `/skip-data` consumption by the local player
- The local player UI (`#localPlayer`, `#lpVideo`, `#lpPreparing`)
- The per-row **Prep** button in the episode picker, `prepForStreaming`, `prepFileState`
- The bulk **Prep for Streaming** button on library cards (`prepItemForStreaming`, `/prep-all`)
- The play chooser (`#playChooserModal`, `playLibraryWithChooser`, `pcChoose`)
- The TV→device **Handoff** (`handoffToDevice`, `#handoffBtn`, `#fcHandoffBtn`)
- `saveProgress`, `_lpFlushProgress`, the `pagehide`/`visibilitychange` flush

For player-style UI changes that don't touch streaming logic, see
[FRONTEND.md](FRONTEND.md). For backend pipeline patterns, see
[BACKEND.md](BACKEND.md).

---

## Flow

### 1. Server-side preconvert (Prep)

The `.offline_cache/<sha>.mp4` cache is the heart of streaming. Two ways to
populate it:

- **Library card bulk** — Click **Prep for Streaming** on a library card. The
  frontend POSTs `/api/library/{id}/prep-all`, which iterates every video file
  in the item and either:
  - marks it `ready_native` (fast-path Safari MP4 — no work needed)
  - finds an existing `.offline_cache/<sha>.mp4` and returns `cached`
  - spawns a remux/transcode job
  Status chip below the title polls `/api/library/{id}/prep-status` every 3 s.
  The global pill `#globalPrepBar` (top-right, amber) polls
  `/api/offline-active` every 3 s while jobs exist (8 s idle) so the indicator
  survives page reloads.

- **Per-file Prep** — Each row in the episode picker has a Prep button. It
  POSTs `/api/library/{id}/offline-prepare {file_path}`, then polls
  `/offline-job/{id}` until `done`. State for the button is mirrored in
  `prepFileState: Map<offKey, "prepping"|"ready">`, which is also refreshed
  from `/prep-status` whenever the picker opens or `/prep-all` runs.

> **Server-CPU note.** `_run_offline_job` caps ffmpeg with
> `-threads {OFFLINE_FFMPEG_THREADS}` (currently 2) on the libx264 path. A
> global `asyncio.Semaphore(OFFLINE_JOB_CONCURRENCY)` (default 1) gates the
> coroutine so a /prep-all on a 77-file pack queues files instead of spawning
> 77 simultaneous ffmpegs. NVIDIA NVENC is used automatically when the host
> has a Pascal-or-newer GPU and an NVENC-enabled ffmpeg build (see the comments
> around `_has_nvenc` in `main.py`).

### 2. Play on this device

1. User taps Play (or the "📱 On Device" button on a library card). The flow
   goes through `playLibraryWithChooser`, which (when the host is reachable)
   opens `#playChooserModal` — **On TV (VLC)** vs **On This Device**.
2. "On This Device" calls `lpPlay(itemId, files, seekTo, label)`. The player
   sets `lp.itemId/playlist/pi`, applies the `.lp-active` class to
   `#localPlayer`, and calls `_lpLoadIndex(seekTo)`.
3. `_lpLoadIndex` POSTs `/api/library/{id}/offline-prepare {file_path}`.
   - If `ready: true` → grab `video_url` directly.
   - If `ready: false` → show the `#lpPreparing` overlay ("Remuxing… 42%") and
     poll `/api/library/offline-job/{job_id}` every 1.5 s until `status: done`.
4. Once a `video_url` is in hand, the player:
   - Sets `<video id="lpVideo">.src` to that URL. The browser issues HTTP Range
     requests (FastAPI/Starlette `FileResponse` supports Range natively), so
     seek-while-streaming works without any client-side help.
   - Wires `<track>` subtitle elements straight from the `subs[].url`
     entries in the prep response (each points at `/api/library/{id}/subtitle`,
     which serves WebVTT — SRT is converted on the fly).
   - Fetches `/api/library/{id}/skip-data?file_path=…` in parallel to populate
     `lp.skipData` for the skip-intro / skip-credits logic.
5. The container toggles between fullscreen overlay and a corner tile via the
   `.lp-tiny` class — pure CSS, no DOM moves. `lpMaximize` / `lpMinimize` flip
   the class; `lpStop` removes both `.lp-active` and `.lp-tiny`.

### 3. Skip-intro / credits

`lpEvaluateSkipOffer(t)` runs on every `timeupdate` and mirrors the backend
`_maybe_emit_skip_offer`:

- Intro window: `start - 2 ≤ t < end - 2`. Show "Skip Intro" button.
- Credits window: `t ≥ credits_start - 1`. Show "Skip Credits" / "End"
  depending on whether there's a next file.
- Dismissed offers add `<filePath>#intro` / `#credits` to `lp.skipDoneFor`.

The offer (`#lpSkipOffer`) renders only when the player is in full overlay
(`.lp-active`) — hidden by CSS in tiny mode. The same CSS rule hides
`#lpPreparing` in tiny mode.

### 4. Watch progress

`#lpVideo`'s `timeupdate` calls `saveProgress(itemId, filePath, posSec, durSec)`
at most once every 15 s (matches `vlc_progress_tracker`). `saveProgress` is a
single best-effort POST to `/api/library/{id}/progress`; failure is silent
because the next tick or flush will overwrite anyway.

To stop the resume position drifting up to ~15 s behind on tab close,
`_lpFlushProgress(useBeacon)` bypasses the throttle on every user-driven
exit/transition:

- `pause` and `seeked` → `fetch` POST (`useBeacon=false`)
- `visibilitychange` → hidden → `fetch` POST
- `pagehide` → `navigator.sendBeacon` (the only request type that reliably
  survives unload)

`lpStop` and `ended` flush via `saveProgress` directly.

There is no longer any offline-progress outbox — a single POST is the entire
write path. When the host is unreachable, that 15-second sample is lost; the
last persisted server-side value is still correct.

### 5. Auto-advance

When `<video>` fires `ended`, `_lpAdvanceOrEnd` saves a final 100% progress
write, increments `lp.pi`, and calls `_lpLoadIndex(0)`. That re-runs the prep
flow for the next file. If the next episode has already been prepped (or was
fast-path Safari-compatible), playback resumes within a network round-trip;
otherwise the user sees the same "Preparing for streaming…" overlay.

### 6. Handoff from VLC (TV → device)

`handoffToDevice(btn)` is a second entry point into `lpPlay` (the play chooser
is the first). It transfers a live VLC (TV) library play onto the requesting
browser, time-synced:

1. Captures VLC's **live** position from `GET /api/vlc/tracks` (`time`) — fresher
   than the ≤2 s-stale `app.vlc_time` snapshot, which is the fallback.
2. Slices the remaining-playlist tail from `app.library_playlist` starting at
   `app.library_current_file`, so on-device auto-advance continues the series.
3. Fires `POST /api/stop` (202; VLC teardown is backgrounded) **and** calls
   `lpPlay(itemId, tail, capturedTime, label)`. Because stop returns immediately,
   the device's prep/transcode overlaps the TV teardown. The resume seek is
   pinned to `capturedTime` (applied on `loadedmetadata`), so the device lands on
   the same frame no matter how long prep takes — VLC is stopped, so the position
   doesn't drift while the device prepares.

Needs `app.is_library_playback && app.library_item_id` (both published in
`state_snapshot()`); the footer **Device** button and fullscreen **To Device**
tile are shown only then. Guarded by `withInflight("handoff")`.

---

## State + storage budget

There is no client-side storage budget anymore. The server keeps
`.offline_cache/<sha>.mp4` files indefinitely; the Admin → Offline Cache tab
([docs/ADMIN.md](ADMIN.md)) lists per-item totals, per-file deletes, a "delete
all for this item" button, and a one-click orphan purge.

Cache keys are `sha256(VERSION | path | mtime | size)[:24]`, so re-encoding a
source file invalidates the cache entry; deleting a library item leaves orphans
on disk until the admin purges them.

---

## Things that are **not** stream-to-device

- The Search tab — depends on Jackett, which depends on the host. Entirely
  network-only.
- New library downloads (qBit) — still need the host (and VPN).
- Admin panel — auth + Jackett + ffmpeg jobs all require the host.
- VLC playback — needs the host.
- Truly-offline playback — gone. If the host is unreachable, neither the
  chooser nor `lpPlay` can do anything useful (and they say so via a toast).

---

## Historical notes

Through Milestone 11, the dashboard shipped a Service Worker (`/sw.js`), a Web
App Manifest (`/manifest.json`), and an IndexedDB store
(`streamlink-offline`) so the entire app could boot offline and a saved
episode could be played without the host. That worked in principle but the
IDB-blob save step turned out to be flaky on long files (chunked downloads
sometimes truncated silently around the 5-minute mark of playback even though
the foreground UI reported the save complete). Milestone 13 replaced the whole
download flow with HTTP-range streaming from the same `.offline_cache/` files,
deleted the IDB / outbox / save modal layers, and kept a one-shot
`/sw.js` whose only job is to call `registration.unregister()` and wipe every
cache it ever created so devices that PWA-installed the old build don't end up
pinned to a stale shell.

---

## See also

- [FRONTEND.md](FRONTEND.md) — JS function reference for `lp*` / `pc*` / `prep*`
- [BACKEND.md](BACKEND.md) — `_ffprobe_codec`, `_run_offline_job`, etc.
- [API.md](API.md) — endpoint signatures
- [GOTCHAS.md](GOTCHAS.md) — Safari MKV, HTTP Range, etc.
