"""Media-binary discovery and low-priority subprocess helpers.

Leaf module: stdlib only, imports nothing else in this repo. `analyzer.py` and
`refiner.py` both need to locate ffmpeg/ffprobe/fpcalc and spawn them the same way, and
`refiner` must not import `analyzer` (analyzer imports refiner). Rather than duplicate
the priority/encoding rules — the exact places this repo has been bitten before — they
live here once.

`analyzer.py` re-exports `ffmpeg_bin` / `fpcalc_bin` / `_lp` / `_LOWPRIO_KW`, so the ~38
`analyzer.ffmpeg_bin()` call sites elsewhere keep working unchanged.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

# Run media subprocesses at lowered OS priority so a Smart-Skip pass never starves the
# StreamLink server. The server raises itself to HIGH at startup and children inherit
# that, so without this an analysis would run at HIGH and lag the controls/UI — exactly
# what we're trying to avoid. Windows: BELOW_NORMAL_PRIORITY_CLASS via creationflags.
# POSIX: prepend `nice -n 10` (no-op when `nice` isn't on PATH).
_LOWPRIO_KW: dict = {}
if os.name == "nt":
    _LOWPRIO_KW["creationflags"] = 0x00004000  # BELOW_NORMAL_PRIORITY_CLASS


def _lp(cmd: list[str]) -> list[str]:
    """Prefix `nice -n 10` on POSIX (when available) so the child de-prioritizes."""
    if os.name == "posix" and shutil.which("nice"):
        return ["nice", "-n", "10", *cmd]
    return cmd


# Where each binary is, remembered (20.26.8). Finding one reads .env and stats the
# exe, or searches PATH, and ~44 call sites ask, most of them on the event loop:
# the admin Compression card's status poll (every 2 s) held it 0.33 s with the
# disk busy. A found binary is re-checked every `_BIN_TTL_FOUND_S`, a missing
# one every `_BIN_TTL_MISSING_S`, so installing ffmpeg is noticed within a minute.
_BIN_TTL_FOUND_S = 300.0
_BIN_TTL_MISSING_S = 60.0
_bin_memo: dict = {}


# A remembered answer that has expired is still the answer (20.30.1): the caller
# gets it at once and the lookup runs again on a thread of its own. The first
# lookup after a boot, on a cold disk, held the event loop 0.72 s (admin
# Activity → `fpcalc_bin`), and every expiry was another lookup on the loop.
# `warm_bins` (run by the lifespan on a thread) makes the first one too.
_bin_refreshing: set = set()
_bin_refresh_lock = threading.Lock()
_bin_last_refresh: Optional[threading.Thread] = None     # for the tests


def _bin_store(key: str, val: Optional[str]) -> None:
    ttl = _BIN_TTL_FOUND_S if val else _BIN_TTL_MISSING_S
    _bin_memo[key] = (time.monotonic() + ttl, val)


def _bin_refresh(key: str, find) -> None:
    global _bin_last_refresh
    with _bin_refresh_lock:
        if key in _bin_refreshing:
            return
        _bin_refreshing.add(key)

    def run() -> None:
        try:
            _bin_store(key, find())
        except Exception:
            pass                        # keep the old answer; try again next expiry
        finally:
            with _bin_refresh_lock:
                _bin_refreshing.discard(key)

    t = threading.Thread(target=run, name="bin-lookup", daemon=True)
    _bin_last_refresh = t
    t.start()


def _memo_bin(key: str, find) -> Optional[str]:
    hit = _bin_memo.get(key)
    if hit is not None:
        if time.monotonic() >= hit[0]:
            _bin_refresh(key, find)
        return hit[1]
    val = find()                        # never asked yet: before warm_bins ran
    _bin_store(key, val)
    return val


def warm_bins() -> dict:
    """Look every binary up now. Blocking: call it on a thread, before serving."""
    return {"ffmpeg": ffmpeg_bin(), "ffprobe": ffprobe_bin(), "fpcalc": fpcalc_bin()}


def _env_bin(env_key: str) -> Optional[str]:
    """Read a binary path from the .env file. Falls back to PATH lookup."""
    env_file = Path(__file__).parent / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line.startswith(f"{env_key}="):
                val = line.split("=", 1)[1].strip()
                if val and Path(val).exists():
                    return val
    return shutil.which(env_key.replace("_BIN", "").replace("_", "").lower())


def fpcalc_bin() -> Optional[str]:
    return _memo_bin("fpcalc", lambda: _env_bin("_FPCALC_BIN") or shutil.which("fpcalc"))


def ffmpeg_bin() -> Optional[str]:
    return _memo_bin("ffmpeg", lambda: _env_bin("_FFMPEG_BIN") or shutil.which("ffmpeg"))


def ffprobe_bin() -> Optional[str]:
    return _memo_bin("ffprobe", _find_ffprobe)


def _find_ffprobe() -> Optional[str]:
    """Locate ffprobe by swapping only the FILENAME of the ffmpeg path.

    A blanket `str.replace("ffmpeg", "ffprobe")` rewrites EVERY "ffmpeg" segment of the
    bundled Windows path ("tools/ffmpeg/ffmpeg-8.1.1-essentials_build/bin/ffmpeg.exe"),
    producing a path that never exists — which silently demoted every Windows host to a
    brittle fallback. See docs/GOTCHAS.md.
    """
    ff = ffmpeg_bin()
    if ff:
        p = Path(ff)
        cand = p.with_name(p.name.replace("ffmpeg", "ffprobe"))
        if cand.name != p.name and cand.exists():
            return str(cand)
    return shutil.which("ffprobe")


def run_capture(cmd: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    """Run a media tool at low priority and capture both streams as text.

    ALWAYS decodes as UTF-8. Bare `text=True` uses the ANSI code page on Windows, and a
    non-Latin path or stream title then makes the call return EMPTY output with rc=0 —
    no exception, no error, just nothing. Chapter titles in this library are routinely
    Japanese, so this is not hypothetical. See docs/GOTCHAS.md.
    """
    return subprocess.run(
        _lp(cmd), capture_output=True, timeout=timeout,
        encoding="utf-8", errors="replace",
        **_LOWPRIO_KW,
    )
