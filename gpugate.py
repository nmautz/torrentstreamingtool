"""Is the all-GPU prep path worth trying on the next encode?

The all-GPU pipeline (decode -> scale_cuda -> nvenc, frames never leaving VRAM)
is the fastest way to prep a file that must fully re-encode, and it can wedge:
ffmpeg stops with no progress and no exit, a watchdog kills it, and the encode
starts again from zero on the transparent path. A stall is therefore not a
failure - the bundle still gets made - it is a tax, and a season pays it per
episode.

Measured on the box, 2026-10-04, This Is Us S03 (1080p HEVC, two video rungs,
two audio tracks): 3 clean all-GPU encodes took 237-247 s; 6 stalled ones took
449-580 s in total, of which 107-206 s was the attempt that got thrown away.
So the transparent path alone is roughly 350 s, and trying the GPU first only
pays while fewer than about four attempts in ten stall. At the six in nine seen
that night it loses.

The gate keeps the last few outcomes and, once stalls are that common, stops
trying for a while. It then tries ONCE: a clean run reopens it, another stall
closes it again at the cost of that one attempt.

State is per process. A restart forgets it and relearns for the price of
`TRIP` stalls, which is cheaper than persisting a verdict that a driver or
ffmpeg update may have made wrong.

Leaf module: stdlib only, no `main` import. Tests in `tests/test_gpugate.py`.
"""

from collections import deque

WINDOW = 4      # outcomes remembered
TRIP = 2        # stalls within WINDOW that close the gate
REST = 8        # qualifying encodes that skip the GPU attempt once closed


class Gate:
    def __init__(self, window=WINDOW, trip=TRIP, rest=REST):
        self.window, self.trip, self.rest = window, trip, rest
        self._recent = deque(maxlen=window)   # True = stalled
        self._resting = 0                     # skips still owed
        self._probing = False                 # the one attempt after a rest

    def allow(self):
        """Call once per encode that qualifies for the all-GPU path. False means
        go straight to the transparent path; the call itself counts the skip."""
        if self._resting > 0:
            self._resting -= 1
            if self._resting == 0:
                self._probing = True
            return False
        return True

    def record(self, stalled):
        """The outcome of an all-GPU attempt that `allow()` let through. Only a
        watchdog kill is a stall: a clean exit and an outright ffmpeg error
        (which fails in seconds, not minutes) are both `False`. Returns True
        when this outcome closed the gate."""
        stalled = bool(stalled)
        if self._probing:
            self._probing = False
            self._recent.clear()
            if stalled:
                self._resting = self.rest
                return True
            return False
        self._recent.append(stalled)
        if sum(self._recent) >= self.trip:
            self._recent.clear()
            self._resting = self.rest
            return True
        return False

    def state(self):
        return {"resting": self._resting, "probing": self._probing,
                "recent_stalls": sum(self._recent), "recent": len(self._recent)}
