"""Global media-key listener for HID wireless remotes (air-mouse remotes).

Air-mouse remotes ("Air Fly Mouse", MX3, W1/W2, Rii i8, G7 and the many
rebrands) pair a 2.4 GHz USB dongle (or Bluetooth) with the host and present
themselves as a standard HID keyboard+mouse. Their playback buttons emit
ordinary *consumer media keys* — play/pause, volume up/down, previous/next
track — so "remote support" is a global media-key hook on the host that routes
those presses into the dashboard's own control paths (main.py's
`_remote_key_action`), which keeps the admin volume cap, YouTube-on-TV routing
and SSE state sync all working exactly as if the button had been a dashboard
tap.

Platform notes (Windows first — see docs/REMOTE.md):

- **Windows**: the pynput low-level keyboard hook both observes *and
  suppresses* the handled keys (`win32_event_filter` + `suppress_event()`),
  so the OS mixer and a focused VLC don't also act on them — without
  suppression volume changes twice and play/pause double-toggles back.
  Dispatch happens inside the filter because a suppressed event is never
  delivered to `on_press`.
- **Linux (X11)** / **macOS**: pynput can't suppress selectively, so keys are
  observed only; the desktop environment may additionally act on volume keys.
  macOS also requires the Input Monitoring permission (TCC) — without it the
  listener starts but receives nothing. Both gaps are acceptable per the
  Windows-first platform policy.

Keys are only claimed while `should_handle()` says real playback is active;
when idle they pass through to the OS untouched, so a keyboard plugged into
the host behaves normally.

The listener needs an interactive desktop session — a session-0 Windows
service receives no keyboard input (hooks are per-desktop). Failure to start
is non-fatal: `start_listener` logs once and returns None, and the dashboard
runs without remote support.
"""
from __future__ import annotations

import asyncio
import logging
import platform
import time
from typing import Any, Awaitable, Callable, Optional

log = logging.getLogger("streamlink.remote")

# Windows virtual-key codes for the consumer media keys the remotes send.
_VK_ACTIONS = {
    0xB3: "playpause",     # VK_MEDIA_PLAY_PAUSE
    0xAF: "volume_up",     # VK_VOLUME_UP
    0xAE: "volume_down",   # VK_VOLUME_DOWN
    0xB0: "seek_forward",  # VK_MEDIA_NEXT_TRACK  (remote ⏭ → skip forward)
    0xB1: "seek_back",     # VK_MEDIA_PREV_TRACK  (remote ⏮ → skip back)
}

_WM_KEYDOWN = (0x0100, 0x0104)   # WM_KEYDOWN, WM_SYSKEYDOWN
_WM_KEYUP   = (0x0101, 0x0105)   # WM_KEYUP,   WM_SYSKEYUP

# Per-action debounce (seconds) — the remotes auto-repeat while a button is
# held. Volume is allowed to repeat (held button = continuous ramp) but is
# throttled so VLC's HTTP interface isn't flooded; play/pause and the seeks
# must fire once per press or a slightly-long press toggles right back.
_MIN_INTERVAL = {
    "playpause":    0.35,
    "seek_forward": 0.30,
    "seek_back":    0.30,
    "volume_up":    0.10,
    "volume_down":  0.10,
}


class RemoteListener:
    """Wraps a pynput keyboard listener; created via :func:`start_listener`."""

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        dispatch: Callable[[str], Awaitable[None]],
        should_handle: Callable[[], bool],
    ) -> None:
        self._loop = loop
        self._dispatch = dispatch
        self._should = should_handle
        self._last: dict[str, float] = {}
        self._listener: Any = None

    # -- called from the pynput listener/hook thread --------------------------

    def _fire(self, action: str) -> None:
        now = time.monotonic()
        if now - self._last.get(action, 0.0) < _MIN_INTERVAL[action]:
            return
        self._last[action] = now
        asyncio.run_coroutine_threadsafe(self._dispatch(action), self._loop)

    def _win32_event_filter(self, msg: int, data: Any) -> bool:
        action = _VK_ACTIONS.get(data.vkCode)
        if action is None or not self._should():
            return True   # not ours — deliver normally everywhere
        if msg in _WM_KEYDOWN:
            self._fire(action)
        # Swallow the down AND the matching up so neither the OS mixer nor a
        # focused VLC also acts on the key. suppress_event() raises to abort
        # the hook chain, so it must be the last statement.
        self._listener.suppress_event()
        return True   # unreachable; keeps the type checker happy

    def _on_press(self, key: Any) -> None:
        # Non-Windows path only — on Windows the filter already dispatched
        # (and a suppressed event never reaches this callback anyway).
        action = self._key_actions.get(key)
        if action is not None and self._should():
            self._fire(action)

    # -- lifecycle -------------------------------------------------------------

    def _start(self) -> None:
        from pynput import keyboard   # may raise (missing dep, no X display)

        self._key_actions = {
            keyboard.Key.media_play_pause:  "playpause",
            keyboard.Key.media_volume_up:   "volume_up",
            keyboard.Key.media_volume_down: "volume_down",
            keyboard.Key.media_next:        "seek_forward",
            keyboard.Key.media_previous:    "seek_back",
        }
        if platform.system() == "Windows":
            self._listener = keyboard.Listener(
                win32_event_filter=self._win32_event_filter)
        else:
            self._listener = keyboard.Listener(on_press=self._on_press)
        self._listener.start()
        self._listener.wait()   # raises if the low-level hook failed to install

    def stop(self) -> None:
        try:
            if self._listener is not None:
                self._listener.stop()
        except Exception:
            pass


def start_listener(
    loop: asyncio.AbstractEventLoop,
    dispatch: Callable[[str], Awaitable[None]],
    should_handle: Callable[[], bool],
) -> Optional[RemoteListener]:
    """Start the global media-key listener. Returns None (after logging why)
    when pynput is unavailable or the hook can't be installed — the dashboard
    then simply runs without HID-remote support."""
    listener = RemoteListener(loop, dispatch, should_handle)
    try:
        listener._start()
    except Exception as e:
        log.warning(
            "HID remote support disabled — media-key listener failed to start "
            "(%s). Usually: pynput not installed, no interactive desktop "
            "session, or missing macOS Input Monitoring permission.", e)
        return None
    log.info("HID remote media-key listener active (%s)", platform.system())
    return listener
