# YouTube on TV

Play any YouTube link **on the TV** (the host display) and drive it from the
dashboard like every other playback. Unlike Search/Library, nothing is
downloaded or routed through VLC: the video plays in a fullscreen **Chrome
kiosk** on the host via the **YouTube IFrame Player API**, and the dashboard
controls it remotely over SSE.

Spans `main.py` (the `/api/youtube*` endpoints + kiosk launch/kill),
`static/tv.html` (the host-side player page), and `static/index.html` (the
Search-tab input + control routing).

---

## When to read this doc

Changing any of:

- `POST /api/youtube`, `POST /api/youtube/control`, `POST /api/youtube/tv-state`,
  `GET /tv`
- `_extract_youtube_id`, `_find_chrome`, `_launch_tv_browser`, `_kill_tv_browser`,
  `TV_CHROME_PROFILE`
- `static/tv.html` (the kiosk player)
- The Search-tab YouTube input / `playYoutube` / `ytControl`, or the
  `app.youtube_active` branches in the player controls

For the control-bar UI itself see [FRONTEND.md](FRONTEND.md); for endpoint
signatures see [API.md](API.md); for the footguns see [GOTCHAS.md](GOTCHAS.md).

---

## Why a browser, not VLC

VLC 3.0's bundled `youtube.lua` is perpetually broken — it "plays" the watch
page for a few seconds, never resolves a title/length (`length: -1`, the
now-playing stays the raw URL), then stops. yt-dlp resolving a direct stream
URL into VLC works but adds a Python dependency that breaks every time YouTube
changes its signatures, plays only ≤720p muxed without an audio-slave hack, and
still leaves you with VLC's UX. A browser plays YouTube natively at full quality
with adaptive streaming, ads, captions, and everything else "just working", and
the IFrame API gives clean programmatic control. See [GOTCHAS.md](GOTCHAS.md).

---

## Flow

```
Dashboard                         Backend (main.py)                 TV (host Chrome kiosk, /tv)
─────────                         ─────────────────                 ──────────────────────────
paste link → POST /api/youtube ─▶ _extract_youtube_id
                                  state.youtube_active = True
                                  vlc("pl_stop")            ───────▶ (VLC cleared off the TV)
                                  broadcast yt_command:load ───SSE──▶ loadVideoById / createPlayer
                                  if /tv not seen <6 s:
                                    _launch_tv_browser ─────────────▶ Chrome --kiosk --app=/tv?v=ID
                                                                      (fresh page autoplays from ?v=)
  footer/fullscreen controls ───▶ POST /api/youtube/control ─SSE──▶ player.pauseVideo/seekTo/…
                                  broadcast yt_command:<action>
  (dashboards mirror the TV) ◀── broadcast state ◀── POST /api/youtube/tv-state ◀─ 1 s heartbeat
hold Stop → POST /api/stop ─────▶ broadcast yt_command:close ─SSE─▶ pauseVideo + window.close()
                                  _kill_tv_browser (by profile dir)
```

### 1. Start (`POST /api/youtube {url}`)

`_extract_youtube_id` pulls the 11-char id from any common form (`watch?v=`,
`youtu.be/`, `/shorts/`, `/live/`, `/embed/`, or a bare id). The endpoint takes
over the now-playing state (`youtube_active=True`, `stream_status="playing"`,
`active_title="YouTube"` until the real title arrives), stops VLC, and:

- **broadcasts `yt_command {action:"load", video_id}`** — picked up instantly if
  a `/tv` page is already open (hot-swap, no relaunch), and
- **launches the kiosk** only if no `/tv` heartbeat was seen in the last 6 s
  (`state.youtube_tv_seen_at`). A freshly launched page reads `?v=<id>` and
  autoplays even if it missed the broadcast — so the load works whether or not
  the page was already up.

Not VPN-gated — this is ordinary HTTPS playback in a browser, not P2P.

### 2. Control (`POST /api/youtube/control {action, value}`)

A thin relay: it just re-broadcasts the command as `yt_command` over SSE. The
`/tv` page calls the matching IFrame API method. Actions: `playpause`, `play`,
`pause`, `seek` (value = ±seconds), `seek_to` (value = 0–100 %), `volume_set`
(value = dashboard 0–200), `volume_step` (value = ±delta). Returns 409 if no
YouTube video is active.

The dashboard reuses its **existing** footer + fullscreen controls — `vlcPause`,
`vlcSeek`, `handleSeekBarClick`, `vlcSetVolume`, `vlcVolumeStep`, `vlcVolume`
each branch on `app.youtube_active` and call `ytControl(...)` instead of the VLC
endpoint. Save-to-library, handoff, episode-nav and the audio/subtitle track
controls are hidden while `youtube_active`.

### 3. State reporting (`POST /api/youtube/tv-state`)

The `/tv` page POSTs a heartbeat every second (and right after acting on a
command) with `{video_id, title, time, duration, volume, playback}`. The backend
mirrors these onto the **reused display fields** — `active_title`, `vlc_time`,
`vlc_duration`, `vlc_volume` — and rebroadcasts a `state` snapshot, so the footer
and fullscreen scrubber render YouTube exactly like a VLC play with no UI
branching. The heartbeat doubles as the "page is open" signal for the
relaunch-vs-hot-swap decision in step 1.

Because those fields are shared with VLC, the VLC pollers are **gated while
youtube_active**: `stat_broadcaster` skips its VLC status read, and
`background_video_loop` skips entirely (so it can't start the idle background
video over the YouTube kiosk). `vlc_progress_tracker` already no-ops without a
library item.

### 4. Stop (`POST /api/stop`)

Clears the YouTube state, broadcasts `yt_command {action:"close"}` (the page
pauses + `window.close()`s), then `_kill_tv_browser()` hard-kills the kiosk as a
backstop. Because the YouTube play has no torrent hash and no library item, the
rest of the stop teardown (qBit delete) is naturally skipped.

---

## The kiosk

`_launch_tv_browser` runs Chrome (`_find_chrome`) as:

```
chrome --user-data-dir=<repo>/.tv_chrome_profile \
       --no-first-run --no-default-browser-check \
       --autoplay-policy=no-user-gesture-required \
       --kiosk --app=http://localhost/tv?v=<id>
```

- **`--kiosk --app=`** → borderless fullscreen, no browser chrome.
- **`--autoplay-policy=no-user-gesture-required`** → the IFrame player can
  autoplay with sound (no click on the TV, which has no mouse/keyboard).
- **dedicated `--user-data-dir`** → isolates the kiosk from the user's normal
  Chrome and lets `_kill_tv_browser` kill *only* this instance (matched by the
  profile path in the process cmdline). The dir is git-ignored.

### Browser discovery (`_find_chrome`) — Windows-first

Windows is the primary target, so it gets the widest net. In order:

1. `_CHROME_BIN` from the environment (explicit override).
2. **Windows: the `App Paths` registry** — HKCU + HKLM, for `chrome.exe`,
   `msedge.exe`, `brave.exe`, `chromium.exe`. This is the most reliable source
   and handles **per-user installs** and non-default locations.
3. **Windows: filesystem candidates** under `%ProgramFiles%`,
   `%ProgramFiles(x86)%` **and `%LOCALAPPDATA%`** (the per-user Chrome location
   the original code missed — the cause of the v3.5.0 "never starts on Windows"
   bug), for Chrome / Edge / Brave / Chromium, plus PATH shims.
4. macOS: the `/Applications/*.app/Contents/MacOS/*` binaries.
5. Linux: `google-chrome` / `chromium` / `microsoft-edge` / `brave-browser`
   (PATH + common absolute paths + the snap path).

**Edge is preinstalled on Windows 10/11**, so discovery should essentially
always resolve there. Discovery and launch both **log** (to
`logs/streamlink_app.log`) what they picked or why they failed — never fail
silently. If nothing is found, `POST /api/youtube` returns 500 with a clear
message (install Chrome/Edge or set `_CHROME_BIN`).

### Launch health-check

`subprocess.Popen` returning doesn't prove the browser actually rendered the
page (a locked profile, an instant exit, or a **session-0 Windows service with
no interactive desktop** all "launch" but show nothing). After a launch,
`_youtube_kiosk_healthcheck` waits 12 s; the `/tv` page heartbeats within ~1 s of
loading, so if `youtube_tv_seen_at` never advances past the launch time the
server clears the YouTube state and broadcasts a `stream_status:error`
("YouTube didn't start on the TV…") — instead of silently dropping back to the
idle background video.

---

## `static/tv.html`

A single self-contained page:

- Loads `https://www.youtube.com/iframe_api`; on `onYouTubeIframeAPIReady`
  creates a `YT.Player` for `?v=<id>` (or the first `load` command), autoplay on.
- Connects to `/api/events` and handles only `yt_command` events.
- Reports state to `/api/youtube/tv-state` every second and after each command.
  If the server replies `active:false` (Stop happened elsewhere) it pauses.
- A command arriving before the API is ready is queued (`pendingLoadId`); a
  `load` for the already-playing id is a no-op (just `playVideo`).

Volume mapping: the dashboard scale is 0–200 (100 = normal); YouTube is 0–100,
so `volume_set`/`volume_step` clamp to 0–100 and the reported `volume` (0–100)
is shown as a percentage in the dashboard.

---

## Limitations / notes

- Single video only — no playlists / queueing (a new link replaces the current).
- The host needs a Chromium-family browser; Safari is not used (no comparable
  kiosk + autoplay-policy flags).
- Captions/quality are controlled on the TV by YouTube itself (the IFrame player
  exposes them); the dashboard drives play/pause/seek/volume.
- The kiosk shows whatever YouTube serves (including ads). This is intentional —
  it's real YouTube, not a re-host.

---

## See also

- [API.md](API.md) — endpoint signatures + the `yt_command` SSE event
- [FRONTEND.md](FRONTEND.md) — `playYoutube` / `ytControl` + control routing
- [GOTCHAS.md](GOTCHAS.md) — broken VLC youtube.lua, autoplay flag, reused
  display fields + poller gating, kiosk kill-by-profile
