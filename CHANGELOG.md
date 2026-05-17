# Changelog

## [2.1.1] — 2026-05-16
- **Bug fix:** qBittorrent no longer launches on startup when Mullvad VPN is off. `run.py` now checks VPN status before starting qBit; if VPN is down, qBit is skipped and the watchdog will start it once VPN connects.

## [2.1.0] — 2026-05-16
- **Minor feature:** Fullscreen vol − / + buttons now detect hold-down (≥400 ms) and apply a ±15 step instead of ±5; short taps retain the original ±5 behaviour. Repeats every 350 ms while held.

## [2.0.0] — baseline
- Initial versioned release (previously tracked as alpha1/alpha2).
