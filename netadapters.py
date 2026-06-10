"""Network-adapter enumeration + preferred-adapter resolution.

Shared by the launcher (`run.py` — which LAN IP to advertise on mDNS / print)
and the dashboard (`main.py` — the middleware that bounces a non-preferred
adapter's IP to the chosen one). Kept dependency-light (psutil + stdlib only)
so it imports identically on Windows / Linux / macOS and under both the system
Python (run.py) and the venv (main.py).

The admin can pick which physical adapter the server should treat as the
*canonical* one. We never bind uvicorn to a single IP — it stays on 0.0.0.0 so
every adapter still answers — but everything that needs a single address (mDNS,
the printed URL, and the redirect target) resolves to the preferred adapter's
current IP, falling back to the route-table heuristic (and logging) when that
adapter is offline. See docs/RUNTIME.md and docs/GOTCHAS.md.
"""
from __future__ import annotations

import re
import socket
import time

# Interface *name* prefixes that are never a LAN adapter (VPN / loopback).
_VPN_STARTS = ("utun", "tun", "tap", "wg", "ppp", "lo")
# Substrings in an interface name that mark it virtual / VPN.
_VPN_SUBS = ("mullvad", "wireguard", "vpn", "virtual",
             "vmware", "vbox", "hyper-v", "loopback")
# Subnets owned by virtual adapters whose friendly names don't reveal them
# (e.g. VirtualBox host-only shows up as "Ethernet 2" on Windows).
_VIRTUAL_PREFIXES = (
    "192.168.56.",   # VirtualBox host-only default
    "192.168.99.",   # Docker Machine default
    "192.168.137.",  # Windows ICS / Mobile Hotspot default
    "169.254.",      # APIPA / link-local
)


def _is_lan(ip: str) -> bool:
    return (ip.startswith("192.168.") or
            ip.startswith("10.") or
            bool(re.match(r"^172\.(1[6-9]|2\d|3[01])\.", ip)))


def _is_virtual(ip: str) -> bool:
    return any(ip.startswith(p) for p in _VIRTUAL_PREFIXES)


def list_adapters() -> list[dict]:
    """Enumerate physical LAN adapters that currently hold a usable IPv4.

    Returns ``[{"name": <iface>, "ip": <ipv4>, "priority": <int>}]`` where a
    lower priority sorts first (192.168.* → 10.* → 172.16-31.*). VPN, loopback,
    and known-virtual adapters are filtered out by both name and subnet.
    """
    out: list[dict] = []
    try:
        import psutil
        for iface, addrs in psutil.net_if_addrs().items():
            n = iface.lower()
            if any(n.startswith(p) for p in _VPN_STARTS):
                continue
            if any(p in n for p in _VPN_SUBS):
                continue
            for addr in addrs:
                if addr.family != socket.AF_INET:
                    continue
                ip = addr.address
                if not _is_lan(ip) or _is_virtual(ip):
                    continue
                pr = 0 if ip.startswith("192.168.") else 1 if ip.startswith("10.") else 2
                out.append({"name": iface, "ip": ip, "priority": pr})
    except Exception:
        pass
    return out


def lan_ips(adapters: list[dict] | None = None) -> set[str]:
    """The set of all candidate LAN IPv4s across physical adapters."""
    adapters = adapters if adapters is not None else list_adapters()
    return {a["ip"] for a in adapters}


def _route_ip(candidate_ips: set[str]) -> str:
    """Whichever candidate the OS routing table would use to reach the internet.

    The UDP connect() sends no packet but sets the source IP to the interface
    that would route to 8.8.8.8 — useful on multi-NIC hosts. Gated to the
    candidate set so an active VPN that captured the default route can't win.
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        if ip in candidate_ips:
            return ip
    except Exception:
        pass
    return ""


def auto_ip(adapters: list[dict] | None = None) -> str:
    """The default LAN IP when no adapter is preferred (or it's offline).

    Route-table pick (gated to candidates) first, then priority-sorted fallback.
    """
    adapters = adapters if adapters is not None else list_adapters()
    if not adapters:
        return ""
    r = _route_ip({a["ip"] for a in adapters})
    if r:
        return r
    return sorted(adapters, key=lambda a: (a["priority"], a["ip"]))[0]["ip"]


def resolve_preferred(preferred_name: str,
                      adapters: list[dict] | None = None) -> tuple[str, bool, str]:
    """Resolve the preferred adapter to a current IP.

    Returns ``(ip, used_fallback, reason)``:
    - If ``preferred_name`` is set and that adapter is online with a LAN IPv4,
      returns that IP, ``used_fallback=False``.
    - If it's set but offline, falls back to :func:`auto_ip` and reports why so
      the caller can log it once.
    - If unset, returns the heuristic pick with ``used_fallback=False``.
    """
    adapters = adapters if adapters is not None else list_adapters()
    if preferred_name:
        for a in adapters:
            if a["name"] == preferred_name:
                return a["ip"], False, ""
        ip = auto_ip(adapters)
        return ip, True, (f"preferred network adapter '{preferred_name}' is not online — "
                          f"falling back to {ip or '(none available)'}")
    return auto_ip(adapters), False, ""


# ── Cached resolver for the per-request redirect middleware ─────────────────
# Scanning psutil on every HTTP request is wasteful; the adapter map changes
# rarely. Cache it with a short TTL.
_cache: dict = {"t": 0.0, "adapters": []}
_CACHE_TTL = 8.0


def _cached_adapters() -> list[dict]:
    now = time.time()
    if not _cache["adapters"] or (now - _cache["t"]) > _CACHE_TTL:
        _cache["adapters"] = list_adapters()
        _cache["t"] = now
    return _cache["adapters"]


def redirect_target(preferred_name: str, host_ip: str) -> str | None:
    """Given the bare IPv4 a client connected to, return the preferred IP to
    redirect it to — or ``None`` if no redirect is needed.

    Returns ``None`` when there's no preferred IP resolvable, when the client is
    already on the preferred adapter, or when ``host_ip`` isn't one of our own
    LAN adapter IPs (e.g. an unrelated address we shouldn't touch).
    """
    adapters = _cached_adapters()
    pref, _used_fallback, _reason = resolve_preferred(preferred_name, adapters)
    if not pref:
        return None
    ips = {a["ip"] for a in adapters}
    if host_ip in ips and host_ip != pref:
        return pref
    return None
