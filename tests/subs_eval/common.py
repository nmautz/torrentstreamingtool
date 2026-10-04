"""Shared plumbing for the subtitle-search eval: a polite cached client for the
keyless legacy OpenSubtitles API, a subtitle parser, and the grader.

Grading a download against the file's own embedded English track:

* content — do the two tracks say the same things? Word-set overlap of their
  dialogue, calibrated against OTHER episodes of the same show (see score.py).
  Anime fansubs and official subs are different translations, so this is a
  score, not a yes/no.
* timing  — speech-activity overlap at the best whole-file offset, plus the best
  offset measured separately over the first and last third. Both thirds within
  SYNC_TOL of zero = in sync as delivered; both thirds agreeing on a non-zero
  offset = a constant shift an aligner can fix; thirds disagreeing = drift
  (framerate / different cut).
"""
import gzip, hashlib, json, os, re, time, urllib.error, urllib.parse, urllib.request
from pathlib import Path

HERE = Path(__file__).parent
CACHE = HERE / ".cache"
UA = "TemporaryUserAgent"
SYNC_TOL = 0.5
_last = [0.0]


def os_get(url, raw=False, follow=True):
    """GET with an on-disk cache (the eval replays freely without re-hitting
    OpenSubtitles). Throttled to ~3 req/s — the legacy API allows 40 per 10 s."""
    d = CACHE / "http"
    d.mkdir(parents=True, exist_ok=True)
    f = d / hashlib.sha1(url.encode()).hexdigest()
    if f.exists():
        data = f.read_bytes()
    elif os.environ.get("SUBS_EVAL_OFFLINE"):
        return None                        # cache only — never touch the network
    else:
        wait = (0.35 if "/search/" in url else 1.0) - (time.time() - _last[0])
        if wait > 0:
            time.sleep(wait)
        _last[0] = time.time()
        try:
            r = urllib.request.Request(url, headers={"User-Agent": UA, "X-User-Agent": UA})
            opener = urllib.request.build_opener() if follow else urllib.request.build_opener(_NoRedirect)
            with opener.open(r, timeout=40) as resp:
                data = resp.read()
        except urllib.error.HTTPError as e:
            body = e.read() or b""
            if b"exceeded" in body:            # the ban page — don't cache, don't retry
                raise BudgetExhausted(f"OpenSubtitles download limit hit for IP {public_ip()}")
            data = b"__ERR__ " + str(e).encode()
        except Exception as e:
            data = b"__ERR__ " + str(e).encode()
        f.write_bytes(data)
    if data.startswith(b"__ERR__"):
        return None
    if raw:
        return data
    try:
        j = json.loads(data)
        return j if isinstance(j, list) else []
    except Exception:
        return None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


# OpenSubtitles legacy: ~200 downloads / IP / 24 h, then a 24 h ban. The home IP is
# shared with the box, so the kit leaves it most of the allowance; any other IP
# (a VPN exit) can go close to the limit — a ban there just means "swap server".
HOME_IP = "136.27.48.134"
DAILY_DOWNLOADS = 120
DAILY_DOWNLOADS_AWAY = 180


_ip = []


def public_ip():
    """The limit is per public IP — a VPN on this PC gets its own allowance."""
    if not _ip:
        try:
            with urllib.request.urlopen("https://api.ipify.org", timeout=15) as r:
                _ip.append(r.read().decode().strip())
        except Exception:
            _ip.append("unknown")
    return _ip[0]


def _budget_ok():
    f = CACHE / "dl_log.json"
    try:
        log = json.loads(f.read_text()) if f.exists() else {}
    except Exception:
        log = {}
    if not isinstance(log, dict):
        log = {}
    ip = public_ip()
    mine = [t for t in log.get(ip, []) if time.time() - t < 86400]
    if len(mine) >= (DAILY_DOWNLOADS if ip in (HOME_IP, "unknown") else DAILY_DOWNLOADS_AWAY):
        return False
    mine.append(time.time())
    log[ip] = mine
    f.write_text(json.dumps(log))
    return True


class BudgetExhausted(Exception):
    pass


def download(link):
    cached = (CACHE / "http" / hashlib.sha1(link.encode()).hexdigest()).exists()
    if not cached and not os.environ.get("SUBS_EVAL_OFFLINE") and not _budget_ok():
        raise BudgetExhausted(f"daily download budget used up for IP {public_ip()}")
    b = os_get(link, raw=True)
    if b and b[:2] == b"\x1f\x8b":
        try:
            b = gzip.decompress(b)
        except Exception:
            return None
    return b


# ── parsing ──────────────────────────────────────────────────────────────────

def _decode(b):
    if b.startswith(b"\xef\xbb\xbf"):
        b = b[3:]
    if b.startswith((b"\xff\xfe", b"\xfe\xff")):
        return b.decode("utf-16", "replace")
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return b.decode("cp1252", "replace")


_TS = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})")


def _ts(s):
    m = _TS.search(s)
    if not m:
        return None
    h, mi, se, frac = m.groups()
    return int(h or 0) * 3600 + int(mi) * 60 + int(se) + int(frac.ljust(3, "0")[:3]) / 1000


def _clean(t):
    t = re.sub(r"\{[^}]*\}", "", t)          # ASS override tags
    t = re.sub(r"<[^>]+>", "", t)            # html-ish tags
    t = t.replace("\\N", " ").replace("\\n", " ").replace("\\h", " ")
    return re.sub(r"\s+", " ", t).strip()


def parse_subs(b, fps=23.976):
    """bytes -> [(start, end, text)], sorted. SRT, VTT, ASS/SSA, MicroDVD."""
    if not b:
        return []
    s = _decode(b)
    cues = []
    if re.search(r"(?im)^\[events\]", s):
        fmt, dropped = None, []
        for line in s.splitlines():
            if line.lower().startswith("format:") and fmt is None and "start" in line.lower():
                fmt = [x.strip().lower() for x in line.split(":", 1)[1].split(",")]
            elif line.startswith("Dialogue:") and fmt:
                parts = line.split(":", 1)[1].split(",", len(fmt) - 1)
                row = dict(zip(fmt, parts))
                st, en = _ts(row.get("start", "")), _ts(row.get("end", ""))
                style = (row.get("style") or "").lower()
                txt = _clean(row.get("text", ""))
                if st is None or en is None or not txt:
                    continue
                if re.search(r"sign|song|^op|^ed|karaoke|title", style) and "default" not in style:
                    dropped.append((st, en, txt))
                    continue
                cues.append((st, en, txt))
        if len(cues) < len(dropped):       # style names we misread — keep everything
            cues += dropped
    elif re.match(r"\s*\{\d+\}\{\d+\}", s):
        for m in re.finditer(r"(?m)^\{(\d+)\}\{(\d+)\}(.*)$", s):
            a, e, t = int(m.group(1)), int(m.group(2)), m.group(3)
            if a == 1 and e == 1:                 # fps header line
                try:
                    fps = float(t)
                except ValueError:
                    pass
                continue
            cues.append((a / fps, e / fps, _clean(t.replace("|", " "))))
    else:
        for block in re.split(r"\r?\n\s*\r?\n", s):
            m = re.search(r"([\d:.,]+)\s*-->\s*([\d:.,]+)", block)
            if not m:
                continue
            st, en = _ts(m.group(1)), _ts(m.group(2))
            if st is None or en is None:
                continue
            txt = _clean(" ".join(block[m.end():].splitlines()))
            if txt:
                cues.append((st, en, txt))
    cues = [(a, max(a + 0.05, e), t) for a, e, t in cues if e >= 0]
    cues.sort()
    return cues


def sub_format(b):
    s = _decode(b[:4000]) if b else ""
    if re.search(r"(?im)^\[script info\]|^\[events\]", s):
        return "ass"
    if s.lstrip().startswith("WEBVTT"):
        return "vtt"
    if re.match(r"\s*\{\d+\}\{\d+\}", s):
        return "sub"
    if "-->" in s:
        return "srt"
    return "?"


# ── grading ──────────────────────────────────────────────────────────────────

_STOP = set("""the and you that was for are with his they this have from one had not but what all were when
we there can your which their said will each about how him them then she her would make like has into
more could its now than been who did get just yes yeah don't i'm it's you're that's what's let's okay
here out know right come going well got really gonna want see look there's this can't didn't we're
oh hey uh um ah""".split())


def words(cues):
    out = set()
    for _, _, t in cues:
        for w in re.findall(r"[a-z][a-z']{2,}", t.lower()):
            if w not in _STOP:
                out.add(w)
    return out


def content_score(ref, cand):
    """Share of the candidate's distinctive words that the reference also uses,
    and vice versa — the smaller of the two, so a tiny or giant file can't game it."""
    a, b = words(ref), words(cand)
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return min(inter / len(a), inter / len(b))


STEP = 0.1


def _bits(cues, lo, hi, pad=0.0):
    """Speech activity over [lo-pad, hi+pad] as an int bitset, bit k = bin k."""
    base = lo - pad
    n = int((hi + pad - base) / STEP) + 1
    v = 0
    for a, e, _ in cues:
        i, j = max(0, int((a - base) / STEP)), min(n - 1, int((e - base) / STEP))
        if j >= i:
            v |= ((1 << (j - i + 1)) - 1) << i
    return v


def _best_offset(ref, cand, lo, hi, search=90.0):
    """Offset (s) to ADD to cand to best match ref over [lo, hi], and the
    overlap (IoU of speech-active 0.1 s bins) it achieves."""
    k = int(search / STEP)
    r = _bits(ref, lo, hi, pad=search)
    win = _bits([(lo, hi, "")], lo, hi, pad=search)        # scoring window mask
    r &= win
    rs = r.bit_count()
    if rs < 20:
        return None, 0.0
    c0 = _bits(cand, lo - search, hi + search, pad=search)  # wide: shifts bring text in
    best = (0.0, 0)
    for s in range(-k, k + 1):
        # cand shifted by +s bins; c0 starts `search` s earlier than r
        c = (c0 >> (k - s) if k - s >= 0 else c0 << (s - k)) & win
        inter = (r & c).bit_count()
        uni = rs + c.bit_count() - inter
        v = inter / uni if uni else 0.0
        if v > best[0]:
            best = (v, s)
    return round(best[1] * STEP, 2), round(best[0], 3)


def _cue_words(t):
    return {w for w in re.findall(r"[a-z][a-z']{2,}", t.lower()) if w not in _STOP}


def aligned_text(ref, cand, offsets, end):
    """Do lines that sit at the same moment share words? For each reference cue,
    the fraction of its content words found in candidate lines within ±1 s once
    the candidate is shifted by the offset for that part of the file. The right
    episode scores well even across translations (names, key nouns); a
    different episode sharing the show's vocabulary does not, because the words
    land at the wrong moments."""
    if not ref or not cand:
        return 0.0
    n = len(offsets)
    shifted = [[(a + (o or 0), e + (o or 0), _cue_words(t)) for a, e, t in cand] for o in offsets]
    hit = tot = 0
    for a, e, t in ref:
        w = _cue_words(t)
        if not w:
            continue
        cs = shifted[min(n - 1, int(a / (end / n)))]
        near = set()
        for ca, ce, cw in cs:          # cues are sorted; linear scan is fine at this size
            if ce >= a - 1.0 and ca <= e + 1.0:
                near |= cw
        hit += len(w & near)
        tot += len(w)
    return round(hit / tot, 3) if tot else 0.0


def timing(ref, cand, duration):
    if not ref or not cand:
        return {"iou": 0.0, "off": None, "off_a": None, "off_b": None, "sync": "none", "text": 0.0}
    end = duration or max(e for _, e, _ in ref)
    off, iou = _best_offset(ref, cand, 0, end)
    third = end / 3
    off_a, _ = _best_offset(ref, cand, 0, third, search=45)
    off_m, _ = _best_offset(ref, cand, third, 2 * third, search=45)
    off_b, _ = _best_offset(ref, cand, 2 * third, end, search=45)
    text = aligned_text(ref, cand, [off_a, off_m, off_b], end)
    if off_a is None or off_b is None:
        state = "none"
    elif abs(off_a) <= SYNC_TOL and abs(off_b) <= SYNC_TOL:
        state = "sync"
    elif abs(off_a - off_b) <= SYNC_TOL:
        state = "shift"                        # constant offset — alignable
    else:
        state = "drift"                        # framerate / cut mismatch
    return {"iou": iou, "off": off, "off_a": off_a, "off_b": off_b, "sync": state, "text": text}


def load_ref(key):
    """Text refs are .vtt; image-sub refs (collect_pgs.py) are timing-only .json."""
    j = CACHE / "refs" / f"{key}.json"
    if j.exists():
        return [tuple(c) for c in json.loads(j.read_text())]
    return parse_subs((CACHE / "refs" / f"{key}.vtt").read_bytes())
