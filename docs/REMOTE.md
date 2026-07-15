# Wireless remote (air-mouse) support + TV UI

How a cheap "Air Fly Mouse"-style wireless remote drives the host TV box —
both direct playback control (media keys → VLC/YouTube) and the
**Firestick-style TV UI** (the dashboard itself in a fullscreen kiosk, woken
by any button, handed back to the idle background video when unused).

Code: [remote_input.py](../remote_input.py) (global input listener) + three
areas of `main.py`: the `_remote_should_handle` / `_remote_key_action` pair
(gate + dispatcher, after the `/api/vlc/seek/to` route), the TV UI block
(`_TVUI_WINDOW_MARKER` … `tv_ui_loop`, before the `/tv` route), and the
`?tv=1` handling in `static/index.html`.

## How these remotes work

Air-mouse remotes (Air Fly Mouse, MX3, W1/W2, Rii i8, G7 and the countless
rebrands) pair a 2.4 GHz USB dongle (or Bluetooth) with the host and enumerate
as a **standard HID keyboard + mouse** — no driver, no SDK. Their buttons emit
ordinary consumer key codes:

| Remote button | HID / Windows VK code | Mapped action |
|---|---|---|
| ⏯ Play/Pause | `VK_MEDIA_PLAY_PAUSE` (0xB3) | Toggle pause |
| Vol + / Vol − | `VK_VOLUME_UP` / `VK_VOLUME_DOWN` (0xAF/0xAE) | Volume ±`REMOTE_VOLUME_STEP` (5) per press; auto-repeats while held |
| ⏭ Next track | `VK_MEDIA_NEXT_TRACK` (0xB0) | Skip **forward** `REMOTE_SEEK_STEP_SECS` (10 s) |
| ⏮ Prev track | `VK_MEDIA_PREV_TRACK` (0xB1) | Skip **back** 10 s |
| 🏠 Home | `VK_BROWSER_HOME` (0xAC) | **Stop playback + show the TV UI** (unsuppressed this key launches the default browser — Edge — which is why it must be claimed) |
| Arrows / OK / mouse ring | normal keys + pointer | Not intercepted — they drive the TV UI kiosk as ordinary keyboard/mouse input (and wake it, see below) |

The next/prev *track* keys are mapped to ±10 s seeks (not playlist next/prev)
because that's what the buttons mean on a streaming remote.

## Claim rules (which presses we act on + suppress)

`_remote_should_handle(action)` — called synchronously on the hook thread,
plain attribute reads only:

- **"Use My Computer" pause active** (`window_mgmt_paused()`): nothing is
  claimed. A real keyboard's volume keys must drive the OS mixer and Home may
  open the user's browser — it's their desktop.
- **`home`**: claimed whenever the TV UI is enabled OR playback is up.
- **Media keys**: claimed only while real playback is up (VLC stream/library
  play incl. paused, or YouTube-on-TV). When idle — incl. the idle background
  video — they pass through untouched (and count as generic input, so they
  wake the TV UI like any other button).

## Dispatch

```
remote button ──HID──▶ host input events
                          │  pynput global listeners (remote_input.py, own threads)
                          │  gate: _remote_should_handle(action)
            ┌─────────────┴───────────────────────────┐
            ▼ handled keys                            ▼ everything else
  _remote_key_action(action)                _tv_input_event(kind)  (activity feed)
  media → VLC / YouTube control paths       key/click wake the TV UI when idle;
  home  → _tv_ui_show + /api/stop           move/media only refresh its idle timer
```

- Media dispatch reuses the exact endpoint code paths, so the admin volume
  cap, the YouTube OS-mixer volume rule, and SSE state sync all apply; a
  `state` snapshot is broadcast after each action so open dashboards reflect
  the press without waiting for the 2 s `stat_broadcaster` tick.
- `home` shows the TV UI **first** (so `tv_ui_active` gates the focus churn),
  then calls the `/api/stop` handler if anything was playing.
- **Debounce** (`_MIN_INTERVAL`): play/pause and the seeks fire once per press
  (0.30–0.35 s), Home once per second; volume repeats while held but is
  throttled to ~10 Hz. The activity feed has its own throttle
  (`_ACTIVITY_MIN_INTERVAL`, mouse moves ≤ 1/s).

## TV UI (Firestick-style dashboard kiosk)

The dashboard itself, opened by the backend in a fullscreen Chrome kiosk at
**`http://127.0.0.1/?tv=1`**, driven from the couch with the remote's pointer
and buttons. The host display cycles between three surfaces:

```
idle background video ──any remote button/click──▶ TV UI (dashboard kiosk)
TV UI ── no input for TV_UI_IDLE_SECS (120 s) ───▶ background video
TV UI ── user plays something ───────────────────▶ VLC / YouTube fullscreen
playback ── 🏠 Home ─────────────────────────────▶ stop + TV UI
```

Mechanics (`main.py`):

- **`state.tv_ui_active`** = "the kiosk should hold the screen". While set,
  every VLC focus assertion stands down: `vlc_focus_and_fullscreen` bails
  (same as it does for `youtube_active`), `background_video_loop` won't
  (re)start the idle video underneath the kiosk, and `_play_background_video`
  skips its focus call. It is **released** the moment real content plays —
  the `vlc("in_play")` branch and `youtube_play` both clear it — so those
  paths regain the screen; `tv_ui_loop` also clears it as a janitor, but only
  when the last input is >10 s old (a just-pressed Home must not be robbed of
  the screen it claimed while `stop()` is still tearing down).
- **Wake** (`_tv_input_event`, called from the hook threads): every input
  stamps `state.tv_input_last`; only a **key or click** wakes the UI, and only
  while nothing is playing. Pointer **motion never wakes** (gyro drift would
  light the TV at night) and neither do handled media keys (a volume press
  during playback is not a request for the dashboard). During playback the
  deliberate way to the UI is 🏠 Home.
- **Show** (`_tv_ui_show`): pauses the background video (`pl_forcepause` —
  keeps its position; `background_playing` stays True so the loop stays out),
  launches the kiosk if its Chrome isn't running (matched by
  `--user-data-dir`), then `_bring_tvui_to_front` (minimize VLC + the same
  Windows force-foreground cocktail as the YouTube kiosk, marker
  `_TVUI_WINDOW_MARKER` = "StreamLink TV Dashboard", set via `document.title`
  by `?tv=1`). The kiosk is **left running** when hidden, so later wakes are
  instant.
- **Hide** (`tv_ui_loop`, every 5 s): after `TV_UI_IDLE_SECS` with no input
  and nothing playing, `pl_forceresume` the background video +
  `vlc_focus_and_fullscreen`. If **no background video is configured**, the
  UI simply stays up (a black VLC window is worse than the dashboard).
- **Separate Chrome profile** (`.tvui_chrome_profile`): must differ from the
  YouTube kiosk's `.tv_chrome_profile` — YouTube's Stop kills its kiosk by
  matching the profile path in the cmdline and would otherwise take the
  dashboard down too. The dir also persists the kiosk's own localStorage
  (profile pick, UI prefs).

### `?tv=1` frontend mode (`static/index.html`)

- Sets `document.title = "StreamLink TV Dashboard"` (the window marker — keep
  in sync with `_TVUI_WINDOW_MARKER`) and adds the `tv-mode` + `no-hls` body
  classes.
- Forces `hlsAvailable = false`: VLC *is* "on device" on the TV, so every
  Prep / On-Device / play-chooser affordance is hidden and the play chooser
  collapses straight to VLC (the same path a no-HLS macOS host uses).
- Additionally hides the hand-off-to-this-device buttons (`#handoffBtn`,
  `#fcHandoffBtn`) via `.tv-mode` CSS and the download-to-device buttons via a
  `TV_MODE` guard in the library-card renderer.
- Everything else is the stock dashboard — profiles, search, library, admin.

## Platform behaviour (Windows first)

| Platform | Listener | Suppression | TV UI |
|---|---|---|---|
| **Windows** | pynput low-level keyboard + mouse hooks | **Yes, selective** — `win32_event_filter` + `suppress_event()` swallow only the handled keys, only when claimed. Required: without it the OS mixer also changes volume, a focused VLC also toggles pause, and Home opens Edge. Dispatch happens *inside* the filter (a suppressed event never reaches `on_press`); both key-down and key-up are swallowed. | Full (kiosk focus cocktail) |
| **Linux (X11)** | pynput X listeners (need `DISPLAY`) | No selective suppression — the DE may also act on volume keys. No Home action (pynput exposes no browser-home key off-Windows). | Wake/hide work; kiosk raise via `wmctrl` if installed |
| **macOS** | pynput CGEventTap | No suppression; requires the **Input Monitoring** permission (TCC) — without it the listeners receive nothing (so the TV UI never wakes). Dev convenience only. | Launch only; no reliable re-raise of an existing kiosk |

Failure to start is always non-fatal: `start_listener` logs one warning and
returns `None`; the dashboard runs without remote support. A **session-0
Windows service receives no input** (hooks are per-desktop) — the installed
Task Scheduler service runs in the interactive user session, so this only
bites truly headless setups. The mouse listener is best-effort on top of the
keyboard hook; if it fails, buttons still work but pointer motion won't
refresh the idle timer.

## Configuration

| `.env` key | Default | Meaning |
|---|---|---|
| `REMOTE_CONTROL` | `1` | Master switch for the input listener (media keys, Home, TV UI wake signal) |
| `TV_UI` | `1` | The Firestick-style dashboard kiosk (needs `REMOTE_CONTROL` for its wake signal) |
| `TV_UI_IDLE_SECS` | `120` | No-input window before the UI hands the screen back to the background video (floor 15 s) |

Step sizes are the `REMOTE_SEEK_STEP_SECS` / `REMOTE_VOLUME_STEP` constants in
`main.py` (10 s / 5 %), matching the dashboard footer's small-skip and
volume-± buttons.
