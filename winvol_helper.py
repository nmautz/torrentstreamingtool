"""Out-of-process Windows OS-mixer volume helper.

This runs in a **child process** so that a native COM/pycaw access violation can
only ever kill THIS process — never the StreamLink server. The parent (main.py)
spawns one long-lived instance, talks to it over stdin/stdout (one JSON line per
request/response), and respawns it transparently if it dies. See docs/GOTCHAS.md.

Why a separate process at all: in-process pycaw was crashing the host. Driving
the Windows endpoint volume through COM from inside the server process produced
a native access violation (no Python traceback, the whole process just vanished)
after a handful of rapid calls — and it crashed even when pinned to a single
COM-initialized thread that never CoUninitialized. The only robust containment
for a native crash is an OS process boundary: here, a crash returns a non-zero
exit / closed pipe that the parent simply detects and recovers from.

Protocol (one UTF-8 JSON line each way, newline-terminated):
  parent -> child:  {"op": "get"}              child -> parent: {"ok": true, "value": 73}
  parent -> child:  {"op": "set", "pct": 50}   child -> parent: {"ok": true}
  on any error:                                 child -> parent: {"ok": false, "error": "..."}

COM is initialized ONCE at startup and never uninitialized; everything runs on
this process's main thread. That's the canonical-safe pycaw usage pattern, and
since it's isolated here, a failure can't take the server down regardless.
"""
from __future__ import annotations

import json
import sys


def _reply(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _endpoint():
    """Build a fresh IAudioEndpointVolume for the current default render device.

    Rebuilt per request (cheap, in-process) so a default-device change (e.g. the
    user switches output to headphones) is always reflected. Raises ImportError
    with a useful message if pycaw/comtypes aren't installed."""
    from ctypes import cast, POINTER
    import comtypes
    from comtypes import CLSCTX_ALL
    from pycaw.api.endpointvolume import IAudioEndpointVolume
    from pycaw.api.mmdeviceapi import IMMDeviceEnumerator
    try:
        from pycaw.constants import CLSID_MMDeviceEnumerator, EDataFlow, ERole
        e_render = int(EDataFlow.eRender.value)
        e_multimedia = int(ERole.eMultimedia.value)
    except Exception:
        from comtypes import GUID
        CLSID_MMDeviceEnumerator = GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}")
        e_render, e_multimedia = 0, 1
    device_enum = comtypes.CoCreateInstance(
        CLSID_MMDeviceEnumerator, IMMDeviceEnumerator, comtypes.CLSCTX_INPROC_SERVER,
    )
    speakers = device_enum.GetDefaultAudioEndpoint(e_render, e_multimedia)
    interface = speakers.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
    return cast(interface, POINTER(IAudioEndpointVolume))


def main() -> None:
    # Initialize COM once for the life of the process. Never CoUninitialize.
    init_error = None
    try:
        import comtypes
        comtypes.CoInitialize()
    except Exception as e:  # noqa: BLE001 — report, don't crash
        init_error = f"COM init failed: {type(e).__name__}: {e}"

    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            req = json.loads(raw)
        except Exception:
            continue
        if init_error:
            _reply({"ok": False, "error": init_error})
            continue
        op = req.get("op")
        try:
            if op == "get":
                vol = _endpoint()
                pct = max(0, min(100, round(vol.GetMasterVolumeLevelScalar() * 100)))
                _reply({"ok": True, "value": pct})
            elif op == "set":
                pct = max(0, min(100, int(req.get("pct", 0))))
                vol = _endpoint()
                vol.SetMute(0, None)
                vol.SetMasterVolumeLevelScalar(pct / 100.0, None)
                _reply({"ok": True})
            elif op == "ping":
                _reply({"ok": True})
            else:
                _reply({"ok": False, "error": f"unknown op {op!r}"})
        except ImportError as e:
            _reply({"ok": False, "error": (
                f"Windows volume control dep is not installed in the server venv "
                f"({e}). Run `pip install -r requirements.txt` (or re-run setup.py) "
                "and restart the StreamLink service."
            )})
        except Exception as e:  # noqa: BLE001 — report per-request, keep serving
            _reply({"ok": False, "error": f"{type(e).__name__}: {e}"})


if __name__ == "__main__":
    main()
