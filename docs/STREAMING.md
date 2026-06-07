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
  (`auto_prep_loop`, `/api/admin/auto-prep`) — see the
  [Pause / resume + auto-prep](#pause--resume--auto-prep) section
- **On-demand (JIT) streaming** — `stream-ondemand`, the `_od_*` session manager,
  the `/api/library/ondemand/<key>/…` virtual-playlist + segment endpoints, and the
  client `lp.mode === "ondemand"` path. See [§ On-Demand](#on-demand-just-in-time-streaming)
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
  # `-hwaccel cuda` is inserted here (before -i) ONLY on the NVENC path when
  # something actually decodes — routes decode through NVDEC so the CPU isn't
  # the bottleneck. Transparent form (no -hwaccel_output_format): frames
  # auto-download for the CPU scale and unsupported codecs fall back to SW.
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
becomes `h264_nvenc -preset medium -rc vbr -cq:v:i 23`, fed by either a GPU
`scale_cuda` or a CPU `scale` filter depending on the offload tier (see the note
below); the original rung copies when `_video_can_copy` regardless of NVENC, so
only the scaled down-rungs hit the encoder.

> **GPU offload on the NVENC path — two tiers.** Plain NVENC offloads only the
> *encode*; the source was software-decoded and every down-rung software-scaled,
> pegging the CPU (80-90%) while the GPU idled (~50%, only the small ABR encodes).
> So when NVENC is in use *and* a rung actually decodes (not a single pure
> stream-copy), the builder routes decode — and, when it can, scaling — onto the
> GPU:
>
> - **All-GPU (`full_gpu`)** — chosen when (a) the build has the `scale_cuda`
>   filter (`_has_cuda_scale`, probed once), (b) the source is NVDEC-safe
>   (`_source_nvdec_safe`: h264/hevc/mpeg2/vc1/vp9 in 4:2:0 8/10-bit), **and (c)
>   the source must fully re-encode (`not copy_original`)** — i.e. there is NO
>   `-c:v copy` rung. Emits `-hwaccel cuda -hwaccel_output_format cuda
>   -extra_hw_frames 8` so decoded frames **stay in VRAM**, and every rung uses
>   `scale_cuda=-2:H:format=yuv420p` instead of software `scale` (no `-pix_fmt`,
>   which would force a host round-trip). Decode → scale → encode never leaves the
>   GPU: no per-frame GPU↔CPU copy, no CPU scaling.
> - **Transparent (`-hwaccel cuda` only)** — everything else: copyable H.264
>   sources (so the original stream-copies and only down-rungs encode), builds
>   without `scale_cuda`, or exotic/4:2:2/4:4:4/12-bit sources. NVDEC decodes,
>   frames auto-download to system memory for the CPU `scale`, and ffmpeg silently
>   falls back to **software** decode for any codec NVDEC can't handle.
>
> Why the three gates, and why no copy rung: pinning the decoder output to `cuda`
> removes ffmpeg's software-decode fallback (an unsupported source would
> *hard-fail*), and — the nastier failure — **mixing a `-c:v copy` rung with
> cuda-filtered rungs DEADLOCKS ffmpeg**: the copy stream races ahead while the
> muxer / NVDEC surface pool backs up, wedging at low CPU+GPU with no progress and
> no exit. Restricting `full_gpu` to the all-encode case sidesteps that entirely
> (and targets the worst CPU offender — h265 packs that re-encode all three rungs).
> Two more safety nets: `-extra_hw_frames 8` enlarges the surface pool the parallel
> `scale_cuda` branches draw from, and a **stall watchdog** in `_run_offline_job`
> (`GPU_STALL_TIMEOUT_SECS`) kills the encode if `out_time` stops advancing for 90s,
> so a residual deadlock auto-**retries once** on the transparent path. The
> optimisation can therefore never leave a file unpreppable (Windows is the primary
> target; a working prep matters more than a slightly warmer CPU).

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
- **Successful encode ⇒ Smart Skip fingerprinting.** After the bundle lands,
  `_run_offline_job` also calls `_ensure_analysis_for(src, item_id)` — the
  fire-and-forget sibling of the STT hook. This is the **only** trigger for
  intro/credits audio fingerprinting now (the old download-ready trigger was
  removed), so it runs on prepped content only. Non-blocking: it just schedules
  a per-series pass (at BELOW_NORMAL priority) and never holds up prep, and a
  failure never fails the bundle. Failed files don't auto-retry. Full detail in
  [ANALYZER.md § Trigger flow](ANALYZER.md).
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
     `"sidecar:i"` for on-disk). The on-disk sidecar list (`prep.subs`) comes
     from `_list_sidecar_subs`, which **aggressively discovers** subs via
     `_discover_local_subs` — next to the video *and* in `Subs/`-style folders
     (see [GOTCHAS.md](GOTCHAS.md)) — so subs the bundle never extracted still
     show up. It's returned in the not-ready/prepping response too, so disk subs
     don't depend on the HLS bundle being built. `_lpApplySubIdx(idx)` just sets
     `tr.el.track.mode` to `"showing"` on the matching key and `"disabled"`
     elsewhere — identical for hls.js and Safari, since these are native
     `<track>`s the browser renders independent of the MSE pipeline. Selecting a
     sidecar triggers the browser to fetch its `<track>` src — the
     `/subtitle` endpoint converts SRT/ASS/SSA → WebVTT **on demand** — and
     `_lpIndicateSubLoading` flashes a "Loading subtitles…" hint until that
     fetch lands (or errors).
   - Each switch POSTs to `/api/library/{id}/local-tracks` with the new
     pick so it persists across sessions. The pick travels as a resolvable
     **descriptor** `subtitle_sel` (`{off, lang, ai, name}`), saved per-file
     *and* per-series — so a sidecar/AI choice comes back on replay and on the
     next episode. (Pre-v4.27 the on-device save dropped every non-bundle pick,
     persisting `-1`/off — a chosen `.srt`/AI sub was never remembered.) On the
     next play the resolver (`_lpResolveSubSel`) matches the file's, then the
     series', descriptor against the live track list: `name` → `lang`+kind →
     any-kind in that language → lone-option.
   - **Late-sub upgrade.** When the default selection lands on an *auto-applied*
     AI sub (`lp.subAutoApplied`), `_lpStartSubUpgradePoll` polls
     `GET /api/library/{id}/subs` every 15 s for a real preferred-language sub
     that finished downloading after playback began; on one it rebuilds the
     sidecar `<track>`s (`_lpRebuildSidecars`), switches to it, toasts, and
     remembers the upgrade. A manual pick clears `subAutoApplied`. Gated on
     `subtitle_upgrade_late` (from `/api/state`). See [STT.md](STT.md).
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
`local_subtitle_idx` / `subtitle_sel` (HLS rendition indices + the subtitle
descriptor) across writes, so a progress
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

## On-Demand (just-in-time) streaming

The full-prep bundle above encodes an **entire** file before playback can start.
On-demand is the opposite trade-off — playback begins almost immediately and
segments are transcoded **just-in-time** as the player requests them (the
Jellyfin/Plex model). It's the path taken when a file **isn't fully prepped yet**:
`_lpLoadIndex` calls `/offline-prepare`, and if the bundle isn't `ready`, instead
of waiting out the full encode it switches to on-demand. An existing bundle always
wins (on-demand is a supplement, never a replacement).

### Server model (`main.py`, the "On-demand (just-in-time)" block)

- **Sessions.** One ffmpeg per `(source bundle key + audio track)` lives in
  `_od_sessions`, keyed by `_od_session_key(src, audio_idx)` (24-hex,
  `_CACHE_KEY_RE`-shaped). Each session owns a dir `.ondemand_cache/<key>/`, the
  source duration, the chosen audio index, the current `start_seg`, the running
  `proc`, a `last_access` stamp, and an `asyncio.Lock`. ffmpeg is **not** started at
  session creation — it's lazy, launched on the first segment fetch.
- **Virtual playlist.** `media.m3u8` is generated from the duration alone
  (`_od_media_playlist`) — `ceil(duration/OD_SEGMENT_SECS)` × `#EXTINF` entries
  naming `seg_<i>.ts`, ending with `#EXT-X-ENDLIST`. No encoding happens to produce
  it; the player believes the whole file already exists. `master.m3u8` is a one-line
  wrapper pointing at it.
- **JIT segment serve.** `GET …/seg_<n>.ts`: if on disk, serve. Else, under the
  session lock, decide — if the running encode covers `n` and is within
  `OD_LOOKAHEAD_SEGS` of it, just wait; otherwise (n is before `start_seg`, or far
  ahead of it = a seek) `_od_start_encode(session, n)` restarts ffmpeg seeked to `n`.
  Then the request is **held open** (async-polling the dir every 150 ms, capped at
  `OD_SEG_WAIT_TIMEOUT`) until the `.ts` lands, and served; `504` on timeout. The
  pending request **is** the browser's buffering spinner — that's the "loading while
  it generates that part" UX.
- **ffmpeg shape** (`_od_build_ffmpeg_args`): `-ss <start_seg*6>` **before** `-i`
  (fast keyframe seek, output PTS reset to 0), `-map 0:v:0 -map 0:a:<idx>`, video
  **always transcodes** (NVENC transparent tier when present, else `libx264
  -preset veryfast`) with `-force_key_frames expr:gte(t,n_forced*OD_SEGMENT_SECS)`,
  audio → AAC stereo, `-hls_segment_type mpegts -start_number <start_seg>
  -hls_segment_filename seg_%d.ts -hls_list_size 0`. All outputs are **bare names**
  run with `cwd=<session dir>` (same Windows-safe rule as the bundle path). Reuses
  `_ffmpeg_nice_prefix()` / `_FFMPEG_SUBPROCESS_KW` (below-normal priority) and
  `_has_nvenc()`.
- **Why mpegts + forced keyframes** (see [GOTCHAS.md](GOTCHAS.md)): TS segments are
  self-contained (no shared `EXT-X-MAP` init), so independently-seeked encodes never
  produce mismatched init segments the way fmp4 would. Forcing keyframes every
  `OD_SEGMENT_SECS` (with the PTS reset from input-seek) guarantees segment *N*
  covers exactly `[N*6,(N+1)*6)` — which is what makes the virtual playlist's timing
  correct and seeking land precisely. Stream-copy can't guarantee that boundary
  alignment, so it stays a full-prep-only win.
- **Lifecycle.** `_od_reaper` (a `lifespan` task) terminates ffmpeg + `rmtree`s the
  dir for sessions idle past `OD_SESSION_IDLE_SECS` (90 s) and caps the live count at
  `OD_MAX_SESSIONS`. Active playback refreshes `last_access` on every segment fetch,
  so a watching session is never reaped mid-stream. `POST …/close` (a sendBeacon on
  stop/unload) tears a session down promptly; the reaper is the backstop.
- **Background full prep.** `stream-ondemand` also fires `_maybe_start_prep_job`
  (**bulk** queue — low priority, honors the global pause / idle-kill) so a
  *subsequent* play uses the rich ABR/multi-audio bundle. JIT only bridges the gap
  for the current session. (Both can run at once — the below-normal priority on both
  keeps the box usable; see [GOTCHAS.md](GOTCHAS.md).)

### Client (`static/index.html`)

- `lp.mode` is `"bundle"` or `"ondemand"`; `lp.odKey` holds the session key for the
  close beacon. `_lpLoadIndex`'s not-ready branch POSTs `stream-ondemand`, sets
  `lp.mode="ondemand"`, and hands `master_url` straight to hls.js / Safari — the old
  "Building stream… 42%" full-encode wait is gone.
- **Loading/seek UX.** A `waiting` listener shows the `#lpPreparing` overlay
  ("Loading…") whenever playback stalls for data (initial start, or a seek into a
  not-yet-generated region — the server is holding that segment request open while it
  transcodes); `playing` hides it. A seek **back** into an already-generated region
  fires no `waiting`, so it's instant. hls.js's `fragLoadingTimeOut` is raised to
  45 s (> the server's `OD_SEG_WAIT_TIMEOUT`) so a still-progressing cold encode
  isn't aborted.
- **Tracks.** Single quality (the hls.js quality row auto-hides — one variant).
  Audio: `lpSetAudio` in OD mode persists the pick (`_lpSaveLocalTracks`) and
  re-enters `_lpLoadIndex` at the current time; `stream-ondemand` picks up the saved
  `audio_idx` (or the background full prep may have finished, promoting playback to
  bundle mode). Subtitles use the **existing** sidecar `<track>` machinery
  (`prep.subs` from `_list_sidecar_subs`) unchanged. The **Clip** row is hidden in OD
  mode — clipping needs the bundle (`409` otherwise), so it returns once the file is
  fully prepped.

> **Not on macOS.** Like all HLS prep, on-demand is disabled when `HLS_AVAILABLE` is
> false (the TCC block). The endpoints `503` and the dashboard never reaches the OD
> path (on-device controls are hidden, play routes to VLC).

---

## Clip

Save & share a short MP4 of the **last N seconds** of whatever's playing — a
30 s clip by default, or a custom length (1–300 s). Two entry points, one
backend.

**UI.** Both spots lead with a prominent **Clip Last 30s** button and offer a
quieter **Clip last…** custom-length button (`_clipPromptSeconds`):

- **Fullscreen VLC controls** — `#fcClipRow` (Row D2, between the track
  selectors and Stop). `fcClip(seconds, btn)` clips what's on the TV: it reads
  the freshest position from a live `GET /api/vlc/tracks` (the SSE snapshot lags
  ≤2 s, like `handoffToDevice`) and clips audio track 0 (VLC ES-ID → source
  audio order isn't reliably resolvable). The row shows only during library
  playback (same `canHandoff` gate) and the tiles **grey out until the file is
  stream-prepped** (`_handoffReadyState(s)===false`); readiness-unknown stays
  enabled and lets the backend explain.
- **On-device player** — `#lpClipRow`, directly under the subtitle selector
  inside `#lpTrackRow` (which is now always shown so the clip row is always
  available). `lpClip(seconds, btn)` clips the local `<video>`'s `currentTime`
  using the **selected audio** (`lp.pendingAudioIdx`, which is the source audio
  rendition index). The file is prepped by definition here.

**Shared client core.** `_doClip(itemId, filePath, endSec, seconds, audioIdx,
btn)` POSTs `/api/library/{id}/clip`, then `_shareOrDownload(url, filename)`
fetches the result and hands it to the OS **share sheet** when the platform can
share files (`navigator.canShare({files})` — iOS/Android), else triggers a
download (desktop), with a `window.open` last resort.

**Server.** `POST /api/library/{id}/clip` re-encodes `[end_sec-duration,
end_sec]` of the **original source** (not the HLS segments — best quality +
precise track mapping) to a universally-compatible MP4 (`_build_clip`: H.264 +
AAC stereo, `+faststart`, `-pix_fmt yuv420p`, NVENC when available else
libx264). `-ss` keyframe-seeks before `-i` for speed; the re-encode lands on the
exact start. The clip is written to `.clips/<token>/<filename>` and served by
`GET /api/library/clip/{token}/{filename}` as a download attachment.

**Prepped-only.** The endpoint requires the HLS bundle to exist
(`master.m3u8`) — `409` otherwise. This both matches the product decision
(clipping is a prepped-only feature) and guarantees the source is on disk +
probed. `503` on macOS (`HLS_AVAILABLE` false).

**Ephemeral.** Clips are cut on demand and auto-purged after `CLIP_TTL_SECS`
(2 h) by `_purge_old_clips()`, called at the start of every clip request.
`.clips/` is gitignored.

> **Audio-track caveat (TV).** A VLC clip always grabs source audio track 0,
> because there's no reliable mapping from VLC's live ES-ID back to the source
> stream order. If a viewer is on a non-default audio track on the TV, the clip
> won't match it. On-device clips don't have this problem — the player knows its
> rendition index. Windows/Linux/macOS-agnostic; gated on HLS like all prep.

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
later" + overnight), `"interactive"` (play-on-device), or `"admin"` (admin
force-prep — see [§ Force-prep (admin)](#force-prep-admin)). `state.prep_paused`
gates **bulk** jobs only; `interactive` and `admin` ignore it. The gate lives at the top of `_run_offline_job`, just
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

A single background loop, `auto_prep_loop` (registered in `lifespan`), drives the
**unified Automatic Stream Prep** control — one `mode` (`library.json →
settings.auto_prep`, read via `_auto_prep_cfg`) that auto-preps the **whole
un-prepped library**. The mode decides the per-tick (every 15 s) `want`:

| mode | `want` | falling-edge pause |
|------|--------|--------------------|
| `always` | always `True` — prep regardless of activity | only when mode is turned off (graceful) |
| `idle` | `not in_use` — prep only while the box is idle | `on_activity`: `hard` ⇒ `kill=True`, `soft` ⇒ `kill=False` |
| `off` | always `False` | n/a |

`state.auto_prep_engaged` is the in-memory edge flag:

- **Rising edge** (`want` and not engaged) → `_resume_prep()` (clears any pause +
  re-spawns paused jobs) then `_enqueue_library_prep()` queues a bulk job for
  every un-prepped video file (idempotent).
- **Stays engaged in `always`** → the loop re-runs `_enqueue_library_prep()` every
  ~20 ticks (~5 min) so newly-downloaded content gets prepped without waiting for
  an edge.
- **Falling edge** (engaged and not `want`) → `_pause_prep(...)`:
  - **`idle` + `on_activity == "hard"`** ⇒ `kill=True` — the in-flight encode is
    terminated immediately for instant responsiveness; it restarts from scratch on
    the next idle stretch (HLS prep can't checkpoint).
  - **`idle` + `on_activity == "soft"`** ⇒ `kill=False` — graceful: the in-flight
    file finishes, the rest hold until the box is idle again.
  - **mode just turned off** ⇒ `kill=False` (graceful hold).

**Idle detection.** `idle` mode reuses `_machine_in_use(idle_minutes*60,
for_prep=True)` — the same helper the scheduled reboot uses — so "idle" means no
VLC playback of real content, no active stream, no running download, no mutating
HTTP interaction, and (with `for_prep`) no open dashboard within the window.
`idle_minutes` is clamped 1–720. That single window doubles as the activity
detector: a fresh interaction stamps `state.last_activity`, flipping
`_machine_in_use` True within a tick, which collapses `want`. The cheap
`_activity_kick` hook (called from the `track_activity` middleware) shortcuts the
**`idle` + hard** case so the kill lands on the request, not up to a tick later;
`always`, `idle`+soft, and `off` are no-ops there (the loop handles them).

See [ADMIN.md § Automatic Stream Prep](ADMIN.md) for the panel + endpoints.

#### Per-file prep schedule (`item.prep`)

The whole-library auto-prep above can be overridden per file from the **episode
picker's scheduling bar** — the stream-prep sibling of the per-file download
schedule. `item["prep"]["files"][path] ∈ {now, idle, never}` (read via `_prep_cfg`
/ `_effective_prep_mode`; default `idle`), written by
`POST /api/library/{id}/prep-schedule`:

- **now** — `/prep-schedule` immediately enqueues a bulk prep job per file (a scoped
  `/prep-all`).
- **idle** — the implicit default: `auto_prep_loop` builds the bundle during the
  idle/always window.
- **never** — opt out of **all** automatic prep. Both `_enqueue_library_prep` (the
  idle/always loop) and the play-driven `_play_prep_chain` skip `never` files. It's
  **non-destructive** — an already-built bundle is kept, and a `never` file still
  plays via the on-demand (JIT) path. Admin **Force Stream Prep** ignores `never` by
  design (explicit "prep everything" override).

The mode surfaces as `prep_mode` in the `/files` response so the bar can highlight
the active segment. See [LIBRARY_DATA.md § `prep`](LIBRARY_DATA.md) and
[FRONTEND.md](FRONTEND.md) (`epSchedPrep`, `_epPrepBtn`).

#### Recheck file hashes (`POST /recheck`)

The same bar carries a **Recheck hashes** button that force-rechecks the item's
torrent against its piece hashes for the **checked** episodes. `recheck_files`
snapshots each requested file's qBit progress, calls `qbit_recheck` (whole-torrent —
qBit has no per-file recheck), waits out the `checking*` states (~5 min cap), then
flags any file that was complete **before** but isn't **after** as damaged: qBit
re-fetches the bad pieces, and the file's cached HLS bundle is purged
(`shutil.rmtree` on `OFFLINE_CACHE/<key>`) so playback re-preps from the repaired
source. The torrent is re-resumed if anything was damaged. See
[API.md](API.md) and [FRONTEND.md](FRONTEND.md) (`epRecheckSelected`).

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

### Force-prep (admin)

A fourth trigger, admin-initiated, that **viewers and host activity cannot stop**
— the deliberate inverse of the interruptible bulk triggers above. The Admin →
System **Force Stream Prep** card preps the whole library (or one selected item)
on demand and runs it to completion regardless of the non-admin Pause control or
live activity.

These jobs ride a dedicated **`"admin"` queue**. Mechanically they behave like
`interactive` prep — they skip the `state.prep_paused` gate, are never touched by
`_pause_prep` / `_activity_kick` (both only act on `bulk`), and preempt any
in-flight bulk encode (`_preempt_running_bulk`). `_priority_hls_pending` counts
both `interactive` and `admin`, so bulk prep defers to a force-prep batch the same
way it defers to a play-on-device prep.

The difference from `interactive` is that the admin **can** halt them, via
`_stop_admin_prep(hard)` gated on `state.admin_prep_stop`:

- **Soft** (`hard=false`) — the in-flight encode finishes (and is cached); every
  queued admin job marks itself `"cancelled"` at the gate in `_run_offline_job`.
- **Hard** (`hard=true`) — additionally `terminate()`s the running ffmpeg
  (`job["_admin_stopped"]` ⇒ the non-zero rc is an intentional cancel, partial
  `.part` dir dropped), then cancels the queued rest.

A stopped batch is **not** auto-resumed (no equivalent of `_resume_prep` for
`admin`); a fresh **Force Prep** clears `admin_prep_stop` and re-queues. No-op on
macOS (`HLS_AVAILABLE` false). Helpers: `_enqueue_admin_prep` /
`_start_admin_prep_job` / `_stop_admin_prep` / `_force_prep_status`. Endpoints +
UI in [ADMIN.md § Force Stream Prep](ADMIN.md).

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
