"""Global input listener for HID wireless remotes (air-mouse remotes).

Air-mouse remotes ("Air Fly Mouse", MX3, W1/W2, Rii i8, G7 and the many
rebrands) pair a 2.4 GHz USB dongle (or Bluetooth) with the host and present
themselves as a standard HID keyboard+mouse. This module hooks the host's
input globally and feeds two things back into main.py:

1. **Media/Home key dispatch** — the remote's playback buttons emit ordinary
   consumer media keys (play/pause, volume up/down, previous/next track), and
   its Home button emits VK_BROWSER_HOME (which would otherwise launch the
   default browser). Handled presses are routed into the dashboard's own
   control paths (`_remote_key_action`), which keeps the admin volume cap,
   YouTube-on-TV routing and SSE state sync working exactly as if the button
   had been a dashboard tap.
2. **Generic input activity** (`on_input`) — *every* key press, mouse click
   and (throttled) mouse move is reported so main.py can drive the TV UI:
   wake the fullscreen dashboard kiosk on any button when the box is idle,
   and hand the screen back to the idle background video after a period of
   no input. See docs/REMOTE.md.
3. **⏻ power button** (`PowerButtonListener`, Windows) — most remotes emit
   power as a HID System Control usage that bypasses the keyboard stack
   entirely (the box locks + sleeps); a Raw Input window observes it while
   main.py disables the OS sleep/power-button actions. See the class docstring.

Platform notes (Windows first — see docs/REMOTE.md):

- **Windows**: the pynput low-level keyboard hook both observes *and
  suppresses* the handled keys (`win32_event_filter` + `suppress_event()`),
  so the OS mixer / a focused VLC / the default browser (Home) don't also act
  on them — without suppression volume changes twice, play/pause
  double-toggles back, and Home opens Edge. Dispatch happens inside the
  filter because a suppressed event is never delivered to `on_press`.
- **Linux (X11)** / **macOS**: pynput can't suppress selectively, so keys are
  observed only; the desktop environment may additionally act on volume keys,
  and the Home button isn't exposed by pynput's Key enum (no "home" action
  off-Windows). macOS also requires the Input Monitoring permission (TCC) —
  without it the listeners start but receive nothing. Both gaps are
  acceptable per the Windows-first platform policy.

Media keys are only claimed while `should_handle(action)` says so; when not
claimed they pass through to the OS untouched, so a keyboard plugged into the
host behaves normally.

The listeners need an interactive desktop session — a session-0 Windows
service receives no input (hooks are per-desktop). Failure to start is
non-fatal: `start_listener` logs once and returns None, and the dashboard
runs without remote support. The mouse listener is best-effort on top of the
keyboard one — its failure never takes the keyboard hook down.
"""
from __future__ import annotations

import asyncio
import logging
import platform
import threading
import time
from typing import Any, Awaitable, Callable, Optional

log = logging.getLogger("streamlink.remote")

# Windows virtual-key codes for the consumer keys the remotes send.
_VK_ACTIONS = {
    0xB3: "playpause",     # VK_MEDIA_PLAY_PAUSE
    0xAF: "volume_up",     # VK_VOLUME_UP
    0xAE: "volume_down",   # VK_VOLUME_DOWN
    0xB0: "seek_forward",  # VK_MEDIA_NEXT_TRACK  (remote ⏭ → skip forward)
    0xB1: "seek_back",     # VK_MEDIA_PREV_TRACK  (remote ⏮ → skip back)
    0xAC: "home",          # VK_BROWSER_HOME      (remote 🏠 → stop + TV UI;
                           #   unsuppressed it launches the default browser)
    0xA6: "back",          # VK_BROWSER_BACK      (remote ← Back → exit playback
                           #   to the TV UI / step back inside it; unsuppressed
                           #   the kiosk Chrome would treat it as history-back)
    0x0D: "ok",            # VK_RETURN            (remote OK/Enter → ⏯ during
                           #   playback; unclaimed while the TV UI is up so it
                           #   keeps activating the focused element)
    0x5F: "power",         # VK_SLEEP             (remote ⏻ power → toggle
                           #   background video ⇄ TV UI; unsuppressed Windows
                           #   sleeps/hibernates the box)
}

_WM_KEYDOWN = (0x0100, 0x0104)   # WM_KEYDOWN, WM_SYSKEYDOWN
_WM_KEYUP   = (0x0101, 0x0105)   # WM_KEYUP,   WM_SYSKEYUP

# KBDLLHOOKSTRUCT.flags bit: the event was synthesized via SendInput /
# keybd_event, not typed on hardware. Injected events never count as generic
# ACTIVITY — main.py's Windows focus cocktail (`_vlc_focus_windows` /
# `_focus_tv_browser_windows`) fires a synthetic ALT press to defeat
# foreground-lock, and counted as activity that ALT re-wakes the TV UI ~1.5 s
# after every idle hand-back. But injected events are NOT skipped for the
# handled action keys: Windows' HID input service translates consumer-page
# usages (AC Home / AC Back, sleep, media) into VK keystrokes *via SendInput*,
# so on many remotes the very keys this module exists for arrive with this
# flag set — blanket-skipping every injected event let 🏠 Home fall through to
# the default browser and made ← Back / ⏻ (VK_SLEEP form) dead on those
# remotes.
_LLKHF_INJECTED = 0x10

# Per-action debounce (seconds) — the remotes auto-repeat while a button is
# held. Volume is allowed to repeat (held button = continuous ramp) but is
# throttled so VLC's HTTP interface isn't flooded; play/pause and the seeks
# must fire once per press or a slightly-long press toggles right back; Home
# triggers a full stop + kiosk wake, so it gets a long guard.
_MIN_INTERVAL = {
    "playpause":    0.35,
    "ok":           0.35,   # same semantics as playpause while claimed
    "seek_forward": 0.30,
    "seek_back":    0.30,
    "volume_up":    0.10,
    "volume_down":  0.10,
    "home":         1.00,
    "back":         0.40,   # once per press — a held Back must not machine-gun
                            # stop() or blow through nested modals
    "power":        1.00,   # surface toggle (stop / kiosk / bg video) — a held
                            # press must not flap between the surfaces
}

# Throttle for the generic activity callback, per kind. Mouse moves arrive per
# pixel on Windows (low-level hook) — they only need to refresh the TV UI idle
# timer, so once a second is plenty. Keys/clicks are what wake the UI.
_ACTIVITY_MIN_INTERVAL = {
    "key":   0.25,
    "click": 0.25,
    "move":  1.00,
    "media": 0.50,
}


class RemoteListener:
    """Wraps the pynput keyboard (+ best-effort mouse) listeners; created via
    :func:`start_listener`."""

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        dispatch: Callable[[str], Awaitable[None]],
        should_handle: Callable[[str], bool],
        on_input: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._loop = loop
        self._dispatch = dispatch
        self._should = should_handle
        self._on_input = on_input
        self._last: dict[str, float] = {}
        self._act_last: dict[str, float] = {}
        self._listener: Any = None        # keyboard
        self._mouse_listener: Any = None  # mouse (optional)

    # -- called from the pynput listener/hook threads --------------------------

    def _fire(self, action: str) -> None:
        now = time.monotonic()
        if now - self._last.get(action, 0.0) < _MIN_INTERVAL[action]:
            return
        self._last[action] = now
        asyncio.run_coroutine_threadsafe(self._dispatch(action), self._loop)

    def _activity(self, kind: str) -> None:
        """Report generic input (throttled). Never raises — runs on hook
        threads where an exception would kill the listener."""
        if self._on_input is None:
            return
        now = time.monotonic()
        if now - self._act_last.get(kind, 0.0) < _ACTIVITY_MIN_INTERVAL[kind]:
            return
        self._act_last[kind] = now
        try:
            self._on_input(kind)
        except Exception:
            pass

    def _win32_event_filter(self, msg: int, data: Any) -> bool:
        action = _VK_ACTIONS.get(data.vkCode)
        if action is None or not self._should(action):
            # Not ours — deliver normally everywhere, but count a keydown as
            # activity so any button can wake the TV UI / refresh its timer.
            # Injected (software-synthesized) keys never count as activity:
            # the focus cocktail's own synthetic ALT would re-wake the UI it
            # just handed the screen back from. See _LLKHF_INJECTED above.
            if msg in _WM_KEYDOWN and not (data.flags & _LLKHF_INJECTED):
                self._activity("key")
            return True
        if msg in _WM_KEYDOWN:
            self._activity("media")   # refreshes the idle timer, never wakes
            self._fire(action)
        # Swallow the down AND the matching up so neither the OS mixer, a
        # focused VLC, nor the default browser (Home) also acts on the key.
        # suppress_event() raises to abort the hook chain, so it must be the
        # last statement.
        self._listener.suppress_event()
        return True   # unreachable; keeps the type checker happy

    def _on_press(self, key: Any) -> None:
        # Non-Windows path only — on Windows the filter already dispatched
        # (and a suppressed event never reaches this callback anyway).
        action = self._key_actions.get(key)
        if action is not None and self._should(action):
            self._activity("media")
            self._fire(action)
        else:
            self._activity("key")

    def _on_click(self, x: float, y: float, button: Any, pressed: bool) -> None:
        if pressed:
            self._activity("click")

    def _on_scroll(self, x: float, y: float, dx: float, dy: float) -> None:
        self._activity("click")   # deliberate input — treat like a click

    def _on_move(self, x: float, y: float) -> None:
        self._activity("move")

    # -- lifecycle -------------------------------------------------------------

    def _start(self) -> None:
        from pynput import keyboard   # may raise (missing dep, no X display)

        self._key_actions = {
            keyboard.Key.media_play_pause:  "playpause",
            keyboard.Key.media_volume_up:   "volume_up",
            keyboard.Key.media_volume_down: "volume_down",
            keyboard.Key.media_next:        "seek_forward",
            keyboard.Key.media_previous:    "seek_back",
            keyboard.Key.enter:             "ok",
            # No "home"/"back"/"power": pynput's Key enum has no browser or
            # sleep keys off-Windows.
        }
        if platform.system() == "Windows":
            self._listener = keyboard.Listener(
                win32_event_filter=self._win32_event_filter)
        else:
            self._listener = keyboard.Listener(on_press=self._on_press)
        self._listener.start()
        self._listener.wait()   # raises if the low-level hook failed to install

        # Mouse listener: only feeds the activity callback (wake/idle-timer),
        # so it's best-effort — never let its failure take the keyboard
        # listener down with it.
        if self._on_input is not None:
            try:
                from pynput import mouse
                self._mouse_listener = mouse.Listener(
                    on_move=self._on_move,
                    on_click=self._on_click,
                    on_scroll=self._on_scroll,
                )
                self._mouse_listener.start()
                self._mouse_listener.wait()
            except Exception as e:
                self._mouse_listener = None
                log.warning("HID remote: mouse listener unavailable (%s) — "
                            "keys still work; pointer motion won't wake the TV UI.", e)

    def stop(self) -> None:
        for l in (self._listener, self._mouse_listener):
            try:
                if l is not None:
                    l.stop()
            except Exception:
                pass


class PowerButtonListener:
    """Windows-only Raw Input listener for the remote's ⏻ power button.

    Air-mouse power buttons usually never enter the keyboard stack: they emit
    a HID **System Control** usage (Generic Desktop page 0x01, usage 0x80
    collection — System Sleep / Power Down), which the HID class driver hands
    straight to the Windows power manager. A low-level keyboard hook neither
    sees nor suppresses it — the box locks and sleeps. Interception is
    therefore split in two:

    - main.py's `_neuter_power_buttons()` sets the power plan's sleep-button
      AND power-button actions to "Do nothing" (`powercfg … SBUTTONACTION /
      PBUTTONACTION 0` — Windows maps System Sleep 0x82 to the former but
      System Power Down 0x81 to the latter), so the press no longer suspends
      the box;
    - this listener registers a hidden window for Raw Input from the System
      Control usage page (`RIDEV_INPUTSINK` — delivered regardless of focus)
      and dispatches "power" on each press. Raw Input is observation-only (it
      can't block the power manager) — that's what the powercfg step is for.

    Button-release reports (all-zero past the report ID) are skipped, and
    main.py's power branch debounces cross-path, so a remote that emits BOTH
    the VK_SLEEP keyboard key and the System Control usage fires once.
    """

    _WM_INPUT        = 0x00FF
    _WM_QUIT         = 0x0012
    _RIDEV_INPUTSINK = 0x0100
    _RID_INPUT       = 0x10000003
    _RIM_TYPEHID     = 2

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        dispatch: Callable[[str], Awaitable[None]],
        should_handle: Callable[[str], bool],
    ) -> None:
        self._loop = loop
        self._dispatch = dispatch
        self._should = should_handle
        self._tid: Optional[int] = None            # message-pump thread id (for WM_QUIT)
        self._thread: Optional[threading.Thread] = None
        self._started = threading.Event()
        self._error: Optional[BaseException] = None

    def _fire(self) -> None:
        try:
            if not self._should("power"):
                return
            asyncio.run_coroutine_threadsafe(self._dispatch("power"), self._loop)
        except Exception:
            pass

    def _thread_main(self) -> None:
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32

            LRESULT = ctypes.c_ssize_t
            WNDPROC = ctypes.WINFUNCTYPE(
                LRESULT, wintypes.HWND, wintypes.UINT,
                wintypes.WPARAM, wintypes.LPARAM)
            user32.DefWindowProcW.restype = LRESULT
            user32.DefWindowProcW.argtypes = (
                wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
            user32.CreateWindowExW.restype = wintypes.HWND
            user32.GetRawInputData.restype = wintypes.UINT
            user32.GetRawInputData.argtypes = (
                wintypes.HANDLE, wintypes.UINT, ctypes.c_void_p,
                ctypes.POINTER(wintypes.UINT), wintypes.UINT)

            class WNDCLASSW(ctypes.Structure):
                _fields_ = [
                    ("style",         wintypes.UINT),
                    ("lpfnWndProc",   WNDPROC),
                    ("cbClsExtra",    ctypes.c_int),
                    ("cbWndExtra",    ctypes.c_int),
                    ("hInstance",     wintypes.HINSTANCE),
                    ("hIcon",         wintypes.HANDLE),
                    ("hCursor",       wintypes.HANDLE),
                    ("hbrBackground", wintypes.HANDLE),
                    ("lpszMenuName",  wintypes.LPCWSTR),
                    ("lpszClassName", wintypes.LPCWSTR),
                ]

            class RAWINPUTDEVICE(ctypes.Structure):
                _fields_ = [
                    ("usUsagePage", wintypes.USHORT),
                    ("usUsage",     wintypes.USHORT),
                    ("dwFlags",     wintypes.DWORD),
                    ("hwndTarget",  wintypes.HWND),
                ]

            class RAWINPUTHEADER(ctypes.Structure):
                _fields_ = [
                    ("dwType",  wintypes.DWORD),
                    ("dwSize",  wintypes.DWORD),
                    ("hDevice", wintypes.HANDLE),
                    ("wParam",  wintypes.WPARAM),
                ]

            header_sz = ctypes.sizeof(RAWINPUTHEADER)

            def handle_input(lparam: int) -> None:
                hraw = wintypes.HANDLE(lparam)
                size = wintypes.UINT(0)
                user32.GetRawInputData(
                    hraw, self._RID_INPUT, None, ctypes.byref(size), header_sz)
                if not size.value:
                    return
                buf = (ctypes.c_ubyte * size.value)()
                if user32.GetRawInputData(
                        hraw, self._RID_INPUT, buf,
                        ctypes.byref(size), header_sz) != size.value:
                    return
                hdr = ctypes.cast(buf, ctypes.POINTER(RAWINPUTHEADER)).contents
                if hdr.dwType != self._RIM_TYPEHID:
                    return
                # Past the header: RAWHID = dwSizeHid (DWORD) + dwCount (DWORD)
                # + bRawData[dwSizeHid * dwCount].
                sz_hid = int.from_bytes(bytes(buf[header_sz:header_sz + 4]), "little")
                count  = int.from_bytes(bytes(buf[header_sz + 4:header_sz + 8]), "little")
                data_off = header_sz + 8
                payload = bytes(buf[data_off:data_off + sz_hid * count])
                # A button-release report is all zeros past the report ID —
                # only the press dispatches (a release must not re-toggle).
                pressed = any(payload[1:]) if len(payload) > 1 else any(payload)
                if pressed:
                    self._fire()

            @WNDPROC
            def wndproc(hwnd: Any, msg: int, wparam: int, lparam: int) -> int:
                if msg == self._WM_INPUT:
                    try:
                        handle_input(lparam)
                    except Exception:
                        pass
                    return 0
                return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

            hinst = kernel32.GetModuleHandleW(None)
            wc = WNDCLASSW()
            wc.lpfnWndProc = wndproc
            wc.hInstance = hinst
            wc.lpszClassName = "StreamLinkPowerButtonSink"
            if not user32.RegisterClassW(ctypes.byref(wc)):
                raise ctypes.WinError()
            hwnd = user32.CreateWindowExW(
                0, wc.lpszClassName, wc.lpszClassName,
                0, 0, 0, 0, 0, None, None, hinst, None)
            if not hwnd:
                raise ctypes.WinError()
            rid = RAWINPUTDEVICE(0x01, 0x80, self._RIDEV_INPUTSINK, hwnd)
            if not user32.RegisterRawInputDevices(
                    ctypes.byref(rid), 1, ctypes.sizeof(RAWINPUTDEVICE)):
                raise ctypes.WinError()

            self._tid = kernel32.GetCurrentThreadId()
            self._started.set()

            msg = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            # `wndproc` (and the ctypes callback keeping it alive) lives in
            # this frame — the pump exiting is what ends its lifetime.
        except BaseException as e:   # report registration failures to start()
            self._error = e
            self._started.set()

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._thread_main, name="power-button-rawinput", daemon=True)
        self._thread.start()
        self._started.wait(timeout=5.0)
        if self._error is not None:
            raise self._error
        if self._tid is None:
            raise RuntimeError("raw-input thread did not become ready")

    def stop(self) -> None:
        if self._tid is None:
            return
        try:
            import ctypes
            ctypes.windll.user32.PostThreadMessageW(self._tid, self._WM_QUIT, 0, 0)
        except Exception:
            pass


def start_power_listener(
    loop: asyncio.AbstractEventLoop,
    dispatch: Callable[[str], Awaitable[None]],
    should_handle: Callable[[str], bool],
) -> Optional[PowerButtonListener]:
    """Start the Raw Input listener for the remote's ⏻ power button.
    Windows-only; returns None (after logging why) elsewhere or when window /
    raw-input registration fails — power handling then relies on the keyboard
    hook's VK_SLEEP mapping alone."""
    if platform.system() != "Windows":
        return None
    listener = PowerButtonListener(loop, dispatch, should_handle)
    try:
        listener.start()
    except Exception as e:
        log.warning("power-button interception unavailable — raw-input "
                    "listener failed to start (%s)", e)
        return None
    log.info("power-button raw-input listener active (System Control usage page)")
    return listener


def start_listener(
    loop: asyncio.AbstractEventLoop,
    dispatch: Callable[[str], Awaitable[None]],
    should_handle: Callable[[str], bool],
    on_input: Optional[Callable[[str], None]] = None,
) -> Optional[RemoteListener]:
    """Start the global input listeners. Returns None (after logging why)
    when pynput is unavailable or the keyboard hook can't be installed — the
    dashboard then simply runs without HID-remote support."""
    listener = RemoteListener(loop, dispatch, should_handle, on_input)
    try:
        listener._start()
    except Exception as e:
        log.warning(
            "HID remote support disabled — input listener failed to start "
            "(%s). Usually: pynput not installed, no interactive desktop "
            "session, or missing macOS Input Monitoring permission.", e)
        return None
    log.info("HID remote input listener active (%s)", platform.system())
    return listener
