"""Client diagnostic logs — keep every upload, never drop the old one.

The first version of this feature answered one question ("what did the external
display do in the last ten minutes?") and the storage matched: the phone
uploaded its whole rolling file and the server wrote it over the top of
whatever was there. Send twice and the first send is gone; halve the file on
the device and the oldest half is gone from the server too. The device's
3 MB rolling window was, in effect, the server's retention policy.

That is the wrong shape for the job it is now asked to do — sit there across
days of real use and let a later reader ask "when did offline playback break,
and what was the external display doing at the time?". So the server keeps its
own append-only transcript per device and the upload becomes a MERGE:

  * The phone always sends its whole file. That is the simple thing for a
    client to get right, and it means a phone that was offline for a week
    catches up in one tap.
  * The server appends only the rows it has never seen. Identity is the exact
    line, hashed — not the timestamp. Two rows can share a millisecond
    (`disarm` and `stopNative` did, in the 2026-09-22 log), and a clock that
    steps backwards must not silently swallow everything after it.
  * Nothing is deleted until a reader says so. Not by the daily archive, not by
    "clear all logs", not by the next upload.

`trim_to` is the one exception and it is a last resort: at the cap the OLDEST
half goes, because an incident being written now beats one from a month ago.

Pure: text in, text out, no I/O and no `main` import. Tests in
`tests/test_clientlog.py`.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Iterable, List, Optional, Tuple

# Rows the device sends are small (~200-400 bytes). A megabyte is roughly three
# thousand of them, so this holds months of ordinary use per device.
DEFAULT_MAX_BYTES = 32 * 1024 * 1024


def line_key(line: str) -> str:
    """Identity of one stored row.

    The whole line, hashed, rather than `(t, ev)`: a re-upload repeats bytes
    exactly, while two genuinely different rows can share both a timestamp and
    an event name. Hashing keeps the seen-set small enough to hold in memory
    for a file at the cap.
    """
    return hashlib.blake2b(line.strip().encode("utf-8", "replace"),
                           digest_size=16).hexdigest()


def split_rows(text: str) -> List[str]:
    """NDJSON text to a list of non-empty lines, whitespace-stripped."""
    return [ln.strip() for ln in (text or "").splitlines() if ln.strip()]


def merge(existing: str, incoming: str) -> Tuple[str, int, int]:
    """Append the rows of `incoming` that `existing` does not already hold.

    Returns `(merged_text, added, skipped)`. Order is preserved and nothing is
    re-sorted: the device writes chronologically and each upload's new rows are
    newer than everything stored, so plain append keeps the transcript readable.
    Sorting would also be wrong — it would interleave two devices' clock skew
    into a timeline that never happened.
    """
    old = split_rows(existing)
    seen = {line_key(ln) for ln in old}
    added: List[str] = []
    skipped = 0
    for ln in split_rows(incoming):
        k = line_key(ln)
        if k in seen:
            skipped += 1
            continue
        seen.add(k)
        added.append(ln)
    out = old + added
    return ("\n".join(out) + "\n" if out else ""), len(added), skipped


def trim_to(text: str, max_bytes: int = DEFAULT_MAX_BYTES) -> Tuple[str, int]:
    """Drop the oldest half once the transcript passes `max_bytes`.

    Returns `(text, dropped_rows)`. Halving rather than truncating to exactly
    the cap means this runs once in a long while instead of on every append.
    """
    if len(text.encode("utf-8", "replace")) <= max_bytes:
        return text, 0
    rows = split_rows(text)
    keep = rows[len(rows) // 2:]
    return ("\n".join(keep) + "\n" if keep else ""), len(rows) - len(keep)


def high_water(text: str) -> str:
    """The newest timestamp in a transcript — the mark a clear records.

    Scans rather than reading the last line: the tail can be a half-written row
    from an interrupted upload, and a clear that recorded "" would then drop
    nothing.
    """
    best = ""
    for ln in split_rows(text):
        r = parse(ln)
        if not r:
            continue
        t = str(r.get("t") or "")
        if t > best:
            best = t
    return best


def drop_through(text: str, mark: str) -> Tuple[str, int]:
    """Keep only rows STRICTLY newer than `mark`. Returns `(text, dropped)`.

    This is what makes a clear stick. The device holds the same rows the reader
    just finished with and will send them all again on its next upload; without
    this the merge would faithfully restore every one of them, and the clear
    would have achieved nothing but a round trip.

    Comparing the device's timestamps against a mark taken FROM the device's own
    timestamps means no clock is being compared to another clock. Rows written
    after the clear — which the reader has not seen — are strictly newer and
    survive, which is the point of doing this instead of discarding the upload.
    """
    if not mark:
        return text, 0
    kept, dropped = [], 0
    for ln in split_rows(text):
        r = parse(ln)
        t = str(r.get("t") or "") if r else ""
        # An unparseable or timestamp-less row cannot be placed relative to the
        # mark. Keep it: a stray duplicate is cheaper than dropping evidence.
        if t and t <= mark:
            dropped += 1
            continue
        kept.append(ln)
    return ("\n".join(kept) + "\n" if kept else ""), dropped


def parse(line: str) -> Optional[Dict[str, Any]]:
    """One stored row as a dict, or None if it is not JSON we can read."""
    try:
        row = json.loads(line)
    except Exception:
        return None
    return row if isinstance(row, dict) else None


def summarize(text: str) -> Dict[str, Any]:
    """What a reader needs before deciding whether to read the whole thing.

    Deliberately includes the per-category and per-event histograms: the first
    question asked of one of these files is always "is the thing I care about
    even in here?", and counting `ev` values answers it without a download.
    """
    rows = split_rows(text)
    first = last = ""
    evs: Dict[str, int] = {}
    cats: Dict[str, int] = {}
    launches = 0
    errors = 0
    for ln in rows:
        r = parse(ln)
        if not r:
            continue
        t = str(r.get("t") or "")
        if t:
            if not first:
                first = t
            last = t
        ev = str(r.get("ev") or "?")
        evs[ev] = evs.get(ev, 0) + 1
        cat = str(r.get("cat") or "-")
        cats[cat] = cats.get(cat, 0) + 1
        if ev == "launch":
            launches += 1
        # A row is an error if it says so in the shape every writer uses: an
        # `err` string, or a non-2xx `status`. Counting these up front is what
        # turns "9 000 rows" into "9 000 rows, 3 of which went wrong".
        if r.get("err"):
            errors += 1
        elif isinstance(r.get("status"), int) and not (200 <= r["status"] < 300):
            errors += 1
    return {
        "rows":     len(rows),
        "bytes":    len(text.encode("utf-8", "replace")),
        "first":    first,
        "last":     last,
        "launches": launches,
        "errors":   errors,
        "events":   dict(sorted(evs.items(), key=lambda kv: -kv[1])),
        "cats":     dict(sorted(cats.items(), key=lambda kv: -kv[1])),
    }


def select(text: str, *, since: str = "", until: str = "",
           events: Iterable[str] = (), cats: Iterable[str] = (),
           errors_only: bool = False, limit: int = 0,
           tail: bool = False) -> List[str]:
    """Filter stored rows without downloading the whole transcript.

    `since`/`until` compare ISO-8601 timestamps as STRINGS. That is exact for
    this writer, which always emits UTC with a `Z` and fractional seconds, and
    it avoids making a pure module depend on a date parser's edge cases.
    """
    ev_set = {e for e in events if e}
    cat_set = {c for c in cats if c}
    out: List[str] = []
    for ln in split_rows(text):
        r = parse(ln)
        if r is None:
            # Unparseable rows are kept only when nothing is being filtered on —
            # a corrupt line is evidence too, but it cannot answer a question
            # about which event it was.
            if not (ev_set or cat_set or since or until or errors_only):
                out.append(ln)
            continue
        t = str(r.get("t") or "")
        if since and t < since:
            continue
        if until and t > until:
            continue
        if ev_set and str(r.get("ev") or "") not in ev_set:
            continue
        # Missing `cat` reads as "-", the same name `summarize` gives it. Rows
        # written before categories existed are still in every transcript, and
        # a filter that names them has to agree with the histogram that counts
        # them or "cats=-" finds nothing while the summary says there are sixty.
        if cat_set and str(r.get("cat") or "-") not in cat_set:
            continue
        if errors_only:
            bad = bool(r.get("err"))
            st = r.get("status")
            if isinstance(st, int) and not (200 <= st < 300):
                bad = True
            if not bad:
                continue
        out.append(ln)
    if limit and limit > 0 and len(out) > limit:
        out = out[-limit:] if tail else out[:limit]
    return out
