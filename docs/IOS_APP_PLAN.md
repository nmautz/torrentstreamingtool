# iOS Client App — Plan for v6.0.0

> **Status:** planning + architecture de-risk. No app/server code written yet.
> This is the implementation plan for the **6.0.0** major release (a new
> top-level capability: a native client app). Pieces ship incrementally; the
> version badge in `static/index.html` + `CHANGELOG.md` get bumped as each lands,
> culminating in **6.0.0** when offline download → playback → sync works
> end-to-end on iOS.

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

## Sequencing

1. **De-risk spike (do first):**
   - **(1a) Mac LAN static-server test — ✅ PASSED (2026-06-18).** A generated
     fmp4 HLS bundle (ffmpeg `testsrc2`+`sine`; master + video + audio renditions
     + `sub_0.vtt`) served from a small Python static server (HLS MIME + `Range`)
     played cleanly on a real iPhone via both the native path (`master.m3u8` in
     Safari) and a `<video>`+`<track>` test page — audio, subtitle toggle, and
     scrub all worked. Confirms the bundle + iOS player work over a plain static
     server. (Stdlib `http.server` is unusable: wrong `.m3u8`/`.m4s` MIME, no
     `Range` — the spike used a custom handler, which doubles as the `NWListener`
     reference.)
   - **(1b) On-device localhost test:** port the static server to `NWListener` on
     the phone; confirm 127.0.0.1 + ATS specifics. *(remaining gate)*
2. Server A1 (manifest) + A2/A3 (sync) + tests.
3. Capacitor skeleton + WebView loading the online UI (instant parity).
4. `BundleDownloader` → `LocalMediaServer` → offline playback wired into B5.
5. `OfflineStore` + sync flush + conflict UI.
6. A4 pairing token; lock down sync/manifest endpoints.

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
- `static/index.html` version badge + `CHANGELOG.md` — bump per piece toward **6.0.0**.
