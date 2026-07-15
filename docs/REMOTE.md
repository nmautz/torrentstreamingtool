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
| Vol + / Vol − | `VK_VOLUME_UP` / `VK_VOLUME_DOWN` (0xAF/0xAE) | Volume ±`REMOTE_VOLUME_STEP` (5) per press; auto-repeats while held. **Never the host OS mixer** — VLC's amp (capped), or the YouTube player's own gain |
| ⏭ Next track | `VK_MEDIA_NEXT_TRACK` (0xB0) | Skip **forward** `REMOTE_SEEK_STEP_SECS` (10 s) |
| ⏮ Prev track | `VK_MEDIA_PREV_TRACK` (0xB1) | Skip **back** 10 s |
| 🏠 Home | `VK_BROWSER_HOME` (0xAC) | **Stop playback + show the TV UI** (unsuppressed this key launches the default browser — Edge — which is why it must be claimed) |
| ← Back | `VK_BROWSER_BACK` (0xA6) | Firestick semantics: during playback **exit the player back to the TV UI** (stop + kiosk, same path as Home); with the TV UI up **step back inside the dashboard** — close the topmost open modal, else drop focus (relayed as the `tv_command` SSE event; unsuppressed the kiosk Chrome would history-back away from `?tv=1`). Idle it passes through and wakes the TV UI like any button. Remotes whose Back emits **Escape/Backspace** instead work too — those reach the kiosk directly and `_tvNavKey` routes them to the same `_tvBack()` |
| OK | `VK_RETURN` (0x0D) — or a left **click** in pointer mode | During playback: ⏯ toggle (both forms). With the TV UI up: activates the focused element (D-pad navigation) |
| Arrows / mouse ring | normal keys + pointer | Not intercepted — arrows **navigate the TV UI** (spatial focus, see below); the pointer stays a mouse. Any of them wakes the UI when idle |

The next/prev *track* keys are mapped to ±10 s seeks (not playlist next/prev)
because that's what the buttons mean on a streaming remote.

## Claim rules (which presses we act on + suppress)

`_remote_should_handle(action)` — called synchronously on the hook thread,
plain attribute reads only:

- **"Use My Computer" pause active** (`window_mgmt_paused()`): nothing is
  claimed. A real keyboard's volume keys must drive the OS mixer and Home may
  open the user's browser — it's their desktop.
- **`home`**: claimed whenever the TV UI is enabled OR playback is up.
- **`back`**: claimed during playback (exit to the TV UI) and while the kiosk
  holds the screen (`tv_ui_active` — Chrome must not treat the key as
  history-back). Idle it passes through as generic input (wakes the TV UI).
  Windows-only, like `home` — pynput exposes no browser keys off-Windows.
- **`volume_up` / `volume_down`**: **always claimed** — the remote's volume
  must never reach the host OS mixer (no Windows overlay, no changed system
  volume after a session). Idle presses adjust VLC's amp (they apply to the
  background video / next playback); during YouTube they step the IFrame
  player's own gain via the `player_volume_step` yt_command (tv.html), NOT
  `set_system_volume`. The dashboard *slider* keeps its documented OS-volume
  behaviour during YouTube — this rule is remote-only.

  Enforcement is **hook-only by default** — claim + suppress + route, the
  behaviour verified on the reference remote since v9.13.0. For a remote
  whose volume provably bypasses the keyboard hook (HID consumer-control
  usages the hook can't suppress: system volume changes even with remote
  support running), there is an **opt-in** backstop: `REMOTE_VOLUME_GUARD=1`
  enables `remote_volume_guard` (`main.py`, Windows-only, ~3 Hz
  endpoint-volume poll via the winvol helper) — every legitimate OS-volume
  writer records itself via `set_system_volume`
  (`state.host_volume_expected`); any other change is reverted and its delta
  re-routed to VLC / the player gain, with a ~1.2 s dedupe window
  (`_remote_vol_key_ts`) so remotes emitting *both* forms don't double-step.
  It is off by default because an always-on guard **fights quantizing audio
  endpoints** and drip-steps VLC's volume — see
  [GOTCHAS.md](GOTCHAS.md#and-the-os-mixer-volume-guard-is-opt-in--an-always-on-guard-fights-quantizing-audio-endpoints).
- **`ok`** (Enter): claimed only during real playback while the TV UI is not
  up → acts as ⏯. Otherwise it passes through so it activates the focused
  element in the kiosk. Its pointer-mode twin (left click) is handled off the
  activity feed: a click during VLC playback (the kiosk is behind fullscreen
  VLC, so every click lands on the video) fires ⏯ too, 0.4 s debounced;
  YouTube is excluded because the IFrame player already toggles on clicks.
- **Remaining media keys**: claimed only while real playback is up (VLC
  stream/library play incl. paused, or YouTube-on-TV). When idle — incl. the
  idle background video — they pass through untouched (and count as generic
  input, so they wake the TV UI like any other button).

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
- `back` during playback takes the same show-UI-then-stop path as `home`;
  with the kiosk up (nothing playing) it broadcasts `tv_command
  {action:"back"}` instead — the `?tv=1` page's `_tvBack()` closes the
  topmost open modal (via the modal's own `close*` button so its cleanup
  runs), else blurs the focused element (dismisses a card overlay).
- **Debounce** (`_MIN_INTERVAL`): play/pause and the seeks fire once per press
  (0.30–0.35 s), Back 0.4 s, Home once per second; volume repeats while held but is
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
playback ── 🏠 Home or ← Back ───────────────────▶ stop + TV UI
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
  deliberate way to the UI is 🏠 Home. On Windows, **injected keys**
  (`LLKHF_INJECTED`) are ignored entirely — the focus cocktail's own synthetic
  ALT press (`_vlc_focus_windows`) would otherwise re-wake the UI ~1.5 s after
  every idle hand-back, flashing the background video and stealing the screen
  right back (see docs/GOTCHAS.md).
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
- **D-pad spatial navigation** (`_tvNavKey`, TV mode only): arrow keys move
  focus to the geometrically nearest actionable element in that direction
  (distance + off-axis-penalty scoring inside a forgiving cone; the target is
  scrolled to center and marked by the `.tv-mode :focus` indigo ring), OK /
  Enter activates it (native click for buttons/links, synthesized `click()`
  for `[onclick]` tiles). Navigation is scoped inside the topmost open
  `*Modal` so focus can't wander behind a dialog; arrows that edit a control
  (←/→ in text inputs, ↑/↓ in selects) are left alone, and with no candidate
  in a direction the key falls through to native scrolling. Hold-to-activate
  buttons (Stop, handoff) still need the pointer — Enter fires a plain click,
  not a pointer hold. **Back** (`_tvBack()`, fired by the `tv_command` SSE
  event or a direct Escape/Backspace keypress — Backspace still deletes in
  text fields) closes the topmost open `*Modal` via its own `close*` button
  (fallback: add `.hidden`), else blurs the focused element.
- **TV card overlay**: hovering or D-pad-focusing a library/show poster card
  reveals a full-card overlay split horizontally — **▶ Play/Resume top,
  Episodes bottom** (Episodes only for multi-file items/shows); the centered
  ▶ button is hidden on TV. Entering a card with the D-pad always lands on
  Play/Resume: the Play button carries `data-tv-default`, the overlay is a
  `data-tv-group`, and `_tvNavKey` redirects any cross-group entry to the
  group's default (movement *within* the group — Play ↔ Episodes — is left
  alone); `_tvCandidates` excludes the poster itself whenever it contains
  `.tv-card-btn`s so the buttons don't compete with their container. The
  buttons are invisible-but-focusable (opacity 0, revealed by
  `:hover`/`:focus-within`) and reuse the card delegation classes
  (`lib-tile-open`/`lib-show-open`/`lib-show-play`); their inline handlers
  stop Enter/Space + click propagation so OK on Play can't also fire the
  poster's open handler — **Enter/Space only**, an unconditional keydown
  stop would swallow the arrows and freeze navigation on the buttons.
- Forces `hlsAvailable = false`: VLC *is* "on device" on the TV, so every
  Prep / On-Device / play-chooser affordance is hidden and the play chooser
  collapses straight to VLC (the same path a no-HLS macOS host uses).
- Additionally hides the hand-off-to-this-device buttons (`#handoffBtn`,
  `#fcHandoffBtn`) via `.tv-mode` CSS and the download-to-device buttons via a
  `TV_MODE` guard in the library-card renderer.
- **TV layout** (`.tv-mode` CSS): the desktop/phone chrome the remote can't
  use well is stripped — the library toolbar (storage gear, Upload, Hidden,
  Refresh), the per-card ⋯ action drawers (`.lib-cardv-more`), the
  fullscreen-controls overlay + its opener (`#fullscreenControls` /
  `#fullscreenBtn`; `openFullscreenControls()` also early-returns in TV mode
  so nothing can open it), and the **entire player footer** — on the TV the
  remote is the transport. The page is upscaled (`zoom: 1.15`, safe in the
  Chrome-only kiosk) for 10-foot readability, and `main`'s footer-clearance
  padding is reclaimed. Fewer focus stops also makes the D-pad navigation
  predictable.
- **Play-press loading overlay** (10.6.0): with the footer gone, a ▶ Play
  press had no visible feedback until VLC took the screen. `renderPlayer`
  mirrors `stream_status === "buffering"` into a fullscreen `#tvLoading`
  overlay (square Metro spinner + title + the footer's "Starting playback…" /
  MB-progress line). It appears instantly on the press — `_optimisticBuffering`
  renders before the `/play` request is sent — and clears when the SSE state
  event flips to playing/idle (or `_revertOptimistic` on a failed request).
  `pointer-events: none` so a stuck buffer never traps the D-pad; z-index above
  modals so it shows over the episode picker too.
- **TV is browse + play only — management/detail chrome is hidden** (10.5.0),
  left to any phone/desktop browser. The same `.tv-mode` CSS block hides the
  profile-settings gear (`#navSettingsBtn`), the profile picker's Manage
  profiles / Admin links, and the search Sources/Categories pickers
  (`#srcWrap`/`#catWrap`); on the **episode page** it hides the rename /
  fix-metadata / on-demand-only hero buttons, the bulk-select chips
  (`#epBulkChips`), and the selection-driven Download (N) / Play bottom
  buttons (`#epDownloadSelBtn`/`#epPlaySelBtn` — selection doesn't exist on
  TV, so they'd be dead). `TV_MODE` guards in the renderers additionally drop
  the per-episode checkboxes + inline action row (watched toggle, prep,
  fetch-now, download-to-device) and priority rows (`_epCardHtml`), the
  download/prep/recheck scheduling bar + Save ZIP (`renderEpList`), and the
  movie panel's secondary actions/scheduling (`_epMoviePanel` — status + the
  sticky ▶ Play only). Episodes play by activating their stills; Shuffle and
  Close stay in the bottom bar. The library is **forced to poster-card view**
  (`libViewMode="card"`) regardless of the kiosk's stored preference — list
  view's dense rows and action strips are touchscreen chrome.
- Everything else is the stock dashboard — profiles, search, library.

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
