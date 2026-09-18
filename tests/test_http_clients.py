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

# Files whose clients are built per call on the server's event loop, and how
# many direct constructions each may keep. (https_proxy.py builds its one client
# at startup, so it isn't listed.)
FILES = {"main.py": 1}   # the one inside _http_client itself

_CALL = re.compile(r"\bhttpx\.AsyncClient\(")

fails = []
for name, allowed in FILES.items():
    with open(os.path.join(ROOT, name), encoding="utf-8") as f:
        lines = f.read().splitlines()
    hits = [(i + 1, ln.strip()) for i, ln in enumerate(lines)
            if _CALL.search(ln) and not ln.lstrip().startswith(("#", "`"))]
    if len(hits) != allowed:
        fails.append("%s: %d httpx.AsyncClient( call(s), expected %d - use _http_client(...)\n%s"
                     % (name, len(hits), allowed,
                        "\n".join("     %d: %s" % h for h in hits)))

print("http_clients: %d passed, %d failed" % (len(FILES) - len(fails), len(fails)))
for f in fails:
    print("  FAIL " + f)
sys.exit(1 if fails else 0)
