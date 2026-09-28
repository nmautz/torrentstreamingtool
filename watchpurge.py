"""Which watched episodes may "Delete watched" take off the disk?

Two surfaces ask. A viewer on a show's page deletes what THEY have watched; the
admin deletes what one or more chosen profiles have watched, across the library.
Both delete the whole episode from the host -- source and bundle -- and keep the
library row, marked Skip, so it can be downloaded again. Watch history is not
touched: a deleted episode stays watched.

The rules, in the order they are applied:

  * **Selected** means *watched*, and watched means the progress record says
    `completed` (see `watchrule.py`) -- never a position near the end. With
    several profiles chosen, `match="all"` needs every one of them to have
    finished it and `match="any"` needs just one.
  * Something that is not on the host any more (no source, no bundle) is not
    listed at all. There is nothing to free.
  * **In progress for ANYONE keeps it** -- not only the chosen profiles. Deleting
    the episode your sibling is half-way through, because you finished it, is
    exactly the surprise this feature must never produce. Same 5-second floor
    the resume logic and source eviction use, so a stray tap doesn't pin a file.
  * **In use keeps it**: playing in VLC, feeding a JIT session, being prepped or
    compressed.
  * **Compressed keeps it**: a compressed-in-place file is no longer torrent
    backed, so "stays re-downloadable" would be a lie. `delete-files` refuses
    these for the same reason.

Missing evidence keeps, never takes: a record we cannot read is not "watched".
Pure: stdlib only, no `main` import. Tests in `tests/test_watchpurge.py`.
"""

from __future__ import annotations

# A position under this many seconds is an accidental tap, not a viewer part-way
# through. Mirrors `_evict_in_progress_paths` in main.py and the resume logic.
IN_PROGRESS_FLOOR_SEC = 5.0

MATCH_ALL = "all"
MATCH_ANY = "any"

DELETE = "delete"
KEEP_IN_PROGRESS = "in-progress"
KEEP_IN_USE = "in-use"
KEEP_COMPRESSED = "compressed"

# Keep reasons, most important first -- a file that is both in use and in
# progress is reported as in progress, the one the viewer can act on.
KEEP_ORDER = (KEEP_IN_PROGRESS, KEEP_IN_USE, KEEP_COMPRESSED)


def record_state(rec) -> str:
    """One profile's progress record for one file: "watched", "in-progress" or ""."""
    if not isinstance(rec, dict):
        return ""
    if rec.get("completed") is True:
        return "watched"
    try:
        pos = float(rec.get("position_sec", 0) or 0)
    except (TypeError, ValueError):
        return ""
    return "in-progress" if pos > IN_PROGRESS_FLOOR_SEC else ""


def file_viewers(records: dict) -> tuple:
    """`{profile_id: record}` for one file -> `(watched_by, in_progress_by)` sets."""
    watched, partial = set(), set()
    for pid, rec in (records or {}).items():
        st = record_state(rec)
        if st == "watched":
            watched.add(pid)
        elif st == "in-progress":
            partial.add(pid)
    return watched, partial


def is_selected(watched_by, profile_ids, match: str = MATCH_ALL) -> bool:
    """Did the chosen profiles watch it? No profiles chosen selects nothing."""
    chosen = set(profile_ids or ())
    if not chosen:
        return False
    have = set(watched_by or ())
    if match == MATCH_ANY:
        return bool(chosen & have)
    return chosen <= have


def verdict(*, watched_by, in_progress_by, profile_ids, match: str = MATCH_ALL,
            on_disk: bool, busy: bool = False, compressed: bool = False):
    """What "Delete watched" does with one file.

    None -- not listed (not watched by the selection, or nothing on the host);
    `DELETE`; or one of the `KEEP_*` reasons.
    """
    if not on_disk or not is_selected(watched_by, profile_ids, match):
        return None
    if in_progress_by:
        return KEEP_IN_PROGRESS
    if busy:
        return KEEP_IN_USE
    if compressed:
        return KEEP_COMPRESSED
    return DELETE


def group_rows(rows) -> list:
    """Roll per-file rows up into one entry per show/film.

    Each row is a dict with at least `group`, `group_title`, `verdict` and
    `bytes`. Returns groups ordered by bytes to free (largest first, then title),
    each `{group, title, delete: [rows], keep: [rows], bytes}` where `bytes` sums
    the rows being deleted only. Rows keep their given order inside a group.
    """
    out: dict = {}
    for r in rows:
        v = r.get("verdict")
        if v is None:
            continue
        g = out.setdefault(r["group"], {"group": r["group"], "title": r.get("group_title", ""),
                                        "delete": [], "keep": [], "bytes": 0})
        if v == DELETE:
            g["delete"].append(r)
            g["bytes"] += int(r.get("bytes", 0) or 0)
        else:
            g["keep"].append(r)
    return sorted(out.values(), key=lambda g: (-g["bytes"], (g["title"] or "").lower()))
