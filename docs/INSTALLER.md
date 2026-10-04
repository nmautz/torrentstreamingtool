# Graphical Installer (`install.bat` + `installer.py` + `installsteps.py`)

The no-terminal first-install path for **Windows**. Someone downloads the ZIP (or
clones), double-clicks `install.bat`, and ends with a configured, running
StreamLink. It is a front-end over [`setup.py`](SETUP.md): it reimplements none
of setup's steps.

> **Status: built on a Mac, never run on Windows.** The decision logic is covered
> by `tests/test_installsteps.py` (120 cases, including a real ZIP-folder-to-clone
> run against a throwaway git remote), and every page was laid out headlessly.
> `install.bat` and everything that touches Windows itself (UAC, winget, Task
> Scheduler, the Tk window) has not been executed. See [What has not been
> checked](#what-has-not-been-checked).

> Linux/macOS: use `python3 setup.py`. `installer.py` starts on those platforms
> but is not a supported path there.

---

## Three pieces

| File | Role |
|------|------|
| `install.bat` | Runs before any Python is known to exist. Elevates, finds or installs a usable Python, starts the wizard. |
| `installer.py` | The Tk window, and nothing else. Standard library only, runs under the **system** Python. |
| `installsteps.py` | Every decision the wizard makes, with no window attached. Leaf module (stdlib only, no `main`/`run`/`setup` import), tests in `tests/test_installsteps.py`. |

### `install.bat`

1. Refuses to run from inside the ZIP viewer (`installer.py` isn't beside it).
2. **Elevates** (`fltmc` as the admin test, `Start-Process -Verb RunAs`). Says so
   and stops if the prompt is declined.
3. **Finds a usable Python**, in order: `%ProgramFiles%\Python3*\python.exe`,
   `py -3`, `python`. "Usable" is checked by running it: 3.9+, `tkinter`
   imports, and its path is **not per-user** (no `AppData`, no `WindowsApps`).
   A per-user Python is the default python.org install, and `setup.py` refuses
   it when there is nobody to ask (see [GOTCHAS.md](GOTCHAS.md) "per-user Python").
4. **Installs Python 3.12 for all users** if none qualifies: `winget --scope
   machine`, falling back to the python.org installer with `InstallAllUsers=1
   Include_tcltk=1`. Re-scans `%ProgramFiles%` afterwards, because the new
   Python is not on this cmd session's PATH.
5. Starts the wizard, from inside one parenthesised block (see GOTCHAS: the
   wizard can replace `install.bat` while cmd is still reading it).

The file is plain ASCII with CRLF endings. `.gitattributes` pins `*.bat eol=crlf`,
which also applies to GitHub's ZIP downloads.

### `installer.py`: the pages

Welcome → Settings → Install → VPN → qBittorrent → VLC → Jackett → TMDb → Finish

| Page | What it does |
|------|--------------|
| **Welcome** | What will happen. **Stops here** if the wizard is running as a different account than the one signed in (the admin prompt was approved with someone else's password: settings would land in the wrong profile), or under a per-user Python. |
| **Settings** | Download folder, **admin password (required, no default)**, VPN kill-switch mode, start-at-sign-in. Pre-filled from an existing `.env`. *Ports & passwords* opens the rarely-needed fields on their own page. |
| **Install** | Connects a ZIP folder to GitHub (below), then runs `setup.py` and streams its output into a log, which is also saved to `logs/installer.log`. On failure: *Try again*, *Back*, *Close*. |
| **VPN** | Skipped when the mode is *off*. Checks with `run.check_vpn()`. |
| **qBittorrent** | Starts qBittorrent (only if the VPN check passes), logs in to its Web UI the way the server does, and stops it again. |
| **VLC** | Reads `vlcrc` to see whether VLC's first-run privacy question has been answered. Does not start VLC. |
| **Jackett** | Starts Jackett, **reads its API key from `ServerConfig.json`**, asks Jackett's own API whether the key works and how many indexers are set up. |
| **TMDb** | Optional key, checked against TMDb. |
| **Finish** | Registers and starts the service (or offers *Start StreamLink*), waits for port 80, offers *Open StreamLink*. Lists any step that was skipped. |

**Check first, explain on failure.** Each step page runs its check when it opens.
Green means one click to move on; the numbered instructions only appear when the
check fails. Every step can be skipped: the dashboard's own first-run checklist
(`/api/setup-status`) reports the same gaps later.

**StreamLink starts last.** `setup.py` is always run with
`STREAMLINK_INSTALL_SERVICE=0`; the wizard runs `run.py --install` itself on the
Finish page. The server reads `.env` once at startup, so started any earlier it
would never see the Jackett or TMDb key. Its watchdog would also kill a
qBittorrent the user had just been asked to open.

**The page area scrolls** if a page is taller than the window. Text height
depends on the font and display scaling, so no fixed size fits everywhere.

---

## The seam into `setup.py`

`setup.py` already answers every prompt with its default when there is no
stdin. The wizard runs it with `stdin=DEVNULL` and steers it with env vars
(built by `installsteps.setup_env`):

| Variable | Effect in `setup.py` |
|----------|----------------------|
| `STREAMLINK_WIZARD=1` | Never reuse `.env` wholesale: re-gather and rewrite it and `qBittorrent.ini`. Skips the closing "steps you still do by hand" list. |
| `SL_<ENV_KEY>` | The value for that key. **Set wins, even when empty; absent leaves the stored/factory value.** The wizard seeds every key it shows except `INDEXER_API_KEY` and `JACKETT_PASSWORD`, which it finds after Jackett is installed, so a stored key survives a re-run. |
| `STREAMLINK_VPN_MODE` | `mullvad` / `generic` / `off`. Mullvad is only winget-installed for `mullvad`. `seed_vpn_mode()` writes the mode into `library.json` so the first start already enforces it. |
| `STREAMLINK_INSTALL_SERVICE=0` | Skip `offer_service_install()`. |

`write_env()` now keeps keys setup doesn't prompt for (`TMDB_API_KEY`,
`WINDOWS_ADMIN_*`, anything added by hand). That also fixes the terminal flow,
where answering "no" to "reuse existing .env?" used to drop them.

`seed_vpn_mode()` writes `library.json` outside `_lib_lock`, which is only safe
when no server holds it. So it writes when the file doesn't exist yet, or when
nothing is listening on port 80; otherwise it leaves the mode alone and points at
Admin → VPN Kill Switch. The Settings page disables the choice in the same case.

`installsteps.DEFAULTS` duplicates `gather_config()`'s factory defaults for
display. The test file parses `setup.py` and fails if they drift.

---

## A ZIP download becomes a clone

The updater is git (`updater.py`), and a ZIP has no `.git`. On the Install page,
before `setup.py`:

1. If Git is missing, `winget install Git.Git --scope machine`.
2. `installsteps.ensure_clone()`: `git init`, add `origin`, fetch one branch,
   `checkout -f -B <branch> origin/<branch>`, set upstream, and add the folder to
   the user's `safe.directory` (the wizard is elevated, so `.git` is owned by
   Administrators and the unelevated service's git would refuse it).

**Which branch:** the folder name. GitHub names a ZIP's folder `<repo>-<branch>`,
so `torrentstreamingtool-alpha (1)` is `alpha`. A renamed folder falls back to
`main`.

**A branch is only used if it contains the installer** (`installer.py` and
`installsteps.py` at its tip). Checking out a branch without them would delete
the running wizard and hand `setup.py` a seam it doesn't have. If no candidate
qualifies, or there is no network, the folder is left as it was, any half-made
`.git` is removed, and the log says updates are off. This never fails the install.

`checkout -f` brings the files to the branch tip, so a ZIP that is a few commits
old is updated on the spot. Untracked files (`.env`, `.venv`, `library.json`) are
not touched.

---

## Checks that need `run.py`

`run.py` re-executes itself into `.venv` at import and its dependencies live
there, so the wizard cannot import it. `installsteps.run_driver()` runs a short
driver under the venv's Python instead; each prints one `RESULT:<word>` line.

- `VPN_DRIVER`: `run.check_vpn()`.
- `JACKETT_DRIVER`: `run.start_jackett()`.
- `QBIT_DRIVER`: if the Web UI isn't already up, require `check_vpn()`, start
  qBittorrent, log in, then **kill it if this check started it**.

Children get `PYTHONIOENCODING=utf-8` / `PYTHONUTF8=1` and are read as UTF-8 with
`errors="replace"` (see GOTCHAS).

---

## What has not been checked

Nothing here has run on Windows. In rough order of how likely each is to need a fix:

- `install.bat` end to end: the elevation relaunch, the `for /d` Python scan, the
  winget and python.org installs, the final parenthesised block.
- The Tk window's look on Windows (fonts, DPI awareness on a scaled display,
  scrolling). Layout was only measured on a Mac, where Apple's bundled Tk cannot
  open a window at all.
- `vlcrc` containing `qt-privacy-ask=0` after the first-run dialog is answered.
  If VLC records it differently the VLC page will always say "still waiting"; it
  can be skipped.
- Jackett's answer to a wrong API key (the code accepts both a 401 and a 200 with
  an `<error>` body) and the location of `ServerConfig.json` for a service install
  (`%ProgramData%\Jackett`).
- qBittorrent's first-launch legal notice. `setup.py` does not pre-accept it; if
  it holds up the Web UI the qBittorrent page fails with instructions that
  mention it.
- `run.py --install` from the elevated wizard, and whether the task it starts is
  serving within the 90 s the Finish page waits.
- The ZIP-to-clone path with a real Git for Windows (line endings, the
  `safe.directory` entry being enough for the unelevated updater).

## See also

- [SETUP.md](SETUP.md): what `setup.py` does, step by step.
- [RUNTIME.md](RUNTIME.md): what `run.py` does once started.
- [GOTCHAS.md](GOTCHAS.md) § Graphical installer.
