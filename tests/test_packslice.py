"""Pack slicing: keeping one episode out of a whole-season torrent.

`_pack_slice_apply` decides which files of an adopted pack download and which are
set to "skip" (priority 0). Getting that wrong is expensive in both directions -
too eager and someone who asked for one episode pulls 144 GB, too shy and the
episode they asked for never arrives - and the re-derivation across the
attribution passes is the part that is genuinely easy to break.

main.py can't be imported without the whole dependency tree (FastAPI, qBit, TMDb),
so the functions under test are lifted out of its source by name and given the
handful of globals they touch. That keeps this a `make test` test: no deps, no
venv, no running services. The lift asserts every name was found, so a rename in
main.py fails this loudly rather than silently testing nothing.
"""
import ast, io, sys
from pathlib import Path
from datetime import datetime, timezone

MAIN = Path(__file__).resolve().parent.parent / "main.py"
src = io.open(MAIN, encoding="utf-8").read()
tree = ast.parse(src)

WANT = {"_pack_slice_want", "_pack_slice_apply", "_pack_slice_settle",
        "_pack_slice_expired", "_pack_available", "_pack_first_cfg",
        "_pack_slice_retire",
        "_download_cfg", "_effective_file_mode", "_file_mode_to_priority"}
CONSTS = {"_PACK_EXTRA_MAX_BYTES", "_PACK_SLICE_GRACE_SECS",
          "_PACK_FIRST_MAX_BYTES", "_FILE_MODES", "_DL_PRIORITIES"}

ns = {"Path": Path, "Optional": None, "datetime": datetime, "timezone": timezone,
      "_now_iso": lambda: datetime.now(timezone.utc).isoformat(),
      "_series_key": lambda it: it.get("series", "")}
pieces = []
for node in tree.body:
    if isinstance(node, ast.FunctionDef) and node.name in WANT:
        pieces.append(ast.get_source_segment(src, node))
    elif isinstance(node, ast.Assign):
        for t in node.targets:
            if isinstance(t, ast.Name) and t.id in CONSTS:
                pieces.append(ast.get_source_segment(src, node))
missing = WANT - {ast.parse(p).body[0].name for p in pieces
                  if isinstance(ast.parse(p).body[0], ast.FunctionDef)}
assert not missing, f"not found in main.py: {missing}"
exec("\n\n".join(pieces), ns)

_apply   = ns["_pack_slice_apply"]
_avail   = ns["_pack_available"]
_settle  = ns["_pack_slice_settle"]
_cfgmode = ns["_effective_file_mode"]
_dlcfg   = ns["_download_cfg"]

SP = r"D:\media\Show S01 COMPLETE"
def qf(name, size): return {"name": name, "size": size}
def lf(name, s, e): return {"path": str(Path(SP) / name), "name": name,
                            "season": s, "episode": e, "size_bytes": 1_400_000_000}

fails = []
def check(label, cond):
    print(("  ok   " if cond else "  FAIL ") + label)
    if not cond:
        fails.append(label)

# ── 1. The happy path: keep E05, skip the other eleven, keep the small sidecars.
qfiles = ([qf(f"Show.S01E{n:02d}.1080p.mkv", 1_400_000_000) for n in range(1, 13)]
          + [qf("Subs/Show.S01E05.eng.srt", 62_000),
             qf("Show.S01.nfo", 2_000),
             qf("sample.mkv", 41_000_000),
             qf("Extras/behind-the-scenes.mkv", 900_000_000)])
item = {"id": "i1", "files": [lf(f"Show.S01E{n:02d}.1080p.mkv", 1, n) for n in range(1, 13)],
        "pack_slice": {"want": [[1, 5]], "settled": False,
                       "since": ns["_now_iso"](), "skipped": [], "fallback": {}}}
print("1. slice a 12-episode pack down to S01E05")
check("verdict is ok", _apply(item, qfiles, SP) == "ok")
modes = item["download"]["files"]
kept = [Path(k).name for k in qfiles and
        [str(Path(SP) / q["name"]) for q in qfiles] if modes.get(k) != "skip"]
check("E05 kept", _cfgmode(_dlcfg(item), str(Path(SP) / "Show.S01E05.1080p.mkv")) != "skip")
check("E01 skipped", _cfgmode(_dlcfg(item), str(Path(SP) / "Show.S01E01.1080p.mkv")) == "skip")
check("E12 skipped", _cfgmode(_dlcfg(item), str(Path(SP) / "Show.S01E12.1080p.mkv")) == "skip")
check("subtitle sidecar kept", _cfgmode(_dlcfg(item), str(Path(SP) / "Subs/Show.S01E05.eng.srt")) != "skip")
check(".nfo kept", _cfgmode(_dlcfg(item), str(Path(SP) / "Show.S01.nfo")) != "skip")
check("sample.mkv skipped (a video nobody asked for)",
      _cfgmode(_dlcfg(item), str(Path(SP) / "sample.mkv")) == "skip")
check("big non-video extra skipped",
      _cfgmode(_dlcfg(item), str(Path(SP) / "Extras/behind-the-scenes.mkv")) == "skip")
check("exactly 1 of 12 episodes downloads",
      sum(1 for n in range(1, 13)
          if _cfgmode(_dlcfg(item), str(Path(SP) / f"Show.S01E{n:02d}.1080p.mkv")) != "skip") == 1)

# ── 2. Unresolved: hold EVERYTHING at zero, not nothing.
print("2. attribution hasn't settled yet")
item2 = {"id": "i2", "files": [], "pack_slice": {"want": [[1, 5]], "settled": False,
         "since": ns["_now_iso"](), "skipped": [], "fallback": {}}}
check("verdict is pending", _apply(item2, qfiles, SP) == "pending")
check("every file held at skip",
      all(_cfgmode(_dlcfg(item2), str(Path(SP) / q["name"])) == "skip" for q in qfiles))

# ── 3. Re-derivation after the anime remap moves the file.
print("3. pass 3 moves E05 to a different season, then it settles")
item3 = {"id": "i3", "files": [lf("Show - 05.mkv", 2, 5)],
         "pack_slice": {"want": [[1, 5]], "settled": False,
                        "since": ns["_now_iso"](), "skipped": [], "fallback": {}}}
q3 = [qf("Show - 05.mkv", 1_400_000_000)]
check("held while it says S02E05", _apply(item3, q3, SP) == "pending")
item3["files"][0].update(season=1, episode=5)          # animemap corrects it
check("kept once corrected to S01E05", _apply(item3, q3, SP) == "ok")
check("file now downloads", _cfgmode(_dlcfg(item3), str(Path(SP) / "Show - 05.mkv")) != "skip")

# ── 4. A hand un-skip is never re-skipped.
print("4. the user fetches E06 out of the pack")
item["download"]["files"][str(Path(SP) / "Show.S01E06.1080p.mkv")] = "mid"
_settle(item)
check("settled", item["pack_slice"]["settled"] is True)
check("re-running is a no-op", _apply(item, qfiles, SP) == "")
check("E06 still downloads",
      _cfgmode(_dlcfg(item), str(Path(SP) / "Show.S01E06.1080p.mkv")) != "skip")
check("E07 still skipped",
      _cfgmode(_dlcfg(item), str(Path(SP) / "Show.S01E07.1080p.mkv")) == "skip")

# ── 5. _pack_available: identity is mandatory, skip is the trigger.
print("5. finding an episode inside a pack already on the box")
lib = {"items": [dict(item, series="Show", torrent_hash="abc", status="downloading",
                      metadata={"tmdb_id": 42})]}
check("finds a skipped episode by tmdb_id", (_avail(lib, 1, 7, "", 42) or {}).get("item_id") == "i1")
check("refuses to match with no identity", _avail(lib, 1, 7, "", 0) is None)
check("wrong show doesn't match", _avail(lib, 1, 7, "", 99) is None)
check("an episode that IS downloading isn't offered", _avail(lib, 1, 6, "", 42) is None)
check("a torrent-less item isn't offered",
      _avail({"items": [dict(lib["items"][0], torrent_hash="")]}, 1, 7, "", 42) is None)

# ── 6. The settings reader.
print("6. pack_first settings")
pf = ns["_pack_first_cfg"]
check("defaults on at 200 GB", pf({}) == {"enabled": True, "max_bytes": 200 * 1024 ** 3})
check("honours an override",
      pf({"settings": {"pack_first": {"enabled": False, "max_bytes": 1}}})
      == {"enabled": False, "max_bytes": 1})
check("a junk cap falls back",
      pf({"settings": {"pack_first": {"max_bytes": "lots"}}})["max_bytes"] == 200 * 1024 ** 3)

# ── 7. Retiring a slice must survive the monitor's merge-back.
# `library_download_monitor` persists a tick with `cur.update(mutated)`, and dict.update
# never DELETES a key — so a retired slice expressed as `item.pop("pack_slice")` came
# back from disk still expired and unsettled, `_pack_slice_fallback` fired again, and the
# torrent was deleted with its files and re-added every five seconds. Caught live on the
# box (ten rounds in forty seconds against a 3.8 GB release); this is its regression.
print("7. retiring a slice survives a dict.update() merge-back")
retire = ns["_pack_slice_retire"]
live_item = {"id": "i9", "files": [], "download": {"mode": "now", "files": {}},
             "pack_slice": {"want": [[1, 99]], "settled": False,
                            "since": "2020-01-01T00:00:00+00:00", "skipped": []}}
check("an old slice reads as expired", ns["_pack_slice_expired"](live_item) is True)
retire(live_item, "unsliceable")
on_disk = {"id": "i9", "pack_slice": {"want": [[1, 99]], "settled": False,
                                      "since": "2020-01-01T00:00:00+00:00", "skipped": []}}
on_disk.update(live_item)          # exactly what the monitor does to persist a tick
check("the retired slice survived the merge",
      on_disk["pack_slice"].get("settled") is True and not on_disk["pack_slice"].get("want"))
check("so the monitor's guard is closed",
      not (on_disk.get("pack_slice") and not on_disk["pack_slice"].get("settled")))
check("and re-applying is a permanent no-op",
      _apply(on_disk, [qf("x.mkv", 1)], SP) == "")
# The shape the bug had: a pop is undone by the same merge.
popped = dict(live_item)
popped.pop("pack_slice")
merged = {"pack_slice": {"want": [[1, 99]], "settled": False,
                         "since": "2020-01-01T00:00:00+00:00", "skipped": []}}
merged.update(popped)
check("(a pop, by contrast, would have been undone — the bug)",
      merged["pack_slice"].get("settled") is False)

print()
print("FAILED: " + "; ".join(fails) if fails else "all checks passed")
sys.exit(1 if fails else 0)
