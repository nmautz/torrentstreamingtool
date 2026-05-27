#!/usr/bin/env python3
"""
StreamLink Auto-Updater
=======================
Pulls new commits from a configured branch (main / beta / alpha), re-runs
setup.py non-interactively to refresh deps + the system-service registration,
then asks the running uvicorn process to exit so the OS service supervisor
relaunches it on the new code.

All branch operations are gated to ALLOWED_BRANCHES so the picker can't be
coaxed into fetching anything else.

The actual *trigger* of `apply()` lives in main.py (`updater_loop` background
task + `/api/admin/updater/*` endpoints) — this module is the platform-agnostic
plumbing it calls into.
"""
from __future__ import annotations

import asyncio
import logging
import os
import platform
import sys
from pathlib import Path
from typing import Optional

HERE = Path(__file__).parent
SYSTEM = platform.system()

ALLOWED_BRANCHES: tuple[str, ...] = ("main", "beta", "alpha")

log = logging.getLogger("streamlink.updater")


# ── git helpers ──────────────────────────────────────────────────────────────

async def _git(*args: str, timeout: float = 60.0) -> tuple[int, str, str]:
    """Run `git <args>` from the repo root.

    Returns (rc, stdout, stderr). On timeout: rc=124, stderr="timeout".
    Never raises — callers branch on rc.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", *args,
            cwd=str(HERE),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return 127, "", "git not found on PATH"
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.wait()
        except ProcessLookupError:
            pass
        return 124, "", "timeout"
    return (
        proc.returncode if proc.returncode is not None else -1,
        out.decode("utf-8", "replace").strip(),
        err.decode("utf-8", "replace").strip(),
    )


async def is_git_repo() -> bool:
    rc, _, _ = await _git("rev-parse", "--is-inside-work-tree", timeout=10.0)
    return rc == 0


async def current_branch() -> str:
    rc, out, _ = await _git("rev-parse", "--abbrev-ref", "HEAD", timeout=10.0)
    return out if rc == 0 else ""


async def current_commit(short: bool = True) -> str:
    rc, out, _ = await _git("rev-parse", "--short=12" if short else "HEAD", "HEAD", timeout=10.0)
    if rc == 0 and out:
        # When passing "--short=12 HEAD" git echoes only the abbrev, but newer
        # versions reject the combined form on some platforms — fall back.
        return out.splitlines()[0].strip()
    rc, out, _ = await _git("rev-parse", "HEAD", timeout=10.0)
    return out[:12] if rc == 0 else ""


async def _fetch(branch: str, timeout: float = 180.0) -> tuple[bool, str]:
    if branch not in ALLOWED_BRANCHES:
        return False, f"Branch '{branch}' is not allowed."
    rc, _, err = await _git("fetch", "--prune", "origin", branch, timeout=timeout)
    if rc != 0:
        return False, err or f"git fetch failed (rc={rc})"
    return True, "ok"


async def check_update(branch: str) -> dict:
    """Look up how far behind origin/<branch> we are. Does a fetch first.

    Returned shape:
        {
          "ok": bool,
          "branch": str,
          "local": "<short sha>",
          "remote": "<short sha>",
          "behind_by": int,    # commits behind origin/<branch>
          "ahead_by": int,     # commits ahead of origin/<branch>
          "has_update": bool,  # behind_by > 0
          "error": str         # only present on failure
        }
    """
    if branch not in ALLOWED_BRANCHES:
        return {"ok": False, "branch": branch, "error": f"Branch '{branch}' not allowed."}

    if not await is_git_repo():
        return {"ok": False, "branch": branch, "error": "Not a git checkout."}

    ok, msg = await _fetch(branch)
    if not ok:
        return {"ok": False, "branch": branch, "error": msg}

    rc, _, _ = await _git("rev-parse", f"origin/{branch}", timeout=10.0)
    if rc != 0:
        return {"ok": False, "branch": branch, "error": f"origin/{branch} not found."}

    local = await current_commit()
    rc, remote, _ = await _git("rev-parse", f"origin/{branch}", timeout=10.0)
    remote = remote[:12] if rc == 0 else ""

    rc, out, _ = await _git("rev-list", "--left-right", "--count", f"HEAD...origin/{branch}", timeout=15.0)
    ahead = behind = 0
    if rc == 0 and out:
        parts = out.split()
        if len(parts) == 2:
            try:
                ahead, behind = int(parts[0]), int(parts[1])
            except ValueError:
                pass
    return {
        "ok": True,
        "branch": branch,
        "local": local,
        "remote": remote,
        "behind_by": behind,
        "ahead_by": ahead,
        "has_update": behind > 0,
    }


async def switch_branch(branch: str) -> dict:
    """Hard-checkout origin/<branch>. Any local edits to tracked files are
    wiped — we own the host repo so this is the intended behaviour.
    """
    if branch not in ALLOWED_BRANCHES:
        return {"ok": False, "error": f"Branch '{branch}' not allowed."}
    if not await is_git_repo():
        return {"ok": False, "error": "Not a git checkout."}

    ok, msg = await _fetch(branch)
    if not ok:
        return {"ok": False, "error": msg}

    # Force checkout — `git switch -C` (re)creates the local branch pointing at
    # origin/<branch>. Equivalent to checkout -B but uses the modern verb.
    rc, _, err = await _git("switch", "-C", branch, f"origin/{branch}", timeout=60.0)
    if rc != 0:
        # Fall back to checkout for very old git builds (pre-2.23 / no `switch`).
        rc, _, err = await _git("checkout", "-B", branch, f"origin/{branch}", timeout=60.0)
        if rc != 0:
            return {"ok": False, "error": err or "git switch/checkout failed"}

    rc, _, err = await _git("reset", "--hard", f"origin/{branch}", timeout=60.0)
    if rc != 0:
        return {"ok": False, "error": err or "git reset failed"}

    return {"ok": True, "branch": branch, "commit": await current_commit()}


async def apply_update(branch: str) -> dict:
    """Switch to + hard-reset onto origin/<branch>. Idempotent."""
    return await switch_branch(branch)


# ── setup re-run + service restart ───────────────────────────────────────────

def _venv_python() -> Optional[Path]:
    if SYSTEM == "Windows":
        p = HERE / ".venv" / "Scripts" / "python.exe"
    else:
        p = HERE / ".venv" / "bin" / "python"
    return p if p.exists() else None


async def run_setup(timeout: float = 900.0) -> dict:
    """Run setup.py from the repo root with NO stdin so every prompt falls
    through to its default value (setup.py's `_STDIN_INTERACTIVE` flag detects
    this and skips interactive input).

    Returns {ok, returncode, output_tail, error?}. `output_tail` is the last
    8 KiB of combined stdout+stderr — enough to surface a traceback to the
    admin UI without flooding it.
    """
    py = _venv_python() or Path(sys.executable)
    setup = HERE / "setup.py"
    if not setup.exists():
        return {"ok": False, "error": "setup.py not found"}

    log.info("Re-running setup.py with %s (non-interactive)", py)
    # PYTHONIOENCODING=utf-8 keeps the Unicode banner / ✓ / ✗ glyphs from
    # crashing setup.py on Python 3.13 + Windows, where a piped stdout
    # defaults to the host's legacy ANSI code page (cp1252 in en-US) and
    # UnicodeEncodeError fires on the very first print(). setup.py also
    # reconfigures its own stdout/stderr defensively, but setting this here
    # belt-and-braces any version of setup.py that doesn't.
    #
    # STREAMLINK_AUTOUPDATE=1 puts setup.py into its non-interactive update
    # mode: reuse the existing .env, skip OS-app installs, tolerate transient
    # pip failures (don't kill the update), and skip offer_service_install
    # (the updater does its own daemon.uninstall() + install() afterwards).
    env = {
        **os.environ,
        "PYTHONUNBUFFERED": "1",
        "PYTHONIOENCODING":  "utf-8",
        "STREAMLINK_AUTOUPDATE": "1",
    }
    try:
        proc = await asyncio.create_subprocess_exec(
            str(py), str(setup),
            cwd=str(HERE),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
    except Exception as exc:
        return {"ok": False, "error": f"Could not spawn setup.py: {exc}"}

    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.wait()
        except ProcessLookupError:
            pass
        return {"ok": False, "error": f"setup.py timed out after {int(timeout)}s"}

    text = out.decode("utf-8", "replace")
    rc = proc.returncode if proc.returncode is not None else -1
    log.info("setup.py exited rc=%d (%d bytes of output)", rc, len(text))
    return {
        "ok": rc == 0,
        "returncode": rc,
        "output_tail": text[-8192:],
    }


async def refresh_service_wrapper() -> dict:
    """Regenerate the `streamlink_service.py` supervisor wrapper from the
    freshly-pulled `daemon._WRAPPER_CONTENT`. Does NOT touch the OS-level
    service registration (Task Scheduler task / launchd plist / systemd unit).

    Why not full uninstall + reinstall:
      - On Windows, `daemon.install()` requires admin and tries to UAC-elevate
        via `ShellExecute(..."runas"...)`. From a service-launched uvicorn
        there's no interactive desktop to display the UAC prompt, so the call
        either fails silently or blocks the auto-update on a manual click —
        which makes the whole flow non-automatic.
      - The OS service entry references the wrapper script by *path*
        (`<repo>/streamlink_service.py`). That path is stable across versions,
        so the supervisor keeps finding the same file. What matters for
        getting the *new code* running is that the wrapper file's CONTENTS
        reflect the new `daemon._WRAPPER_CONTENT` — which is a plain file
        write, no elevation needed (the repo is owned by the user the service
        runs as).
      - The reboot at the end of `_run_apply` gives the supervisor a clean
        process tree on the new wrapper anyway. No `launchctl reload` /
        `systemctl daemon-reload` is necessary.

    If `daemon.py` introduces a change that needs a re-registration (e.g.,
    new plist key, different schtasks arguments), the admin has to run
    `python run.py --install` manually from an elevated shell after the
    update. The admin UI's diagnostic panel calls that out.
    """
    try:
        import daemon as _daemon  # type: ignore
    except Exception as exc:
        log.error("Could not import daemon.py for wrapper refresh: %s", exc)
        return {"ok": False, "error": f"Could not import daemon.py: {exc}",
                "output": ""}

    wrapper_path = getattr(_daemon, "_WRAPPER_PATH", None)
    wrapper_content = getattr(_daemon, "_WRAPPER_CONTENT", None)
    if wrapper_path is None or wrapper_content is None:
        return {"ok": False,
                "error": "daemon.py is missing _WRAPPER_PATH / _WRAPPER_CONTENT",
                "output": ""}

    def _do_it() -> tuple[bool, str]:
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        ok = False
        with redirect_stdout(buf):
            try:
                # Skip the rewrite if the file is already byte-identical — keeps
                # mtime stable and gives the admin UI a clearer "no-op" log entry.
                existing = ""
                try:
                    existing = wrapper_path.read_text(encoding="utf-8")
                except OSError:
                    pass
                if existing == wrapper_content:
                    print(f"Wrapper already up to date: {wrapper_path.name}")
                    ok = True
                else:
                    wrapper_path.write_text(wrapper_content, encoding="utf-8")
                    try:
                        wrapper_path.chmod(0o755)
                    except OSError:
                        pass   # Windows ignores chmod, that's fine
                    print(f"Wrote service wrapper → {wrapper_path.name} "
                          f"({len(wrapper_content)} bytes)")
                    ok = True
            except OSError as exc:
                print(f"[wrapper] write failed: {exc}")
                ok = False
            except Exception as exc:
                print(f"[wrapper] unexpected error: {exc}")
                ok = False
        return ok, buf.getvalue()

    try:
        ok, output = await asyncio.wait_for(
            asyncio.to_thread(_do_it),
            timeout=30.0,
        )
    except asyncio.TimeoutError:
        return {"ok": False, "error": "wrapper refresh timed out",
                "output": ""}

    log.info("Service wrapper refresh %s", "ok" if ok else "failed")
    return {"ok": ok, "output": output,
            "error": "" if ok else "wrapper refresh failed (see output)"}


async def service_is_installed() -> bool:
    """Best-effort: True if the OS service supervisor knows about StreamLink.

    Used to decide whether to trigger a process-restart after an update. When
    the service is registered, exiting uvicorn brings the supervisor's restart
    loop into play and the new code comes up automatically. When it's not, the
    admin has to manually re-launch — surfaced in the UI as a warning.
    """
    try:
        import daemon  # type: ignore
    except Exception:
        return False

    if SYSTEM == "Darwin":
        plist = daemon._LAUNCHD_PLIST  # type: ignore[attr-defined]
        return plist.exists()
    if SYSTEM == "Linux":
        unit = daemon._SYSTEMD_UNIT_PATH  # type: ignore[attr-defined]
        return unit.exists()
    if SYSTEM == "Windows":
        proc = await asyncio.create_subprocess_exec(
            "schtasks", "/Query", "/TN", "StreamLink",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            await asyncio.wait_for(proc.communicate(), timeout=10.0)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return False
        return proc.returncode == 0
    return False
