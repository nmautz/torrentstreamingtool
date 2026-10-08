"""Which disk a library file is on, and what the Storage tab counts.

    python tests/test_storage_volumes.py      (or `make test`)

Two helpers lifted out of main.py by name (it can't be imported without the
whole dependency tree; the lift asserts every name was found):

  * `_volume_of` / `_library_free_by_volume` - source eviction judges each disk
    by its own free space. Checked against Windows path rules (`ntpath`), since
    Windows is where the library is on C: and F:.
  * `_storage_breakdown_sync` - a file that is not on the host counts as zero,
    not as the size the library remembers for it.
"""
import ast, functools, io, ntpath, os, shutil, sys, tempfile, types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
src = io.open(ROOT / "main.py", encoding="utf-8").read()
tree = ast.parse(src)
WANT = {"_volume_of_dir", "_volume_of", "_library_free_by_volume", "_storage_breakdown_sync"}
code = "\n\n".join(ast.get_source_segment(src, n) for n in tree.body
                   if getattr(n, "name", "") in WANT)
found = {n.name for n in tree.body if getattr(n, "name", "") in WANT}
assert found == WANT, f"not found in main.py: {WANT - found}"


def load(os_mod, disk_usage=shutil.disk_usage):
    ns = {"os": os_mod, "functools": functools, "Path": Path, "VIDEO_EXTS": {".mkv"},
          "shutil": types.SimpleNamespace(disk_usage=disk_usage),
          "_file_evicted": lambda f: bool((f.get("bundle") or {}).get("source_evicted")),
          "_item_display_names": lambda it: (it["title"], "")}
    exec(code, ns)
    return ns


fails = []


def check(label, cond):
    print(("  ok   " if cond else "  FAIL ") + label)
    if not cond:
        fails.append(label)


print("which disk (Windows paths)")
free = {"C:\\": 20, "F:\\": 223}


def usage(vol):
    if vol not in free:
        raise OSError("no such drive")
    return types.SimpleNamespace(free=free[vol])


win = load(types.SimpleNamespace(path=ntpath), usage)
vol = win["_volume_of"]
check("a file deep in Downloads is on C:\\",
      vol("C:\\Users\\Nathan\\Downloads\\StreamLink\\Show\\ep.mkv") == "C:\\")
check("a file on F: is on F:\\", vol("F:\\StreamLink\\Show\\ep.mkv") == "F:\\")
check("a network share is its own volume",
      vol("\\\\nas\\media\\Show\\ep.mkv").lower().startswith("\\\\nas\\media"))
lib = {"items": [{"files": [{"path": "C:\\Users\\Nathan\\Downloads\\StreamLink\\A\\1.mkv"},
                            {"path": "C:\\Users\\Nathan\\Downloads\\StreamLink\\A\\2.mkv"},
                            {"path": "F:\\StreamLink\\B\\1.mkv"},
                            {"path": "Z:\\gone\\1.mkv"}, {"path": ""}]}]}
check("free space is reported per disk", win["_library_free_by_volume"](lib) == free)
check("a disk that can't be measured is left out",
      "Z:\\" not in win["_library_free_by_volume"](lib))

print("which disk (this machine)")
here = load(os)
with tempfile.TemporaryDirectory() as tmp:
    v = here["_volume_of"](os.path.join(tmp, "no", "such", "dir", "ep.mkv"))
    check("a folder that no longer exists still resolves to a mount", os.path.ismount(v))

    print("storage breakdown")
    kept = os.path.join(tmp, "kept.mkv")
    Path(kept).write_bytes(b"x" * 10)
    evicted = os.path.join(tmp, "evicted.mkv")
    lib = {"items": [
        {"id": "i", "title": "Show", "files": [
            {"path": kept, "size_bytes": 10},
            {"path": os.path.join(tmp, "deleted.mkv"), "size_bytes": 999},
            {"path": evicted, "size_bytes": 500, "bundle": {"source_evicted": True}}]},
        {"id": "j", "title": "All deleted", "files": [
            {"path": os.path.join(tmp, "x", "y.mkv"), "size_bytes": 77}]}]}
    d = here["_storage_breakdown_sync"](lib, {evicted: 40})
    check("a deleted file counts as zero, not its recorded size", d["source_bytes"] == 10)
    check("an evicted file still shows its bundle", d["bundle_bytes"] == 40)
    check("a show lists only the files with something on the host",
          [(i["title"], i["file_count"]) for i in d["items"]] == [("Show", 2)])
    check("a show with nothing left is not listed",
          all(i["title"] != "All deleted" for i in d["items"]))

print()
if fails:
    print(f"{len(fails)} FAILED"); sys.exit(1)
print("all passed")
