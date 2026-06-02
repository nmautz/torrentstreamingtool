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
  `#lpAudioSelect`, `#lpSubSelect`, the `#lpPrevEpBtn` / `#lpNextEpBtn`
  episode-nav buttons + `_lpRenderNavButtons` / `_lpWarmNextEp`)
- The per-row **Prep** button in the episode picker, `prepForStreaming`,
  `prepFileState`
- The bulk **Prep for Streaming** button on library cards
  (`prepItemForStreaming`, `/prep-all`)
- The prep **lag warning** (`#prepWarnModal`, `confirmStreamPrepWarning`), the
  **Pause/Resume** controls on `#globalPrepBar` (`/api/offline-prep/pause` +
  `/resume`, `_pause_prep` / `_resume_prep`), or **automatic auto-prep**
  (`auto_prep_loop`, `/api/admin/overnight-prep`, `/api/admin/idle-prep`) — see the
  [Pause / resume + auto-prep](#pause--resume--auto-prep) section
- The play chooser (`#playChooserModal`, `playLibraryWithChooser`, `pcChoose`)
- The **Handoff** (both directions): TV→device (`handoffToDevice`, `#handoffBtn`,
  `#fcHandoffBtn`) and device→TV (`lpHandoffToVlc`, the local player's **To TV** button)
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
    master.m3u8           ← top-level playlist (video variants + alternate audio)
    video.m3u8            ← original (source-resolution) video rendition
    init_video.mp4        ← fmp4 init segment (EXT-X-MAP) for the original rendition
    seg_video_00001.m4s   ← 6 s fmp4 segments
    seg_video_00002.m4s
    …
    video_720.m3u8        ← ABR down-rung (only when source height > 720)
    init_video_720.mp4
    seg_video_720_00001.m4s
    …
    video_480.m3u8        ← ABR down-rung (only when source height > 480)
    init_video_480.mp4
    seg_video_480_00001.m4s
    …
    audio_0.m3u8          ← per-audio-track rendition (default=YES on idx 0)
    init_audio_0.mp4      ← fmp4 init segment for audio rendition 0
    seg_audio_0_00001.m4s
    …
    audio_1.m3u8          (when source has multiple audio tracks)
    …
    sub_0.vtt             ← standalone WebVTT sidecar per text sub (NOT in manifest)
    sub_1.vtt
    …
    meta.json             ← track display info for the UI
```

> **Subtitles are NOT in the HLS manifest.** ffmpeg's HLS muxer cannot package
> multi-track WebVTT — a single inline subtitle works, but two or more (as their
> own `s:N,sgroup:…` variants) fail with `No streams to mux were specified` /
> `Could not write header`, for both `fmp4` and `mpegts`. Since every real MKV
> has many subtitle tracks, in-manifest subs meant prep *always* failed (fixed
> in v3.2.0). Instead each text sub is emitted as a standalone `sub_<i>.vtt` in
> the same single ffmpeg pass; the player attaches them as `<track>` children.
> See [GOTCHAS.md](GOTCHAS.md).

`meta.json` carries the video ABR ladder (`videos: [{idx, name, height,
label}]`, master-playlist order, idx 0 = original), the human labels for each
audio rendition (`{idx, playlist, label, language, default}`) and each subtitle
(`{idx, file, label, language, title}` where `file` is the bundle-relative
`sub_<i>.vtt`), plus a `skipped_image_subs` list noting PGS/VOBSUB tracks that
couldn't be included (they need image-OCR or burn-in, neither of which is
implemented). The UI reads `meta.json` via `/offline-prepare` to populate the
audio/subtitle dropdowns — it never re-probes the bundle. The **quality** menu
is instead built from hls.js `levels` (the master-playlist parse), so it never
depends on `videos[]` ordering; `videos[]` is informational (admin/API).

Cache key (`_offline_cache_key`) is
`sha256(OFFLINE_CACHE_VERSION | path | mtime | size)[:24]`. Re-encoding the
source invalidates the bundle. `OFFLINE_CACHE_VERSION = "v7-hls-abr"`; bumping it
forces every bundle to be rebuilt on next prep (old `<sha>/` dirs become
orphans, listed in Admin → Offline Cache for one-click purge).

> **Not available on macOS hosts.** When the server runs on macOS, ffmpeg /
> ffprobe (children of the non-GUI Python process) are blocked by TCC from
> reading the user's `~/Downloads` / `~/Desktop` / `~/Documents`, so every prep
> aborts at the probe step with `Operation not permitted`. The prep endpoints
> short-circuit with a clear error, `state_snapshot` returns
> `hls_available: false`, and the dashboard hides all Prep / On-Device controls
> (`no-hls` body class) and routes the play chooser straight to VLC. VLC ("On
> TV") is unaffected — it's a separate, individually TCC-granted app.

---

## ffmpeg invocation

`_build_hls_ffmpeg_args` in `main.py` constructs the command. The shape:

```
# cwd = the bundle's .part/ dir; EVERY output below is a BARE filename.
# Source video is mapped ONCE PER LADDER RUNG (here: original + 720p + 480p).
ffmpeg -y -progress pipe:1 -nostats \
  -thread_queue_size 1024 -rtbufsize 64M -i /abs/path/src.mkv \
  -map 0:v:0 -map 0:v:0 -map 0:v:0 -map 0:a:0 -map 0:a:1 \
  # v:0 = original (copy when browser-safe H.264, even with NVENC present):
  -c:v:0 copy \
  # v:1 = 720p down-rung (always transcodes; CPU scale feeds NVENC if present):
  -filter:v:1 scale=-2:720 -c:v:1 libx264 -preset veryfast -crf:v:1 23 \
  -maxrate:v:1 3000k -bufsize:v:1 6000k -pix_fmt:v:1 yuv420p -profile:v:1 high -level:v:1 4.1 \
  # v:2 = 480p down-rung:
  -filter:v:2 scale=-2:480 -c:v:2 libx264 -preset veryfast -crf:v:2 23 \
  -maxrate:v:2 1200k -bufsize:v:2 2400k -pix_fmt:v:2 yuv420p -profile:v:2 high -level:v:2 4.1 \
  -c:a aac -b:a 160k -ac 2 \
  -f hls -hls_time 6 -hls_playlist_type vod \
  -hls_segment_type fmp4 -hls_flags independent_segments \
  -hls_fmp4_init_filename "init_%v.mp4" \
  -hls_segment_filename "seg_%v_%05d.m4s" \
  -master_pl_name master.m3u8 \
  -var_stream_map "v:0,agroup:aud,name:video \
                   v:1,agroup:aud,name:video_720 \
                   v:2,agroup:aud,name:video_480 \
                   a:0,agroup:aud,name:audio_0,language:eng,default:yes \
                   a:1,agroup:aud,name:audio_1,language:jpn" \
  "%v.m3u8" \
  -map 0:s:0 -c:s webvtt -f webvtt "sub_0.vtt" \
  -map 0:s:1 -c:s webvtt -f webvtt "sub_1.vtt"
```

Note the two output groups: the HLS bundle (video variants + audio) comes
first, then one extra WebVTT output file per text subtitle. Both are produced in
the **single** ffmpeg pass — no second invocation. The ladder rungs come from
`_hls_video_variants(info)` — original always, `video_720`/`video_480` only when
the source is taller than that rung (so a ≤480p source emits one variant and the
player shows no quality menu). With NVENC, the `-c:v:i` for each transcoded rung
becomes `h264_nvenc -preset medium -rc vbr -cq:v:i 23` (the CPU `scale` filter
still feeds it); the original rung copies when `_video_can_copy` regardless of
NVENC, so only the scaled down-rungs hit the encoder.

> **All outputs are bare filenames and ffmpeg runs with `cwd=<bundle .part dir>`**
> (`_run_offline_job` passes `cwd=str(tmp_dir)`). Only the `-i` source is an
> absolute path. This is load-bearing on Windows: ffmpeg derives the fmp4 init
> segment's directory by *parsing the playlist path*, and a backslash playlist
> path defeats that parse, dumping `init_video.mp4` into the server's working
> directory instead of the bundle → the player 404s it → fatal `fragLoadError`.
> A full path on `-hls_fmp4_init_filename` is **not** a fix (ffmpeg prepends the
> playlist dir and the encode dies). Bare names + cwd is the only portable shape.
> See [GOTCHAS.md](GOTCHAS.md).

Key decisions:

- **ABR ladder: Original + 720p + 480p.** The source video is mapped once per
  rung (`_hls_video_variants` caps the ladder at source height — no upscaling).
  All variants share one audio group, so the player switches video quality
  without re-fetching audio. Each down-rung gets a `scale=-2:<h>` filter (the
  `-2` keeps an even width for yuv420p) and a `maxrate`/`bufsize` VBV cap so the
  rendition is genuinely smaller and the master playlist's `BANDWIDTH` is
  realistic for ABR selection.
- **The original rung stream-copies when possible** — if source video is already
  H.264 / yuv420p with a browser-safe profile (anything other than Hi10P / 4:2:2
  / 4:4:4), `-c:v:0 copy` is used **even when NVENC is present** (decoupled from
  the encoder choice, since only the scaled down-rungs need to encode). The
  down-rungs always transcode (NVENC on GPU when available, else libx264).
- **Every audio is transcoded to AAC stereo.** MP4/HLS only reliably plays
  AAC/MP3/AC3/EAC3 in Safari, and Chrome/hls.js handles AAC universally.
  Source FLAC / Opus / DTS / TrueHD are downmixed to 2 channels and
  re-encoded at 160 kbps. Multi-channel preservation is not implemented;
  for 5.1 playback on TV, use VLC's path which reads the source MKV
  directly.
- **Text subs become standalone WebVTT sidecars** (`sub_<i>.vtt`), *not*
  in-manifest renditions — ffmpeg's HLS muxer can't package more than one
  WebVTT track. subrip / ass / ssa are transparently converted; ASS styling
  (karaoke, positioning, custom fonts) is **lost** — see
  [GOTCHAS.md](GOTCHAS.md) for the libass.js deferral. Image-based subs
  (`hdmv_pgs_subtitle`, `dvd_subtitle`, `dvb_subtitle`, `vobsub`) are filtered
  out by `_ffprobe_full` and noted in `meta.json:skipped_image_subs`.
- **No usable text sub ⇒ AI generation.** After a successful HLS encode,
  `_run_offline_job` calls `_ensure_stt_for`: if the source has no usable text
  subtitle (none, image-only, or — per the admin default-language setting — none
  matching), a whisper.cpp transcription job is queued (bulk, after the encode
  releases the shared slot). It writes a sidecar `<stem>.<lang>.ai.srt` next to
  the source, which then surfaces via `_list_sidecar_subs` (the `subs[]` field,
  also returned by the job-done response) and the on-device player attaches it as
  a `<track>`. The episode picker's per-row Prep and the **AI** button in the
  player both feed the same machinery. Full detail in [STT.md](STT.md).
- **6-second segments, fmp4, independent_segments.** Modern HLS defaults.
  Switching audio/sub mid-stream doesn't require an extra fetch.
- **The fmp4 init filename is templated explicitly** (`-hls_fmp4_init_filename
  init_%v.mp4`) **and every output is a bare name with ffmpeg run from
  `cwd=<bundle dir>`.** Left to ffmpeg's default the init's `%v` expansion
  doesn't reliably match the `#EXT-X-MAP:URI=` in each variant playlist, and on
  Windows a backslash playlist path misdirects the init file out of the bundle
  entirely — either way the player 404s the init segment → fatal `fragLoadError`
  with the manifest already parsed (so the dropdowns populate first, masking it
  as "loaded but won't play"). Bare names + cwd keep the EXT-X-MAP URI and the
  on-disk file in lock-step on every OS. See [GOTCHAS.md](GOTCHAS.md).
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

> **Per-profile title redaction.** `/api/offline-active` accepts `?profile_id=` and
> returns identical counts/progress/ETA to every caller, but if **any** active item
> is `admin_only` and the requester isn't admin or elevated, every entry's `title`
> is replaced with the literal string `"Library content"` and `item_id` is blanked.
> Redaction is all-or-nothing per response — selectively hiding only the restricted
> entries would itself reveal which one is hidden. The per-card chip is unaffected
> because `/api/library` already filters admin-only items out of restricted
> profiles' library views.

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

> **Interactive prep takes precedence over bulk.** A prep started by the user
> *now* — play-on-device (`_lpLoadIndex`) or the fullscreen **Prep for Device**
> tile (both POST `/offline-prepare {bulk:false}`) — must not sit behind
> overnight / idle / manual `/prep-all` bulk work, even when a bulk file is
> already mid-encode. Two pieces enforce this against the single concurrency slot:
> 1. **Preemption.** `_preempt_running_bulk()` (called from `offline_prepare` for
>    every interactive request) `terminate()`s the bulk encode currently holding the
>    slot via its `job["_proc"]` handle, tagging it `_preempted` so `_run_offline_job`
>    treats the non-zero rc as intentional and re-queues the file as `pending`
>    (restarts from scratch — HLS has no mid-file checkpoint) via `_requeue_offline_job`.
>    The global pause gate is **not** touched (distinct from `_pause_prep`).
> 2. **Deferral.** Bulk jobs park before competing for the slot while
>    `_interactive_hls_pending() > 0` (any `queue=="interactive"` HLS job that's
>    `pending`/`processing`), and a bulk job that wins the slot re-checks and yields
>    it back if an interactive prep appeared while it was queued. So queued bulk
>    waiters drain past the interactive job rather than racing it for the freed slot.
>
> Net effect: holding **Prep for Device** boots the in-flight bulk encode and starts
> your file within a tick; the booted bulk file resumes once interactive prep clears.

> **Staying responsive while prepping.** Three layers keep controls/UI/VLC-control
> snappy under heavy prep:
> 1. **The server runs at raised OS priority.** `_raise_own_priority()` (called
>    first in `lifespan`, so both the HTTP and HTTPS uvicorn processes do it) sets
>    `HIGH_PRIORITY_CLASS` on Windows / a negative `nice` on POSIX. The server is
>    I/O-bound, so this just lets its short request bursts preempt the encoder.
> 2. **All background CPU work runs BELOW normal**, so it can never starve the
>    server: prep ffmpeg via `_ffmpeg_nice_prefix` (`nice -n 10`) / `_FFMPEG_SUBPROCESS_KW`
>    (`BELOW_NORMAL_PRIORITY_CLASS`), and the Smart-Skip analyzer subprocesses via
>    `analyzer._lp` / `analyzer._LOWPRIO_KW` (needed because children would
>    otherwise *inherit* the server's HIGH priority). Net order: server ≫ VLC/qBit
>    (normal) ≫ prep/analyzer.
> 3. **Nothing blocks the event loop.** ffmpeg/ffprobe are subprocesses; the
>    recursive bundle FS ops (`shutil.rmtree`, `_dir_size_bytes`) use
>    `asyncio.to_thread`; and the fan-out endpoints (`/prep-all`, the overnight
>    `_enqueue_library_prep`) `await asyncio.sleep(0)` between files (the per-file
>    `_maybe_start_prep_job` is synchronous).
>
> The remaining slowness under prep is pure CPU contention, which the lag warning
> + Pause/Resume + overnight scheduling address.

> **Debugging a failed conversion.** The UI only shows a short stderr tail.
> `_run_offline_job` logs the full ffmpeg command, return code, elapsed time,
> and the last 300 lines of ffmpeg stderr to **`logs/hls.log`** via the
> `streamlink.hls` logger. Start there for any "prep failed / conversion
> died seconds in" report. See [BACKEND.md § Logging](BACKEND.md#logging).

### 2. Play on this device

1. User taps Play (or the "📱 On Device" button on a library card). The
   flow goes through `playLibraryWithChooser`, which opens
   `#playChooserModal` — **On TV (VLC)** vs **On This Device**.
2. "On This Device" calls `lpPlay(itemId, files, seekTo, label)`. The
   player sets `lp.itemId/playlist/pi`, applies the `.lp-active` class to
   `#localPlayer`, and calls `_lpLoadIndex(seekTo)`. A **single-file** `files`
   (per-episode Play / Resume / one-file On Device) is expanded to the item's
   **full ordered file list** by fetching `/api/library/{id}/files`
   (season/episode-sorted) positioned at the chosen file, so Prev/Next span the
   whole series; multi-file queues (selected episodes / Play All / handoff tail)
   are kept verbatim. (The `/api/library` list only carries `first_file`, not the
   full `files` array, hence the extra fetch — this is why Prev/Next worked on
   the TV but not on-device until v4.17.3.)
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
     Safari plays HLS natively, exposing `AudioTrackList` for audio
     switching. Wait for `loadedmetadata` → populate dropdowns. (Subtitles
     are `<track>` children, not in-band, on every engine — see step 5.)
5. Track switching:
   - **Quality** (ABR, hls.js only): `_lpRenderTrackRows` builds the **Res**
     dropdown from `lp.hls.levels` (sorted high→low) as `Auto` + each
     resolution; `lpSetQuality(idx)` sets `lp.hls.currentLevel` (`-1` = Auto/ABR,
     a level index pins that rung — a brief rebuffer on switch is expected).
     Quality is **session-only** — not persisted via `/local-tracks`, since the
     right rung is connection-dependent and Auto is the sensible default. Safari
     native HLS auto-adapts among the variants but exposes no reliable manual
     level API, so its Res row stays hidden (auto-only).
   - **Audio** (in-manifest): hls.js path sets `hls.audioTrack = idx`; Safari
     native sets `video.audioTracks[i].enabled = (i===idx)`.
   - **Subtitles** (`<track>` children): `_lpLoadIndex` appends one `<track>`
     per bundle `sub_<i>.vtt` then per on-disk sidecar, recording each in
     `lp.subTracks` as `{el, key}` in dropdown order (key = `"i"` for bundle,
     `"sidecar:i"` for on-disk). `_lpApplySubIdx(idx)` just sets
     `tr.el.track.mode` to `"showing"` on the matching key and `"disabled"`
     elsewhere — identical for hls.js and Safari, since these are native
     `<track>`s the browser renders independent of the MSE pipeline.
   - Each switch POSTs to `/api/library/{id}/local-tracks` with the new
     pick so it persists across sessions (on-disk sidecar picks save as -1).
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

### 5. Auto-advance + manual Prev/Next + next-episode warm-prep

When `<video>` fires `ended`, `_lpAdvanceOrEnd` saves a final 100% progress
write, increments `lp.pi`, and calls `_lpLoadIndex(0)`. That re-runs the
prep flow for the next file. If the next episode has already been prepped,
playback resumes within a network round-trip; otherwise the user sees the
same "Building stream…" overlay.

**Manual Prev/Next.** The player header (`.lp-chrome`, so hidden in tiny mode)
has `#lpPrevEpBtn` / `#lpNextEpBtn` — **hold-to-activate** (`_holdStart`, the
same 0.5 s gesture as Stop / the TV controls). `lpPrevEp` / `lpNextEp` →
`lpNavEp(±1)` persists the *current* position (not 100% — the user is leaving
mid-episode by choice, `t ≥ 5` guard), moves `lp.pi`, and calls
`_lpLoadIndex(0)`. The hold fires **regardless of whether the target is
prepped**; an un-prepped neighbour just shows the "Building stream…" overlay
while it preps on demand. `_lpRenderNavButtons` shows/hides each button for the
current `lp.pi` and paints its square prep-readiness dot from `prepFileState`:
**green** = ready to stream, **amber** = prepping, **gray** = not prepped yet.

**Next-episode warm-prep.** `_lpWarmNextEp` (fired from `_lpLoadIndex` once the
current file is ready) kicks off an **interactive** HLS prep of `lp.playlist[lp.pi+1]`
so auto-advance / a Next hold resumes instantly instead of cold-encoding. It's the
on-device counterpart to the VLC `_play_prep_chain` (§ *Auto-prep on play*) — on-device
playback never calls `play_library_item`, so the server-side chain doesn't cover it.
Interactive (no `bulk` flag) so it bypasses the bulk pause gate and preempts bulk
work — the user is actively watching the series. Fire-and-forget: it updates
`prepFileState` + the nav dot as it polls `/offline-job/{id}`, and on any failure
(error / paused / network) leaves the file un-prepped for the on-demand path to
handle. No-op on macOS (`hlsAvailable` false).

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
tile are shown only then. Both are **hold-to-activate** (0.5 s `.hold-btn` fill,
same as Stop) so an accidental tap can't pull playback off the TV. Guarded by
`withInflight("handoff")`.

The **footer Device button** is **prep-gated**: it greys out (`.handoff-disabled`)
with a "Not prepped for on-device streaming" note when the current VLC file has no
`.offline_cache` MP4 and isn't Safari-native — otherwise the handoff would stop
the TV and sit in a long transcode. `_handoffReadyState` resolves readiness from
`prepFileState` (instant) or a per-file `GET /prep-status` check
(`_maybeRefreshHandoffReady`); it flips to active automatically once the file is
prepped (episode-picker Prep or a card's Prep for Streaming). Tapping while not
prepped shows a toast instead of acting.

The **fullscreen To-Device tile** (`#fcHandoffBtn`) is richer — instead of greying
when not prepped, it offers **hold-to-prep in place**. `_renderFcHandoff(s)` paints
four states: **ready** ("Play To Device", hold → `handoffToDevice`); **not-ready**
("Prep for Device?", hold → `prepCurrentForDevice`); **prepping** ("Prepping 42%"
with a `#fcHandoffBar` fill, driven by `app._fcPrepPct`); and **unknown/macOS**
(neutral, or the old greyed "Not prepped" note when HLS is unavailable).
`fcDeviceTileHold` is the hold dispatcher. `prepCurrentForDevice` POSTs
`/offline-prepare {bulk:false}` (interactive — bypasses the global pause gate and
the idle-prep kill so the encode starts **while VLC keeps playing the TV**), polls
`/offline-job/{id}` for progress, then `_finishFcPrep` flips the tile to
"Play To Device". See [FRONTEND.md](FRONTEND.md) for the readiness state machine.

### 7. Handoff to VLC (device → TV)

`lpHandoffToVlc(btn)` is the mirror image — it pushes the on-device play back
onto the TV:

1. Captures the local `<video>` `currentTime` and the remaining playlist tail
   (`lp.playlist.slice(lp.pi)`).
2. Calls `lpStop()` (which flushes the current position to the server and tears
   down the device player).
3. Calls `playLibraryFiles(itemId, tail, capturedTime, label)` → `POST
   /api/library/{id}/play` with `seek_first_to`. VLC plays the **original source**
   seeked to the same moment (transcode preserves the timeline, so the device's
   cached MP4 and the source share timestamps), so playback resumes on the same
   frame.

The **To TV** button lives in the local player's fullscreen header (next to
Stop); it's part of `.lp-chrome`, so it's hidden in tiny mode (maximize first).
Guarded by `withInflight("handoff_vlc")`.

---

## Pause / resume + auto-prep

Prep is CPU-heavy enough to make the host laggy, so the work is interruptible
and can be scheduled for the small hours **or** gated on host idleness. Four
pieces:

### Lag warning (client)

`confirmStreamPrepWarning()` shows `#prepWarnModal` the first time a user
triggers an explicit prep in a session — the per-item **Prep for Streaming**
(`prepItemForStreaming`) or a per-row **Prep** (`prepForStreaming`). It resolves
a `Promise<bool>` and remembers the acknowledgement for the session
(`_prepWarnAcked`). The interactive **play-on-device** path (`_lpLoadIndex`)
does **not** warn — the user wants playback now.

### Global pause gate (server)

Every prep job carries a `queue` field: `"bulk"` (per-item / per-row "prep for
later" + overnight) or `"interactive"` (play-on-device). `state.prep_paused`
gates **bulk** jobs only. The gate lives at the top of `_run_offline_job`, just
after the semaphore is acquired:

- A bulk job that reaches the head of the queue while paused marks itself
  `"paused"` and **exits its task** — crucially *releasing* the single
  `OFFLINE_JOB_CONCURRENCY` slot so an interactive play-on-device prep can still
  run while bulk prep is held. Paused jobs are re-spawned (fresh
  `_run_offline_job` tasks) by `_resume_prep()`.
- `_pause_prep(kill)` sets the flag. `kill=False` ("Finish current file, then
  stop") lets the in-flight encode complete; the next bulk job then hits the gate
  and parks. `kill=True` ("Stop now") `terminate()`s the running ffmpeg via the
  handle stashed on `job["_proc"]` and marks `job["_paused_kill"]` so the
  non-zero return code is treated as an intentional pause (re-queued, not an
  error). A killed file restarts from scratch on resume — HLS prep has no
  mid-file checkpoint. Interactive encodes are never killed.

Endpoints (both **non-admin**, exposed in the global prep bar): `POST
/api/offline-prep/pause {kill}` and `POST /api/offline-prep/resume`.
`/api/offline-active`, `/prep-status`, and `state_snapshot` all surface the
paused state so the UI can show "Prep paused" + a Resume button.

### Auto-prep (server, admin-configured)

A single background loop, `auto_prep_loop` (registered in `lifespan`), drives
**two independent triggers** that both auto-prep the **whole un-prepped
library**. One loop owns the decision because both write the shared
`state.prep_paused` gate and would otherwise fight. The combined question each
tick (every 15 s) is:

```
want = overnight_window_open  OR  (idle_prep_enabled AND host idle ≥ idle_minutes)
```

`state.auto_prep_engaged` is the in-memory edge flag:

- **Rising edge** (`want` and not engaged) → `_resume_prep()` (clears any pause +
  re-spawns paused jobs) then `_enqueue_library_prep()` queues a bulk job for
  every un-prepped video file (idempotent).
- **Falling edge** (engaged and not `want`) → pause, with the **kind** decided by
  *why* it stopped:
  - **idle-prep enabled and the box is now in use** ⇒ `_pause_prep(kill=True)` —
    the in-flight encode is terminated immediately for instant responsiveness;
    it restarts from scratch on the next idle stretch (HLS prep can't
    checkpoint). Activity always wins, even over the overnight `on_end` mode.
  - else, overnight `on_end == "continue"` ⇒ no pause, the queue runs to
    completion past the window.
  - else (overnight window closed, `on_end == "pause"`) ⇒ `_pause_prep(kill=False)`
    (graceful: the in-flight file finishes, the rest wait for the next window).

**Trigger 1 — Overnight window.** Config: `library.json → settings.overnight_prep`
(`enabled`, `start`/`end` HH:MM, `timezone`, `on_end ∈ {pause, continue}`). The
window may cross midnight (`_in_overnight_window` handles the wrap) and runs
regardless of activity — heavy load when nobody's watching.

**Trigger 2 — Idle.** Config: `library.json → settings.idle_prep` (`enabled`,
`idle_minutes` clamped 1–720). It reuses `_machine_in_use(idle_minutes*60)` — the
same helper the scheduled reboot uses — so "idle" means no VLC playback of real
content, no active stream, no running download, and no mutating HTTP interaction
within the window. That single window doubles as the activity detector: a fresh
interaction stamps `state.last_activity`, flipping `_machine_in_use` True within a
tick, which collapses `want` and triggers the kill=True pause.

> **Coexistence.** Both triggers can be enabled at once. During the overnight
> window prep runs regardless of activity; outside it, idle-prep governs (prep
> when idle, pause on activity). If idle-prep is on, it naturally extends prep
> past the overnight window whenever the box stays idle, and an activity pause
> overrides overnight Continue.

See [ADMIN.md § Overnight Stream Prep / § Idle Auto-Prep](ADMIN.md) for the
panels + endpoints.

### Auto-prep on play (server, admin-toggled)

A third, **play-driven** prep trigger, independent of the two idle/overnight
triggers above and **on by default** (`library.json → settings.play_prep.enabled`,
`_play_prep_cfg`). Every VLC library play (`play_library_item`) calls
`_maybe_start_play_prep(lib, item, profile_id, playlist, seek_sec)`:

1. If the viewer is resuming the current episode with **< `PLAY_PREP_TAIL_SECS`
   (300 s)** left — judged from the file's saved `duration_sec` (`_file_duration_sec`)
   minus `seek_sec` — the current episode is dropped from the list (prepping the one
   they're about to finish is wasted work) and the chain starts at the next episode.
2. Any prior chain is cancelled (`state.play_prep_task`) so only the series being
   watched is prepped ahead; then a new `_play_prep_chain(item_id, files)` task is
   spawned.

`_play_prep_chain` walks the files **one at a time** — it starts an interactive
prep for a file (`_start_interactive_prep_job`), waits for that job to reach
`done`/`error`, then moves to the next — so the episode most likely to be reached
next is always prepped first (and a long series doesn't fan out 50 ffmpegs).
`_start_interactive_prep_job` is the no-HTTP sibling of the queue-jumping branch in
`offline_prepare`: it coalesces with an existing job (promoting it to interactive),
else spawns a fresh `queue:"interactive"` job, and preempts any in-flight bulk
encode.

**Why `interactive` is load-bearing here.** The requirement is that this prep runs
*regardless of the idle/overnight settings and live user activity*. Interactive
jobs satisfy that for free: the pause gate in `_run_offline_job` only parks `bulk`
jobs, `_pause_prep(kill=…)` only terminates `bulk` encodes, and `_activity_kick`
calls `_pause_prep` — so none of them can stop a play-prep chain. The trade-off is
that, like all interactive prep, it isn't stoppable from the non-admin Pause/Resume
control and it makes `bulk` (overnight/idle/manual) prep defer while the chain has
work pending. Cancelling the chain task only stops *further* enqueues; the ffmpeg
already running finishes. Disabled (and the chain never starts) when `HLS_AVAILABLE`
is false (macOS).

See [ADMIN.md § Auto-Prep on Play](ADMIN.md) for the panel + endpoints.

---

## State + storage

The server keeps `.offline_cache/<sha>/` directories indefinitely; the Admin
→ Offline Cache tab ([docs/ADMIN.md](ADMIN.md)) lists per-item totals,
per-file deletes, a "delete all for this item" button, and a one-click
orphan purge. Storage cost: an HLS bundle shares the video segments across
audio renditions, so total cost is roughly `sum(video variants) + sum(audio at
160 kbps stereo) + subs (tiny)`. Since `v7-hls-abr` the video term is the ABR
ladder (Original + 720p + 480p, capped at source height), which is ~1.6–1.9× the
original-only video — e.g. a ~1 GB episode bundle becomes ~1.7 GB, and a 4 GB,
3-audio movie ~7 GB. The down-rungs also mean the video always transcodes (the
original rung still copies when compatible), so prep takes longer — much faster
with NVENC than libx264.

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
