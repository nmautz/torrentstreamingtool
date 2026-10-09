"""An on-demand session the reaper took is made again when it is asked for.

    python tests/test_od_revive.py      (or `make test`)

A paused iOS app is suspended in the background, so nothing keeps its on-demand
session warm; the reaper takes it, and the native player comes back holding only
the session's URL. This drives the real `_od_teardown`, `_od_revive` and
`_od_new_session` from main.py against a temp directory and a fake ffprobe, and
checks when a reaped session comes back and when it must not.

main.py can't be imported without the whole dependency tree, so - like
tests/test_race_rescue.py - the functions under test are lifted out of its
source by name. The lift asserts every name was found.
"""
import ast, asyncio, hashlib, io, logging, shutil, sys, tempfile, time
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
src = io.open(ROOT / "main.py", encoding="utf-8").read()
tree = ast.parse(src)

WANT = {"_od_teardown", "_od_revive", "_od_new_session", "_od_session_key"}
CONSTS = {"OD_REVIVE_SECS", "OD_REVIVE_MAX"}

TMP = Path(tempfile.mkdtemp(prefix="od_revive_"))
PROBES = []
COMPRESSING = set()


def _offline_cache_key(p: Path) -> str:
    return hashlib.sha256(f"{p.name}|{p.stat().st_size}".encode()).hexdigest()[:24]


def _ffprobe_full(path: str) -> dict:
    PROBES.append(path)
    return {"duration_sec": 600.0, "video": {"codec": "h264"},
            "subtitles": [{"idx": 0}, {"idx": 2}]}


def _od_text_subs(info: dict) -> list:
    return info.get("subtitles") or []


def _extract_bundle_fonts(*a):
    return []


def _is_compressing(path: str) -> bool:
    return path in COMPRESSING


env = {
    "asyncio": asyncio, "hashlib": hashlib, "shutil": shutil, "time": time, "Path": Path,
    "Optional": Optional, "hls_log": logging.getLogger("test"),
    "ONDEMAND_CACHE": TMP / ".ondemand_cache",
    "_od_sessions": {}, "_od_gone": {}, "_od_revive_lock": None,
    "_offline_cache_key": _offline_cache_key, "_ffprobe_full": _ffprobe_full,
    "_od_text_subs": _od_text_subs, "_extract_bundle_fonts": _extract_bundle_fonts,
    "_is_compressing": _is_compressing,
}
found = set()
for node in tree.body:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in WANT:
        found.add(node.name)
    elif isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id in CONSTS for t in node.targets):
        found.update(t.id for t in node.targets if isinstance(t, ast.Name))
    else:
        continue
    exec(compile(ast.Module([node], []), "main.py", "exec"), env)
assert found == WANT | CONSTS, f"not found in main.py: {(WANT | CONSTS) - found}"

sessions, gone = env["_od_sessions"], env["_od_gone"]
teardown, revive, new_session, key_of = (env[n] for n in (
    "_od_teardown", "_od_revive", "_od_new_session", "_od_session_key"))


def film(name: str, size: int = 1000) -> Path:
    p = TMP / name
    p.write_bytes(b"x" * size)
    return p


async def opened(p: Path, aidx: int = 0) -> str:
    key = key_of(p, aidx)
    await new_session(key, p, _ffprobe_full(str(p)), aidx, [0, 2])
    return key


def reset():
    sessions.clear(); gone.clear(); PROBES.clear(); COMPRESSING.clear()


async def test_reaped_session_comes_back():
    reset()
    p = film("a.mkv")
    key = await opened(p, 1)
    await teardown(key, revivable=True)
    assert key not in sessions and key in gone
    assert not (env["ONDEMAND_CACHE"] / key).exists()

    s = await revive(key)
    assert s is not None and sessions[key] is s
    assert s["src"] == str(p) and s["audio_idx"] == 1 and s["sub_idxs"] == [0, 2]
    assert s["duration"] == 600.0 and s["start_seg"] == 0 and s["proc"] is None
    assert s["dir"].is_dir()
    assert key not in gone, "a live session is not also a reaped one"


async def test_stopped_session_does_not():
    """`/close` and the pre-compress teardown mean it: nothing asks for those."""
    reset()
    key = await opened(film("b.mkv"))
    await teardown(key)
    assert key not in gone
    assert await revive(key) is None and key not in sessions

    # Reaped, then stopped by the player on its way out: the stop wins.
    key = await opened(film("b2.mkv"))
    await teardown(key, revivable=True)
    await teardown(key)
    assert await revive(key) is None


async def test_unknown_key_is_not_made():
    reset()
    assert await revive("0" * 24) is None
    assert not PROBES


async def test_source_gone_or_changed():
    reset()
    p = film("c.mkv")
    key = await opened(p)
    await teardown(key, revivable=True)
    p.unlink()
    assert await revive(key) is None
    assert key not in gone, "asked once, answered for good"

    p = film("d.mkv", 1000)
    key = await opened(p)
    await teardown(key, revivable=True)
    p.write_bytes(b"x" * 900)            # compressed or replaced in place
    assert await revive(key) is None and key not in sessions


async def test_compressing_waits():
    reset()
    p = film("e.mkv")
    key = await opened(p)
    await teardown(key, revivable=True)
    COMPRESSING.add(str(p))
    assert await revive(key) is None
    assert key in gone, "refused for now, not forgotten"
    COMPRESSING.clear()
    assert await revive(key) is not None


async def test_a_burst_probes_once():
    """A returning player asks for its playlist, a segment and every subtitle
    track together (37 of them on the film this was found on)."""
    reset()
    key = await opened(film("f.mkv"))
    await teardown(key, revivable=True)
    PROBES.clear()
    got = await asyncio.gather(*(revive(key) for _ in range(40)))
    assert all(s is got[0] and s is not None for s in got)
    assert len(PROBES) == 1, PROBES


async def test_remembered_set_is_bounded():
    reset()
    keys = []
    for i in range(env["OD_REVIVE_MAX"] + 5):
        k = await opened(film(f"g{i}.mkv", 100 + i))
        await teardown(k, revivable=True)
        keys.append(k)
    assert len(gone) == env["OD_REVIVE_MAX"]
    assert keys[0] not in gone and keys[-1] in gone


def test_key_differs_per_audio_track():
    reset()
    p = film("h.mkv")
    assert key_of(p, 0) != key_of(p, 1)
    assert len(key_of(p, 0)) == 24


async def _run_all() -> int:
    # One loop for every test, and the lock made inside it: on Python 3.9 an
    # asyncio.Lock belongs to the loop that was current when it was created.
    env["_od_revive_lock"] = asyncio.Lock()
    n = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_"):
            continue
        r = fn()
        if asyncio.iscoroutine(r):
            await r
        n += 1
    return n


if __name__ == "__main__":
    try:
        print(f"test_od_revive: {asyncio.run(_run_all())} passed")
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
