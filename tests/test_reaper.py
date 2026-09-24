"""Unit tests for reaper.py — run: python tests/test_reaper.py"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import reaper  # noqa: E402

R = os.path.join(tempfile.gettempdir(), "slroot")
R2 = os.path.join(tempfile.gettempdir(), "slroot2")
J = os.path.join


def test_may_reap_guards():
    ok, why = reaper.may_reap(J(R, "Show.S01"), [R], [])
    assert ok and why == "", why
    # the root itself, and anything above it
    assert reaper.may_reap(R, [R], []) == (False, "no-root")
    assert reaper.may_reap(tempfile.gettempdir(), [R], [])[0] is False
    # a nested root inside the target
    assert reaper.may_reap(J(R, "sub"), [R, J(R, "sub", "lib")], []) == (False, "root")
    # outside every root
    assert reaper.may_reap(J(R2, "x"), [R], []) == (False, "no-root")
    # a sibling whose name only shares a prefix is not "inside"
    assert reaper.may_reap(R + "extra" + os.sep + "x", [R], [])[1] == "no-root"


def test_may_reap_owned():
    tgt = J(R, "Show.S01")
    # a live torrent with the same folder (a retry of the same release)
    assert reaper.may_reap(tgt, [R], [tgt]) == (False, "owned")
    # a live file inside the target folder
    assert reaper.may_reap(tgt, [R], [J(tgt, "e01.mkv")]) == (False, "owned")
    # target inside a live torrent's folder
    assert reaper.may_reap(J(tgt, "e01.srt"), [R], [tgt]) == (False, "owned")
    # an unrelated owner doesn't block
    assert reaper.may_reap(tgt, [R], [J(R, "Other")])[0]
    if os.name == "nt":   # case-insensitive on Windows
        assert reaper.may_reap(tgt.upper(), [R], [tgt]) == (False, "owned")


def test_is_sidecar():
    v = "Andor S02E03 Harvest.mkv"
    assert reaper.is_sidecar(v, "Andor S02E03 Harvest.en.opensubs.srt")
    assert reaper.is_sidecar(v, "Andor S02E03 Harvest.eng.ai.SRT")
    assert reaper.is_sidecar(v, "Andor S02E03 Harvest.en.opensubs.2.ass")
    assert not reaper.is_sidecar(v, v)
    assert not reaper.is_sidecar(v, "Andor S02E04 Other.en.srt")
    assert not reaper.is_sidecar(v, "Andor S02E03 Harvest.nfo")
    # a prefix-sharing name without the dot is another episode
    assert not reaper.is_sidecar("Show E1.mkv", "Show E10.en.srt")


def test_parts_hash():
    h = "da074059885d44743d9e091e3bfa10e47524a573"
    assert reaper.parts_hash("." + h + ".parts") == h
    assert reaper.parts_hash("." + h.upper() + ".parts") == h
    assert reaper.parts_hash(h + ".parts") is None
    assert reaper.parts_hash(".abc.parts") is None
    assert reaper.parts_hash("movie.mkv") is None


def test_retry_delay():
    assert reaper.retry_delay(0) == 0.0
    assert reaper.retry_delay(1) == 30.0
    prev = 0.0
    for a in range(1, reaper.MAX_ATTEMPTS + 1):
        d = reaper.retry_delay(a)
        assert d is not None and d >= prev
        prev = d
    assert reaper.retry_delay(reaper.MAX_ATTEMPTS + 1) is None


def test_prunable_parents():
    f = J(R, "Show", "Season 1", "e01.mkv")
    got = reaper.prunable_parents(f, [R])
    assert got == [reaper.norm(J(R, "Show", "Season 1")), reaper.norm(J(R, "Show"))], got
    assert reaper.prunable_parents(J(R, "top.mkv"), [R]) == []
    assert reaper.prunable_parents(J(R2, "x", "y.mkv"), [R]) == []


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
    print(f"test_reaper: {n} tests passed")
