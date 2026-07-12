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
  episode-nav buttons + `_lpRenderNavButtons` / `_lpWarmNextEp`, and the
  custom control overlay `#lpControls` — seek bar / play-pause / ±10 s /
  mute / OS fullscreen; detailed in
  [FRONTEND.md § Local player](FRONTEND.md))
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

## Where bundles live (co-located, v8+)

As of **v8 (`OFFLINE_CACHE_VERSION = "v8-colocated"`)** each prepped file's HLS
bundle lives **beside its source file**, in a hidden
`<file_dir>/.streamlink_cache/<key>/` — *not* in the old central
`.offline_cache/` at the repo root. The single source of truth for the location
is `_offline_cache_dir(src)` in `main.py`; everything else routes through it.

- **Key is path- and mtime-independent:** `_offline_cache_key(src) =
  sha256("v8-colocated | <filename> | <size>")[:24]` (see `_offline_cache_key_for`
  for the from-metadata variant the move op uses). Dropping the absolute path and
  mtime makes the key **move-stable** — a bundle stays valid wherever the file goes,
  including a cross-device move where mtime changes. Two files can't share a name in
  one dir, and identical name+size in *different* dirs land in different
  `.streamlink_cache/` dirs, so there's no collision. mtime is safe to omit because
  the in-place rewrite paths (repair / compress) already purge the bundle explicitly.
- **Serving uses a key→dir index.** The route `/api/library/offline-cache/<key>/<file>`
  can't do `OFFLINE_CACHE / key` anymore, so `_bundle_index` (`dict[key, Path]`) maps
  each key to its dir. It's discovered by *directory name* (`_rebuild_bundle_index_sync`
  walks every library file's parent `.streamlink_cache/` + the legacy central dir — no
  source stat needed), registered on prep-completion / cached-hit
  (`_bundle_index_register`), and invalidated on move / migrate / delete
  (`_invalidate_bundle_index`). A cold miss rebuilds once then 404s.
- **Startup migration (`_migrate_offline_cache_layout`).** Pre-v8 installs kept bundles
  centrally. On boot — **before** the prep loops spawn, so nothing half-migrated gets
  re-queued — each old `.offline_cache/<legacy_key>/` (legacy key =
  `sha256("v7-hls-abr | <abs_path> | <mtime> | <size>")`, via `_offline_cache_key_legacy`)
  is **moved** to its co-located home with a plain directory move. **No re-encode**,
  idempotent, best-effort per file. Stale central bundles (source changed/removed) aren't
  matched and are left for the orphan purge.
- **Moving a series carries the cache.** `POST /api/library/{id}/move` (admin) relocates a
  series; because the key is move-stable, the co-located bundle is just moved alongside the
  media (`_move_series_files_sync`). See [ADMIN.md](ADMIN.md) / [API.md](API.md).
- The legacy `OFFLINE_CACHE` (`.offline_cache/`) constant still exists only so the migration
  and the inventory can find/relocate pre-v8 stragglers. The `.ondemand_cache/` (transient
  JIT) stays central at the repo root.

## Output format

Every prepped source produces a directory `<key>/` inside its file's
`.streamlink_cache/` with the following layout (the on-disk parent is
`<file_dir>/.streamlink_cache/`; pre-v8 this was `.offline_cache/` at the repo
root):

```
<file_dir>/.streamlink_cache/
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

Cache key (`_offline_cache_key`) is `sha256(OFFLINE_CACHE_VERSION | filename |
size)[:24]` (v8+, path/mtime-independent — see *Where bundles live* above).
Re-encoding the source changes its size, so the key changes and the old bundle
is purged (repair/compress do this explicitly). `OFFLINE_CACHE_VERSION =
"v8-colocated"`; bumping it forces every bundle to be rebuilt on next prep (old
`<key>/` dirs become orphans, listed in Admin → Offline Cache for one-click
purge).

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
  # something actually decodes AND the NVDEC probe (`_gpu_can_hwdecode`, the
  # `hw_decode` flag) confirms this GPU can decode the source codec — routes
  # decode through NVDEC so the CPU isn't the bottleneck. It does NOT reliably
  # fall back for codecs whose hwaccel init hard-fails (AV1 on a Pascal card),
  # so an un-decodable source CPU-decodes here; NVENC still encodes. See
  # docs/GOTCHAS.md "`-hwaccel cuda` does NOT gracefully fall back".
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

- **ABR ladder: Original + admin-selected down-rungs (default 720p + 480p).** The
  source video is mapped once per rung (`_hls_video_variants` caps the ladder at
  source height — no upscaling). All variants share one audio group, so the player
  switches video quality without re-fetching audio. Each down-rung gets a
  `scale=-2:<h>` filter (the `-2` keeps an even width for yuv420p) and a
  `maxrate`/`bufsize` VBV cap so the rendition is genuinely smaller and the master
  playlist's `BANDWIDTH` is realistic for ABR selection. **The down-rung set is
  admin-configurable** — see [§ Configurable ABR ladder + trimming](#configurable-abr-ladder--bundle-trimming).
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
- **Audio is timestamp-locked to the video clock** (`+genpts` on the input,
  `aresample=async=1` on each audio output). The original video rung is
  stream-*copied* while audio is *re-encoded*, and the two are **separate HLS
  renditions sharing one timeline** — so a source with a per-stream `start_time`
  skew, broken/missing PTS, or audio gaps would desync the re-encoded audio from
  the copied video (intermittent, source-dependent). `-fflags +genpts` gives
  every stream a clean monotonic clock; the per-output `-filter:a:{i}
  aresample=async=1:min_hard_comp=0.100000` stretches/squeezes the audio and pads
  gaps with silence so it tracks the video. **No `first_pts=0`** — anchoring audio
  at 0 against a copied video whose first PTS isn't 0 would *introduce* a constant
  offset. This handles the **drift / gap** class.
- **Constant-offset guard: re-encode the original rung when the source has a video
  edit list.** `aresample` fixes drift but **not** a fixed offset. The dominant
  real-world cause of a fixed offset is a video **edit list** in the source (common
  in `-ss` stream-copies, web-DL, and many MP4 muxers — and the container still
  reports `start_time=0`, so it's invisible to a start-time check): stream-copying
  the video mishandles the edit list while the re-encoded audio is normalized to
  zero, so audio ends up a steady amount early/late (VLC plays the source fine —
  this is purely a prep artifact). `_source_has_editlist(src)` detects it by
  comparing the first video pts with the edit list applied vs. `-ignore_editlist 1`;
  when they differ by more than `EDITLIST_REENCODE_SECS` (0.05s), `_run_offline_job`
  sets `copy_original = False` (via `_build_hls_ffmpeg_args(force_reencode_original=…)`)
  so the original rung is **re-encoded** — applying the edit list to the frames and
  keeping A/V aligned. Costs a full encode of the original rung for those files only
  (probed only when we'd otherwise copy); clean sources still stream-copy. The
  repair tool below forces this on every bundle it rebuilds.
- **Text subs become standalone WebVTT sidecars** (`sub_<i>.vtt`), *not*
  in-manifest renditions — ffmpeg's HLS muxer can't package more than one
  WebVTT track. subrip / ass / ssa are transparently converted. Image-based subs
  (`hdmv_pgs_subtitle`, `dvd_subtitle`, `dvb_subtitle`, `vobsub`) are filtered
  out by `_ffprobe_full` and noted in `meta.json:skipped_image_subs`.
  > **Styled ASS/SSA subs also get a raw sidecar + fonts (libass path).** For a
  > sub whose codec is `ass`/`ssa` (`meta.subtitles[i].styled`), `_run_offline_job`
  > additionally emits the **raw** `sub_<i>.ass` (stream-copied, styling intact,
  > referenced by `meta.subtitles[i].ass_file`) and extracts the source's embedded
  > fonts as flat `font_<n>.<ext>` files listed in `meta.json:fonts`
  > (`_extract_bundle_fonts`, a separate best-effort ffmpeg `-dump_attachment`
  > pass). In **bundle mode** the on-device player renders that raw ASS with
  > **libass-wasm (SubtitlesOctopus)** on a canvas overlay (`_lpApplyStyledSub`,
  > vendored `static/vendor/subtitles-octopus*` + `libass-fallback-font.ttf`), so
  > karaoke/positioning/fonts survive. **On-demand/JIT now renders styled subs the
  > same way (8.10.0)** — it extracts `sub_<i>.ass` + fonts into its session dir; see
  > the On-Demand § "Tracks" note below. The flattened `sub_<i>.vtt` is kept as the
  > **universal fallback** — used for old bundles (no `ass_file`), the iOS
  > device-copy path (no bundle dir), and any libass failure. Re-prep an existing item to
  > gain styling (`OFFLINE_CACHE_VERSION` isn't bumped). The **iOS app** renders
  > styled subs on both surfaces: the online in-app dashboard reuses this same
  > path, and the **offline downloads player** (`ios-app/www/downloads.html`) has a
  > parallel implementation with the octopus assets vendored into `ios-app/www/`
  > (the `.ass`/`font_*` files download with the bundle and are served by the
  > native `LocalMediaServer`). Reliability plumbing (7.16.5, both players): the
  > overlay apply is guarded by an attempt *token* (`_lpOctopusSeq`) so
  > concurrent applies for the same sub can't each construct-and-leak an
  > instance; a resize "nudge" on the video's `resize`/`loadeddata`/`playing`
  > events un-sticks an overlay constructed before `videoWidth` was known; and a
  > `TextTrackList` `change` enforcer keeps every VTT `<track>` disabled while
  > the overlay is up. **Cue-clearing clock pump (9.10.3):** libass only *erases*
  > a finished cue when its worker re-renders past the cue's end, and the worker
  > re-renders continuously only while it thinks the video is *playing*. Because
  > the overlay is built after the ~3 MB wasm loads (often after `playing`
  > already fired) and the worker also self-pauses on its own "no currentTime for
  > > 5 s" watchdog whenever `timeupdate` gaps (iOS/HLS ManagedMediaSource does
  > this routinely), a finished styled cue could stay painted until the *next*
  > cue — up to a minute over a quiet stretch. `_lpOctopusPumpStart` runs a
  > 250 ms interval while the overlay is up that re-asserts the real play/paused
  > state and feeds `currentTime`, so the 5 s watchdog never trips and cues erase
  > on time (stopped by `_lpTeardownOctopus`). **Stationary-cue renderer (9.11.1):**
  > the pump wasn't enough for *stationary* cues — libass only erases a cue when
  > its worker *posts* a "now empty" frame, and the default `wasm-blend` renderer
  > (`renderBlend()`) doesn't reliably flag that empty transition as changed, so a
  > stationary cue's single end-of-cue post never fired and it lingered until the
  > next cue (moving cues post every frame, so they were fine). The overlay is now
  > built with `renderMode:"js-blend"` (stock libass `renderImage()` + libass's own
  > `detect_change`), which clears stationary cues on time. **Loopback bundles (7.17.1, both players):** when the
  > ass/fonts live on the loopback `LocalMediaServer` (a downloaded copy), the
  > page prefetches them on the main thread and passes octopus `subContent` +
  > `blob:` font URLs — the worker's own sync-XHR fetch of a loopback URL is a
  > cross-origin worker request WKWebView doesn't reliably allow (its failure =
  > silent VTT fallback). Host-bundle playback keeps plain URLs.
  > See [GOTCHAS.md](GOTCHAS.md).
  > **Each generated `sub_<i>.vtt` is run through `_clean_webvtt()`** before the
  > bundle finalizes. ffmpeg's ASS→WebVTT conversion of heavily-typeset fansub
  > tracks emits each overlapping Dialogue *layer* as a separate **identical**
  > cue (lines render doubled on screen), dumps `\p` vector **drawings** as raw
  > coordinate-blob cues, and leaks ASS escapes like `\h`. The sanitiser drops
  > drawing-blob cues, de-duplicates same-timing-same-text cues, and fixes the
  > escapes; it's **idempotent**, so the same heal is applied in-place when
  > serving `.vtt` from an older bundle (`offline_cache_bundle_file`) and inside
  > `_sub_to_vtt` for on-demand sidecar conversion. See [GOTCHAS.md](GOTCHAS.md).
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
- **Optional validate & repair on prep (admin `settings.prep_validate`).** When
  the admin sets the *Validate & Repair on Prep* mode to `before` or `after`,
  **bulk/idle** prep jobs also run the source through the File Validator's deep
  decode and, if damaged, **remux-repair** it in place (lossless; no lossy
  re-encode) — via the shared helper `_prep_validate_repair` (reusing
  `_validate_one_file`/`_repair_one_file`). `before` heals the file ahead of the
  encode and re-points `out`/`tmp_dir` at the healed file's new
  `_offline_cache_key`; `after` validates in this post-prep hook block (alongside
  STT + fingerprinting), where a repair purges the just-built bundle so the file
  re-preps from the healed source next idle cycle. **Bulk jobs only** —
  interactive play-on-device preps never validate (no playback latency). The work
  runs inside the single prep slot and uses the job's own `_proc`, so Pause /
  Stop Now / activity-kick terminate it. GPU-accelerated when NVENC is present
  (`_decode_hwaccel_args`). Default `off`. See [ADMIN.md § Validate & Repair on
  Prep](ADMIN.md).
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

## Configurable ABR ladder + bundle trimming

The down-rung set isn't fixed. `HLS_ABR_LADDER` carries four rungs (1080/720/480/360
with their VBV caps); which of them a prep emits is the admin selection
`settings.admin_overrides.hls_ladder`, read by `_hls_ladder_heights(lib)` and passed
into `_build_hls_ffmpeg_args(..., ladder_heights=…)` → `_hls_video_variants(info,
heights)`. The original (source-resolution) rung is **always** emitted; a down-rung
appears only when the source is taller than it **and** its height is in the selected
set. Default (no override) is `[720, 480]` — today's behaviour. `meta.json videos[]`
derives from the same `kept_videos`, so the manifest, the master playlist, and the
UI's quality menu stay consistent.

Changing the default is **forward-only** and intentionally does **not** bump
`OFFLINE_CACHE_VERSION` (no global rebuild). To slim **existing** bundles, the admin
**Drop HLS Resolutions** tool (`_hls_trim_bundles` → `_trim_one_bundle`, `POST
/api/admin/hls-trim`) deletes a dropped rung's playlist + init + segments from each
`.offline_cache/<sha>/`, rewrites `master.m3u8` (dropping the rung's
`#EXT-X-STREAM-INF` + its following URI line — audio `#EXT-X-MEDIA` lines untouched),
and rewrites `meta.json videos[]`. Bundles with an active prep job are skipped; a
`dry_run` sums the bytes that *would* be freed for the UI estimate. See
[ADMIN.md § Storage & Compression](ADMIN.md).

## Repairing audio-misaligned bundles

Bundles built **before** the audio-sync fix (`+genpts` + `aresample=async=1`, see
the audio bullet under *Key decisions* above) can have audio drifted out of sync
with video. The admin **Detect & Repair Audio Sync** tool (`_hls_resync_bundles` →
`_bundle_audio_sync_delta`, `POST /api/admin/hls-resync`) finds and repairs them:

- **Detection — two complementary checks, flag on EITHER:**
  - **Drift** (`_bundle_audio_sync_delta`) sums each bundle's `#EXTINF` durations
    for the **video** rendition vs every **audio** rendition — a plain playlist
    parse, no decode — and flags when the worst `|audio − video|` divergence exceeds
    `max(HLS_SYNC_FLAG_SECS=1.0s, 1% of duration)`.
  - **Constant offset** (`_bundle_introduced_av_offset`) ffprobes the bundle's
    audio-vs-video first-pts **gap** `apts − vpts` (`_probe_rendition_first_pts`
    reads each rendition playlist + its fmp4 EXT-X-MAP init) and flags it directly:
    `AV_OFFSET_FLAG_SECS=0.12s` for an unpadded bundle, the looser
    `AV_OFFSET_PAD_FLAG_SECS=0.35s` when meta `audio_padded_to_zero` is set (tolerates
    the encoder frame-reorder residual that re-prep can't remove). This catches the
    fixed early/late offset the duration check is structurally blind to (both
    renditions are the same length, just shifted). **It flags the raw gap rather than
    diffing against the source's intended offset:** `av_probe.py` showed the bundle
    player (Safari / iOS AVPlayer / hls.js on separate fmp4 renditions) ignores a
    cross-rendition `baseMediaDecodeTime` gap and anchors each rendition to playback
    start — so a *faithfully reproduced* source delay (e.g. EMBER BDRips' +1.0s audio)
    is itself the desync, even though single-program on-demand + VLC honor it. The
    fix is to collapse the gap, not preserve it. No source needed for detection (only
    for repair).
- **Repair** (non-`dry_run`) purges each flagged bundle (`_delete_cache_artifacts`
  + `_invalidate_bundle_index`) and re-queues a prep via `_maybe_start_prep_job(src,
  item_id, force_reencode_video=True)` — the **force_reencode** re-encodes the
  original rung (so both streams route through one timestamp normalization) AND
  silence-pads the audio to the video's zero (`first_pts=0` → `audio_padded_to_zero`),
  collapsing the cross-rendition gap to ≈0 while preserving any real delay as leading
  silence so it can't carry the offset forward. Re-preps run at the usual bulk
  concurrency / pause semantics (background, not blocking). Bundles with an active
  prep job, or whose source is gone, are skipped.
- `scope` restricts to one library item (by `meta.json src`); `dry_run` only counts
  the flagged bundles for the UI estimate. See [ADMIN.md § Storage & Compression](ADMIN.md).

## Source-file compression

`_compress_one_file` / `_run_file_compression` re-encode **source** files in place to
reclaim disk (admin Storage & Compression card) — distinct from prep, which only
writes derived bundles. It reuses the File-Repair re-encode shape: re-encode the video
(libx264/libx265, or h264_nvenc/hevc_nvenc when `_decode_hwaccel_args` reports NVENC),
**copy** audio + every embedded subtitle/attachment, deep-decode the candidate
(`_ffmpeg_decode_scan`), then `os.replace` the original and purge its stale HLS bundle
(keyed on the old path|mtime|size) — but **only** when the result decodes clean **and**
is smaller (`_compression_params` maps presets → CRF + optional down-scale cap). An
already-efficient source re-encodes no smaller and is reported `skipped`, original
untouched. The savings estimator (`_compress_estimate`) is a per-(resolution × preset)
target-bitrate model plus the current bundle size (`_dir_size_bytes`), shown before
committing. Same footguns as repair: lossy/irreversible, and a torrent-backed file
stops seeding once rewritten. Not macOS-gated (a plain decode, like the File
Validator). See [ADMIN.md § Storage & Compression](ADMIN.md).

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

> **Prep priority orders bulk work (three tiers, mid default).** Within the bulk /
> auto-prep class, a per-item **prep priority** (`item.prep.priority_default` +
> per-file `item.prep.priority`, `low`/`mid`/`high`) decides which files build first.
> `_maybe_start_prep_job` stamps each bulk job's numeric tier as `_prep_prio` (from
> `_effective_prep_priority`), and the precedence gate in `_run_offline_job` parks a
> bulk job while `_higher_priority_bulk_pending(_prep_prio) > 0` — i.e. any *strictly*
> higher-tier bulk job is `pending`/`processing` — in the **same** loop that already
> defers to `_priority_hls_pending()`. So a "high" series/episode jumps the auto-prep
> queue ahead of "mid"/"low" work. Two boundaries: (a) **interactive + admin prep
> still outrank every bulk tier** (they're counted by `_priority_hls_pending`, not this
> gate), and (b) a running **bulk** encode is **never preempted for a higher-tier bulk
> job** — HLS can't checkpoint, so only *queued* order changes; the top tier never
> parks (no starvation/deadlock), and equal tiers stay FIFO. `POST
> /api/library/{id}/prep-priority` re-stamps any already-queued bulk jobs for the
> affected files so a change takes effect immediately. See
> [LIBRARY_DATA.md § prep](LIBRARY_DATA.md).

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
     track picks. MSE decode crashes recover via `hls.recoverMediaError()`;
     network loss recovers via the reconnect loop below.

> **Unstable connections (tunnels, Wi-Fi↔LTE swaps).** Playback is built to
> survive brief outages the way YouTube does — three pieces, all in
> `static/index.html`:
>
> 1. **Aggressive forward buffer.** hls.js is configured with
>    `maxBufferLength: 180` / `maxBufferSize: 240 MB` / `backBufferLength: 90`
>    so ~3 minutes of video are buffered ahead and a tunnel-length outage plays
>    through silently. The byte cap must scale with the length target — the
>    60 MB default is only ~60 s of a high-bitrate original rung and would
>    silently defeat `maxBufferLength`. The browser's SourceBuffer quota is the
>    real ceiling (hls.js backs off on `QuotaExceededError`); `backBufferLength`
>    frees memory behind the playhead so mobile devices can afford it.
>    `preferManagedMediaSource: false` is required for this to actually work on
>    macOS Safari — otherwise hls.js prefers Safari 17.1+'s ManagedMediaSource,
>    whose OS-owned fetch cadence caps the forward buffer at ~30 s. **On iPhone
>    MMS is the only MSE** (so hls.js falls back to it there regardless of the
>    flag), but the ~30 s cap is pushed back to **~120 s** by the iOS preroll
>    override (`_lpInstallIosPreroll`, v5.42.0): it overrides the hls.js
>    instance's `pauseBuffering` on true iOS devices so the MMS `endstreaming`
>    stop is ignored until the forward buffer reaches `IOS_PREROLL_TARGET_SECS`
>    (`backBufferLength` is trimmed to 30 s on iOS to offset the memory). Outages
>    beyond the banked buffer still fall through to the reconnect loop.
>    See [GOTCHAS.md](GOTCHAS.md) § ManagedMediaSource.
> 2. **Indefinite reconnect loop** (`_lpNetLost` / `_lpNetRetryNow` /
>    `_lpNetReset`, state in `lp.netDown`). A **fatal hls.js `NETWORK_ERROR`
>    never tears playback down or alerts** — the `<video>` keeps draining its
>    buffer while the loop retries `hls.startLoad(-1)` with backoff (2 s → 15 s
>    cap), forever; the user decides when to give up, not us. Recovery is
>    detected on `FRAG_LOADED`. The global `online` event short-circuits the
>    backoff (`_lpNetOnline`) — note the loop does **not** gate retries on
>    `navigator.onLine`, since a phone in a tunnel often reports online with no
>    usable data. On the Safari-native path the `<video>` `error` event enters
>    the same loop; a retry reloads the still-set `src` and seeks back via the
>    resume machinery (`lp.resumeSec`/`resumeApplied`); recovery is detected on
>    `playing`. Only `MEDIA_ERR_SRC_NOT_SUPPORTED` (permanent) still alerts.
> 3. **"Reconnecting…" only when it's user-visible.** While the buffer covers
>    the outage nothing is shown; once `waiting` fires (or `_lpNetLost` finds
>    `readyState < 3`) the `#lpPreparing` overlay shows "Reconnecting…", cleared
>    by `playing`. Retry state is reset in `_lpDestroyHls` (so every load /
>    stop / episode change starts clean).
> 4. **Hung-request stall watchdog** (`_lpStallWatch`, `_lpKickLoader`). Pieces
>    1–3 all hinge on hls.js firing a **fatal `NETWORK_ERROR`** — but a connection
>    that drops *mid-fetch* (the classic tunnel) doesn't fail the in-flight
>    fragment XHR, it **hangs** it over the dead socket. No fatal error fires, the
>    reconnect loop never engages, and hls.js sits on the dead request until its
>    45 s `fragLoadingTimeOut` — so playback keeps "buffering" long after the link
>    is back (a stop+restart cured it instantly because that built a fresh request
>    on a live socket). The watchdog polls every 3 s and, once playback has
>    actually started (`lp.everPlayed`, armed on the first `playing`), aborts the
>    hung fragment (`hls.stopLoad()` + `startLoad(-1)`; Safari reloads at position)
>    and re-requests from the current position once the playhead has been starved
>    (`readyState < 3`, no forward progress) past a threshold — **9 s in bundle
>    mode** (segments load near-instantly, so a multi-second stall is a dead
>    request) but **35 s in on-demand mode**, above the server's 30 s
>    `OD_SEG_WAIT_TIMEOUT` so a legitimately-progressing JIT cold seek (which
>    either delivers the segment or returns a 504 hls.js retries itself) is never
>    kicked. The global `online` event also kicks immediately via
>    `_lpNetOnline → _lpKickIfStalled` when playback is starved (tunnel exit the
>    OS *does* notice). Watchdog state resets in `_lpDestroyHls`.
> 5. **iOS cold-start kick** (`_lpArmColdStartKick` / `_lpColdStartKickTick`,
>    v8.11.1). A separate failure from the outage handling above, at the *very
>    start*: on iOS the only MSE is ManagedMediaSource, and it occasionally never
>    fires the first `startstreaming`, so hls.js's loaders never begin — `play()`
>    resolves and the first frame decodes (the video *looks* ready) but the buffer
>    stays empty and playback is wedged with **no error**. The manual cure is to
>    skip ±10 s (a real seek makes MMS start fetching), so this automates it:
>    armed after the cold-start `play()`, a 900 ms poll runs while `!lp.everPlayed`
>    and, once a genuine sustained stall is confirmed (~1.8 s; not paused/scrubbing,
>    `readyState < 3`, resume-seek applied), applies a tiny imperceptible forward
>    `currentTime` nudge each tick until playback starts, giving up after ~6 s.
>    iOS-only, disarmed on the first `playing` and in `_lpDestroyHls`. Piece 4's
>    `_lpStallWatch` can't cover this — it arms only *after* the first `playing`.
>    See [GOTCHAS.md](GOTCHAS.md) § ManagedMediaSource.
   - **Safari** (iOS + macOS): set `<video>.src = master_url` directly.
     Safari plays HLS natively, exposing `AudioTrackList` for audio
     switching. Wait for `loadedmetadata` → populate dropdowns. (Subtitles
     are `<track>` children, not in-band, on every engine — see step 5.)
5. Track switching:
   - **Quality** (ABR, hls.js only): `_lpRenderTrackRows` builds the **Res**
     dropdown from `lp.hls.levels` (sorted high→low) as `Auto` + each
     resolution; `lpSetQuality(val)` sets `lp.hls.currentLevel` (`-1` = Auto/ABR,
     a level index pins that rung — a brief rebuffer on switch is expected).
     Quality is **session-only** — not persisted via `/local-tracks`, since the
     right rung is connection-dependent and Auto is the sensible default. Safari
     native HLS auto-adapts among the variants but exposes no reliable manual
     level API, so its Res row stays hidden (auto-only).
     **iOS app, device-copy playback**: the downloaded master is trimmed to ONE
     rung, so `hls.levels` can't drive the menu. It's built from the bundle's
     own `meta.json` ladder instead: `"<label> — On device"` (value `dev`,
     selected) + every other rung as `"<label> — Server"` (`srv:<height>`;
     hls.js engine only) + `"Auto — Server"` (`srv:auto`, the only server
     option on Safari-native / old bundles without `videos[]`). A `srv:*` pick
     sets a **per-file, session-only** override (`lp._srvOverride`) and
     re-enters `_lpLoadIndex` at the current position — the server master then
     loads normally and, for a numeric pick, the level is pinned **by height**
     in `MANIFEST_PARSED` (level indices don't survive the source switch; a
     ladder changed since the download simply stays Auto). While overridden,
     the server-side menu carries a `"— On device"` option to switch back. The
     override is dropped whenever a different file loads, so each episode in
     back-to-back playback defaults to its own device copy; if the server
     switch fails (host unreachable) `_lpFallBackToLocal` clears the override
     and reloads the downloaded copy instead of stopping.
   - **Audio** (in-manifest): hls.js path sets `hls.audioTrack = idx`; Safari
     native sets `video.audioTracks[i].enabled = (i===idx)`. Applied via
     `_lpApplyAudioIdx` → `_lpApplyAudioIdxRetry`, which **retries until the
     audio-track list is populated** (Safari fills `video.audioTracks`
     asynchronously — at `loadedmetadata` it's often empty, and a one-shot set
     is silently dropped, leaving the default playing under a dropdown that
     shows the remembered track) and re-applies if Safari later reverts to its
     default; it bails when `lp.pendingAudioIdx`/`lp.filePath` change so a stale
     retry never fights a manual pick. See GOTCHAS.md § "VLC re-runs its default
     audio selection".
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
   - **Suspend-drop recovery (iOS).** When the WKWebView is suspended (app
     backgrounded / another app opened) while a `<track>` is *showing*, iOS
     WebKit discards that track's parsed cues; re-setting `mode="showing"` never
     re-fetches, so the active subtitle silently goes blank (other tracks, not
     yet shown at suspend time, still load fine on selection). `_lpSubTrackBroken`
     detects the terminal-but-empty state (`readyState` LOADED with no cues, or
     ERROR) and `_lpRecreateSubTrack` removes + re-appends the `<track>` to force
     a fresh fetch. `_lpRecoverActiveSub` runs it for the active track on
     `visibilitychange`→visible; `lpSetSubtitle` runs it when the user re-selects
     a dropped track. Healthy tracks (cues present) are never recreated, so
     nothing flickers. See [GOTCHAS.md](GOTCHAS.md).
   - Each switch POSTs to `/api/library/{id}/local-tracks` with the new
     pick so it persists across sessions. The pick travels as a resolvable
     **descriptor** `subtitle_sel` (`{off, lang, ai, name}`), saved per-file
     *and* per-series — so a sidecar/AI choice comes back on replay and on the
     next episode. (Pre-v4.27 the on-device save dropped every non-bundle pick,
     persisting `-1`/off — a chosen `.srt`/AI sub was never remembered.) On the
     next play the resolver (`_lpResolveSubSel`) matches the file's, then the
     series', descriptor against the live track list: `name` → `lang`+kind →
     any-kind in that language → lone-option.
   - **Audio picks work the same way (8.9.0).** An audio switch POSTs
     `audio_sel` (`{lang, title, idx, sig, at}` — no sidecar/AI/off, since audio
     is always embedded), saved per-file *and* per profile+series
     (`series_audio_prefs`). `_lpResolveAudioSel` restores the newest of the
     file/series pick by same-layout slot → group → language → title, falling
     back to the legacy `audio_idx` then the `default` rendition. Both this
     player and the VLC/TV path read and write it, so an audio choice follows
     the viewer across a device↔VLC switch and onto the next episode. **Between
     the descriptor chain and the `default` rendition sits the profile-level
     language fallback (8.9.4):** `_lpResolveAudioPref(saved.audio_language_pref)`
     matches by language (then slot, when the track count matches). This carries
     an audio pick across episodes that are *separate library items* (empty
     `series` → no shared per-series key — the common case for individually
     downloaded episodes). `audio_language_pref` arrives in `saved_tracks` from
     the host; for fully-offline playback the app reads a per-profile
     `localStorage` mirror (`_appLearnAudioPref`/`_appReadAudioPref`) written on
     every pick, since `library.json` is unreachable in airplane mode. See
     [LIBRARY_DATA.md](LIBRARY_DATA.md) and [GOTCHAS.md](GOTCHAS.md).
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
7. Transport is the **custom Metro control overlay** (`#lpControls`) — the
   native `controls` attribute is deliberately absent so every OS/browser
   shows the identical UI. The video takes the **entire screen** (`#lpStage`
   is `absolute inset:0`); the header, transport, and options are all
   overlays. Pieces: seek bar with buffered fill + drag-to-scrub
   (the seek commits once on release — in on-demand mode every cold seek
   restarts the JIT ffmpeg, so don't seek per pointermove), ±10 s tiles,
   play/pause, mute, a **gear button** that opens the options panel
   (`#lpTrackRow`: quality / audio / subtitles / AI / Clip — hidden
   otherwise), and OS fullscreen on the **whole container** (so the gear
   panel stays usable in fullscreen). iPhone Safari lacks
   element-fullscreen, so there the button falls back to
   `video.webkitEnterFullscreen()` — the native iOS player takes over
   (native transport; in-stream audio/sub tracks still work — they're in
   the HLS manifest on the Safari-native path) and `webkitendfullscreen`
   resyncs the custom UI on exit; this is the only true-fullscreen path on
   iPhone, where browser chrome otherwise never fully leaves. A swipe up on
   the video also minimizes the browser bar (app-shell escape hatch — see
   [FRONTEND.md](FRONTEND.md) § Layout). An **orientation-lock button**
   (`#lpRotBtn`, touch devices only) keeps playback landscape even when the
   phone auto-rotates to portrait: native `screen.orientation.lock` where it
   works (Android + OS fullscreen), CSS 90°-rotation fallback everywhere
   else (iPhone Safari has no lock API) — see the footgun in
   [GOTCHAS.md](GOTCHAS.md). Auto-hides 3 s into playback; tap to toggle.
   Full detail in [FRONTEND.md](FRONTEND.md); the iOS/fullscreen footgun is
   in [GOTCHAS.md](GOTCHAS.md).

### 3. Skip-intro / credits

`lpEvaluateSkipOffer(t)` runs on every `timeupdate` and mirrors the backend
`_maybe_emit_skip_offer` — **including its per-profile auto-skip**. The current
profile's `auto_skip_intro` / `auto_skip_credits` toggles are read live via
`_lpAutoSkipPrefs()` (the freshly-fetched `allProfiles` entry, falling back to
the stored `profile`; `saveAutoSkip` also writes the new values straight into
both caches so a mid-session toggle takes effect on the next tick without a
re-fetch). Behaviour per window:

- **Auto-skip OFF (manual, unchanged):**
  - Intro window: `start - 2 ≤ t < end - 2`. Show "Skip Intro" button.
  - Credits window: `t ≥ credits_start - 1`. Show "Skip Credits" / "End"
    depending on whether there's a next file.
- **Auto-skip ON (countdown then auto-fire, mirrors the VLC marquee):** once
  `t` enters `[skip_point − lead, skip_point)` the tile counts down (intro lead
  `LP_SKIP_LEAD_INTRO = 5`, credits lead `LP_SKIP_LEAD_CREDITS = 10` — the same
  leads as VLC's `SKIP_COUNTDOWN_*_SEC`) showing e.g. "Skipping Intro in 3" /
  "Next Episode in 7" / "Ending in 7", and at `t ≥ skip_point` calls
  `lpAcceptSkipOffer()` automatically (intro → seek to `end + 1`; credits →
  `_lpAdvanceOrEnd`). The countdown is `timeupdate`-driven, so it freezes while
  paused and tracks seeks, and the user can still tap the tile to skip early or
  **Hide** to cancel. Intro auto-skip only engages while there's still >1 s of
  intro left to skip (matching VLC).
- Dismissed offers add `<filePath>#intro` / `#credits` to `lp.skipDoneFor`
  (suppresses both the manual button and the auto countdown for that file).

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
`local_subtitle_idx` / `subtitle_sel` / `audio_sel` (HLS rendition indices + the
subtitle & audio descriptors) across writes, so a progress
write doesn't wipe any track-pref system.

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
   `lpPlay(itemId, tail, capturedTime, label, app.library_shuffle, app.library_shuffle_scope)`.
   Because stop returns immediately, the device's prep/transcode overlaps the TV
   teardown. The resume seek is pinned to `capturedTime` (applied on
   `loadedmetadata`), so the device lands on the same frame no matter how long prep
   takes — VLC is stopped, so the position doesn't drift while the device prepares.

**Shuffle carries across the handoff (both directions).** The `tail` is already
the shuffled remaining order (VLC's `library_playlist` stays shuffled during a
shuffle session — `vlc_next`/`prev` slice it from `library_shuffle_order`), so the
*order* survives regardless. What the handoff also threads through is the **shuffle
flag + scope**: TV→device passes `app.library_shuffle` / `app.library_shuffle_scope`
(the latter published in `state_snapshot`, masked to `""` when not shuffling) so the
device sets `lp.shuffle`, shows **Exit Shuffle**, walks its random order on
Next/Prev, and records the persisted pref with the right scope; device→TV
(`lpHandoffToVlc`) passes `lp.shuffle` / `lp.shuffleScope` into `playLibraryFiles`'s
`/play` body so the server re-establishes `state.library_shuffle_order`. Without
this the destination treated the handoff as a normal play and snapped Next/Prev back
to natural order. `lpPlay` also skips its single-file→full-series expansion when
`shuffle` is set, so a 1-file shuffled tail isn't padded with un-shuffled episodes.

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
`fcDeviceTileHold` is the hold dispatcher. **Holding the tile while it's `prepping`
hands off to the device immediately** via `handoffToDevice(btn, allowOnDemand=true)`
— the device starts the JIT on-demand stream (`_lpLoadIndex`'s un-prepped fallback)
within a tick rather than waiting out the full encode, and the full ABR bundle keeps
building in the background. The `allowOnDemand` flag is what lets `handoffToDevice`
skip its `_handoffReadyState === false` block in that case. `prepCurrentForDevice` POSTs
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
  `OD_MAX_SESSIONS`. Active playback refreshes `last_access` on every segment fetch.
  `POST …/close` (a sendBeacon on stop/unload) tears a session down promptly; the
  reaper is the backstop.

  > **Reaping vs. a deliberately deep client buffer.** "Active playback refreshes
  > `last_access`" is necessary but **not sufficient** — the player buffers far ahead
  > (`maxBufferLength`, up to 180 s) and while **paused or fully buffered** can go
  > longer than the 90 s idle window without fetching a segment, so the reaper *could*
  > delete a session that's still being watched. The next fetch then `410`s. The
  > client guards both ends (see [§ Client — session keepalive & 410 recovery]
  > (#client-staticindexhtml)): a **keepalive ping** (`_lpOdKeepAlive`, ~30 s) keeps
  > `last_access` fresh while not fetching, and a `410` triggers a **re-prepare at the
  > current position** (`_lpReloadOnDemand`) that promotes to the now-ready full bundle
  > or starts a fresh OD session — never the dead-session retry loop. So a reaped
  > session is recoverable, not a wedge.
- **Background full prep.** `stream-ondemand` also fires `_maybe_start_prep_job`
  (**bulk** queue — low priority, honors the global pause / idle-kill) so a
  *subsequent* play uses the rich ABR/multi-audio bundle. JIT only bridges the gap
  for the current session. (Both can run at once — the below-normal priority on both
  keeps the box usable; see [GOTCHAS.md](GOTCHAS.md).)
- **On-demand-only shows skip the permanent bundle entirely.** When an item is
  flagged `ondemand_only` (admin Storage tab **or** the dashboard episode-page
  toggle, unless admin-locked via `ondemand_only_locked` → `state.ondemand_only_items`),
  `_maybe_start_prep_job` returns `{status:"ondemand_only"}` without building, so the
  background full prep above is a no-op and `/offline-prepare` returns
  `{ready:false, ondemand_only:true}` — every on-device play stays on JIT and nothing
  permanent is written. VLC (TV) playback is unaffected (it never uses HLS). See
  [ADMIN.md § Storage & Compression](ADMIN.md).
- **Flagging an item on-demand-only cancels prep that's already in flight, not just
  queued work.** `_apply_ondemand_only` sets `job["_ondemand_cancelled"] = True` on
  every matching job **before** `proc.terminate()` — mirroring the `_admin_stopped` /
  `_paused_kill` pattern (see [GOTCHAS.md](GOTCHAS.md)). Without that flag the worker
  read the terminate's non-zero exit as a real failure: a full-GPU encode **restarted
  on the transparent path** (the encode just kept running, ignoring the toggle) and a
  regular encode surfaced a spurious `ffmpeg failed`. `_run_offline_job` now treats the
  flag as a clean cancel (drop the partial `.part`, mark `cancelled`, no retry) ahead of
  the full-GPU retry branch; the flag also feeds `is_stopped` so an in-flight
  validate/repair phase bails, and a guard before the encode loop catches a flip that
  lands during validation. Existing permanent bundles are still reclaimed by the
  detached `_purge_ondemand_bundles`.

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
  bundle mode). Subtitles: **on-demand now carries the source's embedded text subs
  too**, not just on-disk sidecars — `stream-ondemand` returns a `subtitles[]` list
  (`_od_subs_meta`, the same `sub_<i>.vtt` shape the bundle uses) and the file endpoint
  extracts each embedded stream to WebVTT **lazily on first fetch** (`_od_extract_sub`,
  cached in the session dir, reaped with it). The client attaches them with the
  **identical** `<track>` machinery as the bundle (`prep.subtitles` + `prep.subs` from
  `_list_sidecar_subs`) — `subBase` resolves to `/api/library/ondemand/<key>/sub_<i>.vtt`.
  So a show played via JIT (un-prepped fallback **or** an *on-demand-only* item) keeps its
  in-MKV subtitles. **Styled ASS/SSA subs are rendered with full libass styling in OD too
  (8.10.0).** `_od_subs_meta` flags a styled sub with `ass_file` (mirroring the bundle
  meta); the raw `sub_<i>.ass` is stream-copied out lazily on first fetch (`_od_extract_ass`)
  and the source's embedded **fonts** are extracted eagerly at session-create
  (`_extract_bundle_fonts` into the session dir, only when a styled sub exists), returned in
  the `stream-ondemand` `fonts` list. `ondemand_file` serves `sub_<i>.ass` + `font_*`, and the
  client's `_lpStyledSubFor` / `lp.fonts` now accept `lp.mode==="ondemand"`, so
  `_lpApplyStyledSub` (SubtitlesOctopus) fires exactly as in bundle mode. The flattened
  `sub_<i>.vtt` stays the fallback on any libass failure. All are reaped with the session dir.
  The **Clip** row is now **shown in OD mode too (8.10.0)** — `_build_clip` cuts straight from
  the source, so `make_clip` accepts either a bundle `master.m3u8` **or** a live OD session for
  the source (both prove it's on disk + probed); the old bundle-only `409` gate is relaxed.
- **Session keepalive & 410 recovery.** The OD session is short-lived (the reaper
  deletes it `OD_SESSION_IDLE_SECS` after the last fetch), but the player buffers far
  ahead and — paused / fully buffered — can out-wait that window, so the session can be
  reaped mid-watch. Two client pieces keep that from wedging playback:
  - `_lpOdKeepAlive()` (called from the 3 s `_lpStallWatch` tick, self-throttled to
    ~30 s) GETs the session's `master.m3u8` while `lp.mode==="ondemand"`, refreshing the
    server's `last_access` even when no segments are being fetched.
  - When a fetch still 410s (a reap won the race, or the tab was backgrounded with
    throttled timers), `_lpReloadOnDemand(reason)` re-runs `_lpLoadIndex` at the current
    playhead instead of the dead-session retry loop. That re-POSTs `/offline-prepare`: if
    the background full prep finished it **switches to the rich bundle**; otherwise it
    creates a fresh OD session (same deterministic `_od_session_key`) and resumes. The
    hls.js path detects the 410 directly (`_lpIsOdSessionGone` → `data.response.code===410`);
    the Safari-native path (no HTTP status on the `<video>` `error`) escalates to the same
    reload once the reconnect backoff has grown past a couple of failed attempts. A
    re-entry guard (`_lpReloading`) collapses the burst of 410s from every queued fragment
    into a single reload.

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
  inside `#lpTrackRow` (the gear-toggled **options panel** — open it via the
  gear button in the bottom control strip; see
  [FRONTEND.md § Local player](FRONTEND.md)).
  `lpClip(seconds, btn)` clips the local `<video>`'s `currentTime`
  using the **selected audio** (`lp.pendingAudioIdx`, which is the source audio
  rendition index). The file is prepped by definition here.

**Shared client core.** `_doClip(itemId, filePath, endSec, seconds, audioIdx,
btn)` POSTs `/api/library/{id}/clip`, then `_shareOrDownload(url, filename)`
delivers the result:

- **iOS app (Capacitor):** opens the clip's host URL in **Safari** via the native
  `BundleDownloader.openExternal({url})` method, where iOS previews the MP4 with a
  native Save-to-Files/Photos + Share sheet. This is required because the dashboard
  runs in the WebView over a plain-http host origin (not a secure context), so
  `navigator.share`'s file API is unavailable and `<a download>` / `window.open`
  are no-ops inside the WebView. The clip URL's random token is the capability, so
  no pairing header is needed. See [GOTCHAS.md](GOTCHAS.md).
- **Web/desktop:** hands the file to the OS **share sheet** when the platform can
  share files (`navigator.canShare({files})` — iOS/Android Safari/Chrome), else
  triggers a download (desktop), with a `window.open` last resort.

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

## Offline download to the iOS app (true offline, not streaming)

Everything above **streams** a bundle from the host (the player swaps `master_url`
to a host URL). The native iOS client (see [IOS_APP_PLAN.md](IOS_APP_PLAN.md), M2)
adds **download-to-device**: the same `.offline_cache/<sha>/` bundle is copied to
the phone and played from a loopback server with **no host connection** (Airplane
Mode). The browser dashboard is untouched — all the glue below is gated behind a
Capacitor native-platform check (`isApp`), so it's inert in a plain browser.

**Download.** A per-row **Download** button (`appDownloadBundle` in
[static/index.html](../static/index.html), app-only via `_appDlBtnHTML`):
1. `GET /api/library/{id}/bundle-manifest?file_path=…` (plan A1). On **409
   not_ready** the app POSTs the normal `/offline-prepare`, polls
   `/offline-job/{id}` until the host bundle is built, then retries the manifest.
2. The manifest's flat `files[]` (name + size) + `cache_key` + `bundle_url` +
   `master_m3u8` are handed to the native **`BundleDownloader`** ([BundleDownloader.swift](../ios-app/ios/App/App/BundleDownloader.swift)),
   which fetches every file over a foreground `URLSession` (held alive across a
   brief backgrounding by a `UIApplication` background-task assertion — a
   *background* session deferred all progress to the next launch, see
   [GOTCHAS.md](GOTCHAS.md)) into `Application Support/StreamLinkBundles/<sha>/`
   (non-evictable, `isExcludedFromBackup`), keyed by the cache sha so a re-download
   of an unchanged source is a no-op. Files are written into the final dir as they
   complete, so a partial download **resumes** by skipping files already on disk at
   their expected size, and completed bundles are **durable across an app kill**.
   Progress + completion are pushed to JS via `bundleProgress` / `bundleComplete`
   events.

> **Single-rung downloads + the quality chooser.** A downloaded file is always
> played at one resolution, so `files[]` is filtered server-side
> (`_bundle_select_rung` in `main.py`) to **one** video rung; the others' playlist +
> init + segments are dropped. The default is the **original (idx 0, source)** rung,
> but the manifest also returns the **full ladder** as `videos[]` (`[{idx, name,
> height, label, bytes}]`, `bytes` = the *download* size if that rung is chosen — its
> own video files plus the shared audio/sub/playlist/meta) via `_bundle_rung_info`,
> and accepts **`?quality=<height>|original`** to ship a specific rung. The app uses
> this for a **Download Quality** chooser (`_appChooseQuality`): on a download, if
> `videos[].length > 1` it lists each rung with its size and re-fetches the manifest
> for the pick. A **bulk** download (card *Download all* → `appDownloadAllBundles`,
> or multi-select *Download (N)* → `epDownloadSelected`) asks **once** via
> `_appBatchQuality` (which probes the first file's ladder) and passes the chosen
> `quality` to every per-file `appDownloadBundle` so the prompt isn't repeated; a
> ≤1-rung bundle skips the prompt entirely. The manifest's **`master_m3u8`** carries a
> non-destructive in-memory rewrite of `master.m3u8` referencing only the kept rung
> (the shared on-disk bundle is untouched and still serves all rungs to streaming
> clients); `BundleDownloader` writes it verbatim from the `masterContent` param
> **before** its resume scan, so the dropped rungs are never fetched or referenced by
> the offline player. (`master_m3u8` is `null` for a ≤1-rung bundle ⇒ master is
> fetched as-is.)

**Offline entry point — the cached player (v8.0.0,
[PLAYER_CACHE_PLAN.md](PLAYER_CACHE_PLAN.md)).** The dashboard is served *by the
host*, so it can't load from the host with no connection — instead the app keeps
a **device snapshot of the dashboard** (`index.html` + `vendor/*`: tailwind,
hls.min.js, the octopus wasm/worker/font) and serves *that* offline, so the SAME
player UI works with no host. The pieces:

- **Snapshot refresh** (`_appRefreshPlayerSnapshot`, static/index.html): every
  online in-app boot fetches `GET /api/player-manifest` ({version, allowlisted
  files+sizes} — see [API.md](API.md)); a version mismatch removes the old
  snapshot, then `BundleDownloader.download` fetches everything from the root
  StaticFiles mount under the sentinel key **`__player__`** (nested `vendor/…`
  names; the native side creates intermediate dirs). Same-version runs are a
  cheap heal (size-match resume). The sentinel is filtered out of the Downloads
  overlay, the durable-queue resumer, and downloads.html's list.
- **Serving — LMS "player mode"** (`start({playerRoot})`,
  [LocalMediaServer.swift](../ios-app/ios/App/App/LocalMediaServer.swift)): the
  snapshot dir is the server root (so the page's absolute `/vendor/*` paths
  work) and the whole `StreamLinkBundles/` storage dir is mounted at
  `/StreamLinkBundles/` — every downloaded bundle is same-origin and the server
  **never restarts while serving the page** (offline playback builds
  `origin + "/StreamLinkBundles/<sha>/"` instead of calling `lms.start`, and
  never sets `_lmsActive`, so nothing ever stops the page's own server). MIME
  map covers html/js/css/wasm — WKWebView won't render an octet-stream page.
- **Offline boot mode** (`?offline=1&host=<url>` → `_appOfflineBoot`,
  static/index.html): Downloads-only — profile from `OfflineStore.getProfile()`
  (the loopback origin's localStorage is empty and its port is ephemeral), no
  SSE/host fetches, the Downloads overlay pinned open as the UI (its Close is
  hidden; the player floats above it via `html.is-offline` CSS), quality menu
  device-rung only, Prev/Next expands across the item's *downloaded* episodes
  from the native index, and a Reconnect probe (auto every 15 s while nothing
  plays + a manual button) that `location.replace(host)`s back to the real
  dashboard. Progress + track picks write to `OfflineStore` explicitly
  (the loopback `/api` 404s RESOLVE — they don't throw — so the online code's
  catch-based fallback would never fire); resume and audio/subtitle picks are
  restored from `getProgress` (which returns the saved track fields). The M3
  sync machinery pushes it all to the host on the next online session.
- **Shell routing** (`ios-app/www/index.html`): probe fails → `goOffline()`
  prefers the snapshot (`goToCachedPlayer`) and falls back to
  **`downloads.html`** — which is now a **feature-frozen fallback** (first-ever
  offline launch / corrupt cache only; new player features belong in
  static/index.html where offline mode inherits them).

**Device-copy playback is always SAME-ORIGIN.** The on-device bundle plays from the
loopback `LocalMediaServer` **only when it can be loaded same-origin** — because
**online the dashboard is a remote host page**, so a device copy loaded directly
would be a *cross-origin* (and, on an HTTPS host, mixed-content) loopback load, which
WKWebView stalls indefinitely (a downloaded episode "never loads while connected"; a
fetch probe can't detect it — CORS `*` lets `fetch()` succeed while the media pipeline
hangs). Two paths keep page and media same-origin:

- **Offline** — the whole dashboard is already served from the loopback player-mode
  LMS, so `_lpLoadIndex`'s device branch (gated `_appOffline || !hlsAvailable`) loads
  the bundle directly. `!hlsAvailable` also covers a no-HLS macOS host.
- **Online proxied playback session (8.7.0)** — the downloaded copy plays while
  connected **with full feature parity** (Play-to-TV, live SSE, library, auto-manage,
  `/api` progress + track sync). `lpPlay` (host side) calls **`_appTryLocalHandoff(itemId,
  filePath, seekTo)`**: if the episode **and** the player snapshot (`__player__`) are
  both fully downloaded, it starts the LMS in **proxy mode** — `lms.start({playerRoot,
  proxyHost: location.origin, proxyToken})` — and `location.replace`s to the loopback
  page with `?proxied=1&host=<origin>&item=&file=&seek=&profile=&am=<automanage prefs>`.
  In proxy mode the native `LocalMediaServer` serves the snapshot + bundles locally and
  **reverse-proxies every non-local request (`/api/*`, SSE, server-stream media) to the
  host** with the bearer token injected (see below + [GOTCHAS.md](GOTCHAS.md)). On the
  loopback page `_appProxied` is set but **`_appOffline` stays false**, so it takes the
  **normal online boot** — SSE, library, Play-to-TV, auto-manage, and real `/api` sync
  all run, just served from the loopback. `_appProxiedSeedStorage` seeds the profile +
  auto-manage prefs (the loopback origin's localStorage is empty); `_appProxiedAutoPlay`
  starts the handed-off episode after the profile restores; `lpStop` →
  **`_appProxiedReturnHost()`** navigates back to the direct host origin (progress
  already synced live via the proxied `/api`). Prev/Next spans the **full series** —
  downloaded episodes play from the device same-origin, non-downloaded ones stream from
  the host through the proxy — one seamless playlist. Only **single-file, non-shuffle**
  plays hand off (per-episode Play / Resume / "On Device"); multi-file "Play All" and
  Shuffle keep their explicit order and stream. Any miss (episode or snapshot not fully
  downloaded, no plugin) falls through to normal server streaming. This supersedes 8.6.0's
  `offline=1&live=1` offline-mode handoff (which lost every live feature during playback).

See [GOTCHAS.md § On-device (loopback) playback is SAME-ORIGIN only](GOTCHAS.md).

**Native reverse proxy (proxied session).** `LocalMediaServer.start({proxyHost,
proxyToken})` makes the hand-rolled `NWListener` server (`HLSStaticServer`) route each
request: a path that resolves to a local snapshot/bundle file (GET/HEAD) is served from
disk (existing `serveFile` + Range); **everything else is forwarded to `proxyHost`** via
a per-request `URLSession` (`ProxyForwarder`). The forwarder streams JSON, long-lived
**SSE** (`/api/events` — the response never ends; chunks relay until the client
disconnects), and **ranged media** (`206`/`Content-Range` relayed verbatim) identically,
with **back-pressure** (the delegate queue blocks on a semaphore until the socket accepts
each chunk — the same discipline `streamBody` uses for local files), injects
`Authorization: Bearer <proxyToken>`, sets `Accept-Encoding: identity` (so the host's
`Content-Length` stays accurate to relay), and **accepts the host's self-signed TLS**
(URLSession server-trust challenge → `.useCredential`, matching the app's
`NSAllowsArbitraryLoads`). Because `master.m3u8`/segments are served at the **relative**
`/api/library/offline-cache/…` path, server-stream media proxies same-origin with no
URL rewriting. Offline player mode leaves `proxyHost` nil, so `/api` still 404s there.

**Playback (offline, or no-HLS host).** When the device branch is taken,
`_lpLoadIndex` checks `BundleDownloader.getLocal({itemId, filePath})`; if a
**complete** local copy exists, `_appStartLocalPlayback` serves it same-origin
(offline: the player-mode LMS's `/StreamLinkBundles/<sha>/`; no-HLS host: a fresh
`LocalMediaServer` over the bundle dir), reads `meta.json` for `audios`/`subtitles`,
and sets `master_url` accordingly — synthesizing the same shape `/offline-prepare`
returns, with **no host round-trip for the stream itself**. Everything downstream
(tracks, embedded `sub_*.vtt` renditions, skip-intro, the custom controls) is
unchanged. On any failure it falls through to the normal prepare path. Online, this
device branch is reached inside the **`proxied=1` session page** (same-origin snapshot),
never on the remote host page directly — the whole "play a downloaded episode" gesture is
redirected to the snapshot up front (see the proxied session above); there a mixed
playlist plays downloaded episodes locally and streams the rest through the proxy.

Extras around the local path (7.17.0; runs whenever the device branch is taken —
offline, no-HLS host, or the `proxied=1` online session):
- **Remembered track picks**: the local path fires a parallel best-effort
  `GET /saved-tracks` (skipped offline via `navigator.onLine`) so audio/subtitle
  restore matches a server stream ([API.md](API.md)).
- **Downloaded-rung identity**: `_appStartLocalPlayback` also fetches the local
  `master.m3u8` and `_appIdentifyLocalRung` matches its single variant URI
  against the `meta.json` ladder (fallback: `RESOLUTION=` height) — this drives
  the device/server **quality menu** (see step 5 above) and the Dev-Mode HUD's
  `source` row (`on-device bundle · 127.0.0.1:<port> · 480p` vs
  `server · <host> · bundle|ondemand`).
- **Warm-next**: `_lpWarmNextEp` skips the host-side prep kick entirely when the
  next episode is already downloaded (it plays instantly from the device, even
  on a no-HLS macOS host).
- **LMS lifecycle**: the loopback server is stopped eagerly when an episode
  advance (or a server-quality switch) moves playback local→server, and `lpStop`
  keys its teardown on `lp._lmsActive` — not `lp._localBase`, which each load
  nulls — so it can't be left running.

**Scope (M2).** Offline **playback** of the bundle; embedded text subtitles (the
`sub_*.vtt` renditions inside the bundle) work offline, but host-side **sidecar**
subs (served via `/subtitle?path=`, source-file-dependent) are online-only.

**Offline progress + auto-sync (M3).** The offline `downloads.html` player now
captures watch progress (the same `timeupdate`/`pause`/`seeked`/`ended`/`pagehide`
events the dashboard uses) into the native **`OfflineStore`**
([OfflineStore.swift](../ios-app/ios/App/App/OfflineStore.swift)) — a durable,
file-backed log keyed by `(profileId, itemId, filePath)` (Application Support,
backup-excluded), the only place offline history can live (the host dashboard can't
load offline). Playback also **resumes from the saved offline position**. The active
profile is pushed to the store by the dashboard (`OfflineStore.setProfile`) while
online so the offline player records under the right profile. When the device
reconnects, the dashboard drains the store: `_appFlushOfflineProgress` reads
`OfflineStore.pending()` (events where `clientUpdatedAt > baseSyncedAt`) and POSTs
them to **`/api/sync/progress`** (plan A2 — conflict detection via the
`base_synced_at` watermark; see [API.md](API.md) / [LIBRARY_DATA.md](LIBRARY_DATA.md)),
then `OfflineStore.markSynced()` records each applied file's new watermark. It fires
on profile-select and the window `online` event; the dashboard's own
`saveProgress` also falls back to the store when an online POST fails. All of this
is `isApp`-gated, so the browser dashboard is unaffected.

**Conflict resolution (M4).** When `/sync/progress` returns genuine divergences in
`conflicts` (the same file advanced both offline *and* elsewhere, positions too far
apart to auto-merge), `_appFlushOfflineProgress` collects them and opens the
`#syncConflictModal` "keep mine / keep server" UI (`_appShowConflicts` →
`_appRenderConflicts`; titles enriched from each download's persisted `meta`). On
**Apply**, `_appApplyConflictResolutions` POSTs the choices to
**`POST /api/sync/resolve`** (plan A3): a "keep mine" win writes the device
values server-side and the device just advances its watermark via
`OfflineStore.markSynced`; a "keep server" win writes nothing host-side and the device
adopts the server's values with a **forced** `OfflineStore.seedProgress` (`force:true`
overrides the unsynced-record guard). Until resolved, conflicts stay pending and
re-report on the next flush.

**Bidirectional seeding (so offline resume reflects online history).** Push alone
would mean an episode watched partway *online* resumes at 0 offline. So sync is
two-way: **`POST /api/sync/pull`** returns the profile's current server
progress for the device's downloads, and `OfflineStore.seedProgress` adopts it as
the local baseline (settled — never re-pushed; never clobbers an unsynced offline
edit). The dashboard seeds at **download time** (the bundle-manifest returns the
file's `progress`) and again on every reconnect (`_appSeedFromServer`, run right
after `_appFlushOfflineProgress`).

**Offline picker metadata.** The bundle-manifest also returns a `meta` block —
series/title, season·episode, episode name, overview, and the **poster inlined as a
`data:` URL** (`_tmdb_image_data_url`, so it renders with no network). `BundleDownloader`
persists `meta` in its index and exposes it via `list()`/`getLocal()`; the offline
`downloads.html` **groups downloads by series** with poster + episode list (S·E +
name) + overview + per-episode **watch-progress bars** ("Watched" / "Resume …") read
from `OfflineStore`.

---

## See also

- [FRONTEND.md](FRONTEND.md) — JS function reference for `lp*` / `pc*` / `prep*`
- [BACKEND.md](BACKEND.md) — `_ffprobe_full`, `_run_offline_job`, etc.
- [API.md](API.md) — endpoint signatures
- [GOTCHAS.md](GOTCHAS.md) — Safari MSE quirks, hls.js segment alignment,
  ffmpeg version floor, etc.
