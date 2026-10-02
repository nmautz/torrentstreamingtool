"""Being found by the app, instead of being typed into it.

The iOS app used to ask for the host's address. It now lists the servers it can
see, and this module is the host's half of that: who this box says it is, and
every address it can be reached at.

Three questions, kept apart because they have different answers:

* **Which box is this?** (`server_id`) A random id written once to `.server_id`
  beside the code. The app remembers it, so a box that comes back on a different
  address (a new DHCP lease, home Wi-Fi vs. Tailscale) is recognised as the same
  box and not offered as a stranger. An id that cannot be persisted is reported
  as "" rather than invented per run: an id that changes on restart would make
  the app follow nothing, and "" says so honestly.
* **What is it called?** (`server_name`) The machine's hostname. It is a label
  for a person, never an identity.
* **Where does it answer?** (`addresses`) The LAN address first, then any
  **Tailscale** address (`100.64.0.0/10`). mDNS does not cross a tailnet and the
  phone has no way to list tailnet machines, so the only way the app can learn
  the 100.x address is to be told it while it can still see the box some other
  way. `netadapters` deliberately drops VPN interfaces by name; Tailscale is
  picked out here by **subnet**, which is the same on Windows ("Tailscale"),
  Linux (`tailscale0`) and macOS (`utunN`).

Leaf module: stdlib + optional psutil, no `main` import. Wired in `main.py`
(`GET /api/discovery`) and `run.py` (`start_mdns`, the `_streamlink._tcp`
service). The other half is `ios-app/ios/App/App/ServerDiscovery.swift`.
See docs/RUNTIME.md § Discovery.
"""
from __future__ import annotations

import ipaddress
import re
import socket
import uuid
from pathlib import Path

# The Bonjour type the app browses for. Its own type rather than a name filter
# over `_http._tcp`, which every printer and router on the network also answers.
SERVICE_TYPE = "_streamlink._tcp.local."
ID_FILE = ".server_id"

_ID_RE = re.compile(r"^[0-9a-f]{16}$")
_CGNAT = ipaddress.ip_network("100.64.0.0/10")
# Tailscale's own resolver lives inside the range and is not a machine.
_TAILSCALE_DNS = "100.100.100.100"


def server_id(root) -> str:
    """This install's stable id, created on first use. "" if it can't be kept."""
    path = Path(root) / ID_FILE
    try:
        got = path.read_text(encoding="ascii", errors="replace").strip()
        if _ID_RE.match(got):
            return got
    except OSError:
        pass
    new = uuid.uuid4().hex[:16]
    try:
        path.write_text(new + "\n", encoding="ascii")
    except OSError:
        return ""
    return new


def server_name(hostname: str | None = None) -> str:
    """A label for the server list: the hostname, without its domain."""
    if hostname is None:
        try:
            hostname = socket.gethostname()
        except OSError:
            hostname = ""
    name = (hostname or "").strip().split(".")[0]
    name = re.sub(r"[^\w \-]", "", name, flags=re.ASCII).strip()
    return name[:40] or "StreamLink"


def is_tailscale(ip: str) -> bool:
    try:
        return ip != _TAILSCALE_DNS and ipaddress.ip_address(ip) in _CGNAT
    except ValueError:
        return False


def tailscale_ips(if_addrs=None) -> list[str]:
    """IPv4 addresses this machine holds on a tailnet, sorted.

    `if_addrs` is `psutil.net_if_addrs()`'s shape and is only passed by tests.
    No psutil, or an error reading interfaces, is "none known".
    """
    if if_addrs is None:
        try:
            import psutil
            if_addrs = psutil.net_if_addrs()
        except Exception:
            return []
    out = set()
    for addrs in if_addrs.values():
        for a in addrs:
            if getattr(a, "family", None) == socket.AF_INET and is_tailscale(a.address):
                out.add(a.address)
    return sorted(out)


def addresses(lan_ip: str, if_addrs=None) -> list[str]:
    """Every IPv4 the app may try for this box, LAN first, no repeats."""
    out = []
    for ip in [lan_ip or ""] + tailscale_ips(if_addrs):
        if ip and ip not in out:
            out.append(ip)
    return out


def payload(root, version: str, lan_ip: str, if_addrs=None) -> dict:
    """The body of `GET /api/discovery`. `app` is how a probe tells this from
    any other device that answers on port 80."""
    return {
        "app": "streamlink",
        "id": server_id(root),
        "name": server_name(),
        "version": version,
        "addrs": addresses(lan_ip, if_addrs),
    }


def mdns_instance(name: str | None = None) -> str:
    """The `_streamlink._tcp` instance name. Dots would read as DNS labels."""
    return f"{server_name(name).replace('.', '-')}.{SERVICE_TYPE}"


def mdns_txt(root, lan_ip: str, port: int) -> dict:
    """TXT record for the `_streamlink._tcp` service.

    It carries the address outright. A Bonjour browse result is a *name*; turning
    it into an address is a second resolve step on the phone, and the address is
    the one thing the app wants.
    """
    return {"id": server_id(root), "name": server_name(),
            "ip": lan_ip, "port": str(int(port))}
