"""`_retry_candidates` from main.py: which releases may replace a dead download.

    python tests/test_retry_candidates.py      (or `make test`)

main.py can't be imported without its whole dependency tree, so the function is
lifted out of its source (as tests/test_packslice.py does) and given stand-ins
for the title parser and the helpers that have nothing to do with the question
asked here: is this release the EPISODE the item was fetched for? Titles are
real SpongeBob SquarePants indexer results from 2026-10-06.
"""

import ast, io, re, sys
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import titleslot            # noqa: E402

src = io.open(ROOT / "main.py", encoding="utf-8").read()
fn = next(n for n in ast.parse(src).body
          if isinstance(n, ast.FunctionDef) and n.name == "_retry_candidates")


def _parse(title):
    m = re.search(r"[Ss](\d{1,2})[Ee](\d{1,3})", title or "")
    return ({"kind": "episode", "season": int(m.group(1)), "episode": int(m.group(2)), "show": ""}
            if m else {"kind": "movie", "season": 0, "episode": 0, "show": ""})


class _No:
    is_cam = staticmethod(lambda t: False)
    title_dv_risk = staticmethod(lambda t: False)


ns = {"Optional": Optional, "titleslot": titleslot, "relquality": _No, "dvprobe": _No,
      "parse_torrent_title": _parse, "extract_hash": lambda m: "",
      "_release_key": lambda t: re.sub(r"[^a-z0-9]+", "", (t or "").lower()),
      "_release_group": lambda t: ""}
exec(ast.get_source_segment(src, fn), ns)
cands = ns["_retry_candidates"]

fails = []
def check(label, cond):
    print(("  ok   " if cond else "  FAIL ") + label)
    if not cond:
        fails.append(label)

S3 = [{"episode": 19, "name": "Party Pooper Pants"}, {"episode": 20, "name": "Chocolate with Nuts"},
      {"episode": 21, "name": "Mermaid Man and Barnacle Boy V"}]
def R(title, seeders=10): return {"title": title, "magnet": "magnet:?x=" + title, "seeders": seeders}
def titles(out): return [r["title"] for _, r in out]

item = {"series": "SpongeBob SquarePants", "season": 3, "episode": 19,
        "title": "SpongeBob SquarePants S03E19 The Spongebob SquarePants Lost Episodes 1080p SKST WEB-DL"}
by_number = [R("SpongeBob SquarePants S03E19 Chocolate with Nuts REPACK 1080p AMZN WEB-DL", 40),
             R("SpongeBob SquarePants S03E18 Krusty Crab Training Video REPACK 1080p AMZN", 23)]
by_name = [R("SpongeBob SquarePants S00E02 Party Pooper Pants 1080p AMZN WEB-DL DDP2 0 H 264-Kitsune", 10),
           R("SpongeBob SquarePants S00E01 Christmas Who 1080p AMZN WEB-DL", 19),
           R("SpongeBob SquarePants S16E10 Laundro Madness 720p HDTV", 1868)]

print("retry candidates")
check("with names: the same number on another episode is refused",
      titles(cands(item, by_number, set(), None, S3)) == [])
check("without names: a different stated title is refused",
      titles(cands(item, by_number, set(), None, [])) == [])
check("by number, another season is never taken",
      titles(cands(item, by_name, set(), None, S3)) == [])
check("by name: the special that states this episode is taken, and only it",
      titles(cands(item, by_name, set(), None, S3, cross=True)) == [by_name[0]["title"]])
check("by name with no names known: nothing",
      titles(cands(item, by_name, set(), None, [], cross=True)) == [])

item20 = {"series": "SpongeBob SquarePants", "season": 3, "episode": 20, "title": "x"}
check("a release numbered one off IS a replacement for the episode it names",
      titles(cands(item20, by_number, set(), None, S3)) == [by_number[0]["title"]])
plain = {"series": "Hacks", "season": 5, "episode": 9, "title": "Hacks S05E09 1080p WEB h264-GRP"}
rs = [R("Hacks S05E09 720p WEB h264-OTHER"), R("Hacks S05E10 1080p WEB h264-GRP")]
check("an ordinary show is unchanged: same number in, others out",
      titles(cands(plain, rs, set(), None, [{"episode": 9, "name": "Episode 9"}])) == [rs[0]["title"]])

print()
print("FAILED: " + "; ".join(fails) if fails else "all checks passed")
sys.exit(1 if fails else 0)
