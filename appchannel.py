"""Release channels for the iOS app (20.1.0).

The server has three branches (`main`, `beta`, `alpha`) and a box follows one of
them. The app used to have one SideStore source that got every build, so a
person whose server follows `main` was offered an app built from `alpha`: an
app NEWER than its server, which is the direction nothing tests. An older app
on a newer server is the well-trodden one, because the server ships the
dashboard and the dashboard already copes with a shell that lacks a plugin.

So the app follows the server's channel. There is one source file per branch in
the public SideStore repo, and this module is the arithmetic both ends share:

  * which source file a branch reads (`source_file`, `source_urls`), with a
    fallback DOWN the chain only: a channel with no file of its own may borrow
    a steadier channel's app, never a fresher one;
  * which app version a server may recommend (`pick`): the newest one in its
    channel's source that is not newer than the server itself;
  * what `promote.py` needs to move a build to a steadier channel
    (`promotable`, `unverified_claims`).

Leaf module: stdlib only, no `main` import. Tests in `tests/test_appchannel.py`.
See docs/GOTCHAS.md § Release channels.
"""

from __future__ import annotations

import re
from typing import Iterable, Optional

# Steadiest first. The order IS the fallback chain, read right to left.
CHANNELS: tuple[str, ...] = ("main", "beta", "alpha")

SOURCE_REPO = "nmautz/streamlink-ios"
BUNDLE_ID = "com.streamlink.client"

_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def parse_version(v) -> Optional[tuple[int, int, int]]:
    """`"20.1.0"` → `(20, 1, 0)`. None for anything that is not x.y.z, so a
    dev build's `1.0` or a missing value never compares as a version."""
    m = _VERSION_RE.match(str(v or "").strip())
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def channel_for(branch: str, configured: str = "") -> str:
    """The channel a checkout belongs to. `branch` is what is checked out;
    `configured` is the auto-updater's branch, used when the checkout cannot
    say (a detached HEAD, a feature branch, a zip with no git). Anything
    unrecognised is `main`: the steadiest answer is the safe one."""
    for b in (branch, configured):
        if b in CHANNELS:
            return b
    return "main"


def source_file(channel: str) -> str:
    """`apps.json` for main (the URL people already have), `apps-<channel>.json`
    for the others."""
    return "apps.json" if channel == "main" else "apps-%s.json" % channel


def source_url(channel: str, repo: str = SOURCE_REPO) -> str:
    return "https://raw.githubusercontent.com/%s/main/%s" % (repo, source_file(channel))


def fallback_chain(channel: str) -> list[str]:
    """`channel` first, then every STEADIER channel. Never a fresher one: an
    alpha box may be told about the beta app, a main box never about alpha's."""
    if channel not in CHANNELS:
        channel = "main"
    return list(reversed(CHANNELS[:CHANNELS.index(channel) + 1]))


def source_urls(channel: str, repo: str = SOURCE_REPO) -> list[tuple[str, str]]:
    """`[(channel, url)]` to try in order; the first that exists wins."""
    return [(c, source_url(c, repo)) for c in fallback_chain(channel)]


def versions_of(source: dict, bundle_id: str = BUNDLE_ID) -> list[dict]:
    """The app's version entries out of a parsed source file, as published."""
    if not isinstance(source, dict):
        return []
    for a in source.get("apps") or []:
        if isinstance(a, dict) and a.get("bundleIdentifier") == bundle_id:
            return [v for v in (a.get("versions") or []) if isinstance(v, dict)]
    return []


def pick(versions: Iterable[dict], ceiling: str = "") -> Optional[dict]:
    """The newest entry that is not newer than `ceiling` (the server's own
    version). A box that has not updated yet must not send people to an app
    built for the version it is about to become. No ceiling, or one that does
    not parse, means no limit. Entries whose version does not parse are
    skipped."""
    top = parse_version(ceiling)
    best, best_v = None, None
    for e in versions:
        v = parse_version(e.get("version") if isinstance(e, dict) else None)
        if v is None or (top is not None and v > top):
            continue
        if best_v is None or v > best_v:
            best, best_v = e, v
    return best


def promotable(alpha_versions: Iterable[dict], target: str,
               have: Iterable[dict] = ()) -> Optional[dict]:
    """The app entry a channel should gain when the server version `target` is
    promoted to it: the newest app not newer than `target`. None when there is
    none, or when the channel already offers that one or a newer one (an app
    is never moved backwards; SideStore would not install it anyway)."""
    e = pick(alpha_versions, target)
    if e is None:
        return None
    cur = pick(have)
    if cur is not None and parse_version(cur.get("version")) >= parse_version(e.get("version")):
        return None
    return e


# What the changelog says when something shipped without being checked. These
# are this repo's own habits of speech (see CHANGELOG.md), not a general list.
_UNVERIFIED_RE = re.compile(
    r"not yet (?:checked|confirmed|verified|tested|run|seen)"
    r"|not (?:checked|confirmed|verified|tested) (?:on|in|against)"
    r"|verif(?:y|ication) pending|awaits? (?:a )?device|needs on-(?:box|device)"
    r"|unit tests only|untested",
    re.I)
_HEADING_RE = re.compile(r"^## \[(\d+\.\d+\.\d+)\]")


def unverified_claims(changelog: str, after: str, upto: str) -> list[tuple[str, str]]:
    """`[(version, sentence)]` for every changelog line that admits something
    was not checked, in versions newer than `after` and not newer than `upto`.
    This is what promotion puts in front of whoever is signing a build off:
    the changelog already records what nobody looked at, it only has to be
    read at the moment it matters. A continuation line is joined to the bullet
    it belongs to so the sentence reads whole."""
    lo, hi = parse_version(after), parse_version(upto)
    out: list[tuple[str, str]] = []
    ver, buf = None, ""

    def flush():
        nonlocal buf
        text = " ".join(buf.split())
        if ver and text and _UNVERIFIED_RE.search(text):
            out.append((ver, re.sub(r"^[-*]\s+", "", text)))
        buf = ""

    for line in (changelog or "").splitlines():
        m = _HEADING_RE.match(line)
        if m:
            flush()
            v = parse_version(m.group(1))
            inside = (lo is None or v > lo) and (hi is None or v <= hi)
            ver = m.group(1) if inside else None
            continue
        if ver is None:
            continue
        s = line.strip()
        if not s or s.startswith("#") or s[:2] in ("- ", "* "):
            flush()
            if s.startswith("#") or not s:
                continue
        buf += " " + s
    flush()
    return out


# ── Evidence for choosing a release candidate ────────────────────────────────
# `promote.py candidates` and `promote.py logs` print these. They only gather:
# which build is fit for main is a judgment (see .claude/skills/release-candidate).

_ENTRY_RE = re.compile(r"^## \[(\d+\.\d+\.\d+)\](?:\s*[—-]\s*(\d{4}-\d{2}-\d{2}))?")
_KIND_RE = re.compile(r"^[-*]\s+\*\*(New|Fixed|Changed|Removed)\b", re.I)


def bump_kind(prev: str, cur: str) -> str:
    """`x`, `y` or `z`: which part of the version moved between two entries."""
    a, b = parse_version(prev), parse_version(cur)
    if a is None or b is None:
        return "?"
    return "x" if b[0] != a[0] else "y" if b[1] != a[1] else "z"


def changelog_entries(changelog: str, after: str, upto: str) -> list[dict]:
    """One dict per changelog entry newer than `after` and not newer than
    `upto`, newest first: `{version, date, title, kind, new, fixed, changed,
    removed, fixes: [first line of each Fixed bullet]}`. `kind` is the bump
    from the entry below it in the file."""
    lo, hi = parse_version(after), parse_version(upto)
    entries: list[dict] = []
    cur = None
    for line in (changelog or "").splitlines():
        m = _ENTRY_RE.match(line)
        if m:
            cur = {"version": m.group(1), "date": m.group(2) or "", "title": "", "kind": "?",
                   "new": 0, "fixed": 0, "changed": 0, "removed": 0, "fixes": []}
            entries.append(cur)
            continue
        if cur is None:
            continue
        if line.startswith("### ") and not cur["title"]:
            cur["title"] = line[4:].strip()
            continue
        k = _KIND_RE.match(line.strip())
        if k:
            kind = k.group(1).lower()
            cur[kind] += 1
            if kind == "fixed":
                cur["fixes"].append(re.sub(r"\*\*", "", re.sub(r"^[-*]\s+", "", line.strip())))
    for i, e in enumerate(entries):
        e["kind"] = bump_kind(entries[i + 1]["version"], e["version"]) if i + 1 < len(entries) else "?"
    return [e for e in entries
            if (lo is None or parse_version(e["version"]) > lo)
            and (hi is None or parse_version(e["version"]) <= hi)]


_LOG_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+(DEBUG|INFO|WARNING|ERROR|CRITICAL)\s+\[([^\]]*)\]\s?(.*)$")
_BANNER_RE = re.compile(r"StreamLink v(\d+\.\d+\.\d+) starting\b.*?branch=(\S+)")


def _signature(logger: str, msg: str) -> str:
    """An error line with the parts that differ between occurrences removed,
    so the same failure counts once however many files it hit."""
    msg = re.sub(r"[0-9a-f]{8,}", "#", msg)
    msg = re.sub(r"(/|[A-Za-z]:\\)\S+", "<path>", msg)
    # A bare file name (they have spaces, so a path rule cannot catch them):
    # everything since the last "] " or ": " up to a media extension.
    msg = re.sub(r"(?:(?<=\] )|(?<=: )|^)(?:(?!\] ).)*?\.(?:mkv|mp4|m4v|avi|mov|ts|srt|ass|vtt|m3u8)\b",
                 "<file>", msg, flags=re.I)
    msg = re.sub(r"\d+", "#", msg)
    return ("[%s] %s" % (logger, msg))[:110]


def log_health(lines: Iterable[str]) -> list[dict]:
    """What a server log says about each version that ran, newest first:
    `{version, branch, runs, first, last, seconds, errors, tracebacks,
    signatures: {text: count}}`.

    The log is cut at each `StreamLink vX starting` banner; everything up to
    the next banner belongs to that run. `seconds` is the time between a run's
    first and last line, so a box that was switched off is not counted as
    having run. Lines before the first banner belong to no version and are
    dropped: an error that cannot be pinned to a build is not evidence about
    one."""
    from datetime import datetime
    out: dict[str, dict] = {}
    cur = None
    run_first = run_last = None

    def close():
        if cur is not None and run_first and run_last:
            cur["seconds"] += max(0, int((run_last - run_first).total_seconds()))

    for line in lines:
        if "Traceback (most recent call last)" in line and cur is not None:
            cur["tracebacks"] += 1
        m = _LOG_RE.match(line)
        if not m:
            continue
        stamp, level, logger, msg = m.groups()
        try:
            t = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        b = _BANNER_RE.search(msg)
        if b:
            close()
            cur = out.setdefault(b.group(1), {
                "version": b.group(1), "branch": b.group(2), "runs": 0, "first": stamp,
                "last": stamp, "seconds": 0, "errors": 0, "tracebacks": 0, "signatures": {}})
            cur["runs"] += 1
            cur["branch"] = b.group(2)
            run_first = run_last = t
            cur["last"] = max(cur["last"], stamp)
            continue
        if cur is None:
            continue
        run_last = t
        cur["last"] = max(cur["last"], stamp)
        if level in ("ERROR", "CRITICAL"):
            cur["errors"] += 1
            sig = _signature(logger, msg)
            cur["signatures"][sig] = cur["signatures"].get(sig, 0) + 1
    close()
    return sorted(out.values(), key=lambda r: parse_version(r["version"]), reverse=True)


def new_signatures(health: list[dict], version: str) -> list[tuple[str, int]]:
    """Error signatures seen under `version` and under NO older version in the
    same logs: the ones a build can be blamed for. An error every version has
    is the box's weather, not this build's doing."""
    v = parse_version(version)
    mine, older = {}, set()
    for r in health:
        rv = parse_version(r["version"])
        if rv == v:
            mine = r["signatures"]
        elif rv is not None and v is not None and rv < v:
            older.update(r["signatures"])
    return sorted(((s, n) for s, n in mine.items() if s not in older), key=lambda x: -x[1])
