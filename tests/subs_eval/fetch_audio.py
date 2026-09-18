"""Pull each target's audio (the ORIGINAL-language rendition — subs are timed to
it, a dub speaks at different moments) out of its HLS bundle on the box, as
16 kHz mono FLAC in .cache/audio/. Feeds align.py.

    FFMPEG=path/to/ffmpeg.exe python tests/subs_eval/fetch_audio.py
"""
import json, os, subprocess, sys
import collect, common

FFMPEG = os.environ.get("FFMPEG", "ffmpeg")
OUT = common.CACHE / "audio"


def pick_audio(meta, anime):
    auds = meta.get("audios") or []
    if anime:
        for a in auds:
            if (a.get("language") or "").lower() in ("jpn", "ja"):
                return a
    return next((a for a in auds if a.get("default")), auds[0] if auds else None)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    corpus = json.load(open(common.HERE / "corpus.json", encoding="utf-8"))
    for t in corpus:
        dest = OUT / f"{t['cache_key']}.flac"
        if dest.exists():
            continue
        meta = collect.req(f"/api/library/offline-cache/{t['cache_key']}/meta.json")
        a = pick_audio(meta, t.get("anime"))
        if not a:
            print("  no audio", t["file"][:60]); continue
        # plain HTTP: ffmpeg's TLS rejects the box's self-signed cert
        url = f"{collect.B.replace('https://', 'http://')}/api/library/offline-cache/{t['cache_key']}/{a['playlist']}"
        tmp = dest.with_suffix(".part.flac")
        r = subprocess.run([FFMPEG, "-nostdin", "-loglevel", "error", "-y", "-i", url,
                            "-vn", "-ac", "1", "-ar", "16000", "-sample_fmt", "s16", "-c:a", "flac", str(tmp)],
                           capture_output=True, text=True)
        if r.returncode:
            print("  ffmpeg failed", t["file"][:50], r.stderr[-300:]); tmp.unlink(missing_ok=True); continue
        tmp.rename(dest)
        print(f"  {t['title'][:20]:20} S{t['season']}E{t['episode']} [{a.get('language')}] {dest.stat().st_size/1e6:.1f} MB")


if __name__ == "__main__":
    main()
