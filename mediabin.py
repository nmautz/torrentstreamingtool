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
    return _env_bin("_FPCALC_BIN") or shutil.which("fpcalc")


def ffmpeg_bin() -> Optional[str]:
    return _env_bin("_FFMPEG_BIN") or shutil.which("ffmpeg")


def ffprobe_bin() -> Optional[str]:
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
