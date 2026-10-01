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
from typing import Optional

MAIN = Path(__file__).resolve().parent.parent / "main.py"
src = io.open(MAIN, encoding="utf-8").read()
tree = ast.parse(src)

WANT = {"_pack_slice_want", "_pack_slice_apply", "_pack_slice_settle",
        "_pack_slice_expired", "_pack_available", "_pack_first_cfg",
        "_pack_slice_retire", "_pack_slice_want_seasons", "_pack_slice_strict",
        "_pack_scope_summary",
        "_download_cfg", "_effective_file_mode", "_file_mode_to_priority"}
CONSTS = {"_PACK_EXTRA_MAX_BYTES", "_PACK_SLICE_GRACE_SECS",
          "_PACK_FIRST_MAX_BYTES", "_FILE_MODES", "_DL_PRIORITIES"}

ns = {"Path": Path, "Optional": Optional, "datetime": datetime, "timezone": timezone,
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

# ── 8. A SEASON out of a multi-season pack (19.12.0). "Get Season 1" answered by the
# best-seeded SpongeBob release - a 206.9 GB `Season 1-13 COMPLETE` - downloaded all
# thirteen. The slice is expressed as seasons, not as an episode list, because a pack's
# own numbering need not match TMDb's count.
print("8. slice a three-season pack down to Season 1")
SP3 = r"D:\media\Show Season 1-3 COMPLETE"
def lf3(name, s, e, bucket=""):
    d = {"path": str(Path(SP3) / name), "name": name, "season": s, "episode": e,
         "size_bytes": 800_000_000}
    if bucket:
        d["bucket"] = bucket
    return d
names = [(f"Season {s}/Show.S{s:02d}E{e:02d}.mkv", s, e) for s in (1, 2, 3) for e in range(1, 6)]
qf3 = ([qf(n, 800_000_000) for n, _s, _e in names]
       + [qf("Season 1/Show.S01E03.eng.srt", 50_000),
          qf("Specials/Show.S00E01.mkv", 700_000_000),
          qf("Season 1/Extras/making-of.mkv", 600_000_000)])
it3 = {"id": "i3",
       "files": [lf3(n, s_, e_) for n, s_, e_ in names]
                + [lf3("Specials/Show.S00E01.mkv", 0, 1),
                   lf3("Season 1/Extras/making-of.mkv", 1, 0, bucket="extras")],
       "pack_slice": {"want": [], "want_seasons": [1], "settled": False,
                      "since": ns["_now_iso"](), "skipped": [], "fallback": {}}}
check("verdict is ok", _apply(it3, qf3, SP3) == "ok")
mode3 = lambda n: _cfgmode(_dlcfg(it3), str(Path(SP3) / n))
check("every Season 1 episode kept",
      all(mode3(f"Season 1/Show.S01E{e:02d}.mkv") != "skip" for e in range(1, 6)))
check("every Season 2 episode skipped",
      all(mode3(f"Season 2/Show.S02E{e:02d}.mkv") == "skip" for e in range(1, 6)))
check("every Season 3 episode skipped",
      all(mode3(f"Season 3/Show.S03E{e:02d}.mkv") == "skip" for e in range(1, 6)))
check("the specials are skipped", mode3("Specials/Show.S00E01.mkv") == "skip")
check("a bucketed extra inside the season folder is skipped",
      mode3("Season 1/Extras/making-of.mkv") == "skip")
check("the small subtitle sidecar rides along", mode3("Season 1/Show.S01E03.eng.srt") != "skip")
summ = ns["_pack_scope_summary"](it3)
check("the summary names Season 1", summ and summ["seasons"] == [1])
check("...and what it costs: 5 of 17 videos, 4 GB of 13.3 GB",
      summ["files"] == 5 and summ["total_files"] == 17
      and summ["bytes"] == 5 * 800_000_000 and summ["total_bytes"] == 17 * 800_000_000
      and summ["resolved"] is True)
check("a season slice is strict", ns["_pack_slice_strict"](it3) is True)
check("an episode slice is not", ns["_pack_slice_strict"](item) is False)
check("an item with no slice has no scope summary", ns["_pack_scope_summary"]({"files": []}) is None)

# The numbering mismatch that an episode list would get wrong: the release ships the
# season as double-length files numbered 1..3 where TMDb counts 6 segments.
print("9. a season slice survives the pack's own episode numbering")
it4 = {"id": "i4",
       "files": [lf3(f"Show.S01E{e:02d}.mkv", 1, e) for e in (1, 2, 3)]
                + [lf3(f"Show.S02E{e:02d}.mkv", 2, e) for e in (1, 2, 3)],
       "pack_slice": {"want": [], "want_seasons": [1], "settled": False,
                      "since": ns["_now_iso"](), "skipped": [], "fallback": {}}}
q4 = [qf(f"Show.S{s:02d}E{e:02d}.mkv", 800_000_000) for s in (1, 2) for e in (1, 2, 3)]
check("verdict is ok", _apply(it4, q4, SP3) == "ok")
check("all three Season 1 files kept, whatever TMDb calls them",
      sum(1 for e in (1, 2, 3)
          if _cfgmode(_dlcfg(it4), str(Path(SP3) / f"Show.S01E{e:02d}.mkv")) != "skip") == 3)

# Nothing attributed to the season yet: hold EVERYTHING, exactly as an episode slice does.
print("10. an unresolved season slice holds the whole pack at zero")
it5 = {"id": "i5", "files": [lf3(f"ep{e}.mkv", 0, 0) for e in range(1, 4)],
       "pack_slice": {"want": [], "want_seasons": [1], "settled": False,
                      "since": ns["_now_iso"](), "skipped": [], "fallback": {}}}
q5 = [qf(f"ep{e}.mkv", 800_000_000) for e in range(1, 4)]
check("verdict is pending", _apply(it5, q5, SP3) == "pending")
check("every file skipped",
      all(_cfgmode(_dlcfg(it5), str(Path(SP3) / f"ep{e}.mkv")) == "skip" for e in range(1, 4)))
s5 = ns["_pack_scope_summary"](it5)
check("the summary says it is not resolved yet (so the card won't print '0 B of ...')",
      s5["resolved"] is False and s5["seasons"] == [1])
# ...and once attribution lands the same slice re-derives, like an episode slice.
for f, sn in zip(it5["files"], (1, 1, 2)):
    f["season"], f["episode"] = sn, 1
check("re-derives to ok once the seasons are known", _apply(it5, q5, SP3) == "ok")
check("ep3 (season 2) stays skipped, ep1 and ep2 are released",
      _cfgmode(_dlcfg(it5), str(Path(SP3) / "ep3.mkv")) == "skip"
      and _cfgmode(_dlcfg(it5), str(Path(SP3) / "ep1.mkv")) != "skip"
      and _cfgmode(_dlcfg(it5), str(Path(SP3) / "ep2.mkv")) != "skip")
# After the user pulls Season 2 as well, the label must follow the files, not the request.
it5["download"]["files"].pop(str(Path(SP3) / "ep3.mkv"))
it5["pack_slice"]["settled"] = True
check("the summary follows what is actually kept (Seasons 1 and 2)",
      ns["_pack_scope_summary"](it5)["seasons"] == [1, 2])

print()
print("FAILED: " + "; ".join(fails) if fails else "all checks passed")
sys.exit(1 if fails else 0)
