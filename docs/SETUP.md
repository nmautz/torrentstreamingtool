# Setup (`setup.py`)

1145 lines. Runs under the **system Python** (not the venv) — uses `from __future__ import annotations` for Python 3.9 compatibility. Idempotent: re-running it merges into existing config rather than overwriting.

## Entry point

`main()` ([setup.py:1092](../setup.py#L1092)):
1. `check_python()` — requires Python 3.9+
2. `setup_venv()` — creates `.venv`, upgrades pip, installs `requirements.txt`
3. `detect_tools()` — finds VLC / qBittorrent / Mullvad / Jackett / ffmpeg / fpcalc
4. `install_core_deps()` — install missing core apps (winget / brew cask / hint on Linux)
5. `install_jackett_service()` — Windows only: register `Jackett` as a Windows service (auto-elevates via UAC)
6. `install_smart_skip_deps()` — install ffmpeg + chromaprint (brew/apt/dnf/pacman/portable zip)
7. Either reuse existing `.env` (merge tool paths only) or prompt for full config
8. `configure_qbittorrent()` — write `qBittorrent.ini` directly
9. `write_env()` — save `.env` with both user config and `_VLC_BIN` / `_QBIT_BIN` / etc.
10. `ensure_download_dir()` — create the download folder
11. `generate_ssl_cert()` — create `cert.pem` + `key.pem` + `ca.pem` for HTTPS admin panel
12. `offer_service_install()` — delegate to `daemon.py` (defaults to yes on Windows)

## Tool detection

`find_exe(*candidates)` checks both literal paths and `shutil.which`. Each tool has its own candidates function (`vlc_candidates`, `qbit_candidates`, `jackett_candidates`, etc.). On Windows the Jackett search covers every install location winget or the official installer may use — `ProgramFiles`, `Program Files (x86)`, `LOCALAPPDATA`, `APPDATA`, `ProgramData` — under both `Jackett/` and `Programs/Jackett/`. Looks for `JackettTray.exe`, `JackettConsole.exe`, `jackett.exe` in that priority order.

## Core app install ([setup.py:481](../setup.py#L481))

- **Windows**: `winget install --id <id> -e --silent --accept-package-agreements --accept-source-agreements`. Treats winget exit codes `0x8A15002B` (already up to date) and `0x8A150061` (already installed) as success. Re-detects paths after install.
- **macOS**: `brew install --cask vlc qbittorrent jackett mullvad-vpn`. Requires Homebrew.
- **Linux**: Prints a hint. Desktop-app packaging varies too much to automate.

## Smart Skip deps ([setup.py:345](../setup.py#L345))

- **macOS**: `brew install ffmpeg chromaprint`
- **Linux**: detects apt/dnf/pacman, runs the matching install command with sudo
- **Windows**: skips winget entirely (flaky); downloads official portable zips for ffmpeg (gyan.dev essentials build) and fpcalc (chromaprint 1.5.1 release) into `./tools/`, extracts with zipfile/tarfile, walks the resulting tree to find the actual exe paths
- After install, calls `find_exe(*ffmpeg_candidates())` to refresh tool paths

## Jackett Windows service ([setup.py:593](../setup.py#L593))

Goal: register `Jackett` as a Windows service so it runs as LocalSystem from boot — same as the tray icon's "Start background service" menu but automated. Strategy:
1. Check `sc.exe query Jackett`; skip if already installed
2. Locate `JackettConsole.exe` / `JackettService.exe` via `_find_jackett_install_dir()`
3. If we're not admin, request elevation via `ShellExecuteW` `runas` and poll for the service to appear (up to ~15 s)
4. Otherwise run `JackettConsole.exe --Install`, falling back to `JackettService.exe --install`, falling back to `sc.exe create Jackett binPath=… start=auto`
5. `sc.exe start Jackett` (1056 = ALREADY_RUNNING is treated as success)
6. `grant_jackett_service_control()` — runs in **both** the already-installed and freshly-installed branches

### `grant_jackett_service_control()` ([setup.py:607](../setup.py#L607))

Lets the non-elevated StreamLink watchdog recover a hung Jackett **without a reboot**. A LocalSystem `Jackett` service can normally only be `sc stop`/`sc start`'d by an administrator, so a non-elevated watchdog can detect a wedged Jackett but not restart it — only a reboot clears it.

- Reads the service's SDDL via `sc.exe sdshow Jackett`, additively inserts `(A;;RPWP;;;AU)` (Authenticated Users → `SERVICE_START` + `SERVICE_STOP`) into the DACL (before the `S:` SACL), and applies it with `sc.exe sdset`.
- Also sets `sc.exe failure Jackett reset= 86400 actions= restart/5000/…` so SCM auto-restarts Jackett on a hard crash (manual stops, like the watchdog's, don't trigger failure actions).
- **Idempotent**: skips when the ACE is already present. **Best-effort**: never fatal to setup.
- When not admin, applies both in a single elevated `cmd /c … & …` (one UAC prompt). If elevation is declined, it prints the exact `sc sdset` / `sc failure` commands to run manually in an elevated PowerShell.
- No-op if Jackett runs as a tray/user process (the watchdog can kill+relaunch that directly). See [GOTCHAS.md](GOTCHAS.md#controlling-the-localsystem-jackett-service-needs-admin).

## qBittorrent ini ([setup.py:846](../setup.py#L846))

Parses the existing ini preserving section order and unknown keys; then injects/overwrites the keys we need:
- `Preferences\WebUI\Enabled=true`, `Port`, `Username`, `Password_ha1` (MD5 of `user:qBittorrent Web UI:pwd`), `LocalHostAuth=false`, `CSRFProtection=false`, `SessionTimeout=3600`
- `BitTorrent\Session\DefaultSavePath` = download folder

Path is platform-specific:
- macOS: `~/Library/Application Support/qBittorrent/qBittorrent.ini`
- Windows: `%APPDATA%/qBittorrent/qBittorrent.ini`
- Linux: `~/.config/qBittorrent/qBittorrent.ini`

User must restart qBittorrent for changes to take effect.

## SSL cert ([setup.py:912](../setup.py#L912))

Uses the `cryptography` package (in requirements.txt). Generates a self-signed CA + server cert, valid 10 years, SAN includes `remote.local`, `localhost`, `127.0.0.1`. Writes `cert.pem`, `key.pem`, `ca.pem` to the project root. Prints platform-specific commands to add `ca.pem` to the system trust store (macOS Keychain / Linux ca-certificates / Windows LocalMachine\\Root).

## .env shape

`write_env()` writes user-facing keys then a "Auto-detected binary paths" section with `_VLC_BIN`, `_QBIT_BIN`, `_JACKETT_BIN`, `_MULLVAD_BIN`, `_FFMPEG_BIN`, `_FPCALC_BIN`. These are read by `run.py`, `watchdog.py`, and `analyzer.py` to skip path discovery on subsequent runs.

`merge_tool_paths()` ([setup.py:1045](../setup.py#L1045)) re-runs without re-prompting: keeps user settings, drops stale `_*_BIN` entries that no longer exist, appends new ones.

## See also

- [RUNTIME.md](RUNTIME.md) — what `run.py` does after `setup.py` is done
- [DAEMON_WATCHDOG.md](DAEMON_WATCHDOG.md) — `setup.py` optionally calls `daemon.install()`
