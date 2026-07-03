# Offline Cached Player — Plan (v8)

> **Status: M1 + M2 + M3 LANDED (code) — `8.0.0`, 2026-07-02. On-device
> verification pending.** All three milestones shipped in one patch: Tailwind
> vendored + `GET /api/player-manifest` + the `?offline=1` boot mode
> (static/index.html, main.py — host-side, no rebuild needed); LMS player mode +
> MIME + nested-dir download fix + `OfflineStore.getProgress` track fields +
> shell routing (`goToCachedPlayer`/`goOffline`) — **needs one full
> `./build-ipa.sh` rebuild**; offline track/resume restore from the native
> store, the Reconnect probe, and the downloads.html freeze banner. Deviations
> from the plan as written: the file-serving endpoint was dropped (the root
> StaticFiles mount already serves every manifest entry — only the manifest
> endpoint was added), and M3's track restore rides `OfflineStore.getProgress`
> (extended to return the saved `subtitleSel`/`localAudioIdx`/
> `localSubtitleIdx`). Verification checklist: §4 per milestone; browser smoke
> test for M1 per §4-M1. See [GOTCHAS.md](GOTCHAS.md) § "Offline cached-player
> mode" for the invariants.
>
> This document remains the design reference for the feature. Each milestone
> lands as a versioned patch (version badge + `CHANGELOG.md` + doc updates per
> [CLAUDE.md](../CLAUDE.md)).

---

## 1. Context & goal

The iOS app has **two players** today:

- **Online**: the WKWebView navigates to the host dashboard
  ([static/index.html](../static/index.html)) — the same player the web uses, with
  every feature (custom controls, quality menu, styled ASS subs, skip-intro,
  progress sync, the in-app Downloads overlay).
- **Offline**: the host-served dashboard can't load with no host, so the shell
  routes to a **bundled parallel player**
  ([ios-app/www/downloads.html](../ios-app/www/downloads.html)) — a bare
  `<video>` page that re-implements a subset (list, resume, subtitles).

The parallel player keeps **drifting** from the dashboard: the styled-ASS
subtitle work had to be built twice (7.14.0 dashboard, 7.15.0 downloads.html) and
*fixed* twice (7.16.5 dashboard, `fix for ass subs` downloads.html; then 7.17.1
in both again). Every future player feature faces the same double work, and the
offline surface is always the stale one.

**Goal:** the app should *always use the same player as the web*. While
connected, the app keeps a **snapshot of the last server's dashboard** (the
"player snapshot") on the device; when the host is unreachable, the native
loopback `LocalMediaServer` serves that snapshot and the WKWebView loads it —
the **same** `index.html`, booting in a new **offline mode** that shows the
existing in-app Downloads overlay and plays downloaded bundles through the
existing 7.17.x local-bundle path.

**Agreed scope decisions:**

- **Offline boot is Downloads-only.** The offline dashboard boots straight into
  the existing Downloads overlay (`_appOpenDashboard`) with library/search/TV
  chrome hidden — those need the host anyway. Full offline library *browsing* is
  an explicit non-goal (§7).
- **`downloads.html` stays as a feature-frozen fallback** — used only when no
  usable snapshot exists (first-ever offline launch, corrupt/incomplete cache).
  No further feature work goes into it.
- **The snapshot refreshes every connect** — a cheap manifest check runs on each
  in-app online boot; a version mismatch wipes and re-downloads, otherwise the
  downloader's size-match resume makes the refresh a no-op heal.

---

## 2. Why this is feasible (verified facts)

These were verified against the code before writing this plan:

1. **The Capacitor bridge is origin-agnostic.**
   `MainViewController.injectCapacitorRuntime()`
   ([MainViewController.swift](../ios-app/ios/App/App/MainViewController.swift))
   injects `capacitor.js` as a `WKUserScript` for **every** page the webview
   loads — it is not gated by URL or origin. A dashboard served from
   `http://127.0.0.1:<port>` therefore gets `window.Capacitor` and all four
   registered plugins (`LocalMediaServer`, `BundleDownloader`, `OfflineStore`,
   `TVRemote`), so the dashboard's entire `isApp` layer works unchanged.
   `capacitor.config.json` already allows navigation anywhere
   (`allowNavigation: ["*"]`).
2. **The in-app Downloads overlay is already offline-capable.**
   `_appRenderDashboard` ([static/index.html](../static/index.html)) renders
   purely from native `_cap.dl.list()` — zero host fetches — and
   `_appPlayDownloaded` → `lpPlay` → the 7.17.0/7.17.1 local-bundle playback
   path (`_appLocalBundle` → `_appStartLocalPlayback`), which streams from the
   loopback server and renders styled ASS subs via the main-thread-prefetch
   octopus path.
3. **Offline progress capture + sync already exist** (M3 of the
   [iOS app plan](IOS_APP_PLAN.md)): `saveProgress` falls back to
   `OfflineStore.saveProgress` when the host POST fails, and
   `_appFlushOfflineProgress` drains it back to the host on reconnect. The
   active profile is already pushed natively while online
   (`_appSyncSetProfile` → `OfflineStore.setProfile`), so an offline boot can
   recover it via `OfflineStore.getProfile()`.
4. **The whole dashboard is plain static content.**
   `app.mount("/", StaticFiles(directory="static", html=True))`
   ([main.py](../main.py) bottom) — `index.html` and every `/vendor/*` asset are
   directly fetchable files. A snapshot needs no build step, only a manifest of
   names + sizes.
5. **`LocalMediaServer` can serve it** — it already resolves **nested** request
   paths under its root with a traversal guard and supports Range. Two gaps,
   both small Swift changes: its MIME map lacks `html/js/css/wasm` (it would
   serve the page as `application/octet-stream`, which won't render), and it is
   **single-instance** (`start()` tears down the previous server), which drives
   the "player mode" design in §3.
6. **`BundleDownloader` can download it** — `download()` accepts arbitrary
   `files:[{name,size}]`, names with slashes create nested dirs, transfers are
   durable/resumable, and files already on disk at the expected size are
   skipped. A snapshot is just a bundle under a sentinel cache key.
7. **The only external dependency is Tailwind** —
   [static/index.html](../static/index.html) line 17 loads
   `https://cdn.tailwindcss.com` synchronously. Offline (and on a
   LAN-without-internet host!) the page renders unstyled garbage without it, so
   it must be vendored host-side regardless of this feature.
8. **Nothing dangerous fires offline by default**: `TVRemote` is inert
   (`_tvSessionActive()` requires `stream_status` playing/buffering; the default
   is `"idle"` and no SSE ever updates it), and the VPN kill-switch overlay
   defaults to hidden (`vpn_secure: true`).

---

## 3. Architecture

### 3.1 The snapshot

A **player snapshot** = the dashboard page + every asset it can lazily load:

```
index.html                          ← the dashboard itself
vendor/tailwind.js                  ← vendored Tailwind (new, see M1)
vendor/hls.min.js                   ← lazy-loaded by _ensureHlsLib
vendor/subtitles-octopus.js         ← lazy-loaded by _ensureLibassLib
vendor/subtitles-octopus-worker.js
vendor/subtitles-octopus-worker.wasm
vendor/libass-fallback-font.ttf
```

It is downloaded by the existing **`BundleDownloader`** under the sentinel key
`__player__` (`itemId = filePath = cacheKey = "__player__"`), so it lives at
`Application Support/StreamLinkBundles/__player__/` and inherits durability,
resume, and `getLocal()/remove()` for free. The server's dashboard **version**
(the `data-ui-version` badge value) is stored in the entry's `meta`, which is
how staleness is detected. The sentinel must be **filtered out** of every
bundle-listing surface: `_appRenderDashboard` (grouping + storage totals),
`_appResumeDownloadQueue`, and `downloads.html`'s list.

New host endpoint **`GET /api/player-manifest`** returns
`{version, files:[{name,size}]}` — `version` is the dashboard version,
`files` enumerates `index.html` + the vendor allowlist above with on-disk byte
sizes. No file-serving endpoint is needed: `baseUrl` for the download is simply
the host origin + `/` (fact 4). Traversal safety comes from the server building
the list itself (an allowlist, not a client-supplied path).

### 3.2 Serving: LMS "player mode"

The page uses **absolute** asset paths (`/vendor/hls.min.js`), and bundles live
*outside* the snapshot dir — while LMS is single-instance and must **never
restart while it is serving the page itself** (a restart would break every
later lazy load). So `LocalMediaServer.start()` gains a **player mode**:

```
start({ playerRoot: <…/StreamLinkBundles/__player__> })
```

- Requests resolve against the snapshot dir (so `/index.html`, `/vendor/*`
  work with the page's natural absolute paths), **except**
- paths under **`/StreamLinkBundles/`** resolve against the bundles storage
  root — every downloaded episode is same-origin at
  `/StreamLinkBundles/<sha>/master.m3u8` (and its `meta.json`, `sub_*.ass`,
  fonts) without ever touching the server.

Both mounts keep the existing traversal guard. The MIME map is extended with
`html/js/css/wasm/svg/ico/map`. Everything else (Range, ACAO, no-store) is
already there.

*Rejected alternative:* rooting LMS at the common parent (Application Support)
— the page's absolute `/vendor/*` paths would 404, and it would expose
`StreamLinkOffline/progress.json` over loopback for no benefit.

### 3.3 Offline boot mode in the dashboard

The shell opens the snapshot as
`http://127.0.0.1:<port>/index.html?offline=1&host=<encoded host url>`.
`static/index.html` gets a boot branch keyed on `?offline=1` (app-gated —
meaningless in a browser):

- **Skip** `connectSSE()`, `fetchProfiles()`, `/api/state`,
  `/api/admin/status`, max-volume, updater/pollers. Pin the connection chrome
  to a static "OFFLINE — downloads only" state instead of the RECONNECTING
  loop.
- **Profile**: synthesize `profile` from `OfflineStore.getProfile()`
  (`{profileId, profileName}` → `{id, name}`); skip the profile picker. If the
  store has no profile (never connected with one), show a minimal notice —
  progress still records under the empty profile the way `downloads.html`
  does today.
- **Chrome**: auto-open the Downloads overlay (`_appOpenDashboard`) and hide
  the tabs/search/TV footer behind an `html.is-offline` class. The overlay's
  close/"Change server" affordances collapse to a single **Reconnect** action.
- **Playback**: bypass `lpPlay`'s `!navigator.onLine` guard and its
  `/files`-expansion + `/prep-status` fetches in offline mode;
  `_appStartLocalPlayback` **does not call `lms.start`** — it builds
  `base = location.origin + "/StreamLinkBundles/" + local.sha + "/"` (the
  player-mode mount) — and `lpStop`/the eager LMS stop **must not stop the
  server** (it is serving the page). The quality menu shows only the on-device
  rung (no `srv:*` options — there is no server). `_lpWarmNextEp` already
  short-circuits to downloaded episodes; the host-prep branch is skipped.
- **Reconnect**: the page probes `host` (from the query param — the loopback
  origin's localStorage is empty, so the URL carries it) on an interval + a
  manual button, and `location.replace(host)` when reachable. Progress synced
  offline flows back through the existing reconnect flush once the real
  dashboard loads.

### 3.4 Refresh + routing flow

```
app launch → shell probes {host}/api/version (2.2 s)
  ├─ reachable   → location.replace(host)            (unchanged)
  │                └─ dashboard online boot (app-gated):
  │                   GET /api/player-manifest
  │                   ├─ version ≠ cached meta.version → dl.remove(__player__)
  │                   └─ dl.download(__player__, baseUrl=origin+"/", files)
  │                     (size-match resume ⇒ unchanged files are no-ops)
  └─ unreachable → getLocal(__player__)
      ├─ complete → lms.start({playerRoot}) →
      │             replace("http://127.0.0.1:<port>/index.html?offline=1&host=…")
      └─ else     → downloads.html          (frozen fallback, unchanged)
```

---

## 4. Milestones

### M1 — Host-side groundwork (no app rebuild; browser-testable)

All host-side, so existing installs get it with a server update.

1. **Vendor Tailwind**: commit the Play-CDN JIT build as
   `static/vendor/tailwind.js`; swap line 17 of
   [static/index.html](../static/index.html) to `/vendor/tailwind.js` (keep the
   inline `tailwind.config` line). Benefits every deployment (LAN without
   internet), not just the app.
2. **`GET /api/player-manifest`** in [main.py](../main.py): dashboard version
   (parse the `data-ui-version` badge or a constant kept beside it) + the
   allowlisted file list with sizes. Pure JSON/file-stat — Windows-first is
   trivially satisfied.
3. **Offline boot mode** in [static/index.html](../static/index.html) per §3.3
   — the `?offline=1` branch, `html.is-offline` chrome, playback guards,
   same-origin bundle base in `_appStartLocalPlayback`, LMS-stop suppression,
   reconnect probe.

**Verification (no device needed):** copy `static/` to a scratch dir, serve it
with `python3 -m http.server` (which has **no** `/api`), open
`http://localhost:8000/index.html?offline=1&host=http://example.invalid` in a
desktop browser → page renders styled (vendored Tailwind), boots to the
Downloads-only chrome with no error spam, no SSE retry loop, no profile picker
(graceful no-profile notice since there's no native store in a browser).
Regression: the normal host-served dashboard behaves identically to today.

### M2 — Native + glue (one `./build-ipa.sh` rebuild)

1. **LMS player mode + MIME**
   ([LocalMediaServer.swift](../ios-app/ios/App/App/LocalMediaServer.swift)):
   MIME map additions; `start({playerRoot})` with the `/StreamLinkBundles/`
   mount per §3.2. No new plugin/pbxproj/`capacitorDidLoad` changes.
2. **Shell routing** ([ios-app/www/index.html](../ios-app/www/index.html)): the
   unreachable branch checks `getLocal(__player__)` and prefers the snapshot
   over `downloads.html` per §3.4.
3. **Snapshot refresh glue** in [static/index.html](../static/index.html)
   (app-gated, online boot): manifest fetch → version compare → remove +
   download per §3.4.
4. **Sentinel filtering**: skip `__player__` in `_appRenderDashboard`,
   `_appResumeDownloadQueue`, and `downloads.html`'s list.

**Verification (on device):** connect once (snapshot downloads — visible in
Xcode console; no `__player__` row in Downloads UIs) → Airplane Mode → relaunch
→ the **same dashboard UI** loads from `127.0.0.1` and boots into Downloads →
play a downloaded episode (styled ASS subs render; dev HUD `source:` row shows
the loopback origin) → progress records → disable Airplane Mode → Reconnect →
real dashboard loads and the offline progress appears on the host (existing
flush). Fallback: fresh install + Airplane Mode → `downloads.html` still opens.
Kill/relaunch offline mid-playback → resume works.

### M3 — Polish + freeze

1. Offline **track-pref restore**: feed the saved `subtitleSel` /
   `localAudioIdx` / `localSubtitleIdx` fields already recorded by
   `OfflineStore.saveProgress` back into the offline player's pending picks
   (the online path uses `/saved-tracks`; offline uses the store).
2. Reconnect UX polish (auto-probe cadence, "back online" toast) and any
   on-device findings from M2.
3. **Freeze `downloads.html`**: banner comment + doc note that it is a fallback
   only; new player features are dashboard-only by design.
4. Docs: update [STREAMING.md](STREAMING.md) (offline section describes the
   cached player as the primary offline surface), [GOTCHAS.md](GOTCHAS.md)
   (player-mode mounts, sentinel filtering, "LMS must never restart while
   serving the page"), and mark milestone statuses in this file.

---

## 5. Risks / open questions

- **WKWebView on a loopback `http://` page**: localStorage works but starts
  empty (why the profile comes from `OfflineStore` and the host URL rides the
  query string); the LMS **port is ephemeral**, so the loopback origin — and
  its localStorage — can change between runs. Nothing critical may live in
  loopback-origin localStorage.
- **Version skew**: the snapshot is one connect old at worst (refresh-every-
  connect); a snapshot page can still call a host API that changed. Mitigation:
  offline mode touches almost no APIs, and the manifest version gate re-syncs
  on the next connect.
- **Same-size asset edits** slip past the size-match heal within one version —
  accepted (the version gate wipes on any release, and every real change bumps
  the version per CLAUDE.md).
- **Tailwind vendoring** pins a Tailwind version; the Play-CDN build is ~400 KB
  and self-contained. Verify no dashboard styles depend on CDN-side updates.
- **Storage**: the snapshot is ~5 MB (wasm dominates) next to multi-GB bundles
  — negligible, but it shows up in `bytesUsed()`; decide in M2 whether storage
  totals exclude it.

## 6. What NOT to touch

- The VLC/TV playback paths, Smart Skip, admin — untouched.
- The M3 sync machinery (`OfflineStore` schema, `/api/sync/*`) — reused as-is.
- `downloads.html` beyond sentinel filtering + the freeze banner.
- The 7.17.x local-bundle playback internals — offline mode only changes how
  `base` is derived and which guards are skipped.

## 7. Non-goals

- Full offline **library browsing** (posters/metadata snapshots) — Downloads
  overlay only.
- Starting new **downloads** offline (nothing to download from).
- Android; multi-server snapshot caching (one snapshot: the last-connected
  host).

---

*Related docs: [IOS_APP_PLAN.md](IOS_APP_PLAN.md) (the v6 app plan this builds
on), [STREAMING.md](STREAMING.md) § offline downloads, [GOTCHAS.md](GOTCHAS.md).*
