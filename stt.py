"""Speech-to-text subtitle generation via whisper.cpp.

When a source file has no usable text subtitle track (none at all, only
image-based PGS/VOBSUB, or none matching the admin's preferred language), this
module transcribes the audio into a sidecar `.srt` placed next to the source —
which both VLC (`addsubtitle`) and the HLS on-device player (`_list_sidecar_subs`
→ `/api/library/{id}/subtitle`) pick up through existing plumbing. No bundle or
manifest changes are needed.

The whisper.cpp CLI + a multilingual GGML model are bundled by setup.py under
`tools/whisper/` (paths recorded in `.env` as `_WHISPER_BIN` / `_WHISPER_MODEL`).
A multilingual model is required: whisper's translate task only emits English,
so for non-English audio we additionally produce an English-translated track.

All subprocess work runs blocking (call via asyncio.to_thread) at lowered OS
priority — same discipline as analyzer.py / HLS prep — so a transcription never
starves the StreamLink server, which runs at raised priority and would otherwise
pass that priority on to children.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, Optional

# Bumped whenever the transcription *pipeline* changes in a way that affects
# output (timing or content). Embedded in sidecar names as `g<N>` so subs from an
# older pipeline read as stale and the UI offers Regenerate. v2 added `-mc 0` +
# optional Silero VAD; v3 dropped `-mc 0` (it tanked transcription *quality* — see
# GOTCHAS), keeping VAD for drift; v4 dropped `--vad` too — whisper.cpp's
# per-speech-region timestamp remapping produced overlapping / out-of-order cue
# times and let music through as hallucinated "speech" (subs showing too early, or
# seconds-to-30s off — the exact regression VAD was meant to cure). Back to the
# proven 4.5.0 timing path (`-dtw` + `-ml`/`-sow`). See GOTCHAS.
STT_VERSION = 4

# Marker embedded in generated sidecar names: "<stem>.<lang>.ai.srt". The `.ai`
# segment distinguishes machine-generated subs from real/downloaded ones so we
# can detect "already generated" and the UI can flag them.
AI_SUFFIX = "ai"

# Run whisper at lowered OS priority so a transcription yields CPU to the web
# server / VLC / qBit. Windows: BELOW_NORMAL_PRIORITY_CLASS via creationflags.
# POSIX: prepend `nice -n 10`. Mirrors analyzer._LOWPRIO_KW / main._FFMPEG_*.
_LOWPRIO_KW: dict = {}
if os.name == "nt":
    _LOWPRIO_KW["creationflags"] = 0x00004000  # BELOW_NORMAL_PRIORITY_CLASS

# Cap whisper worker threads. It runs below-normal, but an unbounded thread
# count still spikes scheduler pressure; 4 keeps headroom on typical hosts.
STT_THREADS = 4

# Subtitle timing precision. whisper's native segment timestamps are coarse and
# drift around long pauses — a single segment carries one start/end, so a line
# lingers across silence or the next line starts early. Two flags fix this:
#
#  * `-dtw <preset>` computes token-level timestamps by Dynamic-Time-Warping the
#    decoder's cross-attention against the audio — far more accurate boundaries
#    than the decoder's timestamp tokens, and it respects pauses. Needs no extra
#    download (alignment heads are built into whisper.cpp), but the preset must
#    name the loaded model's architecture, so we only enable it when we can map
#    the configured model confidently (see `_dtw_preset`).
#  * `-ml`/`-sow` re-split each segment into shorter word-boundary cues, so every
#    cue carries its own (now DTW-accurate) timing and a pause becomes a real
#    gap between two cues instead of one stretched block.
#
# DTW + ml/sow fix *within-cue* boundaries. They don't fight the *cross-window*
# drift that can accumulate over long media (whisper decodes in 30s windows and
# advances its cursor by its own timestamp tokens, so one misjudgment shifts what
# follows) — but in practice `-dtw`'s audio-anchored timings keep that small, and
# the two levers we tried against it were each worse than the disease:
#  * `--vad` (Silero, v4.6.0) re-anchored timing per detected speech region, but
#    whisper.cpp's region→original-time remapping produced overlapping /
#    out-of-order cues AND treated music/ambience as speech (hallucinated lines at
#    wrong offsets) — i.e. subs too early, or seconds-to-30s off. Dropped from the
#    pipeline in v4 (the model stays installable for the admin badge, but we never
#    pass `--vad`).
#  * `-mc 0` (v4.6.0) curbed repetition loops but stripped the decode context the
#    model needs and tanked quality; reverted in v4.6.1.
# See GOTCHAS.
STT_MAX_LEN = 80  # max characters per cue for -ml; 0 would disable splitting

# whisper.cpp `-dtw` alignment-head presets, keyed by the model's short name as
# `model_name()` reports it (dots sanitized to hyphens, so `.en` → `-en`,
# `large-v3`). Passing a preset whose heads don't match the loaded model errors,
# so anything not here disables DTW rather than risking a failed run.
_DTW_PRESETS = {
    "tiny": "tiny", "tiny-en": "tiny.en",
    "base": "base", "base-en": "base.en",
    "small": "small", "small-en": "small.en",
    "medium": "medium", "medium-en": "medium.en",
    "large-v1": "large.v1", "large-v2": "large.v2", "large-v3": "large.v3",
    "large": "large.v3",
}


def _dtw_preset() -> str:
    """DTW alignment preset matching the configured model, or '' if unmappable."""
    return _DTW_PRESETS.get(model_name().lower(), "")


def _lp(cmd: list[str]) -> list[str]:
    """Prefix `nice -n 10` on POSIX (when available) so the child de-prioritizes."""
    if os.name == "posix" and shutil.which("nice"):
        return ["nice", "-n", "10", *cmd]
    return cmd


def _env_bin(env_key: str) -> Optional[str]:
    """Read a path from .env. Falls back to PATH lookup of the bare name."""
    env_file = Path(__file__).parent / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line.startswith(f"{env_key}="):
                val = line.split("=", 1)[1].strip()
                if val and Path(val).exists():
                    return val
    return None


def whisper_bin() -> Optional[str]:
    return (_env_bin("_WHISPER_BIN")
            or shutil.which("whisper-cli")
            or shutil.which("whisper-cpp"))


def whisper_model() -> Optional[str]:
    p = _env_bin("_WHISPER_MODEL")
    return p if p and Path(p).exists() else None


def whisper_vad_model() -> Optional[str]:
    """Path to the bundled Silero VAD model (`_WHISPER_VAD_MODEL`), or None.

    Optional — when present (and the whisper build supports `--vad`), whisper
    segments speech first and re-anchors each region's timing, which stops the
    cross-window timestamp drift that accumulates over long media. NEVER part of
    is_available(): absence just means the pre-VAD behaviour."""
    p = _env_bin("_WHISPER_VAD_MODEL")
    return p if p and Path(p).exists() else None


# whisper.cpp gained `--vad` (Silero speech detection) in ~v1.7.5. A user-built
# Linux/macOS binary may predate it, and passing --vad to such a build errors the
# whole run — so probe `--help` once per binary path (cached) and only pass --vad
# when support is confirmed. The bundled Windows build (v1.8.4+) always has it.
_VAD_SUPPORT: dict[str, bool] = {}


def _whisper_supports_vad(binp: str) -> bool:
    if binp in _VAD_SUPPORT:
        return _VAD_SUPPORT[binp]
    supported = False
    try:
        proc = subprocess.run([binp, "--help"], capture_output=True, text=True,
                              timeout=15, **_LOWPRIO_KW)
        supported = "--vad" in ((proc.stdout or "") + (proc.stderr or ""))
    except (OSError, subprocess.TimeoutExpired):
        supported = False
    _VAD_SUPPORT[binp] = supported
    return supported


def sub_gen_tag() -> str:
    """Pipeline-generation marker embedded in generated sidecar names (after the
    model segment): `g<STT_VERSION>`. A bump makes existing subs read as stale so
    the UI offers Regenerate to pick up the better timing. (Pre-v4 names could
    carry a trailing `v` for VAD; `_list_ai_subs` still parses those, and the v4
    bump retires them as stale.)"""
    return f"g{STT_VERSION}"


def ffmpeg_bin() -> Optional[str]:
    return _env_bin("_FFMPEG_BIN") or shutil.which("ffmpeg")


def is_available() -> bool:
    """True iff every piece STT needs is present: whisper binary, a model, ffmpeg."""
    return bool(whisper_bin() and whisper_model() and ffmpeg_bin())


# whisper.cpp reports detected language as ISO 639-1 (2-letter). Map the common
# ones to the 3-letter codes the rest of the app (_LANG_NAMES, sidecar lang
# tags) speaks. Unknown codes pass through unchanged.
_ISO1_TO_ISO3 = {
    "en": "eng", "ja": "jpn", "es": "spa", "fr": "fre", "de": "ger",
    "it": "ita", "pt": "por", "ru": "rus", "zh": "chi", "ko": "kor",
    "ar": "ara", "hi": "hin", "nl": "nld", "sv": "swe", "fi": "fin",
    "no": "nor", "da": "dan", "pl": "pol", "tr": "tur", "uk": "ukr",
    "th": "tha", "vi": "vie",
}


def _to_iso3(code: str) -> str:
    code = (code or "").strip().lower()
    if len(code) == 3:
        return code
    return _ISO1_TO_ISO3.get(code, code or "und")


def model_name() -> str:
    """Short name of the currently-configured model, e.g. `ggml-base.bin` → 'base'.
    Embedded in generated sidecar names so we can detect a model change. Empty if
    no model is configured."""
    p = whisper_model()
    if not p:
        return ""
    stem = Path(p).stem                       # ggml-base
    name = stem[5:] if stem.lower().startswith("ggml-") else stem
    return re.sub(r"[^A-Za-z0-9_-]", "-", name) or "model"


def _list_ai_subs(src: Path) -> list[tuple[Path, str, str]]:
    """Every machine-generated sidecar next to `src`, as (path, model, gen).

    Generated files are `<stem>.<lang>.ai.<model>[.<gen>].srt`. `model` (after
    `ai`) records which whisper model produced it; `gen` (a `g<N>[v]` tag after
    it — see `sub_gen_tag`) records the transcription-pipeline generation. Either
    differing from the current config flags the sub stale. Both are `""` for the
    legacy pre-tag formats (treated as a mismatch → offered for regen)."""
    out: list[tuple[Path, str, str]] = []
    try:
        for p in src.parent.iterdir():
            if not p.is_file() or p.suffix.lower() not in (".srt", ".vtt"):
                continue
            if not p.stem.startswith(src.stem + "."):
                continue
            segs = p.stem[len(src.stem) + 1:].split(".")
            if AI_SUFFIX not in segs:
                continue
            i = segs.index(AI_SUFFIX)
            model = segs[i + 1] if i + 1 < len(segs) else ""
            gen = (segs[i + 2] if i + 2 < len(segs)
                   and re.fullmatch(r"g\d+v?", segs[i + 2]) else "")
            out.append((p, model, gen))
    except OSError:
        pass
    return out


def has_ai_subs(src: Path) -> bool:
    """True if any machine-generated sidecar already exists next to `src`."""
    return bool(_list_ai_subs(src))


def ai_subs_stale(src: Path) -> bool:
    """True if generated subs exist but at least one was made with a different
    model OR an older transcription pipeline than the current config (so a
    Regenerate is worth offering)."""
    cur_model = model_name()
    cur_gen = sub_gen_tag()
    subs = _list_ai_subs(src)
    return bool(subs) and any(m != cur_model or g != cur_gen for _, m, g in subs)


def ai_sub_model(name: str, stem: str) -> str:
    """Parse the model tag out of a sidecar filename stem (or '' if none/not AI)."""
    if not name.startswith(stem + "."):
        return ""
    segs = name[len(stem) + 1:].split(".")
    if AI_SUFFIX not in segs:
        return ""
    i = segs.index(AI_SUFFIX)
    return segs[i + 1] if i + 1 < len(segs) else ""


def _extract_wav(src: Path, out_wav: Path) -> bool:
    """Decode the source's first audio track to 16 kHz mono PCM — whisper's
    required input format. Returns True on success."""
    ff = ffmpeg_bin()
    if not ff:
        return False
    cmd = _lp([
        ff, "-y", "-loglevel", "error",
        "-i", str(src),
        "-map", "0:a:0", "-vn", "-sn",
        "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
        str(out_wav),
    ])
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=3600, **_LOWPRIO_KW)
    except (subprocess.TimeoutExpired, OSError):
        return False
    return proc.returncode == 0 and out_wav.exists() and out_wav.stat().st_size > 0


# whisper.cpp prints progress like "...progress = 42%" and the detected language
# like "auto-detected language: en (p = 0.97)".
_PROGRESS_RE = re.compile(r"progress\s*=\s*(\d+)%")
_DETECT_RE   = re.compile(r"auto-detected language:\s*([a-z]{2,3})")


def _run_whisper(
    wav: Path, out_base: Path, *,
    translate: bool,
    language: str = "auto",
    progress_cb: Optional[Callable[[float], None]] = None,
) -> tuple[bool, str]:
    """Run whisper-cli, writing `<out_base>.srt`. Returns (ok, detected_iso1).

    `language="auto"` lets whisper detect; `translate=True` emits English
    regardless of source language. Streams stderr to surface progress + the
    detected language without buffering the whole run.

    Timing precision: `-dtw` aligns token timestamps to the audio (accurate
    boundaries that respect pauses) when the model maps to a known preset, and
    `-ml`/`-sow` re-split into word-boundary cues so each carries its own timing.
    `--vad` is deliberately NOT used — it mistimed cues and hallucinated over
    music; see the `STT_MAX_LEN` / `_DTW_PRESETS` notes above and GOTCHAS.

    A CUDA/cuBLAS build offloads to the GPU automatically. If CUDA can't
    initialize at runtime (driver too old, no device), the first attempt fails;
    we retry once with `-ng` (force CPU), which is also a no-op for the CPU
    build. This lets a GPU build degrade gracefully to CPU instead of failing.
    """
    binp = whisper_bin()
    model = whisper_model()
    if not binp or not model:
        return False, ""

    preset = _dtw_preset()

    def _attempt(no_gpu: bool) -> tuple[bool, str]:
        cmd = [
            binp, "-m", model, "-f", str(wav),
            "-l", language or "auto", "-t", str(STT_THREADS),
            "-osrt", "-of", str(out_base), "-pp",
            "-ml", str(STT_MAX_LEN), "-sow",   # word-boundary cues, per-cue timing
        ]
        if preset:
            cmd += ["-dtw", preset]            # DTW token-level timestamp alignment
        if translate:
            cmd.append("-tr")
        if no_gpu:
            cmd.append("-ng")          # --no-gpu: force CPU (ignored by the CPU build)
        detected = ""
        try:
            proc = subprocess.Popen(
                _lp(cmd), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                text=True, **_LOWPRIO_KW,
            )
        except OSError:
            return False, ""
        assert proc.stderr is not None
        for line in proc.stderr:
            m = _DETECT_RE.search(line)
            if m:
                detected = m.group(1)
            if progress_cb:
                pm = _PROGRESS_RE.search(line)
                if pm:
                    try:
                        progress_cb(min(1.0, int(pm.group(1)) / 100.0))
                    except Exception:
                        pass
        proc.wait()
        srt = out_base.with_suffix(".srt")
        return (proc.returncode == 0 and srt.exists()), detected

    # GPU first; on failure force CPU. (A cuBLAS build on a too-old driver fails
    # CUDA init on the first attempt; `-ng` is a harmless no-op for the CPU build.)
    # No VAD attempt — it mistimed cues and hallucinated over music; see GOTCHAS.
    ok, detected = _attempt(no_gpu=False)
    if not ok:
        ok, detected = _attempt(no_gpu=True)
    return ok, detected


def generate(
    src: Path, *,
    want_translation: bool = True,
    progress_cb: Optional[Callable[[float], None]] = None,
) -> dict:
    """Transcribe `src` to sidecar `.srt`(s) next to it.

    Always produces a source-language track (`<stem>.<lang>.ai.<model>.srt`). If
    the detected language isn't English and `want_translation` is set, also
    produces an English-translated track. The `<model>` tag (the configured
    whisper model's short name) lets us detect a model change and offer regen.
    On success, any pre-existing generated sub made with a different model is
    removed so the new model's output replaces it cleanly.

    Returns {"tracks": [{"path","lang","translated","model"}], "error": str|None}.
    `tracks` is empty when nothing was produced.
    """
    if not is_available():
        return {"tracks": [], "error": "whisper.cpp or its model is not installed on the host."}
    if not src.exists():
        return {"tracks": [], "error": "Source file not on disk."}

    mname = model_name() or "model"
    gen = sub_gen_tag()
    tracks: list[dict] = []
    tmpdir = Path(tempfile.mkdtemp(prefix="stt_"))
    try:
        wav = tmpdir / "audio.wav"
        # Audio extraction is a small, bounded fraction of total time; reserve
        # the bulk of the progress bar for transcription.
        if progress_cb:
            progress_cb(0.02)
        if not _extract_wav(src, wav):
            return {"tracks": [], "error": "ffmpeg could not extract audio for transcription."}

        # Pass 1 — transcribe in the spoken language (auto-detect).
        base1 = tmpdir / "transcribe"
        ok1, detected = _run_whisper(
            wav, base1, translate=False, language="auto",
            progress_cb=(lambda p: progress_cb(0.05 + 0.65 * p)) if progress_cb else None,
        )
        if not ok1:
            return {"tracks": [], "error": "Transcription failed."}
        lang3 = _to_iso3(detected) if detected else "und"
        dest1 = src.with_name(f"{src.stem}.{lang3}.{AI_SUFFIX}.{mname}.{gen}.srt")
        try:
            shutil.move(str(base1.with_suffix(".srt")), str(dest1))
        except OSError as e:
            return {"tracks": [], "error": f"Could not save subtitle file: {e}"}
        tracks.append({"path": str(dest1), "lang": lang3, "translated": False, "model": mname})

        # Pass 2 — English translation, only when the audio isn't already English.
        is_english = detected in ("en", "eng") or lang3 == "eng"
        if want_translation and not is_english:
            base2 = tmpdir / "translate"
            ok2, _ = _run_whisper(
                wav, base2, translate=True, language="auto",
                progress_cb=(lambda p: progress_cb(0.70 + 0.28 * p)) if progress_cb else None,
            )
            if ok2:
                dest2 = src.with_name(f"{src.stem}.eng.{AI_SUFFIX}.{mname}.{gen}.srt")
                try:
                    shutil.move(str(base2.with_suffix(".srt")), str(dest2))
                    tracks.append({"path": str(dest2), "lang": "eng", "translated": True, "model": mname})
                except OSError:
                    pass  # the transcription track still succeeded

        # Remove any older generated subs (different model, or legacy untagged)
        # now that the new model's output is in place — keep only what we wrote.
        written = {Path(t["path"]) for t in tracks}
        for p, _m, _g in _list_ai_subs(src):
            if p not in written:
                try:
                    p.unlink()
                except OSError:
                    pass

        if progress_cb:
            progress_cb(1.0)
        return {"tracks": tracks, "error": None}
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
