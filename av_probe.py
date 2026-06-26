#!/usr/bin/env python3
"""Throwaway A/V-offset probe for the HLS-prep desync investigation.

Measures whether the offline HLS bundle dropped the source's intrinsic
audio-vs-video start offset (the suspected cause of "prepped audio plays ~0.5s
early", while on-demand + VLC stay in sync).

Usage (run with the SYSTEM python on the box that holds the files + bundles):

    python av_probe.py "F:\\StreamLink\\...\\S01E01-Maomao [6846AA97].mkv"

  - With only the source path, it auto-finds the matching bundle under
    <repo>\\.offline_cache by reading each meta.json's "src".
  - Or pass the bundle dir explicitly as a 2nd arg:

    python av_probe.py "F:\\...\\S01E01-...mkv" "F:\\torrentstreamingtool\\.offline_cache\\12529da10fb6563f237fef39"

It only READS (ffprobe). It changes nothing.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent


def find_ffprobe() -> str:
    # Prefer the bundled ffprobe next to the bundled ffmpeg, else PATH.
    for p in REPO.glob("tools/ffmpeg/**/bin/ffprobe.exe"):
        return str(p)
    for p in REPO.glob("tools/ffmpeg/**/bin/ffprobe"):
        return str(p)
    return "ffprobe"


FFPROBE = find_ffprobe()


def first_pts(target: str, stream: str, ignore_editlist: bool = False):
    """First packet pts_time (sec) of a stream, or None. `target` may be a media
    file or an HLS .m3u8 rendition playlist."""
    cmd = [FFPROBE, "-v", "error", "-of", "json"]
    if ignore_editlist:
        cmd += ["-ignore_editlist", "1"]
    cmd += ["-select_streams", stream, "-read_intervals", "%+#1",
            "-show_entries", "packet=pts_time", target]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        pkts = (json.loads(r.stdout or "{}").get("packets") or [])
        return float(pkts[0]["pts_time"]) if pkts else None
    except Exception as e:
        print(f"  ! ffprobe failed for {stream} on {target}: {e}")
        return None


def rendition_first_pts(playlist: Path):
    """First pts_time of an HLS rendition playlist (ffprobe reads its EXT-X-MAP
    init + first segment automatically). Stream auto-detected (playlists are
    single-type)."""
    if not playlist.exists():
        print(f"  ! missing rendition playlist: {playlist}")
        return None
    cmd = [FFPROBE, "-v", "error", "-of", "json",
           "-read_intervals", "%+#1",
           "-show_entries", "packet=pts_time", str(playlist)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        pkts = (json.loads(r.stdout or "{}").get("packets") or [])
        return float(pkts[0]["pts_time"]) if pkts else None
    except Exception as e:
        print(f"  ! ffprobe failed on {playlist}: {e}")
        return None


def autofind_bundle(src: str):
    cache = REPO / ".offline_cache"
    if not cache.is_dir():
        return None
    want = Path(src).name.lower()
    for d in sorted(cache.iterdir()):
        mj = d / "meta.json"
        if not mj.is_file():
            continue
        try:
            meta = json.loads(mj.read_text("utf-8"))
        except Exception:
            continue
        msrc = str(meta.get("src", ""))
        if msrc and (msrc == src or Path(msrc).name.lower() == want):
            return d
    return None


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    src = sys.argv[1]
    print(f"ffprobe: {FFPROBE}")
    print(f"source : {src}\n")

    sv = first_pts(src, "v:0")
    sa = first_pts(src, "a:0")
    sv_raw = first_pts(src, "v:0", ignore_editlist=True)
    print("SOURCE first-PTS (what VLC / on-demand honor):")
    print(f"  video v:0            = {sv}")
    print(f"  audio a:0            = {sa}")
    print(f"  video v:0 (no elist) = {sv_raw}")
    if sv is not None and sv_raw is not None:
        print(f"  edit-list delta      = {sv - sv_raw:+.4f} s  (≈0 expected for MKV)")
    src_off = (sa - sv) if (sv is not None and sa is not None) else None
    if src_off is not None:
        print(f"  >> source A/V offset = {src_off:+.4f} s  (audio minus video; "
              f"this is the offset a faithful bundle must reproduce)")
    print()

    bundle = Path(sys.argv[2]) if len(sys.argv) >= 3 else autofind_bundle(src)
    if not bundle:
        print("No bundle dir given and none auto-found under .offline_cache.")
        print("Pass the bundle dir as a 2nd arg to compare.")
        return 0
    print(f"bundle : {bundle}")

    vname, a_playlist = "video", "audio_0.m3u8"
    meta: dict = {}
    mj = bundle / "meta.json"
    if mj.is_file():
        try:
            meta = json.loads(mj.read_text("utf-8"))
            vids = meta.get("videos") or []
            auds = meta.get("audios") or []
            if vids:
                vname = vids[0].get("name") or vname
            if auds:
                da = next((x for x in auds if x.get("default")), auds[0])
                a_playlist = da.get("playlist") or f"audio_{da.get('idx', 0)}.m3u8"
        except Exception:
            pass

    padded = bool(meta.get("audio_padded_to_zero"))
    bv = rendition_first_pts(bundle / f"{vname}.m3u8")
    ba = rendition_first_pts(bundle / a_playlist)
    print("BUNDLE first-PTS (separate fmp4 renditions):")
    print(f"  video rendition {vname}.m3u8 = {bv}")
    print(f"  audio rendition {a_playlist} = {ba}")
    print(f"  meta audio_padded_to_zero    = {padded}")
    bundle_gap = (ba - bv) if (bv is not None and ba is not None) else None
    if bundle_gap is not None:
        print(f"  >> bundle A/V gap = {bundle_gap:+.4f} s  (audio first-PTS minus video)")
    print()

    # The naive bundle player (Safari / iOS AVPlayer / hls.js on SEPARATE fmp4
    # renditions) ignores the cross-rendition baseMediaDecodeTime gap and anchors
    # each rendition to playback start — so the RAW bundle gap (not its diff vs the
    # source) is what the user hears as desync. The fix silence-pads the audio to
    # the video's zero, collapsing the gap to ≈0 while keeping the source's delay as
    # real leading silence. So: a CORRECT (fixed/padded) bundle has gap ≈ 0; an
    # unfixed one reproduces ~the source's audio-start delay and plays early.
    if bundle_gap is not None:
        thresh = 0.35 if padded else 0.12   # AV_OFFSET_PAD_FLAG_SECS / AV_OFFSET_FLAG_SECS
        print("=" * 60)
        print(f"BUNDLE A/V GAP (what a naive HLS player desyncs by) = {bundle_gap:+.4f} s")
        if src_off is not None:
            print(f"  (source's intrinsic A/V offset was {src_off:+.4f} s; the fix bakes "
                  f"that as silence so the gap should collapse to ≈0)")
        print(f"  flag threshold ({'padded' if padded else 'unpadded'}) = {thresh} s")
        if abs(bundle_gap) > thresh:
            sign = "EARLY" if bundle_gap > 0 else "LATE"
            print(f"  >> DESYNC: audio plays ~{abs(bundle_gap):.2f}s {sign} on a naive "
                  f"player. {'Padding did NOT take — re-prep.' if padded else 'Re-prep to silence-pad (first_pts=0).'}")
        else:
            print("  >> OK — bundle audio is aligned with video at playback start.")
        print("=" * 60)
    else:
        print("Could not compute bundle gap (a probe returned None above).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
