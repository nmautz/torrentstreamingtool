"""Jackett's admin session: the login cookie its UI API needs.

The Torznab API key covers search only; managing indexers needs a logged-in
cookie. `_jackett_admin()` hands out a client carrying a fresh one, logging in
under `runtime._jackett_cookie_lock` when it has expired. The cookie and its
expiry are reassigned on each login, so they are read only here.
"""
import time
from contextlib import asynccontextmanager

from fastapi import HTTPException

from backend import runtime
from backend.config import settings
from backend.httpclients import _http_client


# ── Jackett Session ───────────────────────────────────────────────────────────
_jackett_cookie: str = ""
_jackett_cookie_expiry: float = 0.0
# The cookie's lock is runtime._jackett_cookie_lock (built in the lifespan).


async def _jackett_login() -> str:
    """Login to Jackett and return the session cookie value."""
    async with _http_client(follow_redirects=True, timeout=10.0) as tmp:
        await tmp.get(f"{settings.indexer_url}/UI/Login")
        r = await tmp.post(
            f"{settings.indexer_url}/UI/Dashboard",
            data={"password": settings.jackett_password},
            follow_redirects=False,
        )
    cookie = r.cookies.get("Jackett")
    if not cookie:
        raise HTTPException(502, "Could not authenticate with Jackett — check JACKETT_PASSWORD in .env")
    return cookie


@asynccontextmanager
async def _jackett_admin(timeout: float = 15.0):
    """Yield an httpx client authenticated to Jackett's admin API. Testing an
    indexer passes `ixwait.ADMIN`: Jackett runs a full search before it answers,
    and behind Cloudflare that took 45 s (see ixwait.py). Saving one does too,
    because a tracker with a login is logged into before Jackett answers."""
    global _jackett_cookie, _jackett_cookie_expiry
    cookies: dict[str, str] = {}
    if settings.jackett_password:
        async with runtime._jackett_cookie_lock:
            if not _jackett_cookie or time.time() >= _jackett_cookie_expiry:
                _jackett_cookie = await _jackett_login()
                _jackett_cookie_expiry = time.time() + 3600
        cookies = {"Jackett": _jackett_cookie}
    async with _http_client(cookies=cookies, timeout=timeout) as c:
        yield c
