"""A compression run skips files it already compressed, unless asked to (20.26.7).

    python tests/test_compress_skip.py      (or `make test`)

A run stopped by a restart has to be started again by hand, and it used to
re-encode every file it had already finished: a second lossy pass at the same
strength usually comes out a little smaller, so it replaced the file and lost
more quality. `compressed_with` is now written on every compressed file, and the
run (and the estimate) leave those out unless `recompress` is set.

main.py can't be imported without the whole dependency tree, so the functions are
lifted out of its source by name and run against a fake library.
"""
import ast, asyncio, sys, types
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _backend_src                       # noqa: E402

src = _backend_src.read()
tree = ast.parse(src)
WANT = {"_already_compressed", "_run_file_compression"}
pieces = [ast.get_source_segment(src, n) for n in tree.body
          if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in WANT]
assert len(pieces) == len(WANT), "not found in main.py"

fails = []


def check(name, ok):
    print(("  ok    " if ok else "  FAIL  ") + name)
    if not ok:
        fails.append(name)


def make_lib():
    return {"items": [
        {"id": "sg", "title": "Steins;Gate", "torrent_hash": "abc", "files": [
            {"path": "F:/sg/e01.mkv", "compressed": True},           # before 20.26.7
            {"path": "F:/sg/e02.mkv", "compressed_with": {"codec": "hevc", "crf": 28}},
            {"path": "F:/sg/e03.mkv"},
            {"path": "F:/sg/e03.en.srt"}]},
        {"id": "up", "title": "Uploaded", "files": [{"path": "F:/up/film.mp4"}]}]}


def run(recompress):
    lib = make_lib()
    encoded = []

    async def _compress_one_file(ffmpeg, path, **kw):
        encoded.append(path)
        return "compressed", "HEVC CRF 28", 100, 60

    @asynccontextmanager
    async def mutate_library():
        yield lib

    async def get_library():
        return lib

    state = types.SimpleNamespace(file_compression={"results": []},
                                  file_compression_stop=False, file_compression_proc=None)
    ns = {"asyncio": asyncio, "Path": Path, "Optional": Optional,
          "VIDEO_EXTS": {".mkv", ".mp4"}, "state": state,
          "analyzer": types.SimpleNamespace(ffmpeg_bin=lambda: "ffmpeg"),
          "get_library": get_library, "mutate_library": mutate_library,
          "_compress_one_file": _compress_one_file,
          "_item_display_names": lambda it: [it.get("title", "")],
          "_now_iso": lambda: "2026-10-22T12:00:00Z"}
    exec("\n\n".join(pieces), ns)
    asyncio.run(ns["_run_file_compression"]("all", 28, "hevc", None, recompress))
    return lib, encoded, state.file_compression


print("a second run")
lib, encoded, fc = run(recompress=False)
check("leaves out both kinds of already-compressed file",
      encoded == ["F:/sg/e03.mkv", "F:/up/film.mp4"])
check("and says how many", fc.get("already_compressed") == 2 and fc["total"] == 2)
files = {f["path"]: f for it in lib["items"] for f in it["files"]}
check("every compressed file gets compressed_with",
      files["F:/sg/e03.mkv"].get("compressed_with", {}).get("crf") == 28
      and files["F:/up/film.mp4"].get("compressed_with", {}).get("codec") == "hevc")
check("`compressed` is still only for torrent-backed files",
      files["F:/sg/e03.mkv"].get("compressed") is True
      and "compressed" not in files["F:/up/film.mp4"])

print("with recompress")
lib, encoded, fc = run(recompress=True)
check("every video is encoded again",
      encoded == ["F:/sg/e01.mkv", "F:/sg/e02.mkv", "F:/sg/e03.mkv", "F:/up/film.mp4"]
      and not fc.get("already_compressed"))

print("the estimate and the endpoint carry the flag")
est = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "_compress_estimate")
est_src = ast.get_source_segment(src, est)
check("the estimate leaves the same files out",
      "_already_compressed(f)" in est_src and '"already_compressed": skipped' in est_src)
check("both request models have recompress",
      src.count("recompress: bool = False") >= 2)

print("a probe that could not run is not 'no video' (20.26.9)")
inner = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef)
             and n.name == "_compress_one_file_inner")
inner_src = ast.get_source_segment(src, inner)
check("an empty probe is retried with a longer timeout before failing",
      "if not info:" in inner_src
      and inner_src.index("if not info:") < inner_src.index('"No decodable video stream."')
      and "_ffprobe_full, str(src), 90.0" in inner_src)
check("and says it timed out, not that the file has no video",
      "ffprobe failed or timed" in inner_src)

print()
if fails:
    print(f"{len(fails)} FAILED"); sys.exit(1)
print("all passed")
