"""Shared VPN kill-switch verification.

Three entry points enforce the kill-switch and must agree on *how* to verify the
VPN: `main.py`'s in-app `vpn_guard`, `run.py`'s startup check, and `watchdog.py`'s
process-level qBit gate. `run.py`/`watchdog.py` deliberately don't import
`main.py`, so the shared, dependency-light logic lives here (stdlib + optional
psutil only) and all three import it.

The mode is persisted in `library.json` → `settings.vpn_killswitch.mode`:

  * ``"mullvad"`` (default) — require the Mullvad CLI to report ``Connected``.
    This is the original behaviour; keeps the app safe out of the box.
  * ``"generic"``          — provider-agnostic: require any VPN-style tunnel
    interface to be up (WireGuard, OpenVPN, NordLynx, …). Lets people who don't
    use Mullvad still get the kill-switch, without a vendor CLI.
  * ``"off"``              — kill-switch disabled; treat as always connected. For
    users who accept the risk (e.g. they run a VPN at the router). The dashboard
    shows a subtle "VPN off" pill instead of the red overlay.

Only the mode-reading and the generic tunnel check live here. Each caller keeps
its own Mullvad-binary discovery (they already had per-platform paths + `.env`
`_MULLVAD_BIN` overrides) so this module stays free of their env-loading quirks.
"""

from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent

VALID_MODES = ("mullvad", "generic", "off")

# Interface-name tokens that indicate a VPN tunnel. Matched case-insensitively as
# substrings so both POSIX device names (utun3, wg0, tun0, ppp0) and Windows
# adapter descriptions (e.g. "Mullvad", "WireGuard Tunnel", "OpenVPN TAP") hit.
_TUNNEL_TOKENS = (
    "tun", "utun", "wg", "wireguard", "tap", "ppp",
    "nordlynx", "proton", "mullvad", "vpn",
)


_KEY = b'"vpn_killswitch"'
_mode_memo: tuple = (None, None)       # (file key, raw mode or None) of the last read


def _norm(mode, default: str) -> str:
    mode = str(mode or default).strip().lower()
    return mode if mode in VALID_MODES else default


def _killswitch_mode(raw: bytes):
    """`settings.vpn_killswitch.mode` out of library.json's bytes, or None.

    Reads only the small object after the key, never the whole 7.6 MB file:
    the watchdog asks every 3 s from a thread of the server process, and a full
    `json.loads` holds the GIL for its whole run, which stopped the event loop
    for 0.1 s each time (20.25.24). If the key is there more than once and the
    copies disagree, the full parse decides."""
    dec = json.JSONDecoder()
    found = []
    i = raw.find(_KEY)
    while i != -1:
        j = raw.find(b":", i + len(_KEY))
        if j == -1:
            break
        chunk = raw[j + 1:j + 1 + 4096].decode("utf-8", "ignore").lstrip()
        try:
            obj, _ = dec.raw_decode(chunk)
        except ValueError:
            obj = None
        if isinstance(obj, dict):
            found.append(obj.get("mode"))
        i = raw.find(_KEY, i + len(_KEY))
    if len(set(map(str, found))) > 1:
        data = json.loads(raw.decode("utf-8"))
        return ((data.get("settings", {}) or {}).get("vpn_killswitch") or {}).get("mode")
    return found[0] if found else None


def vpn_mode(default: str = "mullvad") -> str:
    """Read `settings.vpn_killswitch.mode` from library.json (best effort).

    Falls back to `default` ("mullvad" — the safe, historical behaviour) if the
    file/key is missing or the value isn't one we recognise. Read-only, and only
    re-read when the file's mtime or size changed.
    """
    global _mode_memo
    path = HERE / "library.json"
    try:
        st = path.stat()
        key = (st.st_mtime_ns, st.st_size)
        if _mode_memo[0] != key:
            _mode_memo = (key, _killswitch_mode(path.read_bytes()))
        return _norm(_mode_memo[1], default)
    except Exception:
        return default


def generic_tunnel_up() -> bool:
    """Return True if a VPN-style tunnel interface is present and up.

    Provider-agnostic best-effort check for the "generic" mode. Uses psutil to
    enumerate interfaces; if psutil is unavailable we can't verify, so we return
    False (absent proof = treat as unsafe, matching the Mullvad path's stance).
    """
    try:
        import psutil
    except Exception:
        return False
    try:
        stats = psutil.net_if_stats()
    except Exception:
        return False
    for name, st in stats.items():
        if not getattr(st, "isup", False):
            continue
        low = name.lower()
        if low.startswith("lo") or "loopback" in low:
            continue
        if any(tok in low for tok in _TUNNEL_TOKENS):
            return True
    return False
