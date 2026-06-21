# Graphical Installer (`install.bat` + `installer.py`)

The one-click, no-terminal first-install path for **Windows** (primary target).
A user double-clicks `install.bat`, clicks through a small wizard, and ends up
with a fully configured, running StreamLink. It is a thin front-end over the
existing [`setup.py`](SETUP.md) — it does **not** reimplement any setup logic.

> Linux/macOS: there's no `.sh` equivalent yet; use `python3 setup.py`. The
> wizard (`installer.py`) does run on those platforms (Tk is cross-platform)
> but is not the documented path there.

---

## Two pieces

### `install.bat` — the bootstrap

Runs under `cmd.exe`, before any Python is guaranteed to exist.

1. **Self-elevates** via `powershell Start-Process -Verb RunAs` if not already
   admin. Everything downstream needs admin: an **all-users** Python install,
   the Jackett Windows service, firewall rules, and binding ports 80/443.
2. **`cd /d "%~dp0"`** so the rest runs from the repo root regardless of where
   the elevated shell starts.
3. **Finds a usable Python ≥ 3.9** with the `:try_py` subroutine, preferring the
   `py -3` launcher, then `python`. The version gate is a `raise SystemExit(...)`
   one-liner, so a too-old Python doesn't count.
4. **Installs Python 3.12 if none found** — `winget install -e --id
   Python.Python.3.12 --scope machine --silent …` (all-users), falling back to
   downloading the official python.org installer and running it `/quiet
   InstallAllUsers=1 PrependPath=1 Include_tcltk=1 Include_launcher=1`. **All-users
   is deliberate**: a per-user Python produces a `.venv` the service account
   can't execute — see [GOTCHAS.md](GOTCHAS.md) "Microsoft Store / per-user
   Python". `Include_tcltk` guarantees Tkinter for the wizard. After install it
   re-detects via `py -3` (the launcher lands in `%WINDIR%`, already on PATH).
5. **Launches the wizard**: `%PY% installer.py`. Blocks until the window closes;
   pauses on a non-zero exit so the user can read the error.

### `installer.py` — the wizard

A standard-library-only Tkinter app (no pip deps — Tk ships with the python.org
build). **Must run under the system Python**, same constraint as `setup.py`; it
launches `setup.py` with `sys.executable`.

Pages (frame-swap, no separate windows):

| Page | What it does |
|------|--------------|
| **Welcome** | Explains what will be installed; shows the detected Python. |
| **Settings** | Download folder (+Browse), admin password, content filter (All/Movies/TV). An **Advanced** drawer reveals Jackett/qBit/VLC URLs+credentials and buffer thresholds. Checkboxes: *AI auto-subtitles* and *Start on boot*. Defaults are the factory defaults, pre-filled. Validates port collision (qBit vs VLC) and numeric buffers before continuing. |
| **Install** | Runs `setup.py` in a worker thread, streams its stdout into a live log (ANSI stripped), indeterminate progress bar. Enables **Next** on completion; flags a non-zero exit in red (which routes straight to Finish with a "didn't finish cleanly" message instead of the guided steps). |
| **Connect VPN** (step 2) | *Guided manual step.* Mullvad is now installed, so this explains the kill-switch, links to get an account, offers an **Open Mullvad** button, the log-in/connect steps, and a **Test VPN connection** button (live check — see below). |
| **qBittorrent Web UI** (step 3) | *Guided manual step.* StreamLink drives downloads through qBit's Web UI; `setup.py` writes the ini but qBit can overwrite it if it was open during install. An **Open qBittorrent** button (launches `_QBIT_BIN`), numbered steps, a read-only **"match these values"** panel (exact Port / Username / Password / save path, pulled live from the chosen config), and a **Test Web UI** button. |
| **VLC remote control** (step 4) | *Guided manual step.* An **Open VLC** button (launches `_VLC_BIN`), steps to dismiss VLC's one-time privacy dialog (which otherwise blocks the Lua HTTP interface), the web port + password panel, and a **Test web control** button (which actually launches VLC with the right flags and confirms reachability — so it also surfaces the first-run dialog). |
| **Add a search source** (step 5) | *Guided manual step.* Jackett is now installed **and running**, so this offers an **Open Jackett** button (opens `INDEXER_URL`), numbered steps to add an indexer and copy the API key, fields for the **API key** + optional Jackett admin password, and a **Test Jackett** button. On continue the key/password are written into `.env` in place via `_set_env_keys()`. *Skip for now* is offered. |
| **Finish** | Success summary + dashboard URLs. Adapts to the *Install as a service* choice (`_update_finish()`): **if the service was installed**, `daemon.install()` already started it during setup, so the redundant "Launch now" is hidden and the page notes it'll auto-start on login (plus the auto-login tip for surviving a full reboot); **if not**, it shows an optional **Launch StreamLink now** that runs `run.py` in a new console. |

### Live connection tests (the green/red checks)

Each guided page has a **Test** button that actually **starts the service and
verifies it**, then shows ✓ green / ✗ red (with the failure's last log line) /
amber "checking…". This reuses `run.py`'s real `start_vlc()` /
`start_qbittorrent()` / `start_jackett()` / `check_mullvad()` — no duplicated
launch logic, so VLC comes up with the exact same flags (HTTP interface,
fullscreen, marquee) the dashboard uses, and the readiness checks match.

The wizard runs under the **system** Python and can't `import run` directly:
`run.py` `os.execv`s itself into `.venv` at import, and its deps (psutil, …)
live there. So `_run_check()` shells out to **the venv Python**
(`.venv/Scripts/python.exe`) with a tiny driver — `import run; run.<call>()` —
in a worker thread, and maps the exit code to pass/fail. (Running under the venv
Python means `run.py`'s execv guard is a no-op, since `sys.prefix` is already the
venv.) Because the test *starts* VLC, it also triggers VLC's first-run privacy
dialog right there, so the user can dismiss it and re-test. If `.venv` isn't
present yet (setup failed), the button reports that instead of hanging.

### Why the manual steps come *after* the install

The Jackett API key and the Mullvad login can't be collected up front: the key
only exists once Jackett is installed, running, and has an indexer added, and the
login needs the Mullvad app present. So the wizard collects only the values
`setup.py` needs *before* running it, then — once `setup.py` has installed and
started those apps — walks the user through the external steps with working
links/buttons. The API key (and optional `JACKETT_PASSWORD`) are written to the
already-generated `.env` directly; everything else flows through `setup.py`.

---

## How the wizard drives `setup.py` (the seam)

The wizard deliberately reuses `setup.py` instead of duplicating it. The seam is
small and lives in `setup.py`:

- **No stdin → defaults.** `setup.py`'s `ask()`/`ask_bool()` already return their
  default when there's no interactive stdin. The wizard runs setup with
  `stdin=DEVNULL`, so every prompt auto-answers with its default.
- **`STREAMLINK_WIZARD=1`** — `WIZARD` flag in `setup.py`. Forces
  `reuse_env=False` even when a stale `.env` exists, so the user's choices are
  written (and `qBittorrent.ini` regenerated) rather than silently skipped.
- **`SL_<ENV_KEY>=value`** — read in `gather_config()`'s `ask_field` / `ask_secret`
  with priority over the stored/factory default
  (`os.environ.get("SL_"+key) or prev.get(key, factory)`). An empty `SL_*` is
  ignored (falls back), so blank fields use the factory default. Keys mirror the
  `.env` keys exactly: `SL_QBIT_DOWNLOAD_PATH`, `SL_ADMIN_PASSWORD`,
  `SL_INDEXER_URL`, `SL_INDEXER_API_KEY`, `SL_INDEXER_CATEGORIES`, `SL_QBIT_URL`,
  `SL_QBIT_USERNAME`, `SL_QBIT_PASSWORD`, `SL_VLC_URL`, `SL_VLC_PASSWORD`,
  `SL_BUFFER_MIN_MB`, `SL_BUFFER_MIN_PCT`.
- **`STREAMLINK_INSTALL_STT` / `STREAMLINK_INSTALL_SERVICE`** — checked via
  `_env_skip()` (`"0"` = skip). `install_stt_deps()` skips the whisper.cpp
  download; `offer_service_install()` skips registering the boot service. Any
  other value leaves the original default behaviour intact.

Because everything routes through `setup.py`, an unattended click-through with
all-defaults produces exactly the same `.env`, `qBittorrent.ini`, SSL cert, and
service registration that the terminal flow would.

> Keep `installer.py`'s `DEFAULTS` dict in lock-step with `gather_config()`'s
> factory defaults — they're duplicated for display, and drift would show the
> user a default the backend doesn't actually use.

---

## Manual steps

Every step that needs a human is **walked through inline**, in its own page, at
the point where the relevant app is installed: connecting **Mullvad VPN**,
verifying the **qBittorrent Web UI**, dismissing **VLC's** first-run dialog, and
adding a **Jackett indexer + API key** (see the table above). They're no longer
just "surfaced" at the end. Each is skippable and can be redone later from the
app / README. The qBit & VLC pages show the exact values to match (port,
credentials, save path) so the user doesn't have to cross-reference `.env`.

## See also

- [SETUP.md](SETUP.md) — what `setup.py` actually does, step by step.
- [RUNTIME.md](RUNTIME.md) — what `run.py` does when the wizard launches it.
- [GOTCHAS.md](GOTCHAS.md) — the per-user-Python footgun the all-users install avoids.
