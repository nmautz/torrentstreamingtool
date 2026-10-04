"""Unit tests for analyzer.py's fingerprint capture — run: python tests/test_analyzer_fp.py

No media binaries needed: fpcalc and ffmpeg are replaced by fakes. What is pinned here is
which tool gets asked, in what order — the head fingerprint must fall back to the ffmpeg
pipe when fpcalc cannot decode the file itself (the static fpcalc build has no DTS).
"""
import io
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import analyzer  # noqa: E402

FP_OUT = "DURATION=360\nFINGERPRINT=11,22,33\n"


class _Fakes:
    """Stand-ins for subprocess.run (fpcalc) and subprocess.Popen (ffmpeg)."""

    def __init__(self, direct_ok: bool, piped_ok: bool = True, ffmpeg: bool = True):
        self.direct_ok, self.piped_ok, self.ffmpeg = direct_ok, piped_ok, ffmpeg
        self.calls: list[str] = []
        self.ff_cmd: list[str] = []

    def run(self, cmd, **kw):
        if cmd[-1] == "-":                       # fpcalc reading WAV from stdin
            self.calls.append("piped")
            assert kw.get("input") == b"WAV", "fpcalc must be fed ffmpeg's output"
            ok = self.piped_ok
            return subprocess.CompletedProcess(cmd, 0 if ok else 2,
                                               stdout=FP_OUT.encode() if ok else b"", stderr=b"")
        self.calls.append("direct")
        ok = self.direct_ok
        return subprocess.CompletedProcess(cmd, 0 if ok else 2,
                                           stdout=FP_OUT if ok else "", stderr="")

    def popen(self, cmd, **kw):
        self.calls.append("ffmpeg")
        self.ff_cmd = list(cmd)

        class _P:
            stdout = io.BytesIO(b"WAV")

            def wait(self, timeout=None):
                return 0
        return _P()


def _with(fakes: _Fakes, fn):
    saved = (subprocess.run, subprocess.Popen, analyzer.fpcalc_bin, analyzer.ffmpeg_bin)
    subprocess.run, subprocess.Popen = fakes.run, fakes.popen
    analyzer.fpcalc_bin = lambda: "fpcalc"
    analyzer.ffmpeg_bin = lambda: "ffmpeg" if fakes.ffmpeg else None
    try:
        return fn()
    finally:
        subprocess.run, subprocess.Popen, analyzer.fpcalc_bin, analyzer.ffmpeg_bin = saved


def _seek_arg(cmd: list[str]) -> str:
    return cmd[cmd.index("-ss") + 1]


def test_head_uses_fpcalc_directly_when_it_can_decode():
    f = _Fakes(direct_ok=True)
    assert _with(f, lambda: analyzer._fpcalc_raw("ep.mkv", 360, 0)) == [11, 22, 33]
    assert f.calls == ["direct"], f.calls       # no ffmpeg spawned for a file that works


def test_head_falls_back_to_ffmpeg_when_fpcalc_cannot_decode():
    # The DTS case: fpcalc exits 2 with "Decoder not found" and prints no fingerprint.
    f = _Fakes(direct_ok=False)
    assert _with(f, lambda: analyzer._fpcalc_raw("ep.mkv", 360, 0)) == [11, 22, 33]
    assert f.calls == ["direct", "ffmpeg", "piped"], f.calls
    assert _seek_arg(f.ff_cmd) == "0.000"       # same time base as the direct read
    assert "ep.mkv" in f.ff_cmd


def test_head_is_empty_only_when_both_paths_fail():
    f = _Fakes(direct_ok=False, piped_ok=False)
    assert _with(f, lambda: analyzer._fpcalc_raw("ep.mkv", 360, 0)) == []
    f = _Fakes(direct_ok=False, ffmpeg=False)
    assert _with(f, lambda: analyzer._fpcalc_raw("ep.mkv", 360, 0)) == []
    assert f.calls == ["direct"], f.calls


def test_tail_always_goes_through_ffmpeg_at_the_float_it_was_given():
    f = _Fakes(direct_ok=True)
    assert _with(f, lambda: analyzer._fpcalc_raw("ep.mkv", 600, 826.134)) == [11, 22, 33]
    assert f.calls == ["ffmpeg", "piped"], f.calls
    assert _seek_arg(f.ff_cmd) == "826.134"


def test_parse_fingerprint():
    assert analyzer._parse_fingerprint(FP_OUT) == [11, 22, 33]
    assert analyzer._parse_fingerprint("DURATION=3\n") == []
    assert analyzer._parse_fingerprint("FINGERPRINT=\n") == []
    assert analyzer._parse_fingerprint("FINGERPRINT=1,x,3\n") == []
    assert analyzer._parse_fingerprint("") == []


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
    print(f"test_analyzer_fp: {n} tests passed")
