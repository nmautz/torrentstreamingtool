# Wireless remote (air-mouse) support

How a cheap "Air Fly Mouse"-style wireless remote drives playback on the host
TV box. Code: [remote_input.py](../remote_input.py) (the global key listener)
+ the `_remote_should_handle` / `_remote_key_action` pair in `main.py` (the
gate + dispatcher, right after the `/api/vlc/seek/to` route).

## How these remotes work

Air-mouse remotes (Air Fly Mouse, MX3, W1/W2, Rii i8, G7 and the countless
rebrands) pair a 2.4 GHz USB dongle (or Bluetooth) with the host and enumerate
as a **standard HID keyboard + mouse** — no driver, no SDK. Their playback
buttons emit ordinary consumer media keys:

| Remote button | HID / Windows VK code | Mapped action |
|---|---|---|
| ⏯ Play/Pause | `VK_MEDIA_PLAY_PAUSE` (0xB3) | Toggle pause |
| Vol + / Vol − | `VK_VOLUME_UP` / `VK_VOLUME_DOWN` (0xAF/0xAE) | Volume ±`REMOTE_VOLUME_STEP` (5) per press; auto-repeats while held |
| ⏭ Next track | `VK_MEDIA_NEXT_TRACK` (0xB0) | Skip **forward** `REMOTE_SEEK_STEP_SECS` (10 s) |
| ⏮ Prev track | `VK_MEDIA_PREV_TRACK` (0xB1) | Skip **back** 10 s |

Anything else the remote sends (arrows, OK/Enter, Back, Home, mouse movement)
is deliberately **not** touched — intercepting those globally would break
normal keyboard/mouse use of the host. The next/prev *track* keys are mapped
to ±10 s seeks (not playlist next/prev) because that's what the buttons mean
on a streaming remote.

## Architecture

```
remote button ──HID──▶ host keyboard events
                          │  pynput global listener (remote_input.py, own thread)
                          │  gate: _remote_should_handle()  (sync, attr reads only)
                          ▼
        asyncio.run_coroutine_threadsafe → _remote_key_action(action)  (main loop)
                          │
            ┌─────────────┴──────────────┐
            ▼                            ▼
   state.youtube_active?          otherwise (VLC playback)
   youtube_control(...)           vlc("pl_pause") / volume() / vlc("seek")
   (playpause / volume_step       (volume capped by settings.max_volume,
    = host OS mixer / seek)        same paths as the dashboard footer)
```

- **Gate**: keys are only claimed while real playback is up —
  `state.youtube_active or state.stream_status in ("playing", "buffering")`.
  "playing" covers VLC-paused too (pause isn't a `stream_status`). When idle
  (incl. the idle background video) every key passes through to the OS
  untouched, so a real keyboard on the host behaves normally.
- **Dispatch** reuses the exact endpoint code paths, so the admin volume cap,
  the YouTube OS-mixer volume rule, and SSE state sync all apply; a `state`
  snapshot is broadcast immediately after each action so open dashboards
  reflect the remote press without waiting for the 2 s `stat_broadcaster`
  tick.
- **Debounce** (`_MIN_INTERVAL` in remote_input.py): play/pause and the seeks
  fire once per press (0.30–0.35 s) so key auto-repeat can't double-toggle;
  volume is allowed to repeat while held but throttled to ~10 Hz so VLC's
  HTTP interface isn't flooded.

## Platform behaviour (Windows first)

| Platform | Listener | Suppression |
|---|---|---|
| **Windows** | pynput low-level keyboard hook (`SetWindowsHookEx`) | **Yes, selective** — `win32_event_filter` + `suppress_event()` swallow only the five handled keys, and only while the gate is open. Required: without it the OS mixer would also change volume and a focused VLC would also toggle pause (net: double volume steps, play/pause cancelling itself out). Dispatch happens *inside* the filter because a suppressed event is never delivered to `on_press`. Both key-down and key-up are swallowed so apps never see an orphan key-up. |
| **Linux (X11)** | pynput X listener (needs `DISPLAY`) | No selective suppression — the desktop environment may additionally act on volume keys (typically OS volume changes alongside VLC volume). Wayland is not supported by pynput's X backend. |
| **macOS** | pynput CGEventTap | No suppression; requires the **Input Monitoring** permission (TCC) — without it the listener starts but receives nothing. Dev convenience only. |

Failure to start is always non-fatal: `start_listener` logs one warning
(missing pynput, no interactive desktop, no TCC permission) and returns
`None`; the dashboard runs without remote support. A **session-0 Windows
service receives no keyboard input** (hooks are per-desktop) — the installed
Task Scheduler service runs in the interactive user session, so this only
bites truly headless setups.

## Configuration

- `.env` → `REMOTE_CONTROL=0` disables the listener entirely (default on;
  `Settings.remote_control`).
- Step sizes are the `REMOTE_SEEK_STEP_SECS` / `REMOTE_VOLUME_STEP` constants
  in `main.py` (10 s / 5 %), chosen to match the dashboard footer's small-skip
  and volume-± buttons.

## Future

A Firestick-style TV web UI driven by the remote's arrow/OK keys is planned;
that will need the arrow/Enter/Back keys routed to the on-TV page (likely via
the existing SSE command relay, as YouTube-on-TV does) rather than global
interception. This doc is where that design should land.
