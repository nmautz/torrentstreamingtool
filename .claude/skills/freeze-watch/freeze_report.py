#!/usr/bin/env python3
"""Apply the freeze standard (SKILL.md) to a StreamLink box's logs.

    STREAMLINK_ADMIN_PASSWORD=... python3 .claude/skills/freeze-watch/freeze_report.py \
        --box https://192.168.0.106 [--since "2026-10-20 19:04:00"] [--save DIR]

Reads vitals.log and streamlink_app.log over the admin API and prints one report:
the window, a verdict (PASS / ACT), and every breach grouped by cause. Exit code
0 = PASS, 2 = ACT, 1 = could not read the box. Stdlib only.

`--since` defaults to the box's last restart (the newest point where `up=` went
down in vitals.log). The thresholds are the constants below and nowhere else:
change them here and in SKILL.md together.
"""
import argparse, collections, json, os, re, ssl, sys, urllib.request
from datetime import datetime, timedelta

HARD_MS = 250          # one freeze this long: find the cause and fix it (1000 until 2026-10-20)
SOFT_MS = 150          # a freeze this long counts toward a repeat (the sampler names >= 150 ms)
SOFT_REPEAT = 3        # the same cause this many times in one window: fix it
PROBE_HARD_MS = 500    # /healthz self-probe this slow with the loop free: lock / pool starvation.
                       # The box's loopback probe itself costs 140-340 ms (the same request
                       # answers in 10-15 ms from the LAN), so it can't share HARD_MS
GAP_S = 45             # vitals lines further apart than this: the process (or machine) stalled
SLOW_REQ_S = 5         # a request this slow with the loop free: a slow endpoint, listed, not a freeze

TS = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)")
FRAME = re.compile(r'^\s*File "([^"]+)", line (\d+), in (\S+)')
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE


def _req(url, data=None, tok=None, timeout=120):
    h = {"Content-Type": "application/json"}
    if tok:
        h["Authorization"] = "Bearer " + tok
    r = urllib.request.Request(url, data=json.dumps(data).encode() if data else None, headers=h)
    with urllib.request.urlopen(r, timeout=timeout, context=CTX) as resp:
        return resp.read()


def fetch(box, pw):
    tok = json.loads(_req(box + "/api/admin/login", {"password": pw}))["token"]
    ver = json.loads(_req(box + "/api/version")).get("version", "?")
    logs = {n: _req(f"{box}/api/admin/logs/{n}", tok=tok).decode("utf-8", "replace")
            for n in ("vitals.log", "streamlink_app.log")}
    names = [f["name"] for f in json.loads(_req(box + "/api/admin/logs", tok=tok))["files"]]
    return ver, logs, [n for n in names if n.startswith("stall_")]


def when(line):
    m = TS.match(line)
    return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S") if m else None


def parse_vitals(text):
    rows = []
    for ln in text.splitlines():
        t = when(ln)
        if not t or " up=" not in ln:
            continue
        g = lambda k, d=0.0: float(re.search(k + r"=([\d.]+)", ln).group(1)) \
            if re.search(k + r"=([\d.]+)", ln) else d
        lag = g("lag")
        rows.append({"t": t, "up": g("up"), "lag": lag, "lagmax": g("lagmax", lag),
                     "has_lagmax": "lagmax=" in ln, "probe": g("probe"),
                     "fails": g("fails"), "line": ln})
    return rows


def last_restart(rows):
    start = rows[0]["t"] if rows else None
    for a, b in zip(rows, rows[1:]):
        if b["up"] < a["up"]:
            start = b["t"] - timedelta(seconds=b["up"])
    return start


def cause_of(stack_lines):
    """Innermost frame in our own code, and its source line."""
    best = None
    for i, ln in enumerate(stack_lines):
        m = FRAME.match(ln)
        if not m:
            continue
        path = m.group(1).replace("\\", "/")
        if ".venv" in path or "WindowsApps" in path or "/Lib/" in path or "<frozen" in path:
            continue
        code = stack_lines[i + 1].strip() if i + 1 < len(stack_lines) else ""
        best = f"{path.rsplit('/', 1)[-1]}:{m.group(3)}  `{code[:90]}`"
    return best or "(no frame in our code: a C call or the stdlib)"


def parse_app(text, since):
    blocks, slow, lock, pool, disk, errors, dumps = [], [], [], [], [], [], []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        ln = lines[i]
        t = when(ln)
        if t and t >= since:
            if "LOOP BLOCKED" in ln:
                body = []
                j = i + 1
                while j < len(lines) and not when(lines[j]):
                    body.append(lines[j]); j += 1
                first = []
                for b in body[1:]:
                    if b.startswith("---"):
                        break
                    first.append(b)
                other = ""
                if "--- other threads running Python meanwhile ---" in body:
                    k = body.index("--- other threads running Python meanwhile ---")
                    other = cause_of(body[k + 1:k + 12])
                secs = float(re.search(r"BLOCKED (?:\(still\) )?([\d.]+)s", ln).group(1))
                blocks.append({"t": t, "s": secs, "still": "(still)" in ln,
                               "cause": cause_of(first), "other": other})
                i = j
                continue
            if "SLOW REQUEST" in ln:
                m = re.search(r"SLOW REQUEST ([\d.]+)s status=\d+ (.*)", ln)
                if m and float(m.group(1)) >= SLOW_REQ_S:
                    slow.append((t, float(m.group(1)), m.group(2).strip()))
            elif "VITALS ANOMALY" in ln:
                if "library lock held" in ln:
                    lock.append((t, re.search(r"library lock held [^;|]*", ln).group(0)))
                if "thread pool saturated" in ln:
                    pool.append((t, re.search(r"thread pool saturated [^;|]*", ln).group(0)))
                if re.search(r"; disk |ANOMALY: disk ", ln):
                    disk.append((t, re.search(r"disk [^|]*", ln).group(0)[:120]))
            elif "STALL DUMP written" in ln:
                dumps.append((t, ln.split("STALL DUMP written to ", 1)[-1][:140]))
            elif " ERROR " in ln or " CRITICAL " in ln:
                errors.append((t, ln[20:180]))
        i += 1
    # An ongoing "(still)" report is followed by the final one for the same block.
    final = [b for b in blocks if not b["still"]]
    for b in blocks:
        if b["still"] and not any(0 <= (f["t"] - b["t"]).total_seconds() <= 3600 for f in final):
            final.append(b)
    return sorted(final, key=lambda b: b["t"]), slow, lock, pool, disk, errors, dumps


def report(ver, logs, stall_files, since_arg):
    rows = parse_vitals(logs["vitals.log"])
    since = datetime.strptime(since_arg, "%Y-%m-%d %H:%M:%S") if since_arg else last_restart(rows)
    rows = [r for r in rows if r["t"] >= since]
    blocks, slow, lock, pool, disk, errors, dumps = parse_app(logs["streamlink_app.log"], since)
    out, act = [], []
    end = rows[-1]["t"] if rows else since
    out.append(f"build {ver}   window {since:%m-%d %H:%M:%S} -> {end:%H:%M:%S} "
               f"({(end - since).total_seconds() / 60:.0f} min, {len(rows)} vitals lines)")
    if rows and not rows[-1]["has_lagmax"]:
        out.append("  NOTE: this build has no lagmax=; lag= is one tick per 30 s and misses freezes")

    # 1. Loop freezes: lagmax per vitals line (measured), LOOP BLOCKED (named).
    lm = [r["lagmax"] * 1000 for r in rows]
    hard_l = [r for r in rows if r["lagmax"] * 1000 >= HARD_MS]
    soft_l = [r for r in rows if SOFT_MS <= r["lagmax"] * 1000 < HARD_MS]
    out.append(f"loop     worst {max(lm, default=0):.0f} ms | >= {HARD_MS} ms: {len(hard_l)} "
               f"| {SOFT_MS}-{HARD_MS - 1} ms: {len(soft_l)}")
    by = collections.defaultdict(list)
    for b in blocks:
        by[b["cause"]].append(b)
    for cause, bs in sorted(by.items(), key=lambda kv: -max(b["s"] for b in kv[1])):
        worst = max(b["s"] for b in bs) * 1000
        hard = worst >= HARD_MS
        rep = sum(1 for b in bs if b["s"] * 1000 >= SOFT_MS) >= SOFT_REPEAT
        tag = "HARD" if hard else ("REPEAT" if rep else "soft")
        if hard or rep:
            act.append(f"{tag}: {cause}")
        other = f"  [GIL held by: {bs[0]['other']}]" if bs[0]["other"] else ""
        out.append(f"  {tag:6} {len(bs)}x worst {worst:.0f} ms  {cause}{other}  "
                   f"(last {bs[-1]['t']:%H:%M:%S})")
    for r in hard_l:
        near = any(abs((b["t"] - r["t"]).total_seconds()) <= 35 for b in blocks)
        if not near:
            act.append(f"HARD: {r['lagmax'] * 1000:.0f} ms at {r['t']:%H:%M:%S} with no LOOP BLOCKED report")
            out.append(f"  HARD   lagmax {r['lagmax'] * 1000:.0f} ms at {r['t']:%H:%M:%S}, unnamed "
                       f"(a C call holding the GIL, or a build older than 20.25.20)")

    # 2. The server not answering although the loop was free.
    pr = [r for r in rows if r["probe"] >= PROBE_HARD_MS and r["lagmax"] * 1000 < SOFT_MS]
    out.append(f"probe    worst {max((r['probe'] for r in rows), default=0):.0f} ms | "
               f">= {PROBE_HARD_MS} ms with the loop free: {len(pr)} | probe fails: "
               f"{int(max((r['fails'] for r in rows), default=0) - min((r['fails'] for r in rows), default=0))}")
    for r in pr:
        act.append(f"HARD: probe {r['probe']:.0f} ms at {r['t']:%H:%M:%S}, loop free")
    for t, what in lock:
        out.append(f"  lock   {t:%H:%M:%S} {what}")
    for t, what in pool:
        out.append(f"  pool   {t:%H:%M:%S} {what}")

    # 3. The whole process or machine stopped (no vitals line at all).
    gaps = [(a["t"], (b["t"] - a["t"]).total_seconds()) for a, b in zip(rows, rows[1:])
            if (b["t"] - a["t"]).total_seconds() > GAP_S]
    out.append(f"stalls   vitals gaps > {GAP_S} s: {len(gaps)}" +
               "".join(f"\n  GAP    {t:%H:%M:%S} {g:.0f} s" for t, g in gaps))
    for t, g in gaps:
        act.append(f"HARD: no vitals for {g:.0f} s after {t:%H:%M:%S} (process or machine stall)")
    for t, what in disk:
        out.append(f"  disk   {t:%H:%M:%S} {what}")

    # 4. Not freezes, listed so they are not lost.
    out.append(f"slow     requests >= {SLOW_REQ_S} s: {len(slow)}")
    agg = collections.defaultdict(list)
    for t, s, req in slow:
        agg[re.sub(r"[0-9a-f]{16,}|[0-9a-f-]{36}", "{id}", req.split("?")[0])].append(s)
    for req, ss in sorted(agg.items(), key=lambda kv: -max(kv[1])):
        out.append(f"  slow   {len(ss)}x worst {max(ss):.0f} s  {req}")
    out.append(f"errors   {len(errors)} ERROR/CRITICAL lines, {len(dumps)} stall dumps")
    for t, e in errors[:8]:
        out.append(f"  {t:%H:%M:%S} {e}")
    for t, d in dumps:
        out.append(f"  dump   {t:%H:%M:%S} {d}")

    verdict = "ACT" if act else "PASS"
    kinds = collections.Counter(x.split(":", 1)[0] for x in act)
    named = [x.split(": ", 1)[1] for x in dict.fromkeys(act)
             if x.startswith(("HARD: ", "REPEAT: ")) and ".py:" in x]
    head = f"VERDICT: {verdict}"
    if act:
        head += (f"  ({len(hard_l)} loop freezes >= {HARD_MS} ms, worst {max(lm, default=0):.0f} ms; "
                 f"{len(pr)} slow probes; {len(gaps)} vitals gaps; "
                 f"{kinds.get('REPEAT', 0)} repeating causes)")
        head += "".join(f"\n  fix: {c}" for c in named)
    return head + "\n" + "\n".join(out), verdict


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--box", default="https://192.168.0.106")
    ap.add_argument("--since", help='"YYYY-MM-DD HH:MM:SS" (default: last restart)')
    ap.add_argument("--save", help="directory to keep the fetched logs in")
    ap.add_argument("--from-dir", help="read vitals.log + streamlink_app.log from here, not the box")
    a = ap.parse_args()
    if a.from_dir:
        logs = {}
        for n in ("vitals.log", "streamlink_app.log"):
            with open(os.path.join(a.from_dir, n), encoding="utf-8", errors="replace") as f:
                logs[n] = f.read()
        ver, stalls = "(from disk)", []
    else:
        pw = os.environ.get("STREAMLINK_ADMIN_PASSWORD")
        if not pw:
            sys.exit("set STREAMLINK_ADMIN_PASSWORD")
        try:
            ver, logs, stalls = fetch(a.box.rstrip("/"), pw)
        except Exception as exc:
            print(f"could not read the box: {type(exc).__name__}: {exc}")
            sys.exit(1)
    if a.save:
        os.makedirs(a.save, exist_ok=True)
        for n, txt in logs.items():
            with open(os.path.join(a.save, n), "w", encoding="utf-8") as f:
                f.write(txt)
    text, verdict = report(ver, logs, stalls, a.since)
    print(text)
    sys.exit(2 if verdict == "ACT" else 0)


if __name__ == "__main__":
    main()
