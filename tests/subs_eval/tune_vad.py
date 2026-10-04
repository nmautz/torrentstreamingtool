"""Tune the aligner on the REFERENCE tracks: a good aligner leaves them at
ratio 1, offset ~0, with high confidence."""
import json, time, common, proto_sync

def run(**kw):
    c = json.load(open('corpus.json', encoding='utf-8'))
    ok = 0; cok = []; cbad = []
    for t in c:
        f = common.CACHE / 'energy' / f"{t['cache_key']}.json"
        if not f.exists(): continue
        ref = common.load_ref(t['cache_key'])
        if len(ref) < 100: continue
        p = proto_sync.align(ref, json.loads(f.read_text()), **kw)
        good = p and p['ratio'] == 1.0 and abs(p['offset']) <= 0.3
        ok += bool(good); (cok if good else cbad).append(p['conf'] if p else 0)
    cok.sort(); cbad.sort()
    return ok, len(cok) + len(cbad), cok[:4], cbad[-4:]

t0 = time.time()
for det in (0, 20, 50, 100):
    for prior in (None, 1.1, 1.25):
        ok, n, lo, hi = run(detrend=det, prior=prior)
        print(f"detrend={det:3} prior={prior}: {ok}/{n} in place | lowest conf right {lo} | highest conf wrong {hi}")
print(round(time.time() - t0, 1), "s")
