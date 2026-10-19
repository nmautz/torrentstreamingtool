"""Settings: the .env keys, read once into `settings`.

`settings` is never rebound: `_reload_settings` (main.py) re-reads .env into
the same object (`settings.__init__()`), so `from backend.config import
settings` always sees the current values. `env_file` is relative to the
working directory, which run.py / the service set to the repo root.
"""
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


# ── Settings ──────────────────────────────────────────────────────────────────

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    indexer_url: str = "http://localhost:9117"
    indexer_api_key: str = ""
    indexer_categories: str = "0"

    # Optional FlareSolverr proxy (Cloudflare/DDoS-Guard challenge solver) that
    # Jackett can use for protected indexers. The base URL the proxy listens on;
    # the admin pastes this into Jackett's "FlareSolverr API URL" setting.
    flaresolverr_url: str = "http://localhost:8191"

    qbit_url: str = "http://localhost:8081"
    qbit_username: str = "admin"
    qbit_password: str = "adminadmin"
    qbit_download_path: str = str(Path.home() / "Downloads" / "StreamLink")
    library_path_2: str = ""   # optional extra storage locations (LIBRARY_PATH_2 in .env)
    library_path_3: str = ""
    library_path_4: str = ""

    vlc_url: str = "http://localhost:8080"
    vlc_password: str = "vlcpassword"

    buffer_min_mb: float = 15.0
    buffer_min_pct: float = 1.0

    admin_password: str = ""   # if empty, admin panel is disabled
    jackett_password: str = ""  # Jackett UI login password for indexer management

    # Windows host-admin credentials for elevated OS tweaks (currently:
    # setting the physical power/sleep buttons to "Do nothing" via powercfg,
    # which needs an admin token the un-elevated service task doesn't have).
    # Optional — the elevated `run.py --install` path applies the tweak
    # directly, so these only matter when that never ran. Windows only.
    windows_admin_user: str = ""
    windows_admin_password: str = ""

    # OpenSubtitles legacy REST API (rest.opensubtitles.org) needs no key, only a
    # User-Agent. "TemporaryUserAgent" is OpenSubtitles' documented testing UA.
    opensubtitles_user_agent: str = "TemporaryUserAgent"

    # TMDb v3 API. Empty → metadata features are disabled (falls back to filename
    # parsing on the client). Key can also be set live from the admin panel and
    # is persisted in library.json → settings.admin_overrides.tmdb_api_key.
    tmdb_api_key: str = ""

    # HID wireless remote (air-mouse) media-key support. The remote's dongle
    # presents as a keyboard; remote_input.py hooks its media keys globally and
    # routes them into the VLC / YouTube control paths (see docs/REMOTE.md).
    # Set REMOTE_CONTROL=0 in .env to disable the listener entirely.
    remote_control: bool = True

    # Opt-in OS-mixer volume guard (see remote_volume_guard). Default OFF: on
    # real hardware the keyboard hook's suppression alone keeps the remote's
    # volume off the host mixer, and the guard's revert loop can FIGHT audio
    # endpoints that quantize volume (HDMI outputs), flickering the mixer and
    # drifting VLC's volume on its own. Enable (REMOTE_VOLUME_GUARD=1) only
    # for a remote whose volume provably bypasses the keyboard hook (system
    # volume changes even though remote support is active during playback).
    remote_volume_guard: bool = False

    # TV UI (Firestick-style): a fullscreen Chrome kiosk showing the dashboard
    # (`/?tv=1`) on the host display. Any remote button wakes it when nothing is
    # playing; after `tv_ui_idle_secs` with no input it hands the screen back to
    # the idle background video. Needs remote_control (the wake signal comes
    # from the global input listener). See docs/REMOTE.md.
    tv_ui: bool = True
    tv_ui_idle_secs: int = 120


settings = Settings()
