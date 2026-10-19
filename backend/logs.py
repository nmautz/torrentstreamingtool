"""Logging: the rotating app log, the HLS log, client-log storage, diagnostics.

Importing this module sets logging up (creates logs/, attaches the handlers,
starts diagnostics and the faulthandler), exactly as main.py's Logging section
did. main.py imports it in the backend/ block under `winaccept_patch.apply()`,
which is where that section used to run. Loggers are named by string
("streamlink", "streamlink.hls"), never by `__name__`.
"""
import logging
import os
import sys
import time
import zipfile
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

import clientlog
import diagnostics as diag
import updater

from backend.paths import ROOT
from backend.version import UI_VERSION


# ── Logging ─────────────────────────────────────────────────────────────────
# main.py historically had no logging: errors raised inside background tasks —
# most painfully the HLS offline-prep ffmpeg pipeline — vanished into a
# truncated job["error"] field with nothing written to disk, so a conversion
# that died seconds after starting left no diagnosable trail. This wires up a
# rotating file logger. HLS gets a child logger with its own dedicated file
# (logs/hls.log) because that pipeline is the most failure-prone and the one
# operators most often need to debug; everything also propagates to the shared
# app log + stderr (captured by launchd/the console into logs/streamlink.err).
LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

# Diagnostic transcripts uploaded by client devices (the iOS app). A SUBDIRECTORY
# rather than `client_<device>.log` beside the server's own logs, and that is the
# whole point: every sweep in this file — `_archive_old_logs()` at update time,
# `DELETE /api/admin/logs`, the `_bundle` download — iterates top-level FILES, so
# a directory is invisible to all of them for free.
#
# That invisibility is the retention policy. A client log is evidence someone is
# expected to come back and read, possibly days later; it must not be collected
# by a routine that exists to give the *server's* logs a clean slate after an
# update. The only thing that removes one is a reader saying so, through
# `DELETE /api/admin/client-logs`.
CLIENT_LOG_DIR = LOG_DIR / "client"
CLIENT_LOG_DIR.mkdir(parents=True, exist_ok=True)
# The whole upload (at most 8 MB) must arrive within this; see diag_client_log.
CLIENT_LOG_BODY_TIMEOUT = 120.0

# Devices whose stored transcript a reader has cleared. The server cannot reach a
# phone, so the clear is finished by the NEXT upload: that response carries
# `clear_local`, the app deletes its own copy, and the two sides agree the old
# rows are gone. Without this the device would keep re-sending a log the reader
# has already dealt with, and the merge would faithfully restore every row.
_CLIENT_LOG_PENDING = CLIENT_LOG_DIR / ".pending_clear.json"


def _migrate_client_logs() -> None:
    """Move pre-18.8.0 `logs/client_<device>.log` into `logs/client/<device>.log`.

    Runs once, at import, before `_init_logging()` can archive them away. The
    logs that exist right now are the only record of what the app did on the
    days before this feature grew retention — losing them to the very change
    meant to stop losing them would be a poor joke.
    """
    try:
        strays = [p for p in LOG_DIR.glob("client_*.log") if p.is_file()]
    except OSError:
        return
    for src in strays:
        dst = CLIENT_LOG_DIR / (src.name[len("client_"):])
        try:
            incoming = src.read_text(encoding="utf-8", errors="replace")
            existing = dst.read_text(encoding="utf-8") if dst.is_file() else ""
            merged, _, _ = clientlog.merge(existing, incoming)
            dst.write_text(merged, encoding="utf-8")
            src.unlink()
        except OSError:
            continue


_migrate_client_logs()


# When the auto-updater applies a new version it drops this marker in LOG_DIR.
# `_init_logging()` consumes it on the next process start — BEFORE any handler
# opens a log file — to roll the previous version's logs into a timestamped zip
# and start the new version with empty logs. Done at startup (not inside the
# update flow) precisely because the running process holds open handles on
# streamlink_app.log / hls.log; on Windows you can't archive + clear a file
# while it's held open, so we defer to the clean post-restart state.
_ROTATE_MARKER = LOG_DIR / ".rotate_pending"


def _archive_old_logs() -> Optional[Path]:
    """Zip every current log file in LOG_DIR into `logs_old_<timestamp>.zip`,
    then clear the originals. Returns the archive path (if anything was
    archived) or None.

    Best-effort and platform-aware: *reading* an open file to add it to the zip
    is safe everywhere, so the archive always captures the prior run's logs.
    *Clearing* the original is allowed to fail — on Windows another live process
    (the service-wrapper that owns streamlink_service.log) may still hold the
    file open, in which case it's left in place (already safe in the zip). Past
    archives (`logs_old_*.zip`) and dotfiles are never re-archived.
    """
    try:
        candidates = [
            p for p in LOG_DIR.iterdir()
            if p.is_file()
            and not p.name.startswith("logs_old_")
            and not p.name.startswith(".")
        ]
    except OSError:
        return None
    if not candidates:
        return None

    archive = LOG_DIR / f"logs_old_{time.strftime('%Y%m%d-%H%M%S')}.zip"
    archived = 0
    try:
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in candidates:
                try:
                    zf.write(p, arcname=p.name)
                    archived += 1
                except OSError:
                    continue
    except OSError:
        return None
    if archived == 0:
        try:
            archive.unlink()
        except OSError:
            pass
        return None

    # Clear the originals now they're safely in the zip: remove if we can, else
    # truncate, else give up on that one file (it stays, but it's archived).
    for p in candidates:
        try:
            p.unlink()
        except OSError:
            try:
                with open(p, "w", encoding="utf-8"):
                    pass
            except OSError:
                pass
    return archive


def _init_logging() -> logging.Logger:
    root = logging.getLogger("streamlink")
    if root.handlers:                    # idempotent across uvicorn reloads
        return root
    root.setLevel(logging.INFO)
    root.propagate = False               # don't double-log via the Python root

    # Roll prior logs into a zip if the updater flagged a version change. This
    # runs before any handler opens a file, so there's no open-handle conflict.
    rotated_to: Optional[Path] = None
    if _ROTATE_MARKER.exists():
        rotated_to = _archive_old_logs()
        try:
            _ROTATE_MARKER.unlink()
        except OSError:
            pass

    fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-7s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    app_fh = RotatingFileHandler(
        LOG_DIR / "streamlink_app.log", maxBytes=2_000_000, backupCount=3,
        encoding="utf-8",
    )
    app_fh.setFormatter(fmt)
    root.addHandler(app_fh)

    # Mirror to stderr (→ console / logs/streamlink.err) but only WARNING+ so a
    # bulk /prep-all doesn't bury the interactive console in per-job INFO lines;
    # the full INFO trail still lands in the rotating files.
    stderr_h = logging.StreamHandler(sys.stderr)
    stderr_h.setLevel(logging.WARNING)
    stderr_h.setFormatter(fmt)
    root.addHandler(stderr_h)

    # Dedicated HLS file. The child propagates to `root`, so HLS lines also
    # reach the app log + stderr — this handler just keeps a focused copy.
    hls = logging.getLogger("streamlink.hls")
    hls_fh = RotatingFileHandler(
        LOG_DIR / "hls.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8",
    )
    hls_fh.setFormatter(fmt)
    hls.addHandler(hls_fh)

    if rotated_to is not None:
        root.info("Previous version's logs archived to %s", rotated_to.name)
    return root


log     = _init_logging()
hls_log = logging.getLogger("streamlink.hls")

# Diagnostics: dedicated access/vitals files, quieter httpx, and last-resort
# exception capture. Wired immediately after the app logger exists so anything
# that blows up during the rest of import is still recorded somewhere useful.
# See docs/DIAGNOSTICS.md for what each piece is for and which blind spot from
# the 2026-09-13 outage it closes.
# The port this app is served on. run.py owns the real value and exports it
# (`STREAMLINK_HTTP_PORT`); 80 is the long-standing default and matches the
# portless `http://127.0.0.1/...` URLs used elsewhere in this file.
HTTP_PORT = int(os.environ.get("STREAMLINK_HTTP_PORT", "80") or 80)

diag.init(LOG_DIR)
# Attach the access log here rather than trusting the launcher's uvicorn
# log config: main.py is imported fresh on every boot, whereas a stale
# streamlink_service.py can silently leave the box with no access log.
diag.attach_access_log(LOG_DIR)
diag.quiet_noisy_loggers()
diag.install_exception_hooks()
diag.enable_faulthandler(LOG_DIR)


async def _log_version_banner() -> None:
    """Log the running version + git checkout once at startup, so every log
    (and every post-update rotation) carries a clear marker of which build
    produced the lines that follow it."""
    branch = commit = ""
    try:
        branch = await updater.current_branch()
        commit = await updater.current_commit()
    except Exception:
        pass
    log.info("StreamLink v%s starting — branch=%s commit=%s",
             UI_VERSION, branch or "?", commit or "?")
