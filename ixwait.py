"""How long does a search wait for each indexer?

An indexer behind Cloudflare is reached through FlareSolverr, which opens a real
browser and solves the challenge. That works, and it is slow. Measured on the
box, 2026-10-08 (Jackett v0.24.2806, FlareSolverr on localhost):

    1337x   every uncached search   11.4 - 19.9 s  (12 searches, none under 11)
    1337x   Jackett's own test      10.2 - 36.9 s
    EZTV    its first test          44.8 s, then 0.5 s a search (cookie kept)
    five others (TPB, TheRARBG, TorrentGalaxyClone, YTS, Nyaa)   0.03 - 3 s

Search gave every indexer 12 s and the admin Test 15 s. So 1337x was reported
"failing" on every search, with no reason (a timeout's message is empty), and
Test said "could not reach Jackett" for both, while Jackett itself went on to
finish and had the results.

Waiting 20 s on every search for one site is not the answer either. So:

- the request to Jackett is never abandoned early (`HARD`); it finishes in the
  background and its answer is in Jackett's cache for the next search,
- a search waits for the indexers that usually answer quickly, plus `GRACE`
  for the rest (a cached answer from a slow one arrives in milliseconds),
- what is still running is reported as pending, not as failed, and a caller
  that can use late results asks again with `patient`.

Which indexers are slow is learned, per process, from how long they took. A
restart forgets it and relearns at the cost of one 12 s search.

Leaf module: stdlib only, no `main` import. Tests in `tests/test_ixwait.py`.
"""

from collections import deque

SOFT = 12.0     # the longest an interactive search waits for anything
GRACE = 1.0     # extra given to slow indexers once the quick ones are in
SLOW = 6.0      # an answer this late marks its indexer slow
HARD = 75.0     # the request to Jackett itself (FlareSolverr's own cap is 55 s)
ADMIN = 120.0   # Test (and saving a tracker with a login): Jackett searches first
WINDOW = 6      # answers remembered per indexer
SLOW_PARALLEL = 3   # searches a slow indexer runs at once (each is a browser)


class Pace:
    def __init__(self, window=WINDOW, slow=SLOW):
        self.window, self.slow = window, slow
        self._recent = {}

    def note(self, indexer_id, secs):
        self._recent.setdefault(indexer_id, deque(maxlen=self.window)).append(float(secs))

    def is_slow(self, indexer_id):
        """True while any remembered answer was late. One late answer is enough:
        the quick ones in between are Jackett's cache, not the site."""
        return any(s >= self.slow for s in self._recent.get(indexer_id, ()))

    def last(self, indexer_id):
        r = self._recent.get(indexer_id)
        return r[-1] if r else None


def more_wait(elapsed, quick_pending, slow_pending, quick_done_at, have_results,
              patient=False):
    """Seconds a search should go on waiting; 0 means answer now.

    `quick_done_at` is when the last quick indexer answered (None when the
    search has no quick indexer at all). A search with nothing to show keeps
    waiting to `SOFT`, and one whose only indexers are slow waits them out:
    there is no earlier answer worth giving.
    """
    if not (quick_pending or slow_pending):
        return 0.0
    if patient:
        return max(0.0, HARD - elapsed)
    if quick_pending:
        limit = SOFT
    elif quick_done_at is None:
        limit = HARD
    elif not have_results:
        limit = SOFT
    else:
        limit = min(SOFT, quick_done_at + GRACE)
    return max(0.0, limit - elapsed)


def failure_text(exc):
    """A reason a person can read. A timeout's own message is the empty string,
    which is how a failing indexer came to be listed with no reason at all."""
    name = type(exc).__name__
    if "Timeout" in name:
        return "no answer from Jackett in %d s" % HARD
    return (str(exc) or name)[:200]


def jackett_error(text):
    """Jackett reports a failing indexer as a .NET stack trace, 25 lines of it.
    The first line says what went wrong; the innermost cause is on the next
    `--->` line and is usually the readable one ("The SSL connection could not
    be established")."""
    lines = [ln.strip() for ln in str(text or "").splitlines() if ln.strip()]
    if not lines:
        return ""
    head = lines[0]
    for ln in lines[1:]:
        if ln.startswith("--->"):
            cause = ln[4:].strip().split(": ", 1)[-1]
            if cause and cause not in head:
                head = "%s (%s)" % (head, cause)
            break
    return head[:300]
