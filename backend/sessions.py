"""Profile session tokens: proof a client entered a profile's PIN.

Issued by POST /api/profiles/{id}/verify-pin, persisted to
profile_sessions.json, checked by `_profile_session_id` and the content lock.
`_profile_sessions` is reassigned when the file is loaded, so it is read only
here, never imported.
"""
import atexit
import json
import secrets
import threading
import time
from typing import Optional

from fastapi import Request

from backend.logs import log
from backend.paths import ROOT


# ── Profile session tokens ────────────────────────────────────────────────────
# Proof that a client actually entered a profile's PIN, issued by
# POST /api/profiles/{id}/verify-pin. Map: token → {profile_id, expires}.
#
# `profile_id` alone was never proof of anything: it is a plain string the client
# sends, and GET /api/profiles hands out every UUID unauthenticated. The content
# lock read `is_elevated = profile_id in elevated_ids`, so quoting an elevated
# profile's UUID revealed admin-locked items with no PIN ever entered — the PIN
# screen was pure client-side decoration.
#
# In-memory and short-lived by design: losing them on restart just means the
# household re-enters a PIN, which is the correct failure direction.
#
# Persisted, unlike _admin_sessions: this box reboots nightly (scheduled restart)
# and the auto-updater restarts it too, and a viewer silently losing content-lock
# access and the ability to delete — with only a 403 to explain it — every morning
# is worse than the tokens outliving a process. The TTL still forces a periodic
# re-entry, which is what actually bounds them.
PROFILE_SESSIONS_FILE = ROOT / "profile_sessions.json"
_profile_sessions: dict[str, dict] = {}
PROFILE_SESSION_TTL = 12 * 3600          # 12 h — a viewing session, not a login
# A REMEMBERED session (20.0.0). The iOS app pins one account to the phone and
# opens straight into it; asking that person for their PIN twice a day on their
# own phone is the app being forgetful, not careful. verify-pin issues one of
# these only when asked (`remember`), and the app asks only for the pinned
# account. It slides: used within its life, it is extended, so a phone in use is
# never asked again, and one left in a drawer for six months is.
# What still ends it: changing or clearing the profile's PIN (see
# _revoke_profile_sessions), deleting the profile, or deleting profile_sessions.json.
PROFILE_REMEMBER_TTL = 180 * 86400
PROFILE_REMEMBER_RENEW_AFTER = 7 * 86400   # extend at most weekly: each one is a file write


def _load_profile_sessions() -> None:
    global _profile_sessions
    try:
        if PROFILE_SESSIONS_FILE.exists():
            data = json.loads(PROFILE_SESSIONS_FILE.read_text(encoding="utf-8"))
            now = time.time()
            if isinstance(data, dict):
                _profile_sessions = {
                    k: v for k, v in data.items()
                    if isinstance(v, dict) and float(v.get("expires", 0)) > now
                }
    except Exception as e:      # a corrupt file must not break startup
        log.warning("Could not load profile sessions: %s", e)
        _profile_sessions = {}


# The file is written by a thread of its own (20.30.1). Entering a PIN wrote it
# on the event loop, and with the disk busy that held the loop 0.32 s. The dict is
# serialised by the caller (it is small, and only the loop changes it); the
# thread writes the newest copy, so a burst of changes is one write. A copy still
# waiting at exit is written by `_flush_profile_sessions`.
_sessions_pending: Optional[str] = None
_sessions_cv = threading.Condition()
_sessions_io = threading.Lock()                # one write at a time
_sessions_writer: Optional[threading.Thread] = None


def _write_profile_sessions(blob: str) -> None:
    with _sessions_io:
        try:
            PROFILE_SESSIONS_FILE.write_text(blob, encoding="utf-8")
        except Exception as e:
            log.warning("Could not persist profile sessions: %s", e)


def _take_pending_sessions() -> Optional[str]:
    global _sessions_pending
    with _sessions_cv:
        blob, _sessions_pending = _sessions_pending, None
    return blob


def _sessions_writer_main() -> None:
    while True:
        with _sessions_cv:
            while _sessions_pending is None:
                _sessions_cv.wait()
        blob = _take_pending_sessions()
        if blob is not None:
            _write_profile_sessions(blob)


@atexit.register
def _flush_profile_sessions() -> None:
    blob = _take_pending_sessions()
    if blob is not None:
        _write_profile_sessions(blob)


def _save_profile_sessions() -> None:
    global _sessions_pending, _sessions_writer
    try:
        blob = json.dumps(_profile_sessions, indent=2)
    except Exception as e:
        log.warning("Could not persist profile sessions: %s", e)
        return
    with _sessions_cv:
        _sessions_pending = blob
        if _sessions_writer is None:
            _sessions_writer = threading.Thread(
                target=_sessions_writer_main, name="sessions-writer", daemon=True)
            _sessions_writer.start()
        _sessions_cv.notify()


def _new_profile_session(profile_id: str, remember: bool = False) -> str:
    """Mint a PIN-verified session token for `profile_id` (and reap expired ones).
    `remember` issues the long, sliding kind - see PROFILE_REMEMBER_TTL."""
    now = time.time()
    for t, meta in list(_profile_sessions.items()):
        if meta["expires"] <= now:
            _profile_sessions.pop(t, None)
    token = secrets.token_hex(32)
    meta = {"profile_id": profile_id,
            "expires": now + (PROFILE_REMEMBER_TTL if remember else PROFILE_SESSION_TTL)}
    if remember:
        meta["remember"] = True
    _profile_sessions[token] = meta
    _save_profile_sessions()
    return token


def _revoke_profile_sessions(profile_id: str) -> int:
    """End every session proved with `profile_id`'s PIN. Called when that PIN is
    changed or cleared: a token is proof the OLD PIN was known, and since 20.0.0
    one can live for months."""
    dead = [t for t, meta in _profile_sessions.items() if meta.get("profile_id") == profile_id]
    for t in dead:
        _profile_sessions.pop(t, None)
    if dead:
        _save_profile_sessions()
    return len(dead)


PROFILE_TOKEN_COOKIE = "streamlink_profile_token"


def _profile_token_of(request: Request) -> str:
    return (request.headers.get("x-profile-token", "").strip()
            or request.cookies.get(PROFILE_TOKEN_COOKIE, "").strip()
            or request.query_params.get("profile_token", "").strip())


def _profile_session_id(request: Request) -> Optional[str]:
    """The profile id this request has PIN-proved, or None.

    Read from a cookie as well as the header. The header alone was origin-bound:
    the dashboard keeps the token in localStorage, and `http://<host>` and
    `https://<host>` are *different origins* with separate localStorage — so a PIN
    entered on one scheme left the other unelevated (admin-locked content simply
    absent, with the profile still logged in and nothing explaining why). A cookie
    without the `Secure` flag is scoped to the host and ignores both scheme and
    port, so one PIN entry now covers http and https alike.
    """
    tok = _profile_token_of(request)
    if not tok:
        return None
    meta = _profile_sessions.get(tok)
    if not meta:
        return None
    now = time.time()
    if now > meta["expires"]:
        _profile_sessions.pop(tok, None)
        return None
    # A remembered session slides. Extended at most weekly, so the file is not
    # rewritten on every request.
    if meta.get("remember") and meta["expires"] - now < PROFILE_REMEMBER_TTL - PROFILE_REMEMBER_RENEW_AFTER:
        meta["expires"] = now + PROFILE_REMEMBER_TTL
        _save_profile_sessions()
    return meta["profile_id"]


def _keep_profile_session(request: Request, profile_id: str) -> bool:
    """Turn the caller's live session for `profile_id` into a remembered one.

    The iOS app pins an account that is ALREADY signed in: at the 20.0.0 upgrade,
    and whenever "Make X this phone's account" is used. Nobody is asked for a
    PIN at that moment, so the session it holds is the 12 h kind and the phone
    asks again the next morning (20.2.2). Needs a session that is still valid,
    so it grants nothing the PIN did not already grant - only for longer, which
    verify-pin's `remember` hands to anyone who knows the PIN anyway."""
    if _profile_session_id(request) != profile_id:
        return False
    meta = _profile_sessions.get(_profile_token_of(request))
    if not meta:
        return False
    meta["remember"] = True
    meta["expires"] = time.time() + PROFILE_REMEMBER_TTL
    _save_profile_sessions()
    return True
