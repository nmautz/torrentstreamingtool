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
    python3 promote.py candidates            every build since main: what it changed, what was
                                             fixed AFTER it, what nobody checked, which app it gets
    python3 promote.py logs FILE...          what the box's server logs say about each version
    python3 promote.py logs --box https://192.168.0.106
                                             the same, fetched from the box (admin password in
                                             the STREAMLINK_ADMIN_PASSWORD environment variable)
    python3 promote.py main                  the plan for alpha's newest build (does nothing)
    python3 promote.py main --version 20.0.3 the plan for a named build
    python3 promote.py main --verified "..." --go     do it

Promoting to `main` also brings `beta` up to the same build when beta is
behind it: beta is never older than main.

The app half needs macOS (`publish-ipa.sh` reads the .ipa with Apple's tools)
and a logged-in `gh`. Elsewhere pass `--no-app` and run the printed command on
the Mac.

`candidates` and `logs` gather evidence and decide nothing. Choosing a build is
the release-candidate procedure in `.claude/skills/release-candidate/SKILL.md`:
Claude reads this evidence and the box's logs, asks about what no log can show,
and the owner has the final say. Stdlib only; runs under the system Python (3.9+). The rules it applies
are in `appchannel.py`. See docs/GOTCHAS.md § Release channels.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import platform
import re
import ssl
import subprocess
import sys
import tempfile
import textwrap
import urllib.error
import urllib.request
import zipfile
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


def released_apps() -> list[dict]:
    """Every app version any channel's source offers. A build's app was
    published to the channel it was built on, and channels are files, so the
    full list of what exists is their union."""
    seen: dict[str, dict] = {}
    for ch in appchannel.CHANNELS:
        for e in fetch_source(ch):
            seen.setdefault(str(e.get("version")), e)
    return list(seen.values())


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


def candidates(channel: str) -> None:
    """Every build between a channel and alpha's tip, newest first, with what
    the changelog can say about each. The lines under a build are what you
    would be SHIPPING WITHOUT by stopping there: fixes that landed later."""
    git("fetch", "--quiet", "--tags", "origin", *appchannel.CHANNELS)
    cur_ver, tip = badge("origin/" + channel), badge("origin/" + SOURCE)
    log = show("origin/" + SOURCE, "CHANGELOG.md")
    entries = appchannel.changelog_entries(log, cur_ver, tip)
    if not entries:
        print("%s is on %s, the same as %s. Nothing to promote." % (channel, cur_ver, SOURCE))
        return
    apps = released_apps()
    claims: dict[str, list[str]] = {}
    for v, text in appchannel.unverified_claims(log, cur_ver, tip):
        claims.setdefault(v, []).append(text)
    today = git("log", "-1", "--format=%cs", "origin/" + SOURCE)
    print("%s is on %s. %s is on %s (%s). %d builds between them, newest first.\n"
          % (channel, cur_ver, SOURCE, tip, today, len(entries)))
    later_fixes: list[tuple[str, str]] = []
    for e in entries:
        app = appchannel.pick(apps, e["version"])
        counts = ", ".join("%d %s" % (e[k], k) for k in ("new", "changed", "fixed", "removed") if e[k])
        print("%-8s %s  %s-bump  app %s  [%s]" % (e["version"], e["date"], e["kind"],
                                                 app["version"] if app else "none", counts or "no tagged bullets"))
        print("         %s" % e["title"][:100])
        for c in claims.get(e["version"], []):
            print(textwrap.fill(c, 100, initial_indent="         NOT CHECKED: ", subsequent_indent="           "))
        if later_fixes:
            print("         fixed after this build (%d):" % len(later_fixes))
            for v, f in later_fixes[-8:]:
                print("           %-8s %s" % (v, f[:110]))
            if len(later_fixes) > 8:
                print("           ... and %d older ones above" % (len(later_fixes) - 8))
        else:
            print("         fixed after this build: nothing yet")
        print()
        later_fixes += [(e["version"], f) for f in e["fixes"]]


APP_LOG = "streamlink_app.log"        # the one with the version banner


def _log_lines(name: str, data: bytes) -> list[str]:
    """The server log's lines out of a plain log or one of the box's
    `logs_old_*.zip` archives (it zips the previous run's logs at each start)."""
    if name.lower().endswith(".zip"):
        out: list[str] = []
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                for m in z.namelist():
                    if m.replace("\\", "/").split("/")[-1] == APP_LOG:
                        out += z.read(m).decode("utf-8", errors="replace").splitlines()
        except zipfile.BadZipFile:
            print("  (skipped %s: not a readable zip)" % name, file=sys.stderr)
        return out
    return data.decode("utf-8", errors="replace").splitlines()


def fetch_box_logs(box: str, limit: int) -> list[str]:
    """Pull the live server log and the newest `limit` archived runs off a box
    through its admin API. Read-only. The box's HTTPS certificate is its own
    self-signed one, so it is not verified: this is for a box on your own
    network, named by address."""
    pw = os.environ.get("STREAMLINK_ADMIN_PASSWORD", "")
    if not pw:
        raise Refused("set STREAMLINK_ADMIN_PASSWORD to the box's admin password.")
    ctx = ssl._create_unverified_context()
    base = box.rstrip("/")

    def call(path: str, body: Optional[dict] = None, token: str = "") -> bytes:
        req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None)
        if body is not None:
            req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("Authorization", "Bearer " + token)
        try:
            with urllib.request.urlopen(req, timeout=60, context=ctx) as r:
                return r.read()
        except (urllib.error.URLError, OSError) as e:
            raise Refused("%s%s: %s" % (base, path, e))

    token = json.loads(call("/api/admin/login", {"password": pw})).get("token", "")
    if not token:
        raise Refused("the box did not accept the admin password.")
    files = json.loads(call("/api/admin/logs", token=token)).get("files", [])
    wanted = [f["name"] for f in files if f["name"] == APP_LOG]
    wanted += [f["name"] for f in files if f.get("prior") and f["name"].endswith(".zip")][:limit]
    lines: list[str] = []
    for name in wanted:
        lines += _log_lines(name, call("/api/admin/logs/" + urllib.request.quote(name), token=token))
    running = json.loads(call("/api/version")).get("version", "?")
    print("Read %d files from %s (running %s now).\n" % (len(wanted), base, running))
    return lines


def logs(paths: list[str], box: str = "", limit: int = 80, to: str = "main", everything: bool = False) -> None:
    """Per version: how long it ran on the box, how often it started, and the
    errors it logged that no older version in the same logs did. Only versions
    newer than the `to` channel are printed (older ones are still read: they
    are what "new" is measured against).

    The version is the one in the start-up banner, which is the BACKEND's. A
    static-only deploy (`reboot: false`) leaves the old banner in place, so a
    newer dashboard can have been running under an older number."""
    lines: list[str] = fetch_box_logs(box, limit) if box else []
    for p in paths:
        try:
            lines += _log_lines(p, Path(p).read_bytes())
        except OSError as e:
            raise Refused("could not read %s: %s" % (p, e))
    # Several files (a rotated log and the live one) are one history.
    health = appchannel.log_health(_keep_order(lines))
    if not health:
        raise Refused("no 'StreamLink vX starting' line in those files. Is this the server log?")
    floor = None if everything else appchannel.parse_version(badge("origin/" + to))
    shown = [r for r in health if floor is None or appchannel.parse_version(r["version"]) > floor]
    print("%-9s %-7s %5s %9s %7s %6s  %s" % ("version", "branch", "runs", "ran", "errors", "trace", "first seen → last seen"))
    for r in shown:
        print("%-9s %-7s %5d %8.1fh %7d %6d  %s → %s" % (
            r["version"], r["branch"][:7], r["runs"], r["seconds"] / 3600.0,
            r["errors"], r["tracebacks"], r["first"], r["last"]))
    if len(shown) < len(health):
        print("(%d versions at or below %s's are not shown; --all prints them)" % (len(health) - len(shown), to))
    print("\nErrors each version logged that no older version in these logs did:")
    for r in shown:
        new = appchannel.new_signatures(health, r["version"])
        if not new:
            continue
        print("  %s" % r["version"])
        for sig, n in new[:12]:
            print("    %4d×  %s" % (n, sig))
        if len(new) > 12:
            print("    ... and %d more" % (len(new) - 12))


def _keep_order(lines: list[str]) -> list[str]:
    """Sorting by timestamp would scatter a traceback (its lines have none)
    away from the error above it. Give each continuation line the stamp of the
    line it follows, sort stably, and the blocks stay whole."""
    keyed, last = [], ""
    for l in lines:
        if l[:4].isdigit() and len(l) > 19 and l[4] == "-":
            last = l[:19]
        keyed.append((last, l))
    return [l for _, l in sorted(keyed, key=lambda kl: kl[0])]


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
    alpha_apps = released_apps()
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
    ap.add_argument("channel", nargs="?",
                    choices=[c for c in appchannel.CHANNELS if c != SOURCE] + ["candidates", "logs"])
    ap.add_argument("paths", nargs="*", help="for `logs`: server log files pulled off the box")
    ap.add_argument("--box", default="", help="for `logs`: fetch them from this box, e.g. https://192.168.0.106")
    ap.add_argument("--runs", type=int, default=80, help="for `logs --box`: how many archived runs to read")
    ap.add_argument("--all", action="store_true", help="for `logs`: print every version in the logs")
    ap.add_argument("--to", default="main", choices=[c for c in appchannel.CHANNELS if c != SOURCE],
                    help="for `candidates` and `logs`: the channel being promoted to (default main)")
    ap.add_argument("--version", default="", help="the build to promote (default: alpha's newest)")
    ap.add_argument("--verified", default="", help="what was checked; recorded in the tag")
    ap.add_argument("--go", action="store_true", help="act; without it this only prints the plan")
    ap.add_argument("--no-app", action="store_true", help="leave the SideStore source alone")
    a = ap.parse_args()
    try:
        if not a.channel:
            status()
            return 0
        if a.channel == "candidates":
            candidates(a.to)
            return 0
        if a.channel == "logs":
            if not a.paths and not a.box:
                raise Refused("give it server log files (streamlink_app.log, logs_old_*.zip) or --box URL")
            logs(a.paths, a.box, a.runs, a.to, a.all)
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
