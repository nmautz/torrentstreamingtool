"""Timed subtitle IMAGE packs — styled ASS and bitmap (PGS/VOBSUB) subtitles
pre-rendered to transparent PNGs plus a timing manifest.

WHY THIS EXISTS
Two subtitle kinds have no home in an HLS bundle:

  * **Styled ASS.** An HLS subtitle rendition may carry WebVTT or IMSC1 and
    nothing else, so every sign, colour, font and karaoke effect is flattened to
    plain text on its way into `sub_<i>.vtt`. The web player dodges this by
    running libass-wasm over the video, but that is a canvas the *app* draws —
    so it cannot exist on a locked phone, where the native AVPlayer owns the
    picture and only renders real media tracks.
  * **Bitmap subs (PGS / VOBSUB).** HLS has no bitmap subtitle track type at
    all. These are dropped at prep time (`skipped_image_subs`) and are currently
    unplayable on *every* on-device surface.

Both become tractable if the renderer stops being the player's problem: render
the subtitles ONCE, server-side, with real libass (fonts, transforms, karaoke
intact), and ship the result as a display list of transparent images with
timestamps. A client then only has to composite an image at a time — which a
`CALayer` can do from the render server while the app is backgrounded, and which
a `<canvas>` can do in any browser.

WHAT IT DOES NOT TOUCH
The video. This is purely additive: a pack lives beside `sub_<i>.vtt` in the
bundle's existing directory, no segment is rewritten, `OFFLINE_CACHE_VERSION`
does not move, and no already-prepped bundle is invalidated. That is the whole
point — burning subtitles into the picture would mean re-encoding every file.

THE OUTPUT
    <out_dir>/
      manifest.json      display list: [{t, file, x, y, w, h} | {t, clear}]
      c_<frame>.png      one image per VISUAL CHANGE (mpdecimate), PTS-named
      sub.ass            the extracted source subtitle (text kinds only)
      fonts/             attachments, so libass renders the intended typefaces

`manifest.cues` is time-ordered and gap-free: the image named at cue `t` stays on
screen until the next cue's `t`. A `clear` cue means "show nothing from here".

Leaf module: stdlib + the media binaries via `mediabin`. Imports nothing else in
this repo and is imported by `main.py`, never the reverse.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Callable, Optional

import mediabin

# Sampling rate for the render. Dialogue changes at cue boundaries, so 5 fps
# costs nothing on a normal track (mpdecimate collapses every identical frame);
# it only matters for ASS transforms (\move, \t, \fad), which are resampled to
# this rate. Raising it makes animated signs smoother and animated-sign-heavy
# tracks much larger — see MAX_FRAMES.
DEFAULT_FPS = 5

# A hard ceiling on images per pack. An anime "Signs & Songs" track can animate
# continuously, and at that point every sampled frame differs and mpdecimate
# saves nothing. Rather than emit a 2 GB pack we stop and report `truncated`, so
# the caller can fall back to the plain VTT rather than serve something partial
# and pretend it is complete.
MAX_FRAMES = 6000

MANIFEST_NAME = "manifest.json"
PACK_VERSION = 1

_META_RE = re.compile(
    r"pts_time:(?P<t>[0-9.]+).*?"
    r"lavfi\.cropdetect\.x1=(?P<x1>-?\d+).*?"
    r"lavfi\.cropdetect\.x2=(?P<x2>-?\d+).*?"
    r"lavfi\.cropdetect\.y1=(?P<y1>-?\d+).*?"
    r"lavfi\.cropdetect\.y2=(?P<y2>-?\d+)",
    re.S,
)
_FRAME_RE = re.compile(r"^c_(\d+)\.png$")

IMAGE_SUB_CODECS = {"hdmv_pgs_subtitle", "pgssub", "dvd_subtitle", "dvdsub", "xsub"}
TEXT_SUB_CODECS = {"ass", "ssa", "subrip", "srt", "webvtt", "mov_text", "text"}


class SubPackError(RuntimeError):
    """Rendering failed in a way the caller should surface, not retry blindly."""


# ── source inspection ────────────────────────────────────────────────────────

def probe_subtitles(src: str | os.PathLike) -> dict:
    """Video geometry, duration and the source's subtitle streams.

    `subtitles[i]` is the i-th SUBTITLE stream (what ffmpeg's `si=`/`0:s:i`
    addressing uses) — never the absolute stream index, which is the classic way
    to render the wrong language.
    """
    ffprobe = mediabin.ffprobe_bin()
    if not ffprobe:
        raise SubPackError("ffprobe not found")
    cp = mediabin.run_capture([
        ffprobe, "-v", "error", "-show_entries",
        "stream=index,codec_type,codec_name,width,height:stream_tags=language,title:format=duration",
        "-of", "json", str(src),
    ], timeout=120)
    if cp.returncode != 0:
        raise SubPackError(f"ffprobe failed: {(cp.stderr or '')[-400:]}")
    data = json.loads(cp.stdout or "{}")

    width = height = 0
    subs: list[dict] = []
    for st in data.get("streams") or []:
        kind = st.get("codec_type")
        if kind == "video" and not width:
            width, height = int(st.get("width") or 0), int(st.get("height") or 0)
        elif kind == "subtitle":
            codec = (st.get("codec_name") or "").lower()
            tags = st.get("tags") or {}
            subs.append({
                "idx": len(subs),
                "codec": codec,
                "image_based": codec in IMAGE_SUB_CODECS,
                "language": tags.get("language") or "und",
                "title": tags.get("title") or "",
            })
    try:
        duration = float((data.get("format") or {}).get("duration") or 0.0)
    except (TypeError, ValueError):
        duration = 0.0
    return {"width": width, "height": height, "duration": duration, "subtitles": subs}


# ── extraction (text kinds) ──────────────────────────────────────────────────

def _extract_ass(ffmpeg: str, src: str, sub_idx: int, out_dir: Path) -> str:
    """Stream-copy one text subtitle to `sub.ass` in `out_dir`.

    Copied, not converted, so ASS override tags survive. SRT sources are
    transcoded (ffmpeg picks the ass muxer from the extension), which gives them
    a default style rather than failing.
    """
    name = "sub.ass"
    codec = ["-c:s", "copy"]
    cp = _run([ffmpeg, "-y", "-hide_banner", "-v", "error", "-i", src,
               "-map", f"0:s:{sub_idx}", *codec, "-f", "ass", name], out_dir)
    if cp.returncode != 0 or not (out_dir / name).exists():
        # Not an ASS source (SRT/mov_text): let ffmpeg convert instead of copy.
        cp = _run([ffmpeg, "-y", "-hide_banner", "-v", "error", "-i", src,
                   "-map", f"0:s:{sub_idx}", "-f", "ass", name], out_dir)
    if cp.returncode != 0 or not (out_dir / name).exists():
        raise SubPackError(f"subtitle extract failed: {(cp.stderr or '')[-400:]}")
    return name


def _extract_fonts(ffmpeg: str, ffprobe: str, src: str, out_dir: Path) -> int:
    """Dump the container's font attachments into `fonts/`, one at a time.

    Without these libass silently substitutes, and a typeface-heavy sign track
    renders in the wrong font — a failure that looks like "the pack is broken"
    rather than "a font is missing".

    Dumped PER INDEX rather than with the obvious `-dump_attachment:t ""`, which
    names each file from its `filename` tag and **aborts the whole run** on the
    first name it can't write — a space in `ObeliskMdITC TT.ttf` was enough to
    stop a 9-font release after 3, silently, because the exit status of a bulk
    dump tells you nothing about how far it got. Per-index also lets us pick the
    output name, so a tag carrying a space, `!`, or a non-ASCII character can't
    decide whether the typeface survives. libass matches on the font's INTERNAL
    family name, so renaming the file is free.
    """
    fonts = out_dir / "fonts"
    fonts.mkdir(exist_ok=True)
    cp = mediabin.run_capture([
        ffprobe, "-v", "error", "-select_streams", "t",
        "-show_entries", "stream_tags=filename", "-of", "json", str(src),
    ], timeout=120)
    try:
        streams = (json.loads(cp.stdout or "{}").get("streams") or [])
    except ValueError:
        streams = []

    n = 0
    for i, st in enumerate(streams):
        raw = ((st.get("tags") or {}).get("filename") or f"font_{i}")
        ext = Path(raw).suffix.lower()
        if ext not in (".ttf", ".otf", ".ttc", ".woff", ".woff2"):
            ext = ".ttf"
        safe = f"font_{i}{ext}"
        # `-vn -an -sn` is a PERFORMANCE requirement, not tidiness. Attachments
        # are written when the input is opened, but a bare `-f null -` then goes
        # on to decode the whole episode — once per attachment. On a 1.4 GB HEVC
        # 10-bit source that turned a 1-second job into a multi-minute one.
        # Disabling every output stream drops it to ~0.1 s per font.
        cp2 = _run([ffmpeg, "-y", "-hide_banner", "-v", "error",
                    f"-dump_attachment:t:{i}", safe,
                    "-i", src, "-vn", "-an", "-sn", "-f", "null", "-"], fonts)
        if (fonts / safe).exists() and (fonts / safe).stat().st_size > 0:
            n += 1
    return n


# ── render ───────────────────────────────────────────────────────────────────

def _run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess:
    """Spawn a media binary at lowered priority with `cwd` set.

    Every filename handed to ffmpeg is BARE and resolved against `cwd`. That is
    deliberate: Windows paths carry `:` and `\\`, both of which are separators
    inside an ffmpeg filter string, and escaping them correctly is the trap this
    repo has already been bitten by elsewhere. Never interpolate an absolute
    path into a filtergraph.
    """
    return subprocess.run(
        mediabin._lp(cmd), cwd=str(cwd), capture_output=True, text=True,
        encoding="utf-8", errors="replace", **mediabin._LOWPRIO_KW,
    )


def _filter_graph(kind: str, sub_name: str, has_fonts: bool) -> str:
    """The render graph: draw subtitles over transparency, drop unchanged
    frames, then TEE the survivors to both the PNG writer and a bounding-box
    detector.

    The split is what makes per-cue cropping free. `cropdetect` reports the ink
    bounds of each kept frame as metadata, `metadata=print` writes them out with
    the frame's pts_time, and the two streams stay in lockstep because the split
    happens AFTER mpdecimate. No second pass, no image library, no pixel work in
    Python.
    """
    if kind == "image":
        draw = "[0:v][1:s]overlay=format=auto"
    else:
        fonts = ":fontsdir=fonts" if has_fonts else ""
        # `alpha=1` is LOAD-BEARING. The filter's alpha handling defaults to OFF,
        # meaning it blends text into RGB and leaves the alpha channel exactly as
        # it found it — which, over a transparent base, is zero everywhere. The
        # result is a pack of PNGs that carry the glyphs in RGB and are 100%
        # transparent, so they look perfect to any luma-based tool (cropdetect
        # happily reports bounding boxes) and composite to nothing on a client.
        draw = f"[0:v]ass={sub_name}{fonts}:alpha=1"
    return (
        f"{draw},mpdecimate=hi=1:lo=1:frac=0,split=2[img][det];"
        f"[det]cropdetect=limit=0:round=2:reset=1,"
        f"metadata=print:file=bbox.txt,nullsink"
    )


def render_pack(
    src: str | os.PathLike,
    sub_idx: int,
    out_dir: str | os.PathLike,
    *,
    fps: int = DEFAULT_FPS,
    info: Optional[dict] = None,
    timeout: int = 3600,
    on_progress: Optional[Callable[[str], None]] = None,
) -> dict:
    """Render subtitle stream `sub_idx` of `src` into a pack at `out_dir`.

    Returns the manifest dict (also written to `manifest.json`). Idempotent in
    the sense that it clears and rebuilds `out_dir`; callers gate on the
    manifest's existence rather than calling this twice.
    """
    src = str(src)
    out_dir = Path(out_dir)
    ffmpeg = mediabin.ffmpeg_bin()
    if not ffmpeg:
        raise SubPackError("ffmpeg not found")

    info = info or probe_subtitles(src)
    subs = info.get("subtitles") or []
    if not (0 <= sub_idx < len(subs)):
        raise SubPackError(f"no subtitle stream {sub_idx} (source has {len(subs)})")
    sub = subs[sub_idx]
    width, height = int(info.get("width") or 0), int(info.get("height") or 0)
    duration = float(info.get("duration") or 0.0)
    if not (width and height and duration > 0):
        raise SubPackError("source geometry/duration unavailable")

    if out_dir.exists():
        shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    kind = "image" if sub["image_based"] else "text"
    sub_name, n_fonts = "", 0
    if kind == "text":
        if on_progress:
            on_progress("extracting subtitle")
        sub_name = _extract_ass(ffmpeg, src, sub_idx, out_dir)
        if on_progress:
            on_progress("extracting fonts")
        n_fonts = _extract_fonts(ffmpeg, mediabin.ffprobe_bin() or "", src, out_dir)

    if on_progress:
        on_progress("rendering")
    # The base is a fully transparent canvas at the video's own geometry, so the
    # pack overlays 1:1 at any display size and the client only has to scale.
    base = f"color=c=black@0.0:s={width}x{height}:rate={fps}:d={duration:.3f},format=rgba"
    cmd = [ffmpeg, "-y", "-hide_banner", "-v", "error", "-f", "lavfi", "-i", base]
    if kind == "image":
        cmd += ["-i", src]          # bitmap subs come straight off the source
    cmd += [
        "-filter_complex", _filter_graph(kind, sub_name, n_fonts > 0),
        "-map", "[img]", "-fps_mode", "vfr", "-frame_pts", "1",
        "-frames:v", str(MAX_FRAMES), "-f", "image2", "c_%d.png",
    ]
    cp = _run(cmd, out_dir)
    if cp.returncode != 0:
        raise SubPackError(f"render failed: {(cp.stderr or '')[-600:]}")

    manifest = _build_manifest(out_dir, sub, kind, width, height, fps, duration)
    manifest["fonts"] = n_fonts
    (out_dir / MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=1), encoding="utf-8")
    if on_progress:
        on_progress("done")
    return manifest


def _build_manifest(out_dir: Path, sub: dict, kind: str, width: int, height: int,
                    fps: int, duration: float) -> dict:
    """Turn the rendered frames + bbox log into a time-ordered display list."""
    boxes = _parse_boxes(out_dir / "bbox.txt")

    frames: list[tuple[float, Path]] = []
    for p in out_dir.iterdir():
        m = _FRAME_RE.match(p.name)
        if m:
            frames.append((int(m.group(1)) / float(fps), p))
    frames.sort(key=lambda t: t[0])

    cues: list[dict] = []
    for t, path in frames:
        box = _nearest_box(boxes, t)
        if box is None:
            # cropdetect found no ink: this frame is the "cue ends" blank that
            # mpdecimate kept. Record it as a clear and drop the image — a
            # transparent PNG on disk is pure waste.
            cues.append({"t": round(t, 3), "clear": True})
            try:
                path.unlink()
            except OSError:
                pass
            continue
        x, y, w, h = box
        cues.append({"t": round(t, 3), "file": path.name,
                     "x": x, "y": y, "w": w, "h": h,
                     "bytes": path.stat().st_size})

    # Collapse repeated clears — two blanks in a row carry no information and
    # just make the client re-composite nothing.
    tidy: list[dict] = []
    for c in cues:
        if c.get("clear") and tidy and tidy[-1].get("clear"):
            continue
        tidy.append(c)

    n_img = sum(1 for c in tidy if not c.get("clear"))
    return {
        "version": PACK_VERSION,
        "kind": kind,
        "sub_index": sub["idx"],
        "codec": sub["codec"],
        "language": sub.get("language") or "und",
        "title": sub.get("title") or "",
        "width": width,
        "height": height,
        "fps": fps,
        "duration": round(duration, 3),
        "images": n_img,
        # Hit the ceiling => the tail of the episode has no subtitles at all, so
        # the caller must prefer the plain VTT over a pack that silently stops.
        "truncated": len(frames) >= MAX_FRAMES,
        "bytes": sum(c.get("bytes", 0) for c in tidy),
        "cues": tidy,
    }


def _parse_boxes(path: Path) -> list[tuple[float, Optional[tuple[int, int, int, int]]]]:
    """Read `metadata=print` output into (pts_time, bbox|None), time-ordered."""
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8", errors="replace")
    out: list[tuple[float, Optional[tuple[int, int, int, int]]]] = []
    for m in _META_RE.finditer(text):
        t = float(m.group("t"))
        x1, x2 = int(m.group("x1")), int(m.group("x2"))
        y1, y2 = int(m.group("y1")), int(m.group("y2"))
        if x2 < x1 or y2 < y1 or x1 < 0 or y1 < 0:
            out.append((t, None))           # no ink in this frame
        else:
            out.append((t, (x1, y1, x2 - x1 + 1, y2 - y1 + 1)))
    out.sort(key=lambda r: r[0])
    return out


def _nearest_box(boxes, t: float, tol: float = 0.05):
    """Match a frame to its bbox by timestamp.

    Matched on time rather than by position because `metadata=print` and the
    image writer are separate sinks: if either ever drops a frame, index
    alignment would pair a cue with someone else's bounding box and the client
    would crop the wrong region.
    """
    best, best_d = None, tol
    for bt, box in boxes:
        d = abs(bt - t)
        if d <= best_d:
            best, best_d = box, d
        elif bt > t + tol:
            break
    return best


# ── CLI (verification harness) ───────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    import time

    ap = argparse.ArgumentParser(description="Render a subtitle image pack.")
    ap.add_argument("src")
    ap.add_argument("sub_idx", type=int, nargs="?", default=None,
                    help="subtitle stream index; omitted = list them and exit")
    ap.add_argument("out", nargs="?", default=None)
    ap.add_argument("--fps", type=int, default=DEFAULT_FPS)
    a = ap.parse_args()

    nfo = probe_subtitles(a.src)
    if a.sub_idx is None:
        print(f"{nfo['width']}x{nfo['height']}  {nfo['duration']:.1f}s")
        for s in nfo["subtitles"]:
            print(f"  [{s['idx']}] {s['codec']:20s} {s['language']:4s} "
                  f"{'IMAGE' if s['image_based'] else 'text ':6s} {s['title']}")
        raise SystemExit(0)

    t0 = time.time()
    man = render_pack(a.src, a.sub_idx, a.out or "subpack_out", fps=a.fps,
                      info=nfo, on_progress=lambda s: print(f"  … {s}", flush=True))
    el = time.time() - t0
    print(f"\n{man['images']} images / {len(man['cues'])} cues, "
          f"{man['bytes']/1048576:.1f} MB, truncated={man['truncated']}")
    print(f"rendered in {el:.1f}s  ({man['duration']/max(el,0.001):.1f}x realtime)")
