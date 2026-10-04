"""Episode-title agreement as a soft rule: a release whose named episode title
contradicts TMDb's name for the requested episode sinks below the others, and is
only shown if nothing else survives."""
import re
_SXE = re.compile(r"[Ss]\d{1,2}[Ee]\d{1,3}(?![0-9])")
_TAG = re.compile(r"(?i)^(\d{3,4}p|2160p|4k|uhd|web|webdl|web-?dl|webrip|hdtv|pdtv|bluray|blu-ray|bdrip|brrip|dvdrip|"
                  r"remux|x26[45]|h\.?26[45]|hevc|avc|xvid|divx|av1|aac\S*|ac3|dts\S*|ddp?\d?|dd|flac\S*|opus|atmos|"
                  r"repack|proper|internal|uncut|extended|ws|hr|dubbed|subbed|multi\S*|vostfr|swesub|subfrench|"
                  r"french|truefrench|german|spanish|ita|eng|spa|jap|jpn|nl|nf|amzn|hulu|pcok|dsnp|atvp|hmax|max|cr|"
                  r"all4|ip|bbc|itv|iqy|viu|shahid|mp4|mkv|avi|ts|e\d{1,3}|v\d|s\d{1,2}|part|cour|batch|complete)$")
_DROP = {"the", "a", "an", "of", "to", "and", "in", "on", "at", "for", "with", "from", "episode", "chapter", "ep", "pt"}

def _words(t):
    return [w for w in re.sub(r"[^a-z0-9 ]", " ", (t or "").lower().replace("'", "")).split() if w]

def _key(t):
    return {w for w in _words(t) if w not in _DROP and not w.isdigit()}

def ep_title_verdict(c, x):
    """1 agrees, 0 no evidence, -1 contradicts."""
    want = _key(c.get("episode_name") or "")
    if not want:
        return 0
    m = _SXE.search(x["title"] or "")
    if not m:
        return 0
    seg = []
    for w in re.split(r"[ ._\[\]()]+", (x["title"] or "")[m.end():]):
        if not w:
            continue
        if _TAG.match(w) or "-" in w and len(w) > 6:
            break
        seg.append(w)
        if len(seg) >= 8:
            break
    got = _key(" ".join(seg))
    if not got:
        return 0
    return 1 if (got & want) else -1
