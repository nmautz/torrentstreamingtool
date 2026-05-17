# Changelog

## [2.1.3] — 2026-05-16
- **Bug fix (Windows):** `run.py --install` now registers the scheduled task to run as the *console* user (the one actually sitting at the keyboard), not the Admin account whose credentials were entered at the UAC prompt. Previously, installing from an elevated Admin shell while logged in as a regular user would tie the task to the Admin's logon, so the task never fired at the user's logon — the service appeared to install successfully but never started, with no logs written. Detection uses `WTSGetActiveConsoleSessionId` + `WTSQuerySessionInformationW` (works on all Windows editions), with a PowerShell `Win32_ComputerSystem.UserName` fallback. Install output now prints the detected `RunAs` user so it's clear which account the service is tied to.

## [2.1.2] — 2026-05-16
- **Bug fix:** System service (`run.py --install`) now registers mDNS, opens Windows firewall, and launches the HTTPS admin process — matching what `python run.py` does interactively. Previously the service only ran the watchdog + HTTP uvicorn, so `remote.local` did not resolve and admin/HTTPS was unavailable when launched via the installed service. The wrapper now reuses `run.py`'s `get_local_ip`/`setup_windows_firewall`/`start_mdns` helpers so the installed service and interactive launch take the same code path for these network bits. (Both `setup.py`'s install offer and `run.py --install` already delegate to `daemon.install()`, so the registered task itself is identical between them.)

## [2.1.1] — 2026-05-16
- **Bug fix:** qBittorrent no longer launches on startup when Mullvad VPN is off. `run.py` now checks VPN status before starting qBit; if VPN is down, qBit is skipped and the watchdog will start it once VPN connects.

## [2.1.0] — 2026-05-16
- **Minor feature:** Fullscreen vol − / + buttons now detect hold-down (≥400 ms) and apply a ±15 step instead of ±5; short taps retain the original ±5 behaviour. Repeats every 350 ms while held.

## [2.0.0] — baseline
- Initial versioned release (previously tracked as alpha1/alpha2).
