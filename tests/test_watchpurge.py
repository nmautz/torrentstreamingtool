"""Unit tests for `watchpurge.py`. Run with plain python, no deps:

    python tests/test_watchpurge.py      (or `make test`)

The module decides what "Delete watched" removes from the host. Every rule here
guards against deleting something a person still wanted, so the cases lean on
the keeps: someone else part-way through, a file in use, a compressed file, an
unreadable record, a profile selection that only half watched it.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import watchpurge as wp          # noqa: E402

_PASS = 0
_FAIL = []


def eq(name, got, want):
    global _PASS
    if got == want:
        _PASS += 1
    else:
        _FAIL.append("%s\n     got:  %r\n     want: %r" % (name, got, want))


# ── record_state ─────────────────────────────────────────────────────────────
eq("completed is watched", wp.record_state({"completed": True, "position_sec": 10}), "watched")
eq("near the end but not completed is NOT watched",
   wp.record_state({"completed": False, "position_sec": 1400, "duration_sec": 1420}), "in-progress")
eq("a stray tap is nothing", wp.record_state({"position_sec": 1.2}), "")
eq("exactly the floor is nothing", wp.record_state({"position_sec": 5}), "")
eq("truthy non-bool completed is not trusted", wp.record_state({"completed": "yes"}), "")
eq("garbage position is nothing", wp.record_state({"position_sec": "abc"}), "")
eq("not a dict is nothing", wp.record_state(None), "")
eq("reset (unwatched) record is nothing",
   wp.record_state({"completed": False, "position_sec": 0, "played_sec": 0}), "")

# ── file_viewers ─────────────────────────────────────────────────────────────
w, p = wp.file_viewers({
    "a": {"completed": True},
    "b": {"completed": False, "position_sec": 600},
    "c": {"position_sec": 2},
    "d": "junk",
})
eq("watched set", w, {"a"})
eq("in-progress set", p, {"b"})
eq("empty records", wp.file_viewers({}), (set(), set()))
eq("None records", wp.file_viewers(None), (set(), set()))

# ── is_selected ──────────────────────────────────────────────────────────────
eq("single profile watched", wp.is_selected({"a"}, ["a"]), True)
eq("single profile not watched", wp.is_selected({"b"}, ["a"]), False)
eq("all: both watched", wp.is_selected({"a", "b"}, ["a", "b"], "all"), True)
eq("all: one missing", wp.is_selected({"a"}, ["a", "b"], "all"), False)
eq("any: one is enough", wp.is_selected({"a"}, ["a", "b"], "any"), True)
eq("any: none", wp.is_selected({"c"}, ["a", "b"], "any"), False)
eq("no profiles selects nothing", wp.is_selected({"a"}, [], "any"), False)
eq("no profiles selects nothing (all)", wp.is_selected({"a"}, [], "all"), False)
eq("unknown match falls back to all", wp.is_selected({"a"}, ["a", "b"], "bogus"), False)


# ── verdict ──────────────────────────────────────────────────────────────────
def v(**kw):
    base = dict(watched_by={"a"}, in_progress_by=set(), profile_ids=["a"],
                match="all", on_disk=True, busy=False, compressed=False)
    base.update(kw)
    return wp.verdict(**base)


eq("watched, idle, on disk -> delete", v(), wp.DELETE)
eq("not watched -> unlisted", v(watched_by=set()), None)
eq("nothing on the host -> unlisted", v(on_disk=False), None)
eq("someone ELSE part-way through keeps it", v(in_progress_by={"b"}), wp.KEEP_IN_PROGRESS)
eq("in use keeps it", v(busy=True), wp.KEEP_IN_USE)
eq("compressed keeps it", v(compressed=True), wp.KEEP_COMPRESSED)
eq("in-progress outranks in-use", v(in_progress_by={"b"}, busy=True), wp.KEEP_IN_PROGRESS)
eq("in-use outranks compressed", v(busy=True, compressed=True), wp.KEEP_IN_USE)
eq("unwatched AND in progress -> unlisted, not kept",
   v(watched_by=set(), in_progress_by={"a"}), None)
eq("all-match half watched -> unlisted",
   v(watched_by={"a"}, profile_ids=["a", "b"], match="all"), None)
eq("any-match half watched -> delete",
   v(watched_by={"a"}, profile_ids=["a", "b"], match="any"), wp.DELETE)

# ── group_rows ───────────────────────────────────────────────────────────────
rows = [
    {"group": "s:x", "group_title": "Xena", "verdict": wp.DELETE, "bytes": 100, "path": "x1"},
    {"group": "s:x", "group_title": "Xena", "verdict": wp.KEEP_IN_USE, "bytes": 999, "path": "x2"},
    {"group": "s:a", "group_title": "Alf", "verdict": wp.DELETE, "bytes": 500, "path": "a1"},
    {"group": "s:a", "group_title": "Alf", "verdict": wp.DELETE, "bytes": 50, "path": "a2"},
    {"group": "s:n", "group_title": "Nope", "verdict": None, "bytes": 1, "path": "n1"},
    {"group": "s:k", "group_title": "Kept", "verdict": wp.KEEP_COMPRESSED, "bytes": 7, "path": "k1"},
]
g = wp.group_rows(rows)
eq("unlisted rows make no group", [x["group"] for x in g], ["s:a", "s:x", "s:k"])
eq("bytes count deletes only", [x["bytes"] for x in g], [550, 100, 0])
eq("order inside a group kept", [r["path"] for r in g[0]["delete"]], ["a1", "a2"])
eq("keeps split out", [r["path"] for r in g[1]["keep"]], ["x2"])
eq("all-kept group still listed (so the preview can say why)", g[2]["delete"], [])
eq("title carried", g[0]["title"], "Alf")
eq("empty input", wp.group_rows([]), [])


if __name__ == "__main__":
    for f in _FAIL:
        print("FAIL", f)
    print("watchpurge: %d passed, %d failed" % (_PASS, len(_FAIL)))
    sys.exit(1 if _FAIL else 0)
