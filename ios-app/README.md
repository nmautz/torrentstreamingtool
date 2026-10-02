# StreamLink — iOS client app

The native iOS client (Capacitor shell + Swift plugins). Background, design
decisions, and the milestone roadmap live in
[../docs/IOS_APP_PLAN.md](../docs/IOS_APP_PLAN.md). This file is just how to
build and run it.

> **Status:** M2 (`6.0.0-preview.2.1.0`) — offline **download + playback**: a
> per-row Download button (in the dashboard) copies a show's HLS bundle to the
> device (native `BundleDownloader`, foreground URLSession), and the bundled
> **Downloads** screen (`www/downloads.html`) plays it offline via
> `LocalMediaServer` — the connect shell routes there automatically when the host
> is unreachable. Builds on M1 (app shell, online parity, `LocalMediaServer` /
> Gate 1b). Offline progress + sync + conflict resolution arrive in M3–M5.

## What's here

```
ios-app/
  www/                      Bundled web shell (the ONLY bundled web asset):
    index.html              First-run "Connect" screen: lists the servers the phone
                            can reach (ServerDiscovery), probes the saved one →
                            navigates to the host, or to downloads.html when offline
    downloads.html          Offline library: lists downloaded bundles + plays them
                            via LocalMediaServer (works with no network)
    localtest.html          Gate 1b on-device localhost-HLS self-test
    capacitor.js            Vendored @capacitor/core runtime (registerPlugin +
                            Plugins proxy); a no-bundler page needs this to reach
                            native plugins. Re-copy from node_modules on upgrade:
                            cp node_modules/@capacitor/core/dist/capacitor.js www/
    sample-bundle/          A tiny fmp4 HLS bundle the self-test serves
  capacitor.config.json     appId, webDir, allowNavigation
  ios/App/                  Generated Xcode project (open this in Xcode)
    App/App/LocalMediaServer.swift   NWListener static HLS server (MIME + Range)
    App/App/BundleDownloader.swift   Background-URLSession offline bundle download
    App/App/MainViewController.swift Registers the plugins + injects @capacitor/core
                                     into the remote dashboard so it can reach them
    App/App/Info.plist               ATS: cleartext only for 127.0.0.1/localhost
```

The app does **not** bundle the dashboard. Online, the WKWebView navigates to
your running host (`https://<host>:<port>`) and loads `static/index.html` exactly
as a desktop browser does; the Capacitor native bridge persists across that
navigation, so the host page can call the native plugins.

## Prerequisites

- macOS with **Xcode** (+ an iOS platform/runtime installed via Xcode → Settings
  → Components) and an Apple ID for free on-device signing.
- **Node 18+** (`npm`). Capacitor 8 uses **Swift Package Manager** — no CocoaPods.

## Install from SideStore (no build)

In SideStore (or AltStore) go to **Sources → +** and add
`https://raw.githubusercontent.com/nmautz/streamlink-ios/main/apps.json`,
then install **StreamLink** from that source. New versions show up as updates.

**Publishing a version** (on the Mac, `gh` logged in): bump the badge in
`static/index.html`, then run `./publish-ipa.sh`. It runs the full
`build-ipa.sh`, uploads `StreamLink.ipa` as release `v<version>` on
`nmautz/streamlink-ios`, and prepends the version to that repo's `apps.json`.
`--dry-run` builds and prints the entry without publishing. `--notes "…"` replaces
the default notes, which are the version's CHANGELOG heading. The app's metadata
(name, description, icon, tint) lives in `sidestore/source.json`.

## Build & run on a device

```bash
cd ios-app
npm install              # first time only
npx cap copy ios         # copy www/ → ios/App/App/public (regenerate after web edits)
npx cap open ios         # opens ios/App in Xcode
```

In Xcode: select the **App** target → Signing & Capabilities → pick your Team,
choose your iPhone as the run destination, and Run. (Web edits under `www/` are
not live — re-run `npx cap copy ios` and rebuild.)

## First run

1. The **Connect** screen lists the StreamLink servers it can see: on the same
   Wi-Fi, or through a VPN such as Tailscale. iOS asks once for permission to
   find devices on the local network; allow it.
2. Tap your server — the app loads your dashboard. There is no password:
   anything that can reach the host can use it.

If the server isn't listed, type its address (e.g. `http://192.168.1.20`) in
the box underneath and tap **Connect**. A server that has only a Tailscale
`100.x` address and has never been seen on Wi-Fi must be typed once; after
that it is remembered.

**Self-signed host cert:** the host serves HTTPS with a self-signed cert
(`cert.pem`). iOS won't trust it until you install the host's CA. On the device,
open the host's `ca.pem`, then Settings → General → VPN & Device Management →
install the profile, and Settings → General → About → Certificate Trust Settings
→ enable full trust. ATS here stays strict (no LAN cleartext); only `127.0.0.1`
is excepted, for the local media server.

## Gate 1b self-test (localhost HLS)

On the Connect screen tap **"Localhost HLS self-test"** → **Start & play**. This
starts `LocalMediaServer` over the bundled `sample-bundle/` and plays it through a
native `<video>` at `http://127.0.0.1:<port>/master.m3u8`. Confirm playback,
audio, the subtitle toggle, and scrubbing — that proves the M2 offline-playback
path end to end on-device.

## Versioning

Tracks the plan's pre-release scheme: `package.json` `version` and the
`6.0.0-preview.x.y.z` tag move together with the host badge. See
[../docs/IOS_APP_PLAN.md#versioning](../docs/IOS_APP_PLAN.md#versioning).
