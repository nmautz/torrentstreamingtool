"""P2P StreamLink — FastAPI backend"""

import asyncio
import base64
import gzip
import hashlib
import io
import json
import logging
import math
import os
import platform
import re
import secrets
import shlex
import shutil
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import uuid
import zipfile
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import AsyncGenerator, Optional
from urllib.parse import quote, unquote, urlparse

import httpx
import psutil
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

import analyzer
import stt
import updater


# ── Logging ─────────────────────────────────────────────────────────────────
# main.py historically had no logging: errors raised inside background tasks —
# most painfully the HLS offline-prep ffmpeg pipeline — vanished into a
# truncated job["error"] field with nothing written to disk, so a conversion
# that died seconds after starting left no diagnosable trail. This wires up a
# rotating file logger. HLS gets a child logger with its own dedicated file
# (logs/hls.log) because that pipeline is the most failure-prone and the one
# operators most often need to debug; everything also propagates to the shared
# app log + stderr (captured by launchd/the console into logs/streamlink.err).
LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)


def _init_logging() -> logging.Logger:
    root = logging.getLogger("streamlink")
    if root.handlers:                    # idempotent across uvicorn reloads
        return root
    root.setLevel(logging.INFO)
    root.propagate = False               # don't double-log via the Python root

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
    return root


log     = _init_logging()
hls_log = logging.getLogger("streamlink.hls")


# ── Settings ──────────────────────────────────────────────────────────────────

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    indexer_url: str = "http://localhost:9117"
    indexer_api_key: str = ""
    indexer_categories: str = "0"

    qbit_url: str = "http://localhost:8081"
    qbit_username: str = "admin"
    qbit_password: str = "adminadmin"
    qbit_download_path: str = str(Path.home() / "Downloads" / "StreamLink")
    library_path_2: str = ""   # optional extra storage locations (LIBRARY_PATH_2 in .env)
    library_path_3: str = ""
    library_path_4: str = ""

    vlc_url: str = "http://localhost:8080"
    vlc_password: str = "vlcpassword"

    buffer_min_mb: float = 15.0
    buffer_min_pct: float = 1.0

    admin_password: str = ""   # if empty, admin panel is disabled
    jackett_password: str = ""  # Jackett UI login password for indexer management

    # OpenSubtitles legacy REST API (rest.opensubtitles.org) needs no key, only a
    # User-Agent. "TemporaryUserAgent" is OpenSubtitles' documented testing UA.
    opensubtitles_user_agent: str = "TemporaryUserAgent"

    # TMDb v3 API. Empty → metadata features are disabled (falls back to filename
    # parsing on the client). Key can also be set live from the admin panel and
    # is persisted in library.json → settings.admin_overrides.tmdb_api_key.
    tmdb_api_key: str = ""


settings = Settings()


# ── Env-key feature registry ──────────────────────────────────────────────────
# Each entry declares an env key that controls a user-visible feature. The
# autoupdater surfaces any key in this list that the running .env doesn't set,
# so the admin can fill it in from the dashboard after an update introduces a
# new requirement — without the dashboard becoming unusable in the meantime.
#
# Fields:
#   key         — the .env key name
#   attr        — the Settings model attribute (lowercase)
#   label       — short feature name shown in the admin UI
#   description — one-line "what does this gate" (shown in the admin form)
#   required    — True = the dashboard banner shows for everyone (blocks UX);
#                 False = optional, only surfaces in the admin Updates tab.
#                 The non-admin UI never *hides* features wholesale — admins
#                 just see a banner so they know to wire the key up.
#   secret      — passed to the admin form so the input is rendered as
#                 type="password" instead of "text".

ENV_KEY_FEATURES: list[dict] = [
    {
        "key": "ADMIN_PASSWORD",
        "attr": "admin_password",
        "label": "Admin panel",
        "description": "Password gating /admin. Without it the admin dashboard is disabled.",
        "required": True,
        "secret": True,
    },
    {
        "key": "INDEXER_API_KEY",
        "attr": "indexer_api_key",
        "label": "Torrent search",
        "description": "Jackett API key. Without it /api/search returns no results.",
        "required": True,
        "secret": True,
    },
    {
        "key": "JACKETT_PASSWORD",
        "attr": "jackett_password",
        "label": "Indexer management",
        "description": "Jackett UI admin password. Only needed for the admin Indexers tab.",
        "required": False,
        "secret": True,
    },
    {
        "key": "TMDB_API_KEY",
        "attr": "tmdb_api_key",
        "label": "Episode metadata",
        "description": "Optional TMDb v3 API key for backdrops/posters/episode titles.",
        "required": False,
        "secret": True,
    },
]


def _missing_env_keys() -> list[dict]:
    """Return the registry entries whose Settings attribute is empty.

    `tmdb_api_key` may be set live via the admin overrides — when that's the
    case, treat the key as present so the UI doesn't nag for one that's
    already configured a different way.
    """
    out: list[dict] = []
    for feat in ENV_KEY_FEATURES:
        val = getattr(settings, feat["attr"], "") or ""
        if not val and feat["attr"] == "tmdb_api_key":
            # Honour the admin override (set via /api/admin/settings) — the
            # library file is the runtime source of truth for that key.
            try:
                lib_raw = _load_lib_raw()
                ov = (lib_raw.get("settings", {}) or {}).get("admin_overrides", {}) or {}
                if ov.get("tmdb_api_key"):
                    continue
            except Exception:
                pass
        if val:
            continue
        out.append({
            "key":         feat["key"],
            "label":       feat["label"],
            "description": feat["description"],
            "required":    feat["required"],
            "secret":      feat["secret"],
        })
    return out


def _write_env_keys(updates: dict[str, str]) -> int:
    """Merge `updates` into the .env file, preserving comments and ordering.

    Existing keys are rewritten in place; unknown keys are appended at the end.
    Empty-string values clear the entry rather than leaving `KEY=` behind.
    Returns the count of keys actually written/changed.
    """
    env_path = Path(__file__).parent / ".env"
    lines: list[str] = []
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8", errors="replace").splitlines()

    remaining = dict(updates)
    out: list[str] = []
    changed = 0
    for raw in lines:
        stripped = raw.strip()
        if "=" in stripped and not stripped.startswith("#"):
            k = stripped.split("=", 1)[0].strip()
            if k in remaining:
                new_val = remaining.pop(k)
                if new_val == "":
                    # Drop the line entirely so Settings reverts to its default.
                    changed += 1
                    continue
                if raw != f"{k}={new_val}":
                    changed += 1
                out.append(f"{k}={new_val}")
                continue
        out.append(raw)

    for k, v in remaining.items():
        if not v:
            continue
        out.append(f"{k}={v}")
        changed += 1

    if changed == 0:
        return 0

    env_path.write_text("\n".join(out) + ("\n" if out and not out[-1].endswith("\n") else ""),
                        encoding="utf-8")
    return changed


def _reload_settings() -> None:
    """Re-instantiate Settings so newly-written .env values take effect without
    a full server restart. Most code references `settings.foo` via the module
    global, so rebinding here propagates to all callers."""
    global settings
    settings = Settings()


LIBRARY_FILE = Path(__file__).parent / "library.json"
BACKGROUND_DIR = Path(__file__).parent / ".background"
# Dedicated Chrome user-data-dir for the YouTube-on-TV kiosk window. Isolated so
# launching/killing it never touches the user's normal Chrome, and so we can
# identify *just* the kiosk instance to kill on Stop (match this path in cmdline).
TV_CHROME_PROFILE = Path(__file__).parent / ".tv_chrome_profile"

# Countdown popup shown on the TV itself. VLC is launched with a `marq`
# sub-source that re-reads this file ~5×/s and renders its contents bottom-right.
# main.py writes the "Skipping … in N" text here; emptying the file clears it.
# run.py and watchdog.py launch VLC with --marq-file pointed at this same path —
# keep _vlc_marquee_args() (below) in sync with their arg lists.
MARQUEE_FILE = Path(__file__).parent / ".vlc_marquee.txt"


def _vlc_marquee_args() -> list:
    """VLC CLI args enabling the bottom-right countdown marquee.

    Mirrored verbatim in run.py (start_vlc) and watchdog.py (vlc_spec) — the
    three launch paths must agree. Text-only (no background box) so regular
    subtitles render unchanged; readability comes from VLC's default outline.
    """
    return [
        "--sub-source=marq",
        f"--marq-file={MARQUEE_FILE}",
        "--marq-refresh=200",      # re-read the file ~5×/s → smooth countdown
        "--marq-position=10",      # 10 = Bottom-Right
        "--marq-x=50", "--marq-y=50",   # padding in from the corner
        "--marq-size=48",
        "--marq-color=16777215",   # white
        "--marq-opacity=255",      # fully opaque text
        "--marq-timeout=0",        # persist until the file is emptied
    ]


# ── Night mode (dynamic-range compression) ──────────────────────────────────
# "Night mode" narrows the gap between the quietest and loudest sounds so quiet
# dialogue stays audible without explosions being jarring at low room volume.
# It's VLC's `compressor` audio filter — there is no runtime HTTP command to add
# an audio filter, so the filter is set at launch and toggling it restarts VLC
# (see _apply_night_mode + _restart_vlc_process).
#
# Three intensity presets (user-selectable, settings menu only). The chosen
# preset persists independently of the on/off toggle, so turning night mode off
# and back on reuses the same intensity. The presets dict below is mirrored
# verbatim in run.py (start_vlc) and watchdog.py (vlc_spec); change one, change
# all three (same contract as _vlc_marquee_args).
#
# Tuning (all dB / ms): a lower threshold + higher ratio pull loud peaks down
# harder, a higher makeup gain lifts the now-quieter dialogue back up, and an RMS
# (not peak) detector with a slow-ish release keeps it from "pumping" on speech.
# light  = gentle leveling; medium = balanced default; max = strongest.
NIGHT_MODE_PRESETS = {
    "light": [
        "--audio-filter=compressor",
        "--compressor-rms-peak=0.00",
        "--compressor-attack=25.0",
        "--compressor-release=250.0",
        "--compressor-threshold=-20.0",
        "--compressor-ratio=3.0",
        "--compressor-knee=5.0",
        "--compressor-makeup-gain=6.0",
    ],
    "medium": [
        "--audio-filter=compressor",
        "--compressor-rms-peak=0.00",
        "--compressor-attack=25.0",
        "--compressor-release=250.0",
        "--compressor-threshold=-24.0",
        "--compressor-ratio=6.0",
        "--compressor-knee=3.0",
        "--compressor-makeup-gain=10.0",
    ],
    "max": [
        "--audio-filter=compressor",
        "--compressor-rms-peak=0.00",
        "--compressor-attack=15.0",
        "--compressor-release=180.0",
        "--compressor-threshold=-28.0",
        "--compressor-ratio=12.0",
        "--compressor-knee=2.0",
        "--compressor-makeup-gain=13.0",
    ],
}
NIGHT_MODE_DEFAULT_PRESET = "medium"

# UI metadata for the settings-menu intensity picker (order + labels). Lives in
# main.py only — run.py / watchdog.py just need the args dict above.
NIGHT_MODE_PRESET_META = [
    {"id": "light",  "label": "Light",  "desc": "Gentle leveling"},
    {"id": "medium", "label": "Medium", "desc": "Balanced (default)"},
    {"id": "max",    "label": "Max",    "desc": "Strongest — flattens loud peaks hard"},
]


def _night_mode_preset(name: Optional[str]) -> str:
    """Normalise a preset name to a known key, falling back to the default."""
    return name if name in NIGHT_MODE_PRESETS else NIGHT_MODE_DEFAULT_PRESET


def _vlc_audio_filter_args(night_mode: bool, preset: Optional[str] = None) -> list:
    """VLC CLI args for the night-mode compressor at `preset`, or [] when off.

    Mirrored in run.py / watchdog.py via the shared NIGHT_MODE_PRESETS contract.
    """
    if not night_mode:
        return []
    return list(NIGHT_MODE_PRESETS[_night_mode_preset(preset)])


def _marquee_write(text: str) -> None:
    """Atomically replace the marquee file's contents (sync; tiny write).

    Clearing writes a single space, never an empty string. VLC's marq filter
    reads the file with getline(); on an *empty* file getline hits EOF, so the
    filter keeps the previously-shown text (and logs a read error every refresh
    tick). A lone space is a valid non-empty line that forces the update yet
    renders nothing — we draw no background box, so a space has no glyph.
    """
    try:
        tmp = MARQUEE_FILE.with_suffix(".tmp")
        tmp.write_text(text or " ", encoding="utf-8")
        os.replace(tmp, MARQUEE_FILE)
    except Exception:
        pass

# Keep in sync with the version badge at the bottom of static/index.html.
# Clients fetch this via /api/version and force a hard reload when the cached
# page's badge value is older than the server's value.
UI_VERSION = "5.0.0"
_lib_lock: asyncio.Lock  # initialised in lifespan


# ── Library Storage ───────────────────────────────────────────────────────────

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
    return item


def _load_lib_raw() -> dict:
    if LIBRARY_FILE.exists():
        try:
            raw = json.loads(LIBRARY_FILE.read_text(encoding="utf-8"))
            raw["items"] = [_migrate_item(it) for it in raw.get("items", [])]
            return raw
        except Exception:
            pass
    return {"profiles": [], "items": []}


def _save_lib_raw(data: dict) -> None:
    LIBRARY_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


async def get_library() -> dict:
    # The read + json.loads + _migrate_item-over-every-item is O(library size)
    # and was previously run inline on the event loop. Hot loops call this every
    # 2-5 s (progress tracker, download monitor) and it grows after every
    # download (skip_data fingerprints), so a large library.json stalled the
    # whole loop — the dashboard went laggy and the HTTPS proxy threw ReadErrors
    # mid-request while the loop was blocked. Run the blocking work in a thread;
    # the lock still serialises access, but the loop stays free.
    async with _lib_lock:
        return await asyncio.to_thread(_load_lib_raw)


async def put_library(data: dict) -> None:
    async with _lib_lock:
        await asyncio.to_thread(_save_lib_raw, data)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── Global State ──────────────────────────────────────────────────────────────

@dataclass
class AppState:
    vpn_secure: bool = True
    vpn_status_text: str = "Checking…"
    jackett_ok: bool = True               # last known Jackett HTTP reachability
    active_hash: Optional[str] = None
    active_title: Optional[str] = None
    active_file: Optional[Path] = None
    stream_status: str = "idle"   # idle | buffering | playing | error
    progress: float = 0.0
    downloaded_mb: float = 0.0
    total_mb: float = 0.0
    dl_speed_bps: int = 0
    ul_speed_bps: int = 0
    stream_task: Optional[asyncio.Task] = None
    library_play_task: Optional[asyncio.Task] = None  # in-flight VLC handoff for a library play / prev / next
    library_item_id: Optional[str] = None
    library_profile_id: Optional[str] = None
    library_item_file_count: int = 0                     # total files in the playing item
    library_playlist: list = field(default_factory=list)  # ordered file paths
    library_current_file: Optional[str] = None            # path VLC is playing now
    downloading_count: int = 0                            # active library downloads
    play_when_ready_item_id: Optional[str] = None        # auto-play this item on download complete
    play_when_ready_profile_id: Optional[str] = None
    play_when_ready_file_path: Optional[str] = None      # if set, wait for this specific file
    current_audio_track: int = -1                         # last track ID sent to VLC
    current_subtitle_track: int = -1                      # last track ID sent to VLC
    track_pref_applied_file: Optional[str] = None         # path for which track prefs were last applied
    vlc_time: int = 0                                     # VLC current position (seconds)
    vlc_duration: int = 0                                 # VLC total duration (seconds)
    vlc_volume: int = 100                                 # VLC volume 0-200 (100 = normal)
    prepare_hash: Optional[str] = None                    # hash added by /stream/prepare, pending user selection
    skip_offer: Optional[dict] = None                     # {"type": "intro"|"credits", "end_at": s, "next_item_id": id?, "next_file_path": p?}
    skip_offer_file: Optional[str] = None                 # path the current skip offer corresponds to
    skip_countdown: Optional[dict] = None                 # {"type", "file_path", "n"} active auto-skip countdown (TV marquee)
    skip_countdown_task: Optional[asyncio.Task] = None    # in-flight countdown coroutine
    resume_offer: Optional[dict] = None                   # {"position_sec": N, "file_path": "..."} when resume_mode="prompt"
    analysis_jobs: dict = field(default_factory=dict)     # series_key → {status, stage, current, total, message, item_ids, started_at, finished_at}
    # Ring buffer of Smart Skip events (most recent first). Each entry:
    # {ts, level: "info"|"warn"|"error", series_key, item_id, file_path,
    #  error_code, message}. Surfaces in the admin "Smart Skip" tab so the
    # operator can see *why* fingerprinting failed for individual files without
    # tailing logs/streamlink_app.log.
    analyzer_log: deque = field(default_factory=lambda: deque(maxlen=200))
    background_playing: bool = False                      # True when the idle background video is the active VLC playlist
    user_volume_before_bg: int = 100                      # snapshot of state.vlc_volume taken when bg first took over
    vlc_night_mode: bool = False                          # VLC compressor (dynamic-range) filter; persisted in library.json → settings.vlc_night_mode, seeded at lifespan startup. VLC must relaunch to apply (no runtime HTTP command for audio filters)
    vlc_night_mode_preset: str = "medium"                 # intensity preset (light|medium|max); persisted in library.json → settings.vlc_night_mode_preset, remembered independently of the on/off toggle
    subtitle_default_language: str = "eng"                # preferred subtitle language (settings.subtitles.default_language; "" = Any), mirrored here for state_snapshot/UI defaults; seeded at lifespan, updated by the admin subtitles POST
    subtitle_upgrade_late: bool = True                    # settings.subtitles.upgrade_late_subs, mirrored for the on-device upgrade poller
    subtitle_single_option: bool = True                   # settings.subtitles.single_option, mirrored for client/UI
    sub_auto_ai_path: str = ""                            # abs path of the AI sidecar currently auto-applied in VLC ("" = none); the upgrade loop watches this and swaps in a real sub when one arrives
    last_activity: float = 0.0                            # time.time() of last user-initiated interaction (drives scheduled-reboot idle check)
    prep_paused: bool = False                             # True ⇒ bulk stream-prep jobs hold (set by the non-admin Pause control / overnight window end)
    admin_prep_stop: bool = False                         # True ⇒ admin force-prep ("admin" queue) jobs cancel at the gate (set by the admin Stop control). Cleared when a new force-prep batch starts. Independent of prep_paused — force-prep ignores the bulk gate + activity-kill by design
    auto_prep_engaged: bool = False                       # True while the unified auto_prep_loop has prep running (overnight window OR idle trigger) — edge flag for resume/pause
    play_prep_task: Optional[asyncio.Task] = None         # the chain task prepping the currently-playing series' tail for on-device (settings.play_prep); cancelled + replaced on each new VLC play
    download_idle_open: bool = False                      # last computed idle/night DOWNLOAD window state (set by download_scheduler_loop) — drives the "waiting for idle" UI
    download_idle_configured: bool = False                # True if any admin prep window (overnight/idle) is enabled — so idle-only downloads have a window to run in
    idle_prep_on: bool = False                            # cached settings.idle_prep.enabled (set each auto_prep_loop tick) — lets the activity hook decide cheaply
    overnight_open: bool = False                          # cached: currently inside the overnight prep window (set each tick) — overnight load is intentional, so the activity hook stands down then
    sys_status: dict = field(default_factory=dict)        # latest CPU/GPU/RAM/network sample + ok/degraded/overloaded classification (set by system_monitor_loop)
    cache_autopurge_last: dict = field(default_factory=dict)  # last auto-purge result (set by cache_autopurge_loop): {at, deleted, bytes_freed, total_bytes_before} — drives the admin card's "last run" line
    # ── YouTube-on-TV (browser playback on the host display, remote-controlled) ──
    youtube_active: bool = False                          # True while a YouTube video is the active TV playback (browser, not VLC)
    youtube_video_id: Optional[str] = None               # 11-char YouTube id currently loaded on the TV page
    youtube_playback: str = ""                            # last player state from /tv: unstarted|buffering|playing|paused|ended
    youtube_tv_seen_at: float = 0.0                       # time.time() of last /tv heartbeat (drives relaunch-vs-load decision)
    system_volume_before_yt: Optional[int] = None        # OS volume (0-100) snapshot at YouTube start; falls back when no default configured
    # ── Auto-updater (transient view of the current update operation) ──
    # All persisted updater state lives in library.json → settings.autoupdate.
    # These fields just expose the live state of an in-flight check/apply so
    # the admin UI can render progress without polling git directly.
    updater_phase: str = "idle"            # idle | checking | applying | setup | restarting | error
    updater_message: str = ""              # human-readable detail for the current phase
    updater_busy: bool = False             # True while a check/apply is running (admin UI disables buttons)
    updater_last_output: str = ""          # last 8 KiB of setup.py stdout/stderr — for diagnostics
    sse_queues: list = field(default_factory=list)


state = AppState()
qbit: Optional[httpx.AsyncClient] = None
vlc_client: Optional[httpx.AsyncClient] = None   # persistent keep-alive client for VLC (see _vlc_http)
_admin_sessions: dict[str, float] = {}   # token → expiry Unix timestamp

# ── Jackett Session ───────────────────────────────────────────────────────────
_jackett_cookie: str = ""
_jackett_cookie_expiry: float = 0.0
_jackett_cookie_lock: asyncio.Lock  # initialised in lifespan


async def _jackett_login() -> str:
    """Login to Jackett and return the session cookie value."""
    async with httpx.AsyncClient(follow_redirects=True, timeout=10.0) as tmp:
        await tmp.get(f"{settings.indexer_url}/UI/Login")
        r = await tmp.post(
            f"{settings.indexer_url}/UI/Dashboard",
            data={"password": settings.jackett_password},
            follow_redirects=False,
        )
    cookie = r.cookies.get("Jackett")
    if not cookie:
        raise HTTPException(502, "Could not authenticate with Jackett — check JACKETT_PASSWORD in .env")
    return cookie


@asynccontextmanager
async def _jackett_admin():
    """Yield an httpx client authenticated to Jackett's admin API."""
    global _jackett_cookie, _jackett_cookie_expiry
    cookies: dict[str, str] = {}
    if settings.jackett_password:
        async with _jackett_cookie_lock:
            if not _jackett_cookie or time.time() >= _jackett_cookie_expiry:
                _jackett_cookie = await _jackett_login()
                _jackett_cookie_expiry = time.time() + 3600
        cookies = {"Jackett": _jackett_cookie}
    async with httpx.AsyncClient(cookies=cookies, timeout=15.0) as c:
        yield c


# ── SSE Helpers ───────────────────────────────────────────────────────────────

async def broadcast(event: str, data: dict) -> None:
    msg = f"event: {event}\ndata: {json.dumps(data)}\n\n"
    dead = []
    for q in state.sse_queues:
        try:
            q.put_nowait(msg)
        except asyncio.QueueFull:
            dead.append(q)
    for q in dead:
        state.sse_queues.remove(q)


def state_snapshot() -> dict:
    playlist = state.library_playlist
    current  = state.library_current_file
    try:
        cur_idx = playlist.index(current) if (current and current in playlist) else -1
    except (ValueError, AttributeError):
        cur_idx = -1
    return {
        "vpn_secure": state.vpn_secure,
        "vpn_status": state.vpn_status_text,
        "jackett_ok": state.jackett_ok,
        "stream_status": state.stream_status,
        "active_title": state.active_title,
        "progress": round(state.progress, 2),
        "downloaded_mb": round(state.downloaded_mb, 2),
        "total_mb": round(state.total_mb, 2),
        "dl_speed_bps": state.dl_speed_bps,
        "ul_speed_bps": state.ul_speed_bps,
        "downloading_count": state.downloading_count,
        "vlc_time": state.vlc_time,
        "vlc_duration": state.vlc_duration,
        "vlc_volume": state.vlc_volume,
        # VLC night mode (compressor / dynamic-range reduction). Persisted global
        # setting, mirrored here so the fullscreen-control toggle reflects it.
        # The intensity preset is settings-menu only but rides along so the
        # picker stays in sync across clients.
        "vlc_night_mode": state.vlc_night_mode,
        "vlc_night_mode_preset": state.vlc_night_mode_preset,
        # Admin's preferred subtitle language ("" = Any); the dashboard uses it to
        # default the subtitle-search modal's language filter. See docs/API.md.
        "subtitle_default_language": state.subtitle_default_language,
        # Auto-upgrade AI subs → real subs when they arrive, and single-option
        # language assumption. The on-device player reads these to drive its
        # subtitle-upgrade poller. See docs/STT.md / docs/STREAMING.md.
        "subtitle_upgrade_late": state.subtitle_upgrade_late,
        "subtitle_single_option": state.subtitle_single_option,
        "library_playlist_count": len(playlist),
        "library_current_index": cur_idx,
        "library_current_file": current,
        "library_playlist": list(playlist),
        "library_item_file_count": state.library_item_file_count,
        "is_library_playback": state.library_item_id is not None,
        "library_item_id": state.library_item_id,
        "play_when_ready_item_id": state.play_when_ready_item_id,
        "play_when_ready_file_path": state.play_when_ready_file_path,
        "skip_offer": state.skip_offer,
        "skip_countdown": state.skip_countdown,
        "resume_offer": state.resume_offer,
        "analysis_jobs": state.analysis_jobs,
        # False on macOS hosts — the UI hides stream-to-device / Prep affordances.
        "hls_available": HLS_AVAILABLE,
        # True ⇒ whisper.cpp + a model are installed; drives the "Generate subtitles
        # (AI)" affordance in the subtitle menus. See docs/STT.md.
        "stt_available": _stt_available(),
        # True ⇒ bulk stream-prep is paused (drives the global prep bar's Resume control).
        "prep_paused": state.prep_paused,
        # Idle/night DOWNLOAD window: whether it's open right now, and whether any
        # admin prep window is even configured (so the UI can warn that an idle-only
        # download has no window to run in). See docs/STREAMING.md + the scheduler.
        "download_idle_open": state.download_idle_open,
        "download_idle_configured": state.download_idle_configured,
        # Host resource health (cpu/ram/gpu/net + overall ok|degraded|overloaded).
        # Drives the user "host busy" banner + the admin System health card.
        "sys_status": state.sys_status,
        # YouTube-on-TV: when active, the dashboard routes its player controls to
        # /api/youtube/control instead of VLC, and hides save/handoff/episode-nav.
        # active_title / vlc_time / vlc_duration / vlc_volume are reused for display,
        # populated from the /tv page's heartbeat (see /api/youtube/tv-state).
        "youtube_active": state.youtube_active,
        "youtube_video_id": state.youtube_video_id,
        # Env keys whose absence disables features. The non-admin UI shows a
        # passive banner ("server needs admin attention") when this is non-empty
        # AND any entry has `required=True`; the admin Updates tab renders the
        # full list as a fill-in form. Keep this cheap to compute — it runs on
        # every state broadcast.
        "missing_env_keys": _missing_env_keys(),
        # Auto-updater phase for the in-flight operation (idle when nothing
        # is happening). Persisted history lives in library.json — fetch
        # `/api/admin/updater` for the full record.
        "updater_phase":   state.updater_phase,
        "updater_busy":    state.updater_busy,
        "updater_message": state.updater_message,
    }


# ── Admin Auth ────────────────────────────────────────────────────────────────

def _check_admin(request: Request) -> bool:
    """Return True if the request carries a valid admin session token."""
    if not settings.admin_password:
        return False
    token: Optional[str] = None
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
    if not token:
        token = request.headers.get("x-admin-token", "").strip() or None
    if not token:
        token = request.query_params.get("admin_token", "").strip() or None
    if not token or token not in _admin_sessions:
        return False
    if time.time() > _admin_sessions[token]:
        _admin_sessions.pop(token, None)
        return False
    return True


def _require_admin(request: Request) -> None:
    if not _check_admin(request):
        raise HTTPException(401, "Admin authentication required.")


def _pin_hash(pin: str) -> str:
    return hashlib.sha256(pin.encode()).hexdigest()


# ── qBittorrent Client ────────────────────────────────────────────────────────

async def qbit_login() -> bool:
    try:
        r = await qbit.post(
            f"{settings.qbit_url}/api/v2/auth/login",
            data={"username": settings.qbit_username, "password": settings.qbit_password},
        )
        return r.text.strip() == "Ok."
    except Exception:
        return False


async def qreq(method: str, path: str, **kw) -> Optional[httpx.Response]:
    """Make an authenticated qBit API request, re-logging in on 403."""
    try:
        r = await qbit.request(method, f"{settings.qbit_url}{path}", **kw)
        if r.status_code == 403:
            await qbit_login()
            r = await qbit.request(method, f"{settings.qbit_url}{path}", **kw)
        return r
    except Exception:
        return None


async def qbit_add_magnet(
    magnet: str,
    save_path: Optional[str] = None,
    sequential: bool = False,
) -> Optional[str]:
    h = extract_hash(magnet)
    data: dict = {"urls": magnet, "savepath": save_path or settings.qbit_download_path}
    if sequential:
        # Set sequential download at add-time so it applies before any pieces download.
        # This is the qBittorrent /add API field, separate from the toggle endpoint.
        data["sequentialDownload"] = "true"
    r = await qreq("POST", "/api/v2/torrents/add", data=data)
    return h if (r and r.text.strip() == "Ok.") else None


async def qbit_set_file_priority(h: str, indices: list[int], priority: int) -> None:
    """Set qBit file priority for a list of file indices. priority 0=skip, 1=normal, 6=high."""
    if not indices:
        return
    await qreq(
        "POST", "/api/v2/torrents/filePrio",
        data={"hash": h, "id": "|".join(str(i) for i in indices), "priority": str(priority)},
    )


async def qbit_streaming_mode(h: str) -> None:
    """Ensure sequential download is on.

    The correct API endpoint is toggleSequentialDownload (not setSequentialDownload,
    which does not exist).  Because it is a toggle, we read seq_dl from torrent info
    first and only call it when sequential is currently off.  This is a belt-and-
    suspenders check — sequential=true is already passed at add-time via qbit_add_magnet.
    """
    info = await qbit_info(h)
    if info is not None and not info.get("seq_dl", False):
        await qreq("POST", "/api/v2/torrents/toggleSequentialDownload",
                   data={"hashes": h})


async def qbit_info(h: str) -> Optional[dict]:
    r = await qreq("GET", f"/api/v2/torrents/info?hashes={h}")
    if r and r.status_code == 200:
        data = r.json()
        return data[0] if data else None
    return None


async def qbit_files(h: str) -> list:
    r = await qreq("GET", f"/api/v2/torrents/files?hash={h}")
    return r.json() if (r and r.status_code == 200) else []


async def qbit_delete(h: str, delete_files: bool = True) -> None:
    await qreq("POST", "/api/v2/torrents/delete",
                data={"hashes": h, "deleteFiles": "true" if delete_files else "false"})


async def qbit_pause(h: str) -> None:
    """Pause a torrent. qBittorrent 5.x renamed pause→stop; /pause still works there
    as a deprecated alias, but fall back to /stop on a 404 to stay correct on the
    Windows builds the box ships with (Windows is the primary target)."""
    r = await qreq("POST", "/api/v2/torrents/pause", data={"hashes": h})
    if r is None or r.status_code == 404:
        await qreq("POST", "/api/v2/torrents/stop", data={"hashes": h})


async def qbit_resume(h: str) -> None:
    """Resume a torrent. See qbit_pause for the 5.x /start fallback."""
    r = await qreq("POST", "/api/v2/torrents/resume", data={"hashes": h})
    if r is None or r.status_code == 404:
        await qreq("POST", "/api/v2/torrents/start", data={"hashes": h})


async def qbit_get_preferences() -> Optional[dict]:
    """Read qBittorrent's global application preferences (JSON dict), or None."""
    r = await qreq("GET", "/api/v2/app/preferences")
    if r and r.status_code == 200:
        try:
            return r.json()
        except Exception:
            return None
    return None


async def qbit_set_preferences(prefs: dict) -> bool:
    """Merge a partial set of global preferences into qBittorrent. qBit persists
    these in its own config, so they survive a qBit (or host) restart."""
    r = await qreq("POST", "/api/v2/app/setPreferences",
                   data={"json": json.dumps(prefs)})
    return bool(r and 200 <= r.status_code < 300)


async def qbit_get_speed_limit(kind: str) -> int:
    """Current global speed limit in bytes/sec (0 = unlimited). kind ∈ download|upload.
    Uses the transfer/*Limit endpoints, which are unambiguously bytes/sec — the
    app/preferences dl_limit/up_limit fields have a KiB-vs-bytes ambiguity across
    qBit versions, so we avoid them for speeds."""
    r = await qreq("GET", f"/api/v2/transfer/{kind}Limit")
    if r and r.status_code == 200:
        try:
            return max(0, int(r.text.strip()))
        except Exception:
            return 0
    return 0


async def qbit_set_speed_limit(kind: str, limit_bytes: int) -> bool:
    """Set the global speed limit in bytes/sec (0 = unlimited). kind ∈ download|upload."""
    verb = "setDownloadLimit" if kind == "download" else "setUploadLimit"
    r = await qreq("POST", f"/api/v2/transfer/{verb}",
                   data={"limit": str(max(0, int(limit_bytes)))})
    return bool(r and 200 <= r.status_code < 300)


async def qbit_global_limits() -> dict:
    """Snapshot qBittorrent's global seeding-ratio + speed limits for the admin UI.
    Returns {ok: False} when qBit is unreachable (the card shows an offline note)."""
    prefs = await qbit_get_preferences()
    if prefs is None:
        return {"ok": False}
    try:
        ratio = float(prefs.get("max_ratio", 1.0))
    except (TypeError, ValueError):
        ratio = 1.0
    if ratio < 0:
        ratio = 1.0   # qBit stores -1 when never set; show a sane default in the box
    return {
        "ok": True,
        "ratio_enabled":  bool(prefs.get("max_ratio_enabled", False)),
        "ratio":          round(ratio, 2),
        "dl_limit_bytes": await qbit_get_speed_limit("download"),
        "up_limit_bytes": await qbit_get_speed_limit("upload"),
    }


# ── VLC Client ────────────────────────────────────────────────────────────────

def _vlc_http() -> httpx.AsyncClient:
    """Return the shared, persistent VLC HTTP client (keep-alive connection pool).

    VLC's built-in HTTP interface is a tiny, effectively single-threaded server.
    Opening a *fresh* TCP connection on every call — which the old per-call
    `httpx.AsyncClient()` did — swamps its accept path: three background loops
    (`stat_broadcaster`, `vlc_progress_tracker`, `background_video_loop`) each
    poll every 2–3 s, and a play fires several commands in a burst, so VLC is
    constantly tearing down and re-establishing sockets and every call ends up
    taking seconds. One keep-alive client amortizes the connect across all calls
    (a warm connection from the polling loops is almost always already in the
    pool when a play starts), so commands and status reads stay sub-second.

    Created in `lifespan`; lazily built here too so any call that runs before
    startup finishes (or in a stray task) still works. `base_url` + client-level
    `auth` mean callers pass only the relative path.
    """
    global vlc_client
    if vlc_client is None or vlc_client.is_closed:
        vlc_client = httpx.AsyncClient(
            base_url=settings.vlc_url,
            auth=httpx.BasicAuth("", settings.vlc_password),
            timeout=httpx.Timeout(5.0, connect=2.0),
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=4, keepalive_expiry=30.0),
        )
    return vlc_client


async def vlc(command: str, **params) -> None:
    try:
        c = _vlc_http()
        if command == "in_play":
            # User content is taking over from the idle background video —
            # restore the volume the user had before bg took the floor.
            if state.background_playing:
                state.vlc_volume = state.user_volume_before_bg
                state.background_playing = False
            # Clamp by the current max-volume cap. state.vlc_volume can drift
            # above the cap: it's polled directly from VLC's reported volume
            # (which is 100 at fresh start) and the user_volume_before_bg
            # snapshot may have been taken before the cap was lowered. Without
            # this clamp, in_play would blast at >cap until the user nudged
            # the slider and tripped the server-side clamp.
            cap = await _global_max_volume()
            state.vlc_volume = max(0, min(cap, state.vlc_volume))
            raw = max(0, min(512, round(state.vlc_volume / 100 * 256)))
            await c.get(
                "/requests/status.xml",
                params={"command": "volume", "val": str(raw)},
            )
        await c.get(
            "/requests/status.xml",
            params={"command": command, **params},
        )
    except Exception:
        pass


async def vlc_clear_playlist() -> None:
    """Empty VLC's playlist (`pl_empty`).

    VLC's HTTP `in_play` *appends* the input to the playlist and plays it — it
    never clears what's already there. So across a session VLC's playlist
    silently grows: `[bg, epA, epB, epC, …]`. Two failure modes follow from the
    leftover items, both of which look like "the wrong thing plays":

    1. `in_play` of a URI already in the playlist plays that *existing* entry
       (often mid-list). When it ends VLC auto-advances to the item after it —
       e.g. replaying the bg video (already at index 0 from an earlier idle
       period) ends and VLC advances into a stale episode → after **Stop** an
       episode plays instead of the background video.
    2. A stale bg/episode entry left in the list can win an end-of-file
       auto-advance during a prev/next transition → the background video plays
       instead of the next episode.

    Calling this immediately *before* every fresh `in_play` keeps VLC's playlist
    a faithful mirror of `state.library_playlist` (or just the bg video), so the
    only auto-advance target is the intended tail. See docs/GOTCHAS.md.
    """
    await vlc("pl_empty")


async def vlc_status() -> Optional[dict]:
    """Return VLC's current status JSON (includes 'time' and 'length' in seconds)."""
    try:
        r = await _vlc_http().get("/requests/status.json", timeout=3.0)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None



async def vlc_playlist_uri() -> Optional[str]:
    """Return the file:// URI of the currently active VLC playlist item."""
    try:
        r = await _vlc_http().get("/requests/playlist.json", timeout=3.0)
        if r.status_code == 200:
            def _find_current(node: dict) -> Optional[str]:
                if node.get("current") == "current":
                    return node.get("uri")
                for child in node.get("children", []):
                    found = _find_current(child)
                    if found:
                        return found
                return None
            return _find_current(r.json())
    except Exception:
        pass
    return None


def uri_to_path(uri: str) -> str:
    """Convert a file:// URI back to an absolute file path."""
    path = unquote(urlparse(uri).path)
    if platform.system() == "Windows":
        path = path.lstrip("/")
    return path


# ── Subtitle Download (OpenSubtitles) ─────────────────────────────────────────

def _opensubtitles_hash(path: Path) -> Optional[str]:
    """Compute the OpenSubtitles movie hash: 64-bit sum of filesize + first 64 KB
    + last 64 KB, read as little-endian 8-byte ints. Returns 16-char hex."""
    try:
        fmt = "<q"
        chunk = 65536
        bsize = struct.calcsize(fmt)
        filesize = path.stat().st_size
        if filesize < chunk * 2:
            return None
        h = filesize
        with path.open("rb") as f:
            for _ in range(chunk // bsize):
                (val,) = struct.unpack(fmt, f.read(bsize))
                h = (h + val) & 0xFFFFFFFFFFFFFFFF
            f.seek(filesize - chunk, 0)
            for _ in range(chunk // bsize):
                (val,) = struct.unpack(fmt, f.read(bsize))
                h = (h + val) & 0xFFFFFFFFFFFFFFFF
        return f"{h:016x}"
    except Exception:
        return None


async def _current_playback_path() -> Optional[Path]:
    """Resolve the file VLC is actually playing, regardless of how it started."""
    uri = await vlc_playlist_uri()
    if uri and uri.startswith("file:"):
        try:
            p = Path(uri_to_path(uri))
            if p.exists():
                return p
        except Exception:
            pass
    for cand in (state.library_current_file, state.active_file):
        if cand:
            try:
                p = Path(cand)
                if p.exists():
                    return p
            except Exception:
                pass
    return None


def _trim_subtitle_result(s: dict) -> dict:
    return {
        "name": s.get("SubFileName") or s.get("MovieReleaseName") or "subtitle",
        "lang": s.get("ISO639") or "",
        "lang_name": s.get("LanguageName") or s.get("SubLanguageID") or "Unknown",
        "downloads": int(s.get("SubDownloadsCount") or 0),
        "matched_by": s.get("MatchedBy") or "",
        "release": s.get("MovieReleaseName") or "",
        "download_link": s.get("SubDownloadLink") or "",
    }


async def _opensubtitles_search(
    file_hash: Optional[str], file_size: Optional[int],
    query: str, lang: str,
) -> list[dict]:
    """Query the keyless rest.opensubtitles.org API by hash and/or text. Hash
    matches are exact; text matches are a fallback. Results are merged."""
    headers = {
        "User-Agent": settings.opensubtitles_user_agent,
        "X-User-Agent": settings.opensubtitles_user_agent,
    }
    lang_seg = f"/sublanguageid-{quote(lang)}" if lang else ""
    urls: list[str] = []
    if file_hash and file_size:
        urls.append(
            f"https://rest.opensubtitles.org/search"
            f"/moviebytesize-{file_size}/moviehash-{file_hash}{lang_seg}"
        )
    if query:
        urls.append(
            f"https://rest.opensubtitles.org/search/query-{quote(query)}{lang_seg}"
        )
    raw: list[dict] = []
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as c:
        for url in urls:
            try:
                r = await c.get(url, headers=headers)
                if r.status_code == 200 and isinstance(r.json(), list):
                    raw.extend(r.json())
            except Exception:
                pass
    # Dedup by download link, prefer hash matches, then by download count
    seen: set[str] = set()
    trimmed: list[dict] = []
    for s in raw:
        t = _trim_subtitle_result(s)
        link = t["download_link"]
        if not link or link in seen:
            continue
        seen.add(link)
        trimmed.append(t)
    trimmed.sort(
        key=lambda t: (0 if t["matched_by"] == "moviehash" else 1, -t["downloads"])
    )
    return trimmed[:40]


# ── TMDb (show / episode metadata) ────────────────────────────────────────────
#
# Optional integration. Provides episode titles, overviews, posters, and stills
# for the Netflix-style episode picker. Requires a free TMDb v3 API key.
#
# Configure via `TMDB_API_KEY` in .env or via the admin panel (overrides .env).
# All metadata is cached on the library item under `item["metadata"]` so the
# UI loads instantly after the first fetch.

TMDB_IMG_BASE = "https://image.tmdb.org/t/p"

_tmdb_fetch_locks: dict[str, asyncio.Lock] = {}
_tmdb_fetch_in_flight: set[str] = set()


async def _tmdb_effective_key() -> str:
    """Resolve the active TMDb API key. Admin override beats .env."""
    lib = await get_library()
    override = (
        lib.get("settings", {})
           .get("admin_overrides", {})
           .get("tmdb_api_key", "")
    )
    return (override or settings.tmdb_api_key or "").strip()


async def _tmdb_get(path: str, params: Optional[dict] = None) -> Optional[dict]:
    key = await _tmdb_effective_key()
    if not key:
        return None
    q = dict(params or {})
    q["api_key"] = key
    url = f"https://api.themoviedb.org/3{path}"
    try:
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.get(url, params=q)
            if r.status_code == 200:
                return r.json()
    except Exception:
        pass
    return None


_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")


def _strip_release_tags(name: str) -> str:
    """Strip common release/quality tags from a torrent title so a TMDb search
    has a shot at matching the canonical show name."""
    if not name:
        return ""
    s = name
    s = re.sub(r"\[[^\]]*\]", " ", s)
    s = re.sub(r"\([^)]*\)", " ", s)
    s = re.sub(
        r"\b(2160p|1080p|720p|480p|4K|UHD|BluRay|BDRip|BRRip|DVDRip|WEB-?DL|WEBRip|"
        r"HDTV|HDR|HDR10|HEVC|x264|x265|H\.?264|H\.?265|AAC|AC3|DTS|FLAC|EAC3|"
        r"REMUX|REPACK|PROPER|EXTENDED|UNRATED|MULTi|DUAL|SUBBED|DUBBED|BATCH|"
        r"COMPLETE|S\d{1,2}|Season\s*\d{1,2}|E\d{1,3})\b.*",
        " ", s, flags=re.IGNORECASE,
    )
    s = re.sub(r"[._\-]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip(" -_.")
    return s


def _search_terms_for_item(item: dict) -> tuple[str, Optional[int]]:
    """Build (query, year_hint?) for TMDb search from a library item."""
    series = (item.get("series") or "").strip()
    title  = (item.get("title")  or "").strip()
    raw    = series or title
    # Strip a trailing "S01" off the series field (it's how we group entries)
    raw = re.sub(r"\s*S\d{1,2}\s*$", "", raw, flags=re.IGNORECASE)
    cleaned = _strip_release_tags(raw)
    year_m = _YEAR_RE.search(title)
    year = int(year_m.group(0)) if year_m else None
    return cleaned, year


async def _tmdb_match_show(item: dict) -> Optional[dict]:
    """Find the most plausible TMDb TV show for this library item. Movies are
    treated as one-shot items and matched via /search/movie when there's only
    one file and no season info."""
    query, year = _search_terms_for_item(item)
    if not query:
        return None

    files = item.get("files", [])
    is_movieish = len(files) <= 1 and not item.get("season")

    # TV first — most of our library is series.
    tv = await _tmdb_get("/search/tv", {"query": query, "include_adult": "false"})
    tv_results = (tv or {}).get("results", []) or []
    # Prefer first result; year hint just biases nothing for TV (first-air-date
    # isn't always present and the search ranks by popularity already).
    if tv_results and not is_movieish:
        r = tv_results[0]
        return {"kind": "tv", "id": r["id"], "raw": r}

    if is_movieish:
        movie = await _tmdb_get(
            "/search/movie",
            {"query": query, "include_adult": "false",
             **({"year": year} if year else {})},
        )
        mv_results = (movie or {}).get("results", []) or []
        if mv_results:
            r = mv_results[0]
            return {"kind": "movie", "id": r["id"], "raw": r}

    # Fall back to the TV result for series-shaped items even if movieish failed
    if tv_results:
        r = tv_results[0]
        return {"kind": "tv", "id": r["id"], "raw": r}
    return None


async def _tmdb_fetch_tv(show_id: int, seasons: list[int]) -> dict:
    """Fetch show details + each requested season's episodes. Returns the
    cache-shaped dict (see _build_metadata_cache)."""
    details = await _tmdb_get(f"/tv/{show_id}", {}) or {}
    cache_seasons: dict[str, dict] = {}
    # Default to season 1 if no seasons were detected on disk (so a one-off
    # picker that opens before season parsing still gets *something*).
    if not seasons:
        seasons = [1]
    for sn in seasons:
        s = await _tmdb_get(f"/tv/{show_id}/season/{sn}", {}) or {}
        eps = []
        for ep in s.get("episodes", []) or []:
            eps.append({
                "season":      ep.get("season_number", sn),
                "episode":     ep.get("episode_number", 0),
                "name":        ep.get("name", "") or "",
                "overview":    ep.get("overview", "") or "",
                "still_path":  ep.get("still_path") or "",
                "air_date":    ep.get("air_date", "") or "",
                "runtime":     ep.get("runtime") or 0,
            })
        if eps or s.get("name"):
            cache_seasons[str(sn)] = {
                "name":     s.get("name", f"Season {sn}"),
                "overview": s.get("overview", "") or "",
                "poster_path": s.get("poster_path") or "",
                "episodes": eps,
            }
    return {
        "tmdb_id":       show_id,
        "tmdb_kind":     "tv",
        "title":         details.get("name") or "",
        "overview":      details.get("overview") or "",
        "poster_path":   details.get("poster_path") or "",
        "backdrop_path": details.get("backdrop_path") or "",
        "first_air_date": details.get("first_air_date") or "",
        "vote_average":  details.get("vote_average") or 0,
        "genres":        [g.get("name", "") for g in details.get("genres", []) or []],
        "seasons":       cache_seasons,
        "fetched_at":    _now_iso(),
    }


async def _tmdb_fetch_movie(movie_id: int) -> dict:
    details = await _tmdb_get(f"/movie/{movie_id}", {}) or {}
    return {
        "tmdb_id":       movie_id,
        "tmdb_kind":     "movie",
        "title":         details.get("title") or "",
        "overview":      details.get("overview") or "",
        "poster_path":   details.get("poster_path") or "",
        "backdrop_path": details.get("backdrop_path") or "",
        "release_date":  details.get("release_date") or "",
        "vote_average":  details.get("vote_average") or 0,
        "runtime":       details.get("runtime") or 0,
        "genres":        [g.get("name", "") for g in details.get("genres", []) or []],
        "seasons":       {},
        "fetched_at":    _now_iso(),
    }


async def _fetch_item_metadata(item_id: str, force: bool = False,
                                override_tmdb_id: Optional[int] = None,
                                override_kind: Optional[str] = None) -> Optional[dict]:
    """Match an item against TMDb and cache the result on the item. Coalesces
    concurrent fetches for the same item via a per-id lock."""
    if not await _tmdb_effective_key():
        return None

    lock = _tmdb_fetch_locks.setdefault(item_id, asyncio.Lock())
    async with lock:
        lib = await get_library()
        item = next((it for it in lib["items"] if it["id"] == item_id), None)
        if not item:
            return None
        cached = item.get("metadata") or {}
        if cached.get("tmdb_id") and not force and not override_tmdb_id:
            return cached

        if override_tmdb_id and override_kind:
            match = {"kind": override_kind, "id": int(override_tmdb_id)}
        else:
            match = await _tmdb_match_show(item)
        if not match:
            return None

        seasons = sorted({
            int(f.get("season", 0)) for f in item.get("files", [])
            if int(f.get("season", 0)) > 0
        })

        if match["kind"] == "tv":
            data = await _tmdb_fetch_tv(match["id"], seasons)
        else:
            data = await _tmdb_fetch_movie(match["id"])

        # Re-read the library before writing — analyzer / progress writers may
        # have updated other fields while we were fetching from TMDb.
        lib2 = await get_library()
        it2 = next((x for x in lib2["items"] if x["id"] == item_id), None)
        if it2:
            it2["metadata"] = data
            await put_library(lib2)
        return data


def _find_vlc_bin() -> Optional[str]:
    """Locate the VLC binary, checking .env first then well-known paths."""
    env_file = Path(__file__).parent / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line.startswith("_VLC_BIN="):
                val = line[9:].strip()
                if val and Path(val).exists():
                    return val
    candidates: list[str] = {
        "Darwin":  ["/Applications/VLC.app/Contents/MacOS/VLC"],
        "Windows": [
            r"C:\Program Files\VideoLAN\VLC\vlc.exe",
            r"C:\Program Files (x86)\VideoLAN\VLC\vlc.exe",
        ],
    }.get(platform.system(), ["/usr/bin/vlc"])
    for c in candidates:
        if Path(c).exists():
            return c
    return shutil.which("vlc")


def _find_vlc_hwnds_windows() -> list:
    """Return all visible top-level window handles belonging to any VLC process.

    Uses psutil for PID lookup + GetWindowThreadProcessId for matching.
    The cb variable keeps the EnumWindowsProc wrapper alive to prevent ctypes GC.
    """
    try:
        import ctypes
        from ctypes import wintypes

        vlc_pids: set = set()
        for p in psutil.process_iter(["name", "pid"]):
            if (p.info["name"] or "").lower().startswith("vlc"):
                vlc_pids.add(p.info["pid"])
        if not vlc_pids:
            return []

        user32 = ctypes.windll.user32
        found: list = []
        EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

        def _cb(hwnd, _):
            if user32.IsWindowVisible(hwnd):
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if pid.value in vlc_pids:
                    found.append(hwnd)
            return True

        cb = EnumWindowsProc(_cb)   # keep ref alive — ctypes GC pitfall
        user32.EnumWindows(cb, 0)
        return found
    except Exception:
        return []


def _stop_vlc_flash_windows() -> None:
    """Clear any taskbar attention flash on all VLC windows.

    When Windows blocks SetForegroundWindow (focus-stealing prevention), it
    falls back to flashing the target's taskbar icon — and a flashing icon
    forces the taskbar to stay visible even over a fullscreen window. Calling
    FlashWindowEx with FLASHW_STOP cancels that attention state.
    """
    hwnds = _find_vlc_hwnds_windows()
    if not hwnds:
        return
    try:
        import ctypes
        from ctypes import wintypes

        class FLASHWINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", ctypes.c_uint),
                ("hwnd", wintypes.HWND),
                ("dwFlags", wintypes.DWORD),
                ("uCount", wintypes.UINT),
                ("dwTimeout", wintypes.DWORD),
            ]

        FLASHW_STOP = 0x00000000
        user32 = ctypes.windll.user32
        for hwnd in hwnds:
            info = FLASHWINFO(ctypes.sizeof(FLASHWINFO), hwnd, FLASHW_STOP, 0, 0)
            user32.FlashWindowEx(ctypes.byref(info))
    except Exception:
        pass


def _minimize_other_windows_windows() -> None:
    """Minimize every visible top-level window that isn't owned by a VLC process.

    Called before focusing VLC so the player owns the screen on TV playback.
    Skips system / chromeless windows (no title) and already-minimized windows.
    """
    try:
        import ctypes
        from ctypes import wintypes

        vlc_pids: set = set()
        for p in psutil.process_iter(["name", "pid"]):
            if (p.info["name"] or "").lower().startswith("vlc"):
                vlc_pids.add(p.info["pid"])

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        my_pid = kernel32.GetCurrentProcessId()
        shell_hwnd = user32.GetShellWindow()

        EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

        def _cb(hwnd, _):
            if not user32.IsWindowVisible(hwnd):
                return True
            if hwnd == shell_hwnd:
                return True
            if user32.IsIconic(hwnd):
                return True
            if user32.GetWindowTextLengthW(hwnd) == 0:
                return True
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value == 0 or pid.value == my_pid or pid.value in vlc_pids:
                return True
            # SW_FORCEMINIMIZE (11) is cross-thread / cross-process safe.
            user32.ShowWindow(hwnd, 11)
            return True

        cb = EnumWindowsProc(_cb)   # keep ref alive — ctypes GC pitfall
        user32.EnumWindows(cb, 0)
    except Exception:
        pass


def _vlc_focus_windows() -> None:
    """Bring the main VLC window to the foreground on Windows using ctypes.

    Bypasses focus-stealing prevention with the standard cocktail:
      1. Zero the foreground-lock timeout (SPI_SETFOREGROUNDLOCKTIMEOUT).
      2. Send a synthetic ALT keypress — Windows resets the foreground lock
         on any keystroke, which lets SetForegroundWindow succeed.
      3. Attach our input queue to the current foreground thread.
      4. SetForegroundWindow + BringWindowToTop.
      5. FlashWindowEx(FLASHW_STOP) on every VLC hwnd to clear any taskbar
         attention flash left over from a previous blocked focus attempt.
    Without #2 and #5 the taskbar stays visible after retry/relaunch even
    though VLC is fullscreen, because the flashing icon defeats auto-hide.
    """
    hwnds = _find_vlc_hwnds_windows()
    if not hwnds:
        return
    hwnd = hwnds[0]
    try:
        import ctypes
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        SPI_SETFOREGROUNDLOCKTIMEOUT = 0x2001
        SPIF_SENDCHANGE = 0x02
        user32.SystemParametersInfoW(SPI_SETFOREGROUNDLOCKTIMEOUT, 0, 0, SPIF_SENDCHANGE)

        VK_MENU = 0x12
        KEYEVENTF_KEYUP = 0x0002
        user32.keybd_event(VK_MENU, 0, 0, 0)
        user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)

        fg_thread = user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), None)
        my_thread = kernel32.GetCurrentThreadId()
        if fg_thread != my_thread:
            user32.AttachThreadInput(my_thread, fg_thread, True)
        user32.ShowWindow(hwnd, 9)   # SW_RESTORE
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        if fg_thread != my_thread:
            user32.AttachThreadInput(my_thread, fg_thread, False)
    except Exception:
        pass
    _stop_vlc_flash_windows()


def _vlc_minimize_windows() -> None:
    """Minimize all VLC windows on Windows using ctypes."""
    hwnds = _find_vlc_hwnds_windows()
    if not hwnds:
        return
    try:
        import ctypes
        user32 = ctypes.windll.user32
        WM_SYSCOMMAND = 0x0112
        SC_MINIMIZE    = 0xF020
        for hwnd in hwnds:
            # PostMessage puts SC_MINIMIZE into VLC's own message queue (UI-thread safe).
            user32.PostMessageW(hwnd, WM_SYSCOMMAND, SC_MINIMIZE, 0)
            # SW_FORCEMINIMIZE (11) is designed for cross-process/thread minimization.
            user32.ShowWindow(hwnd, 11)
    except Exception:
        pass


async def vlc_minimize() -> None:
    """Minimize VLC on all platforms. Best-effort; never raises."""
    system = platform.system()
    try:
        if system == "Windows":
            await asyncio.get_running_loop().run_in_executor(None, _vlc_minimize_windows)
        elif system == "Darwin":
            proc = await asyncio.create_subprocess_exec(
                "osascript", "-e",
                'tell application "VLC" to set miniaturized of every window to true',
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
        elif system == "Linux":
            if shutil.which("xdotool"):
                proc = await asyncio.create_subprocess_exec(
                    "xdotool", "search", "--name", "VLC", "windowminimize", "--sync",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await proc.wait()
            elif shutil.which("wmctrl"):
                proc = await asyncio.create_subprocess_exec(
                    "wmctrl", "-r", "VLC", "-b", "add,hidden",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await proc.wait()
    except Exception:
        pass


_MACOS_HIDE_OTHERS_SCRIPT = (
    'tell application "System Events" to '
    'set visible of (every process whose visible is true and '
    'name is not "VLC" and frontmost is false) to false'
)


async def _vlc_assert_focus(system: str) -> None:
    """Single pass of platform-specific focus + minimize-others. Never raises."""
    try:
        if system == "Windows":
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, _minimize_other_windows_windows)
            await loop.run_in_executor(None, _vlc_focus_windows)
        elif system == "Darwin":
            proc = await asyncio.create_subprocess_exec(
                "osascript", "-e", 'tell application "VLC" to activate',
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
            proc = await asyncio.create_subprocess_exec(
                "osascript", "-e", _MACOS_HIDE_OTHERS_SCRIPT,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
        elif system == "Linux":
            if shutil.which("wmctrl"):
                proc = await asyncio.create_subprocess_exec(
                    "wmctrl", "-a", "VLC",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await proc.wait()
    except Exception:
        pass


async def vlc_focus_and_fullscreen() -> None:
    """Bring VLC to the foreground, minimize everything else, and enable fullscreen.
    Best-effort; never raises.

    Why this loops instead of running once:
    Hitting Stop in the UI reliably comes up correctly because the desktop is
    fully settled — Explorer/Dock/menu bar/startup apps are stable, so a single
    minimize-others + focus pass catches every window. At boot via the system
    service the dashboard starts ~5–15 s into the login sequence, so by the
    time the idle background video kicks in there are still startup apps and
    Explorer shell pieces actively launching, and they appear *on top of* VLC
    after we focused it. A single pass leaves the user looking at a windowed
    VLC behind the user's normal apps with the taskbar visible.

    Fix: keep re-asserting focus + minimize-others + fullscreen for ~20 s after
    the call, on a slowing cadence (0.5 s → 1 s → 2 s). This catches late-
    launching apps without hammering the desktop forever once things settle.
    """
    await asyncio.sleep(1.5)
    system = platform.system()

    # Cadence: tight at the start to grab the screen ASAP, then back off.
    # Total wall-time ≈ 24 s, plenty for Windows logon-time app churn.
    delays = [0.5] * 6 + [1.0] * 8 + [2.0] * 6

    for delay in delays:
        # YouTube took over the TV (browser kiosk) — stop fighting it. Without
        # this, a still-running background focus loop would keep minimizing the
        # kiosk window (via _minimize_other_windows) and re-focusing VLC.
        if state.youtube_active:
            return
        # Always re-assert focus + minimize-others, even if VLC reports
        # fullscreen=True. The reported flag tracks VLC's internal state and
        # can be True while a later-launching app is rendering on top.
        await _vlc_assert_focus(system)

        try:
            vs = await vlc_status()
        except Exception:
            vs = None
        if vs:
            vlc_state = vs.get("state", "")
            is_fs = bool(vs.get("fullscreen"))
            if vlc_state in ("playing", "paused") and not is_fs:
                try:
                    await vlc("fullscreen")
                except Exception:
                    pass

        # Belt-and-suspenders for Windows: clear taskbar attention flash
        # so the taskbar can auto-hide over fullscreen VLC.
        if system == "Windows":
            try:
                await asyncio.get_running_loop().run_in_executor(
                    None, _stop_vlc_flash_windows,
                )
            except Exception:
                pass

        # If the user explicitly stopped the bg / started a new pipeline, bail.
        if state.stream_status == "buffering":
            return

        await asyncio.sleep(delay)


async def _restart_vlc_process() -> bool:
    """Kill any running VLC processes, relaunch with HTTP interface. Returns True when port opens."""
    for p in psutil.process_iter(["name"]):
        if (p.info["name"] or "").lower().startswith("vlc"):
            try:
                p.kill()
            except Exception:
                pass

    await asyncio.sleep(1.5)

    vlc_bin = _find_vlc_bin()
    if not vlc_bin:
        return False

    vlc_port = int(urlparse(settings.vlc_url).port or 8080)

    kw: dict = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if platform.system() == "Windows":
        kw["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    _marquee_write("")  # start clean; marq needs the file to exist at launch
    subprocess.Popen(
        [vlc_bin, "--extraintf=http", "--http-host=localhost",
         f"--http-port={vlc_port}", f"--http-password={settings.vlc_password}", "--no-random",
         # StreamLink owns sidecar loading via _load_all_local_subs (which tags AI
         # subs). VLC's own autodetect would load AI `.srt`s first as *untagged*
         # tracks, so the policy mistakes them for real subs — disable it. See
         # docs/GOTCHAS.md.
         "--no-sub-autodetect-file",
         "--fullscreen", *_vlc_marquee_args(),
         # Night mode (dynamic-range compressor) when enabled — see _apply_night_mode.
         *_vlc_audio_filter_args(state.vlc_night_mode, state.vlc_night_mode_preset)],
        **kw,
    )

    deadline = asyncio.get_event_loop().time() + 15
    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.5)
        try:
            with socket.create_connection(("127.0.0.1", vlc_port), timeout=0.5):
                await asyncio.sleep(0.8)
                return True
        except OSError:
            pass
    return False


async def _retry_task(file_path: Path) -> None:
    """Background task: restart VLC and replay the current file + remainder of playlist."""
    try:
        await broadcast("stream_status", {"status": state.stream_status, "message": "Relaunching VLC…"})
        ok = await _restart_vlc_process()
        if not ok:
            state.stream_status = "error"
            await broadcast("stream_status", {"status": "error", "message": "VLC could not be relaunched."})
            return

        await vlc("in_play", input=file_path.resolve().as_uri())

        if state.library_playlist and state.library_current_file:
            try:
                cur = str(state.library_current_file)
                idx = state.library_playlist.index(cur)
                for p in state.library_playlist[idx + 1:]:
                    await vlc("in_enqueue", input=Path(p).resolve().as_uri())
            except (ValueError, Exception):
                pass

        state.stream_status = "playing"
        state.current_audio_track = -1
        state.current_subtitle_track = -1
        asyncio.create_task(vlc_focus_and_fullscreen())
        await broadcast("stream_status", {"status": "playing", "message": f"Retrying: {file_path.name}"})
    except Exception as exc:
        state.stream_status = "error"
        await broadcast("stream_status", {"status": "error", "message": f"Retry failed: {exc}"})


async def _apply_night_mode(enabled: bool) -> None:
    """Toggle the VLC night-mode compressor by relaunching VLC, then resume.

    VLC's HTTP interface has no command to add/remove an audio filter at runtime,
    so the compressor can only be switched by relaunching VLC — it's a launch arg
    (`NIGHT_MODE_ARGS`, read off `state.vlc_night_mode` in `_restart_vlc_process`).
    To make the toggle seamless mid-playback we snapshot the current file +
    position, relaunch, replay the file (and any remaining playlist tail), and
    seek back. When only the idle background video (or nothing) is on screen we
    just relaunch and let `background_video_loop` bring the bg video back with the
    new filter applied.
    """
    state.vlc_night_mode = enabled

    # Snapshot what's playing *before* killing VLC. Skip the bg video — the loop
    # restarts it on its own.
    resume_path: Optional[Path] = None
    if not state.background_playing:
        if state.library_current_file:
            resume_path = Path(state.library_current_file)
        elif state.active_file:
            resume_path = state.active_file
    vs = await vlc_status()
    live_state = (vs or {}).get("state", "")
    try:
        resume_pos = int(float((vs or {}).get("time", 0) or 0))
    except (TypeError, ValueError):
        resume_pos = 0
    has_content = resume_path is not None and live_state in ("playing", "paused")

    # Remaining playlist tail (multi-episode plays) captured before the restart.
    tail: list[str] = []
    if has_content and state.library_playlist and state.library_current_file:
        try:
            idx = state.library_playlist.index(str(state.library_current_file))
            tail = list(state.library_playlist[idx + 1:])
        except ValueError:
            tail = []

    try:
        if has_content:
            # Buffering both shows the user something and tells
            # background_video_loop to stand down during the restart gap.
            state.stream_status = "buffering"
            await broadcast("stream_status", {"status": "buffering", "message": "Applying night mode…"})
            await broadcast("state", state_snapshot())

        ok = await _restart_vlc_process()

        if not has_content:
            # Idle / background only: nothing to resume. The toggle still took
            # effect (VLC relaunched with/without the filter); just republish.
            await broadcast("state", state_snapshot())
            return

        if not ok:
            state.stream_status = "error"
            await broadcast("stream_status", {"status": "error", "message": "VLC could not be relaunched."})
            return

        first_resolved = resume_path.resolve()
        play_task = asyncio.create_task(vlc("in_play", input=first_resolved.as_uri()))
        ready = await _vlc_wait_until_ready(first_resolved, timeout=10.0)

        state.stream_status = "playing"
        state.current_audio_track = -1
        state.current_subtitle_track = -1
        await broadcast("stream_status", {"status": "playing", "message": f"Playing: {resume_path.name}"})
        await broadcast("state", state_snapshot())
        asyncio.create_task(vlc_focus_and_fullscreen())

        # Seek back to where we were once the demuxer is up. Re-issue once if VLC
        # ignored the first seek the instant it opened the file (same guard as
        # the resume-seek in _library_play_launch).
        if resume_pos > 5 and (ready or await _vlc_wait_until_ready(first_resolved, timeout=10.0)):
            await vlc("seek", val=str(resume_pos))
            await asyncio.sleep(0.6)
            cur = await vlc_status()
            if cur and float(cur.get("time", 0) or 0) < resume_pos - 15:
                await vlc("seek", val=str(resume_pos))

        # Re-apply the saved audio/subtitle track for a library item (the restart
        # reset VLC's track selection to defaults).
        if state.library_item_id and state.library_profile_id:
            asyncio.create_task(_apply_track_prefs(
                state.library_item_id, state.library_profile_id,
                str(state.library_current_file), delay=3.5,
            ))

        # Append the tail after the current file is accepted so order is right.
        if tail:
            try:
                await play_task
            except Exception:
                pass
            for p in tail:
                try:
                    await vlc("in_enqueue", input=Path(p).resolve().as_uri())
                except Exception:
                    pass
    except Exception as exc:
        state.stream_status = "error"
        await broadcast("stream_status", {"status": "error", "message": f"Night mode toggle failed: {exc}"})


# ── Utilities ─────────────────────────────────────────────────────────────────

def extract_hash(magnet: str) -> Optional[str]:
    """Pull the info-hash from a magnet URI, normalising base32 → hex."""
    m = re.search(r"xt=urn:btih:([a-fA-F0-9]{40}|[A-Za-z2-7]{32})", magnet, re.I)
    if not m:
        return None
    h = m.group(1)
    if len(h) == 32:
        try:
            h = base64.b32decode(h.upper()).hex()
        except Exception:
            return None
    return h.lower()


def parse_season_episode(name: str) -> tuple[int, int]:
    """Extract (season, episode) from filenames like S01E03, s2e5, 1x03, etc."""
    m = re.search(r"[Ss](\d{1,2})[Ee](\d{1,2})", name)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.search(r"\b(\d{1,2})x(\d{2})\b", name)
    if m:
        return int(m.group(1)), int(m.group(2))
    return 0, 0


VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".mov", ".wmv", ".m4v", ".ts", ".m2ts", ".webm"}


def build_file_list(qbit_file_list: list, save_path: str) -> list[dict]:
    """Build a sorted video-file list from qBittorrent's files API response."""
    result = []
    for f in qbit_file_list:
        rel = f.get("name", "")
        if Path(rel).suffix.lower() not in VIDEO_EXTS:
            continue
        season, episode = parse_season_episode(rel)
        result.append({
            "name": Path(rel).name,
            "path": str(Path(save_path) / rel),
            "size_bytes": f.get("size", 0),
            "season": season,
            "episode": episode,
        })
    result.sort(key=lambda x: (x["season"] or 9999, x["episode"] or 9999, x["name"]))
    return result


def _file_by_index(files: list, idx: int) -> Optional[dict]:
    """Return the file dict whose 'index' field equals idx (falls back to enumerate position)."""
    for i, f in enumerate(files):
        if f.get("index", i) == idx:
            return f
    return None


def largest_video(files: list) -> Optional[dict]:
    videos = [f for f in files if Path(f["name"]).suffix.lower() in VIDEO_EXTS]
    pool = videos or files
    return max(pool, key=lambda f: f.get("size", 0), default=None)


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


# ── Per-item download scheduling ────────────────────────────────────────────────
# A library item may carry a `download` config controlling WHEN its torrent's files
# are fetched. The download_scheduler_loop is the *single source of truth* for qBit
# file priorities + torrent pause/resume on scheduled items — never write filePrio /
# pause/resume for these items elsewhere without going through this model, or the
# next reconcile reverts it (see docs/GOTCHAS.md).
#
#   item["download"] = {
#       "mode":  "now" | "idle",          # item-wide default for files w/o an override
#       "files": { "<abs path>": "now" | "high" | "idle" | "skip" },
#   }
#
# Effective per-file schedule = files[path] if present else mode. Mapping to a live
# qBit priority depends on whether the idle/night download window is open right now:
#   skip → 0 (never)          high → 7 (download now, first)
#   now  → 1 (download now)    idle → 1 when window open, else 0
_FILE_MODES = ("now", "high", "idle", "skip")


def _download_cfg(item: dict) -> dict:
    """Return item['download'] with defaults filled in (never mutates the item)."""
    dl = item.get("download") or {}
    mode = dl.get("mode", "now")
    if mode not in ("now", "idle"):
        mode = "now"
    files = dl.get("files") or {}
    return {"mode": mode, "files": {k: v for k, v in files.items() if v in _FILE_MODES}}


def _effective_file_mode(cfg: dict, path: str) -> str:
    """Resolve a single file's effective schedule from the item download cfg."""
    fm = cfg["files"].get(path)
    return fm if fm in _FILE_MODES else cfg["mode"]


def _file_mode_to_priority(mode: str, idle_open: bool) -> int:
    """Map an effective file mode + current idle-window state to a qBit priority."""
    if mode == "skip":
        return 0
    if mode == "high":
        return 7
    if mode == "idle":
        return 1 if idle_open else 0
    return 1   # "now"


def _analyzable_files(item: dict) -> list[dict]:
    """Item video files that should be treated as part of the download: excludes
    files the user marked "skip" (deselected from the download). Those never land on
    disk, so they must not be audio-fingerprinted, counted as "pending" Smart Skip,
    or gate the ready flip."""
    cfg = _download_cfg(item)
    return [f for f in item.get("files", [])
            if _effective_file_mode(cfg, f.get("path", "")) != "skip"]


def _all_nonskip_complete(item: dict, qfiles: list, save_path: str) -> bool:
    """True iff every non-skip file in the torrent is fully downloaded (and there is
    at least one). The ready/fingerprint gate: a partial selection flips ready once
    its kept files are done, and an idle-deferred file still has to finish first — so
    audio fingerprinting only ever runs on the complete set the user asked for."""
    cfg = _download_cfg(item)
    saw = False
    for i, qf in enumerate(qfiles):
        full = str(Path(save_path) / qf.get("name", ""))
        if _effective_file_mode(cfg, full) == "skip":
            continue
        saw = True
        if qf.get("progress", 0.0) < 0.999:
            return False
    return saw


def _file_progress(item: dict, profile_id: str, file_path: str) -> Optional[dict]:
    """Return per-file progress dict for a given profile and path, or None."""
    prof = item.get("progress", {}).get(profile_id, {})
    return prof.get("file_progress", {}).get(file_path)


def _canonical_item_path(vlc_path: str, item: dict) -> str:
    """Return the item["files"] path that resolves to the same file as vlc_path.

    VLC receives Path(p).resolve().as_uri() which follows symlinks.  The stored
    path may not be resolved, so we compare resolved forms and return the stored
    path so progress keys always match what get_item_files and find_resume_hint
    expect.  Falls back to vlc_path if no match is found.
    """
    try:
        target = Path(vlc_path).resolve()
    except Exception:
        return vlc_path
    for f in item.get("files", []):
        stored = f.get("path", "")
        try:
            if Path(stored).resolve() == target:
                return stored
        except Exception:
            continue
    return vlc_path


def find_resume_hint(item: dict, profile_id: str) -> Optional[dict]:
    """Return the best file+position to resume for a profile, or None."""
    if not profile_id:
        return None
    files = item.get("files", [])
    if not files:
        return None
    prof = item.get("progress", {}).get(profile_id, {})
    file_progs = prof.get("file_progress", {})
    last_file = prof.get("last_file")

    # Prefer the last-watched file if it has meaningful in-progress position
    if last_file:
        fp = file_progs.get(last_file, {})
        if not fp.get("completed") and fp.get("position_sec", 0) > 5:
            dur = fp.get("duration_sec", 0)
            pos = fp.get("position_sec", 0)
            return {
                "file_path": last_file,
                "episode_name": Path(last_file).name,
                "position_sec": pos,
                "duration_sec": dur,
                "pct": round(pos / dur * 100, 1) if dur else 0,
            }

    # Walk files in order — return first that isn't completed
    for f in files:
        path = f.get("path", "")
        fp = file_progs.get(path, {})
        if not fp.get("completed"):
            pos = fp.get("position_sec", 0)
            dur = fp.get("duration_sec", 0)
            return {
                "file_path": path,
                "episode_name": f.get("name", Path(path).name),
                "position_sec": pos,
                "duration_sec": dur,
                "pct": round(pos / dur * 100, 1) if dur else 0,
            }

    # All completed — point back to first so user can rewatch
    f = files[0]
    return {
        "file_path": f.get("path", ""),
        "episode_name": f.get("name", ""),
        "position_sec": 0,
        "duration_sec": 0,
        "pct": 100,
        "all_completed": True,
    }


# ── Track Preference Helpers ──────────────────────────────────────────────────

async def _save_track_pref(
    item_id: str, profile_id: str, file_path: str,
    audio: Optional[int] = None, subtitle: Optional[int] = None,
) -> None:
    """Persist audio/subtitle track preference for a file into library.json."""
    try:
        async with _lib_lock:
            lib = _load_lib_raw()
            item = next((it for it in lib["items"] if it["id"] == item_id), None)
            if not item:
                return
            fp = (item.setdefault("progress", {})
                      .setdefault(profile_id, {})
                      .setdefault("file_progress", {})
                      .setdefault(file_path, {}))
            if audio is not None:
                fp["audio_track"] = audio
            if subtitle is not None:
                fp["subtitle_track"] = subtitle
            _save_lib_raw(lib)
    except Exception:
        pass


# ── Subtitle-selection descriptor + per-series memory ─────────────────────────
# A subtitle pick is remembered as a *resolvable descriptor* — not a VLC ES ID
# or HLS sidecar index, which both drift between replays (and especially once a
# late-downloaded sidecar shifts the list). The descriptor captures the user's
# intent so it survives across episodes of a series, even weeks later:
#   {off: bool, lang: "<canon>", ai: bool, name: "<sidecar filename>"}
# `name` enables an exact re-match when the same file is present; `lang`+`ai`
# drive the cross-episode / fallback match. Stored per-file (file_progress) and
# per-series (profile["series_subtitle_prefs"][<series>]).

def _norm_sub_sel(sel: Optional[dict]) -> Optional[dict]:
    """Normalize/validate a subtitle-selection descriptor; None if unusable."""
    if not isinstance(sel, dict):
        return None
    if sel.get("off"):
        return {"off": True, "lang": "", "ai": False, "name": ""}
    lang = _canon_lang(str(sel.get("lang") or "")) if sel.get("lang") else ""
    name = str(sel.get("name") or "")
    if not (lang or name):
        return None                       # nothing matchable
    return {"off": False, "lang": lang, "ai": bool(sel.get("ai")), "name": name}


def _get_series_sub_sel(lib: dict, profile_id: str, series: str) -> Optional[dict]:
    """The remembered subtitle descriptor for this profile + series, if any."""
    series = (series or "").strip()
    if not (series and profile_id):
        return None
    prof = next((p for p in lib.get("profiles", []) if p.get("id") == profile_id), None)
    if not prof:
        return None
    return (prof.get("series_subtitle_prefs", {}) or {}).get(series)


async def _save_series_sub_sel(profile_id: str, series: str, sel: Optional[dict]) -> None:
    """Persist a subtitle descriptor for this profile + series so the same kind
    of subtitle is auto-applied on the next episode (and on return weeks later)."""
    series = (series or "").strip()
    sel = _norm_sub_sel(sel)
    if not (series and profile_id and sel):
        return
    try:
        async with _lib_lock:
            lib = _load_lib_raw()
            prof = next((p for p in lib.get("profiles", []) if p.get("id") == profile_id), None)
            if not prof:
                return
            prefs = prof.setdefault("series_subtitle_prefs", {})
            prefs[series] = {**sel, "updated_at": _now_iso()}
            _save_lib_raw(lib)
    except Exception:
        pass


def _series_of_item(item: dict) -> str:
    """The series grouping key for an item ("" for movies / one-offs)."""
    return (item.get("series") or "").strip()


async def _load_all_local_subs(video: Path) -> list[dict]:
    """Load *every* sidecar subtitle found for `video` into VLC so they all show
    up as selectable tracks, then return the resulting subtitle-track list — each
    entry annotated with a best-guess `lang` (VLC's own tag when present, else
    parsed from the source filename, since VLC rarely tags loose sidecars).

    Subs are added one at a time so we can map each freshly-created ES ID back to
    the language we parsed from its filename — and, crucially, whether it's an AI
    sidecar (`_is_ai_sub_file`). VLC's own sidecar autodetect is disabled at launch
    (`--no-sub-autodetect-file`) precisely so this is the *only* path that loads
    sidecars: an AI `.srt` VLC auto-loaded would arrive untagged and read as a real
    track, which the policy would then prefer over a genuine sub. Any track already
    present here is therefore an embedded (in-container) track. The skip-on-no-new
    branch below is a harmless safety net (e.g. a sub already loaded by a download).
    """
    tracks = await _vlc_subtitle_tracks()
    known_ids = {t["id"] for t in tracks}
    lang_by_id = {t["id"]: _canon_lang(t.get("language", "")) for t in tracks}
    # Embedded tracks (present before we add anything) are real, not AI; sidecars
    # we load get their `ai`/path tagged as they're added. Lets the policy prefer
    # a real preferred-language sub over an AI one for the same language.
    meta_by_id: dict[int, dict] = {}

    for sub in await asyncio.to_thread(_discover_local_subs, video):
        guess = _parse_sub_lang(sub.name)
        is_ai = _is_ai_sub_file(sub)
        await vlc("addsubtitle", val=str(sub))
        await asyncio.sleep(0.3)
        after = await _vlc_subtitle_tracks()
        new = [t for t in after if t["id"] not in known_ids]
        if not new:
            continue                      # VLC already had this sidecar loaded
        for t in new:
            known_ids.add(t["id"])
            lang_by_id[t["id"]] = _canon_lang(t.get("language", "")) or guess
            meta_by_id[t["id"]] = {"ai": is_ai, "path": str(sub)}
        tracks = after

    return [{**t,
             "lang": lang_by_id.get(t["id"], _canon_lang(t.get("language", ""))),
             "ai":   meta_by_id.get(t["id"], {}).get("ai", False),
             "path": meta_by_id.get(t["id"], {}).get("path", "")}
            for t in tracks]


async def _apply_subtitle_policy(lib: dict, profile_id: str, file_path: str,
                                 series_sel: Optional[dict] = None) -> None:
    """Decide and *explicitly* set the subtitle track for a freshly-started file
    when the viewer has no saved per-file pick. `series_sel` is the remembered
    per-series subtitle descriptor (if any), honoured ahead of the generic policy.

    Resolves subs on/off (profile `subtitles_on` override → admin `on_by_default`).
    Off ⇒ send `subtitle_track -1` so VLC can't sneak its auto/forced sub on (it
    otherwise does, even when the UI says off — see docs/GOTCHAS.md). On ⇒ first
    aggressively load *all* sidecar subs for the file into VLC (covering `Subs/`
    folders and the like — see `_discover_local_subs`), so every option is
    selectable, then select, in priority order:
      (a) an embedded/loaded track in the preferred language (any track if the
          admin chose "Any") — embedded tracks rank ahead of sidecars;
      (b) an online auto-search download in the preferred language (if enabled);
      (c) otherwise leave subtitles off.
    """
    subs = _subs_cfg(lib)
    prof = next((p for p in lib.get("profiles", []) if p["id"] == profile_id), {})
    override = prof.get("subtitles_on")
    subs_on = subs["on_by_default"] if override is None else bool(override)

    # Any prior auto-applied-AI marker is stale on a fresh selection; re-set
    # below only if we deliberately land on an AI track.
    state.sub_auto_ai_path = ""

    if not subs_on:
        state.current_subtitle_track = -1
        await vlc("subtitle_track", val="-1")
        return

    pref = _canon_lang(subs["default_language"]) if subs["default_language"] else ""
    video = Path(file_path)

    # (a) Load every local sidecar (Subs/ folders included) + embedded tracks,
    #     then pick the preferred language. Tracks come embedded-first, so a
    #     `next()` match prefers an embedded track over a loaded sidecar.
    if video.exists():
        tracks = await _load_all_local_subs(video)
    else:
        tracks = [{**t, "lang": _canon_lang(t.get("language", "")), "ai": False, "path": ""}
                  for t in await _vlc_subtitle_tracks()]

    async def _select(track: dict) -> None:
        state.current_subtitle_track = track["id"]
        # Remember when the chosen track is an AI sub so the upgrade loop can
        # swap in a real preferred-language sub once it finishes downloading.
        state.sub_auto_ai_path = track.get("path", "") if track.get("ai") else ""
        await vlc("subtitle_track", val=str(track["id"]))

    real = [t for t in tracks if not t.get("ai")]
    ai   = [t for t in tracks if t.get("ai")]

    # Per-series memory: honour the kind of subtitle the viewer last chose for
    # this series (real vs AI vs off), falling back across kinds when the exact
    # one isn't present on this episode. Takes priority over the generic policy.
    if series_sel:
        if series_sel.get("off"):
            state.current_subtitle_track = -1
            await vlc("subtitle_track", val="-1")
            return
        slang = series_sel.get("lang") or ""
        sname = series_sel.get("name") or ""
        cand = None
        if sname:
            cand = next((t for t in tracks if t.get("path")
                         and Path(t["path"]).name == sname), None)
        if cand is None and slang:
            pool = ai if series_sel.get("ai") else real
            cand = (next((t for t in pool if t["lang"] == slang), None)
                    or next((t for t in tracks if t["lang"] == slang), None))
        if cand is None and subs["single_option"] and len(real) == 1:
            cand = real[0]
        if cand:
            await _select(cand)
            return

    if pref:
        # Prefer a REAL preferred-language track over an AI one for that language.
        match = next((t for t in real if t["lang"] == pref), None)
        # Only one real option and no exact match → assume it's the right one.
        if match is None and subs["single_option"] and len(real) == 1:
            match = real[0]
        # Fall back to an AI track in the preferred language; the upgrade loop
        # will replace it with a real sub when one arrives (if enabled).
        if match is None:
            match = next((t for t in ai if t["lang"] == pref), None)
        if match:
            await _select(match)
            return
    elif tracks:
        # "Any" preferred language — prefer a real track, else anything available.
        await _select(real[0] if real else tracks[0])
        return

    # (b) auto-search OpenSubtitles for the preferred language.
    if subs["auto_search"] and pref and video.exists():
        new_id = await _auto_fetch_subtitle(video, pref)
        if new_id is not None:
            return

    # (c) nothing usable — leave subtitles off.
    state.current_subtitle_track = -1
    await vlc("subtitle_track", val="-1")


async def _apply_track_prefs(
    item_id: str, profile_id: str, file_path: str, delay: float = 2.0,
) -> None:
    """Apply audio/subtitle tracks for a file after a short delay.

    Audio + subtitle honour any saved per-file preference. With no saved subtitle
    pick, the subtitle track falls through to the admin/profile default policy
    (`_apply_subtitle_policy`), which tells VLC *explicitly* whether subs are on
    or off — VLC otherwise auto-enables its first/forced sub even when subs should
    be off. See docs/GOTCHAS.md.
    """
    try:
        await asyncio.sleep(delay)
        lib = await get_library()
        item = next((it for it in lib["items"] if it["id"] == item_id), None)
        if not item:
            return
        fp = (item.get("progress", {})
                  .get(profile_id, {})
                  .get("file_progress", {})
                  .get(file_path, {}))
        audio = fp.get("audio_track")
        subtitle = fp.get("subtitle_track")
        if audio is not None:
            state.current_audio_track = audio
            await vlc("audio_track", val=str(audio))
        if subtitle is not None:
            # Explicit per-file user pick wins over the default policy. VLC's own
            # sidecar autodetect is disabled, so load every sidecar ourselves first
            # — otherwise the subtitle menu would show only embedded tracks and a
            # saved sidecar ES ID wouldn't resolve. (The no-pick branch loads them
            # via _apply_subtitle_policy.)
            video = Path(file_path)
            if video.exists():
                await _load_all_local_subs(video)
            state.current_subtitle_track = subtitle
            state.sub_auto_ai_path = ""           # an explicit pick isn't auto-AI
            await vlc("subtitle_track", val=str(subtitle))
        else:
            series_sel = _get_series_sub_sel(lib, profile_id, _series_of_item(item))
            await _apply_subtitle_policy(lib, profile_id, file_path, series_sel=series_sel)
        state.track_pref_applied_file = file_path
    except Exception:
        pass


# ── Background Task: Mullvad Guard ───────────────────────────────────────────

async def vpn_guard() -> None:
    """Poll `mullvad status` every 3 s; kill qBittorrent if not connected."""
    while True:
        try:
            proc = await asyncio.create_subprocess_exec(
                "mullvad", "status",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, _ = await proc.communicate()
            text = out.decode(errors="replace")
            connected = "Connected" in text
            first_line = text.strip().splitlines()[0] if text.strip() else "Unknown"
            state.vpn_status_text = first_line

            if not connected and state.vpn_secure:
                state.vpn_secure = False
                pname = "qbittorrent.exe" if platform.system() == "Windows" else "qbittorrent"
                for p in psutil.process_iter(["name"]):
                    if (p.info["name"] or "").lower() == pname.lower():
                        try:
                            p.kill()
                        except psutil.NoSuchProcess:
                            pass
                await broadcast("vpn_status", {"secure": False, "status": first_line})

            elif connected and not state.vpn_secure:
                state.vpn_secure = True
                await broadcast("vpn_status", {"secure": True, "status": first_line})

        except FileNotFoundError:
            state.vpn_status_text = "mullvad CLI not installed"
        except Exception:
            pass

        await asyncio.sleep(3)


# ── Background Task: Jackett Health Monitor ──────────────────────────────────

async def jackett_health_monitor() -> None:
    """Poll Jackett over HTTP regularly so the dashboard knows when the indexer
    is unreachable, and — as a backstop to the process watchdog — kick a local
    restart if Jackett stays wedged for an extended period.

    The watchdog (run.py / the installed service) is the primary supervisor and
    recovers a hung Jackett within seconds. This in-app monitor only force-
    restarts after a *long* sustained outage, so it never duels a healthy
    watchdog: if the watchdog can recover Jackett it already has, and this path
    stays dormant. It also covers running the dashboard without the watchdog.

    Reachability here means "answers HTTP", not "port is open" — a hung Jackett
    keeps the port bound while it stops serving, which is the failure mode that
    used to require a reboot.
    """
    CHECK_EVERY         = 20    # seconds between probes
    FAIL_BEFORE_RESTART = 6     # ~2 min of sustained failure before a backstop restart

    base = settings.indexer_url.rstrip("/")
    m = re.search(r"https?://([^:/]+)", settings.indexer_url)
    host = m.group(1) if m else "127.0.0.1"
    is_local = host in ("localhost", "127.0.0.1", "::1")

    consecutive_fail = 0
    while True:
        await asyncio.sleep(CHECK_EVERY)

        serving = False
        try:
            async with httpx.AsyncClient(timeout=5.0) as c:
                await c.get(f"{base}/UI/Login")
            serving = True   # any HTTP status means the web stack is answering
        except Exception:
            serving = False

        if serving != state.jackett_ok:
            state.jackett_ok = serving
            await broadcast("jackett_status", {"ok": serving, "url": settings.indexer_url})
            print(f"[jackett] {'reachable again' if serving else 'UNREACHABLE'} at {settings.indexer_url}")

        if serving:
            consecutive_fail = 0
            continue

        consecutive_fail += 1
        if is_local and consecutive_fail >= FAIL_BEFORE_RESTART:
            print(f"[jackett] unreachable for ~{consecutive_fail * CHECK_EVERY}s — "
                  "attempting in-app backstop restart")
            try:
                from watchdog import restart_jackett
                ok = await asyncio.to_thread(restart_jackett)
                print(f"[jackett] backstop restart {'succeeded' if ok else 'failed'}")
            except Exception as exc:
                print(f"[jackett] backstop restart errored: {exc}")
            consecutive_fail = 0   # cooldown — re-evaluate fresh on the next probes


# ── Machine Reboot / Scheduled Restart ───────────────────────────────────────

def _reboot_commands() -> list[list[str]]:
    """Platform-appropriate reboot command(s), tried in order until one works.

    On macOS the launchd *user agent* can restart via System Events without
    sudo; we fall back to `sudo -n shutdown` (passwordless) then a bare
    `shutdown` for setups that allow it. Linux prefers `systemctl reboot`.
    """
    sysname = platform.system()
    if sysname == "Windows":
        return [["shutdown", "/r", "/t", "0"]]
    if sysname == "Darwin":
        return [
            ["osascript", "-e", 'tell application "System Events" to restart'],
            ["sudo", "-n", "shutdown", "-r", "now"],
            ["shutdown", "-r", "now"],
        ]
    # Linux / other Unix
    return [
        ["systemctl", "reboot"],
        ["sudo", "-n", "shutdown", "-r", "now"],
        ["shutdown", "-r", "now"],
    ]


def _do_reboot_blocking() -> bool:
    """Fire the reboot. Returns True if a command was accepted. Blocking — run
    via asyncio.to_thread. The machine usually goes down before this returns."""
    for cmd in _reboot_commands():
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
            if proc.returncode == 0:
                print(f"[reboot] issued: {' '.join(cmd)}")
                return True
            print(f"[reboot] '{' '.join(cmd)}' rc={proc.returncode} "
                  f"stderr={proc.stderr.strip()[:200]}")
        except FileNotFoundError:
            continue   # command not on this platform — try the next
        except Exception as exc:
            print(f"[reboot] '{' '.join(cmd)}' errored: {exc}")
    return False


async def _reboot_machine(delay: float = 0.5) -> None:
    """Reboot the host after a short delay (lets the HTTP response flush)."""
    await asyncio.sleep(delay)
    print("[reboot] rebooting host machine now")
    ok = await asyncio.to_thread(_do_reboot_blocking)
    if not ok:
        print("[reboot] all reboot commands failed — host may lack permission. "
              "On macOS/Linux configure passwordless sudo for `shutdown`, "
              "or run the service with reboot rights.")


def _now_in_tz(tzname: str) -> datetime:
    """Current time in the named IANA tz (e.g. 'America/Los_Angeles').
    Empty / unknown tz → system local time."""
    if tzname:
        try:
            from zoneinfo import ZoneInfo
            return datetime.now(ZoneInfo(tzname))
        except Exception:
            pass
    return datetime.now()


async def _machine_in_use(window_secs: int, ignore_downloads: bool = False,
                          for_prep: bool = False) -> bool:
    """True if the box looks busy: VLC actively playing/paused real content, an
    active stream/download, or a user interaction within `window_secs`.

    `ignore_downloads=True` drops library downloads from the "busy" signal — used by
    the download scheduler's idle check so an idle-only download that's running can't
    flip the box "in use" and self-close its own idle window (see _download_idle_open).

    `for_prep=True` ALSO treats an open dashboard (a live SSE client) as "in use", so
    idle stream-prep won't hammer the box while someone has the site open — even if
    they're just looking and haven't clicked anything (a page load is a GET, which
    deliberately doesn't stamp last_activity). The scheduled reboot does NOT pass this
    (a forgotten-open tab shouldn't block the nightly reboot).
    """
    if for_prep and state.sse_queues:
        return True
    # 1. Live VLC playback of non-background content
    if not state.background_playing:
        vs = await vlc_status()
        if vs and vs.get("state") in ("playing", "paused"):
            return True
    # 2. Active stream pipeline or library downloads in flight
    if state.stream_status in ("buffering", "playing"):
        return True
    if not ignore_downloads and state.downloading_count > 0:
        return True
    # 3. Recent user-initiated HTTP interaction (set by the activity middleware)
    if state.last_activity and (time.time() - state.last_activity) < window_secs:
        return True
    return False


def _scheduled_reboot_cfg(lib: dict) -> dict:
    """Read settings.scheduled_reboot with defaults filled in."""
    cfg = (lib.get("settings", {}) or {}).get("scheduled_reboot") or {}
    return {
        "enabled":      bool(cfg.get("enabled", False)),
        "time":         str(cfg.get("time", "00:00")),
        "timezone":     str(cfg.get("timezone", "America/Los_Angeles")),
        "idle_minutes": int(cfg.get("idle_minutes", 15)),
        "last_fired":   str(cfg.get("last_fired", "")),
    }


def _autoupdate_cfg(lib: dict) -> dict:
    """Read settings.autoupdate with defaults filled in.

    The auto-updater is opt-in (default off) and pinned to the `main` branch
    unless the admin changes it. Allowed branches are enforced by updater.py;
    a stale config that names something else is sanitised to `main` on read —
    UNLESS dev mode is on, in which case any structurally-valid branch name
    (e.g. a feature branch) is honoured so a developer can ride it.
    """
    cfg = (lib.get("settings", {}) or {}).get("autoupdate") or {}
    dev_mode = bool(cfg.get("dev_mode", False))
    branch = str(cfg.get("branch", "main"))
    if branch not in updater.ALLOWED_BRANCHES \
            and not (dev_mode and updater.branch_allowed(branch, allow_any=True)[0]):
        branch = "main"
    try:
        interval = int(cfg.get("interval_hours", 6))
    except (TypeError, ValueError):
        interval = 6
    interval = max(1, min(168, interval))   # 1 h .. 1 week
    return {
        "enabled":           bool(cfg.get("enabled", False)),
        "branch":            branch,
        "dev_mode":          dev_mode,
        "interval_hours":    interval,
        "auto_apply":        bool(cfg.get("auto_apply", True)),
        "last_check_at":     int(cfg.get("last_check_at", 0)),
        "last_check_status": str(cfg.get("last_check_status", "")),
        "last_applied_at":   int(cfg.get("last_applied_at", 0)),
        "last_applied_commit": str(cfg.get("last_applied_commit", "")),
        "last_error":        str(cfg.get("last_error", "")),
    }


def _overnight_prep_cfg(lib: dict) -> dict:
    """Read settings.overnight_prep (auto stream-prep window) with defaults."""
    cfg = (lib.get("settings", {}) or {}).get("overnight_prep") or {}
    on_end = str(cfg.get("on_end", "pause"))
    if on_end not in ("pause", "continue"):
        on_end = "pause"
    return {
        "enabled":  bool(cfg.get("enabled", False)),
        "start":    str(cfg.get("start", "02:00")),
        "end":      str(cfg.get("end", "06:00")),
        "timezone": str(cfg.get("timezone", "America/Los_Angeles")),
        "on_end":   on_end,
    }


def _idle_prep_cfg(lib: dict) -> dict:
    """Read settings.idle_prep (activity-gated auto stream-prep) with defaults.

    Unlike the fixed nightly window, this fires whenever the box has been idle —
    no user interaction / VLC playback / active stream / running download — for
    `idle_minutes`, and pauses (discarding the in-flight encode) the instant
    activity returns. The same idle window doubles as the activity detector."""
    cfg = (lib.get("settings", {}) or {}).get("idle_prep") or {}
    try:
        idle_minutes = int(cfg.get("idle_minutes", 30))
    except (TypeError, ValueError):
        idle_minutes = 30
    idle_minutes = max(1, min(720, idle_minutes))
    return {
        "enabled":      bool(cfg.get("enabled", False)),
        "idle_minutes": idle_minutes,
    }


def _play_prep_cfg(lib: dict) -> dict:
    """Read settings.play_prep (auto on-device prep when a video is played on VLC).

    Enabled by default. When on, every VLC library play immediately HLS-preps the
    playing episode for on-device, then the rest of the playlist one at a time —
    regardless of the idle/overnight settings or live user activity (the jobs run
    as 'interactive', so the bulk pause gate and the activity-kill don't touch
    them). See `_maybe_start_play_prep` / `_play_prep_chain`."""
    cfg = (lib.get("settings", {}) or {}).get("play_prep") or {}
    return {"enabled": bool(cfg.get("enabled", True))}


def _cache_autopurge_cfg(lib: dict) -> dict:
    """Read settings.cache_autopurge (auto-evict orphan offline-cache bundles when
    the cache outgrows a size cap) with defaults.

    `max_gb` is the total `.offline_cache/` size at which a purge fires. When the
    walk reports the cache at/above this, every *orphan* bundle (cache/partial dirs
    + legacy MP4s that no longer map to a live library file) is deleted. Bundles
    backing current library files are never touched, so this can't evict something
    a viewer might still want — see `cache_autopurge_loop`."""
    cfg = (lib.get("settings", {}) or {}).get("cache_autopurge") or {}
    try:
        max_gb = float(cfg.get("max_gb", 50))
    except (TypeError, ValueError):
        max_gb = 50.0
    max_gb = max(1.0, min(10000.0, max_gb))
    return {
        "enabled": bool(cfg.get("enabled", False)),
        "max_gb":  max_gb,
    }


def _subs_cfg(lib: dict) -> dict:
    """Read settings.subtitles (the unified subtitle policy) with defaults.

    `default_language` is the preferred subtitle language (3-letter canon code,
    or "" = Any). One language drives all three subtitle features: the online-
    search default, automatic track selection on playback, AND the AI-generation
    trigger (`_stt_cfg` re-sources its language from here).

    Unconfigured ⇒ "eng": when the `subtitles` block has never been written we
    seed from the legacy `settings.stt.default_language` if it's set, else fall
    back to English. Once the block exists its stored value is used verbatim, so
    an admin who explicitly picks "Any" (saved as "") keeps it.

    `on_by_default` is the admin default for subtitles on/off (a profile may
    override via `profile["subtitles_on"]`). `auto_search` lets playback fetch a
    preferred-language subtitle from OpenSubtitles when none is embedded.
    """
    settings = lib.get("settings", {}) or {}
    cfg = settings.get("subtitles")
    if not isinstance(cfg, dict) or "default_language" not in cfg:
        # Never configured — migrate from the old STT language, else English.
        legacy = str((settings.get("stt") or {}).get("default_language", "")).strip().lower()
        default_language = _canon_lang(legacy) if legacy else "eng"
        cfg = cfg if isinstance(cfg, dict) else {}
    else:
        raw = str(cfg.get("default_language", "")).strip().lower()
        default_language = _canon_lang(raw) if raw else ""
    return {
        "default_language": default_language,
        "on_by_default":    bool(cfg.get("on_by_default", False)),
        "auto_search":      bool(cfg.get("auto_search", True)),
        # When a real (downloaded/embedded) preferred-language subtitle arrives
        # AFTER an AI sub was auto-applied (sidecars often finish downloading
        # after the video), swap to it automatically. Default on.
        "upgrade_late_subs": bool(cfg.get("upgrade_late_subs", True)),
        # When exactly one real subtitle is found, treat it as the preferred
        # language even if its filename doesn't declare one. Default on.
        "single_option":     bool(cfg.get("single_option", True)),
    }


def _stt_cfg(lib: dict) -> dict:
    """Read settings.stt (auto subtitle generation) with defaults.

    `default_language` is unified with the subtitle policy — it's sourced from
    `_subs_cfg` (settings.subtitles), not a separate STT key — so the same
    preferred language drives search, track selection, and generation. When set,
    a source with text subtitles but none in that language still triggers
    generation; when blank ("Any"), only a source with NO usable text subtitle
    does. `translate` adds an English track for non-English audio (whisper can
    only translate TO English).
    """
    cfg = (lib.get("settings", {}) or {}).get("stt") or {}
    return {
        "enabled":          bool(cfg.get("enabled", True)),
        "default_language": _subs_cfg(lib)["default_language"],
        "translate":        bool(cfg.get("translate", True)),
    }


# Canonicalize a language tag so 2- and 3-letter spellings of the same language
# compare equal (en/eng, ja/jpn, …). Anything unknown maps to itself.
_LANG_CANON = {
    "en": "eng", "ja": "jpn", "es": "spa", "esp": "spa", "fr": "fre", "fra": "fre",
    "de": "ger", "deu": "ger", "it": "ita", "pt": "por", "ru": "rus",
    "zh": "chi", "zho": "chi", "ko": "kor", "ar": "ara", "hi": "hin",
    "nl": "nld", "sv": "swe", "fi": "fin", "no": "nor", "da": "dan",
    "pl": "pol", "tr": "tur", "uk": "ukr", "th": "tha", "vi": "vie",
}


def _canon_lang(code: str) -> str:
    code = (code or "").strip().lower()
    return _LANG_CANON.get(code, code)


def _hhmm_to_min(s: str) -> Optional[int]:
    """'HH:MM' → minutes-since-midnight, or None if malformed."""
    try:
        h, m = s.split(":")
        h, m = int(h), int(m)
    except (ValueError, AttributeError):
        return None
    if 0 <= h <= 23 and 0 <= m <= 59:
        return h * 60 + m
    return None


def _in_overnight_window(now: datetime, start_min: int, end_min: int) -> bool:
    """True if `now` falls inside [start, end). Handles windows that wrap past
    midnight (e.g. 23:00–06:00). A zero-length window (start == end) is never in."""
    cur = now.hour * 60 + now.minute
    if start_min == end_min:
        return False
    if start_min < end_min:
        return start_min <= cur < end_min
    return cur >= start_min or cur < end_min


# ── Download scheduling: idle/night window + reconcile + loop ───────────────────

async def _download_idle_open(lib: dict) -> bool:
    """True if the moment is inside the idle/night DOWNLOAD window — reusing the
    admin prep schedules: the Overnight Stream Prep window OR Idle Auto-Prep idleness.

    Unlike auto_prep's idle check, a running download does NOT count as activity here
    (`ignore_downloads=True`), so an idle-only download that's actively fetching can't
    flip the box "in use" and immediately close its own window."""
    on_cfg = _overnight_prep_cfg(lib)
    if on_cfg["enabled"]:
        s_min = _hhmm_to_min(on_cfg["start"])
        e_min = _hhmm_to_min(on_cfg["end"])
        if s_min is not None and e_min is not None:
            if _in_overnight_window(_now_in_tz(on_cfg["timezone"]), s_min, e_min):
                return True
    id_cfg = _idle_prep_cfg(lib)
    if id_cfg["enabled"]:
        if not await _machine_in_use(id_cfg["idle_minutes"] * 60, ignore_downloads=True):
            return True
    return False


def _download_idle_configured(lib: dict) -> bool:
    """True if at least one admin prep window (overnight or idle) is enabled, i.e. an
    idle-only download actually has a window to run in."""
    return bool(_overnight_prep_cfg(lib)["enabled"] or _idle_prep_cfg(lib)["enabled"])


async def _reconcile_item_downloads(item: dict, idle_open: bool) -> bool:
    """Apply an item's download schedule to qBittorrent: set each managed file's
    priority from its effective mode + the idle window, then pause/resume the torrent
    so it only fetches when something is allowed to download right now.

    The single writer of scheduled items' qBit file priorities + pause state. Returns
    True if anything is actively downloading for this item now."""
    h = item.get("torrent_hash")
    if not h:
        return False
    cfg = _download_cfg(item)
    # Plain "download now, no per-file overrides" items are left entirely alone.
    if cfg["mode"] == "now" and not cfg["files"]:
        return True
    info = await qbit_info(h)
    qfiles = await qbit_files(h)
    if not info or not qfiles:
        return False
    sp = info.get("save_path", settings.qbit_download_path)
    by_priority: dict[int, list[int]] = {}
    any_active = False
    for i, qf in enumerate(qfiles):
        idx = qf.get("index", i)
        full = str(Path(sp) / qf.get("name", ""))
        want = _file_mode_to_priority(_effective_file_mode(cfg, full), idle_open)
        if want > 0:
            any_active = True
        if qf.get("priority", 1) != want:
            by_priority.setdefault(want, []).append(idx)
    for prio, ids in by_priority.items():
        await qbit_set_file_priority(h, ids, prio)
    # Torrent-level pause is the coarse gate (only touch download-phase states so we
    # never pause a torrent that's already finished + seeding): don't leave a torrent
    # "downloading" with every file at priority 0 (some qBit builds error/auto-pause
    # that), and an explicit pause is what actually halts tracker churn while idle.
    qstate = info.get("state", "")
    DL_PAUSED = ("pausedDL", "stoppedDL")
    DL_ACTIVE = ("downloading", "metaDL", "forcedMetaDL", "stalledDL", "forcedDL",
                 "queuedDL", "checkingDL", "allocating", "checkingResumeData")
    if any_active and qstate in DL_PAUSED:
        await qbit_resume(h)
    elif not any_active and qstate in DL_ACTIVE:
        await qbit_pause(h)
    return any_active


async def _apply_item_schedule(item: dict, lib: dict) -> bool:
    """Reconcile qBit after a user schedule change, and reactivate a *finished* item
    when the change re-introduced a non-skip file that isn't on disk yet — e.g.
    un-skipping a file (or moving an idle file to now) on a download that had already
    flipped to 'ready'. Flipping the item back to 'downloading' is what lets the
    monitor + scheduler resume managing it (downloading→ready stays the monitor's job).
    Returns the current idle-window state. Mutates `item` in place; caller persists."""
    idle_open = await _download_idle_open(lib)
    await _reconcile_item_downloads(item, idle_open)
    h = item.get("torrent_hash")
    if h and item.get("status") == "ready":
        info = await qbit_info(h)
        qfiles = await qbit_files(h) if info else []
        if info and qfiles:
            sp = info.get("save_path", settings.qbit_download_path)
            cfg = _download_cfg(item)
            pending = any(
                _effective_file_mode(cfg, str(Path(sp) / qf.get("name", ""))) != "skip"
                and qf.get("progress", 0.0) < 0.999
                for qf in qfiles
            )
            if pending:
                item["status"] = "downloading"
                state.downloading_count += 1
    return idle_open


async def download_scheduler_loop() -> None:
    """Honour per-item download schedules: only fetch 'idle'-scheduled files/torrents
    during the idle/night window (reusing the admin prep windows), and keep 'now'
    files downloading. Runs every 15 s and is the single writer of scheduled items'
    qBit file priorities + pause state. See _reconcile_item_downloads."""
    while True:
        await asyncio.sleep(15)
        try:
            lib = await get_library()
            idle_open = await _download_idle_open(lib)
            prev_open = state.download_idle_open
            state.download_idle_open = idle_open
            state.download_idle_configured = _download_idle_configured(lib)
            scheduled = [
                it for it in lib["items"]
                if it.get("status") == "downloading" and it.get("torrent_hash")
                and (_download_cfg(it)["mode"] == "idle" or _download_cfg(it)["files"])
            ]
            for item in scheduled:
                await _reconcile_item_downloads(item, idle_open)
                await asyncio.sleep(0)
            if prev_open != idle_open:
                await broadcast("state", state_snapshot())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[dlsched] download_scheduler_loop error: {exc}")


# ── System resource monitor ───────────────────────────────────────────────────
# Samples CPU / RAM / GPU / network every few seconds and classifies each as
# ok | degraded | overloaded, into state.sys_status. Drives the user-facing
# "host busy — performance may be reduced" banner (so a user arriving mid-prep
# knows it's catching up) and the admin System-tab health card. Best-effort:
# psutil for CPU/RAM/net, nvidia-smi (if present) for GPU.

_net_prev: dict = {}                 # last net counters + timestamp, for rate deltas
_gpu_smi_ok: Optional[bool] = None   # None=untested, True=usable, False=give up probing


def _classify(value: float, deg: float, over: float) -> str:
    """Map a 0–100 utilisation to ok / degraded / overloaded."""
    if value >= over:
        return "overloaded"
    if value >= deg:
        return "degraded"
    return "ok"


async def _sample_gpu() -> Optional[dict]:
    """Best-effort NVIDIA GPU sample via nvidia-smi → {util_pct, mem_pct}, or None
    when there's no usable GPU/tool (cached so a GPU-less box stops probing)."""
    global _gpu_smi_ok
    if _gpu_smi_ok is False:
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            "nvidia-smi",
            "--query-gpu=utilization.gpu,memory.used,memory.total",
            "--format=csv,noheader,nounits",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=4.0)
    except Exception:
        _gpu_smi_ok = False
        return None
    parts = [p.strip() for p in (out.decode(errors="ignore").strip().splitlines() or [""])[0].split(",")]
    if len(parts) < 3:
        _gpu_smi_ok = False
        return None
    try:
        util, used, total = float(parts[0]), float(parts[1]), float(parts[2])
    except ValueError:
        _gpu_smi_ok = False
        return None
    _gpu_smi_ok = True
    return {"util_pct": round(util, 1), "mem_pct": round((used / total * 100.0) if total else 0.0, 1)}


async def system_monitor_loop() -> None:
    """Sample host resources every 5 s and classify into state.sys_status."""
    _ORDER = {"ok": 0, "degraded": 1, "overloaded": 2}
    try:
        psutil.cpu_percent(interval=None)   # prime the delta (first call is meaningless)
    except Exception:
        pass
    while True:
        await asyncio.sleep(5)
        try:
            cpu = float(psutil.cpu_percent(interval=None))   # % since last call (~5 s window)
            vm = psutil.virtual_memory()
            ram = float(vm.percent)

            # Network: throughput + error/drop deltas from the counters.
            net = await asyncio.to_thread(psutil.net_io_counters)
            now = time.time()
            up_mbps = down_mbps = 0.0
            net_status = "ok"
            errs = net.errin + net.errout + net.dropin + net.dropout
            if _net_prev:
                dt = max(0.001, now - _net_prev["ts"])
                down_mbps = max(0.0, (net.bytes_recv - _net_prev["recv"]) * 8 / 1e6 / dt)
                up_mbps   = max(0.0, (net.bytes_sent - _net_prev["sent"]) * 8 / 1e6 / dt)
                d_err = errs - _net_prev["errs"]
                net_status = "overloaded" if d_err > 50 else "degraded" if d_err > 0 else "ok"
            _net_prev.update(ts=now, recv=net.bytes_recv, sent=net.bytes_sent, errs=errs)

            gpu = await _sample_gpu()

            cpu_status = _classify(cpu, 75, 92)
            ram_status = _classify(ram, 82, 93)
            comps = [cpu_status, ram_status, net_status]
            gpu_block = None
            if gpu is not None:
                gpu_status = _classify(max(gpu["util_pct"], gpu["mem_pct"]), 80, 94)
                gpu_block = {**gpu, "status": gpu_status}
                comps.append(gpu_status)
            overall = max(comps, key=lambda s: _ORDER.get(s, 0))

            state.sys_status = {
                "cpu": {"pct": round(cpu, 1), "status": cpu_status},
                "ram": {"pct": round(ram, 1), "used_gb": round(vm.used / 1e9, 1),
                        "total_gb": round(vm.total / 1e9, 1), "status": ram_status},
                "gpu": gpu_block,   # None ⇒ no usable NVIDIA GPU on this host
                "net": {"up_mbps": round(up_mbps, 1), "down_mbps": round(down_mbps, 1),
                        "status": net_status},
                "overall": overall,
                "updated_at": now,
            }
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[sysmon] system_monitor_loop error: {exc}")


async def cache_autopurge_loop() -> None:
    """Auto-evict orphan offline-cache bundles once the cache outgrows a size cap.

    When `settings.cache_autopurge.enabled` and the total `.offline_cache/` size
    reaches `max_gb`, purge every orphan bundle (cache/partial dirs + legacy MP4s
    that no longer map to any live library file). Only orphans are removed —
    bundles backing current library files are never touched, so this can never
    delete something a viewer might still want. Active prep jobs are skipped
    (the same `_offline_cache_path_active` guard the manual purge uses).

    The size check rides on `_build_offline_cache_inventory`, which offloads the
    heavy recursive walk to a worker thread, so the event loop never stalls. We
    only run that walk when the feature is enabled, and only every few minutes —
    eviction is housekeeping, not something that needs to be instant.
    """
    await asyncio.sleep(45)   # let the app settle before the first (heavy) walk
    while True:
        try:
            cfg = _cache_autopurge_cfg(await get_library())
            if cfg["enabled"]:
                # force=True: eviction must act on the real current size, and
                # this refreshes the admin snapshot for free every 5 min.
                inv = await _build_offline_cache_inventory(force=True)
                cap_bytes = int(cfg["max_gb"] * (1024 ** 3))
                if inv["total_bytes"] >= cap_bytes and inv["orphans"]:
                    deleted = 0
                    freed = 0
                    for o in inv["orphans"]:
                        if _offline_cache_path_active(o["cache_key"]):
                            continue
                        b = await asyncio.to_thread(_delete_cache_artifacts, o["cache_key"])
                        if b > 0:
                            deleted += 1
                            freed += b
                    if deleted:
                        _invalidate_offline_cache_inventory()
                        state.cache_autopurge_last = {
                            "at":                 time.time(),
                            "deleted":            deleted,
                            "bytes_freed":        freed,
                            "total_bytes_before": inv["total_bytes"],
                        }
                        print(f"[cachepurge] auto-purged {deleted} orphan bundle(s), "
                              f"freed {freed / (1024 ** 3):.2f} GiB (cache was "
                              f"{inv['total_bytes'] / (1024 ** 3):.2f} GiB, cap {cfg['max_gb']:g} GiB)")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[cachepurge] cache_autopurge_loop error: {exc}")
        await asyncio.sleep(300)   # re-check every 5 min


async def scheduled_reboot_loop() -> None:
    """Daily scheduled host reboot with an idle guard.

    At the configured local time, if the machine has been idle for
    `idle_minutes`, reboot it. If it's in use, wait `idle_minutes` and re-check —
    repeating until the box is idle. A persisted `last_fired` date stops the
    just-rebooted machine from immediately re-arming and looping.
    """
    next_check = 0.0   # monotonic time of next idle re-check while armed for today
    while True:
        await asyncio.sleep(20)
        try:
            lib = await get_library()
            cfg = _scheduled_reboot_cfg(lib)
            if not cfg["enabled"]:
                continue

            parts = cfg["time"].split(":")
            try:
                target_h, target_m = int(parts[0]), int(parts[1])
            except (ValueError, IndexError):
                continue
            if not (0 <= target_h <= 23 and 0 <= target_m <= 59):
                continue

            now = _now_in_tz(cfg["timezone"])
            today = now.date().isoformat()
            if cfg["last_fired"] == today:
                continue   # already handled (or rebooted) for today

            scheduled_today = now.replace(hour=target_h, minute=target_m,
                                          second=0, microsecond=0)
            if now < scheduled_today:
                next_check = 0.0   # re-arm fresh for the upcoming window
                continue

            # We're at/past today's scheduled time and haven't fired yet.
            idle_secs = max(60, cfg["idle_minutes"] * 60)
            if asyncio.get_event_loop().time() < next_check:
                continue

            if await _machine_in_use(idle_secs):
                next_check = asyncio.get_event_loop().time() + idle_secs
                print(f"[reboot] scheduled restart deferred — machine in use; "
                      f"re-checking in {cfg['idle_minutes']} min")
                continue

            # Idle → record that we've fired for today (loop guard), then reboot.
            lib2 = await get_library()
            sr = lib2.setdefault("settings", {}).setdefault("scheduled_reboot", {})
            sr["last_fired"] = today
            await put_library(lib2)
            print(f"[reboot] scheduled restart firing — machine idle for "
                  f"{cfg['idle_minutes']} min")
            await _reboot_machine()
            # On success the process is torn down by the reboot before we reach
            # here. If it returns (no reboot permission), don't kill the loop —
            # last_fired == today now stops it re-hammering reboot commands every
            # tick, and it stands down until tomorrow's window.
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[reboot] scheduled_reboot_loop error: {exc}")


async def auto_prep_loop() -> None:
    """Drive automatic stream-prep from two independent triggers that share the
    one bulk-prep concurrency slot + pause gate:

      • **Overnight window** — runs during an admin-defined nightly window
        regardless of activity (heavy ffmpeg load when nobody's watching).
      • **Idle trigger** — runs any time the box has been idle for `idle_minutes`
        (no interaction / VLC playback / active stream / running download), and
        pauses *and discards the in-flight encode* the instant activity returns.

    Because both write `state.prep_paused`, a single loop owns the decision so the
    two triggers can't fight. The combined "should prep run now?" is:

        want = overnight_window_open OR (idle_enabled AND idle_for_threshold)

    A rising edge (`want` and not engaged) resumes + enqueues the whole un-prepped
    library; a falling edge pauses. The pause is a hard **kill** when idle-prep is
    the reason (activity returned — HLS prep can't checkpoint, so the file
    restarts from scratch later); otherwise it honours the overnight `on_end` mode
    (graceful pause vs. run-to-completion). When idle-prep is enabled its
    activity-pause overrides `on_end` — activity always wins. `auto_prep_engaged`
    is the in-memory edge flag (re-derived after any config save)."""
    while True:
        await asyncio.sleep(15)
        try:
            lib = await get_library()
            on_cfg = _overnight_prep_cfg(lib)
            id_cfg = _idle_prep_cfg(lib)

            # Overnight window membership (activity-independent).
            overnight_open = False
            if on_cfg["enabled"]:
                s_min = _hhmm_to_min(on_cfg["start"])
                e_min = _hhmm_to_min(on_cfg["end"])
                if s_min is not None and e_min is not None:
                    now = _now_in_tz(on_cfg["timezone"])
                    overnight_open = _in_overnight_window(now, s_min, e_min)

            # Idle trigger: busy within the idle window ⇒ not eligible. The same
            # `_machine_in_use` window doubles as the activity detector — a fresh
            # interaction stamps last_activity, flipping this True within a tick.
            in_use = False
            if id_cfg["enabled"]:
                # for_prep: an open dashboard counts as "in use" so idle prep won't
                # hammer the box while a viewer is on the site (even if just looking).
                in_use = await _machine_in_use(id_cfg["idle_minutes"] * 60, for_prep=True)

            # Cache for the cheap activity hook (`_activity_kick`): it pauses on
            # interaction only when idle-prep governs and we're NOT inside the
            # (intentional) overnight window.
            state.idle_prep_on = id_cfg["enabled"]
            state.overnight_open = overnight_open

            want = overnight_open or (id_cfg["enabled"] and not in_use)

            if want and not state.auto_prep_engaged:
                state.auto_prep_engaged = True
                _resume_prep()   # clear any prior pause + re-spawn paused jobs
                n = await _enqueue_library_prep()
                reason = "overnight window open" if overnight_open \
                    else f"idle {id_cfg['idle_minutes']} min"
                print(f"[autoprep] {reason} — queued {n} file(s) for stream-prep")
            elif state.auto_prep_engaged and not want:
                state.auto_prep_engaged = False
                if id_cfg["enabled"] and in_use:
                    killed = _pause_prep(kill=True)   # activity returned — discard in-flight
                    print(f"[autoprep] activity detected — pausing prep (killed {killed} in-flight)")
                elif on_cfg["on_end"] == "continue":
                    print("[autoprep] overnight window closed — continuing until prep finishes")
                else:
                    _pause_prep(kill=False)           # graceful: let the current file finish
                    print("[autoprep] overnight window closed — pausing remaining prep")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[autoprep] auto_prep_loop error: {exc}")


# ── Background Task: Auto-Updater ────────────────────────────────────────────

_updater_lock = asyncio.Lock()   # serialise check + apply against each other


async def _set_updater_phase(phase: str, message: str = "", busy: bool = False) -> None:
    """Update the in-memory updater status + broadcast a state event so the
    admin UI animates progress live."""
    state.updater_phase = phase
    state.updater_message = message
    state.updater_busy = busy
    try:
        await broadcast("state", state_snapshot())
    except Exception:
        pass


async def _persist_updater_state(**fields) -> None:
    """Merge `fields` into library.json → settings.autoupdate, preserving everything else."""
    lib = await get_library()
    au = lib.setdefault("settings", {}).setdefault("autoupdate", {})
    au.update(fields)
    await put_library(lib)


async def _run_check(branch: str, allow_any: bool = False) -> dict:
    """Wrapper around updater.check_update that records the result in library.json."""
    res = await updater.check_update(branch, allow_any=allow_any)
    status = "ok" if res.get("ok") else (res.get("error") or "error")
    await _persist_updater_state(
        last_check_at=int(time.time()),
        last_check_status=status,
    )
    return res


async def _run_apply(branch: str, reboot: bool = True, allow_any: bool = False) -> dict:
    """The full update sequence: git apply → setup.py → service reinstall →
    machine reboot.

    Steps (each gated so a failure surfaces with a stage label):
      1. **git apply** — `git switch -C <branch>` (if current branch differs)
         then `git reset --hard origin/<branch>`. Goes forwards AND backwards
         (alpha → main is the same operation as main → alpha as far as git is
         concerned; library.json / .env / .offline_cache survive because they
         are gitignored).
      2. **setup.py** — re-run non-interactively so any new deps land and the
         qBit ini / certs / .env are refreshed against the new code's
         expectations.
      3. **Service reinstall** — `daemon.uninstall()` then `daemon.install()`
         so `streamlink_service.py` (the supervisor wrapper) is regenerated
         from the new `daemon.py`'s `_WRAPPER_CONTENT`. Without this the OS
         supervisor would keep running the old wrapper code even after the
         pull. Best-effort: a failure here is logged but doesn't abort the
         reboot, since the existing service may still work on the new code.
      4. **Reboot** — the whole host. Cleanest possible state on the way back
         up; the OS service supervisor brings StreamLink back on its own.

    With `reboot=False`, steps 3 and 4 are skipped (used by the dev-mode
    "files only" toggle, and by the auto-apply loop's polling path when it
    just wants to validate that the pull + setup would succeed).
    """
    async with _updater_lock:
        if state.updater_busy:
            return {"ok": False, "stage": "busy", "message": "An update is already running."}

        await _set_updater_phase("applying", f"Applying {branch} branch…", busy=True)
        prev_commit = await updater.current_commit()
        apply_res = await updater.apply_update(branch, allow_any=allow_any)
        if not apply_res.get("ok"):
            err = apply_res.get("error") or "git update failed"
            await _set_updater_phase("error", f"git apply: {err}", busy=False)
            await _persist_updater_state(last_error=err)
            return {"ok": False, "stage": "git", "message": err}

        new_commit = apply_res.get("commit", "")
        same_commit = new_commit and new_commit == prev_commit

        await _set_updater_phase("setup", "Running setup.py (non-interactive)…", busy=True)
        setup_res = await updater.run_setup()
        state.updater_last_output = setup_res.get("output_tail", "")
        if not setup_res.get("ok"):
            base_err = setup_res.get("error") or f"setup.py exited rc={setup_res.get('returncode')}"
            # Hoist the last non-empty line of setup.py's output into the
            # phase message so the admin sees the actual cause (e.g. a
            # `UnicodeEncodeError` traceback's exception line) without having
            # to expand the collapsed diagnostic panel.
            last_line = ""
            for line in reversed(state.updater_last_output.splitlines()):
                stripped = line.strip()
                if stripped and not stripped.startswith("─"):
                    last_line = stripped
                    break
            err = f"{base_err} — {last_line}" if last_line else base_err
            await _set_updater_phase("error", err, busy=False)
            await _persist_updater_state(
                last_error=err,
                last_applied_at=int(time.time()),
                last_applied_commit=new_commit,
            )
            return {"ok": False, "stage": "setup", "message": err,
                    "output_tail": state.updater_last_output}

        await _persist_updater_state(
            last_error="",
            last_applied_at=int(time.time()),
            last_applied_commit=new_commit,
        )

        if not reboot:
            await _set_updater_phase("idle",
                                    f"Updated to {new_commit}. Reboot pending.",
                                    busy=False)
            return {"ok": True, "stage": "complete",
                    "message": f"Updated to {new_commit}.",
                    "commit": new_commit, "reboot_pending": True}

        # Step 3: refresh the supervisor wrapper script (streamlink_service.py)
        # from the freshly-pulled daemon._WRAPPER_CONTENT. This is a plain file
        # write — NO elevation required, no UAC prompt on Windows. The OS-level
        # service registration (task / plist / unit) keeps pointing to the same
        # wrapper path, so the reboot brings up the new wrapper code on its own.
        # Re-registration would require admin on Windows (UAC) and is therefore
        # incompatible with a fully-automatic update; the wrapper refresh is
        # enough for the common case.
        await _set_updater_phase("refreshing-service",
                                f"Refreshing supervisor wrapper for {new_commit}…",
                                busy=True)
        svc = await updater.refresh_service_wrapper()
        # Append the wrapper output to the diagnostic buffer alongside setup's.
        if svc.get("output"):
            tail = (state.updater_last_output + "\n── service ──\n" + svc["output"])[-8192:]
            state.updater_last_output = tail
        if not svc.get("ok"):
            # Log + persist but don't bail — proceed to reboot anyway. If the
            # wrapper write genuinely fails (read-only filesystem, missing
            # daemon.py), the existing wrapper file still runs the new code
            # because main.py / watchdog.py path references are dynamic.
            log.warning("Wrapper refresh failed; rebooting anyway. Error: %s",
                       svc.get("error", ""))
            await _persist_updater_state(
                last_error=f"wrapper refresh: {svc.get('error', 'unknown')}",
            )

        # Step 4: machine reboot. Fire after a short grace so this RPC's HTTP
        # response gets flushed to the admin UI first.
        await _set_updater_phase("rebooting",
                                f"Updated to {new_commit}. Rebooting host machine…",
                                busy=True)
        asyncio.create_task(_reboot_machine(delay=1.5))

        msg_prefix = "Updated" if not same_commit else "Branch reset"
        return {"ok": True, "stage": "reboot",
                "message": f"{msg_prefix} to {new_commit}. Host is rebooting…",
                "commit": new_commit,
                # Kept the legacy `service_reinstalled` key name so the admin
                # UI's success-toast branch doesn't have to change shape.
                "service_reinstalled": svc.get("ok", False),
                "service_install_output": svc.get("output", "")}


async def updater_loop() -> None:
    """Periodic auto-update poll.

    Reads settings.autoupdate every minute; when enabled, runs a check at
    `interval_hours` cadence and (when `auto_apply` is on) auto-applies new
    commits if the machine is currently idle. Active streams, downloads, and
    recent admin actions all defer the apply — we never tear the server down
    while the user is watching something.
    """
    last_check_mono = 0.0
    while True:
        await asyncio.sleep(60)
        try:
            lib = await get_library()
            cfg = _autoupdate_cfg(lib)
            if not cfg["enabled"]:
                continue

            interval_secs = cfg["interval_hours"] * 3600
            now_mono = asyncio.get_event_loop().time()
            last_check_wall = cfg["last_check_at"]
            wall_due = (time.time() - last_check_wall) >= interval_secs if last_check_wall else True
            mono_due = (now_mono - last_check_mono) >= interval_secs
            if not (wall_due and mono_due):
                continue

            if state.updater_busy:
                continue

            branch = cfg["branch"]
            allow_any = cfg["dev_mode"]
            async with _updater_lock:
                if state.updater_busy:
                    continue
                await _set_updater_phase("checking", f"Checking origin/{branch}…", busy=True)
                res = await _run_check(branch, allow_any=allow_any)
                last_check_mono = now_mono

            if not res.get("ok"):
                await _set_updater_phase("error", res.get("error", "check failed"), busy=False)
                continue

            if not res.get("has_update"):
                await _set_updater_phase("idle",
                                        f"Up to date with origin/{branch}.",
                                        busy=False)
                continue

            if not cfg["auto_apply"]:
                # New commits available but admin opted out of auto-apply —
                # leave the banner for them and idle.
                await _set_updater_phase("idle",
                                        f"Update available: origin/{branch} ({res['behind_by']} ahead). Apply from the admin panel.",
                                        busy=False)
                continue

            # Idle gate: never tear down a running playback for a routine update.
            if await _machine_in_use(window_secs=300):
                await _set_updater_phase("idle",
                                        f"Update queued: machine in use, will retry next cycle.",
                                        busy=False)
                continue

            print(f"[updater] auto-applying origin/{branch} "
                  f"(local={res['local']} → remote={res['remote']})")
            apply_res = await _run_apply(branch, reboot=True, allow_any=allow_any)
            if not apply_res.get("ok"):
                print(f"[updater] auto-apply failed at stage={apply_res.get('stage')}: "
                      f"{apply_res.get('message')}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[updater] updater_loop error: {exc}")


# ── Background Task: State Broadcaster ───────────────────────────────────────

async def stat_broadcaster() -> None:
    """Push a full state snapshot to all SSE clients every 2 s."""
    while True:
        if state.active_hash and state.stream_status in ("buffering", "playing"):
            info = await qbit_info(state.active_hash)
            if info:
                total = info.get("size", 1) or 1
                done = info.get("completed", 0)
                state.progress = done / total * 100
                state.downloaded_mb = done / 1_048_576
                state.total_mb = total / 1_048_576
                state.dl_speed_bps = info.get("dlspeed", 0)
                state.ul_speed_bps = info.get("upspeed", 0)
        if state.stream_status == "playing" and not state.youtube_active:
            vs = await vlc_status()
            if vs:
                state.vlc_time = int(vs.get("time", 0))
                state.vlc_duration = int(vs.get("length", 0))
                reported = round(int(vs.get("volume", 256)) / 256 * 100)
                # Self-heal: VLC occasionally snaps volume back to 100 on
                # playlist advance / track start. If it sits above the user's
                # cap, push it back down immediately rather than waiting for
                # the user to nudge the slider.
                cap = await _global_max_volume()
                if reported > cap:
                    raw = max(0, min(512, round(cap / 100 * 256)))
                    await vlc("volume", val=str(raw))
                    state.vlc_volume = cap
                else:
                    state.vlc_volume = reported
        await broadcast("state", state_snapshot())
        await asyncio.sleep(2)


async def _auto_play_item(item: dict, profile_id: str, file_path: str = "") -> None:
    """Trigger playback for a library item (or specific file) that just finished downloading."""
    try:
        all_paths = [f["path"] for f in item.get("files", [])]
        if file_path and file_path in all_paths:
            start_idx = all_paths.index(file_path)
            playlist = all_paths[start_idx:]
        else:
            hint = find_resume_hint(item, profile_id)
            if hint and hint.get("file_path") and not hint.get("all_completed"):
                try:
                    start_idx = all_paths.index(hint["file_path"])
                    playlist = all_paths[start_idx:]
                except ValueError:
                    playlist = all_paths
            else:
                playlist = all_paths

        if not playlist:
            return

        # Wait up to 10 s for files to appear on disk after qBit marks complete
        for _ in range(10):
            if all(Path(p).exists() for p in playlist):
                break
            await asyncio.sleep(1)

        first = Path(playlist[0])
        await vlc("in_play", input=first.resolve().as_uri())
        for p in playlist[1:]:
            await vlc("in_enqueue", input=Path(p).resolve().as_uri())
        asyncio.create_task(vlc_focus_and_fullscreen())

        state.stream_status = "playing"
        state.active_title = item["title"]
        state.active_file = first
        state.current_audio_track = -1
        state.current_subtitle_track = -1
        state.active_hash = item.get("torrent_hash") or None
        state.library_item_id = item["id"]
        state.library_profile_id = profile_id
        state.library_item_file_count = len(item.get("files", []))
        state.library_playlist = playlist
        state.library_current_file = playlist[0]

        await broadcast("stream_status", {"status": "playing", "message": f"Auto-playing: {first.name}"})
    except Exception:
        pass


# ── Idle Background Video ─────────────────────────────────────────────────────

async def _play_background_video() -> bool:
    """Start the configured background video on VLC. Returns True on success.

    Caller is responsible for deciding *when* to play; this just issues the
    commands and resets the AppState fields so the rest of the system treats
    the dashboard as idle (no library item, no skip offers, no resume tile).
    """
    lib = await get_library()
    bg = lib.get("settings", {}).get("background_video") or {}
    path_str = bg.get("path", "")
    if not path_str:
        return False
    p = Path(path_str)
    if not p.exists():
        return False
    cap = await _global_max_volume()
    bg_vol = max(0, min(cap, max(0, min(200, int(bg.get("volume", 50))))))
    raw = max(0, min(512, round(bg_vol / 100 * 256)))

    # Snapshot the user's normal volume on the first transition into bg, so the
    # next user-initiated `in_play` can restore it.
    if not state.background_playing:
        state.user_volume_before_bg = state.vlc_volume
    state.vlc_volume = bg_vol
    state.background_playing = True
    # Reset playback state so SSE clients show "nothing is playing"
    state.stream_status = "idle"
    state.library_item_id = None
    state.library_profile_id = None
    state.library_item_file_count = 0
    state.library_playlist = []
    state.library_current_file = None
    state.active_title = None
    state.active_file = None
    state.skip_offer = None
    state.skip_offer_file = None
    state.resume_offer = None

    try:
        c = _vlc_http()
        # Clear stale items (old episodes from the just-stopped playlist) before
        # playing the bg video — otherwise replaying a bg entry that's already in
        # the list ends and VLC auto-advances into a leftover episode. See
        # vlc_clear_playlist() / docs/GOTCHAS.md.
        await c.get("/requests/status.xml", params={"command": "pl_empty"})
        await c.get("/requests/status.xml", params={"command": "volume", "val": str(raw)})
        await c.get("/requests/status.xml", params={"command": "in_play", "input": p.resolve().as_uri()})
    except Exception:
        return False
    asyncio.create_task(vlc_focus_and_fullscreen())
    await broadcast("state", state_snapshot())
    return True


async def background_video_loop() -> None:
    """Poll every 3 s; (re)start the configured background video when VLC is idle.

    Bg is naturally replaced by `vlc("in_play", ...)` from any user-initiated
    playback, so we don't need to explicitly stop it. When VLC stops (a movie
    ends, /api/stop is called, etc.) this loop picks up the slack within ~3 s.
    """
    while True:
        await asyncio.sleep(3)
        try:
            # YouTube is playing in the browser on the TV — VLC is intentionally
            # stopped. Don't let the idle-background loop start a video over it.
            if state.youtube_active:
                continue
            lib = await get_library()
            bg = lib.get("settings", {}).get("background_video") or {}
            if not bg.get("path") or not bg.get("enabled", True):
                state.background_playing = False
                continue
            # Don't fight an in-flight stream pipeline about to hand off to VLC
            if state.stream_status == "buffering":
                continue
            vs = await vlc_status()
            if vs is None:
                continue
            vlc_state = vs.get("state", "")
            if vlc_state in ("playing", "paused"):
                continue
            await _play_background_video()
        except Exception:
            pass


async def _sync_state_from_vlc() -> None:
    """On lifespan startup, restore `state` from VLC's actual playback so SSE
    clients see the correct "now playing" title after a server restart (admin
    shutdown, watchdog kick, manual relaunch) while VLC kept running.

    Without this, every `AppState` field is at its dataclass default —
    `stream_status="idle"`, `active_title=None` — and `background_video_loop`
    sees VLC already `state=playing` so it stays out of the way, leaving the
    dashboard stuck on "No active stream" until someone calls /api/stop and
    starts a new playback.

    `library_profile_id` is intentionally left unset — the profile that
    originally started the playback isn't recoverable, so `vlc_progress_tracker`
    will skip progress saves and skip-offer logic for the restored session.
    Playback control (next/prev/stop, title display) still works.
    """
    try:
        vs = await vlc_status()
        if not vs or vs.get("state") not in ("playing", "paused"):
            return
        uri = await vlc_playlist_uri()
        if not uri or not uri.startswith("file://"):
            return
        cur_path = uri_to_path(uri)
        try:
            cur_resolved = Path(cur_path).resolve()
        except Exception:
            cur_resolved = Path(cur_path)

        lib = await get_library()
        bg_path = (lib.get("settings", {}).get("background_video") or {}).get("path", "")
        if bg_path:
            try:
                if Path(bg_path).resolve() == cur_resolved:
                    state.background_playing = True
                    return
            except Exception:
                pass

        matched_item: Optional[dict] = None
        matched_path: Optional[str] = None
        for item in lib.get("items", []):
            for f in item.get("files", []):
                stored = f.get("path", "")
                if not stored:
                    continue
                try:
                    if Path(stored).resolve() == cur_resolved:
                        matched_item = item
                        matched_path = stored
                        break
                except Exception:
                    continue
            if matched_item:
                break

        state.stream_status = "playing"
        if matched_item and matched_path:
            state.active_title = matched_item.get("title") or Path(cur_path).stem
            state.library_item_id = matched_item["id"]
            state.library_item_file_count = len(matched_item.get("files", []))
            state.active_hash = matched_item.get("torrent_hash") or None
            state.library_playlist = [
                f.get("path", "") for f in matched_item.get("files", []) if f.get("path")
            ]
            state.library_current_file = matched_path
        else:
            state.active_title = Path(cur_path).stem
            state.library_current_file = cur_path
    except Exception:
        pass


# ── Background Task: Library Download Monitor ─────────────────────────────────

async def library_download_monitor() -> None:
    """Poll qBit every 5 s for pending library downloads and mark them complete."""
    while True:
        await asyncio.sleep(5)
        try:
            lib = await get_library()
            pending = [it for it in lib["items"] if it.get("status") == "downloading"]
            state.downloading_count = len(pending)
            if not pending:
                continue
            changed = False
            for item in pending:
                h = item.get("torrent_hash")
                if not h:
                    continue
                info = await qbit_info(h)
                if not info:
                    continue

                # Refresh file list now that sizes are final
                qfiles = await qbit_files(h)
                save_path = info.get("save_path", settings.qbit_download_path)
                new_files = build_file_list(qfiles, save_path)
                if new_files:
                    item["files"] = new_files
                    item["size_bytes"] = sum(f["size_bytes"] for f in new_files)

                qstate = info.get("state", "")
                # Ready is gated on every NON-SKIP file being fully downloaded — not on
                # qBit's torrent state. With skip/idle files at priority 0, qBit reports
                # the torrent "complete" (uploading) while skipped files are absent and
                # idle-deferred files haven't fetched yet; flipping ready on that would
                # both mislabel a partial download as whole and fingerprint a missing set.
                nonskip_done = _all_nonskip_complete(item, qfiles, save_path)
                if qstate in ("error", "missingFiles"):
                    item["status"] = "error"
                    state.downloading_count = max(0, state.downloading_count - 1)
                    changed = True
                    await broadcast("library_update", {"item_id": item["id"], "status": "error"})
                elif nonskip_done:
                    item["status"] = "ready"
                    state.downloading_count = max(0, state.downloading_count - 1)
                    changed = True
                    await broadcast("library_update", {"item_id": item["id"], "status": "ready"})
                    _schedule_series_analysis_if_eligible(item, lib)
                    if state.play_when_ready_item_id == item["id"]:
                        pwr_profile = state.play_when_ready_profile_id
                        pwr_fp = state.play_when_ready_file_path
                        state.play_when_ready_item_id = None
                        state.play_when_ready_profile_id = None
                        state.play_when_ready_file_path = None
                        asyncio.create_task(_auto_play_item(item, pwr_profile or "", pwr_fp or ""))
                else:
                    # Still downloading — push live stats to the UI
                    eta = info.get("eta", 8640000)
                    cfg = _download_cfg(item)
                    # "Waiting for idle window": either qBit paused us, or all the
                    # kept-but-incomplete files are idle-deferred and the window is shut
                    # (qBit may be seeding the now-files meanwhile, so check per-file).
                    waiting_idle = qstate in ("pausedDL", "stoppedDL")
                    if not waiting_idle and not state.download_idle_open:
                        for i, qf in enumerate(qfiles):
                            full = str(Path(save_path) / qf.get("name", ""))
                            if _effective_file_mode(cfg, full) == "idle" and qf.get("progress", 0.0) < 0.999:
                                waiting_idle = True
                                break
                    await broadcast("library_progress", {
                        "item_id": item["id"],
                        "speed_bps": info.get("dlspeed", 0),
                        "downloaded_bytes": info.get("completed", 0),
                        "total_bytes": info.get("size", 0),
                        "progress_pct": round(info.get("completed", 0) / max(info.get("size", 1), 1) * 100, 1),
                        "eta_secs": eta if eta < 8640000 else -1,
                        "download_mode": cfg["mode"],   # now | idle
                        # Intentionally halted (idle window closed) — the UI shows
                        # "Waiting for idle window" instead of "Downloading".
                        "paused": waiting_idle,
                    })
                    changed = True  # file list updated
                    # Check if a specific queued file finished (even while torrent is still going)
                    if (state.play_when_ready_item_id == item["id"]
                            and state.play_when_ready_file_path and qfiles):
                        pwr_path = state.play_when_ready_file_path
                        for qf in qfiles:
                            full = str(Path(save_path) / qf.get("name", ""))
                            if full == pwr_path and qf.get("progress", 0) >= 1.0:
                                pwr_profile = state.play_when_ready_profile_id
                                state.play_when_ready_item_id = None
                                state.play_when_ready_profile_id = None
                                state.play_when_ready_file_path = None
                                asyncio.create_task(_auto_play_item(item, pwr_profile or "", pwr_path))
                                break

            if changed:
                await put_library(lib)
        except Exception:
            pass


# ── Smart Skip helpers ────────────────────────────────────────────────────────

def _series_key(item: dict) -> str:
    """Group items by series for cross-episode fingerprinting.

    Items with a non-empty series field group together; everything else is
    grouped by item ID (a single-item bucket) so movies and one-offs still
    get the credits fallback.
    """
    s = (item.get("series") or "").strip()
    return f"series:{s.lower()}" if s else f"item:{item.get('id', '')}"


def _items_for_series_key(lib: dict, key: str) -> list[dict]:
    return [it for it in lib["items"] if _series_key(it) == key]


def _skip_settings_for_profile(lib: dict, profile_id: str) -> dict:
    """Return {auto_skip_intro, auto_skip_credits} for a profile (defaults False)."""
    if not profile_id:
        return {"auto_skip_intro": False, "auto_skip_credits": False}
    prof = next((p for p in lib.get("profiles", []) if p["id"] == profile_id), None)
    if not prof:
        return {"auto_skip_intro": False, "auto_skip_credits": False}
    return {
        "auto_skip_intro":   bool(prof.get("auto_skip_intro", False)),
        "auto_skip_credits": bool(prof.get("auto_skip_credits", False)),
    }


def _next_file_in_item(item: dict, current_path: str) -> Optional[str]:
    """Return the next file path in this item after current_path, or None."""
    files = item.get("files", [])
    paths = [f.get("path", "") for f in files]
    try:
        idx = paths.index(current_path)
    except ValueError:
        return None
    return paths[idx + 1] if idx + 1 < len(paths) else None


def _find_file_meta(item: dict, file_path: str) -> Optional[dict]:
    """Return the per-file analysis dict (intro/credits_start) if present."""
    skip_data = item.get("skip_data", {})
    return skip_data.get(file_path)


def _item_skip_status(item: dict) -> str:
    """Summarize the Smart Skip availability of every file in this item.

    Drives the user-facing "Skip unavailable" chip in the library list and the
    admin Smart Skip tab's quick filter. Values:
      `none`        — no files (status surface not applicable)
      `pending`     — at least one file has no skip_data entry yet (analysis
                      hasn't run, or item still downloading)
      `failed`      — every analyzed file is marked failed (no intro AND no
                      credits AND analysis.source == "failed")
      `partial`     — some files succeeded, others failed
      `ok`          — every file produced usable intro/credits (or has been
                      manually edited)
    """
    files = _analyzable_files(item)   # skipped (not-downloaded) files aren't analyzed
    if not files:
        return "none"
    skip_data = item.get("skip_data", {}) or {}
    ok = 0
    fail = 0
    pending = 0
    for f in files:
        path = f.get("path", "")
        entry = skip_data.get(path)
        if not entry:
            pending += 1
            continue
        src = (entry.get("analysis") or {}).get("source", "")
        if src == "failed":
            fail += 1
        else:
            ok += 1
    if pending and not fail and not ok:
        return "pending"
    if fail and not ok:
        return "failed"
    if fail and ok:
        return "partial"
    if pending:
        # Some succeeded, some not yet analyzed — surface as partial so the UI
        # still hints that not everything is covered.
        return "partial"
    return "ok"


async def _set_analysis_status(series_key: str, **patch) -> None:
    """Update state.analysis_jobs[series_key] and broadcast the change."""
    job = state.analysis_jobs.setdefault(series_key, {})
    job.update(patch)
    await broadcast("analysis_status", {"series_key": series_key, "job": job})


def _log_analyzer_event(*, level: str, message: str, series_key: str = "",
                        item_id: str = "", file_path: str = "",
                        error_code: str = "") -> None:
    """Append a Smart Skip event to the analyzer ring buffer + app log.

    The ring buffer is the source of truth for the admin "Smart Skip" tab's
    log panel (state lives in-memory and resets on restart — failures are
    re-discovered on the next analysis run, so persistence isn't worth the
    library.json bloat). `streamlink_app.log` mirrors the message for
    longer-term forensics.
    """
    entry = {
        "ts":         _now_iso(),
        "level":      level,
        "series_key": series_key,
        "item_id":    item_id,
        "file_path":  file_path,
        "error_code": error_code,
        "message":    message,
    }
    state.analyzer_log.appendleft(entry)
    fname = Path(file_path).name if file_path else ""
    code = f" [{error_code}]" if error_code else ""
    log_fn = log.error if level == "error" else (log.warning if level == "warn" else log.info)
    log_fn(f"[analyzer]{code} {fname}: {message}")


async def _run_series_analysis(series_key: str) -> None:
    """Background task: analyze a series, save results, broadcast progress.

    `analyzer.analyze_series` now always returns an entry per file (success or
    failure), so the orchestrator persists everything it gets and uses
    `analysis.source == "failed"` to drive the admin log + user-facing chip.
    Missing-binary and exception cases are still surfaced as a series-level
    `analysis_jobs.status = "failed"` for the admin UI.
    """
    lock = analyzer.lock_for_series(series_key)
    async with lock:
        lib = await get_library()
        items = _items_for_series_key(lib, series_key)
        ready_items = [it for it in items if it.get("status") == "ready"]
        item_ids = [it["id"] for it in ready_items]
        if not ready_items:
            return

        # Start
        await _set_analysis_status(
            series_key, status="running",
            stage="starting", current=0, total=0,
            message="Preparing analysis…",
            item_ids=item_ids,
            started_at=_now_iso(),
            finished_at=None,
        )

        async def _on_progress(**kw):
            await _set_analysis_status(series_key, status="running", **kw)

        # Fingerprint only the files the user actually downloaded — exclude "skip"
        # files (deselected from the download) so a partial selection isn't dragged
        # to "failed" by absent files. The persistence loop's `p in results` guard
        # then naturally leaves skip files' skip_data untouched.
        analyze_items = [dict(it, files=_analyzable_files(it)) for it in ready_items]
        try:
            results = await analyzer.analyze_series(analyze_items, progress_cb=_on_progress)
        except Exception as exc:
            err_msg = f"Analysis crashed: {exc}"
            # Record an exception entry for every file in the series so the
            # user-facing chip + admin editor still surface the failure even
            # when nothing else gets persisted.
            results = {}
            for it in ready_items:
                for f in _analyzable_files(it):
                    p = f.get("path", "")
                    if p:
                        results[p] = {
                            "intro": None, "credits_start": None,
                            "analysis": {
                                "version":    analyzer.ANALYZER_VERSION,
                                "source":     "failed",
                                "error_code": analyzer.ERR_EXCEPTION,
                                "error":      err_msg,
                            },
                        }
            _log_analyzer_event(
                level="error", series_key=series_key,
                error_code=analyzer.ERR_EXCEPTION, message=err_msg,
            )
            await _set_analysis_status(
                series_key, status="failed",
                stage="error", message=err_msg,
                finished_at=_now_iso(),
            )
            # fall through to persistence so per-file failures still get recorded

        # Persist results back into library.json under each item.
        files_updated = 0
        files_failed = 0
        lib = await get_library()
        for it in lib["items"]:
            if _series_key(it) != series_key:
                continue
            skip_data = it.setdefault("skip_data", {})
            changed = False
            for f in it.get("files", []):
                p = f.get("path", "")
                if p in results:
                    skip_data[p] = results[p]
                    files_updated += 1
                    changed = True
                    ana = results[p].get("analysis") or {}
                    if ana.get("source") == "failed":
                        files_failed += 1
                        _log_analyzer_event(
                            level="error", series_key=series_key,
                            item_id=it["id"], file_path=p,
                            error_code=ana.get("error_code", ""),
                            message=ana.get("error", "Fingerprinting failed."),
                        )
            if changed:
                await broadcast("library_update", {"item_id": it["id"], "status": it.get("status", "ready")})
        await put_library(lib)

        # If the analyze_series call already set the job to "failed" (exception
        # path), don't overwrite it back to complete.
        job_now = state.analysis_jobs.get(series_key) or {}
        if job_now.get("status") != "failed":
            successes = files_updated - files_failed
            if files_updated == 0:
                await _set_analysis_status(
                    series_key, status="failed",
                    stage="error", message="No analyzable episodes found",
                    finished_at=_now_iso(),
                )
            elif files_failed and not successes:
                await _set_analysis_status(
                    series_key, status="failed",
                    stage="error",
                    message=f"All {files_failed} file(s) failed fingerprinting",
                    current=files_updated, total=files_updated,
                    finished_at=_now_iso(),
                )
            else:
                msg = f"Updated {successes} file(s)"
                if files_failed:
                    msg += f", {files_failed} failed"
                await _set_analysis_status(
                    series_key, status="complete",
                    stage="done", message=msg,
                    current=files_updated, total=files_updated,
                    finished_at=_now_iso(),
                )


def _schedule_series_analysis_if_eligible(item: dict, lib: dict) -> None:
    """When an item flips to 'ready', kick off series analysis if 2+ ready episodes
    exist in the same series and no analysis has been run yet for this series.
    Single-item buckets still run (to populate the credits fallback)."""
    if not analyzer.is_available():
        return
    key = _series_key(item)
    peers = [it for it in _items_for_series_key(lib, key) if it.get("status") == "ready"]

    def _needs_reanalysis(file_data: Optional[dict]) -> bool:
        if not file_data:
            return True
        analysis = file_data.get("analysis") or {}
        if analysis.get("source") == "manual":
            return False
        # Previously-failed files retry whenever a peer in the series flips to
        # ready — a new sibling can unlock a cluster that wasn't possible before.
        if analysis.get("source") == "failed":
            return True
        return analysis.get("version", 0) < analyzer.ANALYZER_VERSION

    needs_run = False
    for peer in peers:
        sk = peer.get("skip_data", {})
        for f in _analyzable_files(peer):   # skip-moded files are never fingerprinted
            if _needs_reanalysis(sk.get(f.get("path", ""))):
                needs_run = True
                break
        if needs_run:
            break
    if needs_run:
        asyncio.create_task(_run_series_analysis(key))


# Pre-roll: show the skip button this many seconds before the range starts so
# the user has visual time to react when entering the window.
SKIP_PREROLL_SEC = 2.0

# Auto-skip countdown lengths (seconds). When a profile has auto-skip enabled we
# warn the viewer on the TV before acting instead of cutting immediately.
SKIP_COUNTDOWN_INTRO_SEC = 5
SKIP_COUNTDOWN_CREDITS_SEC = 10


async def _vlc_marquee(text: str) -> None:
    """Show `text` on the TV (or clear it when empty). Offloads the file I/O."""
    await asyncio.to_thread(_marquee_write, text)


async def _cancel_skip_countdown() -> None:
    """Stop any in-flight auto-skip countdown and wipe the on-screen popup."""
    t = state.skip_countdown_task
    state.skip_countdown_task = None
    state.skip_countdown = None
    if t and not t.done():
        t.cancel()
    await _vlc_marquee("")


def _start_skip_countdown(
    kind: str, item: dict, file_path: str,
    end_at: float, target: float, lead: int,
) -> None:
    """Kick off the auto-skip countdown task (no-op if one is already running).

    `target` is the playback position to skip AT; `lead` is how many seconds of
    countdown precede it. The skip fires when playback reaches `target`.
    """
    if state.skip_countdown_task and not state.skip_countdown_task.done():
        return
    state.skip_countdown = {"type": kind, "file_path": file_path, "n": lead}
    state.skip_countdown_task = asyncio.create_task(
        _run_skip_countdown(kind, item, file_path, end_at, target, lead)
    )


async def _run_skip_countdown(
    kind: str, item: dict, file_path: str,
    end_at: float, target: float, lead: int,
) -> None:
    """Count down to `target` on the TV, then perform the skip.

    Position-driven: the displayed number is `ceil(target - pos)`, so it tracks
    real playback — it freezes while paused and grows/shrinks as the viewer
    seeks. The skip fires once `pos >= target` (intro start → seek past the
    whole intro; credits start → advance to the next episode, else stop), so the
    intro/credits is skipped in full after the warning. Aborts (clearing the
    popup) if the file changes, the viewer seeks well before the countdown
    window, or — for an intro — seeks past the intro end.
    """
    label = "intro" if kind == "intro" else "credits"
    intro_end = end_at if kind == "intro" else None
    last_n: Optional[int] = None
    try:
        while True:
            vs = await vlc_status()
            vstate = (vs or {}).get("state")
            # Playback ended or file changed under us → drop the countdown.
            if vstate in (None, "stopped") or state.library_current_file != file_path:
                return
            pos = float((vs or {}).get("time", 0) or 0)
            # Seeked clean past the intro → nothing left to skip.
            if intro_end is not None and pos >= intro_end:
                return
            remaining = target - pos
            # Seeked back well before the countdown window → drop it.
            if remaining > lead + SKIP_PREROLL_SEC:
                return
            if vstate == "paused":
                # Hold (and don't fire) while paused; pos is frozen anyway.
                await asyncio.sleep(0.5)
                continue
            if remaining <= 0:
                break  # reached the skip point
            n = max(1, min(lead, math.ceil(remaining)))
            if n != last_n:
                last_n = n
                state.skip_countdown = {"type": kind, "file_path": file_path, "n": n}
                await _vlc_marquee(f"Skipping {label} in {n}")
                await broadcast("state", state_snapshot())
            await asyncio.sleep(0.5)

        # Final guard against VLC having already advanced past this file.
        cur_uri = await vlc_playlist_uri()
        if cur_uri and cur_uri.startswith("file://"):
            if Path(uri_to_path(cur_uri)).resolve() != Path(file_path).resolve():
                return

        if kind == "intro":
            state.skip_offer_file = f"{file_path}#intro-done"
            await vlc("seek", val=str(int(end_at) + 1))
        else:
            state.skip_offer_file = f"{file_path}#credits-done"
            next_path = _next_file_in_item(item, file_path)
            if next_path and Path(next_path).exists():
                await vlc_next_file(file_path, item)
            else:
                await vlc("pl_stop")
        await broadcast("state", state_snapshot())
    finally:
        state.skip_countdown = None
        await _vlc_marquee("")


async def _maybe_emit_skip_offer(
    item: dict, file_path: str, meta: Optional[dict],
    prefs: dict, pos_sec: float, dur_sec: float,
) -> None:
    """Set or clear state.skip_offer based on current playback position.

    Auto-skip behavior: if the profile has auto_skip_* enabled, this helper
    starts an on-TV countdown (`_start_skip_countdown`) that fires the skip when
    playback reaches the intro/credits start — it does NOT show the dashboard
    offer. Manual (auto-skip off) still shows the Skip button as before.
    """
    if not meta:
        await _clear_skip_offer(file_path)
        return

    # A countdown owns the screen while it runs — don't fight it with offers.
    if state.skip_countdown_task and not state.skip_countdown_task.done():
        return

    # Intro window
    intro = meta.get("intro")
    if intro and intro.get("end", 0) > intro.get("start", 0):
        start = float(intro.get("start", 0))
        end   = float(intro.get("end",   0))
        if prefs.get("auto_skip_intro"):
            # Auto path: run the countdown over the `lead` seconds *before* the
            # intro and skip the whole intro the moment it starts. Trigger once
            # per file; the countdown task owns the screen from there.
            if ((start - SKIP_COUNTDOWN_INTRO_SEC) <= pos_sec < end
                    and state.skip_offer_file != f"{file_path}#intro-done"):
                state.skip_offer = None
                _start_skip_countdown(
                    "intro", item, file_path, end, start, SKIP_COUNTDOWN_INTRO_SEC,
                )
                await broadcast("state", state_snapshot())
                return
        elif (start - SKIP_PREROLL_SEC) <= pos_sec < end:
            # Manual path: show the Skip button across [start - PREROLL, end].
            offer = {"type": "intro", "end_at": round(end, 1), "file_path": file_path}
            if state.skip_offer != offer:
                state.skip_offer = offer
                state.skip_offer_file = file_path
                await broadcast("state", state_snapshot())
            return
        elif pos_sec >= end and state.skip_offer and state.skip_offer.get("type") == "intro":
            await _clear_skip_offer(file_path)

    # Credits window
    credits_start = meta.get("credits_start")
    if credits_start and pos_sec < dur_sec - 1:
        cs = float(credits_start)
        if prefs.get("auto_skip_credits"):
            # Auto path: run the countdown over the `lead` seconds *before* the
            # credits, then advance at credits_start (credits skipped in full).
            # Trigger once per file; the countdown task owns the screen from there.
            if (pos_sec >= cs - SKIP_COUNTDOWN_CREDITS_SEC
                    and state.skip_offer_file != f"{file_path}#credits-done"):
                state.skip_offer = None
                _start_skip_countdown(
                    "credits", item, file_path, 0.0, cs, SKIP_COUNTDOWN_CREDITS_SEC,
                )
                await broadcast("state", state_snapshot())
                return
        elif pos_sec >= cs - SKIP_PREROLL_SEC:
            # Manual path: show the Skip button from [credits_start - PREROLL, end).
            next_path = _next_file_in_item(item, file_path)
            next_exists = bool(next_path) and Path(next_path).exists()
            offer = {
                "type": "credits",
                "credits_start": round(cs, 1),
                "file_path": file_path,
                "has_next": next_exists,
                "next_file_path": next_path if next_exists else None,
            }
            if state.skip_offer != offer:
                state.skip_offer = offer
                state.skip_offer_file = file_path
                await broadcast("state", state_snapshot())
            return

    # Outside any window — clear if one was set for this file
    await _clear_skip_offer(file_path)


async def _clear_skip_offer(file_path: str) -> None:
    if state.skip_offer is not None and state.skip_offer.get("file_path") == file_path:
        state.skip_offer = None
        # don't reset skip_offer_file — it carries the done marker
        await broadcast("state", state_snapshot())
    elif state.skip_offer is not None:
        # Different file (e.g. user advanced manually) — drop the offer
        state.skip_offer = None
        await broadcast("state", state_snapshot())


async def vlc_next_file(current_file: str, item: dict) -> None:
    """Internal helper: advance VLC to the next file in this item's playlist."""
    next_path = _next_file_in_item(item, current_file)
    if not next_path or not Path(next_path).exists():
        return
    all_paths = [f.get("path", "") for f in item.get("files", [])]
    try:
        idx = all_paths.index(next_path)
        new_tail = all_paths[idx:]
    except ValueError:
        new_tail = [next_path]
    state.library_playlist = new_tail
    state.library_current_file = next_path
    state.current_audio_track = -1
    state.current_subtitle_track = -1
    state.track_pref_applied_file = next_path
    await vlc_clear_playlist()
    await vlc("in_play", input=Path(next_path).resolve().as_uri())
    for p in new_tail[1:]:
        await vlc("in_enqueue", input=Path(p).resolve().as_uri())
    if state.library_item_id and state.library_profile_id:
        asyncio.create_task(_apply_track_prefs(
            state.library_item_id, state.library_profile_id, next_path, delay=2.0,
        ))


# ── Background Task: VLC Progress Tracker ────────────────────────────────────

async def vlc_progress_tracker() -> None:
    """Save watch progress + manage Smart Skip offers. Two cadences:

    - skip-offer detection runs every 2 s while a library item is playing
    - progress save runs every 15 s
    """
    last_progress_save = 0.0
    while True:
        await asyncio.sleep(2)
        if not state.library_item_id or not state.library_profile_id:
            # Clear any stale skip/resume offers when playback ends
            if state.skip_countdown_task and not state.skip_countdown_task.done():
                await _cancel_skip_countdown()
            changed = False
            if state.skip_offer is not None:
                state.skip_offer = None
                state.skip_offer_file = None
                changed = True
            if state.resume_offer is not None:
                state.resume_offer = None
                changed = True
            if changed:
                await broadcast("state", state_snapshot())
            continue
        try:
            vs = await vlc_status()
            if not vs:
                continue
            pos_sec = float(vs.get("time", 0))
            dur_sec = float(vs.get("length", 0))
            if dur_sec < 10:
                continue

            # Resolve which file is playing and look up its skip metadata
            current_uri = await vlc_playlist_uri()
            if current_uri and current_uri.startswith("file://"):
                state.library_current_file = uri_to_path(current_uri)
            cur_file = state.library_current_file
            if cur_file:
                lib_q = await get_library()
                item_q = next((it for it in lib_q["items"] if it["id"] == state.library_item_id), None)
                if item_q:
                    cur_file = _canonical_item_path(cur_file, item_q)
                    state.library_current_file = cur_file
                    meta = _find_file_meta(item_q, cur_file)
                    prefs = _skip_settings_for_profile(lib_q, state.library_profile_id)
                    await _maybe_emit_skip_offer(item_q, cur_file, meta, prefs, pos_sec, dur_sec)

            now = asyncio.get_event_loop().time()
            if now - last_progress_save < 15:
                continue
            last_progress_save = now

            current_file = state.library_current_file
            if not current_file:
                continue

            # Apply saved track prefs when VLC advances to a new episode
            if (current_file != state.track_pref_applied_file and
                    state.library_item_id and state.library_profile_id):
                state.track_pref_applied_file = current_file
                asyncio.create_task(_apply_track_prefs(
                    state.library_item_id, state.library_profile_id, current_file, delay=2.0,
                ))

            lib = await get_library()
            item = next((it for it in lib["items"] if it["id"] == state.library_item_id), None)
            if not item:
                continue

            pct = pos_sec / dur_sec
            prof_prog = item.setdefault("progress", {}).setdefault(state.library_profile_id, {})
            prof_prog["last_file"] = current_file
            prof_prog.setdefault("file_progress", {})[current_file] = {
                "position_sec": round(pos_sec, 1),
                "duration_sec": round(dur_sec, 1),
                "completed": pct > 0.92,
                "updated_at": _now_iso(),
            }
            await put_library(lib)

            await broadcast("progress_saved", {
                "item_id": state.library_item_id,
                "profile_id": state.library_profile_id,
                "file_path": current_file,
                "episode_name": Path(current_file).name,
                "position_sec": pos_sec,
                "duration_sec": dur_sec,
                "pct": round(pct * 100, 1),
            })
        except Exception:
            pass


# ── Stream Pipeline (stream-now, torrent auto-deleted on stop) ────────────────

async def stream_pipeline(
    magnet: str,
    title: str,
    file_index: Optional[int] = None,
    torrent_hash: Optional[str] = None,
) -> None:
    try:
        state.active_title = title
        state.stream_status = "buffering"
        state.current_audio_track = -1
        state.current_subtitle_track = -1
        await broadcast("stream_status", {"status": "buffering", "message": "Adding torrent to qBittorrent…"})

        if torrent_hash:
            # Torrent already added by /stream/prepare — use existing hash directly
            h = torrent_hash
            state.active_hash = h
        else:
            h = await qbit_add_magnet(magnet, sequential=True)
            if not h:
                raise RuntimeError("qBittorrent rejected the magnet (is it running on port 8081?)")
            state.active_hash = h
            for _ in range(30):
                await asyncio.sleep(1)
                if await qbit_info(h):
                    break
            else:
                raise RuntimeError("Torrent did not appear in qBittorrent after 30 s.")

        await qbit_streaming_mode(h)

        if file_index is not None:
            # Skip all files except the selected one
            all_files = await qbit_files(h)
            skip_ids = [f.get("index", i) for i, f in enumerate(all_files)
                        if f.get("index", i) != file_index]
            if skip_ids:
                await qbit_set_file_priority(h, skip_ids, 0)
            await broadcast("stream_status", {
                "status": "buffering", "message": "File selected. Buffering first pieces…",
            })
        else:
            await broadcast("stream_status", {
                "status": "buffering", "message": "Sequential mode set. Buffering first pieces…",
            })

        # Buffer loop — track per-file progress when a specific file is selected
        while True:
            if file_index is not None:
                file_list = await qbit_files(h)
                target = _file_by_index(file_list, file_index)
                info = await qbit_info(h)
                if target and info:
                    f_size = target.get("size", 1) or 1
                    f_prog = target.get("progress", 0)
                    mb = f_size * f_prog / 1_048_576
                    pct = f_prog * 100
                    total_mb = f_size / 1_048_576
                    state.progress = pct
                    state.downloaded_mb = mb
                    state.total_mb = total_mb
                    state.dl_speed_bps = info.get("dlspeed", 0)
                    state.ul_speed_bps = info.get("upspeed", 0)
                    await broadcast("stream_status", {
                        "status": "buffering",
                        "message": f"Buffering {mb:.1f} MB / {total_mb:.1f} MB ({pct:.1f}%)",
                        "progress": pct, "downloaded_mb": mb, "total_mb": total_mb,
                        "dl_speed_bps": state.dl_speed_bps, "ul_speed_bps": state.ul_speed_bps,
                    })
                    if mb >= settings.buffer_min_mb or pct >= settings.buffer_min_pct:
                        break
            else:
                info = await qbit_info(h)
                if info:
                    total = info.get("size", 1) or 1
                    done = info.get("completed", 0)
                    pct = done / total * 100
                    mb = done / 1_048_576
                    state.progress = pct
                    state.downloaded_mb = mb
                    state.total_mb = total / 1_048_576
                    state.dl_speed_bps = info.get("dlspeed", 0)
                    state.ul_speed_bps = info.get("upspeed", 0)
                    await broadcast("stream_status", {
                        "status": "buffering",
                        "message": f"Buffering {mb:.1f} MB / {state.total_mb:.1f} MB ({pct:.1f}%)",
                        "progress": pct, "downloaded_mb": mb, "total_mb": state.total_mb,
                        "dl_speed_bps": state.dl_speed_bps, "ul_speed_bps": state.ul_speed_bps,
                    })
                    if mb >= settings.buffer_min_mb or pct >= settings.buffer_min_pct:
                        break
            await asyncio.sleep(1)

        # Resolve the file to play
        files = await qbit_files(h)
        vid = _file_by_index(files, file_index) if file_index is not None else largest_video(files)
        if not vid:
            raise RuntimeError("No recognisable video file found in torrent.")

        info = await qbit_info(h)
        save_path = (info or {}).get("save_path", settings.qbit_download_path)
        file_path = Path(save_path) / vid["name"]
        state.active_file = file_path

        await vlc_clear_playlist()
        await vlc("in_play", input=file_path.resolve().as_uri())
        asyncio.create_task(vlc_focus_and_fullscreen())
        state.stream_status = "playing"
        await broadcast("stream_status", {"status": "playing", "message": f"Playing: {file_path.name}"})

    except asyncio.CancelledError:
        pass
    except Exception as e:
        state.stream_status = "error"
        await broadcast("stream_status", {"status": "error", "message": str(e)})


# ── Library Download Pipeline (keep file, no auto-delete) ─────────────────────

async def library_download_pipeline(
    item_id: str,
    magnet: str,
    save_path: str = "",
    torrent_hash: str = "",
    selected_file_indices: Optional[list[int]] = None,
    download_mode: str = "now",
) -> None:
    """Add magnet to qBit for a full download; no streaming mode, never auto-deleted."""
    try:
        if torrent_hash:
            h = torrent_hash
        else:
            h = await qbit_add_magnet(magnet, save_path=save_path or None)
            if not h:
                lib = await get_library()
                for it in lib["items"]:
                    if it["id"] == item_id:
                        it["status"] = "error"
                        break
                await put_library(lib)
                await broadcast("library_update", {"item_id": item_id, "status": "error"})
                return

        lib = await get_library()
        for it in lib["items"]:
            if it["id"] == item_id:
                it["torrent_hash"] = h
                break
        await put_library(lib)

        # Wait for torrent metadata to appear, then build the file list
        for _ in range(30):
            await asyncio.sleep(2)
            info = await qbit_info(h)
            if info:
                break

        if info := await qbit_info(h):
            save_path = info.get("save_path", settings.qbit_download_path)
            qfiles = await qbit_files(h)
            files = build_file_list(qfiles, save_path)

            # Record the download schedule in the item model (the single source of
            # truth) and let _reconcile_item_downloads apply qBit priorities + pause.
            # Non-selected files become "skip" so they never download; the rest
            # inherit the item mode (now/idle). The scheduler reconciles every 15 s
            # too, but we apply once here so gating takes effect immediately.
            file_modes: dict[str, str] = {}
            if selected_file_indices:
                selected_set = set(selected_file_indices)
                for i, qf in enumerate(qfiles):
                    full = str(Path(save_path) / qf.get("name", ""))
                    if qf.get("index", i) not in selected_set:
                        file_modes[full] = "skip"

            lib = await get_library()
            target = None
            for it in lib["items"]:
                if it["id"] == item_id:
                    it["files"] = files
                    it["size_bytes"] = info.get("size", 0)
                    it["download"] = {
                        "mode": download_mode if download_mode in ("now", "idle") else "now",
                        "files": file_modes,
                    }
                    target = it
                    break
            await put_library(lib)
            if target is not None:
                idle_open = await _download_idle_open(lib)
                await _reconcile_item_downloads(target, idle_open)

        await broadcast("library_update", {"item_id": item_id, "status": "downloading"})

    except Exception as exc:
        await broadcast("library_update", {"item_id": item_id, "status": "error", "message": str(exc)})


# ── Background Task: late-subtitle upgrade (TV/VLC) ──────────────────────────

async def subtitle_upgrade_loop() -> None:
    """Swap an auto-applied AI subtitle for a real one once it finishes downloading.

    Real `.srt` sidecars routinely arrive *after* the video (streaming/sequential
    download) and discovery happens in waves, so the playback policy often lands
    on an AI sub first. While VLC is playing a library file whose subtitle is a
    *system-auto-applied* AI track (`state.sub_auto_ai_path` set) and the admin
    has `upgrade_late_subs` on, re-scan for a real preferred-language sidecar and
    switch to it, then notify the dashboard. A manual pick clears the marker, so
    we never override a deliberate choice. Runs only on the VLC path; the
    on-device player polls for the same upgrade itself (it isn't server-driven)."""
    while True:
        try:
            await asyncio.sleep(20)
            if not (state.subtitle_upgrade_late and state.sub_auto_ai_path
                    and state.library_item_id):
                continue
            video = await _current_playback_path()
            if not video or not video.exists():
                continue
            lib = await get_library()
            subs = _subs_cfg(lib)
            pref = subs["default_language"]
            # Cheap disk pre-check: is there a real (non-AI) preferred-language
            # sidecar present yet? Skip the heavier VLC re-scan until there is.
            files = await asyncio.to_thread(_discover_local_subs, video)
            real_files = [f for f in files if not _is_ai_sub_file(f)]
            has_pref = (any(_parse_sub_lang(f.name) == pref for f in real_files) if pref
                        else bool(real_files))
            if not has_pref and not (subs["single_option"] and len(real_files) == 1):
                continue
            tracks = await _load_all_local_subs(video)
            real = [t for t in tracks if not t.get("ai")]
            match = next((t for t in real if t["lang"] == pref), None) if pref else (real[0] if real else None)
            if match is None and subs["single_option"] and len(real) == 1:
                match = real[0]
            if match is None:
                continue                       # no real sub yet — keep watching
            state.current_subtitle_track = match["id"]
            state.sub_auto_ai_path = ""        # upgraded — stop watching this file
            await vlc("subtitle_track", val=str(match["id"]))
            lang = (match.get("lang") or pref or "").upper()
            await broadcast("subtitle_upgraded", {
                "lang": match.get("lang") or pref,
                "label": f"Switched to downloaded {lang + ' ' if lang else ''}subtitles".strip(),
            })
        except asyncio.CancelledError:
            raise
        except Exception:
            pass


# ── FastAPI App ───────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator:
    global qbit, vlc_client, _lib_lock, _jackett_cookie_lock
    # Make StreamLink's request handling the box's top priority before anything
    # else — so controls / UI / VLC-control stay responsive under heavy prep load.
    _raise_own_priority()
    _lib_lock = asyncio.Lock()
    _jackett_cookie_lock = asyncio.Lock()
    qbit = httpx.AsyncClient(timeout=10.0)
    _vlc_http()   # build the persistent keep-alive VLC client up front
    await qbit_login()
    Path(settings.qbit_download_path).mkdir(parents=True, exist_ok=True)
    _marquee_write("")  # wipe any stale countdown text from a prior run

    # Seed the night-mode flag + intensity preset from the persisted settings so
    # the snapshot + any mid-session VLC relaunch (retry / restart) reflect them.
    # run.py / watchdog.py read the same settings independently at launch.
    try:
        _lib0 = await get_library()
        _nm = _lib0.get("settings", {})
        state.vlc_night_mode = bool(_nm.get("vlc_night_mode", False))
        state.vlc_night_mode_preset = _night_mode_preset(_nm.get("vlc_night_mode_preset"))
        _subs0 = _subs_cfg(_lib0)
        state.subtitle_default_language = _subs0["default_language"]
        state.subtitle_upgrade_late = _subs0["upgrade_late_subs"]
        state.subtitle_single_option = _subs0["single_option"]
    except Exception:
        state.vlc_night_mode = False
        state.vlc_night_mode_preset = NIGHT_MODE_DEFAULT_PRESET

    # Apply the configured startup volume — a % of the admin cap (default 50%,
    # i.e. half-max). Without this, state.vlc_volume starts at 100 (the dataclass
    # default) which can blast if the cap is low.
    _startup_vol = round((await _global_max_volume()) * (await _global_vlc_start_volume_pct()) / 100)
    state.vlc_volume = _startup_vol
    state.user_volume_before_bg = _startup_vol
    _startup_raw = max(0, min(512, round(_startup_vol / 100 * 256)))
    await vlc("volume", val=str(_startup_raw))

    # If uvicorn was just restarted (admin shutdown, watchdog, manual relaunch)
    # while VLC kept playing, seed state from VLC so the dashboard doesn't sit
    # at "No active stream" until someone presses Stop.
    await _sync_state_from_vlc()

    guard       = asyncio.create_task(vpn_guard())
    broadcaster = asyncio.create_task(stat_broadcaster())
    dl_monitor  = asyncio.create_task(library_download_monitor())
    vlc_tracker = asyncio.create_task(vlc_progress_tracker())
    bg_loop     = asyncio.create_task(background_video_loop())
    jackett_mon = asyncio.create_task(jackett_health_monitor())
    reboot_loop = asyncio.create_task(scheduled_reboot_loop())
    autoprep_loop = asyncio.create_task(auto_prep_loop())
    update_loop = asyncio.create_task(updater_loop())
    dlsched_loop = asyncio.create_task(download_scheduler_loop())
    sysmon_loop = asyncio.create_task(system_monitor_loop())
    cachepurge_loop = asyncio.create_task(cache_autopurge_loop())
    subupgrade_loop = asyncio.create_task(subtitle_upgrade_loop())
    od_reaper_loop  = asyncio.create_task(_od_reaper())

    yield

    for t in (guard, broadcaster, dl_monitor, vlc_tracker, bg_loop, jackett_mon,
              reboot_loop, autoprep_loop, update_loop, dlsched_loop, sysmon_loop,
              cachepurge_loop, subupgrade_loop, od_reaper_loop):
        t.cancel()
    if state.stream_task and not state.stream_task.done():
        state.stream_task.cancel()
    await qbit.aclose()
    if vlc_client is not None:
        await vlc_client.aclose()


app = FastAPI(title="P2P StreamLink", version="2.0", lifespan=lifespan)


@app.middleware("http")
async def admin_https_redirect(request: Request, call_next):
    """Redirect /admin and /api/admin/* to HTTPS when accessed over plain HTTP.

    Honours `X-Forwarded-Proto` / `X-Forwarded-Host` so requests arriving via
    the port-443 reverse proxy (see https_proxy.py) are recognised as already
    HTTPS — without this, every admin hit through the proxy would redirect to
    the upstream's `127.0.0.1` host on https, breaking access for the real
    client and looping.
    """
    path = request.url.path
    if (path == "/admin" or path.startswith("/admin/") or path.startswith("/api/admin")):
        proto = (request.headers.get("x-forwarded-proto") or request.url.scheme).lower()
        if proto == "http":
            host = request.headers.get("x-forwarded-host") or request.url.hostname
            qs   = ("?" + request.url.query) if request.url.query else ""
            return RedirectResponse(f"https://{host}{path}{qs}", status_code=301)
    return await call_next(request)


# Endpoints that fire on a timer regardless of whether a human is present — they
# must NOT count as "usage" for the scheduled-reboot idle check.
_ACTIVITY_IGNORE_PATHS = (
    "/api/admin/scheduled-reboot",
    "/api/admin/reboot",
    "/api/admin/shutdown",
    # Updater config + status pollers — the admin Updates tab refreshes its
    # banner every few seconds; that's not "the user is watching", so it
    # mustn't keep the scheduled-reboot loop on the bench.
    "/api/admin/updater",
)


@app.middleware("http")
async def track_activity(request: Request, call_next):
    """Stamp state.last_activity on user-initiated interactions so the scheduled
    reboot can tell whether the box is idle. Mutating verbs (POST/PUT/PATCH/
    DELETE) and search GETs are treated as real usage; routine GET polling
    (state/events/version/prep-status) is not, so it never blocks a reboot."""
    method = request.method
    path = request.url.path
    if (method in ("POST", "PUT", "PATCH", "DELETE") or path == "/api/search") \
            and path not in _ACTIVITY_IGNORE_PATHS:
        state.last_activity = time.time()
        # Shed heavy background work the instant a user shows up (don't wait for the
        # 15 s auto-prep tick) — see _activity_kick. Cheap + idempotent once paused.
        try:
            _activity_kick()
        except Exception:
            pass
    return await call_next(request)


# ── Request Models ────────────────────────────────────────────────────────────

class StreamPrepareReq(BaseModel):
    magnet: str
    title: str


class StreamReq(BaseModel):
    magnet: str
    title: str
    file_index: Optional[int] = None     # specific file to stream (None = largest video)
    torrent_hash: Optional[str] = None   # hash from /stream/prepare (skips re-adding)


class DownloadReq(BaseModel):
    magnet: str
    title: str
    series: str = ""
    season: int = 0
    episode: int = 0
    save_path: str = ""
    torrent_hash: str = ""          # pre-added hash from /api/library/prepare
    selected_file_indices: list[int] = []  # if non-empty, skip all other files
    default_visible_profiles: list[str] = []  # if non-empty, only these profiles see it by default
    download_mode: str = "now"      # "now" = download immediately; "idle" = only during idle/night window


class VisibilityReq(BaseModel):
    profile_id: str
    hidden: bool


class ProfileReq(BaseModel):
    name: str
    color: str = "indigo"


class ProgressReq(BaseModel):
    profile_id: str
    file_path: str
    position_sec: float
    duration_sec: float


class YouTubeReq(BaseModel):
    url: str


class YouTubeControlReq(BaseModel):
    action: str
    value: Optional[float] = None        # ±seconds (seek) / 0-100% (seek_to) / volume


class YouTubeTvStateReq(BaseModel):
    # Heartbeat + playback report posted by the /tv kiosk page. All optional —
    # an early beat (before the IFrame player is ready) carries only video_id.
    video_id: Optional[str] = None
    title: Optional[str] = None
    time: Optional[float] = None
    duration: Optional[float] = None
    volume: Optional[float] = None
    playback: Optional[str] = None


class LibraryPlayReq(BaseModel):
    profile_id: str
    files: list[str] = []         # ordered list of absolute paths to play as a playlist
    seek_first_to: Optional[float] = None  # seek into the first file (seconds)


class MarkWatchedReq(BaseModel):
    profile_id: str
    watched: bool
    file_paths: list[str] = []    # specific paths; empty = all files in item
    season: Optional[int] = None  # if set (and file_paths empty), filter to this season


class AdminLoginReq(BaseModel):
    password: str


class AdminItemLockReq(BaseModel):
    admin_only: bool


class AdminSettingsReq(BaseModel):
    indexer_categories: Optional[str] = None
    tmdb_api_key: Optional[str] = None


class ScheduledRebootReq(BaseModel):
    enabled: bool = False
    time: str = "00:00"                       # local HH:MM in `timezone`
    timezone: str = "America/Los_Angeles"     # IANA name; "" = system local
    idle_minutes: int = 15                     # idle window before a reboot fires


class OvernightPrepReq(BaseModel):
    enabled: bool = False
    start: str = "02:00"                       # local HH:MM in `timezone` — window opens
    end: str = "06:00"                         # local HH:MM in `timezone` — window closes
    timezone: str = "America/Los_Angeles"     # IANA name; "" = system local
    on_end: str = "pause"                      # "pause" ⇒ stop at window end · "continue" ⇒ run to completion


class IdlePrepReq(BaseModel):
    enabled: bool = False
    idle_minutes: int = 30                     # auto-prep after this many minutes with no activity; clamped 1–720


class PlayPrepReq(BaseModel):
    enabled: bool = True                       # auto on-device prep of the playing episode (+ playlist tail) on every VLC play


class ForcePrepReq(BaseModel):
    item_id: Optional[str] = None              # force-prep one library item; None/"" ⇒ whole library


class ForcePrepStopReq(BaseModel):
    hard: bool = False                         # True ⇒ kill the in-flight encode now; False ⇒ let the current file finish, then cancel the rest


class CacheAutopurgeReq(BaseModel):
    enabled: bool = False
    max_gb: float = 50.0                        # purge orphan offline-cache bundles once .offline_cache/ reaches this many GB; clamped 1–10000


class SttConfigReq(BaseModel):
    enabled: bool = True
    translate: bool = True                     # also emit an English track for non-English audio


class SubsConfigReq(BaseModel):
    # Unified subtitle policy (settings.subtitles). `default_language` is the
    # preferred subtitle language (3-letter code; "" = Any). `on_by_default` is
    # the admin default for subs on/off (a profile may override). `auto_search`
    # fetches a preferred-language sub online on playback when none is embedded.
    default_language: str = "eng"
    on_by_default: bool = False
    auto_search: bool = True
    upgrade_late_subs: bool = True
    single_option: bool = True


class ProfileSubsReq(BaseModel):
    # Per-profile override of the admin subs-on/off default.
    # None ⇒ inherit the admin default; True/False ⇒ force on/off for this profile.
    subtitles_on: Optional[bool] = None


class QbitLimitsReq(BaseModel):
    ratio_enabled: bool = False                # stop seeding once max_ratio is reached
    ratio: float = 1.0                         # share ratio (uploaded / downloaded); clamped 0–9998
    dl_limit_bytes: int = 0                    # global download cap, bytes/sec (0 = unlimited)
    up_limit_bytes: int = 0                    # global upload cap, bytes/sec (0 = unlimited)


class PrepPauseReq(BaseModel):
    # True ⇒ terminate the file ffmpeg is encoding right now (instant relief, partial
    # work is discarded); False ⇒ let the in-flight file finish, then hold the rest.
    kill: bool = False


class MetadataRefreshReq(BaseModel):
    tmdb_id: Optional[int] = None     # manual override; pairs with `kind`
    kind: Optional[str] = None        # "tv" | "movie"


class ProfilePinReq(BaseModel):
    pin: str          # 4 digits to set, "" to clear
    current_pin: str = ""  # required when changing an existing PIN without admin token


class ProfileElevatedReq(BaseModel):
    elevated: bool    # whether this profile can view admin-only library items


class ProfileAutoSkipReq(BaseModel):
    auto_skip_intro: Optional[bool] = None
    auto_skip_credits: Optional[bool] = None


class ProfileResumeModeReq(BaseModel):
    resume_mode: str  # "auto" | "prompt" | "off"


class MaxVolumeReq(BaseModel):
    max_volume: int   # 0-200; 200 = no cap


class VlcStartVolumeReq(BaseModel):
    vlc_start_volume: int   # 0-100; % of the max-volume cap applied to VLC at startup


class NightModeReq(BaseModel):
    night_mode: Optional[bool] = None   # VLC dynamic-range compressor on/off (omit to leave unchanged)
    preset: Optional[str] = None        # intensity preset: light|medium|max (omit to leave unchanged)


class SystemVolumeDefaultReq(BaseModel):
    system_volume_default: int   # 0-100; OS volume restored when YouTube stops


class YouTubeStartVolumeReq(BaseModel):
    youtube_start_volume: int   # 0-100; OS volume pre-set before a YouTube play starts


class HostVolumeReq(BaseModel):
    host_volume: int  # 0-100; live host OS mixer volume


class SkipNowReq(BaseModel):
    type: str         # "intro" or "credits"


class AdminSkipDataReq(BaseModel):
    file_path: str
    intro_start: Optional[float] = None    # null = clear intro
    intro_end:   Optional[float] = None
    credits_start: Optional[float] = None  # null = clear credits


class BackgroundVolumeReq(BaseModel):
    volume: int       # 0-200


class BackgroundEnabledReq(BaseModel):
    enabled: bool


# ── Routes: Profiles ─────────────────────────────────────────────────────────

@app.get("/api/profiles")
async def list_profiles() -> JSONResponse:
    lib = await get_library()
    profiles = [
        {
            "id": p["id"],
            "name": p["name"],
            "color": p.get("color", "indigo"),
            "has_pin": bool(p.get("pin_hash", "")),
            "elevated": bool(p.get("elevated", False)),
            "auto_skip_intro":   bool(p.get("auto_skip_intro", False)),
            "auto_skip_credits": bool(p.get("auto_skip_credits", False)),
            "resume_mode":       p.get("resume_mode", "auto"),
            "subtitles_on":      p.get("subtitles_on"),
        }
        for p in lib["profiles"]
    ]
    return JSONResponse({"profiles": profiles})


@app.post("/api/profiles")
async def create_profile(req: ProfileReq) -> JSONResponse:
    lib = await get_library()
    if len(lib["profiles"]) >= 6:
        raise HTTPException(400, "Maximum 6 profiles reached.")
    profile = {"id": str(uuid.uuid4()), "name": req.name.strip()[:30], "color": req.color}
    lib["profiles"].append(profile)
    await put_library(lib)
    return JSONResponse({"profile": profile})


@app.delete("/api/profiles/{profile_id}")
async def delete_profile(profile_id: str) -> JSONResponse:
    lib = await get_library()
    lib["profiles"] = [p for p in lib["profiles"] if p["id"] != profile_id]
    for item in lib["items"]:
        item.get("progress", {}).pop(profile_id, None)
        hvp = item.get("hidden_by_profiles", [])
        if profile_id in hvp:
            hvp.remove(profile_id)
        dvp = item.get("default_visible_profiles", [])
        if profile_id in dvp:
            dvp.remove(profile_id)
    await put_library(lib)
    return JSONResponse({"ok": True})


# ── Routes: Library ───────────────────────────────────────────────────────────

def _item_hidden_for_profile(item: dict, profile_id: str) -> bool:
    """Return True if this item should appear in the user's hidden tab."""
    if not profile_id:
        return False
    if profile_id in item.get("hidden_by_profiles", []):
        return True
    dvp = item.get("default_visible_profiles", [])
    return bool(dvp) and profile_id not in dvp


@app.get("/api/library")
async def list_library(request: Request, profile_id: str = "") -> JSONResponse:
    is_admin = _check_admin(request)
    lib = await get_library()
    elevated_ids = {p["id"] for p in lib["profiles"] if p.get("elevated")}
    is_elevated  = bool(profile_id) and profile_id in elevated_ids
    items = []
    for it in lib["items"]:
        if it.get("admin_only") and not is_admin and not is_elevated:
            continue
        files = it.get("files", [])
        resume = find_resume_hint(it, profile_id) if profile_id else None
        # Slim "first_file" so single-file UI affordances (e.g. the On Device
        # button) know the path without fetching /files. Cheap: 1 file × 2 keys.
        first_file = None
        if files:
            f0 = files[0]
            first_file = {"path": f0.get("path", ""), "name": f0.get("name", "")}
        items.append({
            "id": it["id"],
            "title": it["title"],
            "series": it.get("series", ""),
            "season": it.get("season", 0),
            "episode": it.get("episode", 0),
            "file_count": len(files),
            "size_bytes": it.get("size_bytes", 0),
            "size_human": human_size(it.get("size_bytes", 0)),
            "added_at": it.get("added_at", ""),
            "status": it.get("status", "ready"),
            "torrent_hash": it.get("torrent_hash", ""),
            "resume": resume,
            "first_file": first_file,
            "hidden": _item_hidden_for_profile(it, profile_id),
            "skip_status": _item_skip_status(it),
            "download_mode": _download_cfg(it)["mode"],   # now | idle — drives the card's Pause/Resume control
            "download_partial": any(m == "skip" for m in _download_cfg(it)["files"].values()),  # some files deselected → "Partial" badge
        })
    items.sort(key=lambda x: (
        x["series"] or "\xff" + x["title"],
        x["season"],
        x["episode"],
        x["added_at"],
    ))
    return JSONResponse({"items": items})


@app.get("/api/library/{item_id}/files")
async def get_item_files(item_id: str, profile_id: str = "") -> JSONResponse:
    """Return the video file list for a library item with per-profile progress."""
    lib = await get_library()
    item = next((it for it in lib["items"] if it["id"] == item_id), None)
    if not item:
        raise HTTPException(404, "Item not found.")

    file_progs = (
        item.get("progress", {}).get(profile_id, {}).get("file_progress", {})
        if profile_id else {}
    )

    cfg = _download_cfg(item)
    has_torrent = bool(item.get("torrent_hash"))
    is_ready = item.get("status") == "ready"
    # Map per-file qBit download progress so the UI shows which files are actually on
    # disk — for a downloading torrent AND a "ready" one (a partial selection flips
    # ready with skipped files still absent; without live progress they'd masquerade
    # as complete). Falls back to status when qBit has no data (uploaded item, or the
    # torrent was removed).
    qmap: dict[str, float] = {}     # full path → progress
    qbase: dict[str, float] = {}    # basename → progress (fallback if path keys drift)
    if has_torrent:
        info = await qbit_info(item["torrent_hash"])
        qfiles = await qbit_files(item["torrent_hash"])
        if info and qfiles:
            sp = info.get("save_path", settings.qbit_download_path)
            for i, qf in enumerate(qfiles):
                name = qf.get("name", "")
                prog = qf.get("progress", 0.0)
                qmap[str(Path(sp) / name)] = prog
                qbase[Path(name).name] = prog   # best-effort; collisions rare (SxxExx names)

    out = []
    for f in item.get("files", []):
        path = f.get("path", "")
        fp = file_progs.get(path)
        if fp:
            dur = fp.get("duration_sec", 0)
            pos = fp.get("position_sec", 0)
            progress = {
                "position_sec": pos,
                "duration_sec": dur,
                "completed": fp.get("completed", False),
                "pct": round(pos / dur * 100, 1) if dur else 0,
            }
        else:
            progress = None
        mode = _effective_file_mode(cfg, path)
        # Live qBit progress is the truth, regardless of mode — a file that was
        # downloaded then later marked "skip" is still on disk and playable. Fall
        # back to basename match if the full-path key didn't line up (save-path drift).
        qp = qmap.get(path)
        if qp is None:
            qp = qbase.get(Path(path).name)
        if qp is not None:
            dl_pct = round(qp * 100, 1)
            complete = qp >= 0.999
        elif mode == "skip":
            # No live data + skipped ⇒ assume it was never fetched.
            dl_pct, complete = 0.0, False
        else:
            # No live qBit data at all (uploaded item, or torrent gone): trust status.
            complete = is_ready or not has_torrent
            dl_pct = 100.0 if complete else None
        out.append({
            "name": f.get("name", Path(path).name),
            "path": path,
            "size_bytes": f.get("size_bytes", 0),
            "size_human": human_size(f.get("size_bytes", 0)),
            "season": f.get("season", 0),
            "episode": f.get("episode", 0),
            "progress": progress,
            "mode": mode,                               # now | high | idle | skip
            "dl_pct": dl_pct,                           # download % (None if unknown)
            "complete": complete,                       # file fully downloaded → playable
        })
    return JSONResponse({
        "files": out,
        "item_status": item.get("status", "ready"),
        "has_torrent": has_torrent,                     # gates the download-scheduling controls
        "download_mode": cfg["mode"],
        "idle_open": state.download_idle_open,
        "idle_configured": state.download_idle_configured,
    })


@app.get("/api/library/{item_id}/metadata")
async def get_item_metadata(item_id: str, refresh: int = 0) -> JSONResponse:
    """Return cached TMDb metadata for an item; auto-fetches on first access.
    Response always includes `enabled` so the UI can gracefully fall back to
    filename parsing when no TMDb key is configured."""
    key_present = bool(await _tmdb_effective_key())
    lib = await get_library()
    item = next((it for it in lib["items"] if it["id"] == item_id), None)
    if not item:
        raise HTTPException(404, "Item not found.")

    cached = item.get("metadata") or {}
    if not cached and key_present:
        cached = await _fetch_item_metadata(item_id) or {}
    elif refresh and key_present:
        cached = await _fetch_item_metadata(item_id, force=True) or cached

    return JSONResponse({
        "enabled":  key_present,
        "img_base": TMDB_IMG_BASE,
        "metadata": cached or None,
    })


@app.post("/api/library/{item_id}/metadata/refresh")
async def refresh_item_metadata(item_id: str, request: Request,
                                 req: MetadataRefreshReq) -> JSONResponse:
    """Admin-only manual rematch / refresh. Accepts an optional `{tmdb_id, kind}`
    to force-bind a specific TMDb entry (covers cases where auto-match picks
    the wrong show)."""
    _require_admin(request)
    if not await _tmdb_effective_key():
        raise HTTPException(400, "TMDb API key is not configured.")
    data = await _fetch_item_metadata(
        item_id,
        force=True,
        override_tmdb_id=req.tmdb_id,
        override_kind=req.kind if req.kind in ("tv", "movie") else None,
    )
    if not data:
        raise HTTPException(404, "No TMDb match found for this item.")
    return JSONResponse({"ok": True, "metadata": data, "img_base": TMDB_IMG_BASE})


@app.post("/api/library/download")
async def library_download(req: DownloadReq) -> JSONResponse:
    if not state.vpn_secure:
        raise HTTPException(403, "VPN not connected — download blocked.")
    lib = await get_library()
    item: dict = {
        "id": str(uuid.uuid4()),
        "title": req.title,
        "series": req.series,
        "season": req.season,
        "episode": req.episode,
        "files": [],
        "size_bytes": 0,
        "added_at": _now_iso(),
        "status": "downloading",
        "torrent_hash": "",
        "progress": {},
        "default_visible_profiles": req.default_visible_profiles,
        "hidden_by_profiles": [],
        "download": {"mode": req.download_mode if req.download_mode in ("now", "idle") else "now",
                     "files": {}},
    }
    lib["items"].append(item)
    await put_library(lib)
    state.downloading_count += 1
    save_path = req.save_path.strip() or settings.qbit_download_path
    asyncio.create_task(library_download_pipeline(
        item["id"], req.magnet, save_path,
        torrent_hash=req.torrent_hash,
        selected_file_indices=req.selected_file_indices or None,
        download_mode=item["download"]["mode"],
    ))
    return JSONResponse({"ok": True, "item_id": item["id"],
                         "default_save_path": settings.qbit_download_path})


@app.delete("/api/library/{item_id}")
async def delete_library_item(item_id: str, delete_file: bool = True) -> JSONResponse:
    lib = await get_library()
    item = next((it for it in lib["items"] if it["id"] == item_id), None)
    if not item:
        raise HTTPException(404, "Item not found.")
    h = item.get("torrent_hash")
    if h:
        await qbit_delete(h, delete_files=delete_file)
    lib["items"] = [it for it in lib["items"] if it["id"] != item_id]
    await put_library(lib)
    return JSONResponse({"ok": True})


@app.post("/api/library/{item_id}/visibility")
async def set_item_visibility(item_id: str, req: VisibilityReq) -> JSONResponse:
    """Toggle per-profile visibility. hidden=true moves item to the user's hidden tab;
    hidden=false restores it to the main list."""
    lib = await get_library()
    item = next((it for it in lib["items"] if it["id"] == item_id), None)
    if not item:
        raise HTTPException(404, "Item not found.")
    pid = req.profile_id
    hidden_by: list = item.setdefault("hidden_by_profiles", [])
    default_visible: list = item.setdefault("default_visible_profiles", [])
    if req.hidden:
        # Move to hidden: remove from explicit visible list (if present), else add to hidden list
        if default_visible and pid in default_visible:
            default_visible.remove(pid)
        elif pid not in hidden_by:
            hidden_by.append(pid)
    else:
        # Move to visible: remove from hidden list; if still restricted by default, grant access
        if pid in hidden_by:
            hidden_by.remove(pid)
        if default_visible and pid not in default_visible:
            default_visible.append(pid)
    await put_library(lib)
    return JSONResponse({"ok": True})


@app.post("/api/library/{item_id}/queue-play")
async def queue_play(item_id: str, profile_id: str = "", file_path: str = "") -> JSONResponse:
    """Queue an in-progress download to auto-play when it (or a specific file) finishes."""
    if not profile_id:
        raise HTTPException(400, "profile_id required.")
    lib = await get_library()
    item = next((it for it in lib["items"] if it["id"] == item_id), None)
    if not item:
        raise HTTPException(404, "Item not found.")
    if item.get("status") != "downloading":
        raise HTTPException(400, "Item is not currently downloading.")
    state.play_when_ready_item_id = item_id
    state.play_when_ready_profile_id = profile_id
    state.play_when_ready_file_path = file_path or None
    # Route the boost through the download model so the scheduler keeps it — a raw
    # filePrio write would be reverted on the next reconcile. A specific file is
    # forced "high" (download now, first); a whole-item queue ensures the item isn't
    # stuck idle (mode→now, sweep idle files→now) + bumps it in qBit's global queue.
    dl = item.setdefault("download", {"mode": "now", "files": {}})
    files = dl.setdefault("files", {})
    if file_path:
        files[file_path] = "high"
    else:
        dl["mode"] = "now"
        for p, m in list(files.items()):
            if m == "idle":
                files[p] = "now"
    await put_library(lib)
    h = item.get("torrent_hash")
    if h:
        idle_open = await _download_idle_open(lib)
        await _reconcile_item_downloads(item, idle_open)
        if not file_path:
            await qreq("POST", "/api/v2/torrents/topPrio", data={"hashes": h})
    return JSONResponse({"ok": True})


@app.delete("/api/library/{item_id}/queue-play")
async def cancel_queue_play(item_id: str) -> JSONResponse:
    """Cancel a pending Play When Ready for this item."""
    if state.play_when_ready_item_id == item_id:
        state.play_when_ready_item_id = None
        state.play_when_ready_profile_id = None
    return JSONResponse({"ok": True})


class DownloadScheduleReq(BaseModel):
    mode: str            # "now" = download immediately; "idle" = only during idle/night window
    reset_files: bool = False   # True ⇒ clear per-file overrides so EVERY file (incl. skipped) inherits `mode`


class FileScheduleReq(BaseModel):
    file_paths: list[str]
    mode: str   # "now" | "high" | "idle" | "skip"


@app.post("/api/library/{item_id}/download-schedule")
async def set_download_schedule(item_id: str, req: DownloadScheduleReq) -> JSONResponse:
    """Item-level download schedule. "idle" = Pause (download only during the idle/
    night window, auto-resuming there); "now" = Resume (download immediately). Sweeps
    the per-file overrides too, but leaves explicit "skip" choices alone."""
    mode = req.mode if req.mode in ("now", "idle") else "now"
    lib = await get_library()
    item = next((it for it in lib["items"] if it["id"] == item_id), None)
    if not item:
        raise HTTPException(404, "Item not found.")
    dl = item.setdefault("download", {"mode": "now", "files": {}})
    dl["mode"] = mode
    files = dl.setdefault("files", {})
    if req.reset_files:
        # "Whole torrent now/idle": drop every override so all files (including ones
        # previously skipped) inherit `mode` — now ⇒ fetch everything, idle ⇒ defer all.
        files.clear()
    elif mode == "idle":
        # Pause: defer the active files, but keep explicit skips skipped.
        for p, m in list(files.items()):
            if m in ("now", "high"):
                files[p] = "idle"
    else:
        for p, m in list(files.items()):
            if m == "idle":
                files[p] = "now"
    idle_open = await _apply_item_schedule(item, lib)   # may flip ready→downloading
    await put_library(lib)
    await broadcast("library_update", {"item_id": item_id, "status": item.get("status", "downloading")})
    return JSONResponse({"ok": True, "mode": mode, "idle_open": idle_open})


@app.post("/api/library/{item_id}/file-schedule")
async def set_file_schedule(item_id: str, req: FileScheduleReq) -> JSONResponse:
    """Set the download schedule for specific files (or a whole folder, by passing
    its files) within a library item: "now" (normal), "high" (download now, first),
    "idle" (only during the idle/night window), or "skip" (never)."""
    mode = req.mode if req.mode in _FILE_MODES else "now"
    if not req.file_paths:
        raise HTTPException(400, "file_paths required.")
    lib = await get_library()
    item = next((it for it in lib["items"] if it["id"] == item_id), None)
    if not item:
        raise HTTPException(404, "Item not found.")
    dl = item.setdefault("download", {"mode": "now", "files": {}})
    files = dl.setdefault("files", {})
    for p in req.file_paths:
        files[p] = mode
    idle_open = await _apply_item_schedule(item, lib)   # may flip ready→downloading (e.g. un-skip)
    await put_library(lib)
    await broadcast("library_update", {"item_id": item_id, "status": item.get("status", "downloading")})
    return JSONResponse({"ok": True, "updated": len(req.file_paths), "mode": mode})


async def _vlc_wait_until_ready(
    expected_file: Optional[Path] = None, timeout: float = 20.0,
) -> bool:
    """Poll VLC until it has actually opened the file, then return True.

    "Ready" = VLC reports a playing/paused state AND a non-zero `length` (the
    duration only becomes known once the demuxer is up, which is precisely the
    moment a `seek` takes effect). When `expected_file` is given we also require
    VLC's current playlist URI to match it, so we don't mistake the *previously*
    playing file (background video, prior episode) for the new one.

    This is the responsiveness lever for play/resume. VLC's HTTP reply to the
    `in_play` command itself can lag several seconds behind actual playback
    (which starts in <1 s); the lighter `status.json` poll reflects the real
    state far sooner. Polling here — instead of awaiting `in_play`'s reply — is
    what lets the UI flip to "playing" and the resume seek land almost
    immediately. We poll every 0.2 s and still wait out a genuinely slow open so
    nothing fires before VLC can honour it.
    """
    target: Optional[Path] = None
    if expected_file is not None:
        try:
            target = expected_file.resolve()
        except Exception:
            target = expected_file
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        vs = await vlc_status()
        if vs and vs.get("state") in ("playing", "paused") and float(vs.get("length", 0) or 0) > 0:
            if target is None:
                return True
            uri = await vlc_playlist_uri()
            if uri:
                cur = uri_to_path(uri)
                try:
                    if Path(cur).resolve() == target:
                        return True
                except Exception:
                    if cur == str(target):
                        return True
        await asyncio.sleep(0.2)
    return False


async def _library_play_launch(
    playlist: list[str],
    item_id: str,
    profile_id: str,
    seek_sec: Optional[float],
    resume_mode: str,
) -> None:
    """VLC handoff for a library play, run in the background.

    Flow:
    1. Fire `in_play` for the first file *detached* — VLC's HTTP reply to this
       command can hang for several seconds while the demuxer spins up, even
       though playback actually starts in <1 s. Awaiting it (the old behaviour)
       pinned the UI on "buffering" for that whole window.
    2. Poll `status.json` for the new file to actually be live, then flip
       `stream_status` to "playing" and seek to the resume point — both happen
       as soon as VLC is genuinely ready, not when `in_play` finally replies.
    3. Enqueue the rest in parallel via `asyncio.gather`. We let the `in_play`
       call finish first (correct playlist order); that wait is invisible since
       the user is already watching the first file.
    """
    first = Path(playlist[0])
    first_resolved = first.resolve()
    # Clear any stale playlist items first (incl. the bg video) so VLC's list
    # mirrors `playlist` and can't auto-advance into a leftover entry. Awaited
    # so it completes before the in_play below appends the new item.
    await vlc_clear_playlist()
    # Detached: don't let in_play's slow reply gate the UI flip or the seek.
    play_task = asyncio.create_task(vlc("in_play", input=first_resolved.as_uri()))
    try:
        # Wait for VLC to actually be playing the new file (poll, not the slow
        # in_play reply). Falls through after the timeout and flips optimistically
        # — the command is already on its way — so the UI never sticks.
        ready = await _vlc_wait_until_ready(first_resolved, timeout=10.0)

        state.stream_status = "playing"
        await broadcast("stream_status", {"status": "playing", "message": f"Playing: {first.name}"})
        await broadcast("state", state_snapshot())

        asyncio.create_task(vlc_focus_and_fullscreen())

        # Resume seek / offer run detached so they can fire in parallel with the
        # enqueue below. They outlive a cancelled _library_play_launch, so each
        # re-checks state.library_current_file before touching VLC — a newer play
        # (or prev/next) moves that pointer and the stale resume bails instead of
        # seeking the wrong file. `ready` lets the common case skip a second poll.
        expected_file = playlist[0]
        if seek_sec and seek_sec > 5:
            if resume_mode == "auto":
                async def _resume_seek(s: float, already_ready: bool) -> None:
                    target = int(s)
                    if not already_ready and not await _vlc_wait_until_ready(first_resolved):
                        return
                    if state.library_current_file != expected_file:
                        return
                    await vlc("seek", val=str(target))
                    # VLC occasionally ignores a seek issued the instant the
                    # demuxer comes up. Re-issue once if we're still parked near
                    # the start a moment later (guarded so a user who manually
                    # seeks forward in this window isn't yanked back).
                    await asyncio.sleep(0.6)
                    if state.library_current_file != expected_file:
                        return
                    vs = await vlc_status()
                    if vs and float(vs.get("time", 0) or 0) < target - 15:
                        await vlc("seek", val=str(target))
                asyncio.create_task(_resume_seek(seek_sec, ready))
            elif resume_mode == "prompt":
                async def _resume_offer(s: float, fp: str, iid: str, pl: list, already_ready: bool) -> None:
                    if not already_ready and not await _vlc_wait_until_ready(first_resolved):
                        return
                    if state.library_current_file != expected_file:
                        return
                    state.resume_offer = {"position_sec": s, "file_path": fp, "item_id": iid, "playlist": pl}
                    await broadcast("state", state_snapshot())
                asyncio.create_task(_resume_offer(seek_sec, playlist[0], item_id, playlist, ready))

        asyncio.create_task(_apply_track_prefs(item_id, profile_id, playlist[0], delay=3.5))

        rest = playlist[1:]
        if rest:
            # Ensure in_play has been accepted (so the tail appends in the right
            # place); bounded by vlc()'s own per-call timeouts.
            try:
                await play_task
            except Exception:
                pass
            await asyncio.gather(
                *(vlc("in_enqueue", input=Path(p).resolve().as_uri()) for p in rest),
                return_exceptions=True,
            )
    except asyncio.CancelledError:
        play_task.cancel()
        raise
    except Exception as e:
        play_task.cancel()
        state.stream_status = "error"
        await broadcast("stream_status", {"status": "error", "message": f"Playback failed: {e}"})
        await broadcast("state", state_snapshot())


@app.post("/api/library/{item_id}/play")
async def play_library_item(item_id: str, req: LibraryPlayReq) -> JSONResponse:
    lib = await get_library()
    item = next((it for it in lib["items"] if it["id"] == item_id), None)
    if not item:
        raise HTTPException(404, "Item not found.")

    # Resolve playlist — if caller passed explicit files use those, else auto-select
    playlist = req.files
    if not playlist:
        hint = find_resume_hint(item, req.profile_id)
        if hint and hint.get("file_path"):
            # Build playlist starting from the resume file
            all_paths = [f["path"] for f in item.get("files", [])]
            try:
                start_idx = all_paths.index(hint["file_path"])
                playlist = all_paths[start_idx:]
            except ValueError:
                playlist = all_paths
        else:
            playlist = [f["path"] for f in item.get("files", [])]

    if not playlist:
        raise HTTPException(400, "No video files available for this item.")

    # Filter to files that are on disk — partial downloads may not have all files yet
    existing = [p for p in playlist if Path(p).exists()]
    if not existing:
        raise HTTPException(400, f"File(s) not yet downloaded: {', '.join(Path(p).name for p in playlist[:3])}")
    playlist = existing
    first = Path(playlist[0])

    # Resolve seek position and resume mode synchronously
    seek_sec = req.seek_first_to
    if seek_sec is None:
        hint = find_resume_hint(item, req.profile_id)
        if hint and hint.get("position_sec", 0) > 5 and not hint.get("all_completed"):
            seek_sec = hint["position_sec"]
    prof_obj = next((p for p in lib.get("profiles", []) if p["id"] == req.profile_id), {})
    resume_mode = prof_obj.get("resume_mode", "auto")

    # If a prior library play is still mid-handoff, cancel it so we don't race VLC
    prior = state.library_play_task
    if prior and not prior.done():
        prior.cancel()
    await _cancel_skip_countdown()

    # Flip state to buffering NOW so the SSE-driven UI paints loading state
    # before the slow VLC roundtrips even start.
    state.stream_status = "buffering"
    state.active_title = item["title"]
    state.active_file = first
    state.current_audio_track = -1
    state.current_subtitle_track = -1
    state.track_pref_applied_file = playlist[0]
    state.active_hash = item.get("torrent_hash") or None
    state.library_item_id = item_id
    state.library_profile_id = req.profile_id
    state.library_item_file_count = len(item.get("files", []))
    state.library_playlist = playlist
    state.library_current_file = playlist[0]
    state.skip_offer = None
    state.skip_offer_file = None
    state.resume_offer = None

    await broadcast("stream_status", {"status": "buffering", "message": f"Starting: {first.name}"})
    await broadcast("state", state_snapshot())

    state.library_play_task = asyncio.create_task(_library_play_launch(
        playlist, item_id, req.profile_id, seek_sec, resume_mode,
    ))

    # Auto-prep this episode (and the rest of the playlist) for on-device, if
    # enabled. Runs regardless of idle/activity settings — see _maybe_start_play_prep.
    await _maybe_start_play_prep(lib, item, req.profile_id, playlist, seek_sec)

    return JSONResponse(
        {"ok": True, "playlist_count": len(playlist), "seek_to": seek_sec},
        status_code=202,
    )


@app.post("/api/library/{item_id}/progress")
async def update_progress(item_id: str, req: ProgressReq) -> JSONResponse:
    lib = await get_library()
    item = next((it for it in lib["items"] if it["id"] == item_id), None)
    if not item:
        raise HTTPException(404, "Item not found.")
    dur = req.duration_sec
    pct = req.position_sec / dur if dur else 0
    prof_prog = item.setdefault("progress", {}).setdefault(req.profile_id, {})
    prof_prog["last_file"] = req.file_path
    file_progress = prof_prog.setdefault("file_progress", {})
    existing = file_progress.get(req.file_path, {})
    file_progress[req.file_path] = {
        "position_sec": round(req.position_sec, 1),
        "duration_sec": round(req.duration_sec, 1),
        "completed": pct > 0.92,
        "updated_at": _now_iso(),
        # Preserve VLC + local-player track picks across progress writes —
        # these are sibling keys in the same file_progress dict.
        **{k: v for k, v in existing.items()
           if k in ("audio_track", "subtitle_track",
                    "local_audio_idx", "local_subtitle_idx")},
    }
    await put_library(lib)
    return JSONResponse({"ok": True})


class LocalTracksReq(BaseModel):
    profile_id: str
    file_path: str
    audio_idx: Optional[int] = None
    subtitle_idx: Optional[int] = None   # -1 = subtitles off (bundle index space only)
    # Resolvable descriptor for the chosen subtitle, used to re-apply the pick on
    # replay AND on other episodes of the same series. {off,lang,ai,name}.
    # Supersedes subtitle_idx, which can't address sidecar/AI picks (see GOTCHAS).
    subtitle_sel: Optional[dict] = None


@app.post("/api/library/{item_id}/local-tracks")
async def set_local_tracks(item_id: str, req: LocalTracksReq) -> JSONResponse:
    """Persist the in-browser player's audio/subtitle picks for a file.

    These are 0-based indices into the HLS bundle's audios/subtitles arrays
    (the order seen in meta.json), distinct from the VLC `audio_track` /
    `subtitle_track` ES IDs sitting next to them in the same file_progress
    dict. The two systems use different addressing schemes that don't
    interchange — that's why they're kept under separate keys.
    """
    series = ""
    async with _lib_lock:
        lib = _load_lib_raw()
        item = next((it for it in lib["items"] if it["id"] == item_id), None)
        if not item:
            raise HTTPException(404, "Item not found.")
        series = _series_of_item(item)
        fp = (item.setdefault("progress", {})
                  .setdefault(req.profile_id, {})
                  .setdefault("file_progress", {})
                  .setdefault(req.file_path, {}))
        if req.audio_idx is not None:
            fp["local_audio_idx"] = req.audio_idx
        if req.subtitle_idx is not None:
            fp["local_subtitle_idx"] = req.subtitle_idx
        if req.subtitle_sel is not None:
            norm = _norm_sub_sel(req.subtitle_sel)
            if norm:
                fp["subtitle_sel"] = norm
        _save_lib_raw(lib)
    # Remember this kind of subtitle for the rest of the series (separate lock).
    if req.subtitle_sel is not None and series:
        await _save_series_sub_sel(req.profile_id, series, req.subtitle_sel)
    return JSONResponse({"ok": True})


@app.post("/api/library/{item_id}/mark-watched")
async def mark_watched(item_id: str, req: MarkWatchedReq) -> JSONResponse:
    """Mark or unmark episodes as watched for a profile."""
    lib = await get_library()
    item = next((it for it in lib["items"] if it["id"] == item_id), None)
    if not item:
        raise HTTPException(404, "Item not found.")

    files = item.get("files", [])
    if req.file_paths:
        targets = {p for p in req.file_paths}
        target_files = [f for f in files if f.get("path", "") in targets]
    elif req.season is not None:
        target_files = [f for f in files if f.get("season", 0) == req.season]
    else:
        target_files = files

    prof_prog = item.setdefault("progress", {}).setdefault(req.profile_id, {})
    file_prog = prof_prog.setdefault("file_progress", {})

    for f in target_files:
        path = f.get("path", "")
        if not path:
            continue
        existing = file_prog.get(path, {})
        if req.watched:
            file_prog[path] = {
                "position_sec": existing.get("duration_sec", 0),
                "duration_sec": existing.get("duration_sec", 0),
                "completed": True,
                "updated_at": _now_iso(),
                **{k: v for k, v in existing.items()
                   if k in ("audio_track", "subtitle_track",
                            "local_audio_idx", "local_subtitle_idx",
                            "subtitle_sel")},
            }
        else:
            file_prog[path] = {
                "position_sec": 0,
                "duration_sec": existing.get("duration_sec", 0),
                "completed": False,
                "updated_at": _now_iso(),
                **{k: v for k, v in existing.items()
                   if k in ("audio_track", "subtitle_track",
                            "local_audio_idx", "local_subtitle_idx",
                            "subtitle_sel")},
            }

    await put_library(lib)
    await broadcast("library_update", {"item_id": item_id, "status": item.get("status", "ready")})
    return JSONResponse({"ok": True, "updated": len(target_files)})


# ── Routes: Search & Stream ───────────────────────────────────────────────────

@app.get("/api/search")
async def search(q: str, limit: int = 30) -> JSONResponse:
    if not q.strip():
        return JSONResponse({"results": []})

    lib = await get_library()
    cats_override = lib.get("settings", {}).get("admin_overrides", {}).get("indexer_categories")
    cats = (cats_override if cats_override is not None else settings.indexer_categories).strip()
    params: dict = {"apikey": settings.indexer_api_key, "Query": q}
    if cats and cats != "0":
        params["Category[]"] = cats

    try:
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.get(
                f"{settings.indexer_url}/api/v2.0/indexers/all/results",
                params=params,
            )
        items = r.json().get("Results", [])
    except Exception as e:
        raise HTTPException(502, f"Indexer unreachable: {e}")

    results = []
    for it in items[: limit * 2]:
        mag = it.get("MagnetUri") or it.get("Link", "")
        if not mag:
            continue
        results.append({
            "title": it.get("Title", "Unknown"),
            "size": it.get("Size", 0),
            "size_human": human_size(it.get("Size", 0)),
            "seeders": it.get("Seeders", 0),
            "peers": it.get("Peers", 0),
            "magnet": mag,
            "tracker": it.get("Tracker", ""),
        })

    results.sort(key=lambda x: x["seeders"], reverse=True)
    return JSONResponse({"results": results[:limit]})


@app.post("/api/stream/prepare")
async def stream_prepare(req: StreamPrepareReq) -> JSONResponse:
    """Add magnet to qBit, wait for file metadata, return file list for the picker UI.

    The caller is responsible for either proceeding with /api/stream (using the returned
    hash) or cancelling with DELETE /api/stream/cancel?hash=... if the user dismisses
    the picker.  The torrent is stored in state.prepare_hash so /api/stop also cleans it up.
    """
    if not state.vpn_secure:
        raise HTTPException(403, "VPN not connected — streaming blocked.")

    # Clean up any previous prepare torrent that the user didn't explicitly cancel
    if state.prepare_hash:
        await qbit_delete(state.prepare_hash, delete_files=True)
        state.prepare_hash = None

    h = await qbit_add_magnet(req.magnet)
    if not h:
        raise HTTPException(500, "qBittorrent rejected the magnet.")

    state.prepare_hash = h

    # Wait for torrent to appear (metadata download)
    for _ in range(30):
        await asyncio.sleep(1)
        if await qbit_info(h):
            break
    else:
        await qbit_delete(h, delete_files=True)
        state.prepare_hash = None
        raise HTTPException(504, "Torrent metadata timed out — check connectivity and try again.")

    # Wait for file list (may need another moment after info appears)
    for _ in range(30):
        files = await qbit_files(h)
        if files:
            break
        await asyncio.sleep(1)
    else:
        await qbit_delete(h, delete_files=True)
        state.prepare_hash = None
        raise HTTPException(504, "Could not fetch file list — torrent may have no seeds.")

    result = [
        {
            "index": f.get("index", i),
            "name": f.get("name", ""),
            "size_bytes": f.get("size", 0),
            "size_human": human_size(f.get("size", 0)),
        }
        for i, f in enumerate(files)
    ]
    return JSONResponse({"hash": h, "files": result})


@app.delete("/api/stream/cancel")
async def stream_cancel(hash: str) -> JSONResponse:
    """Delete a torrent that was added by /stream/prepare but never started streaming."""
    await qbit_delete(hash, delete_files=True)
    if state.prepare_hash == hash:
        state.prepare_hash = None
    return JSONResponse({"ok": True})


@app.post("/api/library/prepare")
async def library_prepare(req: StreamPrepareReq) -> JSONResponse:
    """Fetch the file list for a torrent so the library file-picker UI can show checkboxes.

    Unlike /stream/prepare this does NOT touch state.prepare_hash — the caller is
    responsible for either completing the download (passing the returned hash to
    /api/library/download) or cancelling with DELETE /api/stream/cancel?hash=...
    """
    if not state.vpn_secure:
        raise HTTPException(403, "VPN not connected — download blocked.")

    h = await qbit_add_magnet(req.magnet)
    if not h:
        raise HTTPException(500, "qBittorrent rejected the magnet.")

    for _ in range(30):
        await asyncio.sleep(1)
        if await qbit_info(h):
            break
    else:
        await qbit_delete(h, delete_files=True)
        raise HTTPException(504, "Torrent metadata timed out — check connectivity and try again.")

    for _ in range(30):
        files = await qbit_files(h)
        if files:
            break
        await asyncio.sleep(1)
    else:
        await qbit_delete(h, delete_files=True)
        raise HTTPException(504, "Could not fetch file list — torrent may have no seeds.")

    result = [
        {
            "index": f.get("index", i),
            "name": f.get("name", ""),
            "size_bytes": f.get("size", 0),
            "size_human": human_size(f.get("size", 0)),
        }
        for i, f in enumerate(files)
    ]
    return JSONResponse({"hash": h, "files": result})


@app.post("/api/library/upload")
async def upload_to_library(
    files: list[UploadFile] = File(...),
    title: str = Form(""),
    series: str = Form(""),
    season: int = Form(0),
    episode: int = Form(0),
    save_path: str = Form(""),
) -> JSONResponse:
    """Accept one or more local video files and add them directly to the library."""
    if not files:
        raise HTTPException(400, "No files provided.")
    dest_dir = Path(save_path.strip() or settings.qbit_download_path)
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        raise HTTPException(500, f"Cannot create save directory: {e}")

    saved_files = []
    for upload in files:
        filename = Path(upload.filename or "upload").name
        if not filename or Path(filename).suffix.lower() not in VIDEO_EXTS:
            continue
        dest = dest_dir / filename
        stem, suffix = Path(filename).stem, Path(filename).suffix
        counter = 1
        while dest.exists():
            dest = dest_dir / f"{stem}_{counter}{suffix}"
            counter += 1
        with open(dest, "wb") as f:
            while chunk := await upload.read(1024 * 1024):
                f.write(chunk)
        s, ep = parse_season_episode(filename)
        saved_files.append({
            "name": dest.name,
            "path": str(dest),
            "size_bytes": dest.stat().st_size,
            "season": s,
            "episode": ep,
        })

    if not saved_files:
        raise HTTPException(400, "No supported video files were uploaded.")

    item: dict = {
        "id": str(uuid.uuid4()),
        "title": title.strip() or (Path(saved_files[0]["name"]).stem if len(saved_files) == 1 else "Uploaded Files"),
        "series": series.strip(),
        "season": season,
        "episode": episode,
        "files": saved_files,
        "size_bytes": sum(f["size_bytes"] for f in saved_files),
        "added_at": _now_iso(),
        "status": "ready",
        "torrent_hash": "",
        "progress": {},
    }
    lib = await get_library()
    lib["items"].append(item)
    await put_library(lib)
    await broadcast("library_update", {"item_id": item["id"], "status": "ready"})
    return JSONResponse({"ok": True, "item_id": item["id"], "file_count": len(saved_files)})


@app.post("/api/stream")
async def stream_now(req: StreamReq) -> JSONResponse:
    if not state.vpn_secure:
        raise HTTPException(403, "VPN not connected — streaming blocked.")

    if state.stream_task and not state.stream_task.done():
        state.stream_task.cancel()
    if state.library_play_task and not state.library_play_task.done():
        state.library_play_task.cancel()

    # Snapshot what needs cleaning up; the actual qBit delete runs in the
    # background so we can flip UI state to "buffering" and return immediately.
    prior_active = state.active_hash if not state.library_item_id else None
    prior_prepare = None
    if state.prepare_hash and state.prepare_hash != req.torrent_hash:
        prior_prepare = state.prepare_hash
        state.prepare_hash = None
    elif req.torrent_hash and state.prepare_hash == req.torrent_hash:
        state.prepare_hash = None

    state.active_hash = None
    state.active_file = None
    state.library_item_id = None
    state.library_profile_id = None
    state.library_item_file_count = 0
    state.library_playlist = []
    state.library_current_file = None
    state.resume_offer = None

    if prior_active or prior_prepare:
        async def _cleanup_prior(active: Optional[str], prepare: Optional[str]) -> None:
            try:
                if active:
                    await qbit_delete(active)
                if prepare:
                    await qbit_delete(prepare, delete_files=True)
            except Exception:
                pass
        asyncio.create_task(_cleanup_prior(prior_active, prior_prepare))

    state.stream_task = asyncio.create_task(
        stream_pipeline(req.magnet, req.title, req.file_index, req.torrent_hash)
    )
    return JSONResponse({"ok": True}, status_code=202)


class SaveToLibraryReq(BaseModel):
    title: str = ""
    series: str = ""
    season: int = 0
    episode: int = 0
    save_path: str = ""


@app.post("/api/stream/save-to-library")
async def save_stream_to_library(req: SaveToLibraryReq) -> JSONResponse:
    """Adopt the currently streaming torrent into the persistent library.

    Restores all file priorities to 1 so the full torrent continues downloading,
    then creates a library entry and sets state.library_item_id so /api/stop
    will no longer auto-delete the torrent.
    """
    if not state.active_hash:
        raise HTTPException(400, "No active stream.")
    if state.library_item_id:
        raise HTTPException(400, "Stream is already saved to library.")

    h = state.active_hash

    # Restore all file priorities (streaming mode may have skipped some)
    all_files = await qbit_files(h)
    if all_files:
        all_ids = [f.get("index", i) for i, f in enumerate(all_files)]
        await qbit_set_file_priority(h, all_ids, 1)

    info = await qbit_info(h)
    save_path = req.save_path.strip() or (info.get("save_path") if info else None) or settings.qbit_download_path
    files = build_file_list(all_files, save_path) if all_files else []

    item: dict = {
        "id": str(uuid.uuid4()),
        "title": req.title.strip() or state.active_title or "Unknown",
        "series": req.series,
        "season": req.season,
        "episode": req.episode,
        "files": files,
        "size_bytes": info.get("size", 0) if info else 0,
        "added_at": _now_iso(),
        "status": "downloading",
        "torrent_hash": h,
        "progress": {},
    }

    lib = await get_library()
    lib["items"].append(item)
    await put_library(lib)
    state.downloading_count += 1
    state.library_item_id = item["id"]  # prevents auto-delete on stop

    await broadcast("library_update", {"item_id": item["id"], "status": "downloading"})
    return JSONResponse({"ok": True, "item_id": item["id"],
                         "default_save_path": settings.qbit_download_path})


# ── YouTube on TV ───────────────────────────────────────────────────────────
# VLC 3.0's bundled youtube.lua is perpetually broken (it "plays" the watch page
# for a few seconds, never resolves a title/length, then stops — see GOTCHAS).
# Instead we play YouTube in a Chrome kiosk window on the host display (the TV)
# via the YouTube IFrame Player API on our own /tv page, and drive it remotely
# from the dashboard: dashboard → POST /api/youtube/control → SSE `yt_command`
# → /tv page → IFrame API. The /tv page reports playback back via
# /api/youtube/tv-state, which we mirror onto the reused vlc_time/duration/volume
# fields so the existing footer + fullscreen controls render it unchanged.

_YT_ID_RE = re.compile(
    r"(?:youtu\.be/|youtube\.com/(?:watch\?(?:.*&)?v=|embed/|shorts/|live/|v/))"
    r"([0-9A-Za-z_-]{11})"
)


def _extract_youtube_id(url: str) -> Optional[str]:
    """Pull the 11-char video id from any common YouTube URL form.

    Accepts watch?v=, youtu.be/, /shorts/, /embed/, /live/, or a bare 11-char id.
    Returns None if nothing that looks like a video id is present.
    """
    if not url:
        return None
    url = url.strip()
    m = _YT_ID_RE.search(url)
    if m:
        return m.group(1)
    # Bare id pasted directly (e.g. "dQw4w9WgXcQ")
    if re.fullmatch(r"[0-9A-Za-z_-]{11}", url):
        return url
    return None


def _windows_chrome_from_registry() -> list[str]:
    """Resolve browser exes from the Windows `App Paths` registry keys.

    This is the most reliable discovery on Windows — it finds Chrome/Edge/Brave
    wherever they were installed, including per-user installs under
    %LOCALAPPDATA% that the hard-coded Program Files paths miss. Checked for both
    HKLM (machine-wide) and HKCU (per-user)."""
    found: list[str] = []
    try:
        import winreg  # type: ignore[import-not-found]
    except Exception:
        return found
    subkey_root = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"
    for exe in ("chrome.exe", "msedge.exe", "brave.exe", "chromium.exe"):
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(hive, subkey_root + "\\" + exe) as k:
                    val, _ = winreg.QueryValueEx(k, None)  # default value = full path
                    if val and Path(val).exists():
                        found.append(val)
            except (FileNotFoundError, OSError):
                continue
    return found


def _find_chrome() -> Optional[str]:
    """Locate a Chromium-family browser binary for the kiosk window.

    Windows is the primary target, so it gets the widest net: an explicit
    `_CHROME_BIN` override, the `App Paths` registry, per-user %LOCALAPPDATA%
    installs, both Program Files trees, and PATH — for Chrome, Edge, Brave and
    Chromium. Edge is preinstalled on Windows 10/11, so this should essentially
    always resolve there."""
    saved = os.environ.get("_CHROME_BIN")
    if saved and Path(saved).exists():
        return saved

    system = platform.system()
    candidates: list[str] = []

    if system == "Windows":
        # Registry first — handles per-user installs and non-default locations.
        candidates.extend(_windows_chrome_from_registry())
        local = os.environ.get("LOCALAPPDATA", "")
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        pfx86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        rel = [
            (r"Google\Chrome\Application\chrome.exe"),
            (r"Microsoft\Edge\Application\msedge.exe"),
            (r"BraveSoftware\Brave-Browser\Application\brave.exe"),
            (r"Chromium\Application\chrome.exe"),
        ]
        roots = [r for r in (pf, pfx86, local) if r]
        for root in roots:
            for r in rel:
                candidates.append(str(Path(root) / r))
        # PATH fallbacks (e.g. choco/scoop shims).
        candidates += ["chrome.exe", "msedge.exe", "brave.exe", "chrome", "msedge"]
    elif system == "Darwin":
        candidates = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        ]
    else:  # Linux / other
        candidates = [
            "google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
            "microsoft-edge", "microsoft-edge-stable", "brave-browser",
            "/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/microsoft-edge",
            "/snap/bin/chromium",
        ]

    for c in candidates:
        # A bare command name (no separator) → resolve via PATH; otherwise treat
        # as a literal path and test existence.
        has_sep = (os.path.sep in c) or bool(os.path.altsep and os.path.altsep in c)
        if has_sep:
            if Path(c).exists():
                log.info("YouTube TV: using browser %s", c)
                return c
        else:
            resolved = shutil.which(c)
            if resolved:
                log.info("YouTube TV: using browser %s (from PATH: %s)", resolved, c)
                return resolved

    log.warning(
        "YouTube TV: no Chromium-family browser found on %s. Tried registry + %d "
        "candidate path(s). Install Chrome/Edge or set _CHROME_BIN in .env.",
        system, len(candidates),
    )
    return None


def _launch_tv_browser(video_id: str) -> bool:
    """Open the /tv player page in a fullscreen Chrome kiosk on the host display.

    Uses an isolated --user-data-dir so we never disturb the user's Chrome and
    can kill exactly this instance later. --autoplay-policy lets the IFrame
    player start with sound without a user gesture. Returns False if no Chrome.
    """
    chrome = _find_chrome()
    if not chrome:
        return False
    # 127.0.0.1, NOT localhost. On Windows the hosts file resolves localhost to
    # both `::1` and `127.0.0.1`, and Chromium prefers IPv6 first; uvicorn binds
    # `0.0.0.0` (IPv4 only), so the kiosk hits ECONNREFUSED on `::1` and may
    # show an error page or hang long enough that the heartbeat watchdog fires
    # before the page ever loads. Pinning v4 sidesteps all of that. See
    # docs/GOTCHAS.md.
    url = f"http://127.0.0.1/tv?v={video_id}"
    args = [
        chrome,
        f"--user-data-dir={TV_CHROME_PROFILE}",
        "--no-first-run",
        "--no-default-browser-check",
        # Suppress the Edge / Chrome welcome / signin / promo modals that can
        # block a fresh --user-data-dir profile from rendering the requested URL.
        "--disable-features=msImplicitSignin,SigninInterceptBubbleV2,DesktopPWAsRunOnOsLogin",
        "--disable-fre",                # Edge first-run experience
        "--disable-default-apps",
        "--disable-component-update",
        "--noerrdialogs",               # no crash dialog if a prior kiosk crashed
        "--autoplay-policy=no-user-gesture-required",
        "--kiosk",
        f"--app={url}",
    ]
    kw: dict = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if platform.system() == "Windows":
        kw["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    else:
        kw["start_new_session"] = True
    try:
        subprocess.Popen(args, **kw)
        log.info("YouTube TV: launched kiosk (%s) for video %s", Path(chrome).name, video_id)
        return True
    except Exception as e:
        log.warning("YouTube TV: kiosk launch failed (%s): %s", chrome, e)
        return False


def _kill_tv_browser() -> None:
    """Kill the dedicated kiosk Chrome (matched by our --user-data-dir) — and
    only that instance, leaving the user's normal Chrome windows alone."""
    needle = str(TV_CHROME_PROFILE)
    for p in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmdline = p.info.get("cmdline") or []
            if any(needle in str(a) for a in cmdline):
                p.kill()
        except Exception:
            pass


# ── OS system volume ─────────────────────────────────────────────────────────
# YouTube plays through the host's normal audio mixer (the IFrame player has
# no direct VLC-style amp), so the dashboard volume slider must drive the OS
# system volume during YouTube playback — otherwise every video plays at
# whatever volume the host happened to be at (often max). On Stop we restore
# the OS volume to a configured "expected max" (`settings.system_volume_default`)
# so headphones at 100 % don't blow eardrums after a movie session.

# Last human-readable failure reason from the OS-volume path, surfaced in API
# responses (e.g. the YouTube volume endpoint) so the operator can act. On
# Windows it's set from the helper child's error reply; on POSIX it stays a
# generic message (pactl/osascript failures are rare).
_PYCAW_LAST_ERROR: Optional[str] = None

# ── Windows OS-mixer volume: isolated in a CHILD PROCESS ─────────────────────
# Driving the Windows endpoint volume via pycaw/COM *in-process* crashed the
# whole server: after a handful of rapid calls the process vanished with a
# native access violation and NO Python traceback. It crashed even when pinned
# to a single COM-initialized thread that never CoUninitialized. A native crash
# can only be contained by an OS process boundary, so all Windows volume ops now
# run in `winvol_helper.py` as a long-lived child: if it ever crashes, only the
# child dies and we respawn it — the server stays up. See docs/GOTCHAS.md.
WINVOL_HELPER = Path(__file__).parent / "winvol_helper.py"
_winvol_proc: Optional[subprocess.Popen] = None   # the live helper child
_winvol_lock: Optional[asyncio.Lock] = None       # serializes round-trips


def _winvol_roundtrip_sync(req: dict) -> dict:
    """Send one request to the helper child (spawning/respawning as needed) and
    return its parsed reply. Runs in a worker thread (blocking pipe IO). Raises
    on IO failure / dead child so the async caller can recover."""
    global _winvol_proc
    if _winvol_proc is None or _winvol_proc.poll() is not None:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if platform.system() == "Windows" else 0
        _winvol_proc = subprocess.Popen(
            [sys.executable, str(WINVOL_HELPER)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, bufsize=1, creationflags=flags,
        )
    proc = _winvol_proc
    try:
        proc.stdin.write(json.dumps(req) + "\n")
        proc.stdin.flush()
        line = proc.stdout.readline()
        if not line:
            raise RuntimeError("helper closed its pipe (child exited)")
        return json.loads(line.strip())
    except Exception:
        # Force a clean child on the next call.
        try:
            proc.kill()
        except Exception:
            pass
        _winvol_proc = None
        raise


async def _winvol_request(req: dict) -> Optional[dict]:
    """Async wrapper around the helper round-trip: serialized, time-bounded, and
    self-healing. Returns the reply dict, or None on failure (sets _PYCAW_LAST_ERROR)."""
    global _winvol_lock, _winvol_proc, _PYCAW_LAST_ERROR
    if _winvol_lock is None:
        _winvol_lock = asyncio.Lock()
    async with _winvol_lock:
        try:
            # First call pays the child's comtypes/pycaw import + CoInitialize
            # (a few hundred ms); later calls are fast. 5 s covers a cold start.
            resp = await asyncio.wait_for(
                asyncio.to_thread(_winvol_roundtrip_sync, req), timeout=5,
            )
        except Exception as e:
            # Timeout or IO error — kill the (possibly wedged) child so the next
            # call respawns a clean one, and remember why for the API surface.
            p = _winvol_proc
            if p is not None:
                try:
                    p.kill()
                except Exception:
                    pass
                _winvol_proc = None
            _PYCAW_LAST_ERROR = f"Windows volume helper failed: {type(e).__name__}: {e}"
            log.warning("%s", _PYCAW_LAST_ERROR)
            return None
        if resp and not resp.get("ok"):
            _PYCAW_LAST_ERROR = resp.get("error") or "Windows volume helper returned an error."
            log.warning("System volume: %s", _PYCAW_LAST_ERROR)
        return resp


def _set_system_volume_sync(pct: int) -> bool:
    """POSIX system-volume set (osascript / pactl / amixer). Windows is handled
    out-of-process via the helper child — see set_system_volume."""
    pct = max(0, min(100, int(pct)))
    sys_name = platform.system()
    try:
        if sys_name == "Darwin":
            subprocess.run(
                ["osascript", "-e", f"set volume output volume {pct}"],
                check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=3,
            )
            return True
        if sys_name == "Linux":
            if shutil.which("pactl"):
                subprocess.run(
                    ["pactl", "set-sink-mute", "@DEFAULT_SINK@", "0"],
                    check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=3,
                )
                subprocess.run(
                    ["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{pct}%"],
                    check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=3,
                )
                return True
            if shutil.which("amixer"):
                subprocess.run(
                    ["amixer", "-q", "set", "Master", f"{pct}%", "unmute"],
                    check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=3,
                )
                return True
    except Exception as e:
        log.warning("System volume set failed (%s, %d%%): %s", sys_name, pct, e)
    return False


def _get_system_volume_sync() -> Optional[int]:
    """POSIX system-volume read. Windows is handled out-of-process — see
    get_system_volume."""
    sys_name = platform.system()
    try:
        if sys_name == "Darwin":
            r = subprocess.run(
                ["osascript", "-e", "output volume of (get volume settings)"],
                capture_output=True, text=True, timeout=3,
            )
            if r.returncode == 0:
                return max(0, min(100, int(r.stdout.strip() or 0)))
        if sys_name == "Linux":
            if shutil.which("pactl"):
                r = subprocess.run(
                    ["pactl", "get-sink-volume", "@DEFAULT_SINK@"],
                    capture_output=True, text=True, timeout=3,
                )
                if r.returncode == 0:
                    m = re.search(r"(\d+)%", r.stdout)
                    if m:
                        return max(0, min(100, int(m.group(1))))
    except Exception:
        pass
    return None


async def set_system_volume(pct: int) -> bool:
    pct = max(0, min(100, int(pct)))
    if platform.system() == "Windows":
        resp = await _winvol_request({"op": "set", "pct": pct})
        return bool(resp and resp.get("ok"))
    return await asyncio.to_thread(_set_system_volume_sync, pct)


async def get_system_volume() -> Optional[int]:
    if platform.system() == "Windows":
        resp = await _winvol_request({"op": "get"})
        if resp and resp.get("ok"):
            return resp.get("value")
        return None
    return await asyncio.to_thread(_get_system_volume_sync)


async def _system_volume_default() -> int:
    """Configured system volume to restore when YouTube stops (0-100)."""
    lib = await get_library()
    raw = lib.get("settings", {}).get("system_volume_default", 70)
    return max(0, min(100, int(raw)))


async def _youtube_start_volume() -> int:
    """Configured OS volume to pre-set BEFORE a YouTube video starts (0-100).

    Sensible default 30 — quieter than restore-on-stop because the user is about
    to actively start a video and can turn up from there if they want, but a
    "max-by-default" first frame on a movie at 100 % blows out the room. Stored
    in `library.json → settings.youtube_start_volume`."""
    lib = await get_library()
    raw = lib.get("settings", {}).get("youtube_start_volume", 30)
    return max(0, min(100, int(raw)))


# Window title set by static/tv.html — used to find the kiosk window on Windows
# and pull it to the foreground (Chrome is multi-process, so PID matching is
# unreliable; the --app window's title is the page <title>). Keep both in sync.
_TV_WINDOW_MARKER = "StreamLink TV Player"


def _find_tv_browser_hwnds_windows() -> list:
    """Visible top-level windows whose title marks them as our kiosk page."""
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        found: list = []
        EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

        def _cb(hwnd, _):
            if user32.IsWindowVisible(hwnd):
                n = user32.GetWindowTextLengthW(hwnd)
                if n:
                    buf = ctypes.create_unicode_buffer(n + 1)
                    user32.GetWindowTextW(hwnd, buf, n + 1)
                    if _TV_WINDOW_MARKER in buf.value:
                        found.append(hwnd)
            return True

        cb = EnumWindowsProc(_cb)   # keep ref alive — ctypes GC pitfall
        user32.EnumWindows(cb, 0)
        return found
    except Exception:
        return []


def _focus_tv_browser_windows() -> bool:
    """Pull the kiosk window to the foreground past focus-stealing prevention.

    Same cocktail as `_vlc_focus_windows` (zero foreground-lock timeout +
    synthetic ALT + AttachThreadInput + SetForegroundWindow + clear taskbar
    flash). Returns True once a kiosk window exists (so the caller can stop
    retrying)."""
    hwnds = _find_tv_browser_hwnds_windows()
    if not hwnds:
        return False
    hwnd = hwnds[0]
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        SPI_SETFOREGROUNDLOCKTIMEOUT = 0x2001
        SPIF_SENDCHANGE = 0x02
        user32.SystemParametersInfoW(SPI_SETFOREGROUNDLOCKTIMEOUT, 0, 0, SPIF_SENDCHANGE)

        VK_MENU = 0x12
        KEYEVENTF_KEYUP = 0x0002
        user32.keybd_event(VK_MENU, 0, 0, 0)
        user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)

        fg_thread = user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), None)
        my_thread = kernel32.GetCurrentThreadId()
        if fg_thread != my_thread:
            user32.AttachThreadInput(my_thread, fg_thread, True)
        user32.ShowWindow(hwnd, 9)   # SW_RESTORE
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        if fg_thread != my_thread:
            user32.AttachThreadInput(my_thread, fg_thread, False)

        class FLASHWINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", ctypes.c_uint), ("hwnd", wintypes.HWND),
                ("dwFlags", wintypes.DWORD), ("uCount", wintypes.UINT),
                ("dwTimeout", wintypes.DWORD),
            ]
        FLASHW_STOP = 0x00000000
        for h in hwnds:
            info = FLASHWINFO(ctypes.sizeof(FLASHWINFO), h, FLASHW_STOP, 0, 0)
            user32.FlashWindowEx(ctypes.byref(info))
    except Exception:
        pass
    return True


async def _bring_tv_to_front(video_id: str) -> None:
    """Get VLC out of the way and pull the kiosk window to the foreground.

    On Windows the server isn't the foreground process, so the browser's window
    is blocked from taking focus (it just flashes in the taskbar) and the
    still-visible VLC window keeps the screen — the user has to click the taskbar
    icon. We minimize VLC, then poll for the kiosk window and force it forward
    for a few seconds (it takes ~1–2 s to appear). Best-effort; never raises."""
    await vlc_minimize()
    if platform.system() != "Windows":
        return  # macOS/Linux foreground the --app kiosk on their own
    loop = asyncio.get_running_loop()
    reinforced = 0
    for delay in [0.4] * 6 + [0.8] * 5 + [1.5] * 3:   # ~10 s, slowing cadence
        if not state.youtube_active or state.youtube_video_id != video_id:
            return  # superseded (stopped / different video)
        try:
            got = await loop.run_in_executor(None, _focus_tv_browser_windows)
        except Exception:
            got = False
        if got:
            reinforced += 1
            if reinforced >= 3:   # found + two reinforcing passes → settle
                return
        await asyncio.sleep(delay)


@app.get("/tv", include_in_schema=False)
async def tv_player_page() -> FileResponse:
    """The host-side kiosk page: YouTube IFrame player + SSE command listener."""
    return FileResponse(str(Path(__file__).parent / "static" / "tv.html"))


@app.post("/api/youtube")
async def youtube_play(req: YouTubeReq) -> JSONResponse:
    """Play a YouTube URL on the TV (host browser), remote-controlled from here.

    Not VPN-gated — this is ordinary HTTPS playback in a browser, not P2P.
    """
    video_id = _extract_youtube_id(req.url)
    if not video_id:
        raise HTTPException(400, "Not a recognisable YouTube link.")

    # Cancel any in-flight VLC pipeline so it can't grab the screen mid-launch.
    if state.stream_task and not state.stream_task.done():
        state.stream_task.cancel()
    if state.library_play_task and not state.library_play_task.done():
        state.library_play_task.cancel()

    # Snapshot the current OS volume so we can restore it on Stop if no default
    # is configured. Do this BEFORE we change anything below (so the snapshot is
    # the user's "before" state, not whatever we set later).
    cur_sys_vol = await get_system_volume()
    if cur_sys_vol is not None:
        state.system_volume_before_yt = cur_sys_vol

    # Pre-set the OS mixer to the configured "start" volume BEFORE the kiosk
    # loads the page — once the IFrame player begins producing audio (which can
    # be near-instantaneous after Chrome paints) the system mixer is already at
    # the right level, instead of whatever the OS was at (often max). If the
    # set call fails, we fall back to the user's current OS volume so the
    # dashboard slider isn't out of sync.
    start_vol = await _youtube_start_volume()
    initial_vol = start_vol if await set_system_volume(start_vol) else cur_sys_vol

    # Take over the "now playing" state. Title fills in once the /tv page reports
    # it back via /api/youtube/tv-state; show a placeholder until then.
    state.active_hash = None
    state.active_file = None
    state.library_item_id = None
    state.library_profile_id = None
    state.library_item_file_count = 0
    state.library_playlist = []
    state.library_current_file = None
    state.youtube_active = True
    state.youtube_video_id = video_id
    state.youtube_playback = "buffering"
    state.active_title = "YouTube"
    state.vlc_time = 0
    state.vlc_duration = 0
    state.stream_status = "playing"
    # During YouTube the dashboard volume slider drives the *system* volume
    # (0-100), not VLC's 0-200 amp. Initialise to the pre-set "start" volume
    # we just wrote (or the OS's current value if pycaw isn't usable on this
    # host) so the slider doesn't jump when the user first touches it.
    if initial_vol is not None:
        state.vlc_volume = initial_vol

    # Stop VLC + clear any idle background so only the browser shows on the TV.
    await vlc("pl_stop")

    # If the /tv page is already open (recent heartbeat), just hot-swap the video
    # via SSE — smoother than relaunching the whole kiosk. Otherwise launch it;
    # the fresh page reads ?v=<id> and autoplays even if it missed the broadcast.
    page_open = (time.time() - state.youtube_tv_seen_at) < 6.0
    await broadcast("yt_command", {"action": "load", "video_id": video_id})
    if not page_open:
        launch_at = time.time()
        launched = await asyncio.to_thread(_launch_tv_browser, video_id)
        if not launched:
            state.youtube_active = False
            state.youtube_video_id = None
            state.youtube_playback = ""
            state.active_title = None
            state.stream_status = "idle"
            await broadcast("state", state_snapshot())
            raise HTTPException(
                500,
                "No Chrome/Edge/Chromium browser found on the host to play YouTube "
                "on the TV. Install Google Chrome or Microsoft Edge (or set "
                "_CHROME_BIN in .env). See logs/streamlink_app.log for details.",
            )
        # The browser was spawned, but Popen can't tell us it actually came up
        # (locked profile, instant exit, …). The /tv page heartbeats within ~1 s
        # of loading, so if nothing checks in we know the kiosk never rendered —
        # surface that instead of silently dropping back to the idle background.
        asyncio.create_task(_youtube_kiosk_healthcheck(video_id, launch_at))

    # Minimize VLC and pull the kiosk window to the foreground. On Windows the
    # server can't give the new browser window focus (focus-stealing prevention),
    # so without this the kiosk just flashes in the taskbar behind VLC and the
    # user has to click it. Covers both fresh-launch and hot-swap (user may have
    # alt-tabbed away from an already-open kiosk).
    asyncio.create_task(_bring_tv_to_front(video_id))

    # Note: we deliberately do NOT broadcast a `stream_status` event here — the
    # client's stream_status handler phrases playing as "Now playing in VLC".
    # The `state` snapshot below already carries stream_status="playing".
    await broadcast("state", state_snapshot())
    return JSONResponse({"ok": True, "video_id": video_id}, status_code=202)


async def _youtube_kiosk_healthcheck(video_id: str, launch_at: float) -> None:
    """If the freshly-launched kiosk never heartbeats, the browser didn't render
    the /tv page — report a clear error rather than leaving the TV blank."""
    await asyncio.sleep(12)
    # Superseded (stopped, or a different video started) → nothing to check.
    if not state.youtube_active or state.youtube_video_id != video_id:
        return
    if state.youtube_tv_seen_at >= launch_at:
        return  # the page checked in — all good
    log.warning(
        "YouTube TV: kiosk for %s launched but never reported in within 12 s — "
        "the browser likely failed to open the /tv page (locked profile, blocked "
        "network, or session-0 service with no desktop).", video_id,
    )
    state.youtube_active = False
    state.youtube_video_id = None
    state.youtube_playback = ""
    state.active_title = None
    state.stream_status = "idle"
    await broadcast("stream_status", {
        "status": "error",
        "message": "YouTube didn't start on the TV — the browser failed to open. "
                   "See logs/streamlink_app.log.",
    })
    await broadcast("state", state_snapshot())


@app.post("/api/youtube/control")
async def youtube_control(req: YouTubeControlReq) -> JSONResponse:
    """Drive the kiosk player from the dashboard.

    Playback actions (play/pause/seek) are relayed to the `/tv` page over SSE.
    Volume actions are handled **server-side** by setting the host's OS volume
    (the IFrame player has no real amp; its setVolume only scales the audio it
    emits *before* the system mixer), so the dashboard slider behaves like the
    user expects on the TV — and a configured default is restored on Stop.

    action ∈ {playpause, play, pause, seek, seek_to, volume_set, volume_step}.
    """
    if not state.youtube_active:
        raise HTTPException(409, "No YouTube video is playing on the TV.")
    if req.action not in ("playpause", "play", "pause", "seek", "seek_to",
                          "volume_set", "volume_step"):
        raise HTTPException(400, f"Unknown action: {req.action}")

    if req.action in ("volume_set", "volume_step"):
        # Volume slider sends a dashboard value (historically 0-200 for VLC); for
        # YouTube clamp to the OS scale 0-100. For volume_step, read the OS
        # volume so an out-of-sync client doesn't snap us back to a stale value.
        if req.action == "volume_set":
            target = max(0, min(100, int(req.value or 0)))
        else:
            cur = await get_system_volume()
            base = cur if cur is not None else (state.vlc_volume or 50)
            target = max(0, min(100, int(base) + int(req.value or 0)))
        ok = await set_system_volume(target)
        if ok:
            state.vlc_volume = target
            await broadcast("state", state_snapshot())
            return JSONResponse({"ok": True, "volume": target})
        # Failure: surface *why* so the user can act (most often: pycaw not
        # installed on Windows, or the helper child errored). _PYCAW_LAST_ERROR
        # is set from the helper's error reply on Windows; on POSIX we fall back
        # to a generic message since pactl/osascript failures are rare.
        err = _PYCAW_LAST_ERROR if platform.system() == "Windows" else \
            "Couldn't set the host system volume."
        return JSONResponse({"ok": False, "volume": target, "error": err},
                            status_code=503)

    await broadcast("yt_command", {"action": req.action, "value": req.value})
    return JSONResponse({"ok": True})


@app.post("/api/youtube/tv-state")
async def youtube_tv_state(req: YouTubeTvStateReq) -> JSONResponse:
    """Heartbeat + playback report from the /tv page. Mirrors the player's
    position/duration/volume/title onto the reused display fields and rebroadcasts
    so every dashboard reflects the TV instantly."""
    state.youtube_tv_seen_at = time.time()
    if not state.youtube_active:
        # A stale page still beating after Stop — tell it to close itself.
        return JSONResponse({"ok": True, "active": False})

    if req.video_id:
        state.youtube_video_id = req.video_id
    if req.title and req.title.strip():
        state.active_title = req.title.strip()
    if req.time is not None:
        state.vlc_time = int(req.time)
    if req.duration is not None:
        state.vlc_duration = int(req.duration)
    # Intentionally ignore req.volume here. During YouTube the *system mixer* is
    # the real amp — the IFrame `player.getVolume()` only describes the player's
    # pre-mixer gain (which we lock at 100 in tv.html), so reading it back over
    # the heartbeat would just stomp the dashboard's authoritative OS-volume
    # value with 100 every second. /api/youtube/control owns state.vlc_volume.
    if req.playback:
        state.youtube_playback = req.playback
    await broadcast("state", state_snapshot())
    return JSONResponse({"ok": True, "active": True})


@app.post("/api/stop")
async def stop() -> JSONResponse:
    # Cancel any in-flight pipelines first so a buffering stream doesn't race the teardown
    if state.stream_task and not state.stream_task.done():
        state.stream_task.cancel()
    if state.library_play_task and not state.library_play_task.done():
        state.library_play_task.cancel()

    # Snapshot the cleanup targets before clearing state so the background task knows what to do
    active_hash = state.active_hash
    library_item_id = state.library_item_id
    prepare_hash = state.prepare_hash
    was_youtube = state.youtube_active

    # Clear state + broadcast idle immediately — the UI updates before slow qBit/VLC roundtrips run
    state.active_hash = None
    state.active_file = None
    state.active_title = None
    state.stream_status = "idle"
    state.youtube_active = False
    state.youtube_video_id = None
    state.youtube_playback = ""
    state.vlc_time = 0
    state.vlc_duration = 0
    state.library_item_id = None
    state.library_profile_id = None
    state.library_item_file_count = 0
    state.library_playlist = []
    state.library_current_file = None
    state.progress = 0.0
    state.downloaded_mb = 0.0
    state.total_mb = 0.0
    state.dl_speed_bps = 0
    state.ul_speed_bps = 0
    state.skip_offer = None
    state.skip_offer_file = None
    state.resume_offer = None
    state.prepare_hash = None
    await _cancel_skip_countdown()

    # Tell the TV page to pause + close itself, then hard-kill the kiosk browser
    # so a YouTube play doesn't linger on the TV after Stop.
    if was_youtube:
        await broadcast("yt_command", {"action": "close"})

    await broadcast("stream_status", {"status": "idle", "message": "Stopped."})
    await broadcast("state", state_snapshot())

    # Snapshot the system-volume restore target *before* clearing state — we
    # need the pre-stop snapshot to fall back on if no default is configured.
    yt_sys_vol_snapshot = state.system_volume_before_yt
    state.system_volume_before_yt = None

    async def _stop_cleanup(ah: Optional[str], lid: Optional[str], ph: Optional[str],
                            yt: bool, sys_vol_snapshot: Optional[int]) -> None:
        try:
            if ah and not lid:
                await qbit_delete(ah)
            if ph:
                await qbit_delete(ph, delete_files=True)
            if yt:
                # Give the page a beat to pause via the SSE 'close' command, then
                # kill the dedicated kiosk Chrome instance (matched by profile dir).
                await asyncio.sleep(0.4)
                await asyncio.to_thread(_kill_tv_browser)
                # Verify the kiosk is actually gone before we touch the OS
                # volume — otherwise we'd be turning the still-playing YouTube
                # video's volume up/down underneath the user. Poll a few times.
                deadline = time.monotonic() + 4.0
                needle = str(TV_CHROME_PROFILE)
                while time.monotonic() < deadline:
                    still = False
                    for p in psutil.process_iter(["cmdline"]):
                        try:
                            cl = p.info.get("cmdline") or []
                            if any(needle in str(a) for a in cl):
                                still = True
                                break
                        except Exception:
                            pass
                    if not still:
                        break
                    await asyncio.to_thread(_kill_tv_browser)
                    await asyncio.sleep(0.3)
                # Now safe to restore the OS volume. Prefer the admin-configured
                # default (the user's "expected max"); fall back to the pre-YT
                # snapshot if no default is set.
                target = await _system_volume_default()
                if target is None and sys_vol_snapshot is not None:
                    target = sys_vol_snapshot
                if target is not None:
                    await set_system_volume(target)
                    log.info("YouTube TV: restored system volume to %d%%", target)
            await vlc("pl_stop")
            # Empty the playlist too — pl_stop leaves the enqueued episodes in
            # the list, and a leftover entry can later win an auto-advance and
            # play an episode instead of the bg video. See vlc_clear_playlist().
            await vlc_clear_playlist()
            await vlc_minimize()
        except Exception:
            pass

    asyncio.create_task(_stop_cleanup(active_hash, library_item_id, prepare_hash,
                                       was_youtube, yt_sys_vol_snapshot))
    return JSONResponse({"ok": True}, status_code=202)


@app.post("/api/retry")
async def retry_playback() -> JSONResponse:
    """Relaunch VLC and replay the current file (handles VLC freezes / crashes)."""
    file_path: Optional[Path] = None
    if state.library_current_file:
        file_path = Path(state.library_current_file)
    elif state.active_file:
        file_path = state.active_file
    if not file_path:
        raise HTTPException(400, "Nothing to retry.")
    asyncio.create_task(_retry_task(file_path))
    return JSONResponse({"ok": True})


@app.post("/api/vlc/pause")
async def pause() -> JSONResponse:
    await vlc("pl_pause")
    return JSONResponse({"ok": True})


async def _global_max_volume() -> int:
    """Return the global max-volume cap (0-200). Defaults to 200 = no cap."""
    lib = await get_library()
    raw = lib.get("settings", {}).get("max_volume", 200)
    return max(0, min(200, int(raw)))


async def _global_vlc_start_volume_pct() -> int:
    """Return the VLC startup volume as a % of the max-volume cap (0-100).

    Defaults to 50 (half the cap), preserving the historical 1/2-max behaviour.
    """
    lib = await get_library()
    raw = lib.get("settings", {}).get("vlc_start_volume", 50)
    return max(0, min(100, int(raw)))


@app.post("/api/vlc/volume/set")
async def volume_set(volume: int) -> JSONResponse:
    # volume is 0-200 (100 = normal); VLC uses 0-512 (256 = 100%)
    cap = await _global_max_volume()
    capped = max(0, min(cap, max(0, min(200, volume))))
    raw = max(0, min(512, round(capped / 100 * 256)))
    await vlc("volume", val=str(raw))
    state.vlc_volume = capped
    return JSONResponse({"ok": True, "volume": capped, "max_volume": cap})


@app.post("/api/vlc/volume/{direction}")
async def volume(direction: str, step: int = 10) -> JSONResponse:
    if direction not in ("up", "down"):
        raise HTTPException(400, "direction must be 'up' or 'down'")
    # Server-authoritative relative adjustment so out-of-sync clients can't
    # snap volume back to a stale value (e.g. phone unlocks showing 50 while
    # actual is 35 — pressing − must apply -step to 35, not jump to 45).
    magnitude = max(0, min(200, abs(int(step))))
    delta = magnitude if direction == "up" else -magnitude
    cap = await _global_max_volume()
    next_vol = max(0, min(cap, state.vlc_volume + delta))
    raw = max(0, min(512, round(next_vol / 100 * 256)))
    await vlc("volume", val=str(raw))
    state.vlc_volume = next_vol
    return JSONResponse({"ok": True, "volume": next_vol, "max_volume": cap})


@app.post("/api/vlc/seek")
async def seek(delta: float) -> JSONResponse:
    """Seek relative to current position.  delta is seconds; negative = rewind."""
    sign = "+" if delta >= 0 else ""
    await vlc("seek", val=f"{sign}{int(delta)}s")
    return JSONResponse({"ok": True})


@app.post("/api/vlc/seek/to")
async def seek_to(position_pct: float) -> JSONResponse:
    """Seek to an absolute position (0–100 %)."""
    pct = max(0.0, min(100.0, position_pct))
    await vlc("seek", val=f"{pct:.2f}%")
    return JSONResponse({"ok": True})


async def _item_all_paths() -> list[str]:
    """Return the full sorted file path list for the currently playing library item."""
    if not state.library_item_id:
        return []
    lib = await get_library()
    item = next((it for it in lib["items"] if it["id"] == state.library_item_id), None)
    return [f["path"] for f in item.get("files", [])] if item else []


def _find_in_paths(current: str, paths: list[str]) -> int:
    """Return the index of current in paths, trying exact match then resolved match."""
    try:
        return paths.index(current)
    except ValueError:
        pass
    try:
        resolved = Path(current).resolve()
        for i, p in enumerate(paths):
            try:
                if Path(p).resolve() == resolved:
                    return i
            except Exception:
                continue
    except Exception:
        pass
    return -1


async def _vlc_relaunch_playlist(playlist: list[str], target_name: str) -> None:
    """Background VLC handoff used by prev/next so slow VLC roundtrips don't block the response.

    Fires `in_play` detached and flips `stream_status` to "playing" the moment a
    `status.json` poll shows VLC is actually on the new file — not when in_play's
    (often multi-second) HTTP reply finally comes back, and not after the whole
    playlist has been enqueued. Either of those would pin the UI to "buffering"
    for seconds while the user is already watching the new episode.
    """
    first = Path(playlist[0])
    first_resolved = first.resolve()
    # Clear stale playlist items (a leftover bg entry could otherwise win an
    # end-of-file auto-advance and play the bg video instead of this episode).
    await vlc_clear_playlist()
    play_task = asyncio.create_task(vlc("in_play", input=first_resolved.as_uri()))
    try:
        await _vlc_wait_until_ready(first_resolved, timeout=10.0)
        state.stream_status = "playing"
        await broadcast("stream_status", {"status": "playing", "message": f"Playing: {target_name}"})
        await broadcast("state", state_snapshot())
        if state.library_item_id and state.library_profile_id:
            asyncio.create_task(_apply_track_prefs(
                state.library_item_id, state.library_profile_id, playlist[0], delay=2.0,
            ))
        rest = playlist[1:]
        if rest:
            try:
                await play_task
            except Exception:
                pass
            await asyncio.gather(
                *(vlc("in_enqueue", input=Path(p).resolve().as_uri()) for p in rest),
                return_exceptions=True,
            )
    except asyncio.CancelledError:
        play_task.cancel()
        raise
    except Exception as e:
        play_task.cancel()
        state.stream_status = "error"
        await broadcast("stream_status", {"status": "error", "message": f"Playback failed: {e}"})
        await broadcast("state", state_snapshot())


@app.post("/api/vlc/prev")
async def vlc_prev() -> JSONResponse:
    """Jump to the previous episode in series order, regardless of how playback was started."""
    current = state.library_current_file
    if not current:
        raise HTTPException(400, "No active playback.")

    # Try the active playlist first, then fall back to the item's full file list
    prev_file: Optional[str] = None
    new_tail: list[str] = []

    idx = _find_in_paths(current, state.library_playlist)
    if idx > 0:
        prev_file = state.library_playlist[idx - 1]
        new_tail  = state.library_playlist[idx - 1:]
    else:
        all_paths = await _item_all_paths()
        item_idx  = _find_in_paths(current, all_paths)
        if item_idx > 0:
            prev_file = all_paths[item_idx - 1]
            new_tail  = all_paths[item_idx - 1:]

    if not prev_file:
        raise HTTPException(400, "Already at first episode.")
    if not Path(prev_file).exists():
        raise HTTPException(400, f"File not found: {Path(prev_file).name}")

    prior = state.library_play_task
    if prior and not prior.done():
        prior.cancel()

    await _cancel_skip_countdown()
    state.library_playlist     = new_tail
    state.library_current_file = prev_file
    state.current_audio_track  = -1
    state.current_subtitle_track = -1
    state.track_pref_applied_file = prev_file
    state.skip_offer = None
    state.skip_offer_file = None
    state.stream_status = "buffering"

    await broadcast("stream_status", {"status": "buffering", "message": f"Loading: {Path(prev_file).name}"})
    await broadcast("state", state_snapshot())

    state.library_play_task = asyncio.create_task(_vlc_relaunch_playlist(new_tail, Path(prev_file).name))
    return JSONResponse({"ok": True}, status_code=202)


@app.post("/api/vlc/next")
async def vlc_next() -> JSONResponse:
    """Jump to the next episode in series order, regardless of how playback was started."""
    current = state.library_current_file
    if not current:
        raise HTTPException(400, "No active playback.")

    # Try the active playlist first, then fall back to the item's full file list
    next_file: Optional[str] = None
    new_tail: list[str] = []

    idx = _find_in_paths(current, state.library_playlist)
    if 0 <= idx < len(state.library_playlist) - 1:
        next_file = state.library_playlist[idx + 1]
        new_tail  = state.library_playlist[idx + 1:]
    else:
        all_paths = await _item_all_paths()
        item_idx  = _find_in_paths(current, all_paths)
        if 0 <= item_idx < len(all_paths) - 1:
            next_file = all_paths[item_idx + 1]
            new_tail  = all_paths[item_idx + 1:]

    if not next_file:
        raise HTTPException(400, "Already at last episode.")
    if not Path(next_file).exists():
        raise HTTPException(400, f"File not found: {Path(next_file).name}")

    prior = state.library_play_task
    if prior and not prior.done():
        prior.cancel()

    await _cancel_skip_countdown()
    state.library_playlist     = new_tail
    state.library_current_file = next_file
    state.current_audio_track  = -1
    state.current_subtitle_track = -1
    state.track_pref_applied_file = next_file
    state.skip_offer = None
    state.skip_offer_file = None
    state.stream_status = "buffering"

    await broadcast("stream_status", {"status": "buffering", "message": f"Loading: {Path(next_file).name}"})
    await broadcast("state", state_snapshot())

    state.library_play_task = asyncio.create_task(_vlc_relaunch_playlist(new_tail, Path(next_file).name))
    return JSONResponse({"ok": True}, status_code=202)


def _parse_track_streams(vs: Optional[dict]) -> tuple[list[dict], list[dict]]:
    """Parse a VLC status payload into (audio, subtitle) track lists.

    Each entry is {id, label, language, codec}. `id` is the ES (elementary
    stream) ID — the N in each 'Stream N' key, the same value VLC's
    audio_track / subtitle_track commands take (a sequential counter would
    silently fail). Shared by the /api/vlc/tracks endpoint and the playback
    subtitle-default policy (`_apply_track_prefs`), so they agree on ES IDs.
    """
    audio: list[dict] = []
    subtitle: list[dict] = []
    if not vs:
        return audio, subtitle
    cat = vs.get("information", {}).get("category", {})
    # Sort numerically by stream index so we process in file order.
    stream_keys = sorted(
        (k for k in cat if k.startswith("Stream")),
        key=lambda k: int(k.split()[-1]) if k.split()[-1].isdigit() else 999,
    )
    audio_n = sub_n = 1   # display-only counters for fallback labels
    for key in stream_keys:
        try:
            es_id = int(key.split()[-1])
        except (ValueError, IndexError):
            continue
        s     = cat[key]
        typ   = s.get("Type", "")
        lang  = s.get("Language", s.get("language", ""))
        codec = s.get("Codec", s.get("codec", ""))
        if typ == "Audio":
            audio.append({"id": es_id, "label": lang or codec or f"Track {audio_n}",
                          "language": lang, "codec": codec})
            audio_n += 1
        elif typ == "Subtitle":
            subtitle.append({"id": es_id, "label": lang or codec or f"Track {sub_n}",
                             "language": lang, "codec": codec})
            sub_n += 1
    return audio, subtitle


async def _vlc_subtitle_tracks() -> list[dict]:
    """Live subtitle tracks VLC currently knows about (embedded + any loaded
    sidecars), each {id, label, language, codec}. Excludes the synthetic 'Off'."""
    _, subtitle = _parse_track_streams(await vlc_status())
    return subtitle


@app.get("/api/vlc/tracks")
async def get_tracks() -> JSONResponse:
    """Return available audio/subtitle tracks and which are currently selected.

    Track IDs are the actual ES (elementary stream) IDs from VLC. The
    <audiotrack> / <subtitletrack> XML values are also ES IDs, so they must be
    compared against the same ES IDs for the 'current' highlight to work.
    """
    vs = await vlc_status()
    audio, subs = _parse_track_streams(vs)
    subtitle = [{"id": -1, "label": "Off", "language": ""}] + subs

    # VLC 3.x does not expose current track selection in its HTTP API
    # (no <audiotrack>/<subtitletrack> in status.xml). We track it ourselves.
    return JSONResponse({
        "audio":    audio,
        "subtitle": subtitle,
        "current_audio":    state.current_audio_track,
        "current_subtitle": state.current_subtitle_track,
        "time":   vs.get("time",   0) if vs else 0,
        "length": vs.get("length", 0) if vs else 0,
    })


@app.post("/api/vlc/track/audio/{track_id}")
async def set_audio_track(track_id: int) -> JSONResponse:
    state.current_audio_track = track_id
    await vlc("audio_track", val=str(track_id))
    if state.library_item_id and state.library_profile_id and state.library_current_file:
        asyncio.create_task(_save_track_pref(
            state.library_item_id, state.library_profile_id,
            state.library_current_file, audio=track_id,
        ))
    return JSONResponse({"ok": True})


@app.post("/api/vlc/track/subtitle/{track_id}")
async def set_subtitle_track(track_id: int) -> JSONResponse:
    state.current_subtitle_track = track_id
    state.sub_auto_ai_path = ""                    # explicit pick → not auto-AI
    await vlc("subtitle_track", val=str(track_id))
    if state.library_item_id and state.library_profile_id and state.library_current_file:
        asyncio.create_task(_save_track_pref(
            state.library_item_id, state.library_profile_id,
            state.library_current_file, subtitle=track_id,
        ))
        asyncio.create_task(_remember_vlc_sub_pick(track_id))
    return JSONResponse({"ok": True})


async def _remember_vlc_sub_pick(track_id: int) -> None:
    """Best-effort: record the viewer's manual VLC subtitle pick as a per-series
    descriptor so the same language (or 'off') comes back on the next episode.
    VLC rarely tags loaded sidecars with a language, so an untaggable pick simply
    isn't remembered (the on-device player carries the richer descriptor)."""
    item_id, profile_id = state.library_item_id, state.library_profile_id
    if not (item_id and profile_id):
        return
    if track_id is None or track_id < 0:
        sel: dict = {"off": True}
    else:
        lang = ""
        for t in await _vlc_subtitle_tracks():
            if t.get("id") == track_id:
                lang = _canon_lang(t.get("language", ""))
                break
        if not lang:
            return
        sel = {"off": False, "lang": lang, "ai": False, "name": ""}
    lib = await get_library()
    item = next((it for it in lib["items"] if it["id"] == item_id), None)
    if item:
        await _save_series_sub_sel(profile_id, _series_of_item(item), sel)


@app.get("/api/subtitles/search")
async def search_subtitles(query: str = "", lang: str = "") -> JSONResponse:
    """Find subtitles for the file VLC is playing — by movie hash (exact) and by
    name (fallback). `query` overrides the auto-derived name.

    `lang` is the OpenSubtitles language filter: a 3-letter code, the literal
    `"all"` for every language, or blank to fall back to the admin's preferred
    subtitle language (`settings.subtitles.default_language`). Defaulting to the
    preferred language is what surfaces English instead of burying it under a
    handful of foreign hits. The effective filter is echoed back as `lang`."""
    video = await _current_playback_path()
    file_hash: Optional[str] = None
    file_size: Optional[int] = None
    file_name = ""
    if video:
        file_name = video.name
        file_size = video.stat().st_size
        file_hash = await asyncio.to_thread(_opensubtitles_hash, video)
    q = query.strip() or (video.stem if video else "")
    if not file_hash and not q:
        raise HTTPException(409, "Nothing is playing and no search query was given.")
    sel = lang.strip().lower()
    if sel == "all":
        sel = ""                                       # explicit all-languages
    elif not sel:
        sel = _subs_cfg(await get_library())["default_language"]   # default → preferred
    results = await _opensubtitles_search(file_hash, file_size, q, sel)
    return JSONResponse({"file": file_name, "hash": file_hash, "lang": sel, "results": results})


class SubtitleDownloadReq(BaseModel):
    download_link: str
    lang: str = ""


async def _download_and_attach_subtitle(
    video: Path, link: str, lang: str, save_pref: bool = True,
) -> tuple[Optional[int], Optional[Path]]:
    """Download an OpenSubtitles .gz/.srt, save it as a sidecar next to `video`,
    load it into VLC via `addsubtitle`, and select the newly-added track.

    Returns (selected ES ID, saved sidecar path). Shared by the manual download
    endpoint (`save_pref=True` → persists the pick as a per-file preference) and
    the playback auto-search (`save_pref=False` → the choice stays a live policy
    decision, so a later profile/admin subs-off toggle still wins). Raises
    httpx/OS errors for the caller to map; the auto-search wrapper swallows them.
    """
    headers = {"User-Agent": settings.opensubtitles_user_agent}
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as c:
        r = await c.get(link, headers=headers)
    r.raise_for_status()
    data = r.content
    if data[:2] == b"\x1f\x8b":       # gzip magic — OpenSubtitles serves .gz
        data = gzip.decompress(data)

    safe = re.sub(r"[^a-zA-Z]", "", lang)[:5].lower() or "sub"
    dest = video.with_name(f"{video.stem}.{safe}.srt")
    n = 2
    while dest.exists():
        dest = video.with_name(f"{video.stem}.{safe}.{n}.srt")
        n += 1
    dest.write_bytes(data)

    # Load into VLC, then select the newly added subtitle track (highest ES ID).
    await vlc("addsubtitle", val=str(dest.resolve()))
    await asyncio.sleep(0.6)
    new_id: Optional[int] = None
    subs = await _vlc_subtitle_tracks()
    if subs:
        new_id = max(s["id"] for s in subs)
        state.current_subtitle_track = new_id
        await vlc("subtitle_track", val=str(new_id))
        if save_pref and state.library_item_id and state.library_profile_id and state.library_current_file:
            asyncio.create_task(_save_track_pref(
                state.library_item_id, state.library_profile_id,
                state.library_current_file, subtitle=new_id,
            ))
    return new_id, dest


async def _auto_fetch_subtitle(video: Path, lang: str) -> Optional[int]:
    """Search OpenSubtitles for a `lang` subtitle for `video` and load the best
    match into VLC. Best-effort: returns the selected ES ID or None. Used by the
    playback subtitle-default policy when no preferred-language track is present."""
    try:
        file_size = video.stat().st_size
        file_hash = await asyncio.to_thread(_opensubtitles_hash, video)
        results = await _opensubtitles_search(file_hash, file_size, video.stem, lang)
        results = [r for r in results if r.get("download_link")]
        if not results:
            return None
        new_id, _ = await _download_and_attach_subtitle(
            video, results[0]["download_link"], lang, save_pref=False)
        return new_id
    except Exception:
        return None


@app.post("/api/subtitles/download")
async def download_subtitle(req: SubtitleDownloadReq) -> JSONResponse:
    """Download a chosen subtitle, save it next to the playing video, and load it
    into VLC as a new (and selected) subtitle track."""
    link = req.download_link.strip()
    host = (urlparse(link).hostname or "").lower()
    if not (host == "opensubtitles.org" or host.endswith(".opensubtitles.org")):
        raise HTTPException(400, "Invalid subtitle source.")
    video = await _current_playback_path()
    if not video:
        raise HTTPException(409, "No file is currently playing.")
    try:
        new_id, dest = await _download_and_attach_subtitle(video, link, req.lang, save_pref=True)
    except OSError as e:
        raise HTTPException(500, f"Could not save subtitle file: {e}")
    except Exception as e:
        raise HTTPException(502, f"Subtitle download failed: {e}")
    return JSONResponse({"ok": True, "saved": dest.name if dest else None, "subtitle_track": new_id})


@app.get("/api/state")
async def get_state() -> JSONResponse:
    return JSONResponse(state_snapshot())


@app.get("/api/version")
async def get_ui_version() -> JSONResponse:
    # no-cache so an out-of-date browser can detect that its cached HTML is stale
    return JSONResponse(
        {"version": UI_VERSION},
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


async def _all_library_paths() -> list[dict]:
    """All configured library paths: static (.env) + dynamic (UI-added via library.json)."""
    lib = await get_library()
    dynamic: list[str] = lib.get("settings", {}).get("library_paths", [])
    seen: set[str] = set()
    result = []
    for raw, is_static in [
        (settings.qbit_download_path, True),
        (settings.library_path_2, True),
        (settings.library_path_3, True),
        (settings.library_path_4, True),
        *((p, False) for p in dynamic),
    ]:
        p = (raw or "").strip()
        if p and p not in seen:
            seen.add(p)
            result.append({"path": p, "label": Path(p).name or p, "static": is_static})
    return result


@app.get("/api/settings/download-path")
async def get_download_path() -> JSONResponse:
    return JSONResponse({"path": settings.qbit_download_path})


@app.get("/api/settings/library-paths")
async def get_library_paths_api() -> JSONResponse:
    return JSONResponse({"paths": await _all_library_paths()})


@app.post("/api/settings/library-paths")
async def add_library_path(path: str) -> JSONResponse:
    p = path.strip()
    if not p:
        raise HTTPException(400, "Path cannot be empty.")
    if not Path(p).is_dir():
        raise HTTPException(400, f"Directory does not exist: {p}")
    lib = await get_library()
    existing = [info["path"] for info in await _all_library_paths()]
    if p in existing:
        raise HTTPException(400, "Path is already configured.")
    lib.setdefault("settings", {}).setdefault("library_paths", []).append(p)
    await put_library(lib)
    return JSONResponse({"ok": True})


@app.delete("/api/settings/library-paths")
async def remove_library_path(path: str) -> JSONResponse:
    lib = await get_library()
    paths: list = lib.get("settings", {}).get("library_paths", [])
    if path not in paths:
        raise HTTPException(404, "Path not found in UI-configured paths (static .env paths cannot be removed here).")
    paths.remove(path)
    await put_library(lib)
    return JSONResponse({"ok": True})


@app.get("/api/settings/disk-space")
async def get_disk_space() -> JSONResponse:
    disks = []
    for info in await _all_library_paths():
        try:
            usage = await asyncio.to_thread(shutil.disk_usage, info["path"])
            disks.append({
                "path":        info["path"],
                "label":       info["label"],
                "total_bytes": usage.total,
                "free_bytes":  usage.free,
                "total_human": human_size(usage.total),
                "free_human":  human_size(usage.free),
                "free_pct":    round(usage.free / usage.total * 100, 1) if usage.total else 0,
            })
        except Exception as e:
            disks.append({"path": info["path"], "label": info["label"], "error": str(e)})
    primary = disks[0] if disks else {}
    return JSONResponse({**primary, "disks": disks})


@app.get("/api/library/{item_id}/download")
async def download_library_file(item_id: str, file_path: str = "") -> FileResponse:
    """Stream a library file to the browser for download.

    For single-file items the path can be omitted; for multi-file items pass
    the absolute file_path as a query parameter.
    """
    lib = await get_library()
    item = next((it for it in lib["items"] if it["id"] == item_id), None)
    if not item:
        raise HTTPException(404, "Item not found.")

    files = item.get("files", [])
    if not files:
        raise HTTPException(404, "No files associated with this item.")

    if file_path:
        target = next((f for f in files if f["path"] == file_path), None)
        if not target:
            raise HTTPException(404, "File not found in this item.")
        path = Path(target["path"])
    else:
        if len(files) != 1:
            raise HTTPException(400, "Multiple files — pass file_path query parameter.")
        path = Path(files[0]["path"])

    if not path.exists():
        raise HTTPException(404, f"File not on disk: {path.name}")

    return FileResponse(
        path=str(path),
        filename=path.name,
        media_type="application/octet-stream",
    )


class ZipDownloadReq(BaseModel):
    file_paths: list[str] = []   # empty = all files in item


@app.post("/api/library/{item_id}/download-zip")
async def download_library_zip(item_id: str, req: ZipDownloadReq) -> StreamingResponse:
    """Stream a ZIP of selected (or all) library files to the browser.

    Uses a pipe so the ZIP is streamed without buffering entire video files in memory.
    ZIP_STORED is used — video files are already compressed.
    """
    lib = await get_library()
    item = next((it for it in lib["items"] if it["id"] == item_id), None)
    if not item:
        raise HTTPException(404, "Item not found.")

    all_files = item.get("files", [])
    if req.file_paths:
        valid_paths = {f["path"] for f in all_files}
        targets = [p for p in req.file_paths if p in valid_paths and Path(p).exists()]
    else:
        targets = [f["path"] for f in all_files if Path(f["path"]).exists()]

    if not targets:
        raise HTTPException(404, "No files available for download.")

    zip_name = re.sub(r'[\\/*?:"<>|]', "_", item["title"]) + ".zip"

    # Compute expected ZIP size (ZIP_STORED = no compression, so size is predictable).
    # Local header: 30 + len(name); central dir entry: 46 + len(name); EOCD: 22.
    total_bytes = 22
    for p_str in targets:
        name_len = len(Path(p_str).name.encode())
        size = Path(p_str).stat().st_size
        total_bytes += size + (30 + name_len) + (46 + name_len)

    r_fd, w_fd = os.pipe()

    def _write_zip() -> None:
        try:
            with os.fdopen(w_fd, "wb") as wf, zipfile.ZipFile(wf, "w", zipfile.ZIP_STORED) as zf:
                for path_str in targets:
                    p = Path(path_str)
                    zf.write(str(p), p.name)
        except Exception:
            pass  # reader will get EOF; any partial data is discarded by the browser

    threading.Thread(target=_write_zip, daemon=True).start()

    async def _read_pipe() -> AsyncGenerator[bytes, None]:
        rf = os.fdopen(r_fd, "rb")
        try:
            while True:
                chunk = await asyncio.to_thread(rf.read, 65536)
                if not chunk:
                    break
                yield chunk
        finally:
            rf.close()

    return StreamingResponse(
        _read_pipe(),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{zip_name}"',
            "X-Total-Bytes": str(total_bytes),
            "Access-Control-Expose-Headers": "X-Total-Bytes",
        },
    )


@app.get("/api/events")
async def events(request: Request) -> StreamingResponse:
    q: asyncio.Queue = asyncio.Queue(maxsize=100)
    state.sse_queues.append(q)
    # Opening the dashboard = a viewer is present → shed idle prep immediately, even
    # though a page load is a GET (which doesn't stamp last_activity). The for_prep
    # idle check then keeps prep paused while the tab stays open.
    try:
        _activity_kick()
    except Exception:
        pass

    async def stream() -> AsyncGenerator[str, None]:
        yield f"event: state\ndata: {json.dumps(state_snapshot())}\n\n"
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    yield await asyncio.wait_for(q.get(), timeout=20.0)
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
        finally:
            if q in state.sse_queues:
                state.sse_queues.remove(q)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Routes: Admin Panel ───────────────────────────────────────────────────────

@app.get("/admin", include_in_schema=False)
async def admin_page() -> FileResponse:
    return FileResponse(str(Path(__file__).parent / "static" / "admin.html"))


@app.get("/api/admin/status")
async def admin_status() -> JSONResponse:
    return JSONResponse({"enabled": bool(settings.admin_password)})


@app.post("/api/admin/login")
async def admin_login(req: AdminLoginReq) -> JSONResponse:
    if not settings.admin_password:
        raise HTTPException(503, "Admin panel is not configured (set ADMIN_PASSWORD in .env).")
    if req.password != settings.admin_password:
        raise HTTPException(401, "Incorrect admin password.")
    token = secrets.token_hex(32)
    _admin_sessions[token] = time.time() + 86400   # 24-hour session
    return JSONResponse({"ok": True, "token": token})


@app.post("/api/admin/logout")
async def admin_logout(request: Request) -> JSONResponse:
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    _admin_sessions.pop(token, None)
    return JSONResponse({"ok": True})


@app.get("/api/admin/indexers")
async def admin_list_indexers(request: Request) -> JSONResponse:
    _require_admin(request)
    try:
        async with _jackett_admin() as c:
            r = await c.get(
                f"{settings.indexer_url}/api/v2.0/indexers",
                params={"configured": "true"},
            )
        return JSONResponse({"indexers": r.json() if r.status_code == 200 else []})
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"Could not reach Jackett: {e}")


@app.get("/api/admin/indexers/available")
async def admin_list_available_indexers(request: Request) -> JSONResponse:
    _require_admin(request)
    try:
        async with _jackett_admin() as c:
            r = await c.get(
                f"{settings.indexer_url}/api/v2.0/indexers",
                params={"configured": "false"},
            )
        return JSONResponse({"indexers": r.json() if r.status_code == 200 else []})
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"Could not reach Jackett: {e}")


@app.get("/api/admin/indexers/{indexer_id}/config")
async def admin_get_indexer_config(indexer_id: str, request: Request) -> JSONResponse:
    _require_admin(request)
    try:
        async with _jackett_admin() as c:
            r = await c.get(f"{settings.indexer_url}/api/v2.0/indexers/{indexer_id}/config")
        if r.status_code != 200:
            raise HTTPException(502, f"Jackett returned {r.status_code}")
        return JSONResponse({"config": r.json()})
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"Could not reach Jackett: {e}")


@app.post("/api/admin/indexers/{indexer_id}/config")
async def admin_save_indexer_config(indexer_id: str, request: Request) -> JSONResponse:
    _require_admin(request)
    body = await request.json()
    try:
        async with _jackett_admin() as c:
            r = await c.post(
                f"{settings.indexer_url}/api/v2.0/indexers/{indexer_id}/config",
                json=body,
            )
        if r.status_code >= 300:
            raise HTTPException(502, f"Jackett returned {r.status_code}")
        return JSONResponse({"ok": True})
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"Could not reach Jackett: {e}")


@app.delete("/api/admin/indexers/{indexer_id}")
async def admin_delete_indexer(indexer_id: str, request: Request) -> JSONResponse:
    _require_admin(request)
    try:
        async with _jackett_admin() as c:
            r = await c.delete(f"{settings.indexer_url}/api/v2.0/indexers/{indexer_id}")
        return JSONResponse({"ok": r.status_code < 300})
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"Could not reach Jackett: {e}")


@app.get("/api/admin/settings")
async def admin_get_settings(request: Request) -> JSONResponse:
    _require_admin(request)
    lib = await get_library()
    overrides = lib.get("settings", {}).get("admin_overrides", {})
    return JSONResponse({
        "indexer_url": settings.indexer_url,
        "indexer_api_key": settings.indexer_api_key,
        "indexer_categories": overrides.get("indexer_categories", settings.indexer_categories),
        "tmdb_api_key": overrides.get("tmdb_api_key", settings.tmdb_api_key),
        "tmdb_api_key_source": "admin" if overrides.get("tmdb_api_key") else
                               ("env" if settings.tmdb_api_key else "unset"),
    })


@app.post("/api/admin/settings")
async def admin_update_settings(request: Request, req: AdminSettingsReq) -> JSONResponse:
    _require_admin(request)
    lib = await get_library()
    overrides = lib.setdefault("settings", {}).setdefault("admin_overrides", {})
    if req.indexer_categories is not None:
        overrides["indexer_categories"] = req.indexer_categories.strip()
    if req.tmdb_api_key is not None:
        v = req.tmdb_api_key.strip()
        if v:
            overrides["tmdb_api_key"] = v
        else:
            overrides.pop("tmdb_api_key", None)
    await put_library(lib)
    return JSONResponse({"ok": True})


@app.post("/api/library/{item_id}/admin-lock")
async def admin_lock_item(item_id: str, request: Request, req: AdminItemLockReq) -> JSONResponse:
    _require_admin(request)
    lib = await get_library()
    item = next((it for it in lib["items"] if it["id"] == item_id), None)
    if not item:
        raise HTTPException(404, "Item not found.")
    item["admin_only"] = req.admin_only
    await put_library(lib)
    return JSONResponse({"ok": True})


@app.get("/api/admin/library")
async def admin_list_library(request: Request) -> JSONResponse:
    """Return ALL library items including admin-only ones (admin auth required)."""
    _require_admin(request)
    lib = await get_library()
    items = []
    for it in lib["items"]:
        files = it.get("files", [])
        series_key = _series_key(it)
        skip_data = it.get("skip_data", {})
        files_with_skip = 0
        files_failed = 0
        for f in files:
            entry = skip_data.get(f.get("path", ""))
            if not entry:
                continue
            src = (entry.get("analysis") or {}).get("source", "")
            if src == "failed":
                files_failed += 1
            else:
                files_with_skip += 1
        items.append({
            "id": it["id"],
            "title": it["title"],
            "series": it.get("series", ""),
            "season": it.get("season", 0),
            "episode": it.get("episode", 0),
            "file_count": len(files),
            "size_human": human_size(it.get("size_bytes", 0)),
            "status": it.get("status", "ready"),
            "admin_only": it.get("admin_only", False),
            "series_key": series_key,
            "files_with_skip": files_with_skip,
            "files_failed": files_failed,
            "skip_status": _item_skip_status(it),
            "analysis_job": state.analysis_jobs.get(series_key),
        })
    items.sort(key=lambda x: (x["series"] or "\xff" + x["title"], x["season"], x["episode"]))
    return JSONResponse({"items": items, "jobs": state.analysis_jobs})


# ── Routes: Profile PINs ──────────────────────────────────────────────────────

@app.post("/api/profiles/{profile_id}/set-pin")
async def set_profile_pin(profile_id: str, request: Request, req: ProfilePinReq) -> JSONResponse:
    """Set or clear a profile PIN.

    Admin token: always allowed.
    No token: allowed if the profile has no existing PIN (first-time set).
              If the profile already has a PIN, `current_pin` must be provided and correct.
    """
    is_admin = _check_admin(request)
    pin = req.pin.strip()
    if pin and (len(pin) != 6 or not pin.isdigit()):
        raise HTTPException(400, "PIN must be exactly 6 digits, or empty to clear.")
    lib = await get_library()
    profile = next((p for p in lib["profiles"] if p["id"] == profile_id), None)
    if not profile:
        raise HTTPException(404, "Profile not found.")
    if not is_admin:
        existing = profile.get("pin_hash", "")
        if existing:
            current = req.current_pin.strip()
            if not current or _pin_hash(current) != existing:
                raise HTTPException(403, "Current PIN is incorrect.")
    if pin:
        profile["pin_hash"] = _pin_hash(pin)
    else:
        profile.pop("pin_hash", None)
    await put_library(lib)
    return JSONResponse({"ok": True, "has_pin": bool(pin)})


@app.post("/api/profiles/{profile_id}/set-elevated")
async def set_profile_elevated(profile_id: str, request: Request, req: ProfileElevatedReq) -> JSONResponse:
    """Grant or revoke access to admin-only library items for a profile (admin only)."""
    _require_admin(request)
    lib = await get_library()
    profile = next((p for p in lib["profiles"] if p["id"] == profile_id), None)
    if not profile:
        raise HTTPException(404, "Profile not found.")
    if req.elevated:
        profile["elevated"] = True
    else:
        profile.pop("elevated", None)
    await put_library(lib)
    return JSONResponse({"ok": True, "elevated": req.elevated})


@app.post("/api/profiles/{profile_id}/verify-pin")
async def verify_profile_pin(profile_id: str, req: ProfilePinReq) -> JSONResponse:
    lib = await get_library()
    profile = next((p for p in lib["profiles"] if p["id"] == profile_id), None)
    if not profile:
        raise HTTPException(404, "Profile not found.")
    stored = profile.get("pin_hash", "")
    if not stored:
        return JSONResponse({"ok": True})   # no PIN set — always pass
    if _pin_hash(req.pin.strip()) != stored:
        raise HTTPException(403, "Incorrect PIN.")
    return JSONResponse({"ok": True})


class PinLoginReq(BaseModel):
    pin: str

@app.post("/api/profiles/login-with-pin")
async def login_with_pin(req: PinLoginReq) -> JSONResponse:
    """Find all profiles whose PIN matches. Returns matched profile objects (same shape as /api/profiles).
    403 if no profiles match."""
    pin = req.pin.strip()
    if not pin or len(pin) != 6 or not pin.isdigit():
        raise HTTPException(400, "PIN must be exactly 6 digits.")
    h = _pin_hash(pin)
    lib = await get_library()
    matched = [
        {
            "id": p["id"],
            "name": p["name"],
            "color": p.get("color", "indigo"),
            "has_pin": True,
            "elevated": bool(p.get("elevated", False)),
            "auto_skip_intro":   bool(p.get("auto_skip_intro", False)),
            "auto_skip_credits": bool(p.get("auto_skip_credits", False)),
            "resume_mode":       p.get("resume_mode", "auto"),
            "subtitles_on":      p.get("subtitles_on"),
        }
        for p in lib["profiles"]
        if p.get("pin_hash") == h
    ]
    if not matched:
        raise HTTPException(403, "Incorrect PIN.")
    return JSONResponse({"profiles": matched})


# ── Routes: Smart Skip ────────────────────────────────────────────────────────

@app.post("/api/profiles/{profile_id}/auto-skip")
async def set_profile_auto_skip(profile_id: str, req: ProfileAutoSkipReq) -> JSONResponse:
    """Toggle the per-profile auto-skip preferences for intro and credits."""
    lib = await get_library()
    profile = next((p for p in lib["profiles"] if p["id"] == profile_id), None)
    if not profile:
        raise HTTPException(404, "Profile not found.")
    if req.auto_skip_intro is not None:
        if req.auto_skip_intro:
            profile["auto_skip_intro"] = True
        else:
            profile.pop("auto_skip_intro", None)
    if req.auto_skip_credits is not None:
        if req.auto_skip_credits:
            profile["auto_skip_credits"] = True
        else:
            profile.pop("auto_skip_credits", None)
    await put_library(lib)
    return JSONResponse({
        "ok": True,
        "auto_skip_intro":   bool(profile.get("auto_skip_intro", False)),
        "auto_skip_credits": bool(profile.get("auto_skip_credits", False)),
    })


@app.post("/api/profiles/{profile_id}/resume-mode")
async def set_profile_resume_mode(profile_id: str, req: ProfileResumeModeReq) -> JSONResponse:
    if req.resume_mode not in ("auto", "prompt", "off"):
        raise HTTPException(400, "resume_mode must be 'auto', 'prompt', or 'off'.")
    lib = await get_library()
    profile = next((p for p in lib.get("profiles", []) if p["id"] == profile_id), None)
    if not profile:
        raise HTTPException(404, "Profile not found.")
    profile["resume_mode"] = req.resume_mode
    await put_library(lib)
    return JSONResponse({"ok": True, "resume_mode": req.resume_mode})


@app.post("/api/profiles/{profile_id}/subtitles")
async def set_profile_subtitles(profile_id: str, req: ProfileSubsReq) -> JSONResponse:
    """Per-profile override of the admin subs-on/off default. `subtitles_on` =
    None ⇒ inherit the admin default; True/False ⇒ force on/off for this profile.
    Applied on the next play by `_apply_subtitle_policy`."""
    lib = await get_library()
    profile = next((p for p in lib.get("profiles", []) if p["id"] == profile_id), None)
    if not profile:
        raise HTTPException(404, "Profile not found.")
    if req.subtitles_on is None:
        profile.pop("subtitles_on", None)
    else:
        profile["subtitles_on"] = bool(req.subtitles_on)
    await put_library(lib)
    return JSONResponse({"ok": True, "subtitles_on": profile.get("subtitles_on")})


@app.get("/api/settings/max-volume")
async def get_max_volume() -> JSONResponse:
    return JSONResponse({"max_volume": await _global_max_volume()})


@app.post("/api/settings/max-volume")
async def set_max_volume(req: MaxVolumeReq) -> JSONResponse:
    capped = max(0, min(200, int(req.max_volume)))
    lib = await get_library()
    lib.setdefault("settings", {})["max_volume"] = capped
    await put_library(lib)
    if state.vlc_volume > capped:
        raw = max(0, min(512, round(capped / 100 * 256)))
        await vlc("volume", val=str(raw))
        state.vlc_volume = capped
        await broadcast("state", state_snapshot())
    return JSONResponse({"ok": True, "max_volume": capped})


@app.get("/api/settings/vlc-start-volume")
async def get_vlc_start_volume() -> JSONResponse:
    return JSONResponse({"vlc_start_volume": await _global_vlc_start_volume_pct()})


@app.post("/api/settings/vlc-start-volume")
async def set_vlc_start_volume(req: VlcStartVolumeReq) -> JSONResponse:
    capped = max(0, min(100, int(req.vlc_start_volume)))
    lib = await get_library()
    lib.setdefault("settings", {})["vlc_start_volume"] = capped
    await put_library(lib)
    return JSONResponse({"ok": True, "vlc_start_volume": capped})


@app.get("/api/settings/night-mode")
async def get_night_mode() -> JSONResponse:
    s = (await get_library()).get("settings", {})
    return JSONResponse({
        "night_mode": bool(s.get("vlc_night_mode", False)),
        "preset": _night_mode_preset(s.get("vlc_night_mode_preset")),
        "presets": NIGHT_MODE_PRESET_META,
    })


@app.post("/api/settings/night-mode")
async def set_night_mode(req: NightModeReq) -> JSONResponse:
    """Set VLC night mode (dynamic-range compressor): on/off and/or intensity preset.

    Both fields are optional and merged into the persisted setting, so the
    fullscreen moon button (sends `night_mode` only) and the settings-menu
    intensity picker (sends `preset` only) can each change one without clobbering
    the other. The preset is remembered independently of the on/off toggle.

    When the change actually affects the running filter — turning night mode on
    or off, or changing the preset while it's on — VLC is relaunched in the
    background (`_apply_night_mode`) so it takes effect on whatever's playing
    (there's no runtime HTTP command to add an audio filter); the SSE `state`
    stream reports buffering → playing. A preset change while night mode is off,
    or a no-op, just persists without relaunching VLC.
    """
    lib = await get_library()
    s = lib.setdefault("settings", {})
    new_on = bool(s.get("vlc_night_mode", False)) if req.night_mode is None else bool(req.night_mode)
    new_preset = _night_mode_preset(s.get("vlc_night_mode_preset")) if req.preset is None else _night_mode_preset(req.preset)
    s["vlc_night_mode"] = new_on
    s["vlc_night_mode_preset"] = new_preset
    await put_library(lib)

    # Relaunch only when it changes what VLC is currently outputting: on↔off, or
    # a preset change while night mode is on. (state still holds the *old* values.)
    need_apply = (new_on != state.vlc_night_mode) or (new_on and new_preset != state.vlc_night_mode_preset)
    state.vlc_night_mode = new_on
    state.vlc_night_mode_preset = new_preset

    if need_apply:
        asyncio.create_task(_apply_night_mode(new_on))
    else:
        await broadcast("state", state_snapshot())
    return JSONResponse({"ok": True, "night_mode": new_on, "preset": new_preset, "applied": need_apply})


@app.get("/api/settings/system-volume-default")
async def get_system_volume_default() -> JSONResponse:
    return JSONResponse({"system_volume_default": await _system_volume_default()})


@app.post("/api/settings/system-volume-default")
async def set_system_volume_default(req: SystemVolumeDefaultReq) -> JSONResponse:
    """Configure the OS volume restored when YouTube stops (the "expected max").

    Stored in `library.json → settings.system_volume_default` (0-100, default 70).
    Doesn't change the OS volume right now — only takes effect at the next
    YouTube Stop. See docs/YOUTUBE.md.
    """
    capped = max(0, min(100, int(req.system_volume_default)))
    lib = await get_library()
    lib.setdefault("settings", {})["system_volume_default"] = capped
    await put_library(lib)
    return JSONResponse({"ok": True, "system_volume_default": capped})


@app.get("/api/settings/youtube-start-volume")
async def get_youtube_start_volume() -> JSONResponse:
    return JSONResponse({"youtube_start_volume": await _youtube_start_volume()})


@app.post("/api/settings/youtube-start-volume")
async def set_youtube_start_volume(req: YouTubeStartVolumeReq) -> JSONResponse:
    """Configure the OS volume pre-set before a YouTube play starts.

    Stored in `library.json → settings.youtube_start_volume` (0-100, default 30).
    Doesn't change the OS volume right now — only takes effect at the next
    YouTube play. See docs/YOUTUBE.md.
    """
    capped = max(0, min(100, int(req.youtube_start_volume)))
    lib = await get_library()
    lib.setdefault("settings", {})["youtube_start_volume"] = capped
    await put_library(lib)
    return JSONResponse({"ok": True, "youtube_start_volume": capped})


@app.get("/api/settings/host-volume")
async def get_host_volume() -> JSONResponse:
    """Live host OS mixer volume (0-100). `null` if the platform helper failed."""
    return JSONResponse({"host_volume": await get_system_volume()})


_host_volume_lock: Optional[asyncio.Lock] = None
_host_volume_target: Optional[int] = None
_host_volume_last_written: Optional[int] = None


@app.post("/api/settings/host-volume")
async def set_host_volume(req: HostVolumeReq) -> JSONResponse:
    """Immediately set the host OS mixer volume (0-100).

    Not persisted in `library.json` — the OS already remembers its own mixer
    state, and an external change (TV remote, keyboard volume key) shouldn't
    be clobbered by a stale dashboard value on next launch.

    Coalesced + serialized: concurrent requests share one "latest target" slot.
    Each call takes the lock (queueing if busy), then writes the OS mixer only
    if the latest target differs from the value already written. A drag that
    fires N requests/s on the client therefore produces at most one OS write
    per distinct target value, not N — protects pycaw from being thrashed by
    its own heavy CoInitialize/CoUninitialize cycle on each call.
    """
    global _host_volume_lock, _host_volume_target, _host_volume_last_written
    capped = max(0, min(100, int(req.host_volume)))
    if _host_volume_lock is None:
        _host_volume_lock = asyncio.Lock()
    _host_volume_target = capped
    async with _host_volume_lock:
        target = _host_volume_target
        if target == _host_volume_last_written:
            return JSONResponse({"ok": True, "host_volume": capped, "skipped": True})
        ok = await set_system_volume(target)
        if ok:
            _host_volume_last_written = target
    return JSONResponse({"ok": ok, "host_volume": capped})


@app.post("/api/skip-now")
async def skip_now(req: SkipNowReq) -> JSONResponse:
    """Execute the current Smart Skip offer (called by the client's Skip button)."""
    offer = state.skip_offer
    if not offer or offer.get("type") != req.type:
        raise HTTPException(400, "No matching skip offer is active.")

    if req.type == "intro":
        end_at = float(offer.get("end_at", 0))
        if end_at <= 0:
            raise HTTPException(400, "Invalid intro end position.")
        await vlc("seek", val=str(int(end_at) + 1))
        # Mark this file's intro as handled so the offer doesn't re-show
        if state.skip_offer_file:
            state.skip_offer_file = f"{state.skip_offer_file}#intro-done"
        state.skip_offer = None
        await broadcast("state", state_snapshot())
        return JSONResponse({"ok": True, "action": "seek"})

    if req.type == "credits":
        next_path = offer.get("next_file_path")
        cur_file = offer.get("file_path", "")
        if next_path and Path(next_path).exists():
            lib = await get_library()
            item = next((it for it in lib["items"] if it["id"] == state.library_item_id), None)
            if not item:
                raise HTTPException(404, "Item not found.")
            await vlc_next_file(cur_file, item)
            state.skip_offer = None
            state.skip_offer_file = f"{cur_file}#credits-done"
            await broadcast("state", state_snapshot())
            return JSONResponse({"ok": True, "action": "next_episode"})
        # No next file — just stop playback
        await vlc("pl_stop")
        state.skip_offer = None
        state.skip_offer_file = f"{cur_file}#credits-done"
        await broadcast("state", state_snapshot())
        return JSONResponse({"ok": True, "action": "stop"})

    raise HTTPException(400, "Unknown skip type.")


@app.delete("/api/skip-now")
async def dismiss_skip_offer() -> JSONResponse:
    """Dismiss the current Smart Skip offer without acting on it.

    Marks the offer as handled for the current file so it doesn't re-show on
    the next progress tick. The user can still hit Next/Stop manually.
    """
    if state.skip_offer_file and not state.skip_offer_file.endswith("#dismissed"):
        offer_type = (state.skip_offer or {}).get("type", "intro")
        state.skip_offer_file = f"{state.skip_offer_file}#{offer_type}-done"
    state.skip_offer = None
    await broadcast("state", state_snapshot())
    return JSONResponse({"ok": True})


@app.post("/api/resume-now")
async def resume_now() -> JSONResponse:
    """Seek to the saved position from the active resume offer."""
    offer = state.resume_offer
    if not offer:
        raise HTTPException(400, "No resume offer is active.")
    pos = float(offer.get("position_sec", 0))
    if pos <= 0:
        raise HTTPException(400, "Invalid resume position.")
    await vlc("seek", val=str(int(pos)))
    state.resume_offer = None
    await broadcast("state", state_snapshot())
    return JSONResponse({"ok": True, "sought_to": pos})


@app.delete("/api/resume-now")
async def dismiss_resume_offer() -> JSONResponse:
    """Dismiss the resume offer and start from the beginning."""
    state.resume_offer = None
    await broadcast("state", state_snapshot())
    return JSONResponse({"ok": True})


@app.get("/api/admin/library/{item_id}/skip-data")
async def admin_get_skip_data(item_id: str, request: Request) -> JSONResponse:
    """Return per-file intro/credits times for an item (admin manual editor)."""
    _require_admin(request)
    lib = await get_library()
    item = next((it for it in lib["items"] if it["id"] == item_id), None)
    if not item:
        raise HTTPException(404, "Item not found.")
    skip_data = item.get("skip_data", {})
    files_out = []
    for f in item.get("files", []):
        path = f.get("path", "")
        entry = skip_data.get(path) or {}
        intro = entry.get("intro") or {}
        analysis = entry.get("analysis") or {}
        files_out.append({
            "name": f.get("name", Path(path).name),
            "path": path,
            "intro_start":   intro.get("start"),
            "intro_end":     intro.get("end"),
            "credits_start": entry.get("credits_start"),
            "source":        analysis.get("source", ""),
            # When fingerprinting failed for this file these surface in the
            # editor + Smart Skip log panel so the operator can see why.
            "error_code":    analysis.get("error_code", ""),
            "error":         analysis.get("error", ""),
        })
    return JSONResponse({"files": files_out, "series_key": _series_key(item)})


@app.patch("/api/admin/library/{item_id}/skip-data")
async def admin_set_skip_data(item_id: str, request: Request, req: AdminSkipDataReq) -> JSONResponse:
    """Manual override: set intro/credits times for one file in an item."""
    _require_admin(request)
    lib = await get_library()
    item = next((it for it in lib["items"] if it["id"] == item_id), None)
    if not item:
        raise HTTPException(404, "Item not found.")
    paths = {f.get("path", "") for f in item.get("files", [])}
    if req.file_path not in paths:
        raise HTTPException(400, "file_path is not in this item.")
    skip_data = item.setdefault("skip_data", {})
    entry = skip_data.setdefault(req.file_path, {})
    if req.intro_start is not None and req.intro_end is not None and req.intro_end > req.intro_start:
        entry["intro"] = {"start": round(req.intro_start, 1), "end": round(req.intro_end, 1)}
    elif req.intro_start is None and req.intro_end is None:
        entry.pop("intro", None)
    if req.credits_start is not None:
        entry["credits_start"] = round(req.credits_start, 1) if req.credits_start > 0 else None
        if entry["credits_start"] is None:
            entry.pop("credits_start", None)
    entry["analysis"] = {"version": analyzer.ANALYZER_VERSION, "source": "manual"}
    await put_library(lib)
    return JSONResponse({"ok": True})


@app.post("/api/admin/library/{item_id}/analyze")
async def admin_analyze_series(item_id: str, request: Request) -> JSONResponse:
    """Force a (re-)analysis of the series this item belongs to."""
    _require_admin(request)
    if not analyzer.is_available():
        raise HTTPException(503, "ffmpeg/fpcalc not available on this host.")
    lib = await get_library()
    item = next((it for it in lib["items"] if it["id"] == item_id), None)
    if not item:
        raise HTTPException(404, "Item not found.")
    key = _series_key(item)
    asyncio.create_task(_run_series_analysis(key))
    return JSONResponse({"ok": True, "series_key": key})


@app.get("/api/admin/analyzer-status")
async def admin_analyzer_status(request: Request) -> JSONResponse:
    _require_admin(request)
    return JSONResponse({
        "available": analyzer.is_available(),
        "ffmpeg":    analyzer.ffmpeg_bin(),
        "fpcalc":    analyzer.fpcalc_bin(),
    })


@app.get("/api/admin/analyzer-log")
async def admin_analyzer_log(request: Request, limit: int = 100) -> JSONResponse:
    """Return the in-memory Smart Skip event log (most recent first).

    Used by the admin Smart Skip tab to show why fingerprinting failed for
    individual files. The buffer is bounded at 200 and resets on restart —
    persistent failures will repopulate the log on the next analysis run.
    """
    _require_admin(request)
    limit = max(1, min(int(limit), state.analyzer_log.maxlen or 200))
    return JSONResponse({
        "entries": list(state.analyzer_log)[:limit],
        "available": analyzer.is_available(),
        "ffmpeg":    analyzer.ffmpeg_bin(),
        "fpcalc":    analyzer.fpcalc_bin(),
    })


@app.get("/api/admin/offline-encoder")
async def admin_offline_encoder(request: Request) -> JSONResponse:
    """Report which encoder offline transcodes will use (GPU vs CPU)."""
    _require_admin(request)
    nvenc = await _has_nvenc()
    return JSONResponse({
        "nvenc_available": nvenc,
        "encoder":         "h264_nvenc" if nvenc else "libx264",
        "ffmpeg":          analyzer.ffmpeg_bin(),
    })


@app.post("/api/admin/shutdown")
async def admin_shutdown(request: Request) -> JSONResponse:
    """Stop the StreamLink server.

    Sends SIGTERM to every uvicorn process running our `main:app` (the HTTP
    process on port 80 and, when SSL certs exist, the HTTPS process on port
    443). The launcher (run.py) is blocked on the HTTP uvicorn via
    subprocess.run, so once that exits its finally block also cleans up the
    HTTPS sibling — see run.py:861.
    """
    _require_admin(request)

    async def _kill_uvicorns() -> None:
        # Brief delay so this HTTP response can flush back to the client
        # before we tear our own process down.
        await asyncio.sleep(0.5)
        me = os.getpid()
        targets: list[psutil.Process] = []
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                cmdline = proc.info.get("cmdline") or []
                joined = " ".join(cmdline)
                if "uvicorn" in joined and "main:app" in joined:
                    targets.append(proc)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        # Signal siblings first, then self last, so the admin process stays
        # alive long enough to send the others.
        siblings = [p for p in targets if p.pid != me]
        selves   = [p for p in targets if p.pid == me]
        for p in siblings + selves:
            try:
                p.send_signal(signal.SIGTERM)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        # Hard-exit fallback in case uvicorn ignores SIGTERM for some reason.
        await asyncio.sleep(3.0)
        os._exit(0)

    asyncio.create_task(_kill_uvicorns())
    return JSONResponse({"ok": True, "message": "Server shutting down…"})


@app.post("/api/admin/reboot")
async def admin_reboot(request: Request) -> JSONResponse:
    """Reboot the host machine immediately.

    This restarts the whole computer, not just the web server. For the server to
    come back automatically the host needs auto-login + the StreamLink system
    service installed (`run.py --install`); see README. Used as a hard reset for
    a wedged Jackett. The reboot fires ~0.5 s after this response flushes.
    """
    _require_admin(request)
    asyncio.create_task(_reboot_machine())
    return JSONResponse({"ok": True, "message": "Rebooting host machine…"})


# ── Log download ──────────────────────────────────────────────────────────────
# Operators frequently need the server's rotating log files (hls.log,
# streamlink_app.log, streamlink.err, …) to diagnose a remote box where SSH
# isn't set up. These admin endpoints expose the contents of LOG_DIR read-only:
# a directory listing + per-file download + a bundled zip of everything. Path
# traversal is blocked by resolving the requested name against LOG_DIR and
# refusing anything that escapes it.

def _safe_log_path(name: str) -> Path:
    """Resolve `name` against LOG_DIR, refusing traversal/absolute paths.

    Raises HTTPException on any attempt to escape LOG_DIR or read a non-file.
    """
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        raise HTTPException(400, "Invalid log filename.")
    candidate = (LOG_DIR / name).resolve()
    try:
        candidate.relative_to(LOG_DIR.resolve())
    except ValueError:
        raise HTTPException(400, "Log file is outside the logs directory.")
    if not candidate.exists() or not candidate.is_file():
        raise HTTPException(404, "Log file not found.")
    return candidate


@app.get("/api/admin/logs")
async def admin_list_logs(request: Request) -> JSONResponse:
    """List every file in LOG_DIR with size + mtime (newest first)."""
    _require_admin(request)
    entries = []
    if LOG_DIR.exists():
        for p in LOG_DIR.iterdir():
            if not p.is_file():
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            entries.append({
                "name":   p.name,
                "bytes":  st.st_size,
                "mtime":  int(st.st_mtime),
            })
    entries.sort(key=lambda e: e["mtime"], reverse=True)
    return JSONResponse({"log_dir": str(LOG_DIR), "files": entries})


@app.get("/api/admin/logs/_bundle")
async def admin_download_logs_bundle(request: Request) -> StreamingResponse:
    """Stream a ZIP of every file in LOG_DIR.

    Path is `_bundle` (underscore prefix) so it can't collide with a real log
    filename — `_safe_log_path` rejects names containing slashes anyway, but
    matching against the literal `_bundle` route ensures the per-file handler
    never sees this name.
    """
    _require_admin(request)
    files: list[Path] = []
    if LOG_DIR.exists():
        for p in LOG_DIR.iterdir():
            if p.is_file():
                files.append(p)
    if not files:
        raise HTTPException(404, "No log files to download.")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    zip_name = f"streamlink-logs-{stamp}.zip"

    r_fd, w_fd = os.pipe()

    def _write_zip() -> None:
        try:
            with os.fdopen(w_fd, "wb") as wf, \
                    zipfile.ZipFile(wf, "w", zipfile.ZIP_DEFLATED) as zf:
                for p in files:
                    try:
                        zf.write(str(p), p.name)
                    except OSError:
                        continue
        except Exception:
            pass

    threading.Thread(target=_write_zip, daemon=True).start()

    async def _read_pipe() -> AsyncGenerator[bytes, None]:
        rf = os.fdopen(r_fd, "rb")
        try:
            while True:
                chunk = await asyncio.to_thread(rf.read, 65536)
                if not chunk:
                    break
                yield chunk
        finally:
            rf.close()

    return StreamingResponse(
        _read_pipe(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{zip_name}"'},
    )


@app.get("/api/admin/logs/{name}")
async def admin_download_log(request: Request, name: str) -> FileResponse:
    """Download one file from LOG_DIR by name. Forces attachment disposition."""
    _require_admin(request)
    path = _safe_log_path(name)
    return FileResponse(
        str(path),
        media_type="text/plain; charset=utf-8",
        filename=path.name,
        headers={"Content-Disposition": f'attachment; filename="{path.name}"'},
    )


@app.delete("/api/admin/logs")
async def admin_clear_logs(request: Request) -> JSONResponse:
    """Clear every file in LOG_DIR.

    Active rotating handlers (`streamlink_app.log`, `hls.log`) are **truncated
    in-place via the handler's stream** rather than deleted — on Windows you
    can't unlink a file that the running process has open for writing, and even
    on POSIX a delete would leave the FD valid but disconnected from any
    on-disk file so subsequent writes would vanish until the next restart.
    Truncating via `handler.stream.truncate(0)` keeps logging fully functional;
    new lines append from offset 0.

    Non-active files (rotated `.1`/`.2`/`.3` siblings, plus the
    launchd/systemd-written `streamlink.err`) are unlinked. If unlink fails
    (e.g. service still has an exclusive write handle on Windows), we fall back
    to a write-mode truncate and continue.
    """
    _require_admin(request)

    cleared: list[str] = []
    errors:  list[dict] = []
    active_basenames: set[str] = set()

    # 1) Truncate the live rotating-file handlers via their own streams so we
    #    don't race the logging thread.
    for logger_name in ("streamlink", "streamlink.hls"):
        lg = logging.getLogger(logger_name)
        for h in lg.handlers:
            if not isinstance(h, RotatingFileHandler):
                continue
            try:
                base = Path(h.baseFilename).name
                active_basenames.add(base)
                h.acquire()
                try:
                    if h.stream:
                        h.stream.flush()
                        h.stream.seek(0)
                        h.stream.truncate(0)
                finally:
                    h.release()
                cleared.append(base)
            except Exception as e:
                errors.append({"file": Path(h.baseFilename).name, "error": str(e)})

    # 2) Everything else in LOG_DIR — delete; on failure, truncate-and-keep.
    if LOG_DIR.exists():
        for p in LOG_DIR.iterdir():
            if not p.is_file() or p.name in active_basenames:
                continue
            try:
                p.unlink()
                cleared.append(p.name)
                continue
            except OSError:
                pass
            try:
                with open(p, "wb"):
                    pass
                cleared.append(p.name)
            except OSError as e:
                errors.append({"file": p.name, "error": str(e)})

    log.info(
        "Admin cleared logs: %d file(s) cleared, %d error(s).",
        len(cleared), len(errors),
    )
    return JSONResponse({"ok": True, "cleared": cleared, "errors": errors})


@app.get("/api/admin/scheduled-reboot")
async def admin_get_scheduled_reboot(request: Request) -> JSONResponse:
    """Return the scheduled-reboot config + the host's current time in the
    configured timezone (so the UI can show what 'now' looks like there)."""
    _require_admin(request)
    cfg = _scheduled_reboot_cfg(await get_library())
    now = _now_in_tz(cfg["timezone"])
    cfg["now"] = now.strftime("%Y-%m-%d %H:%M %Z").strip()
    return JSONResponse(cfg)


@app.post("/api/admin/scheduled-reboot")
async def admin_set_scheduled_reboot(
    request: Request, body: ScheduledRebootReq,
) -> JSONResponse:
    """Save the scheduled-reboot config. Validates the HH:MM time and clamps the
    idle window. Changing the config clears any prior `last_fired` guard so a
    newly-set time can arm today."""
    _require_admin(request)

    parts = body.time.split(":")
    try:
        h, m = int(parts[0]), int(parts[1])
        assert 0 <= h <= 23 and 0 <= m <= 59
    except (ValueError, IndexError, AssertionError):
        raise HTTPException(400, "time must be HH:MM (24-hour), e.g. 00:00")

    idle = max(1, min(720, int(body.idle_minutes)))

    lib = await get_library()
    sr = lib.setdefault("settings", {}).setdefault("scheduled_reboot", {})
    sr["enabled"]      = bool(body.enabled)
    sr["time"]         = f"{h:02d}:{m:02d}"
    sr["timezone"]     = body.timezone.strip()
    sr["idle_minutes"] = idle
    sr["last_fired"]   = ""   # reset guard so the new schedule can arm today
    await put_library(lib)

    cfg = _scheduled_reboot_cfg(lib)
    cfg["now"] = _now_in_tz(cfg["timezone"]).strftime("%Y-%m-%d %H:%M %Z").strip()
    return JSONResponse({"ok": True, **cfg})


@app.get("/api/admin/overnight-prep")
async def admin_get_overnight_prep(request: Request) -> JSONResponse:
    """Return the overnight auto-prep config + the host's current time in the
    configured timezone, and whether the window is open right now."""
    _require_admin(request)
    cfg = _overnight_prep_cfg(await get_library())
    now = _now_in_tz(cfg["timezone"])
    cfg["now"] = now.strftime("%Y-%m-%d %H:%M %Z").strip()
    s_min, e_min = _hhmm_to_min(cfg["start"]), _hhmm_to_min(cfg["end"])
    cfg["in_window"] = bool(
        cfg["enabled"] and s_min is not None and e_min is not None
        and _in_overnight_window(now, s_min, e_min)
    )
    cfg["paused"] = state.prep_paused
    return JSONResponse(cfg)


@app.post("/api/admin/overnight-prep")
async def admin_set_overnight_prep(
    request: Request, body: OvernightPrepReq,
) -> JSONResponse:
    """Save the overnight auto-prep config. Validates both HH:MM times and the
    on_end mode. Re-evaluates the window immediately so a change takes effect on
    the next scheduler tick without waiting for a fresh entry/exit transition."""
    _require_admin(request)

    s_min = _hhmm_to_min(body.start)
    e_min = _hhmm_to_min(body.end)
    if s_min is None or e_min is None:
        raise HTTPException(400, "start and end must be HH:MM (24-hour), e.g. 02:00")
    if s_min == e_min:
        raise HTTPException(400, "start and end can't be the same time (empty window).")
    on_end = body.on_end if body.on_end in ("pause", "continue") else "pause"

    lib = await get_library()
    op = lib.setdefault("settings", {}).setdefault("overnight_prep", {})
    op["enabled"]  = bool(body.enabled)
    op["start"]    = f"{s_min // 60:02d}:{s_min % 60:02d}"
    op["end"]      = f"{e_min // 60:02d}:{e_min % 60:02d}"
    op["timezone"] = body.timezone.strip()
    op["on_end"]   = on_end
    await put_library(lib)

    # Reset the auto-prep edge flag so the loop re-derives "want" against the new
    # config on its next tick (and applies the new on_end on exit).
    state.auto_prep_engaged = False

    cfg = _overnight_prep_cfg(lib)
    cfg["now"] = _now_in_tz(cfg["timezone"]).strftime("%Y-%m-%d %H:%M %Z").strip()
    return JSONResponse({"ok": True, **cfg})


@app.get("/api/admin/system-resources")
async def admin_system_resources(request: Request) -> JSONResponse:
    """Live host health for the admin System tab: CPU / RAM / GPU / network with an
    ok|degraded|overloaded status each + an overall. Sampled by system_monitor_loop;
    also rides in every `state` SSE event as `sys_status`. Empty `{}` until the first
    sample (~5 s after start). Includes whether bulk background work is running now."""
    _require_admin(request)
    s = dict(state.sys_status or {})
    s["prep_active"] = _bulk_processing_now()
    s["prep_paused"] = state.prep_paused
    return JSONResponse(s)


@app.get("/api/admin/idle-prep")
async def admin_get_idle_prep(request: Request) -> JSONResponse:
    """Return the idle-triggered auto-prep config + whether the box is idle enough
    to be prepping right now."""
    _require_admin(request)
    cfg = _idle_prep_cfg(await get_library())
    in_use = await _machine_in_use(cfg["idle_minutes"] * 60)
    cfg["idle_now"] = not in_use
    cfg["paused"]   = state.prep_paused
    cfg["active"]   = bool(cfg["enabled"] and state.auto_prep_engaged and not in_use)
    return JSONResponse(cfg)


@app.post("/api/admin/idle-prep")
async def admin_set_idle_prep(request: Request, body: IdlePrepReq) -> JSONResponse:
    """Save the idle-triggered auto-prep config. Clamps idle_minutes to 1–720 and
    re-derives the trigger on the next scheduler tick."""
    _require_admin(request)
    idle_minutes = max(1, min(720, int(body.idle_minutes)))

    lib = await get_library()
    ip = lib.setdefault("settings", {}).setdefault("idle_prep", {})
    ip["enabled"]      = bool(body.enabled)
    ip["idle_minutes"] = idle_minutes
    await put_library(lib)

    # Re-derive "want" fresh against the new config on the next loop tick.
    state.auto_prep_engaged = False

    cfg = _idle_prep_cfg(lib)
    in_use = await _machine_in_use(cfg["idle_minutes"] * 60)
    cfg["idle_now"] = not in_use
    return JSONResponse({"ok": True, **cfg})


@app.get("/api/admin/play-prep")
async def admin_get_play_prep(request: Request) -> JSONResponse:
    """Return the auto-prep-on-play config (on-device prep triggered by VLC play)."""
    _require_admin(request)
    return JSONResponse(_play_prep_cfg(await get_library()))


@app.post("/api/admin/play-prep")
async def admin_set_play_prep(request: Request, body: PlayPrepReq) -> JSONResponse:
    """Enable/disable auto on-device prep on VLC play."""
    _require_admin(request)
    lib = await get_library()
    pp = lib.setdefault("settings", {}).setdefault("play_prep", {})
    pp["enabled"] = bool(body.enabled)
    await put_library(lib)
    return JSONResponse({"ok": True, **_play_prep_cfg(lib)})


@app.get("/api/admin/force-prep")
async def admin_get_force_prep(request: Request) -> JSONResponse:
    """Live status of the admin force-prep batch (counts + aggregate progress)."""
    _require_admin(request)
    return JSONResponse(_force_prep_status())


@app.post("/api/admin/force-prep")
async def admin_start_force_prep(request: Request, body: ForcePrepReq) -> JSONResponse:
    """Force-prep the whole library (or one item) for on-device streaming. These
    jobs ignore the bulk pause gate and the activity-kill — neither a viewer's
    Pause control nor live host activity can stop them; only the admin Stop
    control (POST /force-prep/stop) can. 409 on macOS hosts (no HLS)."""
    _require_admin(request)
    if not HLS_AVAILABLE:
        raise HTTPException(409, HLS_UNAVAILABLE_MSG)
    item_id = (body.item_id or "").strip()
    queued = await _enqueue_admin_prep(item_id)
    return JSONResponse({"ok": True, "queued": queued, **_force_prep_status()})


@app.post("/api/admin/force-prep/stop")
async def admin_stop_force_prep(request: Request, body: ForcePrepStopReq) -> JSONResponse:
    """Stop the admin force-prep batch. `hard=False` lets the in-flight file finish
    then cancels the rest; `hard=True` terminates the running encode immediately."""
    _require_admin(request)
    res = _stop_admin_prep(bool(body.hard))
    return JSONResponse({"ok": True, **res, **_force_prep_status()})


@app.get("/api/admin/cache-autopurge")
async def admin_get_cache_autopurge(request: Request) -> JSONResponse:
    """Return the orphan-cache auto-purge config + the result of the last run."""
    _require_admin(request)
    cfg = _cache_autopurge_cfg(await get_library())
    cfg["last"] = state.cache_autopurge_last or None
    return JSONResponse(cfg)


@app.post("/api/admin/cache-autopurge")
async def admin_set_cache_autopurge(request: Request, body: CacheAutopurgeReq) -> JSONResponse:
    """Save the orphan-cache auto-purge config. Clamps max_gb to 1–10000 GB. The
    next `cache_autopurge_loop` tick (≤5 min) evaluates the cap against the new
    value; nothing is purged inline here."""
    _require_admin(request)
    max_gb = max(1.0, min(10000.0, float(body.max_gb)))

    lib = await get_library()
    cp = lib.setdefault("settings", {}).setdefault("cache_autopurge", {})
    cp["enabled"] = bool(body.enabled)
    cp["max_gb"]  = max_gb
    await put_library(lib)

    cfg = _cache_autopurge_cfg(lib)
    cfg["last"] = state.cache_autopurge_last or None
    return JSONResponse({"ok": True, **cfg})


def _subtitle_language_options() -> list[dict]:
    """The 3-letter language options for the admin subtitle-language picker."""
    return [{"code": c, "name": n}
            for c, n in sorted(_LANG_NAMES.items(), key=lambda kv: kv[1])
            if n and len(c) == 3]


@app.get("/api/admin/stt")
async def admin_get_stt(request: Request) -> JSONResponse:
    """Return the auto-subtitle (STT) config + whether the host can actually run
    it. The preferred language lives in the unified subtitle policy
    (`/api/admin/subtitles`), so it's reported but not editable here."""
    _require_admin(request)
    cfg = _stt_cfg(await get_library())
    cfg["available"] = _stt_available()
    return JSONResponse(cfg)


@app.post("/api/admin/stt")
async def admin_set_stt(request: Request, body: SttConfigReq) -> JSONResponse:
    """Save the STT config (enabled + translate). The preferred language is owned
    by the unified subtitle policy (`POST /api/admin/subtitles`), not here."""
    _require_admin(request)
    lib = await get_library()
    s = lib.setdefault("settings", {}).setdefault("stt", {})
    s["enabled"]   = bool(body.enabled)
    s["translate"] = bool(body.translate)
    await put_library(lib)
    cfg = _stt_cfg(lib)
    cfg["available"] = _stt_available()
    return JSONResponse({"ok": True, **cfg})


@app.get("/api/admin/subtitles")
async def admin_get_subtitles(request: Request) -> JSONResponse:
    """Return the unified subtitle policy (preferred language, subs on/off by
    default, auto-search-online) + the language option list for the picker."""
    _require_admin(request)
    cfg = _subs_cfg(await get_library())
    cfg["languages"] = _subtitle_language_options()
    return JSONResponse(cfg)


@app.post("/api/admin/subtitles")
async def admin_set_subtitles(request: Request, body: SubsConfigReq) -> JSONResponse:
    """Save the unified subtitle policy. `default_language` is canonicalized to a
    3-letter code; "" means "Any" (no preferred language). This same language
    also drives AI generation (`_stt_cfg` reads it) and the search default."""
    _require_admin(request)
    lib = await get_library()
    s = lib.setdefault("settings", {}).setdefault("subtitles", {})
    s["default_language"] = _canon_lang(body.default_language) if body.default_language.strip() else ""
    s["on_by_default"]    = bool(body.on_by_default)
    s["auto_search"]      = bool(body.auto_search)
    s["upgrade_late_subs"] = bool(body.upgrade_late_subs)
    s["single_option"]     = bool(body.single_option)
    await put_library(lib)
    cfg = _subs_cfg(lib)
    state.subtitle_default_language = cfg["default_language"]
    state.subtitle_upgrade_late = cfg["upgrade_late_subs"]
    state.subtitle_single_option = cfg["single_option"]
    await broadcast("state", state_snapshot())
    return JSONResponse({"ok": True, **cfg})


@app.get("/api/admin/qbit-limits")
async def admin_get_qbit_limits(request: Request) -> JSONResponse:
    """Return qBittorrent's global seeding-ratio limit + max up/down speeds. Read
    live from qBit (the source of truth — it persists them in its own config), so
    the payload is `{ok: false}` when qBit isn't reachable."""
    _require_admin(request)
    return JSONResponse(await qbit_global_limits())


@app.post("/api/admin/qbit-limits")
async def admin_set_qbit_limits(request: Request, body: QbitLimitsReq) -> JSONResponse:
    """Write the global ratio limit + speed caps to qBittorrent. These are global
    qBit settings (apply to every torrent — stream-now and library) and persist in
    qBit's own config. The ratio action is fixed to 'pause' (max_ratio_act=0) so a
    torrent stops seeding — keeping its files on disk — the moment the ratio is hit
    (e.g. a 1.0 ratio stops a 10 GB show after it has uploaded 10 GB). Speeds are
    bytes/sec, 0 = unlimited."""
    _require_admin(request)
    ratio = max(0.0, min(9998.0, float(body.ratio)))
    dl = max(0, int(body.dl_limit_bytes))
    up = max(0, int(body.up_limit_bytes))
    ok = await qbit_set_preferences({
        "max_ratio_enabled": bool(body.ratio_enabled),
        "max_ratio":         ratio,
        "max_ratio_act":     0,   # 0 = pause/stop the torrent (keep files); 1 = remove
    })
    ok = (await qbit_set_speed_limit("download", dl)) and ok
    ok = (await qbit_set_speed_limit("upload", up)) and ok
    if not ok:
        raise HTTPException(502, "Could not reach qBittorrent to apply the limits.")
    return JSONResponse({"ok": True, **(await qbit_global_limits())})


@app.get("/api/admin/components")
async def admin_components(request: Request) -> JSONResponse:
    """Status of the installable portable dependencies (ffmpeg, fpcalc, whisper
    binary, whisper model) + any in-flight install job. Polled by the admin
    Components card while an install runs. `nvenc` flags an NVIDIA GPU so the UI
    can recommend a CUDA whisper build."""
    _require_admin(request)
    payload = _component_status_payload()
    try:
        payload["nvenc"] = await _has_nvenc()
    except Exception:
        payload["nvenc"] = False
    return JSONResponse(payload)


class ComponentInstallReq(BaseModel):
    component: str
    model: str = "base"      # whisper_model only: base | small | medium
    build: str = "cpu"       # whisper only: cpu | cuda12 | cuda11


@app.post("/api/admin/components/install")
async def admin_components_install(request: Request, req: ComponentInstallReq) -> JSONResponse:
    """Download + install one portable component in the background. Poll
    /api/admin/components for progress. ffmpeg/whisper binaries are Windows-only
    here (elsewhere use the OS package manager); fpcalc + the model work anywhere.
    For whisper, `build` picks the CPU or a CUDA/cuBLAS variant."""
    _require_admin(request)
    if req.component not in _COMPONENT_KEYS:
        raise HTTPException(400, "Unknown component.")
    if req.component in ("ffmpeg", "whisper") and platform.system() != "Windows":
        raise HTTPException(400, "This component can only be auto-installed on Windows. "
                                 "Install it via your OS package manager instead.")
    existing = _component_jobs.get(req.component)
    if existing and existing.get("status") in ("pending", "downloading"):
        return JSONResponse({"ok": True, "status": existing["status"], "already_running": True})
    _component_jobs[req.component] = {
        "status": "pending", "progress": 0.0, "error": None,
        "component": req.component, "started_at": time.time(),
    }
    asyncio.create_task(_run_component_install(req.component, req.model, req.build))
    return JSONResponse({"ok": True, "status": "started"})


# ── Routes: Auto-Updater ──────────────────────────────────────────────────────
# Periodic git-pull + setup-rerun + service-restart, gated to the main / beta /
# alpha branches. State lives in library.json → settings.autoupdate; in-flight
# operation status is mirrored on AppState for live UI. See `updater_loop` and
# the updater.py module for the actual git plumbing.

class UpdaterConfigReq(BaseModel):
    enabled:        Optional[bool] = None
    branch:         Optional[str]  = None     # main | beta | alpha (any branch when dev_mode)
    interval_hours: Optional[int]  = None     # 1-168
    auto_apply:     Optional[bool] = None
    dev_mode:       Optional[bool] = None     # "show all branches" — relax the branch gate


class UpdaterApplyReq(BaseModel):
    branch: Optional[str] = None     # default: settings.autoupdate.branch
    dev_mode: Optional[bool] = None  # picker's "show all branches" state; gates a non-canonical branch
    # When True (default), the apply ends with daemon.uninstall() +
    # daemon.install() + a full host reboot for a clean-state restart on the
    # new code. False = code-only refresh: git apply + setup.py, no service
    # reinstall, no reboot. Used by the dev "Apply files only" toggle.
    reboot: bool = True


@app.get("/api/admin/updater")
async def admin_get_updater(request: Request) -> JSONResponse:
    """Return the persisted updater config + the live git state + the current
    phase of any in-flight check/apply.

    Fields:
        cfg            — settings.autoupdate (enabled, branch, interval_hours, auto_apply, …)
        allowed_branches — the three branches the picker is allowed to select
        is_git_repo    — False ⇒ the dashboard is running from a non-git copy
                         (admin UI hides the Apply controls in that case)
        current_branch — git's view of the active branch
        current_commit — short HEAD sha
        phase          — idle | checking | applying | setup | restarting | error
        message        — human-readable detail for the current phase
        busy           — True while an admin endpoint is mid-operation
        last_output    — last 8 KiB of setup.py stdout/stderr (diagnostics)
    """
    _require_admin(request)
    lib = await get_library()
    cfg = _autoupdate_cfg(lib)
    is_repo = await updater.is_git_repo()
    return JSONResponse({
        "cfg":             cfg,
        "allowed_branches": list(updater.ALLOWED_BRANCHES),
        "is_git_repo":     is_repo,
        "current_branch":  (await updater.current_branch()) if is_repo else "",
        "current_commit":  (await updater.current_commit()) if is_repo else "",
        "phase":           state.updater_phase,
        "message":         state.updater_message,
        "busy":            state.updater_busy,
        "last_output":     state.updater_last_output,
        "service_installed": await updater.service_is_installed(),
        "ui_version":      UI_VERSION,
    })


@app.get("/api/admin/updater/branches")
async def admin_updater_branches(request: Request) -> JSONResponse:
    """List every branch on origin (a fresh `git ls-remote`), for the dev-mode
    "show all branches" picker.

    Kept off the polled `/api/admin/updater` payload on purpose — it costs a
    network round-trip, so the UI fetches it only when the admin opens the
    expanded picker rather than every 4 s. `ALLOWED_BRANCHES` (main/beta/alpha)
    sort to the top; the rest follow alphabetically. Returns `[]` (not an error)
    when this isn't a git checkout so the UI can fall back to the defaults.
    """
    _require_admin(request)
    branches = await updater.list_remote_branches()
    return JSONResponse({
        "ok": True,
        "branches": branches,
        "allowed_branches": list(updater.ALLOWED_BRANCHES),
    })


@app.post("/api/admin/updater/config")
async def admin_set_updater_config(request: Request, body: UpdaterConfigReq) -> JSONResponse:
    """Persist a partial update to settings.autoupdate. Fields that aren't
    provided are left untouched. Branch is validated against ALLOWED_BRANCHES
    (or, when dev_mode is on, any structurally-valid branch name);
    interval_hours is clamped to [1, 168]."""
    _require_admin(request)
    lib = await get_library()
    au = lib.setdefault("settings", {}).setdefault("autoupdate", {})
    # Apply dev_mode first so the branch validation below uses the new value.
    if body.dev_mode is not None:
        au["dev_mode"] = bool(body.dev_mode)
    allow_any = bool(au.get("dev_mode", False))
    if body.branch is not None:
        ok, err = updater.branch_allowed(body.branch, allow_any=allow_any)
        if not ok:
            raise HTTPException(400, err)
        au["branch"] = body.branch
    if body.interval_hours is not None:
        try:
            ih = int(body.interval_hours)
        except (TypeError, ValueError):
            raise HTTPException(400, "interval_hours must be an integer.")
        au["interval_hours"] = max(1, min(168, ih))
    if body.enabled is not None:
        au["enabled"] = bool(body.enabled)
    if body.auto_apply is not None:
        au["auto_apply"] = bool(body.auto_apply)
    await put_library(lib)
    return JSONResponse({"ok": True, "cfg": _autoupdate_cfg(lib)})


@app.post("/api/admin/updater/check")
async def admin_check_update(request: Request) -> JSONResponse:
    """Force an immediate git fetch + compare. Branch defaults to settings.autoupdate.branch."""
    _require_admin(request)
    if state.updater_busy:
        raise HTTPException(409, "An update operation is already running.")
    lib = await get_library()
    cfg = _autoupdate_cfg(lib)
    branch = cfg["branch"]
    async with _updater_lock:
        await _set_updater_phase("checking", f"Checking origin/{branch}…", busy=True)
        try:
            res = await _run_check(branch, allow_any=cfg["dev_mode"])
        finally:
            if state.updater_busy:
                if res.get("ok"):
                    msg = (f"Update available ({res.get('behind_by', 0)} commits)"
                           if res.get("has_update")
                           else f"Up to date with origin/{branch}.")
                    await _set_updater_phase("idle", msg, busy=False)
                else:
                    await _set_updater_phase("error", res.get("error") or "check failed",
                                            busy=False)
    return JSONResponse(res)


@app.post("/api/admin/updater/apply")
async def admin_apply_update(request: Request, body: UpdaterApplyReq) -> JSONResponse:
    """Run the full update sequence right now (git apply → setup.py → service
    reinstall → host reboot).

    `body.branch` accepts main / beta / alpha (or any branch when dev_mode is
    on) — passing a branch that differs from the current working tree triggers
    a downgrade or sidegrade (git apply handles the switch). With
    `body.reboot=False` the service reinstall and machine reboot are skipped
    (dev convenience — refresh the code without tearing the box down).

    Does NOT check `_machine_in_use` — an admin clicking Apply Now is taken at
    their word. The auto-apply loop (in `updater_loop`) is the path that defers
    to the idle gate.
    """
    _require_admin(request)
    if state.updater_busy:
        raise HTTPException(409, "An update operation is already running.")

    lib = await get_library()
    cfg = _autoupdate_cfg(lib)
    allow_any = bool(body.dev_mode) if body.dev_mode is not None else cfg["dev_mode"]
    branch = (body.branch or cfg["branch"]).strip()
    ok, err = updater.branch_allowed(branch, allow_any=allow_any)
    if not ok:
        raise HTTPException(400, err)

    res = await _run_apply(branch, reboot=bool(body.reboot), allow_any=allow_any)
    if not res.get("ok"):
        raise HTTPException(500, res.get("message") or "Update failed.")
    return JSONResponse(res)


@app.post("/api/admin/updater/switch-branch")
async def admin_switch_branch(request: Request, body: UpdaterConfigReq) -> JSONResponse:
    """Switch the working tree to a new branch (without auto-applying yet).

    Useful when an admin wants to try the alpha branch immediately — sets
    the saved branch AND does a hard checkout of origin/<branch>. Does NOT
    run setup.py or restart; the admin can follow up with /apply if they
    want the full sequence.

    `body.dev_mode` rides along so the picker's "show all branches" state takes
    effect even before a separate Save: when provided it is persisted and used
    to gate the (possibly non-canonical) target branch.
    """
    _require_admin(request)
    if state.updater_busy:
        raise HTTPException(409, "An update operation is already running.")
    if body.branch is None:
        raise HTTPException(400, "branch is required.")

    lib = await get_library()
    allow_any = bool(body.dev_mode) if body.dev_mode is not None \
        else _autoupdate_cfg(lib)["dev_mode"]
    ok, err = updater.branch_allowed(body.branch, allow_any=allow_any)
    if not ok:
        raise HTTPException(400, err)

    async with _updater_lock:
        await _set_updater_phase("applying", f"Switching to {body.branch}…", busy=True)
        res = await updater.switch_branch(body.branch, allow_any=allow_any)
        if not res.get("ok"):
            await _set_updater_phase("error", res.get("error", "switch failed"), busy=False)
            raise HTTPException(500, res.get("error", "switch failed"))

        # Persist the new branch (+ the dev-mode preference) as the default for
        # future auto-checks.
        lib = await get_library()
        au = lib.setdefault("settings", {}).setdefault("autoupdate", {})
        au["branch"] = body.branch
        if body.dev_mode is not None:
            au["dev_mode"] = bool(body.dev_mode)
        await put_library(lib)

        await _set_updater_phase("idle",
                                f"Switched to {body.branch} ({res['commit']}).",
                                busy=False)
    return JSONResponse({"ok": True, "branch": body.branch, "commit": res["commit"]})


@app.post("/api/admin/updater/reset-hard")
async def admin_reset_hard(request: Request) -> JSONResponse:
    """Force the working tree back onto origin/<current-branch>.

    Recovery for a wedged / diverged checkout: `git fetch` + `git reset
    --hard origin/<current-branch>`, discarding local commits and edits to
    tracked files. Stays on the same branch and does no `git clean`, so
    untracked / gitignored files (library.json, .env, .offline_cache/,
    .background/) survive. Does NOT run setup.py or reboot.
    """
    _require_admin(request)
    if state.updater_busy:
        raise HTTPException(409, "An update operation is already running.")

    lib = await get_library()
    allow_any = _autoupdate_cfg(lib)["dev_mode"]
    async with _updater_lock:
        await _set_updater_phase("applying", "Resetting hard to origin…", busy=True)
        res = await updater.reset_hard(allow_any=allow_any)
        if not res.get("ok"):
            await _set_updater_phase("error", res.get("error", "reset failed"), busy=False)
            raise HTTPException(500, res.get("error", "reset failed"))
        await _set_updater_phase("idle",
                                f"Reset {res['branch']} to origin ({res['commit']}).",
                                busy=False)
    return JSONResponse({"ok": True, "branch": res["branch"], "commit": res["commit"]})


# ── Routes: Env Keys ──────────────────────────────────────────────────────────
# Companion to the auto-updater: when an update introduces a new required env
# key (e.g. an API key for a new feature), the dashboard surfaces a banner and
# the admin sets the missing keys from the Updates tab — no shell access needed.

class EnvKeysReq(BaseModel):
    # key → value. Empty string removes the entry from .env (Settings falls
    # back to its declared default). Non-listed keys are left untouched.
    keys: dict[str, str]


@app.get("/api/admin/env-keys")
async def admin_get_env_keys(request: Request) -> JSONResponse:
    """Return the env-key feature registry + which keys are currently missing.

    The non-admin UI uses /api/state.missing_env_keys (a redacted subset) to
    drive its banner; this admin-only endpoint includes the full registry
    metadata (label / description / required / secret) plus a `present` flag
    on each entry so the form can show "(set)" instead of asking again.
    """
    _require_admin(request)
    out = []
    for feat in ENV_KEY_FEATURES:
        val = getattr(settings, feat["attr"], "") or ""
        present = bool(val)
        # tmdb_api_key has the live admin-overrides path too — reflect that.
        if feat["attr"] == "tmdb_api_key" and not present:
            try:
                lib_raw = _load_lib_raw()
                ov = (lib_raw.get("settings", {}) or {}).get("admin_overrides", {}) or {}
                if ov.get("tmdb_api_key"):
                    present = True
            except Exception:
                pass
        out.append({
            "key":         feat["key"],
            "label":       feat["label"],
            "description": feat["description"],
            "required":    feat["required"],
            "secret":      feat["secret"],
            "present":     present,
        })
    return JSONResponse({"features": out})


@app.post("/api/admin/env-keys")
async def admin_set_env_keys(request: Request, body: EnvKeysReq) -> JSONResponse:
    """Write the provided keys into .env (creating it if needed), then reload
    Settings so the new values take effect for the running process without
    requiring a service restart.

    Only keys listed in ENV_KEY_FEATURES are accepted — the endpoint can't be
    used as a generic .env editor.
    """
    _require_admin(request)
    allowed = {feat["key"] for feat in ENV_KEY_FEATURES}
    sanitised: dict[str, str] = {}
    for k, v in (body.keys or {}).items():
        if k not in allowed:
            raise HTTPException(400, f"Env key '{k}' is not in the writable feature registry.")
        # Strip newlines and surrounding whitespace; never write multi-line values.
        sanitised[k] = (v or "").replace("\r", "").replace("\n", "").strip()
    written = _write_env_keys(sanitised)
    _reload_settings()
    # Broadcast immediately so the banner clears on every connected client.
    try:
        await broadcast("state", state_snapshot())
    except Exception:
        pass
    return JSONResponse({"ok": True, "written": written,
                         "missing_env_keys": _missing_env_keys()})


# ── Routes: Idle Background Video ─────────────────────────────────────────────

@app.get("/api/admin/background-video")
async def admin_get_background_video(request: Request) -> JSONResponse:
    _require_admin(request)
    lib = await get_library()
    bg = lib.get("settings", {}).get("background_video") or {}
    path = bg.get("path", "")
    exists = bool(path) and Path(path).exists()
    size = Path(path).stat().st_size if exists else 0
    return JSONResponse({
        "name":              bg.get("name", ""),
        "volume":            int(bg.get("volume", 50)),
        "enabled":           bool(bg.get("enabled", True)) if bg else False,
        "exists":            exists,
        "size_bytes":        size,
        "currently_playing": state.background_playing,
    })


@app.post("/api/admin/background-video")
async def admin_upload_background_video(
    request: Request,
    file: UploadFile = File(...),
) -> JSONResponse:
    _require_admin(request)
    filename = Path(file.filename or "background").name
    if not filename or Path(filename).suffix.lower() not in VIDEO_EXTS:
        raise HTTPException(400, "File must be a supported video format.")
    BACKGROUND_DIR.mkdir(parents=True, exist_ok=True)
    # Wipe any existing background file(s) so disk usage stays bounded
    for old in BACKGROUND_DIR.iterdir():
        try:
            old.unlink()
        except Exception:
            pass
    dest = BACKGROUND_DIR / filename
    with open(dest, "wb") as f:
        while chunk := await file.read(1024 * 1024):
            f.write(chunk)
    lib = await get_library()
    bg_settings = lib.setdefault("settings", {}).setdefault("background_video", {})
    bg_settings["path"]    = str(dest.resolve())
    bg_settings["name"]    = dest.name
    bg_settings.setdefault("volume", 50)
    bg_settings.setdefault("enabled", True)
    await put_library(lib)
    # If bg was already on screen, swap in the new file immediately
    if state.background_playing:
        asyncio.create_task(_play_background_video())
    return JSONResponse({
        "ok":         True,
        "name":       dest.name,
        "size_bytes": dest.stat().st_size,
    })


@app.delete("/api/admin/background-video")
async def admin_delete_background_video(request: Request) -> JSONResponse:
    _require_admin(request)
    lib = await get_library()
    bg = lib.get("settings", {}).get("background_video") or {}
    path = bg.get("path", "")
    if path:
        try:
            Path(path).unlink()
        except Exception:
            pass
    if "background_video" in lib.get("settings", {}):
        lib["settings"].pop("background_video", None)
        await put_library(lib)
    if state.background_playing:
        state.background_playing = False
        state.vlc_volume = state.user_volume_before_bg
        await vlc("pl_stop")
    return JSONResponse({"ok": True})


@app.post("/api/admin/background-video/volume")
async def admin_set_background_volume(request: Request, req: BackgroundVolumeReq) -> JSONResponse:
    _require_admin(request)
    capped = max(0, min(200, int(req.volume)))
    lib = await get_library()
    bg_settings = lib.setdefault("settings", {}).setdefault("background_video", {})
    bg_settings["volume"] = capped
    await put_library(lib)
    # Apply the new volume live if bg is on screen right now
    if state.background_playing:
        cap = await _global_max_volume()
        applied = min(capped, cap)
        raw = max(0, min(512, round(applied / 100 * 256)))
        await vlc("volume", val=str(raw))
        state.vlc_volume = applied
        await broadcast("state", state_snapshot())
    return JSONResponse({"ok": True, "volume": capped})


@app.post("/api/admin/background-video/enabled")
async def admin_set_background_enabled(request: Request, req: BackgroundEnabledReq) -> JSONResponse:
    _require_admin(request)
    lib = await get_library()
    bg_settings = lib.setdefault("settings", {}).setdefault("background_video", {})
    bg_settings["enabled"] = bool(req.enabled)
    await put_library(lib)
    if not req.enabled and state.background_playing:
        state.background_playing = False
        state.vlc_volume = state.user_volume_before_bg
        await vlc("pl_stop")
    return JSONResponse({"ok": True, "enabled": bool(req.enabled)})


# ── Routes: Stream-to-Device prep + HLS streaming ────────────────────────────
#
# These endpoints prepare an HLS bundle of any library file so the device's
# local <video> can stream it via hls.js (Chrome/Firefox/Edge) or Safari
# native HLS playback:
#   1. Client POSTs /api/library/{id}/offline-prepare with a file_path.
#   2. Server hashes the source into a cache key. If a bundle directory
#      `<sha>/master.m3u8` already exists, returns master_url + audios[] +
#      subtitles[] from meta.json. Otherwise kicks off a single ffmpeg job
#      that maps the video + every audio track + every text subtitle into
#      one HLS bundle and returns {ready:false, job_id, operation:"hls"}.
#   3. Client polls /api/library/offline-job/{job_id} until status=="done",
#      then hands master_url to hls.js (or sets <video>.src on Safari).
#      Per-rendition playlists + fmp4 segments are served from
#      /api/library/offline-cache/<sha>/<filename>.
#   4. Subtitles, skip-data, and the saved track picks all come back in the
#      same /offline-prepare response; the client wires them into the audio
#      / subtitle dropdowns and the skip-offer logic.
#
# The `offline` token in the endpoint paths is a historical artifact — these
# powered the older "download to device" Handoff feature. The cache namespace
# stays for backwards compatibility; the user-facing UX is stream-to-device.
# See docs/STREAMING.md for the full design.

OFFLINE_CACHE = Path(__file__).parent / ".offline_cache"
# Bump this when offline-output requirements change (codec rules, ffmpeg args,
# cache layout) so previously-cached bundles built by older logic get rebuilt
# on next request. v3-hls switched single-MP4 output to per-source HLS bundles;
# v4-hls moved subtitles out of the (broken) in-manifest renditions into
# standalone sub_<i>.vtt sidecars; v5-hls pinned the fmp4 init filename
# (-hls_fmp4_init_filename init_%v.mp4); v6-hls makes every output a bare name
# and runs ffmpeg with cwd=<bundle dir> so the init segment actually lands in
# the bundle on Windows (backslash playlist paths otherwise misdirect it → the
# player 404s init_video.mp4 and stalls with fragLoadError); v7-hls-abr emits
# multiple video variants (Original + 720p + 480p, capped at source height) so
# the player can offer Auto/manual quality switching.
OFFLINE_CACHE_VERSION = "v7-hls-abr"

# Stream-to-device (HLS prep) is unavailable on macOS hosts. ffmpeg/ffprobe run
# as children of the (non-GUI) server process, and macOS TCC blocks them from
# reading media in the user's protected ~/Downloads / ~/Desktop / ~/Documents —
# every prep would abort at the probe step with "Operation not permitted". VLC
# ("On TV") is unaffected because it's a separate, individually-granted app.
HLS_AVAILABLE = platform.system() != "Darwin"
HLS_UNAVAILABLE_MSG = (
    "Stream-to-device isn’t available on macOS hosts — the server can’t read "
    "media in TCC-protected folders. Use “On TV (VLC)” instead."
)
# Cap ffmpeg worker threads for offline prep so bulk Save Offline on a 30+ episode
# show can't saturate every core on the host. The browser and OS need headroom —
# without this, ffmpeg pegs all cores and the dashboard becomes unresponsive.
# Only used on the CPU (libx264) path; NVENC offloads to the GPU and ignores it.
OFFLINE_FFMPEG_THREADS = 2

# Watchdog timeout (seconds) for the all-GPU prep path only. The cuda-resident
# decode→scale→encode pipeline can occasionally DEADLOCK ffmpeg (NVDEC surface
# pool / muxer backpressure): it wedges at low CPU+GPU with no progress and no
# exit, so neither the failure-retry nor a crash supervisor would ever fire. If
# job["progress"] doesn't advance for this long while the encode is alive, we
# kill ffmpeg and let _run_offline_job retry on the transparent -hwaccel path.
# Generous enough that a genuinely-encoding job (out_time ticks every ~second)
# never trips it; only a true stall does.
GPU_STALL_TIMEOUT_SECS = 90

# Lower ffmpeg's OS scheduling priority so a bulk prep can't starve the web
# server (the asyncio event loop), VLC playback, or qBit — the whole point of
# "the website still works while prepping". On Windows we pass
# BELOW_NORMAL_PRIORITY_CLASS (== 0x00004000) via creationflags (also tames the
# NVIDIA driver's DPC/ISR "System Interrupts" storm during a fast NVENC encode).
# On POSIX we prepend `nice -n 10` to the command (preferred over preexec_fn,
# which is fork-unsafe in a threaded process); ffmpeg then yields CPU to every
# normal-priority process. Falls back to no-nice if the `nice` binary is absent.
_FFMPEG_SUBPROCESS_KW: dict = {}
if sys.platform == "win32":
    _FFMPEG_SUBPROCESS_KW["creationflags"] = 0x00004000  # BELOW_NORMAL_PRIORITY_CLASS

def _ffmpeg_nice_prefix() -> list[str]:
    """`['nice','-n','10']` on POSIX when available, else `[]` (Windows uses
    creationflags instead)."""
    if os.name == "posix" and shutil.which("nice"):
        return ["nice", "-n", "10"]
    return []


def _raise_own_priority() -> None:
    """Raise the StreamLink server process ABOVE normal priority so its
    control / UI / VLC-control request handling stays snappy even when a
    background prep (or anything else) is saturating the CPU.

    The server is overwhelmingly I/O-bound, so a high priority just lets its
    short request bursts preempt the encoder — it doesn't hog the box when idle.
    Combined with prep ffmpeg running BELOW normal (and the analyzer likewise),
    StreamLink's core path decisively wins the CPU. Both the HTTP and HTTPS
    uvicorn processes run this (each imports `main` → `lifespan`).

    Best-effort — never crashes startup:
    - Windows: HIGH_PRIORITY_CLASS (allowed for one's own process without admin;
      REALTIME is deliberately avoided — it can starve OS/driver threads).
    - POSIX: a negative nice, which needs privilege (root / CAP_SYS_NICE, e.g.
      the installed system service). Tries a strong boost, backs off, then gives
      up — even at nice 0 the de-prioritized encoder still yields to the server.
    """
    try:
        p = psutil.Process()
    except Exception:
        return
    try:
        if sys.platform == "win32":
            p.nice(psutil.HIGH_PRIORITY_CLASS)
            print("[priority] StreamLink server set to HIGH_PRIORITY_CLASS")
        else:
            for target in (-10, -5, -1):
                try:
                    p.nice(target)
                    print(f"[priority] StreamLink server niceness set to {target}")
                    break
                except (psutil.AccessDenied, PermissionError, OSError):
                    continue
            else:
                print("[priority] could not raise server niceness (needs root / "
                      "CAP_SYS_NICE) — prep/analyzer are still de-prioritized below it")
    except Exception as exc:
        print(f"[priority] could not raise server priority: {exc}")
# Lazy-probed once per process: True if this ffmpeg can open an h264_nvenc session
# on the host's GPU. Pascal (GTX 10xx) and newer all qualify; the only failure
# modes are (a) ffmpeg built without --enable-nvenc, (b) no NVIDIA driver loaded,
# (c) headless containers without /dev/nvidia*. We fall back to libx264 silently.
_nvenc_probe: dict[str, bool] = {}
_offline_jobs: dict[str, dict] = {}   # job_id → {id, src, out, status, operation, progress, error, started_at}
_stt_jobs: dict[str, dict] = {}       # job_id → {id, src, item_id, status, progress, error, tracks, ...}
_stt_available_probe: dict[str, bool] = {}   # cached stt.is_available() (see _stt_available)

# Cap how many ffmpeg prep jobs can run AT THE SAME TIME. Without this,
# /prep-all on a 77-episode pack fires asyncio.create_task for every file,
# spawning 77 simultaneous ffmpeg processes — host CPU is pegged, and
# consumer NVIDIA GPUs (Pascal/Turing) silently reject NVENC sessions past
# their 2–3 concurrent-encoder limit, so most jobs error out before
# emitting a single frame. Jobs stay in `pending` until they grab the
# semaphore; pending jobs are still surfaced by /prep-status and
# /api/offline-active so the UI shows the queue correctly.
OFFLINE_JOB_CONCURRENCY = 1
_OFFLINE_JOB_SEM: Optional[asyncio.Semaphore] = None

def _offline_job_sem() -> asyncio.Semaphore:
    # Lazily constructed so the semaphore binds to the running event loop on
    # first use (asyncio.Semaphore() at import time would attach to whatever
    # loop happened to be current, which uvicorn replaces on startup).
    global _OFFLINE_JOB_SEM
    if _OFFLINE_JOB_SEM is None:
        _OFFLINE_JOB_SEM = asyncio.Semaphore(OFFLINE_JOB_CONCURRENCY)
    return _OFFLINE_JOB_SEM


async def _has_nvenc() -> bool:
    """Detect whether ffmpeg can actually open an h264_nvenc session right now."""
    if "result" in _nvenc_probe:
        return _nvenc_probe["result"]
    ffmpeg = analyzer.ffmpeg_bin()
    if not ffmpeg:
        _nvenc_probe["result"] = False
        return False
    # Step 1: is the encoder compiled in at all?
    try:
        proc = await asyncio.create_subprocess_exec(
            ffmpeg, "-hide_banner", "-encoders",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
        if b"h264_nvenc" not in (out or b""):
            _nvenc_probe["result"] = False
            print("[offline] h264_nvenc not compiled into ffmpeg — using libx264.")
            return False
    except Exception:
        _nvenc_probe["result"] = False
        return False
    # Step 2: can we actually open a session? An encoder can be listed even when
    # the driver/library is missing, so we try a 1-frame dummy encode.
    try:
        proc = await asyncio.create_subprocess_exec(
            ffmpeg, "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=size=64x64:duration=0.04:rate=25",
            "-c:v", "h264_nvenc", "-frames:v", "1", "-f", "null", "-",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, err = await proc.communicate()
        ok = proc.returncode == 0
        if not ok:
            tail = (err or b"").decode("utf-8", "replace").strip().splitlines()[-1:]
            print(f"[offline] NVENC probe failed — using libx264. ({'; '.join(tail) or 'no detail'})")
    except Exception:
        ok = False
    _nvenc_probe["result"] = ok
    if ok:
        print("[offline] NVENC available — H.264 encodes will run on the GPU.")
    return ok


# Lazy-probed once per process: True if this ffmpeg exposes the `scale_cuda`
# filter, which the all-GPU prep pipeline needs to resize ON the GPU (keeping
# decode→scale→encode resident in VRAM). Gyan/BtbN "full" builds carry it;
# some "essentials" builds compile NVENC but not the CUDA filters, so we probe
# and fall back to the transparent `-hwaccel cuda` path (GPU decode, CPU scale)
# when it's missing.
_cuda_scale_probe: dict[str, bool] = {}

# Source codec / pixel-format combos NVDEC decodes reliably across every
# NVENC-capable consumer GPU. The all-GPU pipeline pins the decoder output to
# `cuda` (`-hwaccel_output_format cuda`), which — unlike the transparent
# `-hwaccel cuda` — has NO software fallback: an unsupported source HARD-fails
# the encode. So we only take that path for these safe 4:2:0 sources; anything
# else (AV1 on older cards, 4:2:2/4:4:4, 12-bit, odd codecs) uses the
# transparent path instead. A genuine failure still auto-retries on the
# transparent path in _run_offline_job, but gating keeps that rare.
_NVDEC_SAFE_CODECS  = {"h264", "hevc", "h265", "mpeg2video", "vc1", "vp9"}
_NVDEC_SAFE_PIXFMTS = {"yuv420p", "yuvj420p", "yuv420p10le", "nv12", "p010le"}


async def _has_cuda_scale() -> bool:
    """True if this ffmpeg lists the scale_cuda filter (needed for all-GPU prep)."""
    if "result" in _cuda_scale_probe:
        return _cuda_scale_probe["result"]
    ffmpeg = analyzer.ffmpeg_bin()
    if not ffmpeg:
        _cuda_scale_probe["result"] = False
        return False
    try:
        proc = await asyncio.create_subprocess_exec(
            ffmpeg, "-hide_banner", "-filters",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
        ok = b"scale_cuda" in (out or b"")
    except Exception:
        ok = False
    _cuda_scale_probe["result"] = ok
    if not ok:
        print("[offline] scale_cuda filter absent — prep will GPU-decode but "
              "CPU-scale (transparent -hwaccel cuda).")
    return ok


def _source_nvdec_safe(info: dict) -> bool:
    """True if the source is a codec+pixfmt NVDEC decodes reliably, so it's safe
    to pin the decoder output to VRAM (`-hwaccel_output_format cuda`)."""
    v = info.get("video") or {}
    codec = (v.get("codec") or "").lower()
    pix   = (v.get("pix_fmt") or "").lower()
    return codec in _NVDEC_SAFE_CODECS and pix in _NVDEC_SAFE_PIXFMTS

# HLS prep target — H.264 yuv420p video, AAC stereo audio, WebVTT subtitles.
#   • Video that's already H.264 yuv420p with a browser-safe profile is stream-copied
#     (no re-encode). Anything else transcodes to libx264 / h264_nvenc.
#   • Every audio track is transcoded to AAC stereo regardless of source — uniform
#     codec across renditions, and Safari iOS won't play FLAC/Opus/DTS/TrueHD.
#   • Text subtitles (subrip/ass/ssa) are remuxed to WebVTT. Image-based subs
#     (PGS/VOBSUB/DVB) can't go into HTML5 video tracks; they're reported in
#     meta.json as `skipped_image_subs` so the UI can flag them.
_HLS_PROFILE_BAD = {"high 10", "high 4:2:2", "high 4:4:4 predictive"}
_TEXT_SUB_CODECS  = {"subrip", "srt", "ass", "ssa", "webvtt", "mov_text", "text"}
_IMAGE_SUB_CODECS = {"hdmv_pgs_subtitle", "pgssub", "dvd_subtitle", "dvdsub",
                     "dvb_subtitle", "vobsub", "xsub"}

# ffmpeg ≥ 4.3 is required for reliable multi-rendition HLS with var_stream_map
# (subtitle groups and the agroup/sgroup tagging system are flaky on older builds).
_FFMPEG_MIN_VERSION = (4, 3)
_ffmpeg_version_probe: dict = {}

# HLS segment duration. 6 s is the de-facto default — Safari and hls.js both
# tune their buffering for this range, and segment-boundary seek granularity
# stays under the user-perceptible threshold.
HLS_SEGMENT_SECS = 6

# Adaptive-bitrate ladder: lower video renditions emitted alongside the original.
# Each rung is (name, target_height, maxrate_kbps, bufsize_kbps). The original
# (source-resolution) variant is always emitted as index 0; a rung is added only
# when the source is TALLER than it (no upscaling). The maxrate/bufsize VBV caps
# keep the down-rungs genuinely smaller and give the master playlist realistic
# BANDWIDTH numbers so hls.js / Safari pick sensibly under ABR.
HLS_ABR_LADDER = [
    ("video_720", 720, 3000, 6000),
    ("video_480", 480, 1200, 2400),
]


def _hls_video_variants(info: dict) -> list[dict]:
    """Video renditions to emit for this source, capped at its height.

    Returns a list ordered original-first:
      [{name, height, scale}]  where scale=None means "no -filter, keep source".
    Down-rungs carry maxrate/bufsize too. A ≤480p source yields a single
    variant (the original) — i.e. no ABR menu, today's behaviour.
    """
    src_h = int((info.get("video") or {}).get("height") or 0)
    variants: list[dict] = [{"name": "video", "height": src_h, "scale": None}]
    for name, h, maxrate, bufsize in HLS_ABR_LADDER:
        if src_h > h:
            variants.append({
                "name": name, "height": h, "scale": h,
                "maxrate": maxrate, "bufsize": bufsize,
            })
    return variants


def _ffprobe_bin() -> Optional[str]:
    """Locate ffprobe alongside ffmpeg (or via PATH)."""
    ffm = analyzer.ffmpeg_bin()
    if ffm:
        sib = Path(ffm).with_name("ffprobe" + Path(ffm).suffix)
        if sib.exists():
            return str(sib)
    return shutil.which("ffprobe")


async def _ffmpeg_version() -> Optional[tuple[int, int]]:
    """Return (major, minor) of the active ffmpeg binary, or None if unavailable.

    Cached per process — `ffmpeg -version` is cheap but we hit this on every
    prep job and there's no reason to re-spawn it.
    """
    if "result" in _ffmpeg_version_probe:
        return _ffmpeg_version_probe["result"]
    ffmpeg = analyzer.ffmpeg_bin()
    if not ffmpeg:
        _ffmpeg_version_probe["result"] = None
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            ffmpeg, "-version",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
        m = re.search(rb"ffmpeg version (\d+)\.(\d+)", out or b"")
        ver: Optional[tuple[int, int]] = (int(m.group(1)), int(m.group(2))) if m else None
    except Exception:
        ver = None
    _ffmpeg_version_probe["result"] = ver
    return ver


# Crude ISO 639-2/3-letter → display name. Anything not in the map falls back
# to the upper-cased code (better than blank), so unknown tags still render
# something the user can identify against the MKV's track list.
_LANG_NAMES = {
    "eng": "English", "jpn": "Japanese", "spa": "Spanish", "esp": "Spanish",
    "fre": "French",  "fra": "French",  "ger": "German",   "deu": "German",
    "ita": "Italian", "por": "Portuguese", "rus": "Russian",
    "chi": "Chinese", "zho": "Chinese", "kor": "Korean",
    "ara": "Arabic",  "hin": "Hindi",   "nld": "Dutch",
    "swe": "Swedish", "fin": "Finnish", "nor": "Norwegian",
    "dan": "Danish",  "pol": "Polish",  "tur": "Turkish",
    "ukr": "Ukrainian", "tha": "Thai",  "vie": "Vietnamese",
    "und": "",
}


def _track_label(track: dict, fallback: str) -> str:
    """Build a user-facing label for an audio/sub track."""
    lang = (track.get("language") or "und").lower()
    base = _LANG_NAMES.get(lang, lang.upper())
    title = (track.get("title") or "").strip()
    if title and base:
        return f"{base} ({title})"
    return title or base or fallback


def _ffprobe_full(path: str) -> dict:
    """Enumerate every stream + container metadata.

    Returns:
      {
        duration_sec, container,
        video: {codec, profile, pix_fmt, width, height} | None,
        audios:    [{idx, codec, language, title, channels, default}],
        subtitles: [{idx, codec, language, title, default, image_based}],
      }
    `idx` is the index within the stream's own type — what ffmpeg `-map`
    accepts as `0:a:<idx>` / `0:s:<idx>`.
    """
    binp = _ffprobe_bin()
    if not binp:
        return {}
    try:
        r = subprocess.run(
            [binp, "-v", "error", "-print_format", "json",
             "-show_streams", "-show_format", path],
            capture_output=True, text=True, timeout=15,
        )
        data = json.loads(r.stdout or "{}")
    except Exception:
        return {}
    fmt = data.get("format") or {}
    out: dict = {
        "duration_sec": float(fmt.get("duration", 0) or 0),
        "container":    fmt.get("format_name", ""),
        "video":        None,
        "audios":       [],
        "subtitles":    [],
    }
    audio_i = sub_i = 0
    for s in data.get("streams") or []:
        kind = s.get("codec_type")
        tags = s.get("tags") or {}
        disp = s.get("disposition") or {}
        if kind == "video" and out["video"] is None:
            out["video"] = {
                "codec":   s.get("codec_name", ""),
                "profile": (s.get("profile", "") or "").lower(),
                "pix_fmt": (s.get("pix_fmt", "") or "").lower(),
                "width":   s.get("width", 0),
                "height":  s.get("height", 0),
            }
        elif kind == "audio":
            out["audios"].append({
                "idx":      audio_i,
                "codec":    s.get("codec_name", ""),
                "language": (tags.get("language") or "und").lower(),
                "title":    tags.get("title", ""),
                "channels": s.get("channels", 0),
                "default":  bool(disp.get("default")),
            })
            audio_i += 1
        elif kind == "subtitle":
            codec = (s.get("codec_name", "") or "").lower()
            out["subtitles"].append({
                "idx":         sub_i,
                "codec":       codec,
                "language":    (tags.get("language") or "und").lower(),
                "title":       tags.get("title", ""),
                "default":     bool(disp.get("default")),
                "image_based": codec in _IMAGE_SUB_CODECS,
            })
            sub_i += 1
    return out


def _video_can_copy(v: Optional[dict]) -> bool:
    """True iff the video stream is already H.264 / yuv420p / good profile —
    safe to stream-copy into HLS instead of re-encoding.
    """
    if not v:
        return False
    return (v.get("codec") == "h264"
            and v.get("pix_fmt") == "yuv420p"
            and v.get("profile") not in _HLS_PROFILE_BAD)


# ── Aggressive local subtitle discovery ──────────────────────────────────────
# Sidecar subtitle file extensions VLC can load via `addsubtitle`.
_SUB_FILE_EXTS = {".srt", ".vtt", ".ass", ".ssa", ".sub"}
# Folder names releases commonly tuck subtitles into (alongside the video).
_SUB_DIR_NAMES = {"subs", "subtitles", "sub", "subtitle"}
# Full language names → canonical 3-letter codes (the reverse of _LANG_NAMES,
# plus a few aliases torrents use). Lets us read a language out of names like
# "2_English.srt", "Movie.Spanish.srt" or "3_Brazilian.Portuguese.srt".
_LANG_NAME_TO_CODE = {
    "english": "eng", "japanese": "jpn", "spanish": "spa", "castilian": "spa",
    "french": "fre", "german": "ger", "italian": "ita", "portuguese": "por",
    "brazilian": "por", "russian": "rus", "chinese": "chi", "mandarin": "chi",
    "cantonese": "chi", "korean": "kor", "arabic": "ara", "hindi": "hin",
    "dutch": "nld", "swedish": "swe", "finnish": "fin", "norwegian": "nor",
    "danish": "dan", "polish": "pol", "turkish": "tur", "ukrainian": "ukr",
    "thai": "tha", "vietnamese": "vie",
}


def _parse_sub_lang(name: str) -> str:
    """Best-effort canonical language code from a subtitle filename. Recognises
    ISO codes (en, eng) and English language names (English, 2_English.SDH),
    splitting on the usual separators. Returns "" when nothing matches."""
    tokens = re.split(r"[\s._\-\[\]()]+", Path(name).stem.lower())
    for tok in tokens:
        if tok in _LANG_NAME_TO_CODE:
            return _LANG_NAME_TO_CODE[tok]
    for tok in tokens:
        c = _canon_lang(tok)
        if c in _LANG_NAMES and c != "und":
            return c
    return ""


def _is_ai_sub_file(p: Path) -> bool:
    """True when a sidecar `.srt` is one we generated (`<stem>.<lang>.ai[.<model>].srt`)
    rather than a real downloaded/release subtitle. The upgrade watcher prefers a
    real sub over one of these."""
    return stt.AI_SUFFIX in p.stem.split(".")


def _discover_local_subs(video: Path) -> list[Path]:
    """Aggressively locate every sidecar subtitle file for `video`.

    Releases scatter subs across several layouts: next to the video
    (`Movie.srt`, `Movie.eng.srt`), or inside a `Subs/` / `Subtitles/` folder —
    flat for a single-video release, or in a per-episode subfolder named after
    the video for season packs. We scan the video's own directory plus any
    sibling subtitle folders (and one level up, in case the video sits in its
    own subfolder), recursing into the subtitle folders. A file belongs to this
    video when its name carries the stem, it lives in a folder named after the
    stem, or the release holds only one video (so loose subs must be its). For a
    lone-video release we take everything; for a season pack we keep only subs
    that match the episode stem, so neighbours' subs don't leak in.

    Returns de-duplicated absolute paths.
    """
    found: list[Path] = []
    seen: set[str] = set()

    def _is_sub(p: Path) -> bool:
        try:
            return p.is_file() and p.suffix.lower() in _SUB_FILE_EXTS
        except OSError:
            return False

    def _add(p: Path) -> None:
        try:
            rp = p.resolve()
        except OSError:
            return
        k = str(rp).lower()
        if k not in seen and _is_sub(rp):
            seen.add(k)
            found.append(rp)

    stem_l = video.stem.lower()
    parent = video.parent
    if not parent.exists():
        return found

    def _sole_video(d: Path) -> bool:
        try:
            vids = [f for f in d.iterdir()
                    if f.is_file() and f.suffix.lower() in VIDEO_EXTS]
        except OSError:
            return False
        return len(vids) <= 1

    def _belongs(p: Path, sole: bool) -> bool:
        n = p.stem.lower()
        if n == stem_l or n.startswith(stem_l + ".") or stem_l in n:
            return True
        if stem_l and stem_l in p.parent.name.lower():
            return True
        return sole

    sole_in_parent = _sole_video(parent)

    # 1) Same directory as the video.
    try:
        for p in parent.iterdir():
            if _is_sub(p) and _belongs(p, sole_in_parent):
                _add(p)
    except OSError:
        pass

    # 2) Dedicated subtitle folders beside the video and one level up, recursed.
    bases = [parent]
    if parent.parent != parent:
        bases.append(parent.parent)
    for base in bases:
        # The lone-video "take everything" fallback only applies to the video's
        # OWN directory. A shared Subs/ one level up may serve sibling episodes
        # in their own subfolders, so there we match strictly by stem.
        sole = sole_in_parent and base is parent
        try:
            entries = list(base.iterdir())
        except OSError:
            continue
        for d in entries:
            if not (d.is_dir() and d.name.lower() in _SUB_DIR_NAMES):
                continue
            try:
                for p in d.rglob("*"):
                    if _is_sub(p) and _belongs(p, sole):
                        _add(p)
            except OSError:
                pass
    # Stable order so loaded-sidecar ES IDs don't drift between replays
    # (iterdir/rglob order is filesystem-dependent).
    found.sort(key=lambda p: str(p).lower())
    return found


def _list_sidecar_subs(src: Path, item_id: str) -> list[dict]:
    """Aggressively list sidecar subtitle files for a video — everywhere
    `_discover_local_subs` looks (next to the file, in `Subs/`-style folders,
    one level up), not just exact-stem files beside it. This is what makes the
    on-device player surface subs stored in a `Subs/` folder. Each entry is
    servable via `/api/library/{id}/subtitle?path=…`, which converts
    SRT/ASS/SSA → WebVTT on demand.

    AI-generated subs the box wrote itself (`<stem>.<lang>.ai[.<model>].srt`,
    always next to the source) keep their `ai`/`model`/`stale` flags so the UI
    can label them and offer Regenerate; for every other file the language is
    guessed from the filename via `_parse_sub_lang`.
    """
    out: list[dict] = []
    stem = src.stem
    cur_model = stt.model_name()
    try:
        src_dir = src.resolve().parent
    except OSError:
        src_dir = src.parent
    for p in _discover_local_subs(src):
        # `.sub` is usually image-based VobSub (needs the .idx + OCR/burn-in) —
        # the browser can't render it and ffmpeg can't make WebVTT from it, so
        # don't offer it on-device. VLC still loads it natively on the TV path.
        if p.suffix.lower() == ".sub":
            continue
        ai = False
        model = ""
        lang = ""
        # AI subs live beside the source and encode lang + model in the stem.
        if p.parent == src_dir and p.stem.startswith(stem + "."):
            segs = p.stem[len(stem) + 1:].split(".")
            ai = stt.AI_SUFFIX in segs
            if ai:
                i = segs.index(stt.AI_SUFFIX)
                model = segs[i + 1] if i + 1 < len(segs) else ""
                lang = _canon_lang(segs[0]) if segs[0] != stt.AI_SUFFIX else ""
        if not lang:
            lang = _parse_sub_lang(p.name)
        out.append({
            "name":  p.name,
            "lang":  lang or "und",
            "ai":    ai,
            "model": model,
            "stale": bool(ai and model != cur_model),
            "fmt":   p.suffix.lower().lstrip("."),
            "url":   f"/api/library/{item_id}/subtitle?path={quote(str(p))}",
        })
    return out


def _needs_stt_subs(info: dict, default_lang: str = "") -> bool:
    """Decide whether a source warrants generated (STT) subtitles.

    True when there is no usable *text* subtitle: none at all, only image-based
    (PGS/VOBSUB/DVB) tracks, or — when `default_lang` is set — none matching that
    language. `default_lang` is canonicalized so en/eng etc. compare equal.
    """
    text_subs = [s for s in (info.get("subtitles") or []) if not s.get("image_based")]
    if not text_subs:
        return True
    if default_lang:
        want = _canon_lang(default_lang)
        if not any(_canon_lang(s.get("language")) == want for s in text_subs):
            return True
    return False


def _srt_to_vtt(srt: str) -> str:
    """Convert SRT cues to WebVTT. SRT timestamps use a comma; VTT uses a period."""
    body = re.sub(r"(\d\d:\d\d:\d\d),(\d\d\d)", r"\1.\2", srt)
    return "WEBVTT\n\n" + body


async def _sub_to_vtt(path: Path) -> str:
    """Read a sidecar subtitle file and return WebVTT text the browser can render.

    `.vtt` passes through and `.srt` is converted inline (both instant); `.ass`,
    `.ssa` and text `.sub` are converted via ffmpeg on demand — this is the
    "prep" for a non-WebVTT sub. Raises on conversion failure so the endpoint can
    surface it. Styling (ASS karaoke/positioning) is lost in the WebVTT downgrade.
    """
    ext = path.suffix.lower()
    if ext == ".vtt":
        return path.read_text(encoding="utf-8", errors="replace")
    if ext == ".srt":
        return _srt_to_vtt(path.read_text(encoding="utf-8", errors="replace"))
    ffmpeg = analyzer.ffmpeg_bin()
    if not ffmpeg:
        raise RuntimeError("ffmpeg unavailable for subtitle conversion")
    proc = await asyncio.create_subprocess_exec(
        ffmpeg, "-y", "-i", str(path), "-f", "webvtt", "pipe:1",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        **_FFMPEG_SUBPROCESS_KW,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0 or not stdout.strip():
        tail = stderr.decode("utf-8", "replace")[-200:].strip()
        raise RuntimeError(tail or "ffmpeg subtitle conversion failed")
    return stdout.decode("utf-8", "replace")


def _offline_cache_key(src: Path) -> str:
    st = src.stat()
    raw = f"{OFFLINE_CACHE_VERSION}|{src}|{int(st.st_mtime)}|{st.st_size}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def _build_hls_ffmpeg_args(
    ffmpeg: str,
    src: Path,
    info: dict,
    use_nvenc: bool,
    full_gpu: bool = False,
) -> tuple[list[str], list[dict], list[dict], list[dict]]:
    """Construct the full ffmpeg invocation that emits one HLS bundle.

    All OUTPUT paths are bare filenames (init/segments/playlists/subs) — the
    caller MUST run ffmpeg with cwd set to the bundle directory so they land
    there. This is deliberate: ffmpeg derives the init segment's directory by
    parsing the playlist path, which fails on Windows backslash paths and sends
    the init file to the wrong place (→ 404 → fragLoadError). Bare names + cwd
    sidesteps that on every OS. The only absolute path is the `-i` source input.

    Emits an ABR ladder: the original video (stream-copied when compatible) plus
    a 720p and/or 480p down-rung (capped at source height — see
    `_hls_video_variants`). All video variants share one audio group, so the
    player switches video quality without re-fetching audio.

    Returns (args, kept_audios, kept_subs, video_variants) — audios/subs are the
    tracks (from `info`) included in the output in manifest order (image-based
    subs filtered out); video_variants is the ladder actually emitted, in master
    playlist order (index 0 = original).
    """
    audios = list(info.get("audios") or [])
    subs   = [s for s in (info.get("subtitles") or []) if not s.get("image_based")]
    videos = _hls_video_variants(info)
    # Original can stream-copy when already browser-safe H.264; down-rungs must
    # always re-encode (they're scaled). Decoupled from use_nvenc so the source
    # rung stays a cheap remux even when the GPU encoder is present.
    copy_original = _video_can_copy(info.get("video"))

    # Exactly one audio rendition should be marked DEFAULT in the master
    # playlist. Honor the source's disposition.default if any; otherwise the
    # first audio is the default.
    default_audio_i = next(
        (i for i, a in enumerate(audios) if a.get("default")),
        0 if audios else -1,
    )

    args: list[str] = [
        ffmpeg, "-y",
        # -progress emits machine-readable key=value to stdout — we tail it
        # below for an accurate % instead of guessing from output-dir growth.
        "-progress", "pipe:1", "-nostats",
    ]

    # GPU-accelerated decode for the NVENC path, in two tiers. Without either,
    # NVENC only offloads the *encode* while the source is decoded (and every
    # down-rung scaled) on the CPU — so the CPU pegs while the GPU idles.
    #
    #   full_gpu  → `-hwaccel cuda -hwaccel_output_format cuda` + scale_cuda:
    #     the WHOLE pipeline (decode → scale → nvenc) stays resident in VRAM —
    #     no per-frame GPU↔CPU copy, no CPU scaling. The caller only sets this
    #     when the source must fully re-encode (NOT copy_original) AND is
    #     NVDEC-safe: that guarantees there's NO stream-copy rung in the ladder.
    #     Mixing `-c:v copy` with cuda-filtered rungs DEADLOCKS ffmpeg (the copy
    #     stream races ahead while the muxer/NVDEC surface pool backs up, wedging
    #     at low CPU/GPU with no progress and no exit). `-extra_hw_frames`
    #     enlarges the decoder surface pool so the parallel scale_cuda branches
    #     don't starve it. A stall watchdog + retry in _run_offline_job still
    #     guards against any residual hang.
    #   else (transparent) → `-hwaccel cuda` only: NVDEC decodes, frames
    #     auto-download to system memory for the CPU `scale` filter, and ffmpeg
    #     silently falls back to software decode for any codec NVDEC can't
    #     handle — so it can never hard-fail (or hang) a copy+encode ladder.
    #
    # Only added when something actually decodes (a pure stream-copy rung has
    # no decode to accelerate).
    needs_decode = (not copy_original) or len(videos) > 1
    if full_gpu and needs_decode:
        args += ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda",
                 "-extra_hw_frames", "8"]
    elif use_nvenc and needs_decode:
        args += ["-hwaccel", "cuda"]

    args += [
        # Larger input buffers coalesce disk reads; without these the Windows
        # storage stack can dominate kernel time during a fast encode.
        "-thread_queue_size", "1024",
        "-rtbufsize", "64M",
        "-i", str(src),
    ]

    # Stream mapping for the HLS output: the source video mapped once per ladder
    # rung, then every audio. Text subs are mapped separately into their own
    # sidecar .vtt outputs further below.
    for _ in videos:
        args += ["-map", "0:v:0"]
    for a in audios:
        args += ["-map", f"0:a:{a['idx']}"]

    # Per-output video codec. Output video stream index `i` matches `videos[i]`:
    #   • original (i==0): stream-copy when browser-safe, else encode full-res
    #   • down-rungs:      always encode + scale=-2:<h> (even width for yuv420p),
    #                      with a maxrate/bufsize VBV cap so the rendition is
    #                      genuinely smaller and the master BANDWIDTH is realistic
    # NVENC encodes on the GPU when available; decode runs on NVDEC too (see
    # the `-hwaccel` block above). On the full_gpu path the scale happens on the
    # GPU as well (scale_cuda), so frames never leave VRAM; otherwise a CPU
    # `scale` filter sits between the NVDEC decode and the NVENC encode.
    def _encode_video(i: int, v: dict) -> list[str]:
        a: list[str] = []
        if full_gpu:
            # All-GPU: scale_cuda both resizes AND pins the pixel format on the
            # GPU, handing nvenc a ready cuda surface — so NO `-pix_fmt yuv420p`
            # (that would force a hwdownload/CPU convert and defeat the point).
            # The original full-res rung still routes through scale_cuda (to its
            # own height) purely to normalise to browser-safe 8-bit 4:2:0.
            a += [
                f"-filter:v:{i}", f"scale_cuda=-2:{v['height']}:format=yuv420p",
                f"-c:v:{i}", "h264_nvenc",
                "-preset", "medium", "-rc", "vbr", f"-cq:v:{i}", "23",
            ]
        elif use_nvenc:
            if v["scale"]:
                a += [f"-filter:v:{i}", f"scale=-2:{v['scale']}"]
            a += [
                f"-c:v:{i}", "h264_nvenc",
                "-preset", "medium", "-rc", "vbr", f"-cq:v:{i}", "23",
                f"-pix_fmt:v:{i}", "yuv420p",
            ]
        else:
            if v["scale"]:
                a += [f"-filter:v:{i}", f"scale=-2:{v['scale']}"]
            a += [
                f"-c:v:{i}", "libx264",
                "-preset", "veryfast", f"-crf:v:{i}", "23",
                # Caps x264 worker threads so the host stays responsive during
                # bulk /prep-all runs. NVENC ignores this (runs on the GPU).
                "-threads", str(OFFLINE_FFMPEG_THREADS),
                f"-pix_fmt:v:{i}", "yuv420p",
            ]
        if v.get("maxrate"):
            a += [f"-maxrate:v:{i}", f"{v['maxrate']}k",
                  f"-bufsize:v:{i}", f"{v['bufsize']}k"]
        a += [f"-profile:v:{i}", "high", f"-level:v:{i}", "4.1"]
        return a

    for i, v in enumerate(videos):
        if i == 0 and copy_original:
            args += [f"-c:v:{i}", "copy"]
        else:
            args += _encode_video(i, v)

    # Audio: every track to AAC stereo, regardless of source. Safari iOS only
    # plays AAC/MP3/AC3/EAC3 reliably and won't decode FLAC/Opus/DTS in MP4 —
    # uniform AAC across renditions is the only safe lowest common denominator.
    if audios:
        args += ["-c:a", "aac", "-b:a", "160k", "-ac", "2"]

    args += [
        "-f", "hls",
        "-hls_time", str(HLS_SEGMENT_SECS),
        "-hls_playlist_type", "vod",
        "-hls_segment_type", "fmp4",
        # independent_segments: every segment starts on a keyframe so the
        # player can switch renditions without an extra fetch round-trip.
        "-hls_flags", "independent_segments",
        # Template the fmp4 init filename per-variant. Two reasons this is
        # critical, both of which produce the *same* symptom — a fatal hls.js
        # `fragLoadError` on the init segment after the manifest + variant
        # playlists already parsed (so audio/sub dropdowns populate first,
        # making it look like "everything loaded but won't play"):
        #   1. Without an explicit -hls_fmp4_init_filename, ffmpeg picks its OWN
        #      %v expansion for the init segment, which across versions doesn't
        #      always match the `#EXT-X-MAP:URI=` it writes into the playlist.
        #   2. The init filename MUST be a BARE name (no directory). ffmpeg
        #      joins it to the directory it parses out of the playlist path; a
        #      full path breaks the encode ("Failed to open segment"), and on
        #      Windows the backslash playlist path defeats that dir-parse so the
        #      init lands in the process CWD instead of the bundle → 404.
        # The portable fix: bare names for EVERY output (init/segments/playlists
        # /subs below) and run ffmpeg with cwd=<bundle dir> (see _run_offline_job)
        # so all of them land in the bundle on every OS. See GOTCHAS.md.
        "-hls_fmp4_init_filename", "init_%v.mp4",
        "-hls_segment_filename", "seg_%v_%05d.m4s",
        "-master_pl_name", "master.m3u8",
    ]

    # Build the var_stream_map for the HLS output — VIDEO + AUDIO ONLY.
    #
    # Subtitles are deliberately NOT part of the HLS manifest. ffmpeg's HLS
    # muxer cannot package WebVTT renditions: a single inline subtitle works,
    # but two or more (declared as their own `s:N,sgroup:…` variants) fail with
    # "No streams to mux were specified" / "Could not write header". Every real
    # release MKV has many subtitle tracks, so in-manifest subs meant HLS prep
    # ALWAYS failed. Instead we emit one standalone `sub_<i>.vtt` per text track
    # (see below) and the player wires them as <track> children. See GOTCHAS.md.
    # One entry per video variant (all sharing the single audio group) followed
    # by one per audio rendition. %v in the playlist/segment/init templates
    # expands to each `name:` tag, so the bundle gets video.m3u8 / video_720.m3u8
    # / video_480.m3u8 (+ matching init_*/seg_*).
    parts: list[str] = []
    for i, v in enumerate(videos):
        vp = [f"v:{i}"]
        if audios:
            vp.append("agroup:aud")
        vp.append(f"name:{v['name']}")
        parts.append(",".join(vp))

    for i, a in enumerate(audios):
        p = [f"a:{i}", "agroup:aud", f"name:audio_{i}",
             f"language:{a.get('language') or 'und'}"]
        if i == default_audio_i:
            p.append("default:yes")
        parts.append(",".join(p))

    args += ["-var_stream_map", " ".join(parts)]
    # Per-rendition playlist template — %v expands to the `name:` tag above.
    # Bare name (relative to ffmpeg's cwd, which the caller sets to the bundle
    # dir) — see the init-filename note above for why every output is bare.
    args += ["%v.m3u8"]

    # Sidecar subtitle outputs, one additional WebVTT output file per text sub,
    # produced in the SAME ffmpeg pass. subrip / ass / ssa convert transparently;
    # ASS styling (karaoke, positioning, fonts) is lost — see GOTCHAS.md. The
    # `sub_<i>.vtt` index matches `meta.json["subtitles"][i]`.
    for i, s in enumerate(subs):
        args += [
            "-map", f"0:s:{s['idx']}",
            "-c:s", "webvtt",
            "-f", "webvtt",
            f"sub_{i}.vtt",   # bare name — lands in ffmpeg's cwd (the bundle dir)
        ]

    return args, audios, subs, videos


async def _run_offline_job(job_id: str) -> None:
    """Build an HLS bundle for one source file. The output is a directory
    keyed by `_offline_cache_key(src)` under OFFLINE_CACHE containing the
    master playlist, per-rendition playlists, segments, and meta.json.
    """
    job = _offline_jobs.get(job_id)
    if not job:
        return
    if not HLS_AVAILABLE:
        job["status"] = "error"; job["error"] = HLS_UNAVAILABLE_MSG
        return
    is_bulk = job.get("queue") == "bulk"
    # Priority-prep precedence. A bulk job parks here until no priority HLS prep is
    # queued or encoding — that's interactive (fullscreen "Prep for Device" /
    # play-on-device) AND admin force-prep, both of which must run ahead of
    # overnight / idle / manual bulk prep. The file a bulk job is *currently*
    # encoding is separately booted by _preempt_running_bulk so the slot frees
    # without waiting for it. (The `not prep_paused` escape lets a bulk job fall
    # through to park at the pause gate below instead of spinning here.)
    if is_bulk:
        while _priority_hls_pending() > 0 and not state.prep_paused:
            await asyncio.sleep(0.25)
    # Hold pending until the global concurrency slot frees up. This is what
    # keeps a 77-file /prep-all from spawning 77 ffmpegs at once.
    async with _offline_job_sem():
        # An interactive prep may have arrived while this bulk job sat in the
        # semaphore queue. Yield the slot straight back and re-queue so the
        # interactive job (also waiting on the slot) takes it first. (When prep is
        # paused, skip this and fall through to park at the pause gate instead — a
        # paused job holds no slot, so interactive still wins without the churn.)
        if is_bulk and not state.prep_paused and _priority_hls_pending() > 0:
            job["status"] = "pending"
            asyncio.create_task(_requeue_offline_job(job_id))
            return
        out_dir = Path(job["out"])
        # Sibling-job race: another worker may have produced this bundle while
        # we sat in the queue. If so, exit fast.
        if (out_dir / "master.m3u8").exists():
            job["progress"] = 1.0
            job["status"]   = "done"
            return
        # Global pause gate. A bulk ("Prep for later" / overnight) job that reaches
        # the head of the queue while prep is paused marks itself "paused" and
        # EXITS — releasing the single concurrency slot so an interactive
        # play-on-device prep can still run. _resume_prep() re-spawns this worker
        # when the user (or the overnight window) resumes. Interactive jobs ignore
        # the gate entirely. See PrepPauseReq / _pause_prep / _resume_prep.
        if is_bulk and state.prep_paused:
            job["status"] = "paused"
            return
        # Admin force-prep stop gate. The admin Stop control sets admin_prep_stop
        # to cancel the whole force-prep batch; an "admin" job that reaches the
        # slot after Stop was pressed cancels itself (releasing the slot so other
        # work proceeds). A hard stop additionally terminates the in-flight encode
        # — handled below via the `_admin_stopped` branch. Unlike the bulk pause
        # gate, this is a one-way cancel: stopped jobs are not auto-resumed.
        if job.get("queue") == "admin" and state.admin_prep_stop:
            job["status"] = "cancelled"
            return
        job["status"] = "processing"
        # Re-baseline started_at to when the work actually begins so per-job
        # ETAs reflect real ffmpeg throughput, not queue wait time.
        job["started_at"] = time.time()
        src = Path(job["src"])
        hls_log.info("job %s START src=%s out=%s", job_id, src, out_dir.name)
        tmp_dir = out_dir.with_name(out_dir.name + ".part")
        ffmpeg = analyzer.ffmpeg_bin()
        if not ffmpeg:
            job["status"] = "error"; job["error"] = "ffmpeg not available."
            hls_log.error("job %s ABORT: ffmpeg binary not found on PATH", job_id)
            return
        ver = await _ffmpeg_version()
        if ver is None or ver < _FFMPEG_MIN_VERSION:
            job["status"] = "error"
            need = f"{_FFMPEG_MIN_VERSION[0]}.{_FFMPEG_MIN_VERSION[1]}"
            have = (f"{ver[0]}.{ver[1]}" if ver else "unknown")
            job["error"] = f"ffmpeg too old ({have}) — need ≥ {need} for HLS prep."
            hls_log.error("job %s ABORT: %s", job_id, job["error"])
            return
        info = await asyncio.to_thread(_ffprobe_full, str(src))
        duration = float(info.get("duration_sec", 0) or 0)
        if not info.get("video"):
            job["status"] = "error"; job["error"] = "No video stream in source."
            hls_log.error(
                "job %s ABORT: no video stream in %s (ffprobe keys: %s)",
                job_id, src, sorted(info.keys()),
            )
            return

        # Clean any leftover .part dir from a previous crashed run. Offloaded to a
        # thread so a large stale bundle's recursive unlink can't stall the event
        # loop (and with it every other HTTP request) while prep is starting.
        if tmp_dir.exists():
            await asyncio.to_thread(shutil.rmtree, tmp_dir, ignore_errors=True)
        tmp_dir.mkdir(parents=True, exist_ok=True)

        try:
            use_nvenc = await _has_nvenc()
            down_encoder = "h264_nvenc" if use_nvenc else "libx264"
            copy_original = _video_can_copy(info.get("video"))
            # All-GPU pipeline (decode→scale_cuda→nvenc resident in VRAM), chosen
            # only when:
            #   • the build has scale_cuda (_has_cuda_scale), and
            #   • the source is NVDEC-safe (_source_nvdec_safe), and
            #   • the source must FULLY re-encode (not copy_original) — i.e. there
            #     is NO `-c:v copy` rung in the ladder. Mixing stream-copy with
            #     cuda-filtered rungs deadlocks ffmpeg (hangs at low CPU/GPU). By
            #     restricting to the all-encode case we both dodge that and target
            #     the worst CPU offender (e.g. h265 packs that re-encode 3 rungs).
            # Copyable H.264 sources keep the proven transparent path (cheap copy
            # original + CPU-scaled down-rungs). Pinning frames to VRAM has no
            # software fallback, so a stall/failure auto-retries on the
            # transparent path below (full_gpu=False) — never regressing a file.
            full_gpu = (
                use_nvenc
                and not copy_original
                and await _has_cuda_scale()
                and _source_nvdec_safe(info)
            )

            while True:
                args, kept_audios, kept_subs, kept_videos = _build_hls_ffmpeg_args(
                    ffmpeg, src, info, use_nvenc, full_gpu=full_gpu,
                )
                # Record encoder for admin/UI display. The original rung may copy
                # while the ABR down-rungs (if any) still transcode, so reflect both.
                if copy_original and len(kept_videos) > 1:
                    job["encoder"] = f"copy+{down_encoder}"
                elif copy_original:
                    job["encoder"] = "copy"
                else:
                    job["encoder"] = down_encoder

                hls_log.info(
                    "job %s encode: encoder=%s duration=%.1fs videos=%d audios=%d subs=%d nvenc=%s gpu=%s",
                    job_id, job["encoder"], duration,
                    len(kept_videos), len(kept_audios), len(kept_subs), use_nvenc, full_gpu,
                )
                # Run ffmpeg at lowered OS priority (POSIX: `nice -n 10`; Windows:
                # BELOW_NORMAL via _FFMPEG_SUBPROCESS_KW) so the bulk encode yields CPU
                # to the web server, VLC, and qBit — keeping the dashboard responsive.
                cmd = _ffmpeg_nice_prefix() + args
                hls_log.info(
                    "job %s ffmpeg cmd: %s",
                    job_id, " ".join(shlex.quote(a) for a in cmd),
                )

                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    # Run inside the staging dir: every OUTPUT arg is a bare filename
                    # (init/segments/playlists/subs), so they all land here. This is
                    # what keeps the fmp4 init segment in the bundle on Windows,
                    # where a backslash playlist path otherwise misdirects it (→ 404
                    # → fragLoadError). The `-i` source is absolute, so cwd is safe.
                    cwd=str(tmp_dir),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    **_FFMPEG_SUBPROCESS_KW,
                )
                # Expose the handle so _pause_prep(kill=True) can terminate this encode.
                job["_proc"] = proc

                # Drain stderr concurrently into a bounded buffer. Two reasons:
                # (1) ffmpeg with -nostats still writes stream mapping, warnings and
                #     the fatal error line to stderr; if nobody reads it the OS pipe
                #     buffer can fill and ffmpeg blocks on write → proc.wait() hangs.
                # (2) on failure we want the *whole* error, not the 500-char tail
                #     job["error"] keeps for the UI — it goes to logs/hls.log.
                stderr_tail: deque[str] = deque(maxlen=300)

                async def _drain_stderr() -> None:
                    assert proc.stderr is not None
                    while True:
                        try:
                            line = await proc.stderr.readline()
                        except Exception:
                            return
                        if not line:
                            return
                        stderr_tail.append(line.decode("utf-8", "replace").rstrip())

                async def _drain_progress() -> None:
                    assert proc.stdout is not None
                    while True:
                        try:
                            line = await proc.stdout.readline()
                        except Exception:
                            return
                        if not line:
                            return
                        try:
                            txt = line.decode("ascii", "replace").strip()
                        except Exception:
                            continue
                        if not txt or "=" not in txt:
                            continue
                        k, _, v = txt.partition("=")
                        if k in ("out_time_ms", "out_time_us") and duration > 0:
                            # out_time_ms is microseconds despite the name on
                            # older ffmpeg; out_time_us is always microseconds.
                            try:
                                out_us = int(v)
                                job["progress"] = max(0.0, min(0.99,
                                    (out_us / 1_000_000.0) / duration))
                            except ValueError:
                                pass
                        elif k == "progress" and v == "end":
                            job["progress"] = 1.0
                            return

                # Stall watchdog — GUARDS THE ALL-GPU ATTEMPT ONLY. If the cuda
                # pipeline deadlocks it hangs forever at low CPU/GPU with no exit;
                # nothing else can break that, so we watch out_time and kill the
                # encode if it stops advancing. The kill makes proc exit non-zero,
                # which (since no intentional-kill flag is set) falls into the
                # `if full_gpu:` retry below → transparent path. Skipped entirely
                # on the transparent / libx264 paths, which are proven and whose
                # progress always advances.
                stall = {"killed": False}

                async def _stall_watchdog() -> None:
                    # Needs a known duration to read progress; without one,
                    # job["progress"] never advances even on a healthy encode, so
                    # skip rather than false-kill (the encode still runs).
                    if not full_gpu or duration <= 0:
                        return
                    last = -1.0
                    idle = 0.0
                    while proc.returncode is None:
                        await asyncio.sleep(5)
                        cur = float(job.get("progress") or 0.0)
                        if cur > last + 1e-9:
                            last, idle = cur, 0.0
                            continue
                        idle += 5
                        if idle >= GPU_STALL_TIMEOUT_SECS:
                            stall["killed"] = True
                            hls_log.warning(
                                "job %s all-GPU encode STALLED %.0fs with no "
                                "progress — killing to retry on transparent path",
                                job_id, idle,
                            )
                            try:
                                proc.kill()
                            except Exception:
                                pass
                            return

                progress_task = asyncio.create_task(_drain_progress())
                stderr_task   = asyncio.create_task(_drain_stderr())
                watchdog_task = asyncio.create_task(_stall_watchdog())
                await proc.wait()
                # Both drain loops end on pipe EOF once the process exits; bound the
                # wait so a wedged pipe can't hang the job indefinitely. The
                # watchdog ends on its own once proc.returncode is set.
                for t in (progress_task, stderr_task, watchdog_task):
                    try:
                        await asyncio.wait_for(t, timeout=5)
                    except (asyncio.TimeoutError, asyncio.CancelledError):
                        t.cancel()

                job["_proc"] = None
                if proc.returncode == 0:
                    break  # success → leave the retry loop and write meta.json

                # Did _preempt_running_bulk() boot us for an interactive prep? If so,
                # this isn't a failure — re-queue this bulk file as "pending" so it
                # resumes once the interactive encode releases the slot (it restarts
                # from scratch; HLS prep can't checkpoint mid-file).
                if job.pop("_preempted", False):
                    job["status"]   = "pending"
                    job["progress"] = 0.0
                    await asyncio.to_thread(shutil.rmtree, tmp_dir, ignore_errors=True)
                    asyncio.create_task(_requeue_offline_job(job_id))
                    hls_log.info("job %s re-queued after interactive preemption", job_id)
                    return
                # Did _pause_prep(kill=True) terminate us on purpose? If so, this
                # isn't a real failure — re-queue the job as "paused" (it restarts
                # from scratch on resume; HLS prep can't checkpoint mid-file) and
                # don't surface a spurious error.
                if job.pop("_paused_kill", False):
                    job["status"] = "paused"
                    job["progress"] = 0.0
                    await asyncio.to_thread(shutil.rmtree, tmp_dir, ignore_errors=True)
                    hls_log.info("job %s PAUSED (encode terminated by user)", job_id)
                    return
                # Did the admin hard-stop this force-prep encode? Same deal — the
                # non-zero rc is intentional, so mark it cancelled (not an error)
                # and drop the partial bundle. Not re-queued; the batch is over.
                if job.pop("_admin_stopped", False):
                    job["status"] = "cancelled"
                    job["progress"] = 0.0
                    await asyncio.to_thread(shutil.rmtree, tmp_dir, ignore_errors=True)
                    hls_log.info("job %s CANCELLED (admin hard-stop)", job_id)
                    return
                # All-GPU attempt failed or stalled. The cuda decode→scale_cuda→
                # nvenc chain can be unsupported for this source/driver (non-zero
                # exit) or DEADLOCK (the watchdog killed it). Either way, wipe the
                # partial bundle and retry ONCE on the transparent CPU-scale path
                # before surfacing an error — so the GPU pipeline can never leave a
                # file unpreppable.
                if full_gpu:
                    reason = ("stalled (watchdog kill)" if stall["killed"]
                              else f"failed rc={proc.returncode}")
                    tail = "\n".join(stderr_tail).strip()
                    hls_log.warning(
                        "job %s all-GPU encode %s — retrying on the transparent "
                        "-hwaccel path.\n  stderr tail:\n%s",
                        job_id, reason, tail[-800:] or "(no stderr captured)",
                    )
                    full_gpu = False
                    await asyncio.to_thread(shutil.rmtree, tmp_dir, ignore_errors=True)
                    tmp_dir.mkdir(parents=True, exist_ok=True)
                    job["progress"] = 0.0
                    continue

                err = "\n".join(stderr_tail).strip()
                elapsed = time.time() - job["started_at"]
                hls_log.error(
                    "job %s FAILED rc=%s after %.1fs src=%s\n"
                    "  ffmpeg cmd: %s\n"
                    "  ffmpeg stderr (last %d lines):\n%s",
                    job_id, proc.returncode, elapsed, src,
                    " ".join(shlex.quote(a) for a in args),
                    len(stderr_tail), err or "(no stderr captured)",
                )
                job["status"] = "error"
                job["error"]  = f"ffmpeg failed: {err[-500:] or 'unknown error'}"
                await asyncio.to_thread(shutil.rmtree, tmp_dir, ignore_errors=True)
                return

            # Write meta.json with everything the UI needs to build dropdowns
            # without re-probing the bundle. Held next to the manifest so a
            # straight directory move keeps them in sync.
            meta = {
                "version":      OFFLINE_CACHE_VERSION,
                "src":          str(src),
                "duration_sec": duration,
                # ABR video ladder, master-playlist order (idx 0 = original).
                # Informational for admin/API — the player builds its quality
                # menu from hls.js `levels`, not from this ordering.
                "videos": [
                    {
                        "idx":      i,
                        "name":     v["name"],
                        "height":   v["height"],
                        "label":    (f"{v['height']}p (Source)" if i == 0
                                     else f"{v['height']}p"),
                    }
                    for i, v in enumerate(kept_videos)
                ],
                "audios": [
                    {
                        "idx":      i,
                        "playlist": f"audio_{i}.m3u8",
                        "language": a.get("language") or "und",
                        "title":    a.get("title") or "",
                        "label":    _track_label(a, f"Audio {i+1}"),
                        "default":  (i == 0) if all(not x.get("default") for x in kept_audios)
                                    else bool(a.get("default")),
                    }
                    for i, a in enumerate(kept_audios)
                ],
                "subtitles": [
                    {
                        "idx":      i,
                        # Standalone WebVTT sidecar emitted alongside the HLS
                        # bundle (not an in-manifest rendition). The player loads
                        # it as a <track> child. Relative to the bundle dir.
                        "file":     f"sub_{i}.vtt",
                        "language": s.get("language") or "und",
                        "title":    s.get("title") or "",
                        "label":    _track_label(s, f"Subtitles {i+1}"),
                    }
                    for i, s in enumerate(kept_subs)
                ],
                "skipped_image_subs": [
                    {"codec": s["codec"], "language": s.get("language") or "und"}
                    for s in (info.get("subtitles") or []) if s.get("image_based")
                ],
            }
            (tmp_dir / "meta.json").write_text(json.dumps(meta, indent=2))

            # Atomic-ish swap: rename .part dir into place. Path.rename onto an
            # existing directory fails on every platform, so we drop any prior
            # output first; the master.m3u8 guard at the top of this function
            # handles the "another worker beat us" race separately. The recursive
            # drop of a prior bundle runs in a thread so it doesn't stall the loop.
            if out_dir.exists():
                await asyncio.to_thread(shutil.rmtree, out_dir, ignore_errors=True)
            tmp_dir.rename(out_dir)

            job["progress"] = 1.0
            job["status"]   = "done"
            size_mb = (await asyncio.to_thread(_dir_size_bytes, out_dir)) / 1_000_000
            hls_log.info(
                "job %s DONE in %.1fs encoder=%s size=%.1f MB src=%s",
                job_id, time.time() - job["started_at"], job["encoder"], size_mb, src,
            )
            # Auto-generate subtitles for sources that have none usable. Reuses
            # the ffprobe we already ran; enqueues a bulk STT job that runs after
            # this HLS encode releases the shared concurrency slot. Best-effort —
            # a failure here must never fail the HLS bundle.
            try:
                await _ensure_stt_for(src, job.get("item_id", ""), info=info, queue="bulk")
            except Exception as exc:
                hls_log.warning("job %s: STT enqueue skipped: %s", job_id, exc)
        except Exception as e:
            hls_log.exception("job %s CRASHED: %s", job_id, e)
            job["status"] = "error"; job["error"] = str(e)
            await asyncio.to_thread(shutil.rmtree, tmp_dir, ignore_errors=True)


def _pause_prep(kill: bool) -> int:
    """Pause bulk stream-prep. Sets the global gate so no further bulk job starts.

    kill=False ("Finish current file, then halt") — the file ffmpeg is encoding
    right now runs to completion; every queued file then holds at the gate.
    kill=True ("Stop now") — the in-flight bulk encode is terminated immediately
    for instant relief; it restarts from scratch on resume (HLS prep can't
    checkpoint mid-file). Interactive play-on-device encodes are never killed.

    Returns the number of encodes terminated.
    """
    state.prep_paused = True
    killed = 0
    if kill:
        for j in _offline_jobs.values():
            if j.get("queue") != "bulk" or j.get("status") != "processing":
                continue
            proc = j.get("_proc")
            if proc is not None and proc.returncode is None:
                j["_paused_kill"] = True   # tells _run_offline_job this was intentional
                try:
                    proc.terminate()
                except Exception:
                    pass
                killed += 1
        # Kill running bulk STT (whisper) too — it's the heaviest background load and
        # was previously left churning the CPU/GPU long after HLS prep "paused", which
        # made the box barely usable in the morning. The signalled job re-queues as
        # "paused" (see _run_stt_job) and _resume_prep re-spawns it later.
        for j in _stt_jobs.values():
            if j.get("queue") != "bulk" or j.get("status") != "processing":
                continue
            ev = j.get("_cancel")
            if ev is not None:
                ev.set()
            proc = j.get("_proc")
            if proc is not None:
                try:
                    proc.kill()   # whisper ignores graceful term; SIGKILL/TerminateProcess
                except Exception:
                    pass
            killed += 1
    return killed


def _resume_prep() -> int:
    """Clear the global pause gate and re-spawn a worker for every paused job.

    Paused jobs exited their task at the gate (so they didn't hold the
    concurrency slot), so resuming means creating fresh `_run_offline_job`
    tasks for them. Returns how many were re-queued.
    """
    state.prep_paused = False
    n = 0
    for jid, j in list(_offline_jobs.items()):
        if j.get("status") == "paused":
            j["status"] = "pending"
            j["_proc"] = None
            j.pop("_paused_kill", None)
            asyncio.create_task(_run_offline_job(jid))
            n += 1
    for jid, j in list(_stt_jobs.items()):
        if j.get("status") == "paused":
            j["status"] = "pending"
            asyncio.create_task(_run_stt_job(jid))
            n += 1
    return n


def _bulk_processing_now() -> bool:
    """True if any bulk HLS-prep or STT job is actively encoding/transcribing."""
    return any(j.get("queue") == "bulk" and j.get("status") == "processing"
               for j in (*_offline_jobs.values(), *_stt_jobs.values()))


def _priority_hls_pending() -> int:
    """Count priority (non-bulk) HLS jobs that are queued or encoding — both
    interactive (fullscreen "Prep for Device" / play-on-device) and admin
    force-prep. Bulk prep defers to these so a user-initiated prep OR an admin
    force-prep always jumps the queue ahead of overnight / idle / manual bulk
    prep — see the precedence gate in `_run_offline_job`."""
    return sum(1 for j in _offline_jobs.values()
               if j.get("queue") in ("interactive", "admin")
               and j.get("status") in ("pending", "processing"))


def _preempt_running_bulk(except_job_id: str = "") -> bool:
    """Terminate the bulk HLS encode currently holding the single concurrency slot
    (if any) so a just-queued interactive prep can take it over immediately, instead
    of waiting — potentially minutes — for the in-flight bulk file to finish. The
    killed job re-queues itself (see the `_preempted` branch in `_run_offline_job`)
    and resumes once interactive prep clears; it is **not** abandoned, and the global
    pause gate is left untouched. Returns True if an encode was preempted."""
    for j in _offline_jobs.values():
        if j.get("id") == except_job_id:
            continue
        if j.get("queue") != "bulk" or j.get("status") != "processing":
            continue
        proc = j.get("_proc")
        if proc is not None and proc.returncode is None:
            j["_preempted"] = True   # tells _run_offline_job this kill was intentional
            try:
                proc.terminate()
            except Exception:
                pass
            hls_log.info("job %s PREEMPTED by interactive prep", j.get("id"))
            return True
    return False


async def _requeue_offline_job(job_id: str, delay: float = 0.3) -> None:
    """Re-spawn a worker for a bulk job that yielded its slot to interactive prep,
    after a brief delay so the interactive encode claims the freed slot first."""
    await asyncio.sleep(delay)
    j = _offline_jobs.get(job_id)
    if j and j.get("status") == "pending":
        asyncio.create_task(_run_offline_job(job_id))


def _activity_kick() -> None:
    """Called on genuine user interaction. When idle-prep governs (and we're outside
    the intentional overnight window), stop bulk background work *immediately* — kill
    the in-flight HLS encode AND whisper — instead of waiting up to a full
    `auto_prep_loop` tick. This is the responsiveness lever: a user who shows up
    mid-idle-prep shouldn't sit through ~15 s (or, when whisper wasn't being killed,
    much longer) of a laggy box. The loop then leaves prep paused until the box is
    idle again. No-op once already paused, so it's cheap to call on every request."""
    if state.prep_paused:
        return
    if not state.idle_prep_on or state.overnight_open:
        return
    if not (state.auto_prep_engaged or _bulk_processing_now()):
        return
    killed = _pause_prep(kill=True)
    state.auto_prep_engaged = False
    print(f"[autoprep] user activity — pausing prep immediately (killed {killed} in-flight)")


async def _enqueue_library_prep() -> int:
    """Queue a bulk HLS-prep job for every un-prepped video file in the library.

    Used by the overnight auto-prep window. Idempotent: `_maybe_start_prep_job`
    skips files already cached or already queued, so re-running (e.g. on a
    mid-window restart) only adds genuinely-new work. Returns the count of files
    that still need prep (newly-queued + already in flight).
    """
    if not HLS_AVAILABLE:
        return 0
    lib = await get_library()
    queued = 0
    for item in lib.get("items", []):
        for f in item.get("files", []):
            p = Path(f.get("path", ""))
            if p.suffix.lower() not in VIDEO_EXTS:
                continue
            try:
                if not p.exists():
                    continue
            except OSError:
                continue
            st = await _maybe_start_prep_job(p, item.get("id", ""))
            if st.get("status") in ("processing", "pending", "paused"):
                queued += 1
            elif st.get("status") == "cached":
                # Already HLS-prepped, so the post-encode STT hook never fires for
                # it — check here so overnight still backfills generated subs for
                # files prepped before STT existed (or before the lang setting).
                await _ensure_stt_for(p, item.get("id", ""), queue="bulk")
            # Yield between every file (sync FS stat + task spawn, no internal
            # await) so scanning a large library never stalls the event loop and
            # the dashboard stays responsive during the overnight enqueue.
            await asyncio.sleep(0)
    return queued


# ── Auto-prep on play (settings.play_prep) ───────────────────────────────────
# When a video is played on VLC, immediately HLS-prep that episode for on-device
# viewing, then the rest of the playlist one episode at a time. These jobs are
# queued as "interactive" so they bypass the bulk pause gate AND survive the
# activity-kill — by design they run regardless of the idle/overnight settings or
# whether someone is actively using the box.

PLAY_PREP_TAIL_SECS = 300  # <5 min left on the current episode ⇒ skip it, start at the next


def _file_duration_sec(item: dict, profile_id: str, file_path: str) -> float:
    """Best-known duration (s) of a file from saved progress, or 0 if unknown."""
    fp = (item.get("progress", {}).get(profile_id, {})
              .get("file_progress", {}).get(file_path, {}))
    try:
        return float(fp.get("duration_sec", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def _start_interactive_prep_job(src: Path, item_id: str) -> Optional[str]:
    """Start (or coalesce with) an *interactive* HLS-prep job for `src`.

    Returns the job id, or None if the bundle is already cached or ffmpeg is
    unavailable. Interactive jobs ignore the bulk pause gate and are never killed
    by `_pause_prep` / `_activity_kick`, and they preempt any in-flight bulk encode
    so play-driven prep takes the slot immediately. Mirrors the queue-jumping
    branch of `offline_prepare` without an HTTP request."""
    out_dir = OFFLINE_CACHE / _offline_cache_key(src)
    if (out_dir / "master.m3u8").exists():
        return None
    existing = next(
        (j for j in _offline_jobs.values()
         if j["src"] == str(src) and j["status"] in ("pending", "processing", "paused")),
        None,
    )
    if existing:
        existing["queue"] = "interactive"
        if existing.get("status") == "paused":
            existing["status"] = "pending"
            existing["_proc"] = None
            existing.pop("_paused_kill", None)
            asyncio.create_task(_run_offline_job(existing["id"]))
        _preempt_running_bulk(except_job_id=existing["id"])
        return existing["id"]
    if not analyzer.ffmpeg_bin():
        return None
    job_id = secrets.token_hex(8)
    _offline_jobs[job_id] = {
        "id": job_id, "src": str(src), "out": str(out_dir),
        "status": "pending", "operation": "hls",
        "progress": 0.0, "error": None,
        "started_at": time.time(), "item_id": item_id,
        "queue": "interactive",
    }
    asyncio.create_task(_run_offline_job(job_id))
    _preempt_running_bulk(except_job_id=job_id)
    return job_id


# ── Admin force-prep (settings-free, admin-only) ─────────────────────────────
# An admin can force-prep the whole library (or one item) on demand. These jobs
# use a dedicated "admin" queue: like "interactive" they ignore the bulk pause
# gate and the activity-kill and preempt in-flight bulk work — so neither a
# viewer's Pause control nor live host activity can stop them. UNLIKE
# interactive, the admin can stop them via _stop_admin_prep (soft = let the
# current file finish then cancel the rest; hard = kill the in-flight encode now).

def _start_admin_prep_job(src: Path, item_id: str) -> Optional[str]:
    """Start (or coalesce with) an *admin* force-prep HLS job for `src`.

    Returns the job id, or None if the bundle is already cached or ffmpeg is
    unavailable. Mirrors `_start_interactive_prep_job` but tags the job
    `queue:"admin"` so the admin Stop control (and only it) can halt the batch."""
    out_dir = OFFLINE_CACHE / _offline_cache_key(src)
    if (out_dir / "master.m3u8").exists():
        return None
    existing = next(
        (j for j in _offline_jobs.values()
         if j["src"] == str(src) and j["status"] in ("pending", "processing", "paused")),
        None,
    )
    if existing:
        existing["queue"] = "admin"
        if existing.get("status") == "paused":
            existing["status"] = "pending"
            existing["_proc"] = None
            existing.pop("_paused_kill", None)
            asyncio.create_task(_run_offline_job(existing["id"]))
        _preempt_running_bulk(except_job_id=existing["id"])
        return existing["id"]
    if not analyzer.ffmpeg_bin():
        return None
    job_id = secrets.token_hex(8)
    _offline_jobs[job_id] = {
        "id": job_id, "src": str(src), "out": str(out_dir),
        "status": "pending", "operation": "hls",
        "progress": 0.0, "error": None,
        "started_at": time.time(), "item_id": item_id,
        "queue": "admin",
    }
    asyncio.create_task(_run_offline_job(job_id))
    _preempt_running_bulk(except_job_id=job_id)
    return job_id


async def _enqueue_admin_prep(item_id: str = "") -> int:
    """Force-queue an admin HLS-prep job for every un-prepped video file in the
    library, or just in `item_id` when given. Clears any prior Stop so the new
    batch runs. Returns the count of files newly queued (already-cached files are
    skipped). Yields between files so scanning a large library never stalls the
    event loop."""
    if not HLS_AVAILABLE:
        return 0
    state.admin_prep_stop = False   # a fresh batch overrides a previous Stop
    lib = await get_library()
    queued = 0
    for item in lib.get("items", []):
        if item_id and item.get("id") != item_id:
            continue
        for f in item.get("files", []):
            p = Path(f.get("path", ""))
            if p.suffix.lower() not in VIDEO_EXTS:
                continue
            try:
                if not p.exists():
                    continue
            except OSError:
                continue
            if _start_admin_prep_job(p, item.get("id", "")):
                queued += 1
            await asyncio.sleep(0)
    return queued


def _stop_admin_prep(hard: bool) -> dict:
    """Stop the admin force-prep batch. Sets the gate so no further "admin" job
    starts, and cancels every queued one. `hard=True` also terminates the
    in-flight admin encode immediately; `hard=False` lets the current file finish
    (it completes and is cached) while the rest are dropped. Returns counts."""
    state.admin_prep_stop = True
    cancelled = killed = 0
    for j in _offline_jobs.values():
        if j.get("queue") != "admin":
            continue
        st = j.get("status")
        if st == "pending":
            j["status"] = "cancelled"
            cancelled += 1
        elif st == "processing" and hard:
            proc = j.get("_proc")
            if proc is not None and proc.returncode is None:
                j["_admin_stopped"] = True   # tells _run_offline_job this rc is intentional
                try:
                    proc.terminate()
                except Exception:
                    pass
                killed += 1
    return {"cancelled": cancelled, "killed": killed}


def _force_prep_status() -> dict:
    """Live status of the admin force-prep batch for the admin panel."""
    admin_jobs = [j for j in _offline_jobs.values() if j.get("queue") == "admin"]
    active = [j for j in admin_jobs if j.get("status") in ("pending", "processing")]
    processing = [j for j in active if j.get("status") == "processing"]
    progress = (sum(float(j.get("progress", 0)) for j in active) / len(active)
                if active else 0.0)
    return {
        "hls_available": HLS_AVAILABLE,
        "active":        bool(active),
        "stopped":       state.admin_prep_stop,
        "total":         len(active),
        "processing":    len(processing),
        "pending":       len(active) - len(processing),
        "progress":      round(progress, 3),
    }


async def _play_prep_chain(item_id: str, files: list[str]) -> None:
    """Sequentially HLS-prep each file in `files` for on-device, one at a time.

    Started by a VLC play (`_maybe_start_play_prep`). Each file finishes before the
    next starts, so the episode the viewer is most likely to reach next is always
    prepped first. Cancellable: a new play cancels this chain (the in-flight ffmpeg
    keeps running — only further enqueues stop)."""
    if not HLS_AVAILABLE or not analyzer.ffmpeg_bin():
        return
    OFFLINE_CACHE.mkdir(exist_ok=True)
    for path in files:
        p = Path(path)
        try:
            if p.suffix.lower() not in VIDEO_EXTS or not p.exists():
                continue
        except OSError:
            continue
        out_dir = OFFLINE_CACHE / _offline_cache_key(p)
        if (out_dir / "master.m3u8").exists():
            continue
        job_id = _start_interactive_prep_job(p, item_id)
        if not job_id:
            continue
        # Block until this episode is done (or errored) before queuing the next.
        while True:
            await asyncio.sleep(2)
            j = _offline_jobs.get(job_id)
            if not j or j.get("status") in ("done", "error"):
                break


async def _maybe_start_play_prep(
    lib: dict, item: dict, profile_id: str,
    playlist: list[str], seek_sec: Optional[float],
) -> None:
    """If auto-prep-on-play is enabled, prep the playing episode (then the rest of
    the playlist) for on-device. If the viewer is resuming within `PLAY_PREP_TAIL_SECS`
    of the current episode's end, skip it and start at the next (prepping it would
    finish after they've moved on). Cancels any prior chain so only the current
    series' tail is being prepped."""
    if not HLS_AVAILABLE or not _play_prep_cfg(lib)["enabled"]:
        return
    files = list(playlist)
    if not files:
        return
    if seek_sec and seek_sec > 0 and len(files) > 1:
        dur = _file_duration_sec(item, profile_id, files[0])
        if dur and (dur - seek_sec) < PLAY_PREP_TAIL_SECS:
            files = files[1:]
    prior = state.play_prep_task
    if prior and not prior.done():
        prior.cancel()
    state.play_prep_task = asyncio.create_task(_play_prep_chain(item["id"], files))


# ── STT subtitle generation (whisper.cpp) ────────────────────────────────────
# Generated subs are sidecar `<stem>.<lang>.ai.srt` files written next to the
# source. They flow into both players through existing plumbing: VLC via
# `addsubtitle`, the HLS on-device player via `_list_sidecar_subs`. STT jobs
# share the offline-prep concurrency semaphore + pause gate so a transcription
# never competes with (or starves alongside) an HLS encode. See docs/STT.md.

def _stt_available() -> bool:
    """stt.is_available() with a 60 s TTL cache. Probing every state broadcast
    (reads .env + shutil.which) is wasteful, but caching forever meant installing
    whisper.cpp after startup never took effect until a full server restart — so
    the Generate-subtitles affordances stayed hidden. The TTL lets a fresh setup
    light them up within a minute, no restart needed."""
    now = time.time()
    if now - _stt_available_probe.get("at", 0.0) > 60:
        _stt_available_probe["result"] = stt.is_available()
        _stt_available_probe["at"] = now
    return bool(_stt_available_probe.get("result", False))


async def _attach_stt_to_vlc(src: Path, tracks: list[dict]) -> None:
    """Load a freshly-generated sidecar into VLC and select it — but only if VLC
    is still playing the same file. Mirrors download_subtitle's load+select."""
    try:
        cur = await _current_playback_path()
        if not cur or cur.resolve() != src.resolve():
            return
    except Exception:
        return
    dest = Path(tracks[0]["path"])
    await vlc("addsubtitle", val=str(dest.resolve()))
    await asyncio.sleep(0.6)
    vs = await vlc_status()
    if not vs:
        return
    cat = vs.get("information", {}).get("category", {})
    sub_ids = [
        int(k.split()[-1]) for k, v in cat.items()
        if k.startswith("Stream") and k.split()[-1].isdigit()
        and v.get("Type") == "Subtitle"
    ]
    if sub_ids:
        new_id = max(sub_ids)
        state.current_subtitle_track = new_id
        await vlc("subtitle_track", val=str(new_id))


async def _run_stt_job(job_id: str) -> None:
    """Transcribe one source to sidecar .srt(s). Shares the offline-prep
    semaphore + bulk pause gate; runs whisper at lowered OS priority (stt.py)."""
    job = _stt_jobs.get(job_id)
    if not job:
        return
    async with _offline_job_sem():
        # Bulk STT honors the same global pause as bulk HLS prep; interactive
        # (play-now / "Generate now") jobs ignore it.
        if job.get("queue") == "bulk" and state.prep_paused:
            job["status"] = "paused"
            return
        job["status"] = "processing"
        job["started_at"] = time.time()
        src = Path(job["src"])
        hls_log.info("stt job %s START src=%s translate=%s", job_id, src, job.get("translate"))

        # Cancellation: an activity-pause sets this event and kills the registered
        # whisper/ffmpeg subprocess so STT stops *immediately* (whisper is the heaviest
        # background load; without this it churned the CPU/GPU long after prep "paused").
        cancel = threading.Event()
        job["_cancel"] = cancel
        job["_proc"] = None

        def _set_progress(p: float) -> None:
            job["progress"] = max(0.0, min(1.0, p))

        def _on_proc(p) -> None:
            job["_proc"] = p

        result = await asyncio.to_thread(
            stt.generate, src,
            want_translation=bool(job.get("translate", True)),
            progress_cb=_set_progress,
            on_proc=_on_proc, cancel_check=cancel.is_set,
        )
        job["_proc"] = None
        if result.get("cancelled") or cancel.is_set():
            # Intentionally killed by a pause — re-queue (don't mark error/done).
            # _resume_prep re-spawns "paused" STT jobs on the next idle stretch.
            job["status"] = "paused"
            hls_log.info("stt job %s PAUSED (cancelled) src=%s", job_id, src)
            return
        if result.get("error"):
            job["status"] = "error"
            job["error"]  = result["error"]
            hls_log.error("stt job %s FAILED: %s", job_id, result["error"])
            return
        job["tracks"]   = result.get("tracks", [])
        job["progress"] = 1.0
        job["status"]   = "done"
        hls_log.info("stt job %s DONE tracks=%d src=%s", job_id, len(job["tracks"]), src)

        if job.get("vlc_attach") and job["tracks"]:
            await _attach_stt_to_vlc(src, job["tracks"])


def _maybe_start_stt_job(src: Path, item_id: str = "", *,
                         translate: bool = True, queue: str = "bulk",
                         vlc_attach: bool = False) -> dict:
    """Per-file: return current STT state, starting a job if none exists.

    Returns {status:"cached"} when current-model sidecars already exist,
    {status:"error"} when STT is unavailable, else {status, job_id, progress}.
    Generated subs made with a DIFFERENT model are treated as stale, so an
    explicit request regenerates them (generate() replaces the old ones).
    """
    if not _stt_available():
        return {"status": "error", "error": "Subtitle generation is not available on this host."}
    if stt.has_ai_subs(src) and not stt.ai_subs_stale(src):
        return {"status": "cached"}
    existing = next(
        (j for j in _stt_jobs.values()
         if j["src"] == str(src) and j["status"] in ("pending", "processing", "paused")),
        None,
    )
    if existing:
        # Promote a queued bulk job to interactive when a user asks for it now.
        if vlc_attach or queue == "interactive":
            existing["queue"] = "interactive"
            existing["vlc_attach"] = existing.get("vlc_attach") or vlc_attach
            if existing.get("status") == "paused":
                existing["status"] = "pending"
                asyncio.create_task(_run_stt_job(existing["id"]))
        return {"status": existing["status"], "job_id": existing["id"],
                "progress": existing["progress"]}
    job_id = secrets.token_hex(8)
    _stt_jobs[job_id] = {
        "id": job_id, "src": str(src), "item_id": item_id,
        "status": "pending", "progress": 0.0, "error": None,
        "tracks": [], "translate": bool(translate),
        "queue": queue, "vlc_attach": vlc_attach,
        "started_at": time.time(),
    }
    asyncio.create_task(_run_stt_job(job_id))
    return {"status": "processing", "job_id": job_id, "progress": 0.0}


async def _ensure_stt_for(src: Path, item_id: str = "", *,
                          info: Optional[dict] = None, queue: str = "bulk") -> Optional[dict]:
    """Start a bulk STT job for `src` iff STT is enabled and the source lacks a
    usable text subtitle (honoring the admin default language). Idempotent: skips
    when sidecars already exist or a job is already in flight. `info` (ffprobe
    output) is reused when the caller already has it to avoid a re-probe."""
    if not _stt_available():
        return None
    cfg = _stt_cfg(await get_library())
    if not cfg["enabled"]:
        return None
    if stt.has_ai_subs(src):
        return None
    if info is None:
        info = await asyncio.to_thread(_ffprobe_full, str(src))
    if not _needs_stt_subs(info, cfg["default_language"]):
        return None
    return _maybe_start_stt_job(src, item_id, translate=cfg["translate"], queue=queue)


# ── Optional component installer (admin-driven setup) ────────────────────────
# Lets the admin install the portable dependencies — ffmpeg, fpcalc, the
# whisper.cpp binary, and a whisper model — from the web panel instead of a
# terminal. The auto-updater runs setup.py NON-interactively and skips every
# install_* step, so these never landed on the production box otherwise. We reuse
# setup.py's URL/extract/detect helpers (it imports cleanly — its prompts live
# under __main__) and stream the download here for live progress. Once a file is
# in tools/, setup.py's detect_tools()+merge_tool_paths() keep .env pointing at it
# across future auto-updates, so a one-time install here persists. See docs/SETUP.md.

_component_jobs: dict[str, dict] = {}   # component → {status, progress, error, ...}
_COMPONENT_KEYS = ("ffmpeg", "fpcalc", "whisper", "whisper_model")
_WHISPER_MODEL_SIZES = ("base", "small", "medium")


async def _download_to(url: str, dest: Path, job: dict) -> None:
    """Stream `url` to `dest` (atomic via a `.part` temp), updating job['progress'].
    Follows redirects (GitHub/HuggingFace assets resolve to signed CDN URLs)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    async with httpx.AsyncClient(follow_redirects=True,
                                 timeout=httpx.Timeout(60.0, read=600.0)) as c:
        async with c.stream("GET", url, headers={"User-Agent": "StreamLink"}) as r:
            r.raise_for_status()
            total = int(r.headers.get("content-length") or 0)
            got = 0
            with open(tmp, "wb") as f:
                async for chunk in r.aiter_bytes(131072):
                    f.write(chunk)
                    got += len(chunk)
                    job["progress"] = (min(0.99, got / total) if total > 0 else 0.5)
    tmp.replace(dest)


async def _run_component_install(component: str, model: str = "base", build: str = "cpu") -> None:
    """Download + install one portable component, then point .env at it and clear
    the relevant detection caches so the new binary takes effect without a restart.

    `build` (whisper only) selects the whisper.cpp variant: cpu / cuda12 / cuda11.
    """
    job = _component_jobs[component]
    job["status"] = "downloading"
    try:
        import setup  # stdlib-only, prompts gated under __main__ — safe to import
    except Exception as e:
        job["status"] = "error"; job["error"] = f"setup module import failed: {e}"
        return
    try:
        if component == "whisper_model":
            size = model if model in _WHISPER_MODEL_SIZES else "base"
            fname = f"ggml-{size}.bin"
            url = f"https://huggingface.co/ggerganov/whisper.cpp/resolve/main/{fname}"
            dest = setup.TOOLS_DIR / "whisper" / "models" / fname
            await _download_to(url, dest, job)
            _write_env_keys({"_WHISPER_MODEL": str(dest)})

        elif component == "whisper":
            if os.name != "nt":
                raise RuntimeError("Portable whisper.cpp is Windows-only here — build it on "
                                   "Linux, or `brew install whisper-cpp` on macOS.")
            url = await asyncio.to_thread(setup._resolve_whisper_win_url, build)
            wh_dir = setup.TOOLS_DIR / "whisper"
            tmp_zip = setup.TOOLS_DIR / "_dl_whisper.zip"
            await _download_to(url, tmp_zip, job)
            ok = await asyncio.to_thread(setup._extract_archive, tmp_zip, wh_dir)
            tmp_zip.unlink(missing_ok=True)
            if not ok:
                raise RuntimeError("Could not extract the whisper.cpp archive.")
            binp = (setup._find_in_tree(wh_dir, ["whisper-cli.exe"])
                    or setup._find_in_tree(wh_dir, ["main.exe"])
                    or setup._find_in_tree(wh_dir, ["whisper.exe"]))
            if not binp:
                raise RuntimeError("whisper-cli.exe not found inside the downloaded archive.")
            _write_env_keys({"_WHISPER_BIN": binp})

        elif component == "ffmpeg":
            if os.name != "nt":
                raise RuntimeError("Portable ffmpeg is Windows-only here — install it via your "
                                   "package manager (brew / apt / dnf).")
            ff_dir = setup.TOOLS_DIR / "ffmpeg"
            tmp_zip = setup.TOOLS_DIR / "_dl_ffmpeg.zip"
            await _download_to(setup.FFMPEG_WIN_URL, tmp_zip, job)
            ok = await asyncio.to_thread(setup._extract_archive, tmp_zip, ff_dir)
            tmp_zip.unlink(missing_ok=True)
            if not ok:
                raise RuntimeError("Could not extract the ffmpeg archive.")
            binp = setup._find_in_tree(ff_dir, ["ffmpeg.exe"])
            if not binp:
                raise RuntimeError("ffmpeg.exe not found inside the downloaded archive.")
            _write_env_keys({"_FFMPEG_BIN": binp})
            _ffmpeg_version_probe.clear(); _nvenc_probe.clear(); _cuda_scale_probe.clear()

        elif component == "fpcalc":
            sysname = platform.system()
            if sysname == "Windows":
                url, arcname, exe = setup.FPCALC_WIN_URL, "_dl_fpcalc.zip", "fpcalc.exe"
            elif sysname == "Linux":
                url, arcname, exe = setup.FPCALC_LINUX_URL, "_dl_fpcalc.tar.gz", "fpcalc"
            else:
                url, arcname, exe = setup.FPCALC_MAC_URL, "_dl_fpcalc.tar.gz", "fpcalc"
            fp_dir = setup.TOOLS_DIR / "chromaprint"
            tmp_arc = setup.TOOLS_DIR / arcname
            await _download_to(url, tmp_arc, job)
            ok = await asyncio.to_thread(setup._extract_archive, tmp_arc, fp_dir)
            tmp_arc.unlink(missing_ok=True)
            if not ok:
                raise RuntimeError("Could not extract the fpcalc archive.")
            binp = setup._find_in_tree(fp_dir, [exe])
            if not binp:
                raise RuntimeError(f"{exe} not found inside the downloaded archive.")
            if os.name == "posix":
                try:
                    os.chmod(binp, 0o755)
                except OSError:
                    pass
            _write_env_keys({"_FPCALC_BIN": binp})
        else:
            raise RuntimeError(f"Unknown component: {component}")

        _stt_available_probe.clear()   # re-probe STT availability on next state read
        job["progress"] = 1.0
        job["status"] = "done"
        hls_log.info("component install DONE: %s", component)
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)
        hls_log.error("component install FAILED: %s — %s", component, e)


def _component_status_payload() -> dict:
    """Status of every installable portable dependency + any in-flight install job."""
    import setup
    win = (platform.system() == "Windows")
    ffmpeg  = analyzer.ffmpeg_bin()
    fpcalc  = analyzer.fpcalc_bin()
    whisper = stt.whisper_bin()
    model   = stt.whisper_model()
    comps = {
        "ffmpeg":        {"label": "ffmpeg",               "installed": bool(ffmpeg),
                          "path": ffmpeg or "",  "installable": win,
                          "purpose": "Stream-prep (HLS), Smart Skip, AI-subtitle audio extraction"},
        "fpcalc":        {"label": "fpcalc (chromaprint)", "installed": bool(fpcalc),
                          "path": fpcalc or "",  "installable": True,
                          "purpose": "Smart Skip intro/credits fingerprinting"},
        "whisper":       {"label": "whisper.cpp",          "installed": bool(whisper),
                          "path": whisper or "", "installable": win,
                          "purpose": "AI subtitle generation engine"},
        "whisper_model": {"label": "whisper model",        "installed": bool(model),
                          "path": model or "",   "installable": True,
                          "purpose": "AI subtitle language model (multilingual)"},
    }
    for k, c in comps.items():
        j = _component_jobs.get(k)
        if j:
            c["job"] = {"status": j["status"], "progress": round(j.get("progress", 0.0), 3),
                        "error": j.get("error")}
    return {
        "components":   comps,
        "platform":     platform.system(),
        "model_sizes":  list(_WHISPER_MODEL_SIZES),
        "stt_available": _stt_available(),
    }


def _read_meta(out_dir: Path) -> dict:
    try:
        return json.loads((out_dir / "meta.json").read_text())
    except (OSError, ValueError):
        return {}


def _dir_size_bytes(p: Path) -> int:
    """Sum the size of every file under `p` (one level deep is enough for HLS
    bundles, but we recurse anyway for safety). Missing/unreadable files
    contribute 0 — used by the admin cache view, where best-effort is fine.
    """
    total = 0
    try:
        for entry in p.rglob("*"):
            try:
                if entry.is_file():
                    total += entry.stat().st_size
            except OSError:
                pass
    except OSError:
        pass
    return total


def _saved_local_tracks(lib: dict, item: dict, profile_id: str, file_path: str) -> dict:
    """Return the local-player track picks saved for this profile + file.

    `subtitle_sel` is the per-file resolvable subtitle descriptor; `series_subtitle_sel`
    is the per-series fallback (applied when this file has no own pick). The client
    resolves whichever applies against its live track list. `subtitle_idx` is kept
    for the legacy bundle-index path only."""
    fp = (item.get("progress", {})
              .get(profile_id, {})
              .get("file_progress", {})
              .get(file_path, {}))
    out: dict = {}
    if "local_audio_idx" in fp:
        out["audio_idx"] = fp["local_audio_idx"]
    if "local_subtitle_idx" in fp:
        out["subtitle_idx"] = fp["local_subtitle_idx"]
    if isinstance(fp.get("subtitle_sel"), dict):
        out["subtitle_sel"] = fp["subtitle_sel"]
    series_sel = _get_series_sub_sel(lib, profile_id, _series_of_item(item))
    if series_sel:
        out["series_subtitle_sel"] = series_sel
    return out


class OfflinePrepareReq(BaseModel):
    file_path: str
    profile_id: str = ""   # Optional: when set, response includes saved track picks.
    # True ⇒ this is a "Prep for later" request (per-row Prep button / bulk prep) and
    # the job honors the global pause. False (default) ⇒ interactive play-on-device, which
    # must run even while bulk prep is paused so a user can still watch on demand.
    bulk: bool = False


@app.post("/api/library/{item_id}/offline-prepare")
async def offline_prepare(item_id: str, req: OfflinePrepareReq) -> JSONResponse:
    """Decide what processing a file needs for HLS browser playback.

    Returns either {ready:true, master_url, audios, subtitles, ...} if the
    bundle is already on disk, or {ready:false, job_id, operation:"hls"} after
    spawning a background prep job. The master_url points at
    /api/library/offline-cache/<sha>/master.m3u8 — both hls.js (Chrome /
    Firefox / Edge) and Safari (native HLS) load it the same way.
    """
    if not HLS_AVAILABLE:
        raise HTTPException(503, HLS_UNAVAILABLE_MSG)
    lib = await get_library()
    item = next((it for it in lib["items"] if it["id"] == item_id), None)
    if not item:
        raise HTTPException(404, "Item not found.")
    target = next((f for f in item.get("files", []) if f["path"] == req.file_path), None)
    if not target:
        raise HTTPException(404, "File not found in this item.")
    src = Path(target["path"])
    if not src.exists():
        raise HTTPException(404, "File not on disk.")

    sidecar_subs = await asyncio.to_thread(_list_sidecar_subs, src, item_id)
    saved_tracks = (_saved_local_tracks(lib, item, req.profile_id, req.file_path)
                    if req.profile_id else {})
    OFFLINE_CACHE.mkdir(exist_ok=True)
    key = _offline_cache_key(src)
    out_dir = OFFLINE_CACHE / key

    if (out_dir / "master.m3u8").exists():
        meta = _read_meta(out_dir)
        return JSONResponse({
            "ready":             True,
            "needs_processing":  False,
            "master_url":        f"/api/library/offline-cache/{key}/master.m3u8",
            "duration_sec":      meta.get("duration_sec", 0),
            "videos":            meta.get("videos", []),
            "audios":            meta.get("audios", []),
            "subtitles":         meta.get("subtitles", []),
            "skipped_image_subs": meta.get("skipped_image_subs", []),
            "subs":              sidecar_subs,
            "saved_tracks":      saved_tracks,
        })

    # Coalesce: if a job for this exact source is already running, return its id.
    existing = next(
        (j for j in _offline_jobs.values()
         if j["src"] == str(src) and j["status"] in ("pending", "processing", "paused")),
        None,
    )
    if existing:
        # An interactive (play-now / fullscreen prep) request must jump the queue.
        # Promote it to interactive (bypasses the pause gate); re-spawn it if it was
        # parked; and preempt any *other* bulk encode hogging the single slot so this
        # file starts now instead of waiting behind overnight / idle / manual prep.
        if not req.bulk:
            existing["queue"] = "interactive"
            if existing.get("status") == "paused":
                existing["status"] = "pending"
                existing["_proc"] = None
                existing.pop("_paused_kill", None)
                asyncio.create_task(_run_offline_job(existing["id"]))
            _preempt_running_bulk(except_job_id=existing["id"])
        return JSONResponse({
            "ready":             False,
            "needs_processing":  True,
            "job_id":            existing["id"],
            "operation":         existing["operation"],
            "subs":              sidecar_subs,
            "saved_tracks":      saved_tracks,
        })

    if not analyzer.ffmpeg_bin():
        raise HTTPException(503, "ffmpeg is not available — cannot prepare this file for streaming.")

    job_id = secrets.token_hex(8)
    _offline_jobs[job_id] = {
        "id": job_id, "src": str(src), "out": str(out_dir),
        "status": "pending", "operation": "hls",
        "progress": 0.0, "error": None,
        "started_at": time.time(),
        "item_id": item_id,
        # Interactive play-on-device bypasses the pause gate; "Prep for later" honors it.
        "queue": "bulk" if req.bulk else "interactive",
    }
    asyncio.create_task(_run_offline_job(job_id))
    if not req.bulk:
        # Interactive prep: boot any in-flight bulk encode so this file claims the
        # slot immediately. The booted job re-queues and resumes afterwards.
        _preempt_running_bulk(except_job_id=job_id)
    return JSONResponse({
        "ready":             False,
        "needs_processing":  True,
        "job_id":            job_id,
        "operation":         "hls",
        "subs":              sidecar_subs,
        "saved_tracks":      saved_tracks,
    })


@app.get("/api/library/{item_id}/subs")
async def list_file_subs(item_id: str, file_path: str = "") -> JSONResponse:
    """Re-list a file's sidecar subtitles (incl. late-downloaded ones).

    Cheap, no-prep endpoint the on-device player polls while watching: real subs
    often finish downloading after playback starts, so the client re-checks this
    to upgrade off an auto-applied AI sub. Mirrors the `subs` field of
    /offline-prepare via `_list_sidecar_subs`."""
    lib = await get_library()
    item = next((it for it in lib["items"] if it["id"] == item_id), None)
    if not item:
        raise HTTPException(404, "Item not found.")
    target = next((f for f in item.get("files", []) if f["path"] == file_path), None)
    if not target:
        raise HTTPException(404, "File not found in this item.")
    src = Path(target["path"])
    subs = await asyncio.to_thread(_list_sidecar_subs, src, item_id) if src.exists() else []
    return JSONResponse({"subs": subs})


async def _maybe_start_prep_job(src: Path, item_id: str = "") -> dict:
    """Per-file: return current prep state, starting an HLS job if none exists.

    Returns one of:
      {status:"cached"}                              — bundle already on disk
      {status:"processing", job_id, progress, operation}
      {status:"error", error}                        — ffmpeg unavailable / macOS
    """
    if not HLS_AVAILABLE:
        return {"status": "error", "error": HLS_UNAVAILABLE_MSG}
    OFFLINE_CACHE.mkdir(exist_ok=True)
    out_dir = OFFLINE_CACHE / _offline_cache_key(src)
    if (out_dir / "master.m3u8").exists():
        return {"status": "cached"}
    existing = next(
        (j for j in _offline_jobs.values()
         if j["src"] == str(src) and j["status"] in ("pending", "processing", "paused")),
        None,
    )
    if existing:
        return {"status": existing["status"], "job_id": existing["id"],
                "progress": existing["progress"], "operation": existing["operation"]}
    if not analyzer.ffmpeg_bin():
        return {"status": "error", "error": "ffmpeg not available."}
    job_id = secrets.token_hex(8)
    _offline_jobs[job_id] = {
        "id": job_id, "src": str(src), "out": str(out_dir),
        "status": "pending", "operation": "hls",
        "progress": 0.0, "error": None,
        "started_at": time.time(),
        "item_id": item_id,
        # Bulk / per-item / overnight prep — honors the global pause gate.
        "queue": "bulk",
    }
    asyncio.create_task(_run_offline_job(job_id))
    return {"status": "processing", "job_id": job_id, "progress": 0.0, "operation": "hls"}


def _peek_prep_state(src: Path) -> dict:
    """Read-only sibling of _maybe_start_prep_job — never starts new work.

    Used by /prep-status polling. Includes finished+errored jobs so the UI can
    show a final result for a few seconds before the bundle is consulted.
    """
    out_dir = OFFLINE_CACHE / _offline_cache_key(src)
    if (out_dir / "master.m3u8").exists():
        return {"status": "cached"}
    # Pick the most recent matching job — done/error states still surface here
    # so the UI can show "error" until the user retries.
    matching = [j for j in _offline_jobs.values() if j["src"] == str(src)]
    if matching:
        j = max(matching, key=lambda x: x.get("started_at", 0))
        out: dict = {
            "status": j["status"], "job_id": j["id"],
            "progress": j["progress"], "operation": j["operation"],
            "error": j["error"],
            "started_at": j.get("started_at", 0),
        }
        # Per-file ETA: scale elapsed by (1-progress)/progress. Skip until we
        # have at least 2s of elapsed AND >2% progress, otherwise the estimate
        # is dominated by ffmpeg startup overhead and bounces wildly.
        if j["status"] in ("pending", "processing"):
            elapsed = max(0.0, time.time() - j.get("started_at", time.time()))
            p = float(j.get("progress", 0))
            if elapsed > 2.0 and p > 0.02:
                out["eta_secs"] = max(0.0, elapsed * (1 - p) / p)
                out["elapsed_secs"] = elapsed
        return out
    return {"status": "needs_prep"}


@app.post("/api/library/{item_id}/prep-all")
async def prep_all(item_id: str) -> JSONResponse:
    """Kick off (or coalesce with existing) remux/transcode jobs for every video file in an item.

    Returns the current state of every file. The UI then polls /prep-status to track progress.
    """
    lib = await get_library()
    item = next((it for it in lib["items"] if it["id"] == item_id), None)
    if not item:
        raise HTTPException(404, "Item not found.")
    out = []
    for f in item.get("files", []):
        p = Path(f.get("path", ""))
        if not p.exists():
            out.append({"file_path": f.get("path", ""), "name": f.get("name", ""), "status": "missing"})
            continue
        st = await _maybe_start_prep_job(p, item_id)
        out.append({"file_path": f["path"], "name": f.get("name", ""), **st})
        # _maybe_start_prep_job is sync (FS stat + task spawn, no internal await),
        # so yield between files — a 77-file pack otherwise hogs the event loop in
        # one burst and briefly stalls every other request.
        await asyncio.sleep(0)
    return JSONResponse({"files": out, **_prep_summary(out)})


@app.get("/api/library/{item_id}/prep-status")
async def prep_status(item_id: str) -> JSONResponse:
    """Aggregated read-only prep state for one library item — what /prep-all kicked off."""
    lib = await get_library()
    item = next((it for it in lib["items"] if it["id"] == item_id), None)
    if not item:
        raise HTTPException(404, "Item not found.")
    out = []
    for f in item.get("files", []):
        p = Path(f.get("path", ""))
        if not p.exists():
            out.append({"file_path": f.get("path", ""), "name": f.get("name", ""), "status": "missing"})
            continue
        st = await asyncio.to_thread(_peek_prep_state, p)
        out.append({"file_path": f["path"], "name": f.get("name", ""), **st})
    return JSONResponse({"files": out, **_prep_summary(out)})


def _prep_summary(files: list[dict]) -> dict:
    """Roll up per-file states into the chip the UI shows on the library card.

    `eta_secs` is for the whole item: ETA of the in-flight file plus an
    extrapolated estimate for each file still queued (uses the in-flight file's
    observed throughput as the per-file estimate). When no file is in flight
    yet, returns None — the UI falls back to "starting…".
    """
    ready_states = ("cached", "done")
    busy_states  = ("pending", "processing")
    ready      = sum(1 for x in files if x["status"] in ready_states)
    processing = sum(1 for x in files if x["status"] in busy_states)
    paused     = sum(1 for x in files if x["status"] == "paused")
    errored    = sum(1 for x in files if x["status"] == "error")
    needs_prep = sum(1 for x in files if x["status"] == "needs_prep")
    missing    = sum(1 for x in files if x["status"] == "missing")

    # Aggregate ETA: sum of the in-flight files' remaining time + (queued *
    # estimated full-file time, derived from the busiest in-flight file).
    eta_secs: Optional[float] = None
    in_flight = [x for x in files if x["status"] in busy_states and x.get("eta_secs") is not None]
    if in_flight:
        in_flight_eta = sum(float(x["eta_secs"]) for x in in_flight)
        # Per-file estimate from the most-progressed in-flight file
        # (elapsed / progress = full-file estimate).
        per_file_estimate = max(
            (float(x["elapsed_secs"]) / max(float(x["progress"]), 0.01)
             for x in in_flight if x.get("elapsed_secs") and x.get("progress")),
            default=0.0,
        )
        # Files that have a job entry but aren't started yet contribute a full estimate each.
        not_started = sum(
            1 for x in files
            if x["status"] in busy_states and x.get("eta_secs") is None
        )
        # Files that have not been touched at all (needs_prep) also contribute.
        queued_full = not_started + needs_prep
        eta_secs = in_flight_eta + queued_full * per_file_estimate

    return {
        "total":       len(files),
        "ready":       ready,
        "processing":  processing,
        "paused":      paused,
        "errored":     errored,
        "needs_prep":  needs_prep,
        "missing":     missing,
        "eta_secs":    eta_secs,
    }


@app.get("/api/offline-active")
async def offline_active(request: Request, profile_id: str = "") -> JSONResponse:
    """Global view of every offline-prep job currently running.

    The library card chip is only rendered when the card is on-screen, so prep
    progress disappears the moment the user navigates away or reloads the page.
    This endpoint surfaces ALL active jobs (across all items) so a persistent
    indicator can stay visible regardless of which tab the user is on.

    Active = status in (pending, processing, paused) — paused jobs are included
    so the global bar keeps showing (with a Resume affordance) while the queue is
    held. Done/error jobs are NOT returned; those are still visible via the
    per-item /prep-status when the card mounts.

    All callers see the SAME counts/progress so prep activity looks identical to
    everyone. Item titles are redacted to "Library content" and item_id is blanked
    when the requesting profile can't see any one of the active items (admin_only
    without admin/elevated). Redaction is all-or-nothing per response, so the
    absence of a title doesn't leak which specific item the user is blocked from.
    """
    active = [j for j in _offline_jobs.values()
              if j.get("status") in ("pending", "processing", "paused")]
    if not active:
        return JSONResponse({"active": False, "paused": state.prep_paused,
                             "total_jobs": 0, "items": []})
    # Group by item_id, falling back to "" for jobs created before item_id
    # tagging existed (shouldn't happen after the upgrade, but be defensive).
    by_item: dict[str, list[dict]] = {}
    for j in active:
        by_item.setdefault(j.get("item_id", ""), []).append(j)
    lib = await get_library()
    is_admin = _check_admin(request)
    elevated_ids = {p["id"] for p in lib.get("profiles", []) if p.get("elevated")}
    is_elevated = bool(profile_id) and profile_id in elevated_ids
    items_by_id = {it["id"]: it for it in lib.get("items", [])}
    # Redact when ANY active job is restricted from this requester — keeps the
    # response shape identical so a curious user can't infer which one is hidden.
    redact = False
    if not is_admin and not is_elevated:
        for iid in by_item.keys():
            it = items_by_id.get(iid)
            if it and it.get("admin_only"):
                redact = True
                break
    items_out: list[dict] = []
    now = time.time()
    for item_id, jobs in by_item.items():
        total_progress = 0.0
        eta_total = 0.0
        eta_count = 0
        for j in jobs:
            p = float(j.get("progress", 0))
            total_progress += p
            elapsed = max(0.0, now - j.get("started_at", now))
            if elapsed > 2.0 and p > 0.02:
                eta_total += elapsed * (1 - p) / p
                eta_count += 1
        it = items_by_id.get(item_id)
        items_out.append({
            "item_id":    "" if redact else item_id,
            "title":      "Library content" if redact else (it.get("title", "") if it else ""),
            "processing": len(jobs),
            "progress":   round(total_progress / len(jobs), 3) if jobs else 0,
            "eta_secs":   round(eta_total, 1) if eta_count > 0 else None,
            "operation":  jobs[0].get("operation", "transcode"),
        })
    return JSONResponse({
        "active": True,
        "paused": state.prep_paused,
        "total_jobs": len(active),
        "processing_jobs": sum(1 for j in active if j.get("status") == "processing"),
        "pending_jobs":    sum(1 for j in active if j.get("status") == "pending"),
        "paused_jobs":     sum(1 for j in active if j.get("status") == "paused"),
        "items": items_out,
    })


@app.post("/api/offline-prep/pause")
async def offline_prep_pause(req: PrepPauseReq) -> JSONResponse:
    """Pause bulk stream-prep from the (non-admin) UI. `kill` decides whether the
    in-flight encode is terminated now or allowed to finish. See _pause_prep."""
    killed = _pause_prep(req.kill)
    await broadcast("state", state_snapshot())
    return JSONResponse({"ok": True, "paused": True, "killed": killed})


@app.post("/api/offline-prep/resume")
async def offline_prep_resume() -> JSONResponse:
    """Resume bulk stream-prep from the (non-admin) UI — re-spawns paused jobs."""
    n = _resume_prep()
    await broadcast("state", state_snapshot())
    return JSONResponse({"ok": True, "paused": False, "resumed": n})


@app.get("/api/library/offline-job/{job_id}")
async def offline_job_status(job_id: str) -> JSONResponse:
    job = _offline_jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found.")
    out: dict = {
        "status":    job["status"],
        "operation": job["operation"],
        "progress":  round(job["progress"], 3),
        "error":     job["error"],
    }
    if job["status"] == "done":
        out_dir = Path(job["out"])
        key = out_dir.name
        meta = _read_meta(out_dir)
        out["master_url"]        = f"/api/library/offline-cache/{key}/master.m3u8"
        out["duration_sec"]      = meta.get("duration_sec", 0)
        out["videos"]            = meta.get("videos", [])
        out["audios"]            = meta.get("audios", [])
        out["subtitles"]         = meta.get("subtitles", [])
        out["skipped_image_subs"] = meta.get("skipped_image_subs", [])
        out["bundle_size_bytes"] = await asyncio.to_thread(_dir_size_bytes, out_dir)
        # Include on-disk sidecars (incl. any generated `.ai.srt`) so the local
        # player can attach them as <track>s without a separate prep round-trip.
        src = Path(job.get("src", ""))
        out["subs"] = await asyncio.to_thread(_list_sidecar_subs, src, job.get("item_id", ""))
    return JSONResponse(out)


class GenerateSubsReq(BaseModel):
    file_path: str
    translate: bool = True


@app.post("/api/library/{item_id}/generate-subtitles")
async def generate_subtitles(item_id: str, req: GenerateSubsReq) -> JSONResponse:
    """On-demand STT for a library file (on-device context). Writes sidecar
    `.srt`(s) next to the source; poll /api/stt-job/{job_id} until done, then the
    sidecars appear in the file's `subs` list. Runs even while bulk prep is
    paused (interactive)."""
    if not _stt_available():
        raise HTTPException(503, "Subtitle generation isn’t available — whisper.cpp "
                                 "or its model isn’t installed. Re-run setup.py.")
    lib = await get_library()
    item = next((it for it in lib["items"] if it["id"] == item_id), None)
    if not item:
        raise HTTPException(404, "Item not found.")
    target = next((f for f in item.get("files", []) if f["path"] == req.file_path), None)
    if not target:
        raise HTTPException(404, "File not found in this item.")
    src = Path(target["path"])
    if not src.exists():
        raise HTTPException(404, "File not on disk.")
    st = _maybe_start_stt_job(src, item_id, translate=req.translate, queue="interactive")
    return JSONResponse(st)


class VlcGenerateSubsReq(BaseModel):
    translate: bool = True


@app.post("/api/subtitles/generate")
async def generate_subtitles_vlc(req: VlcGenerateSubsReq) -> JSONResponse:
    """On-demand STT for the file VLC is currently playing. On completion the
    sidecar is loaded into VLC and selected (like the OpenSubtitles download
    flow). Poll /api/stt-job/{job_id} for progress."""
    if not _stt_available():
        raise HTTPException(503, "Subtitle generation isn’t available — whisper.cpp "
                                 "or its model isn’t installed. Re-run setup.py.")
    video = await _current_playback_path()
    if not video:
        raise HTTPException(409, "No file is currently playing.")
    st = _maybe_start_stt_job(video, state.library_item_id or "",
                              translate=req.translate, queue="interactive",
                              vlc_attach=True)
    return JSONResponse(st)


@app.get("/api/stt-job/{job_id}")
async def stt_job_status(job_id: str) -> JSONResponse:
    """Status of an STT subtitle-generation job. On `done`, `subs` carries the
    file's sidecar list (incl. the generated tracks) for the on-device player."""
    job = _stt_jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found.")
    out: dict = {
        "status":   job["status"],
        "progress": round(job["progress"], 3),
        "error":    job["error"],
        "tracks":   job.get("tracks", []),
    }
    if job["status"] == "done":
        out["subs"] = await asyncio.to_thread(
            _list_sidecar_subs, Path(job["src"]), job.get("item_id", ""))
    return JSONResponse(out)


# Cache keys are sha256[:24] hex (24 chars). Bundle filenames are
# manifest / segment / meta files — alnum, dot, underscore, dash only.
_CACHE_KEY_RE = re.compile(r"^[a-f0-9]{24}$")
_BUNDLE_FILE_RE = re.compile(r"^[A-Za-z0-9._-]+$")

_HLS_MIME = {
    ".m3u8": "application/vnd.apple.mpegurl",
    ".m4s":  "video/iso.segment",
    ".mp4":  "video/mp4",
    ".vtt":  "text/vtt",
    ".json": "application/json",
}


@app.get("/api/library/offline-cache/{cache_key}/{filename}")
async def offline_cache_bundle_file(cache_key: str, filename: str) -> FileResponse:
    """Serve one file from an HLS bundle directory.

    Strict regex validation on both segments prevents path traversal: even
    though FastAPI doesn't pass slashes through `{filename}` by default, ".."
    or absolute filenames would still resolve outside the cache root via
    Path arithmetic without the guard.
    """
    if not _CACHE_KEY_RE.match(cache_key) or not _BUNDLE_FILE_RE.match(filename):
        raise HTTPException(400, "Invalid path.")
    p = OFFLINE_CACHE / cache_key / filename
    if not p.exists() or not p.is_file():
        raise HTTPException(404, "Cached file not found.")
    media = _HLS_MIME.get(p.suffix.lower(), "application/octet-stream")
    return FileResponse(str(p), media_type=media, filename=p.name)


# ── On-demand (just-in-time) HLS streaming ──────────────────────────────────
#
# The full-prep bundle above encodes an ENTIRE file before playback can start.
# On-demand is the opposite trade-off: playback begins almost immediately and
# segments are transcoded just-in-time as the player requests them — the
# Jellyfin/Plex model. It's the fallback when a file isn't fully prepped yet.
#
# How it works:
#   1. POST /stream-ondemand registers a "session" (one ffmpeg per source+audio)
#      and returns a master playlist URL. ffmpeg is NOT started yet (lazy).
#   2. The player loads master.m3u8 → media.m3u8. media.m3u8 is VIRTUAL: it's
#      computed from the source duration alone (no encoding), listing every
#      6-second segment as if it already existed.
#   3. When the player fetches seg_<n>.ts, the segment endpoint ensures an ffmpeg
#      is running that covers segment n (starting one seeked to n*6 if the
#      current encode can't reach it — i.e. the user seeked), then HOLDS the HTTP
#      response open until that .ts lands on disk. The browser shows its native
#      buffering spinner the whole time — that IS the "loading/waiting" UX.
#
# Key technical choices (see docs/STREAMING.md § On-Demand and docs/GOTCHAS.md):
#   • mpegts segments (NOT fmp4): self-contained, no shared EXT-X-MAP init, so
#     independently-seeked encodes never produce mismatched init segments.
#   • video ALWAYS transcodes with forced keyframes every OD_SEGMENT_SECS
#     (`-force_key_frames expr:gte(t,n_forced*6)`). With `-ss` BEFORE `-i`
#     (fast keyframe seek, output PTS reset to 0) this guarantees segment N
#     covers exactly [N*6,(N+1)*6) — which is what makes the virtual playlist's
#     timing correct and seeking land precisely. Stream-copy can't guarantee
#     that boundary alignment, so it's not used here (it stays a full-prep win).
#   • single source-resolution rendition + one (switchable) audio track. The ABR
#     ladder, seamless quality menu and seamless multi-audio stay full-prep-only.

ONDEMAND_CACHE = Path(__file__).parent / ".ondemand_cache"
OD_SEGMENT_SECS = HLS_SEGMENT_SECS    # MUST match for segment-index ↔ time math
OD_SESSION_IDLE_SECS = 90             # reap a session this long without a fetch
OD_SEG_WAIT_TIMEOUT  = 30             # max seconds to hold a segment request open
OD_LOOKAHEAD_SEGS    = 12             # running encode may be this far behind a
                                      # requested seg before we restart vs. wait
OD_MAX_SESSIONS      = 4              # reap the least-recently-used beyond this
_OD_SEG_RE = re.compile(r"^seg_(\d+)\.ts$")

# session_key → {src, dir, duration, audio_idx, has_audio, start_seg, proc,
#                last_access, lock}
_od_sessions: dict[str, dict] = {}


def _od_session_key(src: Path, audio_idx: int) -> str:
    """Stable 24-hex key per (source bundle key + audio track). A different audio
    selection is a different encode, hence a different session/dir."""
    raw = f"{_offline_cache_key(src)}:{audio_idx}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def _od_seg_path(session: dict, n: int) -> Path:
    return session["dir"] / f"seg_{n}.ts"


def _od_max_seg_on_disk(session: dict) -> int:
    """Highest segment index currently written, or start_seg-1 if none yet."""
    mx = session["start_seg"] - 1
    try:
        for f in session["dir"].iterdir():
            m = _OD_SEG_RE.match(f.name)
            if m:
                mx = max(mx, int(m.group(1)))
    except (FileNotFoundError, OSError):
        pass
    return mx


def _od_wipe_segments(d: Path) -> None:
    """Drop a session dir's segments + internal playlist (sync; tiny tree). Called
    on every (re)start so a previous seek's stale far-ahead segments can't fool
    the 'is the encode ahead of n' check."""
    try:
        for f in d.iterdir():
            if _OD_SEG_RE.match(f.name) or f.name == "internal.m3u8":
                try:
                    f.unlink()
                except OSError:
                    pass
    except (FileNotFoundError, OSError):
        pass


def _od_build_ffmpeg_args(
    ffmpeg: str, src: Path, audio_idx: int, has_audio: bool,
    start_seg: int, use_nvenc: bool,
) -> list[str]:
    """JIT ffmpeg command: transcode from segment `start_seg` forward, emitting
    mpegts HLS segments named seg_<start_seg>.ts, seg_<start_seg+1>.ts, … . All
    outputs are bare names — the caller runs ffmpeg with cwd=<session dir> (same
    Windows-safe rule as the bundle path)."""
    start_sec = start_seg * OD_SEGMENT_SECS
    args = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error"]
    if use_nvenc:
        # Transparent NVDEC tier (no -hwaccel_output_format): NVDEC decodes when
        # it can and silently falls back to software for codecs it can't — so it
        # never hard-fails, mirroring the full-prep transparent path.
        args += ["-hwaccel", "cuda"]
    # `-ss` BEFORE `-i` is a fast keyframe seek that resets output PTS to 0, so
    # `-force_key_frames` (which reads output time t) places keyframes at exact
    # 0/6/12… boundaries → segment N == [N*6,(N+1)*6).
    args += ["-ss", str(start_sec), "-i", str(src), "-map", "0:v:0"]
    if has_audio:
        args += ["-map", f"0:a:{audio_idx}"]
    if use_nvenc:
        args += ["-c:v", "h264_nvenc", "-preset", "medium", "-rc", "vbr", "-cq", "23"]
    else:
        args += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                 "-threads", str(OFFLINE_FFMPEG_THREADS)]
    args += [
        "-pix_fmt", "yuv420p", "-profile:v", "high", "-level", "4.1",
        "-force_key_frames", f"expr:gte(t,n_forced*{OD_SEGMENT_SECS})",
    ]
    if has_audio:
        args += ["-c:a", "aac", "-b:a", "160k", "-ac", "2"]
    args += [
        # Shift the MUXED timestamps to the absolute source position. `-ss` reset
        # output PTS to 0 (so force_key_frames' `t` is correctly 0-based and
        # boundaries land at 0/6/12…), but a player seeking across a session
        # restart needs each segment's timestamps to match its playlist position.
        # output_ts_offset is applied after encoding, so it shifts PTS to
        # [start_sec, …] WITHOUT disturbing the keyframe spacing — segments from
        # any seek-restarted session then share one consistent absolute timeline
        # (no EXT-X-DISCONTINUITY needed; works on hls.js AND Safari native).
        "-output_ts_offset", str(start_sec),
        "-f", "hls",
        "-hls_time", str(OD_SEGMENT_SECS),
        "-hls_segment_type", "mpegts",
        "-hls_flags", "independent_segments",
        "-start_number", str(start_seg),
        "-hls_segment_filename", "seg_%d.ts",   # bare name → lands in cwd (session dir)
        "-hls_list_size", "0",
        "internal.m3u8",   # ffmpeg's own playlist; ignored (we serve a virtual one)
    ]
    return args


async def _od_drain_stderr(proc, tail: deque) -> None:
    """Drain ffmpeg stderr so the OS pipe can't fill (which would wedge the
    encode), keeping the last lines for failure logging."""
    if proc.stderr is None:
        return
    while True:
        try:
            line = await proc.stderr.readline()
        except Exception:
            return
        if not line:
            return
        tail.append(line.decode("utf-8", "replace").rstrip())


async def _od_start_encode(session: dict, start_seg: int) -> None:
    """(Re)start the session's ffmpeg seeked to `start_seg`. Caller holds
    session['lock']. Terminates any prior encode and wipes stale segments first."""
    start_seg = max(0, start_seg)
    old = session.get("proc")
    if old is not None and old.returncode is None:
        try:
            old.terminate()
        except Exception:
            pass
    await asyncio.to_thread(_od_wipe_segments, session["dir"])
    session["dir"].mkdir(parents=True, exist_ok=True)
    session["start_seg"] = start_seg
    ffmpeg = analyzer.ffmpeg_bin()
    if not ffmpeg:
        raise HTTPException(503, "ffmpeg is not available.")
    use_nvenc = await _has_nvenc()
    args = _od_build_ffmpeg_args(
        ffmpeg, Path(session["src"]), session["audio_idx"],
        session["has_audio"], start_seg, use_nvenc,
    )
    cmd = _ffmpeg_nice_prefix() + args
    hls_log.info("ondemand %s START seg=%d nvenc=%s cmd: %s",
                 session["key"], start_seg, use_nvenc,
                 " ".join(shlex.quote(a) for a in cmd))
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=str(session["dir"]),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
        **_FFMPEG_SUBPROCESS_KW,
    )
    session["proc"] = proc
    session["last_access"] = time.time()
    tail: deque = deque(maxlen=40)

    async def _watch() -> None:
        await _od_drain_stderr(proc, tail)
        await proc.wait()
        if proc.returncode not in (0, None) and session.get("proc") is proc:
            # Non-zero AND still the live proc (i.e. not a deliberate restart):
            # log the tail so a broken source surfaces in logs/hls.log.
            hls_log.warning("ondemand %s ffmpeg rc=%s\n  %s",
                            session["key"], proc.returncode,
                            "\n  ".join(tail) or "(no stderr)")

    asyncio.create_task(_watch())


async def _od_teardown(session_key: str) -> None:
    """Terminate a session's ffmpeg and delete its segment dir (best-effort)."""
    session = _od_sessions.pop(session_key, None)
    if not session:
        return
    proc = session.get("proc")
    if proc is not None and proc.returncode is None:
        try:
            proc.terminate()
        except Exception:
            pass
    await asyncio.to_thread(shutil.rmtree, session["dir"], ignore_errors=True)
    hls_log.info("ondemand %s torn down", session_key)


async def _od_reaper() -> None:
    """Background loop: reap idle sessions and cap the live count. Idle is long
    (90 s) and active playback refreshes last_access on every segment fetch, so a
    watching session is never reaped mid-stream."""
    while True:
        await asyncio.sleep(30)
        try:
            now = time.time()
            for key, s in list(_od_sessions.items()):
                if now - s.get("last_access", 0) > OD_SESSION_IDLE_SECS:
                    await _od_teardown(key)
            if len(_od_sessions) > OD_MAX_SESSIONS:
                victims = sorted(_od_sessions.items(),
                                 key=lambda kv: kv[1].get("last_access", 0))
                for key, _s in victims[:len(_od_sessions) - OD_MAX_SESSIONS]:
                    await _od_teardown(key)
        except Exception:
            hls_log.exception("ondemand reaper tick failed")


class OnDemandReq(BaseModel):
    file_path: str
    profile_id: str = ""
    audio_idx: Optional[int] = None   # source audio stream index; None ⇒ default


@app.post("/api/library/{item_id}/stream-ondemand")
async def stream_ondemand(item_id: str, req: OnDemandReq) -> JSONResponse:
    """Begin (or re-attach to) a just-in-time HLS session for a file and return a
    master playlist URL the player can load immediately. Also kicks off the normal
    full background prep so the NEXT play gets the rich ABR/multi-audio bundle."""
    if not HLS_AVAILABLE:
        raise HTTPException(503, HLS_UNAVAILABLE_MSG)
    lib = await get_library()
    item = next((it for it in lib["items"] if it["id"] == item_id), None)
    if not item:
        raise HTTPException(404, "Item not found.")
    target = next((f for f in item.get("files", []) if f["path"] == req.file_path), None)
    if not target:
        raise HTTPException(404, "File not found in this item.")
    src = Path(target["path"])
    if not src.exists():
        raise HTTPException(404, "File not on disk.")

    info = await asyncio.to_thread(_ffprobe_full, str(src))
    duration = float(info.get("duration_sec", 0) or 0)
    if not info.get("video"):
        raise HTTPException(422, "No video stream in source.")
    if duration <= 0:
        raise HTTPException(422, "Could not determine source duration.")

    src_audios = info.get("audios") or []
    audios_meta = [
        {
            "idx":      a["idx"],
            "label":    _track_label(a, f"Audio {a['idx']+1}"),
            "language": a.get("language") or "und",
            "title":    a.get("title") or "",
            "default":  (a["idx"] == 0) if all(not x.get("default") for x in src_audios)
                        else bool(a.get("default")),
        }
        for a in src_audios
    ]
    saved = (_saved_local_tracks(lib, item, req.profile_id, req.file_path)
             if req.profile_id else {})

    valid_idxs = {a["idx"] for a in src_audios}
    if req.audio_idx is not None and req.audio_idx in valid_idxs:
        aidx = req.audio_idx
    elif isinstance(saved.get("audio_idx"), int) and saved["audio_idx"] in valid_idxs:
        aidx = saved["audio_idx"]
    elif src_audios:
        aidx = next((a["idx"] for a in src_audios if a.get("default")), src_audios[0]["idx"])
    else:
        aidx = -1   # no audio in source

    key = _od_session_key(src, aidx)
    session = _od_sessions.get(key)
    if session is None:
        ONDEMAND_CACHE.mkdir(exist_ok=True)
        session = {
            "key":         key,
            "src":         str(src),
            "dir":         ONDEMAND_CACHE / key,
            "duration":    duration,
            "audio_idx":   aidx,
            "has_audio":   aidx >= 0,
            "start_seg":   0,
            "proc":        None,
            "last_access": time.time(),
            "lock":        asyncio.Lock(),
        }
        session["dir"].mkdir(parents=True, exist_ok=True)
        _od_sessions[key] = session
        hls_log.info("ondemand %s session created src=%s audio=%d dur=%.1fs",
                     key, src.name, aidx, duration)
    else:
        session["last_access"] = time.time()

    # Kick off the full bundle prep in the background (bulk queue — low priority,
    # honors the global pause) so a SUBSEQUENT play uses the rich multi-audio/ABR
    # bundle instead of JIT. _maybe_start_prep_job only registers the job + spawns
    # its own task (no long await), so this returns immediately; best-effort.
    try:
        await _maybe_start_prep_job(src, item_id)
    except Exception as exc:
        hls_log.warning("ondemand %s: background full-prep enqueue skipped: %s", key, exc)

    sidecar_subs = await asyncio.to_thread(_list_sidecar_subs, src, item_id)
    return JSONResponse({
        "ready":             True,
        "mode":              "ondemand",
        "master_url":        f"/api/library/ondemand/{key}/master.m3u8",
        "duration_sec":      duration,
        "audios":            audios_meta,
        "subtitles":         [],
        "subs":              sidecar_subs,
        "saved_tracks":      saved,
        "default_audio_idx": aidx,
    })


def _od_media_playlist(duration: float) -> str:
    """Build the VIRTUAL VOD media playlist from duration alone — no encoding.
    Lists every OD_SEGMENT_SECS segment as if it already existed; the segment
    endpoint conjures each .ts on demand when the player fetches it."""
    n = int(duration // OD_SEGMENT_SECS)
    if duration - n * OD_SEGMENT_SECS > 0.001:
        n += 1
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        f"#EXT-X-TARGETDURATION:{OD_SEGMENT_SECS + 1}",
        "#EXT-X-MEDIA-SEQUENCE:0",
        "#EXT-X-PLAYLIST-TYPE:VOD",
    ]
    for i in range(n):
        seglen = duration - i * OD_SEGMENT_SECS if i == n - 1 else OD_SEGMENT_SECS
        if seglen <= 0:
            seglen = OD_SEGMENT_SECS
        lines.append(f"#EXTINF:{seglen:.3f},")
        lines.append(f"seg_{i}.ts")
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


@app.get("/api/library/ondemand/{session_key}/{filename}")
async def ondemand_file(session_key: str, filename: str):
    """Serve a JIT session's master/media playlist or a segment (transcoding it
    on demand). See the module comment above for the full flow."""
    if not _CACHE_KEY_RE.match(session_key):
        raise HTTPException(400, "Invalid session.")
    session = _od_sessions.get(session_key)
    if session is None:
        # Reaped or never created — the client should re-POST /stream-ondemand.
        raise HTTPException(410, "Streaming session expired.")
    session["last_access"] = time.time()

    if filename == "master.m3u8":
        master = ("#EXTM3U\n"
                  "#EXT-X-STREAM-INF:BANDWIDTH=4000000\n"
                  "media.m3u8\n")
        return Response(content=master, media_type="application/vnd.apple.mpegurl")

    if filename == "media.m3u8":
        return Response(content=_od_media_playlist(session["duration"]),
                        media_type="application/vnd.apple.mpegurl")

    m = _OD_SEG_RE.match(filename)
    if not m:
        raise HTTPException(400, "Invalid path.")
    n = int(m.group(1))
    seg_path = _od_seg_path(session, n)

    # Already produced → serve straight away.
    if seg_path.exists():
        return FileResponse(str(seg_path), media_type="video/mp2t")

    # Decide (under the session lock) whether the running encode will reach n soon
    # or whether we must (re)start seeked to n. The lock is held only for this
    # decision + any restart — NOT for the wait below — so concurrent segment
    # requests during a seek don't each spawn an ffmpeg.
    async with session["lock"]:
        if not seg_path.exists():   # re-check inside the lock
            proc = session.get("proc")
            alive = proc is not None and proc.returncode is None
            covered = alive and session["start_seg"] <= n
            near = (n - _od_max_seg_on_disk(session)) <= OD_LOOKAHEAD_SEGS
            if not (covered and near):
                # n is before the current encode, or too far ahead of it (a seek)
                # — start a fresh encode at n.
                await _od_start_encode(session, n)

    # Hold the response open until the segment lands — the browser's buffering
    # spinner is the user-visible "generating that part" state.
    deadline = time.time() + OD_SEG_WAIT_TIMEOUT
    while time.time() < deadline:
        if seg_path.exists():
            session["last_access"] = time.time()
            return FileResponse(str(seg_path), media_type="video/mp2t")
        proc = session.get("proc")
        # Encode ended without producing n (crash / unexpected EOF) — stop waiting.
        if proc is not None and proc.returncode not in (None, 0) \
                and _od_max_seg_on_disk(session) < n:
            break
        await asyncio.sleep(0.15)
    raise HTTPException(504, "Segment generation timed out.")


@app.post("/api/library/ondemand/{session_key}/close")
async def ondemand_close(session_key: str) -> JSONResponse:
    """Best-effort teardown when the player stops (sendBeacon on unload). The
    reaper is the backstop if this never arrives."""
    if not _CACHE_KEY_RE.match(session_key):
        raise HTTPException(400, "Invalid session.")
    await _od_teardown(session_key)
    return JSONResponse({"ok": True})


# ── Clip: save & share the last N seconds of whatever is playing ────────────
#
# A "clip" is a short, standalone MP4 (H.264 + AAC, +faststart) cut from the end
# of the current playback position — pressed from the fullscreen VLC controls
# ("On TV") or the on-device player. It's re-encoded to a universally-compatible
# container so it can be AirDropped / shared straight off the phone. Clips are
# ephemeral: written under `.clips/<token>/` and purged after CLIP_TTL_SECS.
#
# We require the source to already be HLS-prepped (the bundle exists) before
# clipping — that guarantees the file is on disk and probed, and matches the
# product decision that clipping is a prepped-only feature. The clip itself is
# cut from the ORIGINAL source (not the HLS segments) for best quality + precise
# track mapping.
CLIPS_CACHE = Path(__file__).parent / ".clips"
CLIP_TTL_SECS = 2 * 3600           # generated clips are purged after 2 hours
CLIP_MAX_SECONDS = 300             # cap "last X seconds" so a clip can't balloon into a huge re-encode
CLIP_ENCODE_TIMEOUT = 240          # kill a wedged clip encode after this long
_CLIP_TOKEN_RE = re.compile(r"^[a-f0-9]{16}$")
_CLIP_FILE_RE = re.compile(r"^[A-Za-z0-9._-]+\.mp4$")


def _safe_clip_stem(name: str) -> str:
    """Filesystem/URL-safe stem (regex-validated charset, capped length)."""
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(name or "").stem).strip("._-")
    return (stem or "clip")[:60]


def _purge_old_clips() -> None:
    """Drop clip dirs older than CLIP_TTL_SECS (best-effort; sync, tiny tree)."""
    try:
        cutoff = time.time() - CLIP_TTL_SECS
        for d in CLIPS_CACHE.iterdir():
            try:
                if d.is_dir() and d.stat().st_mtime < cutoff:
                    shutil.rmtree(d, ignore_errors=True)
            except Exception:
                pass
    except FileNotFoundError:
        pass
    except Exception:
        pass


async def _build_clip(src: Path, start: float, dur: float, audio_idx: int,
                      out_path: Path) -> tuple[bool, str]:
    """Re-encode `[start, start+dur]` of `src` to a shareable MP4 at `out_path`.

    `-ss` before `-i` keyframe-seeks for speed; the re-encode then lands on the
    exact start. Maps the first video + the requested audio stream (optional, so
    a bad index doesn't fail the encode). NVENC when available, else libx264.
    Returns (ok, error_message).
    """
    ffmpeg = analyzer.ffmpeg_bin()
    if not ffmpeg:
        return False, "ffmpeg is not available."
    use_nvenc = await _has_nvenc()
    args = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-ss", f"{max(0.0, start):.3f}", "-i", str(src), "-t", f"{dur:.3f}",
        "-map", "0:v:0", "-map", f"0:a:{max(0, audio_idx)}?",
    ]
    if use_nvenc:
        args += ["-c:v", "h264_nvenc", "-preset", "medium", "-rc", "vbr", "-cq", "23"]
    else:
        args += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                 "-threads", str(OFFLINE_FFMPEG_THREADS)]
    args += [
        "-pix_fmt", "yuv420p", "-profile:v", "high", "-level", "4.1",
        "-c:a", "aac", "-b:a", "160k", "-ac", "2",
        "-movflags", "+faststart", str(out_path),
    ]
    cmd = _ffmpeg_nice_prefix() + args
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            **_FFMPEG_SUBPROCESS_KW,
        )
        try:
            _, err = await asyncio.wait_for(proc.communicate(), timeout=CLIP_ENCODE_TIMEOUT)
        except asyncio.TimeoutError:
            try:
                proc.terminate()
            except Exception:
                pass
            return False, "Clip encode timed out."
    except Exception as e:
        return False, f"Failed to start clip encode: {e}"
    if proc.returncode != 0 or not out_path.exists():
        tail = (err or b"").decode("utf-8", "replace").strip().splitlines()[-1:]
        hls_log.warning("clip encode failed rc=%s src=%s: %s",
                        proc.returncode, src, "; ".join(tail) or "no detail")
        return False, "; ".join(tail) or "Clip encode failed."
    return True, ""


class ClipReq(BaseModel):
    file_path: str
    end_sec: float                  # clip ENDS here (current playback position)
    duration_sec: float = 30.0      # length to grab back from end_sec
    audio_idx: int = 0              # source audio stream index (on-device passes its rendition idx)


@app.post("/api/library/{item_id}/clip")
async def make_clip(item_id: str, req: ClipReq) -> JSONResponse:
    """Cut & re-encode a shareable MP4 of the last `duration_sec` seconds.

    Ends at `end_sec` (the live playback position). Requires the file to be
    HLS-prepped first (bundle on disk). Returns {ok, url, filename, duration_sec};
    the browser then downloads / shares the URL. 503 on macOS (no HLS), 409 when
    the file isn't prepped yet.
    """
    if not HLS_AVAILABLE:
        raise HTTPException(503, HLS_UNAVAILABLE_MSG)
    lib = await get_library()
    item = next((it for it in lib["items"] if it["id"] == item_id), None)
    if not item:
        raise HTTPException(404, "Item not found.")
    target = next((f for f in item.get("files", []) if f["path"] == req.file_path), None)
    if not target:
        raise HTTPException(404, "File not found in this item.")
    src = Path(target["path"])
    if not src.exists():
        raise HTTPException(404, "File not on disk.")

    # Prepped-only: require the HLS bundle to exist (matches the product gate and
    # guarantees the source is readable + probed).
    if not (OFFLINE_CACHE / _offline_cache_key(src) / "master.m3u8").exists():
        raise HTTPException(409, "Prep this episode for streaming first, then you can clip it.")

    dur = max(1.0, min(float(CLIP_MAX_SECONDS), float(req.duration_sec or 30.0)))
    end = max(0.0, float(req.end_sec or 0.0))
    start = max(0.0, end - dur)
    real_dur = end - start
    if real_dur < 0.5:
        raise HTTPException(400, "Play a little further in before clipping.")

    _purge_old_clips()
    token = secrets.token_hex(8)
    out_dir = CLIPS_CACHE / token
    out_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{_safe_clip_stem(target.get('name') or src.name)}-clip-{int(round(real_dur))}s.mp4"
    out_path = out_dir / filename

    ok, err = await _build_clip(src, start, real_dur, int(req.audio_idx or 0), out_path)
    if not ok:
        shutil.rmtree(out_dir, ignore_errors=True)
        raise HTTPException(500, err or "Couldn't create the clip.")
    return JSONResponse({
        "ok": True,
        "url": f"/api/library/clip/{token}/{filename}",
        "filename": filename,
        "duration_sec": round(real_dur, 1),
    })


@app.get("/api/library/clip/{token}/{filename}")
async def serve_clip(token: str, filename: str) -> FileResponse:
    """Serve a generated clip as a downloadable MP4 attachment."""
    if not _CLIP_TOKEN_RE.match(token) or not _CLIP_FILE_RE.match(filename):
        raise HTTPException(400, "Invalid path.")
    p = CLIPS_CACHE / token / filename
    if not p.exists() or not p.is_file():
        raise HTTPException(404, "Clip not found (it may have expired).")
    return FileResponse(str(p), media_type="video/mp4", filename=p.name)


# ── Admin: offline-cache inventory + cleanup ────────────────────────────────
#
# `.offline_cache/<cache_key>.mp4` accumulates indefinitely — there's no
# automatic eviction, and re-encoding a source (mtime/size change) leaves the
# old entry on disk as an orphan. These endpoints power an admin tab that
# shows what's there, how much space it costs, and lets the operator delete
# per-file, per-item, or every orphan in one shot.

# Cached inventory snapshot. The FS walk below is O(total segments) and on a
# large ABR cache takes long enough that re-walking on every admin tab open felt
# broken. We keep the last result + when it was built; reads serve the snapshot
# instantly and the admin "Refresh" button forces a fresh walk (`force=True`).
# Mutations (deletes) invalidate it via `_invalidate_offline_cache_inventory`.
_offline_cache_inv_snapshot: "dict | None" = None
_offline_cache_inv_lock = asyncio.Lock()


def _invalidate_offline_cache_inventory() -> None:
    """Drop the cached inventory so the next read rebuilds from disk. Called
    after any delete so a stale snapshot can't show purged bundles."""
    global _offline_cache_inv_snapshot
    _offline_cache_inv_snapshot = None


async def _build_offline_cache_inventory(*, force: bool = False) -> dict:
    """Return the offline-cache inventory, serving a cached snapshot unless
    `force` (or no snapshot yet). Adds `generated_at` (epoch secs) so the UI can
    show how old the data is.

    On a miss it fetches the library + snapshots the jobs, then runs the (heavy,
    blocking) filesystem walk in a worker thread.

    The walk sums every file in every HLS bundle via `_dir_size_bytes` (recursive
    `rglob` + `stat`). Since the ABR ladder tripled the segment count per bundle,
    a sizeable `.offline_cache/` makes that walk take long enough that doing it
    inline on the event loop freezes the WHOLE server — SSE, VLC polling, and
    every other request stall until it looks crashed and the service restarts.
    Offloading keeps the loop free (same discipline as `_run_offline_job`). The
    job list is snapshotted here so the worker thread never iterates the live
    `_offline_jobs` dict while the loop mutates it.

    The lock serialises concurrent builds so two near-simultaneous opens (or an
    open racing the auto-purge loop) don't both walk the whole tree.
    """
    global _offline_cache_inv_snapshot
    if not force and _offline_cache_inv_snapshot is not None:
        return _offline_cache_inv_snapshot
    async with _offline_cache_inv_lock:
        # Re-check inside the lock: a concurrent caller may have just built it.
        if not force and _offline_cache_inv_snapshot is not None:
            return _offline_cache_inv_snapshot
        lib = await get_library()
        jobs = list(_offline_jobs.values())
        data = await asyncio.to_thread(_offline_cache_inventory_sync, lib, jobs)
        data["generated_at"] = time.time()
        _offline_cache_inv_snapshot = data
        return data


def _offline_cache_inventory_sync(lib: dict, jobs: list[dict]) -> dict:
    """Walk OFFLINE_CACHE + the library + the job snapshot and return the admin payload.

    Each per-file entry has a `status` of:
      cached         — `<sha>/master.m3u8` exists, ready for HLS playback
      processing     — ffmpeg is actively encoding now (progress + ETA included)
      pending        — queued behind the OFFLINE_JOB_CONCURRENCY semaphore
      error          — the most recent prep job failed; `error` carries the message
      partial_stale  — a `<sha>.part/` directory exists with no live job
                       (process crashed mid-encode, or the job entry was evicted)
    Only entries with one of those states are included; "no work has ever started
    for this file" is just absent.
    """
    if not OFFLINE_CACHE.exists():
        return {"total_bytes": 0, "cache_dir": str(OFFLINE_CACHE),
                "items": [], "orphans": []}
    # 1) Inventory the cache directory: distinguish completed bundles (`<sha>/`)
    #    from in-flight staging dirs (`<sha>.part/`). Both count toward total
    #    bytes. We also include any leftover top-level `.mp4` files from
    #    pre-v3-hls caches in the "orphan" pool so the operator can clear them.
    PART_SUFFIX = ".part"
    cached_dirs:  dict[str, dict] = {}   # cache_key → {bytes, mtime}
    partial_dirs: dict[str, dict] = {}   # cache_key → {bytes, mtime}
    legacy_files: list[dict] = []        # pre-v3-hls leftovers (.mp4 / .part.mp4)
    total_bytes = 0
    for p in OFFLINE_CACHE.iterdir():
        try:
            st = p.stat()
        except OSError:
            continue
        if p.is_dir() and _CACHE_KEY_RE.match(p.name):
            sz = _dir_size_bytes(p)
            cached_dirs[p.name] = {"bytes": sz, "mtime": st.st_mtime}
            total_bytes += sz
        elif p.is_dir() and p.name.endswith(PART_SUFFIX):
            key = p.name[: -len(PART_SUFFIX)]
            if _CACHE_KEY_RE.match(key):
                sz = _dir_size_bytes(p)
                partial_dirs[key] = {"bytes": sz, "mtime": st.st_mtime}
                total_bytes += sz
        elif p.is_file() and p.suffix == ".mp4":
            # Pre-v3-hls single-MP4 cache. Always orphan now — surface it so
            # the operator can purge with one click.
            legacy_files.append({
                "cache_key": p.stem.removesuffix(".part"),
                "name":      p.name,
                "kind":      "legacy",
                "bytes":     st.st_size,
                "mtime":     st.st_mtime,
            })
            total_bytes += st.st_size
    # 2) Index jobs by their output cache key. Keep only the most recent job per
    #    key so a series of failed retries collapses to one row.
    jobs_by_key: dict[str, dict] = {}
    for j in jobs:
        try:
            key = Path(j.get("out", "")).name
        except Exception:
            continue
        if not key:
            continue
        prev = jobs_by_key.get(key)
        if prev is None or j.get("started_at", 0) > prev.get("started_at", 0):
            jobs_by_key[key] = j
    # 3) For every library file still on disk, compute the cache key and combine
    #    cache + partial + job state into one entry. Anything not referenced by
    #    a current library file lands in `orphans` below.
    items_out: list[dict] = []
    matched_keys: set[str] = set()
    now = time.time()
    for it in lib.get("items", []):
        files_out: list[dict] = []
        item_bytes = 0
        cached_n = processing_n = pending_n = error_n = partial_n = 0
        for f in it.get("files", []):
            src = Path(f.get("path", ""))
            try:
                if not src.exists():
                    continue
                key = _offline_cache_key(src)
            except OSError:
                continue
            cached  = cached_dirs.get(key)
            partial = partial_dirs.get(key)
            job     = jobs_by_key.get(key)
            entry: dict = {
                "file_path": f.get("path", ""),
                "name":      f.get("name", "") or src.name,
                "cache_key": key,
                "bytes":     0,
            }
            if cached:
                entry["status"] = "cached"
                entry["bytes"]  = cached["bytes"]
                entry["mtime"]  = cached["mtime"]
                item_bytes += cached["bytes"]
                cached_n   += 1
                matched_keys.add(key)
            elif job and job["status"] in ("pending", "processing"):
                entry["status"]     = job["status"]
                entry["progress"]   = float(job.get("progress", 0) or 0)
                entry["operation"]  = job.get("operation", "hls")
                entry["encoder"]    = job.get("encoder", "")
                entry["job_id"]     = job.get("id", "")
                entry["started_at"] = job.get("started_at", 0)
                if partial:
                    entry["bytes"] = partial["bytes"]
                    item_bytes += partial["bytes"]
                # Per-file ETA (only meaningful once ffmpeg has been running long
                # enough for the progress reading to stabilize).
                elapsed = max(0.0, now - entry["started_at"])
                if entry["status"] == "processing" and elapsed > 2.0 and entry["progress"] > 0.02:
                    entry["eta_secs"] = elapsed * (1 - entry["progress"]) / entry["progress"]
                if entry["status"] == "processing":
                    processing_n += 1
                else:
                    pending_n    += 1
                matched_keys.add(key)
            elif job and job["status"] == "error":
                entry["status"]     = "error"
                entry["error"]      = job.get("error", "")
                entry["operation"]  = job.get("operation", "hls")
                entry["encoder"]    = job.get("encoder", "")
                entry["job_id"]     = job.get("id", "")
                entry["started_at"] = job.get("started_at", 0)
                if partial:
                    entry["bytes"] = partial["bytes"]
                    item_bytes += partial["bytes"]
                error_n += 1
                matched_keys.add(key)
            elif partial:
                # `<sha>.part/` on disk with no remembered job — almost always
                # a process crash mid-encode. Surface so the operator can clean.
                entry["status"] = "partial_stale"
                entry["bytes"]  = partial["bytes"]
                entry["mtime"]  = partial["mtime"]
                item_bytes += partial["bytes"]
                partial_n  += 1
                matched_keys.add(key)
            else:
                continue
            files_out.append(entry)
        if files_out:
            items_out.append({
                "item_id":          it["id"],
                "title":            it.get("title", ""),
                "file_count":       len(files_out),
                "total_bytes":      item_bytes,
                "cached_count":     cached_n,
                "processing_count": processing_n,
                "pending_count":    pending_n,
                "error_count":      error_n,
                "partial_count":    partial_n,
                "files":            sorted(files_out, key=lambda x: x["name"].lower()),
            })
    items_out.sort(key=lambda x: x["total_bytes"], reverse=True)
    # 4) Orphans: cache/partial dirs whose source no longer maps to any library
    #    file (re-encoded, deleted, or library item removed), plus legacy
    #    single-MP4 files from the pre-HLS cache layout.
    orphans: list[dict] = []
    for k, v in cached_dirs.items():
        if k not in matched_keys:
            orphans.append({"cache_key": k, "kind": "cached",
                            "bytes": v["bytes"], "mtime": v["mtime"]})
    for k, v in partial_dirs.items():
        if k not in matched_keys:
            orphans.append({"cache_key": k, "kind": "partial",
                            "bytes": v["bytes"], "mtime": v["mtime"]})
    orphans.extend(legacy_files)
    orphans.sort(key=lambda x: x["bytes"], reverse=True)
    return {
        "total_bytes": total_bytes,
        "cache_dir":   str(OFFLINE_CACHE),
        "items":       items_out,
        "orphans":     orphans,
    }


def _delete_cache_artifacts(cache_key: str) -> int:
    """Remove `<key>/` and `<key>.part/` bundle directories, plus any legacy
    pre-v3-hls `.mp4` files keyed by `cache_key`, AND any terminal job entries
    targeting this cache key. Returns total bytes freed on disk. The caller is
    responsible for the active-job 409 check.
    """
    bytes_freed = 0
    for name in (cache_key, f"{cache_key}.part"):
        d = OFFLINE_CACHE / name
        if d.exists() and d.is_dir():
            bytes_freed += _dir_size_bytes(d)
            shutil.rmtree(d, ignore_errors=True)
    # Legacy single-MP4 cache from v2 — orphaned by the v3 cache-key rebase,
    # but still on disk until purged.
    for fname in (f"{cache_key}.mp4", f"{cache_key}.part.mp4"):
        p = OFFLINE_CACHE / fname
        try:
            sz = p.stat().st_size
            p.unlink()
            bytes_freed += sz
        except OSError:
            pass
    # Drop terminal job entries for this key so the UI doesn't keep showing the
    # stale error after the operator has cleaned it up.
    for jid, j in list(_offline_jobs.items()):
        try:
            if Path(j.get("out", "")).name == cache_key and j.get("status") in ("done", "error"):
                _offline_jobs.pop(jid, None)
        except Exception:
            pass
    return bytes_freed


def _offline_cache_path_active(cache_key: str) -> bool:
    """True if a pending/processing prep job is currently writing to this key."""
    return any(Path(j.get("out", "")).name == cache_key
               and j.get("status") in ("pending", "processing")
               for j in _offline_jobs.values())


@app.get("/api/admin/offline-cache")
async def admin_offline_cache_list(request: Request) -> JSONResponse:
    """Inventory: totals + per-item breakdown + orphans.

    Serves a cached snapshot for an instant open; `?refresh=1` forces a fresh
    filesystem walk. The payload carries `generated_at` so the UI can show how
    stale the data is.
    """
    _require_admin(request)
    refresh = request.query_params.get("refresh") in ("1", "true", "yes")
    return JSONResponse(await _build_offline_cache_inventory(force=refresh))


# Route order matters — declare /orphans BEFORE /{cache_key} so FastAPI doesn't
# try to parse "orphans" as a 24-hex cache key.
@app.delete("/api/admin/offline-cache/orphans")
async def admin_offline_cache_purge_orphans(request: Request) -> JSONResponse:
    """Delete every cache/partial dir + legacy MP4 that no longer maps to any library file."""
    _require_admin(request)
    inv = await _build_offline_cache_inventory(force=True)
    deleted = 0
    bytes_freed = 0
    for o in inv["orphans"]:
        if _offline_cache_path_active(o["cache_key"]):
            continue
        freed = await asyncio.to_thread(_delete_cache_artifacts, o["cache_key"])
        if freed > 0:
            deleted += 1
            bytes_freed += freed
    _invalidate_offline_cache_inventory()
    return JSONResponse({"deleted_count": deleted, "bytes_freed": bytes_freed})


@app.delete("/api/admin/offline-cache/{cache_key}")
async def admin_offline_cache_delete_one(cache_key: str, request: Request) -> JSONResponse:
    """Delete the HLS bundle dir, any `.part` staging dir, and any terminal
    job entries targeting `cache_key`. 409 if an active job is writing it.
    """
    _require_admin(request)
    if not _CACHE_KEY_RE.match(cache_key):
        raise HTTPException(400, "Invalid cache key.")
    if _offline_cache_path_active(cache_key):
        raise HTTPException(409, "An active prep job is writing this bundle.")
    out_dir   = OFFLINE_CACHE / cache_key
    part_dir  = OFFLINE_CACHE / f"{cache_key}.part"
    legacy_a  = OFFLINE_CACHE / f"{cache_key}.mp4"
    legacy_b  = OFFLINE_CACHE / f"{cache_key}.part.mp4"
    if not (out_dir.exists() or part_dir.exists() or legacy_a.exists() or legacy_b.exists()
            or any(Path(j.get("out", "")).name == cache_key for j in _offline_jobs.values())):
        raise HTTPException(404, "Nothing on disk and no job for that cache key.")
    bytes_freed = await asyncio.to_thread(_delete_cache_artifacts, cache_key)
    _invalidate_offline_cache_inventory()
    return JSONResponse({"deleted": True, "bytes_freed": bytes_freed})


@app.delete("/api/admin/library/{item_id}/offline-cache")
async def admin_offline_cache_delete_for_item(item_id: str, request: Request) -> JSONResponse:
    """Delete every cached/partial bundle + clear every error-state job entry
    for one library item. Active prep jobs are skipped (the operator can stop
    them via the library card if they really want to abandon them).
    """
    _require_admin(request)
    inv = await _build_offline_cache_inventory(force=True)
    item = next((x for x in inv["items"] if x["item_id"] == item_id), None)
    if not item:
        return JSONResponse({"deleted_count": 0, "bytes_freed": 0})
    deleted = 0
    bytes_freed = 0
    for f in item["files"]:
        if _offline_cache_path_active(f["cache_key"]):
            continue
        freed = await asyncio.to_thread(_delete_cache_artifacts, f["cache_key"])
        if freed > 0 or f.get("status") == "error":
            deleted += 1
            bytes_freed += freed
    _invalidate_offline_cache_inventory()
    return JSONResponse({"deleted_count": deleted, "bytes_freed": bytes_freed})


@app.get("/api/library/{item_id}/subtitle")
async def get_subtitle(item_id: str, path: str = "", file: str = "") -> Response:
    """Return a sidecar subtitle as WebVTT, converting SRT/ASS/SSA on demand.

    `path` is the absolute path of a sub found by `_discover_local_subs` (the
    aggressive search — so it may live in a `Subs/` folder). It's validated to
    resolve *inside* the item's media tree (a video file's directory, that
    directory's parent, or a child of either) before anything is read, so it
    can't be used to fetch arbitrary files. `file` (a bare filename next to a
    video) is still accepted for backward compatibility with old links.
    """
    lib = await get_library()
    item = next((it for it in lib["items"] if it["id"] == item_id), None)
    if not item:
        raise HTTPException(404, "Item not found.")

    # Allowed roots: each video file's directory + that directory's parent
    # (covers a shared `Subs/` one level up). A requested sub must resolve to,
    # or under, one of these.
    roots: set[Path] = set()
    for vf in item.get("files", []):
        try:
            d = Path(vf.get("path", "")).resolve().parent
        except OSError:
            continue
        roots.add(d)
        roots.add(d.parent)

    target: Optional[Path] = None
    if path:
        try:
            cand = Path(path).resolve()
        except OSError:
            cand = None
        if (cand and cand.is_file() and cand.suffix.lower() in _SUB_FILE_EXTS
                and any(cand == r or r in cand.parents for r in roots)):
            target = cand
    elif file:
        if "/" in file or "\\" in file or ".." in file:
            raise HTTPException(400, "Invalid filename.")
        for r in roots:
            cand = r / file
            if cand.is_file() and cand.suffix.lower() in _SUB_FILE_EXTS:
                target = cand
                break

    if target is None:
        raise HTTPException(404, "Subtitle not found.")
    try:
        vtt = await _sub_to_vtt(target)
    except Exception as e:
        raise HTTPException(422, f"Could not convert subtitle: {e}")
    return Response(vtt, media_type="text/vtt")


@app.get("/api/library/{item_id}/skip-data")
async def get_skip_data_for_play(item_id: str, file_path: str = "") -> JSONResponse:
    """Return per-file intro/credits times so the local player can run skip-intro.

    No admin auth — any profile that can play the item can read its skip data.
    Returns the entry for one file when file_path is given, else the full map.
    """
    lib = await get_library()
    item = next((it for it in lib["items"] if it["id"] == item_id), None)
    if not item:
        raise HTTPException(404, "Item not found.")
    sd = item.get("skip_data", {}) or {}
    if file_path:
        return JSONResponse(sd.get(file_path) or {})
    return JSONResponse(sd)


# Static files must be mounted last so API routes take priority
app.mount("/", StaticFiles(directory="static", html=True), name="static")
