"""Guard: no per-call `httpx.AsyncClient(...)` in the server. Run with plain
python, no deps:

    python tests/test_http_clients.py      (or `make test`)

Building an AsyncClient loads the certifi CA bundle into a fresh SSL context,
~150 ms of synchronous CPU on the event loop. One search used to build six and
freeze the whole server for a second. Every construction must go through
`main._http_client`, which reuses one shared context. This scans the source
text rather than importing main, so it needs no venv.
"""

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The server's event-loop code: main.py and the backend/ package it is being
# split into. Between them they may keep exactly one direct construction, the
# one inside _http_client itself, wherever that lives. (https_proxy.py builds
# its one client at startup, so it isn't scanned.)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _backend_src                       # noqa: E402
ALLOWED = 1

_CALL = re.compile(r"\bhttpx\.AsyncClient\(")

fails = []
hits = []
for path in _backend_src.files():
    with open(path, encoding="utf-8") as f:
        lines = f.read().splitlines()
    hits += [(os.path.relpath(path, ROOT), i + 1, ln.strip()) for i, ln in enumerate(lines)
             if _CALL.search(ln) and not ln.lstrip().startswith(("#", "`"))]
if len(hits) != ALLOWED:
    fails.append("%d httpx.AsyncClient( call(s), expected %d - use _http_client(...)\n%s"
                 % (len(hits), ALLOWED, "\n".join("     %s:%d: %s" % h for h in hits)))

print("http_clients: %d passed, %d failed" % (1 - len(fails), len(fails)))
for f in fails:
    print("  FAIL " + f)
sys.exit(1 if fails else 0)
