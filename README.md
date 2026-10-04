# P2P StreamLink — Setup Guide (Windows)

StreamLink is a home media server you run on the PC connected to your TV. Search for a show or film, and StreamLink finds a release through your Jackett indexers and downloads it with qBittorrent, only while your VPN is up. It then keeps the show in a library with posters, episode names and per-profile watch progress. You can watch it:

- **on the TV**, in a fullscreen player on the host, driven from your phone or an air-mouse remote
- **in any browser** on your network
- **on an iPhone**, with the StreamLink app. The app can stream, AirPlay or Chromecast to another TV, and download episodes to watch offline.

**Windows is the primary, supported platform.** Linux and macOS work but are secondary. This guide is written for Windows, and platform differences are called out where they matter.

---

## Easiest: the graphical installer

> **New and not yet proven on a clean PC.** If it gets stuck, the terminal steps below always work, and `logs\installer.log` records what the installer did.

1. On GitHub, press **Code → Download ZIP**, then right-click the ZIP and choose **Extract All**. (A `git clone` works too.)
2. Open the extracted folder and double-click **`install.bat`**. Windows may say it "protected your PC" because the file was downloaded: press **More info → Run anyway**. Then click **Yes** on the administrator prompt.
3. Follow the window. You don't need Python, Git or a terminal first: the installer gets what is missing.

You choose a **download folder**, an **admin password** (there is no default, you must pick one) and **which VPN you use**: Mullvad, another VPN, or none. It then installs everything `setup.py` installs, and checks each thing that needs you, one page at a time:

| Page | What it checks | What you may have to do |
|------|----------------|-------------------------|
| VPN | that your VPN is connected | log in to your VPN and connect |
| qBittorrent | that its remote control answers | usually nothing |
| VLC | that its first-run question was answered | open VLC once and press Continue |
| Jackett | that the API key works (it is read for you) and a search site exists | add at least one indexer in Jackett |
| TMDb | an optional free key for posters and episode names | paste a key |

A green square means that step is done. Instructions appear only when a check fails, and any step can be skipped: StreamLink's home page lists what is still missing. On the last page StreamLink is set to start when you sign in, and started.

The installer also connects a ZIP download to GitHub, so **Admin → Updates** works afterwards.

> Linux / macOS, or if you'd rather use a terminal, keep reading.

---

## TL;DR (terminal)

```powershell
python setup.py     # one-time: installs & configures everything it can
python run.py       # start all services + the dashboard
```

Then do the steps `setup.py` can't do for you (below). **Pick an admin password**, **connect your VPN**, **verify the qBittorrent Web UI**, **add a Jackett indexer and paste its API key**, and **add a TMDb API key**. For unattended restarts, you can also **enable Windows auto-login** and **install the system service**. Don't assume any of these happened on their own: verify each one.

---

## What's automatic vs. what you do by hand

`setup.py` does as much as it can on its own. Some steps **need a person** because they involve a login, an outside account, or a Windows security prompt.

| Step | Who does it | Notes |
|------|-------------|-------|
| Install Python 3.9+ | **You** (once), or `install.bat` | See [Prerequisite](#0--prerequisite-python). Everything else is bootstrapped from here. |
| Create `.venv` + install Python packages | Automatic | `setup.py` |
| Install qBittorrent, Jackett, Mullvad, VLC | Automatic | via `winget` |
| Register Jackett as a Windows service | Automatic | UAC prompt: **click Yes** |
| Install ffmpeg + Chromaprint | Automatic | portable zips into `tools\`. ffmpeg builds everything phones, browsers and the TV player play. Chromaprint powers Smart Skip. |
| Generate `.env` config | Automatic | merges on re-run, never clobbers |
| Create the download folder | Automatic | |
| Generate the HTTPS/admin SSL certificate | Automatic | self-signed, unique to this machine |
| Write the qBittorrent Web UI config | Automatic **if qBittorrent is local and closed**, but verify | See [step 4](#4--verify-the-qbittorrent-web-ui). |
| Disable the power/sleep buttons (Windows) | Automatic | applied by `run.py --install` (elevated) or at launch. `WINDOWS_ADMIN_USER`/`_PASSWORD` in `.env` are needed only if neither ran elevated. |
| **Choose ports + credentials** (qBittorrent / Jackett / admin / VLC) | **You** | Prompted in setup. They must **match** in each app and in `.env`. See [Ports & credentials](#ports--credentials-you-choose-these). |
| **Set an admin password** | **You** | Blank (the default) **disables** the `/admin` panel entirely. |
| **Connect the VPN** (or choose another kill-switch mode) | **You** | Account login. See [step 3](#3--set-up-the-vpn-kill-switch). |
| **Add a Jackett indexer + copy its API key into `.env`** | **You** | Needs the Jackett web UI. See [step 5](#5--configure-jackett). |
| **Add a TMDb API key** | **You** | Free, strongly recommended. See [step 6](#6--add-a-tmdb-api-key). |
| **Enable Windows auto-login** (for unattended reboots) | **You** | Optional. See [Unattended restarts](#unattended-restarts-optional). |
| **Install the StreamLink system service** | **You** (one command, or yes at the setup prompt) | Optional. `python run.py --install`. |
| **Install the iPhone app** | **You** | Optional. From SideStore, no build needed. See [iPhone app](#iphone-app-optional). |

> Rule of thumb: anything that needs an **account login** (VPN, TMDb), a **third-party UI** (Jackett indexers), a **port or credential you pick**, or a **Windows security decision** (auto-login, UAC prompts) is yours to do. `setup.py` automates the rest and *attempts* the qBittorrent wiring. **Verify** that wiring anyway, because those settings live inside another app that can ignore or overwrite what setup wrote.

---

## 0 — Prerequisite: Python

> Using `install.bat`? **Skip this section.** It installs a suitable Python for you.

Install **Python 3.9 or newer** from [python.org](https://www.python.org/downloads/) (or `winget install Python.Python.3.12`).

- On the installer's first screen, **tick "Add python.exe to PATH"**.
- Verify in a new terminal: `python --version`.

You do **not** need to install qBittorrent, Jackett, Mullvad or VLC yourself. `setup.py` installs them via `winget`, which is built into Windows 10/11. If `winget` is missing, install **App Installer** from the Microsoft Store.

**Also assumed:**

- An **internet connection** during setup. `setup.py` downloads Python packages, app installers and portable tools (ffmpeg, Chromaprint).
- You can **accept UAC prompts** (admin elevation) for the Jackett service and the firewall rules.
- **Chrome or Edge** on the host. The TV player and YouTube-on-TV run in a fullscreen browser window, and Edge ships with Windows.
- To bind ports 80/443 and add firewall rules, run the **first `python run.py` from a terminal opened "as Administrator"**. It warns if not elevated.

---

## 1 — Run setup

From the project folder, in a terminal:

```powershell
python setup.py
```

This will, without further input where possible:

- Create `.venv` and install all Python dependencies.
- Install **qBittorrent**, **Jackett**, **Mullvad VPN** and **VLC** via `winget`. Anything already present is skipped.
- Register **Jackett as a Windows service** so it runs from boot. **A UAC prompt appears: click Yes.**
- Install **ffmpeg + Chromaprint** as portable builds into `tools\`.
- **Attempt** to write the **qBittorrent Web UI config** to `qBittorrent.ini`: port **8081**, localhost auth bypassed, CSRF off, start minimized to the tray. This only takes effect when qBittorrent is **local and closed**. You still verify it in [step 4](#4--verify-the-qbittorrent-web-ui).
- Generate **`.env`** with your settings and the auto-detected tool paths.
- Create the **download folder**.
- Generate a self-signed **SSL certificate** for the HTTPS admin panel.
- Offer to **install the StreamLink system service**. The default is **yes** on Windows; see [Unattended restarts](#unattended-restarts-optional).

It prompts for a few values. Press **Enter** to accept the shown default for any of them:

- Jackett URL (default `http://localhost:9117`), its API key, and its admin password if you set one
- qBittorrent username / password / download folder
- VLC password
- Buffer thresholds
- **Admin password** for the `/admin` panel. **Leaving it blank disables the admin panel**, and you need the panel to change most settings, so set one.

> Re-running `python setup.py` later is safe. It **merges** into your existing `.env` and re-detects tool paths. Each prompt pre-fills your current value, and secrets show `••••••` and keep their stored value on Enter.

> Run setup with **qBittorrent closed** so it picks up the new config on its next launch.

**Jackett on another PC?** Enter its address (e.g. `http://192.168.1.50:9117`) when prompted for the Jackett URL. `setup.py` won't install or register Jackett locally, and `run.py` only checks that the remote one is reachable.

---

## 2 — Start everything

```powershell
python run.py
```

`run.py` relaunches itself inside `.venv` and then starts:

- **VPN check**: verifies your VPN per the [kill-switch mode](#3--set-up-the-vpn-kill-switch), and warns and asks to continue if it's down.
- **qBittorrent**: starts minimized, but only while the VPN is up. A watchdog keeps it gated on the VPN from then on.
- **Jackett**: local: starts the service or binary. Remote: checks it's reachable.
- **FlareSolverr**, if you installed it (see [step 5](#flaresolverr-optional--only-for-cloudflare-protected-indexers)).
- **VLC**, with its HTTP interface on port **8080**, used as the TV fallback player (see [Watching](#watching)).
- **Windows Firewall**: adds inbound rules for the dashboard ports and mDNS (needs admin; warns if not elevated).
- **Dashboard**: serves on **port 80** (HTTP) and **port 443** (HTTPS, for the admin panel), on every network interface so phones on your LAN can connect.

Open the dashboard at **http://localhost**, or from another device on the network at `http://<this-pc-LAN-ip>` or `http://remote.local`. The admin panel is at **https://localhost/admin**.

Press **Ctrl+C** to stop the dashboard. qBittorrent and Jackett keep running.

> Steps 3–6 are **manual** and must be done once before the tool works end to end. `run.py` starts the apps, but it can't log into your VPN or know your Jackett indexers and API keys.

---

## 3 — Set up the VPN kill switch

While the kill switch is on, StreamLink checks your VPN every 3 seconds. If you're not protected, it **kills qBittorrent**, puts a red warning over the dashboard, and refuses new downloads until you reconnect. Playback of what's already downloaded is unaffected. **You choose how it verifies the VPN** in **Admin → VPN Kill Switch**:

- **Mullvad** (default): runs `mullvad status` and requires `Connected`. `setup.py` installs the Mullvad app but can't log in for you. Open **Mullvad VPN**, enter your **account number**, **connect**, and make sure the Mullvad **CLI** is on PATH (the installer adds it).
- **Generic VPN**: works with any VPN provider. You count as protected whenever a VPN tunnel interface (WireGuard, OpenVPN, NordLynx, …) is up. No vendor CLI needed.
- **Off**: disables the kill switch. qBittorrent runs freely, and the dashboard shows a small "VPN OFF" pill instead of the overlay. Only pick this if you're protected another way, e.g. a VPN on your router.

---

## 4 — Verify the qBittorrent Web UI

`setup.py` *tries* to configure this by writing `qBittorrent.ini`. That only works when qBittorrent is **local and was closed** during setup, and qBittorrent can overwrite the file when it exits. **Always verify.** Configure it by hand if qBittorrent is on another machine or the auto-config didn't stick.

In **qBittorrent → Tools → Preferences → Web UI**, confirm:

1. **"Web User Interface (Remote control)" is enabled.**
2. **Port** matches `QBIT_URL` in `.env` (default **8081**).
3. **Username / password** match `QBIT_USERNAME` / `QBIT_PASSWORD` in `.env`.
4. **"Bypass authentication for clients on localhost"** is on, and CSRF protection is off, so the dashboard can talk to it locally.
5. The **default save path** points at your download folder.

Restart qBittorrent after any change. Quick check: open `http://localhost:8081` and log in with your credentials.

---

## 5 — Configure Jackett

Adding indexers needs Jackett's own web UI, so this can't be automated.

1. Open Jackett at the URL you configured (default **http://localhost:9117**).
2. **Add one or more indexers** for the content you want.
3. Copy the **API Key** (top-right of the Jackett dashboard).
4. Paste it into `.env` as `INDEXER_API_KEY=…`, or re-run `python setup.py` and enter it at the prompt.

Until an indexer is added and the API key is set, searches return `Indexer unreachable`. If you set a Jackett admin password, give it to StreamLink too (`JACKETT_PASSWORD`) so **Admin → Indexers** can manage indexers for you.

### FlareSolverr (optional — only for Cloudflare-protected indexers)

Some indexers sit behind a Cloudflare / DDoS-Guard browser challenge and fail in Jackett with a "challenge" error. **FlareSolverr** is an optional proxy that solves those. Most indexers don't need it, so only set it up if one is failing this way.

1. In the admin panel, open **Indexers → FlareSolverr** and click **Install FlareSolverr** (Windows + Linux only; on macOS, run it via Docker). StreamLink downloads the portable bundle and starts it.
2. Copy the **FlareSolverr API URL** shown on that card (default **http://localhost:8191**).
3. In Jackett, click the **cog (Configure Jackett)**, paste the URL into **FlareSolverr API URL**, and **Save**. StreamLink can't set this for you, because Jackett has no API for it.

`run.py` relaunches FlareSolverr on every startup once it's installed.

---

## 6 — Add a TMDb API key

Get a free key at [themoviedb.org/settings/api](https://www.themoviedb.org/settings/api), then paste it into **Admin → Indexers → TMDb Metadata** (or set `TMDB_API_KEY` in `.env`).

It is optional but strongly recommended. TMDb powers the poster search, the Explore tab, bookmarks, artwork, and episode names and numbering in the library. Without a key, search falls back to plain Jackett results, and episodes show their file names.

---

## Watching

Every show and film in the library can be played three ways. Pressing **Play** asks where.

- **On TV** plays on the host's screen, in a fullscreen player the host runs in Chrome/Edge. You control it from any phone's dashboard, the dashboard on the TV itself, or an [air-mouse remote](#wireless-remote-control-optional). Before an episode plays, StreamLink **preps** it: ffmpeg turns the file into a streaming bundle that any browser or phone can play. The same bundle serves the TV, browsers and the iPhone app. An episode that isn't prepped yet is converted on the fly.
- **On this device** plays in the browser you're using: phone, laptop or tablet.
- **On the iPhone app**, which adds offline downloads, lock-screen playback, AirPlay and Chromecast.

### VLC: the fallback player

VLC is no longer the main player. `run.py` still starts it, because the TV player **falls back to VLC** when a file can't be prepped or streamed. It also plays anything that plays straight from the torrent while it's still downloading.

The browser-based player trades a few things for working everywhere: stereo AAC audio, HDR flattened to SDR, seeks that snap to about ±6 s, and no image-based (PGS/VOBSUB) subtitles. If your TV has a 5.1 system or you want HDR passed through, turn on **User Settings → Always Use VLC on the TV** and every TV playback will use VLC instead.

For VLC to work, two things need a person, once:

1. **Clear VLC's first-run dialog.** On a brand-new VLC install, the privacy/network-access prompt blocks the HTTP interface until it's dismissed. Open VLC once and accept it.
2. **Keep the password in step.** `VLC_PASSWORD` in `.env` is what `run.py` launches VLC with. If you change it, restart via `run.py`.

If port **8080** is taken, change `VLC_URL` in `.env`. It must not collide with qBittorrent's 8081.

---

## Profiles: Simple vs Full

Everyone who watches gets a profile, and each profile gets one of two interfaces.

- **Simple** is the default for every profile. Search for a show, pick it by its poster, and press **Get this season**: StreamLink finds the best copy and downloads it. There are no seeder counts, no choice between season packs and single episodes, no download or prep priorities, and no storage settings. Leave the household on this one.
- **Full** adds the advanced controls: per-episode release pickers, download/prep schedules and priorities, hash rechecks, bulk selection, storage paths and per-drive free space.

Profiles marked **elevated** (the ones that can see content-locked items) default to Full, and every other profile defaults to Simple. To change one, open the profile menu → **Manage profiles** and use the **Simple / Full** button on that profile's row. Changing it needs the admin password or a PIN-verified profile, so nobody can promote themselves.

Simple mode only hides controls; it is not a permission system. Use a **PIN** plus the content lock (in the admin panel) for anything that must actually be restricted.

---

## iPhone app (optional)

The StreamLink app streams your library, keeps playing with the phone locked, sends video to a TV with AirPlay or Chromecast, and downloads episodes to watch offline. It is a client: it connects to this host and does nothing on its own.

**Install it from SideStore** (or AltStore), no build needed:

1. Install [SideStore](https://sidestore.io) on the iPhone.
2. In SideStore, open **Sources**, tap **+**, and add:
   ```
   https://raw.githubusercontent.com/nmautz/streamlink-ios/main/apps.json
   ```
3. Install **StreamLink** from that source. New versions show up as updates in SideStore.

That source matches a host on the `main` branch, which is what `setup.py` gives you. If you have switched the host to `beta` or `alpha` (Admin → Updates), add that channel's source instead, so the app is never newer than the host: replace `apps.json` in the address with `apps-beta.json` or `apps-alpha.json`. If the app says a server is out of date, update the host from Admin → Updates.

Open the app and tap your server in the list. The app finds it on its own when the phone is on the same Wi-Fi as the host; iOS asks once for permission to find devices on your local network, so allow it. If the server isn't listed, type the host's address (e.g. `192.168.1.20`) in the box underneath. It connects over plain HTTP on your home network, so no certificate setup is needed.

There is no login in the app: anything that can reach the host can use it, the same as the browser dashboard. Keep the host off the open internet. To use it away from home, reach it over a VPN such as Tailscale: with Tailscale on, the app lists a host that advertises its home subnet, and any host it has already connected to at home.

To build the app yourself instead, see [ios-app/README.md](ios-app/README.md).

---

## Ports & credentials (you choose these)

StreamLink doesn't invent ports or passwords for you. **You pick them in `setup.py`, and the same values must be set inside each app and in `.env`.** A mismatch is the most common reason a fresh install "doesn't work."

| Service | Default port | Credential | Must match between |
|---------|-------------|------------|-------------------|
| Dashboard | `80` (HTTP) | none | none |
| Admin panel | `443` (HTTPS) | `ADMIN_PASSWORD` | `.env` only (blank disables the panel) |
| qBittorrent Web UI | `8081` | `QBIT_USERNAME` / `QBIT_PASSWORD` | qBittorrent → Preferences → Web UI ↔ `.env` |
| Jackett | `9117` | API key (+ optional admin password) | Jackett dashboard ↔ `.env` |
| VLC (Lua HTTP) | `8080` | `VLC_PASSWORD` | VLC launch flags ↔ `.env` (`run.py` handles this) |
| FlareSolverr (optional) | `8191` | none | Jackett's FlareSolverr setting |

Rules:

- **No two services may share a port.** `setup.py` warns if qBittorrent and VLC collide.
- If you change a port, change it in **both** the app's own settings **and** the matching `.env` URL (`QBIT_URL`, `INDEXER_URL`, `VLC_URL`), then restart that service.
- Passwords are your choice. The factory defaults (`adminadmin` for qBittorrent, `vlcpassword`) are placeholders: **change them**.

---

## Wireless remote control (optional)

You can drive playback from the couch with a cheap **air-mouse wireless remote**. Plug its 2.4 GHz USB dongle into the StreamLink host and it works right away: no pairing, no configuration. These remotes show up to Windows as an ordinary keyboard and mouse, and StreamLink listens for their media keys globally.

**Supported remotes:** anything that sends standard media keys, which is nearly every air-mouse / HTPC remote sold for TV boxes, for example:

- Air Fly Mouse (all variants)
- MX3 / MX3 Pro air mouse
- W1 / W2 wireless air remote
- Rii i8 / i8+ mini-keyboard remotes
- G7 / G20 and similar 2.4 GHz "TV box" remotes

**What each button does.** These apply whenever something is playing on the TV (the TV player, VLC or YouTube-on-TV). When nothing is playing, the buttons are left alone.

| Button | Action |
|--------|--------|
| ⏯ Play/Pause | Toggle pause |
| OK (or a click in pointer mode) | During playback: same as ⏯. In the TV UI: activates the highlighted item |
| Vol + / Vol − | Volume up / down 5 % per press (hold to ramp). Respects the admin max-volume cap. **Never changes the host's system volume** |
| ⏭ (next track) | Skip forward 10 s |
| ⏮ (previous track) | Skip back 10 s |
| ← Back | During playback: stop and return to the TV UI. In the TV UI: go back one step |
| 🏠 Home | Stop whatever is playing and open the **TV UI** (below). Without StreamLink this key would open the default browser; it's captured while StreamLink runs |
| Arrow ring / mouse pointer | Navigate the TV UI: arrows move the highlight between buttons and tiles, OK selects, and the pointer keeps working as a mouse. Any of them wakes the UI when idle |

### TV UI

The host also runs a **fullscreen dashboard kiosk**: the normal web UI in a borderless Chrome/Edge window, laid out like a streaming-stick home screen.

- **Idle**: the [background video](docs/ADMIN.md) plays fullscreen, and the TV UI stays out of the way.
- **Press any button** (or click) on the remote and the dashboard appears. Browse with the arrow ring + OK or point and click with the air-mouse, and play something to take the screen.
- **No input for 2 minutes** with nothing playing hands the screen back to the background video.
- **🏠 Home during playback** stops playback and brings the dashboard up.

No setup needed: the kiosk launches itself on the first button press. On the first wake, pick a profile with the pointer; it's remembered.

Notes:

- **Windows**: the handled keys are consumed while StreamLink uses them, so the Windows volume overlay won't pop and Home won't open Edge. Everything else passes through.
- **Linux/macOS**: the keys are observed but can't be selectively consumed, so your desktop may also react to the volume keys, and the Home button isn't available. On macOS you must grant Python the **Input Monitoring** permission, or the TV UI never wakes.
- Set `REMOTE_CONTROL=0` in `.env` to turn the listener off, `TV_UI=0` to disable just the kiosk, and `TV_UI_IDLE_SECS` to change the hand-back delay. Details in [docs/REMOTE.md](docs/REMOTE.md).

---

## Unattended restarts (optional)

For the box to come back to a running StreamLink on its own after a reboot (e.g. the admin **System → Scheduled Restart** feature, or an update), you need **two** things:

1. **Enable Windows auto-login** so the user account signs in without a password prompt after a reboot. The service only starts once that user session exists.
   - Run `netplwiz` and untick *Users must enter a user name and password…*, **or** use Sysinternals **Autologon**.
2. **Install the StreamLink system service** so it relaunches on login (setup offers this; you can also do it later):
   ```powershell
   python run.py --install     # registers the Windows Task Scheduler task (UAC prompt)
   python run.py --status      # confirm it's registered
   python run.py --uninstall   # remove it
   ```

Without both, a reboot leaves the dashboard offline until you run `python run.py` by hand.

---

## Updates

StreamLink updates itself from GitHub. In **Admin → Updates**, choose a branch and how often to check:

- **main**: the stable releases. Use this.
- **beta**: newer, may have issues.
- **alpha**: newest, may break StreamLink and need a manual reinstall to recover.

Applying an update pulls the code, re-runs `setup.py` non-interactively to pick up new dependencies, and restarts the service. You can also update by hand: `git pull`, then `python setup.py`, then restart `run.py`.

---

## Security & network boundary (read this)

StreamLink is a **trusted-LAN home appliance**. Three things follow:

- **The dashboard has no login.** It listens on every network interface, and the main UI is open to anyone who can reach the host's IP. That's what lets phones and the TV connect with no setup. **Keep it on your home network. Never port-forward its ports (80/443) to the internet.** Only the `/admin` panel is password-gated (`ADMIN_PASSWORD`). To reach it away from home, use a private network like Tailscale rather than an open port.
- **Two things need a profile PIN**, and they're the two that can lose you content. One is seeing anything you've marked **Admin only** in the Content Lock tab. The other is **deleting** anything: library items, files, profiles, storage paths. Deleting a library item takes the media off disk by default. Set a 6-digit PIN on any profile that should be allowed to do either. A profile with no PIN can do neither, whatever its Elevated toggle says. Entering the PIN gives that browser a 12-hour session. This is a boundary against other people on your network, not against someone at the host: the PIN hashes live in `library.json` and there's no lockout on repeated guesses.
- **The TLS cert is generated per machine.** `cert.pem` / `key.pem` / `ca.pem` are created locally by `setup.py` and are **git-ignored: never commit them**. Early builds accidentally shipped a shared cert; re-running `setup.py` detects that one and regenerates a unique cert.

## Trusting the HTTPS certificate (optional)

The admin panel uses the machine's self-signed cert, so browsers show a warning. To remove it, add `ca.pem` (generated in the project folder by `setup.py`) to the Windows trust store:

```powershell
# elevated PowerShell
Import-Certificate -FilePath .\ca.pem -CertStoreLocation Cert:\LocalMachine\Root
```

`setup.py` prints the exact command for your platform. The iPhone app doesn't need this; it connects over HTTP.

---

## Configuration reference

All settings live in `.env`, generated by `setup.py`; see `.env.example` for the full list. Most day-to-day settings (VPN mode, TMDb key, storage, Smart Skip, updates) live in the admin panel, not here.

| Variable | Default | Notes |
|----------|---------|-------|
| `INDEXER_URL` | `http://localhost:9117` | Jackett. Accepts a remote `http://host:port` |
| `INDEXER_API_KEY` | _(empty)_ | Paste from the Jackett dashboard |
| `INDEXER_CATEGORIES` | `0` | `0` = all; `2000` = Movies; `5000` = TV |
| `JACKETT_PASSWORD` | _(empty)_ | Only if Jackett has an admin password; lets the admin panel manage indexers |
| `TMDB_API_KEY` | _(empty)_ | Can also be set in Admin → Indexers → TMDb Metadata |
| `FLARESOLVERR_URL` | `http://localhost:8191` | Only used with FlareSolverr |
| `QBIT_URL` | `http://localhost:8081` | qBittorrent Web UI |
| `QBIT_USERNAME` / `QBIT_PASSWORD` | `admin` / `adminadmin` | Set during setup |
| `QBIT_DOWNLOAD_PATH` | _(under your user folder)_ | Where downloads are saved (created automatically). If you add more folders, a download you don't give a folder goes to whichever has the **most free space** |
| `LIBRARY_PATH_2` / `_3` / `_4` | _(empty)_ | Optional extra storage folders, typically on other drives. They show as one-tap destinations in the download dialog and share the free-space auto-pick. You can also add folders in the dashboard's **Storage** view |
| `ADMIN_PASSWORD` | _(empty)_ | `/admin` panel. **Blank disables the admin panel** |
| `VLC_URL` | `http://localhost:8080` | VLC Lua HTTP interface (fallback player) |
| `VLC_PASSWORD` | `vlcpassword` | What `run.py` launches VLC with |
| `BUFFER_MIN_MB` | `15.0` | When playing a file that's still downloading in VLC, start once this many MB are in… |
| `BUFFER_MIN_PCT` | `1.0` | …or once this % of the file is in |
| `REMOTE_CONTROL` | `1` | Air-mouse remote media keys. `0` disables (see [Wireless remote control](#wireless-remote-control-optional)) |
| `TV_UI` | `1` | The dashboard kiosk on the TV. `0` disables |
| `TV_UI_IDLE_SECS` | `120` | Seconds of no remote input before the TV UI hands back to the background video |
| `REMOTE_VOLUME_GUARD` | `0` | Advanced: set `1` only if the remote's volume buttons change the **Windows system volume** while StreamLink runs |
| `WINDOWS_ADMIN_USER` / `WINDOWS_ADMIN_PASSWORD` | _(empty)_ | Windows only, optional. Lets StreamLink set the physical **power/sleep buttons to "Do nothing"** so a stray press can't suspend the host mid-film. `run.py --install` does this while elevated, so most installs never need these. Set them (here or in Admin → Updates) only if launches keep warning that the buttons couldn't be disabled |

> **Where downloads go.** A download you don't give a folder lands in whichever configured folder has the **most free space**, so one drive can't fill up while another sits empty. The download dialog shows the choice (`Auto — most free space`), and you can override it. If a folder is a fine place to *keep* media but a bad place to *put* new downloads (a NAS, an archive drive), open **Storage** in the dashboard and click its **Auto** button to opt it out. That needs a profile PIN or the admin password.

---

## How it works

```
Search → TMDb (real shows and films, by poster)
  ↓
Pick a show → its seasons and episodes from TMDb
  ↓
"Get this season" (Simple) or pick a release (Full) → Jackett finds releases
  ↓
qBittorrent downloads it (only while the VPN is verified)
  ↓
Library → files matched to TMDb seasons/episodes, Smart Skip finds intros and credits
  ↓
Prep → ffmpeg builds a streaming bundle (H.264 + AAC, several qualities)
  ↓
Play → On TV (host's fullscreen player, VLC as fallback), in a browser, or in the iPhone app
```

**VPN kill switch:** every 3 s the backend verifies the VPN using the mode set in **Admin → VPN Kill Switch**. Unless it's Off, when the VPN isn't verified, qBittorrent is killed, a full-screen red warning covers the dashboard, and new downloads are refused until the VPN reconnects. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) and `vpncheck.py`.

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `Indexer unreachable` | Add an indexer in Jackett and set `INDEXER_API_KEY`; confirm `INDEXER_URL` is correct |
| Remote Jackett not reachable | Confirm it's running on the other PC and its port is reachable from this machine |
| Search shows plain text results, no posters | Add a TMDb API key ([step 6](#6--add-a-tmdb-api-key)) |
| qBittorrent Web UI won't respond | Close qBittorrent, re-run `python setup.py`, relaunch |
| "VPN DISCONNECTED" overlay | Reconnect the VPN. The overlay clears automatically |
| `mullvad CLI not found` | Reinstall Mullvad or add its CLI to PATH, then re-run `setup.py`. Or switch to **Generic** mode |
| `/admin` returns an error or is disabled | Set `ADMIN_PASSWORD` in `.env` (blank disables the panel) and restart |
| TV playback fell back to VLC and VLC shows nothing | Open VLC once to clear its first-run dialog; confirm `VLC_PASSWORD` in `.env` matches |
| No sound in 5.1 / HDR looks washed out on the TV | Expected for the browser player. Turn on **User Settings → Always Use VLC on the TV** |
| Dashboard won't bind / no firewall rule | Run the terminal **as Administrator** so `run.py` can add firewall rules and bind ports 80/443 |
| Service doesn't restart after reboot | Confirm Windows **auto-login** is on and `python run.py --status` shows the task registered |
| Server unreachable or slow | See [docs/DIAGNOSTICS.md](docs/DIAGNOSTICS.md); the logs are in `logs\` and in Admin → System → Server Logs |

---

## Linux / macOS notes

The same `python3 setup.py` / `python3 run.py` flow works, with these differences:

- **App installs**: Linux prints a hint (packaging varies by distro); macOS uses Homebrew casks.
- **Service**: Linux = systemd user unit; macOS = launchd agent. Use `python3 run.py --install`.
- **Auto-login**: Linux = display-manager autologin (or `loginctl enable-linger $USER` for headless); macOS = System Settings → Users & Groups → *Automatically log in as…* (needs FileVault off).
- **Privileged ports**: binding 80/443 needs root, so run `sudo python3 run.py`.
- **macOS** has a TCC limitation that blocks some HLS playback. Treat it as a development host only.

---

## More documentation

Deeper reference docs live in [`docs/`](docs/): architecture, backend and frontend maps, the full API, the admin panel, Smart Skip, streaming and prep, the iPhone app, and gotchas. Start with [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
