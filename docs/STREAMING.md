# Stream to Device

End-to-end flow for "play any library episode in this device's browser via
HLS streamed from the host". Lives across `main.py` (the `/offline-prepare`
endpoint + `.offline_cache/<sha>/` HLS bundles) and `static/index.html` (the
per-row Prep button and the `<video>`-based local player powered by hls.js or
Safari native HLS).

This file replaced the earlier `docs/OFFLINE.md` after Milestone 13 retired
the download-to-device "Handoff" feature, and was rewritten again in
Milestone 16 when single-MP4 prep was replaced with per-source **HLS
bundles** that carry every audio track and every text subtitle from the
source MKV.

---

## When to use this doc

Read this when changing anything related to:

- `/api/library/{id}/offline-prepare`, `/offline-job/{id}`,
  `/offline-cache/<sha>/<file>`
- `/api/library/{id}/subtitle` (sidecar `.srt`/`.vtt`) or
  `/api/library/{id}/local-tracks` (persisted browser track picks)
- The local player UI (`#localPlayer`, `#lpVideo`, `#lpPreparing`,
  `#lpAudioSelect`, `#lpSubSelect`)
- The per-row **Prep** button in the episode picker, `prepForStreaming`,
  `prepFileState`
- The bulk **Prep for Streaming** button on library cards
  (`prepItemForStreaming`, `/prep-all`)
- The play chooser (`#playChooserModal`, `playLibraryWithChooser`, `pcChoose`)
- `saveProgress`, `_lpFlushProgress`, the `pagehide`/`visibilitychange` flush
- hls.js integration (`/vendor/hls.min.js`, `_ensureHlsLib`, `_lpDestroyHls`)

For player-style UI changes that don't touch streaming logic, see
[FRONTEND.md](FRONTEND.md). For backend pipeline patterns, see
[BACKEND.md](BACKEND.md). For HLS-specific footguns, see
[GOTCHAS.md](GOTCHAS.md).

---

## Output format

Every prepped source produces a directory `<sha>/` under `.offline_cache/`
with the following layout:

```
.offline_cache/
  <sha24>/
    master.m3u8           ← top-level playlist (variants + alternate audio/subs)
    video.m3u8            ← video rendition playlist
    seg_video_00001.m4s   ← 6 s fmp4 segments
    seg_video_00002.m4s
    …
    audio_0.m3u8          ← per-audio-track rendition (default=YES on idx 0)
    seg_audio_0_00001.m4s
    …
    audio_1.m3u8          (when source has multiple audio tracks)
    …
    sub_0.m3u8            ← per-text-sub rendition (WebVTT segments)
    seg_sub_0_00001.m4s
    …
    meta.json             ← track display info for the UI
```

`meta.json` carries the human labels (`{idx, label, language, default}`) for
each audio and subtitle rendition plus a `skipped_image_subs` list noting
PGS/VOBSUB tracks that couldn't be included (they need image-OCR or burn-in,
neither of which is implemented). The UI reads `meta.json` via
`/offline-prepare` to populate dropdowns — it never re-probes the bundle.

Cache key (`_offline_cache_key`) is
`sha256(OFFLINE_CACHE_VERSION | path | mtime | size)[:24]`. Re-encoding the
source invalidates the bundle. `OFFLINE_CACHE_VERSION = "v3-hls"`; bumping it
forces every bundle to be rebuilt on next prep (old `<sha>/` dirs become
orphans, listed in Admin → Offline Cache for one-click purge).

---

## ffmpeg invocation

`_build_hls_ffmpeg_args` in `main.py` constructs the command. The shape:

```
ffmpeg -y -progress pipe:1 -nostats \
  -thread_queue_size 1024 -rtbufsize 64M -i src.mkv \
  -map 0:v:0 -map 0:a:0 -map 0:a:1 -map 0:s:0 \
  [-c:v copy | -c:v libx264 -preset veryfast -crf 23 | -c:v h264_nvenc -preset medium -cq 23] \
  -pix_fmt yuv420p -profile:v high -level 4.1 \
  -c:a aac -b:a 160k -ac 2 \
  -c:s webvtt \
  -f hls -hls_time 6 -hls_playlist_type vod \
  -hls_segment_type fmp4 -hls_flags independent_segments \
  -hls_segment_filename "<out>/seg_%v_%05d.m4s" \
  -master_pl_name master.m3u8 \
  -var_stream_map "v:0,agroup:aud,sgroup:sub,name:video \
                   a:0,agroup:aud,name:audio_0,language:eng,default:yes \
                   a:1,agroup:aud,name:audio_1,language:jpn \
                   s:0,sgroup:sub,name:sub_0,language:eng" \
  "<out>/%v.m3u8"
```

Key decisions:

- **Video stream-copies when possible.** If source video is already H.264 /
  yuv420p with a browser-safe profile (anything other than Hi10P / 4:2:2 /
  4:4:4), `-c:v copy` is used. NVENC always transcodes — it doesn't honor
  source-compat checks.
- **Every audio is transcoded to AAC stereo.** MP4/HLS only reliably plays
  AAC/MP3/AC3/EAC3 in Safari, and Chrome/hls.js handles AAC universally.
  Source FLAC / Opus / DTS / TrueHD are downmixed to 2 channels and
  re-encoded at 160 kbps. Multi-channel preservation is not implemented;
  for 5.1 playback on TV, use VLC's path which reads the source MKV
  directly.
- **Text subs become WebVTT.** subrip / ass / ssa are transparently
  converted by ffmpeg. ASS styling (karaoke, positioning, custom fonts)
  is **lost** — see [GOTCHAS.md](GOTCHAS.md) for the libass.js deferral.
  Image-based subs (`hdmv_pgs_subtitle`, `dvd_subtitle`, `dvb_subtitle`,
  `vobsub`) are filtered out by `_ffprobe_full` and noted in
  `meta.json:skipped_image_subs`.
- **6-second segments, fmp4, independent_segments.** Modern HLS defaults.
  Switching audio/sub mid-stream doesn't require an extra fetch.
- **ffmpeg ≥ 4.3** is enforced via `_ffmpeg_version()` cache. Older builds
  fail-fast at prep start with a clear error message — multi-rendition
  `-var_stream_map` is unreliable on 4.0–4.2.

---

## Flow

### 1. Server-side prep

Two ways to populate the cache:

- **Library card bulk** — Click **Prep for Streaming** on a library card.
  The frontend POSTs `/api/library/{id}/prep-all`, which iterates every
  video file in the item and either:
  - finds an existing `<sha>/master.m3u8` and returns `cached`
  - spawns an HLS-prep job
  Status chip below the title polls `/api/library/{id}/prep-status` every
  3 s. The global pill `#globalPrepBar` (top-right, amber) polls
  `/api/offline-active` every 3 s while jobs exist (8 s idle) so the
  indicator survives page reloads.

- **Per-file Prep** — Each row in the episode picker has a Prep button. It
  POSTs `/api/library/{id}/offline-prepare {file_path}`, then polls
  `/offline-job/{id}` until `done`. State for the button is mirrored in
  `prepFileState: Map<offKey, "prepping"|"ready">`, which is also refreshed
  from `/prep-status` whenever the picker opens or `/prep-all` runs.

> **Concurrency.** `_run_offline_job` holds a global
> `asyncio.Semaphore(OFFLINE_JOB_CONCURRENCY)` (default 1) so a /prep-all
> on a 77-file pack queues files instead of spawning 77 simultaneous
> ffmpegs. The CPU path uses `-threads OFFLINE_FFMPEG_THREADS` (2);
> NVENC ignores it. See [GOTCHAS.md](GOTCHAS.md) for why the cap is 1.

### 2. Play on this device

1. User taps Play (or the "📱 On Device" button on a library card). The
   flow goes through `playLibraryWithChooser`, which opens
   `#playChooserModal` — **On TV (VLC)** vs **On This Device**.
2. "On This Device" calls `lpPlay(itemId, files, seekTo, label)`. The
   player sets `lp.itemId/playlist/pi`, applies the `.lp-active` class to
   `#localPlayer`, and calls `_lpLoadIndex(seekTo)`.
3. `_lpLoadIndex` POSTs `/api/library/{id}/offline-prepare {file_path,
   profile_id}`.
   - If `ready: true` → grab `master_url`, `audios[]`, `subtitles[]`,
     `saved_tracks{audio_idx, subtitle_idx}` directly.
   - If `ready: false` → show the `#lpPreparing` overlay
     ("Building stream… 42%") and poll
     `/api/library/offline-job/{job_id}` every 1.5 s until
     `status: done`. The done response carries the same fields.
4. Player engine selection:
   - **`Hls.isSupported()` true** (Chrome, Firefox, Edge, Android Chrome):
     instantiate hls.js, `attachMedia(<video>)`, `loadSource(master_url)`.
     Wait for `MANIFEST_PARSED` → populate dropdowns and apply saved
     track picks. Errors recoverable via `hls.recoverMediaError()`.
   - **Safari** (iOS + macOS): set `<video>.src = master_url` directly.
     Safari plays HLS natively, exposing `AudioTrackList` and
     `TextTrackList` for switching. Wait for `loadedmetadata` →
     populate dropdowns.
5. Track switching:
   - **hls.js path**: `hls.audioTrack = idx`, `hls.subtitleTrack = idx`.
   - **Safari native**: `for t of video.audioTracks: t.enabled = (i===idx)`;
     `for t of video.textTracks: t.mode = (i===idx) ? "showing" : "disabled"`.
   - Each switch POSTs to `/api/library/{id}/local-tracks` with the new
     pick so it persists across sessions.
6. The container toggles between fullscreen overlay and a corner tile via
   the `.lp-tiny` class — pure CSS, no DOM moves. `lpMaximize` /
   `lpMinimize` flip the class; `lpStop` removes both `.lp-active` and
   `.lp-tiny` and destroys the active hls.js instance via
   `_lpDestroyHls`.

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

`update_progress` preserves the file's existing `audio_track` /
`subtitle_track` (VLC ES IDs) **and** `local_audio_idx` /
`local_subtitle_idx` (HLS rendition indices) across writes, so a progress
write doesn't wipe either track-pref system.

### 5. Auto-advance

When `<video>` fires `ended`, `_lpAdvanceOrEnd` saves a final 100% progress
write, increments `lp.pi`, and calls `_lpLoadIndex(0)`. That re-runs the
prep flow for the next file. If the next episode has already been prepped,
playback resumes within a network round-trip; otherwise the user sees the
same "Building stream…" overlay.

---

## State + storage

The server keeps `.offline_cache/<sha>/` directories indefinitely; the Admin
→ Offline Cache tab ([docs/ADMIN.md](ADMIN.md)) lists per-item totals,
per-file deletes, a "delete all for this item" button, and a one-click
orphan purge. Storage cost vs. the old single-MP4 cache: an HLS bundle
shares the video segments across audio renditions, so total cost is roughly
`video + sum(audio at 160 kbps stereo) + subs (tiny)`. For a typical TV
episode (~1 GB MKV, one audio), the bundle is ~1 GB; for a 4 GB movie with
3 audios, ~4.5 GB total.

Cache keys are `sha256(VERSION | path | mtime | size)[:24]`. Re-encoding the
source invalidates the entry; deleting a library item leaves orphans on
disk until the admin purges them. Pre-`v3-hls` MP4 caches surface as
`kind: "legacy"` orphans and are purged the same way.

---

## Things that are **not** stream-to-device

- The Search tab — depends on Jackett, which depends on the host.
  Entirely network-only.
- New library downloads (qBit) — still need the host (and VPN).
- Admin panel — auth + Jackett + ffmpeg jobs all require the host.
- VLC playback — needs the host; reads the source MKV directly so all
  audio tracks / 5.1 channels / image subs work natively (the local
  browser player limitations don't apply to TV mode).
- Truly-offline playback — gone. If the host is unreachable, neither the
  chooser nor `lpPlay` can do anything useful.

---

## Historical notes

Through Milestone 11, the dashboard shipped a Service Worker (`/sw.js`), a
Web App Manifest (`/manifest.json`), and an IndexedDB store
(`streamlink-offline`) so the entire app could boot offline. That worked in
principle but the IDB-blob save step was flaky on long files. Milestone 13
replaced the whole download flow with HTTP-range streaming from a single
`.offline_cache/<sha>.mp4`, deleted the IDB layers, and kept a one-shot
`/sw.js` whose only job is to call `registration.unregister()`.

Milestone 16 (this rewrite) replaced single-MP4 prep with per-source HLS
bundles to support multi-audio and multi-subtitle in-browser playback. The
old `/api/library/offline-cache/{name}` endpoint is gone; the bundle file
endpoint is `/api/library/offline-cache/{cache_key}/{filename}` with strict
regex validation on both segments. The `OFFLINE_CACHE_VERSION` bump to
`v3-hls` orphans every pre-existing MP4 cache; they remain on disk under
the "legacy" orphan kind until purged.

---

## See also

- [FRONTEND.md](FRONTEND.md) — JS function reference for `lp*` / `pc*` / `prep*`
- [BACKEND.md](BACKEND.md) — `_ffprobe_full`, `_run_offline_job`, etc.
- [API.md](API.md) — endpoint signatures
- [GOTCHAS.md](GOTCHAS.md) — Safari MSE quirks, hls.js segment alignment,
  ffmpeg version floor, etc.
