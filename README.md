# P2P StreamLink — Setup Guide (Windows)

Local dashboard for searching, buffering, and instantly streaming P2P media through VLC — with a Mullvad VPN kill-switch.

**Windows is the primary, supported platform.** Linux and macOS work but are secondary; this guide is written for Windows. Platform differences are called out where they matter.

---

## TL;DR

```powershell
python setup.py     # one-time: installs & configures everything it can
python run.py       # start all services + the dashboard
```

Then do the **manual** things `setup.py` cannot do for you (below): **pick/confirm ports + credentials**, **verify the qBittorrent and VLC Web UIs**, **log into Mullvad VPN**, and **add a Jackett indexer + paste its API key**. Optionally, for unattended restarts, **enable Windows auto-login** and **install the system service**. Don't assume any of these happened on their own — verify each.

---

## What's automatic vs. what you do by hand

`setup.py` does as much as possible without you. A handful of steps **require a human** because they involve a login, an external account, or a Windows security prompt.

| Step | Who does it | Notes |
|------|-------------|-------|
| Install Python 3.9+ | **You** (once) | See [Prerequisite](#0--prerequisite-python). Everything else is bootstrapped from here. |
| Create `.venv` + install Python packages | Automatic | `setup.py` |
| Install VLC, qBittorrent, Mullvad, Jackett | Automatic | via `winget` |
| Register Jackett as a Windows service | Automatic | UAC prompt — **click Yes** |
| Install ffmpeg + Chromaprint (Smart Skip) | Automatic | portable zips into `tools\` |
| Install whisper.cpp + model (AI subtitles) | Automatic (optional) | decline = no AI subtitles |
| Generate `.env` config | Automatic | merges on re-run, never clobbers |
| Create the download folder | Automatic | |
| Generate the HTTPS/admin SSL certificate | Automatic | self-signed |
| **Choose ports + credentials** (VLC / qBittorrent / Jackett / admin) | **You** | Prompted in setup; you pick them and they must **match** in each app and `.env`. See [Ports & credentials](#ports--credentials-you-choose-these). |
| **Configure & verify the qBittorrent Web UI** | **Automatic for local, but verify — manual if remote** | `setup.py` writes `qBittorrent.ini` only when qBittorrent is local **and closed**; if it's remote, was open, or rewrites the ini on exit, enable the Web UI / set the port + credentials yourself. See [step 4](#4--configure--verify-the-qbittorrent-web-ui-manualverify). |
| **Configure & verify the VLC Web (Lua HTTP) interface** | **Mostly automatic, but verify** | `run.py` launches VLC with the HTTP interface + password, but you must clear VLC's first-run dialog and confirm the password matches `.env`. See [step 5](#5--configure--verify-the-vlc-web-interface-manualverify). |
| **Log into Mullvad and connect the VPN** | **You** | Account login — `setup.py` can install the app but not log you in. See [step 3](#3--log-into-mullvad-manual). |
| **Add a Jackett indexer + copy its API key into `.env`** | **You** | Needs the Jackett web UI. See [step 6](#6--configure-jackett-manual). |
| **Enable Windows auto-login** (for unattended reboots) | **You** | Optional. See [Unattended restarts](#unattended-restarts-optional). |
| **Install the StreamLink system service** | **You** (one command) | Optional. `python run.py --install`. |

> Rule of thumb: if it needs an **account login** (Mullvad), a **third-party UI** (Jackett indexers), a **port/credential you pick** (VLC, qBittorrent, admin), or a **Windows security decision** (auto-login, accepting UAC prompts), it's your job. `setup.py` automates the rest and *attempts* the qBittorrent/VLC wiring — but you must **verify** it, because those settings live inside other apps that can ignore or overwrite what we write.

---

## 0 — Prerequisite: Python

Install **Python 3.9 or newer** from [python.org](https://www.python.org/downloads/) (or `winget install Python.Python.3.12`).

- On the installer's first screen, **tick "Add python.exe to PATH"**.
- Verify in a new terminal: `python --version`.

You do **not** need to install VLC, qBittorrent, Mullvad, or Jackett yourself — `setup.py` installs them via `winget` (built into Windows 10/11). If `winget` is missing, install **App Installer** from the Microsoft Store.

**Also assumed:**

- A working **internet connection** during setup — `setup.py` downloads packages, app installers, and portable tool builds (ffmpeg, Chromaprint, whisper.cpp).
- You can **accept UAC prompts** (admin elevation) for the Jackett service and firewall rules.
- For the dashboard to bind ports 80/443 and add firewall rules, run the **first `python run.py` from a terminal opened "as Administrator"** (it warns if not elevated).

---

## 1 — Run setup (automatic)

From the project folder, in a terminal:

```powershell
python setup.py
```

This will, without further input where possible:

- Create `.venv` and install all Python dependencies.
- Install **VLC**, **qBittorrent**, **Mullvad VPN**, and **Jackett** via `winget` (skips any already present).
- Register **Jackett as a Windows service** so it runs from boot. **A UAC prompt appears — click Yes.**
- Install **ffmpeg + Chromaprint** (for Smart Skip) as portable builds into `tools\`.
- Optionally install **whisper.cpp + a language model** for AI auto-subtitles. Declining only disables that feature.
- **Attempt** to write the **qBittorrent Web UI config** to `qBittorrent.ini` (port **8081**, localhost auth disabled, CSRF off) — this only takes effect when qBittorrent is **local and closed**; you still verify it in [step 4](#4--configure--verify-the-qbittorrent-web-ui-manualverify).
- Generate **`.env`** with your settings and the auto-detected tool paths.
- Create the **download folder**.
- Generate a self-signed **SSL certificate** for the HTTPS admin panel.
- Offer to **install the StreamLink system service** (defaults to **yes** on Windows — see [Unattended restarts](#unattended-restarts-optional)).

It will prompt for a few values; press **Enter** to accept the shown default for any of them:

- Jackett URL (default `http://localhost:9117`) and, later, its API key
- qBittorrent username / password / download path
- VLC password
- Buffer thresholds
- **Admin password** (for the `/admin` panel; default `adminadmin` — change this)

> Re-running `python setup.py` later is safe: it **merges** into your existing `.env` and re-detects tool paths. Each prompt pre-fills your current value; secrets show `••••••` and keep their stored value on Enter.

> Run setup with **qBittorrent closed** so it picks up the new config on next launch.

**Jackett on another PC?** Enter your remote address (e.g. `http://192.168.1.50:9117`) when prompted for the Jackett URL. `setup.py` won't try to install or register a Jackett service locally, and `run.py` will only check that the remote one is reachable.

---

## 2 — Start everything

```powershell
python run.py
```

`run.py` relaunches itself inside `.venv` and then:

- **VLC** — starts with the Lua HTTP interface on port **8080** (restarts VLC if it's open without HTTP).
- **qBittorrent** — starts if not running; waits for its Web UI on port **8081**.
- **Jackett** — local: starts the service / binary; remote: checks reachability only.
- **Mullvad** — verifies you're connected; warns and asks to continue if not.
- **Windows Firewall** — adds inbound rules for the dashboard ports and mDNS (UAC / admin needed; warns if not elevated).
- **Dashboard** — serves on **port 80** (HTTP) and, with the SSL cert, **port 443** (HTTPS, used by the admin panel), bound to `0.0.0.0` so phones on your LAN can connect.

Open the dashboard at **http://localhost** (or `http://<this-pc-LAN-ip>` / `http://remote.local` from another device on the network). The admin panel lives at **https://localhost/admin**.

Press **Ctrl+C** to stop the dashboard. VLC, qBittorrent, and Jackett keep running.

> The remaining steps (3–6) are **manual** and must be done once before the tool works end to end. `run.py` starts the apps, but it cannot log into your VPN, accept VLC's first-run dialog, or know your Jackett indexers and API key.

---

## Ports & credentials (you choose these)

StreamLink does not invent ports or passwords for you — **you pick them in `setup.py`, and the exact same values must be set inside each app and in `.env`.** A mismatch is the most common reason a fresh install "doesn't work."

| Service | Default port | Credential | Must match between |
|---------|-------------|------------|-------------------|
| VLC (Lua HTTP) | `8080` | `VLC_PASSWORD` | VLC launch flags ↔ `.env` |
| qBittorrent Web UI | `8081` | `QBIT_USERNAME` / `QBIT_PASSWORD` | qBittorrent → Preferences → Web UI ↔ `.env` |
| Jackett | `9117` | API key | Jackett dashboard ↔ `.env` |
| Admin panel | `443` | `ADMIN_PASSWORD` | `.env` only |
| Dashboard | `80` (HTTP) | — | — |

Rules:

- **No two services may share a port.** VLC `8080` and qBittorrent `8081` in particular must differ — `setup.py` warns if they collide.
- If you change a port, change it in **both** the app's own settings **and** the matching `.env` URL (`VLC_URL`, `QBIT_URL`, `INDEXER_URL`), then restart that service.
- Passwords are your choice. The factory defaults (`vlcpassword`, `adminadmin`) are placeholders — **change them**, especially `ADMIN_PASSWORD`.

---

## 3 — Log into Mullvad (manual)

`setup.py` can *install* the Mullvad app, but it cannot log into your account for you.

1. Open the **Mullvad VPN** app.
2. Enter your **account number** and **connect**.
3. Make sure the Mullvad **CLI** is on your PATH (the official installer adds it). `run.py` runs `mullvad status` to verify the kill-switch.

The VPN kill-switch is core to this tool: every 3 seconds the backend checks `mullvad status`, and if you're not connected it **kills qBittorrent**, overlays a red warning, and blocks new streams until you reconnect. Without Mullvad connected, downloads will not run.

---

## 4 — Configure & verify the qBittorrent Web UI (manual/verify)

`setup.py` *tries* to set this up by writing `qBittorrent.ini`, but that only works when qBittorrent is **local and was closed** during setup — and qBittorrent can overwrite the file when it exits. **Always verify**, and configure by hand if qBittorrent is on another machine or the auto-config didn't stick.

In **qBittorrent → Tools → Preferences → Web UI**, confirm:

1. **"Web User Interface (Remote control)" is enabled.**
2. **Port** matches `QBIT_URL` in `.env` (default **8081**, and **not** the same as VLC's 8080).
3. **Username / password** match `QBIT_USERNAME` / `QBIT_PASSWORD` in `.env`.
4. **"Bypass authentication for clients on localhost"** is on (and CSRF protection off) so the dashboard can talk to it locally.
5. The **default save path** points at your download folder.

Restart qBittorrent after any change. Quick check: open `http://localhost:8081` and log in with your credentials.

---

## 5 — Configure & verify the VLC Web interface (manual/verify)

`run.py` launches VLC with the **Lua HTTP interface and password** on the command line, so you normally don't touch VLC's preferences — but two things still need a human:

1. **Clear VLC's first-run dialog.** On a brand-new VLC install, the privacy/network-access prompt blocks the HTTP interface until you dismiss it. Open VLC once and accept it.
2. **Confirm the password matches.** `VLC_PASSWORD` in `.env` must equal what VLC is launched with. If you change it in `.env`, restart via `run.py` (which re-launches VLC with the new password).

If VLC was already open *without* the HTTP interface, `run.py` restarts it for you. If port **8080** is taken, change `VLC_URL` in `.env` and ensure it doesn't collide with qBittorrent's 8081.

To configure VLC's web interface by hand instead (e.g. you run VLC yourself): **Preferences → Show All → Interface → Main interfaces → tick "Web"**, then set the Lua HTTP password under **Interface → Main interfaces → Lua**, using port 8080 and the same password as `.env`.

---

## 6 — Configure Jackett (manual)

Adding indexers requires Jackett's own web UI — this can't be automated.

1. Open Jackett at the URL you configured (default **http://localhost:9117**).
2. **Add one or more indexers** for the content you want.
3. Copy the **API Key** (top-right of the Jackett dashboard).
4. Paste it into `.env` as `INDEXER_API_KEY=…` (or re-run `python setup.py` and enter it at the prompt).

Until an indexer is added and the API key is set, searches return `Indexer unreachable`.

### FlareSolverr (optional — only for Cloudflare-protected indexers)

Some indexers sit behind a Cloudflare / DDoS-Guard browser challenge and fail in Jackett with a "challenge" error. **FlareSolverr** is an optional proxy that solves those. Most indexers don't need it — only set it up if one is failing this way.

1. In the admin panel, open **Indexers → FlareSolverr** and click **Install FlareSolverr** (Windows + Linux only; on macOS run it via Docker). StreamLink downloads the portable bundle and starts it automatically.
2. Copy the **FlareSolverr API URL** shown on that card (default **http://localhost:8191**).
3. In Jackett, click the **cog (Configure Jackett)**, paste the URL into **FlareSolverr API URL**, and **Save**. StreamLink can't set this for you — Jackett has no API for it.

`run.py` relaunches FlareSolverr on every startup once it's installed.

---

## Unattended restarts (optional)

If you want the box to come back to a running StreamLink on its own after a reboot (e.g. the admin **System → Scheduled Restart** feature), you need **two** things — both partly manual:

1. **Enable Windows auto-login** so the user account signs in without a password prompt after reboot. The service only starts once that user session exists.
   - Run `netplwiz`, untick *Users must enter a user name and password…*, **or** use Sysinternals **Autologon**.
2. **Install the StreamLink system service** so it relaunches on login:
   ```powershell
   python run.py --install     # registers the Windows Task Scheduler task (UAC prompt)
   python run.py --status      # confirm it's registered
   python run.py --uninstall   # remove it
   ```

Without both, a reboot leaves the dashboard offline until you run `python run.py` by hand again.

---

## Trusting the HTTPS certificate (optional)

The admin panel uses a self-signed cert, so browsers show a warning. To remove it, add `ca.pem` (in the project root) to the Windows trust store:

```powershell
# elevated PowerShell
Import-Certificate -FilePath .\ca.pem -CertStoreLocation Cert:\LocalMachine\Root
```

`setup.py` prints the exact command for your platform.

---

## Configuration reference

All settings live in `.env` (generated by `setup.py`); see `.env.example` for the full list.

| Variable | Default | Notes |
|----------|---------|-------|
| `INDEXER_URL` | `http://localhost:9117` | Jackett — accepts a remote `http://host:port` |
| `INDEXER_API_KEY` | _(empty)_ | Paste from the Jackett dashboard (manual) |
| `INDEXER_CATEGORIES` | `0` | `0` = all; `2000` = Movies; `5000` = TV |
| `QBIT_URL` | `http://localhost:8081` | qBittorrent Web UI |
| `QBIT_USERNAME` / `QBIT_PASSWORD` | `admin` / `adminadmin` | Set during setup |
| `QBIT_DOWNLOAD_PATH` | _(under your user folder)_ | Where files are saved (created automatically) |
| `VLC_URL` | `http://localhost:8080` | VLC Lua HTTP interface |
| `VLC_PASSWORD` | `vlcpassword` | Must match what VLC launches with |
| `ADMIN_PASSWORD` | `adminadmin` | `/admin` panel — **change this** |
| `BUFFER_MIN_MB` | `15.0` | Start VLC once this many MB are downloaded… |
| `BUFFER_MIN_PCT` | `1.0` | …or once this % of the file is downloaded |

> **Ports must not collide.** VLC = 8080, qBittorrent = 8081, Jackett = 9117, dashboard = 80/443. `setup.py` warns if VLC and qBittorrent overlap.

---

## How it works

```
Search → Jackett API → results sorted by seeders
  ↓
Play → qBittorrent adds magnet → sequential download enabled
  ↓
Buffer check (async poll) → 15 MB or 1% threshold crossed
  ↓
VLC ← file:/// URI sent to the Lua HTTP interface → playback begins
  ↓
Stop → torrent + local file deleted → VLC stopped
```

**VPN kill-switch** — every 3 s the backend runs `mullvad status`. If `"Connected"` is absent: qBittorrent is killed, a full-screen red warning overlays the dashboard, and new `/api/stream` requests return HTTP 403 until the VPN reconnects.

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `Indexer unreachable` | Add an indexer in Jackett and set `INDEXER_API_KEY`; confirm `INDEXER_URL` is correct |
| Remote Jackett not reachable | Confirm it's running on the other PC and the port is reachable from this machine |
| qBittorrent Web UI won't respond | Close qBittorrent, re-run `python setup.py`, relaunch |
| VLC doesn't play | Confirm VLC is open (`run.py` starts it) and `VLC_PASSWORD` in `.env` matches |
| "VPN DISCONNECTED" overlay | Reconnect Mullvad — the overlay clears automatically |
| `mullvad CLI not found` | Reinstall Mullvad / add its CLI to PATH, then re-run `setup.py` |
| Dashboard won't bind / no firewall rule | Run the terminal **as Administrator** so `run.py` can add firewall rules and bind ports |
| Service doesn't restart after reboot | Confirm Windows **auto-login** is on and `python run.py --status` shows the task registered |
| Port conflict on 8080 / 8081 | They must differ. Edit `VLC_URL` / `QBIT_URL` in `.env`; for qBittorrent also update its Web UI port |

---

## Linux / macOS notes

The same `python3 setup.py` / `python3 run.py` flow works, with these differences:

- **Install command**: Linux prints a hint (desktop packaging varies); macOS uses Homebrew casks.
- **Service**: Linux = systemd user unit; macOS = launchd agent. Use `python3 run.py --install`.
- **Auto-login**: Linux = display-manager autologin (or `loginctl enable-linger $USER` for headless); macOS = System Settings → Users & Groups → *Automatically log in as…* (needs FileVault off).
- **Privileged ports**: binding 80/443 needs root — run with `sudo python3 run.py`.
- **macOS** has a TCC limitation that blocks some HLS playback; treat it as dev-only.

---

## iOS client app (optional, in progress)

A native **iOS client** is being built toward the 6.0.0 release — primarily for reliable *offline* downloads/playback, with the full online dashboard available in-app. It does **not** replace this host: it connects to your running StreamLink server. The app is a separate Capacitor/Xcode project under [`ios-app/`](ios-app/) — see [ios-app/README.md](ios-app/README.md) to build and run it on an iPhone, and [docs/IOS_APP_PLAN.md](docs/IOS_APP_PLAN.md) for the roadmap. (To connect over HTTPS you'll install the host's CA on the device — same cert as [§ Trusting the HTTPS certificate](#--trusting-the-https-certificate-optional).)

**Now in the app (M2, `6.0.0-preview.2.0.0`):** each episode row has a **Download** button — tap it to copy the show to the phone, then play it with no host connection (Airplane Mode), full audio/subtitles/skip-intro. This is the host-side feature too: it reuses the existing **Stream-to-Device** HLS bundles, so nothing new to configure on the server. Online streaming and the dashboard are unchanged.

**Device pairing for remote use (M5, `6.0.0-preview.5.0.0`):** the app can now authenticate to the host so it's safe to expose beyond your LAN. On the app's Connect screen, enter your host's **`ADMIN_PASSWORD`** once to *pair* the device (it stores a long-lived token). An always-on `☰ App` menu in the app gives one-tap access to **Downloads** and **Change Server / Re-pair**. Pairing is **optional and off by default** — on your home network nothing changes. To *require* it (reject unpaired callers on the sync + download-manifest endpoints), set **`REQUIRE_DEVICE_AUTH=true`** in `.env` and restart; the browser dashboard and online playback are unaffected either way. Manage paired devices from the admin panel's API (`/api/admin/devices`).

## More documentation

Deeper reference docs live in [`docs/`](docs/): architecture, backend/frontend maps, the full API, the admin panel, Smart Skip, AI subtitles, streaming, and gotchas. Start with [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
