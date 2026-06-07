# AirPlay screen-mirror receiver (iPhone → host TV)

Mirror **anything on an iPhone** (TikTok, Safari, any app — not just StreamLink
content) onto the TV wired to the **Windows host**, using iOS's native **Screen
Mirroring**. This is general AirPlay screen mirroring, distinct from the
host-VLC "On TV (VLC)" / "To TV" paths (which only play StreamLink library
content) and from the on-device player.

> **Windows-only, opt-in.** macOS has a native AirPlay Receiver and Linux can use
> UxPlay/Avahi; neither is wired up here because the host (the box at the TV) is
> Windows-first. Off Windows this whole subsystem is a no-op.

---

## When to read this doc

Changing any of:

- `setup.py` — `install_airplay_receiver`, `uxplay_win_candidates`,
  `_resolve_airplay_win_installer_url`, `_bonjour_service_present`, the
  `_UXPLAY_WIN` `.env` key.
- `main.py` admin **Optional Components** pipeline — the `airplay` branch of
  `_run_component_install` + entry in `_component_status_payload` + `_COMPONENT_KEYS`
  (this is the autoupdater-safe install path), and `_airplay_installed`.
- `run.py` — `start_airplay`, `find_airplay`, the `AIRPLAY_RECEIVER` gate.
- `main.py` — `airplay_mirror_watch`, `_find_airplay_hwnds_windows`,
  `_focus_airplay_windows`, `_airplay_receiver_enabled`, `state.airplay_active` /
  `state.airplay_available`, and the `airplay_active` guards in
  `vlc_focus_and_fullscreen` / `background_video_loop`.
- `static/index.html` — `#airplayHint`, `_renderAirplayHint`.

---

## Why a third-party receiver

iOS Screen Mirroring only lists a target that **advertises AirPlay over
Bonjour/mDNS** on the LAN. A website (the dashboard) cannot inject itself into
that list, and Windows is not an AirPlay receiver out of the box — which is
exactly why "it's not in the Screen Mirroring window" until receiver software is
installed. We use the prebuilt **[leapbtw/uxplay-windows](https://github.com/leapbtw/uxplay-windows)**
build of UxPlay, which bundles:

- **GStreamer** — decodes/renders the mirrored H.264 video + audio.
- **Apple Bonjour** (`Bonjour Service`) — the mDNS responder that makes the host
  discoverable on the iPhone.

The maintained UxPlay (FDH2) has **no prebuilt Windows binary** (MSYS2/MinGW
build only), so the turnkey package is the pragmatic choice. StreamLink does
**not** redistribute it — `setup.py` downloads it from upstream at install time.

---

## How it fits together

```
iPhone  ──Bonjour discovery──▶  uxplay-windows (tray app, on the Windows host)
        ──AirPlay mirror────▶   GStreamer window on the host's TV display
                                        ▲
StreamLink (main.py) ── airplay_mirror_watch ─┘ detects the mirror window,
                        minimizes VLC, sets state.airplay_active so VLC's focus
                        loop + the idle background video stand down.
```

The receiver runs as its **own tray app** and owns start/stop of the actual
mirror. StreamLink's only runtime job is to **mediate the screen** so the mirror
and VLC don't fight over the TV.

### 1. Install — two paths

**Preferred / autoupdater-safe: the admin panel.** Admin → **Optional Components**
lists **AirPlay receiver** (Windows only) next to ffmpeg/whisper. Click **Get
installer** → the server downloads the uxplay-windows installer to `tools/airplay/`
(`_run_component_install`'s `airplay` branch, reusing `_download_to` +
`setup._resolve_airplay_win_installer_url`), pre-enables `AIRPLAY_RECEIVER=1`, and
best-effort launches the wizard. Finish the wizard **on the host's TV desktop**
(allow the firewall), then click **Refresh** — the row flips to **Installed** once
the binary is detected. This is the path that **works on auto-updating hosts**:
`setup.py` skips every `install_*` step under `STREAMLINK_AUTOUPDATE=1`, so the
interactive installer below never runs there.

**Interactive: `setup.py` (manual runs only).** `install_airplay_receiver` offers
(default **No** — large GStreamer download) the same download-and-run flow during a
manual `python setup.py`, records `_UXPLAY_WIN`, and `_bonjour_service_present`
(`sc query "Bonjour Service"`) warns if Bonjour didn't register. Skipped under
auto-update.

Either way the receiver path persists across auto-updates: `merge_tool_paths` keeps
`_UXPLAY_WIN` and the reused `.env` keeps `AIRPLAY_RECEIVER`.

### 2. Launch (`run.py`, Windows)

`start_airplay` runs only when **`AIRPLAY_RECEIVER=1`** is set in `.env` (so hosts
that don't want it pay nothing). It `net start "Bonjour Service"` (idempotent),
then launches the tray app via `launch_bg` if it isn't already running. Strictly
best-effort — like Jackett, it never blocks or fails startup.

### 3. Screen mediation (`main.py`)

`state.airplay_available` is **detection-based** (`_airplay_installed`: Windows +
the receiver binary present on disk, via `_UXPLAY_WIN` or `uxplay_win_candidates`)
— *not* the `AIRPLAY_RECEIVER` env toggle. It's set at lifespan start, surfaced in
`state_snapshot` to drive the dashboard hint, and **re-checked by the watcher every
~15 s** so an admin-panel install goes live without a server restart.
`AIRPLAY_RECEIVER` only gates `run.py`'s auto-launch.

`airplay_mirror_watch` (a lifespan task, Windows-only) runs continuously; it
refreshes `state.airplay_available` periodically (broadcasting on change) and, while
available, polls every 2 s for a live mirror window via
`_find_airplay_hwnds_windows` — visible top-level windows whose owning **process
name** or **title** matches the (env-overridable) hints and that are at least
320×240 (to skip tray/helper popups). A mirror window only exists while an iPhone
is actively mirroring, so its presence *is* the signal.

- **Rising edge** → `state.airplay_active = True`, `vlc_minimize()`,
  `_focus_airplay_windows()` (the same Windows focus cocktail as the YouTube
  kiosk), broadcast `state`.
- **While active** → keep reinforcing focus so a late-launching shell window can't
  bury the mirror.
- **Falling edge** → `state.airplay_active = False`, broadcast, and
  `vlc_focus_and_fullscreen()` to reclaim the TV.

`state.airplay_active` gates `vlc_focus_and_fullscreen` and
`background_video_loop` (same shape as `youtube_active`), so VLC's focus loop and
the idle background video stand down while a mirror is on screen.

### 4. Dashboard hint (`static/index.html`)

`#airplayHint` (Search tab) is shown only when `airplay_available`. It explains
how to mirror ("Control Center → Screen Mirroring → this PC") and flips to a live
**"Mirroring now"** state when `airplay_active`. `_renderAirplayHint(s)` is called
from the SSE `state` handler. There is **no start/stop button** — mirroring is
initiated from iOS, and start/stop of the receiver itself belongs to its tray app.

---

## Configuration (`.env`)

| Key | Meaning |
|-----|---------|
| `AIRPLAY_RECEIVER` | `1`/`true` to have `run.py` start the receiver + Bonjour. Default off. |
| `_UXPLAY_WIN` | Auto-detected path to the receiver exe (written by `setup.py`). |
| `AIRPLAY_PROC_HINTS` | Optional CSV overriding the process-name match for mirror-window detection (default `uxplay,gst-launch,gstreamer`). |
| `AIRPLAY_WINDOW_HINTS` | Optional CSV overriding the window-title match (default `uxplay,airplay,screen mirror`). |

The two `*_HINTS` overrides exist because the exact uxplay-windows window
title/process name must be confirmed on Windows — if VLC doesn't yield when a
mirror connects, set these to whatever the receiver's window actually reports
(see Troubleshooting) instead of changing code.

---

## Troubleshooting

- **Host not listed in iPhone Screen Mirroring** — the #1 failure. Check, in order:
  - `sc query "Bonjour Service"` shows `RUNNING` (start with `net start "Bonjour Service"`).
  - The Windows Firewall allows the receiver **and** mDNS (UDP 5353) + the AirPlay ports.
  - The iPhone and PC are on the **same subnet** — guest Wi-Fi / AP "client
    isolation" / separate 2.4 vs 5 GHz VLANs block mDNS.
- **Listed but won't connect / no picture** — GStreamer codec issue or the
  receiver wasn't allowed through the firewall; re-run the installer.
- **Mirror plays but VLC doesn't get out of the way** — the mirror-window
  detection hints don't match this build. Find the receiver's window/process name
  (Task Manager → Details) and set `AIRPLAY_PROC_HINTS` / `AIRPLAY_WINDOW_HINTS`
  in `.env`.
- **Installer flagged by Windows Defender SmartScreen** — the uxplay-windows
  build is unsigned; allow it through to install.

---

## Limitations / decisions

- **Windows host only** (see top).
- **No watchdog supervision.** The receiver is a self-managing GUI tray app with
  no health port; policing it with `watchdog.py` (which is port/health-check
  oriented) risks relaunching one the user intentionally closed. It autostarts via
  its own setting + `run.py` launch; if it crashes mid-session, relaunch from its
  tray icon or restart `run.py`.
- **No in-app start/stop.** Mirroring is an iOS Control Center action; the receiver
  owns its own lifecycle. StreamLink only shows status + mediates the screen.
- **GPL.** uxplay-windows is GPL; StreamLink downloads it from upstream at setup
  and does not redistribute it.
- **Cannot be verified from the macOS dev box** — needs a real Windows host +
  iPhone + Apple TV-class display (see PLAN.md verification steps).

---

## See also

- [ARCHITECTURE.md](ARCHITECTURE.md) — service topology
- [RUNTIME.md](RUNTIME.md) — `run.py` launchers
- [SETUP.md](SETUP.md) — first-time install
- [GOTCHAS.md](GOTCHAS.md) — Bonjour/firewall/subnet footguns, screen contention
- [YOUTUBE.md](YOUTUBE.md) — the analogous "browser kiosk owns the TV, VLC yields"
  pattern this reuses
