"""Evaluate the aligner + verifier on every graded candidate that has audio.

For each candidate: align it to the target's speech energy, re-grade the
aligned cues against the embedded reference, and record the verifier's
confidence. Offline (cache only). Energy per target is cached in .cache/energy/.

    FFMPEG=... SUBS_EVAL_OFFLINE=1 python tests/subs_eval/align_eval.py [title-filter]
"""
import collections, json, os, sys, time
import common, grade, features, proto_sync

FFMPEG = os.environ.get("FFMPEG", "ffmpeg")
E = common.CACHE / "energy"


def energy(t):
    E.mkdir(exist_ok=True)
    f = E / f"{t['cache_key']}.json"
    if f.exists():
        return json.loads(f.read_text())
    a = common.CACHE / "audio" / f"{t['cache_key']}.flac"
    if not a.exists():
        return None
    e = proto_sync.speech_energy(a, FFMPEG)
    f.write_text(json.dumps(e))
    return e


def cand_cues(r):
    b = common.download(r["SubDownloadLink"])
    return common.parse_subs(b, fps=float(r.get("MovieFPS") or 0) or 23.976) if b else []


def regrade(t, ref, cues):
    x = common.timing(ref, cues, t["duration"])
    return features.label(t, x), x


def main():
    filt = sys.argv[1] if len(sys.argv) > 1 else ""
    corpus = json.load(open(common.HERE / "corpus.json", encoding="utf-8"))
    g = json.load(open(common.CACHE / "graded.json"))
    out_f = common.CACHE / "aligned.json"
    done = json.loads(out_f.read_text()) if out_f.exists() else {}
    trans = collections.Counter()
    for t in corpus:
        if filt and filt.lower() not in t["title"].lower():
            continue
        e = energy(t)
        if not e:
            continue
        ref = common.load_ref(t["cache_key"])
        if len(ref) < 100:
            continue
        for r in grade.pool(t):
            k = f"{t['cache_key']}:{r['IDSubtitleFile']}"
            if k not in g:
                continue
            before = features.label(t, g[k])
            if k not in done:
                cues = cand_cues(r)
                if not cues:
                    continue
                t0 = time.time()
                a = proto_sync.align(cues, e, detrend=20, prior=1.25)
                if not a:
                    continue
                ch = proto_sync.refine_chunks(cues, e, a["ratio"], a["offset"], detrend=20)
                glob = proto_sync.apply(cues, a["ratio"], a["offset"])
                chunked = proto_sync.apply(cues, a["ratio"], a["offset"], ch)
                lg, xg = regrade(t, ref, glob)
                lc, xc = regrade(t, ref, chunked)
                done[k] = {"before": before, "global": lg, "chunked": lc, "conf": a["conf"], "fit": a["fit"],
                           "ratio": a["ratio"], "offset": a["offset"], "secs": round(time.time() - t0, 2),
                           "g_off": [xg["off_a"], xg["off_b"]], "c_off": [xc["off_a"], xc["off_b"]]}
                out_f.write_text(json.dumps(done))
            d = done[k]
            trans[(d["before"], d["global"], d["chunked"])] += 1
            print(f"{t['title'][:14]:14} E{t['episode']:<3} {d['before']:5} -> glob {d['global']:5} chunk {d['chunked']:5}  "
                  f"conf {d['conf']:6.2f} fit {d['fit']:6.2f} r {d['ratio']:.4f} off {d['offset']:7.2f} ({d['secs']}s)  {r['SubFileName'][:40]}")
    print()
    for k, v in sorted(trans.items()):
        print(f"  {k[0]:5} -> global {k[1]:5} / chunked {k[2]:5}: {v}")


if __name__ == "__main__":
    main()


# ── alass comparison ──────────────────────────────────────────────────────────
ALASS = os.environ.get("ALASS", "alass-cli")


def to_srt(cues):
    def ts(x):
        x = max(0.0, x); h = int(x // 3600); m = int(x % 3600 // 60); s = x % 60
        return f"{h:02}:{m:02}:{int(s):02},{int(round((s % 1) * 1000)) % 1000:03}"
    return "\n".join(f"{i}\n{ts(a)} --> {ts(b)}\n{(txt if len(c) > 2 else '') or '.'}\n"
                     for i, c in enumerate(cues, 1) for a, b, txt in [(c[0], c[1], c[2] if len(c) > 2 else '.')])


def alass(t, cues, split_penalty=None):
    import subprocess, tempfile
    ffdir = os.path.dirname(FFMPEG)
    env = dict(os.environ, ALASS_FFMPEG_PATH=FFMPEG, ALASS_FFPROBE_PATH=os.path.join(ffdir, "ffprobe.exe"))
    with tempfile.TemporaryDirectory() as d:
        i, o = os.path.join(d, "in.srt"), os.path.join(d, "out.srt")
        open(i, "w", encoding="utf-8").write(to_srt(cues))
        cmd = [ALASS, str(common.CACHE / "audio" / f"{t['cache_key']}.flac"), i, o]
        if split_penalty is not None:
            cmd[1:1] = ["--split-penalty", str(split_penalty)]
        t0 = time.time()
        r = subprocess.run(cmd, capture_output=True, text=True, env=env)
        if r.returncode or not os.path.exists(o):
            return None, r.stderr[-300:], time.time() - t0
        return common.parse_subs(open(o, "rb").read()), "", time.time() - t0


def main_alass(filt="", split_penalty=None, tag="alass"):
    corpus = json.load(open(common.HERE / "corpus.json", encoding="utf-8"))
    g = json.load(open(common.CACHE / "graded.json"))
    out_f = common.CACHE / f"{tag}.json"
    done = json.loads(out_f.read_text()) if out_f.exists() else {}
    trans = collections.Counter()
    for t in corpus:
        if filt and filt.lower() not in t["title"].lower():
            continue
        if not (common.CACHE / "audio" / f"{t['cache_key']}.flac").exists():
            continue
        ref = common.load_ref(t["cache_key"])
        if len(ref) < 100:
            continue
        for r in grade.pool(t):
            k = f"{t['cache_key']}:{r['IDSubtitleFile']}"
            if k not in g:
                continue
            if k not in done:
                cues = cand_cues(r)
                if not cues:
                    continue
                al, err, secs = alass(t, cues, split_penalty)
                if al is None:
                    print("  alass failed", err); continue
                lab, x = regrade(t, ref, al)
                done[k] = {"before": features.label(t, g[k]), "after": lab, "secs": round(secs, 1),
                           "off": [x["off_a"], x["off_b"]]}
                out_f.write_text(json.dumps(done))
            d = done[k]
            trans[(d["before"], d["after"])] += 1
            print(f"{t['title'][:14]:14} E{t['episode']:<3} {d['before']:5} -> {d['after']:5} ({d['secs']}s) {r['SubFileName'][:45]}")
    print()
    for k, v in sorted(trans.items()):
        print(f"  {k[0]:5} -> {k[1]:5}: {v}")
