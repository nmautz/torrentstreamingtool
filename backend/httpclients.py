"""The one way to build an outbound httpx.AsyncClient: `_http_client`.

Shares one SSL context so building a client costs ~0.5 ms instead of ~150 ms of
CPU on the event loop (see below). tests/test_http_clients.py fails on any other
`httpx.AsyncClient(` in main.py or backend/.
"""
import httpx


# ── Outbound HTTP clients ─────────────────────────────────────────────────────
# Constructing an `httpx.AsyncClient` builds a fresh SSL context and loads the
# whole certifi CA bundle into it — ~150 ms of synchronous CPU, on the event
# loop, even for a plain-http target like Jackett. A search built six of them,
# so every search froze the whole server for about a second, and four searches
# at once pushed /healthz past 7 s. Built once here and shared, construction
# is ~0.5 ms. Always go through `_http_client`, never `httpx.AsyncClient`
# directly (tests/test_http_clients.py enforces it).
_HTTPX_SSL = httpx.create_ssl_context()


def _http_client(**kw) -> httpx.AsyncClient:
    """An `httpx.AsyncClient` on the shared SSL context. An explicit
    `verify=` (e.g. False for an arbitrary user link) still wins."""
    kw.setdefault("verify", _HTTPX_SSL)
    return httpx.AsyncClient(**kw)
