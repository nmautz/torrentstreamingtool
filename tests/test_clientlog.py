"""Unit tests for clientlog.py — no deps, no venv, no running services.

Run: python3 tests/test_clientlog.py
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import clientlog  # noqa: E402

FAILS = []


def check(name, got, want):
    if got != want:
        FAILS.append(f"{name}: got {got!r}, want {want!r}")
    else:
        print(f"  ok  {name}")


def row(t, ev, **kw):
    d = {"t": t, "ev": ev}
    d.update(kw)
    return json.dumps(d, sort_keys=True)


def text(*rows):
    return "\n".join(rows) + "\n"


# ── merge ────────────────────────────────────────────────────────────────────
print("merge")

a = row("2026-09-22T03:38:51.071Z", "launch")
b = row("2026-09-22T03:38:52.000Z", "snap", pos=1)
c = row("2026-09-22T03:39:00.000Z", "progress", pos=5)

merged, added, skipped = clientlog.merge("", text(a, b))
check("first upload adds everything", (added, skipped), (2, 0))
check("first upload text", merged, text(a, b))

# The phone always re-sends its whole file; the overlap must not duplicate.
merged, added, skipped = clientlog.merge(merged, text(a, b, c))
check("re-upload appends only the new row", (added, skipped), (1, 2))
check("re-upload preserves order", merged, text(a, b, c))

# A second upload of exactly the same bytes changes nothing at all.
merged2, added, skipped = clientlog.merge(merged, text(a, b, c))
check("idempotent re-upload", (added, skipped), (0, 3))
check("idempotent text unchanged", merged2, merged)

# Two rows sharing a millisecond are two rows. The 2026-09-22 log had
# disarm/stopNative at an identical timestamp; a (t, ev)-keyed dedupe would be
# fine there but a t-only high-water mark would have dropped the second.
same_t = [row("2026-09-23T00:38:03.788Z", "disarm"),
          row("2026-09-23T00:38:03.788Z", "stopNative")]
merged3, added, _ = clientlog.merge("", text(*same_t))
check("same-millisecond rows both kept", added, 2)

# A device that cleared its log and started over sends OLDER timestamps than
# what the server holds. They are still new rows and must still land.
old = row("2026-09-01T00:00:00.000Z", "launch")
merged4, added, _ = clientlog.merge(text(c), text(old))
check("older timestamps still append", added, 1)
check("older row appended, not re-sorted", merged4, text(c, old))

# Blank lines and trailing whitespace are not rows.
merged5, added, _ = clientlog.merge("", "\n\n" + a + "  \n\n")
check("blank lines ignored", added, 1)
check("row is whitespace-stripped", merged5, text(a))


# ── trim_to ──────────────────────────────────────────────────────────────────
print("trim_to")

small = text(a, b, c)
check("under the cap is untouched", clientlog.trim_to(small, 10_000), (small, 0))

big = text(*[row(f"2026-09-22T00:00:{i:02d}.000Z", "snap", n=i) for i in range(10)])
trimmed, dropped = clientlog.trim_to(big, 1)
check("over the cap drops the oldest half", dropped, 5)
check("the newest half survives", trimmed.splitlines()[0],
      row("2026-09-22T00:00:05.000Z", "snap", n=5))


# ── high_water / drop_through (the clear handshake) ──────────────────────────
print("clear handshake")

span = text(a, b, c)
check("high_water is the newest timestamp", clientlog.high_water(span),
      "2026-09-22T03:39:00.000Z")
check("high_water of nothing", clientlog.high_water(""), "")
# The newest row is not always the last one: a device whose clock stepped back
# appends an older timestamp, and a mark read off the final line would then be
# too low and fail to drop what the reader already saw.
check("high_water scans, not tails",
      clientlog.high_water(text(c, row("2026-09-01T00:00:00.000Z", "launch"))),
      "2026-09-22T03:39:00.000Z")
# A half-written final line must not become the mark.
check("high_water ignores a torn tail",
      clientlog.high_water(text(a, b) + '{"t":"2026-12'),
      "2026-09-22T03:38:52.000Z")

mark = clientlog.high_water(span)
kept, dropped = clientlog.drop_through(span, mark)
check("a re-send of exactly what was cleared drops entirely", (kept, dropped), ("", 3))

# Rows written AFTER the clear are what make this a prefix drop rather than
# discarding the upload — the reader has not seen them.
after = row("2026-09-22T04:00:00.000Z", "launch")
kept, dropped = clientlog.drop_through(text(a, b, c, after), mark)
check("post-clear rows survive", kept, text(after))
check("pre-clear rows counted", dropped, 3)

check("no mark drops nothing", clientlog.drop_through(span, ""), (span, 0))
# A row with no usable timestamp cannot be placed; keeping it is the safe error.
odd = '{"ev":"snap"}'
kept, _ = clientlog.drop_through(text(a, odd), mark)
check("timestamp-less rows are kept", kept, text(odd))


# ── summarize ────────────────────────────────────────────────────────────────
print("summarize")

s = clientlog.summarize(text(
    row("2026-09-22T03:38:51.071Z", "launch", cat="app"),
    row("2026-09-22T03:39:00.000Z", "progress", cat="net", status=200),
    row("2026-09-22T03:39:15.000Z", "progress", cat="net", status=0,
        err="The request timed out."),
    row("2026-09-22T03:39:30.000Z", "progress", cat="net", status=500),
    row("2026-09-23T00:00:00.000Z", "launch", cat="app"),
))
check("row count", s["rows"], 5)
check("span start", s["first"], "2026-09-22T03:38:51.071Z")
check("span end", s["last"], "2026-09-23T00:00:00.000Z")
check("launch count", s["launches"], 2)
check("errors counts err strings and non-2xx alike", s["errors"], 2)
check("event histogram", s["events"], {"progress": 3, "launch": 2})
check("category histogram", s["cats"], {"net": 3, "app": 2})
check("empty transcript", clientlog.summarize("")["rows"], 0)


# ── select ───────────────────────────────────────────────────────────────────
print("select")

body = text(
    row("2026-09-22T01:00:00.000Z", "launch", cat="app"),
    row("2026-09-22T02:00:00.000Z", "snap", cat="ext"),
    row("2026-09-22T03:00:00.000Z", "progress", cat="net", status=200),
    row("2026-09-22T04:00:00.000Z", "progress", cat="net", status=0, err="lost"),
    row("2026-09-22T05:00:00.000Z", "bundle", cat="offline"),
)

check("no filter returns everything", len(clientlog.select(body)), 5)
check("since is inclusive of its own timestamp",
      len(clientlog.select(body, since="2026-09-22T03:00:00.000Z")), 3)
check("until is inclusive",
      len(clientlog.select(body, until="2026-09-22T02:00:00.000Z")), 2)
check("event filter", len(clientlog.select(body, events=["progress"])), 2)
check("category filter", len(clientlog.select(body, cats=["offline", "ext"])), 2)
# Pre-category rows are in every real transcript. `summarize` calls them "-", so
# selecting on "-" has to find them or the two views disagree.
legacy = text(row("2026-09-22T00:30:00.000Z", "snap"),
              row("2026-09-22T00:31:00.000Z", "snap", cat="ext"))
check("uncategorised rows summarize as '-'", clientlog.summarize(legacy)["cats"],
      {"-": 1, "ext": 1})
check("uncategorised rows select as '-'", len(clientlog.select(legacy, cats=["-"])), 1)
check("errors_only keeps err rows", len(clientlog.select(body, errors_only=True)), 1)
check("limit takes the head", clientlog.select(body, limit=2),
      clientlog.split_rows(body)[:2])
check("limit with tail takes the end", clientlog.select(body, limit=2, tail=True),
      clientlog.split_rows(body)[-2:])

# A half-written line at the end of an upload must not take the whole read down.
torn = body + '{"t":"2026-09-22T06:00:00.000Z","ev":"sn'
check("corrupt tail survives an unfiltered read", len(clientlog.select(torn)), 6)
check("corrupt tail is excluded once filtering",
      len(clientlog.select(torn, events=["progress"])), 2)
check("corrupt tail does not break summarize",
      clientlog.summarize(torn)["rows"], 6)


print()
if FAILS:
    print(f"FAILED ({len(FAILS)}):")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("clientlog: all tests passed")
