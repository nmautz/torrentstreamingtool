# StreamLink — iOS client app

The native iOS client (Capacitor shell + Swift plugins). Background, design
decisions, and the milestone roadmap live in
[../docs/IOS_APP_PLAN.md](../docs/IOS_APP_PLAN.md). This file is just how to
build and run it.

> **Status:** M1 (`6.0.0-preview.1.0.0`) — app shell + online parity + the native
> `LocalMediaServer` (Gate 1b). Offline download/playback/sync arrive in M2–M5.

## What's here

```
ios-app/
  www/                      Bundled web shell (the ONLY bundled web asset):
    index.html              First-run "Connect" screen → navigates to the host
    localtest.html          Gate 1b on-device localhost-HLS self-test
    sample-bundle/          A tiny fmp4 HLS bundle the self-test serves
  capacitor.config.json     appId, webDir, allowNavigation
  ios/App/                  Generated Xcode project (open this in Xcode)
    App/App/LocalMediaServer.swift   NWListener static HLS server (MIME + Range)
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

1. The **Connect** screen asks for your host address (e.g.
   `https://192.168.1.20:8000`). Leave the pairing token blank on a home network
   (it's reserved for remote use in a later milestone).
2. Tap **Connect** — the app loads your dashboard.

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
