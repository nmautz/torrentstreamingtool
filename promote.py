#!/usr/bin/env python3
"""promote.py: mark a build safe for a steadier channel, and move it there (20.1.0).

Work lands on `alpha`. `beta` and `main` only ever receive a build that
somebody has signed off, and this is the signing off. One command does all of
it, so the three things that have to agree cannot drift apart:

  1. THE MARK. An annotated tag `release/<channel>/<version>` on the commit,
     whose message is what was checked and what the changelog admits was not.
     That is the record of "safe for main": who can say later why a build
     was let through reads it with `git show release/main/20.1.0`.
  2. THE BRANCH. `origin/<channel>` is fast-forwarded to that commit. Boxes on
     the channel pick it up through the auto-updater.
  3. THE APP. The newest iOS app that is not newer than the build is added to
     the channel's SideStore source (`ios-app/publish-ipa.sh --promote`). The
     same released .ipa: nothing is rebuilt.

In that order. A box that has updated while its phone has not is the direction
the dashboard is built for; a phone that has updated while its box has not is
the one the whole channel scheme exists to prevent.

It refuses unless:
  * the commit is on `origin/alpha` and the channel can fast-forward to it;
  * the version is newer than the channel's;
  * the unit tests pass AT THAT COMMIT (a clean temporary worktree, so
    uncommitted work cannot vouch for it);
  * the changelog has an entry for the version;
  * the app the channel would get still accepts this server (its MIN_SERVER).

And it will not act without `--verified "what you checked"`. It prints every
line of the changelog, between the channel's version and this one, that admits
something was not checked: read them before writing that sentence.

    python3 promote.py                       where each channel stands
    python3 promote.py main                  the plan for alpha's newest build (does nothing)
    python3 promote.py main --version 20.0.3 the plan for a named build
    python3 promote.py main --verified "..." --go     do it

Promoting to `main` also brings `beta` up to the same build when beta is
behind it: beta is never older than main.

The app half needs macOS (`publish-ipa.sh` reads the .ipa with Apple's tools)
and a logged-in `gh`. Elsewhere pass `--no-app` and run the printed command on
the Mac. Stdlib only; runs under the system Python (3.9+). The rules it applies
are in `appchannel.py`. See docs/GOTCHAS.md § Release channels.
"""

from __future__ import annotations

import argparse
import json
import platform
import re
import subprocess
import sys
import tempfile
import textwrap
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

import appchannel

HERE = Path(__file__).resolve().parent
SOURCE = "alpha"                       # where every build starts
_BADGE_RE = re.compile(r"<div[^>]*\bdata-ui-version\b[^>]*>(\d+\.\d+\.\d+)</div>")
_MIN_SERVER_RE = re.compile(r'const MIN_SERVER = "(\d+\.\d+\.\d+)"')


class Refused(Exception):
    """A check failed. The message is the whole explanation."""


def git(*args: str, check: bool = True) -> str:
    r = subprocess.run(["git", *args], cwd=HERE, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise Refused("git %s failed: %s" % (" ".join(args), (r.stderr or r.stdout).strip()))
    return r.stdout.strip()


def show(rev: str, path: str) -> str:
    r = subprocess.run(["git", "show", "%s:%s" % (rev, path)], cwd=HERE,
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    return r.stdout if r.returncode == 0 else ""


def badge(rev: str) -> str:
    m = _BADGE_RE.findall(show(rev, "static/index.html"))
    return m[-1] if m else ""


def is_ancestor(a: str, b: str) -> bool:
    return subprocess.run(["git", "merge-base", "--is-ancestor", a, b], cwd=HERE).returncode == 0


def commit_for(version: str, floor: str) -> str:
    """The newest commit on alpha whose badge says `version`. Host-only commits
    after a bump keep the badge, and they are part of that build."""
    for rev in git("rev-list", "--first-parent", "%s..origin/%s" % (floor, SOURCE)).split():
        if badge(rev) == version:
            return rev
    raise Refused("no commit on origin/%s after %s carries version %s." % (SOURCE, floor, version))


def fetch_source(channel: str) -> list[dict]:
    """The app versions a channel's SideStore source offers. [] when the
    channel has no source file yet."""
    try:
        with urllib.request.urlopen(appchannel.source_url(channel), timeout=15) as r:
            return appchannel.versions_of(json.loads(r.read().decode("utf-8")))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return []
        raise Refused("could not read the %s source: HTTP %s" % (channel, e.code))
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise Refused("could not read the %s source: %s" % (channel, e))


def run_tests(rev: str) -> list[str]:
    """Run the unit tests the commit itself lists in its Makefile, in a clean
    worktree of that commit. Returns the names of the files that failed."""
    with tempfile.TemporaryDirectory(prefix="streamlink-promote-") as tmp:
        wt = str(Path(tmp) / "wt")
        git("worktree", "add", "--detach", "--quiet", wt, rev)
        try:
            tests = re.findall(r"^\s*python3 (tests/test_\w+\.py)\s*$",
                               (Path(wt) / "Makefile").read_text(encoding="utf-8"), re.M)
            if not tests:
                raise Refused("the Makefile at %s lists no tests." % rev[:9])
            failed = []
            for t in tests:
                r = subprocess.run([sys.executable, t], cwd=wt, capture_output=True, text=True)
                tail = (r.stdout.strip().splitlines() or [""])[-1] if r.returncode == 0 else "FAILED"
                print("    %-34s %s" % (t, tail[:70]))
                if r.returncode != 0:
                    failed.append(t)
            return failed
        finally:
            git("worktree", "remove", "--force", wt, check=False)


def status() -> None:
    git("fetch", "--quiet", "--tags", "origin", *appchannel.CHANNELS)
    print("%-6s %-9s %-10s %-14s %s" % ("", "server", "commit", "behind alpha", "app in its source"))
    for ch in appchannel.CHANNELS:
        rev = "origin/" + ch
        app = appchannel.pick(fetch_source(ch))
        print("%-6s %-9s %-10s %-14s %s" % (
            ch, badge(rev) or "?", git("rev-parse", "--short=9", rev),
            git("rev-list", "--count", "%s..origin/%s" % (rev, SOURCE)),
            app["version"] if app else "(no source file; borrows the next steadier one)"))
    marks = git("tag", "--list", "release/*", "--sort=-creatordate").split()
    print("\nLast marks: " + (", ".join(marks[:6]) if marks else "none yet"))


def plan(channel: str, version: str) -> dict:
    """Everything that would happen, with every check run. Raises Refused."""
    git("fetch", "--quiet", "--tags", "origin", *appchannel.CHANNELS)
    cur_rev = "origin/" + channel
    cur_ver = badge(cur_rev)
    version = version or badge("origin/" + SOURCE)
    if appchannel.parse_version(version) is None:
        raise Refused("'%s' is not a version." % version)
    if appchannel.parse_version(cur_ver) is None:
        raise Refused("could not read the version %s is on." % channel)
    if appchannel.parse_version(version) <= appchannel.parse_version(cur_ver):
        raise Refused("%s is already on %s; %s would not move it forward." % (channel, cur_ver, version))
    if not is_ancestor(cur_rev, "origin/" + SOURCE):
        raise Refused("%s has commits %s does not. Merge them into %s first." % (channel, SOURCE, SOURCE))
    rev = commit_for(version, cur_rev)

    print("Promote %s  →  %s   (%s is on %s)" % (version, channel, channel, cur_ver))
    print("  commit  %s  %s" % (rev[:9], git("log", "-1", "--format=%s", rev)[:80]))
    print("  brings  %s commits" % git("rev-list", "--count", "%s..%s" % (cur_rev, rev)))

    log = show(rev, "CHANGELOG.md")
    if "## [%s]" % version not in log:
        raise Refused("CHANGELOG.md at %s has no entry for %s." % (rev[:9], version))

    # Which channels move. beta is never left older than main.
    moves = [channel]
    if channel == "main" and is_ancestor("origin/beta", rev) and git("rev-parse", "origin/beta") != rev:
        moves.insert(0, "beta")
    if channel == "beta" and not is_ancestor("origin/main", rev):
        raise Refused("main is not behind this build; beta cannot go behind main.")

    # The app each moved channel gains: the newest one not newer than the build.
    alpha_apps = fetch_source(SOURCE) or fetch_source("main")
    apps = {}
    for ch in moves:
        e = appchannel.promotable(alpha_apps, version, fetch_source(ch))
        if e:
            apps[ch] = e["version"]
    newest = appchannel.pick(alpha_apps, version)
    if newest is None:
        print("  app     none published at or below %s; the channel's app stays as it is" % version)
    else:
        for ch in moves:
            print("  app     %s: %s" % (ch, ("gains " + apps[ch]) if ch in apps else "already has %s or newer" % newest["version"]))
        m = _MIN_SERVER_RE.search(show(rev, "ios-app/www/index.html"))
        if m and appchannel.parse_version(m.group(1)) > appchannel.parse_version(version):
            raise Refused("the app at this commit needs a server on %s or newer, and this is %s."
                          % (m.group(1), version))

    print("\n  Unit tests at %s:" % rev[:9])
    failed = run_tests(rev)
    if failed:
        raise Refused("tests failed at %s: %s" % (rev[:9], ", ".join(failed)))

    claims = appchannel.unverified_claims(log, cur_ver, version)
    print("\n  What the changelog says was NOT checked, %s → %s:" % (cur_ver, version))
    if not claims:
        print("    nothing.")
    for v, text in claims:
        print(textwrap.fill(text, 96, initial_indent="    %-8s " % v, subsequent_indent=" " * 13))
    return {"channel": channel, "version": version, "rev": rev, "from": cur_ver,
            "moves": moves, "apps": apps, "claims": claims}


def carry_out(p: dict, verified: str, do_app: bool) -> None:
    tag = "release/%s/%s" % (p["channel"], p["version"])
    lines = ["%s is safe for %s" % (p["version"], p["channel"]), "",
             "Checked: " + verified, "",
             "Promoted from %s. Channels moved: %s." % (p["from"], ", ".join(p["moves"]))]
    if p["apps"]:
        lines.append("App: " + ", ".join("%s gains %s" % kv for kv in p["apps"].items()) + ".")
    if p["claims"]:
        lines += ["", "The changelog says these were not checked when they shipped:"]
        lines += ["- %s: %s" % c for c in p["claims"]]
    print("\n==> Marking %s" % tag)
    git("tag", "-a", tag, p["rev"], "-m", "\n".join(lines))
    git("push", "--quiet", "origin", "refs/tags/" + tag)
    for ch in p["moves"]:
        print("==> Moving %s to %s" % (ch, p["rev"][:9]))
        git("push", "--quiet", "origin", "%s:refs/heads/%s" % (p["rev"], ch))
    git("fetch", "--quiet", "origin", *appchannel.CHANNELS)
    for ch, app_ver in p["apps"].items():
        cmd = [str(HERE / "ios-app" / "publish-ipa.sh"), "--promote", "--channel", ch, "--version", app_ver]
        if not do_app:
            print("==> App not moved (--no-app). On the Mac, run:\n    " + " ".join(cmd))
            continue
        print("==> Adding app %s to the %s source" % (app_ver, ch))
        if subprocess.run(cmd, cwd=HERE).returncode != 0:
            raise Refused("the branch moved but the app did not. Re-run:\n    " + " ".join(cmd))
    print("\nDone. %s is on %s. Boxes on that channel take it at their next update check."
          % (", ".join(p["moves"]), p["version"]))


def main() -> int:
    ap = argparse.ArgumentParser(description="Mark a build safe for a channel and move it there.",
                                 epilog="With no channel: show where each channel stands.")
    ap.add_argument("channel", nargs="?", choices=[c for c in appchannel.CHANNELS if c != SOURCE])
    ap.add_argument("--version", default="", help="the build to promote (default: alpha's newest)")
    ap.add_argument("--verified", default="", help="what was checked; recorded in the tag")
    ap.add_argument("--go", action="store_true", help="act; without it this only prints the plan")
    ap.add_argument("--no-app", action="store_true", help="leave the SideStore source alone")
    a = ap.parse_args()
    try:
        if not a.channel:
            status()
            return 0
        p = plan(a.channel, a.version)
        if not a.go:
            print("\nNothing was changed. To do it:\n  python3 promote.py %s --version %s --verified \"what you checked\" --go"
                  % (p["channel"], p["version"]))
            return 0
        if len(a.verified.strip()) < 12:
            raise Refused("--verified needs a sentence saying what was checked. It is the record.")
        do_app = not a.no_app
        if do_app and p["apps"] and platform.system() != "Darwin":
            raise Refused("moving the app needs macOS. Pass --no-app and run the printed command on the Mac.")
        carry_out(p, a.verified.strip(), do_app)
        return 0
    except Refused as e:
        print("\nREFUSED: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
