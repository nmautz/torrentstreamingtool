"""Who may do what: admin sessions and profile authorisation.

`_admin_sessions` (token -> expiry) is filled by POST /api/admin/login and
only ever mutated, never reassigned, so main.py's login route shares the same
dict. `_check_admin` / `_require_admin` gate the admin API; the profile helpers
decide what a PIN-verified (or admin) caller may see and change.
"""
import hashlib
import math
import time
from typing import Optional

from fastapi import HTTPException, Request

from backend.config import settings
from backend.sessions import _profile_session_id


_admin_sessions: dict[str, float] = {}   # token → expiry Unix timestamp
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


# ── Profile authorisation ─────────────────────────────────────────────────────

def _is_elevated(request: Request, lib: dict, profile_id: str = "") -> bool:
    """True if this request may see admin-locked ("content lock") items.

    Requires BOTH that the profile is marked `elevated` AND that the request
    carries a PIN-verified session token for it (`_profile_session_id`). Claiming
    the id is not enough — see `_profile_sessions` for why. An admin session is a
    superset and passes on its own.

    A profile with no PIN can never be elevated in practice: there is nothing to
    verify, so no token is ever issued for it. That is deliberate — an elevated
    profile anyone can select by name isn't a lock.
    """
    if _check_admin(request):
        return True
    verified = _profile_session_id(request)
    if not verified or (profile_id and verified != profile_id):
        return False
    prof = next((p for p in lib.get("profiles", []) if p.get("id") == verified), None)
    return bool(prof and prof.get("elevated"))


def _require_delete_auth(request: Request, lib: dict) -> None:
    """Gate the endpoints that destroy content — library items, their files, a
    profile (and its watch history), a configured library path.

    All four were completely unauthenticated: anyone who could reach the LAN could
    `DELETE /api/library/{id}` and, since `delete_file` defaults to true, take the
    media off disk with it. The dashboard (not just /admin) offers these, so they
    can't simply be admin-only — a PIN-verified profile is the household's own
    proof of identity, and an admin session still passes.
    """
    if _check_admin(request):
        return
    verified = _profile_session_id(request)
    if verified and any(p.get("id") == verified for p in lib.get("profiles", [])):
        return
    raise HTTPException(
        403, "Deleting requires a PIN-verified profile or the admin password.")


def _finite(value: float, default: float = 0.0) -> float:
    """Coerce a float that reached us from the network into a usable number.

    Pydantic accepts NaN and ±Infinity for a plain `float` field (`allow_inf_nan`
    defaults on), and neither survives contact with the rest of the system:

    * NaN defeats comparison-based clamping. `max(0.0, min(100.0, nan))` is
      **100.0**, not 0 — `nan < 100.0` is False, so min keeps its first argument.
      A seek to NaN therefore clamped to *the end of the episode*, which also
      marks it watched and rolls on to the next one. The dashboard could produce
      exactly that on its own: the seek bar computes
      `(clientX - rect.left) / rect.width`, and a zero-width bar (tapped before
      layout settles) divides by zero.
    * A non-finite number that reaches `library.json` is worse, because it is
      persistent. `json.dumps` writes a bare `NaN` token, which is not valid JSON;
      Starlette's JSONResponse sets `allow_nan=False` and raises on it. One bad
      progress POST could therefore make `/api/library` fail for everyone, every
      time, until the value was edited out by hand.

    Anything not finite becomes `default`. Callers still clamp for range — this
    only guarantees the value is a real number first.
    """
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return v if math.isfinite(v) else default


def _pin_hash(pin: str) -> str:
    return hashlib.sha256(pin.encode()).hexdigest()
