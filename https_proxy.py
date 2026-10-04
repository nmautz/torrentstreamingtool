#!/usr/bin/env python3
"""
HTTPS-to-HTTP reverse proxy for StreamLink.

When SSL certs are present, the launcher binds port 443 with THIS app instead
of mounting `main:app` a second time. The result is a single canonical
FastAPI instance (on port 80) holding the only `AppState`; port 443 becomes
a pure transport layer that streams every request to 127.0.0.1:80 and
streams the response back.

Why this exists
---------------
Earlier versions ran two uvicorn servers in the same process, both pointing
at `main:app`, and relied on module-global state being shared. In practice
that gave subtle, intermittent sync glitches between clients connected via
`http://192.168.x.x` (port 80) and `https://remote.local` (port 443) — the
state would diverge for the duration of a stale SSE buffer or a startup
race, and reconcile only after the user refreshed. Funneling all traffic
through one app eliminates that entire failure class: there is provably
one place state lives.

Behaviour
---------
- Every HTTP method is proxied (GET, POST, PUT, DELETE, PATCH, OPTIONS, HEAD).
- Request bodies stream (`request.stream()`) so large uploads aren't buffered.
- Response bodies stream (`aiter_raw()`) so SSE / range requests / video
  downloads flow without buffering. Heartbeats reach the browser instantly.
- Hop-by-hop headers (RFC 7230 §6.1) are stripped on both directions.
- `X-Forwarded-Proto: https` is added so `admin_https_redirect` in main.py
  knows the original request was already secure (otherwise the upstream
  would 301 us into a loop).
- `X-Forwarded-Host` carries the original Host so redirects keep using
  `remote.local` instead of the upstream's `127.0.0.1:80`.

WebSockets are NOT proxied — `main:app` exposes none. If you add one,
revisit this file.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from http.cookiejar import CookieJar

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import Response, StreamingResponse

UPSTREAM = "http://127.0.0.1:80"

# Hop-by-hop headers (RFC 7230 §6.1) MUST NOT be forwarded by an intermediary.
# `host` is also stripped because httpx sets it from the upstream URL.
_HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
})
# `content-length` is stripped because httpx computes its own when sending
# a streaming body; sending two different values triggers an upstream 400.
_DROP_REQ = _HOP_BY_HOP | {"content-length", "host"}
_DROP_RESP = _HOP_BY_HOP


class _NoCookieJar(CookieJar):
    """A cookie jar that never stores or sends anything.

    **This is a security control, not an optimisation.** `httpx.AsyncClient`
    keeps a cookie jar by default, and this proxy shares ONE client across every
    request from every device on the LAN. So the jar made the proxy a
    session-laundering machine: the moment any response carried a `Set-Cookie`,
    httpx stored it and then attached it to *every subsequent request from
    everyone*.

    `POST /api/profiles/{id}/verify-pin` sets `streamlink_profile_token` as a
    cookie (deliberately not `Secure`, so one PIN entry covers the http and
    https origins of the same host). One person entering their PIN over HTTPS
    therefore silently elevated every other device on the network to that
    profile — observed directly: an unauthenticated `GET /api/profiles` over
    :443 came back with `verified_profile_id` set to the last profile that had
    verified, while the same request to :80 correctly returned `null`. That
    token is what gates **admin-locked content and the delete endpoints**, so
    any LAN client could see locked items and delete library entries.

    A proxy must be transparent about credentials: it forwards whatever the
    client sent (the `Cookie` request header is passed through untouched below)
    and remembers nothing of its own.

    Subclassing `http.cookiejar.CookieJar` rather than `httpx.Cookies` is
    load-bearing. `httpx.Cookies.__init__` REBUILDS its state from what you hand
    it — a `Cookies` instance is copied cookie-by-cookie into a fresh jar, so a
    `Cookies` subclass is silently discarded and the leak survives (verified: it
    did). Only the `else` branch keeps the object verbatim (`self.jar = cookies`),
    and that branch wants a raw `CookieJar`. Both `Cookies.set_cookie_header` and
    `Cookies.extract_cookies` then delegate straight to `jar.add_cookie_header` /
    `jar.extract_cookies`, which is why overriding exactly those two is enough.
    """

    def add_cookie_header(self, request) -> None:            # send nothing
        return None

    def extract_cookies(self, response, request) -> None:    # store nothing
        return None


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # `timeout=None` — SSE / long-poll keep the connection open indefinitely.
    app.state.client = httpx.AsyncClient(
        base_url=UPSTREAM,
        timeout=None,
        follow_redirects=False,
        cookies=_NoCookieJar(),
    )
    try:
        yield
    finally:
        await app.state.client.aclose()


app = FastAPI(
    title="StreamLink HTTPS Proxy",
    lifespan=_lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


@app.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"],
)
async def proxy(path: str, request: Request) -> Response:
    client: httpx.AsyncClient = request.app.state.client

    # Forward headers as raw byte pairs so duplicates (e.g. multiple
    # Set-Cookie on the response) survive — request side rarely has dupes
    # but doing both sides the same way keeps the code symmetric.
    fwd_headers: list[tuple[bytes, bytes]] = []
    for raw_name, raw_value in request.headers.raw:
        if raw_name.decode("latin-1").lower() in _DROP_REQ:
            continue
        fwd_headers.append((raw_name, raw_value))

    # Tell the upstream the original request was HTTPS so the
    # `admin_https_redirect` middleware doesn't bounce us into a redirect
    # loop, and keep the original Host so any redirect URL it emits points
    # back at the public name (remote.local) rather than 127.0.0.1.
    fwd_headers.append((b"x-forwarded-proto", b"https"))
    orig_host = request.headers.get("host", "")
    if orig_host:
        fwd_headers.append((b"x-forwarded-host", orig_host.encode("latin-1")))
    client_ip = request.client.host if request.client else ""
    if client_ip:
        fwd_headers.append((b"x-forwarded-for", client_ip.encode("latin-1")))

    upstream_req = client.build_request(
        method=request.method,
        url=httpx.URL(
            path=f"/{path}",
            query=request.url.query.encode("utf-8"),
        ),
        headers=fwd_headers,
        content=request.stream(),
    )

    try:
        upstream_resp = await client.send(upstream_req, stream=True)
    except (httpx.ConnectError, httpx.RemoteProtocolError) as exc:
        return Response(
            content=f"StreamLink upstream unavailable: {exc}".encode("utf-8"),
            status_code=502,
            media_type="text/plain",
        )

    # Kept as a LIST of raw pairs, not a dict. The previous comment here said
    # Set-Cookie never repeats "because the admin auth flow carries its token in
    # headers/body" — that stopped being true when `verify-pin` began setting
    # `streamlink_profile_token` as a cookie, and a dict silently drops all but
    # the last of any repeated header. One cookie per response survives either
    # way, which is exactly why this would have gone unnoticed until the day a
    # response set two.
    resp_raw: list[tuple[bytes, bytes]] = [
        (raw_name, raw_value)
        for raw_name, raw_value in upstream_resp.headers.raw
        if raw_name.decode("latin-1").lower() not in _DROP_RESP
    ]

    async def body_iter():
        try:
            async for chunk in upstream_resp.aiter_raw():
                yield chunk
        finally:
            await upstream_resp.aclose()

    resp = StreamingResponse(
        body_iter(),
        status_code=upstream_resp.status_code,
    )
    # Starlette builds `raw_headers` from the `headers` dict, so assign after
    # construction to keep duplicates intact.
    resp.raw_headers = resp_raw
    return resp
