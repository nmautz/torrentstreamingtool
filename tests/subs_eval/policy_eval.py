"""Re-apply alignment under different policies and re-grade against the refs.
Uses aligned.json's global fit; recomputes chunks (cheap) per policy."""
import collections, json, sys
import common, grade, features, proto_sync, align_eval

def main():
    corpus = json.load(open(common.HERE / "corpus.json", encoding="utf-8"))
    d = json.load(open(common.CACHE / "aligned.json"))
    pols = [("global only", None)] + [(f"chunks z>={z} d>={dm}", (z, dm)) for z in (3, 4, 5, 6) for dm in (0.5, 1.0)]
    C, MV = 5.0, 1.0
    res = {p: collections.Counter() for p, _ in pols}
    for t in corpus:
        ef = common.CACHE / "energy" / f"{t['cache_key']}.json"
        if not ef.exists():
            continue
        e = json.loads(ef.read_text()); ref = common.load_ref(t["cache_key"])
        if len(ref) < 100:
            continue
        for r in grade.pool(t):
            k = f"{t['cache_key']}:{r['IDSubtitleFile']}"
            if k not in d:
                continue
            v = d[k]; cues = align_eval.cand_cues(r)
            app = v["conf"] >= C and (v["ratio"] != 1.0 or abs(v["offset"]) >= MV)
            base_r, base_o = (v["ratio"], v["offset"]) if app else (1.0, 0.0)
            chunks = proto_sync.refine_chunks(cues, e, base_r, base_o, detrend=20)
            for name, zd in pols:
                if zd is None:
                    out = proto_sync.apply(cues, base_r, base_o)
                else:
                    out = proto_sync.apply(cues, base_r, base_o, proto_sync.gate_chunks(chunks, base_o, *zd))
                lab, _ = align_eval.regrade(t, ref, out)
                res[name][(v["before"], lab)] += 1
    for name, _ in pols:
        c = res[name]
        good = sum(n for (b, a), n in c.items() if a == "sync")
        broke = sum(n for (b, a), n in c.items() if b == "sync" and a != "sync")
        print(f"{name:22} sync after {good}/{sum(c.values())}  broke {broke}")

main()
