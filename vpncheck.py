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


def vpn_mode(default: str = "mullvad") -> str:
    """Read `settings.vpn_killswitch.mode` from library.json (best effort).

    Falls back to `default` ("mullvad" — the safe, historical behaviour) if the
    file/key is missing or the value isn't one we recognise. A read-only
    `json.load` is fine: the launcher is the only startup writer and these poll
    loops only read.
    """
    try:
        data = json.loads((HERE / "library.json").read_text(encoding="utf-8"))
        ks = (data.get("settings", {}) or {}).get("vpn_killswitch") or {}
        mode = str(ks.get("mode", default) or default).strip().lower()
        return mode if mode in VALID_MODES else default
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
