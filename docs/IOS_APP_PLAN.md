# iOS Client App — Plan for v6.0.0

> **Status:** **M2 code landed** (`6.0.0-preview.2.0.0`) — offline download +
> fully-offline playback. The host **A1** `bundle-manifest` endpoint
> ([main.py](../main.py)) is implemented and unit-/integration-tested; the native
> **`BundleDownloader`** ([`BundleDownloader.swift`](../ios-app/ios/App/App/BundleDownloader.swift),
> background URLSession) and the web glue ([`static/index.html`](../static/index.html):
> per-row Download button + the `master_url` swap to `LocalMediaServer` in
> `_lpLoadIndex`) are written and JS-syntax-checked. What's left on M2 is the
> **on-device run**: open `ios-app/ios/App` in Xcode, build to an iPhone, download
> a multi-episode item, enter Airplane Mode, and confirm 2 episodes play with
> tracks/subs/skip and survive an app kill + relaunch.
>
> **M1 landed** (`6.0.0-preview.1.0.0`) — Capacitor shell, first-run Connect
> screen, native `LocalMediaServer` (Gate 1b). Its remaining gate is the same
> on-device pass (online parity + the "Localhost HLS self-test").
> Later milestones (M3–M5) remain as planned below.
> This is the implementation plan for the **6.0.0** major release (a new
> top-level capability: a native client app). Pieces ship incrementally; the
> version badge in `static/index.html` + `CHANGELOG.md` get bumped as each lands,
> culminating in **6.0.0** when offline download → playback → sync works
> end-to-end on iOS. See [Versioning](#versioning) for the pre-release scheme.

---

## Versioning

The working release is **6.0.0**. While building toward it, every change uses a
**SemVer pre-release** tag:

```
6.0.0-preview.<x>.<y>.<z>
```

- `<x>.<y>.<z>` keep their normal [CLAUDE.md](../CLAUDE.md) meanings **within the
  preview line** — `x` = major feature, `y` = minor feature, `z` = bug fix — so the
  per-change bump rules don't change; they just live under the `6.0.0-preview.`
  prefix.
- **Use a dot before the triple, not a hyphen.** `6.0.0-preview.1.2.3` is correct;
  `6.0.0-preview-1.2.3` is not. SemVer compares dot-separated pre-release
  identifiers, comparing **numeric** ones numerically — so with dots,
  `6.0.0-preview.1.2.3 < 6.0.0-preview.1.3.0`, and **any** `6.0.0-preview.*` sorts
  before the final `6.0.0` (a pre-release always ranks below the release). The
  hyphen form folds `preview-1` into a single text identifier and breaks numeric
  ordering.
- **Example progression:** `6.0.0-preview.1.0.0` (first preview feature) →
  `6.0.0-preview.1.0.1` (fix) → `6.0.0-preview.1.1.0` (minor feature) → … →
  **`6.0.0`** (drop the suffix at release).
- The version badge in `static/index.html` + a `CHANGELOG.md` bullet are updated in
  the same patch as every change, exactly as today — just carrying the
  `6.0.0-preview.x.y.z` value until release.

> **Simpler alternative (not chosen):** `6.0.0-preview.N`, a single incrementing
> counter. Standard and minimal, but it drops the feature-vs-fix granularity the
> triple encodes — so this project keeps the triple.

---

## Context

StreamLink today is a local web dashboard (FastAPI host + browser UI). The
on-device player in `static/index.html` is already a full HLS player (hls.js +
Safari-native) with progress sync, multi-audio/sub, ABR, reconnect, and
skip-intro — so the **online** experience is already mobile-web-capable
(see [STREAMING.md](STREAMING.md)).

The reason for a client app is **reliable extended-offline** use: download a
season to the phone, play it in-app with no host connection, track watch history
offline, and sync that history back to the server on reconnect — with user
resolution for conflicts that can't be auto-resolved.

A pure web wrapper / PWA **cannot** do this on iOS: web storage is an evictable
cache (Safari/WKWebView purges it under pressure or after ~7 days), there is no
reliable background download, and WKWebView Service Workers are gated/flaky. The
offline core must be native; everything else (search, library, the player UI,
online streaming) is reused as-is.

**Scope: iOS only.** Android is deferred — the chosen design (Capacitor + a
localhost static server) ports cleanly later (NanoHTTPD + `DownloadManager` /
`WorkManager` replace the iOS equivalents; the web UI and all new server
endpoints are shared).

**Platform-priority note:** the iOS app is the only Apple-surface deliverable and
is exempt from the repo's Windows-first rule. **The new *server* endpoints
(bundle-manifest, sync, pairing) run on the host and MUST be correct on the
Windows deployment target** per [CLAUDE.md](../CLAUDE.md).

---

## Decisions (locked)

| Decision | Choice | Why |
|----------|--------|-----|
| App framework | **Capacitor** (WKWebView shell + Swift plugins) | Max reuse of the existing player/library UI. Not React Native / full native (those rewrite the UI). |
| Offline playback | **Reuse the existing web player** via a localhost HLS server | The `.offline_cache/<sha>/` dir is already a self-contained HLS bundle; serve it at `http://127.0.0.1:<port>/` and swap the player's `master_url`. Multi-audio/sub/ABR/skip-intro work unchanged. |
| iOS player engine | **Native HLS** (`<video>.src`), not hls.js | iOS path in [STREAMING.md](STREAMING.md) "Play on this device" step 4. ⇒ the local server must return correct HLS MIME + support `Range`. |
| Embedded server | **Roll-your-own `Network.framework` `NWListener`** | GCDWebServer is archived; a static-file HLS server (GET + MIME + `Range`) is ~150 lines with zero dependency lifetime risk. Fallbacks: FlyingFox / Hummingbird. Avoid Telegraph (stale) / Vapor (overkill). |
| Distribution | **Personal / TestFlight** | Light device-pairing token auth; no App Store review constraints. |

---

## Architecture

```
┌─ iOS app (Capacitor project) ───────────────────────────────────┐
│  WKWebView ── loads static/index.html                            │
│     • ONLINE: points at the host server over HTTPS (as today)    │
│     • OFFLINE: same UI; player master_url swapped to localhost   │
│                                                                  │
│  Swift Capacitor plugins (the only new native code):             │
│   1. BundleDownloader  — URLSession background download of a     │
│                          full .offline_cache/<sha>/ bundle to    │
│                          the app sandbox; resumable; progress    │
│   2. LocalMediaServer  — NWListener static server over the       │
│                          downloaded dir (MIME + Range)           │
│   3. OfflineStore      — on-device progress log + sync/conflict  │
└──────────────────────────────────────────────────────────────────┘
                       │ HTTPS (online only)
                       ▼
        FastAPI host (main.py) — existing + new endpoints
```

**Why the bundle reuse works:** an `.offline_cache/<sha>/` dir is already a
self-contained HLS bundle (`master.m3u8`, variant playlists, `init_*.mp4`,
`seg_*.m4s`, sidecar `sub_*.vtt`, `meta.json`) — see [STREAMING.md](STREAMING.md)
"Output format" and the server at `offline_cache_bundle_file`
([main.py:15638](../main.py#L15638)). Downloading that dir verbatim and serving it
from localhost means the existing player needs only its `master_url` swapped from
`/api/library/offline-cache/<sha>/master.m3u8` to
`http://127.0.0.1:<port>/master.m3u8`.

### Component chart

```mermaid
flowchart TB
  subgraph App["iOS app — Capacitor"]
    WV["WKWebView<br/>static/index.html"]
    DL["BundleDownloader<br/>URLSession (bg)"]
    LS["LocalMediaServer<br/>NWListener"]
    OS["OfflineStore<br/>progress log + sync"]
    FSB[("App sandbox<br/>.../{sha}/ bundles")]
  end
  subgraph Host["FastAPI host (main.py)"]
    MAN["GET bundle-manifest — A1"]
    BUN["GET offline-cache/{sha}/{file}"]
    SYNC["POST sync/progress — A2"]
    RES["POST sync/resolve — A3"]
    PAIR["POST pair — A4"]
    LIB[("library.json")]
  end
  WV -->|online HTTPS| Host
  WV -->|offline: master_url| LS
  LS --> FSB
  DL -->|enumerate| MAN
  DL -->|fetch files| BUN
  DL --> FSB
  WV -->|progress events| OS
  OS -->|batch flush| SYNC
  OS -->|user choice| RES
  SYNC --> LIB
  RES --> LIB
  SYNC -. "Bearer token" .-> PAIR
```

---

## Work breakdown

### A. Server changes (`main.py`, docs) — Windows-correct

The only backend changes, and the bulk of the *novel logic*.

**A1. Bundle manifest endpoint** (small).
`GET /api/library/{item_id}/bundle-manifest?file_path=…` → flat list of bundle
files (relative names + byte sizes + total) plus `duration_sec`, `audios`,
`subtitles` (from `meta.json`) and the sidecar `subs` list, so the iOS downloader
enqueues deterministically with real progress instead of crawling playlists.
- Reuse: `_offline_cache_key(src)`, `_read_meta(out_dir)`, `_list_sidecar_subs`
  — already used by `offline_prepare` ([main.py:15145-15164](../main.py#L15145-L15164)).
- Reuse the existing per-file server ([main.py:15638](../main.py#L15638)) for the
  actual file downloads (already traversal-guarded by `_CACHE_KEY_RE` /
  `_BUNDLE_FILE_RE`); the manifest only enumerates.
- If the bundle isn't built yet → 409 `not_ready`; the app triggers a normal
  `POST /offline-prepare` and polls `/offline-job/{id}` first. (Only fully-prepped
  bundles are downloadable — JIT on-demand stays online-only.)
- Document in [API.md](API.md); reference from [STREAMING.md](STREAMING.md).

**A2. Batch progress sync + conflict resolution** (the real new work).
Today progress is written one file at a time, last-write-wins, **no conflict
detection** — `update_progress` ([main.py:6692-6716](../main.py#L6692-L6716)),
schema in [LIBRARY_DATA.md](LIBRARY_DATA.md) "Progress (per profile)".

Add `POST /api/library/sync/progress`:
```jsonc
{
  "profile_id": "...",
  "events": [
    { "item_id": "...", "file_path": "...",
      "position_sec": 1234.5, "duration_sec": 4174.0,
      "client_updated_at": "2026-06-18T20:01:00Z",  // when watched on device
      "base_synced_at":    "2026-06-17T09:00:00Z",  // device's last sync of THIS file
      "subtitle_sel": {...}, "local_audio_idx": 0 } // optional, preserved
  ]
}
```
Per-event logic:
- **No server entry, or server `updated_at` ≤ `base_synced_at`** → server unchanged
  since the device last synced → **apply** the device event. Reuse the exact merge
  shape of `update_progress`: recompute `completed = pct > 0.92`, preserve sibling
  track keys (`audio_track`/`subtitle_track`/`local_audio_idx`/
  `local_subtitle_idx`/`subtitle_sel`).
- **Server `updated_at` > `base_synced_at`** (both advanced): if positions agree
  within a threshold (≈60 s) OR one side is `completed`, auto-resolve (newest
  timestamp wins; `completed` is monotonic — never un-complete). Otherwise emit a
  **conflict**: return `{server, client}` for that file and write nothing.
- Response: `{ applied:[...], conflicts:[...], server_updated_at }`. The app records
  `server_updated_at` as each applied file's new `base_synced_at`.
- All writes under `_lib_lock` (`get_library`/`put_library` or `_load_lib_raw`/
  `_save_lib_raw`) — process the whole batch under one lock acquisition; never raw
  (see [LIBRARY_DATA.md](LIBRARY_DATA.md) "Concurrency").
- Document the endpoint + the `base_synced_at` watermark in [API.md](API.md) and
  [LIBRARY_DATA.md](LIBRARY_DATA.md).

Per-event decision logic (A2):

```mermaid
flowchart TD
  A["Incoming progress event"] --> B{"Server entry exists?"}
  B -- No --> APPLY["Apply event<br/>completed = pct > 0.92<br/>preserve track keys"]
  B -- Yes --> C{"server.updated_at ><br/>base_synced_at?"}
  C -- "No (server unchanged<br/>since last sync)" --> APPLY
  C -- "Yes (both advanced)" --> D{"positions within ~60s<br/>OR either completed?"}
  D -- Yes --> E["Auto-resolve:<br/>newest timestamp wins<br/>completed is monotonic"]
  D -- No --> F["Conflict:<br/>return server + client<br/>write nothing"]
  E --> APPLY
  F --> G(["Returned in conflicts list<br/>then client resolution UI then A3"])
```

**A3. Conflict-resolve apply** (small).
`POST /api/library/sync/resolve` (or a `force` flag on A2) writes the user-chosen
winner for a previously-reported conflict, bumping `updated_at`.

**A4. Device-pairing token** (light, needed for remote use).
Regular library/progress endpoints take an unauthenticated `profile_id` in the
body (only an *admin* token exists today — [main.py:939](../main.py#L939)).
- `POST /api/pair {admin_password}` → long-lived device token (persisted list,
  same pattern as `_admin_sessions`).
- Accept `Authorization: Bearer <device_token>` on sync + manifest (ideally also
  `/offline-prepare`, `/files`, `/api/library`). Reuse `admin_password` as the
  pairing secret. Can be deferred if first cut is LAN-only, but scope now.

### B. Capacitor iOS project (new; lives in `ios/` or a sibling project)

**B1. Skeleton.** `npx cap init`, add iOS platform, WKWebView loads the host URL
(first-run server-address + token entry). ATS exception for `http://127.0.0.1:*`
only (the localhost player).

**B2. `LocalMediaServer` plugin (Swift).** `NWListener` static-file server.
- `start(bundleDir) -> {baseUrl}` mounts a `<sha>/` dir at `127.0.0.1:<port>`;
  `stop()`.
- Correct MIME (`application/vnd.apple.mpegurl` m3u8, `video/mp4` m4s/init,
  `text/vtt`) — mirror `_HLS_MIME` — and honor `Range`.
- The validated spike's ~90-line Python handler is the reference port.

**B3. `BundleDownloader` plugin (Swift).** `URLSession` background config.
- `download(itemId, filePath)` → call `bundle-manifest`, enqueue every file to a
  non-evictable app-support dir keyed by `<sha>/`, report progress, resumable.
- `list()`, `delete(sha)`, `bytesUsed()` for a Downloads screen.
- Mark dir `isExcludedFromBackup`; store in Application Support (not Caches).

**B4. `OfflineStore` plugin + JS glue.** Local progress log keyed by
`(profile_id, item_id, file_path)` with `position/duration/client_updated_at/
base_synced_at`. A connectivity watcher flushes to `POST /sync/progress` when
online, surfaces conflicts to a resolution UI, calls A3 on resolve.

**B5. Web-UI glue (minimal `static/index.html` edits, behind a Capacitor flag).**
Detect `window.Capacitor`; when present:
- A **Download** affordance per item/episode → `BundleDownloader`.
- In `_lpLoadIndex`, when a bundle is downloaded locally, set `lp.mode="bundle"`
  and point `master_url` at the `LocalMediaServer` base URL instead of POSTing
  `/offline-prepare`. Everything downstream (tracks/subs/skip/progress) unchanged.
- Route `saveProgress`/`_lpFlushProgress` to `OfflineStore` when offline (already
  throttles + flushes on `pause`/`seeked`/`pagehide` — see [STREAMING.md](STREAMING.md)
  "Watch progress"); online path untouched.
- Add a Downloads + conflict-resolution screen.
- Guard every edit so the plain browser UI is byte-for-byte unaffected.

---

## Delivery roadmap — what to build, in what order

Each milestone is an **independently shippable, on-device-testable** increment and
maps to a preview-version line. Build strictly in order: each depends on the one
before. The guiding principle is **value-first, risk-first** — get a usable app on
the phone (M1), then the one thing only a native app can do (offline playback, M2),
then make that history trustworthy (sync M3 → conflicts M4), then make it safe to
use remotely (auth M5).

```mermaid
flowchart LR
  G0(["Gate 1a PASSED<br/>Mac LAN test"]) --> M1
  M1["M1 · preview.1.x.x<br/>App shell + online parity<br/>+ Gate 1b (NWListener)"]
  M1 --> M2["M2 · preview.2.x.x<br/>Offline download<br/>+ playback"]
  M2 --> M3["M3 · preview.3.x.x<br/>Offline progress<br/>+ auto-sync"]
  M3 --> M4["M4 · preview.4.x.x<br/>Conflict resolution"]
  M4 --> M5["M5 · preview.5.x.x<br/>Auth/pairing +<br/>downloads mgmt"]
  M5 --> R(["6.0.0 release"])
```

| Milestone (version line) | What it adds (user-visible) | Server work | App work | Done when |
|---|---|---|---|---|
| **M1** `6.0.0-preview.1.x.x` — App shell + online parity | The app exists: install on iPhone, it *is* the dashboard. Full online experience identical to the browser. | — | B1 Capacitor skeleton + first-run server/token screen. **Gate 1b**: port the spike server to `NWListener`, prove localhost HLS + ATS on-device. | App runs on a real device; online search/library/play/admin all behave exactly as in the browser; a downloaded sample bundle plays from the on-device `NWListener` server. |
| **M2** `6.0.0-preview.2.x.x` — Offline download + playback | **The core feature.** Download an episode/season; play it fully offline (Airplane Mode) with audio, subtitles, ABR, skip-intro. | **A1** bundle-manifest endpoint. | **B3** `BundleDownloader` (bg, resumable) → **B2** `LocalMediaServer` → **B5** Download button + `master_url` swap in `_lpLoadIndex`. | Download a multi-episode item online → Airplane Mode → play 2 episodes with working tracks/subs/skip; files survive app kill + relaunch. |
| **M3** `6.0.0-preview.3.x.x` — Offline progress + auto-sync | Offline watch history is kept and **flows back to the server** on reconnect (auto-resolvable cases only). Resume works offline. | **A2** `sync/progress` (apply + auto-resolve path). | **B4** `OfflineStore` (local progress log, offline resume) + connectivity-watcher flush; route `saveProgress`/`_lpFlushProgress` to it when offline. | Watch offline, reconnect → history appears on the server; device-only and close-position cases merge silently; `completed` never regresses. |
| **M4** `6.0.0-preview.4.x.x` — Conflict resolution | When the same episode advanced both offline **and** elsewhere (e.g. the TV), the app asks the user which to keep. | **A3** `sync/resolve` + surface conflicts from A2. | Conflict-resolution UI (mine vs server) wired to the sync flush. | A deliberately-divergent case (offline + TV) surfaces a conflict and the chosen winner is written and re-synced. |
| **M5** `6.0.0-preview.5.x.x` — Auth/pairing + downloads mgmt + hardening | Safe remote use over the internet; manage/delete downloads and see storage used. | **A4** `POST /pair` + Bearer-token enforcement on sync/manifest (ideally `/offline-prepare`, `/files`, `/api/library`). | Pairing/login screen; Downloads management screen (`list`/`delete`/`bytesUsed`). | Endpoints reject unpaired callers; downloads are listable/removable; end-to-end cycle passes over a real remote connection. |
| **🚀 Release** `6.0.0` | All of the above, integrated and verified. | — | — | Full [Verification](#verification) end-to-end cycle green; docs updated; suffix dropped to `6.0.0`. |

> **De-risk gates (not features):** **1a** (Mac LAN static-server) is ✅ **PASSED
> (2026-06-18)** — a generated fmp4 HLS bundle (ffmpeg `testsrc2`+`sine`; master +
> video + audio + `sub_0.vtt`) served from a small Python static server (HLS MIME +
> `Range`) played on a real iPhone via both native `master.m3u8` and a
> `<video>`+`<track>` page (audio, subtitle toggle, scrub all worked). **1b**
> (on-device `NWListener` localhost + ATS) is folded into **M1** as the gating task —
> the native server ([`LocalMediaServer.swift`](../ios-app/ios/App/App/LocalMediaServer.swift))
> + a bundled sample + a one-tap self-test ([`localtest.html`](../ios-app/www/localtest.html))
> are **written and type-checked**; the on-device pass is the remaining M1 step.

### Core flows

**Offline download (M2):**

```mermaid
sequenceDiagram
  actor U as User
  participant WV as WebView UI
  participant DL as BundleDownloader
  participant H as Host
  participant FSB as Sandbox
  U->>WV: Tap Download
  WV->>DL: download(itemId, filePath)
  DL->>H: GET bundle-manifest?file_path
  alt bundle not built yet
    H-->>DL: 409 not_ready
    DL->>H: POST offline-prepare
    DL->>H: poll offline-job/{id} until done
  end
  H-->>DL: file list + sizes
  loop each file
    DL->>H: GET offline-cache/{sha}/{file}
    H-->>DL: bytes
    DL->>FSB: write into /{sha}/
  end
  DL-->>WV: progress %, then complete
```

**Offline playback + progress capture (M2 → M3):**

```mermaid
sequenceDiagram
  actor U as User
  participant WV as WebView UI
  participant LS as LocalMediaServer
  participant OS as OfflineStore
  U->>WV: Play (offline)
  WV->>LS: start({sha} dir)
  LS-->>WV: baseUrl http://127.0.0.1:port
  WV->>WV: master_url = baseUrl/master.m3u8
  WV->>LS: GET master.m3u8 + segments (Range)
  LS-->>WV: HLS bytes (correct MIME)
  loop timeupdate / pause / seek
    WV->>OS: saveProgress(pos, dur, subtitle_sel)
  end
```

**Sync + conflict resolution (M3 → M4):**

```mermaid
sequenceDiagram
  participant OS as OfflineStore
  participant H as Host
  actor U as User
  Note over OS: device back online
  OS->>H: POST sync/progress { events[] + base_synced_at }
  H->>H: per-event decision (A2 chart above)
  H-->>OS: { applied[], conflicts[], server_updated_at }
  OS->>OS: base_synced_at = server_updated_at (applied)
  alt conflicts present
    OS->>U: resolution UI (mine vs server)
    U->>OS: choose winner
    OS->>H: POST sync/resolve { choice }
    H-->>OS: ok (updated_at bumped)
  end
```

---

## Verification

- **Server endpoints:** unit-test the A2 conflict matrix against the merge logic —
  (a) device-only advance applies; (b) server-newer + close positions auto-resolves;
  (c) server-newer + divergent positions returns a conflict and writes nothing;
  (d) `completed` never regresses; (e) track prefs (`subtitle_sel` etc.) survive a
  sync write. Run on the **Windows** host path (lock usage, no POSIX assumptions).
- **End-to-end offline cycle:** download a multi-episode item online → Airplane
  Mode → play 2 episodes part-way → kill + relaunch (history intact, files durable)
  → re-enable network → confirm sync flushes, an auto-resolvable case merges
  silently, and a deliberately-divergent case (also advanced on the TV) surfaces
  the conflict UI and resolves correctly.
- **No-regression:** load `static/index.html` in a desktop browser; player/library/
  admin behave exactly as before (all native glue is bridge-gated).

---

## Out of scope (this release)
- Android (deferred — design is portable).
- Offline of JIT on-demand / `ondemand_only` items (online-only by nature).
- App Store submission hardening.

---

## Docs to update as pieces land
- [API.md](API.md) — `bundle-manifest`, `sync/progress`, `sync/resolve`, `pair`.
- [LIBRARY_DATA.md](LIBRARY_DATA.md) — `base_synced_at` watermark + conflict semantics on progress.
- [STREAMING.md](STREAMING.md) — offline-bundle download + localhost-server playback path.
- [GOTCHAS.md](GOTCHAS.md) — any iOS ATS / native-HLS / localhost footguns found.
- [CLAUDE.md](../CLAUDE.md) docs index — add this subsystem row once code exists.
- `static/index.html` version badge + `CHANGELOG.md` — bump per piece as
  `6.0.0-preview.x.y.z` (see [Versioning](#versioning)), dropping the suffix at the **6.0.0** release.
