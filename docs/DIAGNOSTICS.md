# Diagnostics

Runtime health instrumentation: `diagnostics.py` plus its wiring in `main.py`,
`run.py` and `daemon.py`. Read this when the server is unreachable, slow, or
"crashed" with nothing in the logs.

---

## Why this exists

On **2026-09-13** the dashboard and TV UI went dead for roughly 50 minutes and
the operator eventually power-cycled the host by hand. The logs could not say
why — and that, not the outage itself, is the problem this subsystem solves.

What the logs *did* show:

- The process was **healthy throughout**: a flat ~168 log lines/min of VLC and
  qBittorrent polling right up to the manual reboot. No stall, no traceback, no
  memory symptom. The event loop was plainly turning.
- Inbound requests arriving **over HTTPS were still being served** as late as
  14:44:11 — a `200` on `/api/admin/status`, seconds before the reboot.

What they could not show, and why:

| Blind spot | Cause |
|---|---|
| **No record of any request to port 80** — the port the dashboard and TV UI use | `run.py` and `daemon.py` both started uvicorn with `log_level="warning"` and a `log_config` stub containing **no handlers**. uvicorn emits access lines at INFO, so they were discarded. |
| Inbound HTTPS traffic visible only by accident | The :443 proxy forwards to `127.0.0.1:80` with `httpx`, and **httpx's own INFO logging** recorded those hops. Plain-HTTP clients bypass the proxy and left no trace at all. |
| No proof the server was still accepting | Nothing probed the listening socket from inside the process. A wedged server and a healthy one look identical in a log of outbound polls. |
| No view of the loop, thread pool or library lock | Never collected. These are the three things that starve request handling while leaving background pollers untouched. |
| Real events buried | ~168 lines/min of httpx `200 OK` chatter, plus a flood of benign `ConnectionResetError` (WinError 10054) logged at ERROR. |
| `print()` diagnostics unreachable | The updater and reboot paths report via `print()`, not the `streamlink` logger — no `[reboot]`/`[updater]` lines appear in `streamlink_service.log` at all. |

### The failure mode this is built to catch

`get_library()` holds `_lib_lock` **across** `asyncio.to_thread(_load_lib_raw)`.
So if the default thread pool saturates, the lock is held while waiting for a
worker, and **every request touching the library stalls** — while the VLC and
qBittorrent pollers, which are pure async `httpx` and take no lock, carry on at
full cadence.

The result is a box that looks perfectly healthy in the logs and is dead to
every client. That asymmetry is invisible unless something is watching the lock,
the pool and the socket specifically. That is what `diagnostics.py` does.

---

## Client logs from the iOS app

`logs/client/<device>.log` is **not** written by the server — it is uploaded by the app
through `POST /api/diag/client-log` (☰ App → Settings → Playback → **Send log to
server**). It is newline-delimited JSON with absolute timestamps, written on the device by
`DiagLog` in `NativePlayback.swift`, and it survives app restarts, which is the whole
point: the in-memory diagnostics trail is 40 rows that die with the process and timestamps
in seconds-since-launch, so it can answer "what happened in this ten-minute test" and
nothing longer.

### Retention — the part that is easy to get wrong

**The device sends its WHOLE file every time and the server MERGES it.** Before 18.8.0 the
upload was written straight over the previous one, which made the phone's rolling window
the server's retention policy: send twice and the first send was gone. Now the server
keeps an append-only transcript per device and takes only the rows it has never seen
(`clientlog.merge`, keyed on the hashed line — see `clientlog.py` for why not on `t`).
Sending often therefore costs nothing and loses nothing, and a phone that has been off the
network for a week catches up in one tap.

**Nothing deletes a client log except a reader.** They live in a *subdirectory*, and that
is the mechanism, not an aesthetic choice: `_archive_old_logs()` at update time,
`DELETE /api/admin/logs` and the `_bundle` download all iterate top-level **files**, so a
directory is invisible to every sweep for free.

The one thing that removes them is `DELETE /api/admin/client-logs` (optionally
`?device=<slug>`). Because the server cannot reach a phone, that clear is finished by the
**next upload**: the response carries `clear_local`, the app deletes its own copy and
writes a `log-cleared-by-server` row so the gap in the next transcript is explained rather
than mysterious.

The clear also records a **high-water mark** — the newest timestamp the transcript held
when it was deleted — and the next upload drops every row at or below it (`dropped_pre_clear`
in the response). Without that the clear would achieve nothing: the device is still holding
the rows the reader just finished with and sends them all again, and the merge would
restore every one. It drops a *prefix* rather than discarding the whole body so that rows
written **after** the clear, which the reader has not seen, still land. The mark is compared
against the device's own timestamps, so no clock is compared to another clock.

### Reading one

```bash
TOK=$(curl -sk -X POST https://<box>/api/admin/login \
      -H 'Content-Type: application/json' -d '{"password":"<admin pass>"}' | jq -r .token)

# Which devices, how much, and is the thing I care about even in here?
curl -sk https://<box>/api/admin/client-logs -H "Authorization: Bearer $TOK" | jq

# Everything, or a slice of it
curl -sk "https://<box>/api/admin/client-logs/<device>" -H "Authorization: Bearer $TOK"
curl -sk "https://<box>/api/admin/client-logs/<device>?cats=offline,ext&since=2026-09-22" \
     -H "Authorization: Bearer $TOK"
curl -sk "https://<box>/api/admin/client-logs/<device>?errors_only=true" \
     -H "Authorization: Bearer $TOK"

# Done with it
curl -sk -X DELETE https://<box>/api/admin/client-logs -H "Authorization: Bearer $TOK"
```

The listing's per-event and per-category histograms exist because the first question asked
of one of these files is always "is the thing I care about even in here?", and a long
transcript is mostly `snap` rows. Filter server-side rather than downloading it all.

### Categories

Every row carries a `cat`. Rows written before 18.8.0 have none and read as `-`.

| `cat` | Covers |
|---|---|
| `play` | the player's transport — what it was told, what it did, who asked |
| `ext` | external display: screens, scenes, windows, layers, audio routes |
| `offline` | downloaded bundles: fetch, verify, local server, offline play, sync |
| `net` | anything crossing to the host (progress, session) |
| `app` | lifecycle: launch, audio session, interruptions |

Rows come from **both** engines. The web player writes through the `np.log` bridge
(`_npLog` in `static/index.html`) into the same file on the same clock — before 18.8.0 the
log was native-only, which made it a log of the wrong thing, since on-device playback is
mostly the WKWebView's own `<video>` and the offline path barely touches the native player.
JS-written rows carry `src: "js"`.

### Useful rows

| `ev` | Means |
|---|---|
| `launch` | app start — carries the build; use these to split a long log into sessions |
| `crash` | **the previous run died and said so.** `kind` is `exception` (with `name`/`reason`) or `signal` (with `sig`); `last` is the final event `DiagLog.write` was handed, `stack` the first 24 frames, `was` whether the app was foreground or background, `since` when that was last true. Written at the NEXT launch, immediately above its `launch` row |
| `prev-launch-dirty` | the previous run never reached `applicationWillTerminate` and left **no** signal record: a watchdog kill, a memory kill, or the user swiping the app away. `was: "fg"` is a defect; `was: "bg"` is usually iOS reclaiming a backgrounded app and is expected |
| `snap` | a diagnostics trail row (every field the Monitor diagnostics panel shows) |
| `startNative` | handoff began — `reason` (`early`/`background`/`manual`), `shouldPlay`, `extWindow`. Written as soon as the player exists and its surface is attached, so it means "we got this far", not "it all worked" |
| `stopNative` | handoff ended, with the position it ended at |
| `disarm` | teardown, with the `reason` threaded from JS (`unload` = episode advance, `stop`, `yield`, `transport-next`/`-prev`, `bgplay-off`, `not-armable`), the position, and whether a final flush was posted |
| `rearm-swap` | an arm changed which FILE this is while a player was running; the outgoing file was flushed first. **Must be followed by `native-swap`** — see below |
| `native-swap` | the running player really moved onto the new file (same player, same layer, same external window). This is what makes a skip land on the monitor |
| `swap-no-url` | the page armed a file the native side cannot play, so the swap could not happen and the player is still on the old episode |
| `arm-dropped-hold` | `_npArm()` turned into a teardown while native held the display, because the new file has no native master. The display is about to go back to the phone |
| `advance` | an advance. From the page: `holding`, `ext`, `nextArmed`, `path` (`normal` vs `teardown-rebuild`). From native: `reason` (`ended` vs `credits`) plus the `introEnd`/`creditsAt` it promoted for the incoming episode (18.12.0) |
| `native-advanced` | the **good** end-of-episode advance: native switched file without releasing the display |
| `seek` | a user-intent seek was committed (18.12.1) — `from`, `to`, `back`, the `engine`, the `buffered` ranges and, the field that matters, **`inBuf`**: was the target inside the buffer. `engine: avplayer` means it was proxied to the native player instead |
| `seek-verdict` | what became of it, 4.5 s later. `landed` / `elsewhere` / **`no-frames`** — the last meaning the element presented nothing at all, which is the shape a backward seek past the buffered edge takes and the one `_lpVerifySeek` cannot judge |
| `seek-swallowed` | the detector caught the pipeline still presenting the pre-seek position, and a rebuild followed |
| `seek-stuck` | **the one that actually fires** (18.12.3). A seek out of the buffer did not make the buffered set change — nothing is being fetched. `stage: "kick"` is the loader restart, `stage: "rebuild"` means the kick didn't take either and `_lpPipelineRebuild` ran. Carries the frozen `buffered` string, plus `ready` and `paused` |
| `auto-skip` | the **native** player fired a Smart Skip while it held the display (18.12.0) — `type` (`intro`/`credits`), `from`, and for an intro `to`. This is the only skip actor during a handoff; the page draws the tile but never fires |
| `native-skipped` | the page's record of the same event, written when native's `nativeSkipped` reaches it. Its absence under an `auto-skip` means the page was asleep at the time — normal, and the flag is reconciled on the next arm |
| `next-armed` | the next episode was handed to native (`via`: `already-ready` / `warmed`). From 18.12.0 the arm also carries that episode's skip windows |
| `hold-start` | native took the display; `elementWasPlaying` says whether a second engine was running |
| `display-handoff` | a display arrived **mid-episode** and the native player took it over on the spot (18.11.0). Follows a `sceneConnect` snap and its `earlyClaim`/`earlyHandoff` pair. Its absence after a `sceneConnect` with `native=false` is the pre-18.11.0 bug: window claimed, no player in it, glasses black |
| `display-lost-handback` | the display went away while native held it, so the page took playback back into its own element (18.11.0). Foreground only — unplugging while locked deliberately leaves the native player alone |
| `play-while-holding` | the web element tried to play while native held the display — always a bug, and it names the path |
| `setPaused` | who paused/resumed, by `src` (`user-transport`, `remote-play`, `remote-pause`, `transport-toggle`) |
| `transport` | a `timeControlStatus` change, with `requested`/`by` |
| `transport-stopped-itself` | the player stopped and **nobody asked** — carries `waitReason`, `itemErr`, `sess` and the audio route |
| `interruption` | audio-session interruption (began/ended, `shouldResume`) |
| `interruption-resumed` | we reactivated the session and resumed because our intent said "playing" |
| `route` | audio route change — the glasses are a route as well as a screen |
| `item-stalled` / `item-failed` | the native item ran dry or could not finish |
| `video-error` / `video-stalled` | the same, from the web element |
| `progress` | a progress POST **and its HTTP status**. `final: true` marks the forced flush at teardown; `try`/`retrying` show the retry |
| `progress-skipped` | a POST refused by a guard — `why` names the guard (`near-start`, `duration-0`, `no-item`, `no-file`, `no-server`), plus `native` |
| `progress-failed` | a web-player save that failed, and whether it was stashed offline or lost |
| `bundle-retry` / `bundle-complete` / `bundle-failed` | a download's course, not just its verdict |
| `bundle-healed` | an index entry marked complete by **reconciliation** rather than by finishing — check for this first when offline playback breaks on a bundle |
| `lms-start` / `lms-failed` / `lms-stop` | the loopback server, the middle link in the offline chain |
| `offline-completed` | an offline watch crossed the completion line, **with the played-time inputs** — the one case the server cannot measure |
| `offline-pending` / `offline-synced` | the offline→online handover from the side that knows what it is holding |
| `audio-session-failed` | `setActive` threw, with the app state at the time |

**`progress-skipped` is the row to look for first** when positions are not being saved. A
silent `guard` hid the duration-0 bug for a full day; that guard now says so.

Read `why` first, then `native`:

- **`why: near-start`** — correct behaviour, not a failure. Positions under 5 s are not
  saved (the native mirror of `saveProgress`'s near-zero guard).
- **`why: no-item` / `no-file` / `no-server` with `native: true`** — a player was up and
  a write was still refused, so something was never armed, or `armed` was cleared out
  from under a live player. That is a bug.
- **anything with `native: false`** — the player is already gone, so the state is *meant*
  to be missing. Expected noise.
- **`why: duration-0`** — the shape of the bug 18.6.1 fixed; worth a second look if it
  reappears.

That distinction is what 18.7.1 added, and it is the distinction that had been hiding the
disarm-order bug: a real 322 s position was being dropped by a row that looked exactly
like harmless post-teardown noise. See
[GOTCHAS.md § The one teardown path that wiped its state before saving it](GOTCHAS.md).

### Recipe: "the app crashed"

Since 18.10.0 a crash is a row, not a gap. Find the `launch` row for the run **after** the
one that died and read upwards from it — the report is written immediately above its own
`launch`.

1. **`crash` with `kind: "exception"`** — the best case. `name` and `reason` are UIKit's
   or AVFoundation's own words for what was wrong, and `stack` names the frames. This is
   the shape an `NSInternalInconsistencyException` or a bad KVO/observer teardown takes.
2. **`crash` with `kind: "signal"`** — a Swift runtime trap (nil force-unwrap, array
   bounds, a failed `as!`) arrives as `SIGTRAP`/`SIGILL`; a memory fault as `SIGSEGV`;
   `SIGABRT` without a preceding exception record is usually an assertion inside a system
   framework. Read `stack` for the first frame inside `StreamLink`.
3. **`prev-launch-dirty`** — nothing caught it. Check `was`: `"bg"` is almost always iOS
   reclaiming a backgrounded app and is not a bug; `"fg"` with an episode playing means a
   watchdog kill (the main thread was blocked) or a memory kill.
4. **`last` on either row is the breadcrumb.** `DiagLog.write` hands rows to a queue and
   returns, so the final rows of a crashing run are lost; `last` is the event name the
   queue never flushed, captured before it was queued. When `last` names a row you cannot
   find in the transcript, that is the queue's loss, not the app failing to reach it.

The handlers re-raise with the default disposition, so the device still writes its own
`.ips` report — Settings → Privacy & Security → Analytics & Improvements → Analytics
Data, named `StreamLink-<date>`. That is the symbolicated version of the same crash.

### Recipe: "I pressed Next and the monitor kept playing the old episode"

A **user-driven skip** is a different path from an episode ending, and it fails
differently: nothing changes on the display, but the title, the seek bar and the server's
progress all move on. Read it in this order.

1. `load-while-holding` — the page loaded a new file while native held the display.
2. `rearm-swap` — the arm carried the file change to the native side. `flushedAt` is the
   outgoing episode's final position and `nativePos` is where the player actually is; they
   should agree to within a second.
3. **`native-swap`** — the player moved. *This is the row that matters.* A `rearm-swap`
   with no `native-swap` after it is the 18.9.0 bug: the config advanced and the player
   did not, so the display is still on the old episode while every progress POST is filed
   under the new one. (Which is also how Resume ends up opening an episode that was never
   watched — check the `progress` rows between the two `rearm-swap`s and see whose `file`
   they name.)
4. If `swap-no-url` is there instead, the page armed a file with no native master.
5. If `arm-dropped-hold` is there, `_npArm()` disarmed rather than armed — the display is
   being handed back to the phone, and a `disarm`/`stopNative` pair follows.

`takeover` appears on this path and proves nothing: it routes to `startNative()`, which
guards on `!isNativeActive`, so while holding it is a no-op by construction.

### Recipe: "it advanced onto the phone instead of the glasses"

This is the *end-of-episode* version. With a display connected there are two advance paths
and they look identical from outside. `advance`'s `path` field names which one ran:

- **`path: "normal"` followed by `native-advanced`** — the native player switched file in
  place. The display never changed hands. This is what should happen.
- **`path: "teardown-rebuild"`** (i.e. `holding: true, nextArmed: false`) — the page tore
  the native player down, which released the external window, loaded the next episode into
  its own element, and re-claimed the display a beat later. The episode comes up on the
  phone. `nextArmed: false` is the cause: `lp._nextNative` was never set, so `armed.nextUrl`
  was empty and `itemDidEnd` had nothing to switch to.

Then look for the second failure that usually rides along: a `hold-start` with
`elementWasPlaying: true`, or any `play-while-holding` row. Two engines in one process means
the web element takes the audio session and **interrupts** the AVPlayer feeding the display.
The tell in the older logs was a `snap` showing `tcs: pause, rate: 0` a few seconds after a
handoff that had been playing, with `armPaused` flipped to true and never coming back —
because the 1 Hz time observer mirrors the transport into `armed.paused`, so a pause that
happened *to* the app is adopted as if it were the user's wish. That now produces a
`transport-stopped-itself` row naming the wait reason and the audio route, an `interruption`
row if the session was taken, and an `interruption-resumed` row when we put it back.

**Pair every `disarm` with the `progress` row before it.** A `disarm` with
`flushed: true` should be immediately preceded by a `progress` row carrying
`final: true` and a 200. If that row is missing or non-200, that episode lost its
tail — which is precisely what could not be seen before these fields existed.

### Recipe: "I plugged the glasses in (or pulled them out) mid-episode"

Plugging and unplugging during playback are two different failures with the same shape:
the display and the player disagree about who is presenting, and nothing reconciles them.

**Plugged in mid-episode.** Read forward from the `screenChange` snap that shows
`screens=2`:

- `sceneConnect` → `earlyClaim` → `earlyHandoff` → `startNative` → `hold-start` →
  **`display-handoff`** is the whole correct sequence. The episode moves to the glasses
  where the user is looking.
- `sceneConnect` with `native=false` and **nothing after it** is the pre-18.11.0 bug.
  `extWindow=True, winOnExt=True, extLayer=False` in the snap says it exactly: we claimed
  the display (which replaces mirroring, so the glasses go black) and never put a player
  in the window. Playback carried on down on the phone. Only a stop/start fixed it,
  because only a fresh arm ran the claim-and-hand-off routine.

**Unplugged mid-episode.** Read forward from `sceneDisconnect`:

- With `native=True` in that snap, expect `route` (`old-device-gone`), an `interruption`
  with `type: began`, and then **`display-lost-handback`**. The page takes the episode
  back into its own element and plays on.
- **There is no `interruption` with `type: "ended"` for this**, and there never will be:
  a route-disconnect interruption (`reason: 4`) is not one iOS resolves. Do not go looking
  for the `ended` that would have triggered `interruption-resumed`; handling the route
  change is the app's job, which is what the hand-back is.
- `display-lost-handback` missing, with `snap` rows still showing `native=True` and no
  `extWindow`, is the pre-18.11.0 bug: the page stayed a remote for a player with no
  surface. Its tell is a run of `setPaused` / `transport` pairs a second or two apart —
  the user pressing play, seeing nothing, and pressing pause again — with the position
  creeping forward the whole time. Nothing short of relaunching the app cleared it.

Unplugging while the phone is **locked** is not this bug. The native player is left alone
on purpose (handing back to a suspended WKWebView would stop playback outright), so the
hand-back lands on the return to the foreground instead.

### Recipe: "the ±10 button doesn't really move the picture"

Every user-intent seek writes a `seek` row and, 4.5 s later, a `seek-verdict`. Read them
as a pair.

- **`seek` then `seek-verdict: landed`** — it worked. If the viewer still says it didn't,
  they are describing something else (check `from`/`to` against what they expected; two
  fast presses are two rows).
- **`seek-swallowed`** — the known wedge: the pipeline kept presenting the old position
  while the element accepted the seek. A `_lpPipelineRebuild` follows. Twice inside a
  minute escalates to a full reload.
- **`seek-stuck`** — the wedge the 2026-09-23 report actually was, and the row to look for
  first. The loader died; see the next recipe.
- **`seek-verdict: no-frames`** — says almost nothing on its own. `requestVideoFrameCallback`
  does not fire on a **paused** element, so *every* seek made while paused verdicts
  `no-frames` whether the loader is healthy or dead. **Read `paused` before drawing any
  conclusion, and never treat this row as evidence of a wedge.**
- **No `seek` row at all** for a press the viewer swears they made — the press never
  reached `_lpCommitSeek`. Three ordinary causes before you suspect a bug: the controls were
  in remote mode (look for `engine: avplayer` rows), the player had been torn down, or the
  presses were **coalesced** (18.12.3 — a continuous burst of ±10 writes exactly two rows,
  the first press and the resting place, so `from`→`to` on the second row will show a jump
  of many multiples of 10).

### Recipe: "the picture is frozen but the seek bar moves"

This is the 2026-09-23 wedge, diagnosed from a 316-row transcript, and it is a **loader**
failure, not a decode failure. The tell is in the `seek` rows themselves: compare the
`buffered` string across consecutive seeks.

- **`buffered` identical on row after row, with `inBuf: 0` on every one** — hls.js has
  stopped fetching. In the reference case it froze at
  `192.0-372.0,378.0-384.0,390.0-414.0` and stayed byte-identical across **22 seeks over
  37.5 s**, including forward seeks far outside it. The element has media in exactly one
  place and presents from there: `seek-verdict` reported `lastFrame: 204` while `currentTime`
  read **991**.
- **`ready: 4` throughout is not a contradiction** — it is the reason nothing caught this
  for so long. There were 180 s of media buffered *ahead* of the playhead, just not *at* it,
  so the element honestly reported HAVE_ENOUGH_DATA and both `_lpStallWatch` and
  `_lpKickIfStalled` (which bail on `readyState >= 3`) reset themselves every tick.
  `_lpStallWatch` also resets whenever `currentTime` advances — so the viewer's own presses
  hid the stall from the stall detector.
- From 18.12.3 this self-heals and says so: a `seek-stuck` with `stage: "kick"`, and if that
  didn't take, a second one with `stage: "rebuild"`. A `kick` with no `rebuild` behind it is
  the system working.

Before 18.12.1 none of these existed: seeks were entirely unlogged, and the only trace of
a bad one in the 2026-09-23 transcript was an accident — two unrelated rows that happened
to sample `armed.position` either side of a clean −20.000.

### Recipe: "the intro didn't skip on the glasses"

During a handoff the **native** player fires auto-skip and the page only draws the tile
(18.12.0), so read the native rows first.

- **`auto-skip` present** → it fired. An intro carries `from`/`to`; check `to` against the
  `seekTo` that follows.
- **No `auto-skip`, and the arm never carried the windows.** The page fetches skip data
  from the host, so an **offline session has none at all** — `_lpFetchSkipData` returns
  early on `_appOffline` and every window arms as `-1`. That is a limitation, not a bug.
- **No `auto-skip` on an episode native advanced into.** Look at that `advance` row's
  `introEnd` / `creditsAt`: `-1` means the page never armed the next episode's windows
  (`_lpAttachNextSkip` couldn't run, or `next-arm-skipped` says the episode was never
  armed at all — a fully downloaded local bundle still cannot be). Native will not skip
  what it was not told.
- **No `auto-skip`, windows present, position past the point.** Check `armed.paused` — a
  player paused inside the intro is deliberately left alone — and remember the guard on
  `.readyToPlay`: nothing fires until the item has loaded, which on a host-streamed bundle
  has been measured at six seconds after the handoff.
- **A `native-skipped` with no `auto-skip` above it** cannot happen; the reverse (an
  `auto-skip` the page never acknowledged) is normal and just means the phone was asleep.

If the tile never *appeared* while you were watching the phone, that is the page side:
it draws off `_npApplyNativeTransport`, so a stalled `nativeProgress` stream takes the
seek bar with it — check whether the clock was moving at all.

### `locked+3s` / `handoff+10s` name a SCHEDULE, not an elapsed time

The trail's timed snaps are `asyncAfter` work items, and a suspended app does not run
them. Measured 2026-09-23: `locked+3s` landed on time, then `locked+10s` and `locked+20s`
both fired **5 m 45 s later, in the same millisecond** — iOS had suspended the app (it was
paused, so nothing held it up) and released both timers together when it next got CPU.

Read `t` for when a row was taken and the `at` label only for which sample it is. A large
gap before a coalesced pair is itself evidence — it dates the suspension.

### The build stamp

Every `launch` row carries `build`, sourced from the single `NP_BUILD` constant in
`NativePlayback.swift`. **This is the only trustworthy version signal the app has** —
`CFBundleShortVersionString` is pinned at 1.0 and never bumped, and the dashboard badge
belongs to the host, not to the installed binary. Bump `NP_BUILD` with any change to that
file, or a log cannot answer whether a fix is actually on the phone.

Note that a current `build` does **not** guarantee a current JS bridge: a WebView session
started before the app was updated keeps the old plugin method list, so a newly added
plugin method can be missing from a binary that defines it. Relaunch before concluding
anything about the build (see GOTCHAS.md § A current binary behind a stale WebView bridge).

## Log files

All under `logs/`, all listed and downloadable from **Admin → System → Server
Logs** (and included in "Download All").

| File | Written by | Contents |
|---|---|---|
| `access.log` | uvicorn (`uvicorn.access`) | **Every inbound request**, both :80 and :443. 8 MB × 5. |
| `uvicorn.log` | uvicorn (`uvicorn.error`) | Server startup / bind / shutdown errors. |
| `vitals.log` | `vitals_loop` | One health sample every 30 s. 4 MB × 3. |
| `stall_<ts>.txt` | `dump_stalled_state` | Full asyncio task + thread stack dump at the moment of a stall. |
| `faulthandler.log` | `faulthandler` | Native-level stack if the interpreter dies hard (segfault in a C extension). |
| `streamlink_app.log` | `streamlink` logger | App log. Anomalies escalate here at WARNING. |

> **One file, one handler.** `access.log` is owned exclusively by uvicorn's
> dictConfig, and `uvicorn.error` deliberately writes to `uvicorn.log` rather
> than sharing `streamlink_app.log`. Two `RotatingFileHandler`s on one file
> means rotation renames it while the other handle is open — a `PermissionError`
> on Windows, the primary target, and potentially a lost log.

---

## What runs

Three background tasks, started in `lifespan` and cancelled on shutdown.

### `_measure_loop_lag`
Sleeps 0.5 s in a loop and records how late it actually returns. Lag is the
clearest single indicator that something synchronous is blocking the loop.

### `vitals_loop` — every 30 s
Writes one compact line to `vitals.log`:

```
up=3612s lag=0.01s tasks=47 tp=12/20+q0 inflight=1 oldest=0.3s
lock_held=0.0s waiters=0 probe=4ms fails=0 rss=412MB thr=31 fds=612 conns=48
```

If the sampler *itself* throws, that is reported at **ERROR** with a traceback on
the 1st, 2nd, 4th, 8th … consecutive failure (geometric backoff), and recovery is
logged too. It is never swallowed at DEBUG: a diagnostics loop that dies quietly
just stops growing `vitals.log`, which reads exactly like a healthy idle box.

If any threshold is breached, the same line is **also** logged at WARNING to
`streamlink_app.log`, prefixed `VITALS ANOMALY` — so the onset of trouble shows
up in the file an operator opens first. Thresholds (all in `diagnostics.py`):

| Constant | Default | Meaning |
|---|---|---|
| `LOOP_LAG_WARN_S` | 1.0 s | Event loop this far behind ⇒ something is blocking it |
| `LOCK_HELD_WARN_S` | 5.0 s | Library lock held this long ⇒ every waiter is stuck |
| `REQUEST_SLOW_S` | 5.0 s | Individual request logged as `SLOW REQUEST` |
| `REQUEST_STUCK_S` | 30 s | In-flight this long ⇒ anomaly |
| `THREADPOOL_WARN_FRAC` | 0.85 | Pool this full **and** work queued ⇒ saturated |
| `STALL_DUMP_COOLDOWN_S` | 300 s | Minimum gap between stack dumps |

Thread-pool saturation is reported only when the pool is at capacity *and* the
work queue is non-empty. `ThreadPoolExecutor` exposes no "busy" counter and
inventing one is guesswork; it only spawns a worker when none is idle, so
"threads at max + queue non-empty" is a real signal.

### `self_probe_loop` — every 30 s
`GET /healthz` on `127.0.0.1:<port>` over a **raw socket** (deliberately not
httpx — the point is to test the listening socket with as few of our own moving
parts as possible). This is the check whose absence made 2026-09-13
undiagnosable.

- Success → nothing logged (the timing lands in `vitals.log` as `probe=`).
- Failure → `CRITICAL SELF-PROBE FAILED` with a full vitals snapshot.
- **Two** consecutive failures → a stall dump.

`/healthz` is deliberately trivial: it touches no lock, no disk and no library.
A `200` there means "the socket accepts and the loop is turning", which cleanly
separates a networking/accept-queue fault from library-lock or thread-pool
starvation. **If `/healthz` answers while real endpoints hang, the fault is
downstream of the server** — look at `lock_held` and `tp_*` in the vitals line.

---

## Request tracking

`diag_track_requests` is registered **first** in `main.py`, which makes it the
outermost middleware (Starlette applies them in reverse), so it times the whole
stack including the HTTPS and adapter redirects.

It matters because **uvicorn's access log only records a request when it
finishes** — a handler that never returns leaves no line at all. `diag.state.inflight`
shows it *while it is still hanging*, the vitals line counts it (`inflight=`,
`oldest=`), and the stall dump names it.

Requests slower than `REQUEST_SLOW_S` are logged as `SLOW REQUEST 7.2s status=200 GET /api/library`.

---

## Stall dumps

`dump_stalled_state(reason)` writes `logs/stall_<ts>.txt` containing:

1. The vitals snapshot and the triggering reason
2. Every in-flight request with its age
3. Every instrumented lock: held-for, holder task, waiter count
4. **Every asyncio task stack**
5. **Every thread stack**

This is the artifact the 2026-09-13 incident lacked, and nothing reconstructs it
after the fact. Rate-limited to one per `STALL_DUMP_COOLDOWN_S` so a long outage
produces a handful of dumps rather than thousands.

Triggered by: two consecutive self-probe failures, loop lag ≥ 5 s, or a request
in-flight ≥ 60 s.

---

## Noise control

- `quiet_noisy_loggers()` drops `httpx` / `httpcore` / `urllib3` to WARNING,
  removing the ~168 lines/min of poll chatter. Nothing is lost: inbound traffic
  is now recorded properly in `access.log` rather than incidentally via the
  proxy's httpx hops.
- `install_loop_exception_handler()` demotes `ConnectionResetError` /
  `connection_lost` (benign WinError 10054 client-disconnect churn, which used
  to flood the log at ERROR) to DEBUG, and logs everything else loudly. The goal
  is that **an ERROR in the app log means something**.
- `install_exception_hooks()` routes `sys.excepthook` and `threading.excepthook`
  into the app log, so an exception on a non-asyncio thread is no longer lost to
  a stderr nobody correlates.

---

## Reading an incident

1. **`access.log` — did requests arrive at all?**
   - No entries → the problem is upstream: network, firewall, mDNS, adapter, or
     the client never resolved the host. Cross-check `SELF-PROBE` lines: if the
     probe succeeded throughout, the server was accepting fine and the fault was
     outside the process.
   - Entries present but no matching completion → the handler hung; find it in
     the stall dump.
2. **`streamlink_app.log` — grep `VITALS ANOMALY`, `SELF-PROBE`, `SLOW REQUEST`, `LOCK`.**
   The first anomaly line marks the onset.
3. **`vitals.log` — scroll back to the onset** and read across: `lag`, `tp=`,
   `lock_held=`, `inflight=`. These name the starvation mode directly.
   High `lag` with `lock_held=0.0s` and spare pool threads means synchronous work
   on the loop itself, e.g. the per-call `httpx.AsyncClient` construction fixed
   in 16.3.3 (see GOTCHAS).
4. **`stall_*.txt`** — the stacks say exactly what every task was awaiting.

### Two traps
- **A `logs_old_<ts>.zip` is not evidence of an update-triggered restart.**
  `_archive_old_logs()` runs only when the updater left a `.rotate_pending`
  marker, and that marker can sit unconsumed for hours. It means "an update was
  applied at some point before this start".
- **Applying an update without the reboot leaves a split-brain process.** A
  `POST /api/admin/updater/apply` with `reboot` off swaps the git checkout *and*
  re-runs `setup.py` under the still-running interpreter: in-memory code is the
  old build while `static/*.html` and anything imported lazily come off disk from
  the new one. Reboot promptly after applying.

---

## Deploying a change to this subsystem

> **Access logging does not depend on any of this.** `main.py` calls
> `diag.attach_access_log()` at import, and uvicorn imports `main.py` fresh on
> every boot, so `access.log` works even from a stale launcher. The configs
> below remain the belt to that pair of braces; `attach_access_log` skips a file
> that already has a handler, so the two never collide.


The uvicorn log config lives in **three** places that must stay in step:

| Path | Used by |
|---|---|
| `daemon.py` `_WRAPPER_CONTENT` | **The installed service** — this is what runs on the box |
| `run.py` | Interactive `python run.py` |
| `diagnostics.uvicorn_log_config()` | The shared definition both import |

⚠️ `daemon.py` only *generates* `streamlink_service.py`. An existing install
keeps running its old wrapper until the file is refreshed — via
`updater.refresh_service_wrapper()` (the updater does this automatically) or
`python run.py --install`. Changing `daemon.py` alone changes nothing on a box
that is already installed.

---

## See also

- [GOTCHAS.md](GOTCHAS.md) — the 2026-09-13 write-up and other footguns
- [ARCHITECTURE.md](ARCHITECTURE.md) — process model and service topology
- [DAEMON_WATCHDOG.md](DAEMON_WATCHDOG.md) — the service wrapper and crash supervisor
- [ADMIN.md](ADMIN.md) — Server Logs card, scheduled restart
