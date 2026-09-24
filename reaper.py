"""Which leftover paths may StreamLink delete after it removes a torrent?

qBittorrent's "delete with files" removes the files the TORRENT owns and nothing
else, and on Windows it gives up silently on any file another process holds open.
Both leave clutter that nothing owns any more, which is all the admin Cleanup tab's
"stray" list ever was:

* StreamLink's own files beside the media — subtitle sidecars
  (`<stem>.<lang>.opensubs.srt`, AI `.srt`), the `.streamlink_cache` bundle folder —
  keep the torrent's folder non-empty, so qBit can't remove it.
* A file ffmpeg (prep), the analyzer, or VLC had open at the moment of the delete.
* qBit's own `.<infohash>.parts` file, which outlives its torrent.

The main process records what a delete was supposed to remove and comes back to
finish the job. This module is the part of that which must be right before
anything is unlinked, so it is pure and tested:

* `may_reap` — never outside a configured root, never a root or anything above
  one, never a path any live torrent or library file still owns (a retry can land
  in the same folder name as the torrent it replaced). Missing evidence never
  reads as permission: the caller passes the owned set only when it could build
  one.
* `is_sidecar` — the exact names StreamLink writes beside a video.
* `parts_hash` — recognise qBit's `.parts` file and the torrent it belongs to.
* `retry_delay` — backoff for a file something still has open.

Stdlib only; no `main` import. Tests: tests/test_reaper.py.
"""
from __future__ import annotations

import os
import re
from pathlib import PurePath
from typing import Iterable, Optional

SUB_EXTS = {".srt", ".ass", ".ssa", ".vtt", ".sub", ".idx"}

_PARTS_RE = re.compile(r"^\.([0-9a-fA-F]{40})\.parts$")

# Seconds to wait before retry N (1-based). A prep encode finishes within minutes,
# a long analysis within the hour; anything still locked after the last step is
# genuinely stuck and belongs to a human (it surfaces in the Cleanup tab).
_RETRY_DELAYS = (30, 120, 600, 1800, 3600, 3 * 3600, 6 * 3600, 12 * 3600, 24 * 3600)
MAX_ATTEMPTS = len(_RETRY_DELAYS)


def norm(path: str) -> str:
    """Case- and separator-insensitive key (Windows paths collide correctly)."""
    try:
        return os.path.normcase(os.path.normpath(os.path.abspath(str(path))))
    except Exception:
        return os.path.normcase(str(path))


def _inside(child: str, parent: str) -> bool:
    """`child` strictly below `parent` (both already normalised)."""
    parent = parent.rstrip("\\/")
    return child != parent and (child.startswith(parent + os.sep)
                                or child.startswith(parent + "/"))


def may_reap(target: str, roots: Iterable[str], owned: Iterable[str]) -> tuple[bool, str]:
    """May `target` be deleted? Returns (ok, reason) — reason is "" when ok, else
    "no-root" (outside every configured root), "root" (is or contains a root) or
    "owned" (something live still claims it, it, or something inside it)."""
    t = norm(target)
    nroots = [norm(r) for r in roots if (r or "").strip()]
    if not any(_inside(t, r) for r in nroots):
        return False, "no-root"
    if any(t == r or _inside(r, t) for r in nroots):
        return False, "root"
    for o in owned:
        o = norm(o)
        if o == t or _inside(o, t) or _inside(t, o):
            return False, "owned"
    return True, ""


def is_sidecar(video_name: str, candidate: str) -> bool:
    """Is `candidate` a subtitle sidecar StreamLink (or the release) put beside
    `video_name`? Matches `<stem>.<anything>.<sub ext>` in the same folder."""
    if candidate == video_name:
        return False
    stem = PurePath(video_name).stem
    ext = PurePath(candidate).suffix.lower()
    return ext in SUB_EXTS and candidate.startswith(stem + ".")


def parts_hash(name: str) -> Optional[str]:
    """The info-hash of a qBit `.parts` file name, lowercased; None otherwise."""
    m = _PARTS_RE.match(name or "")
    return m.group(1).lower() if m else None


def retry_delay(attempts: int) -> Optional[float]:
    """Delay before the next try after `attempts` failed ones; None = give up."""
    if attempts < 1:
        return 0.0
    if attempts > MAX_ATTEMPTS:
        return None
    return float(_RETRY_DELAYS[attempts - 1])


def prunable_parents(path: str, roots: Iterable[str]) -> list[str]:
    """Parent folders of `path`, innermost first, stopping BELOW the root that
    contains it. The caller removes each only if it is empty."""
    p = norm(path)
    nroots = [norm(r) for r in roots if (r or "").strip()]
    root = next((r for r in nroots if _inside(p, r)), None)
    if root is None:
        return []
    out: list[str] = []
    cur = os.path.dirname(p)
    while _inside(cur, root):
        out.append(cur)
        nxt = os.path.dirname(cur)
        if nxt == cur:
            break
        cur = nxt
    return out
