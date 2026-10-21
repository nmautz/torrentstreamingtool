"""The VPN mode is read out of library.json without parsing the whole file (20.25.24).

    python tests/test_vpncheck.py      (or `make test`)

The watchdog asks for it every 3 s from a thread of the server process. A full
`json.loads` of the 7.6 MB library holds the GIL throughout and stopped the
event loop ~0.1 s each time, so `vpn_mode` reads only the object after the key,
and only when the file changed.
"""
import json, os, sys, tempfile, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import vpncheck                           # noqa: E402

fails = []


def check(name, ok):
    print(("  ok    " if ok else "  FAIL  ") + name)
    if not ok:
        fails.append(name)


def full(lib, default="mullvad"):
    ks = (lib.get("settings", {}) or {}).get("vpn_killswitch") or {}
    m = str(ks.get("mode", default) or default).strip().lower()
    return m if m in vpncheck.VALID_MODES else default


items = [{"id": str(n), "title": f"Show {n}", "files": [{"path": f"C:\\x\\{n}.mkv"}]}
         for n in range(20000)]
cases = {
    "generic": {"items": items, "settings": {"vpn_killswitch": {"mode": "generic"}}},
    "off, settings first": {"settings": {"vpn_killswitch": {"mode": "OFF "}}, "items": items},
    "unknown value": {"items": [], "settings": {"vpn_killswitch": {"mode": "nord"}}},
    "no key": {"items": items, "settings": {}},
    "no settings": {"items": items},
    "null killswitch": {"settings": {"vpn_killswitch": None}},
    "mode missing": {"settings": {"vpn_killswitch": {"block_ui": True}}},
    "key also in a title, agreeing": {"items": [{"title": '"vpn_killswitch": {"mode": "generic"}'}],
                                      "settings": {"vpn_killswitch": {"mode": "generic"}}},
    "a decoy elsewhere that disagrees": {"profiles": [{"vpn_killswitch": {"mode": "off"}}],
                                         "settings": {"vpn_killswitch": {"mode": "generic"}}},
}

d = Path(tempfile.mkdtemp())
vpncheck.HERE = d
lib = d / "library.json"
print("matches a full parse")
for name, data in cases.items():
    for indent in (None, 2):
        lib.write_text(json.dumps(data, indent=indent), encoding="utf-8")
        os.utime(lib, ns=(time.time_ns(), time.time_ns() + len(name) + (indent or 0)))
        check(f"{name}{' (indented)' if indent else ''}", vpncheck.vpn_mode() == full(data))

print("only re-reads a changed file")
lib.write_text(json.dumps(cases["generic"]), encoding="utf-8")
vpncheck.vpn_mode()
reads = []
real = vpncheck._killswitch_mode
vpncheck._killswitch_mode = lambda raw: reads.append(1) or real(raw)
for _ in range(5):
    vpncheck.vpn_mode()
check("five asks of an unchanged file read it no more", reads == [])
lib.write_text(json.dumps(cases["off, settings first"]), encoding="utf-8")
os.utime(lib, ns=(time.time_ns(), time.time_ns() + 10**9))
check("a changed file is read again", vpncheck.vpn_mode() == "off" and reads == [1])
vpncheck._killswitch_mode = real
lib.unlink()
check("a missing file is the safe default", vpncheck.vpn_mode() == "mullvad")
lib.write_text("{not json", encoding="utf-8")
check("a broken file is the safe default", vpncheck.vpn_mode() == "mullvad")

big = json.dumps(cases["generic"]).encode()
t = time.perf_counter()
vpncheck._killswitch_mode(big)
fast = time.perf_counter() - t
t = time.perf_counter()
json.loads(big)
slow = time.perf_counter() - t
print(f"  ({len(big) / 1e6:.1f} MB: key read {fast * 1000:.1f} ms, full parse {slow * 1000:.1f} ms)")
check("reading the key is far cheaper than parsing the file", fast * 5 < slow)

print()
if fails:
    print(f"{len(fails)} FAILED"); sys.exit(1)
print("all passed")
