"""library.json: the one persistent store, and the only way to touch it.

`get_library` / `put_library` / `mutate_library` hold `runtime._lib_lock` (built
in the lifespan) around every read and write; never read or write library.json
outside them (CLAUDE.md § Style conventions). Saves are atomic and durable
(`_save_lib_raw`, with a bounded Windows retry), snapshotted to
library_backups/, and recovered from a snapshot when the file will not parse.
`_migrate_item` brings an item read from disk up to the current schema
(docs/LIBRARY_DATA.md).
"""
import asyncio
import json
import os
import sys
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import diagnostics as diag
import episodes

from backend import runtime
from backend.logs import log
from backend.paths import ROOT
from backend.story_order import _SPLIT_TITLE_RE, _story_index


LIBRARY_FILE = ROOT / "library.json"
# ── Library Storage ───────────────────────────────────────────────────────────

# Bump to re-run the structural attribution pass over every existing item once.
# Stamped onto the item as `attrib_v` so the (regex-heavy) pass costs one run per
# item rather than one per library load — and `_load_lib_raw` runs on every load.
_ATTRIB_VERSION = 3


def _needs_attrib(f: dict) -> bool:
    """A file the structural pass failed to place: no episode number, and not
    parked in a bucket.

    Bucketed files are exempt on purpose — "Specials/Behind the Scenes.mkv" has
    no episode number because there is none to have, which is a correct end
    state, not a failure to retry.
    """
    return not int(f.get("episode", 0) or 0) and not (f.get("bucket") or "")


def _migrate_item(item: dict) -> dict:
    """Upgrade items written by older versions of the server in-place."""
    # v2.0 → v2.1: flat file_path → files list
    if not item.get("files") and item.get("file_path"):
        item["files"] = [{
            "name": Path(item["file_path"]).name,
            "path": item["file_path"],
            "size_bytes": item.get("size_bytes", 0),
            "season": item.get("season", 0),
            "episode": item.get("episode", 0),
        }]
    # v2.0 → v2.1: flat per-profile progress → per-file progress
    for prof_id, prog in list(item.get("progress", {}).items()):
        if isinstance(prog, dict) and "file_progress" not in prog and "position_sec" in prog:
            file_path = item.get("file_path", "")
            if file_path:
                item["progress"][prof_id] = {
                    "last_file": file_path,
                    "file_progress": {
                        file_path: {
                            "position_sec": prog.get("position_sec", 0),
                            "duration_sec": prog.get("duration_sec", 0),
                            "completed": prog.get("completed", False),
                            "updated_at": prog.get("updated_at", ""),
                        }
                    },
                }
    # 11.19.0: re-attribute files an older parser left at S0E0. That parser only
    # read SxxExx / NxNN off the basename, so every release that carries its
    # season in a FOLDER (the anime-batch norm) landed entirely in S0E0 — no
    # season tabs, no TMDb episode names, no stills.
    #
    # 14.0.1 widens the target from "no season AND no episode" to "no episode":
    # a season pack whose first file is named after the RELEASE
    # ("Futurama-1999-S03 1080p WEBRip 10bit EAC3 2 0 x265-iVy.mkv") reads its
    # season off the folder and nothing off the basename, so it settled at
    # season 3 / episode 0 — which the old test, requiring season 0 too, never
    # looked at. It sat first in the season as a nameless row while TMDb
    # reported episode 1 MISSING on a season the box holds complete.
    #
    # Only files that still have no episode number are written, so a number
    # already parsed (or corrected by `_reattribute_item_files`) is never
    # overwritten. Numbers that turn out to be series-absolute are fixed later
    # by that same function, which needs TMDb's season inventory.
    #
    # `_migrate_item` runs on every library load (hot loops re-read every few
    # seconds), so the result is stamped with `attrib_v`: the regex work happens
    # once per item, not once per load, and an item whose files genuinely can't
    # be numbered stops being retried forever.
    #
    # 20.24.2 (v3): a spin-off file has an episode AND a bucket, so the rule above
    # never re-reads one, and 20.24.0's new pass-1 rules ("2nd Season", "OADs",
    # "NC") never reached the pack they were written for. See
    # episodes.reread_spinoffs.
    files = item.get("files") or []
    if files and int(item.get("attrib_v", 0) or 0) < _ATTRIB_VERSION:
        if any(_needs_attrib(f) for f in files):
            slots = episodes.attribute_paths([f.get("path", "") for f in files])
            for f, slot in zip(files, slots):
                if _needs_attrib(f):
                    episodes.apply_slot(f, slot)
            files.sort(key=episodes.sort_key)
        if episodes.reread_spinoffs(files):
            files.sort(key=episodes.sort_key)
        item["attrib_v"] = _ATTRIB_VERSION
    # 20.24.1: a film's story number was once read off "Part N" / "Chapter N",
    # which put Mockingjay Part 1 and 2 first on The Hunger Games shelf. Clear a
    # number that can only have come from there; a refetch would set it to 0 too,
    # but nothing refetches a film's metadata on its own. Idempotent: a cleared
    # number is 0 and skipped. See _story_index.
    meta = item.get("metadata")
    if isinstance(meta, dict) and meta.get("tmdb_kind") == "movie" \
            and int(meta.get("story_index") or 0):
        titles = [meta.get("title") or "", meta.get("original_title") or ""]
        if any(_SPLIT_TITLE_RE.search(t) for t in titles) \
                and not _story_index(*titles, *(meta.get("aka") or [])):
            meta["story_index"] = 0
    return item


# ── Library durability ──────────────────────────────────────────────────────
#
# 2026-09-16 the box was hard-reset seconds after a progress save and came back
# with library.json at its full 1.5 MB size and every byte zero — every profile
# and item gone. temp-file + os.replace is only crash-safe for the *process*: the
# rename is journaled metadata and reaches disk at once, while the new file's
# data sits in the OS cache for seconds. Power loss in that window commits a
# rename pointing at never-written clusters. Three layers, so it can't recur:
#
#   1. fsync the temp file before the replace (`_write_durable`).
#   2. Rolling snapshots in library_backups/ (`_maybe_snapshot_library`).
#   3. A library that won't parse is never read as empty: it is moved aside to
#      library.corrupt-<ts>.json and the newest parseable snapshot restored
#      (`_recover_library`). Before, the silent empty fallback meant the next
#      save overwrote the damaged file — destroying the evidence and any chance
#      of carving it back off the disk.
LIBRARY_BACKUP_DIR = LIBRARY_FILE.parent / "library_backups"
_BACKUP_INTERVAL_S = 15 * 60      # at most one snapshot per 15 min of writes
_BACKUP_KEEP_RECENT = 16          # newest 16 kept regardless (~4 h of activity)
_BACKUP_KEEP_DAILY = 30           # older: newest snapshot per day, 30 days
_last_library_snapshot = 0.0
_lib_recover_lock = threading.Lock()


def _parse_library_bytes(blob: bytes) -> Optional[dict]:
    """The library dict, or None if `blob` isn't a well-formed library."""
    try:
        raw = json.loads(blob.decode("utf-8"))
    except Exception:
        return None
    if (not isinstance(raw, dict)
            or not isinstance(raw.get("profiles", []), list)
            or not isinstance(raw.get("items", []), list)):
        return None
    return raw


def _write_durable(path: Path, blob: bytes, steps: Optional[dict] = None) -> None:
    """Write `blob` to `path` so a power cut leaves the old or the new file, never zeros.

    Temp file in the same directory (same volume ⇒ atomic replace on NTFS and
    POSIX), flushed *and fsynced* before the replace. PermissionError propagates
    so the caller's retry loop can ride out Windows scanner locks.

    `steps` (optional) accumulates seconds per step: open / write / fsync / close
    / replace. `close` is its own step because a Windows scanner inspects a file
    when its handle closes (#96).
    """
    tmp = path.with_name(path.name + ".tmp")
    clock = time.monotonic
    t0 = clock()
    fh = open(tmp, "wb")
    try:
        t1 = clock()
        fh.write(blob)
        fh.flush()
        t2 = clock()
        os.fsync(fh.fileno())
        t3 = clock()
    finally:
        fh.close()
    t4 = clock()
    os.replace(tmp, path)
    t5 = clock()
    if steps is not None:
        for k, v in (("open", t1 - t0), ("write", t2 - t1), ("fsync", t3 - t2),
                     ("close", t4 - t3), ("replace", t5 - t4)):
            steps[k] = steps.get(k, 0.0) + v


def _library_snapshots() -> list[Path]:
    """Snapshot files, oldest first (names embed a sortable timestamp)."""
    try:
        return sorted(LIBRARY_BACKUP_DIR.glob("library-*.json"))
    except OSError:
        return []


def _maybe_snapshot_library(data: dict, blob: bytes) -> None:
    """Copy a just-saved library into library_backups/, rate-limited, then prune.

    Never raises: a failed snapshot must not fail the save it follows.
    """
    global _last_library_snapshot
    now = time.time()
    if now - _last_library_snapshot < _BACKUP_INTERVAL_S:
        return
    # An empty library isn't worth a slot — snapshotting one repeatedly would
    # rotate the real history out of the recent window.
    if not data.get("profiles") and not data.get("items"):
        return
    try:
        LIBRARY_BACKUP_DIR.mkdir(exist_ok=True)
        name = "library-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".json"
        _write_durable(LIBRARY_BACKUP_DIR / name, blob)
        _last_library_snapshot = now
    except OSError as exc:
        log.warning("library snapshot failed: %s", exc)
        return
    snaps = _library_snapshots()
    keep = set(snaps[-_BACKUP_KEEP_RECENT:])
    days: set[str] = set()
    for p in reversed(snaps[:-_BACKUP_KEEP_RECENT]):
        day = p.name[len("library-"):len("library-") + 8]
        if day not in days and len(days) < _BACKUP_KEEP_DAILY:
            days.add(day)
            keep.add(p)
    for p in snaps:
        if p not in keep:
            try:
                p.unlink()
            except OSError:
                pass


def _recover_library() -> Optional[dict]:
    """library.json exists but won't parse: quarantine it and restore a snapshot.

    Returns the restored library, or None when no snapshot parses (the caller
    then starts empty — but the damaged file is kept under its corrupt- name,
    so the empty library's first save can't destroy it).
    """
    with _lib_recover_lock:
        # A concurrent reader may have recovered it while this one waited.
        try:
            cur = _parse_library_bytes(LIBRARY_FILE.read_bytes())
        except FileNotFoundError:
            cur = None
        if cur is not None:
            return cur
        if LIBRARY_FILE.exists():
            aside = LIBRARY_FILE.with_name(
                "library.corrupt-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".json")
            os.replace(LIBRARY_FILE, aside)
            log.critical("library.json is unreadable (corrupt or zero-filled) — moved "
                         "aside to %s", aside.name)
        for snap in reversed(_library_snapshots()):
            try:
                blob = snap.read_bytes()
            except OSError:
                continue
            raw = _parse_library_bytes(blob)
            if raw is None:
                continue
            _write_durable(LIBRARY_FILE, blob)
            log.critical("library.json restored from snapshot %s (%d profiles, %d items) — "
                         "changes made after that snapshot are lost",
                         snap.name, len(raw.get("profiles", [])), len(raw.get("items", [])))
            return raw
        log.critical("library.json unreadable and no usable snapshot in %s — starting "
                     "with an EMPTY library", LIBRARY_BACKUP_DIR)
        return None


def _load_lib_raw() -> dict:
    t0 = time.monotonic()
    blob = b""
    for attempt in range(len(_SAVE_RETRY_DELAYS) + 1):
        try:
            blob = LIBRARY_FILE.read_bytes()
            break
        except FileNotFoundError:
            return {"profiles": [], "items": []}      # fresh install
        except OSError as exc:
            # A scanner lock is transient. If it persists, raise rather than
            # return empty: an empty result inside mutate_library() gets SAVED.
            if attempt == len(_SAVE_RETRY_DELAYS):
                raise
            if not attempt:
                log.info("library.json read retrying: %s%s", _lock_failure_desc(exc),
                         _lock_holders_note())
            time.sleep(_SAVE_RETRY_DELAYS[attempt])
    t_read = time.monotonic()
    raw = _parse_library_bytes(blob)
    if raw is None:
        raw = _recover_library()
        if raw is None:
            return {"profiles": [], "items": []}
    t_parse = time.monotonic()
    raw["items"] = [_migrate_item(it) for it in raw.get("items", [])]
    _fold_item_hides(raw)
    t_end = time.monotonic()
    _note_slow_lib_io("load", t_end - t0,
                      {"read": t_read - t0, "parse": t_parse - t_read,
                       "migrate": t_end - t_parse}, len(blob))
    return raw


def _fold_item_hides(raw: dict) -> None:
    """Pre-18.22 hiding was per ITEM (`hidden_by_profiles`); it is per SERIES now
    (`profile.hidden_series`). Fold any per-item hide into its series — one hidden
    episode hides the show, which is what the old UI's fan-out meant anyway — and
    clear the item flag so this is a no-op from the next load on."""
    profs = {p.get("id"): p for p in raw.get("profiles", [])}
    for it in raw.get("items", []):
        hb = it.get("hidden_by_profiles")
        if not hb:
            continue
        key = _series_key(it)
        for pid in hb:
            p = profs.get(pid)
            if p is not None:
                hs = p.setdefault("hidden_series", [])
                if key not in hs:
                    hs.append(key)
        it["hidden_by_profiles"] = []


# A transient Windows file-lock costs one library write. Retry rather than lose it.
#
# `os.replace` onto an open file fails on Windows with ERROR_ACCESS_DENIED (5) or
# ERROR_SHARING_VIOLATION (32) whenever *anything* holds a handle without
# FILE_SHARE_DELETE — Defender scanning the file we just wrote, the Search
# indexer, a backup/sync agent. It clears in milliseconds. POSIX rename has no
# such failure mode, which is exactly why this went unnoticed on the dev Mac.
#
# The logs say it is not rare: **584** library writes died this way, 564 of them
# `update_progress` — one silently discarded "where I was in this episode"
# apiece, every one of which reads to the viewer as the app forgetting their
# place. library.json is rewritten every ~15 s during playback, so the hot path
# is also the exposed one.
_SAVE_RETRY_DELAYS = (0.05, 0.12, 0.25, 0.5, 1.0)   # ~1.9 s total, then give up
# Asking Windows who holds the file costs a few ms on a write that is already
# retrying; once a minute is plenty to name the culprit (#22).
_LOCK_HOLDER_EVERY_S = 60.0
_lock_holder_last = 0.0


def _lock_failure_desc(exc: OSError) -> str:
    """`[WinError 32] replace library.json.tmp -> library.json` — which step lost.
    `open` failing names only the .tmp; `os.replace` names both."""
    code = getattr(exc, "winerror", None) or exc.errno
    a = Path(exc.filename).name if exc.filename else "?"
    b = Path(exc.filename2).name if getattr(exc, "filename2", None) else ""
    step = ("replace %s -> %s" % (a, b)) if b else ("write %s" % a)
    return "[%s %s] %s" % ("WinError" if getattr(exc, "winerror", None) else "errno", code, step)


def _lock_holders_note() -> str:
    """` held by: MsMpEng.exe pid=4120` at most once a minute; '' otherwise."""
    global _lock_holder_last
    now = time.monotonic()
    if now - _lock_holder_last < _LOCK_HOLDER_EVERY_S:
        return ""
    _lock_holder_last = now
    who = diag.file_holders([LIBRARY_FILE, LIBRARY_FILE.with_name(LIBRARY_FILE.name + ".tmp")])
    return (" held by: " + ", ".join(who)) if who else " held by: (nobody by then)"


# A library.json read or write that takes this long logs where the time went
# (#96). On an idle disk both take milliseconds. On 2026-10-18 the box's progress
# saves held the library lock 5-17 s each while F: was busy, and nothing said
# whether it was the write, the fsync, the replace or the snapshot.
_LIB_IO_SLOW_S = 1.0


def _steps_note(steps: dict) -> str:
    """`dumps 0.08s, fsync 6.81s, …` in the order the steps ran."""
    return ", ".join("%s %.2fs" % (k, v) for k, v in steps.items())


def _note_slow_lib_io(op: str, total: float, steps: dict, size: int) -> None:
    if total >= _LIB_IO_SLOW_S:
        log.warning("library.json %s took %.2fs (%.1f MB): %s",
                    op, total, size / 1e6, _steps_note(steps))


def _save_lib_raw(data: dict) -> None:
    # Durable atomic write — see "Library durability" above. The replace alone
    # survives a process crash; the fsync in _write_durable is what survives a
    # power cut. library.json is rewritten every ~15 s during playback.
    t0 = time.monotonic()
    blob = json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")
    steps: dict = {"dumps": time.monotonic() - t0}
    last: Optional[OSError] = None
    fails: list[str] = []
    for attempt in range(len(_SAVE_RETRY_DELAYS) + 1):
        try:
            # The temp write is retried too: the previous tick's .tmp can still be
            # held by the same scanner, and failing there loses the write just as
            # completely as failing on the replace.
            _write_durable(LIBRARY_FILE, blob, steps)
            if attempt:
                # What failed and who held it, so #22 can name the holder instead
                # of counting retries.
                log.info("library.json write succeeded on attempt %d after %.2fs; %s",
                         attempt + 1, time.monotonic() - t0, "; ".join(fails))
            t_snap = time.monotonic()
            _maybe_snapshot_library(data, blob)
            steps["snapshot"] = time.monotonic() - t_snap
            _note_slow_lib_io("save", time.monotonic() - t0, steps, len(blob))
            return
        except PermissionError as exc:      # WinError 5 / 32 both surface here
            last = exc
            note = _lock_failure_desc(exc)
            if not fails:
                note += _lock_holders_note()
            fails.append(note)
        except OSError as exc:
            # Anything else (disk full, path gone) is not a lock; don't spin on it.
            raise
        if attempt < len(_SAVE_RETRY_DELAYS):
            t_wait = time.monotonic()
            time.sleep(_SAVE_RETRY_DELAYS[attempt])
            steps["retry_wait"] = steps.get("retry_wait", 0.0) + time.monotonic() - t_wait
    # Out of retries. Raise — the caller's 500 is the honest answer, and the
    # client now treats it as a failed save rather than a successful one.
    log.error("library.json write failed after %d attempts (%.2fs): %s [%s]",
              len(_SAVE_RETRY_DELAYS) + 1, time.monotonic() - t0, "; ".join(fails),
              _steps_note(steps))
    raise last if last is not None else OSError("library.json write failed")


async def get_library() -> dict:
    # The read + json.loads + _migrate_item-over-every-item is O(library size)
    # and was previously run inline on the event loop. Hot loops call this every
    # 2-5 s (progress tracker, download monitor) and it grows after every
    # download (skip_data fingerprints), so a large library.json stalled the
    # whole loop — the dashboard went laggy and the HTTPS proxy threw ReadErrors
    # mid-request while the loop was blocked. Run the blocking work in a thread;
    # the lock still serialises access, but the loop stays free.
    caller = sys._getframe(1).f_code.co_name
    async with runtime._lib_lock:
        runtime._lib_lock.set_holder(f"{caller} [get_library]")
        return await asyncio.to_thread(_load_lib_raw)


async def put_library(data: dict) -> None:
    caller = sys._getframe(1).f_code.co_name
    async with runtime._lib_lock:
        runtime._lib_lock.set_holder(f"{caller} [put_library]")
        await asyncio.to_thread(_save_lib_raw, data)


class LibraryUnchanged(Exception):
    """Raise inside `mutate_library()` to leave the transaction without writing.

    Hot callers (the 15 s progress saver, the 5 s download monitor) open a
    transaction and only then discover there's nothing to store — a stale tick, a
    mid-transition VLC read, an item that vanished. A plain `return`/`continue`
    exits the block cleanly, which *commits*; rewriting a multi-megabyte
    library.json on every no-op tick is pure IO. This aborts the write instead.
    It is swallowed by the context manager, so it never escapes to the caller.
    """


def mutate_library():
    """Read → mutate → write library.json as ONE atomic transaction.

    `get_library()` and `put_library()` each take `_lib_lock`, but neither holds
    it across the caller's mutation — so the near-universal

        lib = await get_library()
        ...mutate...
        await put_library(lib)

    is a lost-update race: anything that reads between the two calls writes a
    snapshot that predates this mutation and silently clobbers it. Both writers
    return success, so nothing surfaces the loss. Observed live: three concurrent
    writes to three DIFFERENT profiles landed one change and dropped two, and six
    concurrent identical downloads defeated the info-hash dedup guard (six items
    minted from one torrent) and then lost five of them on write-back, stranding
    a hash-less item stuck in `downloading` forever.

    Background writers make this routine rather than exotic: the progress tracker
    saves every 15 s and the download monitor every 5 s, so any user action has a
    standing chance of landing inside someone else's read-modify-write window.

    Use this for every mutation. `get_library()` stays correct for read-only
    callers; `put_library()` remains for the few places that build a library dict
    from scratch rather than editing the current one.

        async with mutate_library() as lib:
            item["status"] = "ready"

    The write happens on clean exit only — any exception aborts with nothing
    persisted, so an HTTPException validation guard inside the block behaves
    exactly as it did before. `raise LibraryUnchanged` is the explicit no-write
    exit for "turns out there was nothing to store" (it's swallowed here).

    Note a plain `return` inside the block COMMITS — the block ran to completion
    as far as the context manager is concerned. That's harmless for the common
    "bail before mutating" guard (it rewrites what it just read) but means an
    early return can no longer be used to discard a mutation already made; raise
    LibraryUnchanged for that.

    Not reentrant: `_lib_lock` is a plain asyncio.Lock, so never call
    get_library()/put_library()/mutate_library() — or any helper that does, such
    as `_all_library_paths()` — inside the block. Keep network and disk IO out of
    it too: this is the single global library lock, and holding it across a qBit
    round trip or an N-file delete stalls every other reader and writer.

    The caller's name labels the lock while it is held, with the step it is on
    (`update_progress [save]`), and a transaction that holds the lock for
    `_LIB_IO_SLOW_S` or more logs how the time split (#96).
    """
    return _library_txn(sys._getframe(1).f_code.co_name)


@asynccontextmanager
async def _library_txn(caller: str):
    clock = time.monotonic
    t_ask = clock()
    async with runtime._lib_lock:
        t_in = clock()
        t_loaded = t_body = 0.0
        outcome = "aborted"
        try:
            runtime._lib_lock.set_holder(f"{caller} [load]")
            lib = await asyncio.to_thread(_load_lib_raw)
            t_loaded = clock()
            runtime._lib_lock.set_holder(f"{caller} [body]")
            try:
                yield lib
            except LibraryUnchanged:
                outcome = "unchanged"
                return
            t_body = clock()
            runtime._lib_lock.set_holder(f"{caller} [save]")
            await asyncio.to_thread(_save_lib_raw, lib)
            outcome = "saved"
        finally:
            _note_slow_txn(caller, outcome, t_ask, t_in, t_loaded, t_body, clock())


def _note_slow_txn(caller: str, outcome: str, t_ask: float, t_in: float,
                   t_loaded: float, t_body: float, t_end: float) -> None:
    """One line when a transaction held the library lock `_LIB_IO_SLOW_S` or more.

    load and save are measured around the `to_thread` call, so they include any
    wait for a free worker; the `library.json load/save took` line (logged when
    the I/O itself is slow) is the time on the thread. A big gap between the two
    is a busy thread pool, not a slow disk.
    """
    held = t_end - t_in
    if held < _LIB_IO_SLOW_S:
        return
    parts = []
    if t_loaded:
        parts.append("load %.2fs" % (t_loaded - t_in))
        end_body = t_body or t_end
        parts.append("body %.2fs" % (end_body - t_loaded))
        if t_body:
            parts.append("save %.2fs" % (t_end - t_body))
    else:
        parts.append("load %.2fs" % held)
    log.warning("library txn by %s held the lock %.2fs (%s; waited %.2fs for it): %s",
                caller, held, outcome, t_in - t_ask, ", ".join(parts))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_iso_dt(s: Optional[str]) -> Optional[datetime]:
    """Tolerant ISO-8601 → aware datetime (UTC). Accepts a trailing 'Z' and
    naive strings (assumed UTC). Returns None if unparseable. Used by the iOS
    progress-sync conflict logic to compare server `updated_at` against the
    device's `base_synced_at` / `client_updated_at` watermarks."""
    if not s:
        return None
    try:
        t = s.strip()
        if t.endswith("Z"):
            t = t[:-1] + "+00:00"
        dt = datetime.fromisoformat(t)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _series_key(item: dict) -> str:
    """Group items by series for cross-episode fingerprinting.

    Items with a non-empty series field group together; everything else is
    grouped by item ID (a single-item bucket) so movies and one-offs still
    get the credits fallback.
    """
    s = (item.get("series") or "").strip()
    return f"series:{s.lower()}" if s else f"item:{item.get('id', '')}"
