# Frontend

Vanilla JS, Tailwind CDN, no build step. Two pages: `static/index.html` (main dashboard) and `static/admin.html` (admin panel).

## Design system

Metro UI throughout — flat tiles, no rounded corners, bold uppercase typography, accent stripes, sharp dividers. No `backdrop-blur`. All status dots are square. Modals are bottom-sheets on mobile, centered on desktop.

## `static/index.html` (3608 lines)

### HTML structure (in order)

| Lines | Section |
|------:|---------|
| 56–73   | VPN-disconnected full-screen overlay (toggled by `renderVpn`) |
| 75–116  | Profile PIN prompt modal (numpad, hidden keyboard input) |
| 118–151 | Change PIN modal |
| 153–218 | Profile settings modal (auto-skip toggles, resume mode, global max-volume, change-PIN button) |
| 220–240 | Profile picker (full-screen on first load). Acts as a lock screen: a `body:has(#profilePicker:not(.hidden))` rule in `<style>` hides the player footer, skip/resume offers, and `#localPlayer` while the picker is open, so background playback chrome doesn't bleed through over the bottom-row login buttons on short mobile viewports. |
| 242–274 | Profile add/delete modal |
| 276–372 | Download modal (with metadata fields + file picker) |
| 374–447 | Upload modal (drop zone, progress bar) |
| 449–480 | Storage paths modal |
| 482–550 | Episode page (full-screen, Netflix-style — hero / season tabs / episode cards / sticky action bar). Replaced the legacy bottom-sheet modal in Milestone 12 |
| 552–575 | Stream file picker modal (`/api/stream/prepare` picker) |
| 577–609 | Subtitle search modal |
| 611–615 | Global toast (visible from any tab — sits under navbar). `top-24 sm:top-16` because the mobile navbar is two rows. |
| 618–671 | Navbar (tabs, VPN pill, SSE dot, profile avatar, settings gear). On mobile portrait the row is `flex-wrap`: row 1 = logo + status/profile/settings, row 2 = the three tabs (each `flex-1`, full-width). `sm:` and up collapses back to a single row. `ml-auto` on the right cluster doubles as the desktop spacer. Tab order swaps via `order-3 sm:order-2` on tabs and `order-2 sm:order-3` on the right cluster. |
| 677–750 | Search tab + Library tab containers |
| 752–793 | Skip / Resume offer floating tiles |
| 796–939 | Player footer (seek bar, track selectors, controls row) |
| 942–1159| Fullscreen controls overlay (5-row tile grid + safe-area handling) |
| 1162–end | All JavaScript |

### Top-level state ([static/index.html:1175](../static/index.html#L1175))

```js
let app = { vpn_secure, vpn_status, stream_status, active_title, progress,
            downloaded_mb, total_mb, dl_speed_bps, ul_speed_bps,
            vlc_time, vlc_duration, vlc_volume,
            library_playlist_count, library_current_index, library_current_file,
            library_playlist, library_item_id,
            library_item_file_count, is_library_playback,
            play_when_ready_item_id, play_when_ready_file_path,
            skip_offer, resume_offer };
```
- `profile` — currently selected profile object (persisted to `localStorage.streamlink_profile`)
- `allProfiles` — fetched list from `/api/profiles`
- `activeTab` — `"search"`, `"library"`, or `"offline"`
- `expandedDownloads: Set<itemId>` — which library download cards have file list expanded
- `downloadFilesData: Map<itemId, files[]>` — cached file lists
- `libDownloadStats: Map<itemId, payload>` — latest `library_progress` event per item
- `epChecked`, `epFiles` — selection state for the episode picker modal

### SSE event handlers ([static/index.html:3494](../static/index.html#L3494))

Single `EventSource('/api/events')`. Handlers:
- `state` — full snapshot; `Object.assign(app, d)`, then `renderVpn` + `renderPlayer` + `updateDlBadge`
- `vpn_status` — show alert; update VPN pill + overlay
- `stream_status` — phase transitions (`buffering`/`playing`/`error`/`idle`); push progress fields
- `library_progress` — per-item dl speed/ETA (~every 5 s while item is downloading). Stored in `libDownloadStats` and rendered into `#dl-stat-<itemId>`
- `library_update` — item status changed (`downloading`→`ready`/`error`); triggers `loadLibrary()` if on the Library tab
- `progress_saved` — quiet refresh of the library tab so watch-progress bars update. If the episode picker is open for the same item, also calls `refreshEpFiles()` so the picker never displays stale watch data once the server has new state

### Key render functions

- `renderPlayer(s)` — drives the footer + fullscreen overlay. The seek bar shows **VLC position** when `stream_status==="playing"` and `vlc_duration > 0`; otherwise download progress. On mobile (<768 px) the fullscreen overlay auto-opens on **buffering OR playing** (not just `playing`) so slow-network Play taps get immediate visible feedback. The overlay is **never auto-closed** on `stream_status==="idle"` — the server can take seconds to publish the next track, and the volume slider must stay reachable during that gap. Closing the fullscreen is manual only (the X button, which sets `_fcDismissed=true`). For library plays the buffering badge says **"Loading…"** and the fullscreen status says **"Starting playback…"** (instead of the misleading "Buffering…" / "Connecting…" copy — those imply network work, but the file is already local).
- `renderSkipOffer(offer)` / `renderResumeOffer(offer)` — manage the floating amber/blue offer tiles.
- `renderLibrary(items)` — groups items by `series`, renders cards with Play/Resume/Delete; in-progress downloads show a live ETA chip.
- `renderEpList()` — episode picker rows with progress bars, per-episode ▶ button, checkbox selection, watched toggle. The watched toggle is an element-based handler (`epToggleWatched(this)` reading `data-path`/`data-watched`); the prior inline `JSON.stringify(f.path)` form blew up the `onclick="…"` quoting and broke the button silently.
- `refreshEpFiles()` — re-fetches `/api/library/{id}/files` for the open picker and re-renders. Called after `epToggleWatched` and from the `progress_saved` SSE handler. Preserves `epChecked` selections that still exist; does not touch the modal title (so it's safe to call mid-session).
- `setFcTitle(title, filePath)` — sets the fullscreen overlay's title. Uses `parseEpisodeInfo` to extract "S01E04 · Episode Name" when possible.
- `renderVpn(secure, statusText)` — updates the navbar pill + toggles the full-screen red overlay.

### Volume

VLC's volume slider is debounced — `oninput="updateVolumeDisplay"` updates label only, `onmouseup`/`ontouchend="vlcSetVolume"` sends the actual request. This was a fix for VLC lag when scrubbing the slider. Hard cap is the global `settings.max_volume` (fetched once at startup into `globalMaxVolume`, also refreshed when the profile-settings modal opens); `applyMaxVolumeToSliders` enforces it on the slider `max` attribute.

### Optimistic Play UI + in-flight guards

`continueLibraryItem` and `playLibraryFiles` run under `withInflight("play_${itemId}", …)` so a frustrated double-tap during a slow VLC handoff is dropped client-side instead of racing extra `in_play` requests to VLC. Before the fetch they call `_optimisticBuffering(label, itemId)` which:

- Flips `app.stream_status="buffering"` + `is_library_playback=true` immediately and calls `renderPlayer`.
- Seeds `app.active_title` from the optional `label` (e.g. the episode-specific "S01·E04 · Name") so the user sees what's loading, while seeding `_fcAutoTitle` from the cached item title (so the server's confirming state event with the canonical title doesn't trip "new track → re-open fullscreen" and pop the overlay back up if the user has dismissed it).
- Opens the mobile fullscreen overlay so the user always has something on screen while the server is mid-handoff.

If the Play fetch errors, `_revertOptimistic()` restores `stream_status="idle"`. If the fetch succeeds the server's `buffering` → `playing` state events overwrite the optimistic values.

Both functions also bail with a warn-toast if `app._connected === false` (SSE has been disconnected past its 4 s grace timer — see "SSE pill" below).

### SSE pill ↔ `app._connected`

`connectSSE()`'s open / error handlers maintain `app._connected` and the navbar `#sseLabel`. On `error`, a 4 s grace timer runs — brief reconnect hiccups don't flag the app as offline. Once the timer fires, `app._connected=false`, a "Lost connection to host — reconnecting…" toast shows, and Play guards block new actions until SSE re-opens. The pill itself is no longer mobile-hidden — it shows **LIVE** (green) / **OFFLINE** (red) on every viewport so the connection state is always visible.

### Seek bar

Handles click + touch (`handleSeekBarClick`) and pre-click hover tooltip (`handleSeekBarHover`/`Leave`). Calls `POST /api/vlc/seek/to?position_pct=N` — VLC's `seek` uses `val=N%` for absolute and `val=±Ns` for relative. **Don't mix the two**.

### Episode page (`#episodePage`, full-screen)

Replaces the legacy `#episodeModal` (Milestone 12). `openEpisodePicker(itemId, title)` opens it; under the hood it now reveals a full-screen view. Key DOM:

- `#epHero` — backdrop + poster + show title + meta line + 3–4-line overview. Backdrop uses TMDb `/w1280<backdrop_path>`; poster uses `/w342<poster_path>`. Painted by `renderEpHero`.
- `#epSeasonTabs` — one button per detected season (hidden when 0 or 1 season). `epSwitchSeason(s)` updates `epCurrentSeason` and re-renders.
- `#epList` — scrollable episode list. Each card is a 16:9 still (TMDb `/w300<still_path>` or "S01·E02" placeholder), headline `S01·E02 · Episode Title`, 2-line overview, watch-progress bar overlaid on the still, plus inline Watched / Offline / Download buttons. Tapping the still calls `epPlayFrom(idx)`.

State additions:
- `epMetadata` — cached TMDb payload for the open item (or `null`).
- `epMetaImgBase` — TMDb image base URL returned by the metadata endpoint.
- `epCurrentSeason` — visible season number, set by `pickDefaultSeason()` to the season containing the currently-playing file → first season with unwatched episodes → first available.
- `epSeasonList` — sorted positive seasons present in `epFiles`.

Per-episode ▶: `epPlayFrom(globalIndex)` slices `epFiles` from the tapped index forward (using the original full-list index, *not* the per-season filtered index), respects resume position on the first file, plays as a playlist. This means "press play on episode 3" plays 3 → 4 → 5 …, not just 3.

`closeEpisodeModal()` is kept as a back-compat alias for `closeEpisodePage()` so existing callers (refreshEpFiles, mark-watched, keyboard Escape handler) continue to work without changes.

#### TMDb metadata

Loaded in parallel with `/files` inside `openEpisodePicker`. Returns `{enabled, img_base, metadata}`. When `enabled=false` (no TMDb API key configured) or no match was found, the page degrades gracefully:
- No backdrop / poster / stills.
- Episode headlines fall through to `parseEpisodeInfo` (filename parsing).
- Season tabs still work because they're built from `f.season` parsed off disk, not from TMDb.

Admin sets the key under **Admin → Indexers → TMDb Metadata** (`POST /api/admin/settings { tmdb_api_key }`).

### Stream to Device

All Play surfaces (`epPlay`, `epPlayFrom`, `continueLibraryItem`, the
`lib-restart-btn` listener, the per-card "📱 On Device" button) route through
`playLibraryWithChooser(itemId, files, seekTo, label)`. When the host is
reachable it opens `#playChooserModal` — "On TV (VLC)" vs "On This Device".
There is no offline-only fallback path; if `navigator.onLine === false` the
function shows a toast and bails.

"On This Device" calls `lpPlay`, which kicks off `_lpLoadIndex`. That POSTs
`/api/library/{id}/offline-prepare` for the current file and either uses the
returned `video_url` directly (Safari-native MP4 fast path or a cache hit) or
polls `/api/library/offline-job/{id}` while the `#lpPreparing` overlay shows
"Remuxing… 42%" / "Transcoding…". Once the URL is ready, `<video>.src` is set
to it. The browser issues HTTP Range requests (`FileResponse` is range-aware),
so seek-while-streaming works without client-side help.

Subtitle `<track>` elements are wired straight from the `subs[].url` entries
in the prep response (each points at `/api/library/{id}/subtitle` — server
converts SRT→VTT on the fly). Skip-data is fetched in parallel from
`/api/library/{id}/skip-data?file_path=…` and assigned to `lp.skipData` for
the skip-intro / skip-credits logic in `lpEvaluateSkipOffer`.

There is **one** `<video id="lpVideo">` inside `#localPlayer`. The container
has two visual modes toggled via class:
- default (no `.lp-tiny`) — fullscreen overlay, uses native browser `<video>` controls.
- `.lp-tiny` — corner tile (96×56 video + huge fullscreen button + close), repositioned via CSS only (no DOM move, no video re-load).

Single-element design avoids iOS Safari's per-page video budget and the audio
desync that two synchronized videos would create. iOS-friendly: `playsinline`,
`<track>` for VTT subs, no MediaSource.

`saveProgress(itemId, filePath, posSec, durSec)` is called every 15 s by
`#lpVideo`'s `timeupdate` handler and POSTs `/api/library/{id}/progress`.
**Writes with `posSec < 5` or `durSec ≤ 0` are dropped** — the server
recomputes `completed` from `pct = position/duration`, so a t≈0 write would
wipe a watched episode back to unwatched. The same guard lives in
`_lpFlushProgress` (which also fires on `pause`, `seeked`,
`visibilitychange`→hidden, and `pagehide`, using `sendBeacon` for the last
one). `_lpLoadIndex` seeds `lp.lastSaveAt = Date.now()` so the first throttle
window starts at load, giving the resume seek time to land before any save
can fire. There is no offline outbox — a single best-effort POST is the
entire write path.

Per-file Prep state for the picker rows lives in `prepFileState:
Map<offKey, "prepping"|"ready">`. `prepForStreaming(itemId, filePath, fileName)`
POSTs `/offline-prepare`, polls `/offline-job/{id}` to completion, and
flips the row to "Stream Ready". The map is also hydrated from
`/api/library/{id}/prep-status` whenever the picker opens or `/prep-all` runs
on the library card.

The service worker (`static/sw.js`) is now a one-shot eviction stub registered
via `evictLegacyServiceWorker()` so devices that PWA-installed the old build
drop their stale caches. See [STREAMING.md](STREAMING.md) and
[GOTCHAS.md](GOTCHAS.md) for details.

### Handoff to Device (TV → device, time-synced)

`handoffToDevice(btn)` moves an in-progress VLC (TV) library play onto the
browser that tapped the button, resuming at the same position. It:

1. Reads VLC's **live** position from `GET /api/vlc/tracks` (`time`), falling
   back to the ≤2 s-stale `app.vlc_time` from the SSE snapshot.
2. Builds the remaining-playlist tail by slicing `app.library_playlist` from
   `app.library_current_file` forward (so auto-advance keeps going on-device),
   falling back to just the current file.
3. Fires `POST /api/stop` (202 — VLC teardown runs server-side) and immediately
   calls `lpPlay(itemId, files, seekTo, label)`. The device prep/transcode runs
   in parallel with the TV stopping; `lpPlay`'s resume seek is frozen at the
   captured time (applied on `loadedmetadata`), so the handoff lands on the same
   frame regardless of prep latency.

Wrapped in `withInflight("handoff")` against double-taps. Requires
`app.is_library_playback && app.library_item_id` (both now in the state
snapshot). Two entry points, both shown only during library playback by
`renderPlayer`: the footer **Device** button (`#handoffBtn`, next to Stop) and
the fullscreen **To Device** tile (`#fcHandoffBtn`, next to the Stop tile). Both
are **hold-to-activate** (`_holdStart(this, handoffToDevice, event)` + the
`.hold-btn` 0.5 s progress fill, same as Stop) so an accidental tap can't pull
playback off the TV — a short tap does nothing.

**Prep gating.** The button is greyed (`.handoff-disabled`) with a "Not prepped
for on-device streaming" note when the current file isn't stream-ready (footer:
via `title`; fullscreen tile: the `#fcHandoffNote` sub-label). Readiness is
`_handoffReadyState(s)` → `true | false | null`: it reads `prepFileState` first
(instant when the file was prepped via the picker / prep-all), else the resolved
result of `_maybeRefreshHandoffReady(s)`, which fetches
`GET /api/library/{id}/prep-status` once per current file and repaints. Only a
*known-not-ready* (`=== false`) greys the button and blocks the click (with a
toast); `null` (unknown) stays clickable to avoid false-blocking. The async
result is cached in `app._handoffReady` / `app._handoffReadyFile` (guarded by
`app._handoffInflightFile`).

`lpHandoffToVlc(btn)` is the reverse — it pushes the on-device play back onto the
TV. It captures the local `<video>` position + remaining playlist tail, calls
`lpStop()` (flushes progress, tears down the device player), then
`playLibraryFiles(itemId, tail, capturedTime, label)` (`POST
/api/library/{id}/play` with `seek_first_to`). VLC plays the original source
seeked to the same moment. The **To TV** button sits in the local player's
fullscreen header next to Stop (part of `.lp-chrome`, hidden in tiny mode).
Guarded by `withInflight("handoff_vlc")`.

### Init ([static/index.html:3569](../static/index.html#L3569))

On `DOMContentLoaded`:
1. Pre-fetches default download path.
2. Wires search input (debounced, 600 ms after 3+ chars).
3. Calls `/api/admin/status`; shows admin link if enabled.
4. Reads `localStorage.streamlink_profile`. If valid profile is restored, connects SSE and goes straight to the dashboard. Otherwise shows the full-screen profile picker first.

## `static/admin.html` (990 lines)

Password-protected at `/admin`. Token stored in `sessionStorage.admin_token` and sent via `Authorization: Bearer …`. The dashboard auto-redirects HTTP → HTTPS for `/admin*` ([main.py:1772](../main.py#L1772)).

### Tabs

1. **Indexers** ([line 95](../static/admin.html#L95)) — `INDEXER_CATEGORIES` override; list of configured Jackett indexers with delete; "Add indexer" modal that lists available indexers from Jackett and renders the config form for each.
2. **Content Lock** ([line 142](../static/admin.html#L142)) — toggle `admin_only` per library item. Profiles can be marked `elevated` to also see admin-only items.
3. **Smart Skip** ([line 155](../static/admin.html#L155)) — list items with their skip-data status; per-item `Analyze` button (force re-run); `Edit` opens inline editor with three numeric fields per file (intro start, intro end, credits start). Manual edits set `analysis.source="manual"` so they survive re-analysis.
4. **Profile PINs** ([line 182](../static/admin.html#L182)) — set/clear PIN per profile (admin overrides current-PIN check); toggle the `elevated` flag.

### Admin SSE ([static/admin.html:483](../static/admin.html#L483))

`ensureAdminSSE()` connects to `/api/events?admin_token=...` to receive `analysis_status` events while a Smart Skip run is in progress — the Smart Skip tab live-updates progress bars.

## See also

- [BACKEND.md](BACKEND.md) — what each endpoint actually does
- [API.md](API.md) — endpoint signatures
- [ADMIN.md](ADMIN.md) — admin auth, indexer flow, content lock semantics
