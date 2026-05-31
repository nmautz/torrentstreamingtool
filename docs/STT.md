# Auto-Generated Subtitles (Speech-to-Text)

How StreamLink transcribes audio into subtitles for sources that ship none
usable. Lives across `stt.py` (the whisper.cpp wrapper), `main.py` (trigger +
job machinery + endpoints), `setup.py` (binary/model bundling), and the two
players' subtitle menus in `static/index.html` / settings in `static/admin.html`.

Read this when changing anything related to:

- `stt.py` — `generate()`, `_run_whisper()`, `_extract_wav()`, language mapping
- the trigger `_needs_stt_subs()` / `_stt_cfg()` / `_ensure_stt_for()` in `main.py`
- the STT job runner `_run_stt_job` / `_maybe_start_stt_job` / `_stt_jobs`
- `POST /api/library/{id}/generate-subtitles`, `POST /api/subtitles/generate`,
  `GET /api/stt-job/{id}`, `GET`/`POST /api/admin/stt`
- the **Generate with AI** action (VLC subtitle modal) / **AI** button (on-device
  player) / the admin System-tab "Auto-Generated Subtitles" card
- whisper.cpp download in `setup.py` (`install_stt_deps`, `_portable_install_whisper_windows`)

---

## The core idea: it's just a sidecar

STT output is a **sidecar `.srt` written next to the source video**, named
`<stem>.<lang>.ai.<model>.srt` (e.g. `Movie.eng.ai.base.srt`,
`Show.jpn.ai.medium.srt`). This is the same shape the OpenSubtitles download
flow already produces, so it flows into both players through existing plumbing
with **no manifest/bundle changes**:

- **VLC (TV):** `await vlc("addsubtitle", val=<abs path>)` loads + selects it
  (see `_attach_stt_to_vlc`, mirroring `download_subtitle`).
- **On-device HLS player:** `_list_sidecar_subs()` already scans for `.srt`/`.vtt`
  next to the source and serves them via `/api/library/{id}/subtitle` (SRT→VTT
  on the fly). The player attaches each as a `<track>` child.

The `.ai` segment marks the track as machine-generated; the segment after it is
the **model name** that produced it (`base`/`small`/`medium`/… — derived from
the configured `ggml-*.bin`). `_list_sidecar_subs` strips both from the parsed
language tag and returns `ai: true`, the `model`, and `stale: true` when that
model **or the transcription-pipeline generation** (the `g<N>` tag — see
“Timing precision” below) differs from what's configured now. The UI labels the
track with its model (“English (AI · base)”). `stt.has_ai_subs(src)` (any
generated sub exists) gates idempotent *preprocess*; `stt.ai_subs_stale(src)`
drives the **Regenerate** affordance.

### Regenerating on a model change

Only an **explicit** request regenerates — preprocess/overnight prep stays
idempotent (`_ensure_stt_for` skips when any generated sub exists), so changing
the model never silently re-transcodes the whole library. The manual paths
(`_maybe_start_stt_job`) treat a **same-model** sub as `cached` and a
**different-model** sub as regenerable; on the on-device player the subtitle
button flips from **AI** → **Regen** when `stale`. `generate()` tags each new
file with the current model and, after the new ones are written, removes any
superseded generated subs (different model, or legacy untagged) so the switch is
clean. Legacy `<stem>.<lang>.ai.srt` files (no model tag, pre-v4.3) read as
model `""` → always stale → offered for regen, which migrates them to the tagged
form.

---

## When generation triggers — `_needs_stt_subs(info, default_lang)`

A source warrants generated subs when it has **no usable text subtitle**:

1. **No subtitle tracks at all.**
2. **Only image-based tracks** (PGS / VOBSUB / DVB / XSUB) — these can't go into
   HTML5 `<track>`s and aren't burned in, so for our purposes they're "no subs".
   `_ffprobe_full` already flags them `image_based`; `_needs_stt_subs` ignores them.
3. **An admin default language is set and no text track matches it.** Languages
   are canonicalized (`_canon_lang`) so `en`/`eng`, `ja`/`jpn`, etc. compare
   equal. `default_language == ""` means "any text sub is acceptable" (only case
   1 / 2 trigger).

> **Whisper translate is English-only.** Whisper can transcribe the spoken
> language, and *translate to English* — nothing else. So if the admin sets a
> non-English default (say Spanish) and the audio is English, the trigger fires
> but STT can only produce English (transcription). It does the best it can; the
> requested language simply can't be synthesised from foreign audio. Documented
> here and in [GOTCHAS.md](GOTCHAS.md).

---

## What gets produced — `stt.generate(src, want_translation=…)`

1. Extract the **first audio track** to 16 kHz mono PCM WAV via the bundled
   ffmpeg (whisper's required input).
2. **Pass 1 — transcribe** (auto language detect) → `<stem>.<detected>.ai.<model>.srt`.
3. **Pass 2 — translate to English**, only when the detected language isn't
   English and `want_translation` is on → `<stem>.eng.ai.<model>.srt`. (English
   audio skips this — transcription already *is* English.)

Returns `{"tracks": [{path, lang, translated, model}], "error": None}`.

Model: a **multilingual** GGML model (default `ggml-base`). `.en` models can't
translate, so a multilingual model is mandatory — see SETUP. Larger models
(`small`/`medium`) are more accurate but slower; swap by installing one via the
admin Components card (or dropping the file in `tools/whisper/` and pointing
`_WHISPER_MODEL` at it) — existing subs then read as stale and can be regenerated
(see "Regenerating on a model change" above).

### Timing precision — DTW, word-boundary cues, and cross-window drift control

whisper's native segment timestamps are coarse and **drift around long pauses**:
a segment carries a single start/end, so a line lingers across silence or the
next one starts early. `_run_whisper` passes two flags to fix this:

- **`-dtw <preset>`** — token-level timestamps via Dynamic Time Warping of the
  decoder's cross-attention against the audio. Far more accurate boundaries than
  the decoder's timestamp tokens, and it *respects pauses*. Needs **no extra
  download** (alignment heads are built into whisper.cpp). The preset must name
  the loaded model's architecture, so `stt._dtw_preset()` maps `model_name()` →
  preset via `_DTW_PRESETS` (`base`→`base`, `large-v3`→`large.v3`, `.en`→`.en`,
  …) and **disables DTW** for any model it can't map — a wrong preset would error
  the run. The offered sizes (base/small/medium) all map.
- **`-ml STT_MAX_LEN` + `-sow`** — re-split each segment into shorter
  word-boundary cues (≤ 80 chars). Each cue then carries its **own** DTW-accurate
  timing, so a pause becomes a real gap between two cues instead of one stretched
  block. `-sow` keeps splits off mid-word.

> A literal **one-word-per-cue** track (`-ml 1`) would be unreadable as a
> subtitle; DTW alignment is the higher-precision *and* readable answer.

#### Cross-window drift — why we no longer use `--vad` or `-mc 0`

DTW and `-ml`/`-sow` fix *within-cue* boundaries. They don't directly fight the
**cross-window drift** that can accumulate over long (~1 hr) media: whisper
decodes in 30-second windows and advances its audio cursor by its own timestamp
tokens, so one misjudgment (silence, music, a hallucinated repetition) can shift
what follows. In practice DTW's audio-anchored timings keep that small. Two
levers were tried against it in 4.6.0 and **both were reverted** — each hurt more
than it helped:

- **`--vad --vad-model <silero>`** (added 4.6.0, removed from the pipeline in
  **4.6.2**) — Silero VAD detects speech regions up front and whisper transcribes
  each on a *filtered* (silence-removed) timeline, then remaps the result back to
  real time. The remap is the problem: whisper.cpp's region→original mapping
  yields **overlapping / out-of-order cue timestamps**, and the detector lets
  **music/ambience through as "speech"**, so whisper hallucinates lines and stamps
  them at arbitrary offsets. Net effect: subs showing **too early, or
  seconds-to-30s late** — the exact regression VAD was meant to cure. (Known
  whisper.cpp issue class: ggml-org/whisper.cpp #3207, #3250, #3711.) The pipeline
  now **never passes `--vad`**.
- **`-mc 0` (`--max-context 0`)** (added 4.6.0, reverted **4.6.1**) — curbed
  repetition loops but stripped the cross-window context whisper needs to decode
  ambiguous audio, **drastically degrading quality** (worst combined with VAD's
  short segments). See [GOTCHAS.md](GOTCHAS.md).

The Silero model (`ggml-silero-v5.1.2.bin`, ~1 MB) is still bundled by `setup.py`
into `tools/whisper/vad/` and installable from **Admin → System → Optional
Components**; `main._vad_active()` / `stt._whisper_supports_vad()` still report
whether the build *could* accept `--vad` (surfaced in `/api/admin/stt` and
`/api/admin/components`). But the transcription pipeline is **dormant** with
respect to VAD — installing the model changes nothing unless/until whisper.cpp's
VAD remap is fixed and we re-enable it. (Relabeling the now-misleading "reduces
timing drift" component copy is a tracked follow-up.)

`_run_whisper` runs **GPU first, then forces CPU (`-ng`) on failure** — no VAD
attempt in the chain. All flags are orthogonal to GPU offload.

#### Picking up the improvement on existing subs — the generation tag

Sidecars are named `<stem>.<lang>.ai.<model>.g<N>.srt`. The `g<N>` segment
(`g{STT_VERSION}` — see `sub_gen_tag()`) records the **transcription-pipeline
generation**. `ai_subs_stale` / `_list_sidecar_subs` flag a sub stale when
**either** the model **or** the gen tag differs from the current config, so subs
made on an older pipeline read as stale → the on-device player shows **Regen** and
VLC offers **Generate with AI**. Legacy untagged files parse as model `""` / gen
`""` → always stale → migrated to the tagged form on regen.

> Pre-4.6.2 names could carry a trailing `v` (a VAD model was installed at
> generation time); `_list_ai_subs` still parses those — its `g\d+v?` regex
> matches both forms — and the `g4` bump retires them all as stale, so any
> `…v`-tagged sub is offered for a clean, VAD-free regenerate.

---

## Preprocess vs on-demand (both are wired)

### Preprocess (bulk, off the playback path)
- **After every HLS stream-prep** — `_run_offline_job` calls `_ensure_stt_for`
  on success, reusing the ffprobe it already ran. Enqueues a **bulk** STT job
  that runs *after* the HLS encode releases the shared concurrency slot.
- **Overnight auto-prep** — `_enqueue_library_prep` already preps every file;
  for files that were **already HLS-cached** (so the post-encode hook never
  fires) it calls `_ensure_stt_for` directly to backfill.

### On-demand (interactive, ignores the bulk pause gate)
- **On-device player:** the **AI** button → `POST /api/library/{id}/generate-subtitles`
  → poll `GET /api/stt-job/{id}`. On done the new sidecar `<track>`s are attached
  without reloading the stream (`_lpAttachSidecarSubs`) and the first is selected.
- **VLC:** the **Generate with AI** action in the subtitle modal →
  `POST /api/subtitles/generate` (current playback file, `vlc_attach=True`) →
  poll. On done the server loads + selects the track in VLC.

---

## Concurrency & priority

STT jobs share the single `OFFLINE_JOB_CONCURRENCY` semaphore with HLS prep, so
a transcription never runs *alongside* an encode (both are CPU-heavy). Bulk STT
honors `state.prep_paused` (the global pause gate); interactive STT ignores it.
whisper runs at lowered OS priority (`stt._LOWPRIO_KW` / `nice -n 10`) for the
same reason ffmpeg/the analyzer do — the server raises itself to HIGH and
children would otherwise inherit it and lag the UI. See [STREAMING.md](STREAMING.md)
§ "Staying responsive while prepping".

> **STT is slower than the HLS transcode.** A 45-minute episode is minutes of
> CPU even on `base`. That's why the default path is preprocess (overnight),
> with on-demand as an explicit, clearly-progress-indicated fallback — never a
> silent stall at play time.

---

## Availability & platform notes

`state.stt_available` (cached `stt.is_available()`) is true iff the whisper
binary, a model, and ffmpeg are all present. The UI gates the Generate
affordances on it; the admin card shows an "unavailable" banner otherwise.

- **Windows (primary):** portable whisper.cpp build + model downloaded by
  `setup.py` into `tools/whisper/`. CPU build by default; a **CUDA/cuBLAS build**
  (GPU) is selectable in the admin Components card — see GPU acceleration below.
- **Installing without a terminal:** the auto-updater runs `setup.py`
  non-interactively and skips the whisper download, so on an auto-updating box
  install it from **Admin → System → Optional Components** instead (binary +
  model, with a size picker). It streams the download, writes `.env`, and clears
  the availability cache so STT lights up without a restart. See
  [ADMIN.md](ADMIN.md) / [SETUP.md](SETUP.md).
- **Linux:** no reliable prebuilt — build whisper.cpp so `whisper-cli` is on
  PATH; the model still downloads.
- **macOS (dev only):** `brew install whisper-cpp` + model download. Note HLS
  prep is TCC-blocked on macOS, so **preprocess** STT won't run there, but the
  **VLC on-demand** path still works.

---

## GPU (CUDA) acceleration

> **NVENC ≠ CUDA.** The `_has_nvenc()` probe / "GPU: NVENC" badge is about the
> NVIDIA *video encoder* (used by the HLS ffmpeg transcode). Whisper uses
> **CUDA/cuBLAS** general compute — a different subsystem. NVENC being available
> is a good *signal* that a CUDA-capable GPU exists, which is why the Components
> card recommends a CUDA build when `nvenc` is true, but the CPU whisper build
> never uses the GPU regardless.

whisper.cpp ships three Windows builds per release: the CPU build
(`whisper-bin-x64.zip`) and two cuBLAS builds
(`whisper-cublas-12.x-bin-x64.zip` ≈ 440 MB, `whisper-cublas-11.8.0-…` ≈ 60 MB).
`setup._resolve_whisper_win_url(build)` resolves the right asset by build key
(`cpu`/`cuda12`/`cuda11`) from the releases API. The admin picks the build in the
**Optional Components** card; CUDA 12 is the default when a GPU is detected.

- **No flag needed to use the GPU** — a cuBLAS build auto-offloads. `stt._run_whisper`
  passes the same args for every build.
- **Runtime CPU fallback** — `_run_whisper` runs once, and on failure retries
  once with `-ng` (`--no-gpu`). So a cuBLAS build on a too-old driver (CUDA init
  fails) degrades to CPU instead of erroring; `-ng` is a harmless no-op for the
  CPU build. The cost is a wasted first attempt only on actual failure.
- **Driver compatibility** — CUDA 12.x needs a newer NVIDIA driver than 11.8.
  The cuBLAS zips bundle the CUDA *runtime* DLLs (no toolkit install needed) but
  still require a compatible driver. If unsure, CUDA 11 is the wider-compatible,
  much smaller download; the CPU fallback covers a wrong pick either way.

---

## Files

| File | Role |
|------|------|
| `stt.py` | whisper.cpp wrapper: wav extract, transcribe/translate, lang map, `generate()`, `model_name`, `_dtw_preset` (DTW timing presets), `whisper_vad_model`/`_whisper_supports_vad` (optional VAD), `sub_gen_tag` (pipeline-generation tag), `_list_ai_subs`, `has_ai_subs`, `ai_subs_stale` |
| `main.py` | `_stt_cfg`, `_needs_stt_subs`, `_canon_lang`, `_stt_jobs`, `_run_stt_job`, `_maybe_start_stt_job`, `_ensure_stt_for`, `_attach_stt_to_vlc`, `_vad_active`, the 3 STT endpoints + admin endpoints, the `whisper_vad` component, `_list_sidecar_subs` `ai`/`stale` flags |
| `setup.py` | `whisper_candidates`, `whisper_model_candidates` (excludes silero), `whisper_vad_candidates`, `install_stt_deps`, `_portable_install_whisper_windows`, `_download_whisper_model`, `_download_whisper_vad_model`, `detect_tools` (prefers the `.env` model so auto-update can't reset it), `.env` mapping |
| `static/index.html` | `generateSubsVlc`, `_pollSttJob`, `lpGenerateSubs`, `_lpAttachSidecarSubs`, `sttAvailable`, subtitle-modal AI row, `#lpGenSubBtn` |
| `static/admin.html` | System-tab "Auto-Generated Subtitles" card + `loadStt`/`saveStt`/`toggleStt` |

## See also

- [STREAMING.md](STREAMING.md) — sidecar subs, HLS prep pipeline, priority discipline
- [API.md](API.md) — endpoint signatures
- [SETUP.md](SETUP.md) — whisper.cpp + model bundling
- [GOTCHAS.md](GOTCHAS.md) — whisper translate-is-English-only, model must be multilingual
