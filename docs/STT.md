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
`<stem>.<lang>.ai.srt` (e.g. `Movie.eng.ai.srt`, `Show.jpn.ai.srt`). This is
the same shape the OpenSubtitles download flow already produces, so it flows
into both players through existing plumbing with **no manifest/bundle changes**:

- **VLC (TV):** `await vlc("addsubtitle", val=<abs path>)` loads + selects it
  (see `_attach_stt_to_vlc`, mirroring `download_subtitle`).
- **On-device HLS player:** `_list_sidecar_subs()` already scans for `.srt`/`.vtt`
  next to the source and serves them via `/api/library/{id}/subtitle` (SRT→VTT
  on the fly). The player attaches each as a `<track>` child.

The `.ai` segment in the filename marks the track as machine-generated:
`_list_sidecar_subs` strips it from the parsed language tag and sets `ai: true`
so the UI can label it “(AI)”. It's also how idempotency works — `stt.has_ai_subs(src)`
checks for an existing `<stem>.*.ai.srt` so we never regenerate.

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
2. **Pass 1 — transcribe** (auto language detect) → `<stem>.<detected>.ai.srt`.
3. **Pass 2 — translate to English**, only when the detected language isn't
   English and `want_translation` is on → `<stem>.eng.ai.srt`. (English audio
   skips this — transcription already *is* English.)

Returns `{"tracks": [{path, lang, translated}], "error": None}`.

Model: a **multilingual** GGML model (default `ggml-base`). `.en` models can't
translate, so a multilingual model is mandatory — see SETUP. Larger models
(`small`/`medium`) are more accurate but slower; swap by dropping the file in
`tools/whisper/` and pointing `_WHISPER_MODEL` at it.

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
  `setup.py` into `tools/whisper/`. CPU build by default; a CUDA build is a
  manual upgrade (drop it in and re-point `_WHISPER_BIN`).
- **Linux:** no reliable prebuilt — build whisper.cpp so `whisper-cli` is on
  PATH; the model still downloads.
- **macOS (dev only):** `brew install whisper-cpp` + model download. Note HLS
  prep is TCC-blocked on macOS, so **preprocess** STT won't run there, but the
  **VLC on-demand** path still works.

---

## Files

| File | Role |
|------|------|
| `stt.py` | whisper.cpp wrapper: wav extract, transcribe/translate, lang map, `generate()` |
| `main.py` | `_stt_cfg`, `_needs_stt_subs`, `_canon_lang`, `_stt_jobs`, `_run_stt_job`, `_maybe_start_stt_job`, `_ensure_stt_for`, `_attach_stt_to_vlc`, the 3 STT endpoints + admin endpoints, `_list_sidecar_subs` `ai` flag |
| `setup.py` | `whisper_candidates`, `whisper_model_candidates`, `install_stt_deps`, `_portable_install_whisper_windows`, `_download_whisper_model`, `.env` mapping |
| `static/index.html` | `generateSubsVlc`, `_pollSttJob`, `lpGenerateSubs`, `_lpAttachSidecarSubs`, `sttAvailable`, subtitle-modal AI row, `#lpGenSubBtn` |
| `static/admin.html` | System-tab "Auto-Generated Subtitles" card + `loadStt`/`saveStt`/`toggleStt` |

## See also

- [STREAMING.md](STREAMING.md) — sidecar subs, HLS prep pipeline, priority discipline
- [API.md](API.md) — endpoint signatures
- [SETUP.md](SETUP.md) — whisper.cpp + model bundling
- [GOTCHAS.md](GOTCHAS.md) — whisper translate-is-English-only, model must be multilingual
