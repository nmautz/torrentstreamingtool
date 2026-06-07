#!/usr/bin/env python3
"""
StreamLink Auto-Updater
=======================
Pulls new commits from a configured branch (main / beta / alpha), re-runs
setup.py non-interactively to refresh deps + the system-service registration,
then asks the running uvicorn process to exit so the OS service supervisor
relaunches it on the new code.

All branch operations are gated by `branch_allowed()`. By default only
ALLOWED_BRANCHES (main/beta/alpha) pass; the admin's "show all branches" dev
toggle relaxes the gate to any structurally-valid branch that exists on origin,
so a developer can ride a feature branch without weakening the default picker.

The actual *trigger* of `apply()` lives in main.py (`updater_loop` background
task + `/api/admin/updater/*` endpoints) — this module is the platform-agnostic
plumbing it calls into.
"""
from __future__ import annotations

import asyncio
import logging
import os
import platform
import re
import sys
from pathlib import Path
from typing import Optional

HERE = Path(__file__).parent
SYSTEM = platform.system()

# The branches the picker offers by default. "Dev mode" (the admin's
# "show all branches" checkbox) relaxes this to *any* branch that actually
# exists on origin and passes the structural guard below — see branch_allowed()
# and list_remote_branches(). Without dev mode, only these three are reachable.
ALLOWED_BRANCHES: tuple[str, ...] = ("main", "beta", "alpha")

# Structural guard for a dev-mode branch name. A real git branch is made of
# these chars; rejecting everything else stops a relaxed name from smuggling in
# git option injection (leading "-"), a traversal-y refspec ("..", leading/
# trailing "/"), or anything that isn't HTML-safe when echoed into the picker.
_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/\-]*$")

# A git commit id: 7–40 hex chars (abbreviated or full SHA-1). Anything else is
# rejected so a dev-mode commit pin can't smuggle in a refspec, option injection
# (leading "-"), or a branch name where a raw SHA is expected.
_COMMIT_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")

log = logging.getLogger("streamlink.updater")


def _looks_like_branch(name: str) -> bool:
    """True if `name` is a plausible, safe branch name for the relaxed picker."""
    if not name or ".." in name or name.endswith("/") or "//" in name:
        return False
    return bool(_BRANCH_RE.match(name))


def _looks_like_commit(sha: str) -> bool:
    """True if `sha` is a plausible abbreviated/full commit id (7–40 hex chars)."""
    return bool(sha) and bool(_COMMIT_RE.match(sha))


def branch_allowed(branch: str, allow_any: bool = False) -> tuple[bool, str]:
    """Gate a branch name. Without dev mode, only ALLOWED_BRANCHES pass. With
    dev mode (`allow_any`), any structurally-valid branch name passes — git
    itself rejects one that doesn't exist on origin when the fetch runs, so the
    structural guard is the only thing we need to enforce here.

    Returns (ok, error_message). Shared by the async git helpers below and the
    API layer in main.py so both agree on what's reachable.
    """
    if branch in ALLOWED_BRANCHES:
        return True, ""
    if not allow_any:
        return False, (f"Branch '{branch}' is not allowed. Pick one of: "
                       f"{', '.join(ALLOWED_BRANCHES)}.")
    if not _looks_like_branch(branch):
        return False, f"Branch '{branch}' is not a valid branch name."
    return True, ""


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


async def is_detached_head() -> bool:
    """True when HEAD points at a raw commit rather than a branch (the admin
    pinned a specific commit via switch_commit). `git rev-parse --abbrev-ref
    HEAD` echoes the literal string "HEAD" in that state.

    The auto-updater stands down while detached — there is no branch to track —
    until the admin switches back to a branch. See switch_commit() and the
    detached-HEAD guard in main.py's updater_loop.
    """
    return (await current_branch()) == "HEAD"


async def current_commit(short: bool = True) -> str:
    rc, out, _ = await _git("rev-parse", "--short=12" if short else "HEAD", "HEAD", timeout=10.0)
    if rc == 0 and out:
        # When passing "--short=12 HEAD" git echoes only the abbrev, but newer
        # versions reject the combined form on some platforms — fall back.
        return out.splitlines()[0].strip()
    rc, out, _ = await _git("rev-parse", "HEAD", timeout=10.0)
    return out[:12] if rc == 0 else ""


async def _fetch(branch: str, timeout: float = 180.0, allow_any: bool = False) -> tuple[bool, str]:
    ok, err = branch_allowed(branch, allow_any)
    if not ok:
        return False, err
    rc, _, err = await _git("fetch", "--prune", "origin", branch, timeout=timeout)
    if rc != 0:
        return False, err or f"git fetch failed (rc={rc})"
    return True, "ok"


async def list_remote_branches(timeout: float = 30.0) -> list[str]:
    """All branch names on origin, structurally valid + sorted, for the dev-mode
    ("show all branches") picker. Canonical branches (main/beta/alpha) float to
    the top; the rest follow alphabetically. Returns [] if not a git repo or the
    remote query fails — the UI falls back to the three defaults in that case.
    """
    if not await is_git_repo():
        return []
    rc, out, _ = await _git("ls-remote", "--heads", "origin", timeout=timeout)
    if rc != 0 or not out:
        return []
    names: set[str] = set()
    for line in out.splitlines():
        # Each line is "<sha>\trefs/heads/<branch>".
        _, _, ref = line.partition("\trefs/heads/")
        ref = ref.strip()
        if ref and _looks_like_branch(ref):
            names.add(ref)
    canon = [b for b in ALLOWED_BRANCHES if b in names]
    rest = sorted(b for b in names if b not in ALLOWED_BRANCHES)
    return canon + rest


async def check_update(branch: str, allow_any: bool = False) -> dict:
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
    ok, msg = branch_allowed(branch, allow_any)
    if not ok:
        return {"ok": False, "branch": branch, "error": msg}

    if not await is_git_repo():
        return {"ok": False, "branch": branch, "error": "Not a git checkout."}

    ok, msg = await _fetch(branch, allow_any=allow_any)
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


async def switch_branch(branch: str, allow_any: bool = False) -> dict:
    """Hard-checkout origin/<branch>. Any local edits to tracked files are
    wiped — we own the host repo so this is the intended behaviour.
    """
    ok, msg = branch_allowed(branch, allow_any)
    if not ok:
        return {"ok": False, "error": msg}
    if not await is_git_repo():
        return {"ok": False, "error": "Not a git checkout."}

    ok, msg = await _fetch(branch, allow_any=allow_any)
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


async def switch_commit(commit: str, allow_any: bool = False) -> dict:
    """Force-detach HEAD onto a specific commit id. **Developer mode only.**

    A developer pins an exact build by SHA (e.g. to bisect a regression). This
    leaves the repo in a *detached HEAD* state, which deliberately **disables
    the auto-updater** (is_detached_head + the updater_loop guard): there is no
    branch to track. The admin returns to auto-update by selecting a branch and
    using Switch Branch.

    Gated on `allow_any` (the admin's "show all branches" developer toggle) —
    without it we refuse, since pinning a commit is a developer-only operation
    and never reachable from the default three-branch picker.
    """
    if not allow_any:
        return {"ok": False, "error": "Pinning a commit requires developer mode."}
    commit = (commit or "").strip()
    if not _looks_like_commit(commit):
        return {"ok": False,
                "error": f"'{commit}' is not a valid commit id (7–40 hex chars)."}
    if not await is_git_repo():
        return {"ok": False, "error": "Not a git checkout."}

    # Make sure the object is present locally. A fetch of every branch usually
    # brings it in; if the commit isn't on a branch tip we also try fetching the
    # bare SHA (GitHub serves any commit reachable from an advertised ref).
    await _git("fetch", "--prune", "origin", timeout=180.0)
    rc, _, _ = await _git("rev-parse", "--verify", "--quiet",
                          f"{commit}^{{commit}}", timeout=10.0)
    if rc != 0:
        await _git("fetch", "origin", commit, timeout=180.0)
        rc, _, _ = await _git("rev-parse", "--verify", "--quiet",
                              f"{commit}^{{commit}}", timeout=10.0)
        if rc != 0:
            return {"ok": False, "error": f"Commit '{commit}' not found on origin."}

    # Force-detach onto it. `-f` discards local edits to tracked files (we own
    # the repo); `--detach` is explicit so git never resolves it as a branch.
    rc, _, err = await _git("checkout", "-f", "--detach", commit, timeout=60.0)
    if rc != 0:
        return {"ok": False, "error": err or "git checkout failed"}

    return {"ok": True, "commit": await current_commit(), "detached": True}


async def apply_update(branch: str, allow_any: bool = False) -> dict:
    """Switch to + hard-reset onto origin/<branch>. Idempotent."""
    return await switch_branch(branch, allow_any=allow_any)


async def reset_hard(allow_any: bool = False) -> dict:
    """Force the working tree back onto origin/<current-branch>.

    Recovery tool for a wedged / diverged checkout: fetches the current
    branch and `git reset --hard origin/<branch>`, discarding any local
    commits and uncommitted edits to tracked files. Stays on the same
    branch (unlike switch_branch) and does NOT `git clean`, so untracked /
    gitignored files (library.json, .env, .offline_cache/, .background/)
    survive.

    Gated by branch_allowed: refuses to act on a detached HEAD or — without
    dev mode — any branch outside main/beta/alpha, so the button can't quietly
    nuke an unexpected checkout. With dev mode (`allow_any`) it will reset onto
    whatever dev branch is checked out.
    """
    if not await is_git_repo():
        return {"ok": False, "error": "Not a git checkout."}

    branch = await current_branch()
    if not branch or branch == "HEAD":
        return {"ok": False, "error": "Detached HEAD — checkout a branch first."}
    ok, msg = branch_allowed(branch, allow_any)
    if not ok:
        return {"ok": False, "error": f"Current branch '{branch}': {msg}"}

    ok, msg = await _fetch(branch, allow_any=allow_any)
    if not ok:
        return {"ok": False, "error": msg}

    rc, _, err = await _git("reset", "--hard", f"origin/{branch}", timeout=60.0)
    if rc != 0:
        return {"ok": False, "error": err or "git reset failed"}

    return {"ok": True, "branch": branch, "commit": await current_commit()}


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
