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
