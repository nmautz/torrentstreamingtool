"""Unit tests for `discovery.py`. Run with plain python, no deps:

    python tests/test_discovery.py      (or `make test`)

Same shape as test_tmdbcache.py: a list of cases and a counter. Interfaces are
handed in as psutil's shape, so no real adapter is read.
"""

import os
import shutil
import socket
import sys
import tempfile
from collections import namedtuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import discovery as d            # noqa: E402

_PASS = 0
_FAIL = []


def eq(name, got, want):
    global _PASS
    if got == want:
        _PASS += 1
    else:
        _FAIL.append("%s\n     got:  %r\n     want: %r" % (name, got, want))


Addr = namedtuple("Addr", "family address")


def v4(ip):
    return Addr(socket.AF_INET, ip)


# The live box, as Windows names its adapters: LAN, Tailscale, Mullvad.
WINDOWS = {
    "Ethernet":  [v4("192.168.0.106"), Addr(socket.AF_INET6, "fe80::1")],
    "Tailscale": [v4("100.72.10.4"), Addr(socket.AF_INET6, "fd7a:115c:a1e0::1")],
    "Mullvad":   [v4("10.64.0.3")],
    "Loopback Pseudo-Interface 1": [v4("127.0.0.1")],
}

# ── is_tailscale ─────────────────────────────────────────────────────────────
eq("cgnat low edge", d.is_tailscale("100.64.0.1"), True)
eq("cgnat high edge", d.is_tailscale("100.127.255.254"), True)
eq("just below cgnat", d.is_tailscale("100.63.255.255"), False)
eq("just above cgnat", d.is_tailscale("100.128.0.1"), False)
eq("lan is not tailscale", d.is_tailscale("192.168.0.106"), False)
eq("mullvad is not tailscale", d.is_tailscale("10.64.0.3"), False)
eq("tailscale's resolver is not a machine", d.is_tailscale("100.100.100.100"), False)
eq("garbage", d.is_tailscale("not an ip"), False)
eq("empty", d.is_tailscale(""), False)

# ── tailscale_ips / addresses ────────────────────────────────────────────────
eq("found by subnet, whatever the adapter is called",
   d.tailscale_ips(WINDOWS), ["100.72.10.4"])
eq("same on macOS/Linux names",
   d.tailscale_ips({"utun9": [v4("100.72.217.121")], "en0": [v4("192.168.86.20")]}),
   ["100.72.217.121"])
eq("none", d.tailscale_ips({"Ethernet": [v4("192.168.0.106")]}), [])
eq("empty map", d.tailscale_ips({}), [])
eq("lan first", d.addresses("192.168.0.106", WINDOWS), ["192.168.0.106", "100.72.10.4"])
eq("no lan ip yet (boot before Wi-Fi)", d.addresses("", WINDOWS), ["100.72.10.4"])
eq("no repeats", d.addresses("100.72.10.4", WINDOWS), ["100.72.10.4"])
eq("nothing at all", d.addresses("", {}), [])

# ── server_name ──────────────────────────────────────────────────────────────
eq("plain hostname", d.server_name("LIVINGROOM-PC"), "LIVINGROOM-PC")
eq("domain dropped", d.server_name("media.local"), "media")
eq("odd characters dropped", d.server_name("Nathan’s <Box>"), "Nathans Box")
eq("empty falls back", d.server_name(""), "StreamLink")
eq("only punctuation falls back", d.server_name("...."), "StreamLink")
eq("long name capped", len(d.server_name("x" * 200)), 40)
eq("instance name", d.mdns_instance("media.local"), "media._streamlink._tcp.local.")

# ── server_id ────────────────────────────────────────────────────────────────
tmp = tempfile.mkdtemp()
try:
    first = d.server_id(tmp)
    eq("id shape", bool(d._ID_RE.match(first)), True)
    eq("id is stable across calls", d.server_id(tmp), first)
    eq("id is on disk", open(os.path.join(tmp, d.ID_FILE)).read().strip(), first)

    with open(os.path.join(tmp, d.ID_FILE), "w") as fh:
        fh.write("<<<<<<< not an id\n")
    healed = d.server_id(tmp)
    eq("a damaged file is replaced", bool(d._ID_RE.match(healed)), True)
    eq("...and the replacement sticks", d.server_id(tmp), healed)

    eq("an id that can't be kept is not invented",
       d.server_id(os.path.join(tmp, "no", "such", "dir")), "")

    # ── payload / mdns_txt ───────────────────────────────────────────────────
    p = d.payload(tmp, "19.14.0", "192.168.0.106", WINDOWS)
    eq("payload marker", p["app"], "streamlink")
    eq("payload id", p["id"], healed)
    eq("payload version", p["version"], "19.14.0")
    eq("payload addrs", p["addrs"], ["192.168.0.106", "100.72.10.4"])
    eq("payload keys", sorted(p), ["addrs", "app", "id", "name", "version"])

    t = d.mdns_txt(tmp, "192.168.0.106", 80)
    eq("txt id matches the endpoint", t["id"], healed)
    eq("txt ip", t["ip"], "192.168.0.106")
    eq("txt port is a string", t["port"], "80")
    eq("txt values are all strings", all(isinstance(v, str) for v in t.values()), True)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print("discovery: %d passed, %d failed" % (_PASS, len(_FAIL)))
for f in _FAIL:
    print("  FAIL " + f)
sys.exit(1 if _FAIL else 0)
