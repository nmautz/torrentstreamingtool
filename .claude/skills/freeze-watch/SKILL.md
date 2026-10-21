---
name: freeze-watch
description: The freeze standard for the StreamLink box (how long the server may stop answering before we fix the cause) and the protocol for watching the box's logs against it over a timed window, fixing and redeploying when it is breached. Use when the user says "watch the box", "watch for freezes", "check the logs for slowdowns / event-loop blocks", "freeze watch", or asks whether the box is meeting the freeze standard.
---

# Freeze watch

A freeze is the server not answering. On this box that is almost always the asyncio
event loop running something that waits on the disk (a `stat`, `exists`, `resolve`,
`open`) or holds the GIL (a big `json.loads`), and while it does, every request,
the TV's remote, the phone's player and VLC's controls wait with it. CPU, RAM and RDP
look fine throughout. Read docs/GOTCHAS.md § `get_library`/`put_library` and
§ Sync file I/O on the event loop once per session; they list every cause found so far.

## The standard

All thresholds live in `freeze_report.py` (top of the file). Change them there and
here together.

| Measured | Threshold | Action |
|---|---|---|
| Event-loop block (`lagmax=`, `LOOP BLOCKED`) | **>= 1000 ms, once** | **HARD.** Find the cause and fix it. No "it was only once". |
| Event-loop block | 250-999 ms, **same cause >= 3 times in one window** | **REPEAT.** Fix it. |
| Event-loop block | 250-999 ms, scattered | Note it in the watch log; fix only if it becomes a REPEAT. |
| Event-loop block | < 250 ms | Not a freeze. Ignore. |
| `/healthz` self-probe (`probe=`) with the loop free | >= 1000 ms | **HARD.** The loop was free but requests weren't served: the library lock, the thread pool, or the proxy. |
| No vitals line for > 45 s | any | **HARD.** The process (or the whole machine) stopped. Check whether the box itself froze (other processes, `dsk=` in the lines either side) before blaming our code, and file it either way. |
| One request slow (>= 5 s) with the loop free | - | Not a freeze. Listed; give it an issue if a person waits on it. |

Why these numbers: under ~100 ms feels instant; at about a second a remote press
looks ignored and gets pressed again; a player's buffer hides a few seconds of
server stall but the controls do not. 1 s is the line where a person notices,
every time. 250 ms is where a single block is still unnoticed but a cause that
keeps producing it is a bug waiting for a busier disk, which is how every past
freeze started (a 3 ms `stat` on an idle disk is a 26 s one on a busy one).
Judge by cause, not by the number alone: the same code path can be 5 ms tonight and
30 s tomorrow.

**What can and can't be seen.** `lagmax=` (20.25.18+) is the worst loop turn in each
30 s, measured to within 0.1 s. `LOOP BLOCKED` names the code for any block over
0.5 s. A C call holding the GIL for its whole run (a huge `json.loads` in a worker
thread) delays the sampler too, so its report names what ran after; a HARD
`lagmax` with no named cause is that case or a block just under 0.5 s. Builds before
20.25.18 have no `lagmax=` and a 2 s sampler: the report says so, and anything under
2 s on them is invisible.

## Running the check

```bash
STREAMLINK_ADMIN_PASSWORD='<admin pass>' python3 .claude/skills/freeze-watch/freeze_report.py \
    --box https://192.168.0.106 --since "YYYY-MM-DD HH:MM:SS" --save <dir>
```

`--since` is the start of the watch window (default: the last restart). Exit 0 =
PASS, 2 = ACT, 1 = box unreachable (that is itself a finding if it lasts). `--save`
keeps the raw logs for reading the `LOOP BLOCKED` stacks. `--from-dir <dir>` reads
saved logs instead of the box. The admin password and box address are in the
memory file `streamlink-client-logs.md`.

## The watch protocol

Keep all state in one file, `<scratchpad>/freeze-watch.md`. The conversation may be
compacted mid-watch; that file is what survives. It holds: the window's start time
and build, the planned end, one line per check, every finding and what was done.
Read it first on every wake.

1. **Start a window.** Note the time, the build (`/api/version`) and the end time
   (start + the length asked for, usually 4 h) in the watch file.
2. **Every 30 minutes**, run the report with `--since <window start>`. Write one line
   to the watch file: time, verdict, worst `lagmax`, worst probe, anything new.
   Tell the user one short line. Say nothing more on a PASS.
3. **Between checks, pause yourself.** Start a background timer and end the turn:
   `Bash` with `run_in_background: true` running `perl -e 'sleep 1800'`. Its exit
   wakes you. Don't poll and don't sleep in the foreground.
4. **On ACT**, find the cause from the saved logs: the `LOOP BLOCKED` stack (innermost
   frame in our code), the stall dump, the vitals lines either side. Then fix it the
   way this repo fixes them: move the disk work into a `*_sync` helper run through
   `asyncio.to_thread`, compare strings before touching the disk, cache what does not
   change. Add the function to an AST rule (`tests/test_loop_paths.py` `ON_LOOP`, or
   `tests/test_prep_space.py`) so it can't come back. Version bump, CHANGELOG,
   GOTCHAS paragraph, `make test`, `make surface`, commit (`Closes #N` + `in-alpha` if
   an issue exists, else open one), push to `alpha`.
5. **Deploy** with the updater (`reboot: true`, see memory `streamlink-box-deploy.md`).
   Reboot only if the user authorised reboots for this session; otherwise ask with
   AskUserQuestion, naming what is playing. Before rebooting, check `/api/state` and
   say what was interrupted.
6. **Any deploy or reboot restarts the window**: new start time, new end time, a line
   in the watch file saying why. A delayed bug only counts as absent once a full
   window has run on the build that would have it.
7. **A cause outside our code** (the machine itself stalled, another process saturating
   the disk) is still written down and given an issue. Fix what we can (our own I/O
   that made it worse), say plainly what we can't.
8. **End of window**: a short summary to the user: the build, the window, PASS or
   what was fixed, each fix's before/after numbers, issues opened, and anything below
   the threshold worth knowing. Update the verify issue for the build (#100 at the
   time of writing).

Never mark a window PASS from a report that could not reach the box, or from a build
older than the one meant to be under watch.
