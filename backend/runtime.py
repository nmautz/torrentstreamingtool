"""Objects the server builds when it starts, not when it is imported.

The lifespan in main.py assigns each of these once (`vlc_client` is also rebuilt
by `_vlc_http` if it closes). They live here, and are always read through the
module (`runtime.qbit`, never `from backend.runtime import qbit`), so code in
any module sees the object the lifespan built rather than the None it was at
import. tests/test_backend_layout.py refuses a `from` import of anything a module
rebinds; this module is the deliberate home for exactly that kind of name.
"""
import asyncio
from typing import Optional

import httpx

import diagnostics as diag

# The main asyncio loop, captured at lifespan startup. Needed by code running
# on foreign threads (the HID input listener) to schedule coroutines back onto
# the loop via run_coroutine_threadsafe.
_MAIN_LOOP: Optional[asyncio.AbstractEventLoop] = None
qbit: Optional[httpx.AsyncClient] = None
vlc_client: Optional[httpx.AsyncClient] = None   # persistent keep-alive client for VLC (see _vlc_http)

_lib_lock: 'diag.InstrumentedLock'  # initialised in lifespan; wraps an asyncio.Lock
_analysis_gate: asyncio.Semaphore  # initialised in lifespan; ANALYSIS_CONCURRENCY slots
_jackett_cookie_lock: asyncio.Lock  # initialised in lifespan
