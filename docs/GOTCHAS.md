# Gotchas

Non-obvious behaviours and footguns. Read before changing anything load-bearing.

## VLC

### Track IDs are ES IDs, not 1/2/3 counters

VLC's `audio_track` / `subtitle_track` commands accept **elementary stream IDs** — the number N in each `"Stream N"` key of `vs.information.category`. Using sequential per-type counters (1, 2, 3 for audio; 1, 2, 3 for subs) sends the wrong ID and the command silently does nothing. The `<audiotrack>`/`<subtitletrack>` values in the XML status are also ES IDs, so the "current" highlight in the UI dropdown only works if they're compared as ES IDs.

See `get_tracks()` ([main.py:2799](../main.py#L2799)) — `es_id = int(key.split()[-1])`.

### VLC 3.x has no current-track in status

`status.xml` / `status.json` don't include `<audiotrack>` or `<subtitletrack>` in VLC 3.x. We track it ourselves in `state.current_audio_track` / `state.current_subtitle_track`, reset to `-1` on every new `in_play`. The `POST /api/vlc/track/*` endpoints update this state.

### Absolute vs relative seek

- Absolute: `val=N%` (percentage) or `val=Ns` (seconds). Our `/api/vlc/seek/to` uses `val=N%`
- Relative: `val=+Ns` / `val=-Ns`. Our `/api/vlc/seek?delta=N` uses this

`val=N` with no suffix is interpreted as a **0–1 fraction**, not seconds. Don't confuse them.

### Resume seek must wait for VLC to open the file — poll, don't sleep

A `seek` issued right after `in_play` is silently dropped: VLC can't honour it until its demuxer is up, which is when `status.json`'s `length` becomes non-zero. The resume path therefore **polls** via `_vlc_wait_until_ready()` (state `playing`/`paused` **and** `length > 0`, every 0.2 s) and seeks the instant VLC is ready, instead of the old blind `asyncio.sleep(3)`. On a local file VLC opens in well under a second, so the old fixed wait left the user staring at 0:00 for ~3 s before the jump; a slow open could miss the 3 s window entirely and never resume. `_library_play_launch` re-issues the seek **once** (guarded: only if `time` is still >15 s behind target) because VLC occasionally ignores a seek fired the very moment the demuxer comes up. Don't revert this to a fixed sleep — and if you add another "do X a few seconds after `in_play`" path (track prefs, marquee, etc.), prefer the same ready-poll over a magic delay. The `resume_mode="prompt"` offer uses the same gate so it appears as soon as playback is live.

### File path → URI

Always use `Path(p).resolve().as_uri()` when sending to VLC. This:
- Handles symlinks (important — VLC plays the resolved path, so `library_current_file` is also stored resolved)
- Generates correct `file:///C:/...` on Windows and `file:///...` on macOS/Linux without extra string surgery

### Volume scale mismatch

VLC uses 0–512 (256 = 100 %). Our API uses 0–200 (100 = normal). Conversion is `raw = volume / 100 * 256`. The global `settings.max_volume` cap is also 0–200. `state.vlc_volume` is in our scale.

`vlc("in_play", ...)` pushes a `volume` command first so VLC's default doesn't blast briefly. Important when the global cap is low.

### Volume cap must be re-applied at every track start

`state.vlc_volume` is polled directly from VLC every 2 s, so it tracks VLC's reality — which can drift above the user's `max_volume` cap (e.g., VLC defaults to 100 on a fresh start, and `user_volume_before_bg` is seeded to 100 before the user ever touches the slider). Two defenses, both required:

1. `vlc("in_play")` clamps `state.vlc_volume` by the current cap **before** sending the pre-play `volume` command. Otherwise a low cap (say 60) plus a 100-default `user_volume_before_bg` blasts at 100 on every bg→content handoff.
2. The state broadcaster ([main.py:1112](../main.py#L1112)) checks the polled VLC volume against the cap each tick and pushes a correction if VLC is over. This self-heals against VLC's occasional snap-to-100 on playlist advance.

Don't drop either one thinking the other covers it — #1 is fast (no audible blast), #2 is the safety net for mid-playback drift.

### Dashboard state desyncs from VLC on restart — `_sync_state_from_vlc` reconciles

`AppState` is purely in-memory in the uvicorn process. If `main.py` restarts (admin Shut Down, watchdog kick, manual relaunch) while VLC keeps playing, every state field is back at its dataclass default — `stream_status="idle"`, `active_title=None`, `library_item_id=None`. `background_video_loop` sees VLC already in `state=playing` and stays out of the way (its job is to start bg when VLC is *stopped*), so the dashboard sits at "No active stream" forever even though real content is on screen.

`_sync_state_from_vlc` ([main.py](../main.py), called from `lifespan` right after the volume init) fixes this: it queries `status.json` + `playlist.json`, matches the playing URI against the background-video path (→ `background_playing=True` and bail) or each library item's files (→ seed `active_title` + `library_item_id` + `library_playlist` + `library_current_file` + `active_hash`), or falls back to the file stem as title for unmatched playback (external VLC plays / stream-now items whose torrent has been GC'd from `library.json`).

**`library_profile_id` is intentionally left unset.** The profile that originally started the playback isn't recoverable from disk state alone, and the wrong guess would mis-key progress writes. `vlc_progress_tracker` therefore skips progress saves and skip offers for the restored session (its first check is `if not state.library_item_id or not state.library_profile_id: continue`). Title display, next/prev, stop, the seek bar, and skip-back-by-30s all still work; resume + skip-credits offers come back the next time the user starts a play.

### Restart-on-retry

`POST /api/retry` ([main.py:2610](../main.py#L2610)) calls `_restart_vlc_process()` which kills all `vlc`/`VLC` processes, sleeps 1.5 s, relaunches with `--extraintf=http`, waits for the port. Then replays the current file + remainder of playlist. Used when VLC freezes on a partially-downloaded file.

### Boot-time fullscreen — pass `--fullscreen` AND loop the focus pass

When StreamLink launches via the system service at boot/login, the dashboard's `background_video_loop` kicks `in_play` to VLC within a few seconds of the desktop coming up, then calls `vlc_focus_and_fullscreen()`. A single focus + minimize-others pass is **not enough**: startup apps (Discord, Steam, OneDrive, the browser, etc.) launch on a staggered schedule across the first ~20 s after logon and pop up *after* our pass already ran, leaving them on top of VLC with the taskbar/Dock visible. Pressing Stop in the UI doesn't have this problem because by then the desktop is fully settled, so a single pass catches every window.

Defenses (all required):
1. **VLC is launched with `--fullscreen`** in every spawn path (`run.py start_vlc`, `watchdog.py vlc_spec.build_args`, `main.py _restart_vlc_process`). This makes VLC come up fullscreen even before any media is loaded, so there's no race with the desktop on the cold start.
2. **`vlc_focus_and_fullscreen` loops for ~24 s** on a slowing cadence (6× 0.5 s, then 8× 1 s, then 6× 2 s). Each iteration: re-runs `_vlc_assert_focus` (Windows: `_minimize_other_windows_windows` + `_vlc_focus_windows`; macOS: AppleScript `activate VLC` + hide-other-apps; Linux: `wmctrl -a VLC`), polls `vlc_status`, re-issues the HTTP `fullscreen` toggle if VLC is `playing`/`paused` but not fullscreen, and on Windows re-runs `_stop_vlc_flash_windows` to clear any taskbar attention flash. The loop bails early if `state.stream_status == "buffering"` so it doesn't fight a new pipeline taking over. Total wall-clock comfortably outlasts a typical Windows logon's startup-app churn.
3. On macOS, the focus pass hides every other visible app via AppleScript (`set visible of (every process whose visible is true and name is not "VLC" and frontmost is false) to false`). This is the macOS counterpart to the Windows `_minimize_other_windows_windows` call — without it, the user sees the menu bar / Dock / Finder windows on top of VLC.

Don't shorten the loop back to a single pass — the visible regression is "tiny VLC window at boot with the taskbar still showing and Discord/Steam/etc. on top." Don't drop `--fullscreen` either — without it, the very first frame after `in_play` is windowed and the user sees a flash before the toggle settles.

### Windows window control needs ctypes

`_find_vlc_hwnds_windows` uses `EnumWindows` via ctypes; the EnumWindowsProc wrapper must be kept alive (`cb = EnumWindowsProc(_cb)` and pass `cb`, not `_cb` directly) or ctypes will GC it and the callback will crash.

### Focus-stealing prevention → flashing taskbar → visible taskbar

When VLC is relaunched in the background (DETACHED_PROCESS, e.g. after `/api/retry`), a plain `SetForegroundWindow` is usually blocked by Windows' focus-stealing prevention. The fallback is a **taskbar attention flash** on VLC's icon — and a flashing icon also forces the taskbar to stay visible **even over a fullscreen window**, so the user sees both the flashing icon and the taskbar until they click it.

`_vlc_focus_windows` ([main.py:707](../main.py#L707)) defeats this with the full cocktail: zero `SPI_SETFOREGROUNDLOCKTIMEOUT`, synthesize an ALT keypress (any keystroke releases the foreground lock), AttachThreadInput, `BringWindowToTop` + `SetForegroundWindow`, then `_stop_vlc_flash_windows` (FlashWindowEx with `FLASHW_STOP`) to clear any flash that was already raised. `vlc_focus_and_fullscreen` calls `_stop_vlc_flash_windows` a second time after toggling fullscreen, because Explorer can re-raise the flash when the window changes state. Don't drop either flash-stop call — without them the retry-then-flash bug returns.

### Smart Skip countdown marquee

The auto-skip countdown popup is drawn by VLC, not the dashboard. VLC's HTTP interface has **no marquee command** (see its `requests/README.txt` — there's no `marq`/OSD verb), so the only way to put dynamic text on the video output is the **`marq` sub-source** configured at launch with `--marq-file=<path>`: VLC re-reads that file every `--marq-refresh` ms. `main.py` writes the countdown text into `<repo>/.vlc_marquee.txt` (`_marquee_write`, atomic `os.replace`) and empties it to clear.

Four traps:
- **Emptying the file does NOT clear the marquee.** `marq` reads `--marq-file` with `getdelim()`, which returns EOF on an empty file — so the filter keeps the *previously-rendered* text (and logs a read error every refresh tick). To clear, write a single **space**, not `""` (`_marquee_write` maps empty → `" "`). A space is a valid non-empty line that forces the update but renders no glyph (we draw no background box). Proof it works: the visible 5→1 count is a series of non-empty→non-empty updates; the space makes the final clear one too. `run.py` / `watchdog.py` also seed the file with a space.
- **The launch args live in three places** — `main.py` `_vlc_marquee_args()` (used by `_restart_vlc_process`), `run.py` `start_vlc`, and `watchdog.py` `vlc_spec`. All three must pass the same `--marq-*` flags and point `--marq-file` at the same `<repo>/.vlc_marquee.txt`. Change one, change all three.
- **The marquee file path must resolve identically across processes.** It's anchored to the repo root via `Path(__file__).parent` (all three modules live there) — *not* `tempfile.gettempdir()`, which can differ between the system-Python `run.py`, the venv `main.py`, and a service-launched `watchdog.py`. Create it empty before launch so `marq` has something to open.
- **Don't add `--freetype-background-*` for an opaque box.** The freetype background opacity/color is a *global* text-renderer setting — turning it on to box the marquee also boxes every regular subtitle line. The countdown is intentionally text-only (opaque white + VLC's default outline). `--marq-position=10` is natively Bottom-Right; `--marq-x`/`--marq-y` add the corner padding.

## qBittorrent

### `setSequentialDownload` doesn't exist

The qBittorrent API endpoint is `toggleSequentialDownload`. It's a toggle, so check `seq_dl` from `qbit_info` before calling — see `qbit_streaming_mode` ([main.py:344](../main.py#L344)). Sequential is also passed at add-time as the `sequentialDownload=true` form field to `/torrents/add`.

### Don't enable first/last-piece priority

`toggleFirstLastPiecePrio` fetches the last piece early. That **breaks** piece-order streaming because the playhead is at the start, not the end. We deliberately leave it off.

### LocalHost auth is disabled

`setup.py` writes `WebUI\LocalHostAuth=false` to qBit's ini. Localhost requests never need a cookie. `qbit_login` is still called on startup and `qreq` retries on 403 for safety, but the cookie is mostly cosmetic.

### Sequential vs library downloads

Stream-now uses sequential. Library downloads do NOT — they should download normally so all files arrive. See [BACKEND.md](BACKEND.md#pipelines).

## VPN

### Two enforcement points

1. `vpn_guard` in `main.py` ([main.py:997](../main.py#L997)) — kills qBit when VPN drops; gates `/api/stream` and `/api/library/download` via `state.vpn_secure`
2. `watchdog.py` ([watchdog.py:343](../watchdog.py#L343)) — kills qBit if it's running while VPN is down, AND refuses to restart it until VPN reconnects

If you're tempted to remove one, **don't**. They cover different failure modes:
- `vpn_guard` runs inside the dashboard process and protects the API
- `watchdog.py` runs in a thread (or as a separate service) and protects the process

### Mullvad CLI missing → treated as unsafe

Both guards return `vpn=False` if `mullvad` is not in PATH. Cannot-verify = unsafe. Make sure the CLI is on PATH (or set `_MULLVAD_BIN` in `.env`).

## Jackett

### `Category[]=0` returns no results

Jackett treats `0` as an unknown category ID, not "all". To search all categories, omit the `Category[]` parameter entirely. See `/api/search` ([main.py:2272](../main.py#L2272)) — only passes `Category[]` when `INDEXER_CATEGORIES != "0"`.

### Remote Jackett vs local

`INDEXER_URL` hostname is parsed in `run.py` and `watchdog.py`. If it's `localhost`/`127.0.0.1`/`::1` → try to launch + monitor locally. Otherwise → reachability check only, never launch. This is the correct behavior — remote Jackett shouldn't be launched from the local machine.

### Windows service vs tray exe

The Jackett Windows installer registers a `Jackett` Windows service that runs as LocalSystem and actually serves port 9117. `JackettTray.exe` is cosmetic — it shows the icon and offers a "Start background service" menu item. Both `setup.py` and `watchdog.py` prefer the service (via `sc.exe start Jackett`) and only fall back to launching the tray exe.

Service config files live under LocalSystem's profile: `C:\Windows\System32\config\systemprofile\AppData\Roaming\Jackett` or `C:\ProgramData\Jackett`. **Not** the interactive user's `%APPDATA%`. The `--verbose` mode of `run.py` searches all five candidate locations.

### Port-open is NOT a Jackett health check

A hung Jackett keeps its TCP listener socket bound (so a port-connect "succeeds") while it has stopped answering HTTP. A bare port check therefore reports a wedged Jackett as healthy forever and never restarts it — the long-standing "Jackett stops after a while, only a reboot fixes it" bug. The watchdog (and `run.py`'s startup reachability check, and `main.py`'s `jackett_health_monitor`) now probe **HTTP** `GET {INDEXER_URL}/UI/Login` (served without auth — any HTTP status proves the web stack is alive) via `_http_ok()`. Liveness = "answers HTTP", not "port open".

### Restarting a hung Jackett needs a force-down first

`sc.exe start Jackett` is a **no-op** (returns 1056 ALREADY_RUNNING) when the service is wedged-but-RUNNING — that's why `sc start` alone never recovered it and a reboot was required. The watchdog's Jackett `ServiceSpec` has a `pre_restart` hook (`_force_stop_jackett_windows` / `_kill_by_name`) that forces the old process down (service stop, waiting for STOPPED; hard-kill fallback) **before** relaunching, so the port frees and the restart actually takes. `ServiceSpec.start()` then waits on the HTTP health check (not just the port) so it doesn't tight-loop while Jackett's web stack is still warming up.

### Controlling the LocalSystem Jackett service needs admin

A non-elevated StreamLink (the normal install: Task Scheduler at logon, no `/RL HIGHEST`) **cannot** `sc stop`/`sc start` a LocalSystem `Jackett` service — Windows returns access-denied (you see a UAC prompt). So the watchdog can *detect* a hung Jackett but not recover it without rights. `setup.py`'s `grant_jackett_service_control()` additively grants Authenticated Users `SERVICE_START`+`SERVICE_STOP` via `sc sdset Jackett "(A;;RPWP;;;AU)…"` (one-time, elevated) and sets `sc failure` restart actions, so the non-elevated watchdog can recover Jackett with no UAC and no reboot. Re-run `setup.py` to apply. The access-denied paths log an actionable hint instead of failing silently. If Jackett runs as a **tray/user process** instead of a service, no grant is needed — the watchdog kills+relaunches it directly.

## Library

### `library_item_id` is the "don't auto-delete" flag

`/api/stop` ([main.py:2576](../main.py#L2576)) checks `if state.active_hash and not state.library_item_id` before deleting the torrent. If you're streaming a torrent and then call `/api/stream/save-to-library`, that sets `library_item_id` and the next `/api/stop` will leave files alone.

### `track_pref_applied_file` prevents double-apply

`vlc_progress_tracker` triggers `_apply_track_prefs` when `state.library_current_file != state.track_pref_applied_file`. Without this guard, every 2 s tick would re-send the audio/subtitle commands and the user couldn't override them mid-playback.

### Canonical path matching

VLC plays `Path(p).resolve().as_uri()` (resolved). The stored item file path may not be resolved. `_canonical_item_path` ([main.py:868](../main.py#L868)) compares both as resolved Paths and returns the stored path — so progress and skip-data lookups key correctly against `item.files[].path`.

### Resume hint walks files in order

`find_resume_hint` ([main.py:890](../main.py#L890)):
1. If `last_file` has meaningful in-progress position (>5 s, not completed) → return it
2. Walk `files` in order, return first not-completed file
3. If all completed → return file[0] with `all_completed: true` (UI lets user rewatch from start)

### Frontend drops saveProgress writes under t=5 s

The server recomputes `completed` on every `/api/library/{id}/progress` write as `pct = position/duration > 0.92`. A save at `t≈0` therefore wipes a previously-watched episode back to unwatched. The local player can fire those near-zero writes from at least three places: the very first `timeupdate` event before the resume seek lands, the `pause` event that browsers fire during initial load, and `lpStop` if the user opens the player and closes immediately. `saveProgress` and `_lpFlushProgress` both early-return when `posSec < 5` to keep watched marks stable. The 5 s threshold matches the resume hint's "meaningful in-progress" cutoff, so dropping these writes also has no resume-UX cost.

## SSE

### Per-client queues, dead-queue cleanup

Every `/api/events` connection creates its own `asyncio.Queue(maxsize=100)`. `broadcast` iterates `state.sse_queues`, drops any that raise `QueueFull`. Disconnected clients are cleaned up in the `finally` block of the stream generator.

### EventSource can't set headers

For admin SSE, the token is passed via `?admin_token=…` query param. The middleware accepts it from query string too.

### Slow-network Play must be non-blocking

`/api/library/{id}/play`, `/api/vlc/prev`, `/api/vlc/next`, `/api/stop`, and `/api/stream` all return **202** and do their VLC `in_play`/`in_enqueue` (and qBit deletes on stop/stream) in background tasks. They synchronously update `state`, broadcast a `buffering` / `idle` state event, then return. The SSE-driven UI repaints from that broadcast within ~tens of ms even when VLC is taking seconds to actually open the file.

Don't be tempted to "simplify" any of these handlers back to inline `await vlc("in_play", …)` — on flaky links each VLC HTTP roundtrip can take 1–5 s, and a 5-episode playlist with `in_play` + 4× `in_enqueue` would block the response for that whole window. The frontend's optimistic-buffering UI (`_optimisticBuffering` in `index.html`) also assumes the buffering broadcast lands fast — bringing back inline VLC blocks would leave the user staring at "Loading…" with no confirming state event.

The handoff tasks are tracked on `state.library_play_task`. `/api/stop` and any subsequent Play / prev / next cancels the prior task before kicking off its own so a slow `in_play` can't keep going after the user has already moved on (otherwise VLC would end up playing whatever the *previous* request was reaching for).

### Flip `stream_status` to "playing" right after `in_play`, not after the enqueue loop

`_library_play_launch` and `_vlc_relaunch_playlist` set `state.stream_status = "playing"` and broadcast the state event the instant VLC accepts the first track. The remaining `in_enqueue` calls then run **in parallel via `asyncio.gather`**, not sequentially after the state flip.

Why this ordering matters: VLC is local, but its HTTP API still serializes per call, and a "continue watching" play on a long show easily ends up with 50+ files in the playlist tail. If the state flip waits for a sequential enqueue loop to finish, VLC is already playing the first episode but the UI stays pinned to "buffering" / "Loading…" for many seconds — exactly the regression that 2.2.1 fixed. Don't reorder these.

Failures inside the parallel `gather(..., return_exceptions=True)` are silently absorbed because the user-visible playback already started; a missing enqueue just means a future Next would fall through to `item.files`.

## Stream to Device (HLS)

### Output is an HLS bundle directory — not a single MP4 anymore

The cache layout switched in Milestone 16. Each prepped source produces `.offline_cache/<sha>/` with `master.m3u8`, per-rendition playlists, fmp4 segments, and `meta.json`. The pre-v3 single-MP4 cache (`<sha>.mp4`) is dead code on disk — surfaced as `kind: "legacy"` orphans in Admin → Offline Cache for purge. Don't reintroduce code that assumes "a prepped file is one MP4" — every endpoint, admin tool, and cleanup path now walks the directory.

### Subtitles can NOT live in the HLS manifest — they're standalone `.vtt` sidecars

ffmpeg's HLS muxer cannot package multi-track WebVTT. Exactly *one* subtitle works if you declare it inline on the video variant (`v:0,a:0,s:0,sgroup:…`); declaring two or more as their own `s:N,sgroup:…` variants fails unconditionally with `[mpegts/mp4] No streams to mux were specified` → `Could not write header (incorrect codec parameters ?)` → `Conversion failed!`. This holds for **both** `fmp4` and `mpegts` segment types (verified on ffmpeg 8.1.1). Because virtually every release MKV ships many subtitle tracks, the old in-manifest design meant HLS prep failed on essentially every real file — it had never once succeeded (fixed in v3.2.0).

The fix: `_build_hls_ffmpeg_args` builds a **video + audio only** HLS bundle, then emits one standalone `sub_<i>.vtt` per text sub via extra outputs in the *same* ffmpeg pass (`… <out>/%v.m3u8 -map 0:s:0 -c:s webvtt -f webvtt <out>/sub_0.vtt …`). The player attaches them as `<track>` children. Do **not** "re-add subtitles to `-var_stream_map`" — it will silently break prep again. If you ever need a single inline sub, the one-subtitle inline form is the only var_stream_map shape that works.

### The fmp4 init filename MUST be templated, or playback dies with `fragLoadError`

Symptom: prep "succeeds", the manifest parses (the audio/subtitle dropdowns populate, so `MANIFEST_PARSED` / `loadedmetadata` already fired), then playback never starts and hls.js throws a fatal `fragLoadError` (black player). It is **not** a server bug — `offline_cache_bundle_file` serves every real file fine (200 for `.m3u8`/`.m4s`, 206 for Range). The failing fetch is the **fmp4 init segment**: the variant playlist's `#EXT-X-MAP:URI="…"` points at an init file ffmpeg never wrote under that name, so it 404s, hls.js exhausts its frag retries, and the error goes fatal *before any frame decodes*.

There are **two** independent ways to hit this, fixed in two steps:

1. **Wrong init *name* (v3.2.1).** We templated `-hls_segment_filename` (`seg_%v_%05d.m4s`) and `%v.m3u8` but originally left `-hls_fmp4_init_filename` at ffmpeg's default. ffmpeg's own `%v` expansion for the init segment is version-dependent and doesn't always match the URI it writes into the playlist (e.g. it may number inits `init_0.mp4`/`init_1.mp4` while segments use the `name:` tag). Fix: pin `-hls_fmp4_init_filename "init_%v.mp4"` so inits are `init_video.mp4` / `init_audio_0.mp4`, matching the segment scheme. **Do NOT give it a full path** — ffmpeg prepends the playlist's directory to the init filename, so a full path becomes a doubled/invalid path and the encode dies with `Failed to open segment … Could not write header`.

2. **Wrong init *location* on Windows (v3.2.2).** Even with the right name, the init still 404'd on a Windows host. ffmpeg derives the init segment's *output directory* by parsing the **playlist** path; a Windows backslash playlist path defeats that parse, so `init_video.mp4` is written to the server's working directory instead of the bundle `.part/` dir — segments are fine because we pass them as absolute paths ffmpeg uses verbatim. Fix: make **every output a bare filename** (init / segments / playlists / subs) and run ffmpeg with `cwd=<bundle .part dir>` (`_run_offline_job` passes `cwd=str(tmp_dir)`). Now everything lands in the bundle on every OS; only `-i <source>` is absolute. `_build_hls_ffmpeg_args` no longer takes `out_dir`.

If you change any output naming or the cwd handling, bump `OFFLINE_CACHE_VERSION` so old bundles rebuild. Debug tip: a fatal `fragLoadError` is logged with the exact failing URL + HTTP code in the browser console (and the on-screen alert shows the filename + code) — a `404` on `init_*.mp4` is this bug; a `404` on `seg_*.m4s` means segment naming drifted; code `0` means a transport/TLS failure, not a 404.

### macOS hosts can't run HLS prep — TCC blocks ffmpeg from `~/Downloads`

ffmpeg / ffprobe run as children of the (non-GUI) Python server process. macOS TCC denies that process access to the user's protected folders (`~/Downloads`, `~/Desktop`, `~/Documents`) — `ffprobe` returns empty JSON + `Operation not permitted`, so `_ffprobe_full` yields `video: None` and prep aborts with a misleading "no video stream" (the file is fine; the process just can't open it). VLC and qBittorrent work on the same files because they're separate `.app`s the user individually granted. Rather than chase per-process Full-Disk-Access grants, `HLS_AVAILABLE = platform.system() != "Darwin"` short-circuits the prep endpoints with a clear message, `state_snapshot` exposes `hls_available`, and the UI hides the controls. If you ever want HLS on a Mac host, the file would need to live outside the TCC-protected folders **and** the responsible app (Terminal / the service binary) would need Full Disk Access.

### ffmpeg ≥ 4.3 is required for multi-rendition HLS

`-var_stream_map` with subtitle groups is unreliable on ffmpeg 4.0–4.2 — the master playlist sometimes drops audio renditions, sometimes mis-tags `agroup`. `_run_offline_job` calls `_ffmpeg_version()` (cached per process) and fail-fast errors the job before launching ffmpeg if the version is too old. Don't drop this check — the silent-bad-manifest failure mode is hard to diagnose from the UI side (the player just shows "no audio" or stalls on a missing rendition).

### ABR ladder: map the video once per rung, mix copy + transcode in one pass

Since `v7-hls-abr`, `_build_hls_ffmpeg_args` emits multiple video variants (Original + 720p + 480p, capped at source height by `_hls_video_variants`). The shape that works in a single ffmpeg pass:
- **Map `0:v:0` once per rung** (`-map 0:v:0 -map 0:v:0 -map 0:v:0`), then the audios. Output video stream index `i` then lines up with `videos[i]` and the `v:i` entries in `-var_stream_map`.
- **Per-output codec/scale options are index-qualified** — `-c:v:0 copy`, `-c:v:1 libx264 -filter:v:1 scale=-2:720 -crf:v:1 23 …`. A global `-c:v`/`-crf`/`-vf` would apply to *all* video outputs and break the mix. The original rung (`i==0`) can `copy` while the down-rungs transcode in the **same** invocation — that's intentional and supported.
- **`scale=-2:<h>` not `scale=W:<h>`** — the `-2` lets ffmpeg pick an even width preserving aspect ratio; libx264 / yuv420p reject odd dimensions.
- **Set `-maxrate:v:i` / `-bufsize:v:i` on the down-rungs.** Without a VBV cap, CRF alone leaves the master playlist `BANDWIDTH` as the measured peak and the rungs barely shrink — ABR then picks badly. The caps (720p≈3 Mbps, 480p≈1.2 Mbps) make the ladder real.
- **The original copies even when NVENC is present.** This is decoupled from `use_nvenc` (unlike the pre-ABR code, where any NVENC availability forced a full re-encode) — only the scaled down-rungs need the encoder, so a browser-safe source still gets a cheap remux at full quality. Don't re-tie `copy` to `not use_nvenc`.

`%v` in the playlist / segment / init templates expands to each `name:` tag, so the bundle gets `video.m3u8` / `video_720.m3u8` / `video_480.m3u8` plus matching `init_*`/`seg_*` — bare names + `cwd=<bundle>` still load-bearing (see the fmp4-init gotcha above).

### Server runs at raised OS priority — keep heavy children below it

`_raise_own_priority()` (first call in `lifespan`) bumps the StreamLink server to `HIGH_PRIORITY_CLASS` (Windows) / negative `nice` (POSIX) so controls/UI/VLC-control never lag behind a background encode. **The catch: child processes inherit the parent's priority** (Windows: at creation unless a creationflag overrides; POSIX: the nice value). So any *new* CPU-heavy subprocess spawned by the server must explicitly drop itself below normal, or it runs at HIGH and re-creates the exact lag this fixes. Today that's prep ffmpeg (`_ffmpeg_nice_prefix` + `_FFMPEG_SUBPROCESS_KW`) and every analyzer subprocess (`analyzer._lp` + `analyzer._LOWPRIO_KW`). If you add another heavy spawn (a new transcode, a thumbnailer, …), give it the same `nice -n 10` / `BELOW_NORMAL_PRIORITY_CLASS` treatment. Brief one-shots (ffprobe, the `_has_nvenc` probe, `mullvad status`) are fine to leave — they finish in well under a second. Don't reach for `REALTIME_PRIORITY_CLASS`/very-negative nice on the server: it can starve OS/driver threads and needs privilege; `HIGH` is the intended ceiling.

### Pausing prep: a paused bulk job *exits its task* (and releases the slot)

The global pause (`state.prep_paused`, set by `/api/offline-prep/pause`) gates only **bulk** jobs (`queue == "bulk"` — per-item / per-row / overnight). When a bulk job reaches the pause gate at the top of `_run_offline_job`, it marks itself `"paused"` and **returns** — it does *not* sit in a `while paused: sleep` loop holding the `OFFLINE_JOB_CONCURRENCY` semaphore. That's deliberate: holding the single slot would block an interactive play-on-device prep (`queue == "interactive"`, which bypasses the gate) from ever running while the queue is paused. Because the task exits, **resume must re-spawn it** — `_resume_prep()` walks `_offline_jobs`, flips every `"paused"` job back to `"pending"`, and `asyncio.create_task(_run_offline_job(...))` again. If you add a new place that pauses jobs, route resume through `_resume_prep()` or the paused jobs will never restart.

"Stop now" (`_pause_prep(kill=True)`) terminates the in-flight encode via the `job["_proc"]` handle and sets `job["_paused_kill"]` so `_run_offline_job` reads the non-zero ffmpeg return code as an intentional pause (re-queue as `"paused"`, delete the `.part` dir) rather than a real `"error"`. HLS prep has no mid-file checkpoint, so a killed file restarts from scratch on resume — don't assume partial segments are reusable. Never serialize a job dict straight to JSON: `_proc` is a non-picklable `Process` (and `_paused_kill` is transient) — every endpoint extracts explicit fields, keep it that way.

### When a conversion fails, read `logs/hls.log` — not the UI

The prep UI only shows the last 500 chars of `job["error"]` (an ffmpeg stderr tail). The **full** diagnosis — the exact ffmpeg command line, return code, elapsed time, and the last 300 lines of stderr — goes to `logs/hls.log` (and `logs/streamlink_app.log`) via `hls_log`. A conversion that "fails 3-4 s after starting" is almost always ffmpeg rejecting an argument or a stream mapping at startup; the stderr in `logs/hls.log` names the cause. See [BACKEND.md § Logging](BACKEND.md#logging).

### ffmpeg's stderr must be drained *while* it runs, not after `proc.wait()`

`_run_offline_job` reads ffmpeg's stderr concurrently into a bounded `deque` via a `_drain_stderr` task that runs alongside `proc.wait()`. Do **not** "simplify" this back to reading `proc.stderr.read()` after the process exits: ffmpeg writes stream mapping + warnings + errors to stderr even with `-nostats`, and if nobody drains the pipe the OS buffer (~64 KB) fills, ffmpeg blocks on `write()`, and `proc.wait()` hangs forever — the job sits at "processing" with no timeout. Same rule applies to the `-progress pipe:1` stdout drain. Both tasks end naturally on pipe EOF once the process exits; we `wait_for(..., timeout=5)` them afterward purely as a wedge guard.

### hls.js vs Safari native is a runtime branch, not a build-time pick

`_lpLoadIndex` checks `window.Hls.isSupported()` (which returns true on every MSE-capable browser and false on iOS Safari, which has no MSE — Safari plays HLS via the platform stack instead). The two paths read/write **different APIs** for **audio** selection:
- **hls.js**: `hls.audioTrack = idx`, `hls.recoverMediaError()`. The element's `<video>.audioTracks` will be empty — hls.js owns audio-rendition selection.
- **Safari native**: `<video>.audioTracks[i].enabled`. There is no hls.js instance — `lp.hls` is null.

**Subtitles are the exception and are now engine-agnostic:** they're `<track>` children of `<video>` (bundle `sub_<i>.vtt` + on-disk sidecars), so `_lpApplySubIdx` toggles `tr.el.track.mode` the same way regardless of `lp.hls`. Don't route subtitles back through `hls.subtitleTrack` — there are no in-manifest subtitle renditions to select.

### The quality (Res) menu is hls.js-only and driven by `hls.levels`, not meta.json

`_lpRenderTrackRows` builds the **Res** dropdown from `lp.hls.levels` (the hls.js master-playlist parse) and `lpSetQuality` sets `lp.hls.currentLevel` (`-1` = Auto/ABR; a level index pins a rung). Two things to keep in mind:
- **Don't drive the menu from `meta.json:videos[]`.** hls.js owns the level array and its indices; reading `hls.levels` keeps the dropdown values aligned with `currentLevel` no matter how hls.js orders them. `videos[]` is informational only (admin/API).
- **Safari native HLS gets no Res row.** Safari auto-adapts among the variants but exposes no reliable API to *pin* a level, so `_lpRenderTrackRows` leaves `#lpQualityRow` hidden when `lp.hls` is null and `lpSetQuality` no-ops. Don't try to wire a manual selector to `<video>` for Safari — there isn't one.
- **Quality is session-only.** Unlike audio/sub picks, it's not persisted via `/local-tracks` — the right rung depends on the current connection, so every session starts at Auto.

### Always destroy the previous hls.js instance before re-using `<video>`

When advancing to the next episode or switching files, `_lpDestroyHls()` MUST run before assigning a new `<video>.src` or `attachMedia`-ing a fresh hls.js instance. Otherwise the old hls.js keeps a reference to the media element and can fight the new pipeline (especially on Safari, where a leftover hls.js error handler will fire on the new native-HLS playback). `lpUnloadCurrent` does this; if you add a new code path that swaps the source, call `_lpDestroyHls` there too.

### Bundle subs and sidecar subs share the `<video>.textTracks` array

When hls.js is active, it surfaces the bundle's subtitle renditions through its own `hls.subtitleTracks` API. We also append sidecar `.srt`/`.vtt` files (from `_list_sidecar_subs`) as `<track>` children on the `<video>`, which lands them in `video.textTracks` **after** the bundle's tracks. The frontend uses a sentinel `"sidecar:N"` string for sidecar picks in the dropdown so the index space doesn't collide with bundle indices. If you add a new subtitle source, follow the same naming convention or the audio/sub-pick persistence will save garbage indices.

### Image subs (PGS / VOBSUB / DVB) are intentionally not in the bundle

`_ffprobe_full` flags subs with `codec_name in {hdmv_pgs_subtitle, pgssub, dvd_subtitle, dvdsub, dvb_subtitle, vobsub, xsub}` as `image_based: True`. `_build_hls_ffmpeg_args` filters them out before mapping streams — HTML5 `<video>` can't render bitmap subs through `<track>`, and ffmpeg can't transmux them to WebVTT (would need OCR). They surface in `meta.json:skipped_image_subs` for the UI to flag. If a user complains "my subs are missing on the phone but show in VLC", check this list first. The VLC path reads the source MKV directly so image subs work there.

### Cache key is sha256(VERSION | path | mtime | size), and VERSION includes layout

`OFFLINE_CACHE_VERSION = "v3-hls"`. Bumping the version invalidates every existing bundle because it changes the key. Old `<sha>.mp4` cache files map to *different* keys under v3 (since v3 keys never resolve to a `.mp4`), so they auto-orphan and surface in the admin tab. If you change the ffmpeg invocation in a way that breaks compatibility (segment naming, codec, container), bump the version — don't try to be clever about partial invalidation.

### Path traversal in `/offline-cache/{key}/{filename}`

`offline_cache_bundle_file` enforces `_CACHE_KEY_RE = ^[a-f0-9]{24}$` and `_BUNDLE_FILE_RE = ^[A-Za-z0-9._-]+$`. The cache_key check kills obvious traversal (`..`, `/`, leading dots); the filename check kills the same plus URL-decoded variants. Don't relax these — even though FastAPI's path-param parser doesn't pass `/` through `{filename}` by default, Path arithmetic with a malicious filename could still resolve outside the cache root.

### `/prep-all` must serialize ffmpeg jobs

`/api/library/{id}/prep-all` enumerates every video file in a library item. Without a global concurrency cap, that fires `asyncio.create_task(_run_offline_job(...))` for each file in one tight loop — a 77-episode pack instantly spawns 77 ffmpeg processes. Two failure modes both trip:
1. **NVENC session limit.** Consumer NVIDIA encoders (Pascal/Turing) reject NVENC sessions past the driver's 2–3-encoder cap. Excess jobs ffmpeg-exit immediately with `Cannot load nvcuda.dll`-style errors, the job's `error` field is set, and the UI tallies them as "prep errors".
2. **CPU/IO storm on the libx264 path.** Even with `-threads 2`, 77 concurrent ffmpegs is 150+ encoder threads plus 77 decoders fighting over the same disk, OOM-killing some and timing out others.

Keep the `_offline_job_sem()` semaphore in place (`OFFLINE_JOB_CONCURRENCY = 1`). Jobs sit in `status="pending"` until they acquire it; both `/prep-status` and `/api/offline-active` already treat `pending` as in-progress, so the UI behaves correctly. If you ever raise the cap, also re-baseline `started_at` inside the semaphore (already done) so per-job ETAs don't include queue time.

### Resume seek lands on segment boundaries

HLS playback seeks land on the nearest fmp4 segment boundary, then plays from there. With 6-second segments, the resume position can drift up to ~6 s after the saved position. The browser handles the within-segment offset automatically after the segment loads, so this is mostly invisible — but if a user reports "my resume is always a few seconds late on the browser player but not VLC", this is why. Don't shrink the segment size to compensate (you'd just multiply the segment count without solving the underlying snap-to-boundary behavior).

### Local-player track picks ≠ VLC track picks

Two parallel persistence systems live in `file_progress`:
- `audio_track` / `subtitle_track` — VLC's elementary-stream IDs (from `"Stream N"` keys of `vs.information.category`). Set via `/api/vlc/track/audio/{id}`, applied by `_apply_track_prefs` after a short delay on VLC playback start.
- `local_audio_idx` / `local_subtitle_idx` — 0-based indices into the HLS bundle's `meta.json.audios` / `subtitles` arrays. Set via `/api/library/{id}/local-tracks`, applied by the frontend on `MANIFEST_PARSED` / `loadedmetadata`.

The two are intentionally independent — a user who switches audio to Japanese in VLC on TV might still want English on their phone (different speakers / different room). `update_progress` and `mark_watched` both preserve **all four** keys across writes. Don't merge them into a single field thinking "they mean the same thing" — they don't.

### ASS/SSA styling is lost in HLS conversion

ffmpeg's `-c:s webvtt` strips karaoke effects, positioning tags, custom fonts, and animations from ASS/SSA source subtitles down to plain WebVTT. Acceptable for the vast majority of content; jarring for anime fansubs. The deferred fix (Milestone 16.10) is to ship libass.js + a WebAssembly font renderer (~200 KB JS) and render styled subs onto a canvas overlay. Not implemented until someone actually complains. Don't go halfway by piping unstyled ASS into the bundle — players treat it as broken WebVTT.

### Service worker is an eviction stub — keep it that way

`static/sw.js` exists only to unregister itself and `caches.delete` everything it ever cached, so devices with the old "Handoff" SW installed don't stay pinned to a stale app shell. Don't reintroduce caching strategies, navigation fallbacks, or API caches in `sw.js`. Once enough time has passed that no device has the old SW alive, the file and the `evictLegacyServiceWorker` call in `index.html` can be deleted entirely.

## Settings

### Two layers of settings

1. **`.env`** (loaded by `pydantic-settings`) — service URLs, credentials, buffer thresholds, admin password
2. **`library.json` → `settings`** — UI-managed library paths, admin overrides (`indexer_categories`, `tmdb_api_key`)

`/api/search` reads `indexer_categories` from the admin override first, falling back to `.env`. Library paths are unioned across both. `_tmdb_effective_key()` follows the same admin-beats-env precedence.

## TMDb metadata

### Auto-match grabs the most-popular result

`_tmdb_match_show` ([main.py](../main.py)) calls `/search/tv` (or `/search/movie` for single-file no-season items) and takes the **first** result. TMDb's search ranks by popularity, so for ambiguous titles ("Monster", "The Office", "It") the match may be the wrong show. Recovery path: an admin POSTs `/api/library/{id}/metadata/refresh` with `{tmdb_id: <correct>, kind: "tv"|"movie"}` to force-bind the item to a specific TMDb entry. The result is cached on `item["metadata"]` and only re-fetched on another `refresh=1`.

### Season tab uses `f.season` parsed off disk

The season list in the episode page (`epSeasonList`) is built from `parse_season_episode` on the file paths, not from TMDb. This is intentional — TMDb has the canonical seasons, but the **on-disk** files are what the user can actually play. A file with no parseable `SxxEyy` lands in season `0` and shows up in the no-season fallback branch. If TMDb says season 4 exists but the user only has files for seasons 1–3, season 4 never appears as a tab.

### Episode stills are joined by (season, episode) pair

`_tmdbEpisode(file)` matches the file's `(season, episode)` against `metadata.seasons[N].episodes[*]`. If the filenames are mis-labelled — e.g. an anime cour where the on-disk numbering restarts each cour but TMDb uses one continuous season — the still and overview will be wrong even though the show match is right. The TMDb episode overview is still better than nothing; the user can always rename files or override the match. Don't add complex episode-offset heuristics without a clear failure case.

## Python compatibility

`setup.py` and `run.py` are run by **system Python** (any version 3.9+). They use `from __future__ import annotations` so they parse on 3.9. `main.py`, `analyzer.py`, `watchdog.py`, `daemon.py` run inside the venv (also 3.9+ baseline but the project doesn't pin newer syntax).

### Windows: Microsoft Store Python / per-user Python breaks multi-user use

A Windows venv's `.venv\Scripts\python.exe` is a tiny launcher that re-executes the **base** Python recorded in `pyvenv.cfg`. If the base Python was installed per-user (e.g. Microsoft Store Python at `C:\Users\<name>\AppData\Local\Microsoft\WindowsApps\PythonSoftwareFoundation.Python.3.x_...\python.exe`), that path is only readable by `<name>`. Any other user — including the scheduled task running as a different account — gets `Access is denied` and the wrapper silently fails (no log written because the wrapper process never starts).

Symptoms:
- `python run.py` from a different user fails with `did not find executable at 'C:\Users\<other>\AppData\Local\Microsoft\WindowsApps\...python.exe': Access is denied.`
- `run.py --install` succeeds but the service never runs and `logs\streamlink_service.log` stays empty.

Fix: install Python from python.org with "Install Python for all users" checked (lands in `C:\Program Files\Python3xx\` — world-readable), uninstall the Microsoft Store Python, turn off the `python.exe`/`python3.exe` app-execution aliases (Settings → Apps → Advanced app settings → App execution aliases), `Remove-Item -Recurse -Force .venv`, then `py -3 -m venv .venv` and `python setup.py` again.

### Windows: don't use `/RL HIGHEST` on the scheduled task

`daemon.py` deliberately omits `/RL HIGHEST` from the `schtasks /Create` call. On Windows, ports below 1024 do not require admin to bind (the "privileged ports" concept is Unix-only), so the wrapper doesn't actually need elevation to serve port 80/443. Adding HIGHEST would force Task Scheduler to try to elevate the user's token at trigger time — which fails silently for Standard Users (they have no admin to elevate to), leaving the task registered but never running. Firewall rules (which DO need admin) are added once during `_windows_install` while the install process holds the admin token from UAC.

### Windows: scheduled task `/RU` must be the console user, not `USERNAME`

When `_windows_install` runs after a UAC bounce (or from any "Run as Administrator" shell), `os.environ['USERNAME']` is the admin account that accepted the prompt, not the regular user logged in at the keyboard. Registering with `/RU <admin>` ties the task to the admin's logon trigger, so the task never fires for the actual user. `_windows_console_user()` queries `WTSGetActiveConsoleSessionId` + `WTSQuerySessionInformationW` to find the real interactive user (PowerShell `Win32_ComputerSystem.UserName` fallback). The install output prints the detected `RunAs` so the user can verify.

## Scheduled reboot

### Scheduled-reboot loop guard

The single most dangerous bug here is a **reboot loop**: the machine reboots at the scheduled time, comes back up (auto-login + service) still past that time, re-arms, sees itself idle, and reboots again every couple of minutes. `scheduled_reboot_loop` prevents this by persisting `settings.scheduled_reboot.last_fired = <tz date>` to `library.json` **before** calling `_reboot_machine()`. On the way back up the loop reads `last_fired == today` and stands down until tomorrow. If you ever refactor this, keep the write-then-reboot order and make sure the `put_library` completes (it's `await`ed) before the reboot fires. Saving config from the admin UI resets `last_fired` to `""` so a newly-set time can still arm the same day.

There is intentionally **no upper time window** on arming: if the host was powered off at the scheduled time and only came up hours later, it still gets one daily reboot when next idle. The `last_fired` guard caps that at one per tz-day, so the worst case is a single "catch-up" reboot, not a loop.

### Reboot needs host permission; "in use" must include the TV

`_reboot_machine()` tries a platform chain (macOS System Events restart works from a launchd *user agent* without sudo; Linux/Windows may need passwordless `sudo`/elevation). If none succeed it logs a hint rather than throwing — a failed reboot must not crash the loop. Separately, `_machine_in_use()` must check **live VLC state**, not just `state.last_activity`: someone watching on the TV makes no HTTP requests for the whole episode, so an activity-timestamp-only check would call the box idle and reboot mid-movie. Active streams and downloads count as in-use too, so a nightly reboot never interrupts a download.

## Networking / mDNS

### `remote.local` doesn't resolve after a reboot

mDNS registration must be **resilient, not one-shot**. The installed service (launchd/systemd) starts at login/boot **before Wi-Fi has associated and the interface has a LAN IP**. A single `start_mdns(get_local_ip(), …)` at startup sees `get_local_ip() == ""` and silently skips registration — so `remote.local` never resolves, even though uvicorn binds `0.0.0.0` and becomes reachable **by IP** the moment the network comes up. Classic symptom: "remote.local works right after `run.py --install` (network was already up) but not after a reboot; the IP still works." Both `run.py` and the service wrapper use `start_mdns_resilient()`, which registers from a daemon thread that waits for the IP and re-registers if it changes. Don't revert either call to the bare one-shot `start_mdns()`. After changing the wrapper, re-run `python3 run.py --install` to regenerate `streamlink_service.py`. See [RUNTIME.md](RUNTIME.md#mdns-runpy734).

## See also

- [BACKEND.md](BACKEND.md) — invariants enforced by `main.py`
- [DAEMON_WATCHDOG.md](DAEMON_WATCHDOG.md) — VPN guard at the process level
- [ANALYZER.md](ANALYZER.md) — Smart Skip algorithm details and fallback chain
