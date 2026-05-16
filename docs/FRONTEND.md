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
| 220–240 | Profile picker (full-screen on first load) |
| 242–274 | Profile add/delete modal |
| 276–372 | Download modal (with metadata fields + file picker) |
| 374–447 | Upload modal (drop zone, progress bar) |
| 449–480 | Storage paths modal |
| 482–550 | Episode picker modal (per-episode play, mark-watched, ZIP download) |
| 552–575 | Stream file picker modal (`/api/stream/prepare` picker) |
| 577–609 | Subtitle search modal |
| 611–615 | Global toast (visible from any tab — sits under navbar) |
| 618–671 | Navbar (tabs, VPN pill, SSE dot, profile avatar, settings gear) |
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
- `progress_saved` — quiet refresh of the library tab so watch-progress bars update

### Key render functions

- `renderPlayer(s)` — drives the footer + fullscreen overlay. The seek bar shows **VLC position** when `stream_status==="playing"` and `vlc_duration > 0`; otherwise download progress.
- `renderSkipOffer(offer)` / `renderResumeOffer(offer)` — manage the floating amber/blue offer tiles.
- `renderLibrary(items)` — groups items by `series`, renders cards with Play/Resume/Delete; in-progress downloads show a live ETA chip.
- `renderEpList()` — episode picker rows with progress bars, per-episode ▶ button, checkbox selection, watched toggle.
- `setFcTitle(title, filePath)` — sets the fullscreen overlay's title. Uses `parseEpisodeInfo` to extract "S01E04 · Episode Name" when possible.
- `renderVpn(secure, statusText)` — updates the navbar pill + toggles the full-screen red overlay.

### Volume

VLC's volume slider is debounced — `oninput="updateVolumeDisplay"` updates label only, `onmouseup`/`ontouchend="vlcSetVolume"` sends the actual request. This was a fix for VLC lag when scrubbing the slider. Hard cap is the global `settings.max_volume` (fetched once at startup into `globalMaxVolume`, also refreshed when the profile-settings modal opens); `applyMaxVolumeToSliders` enforces it on the slider `max` attribute.

### Seek bar

Handles click + touch (`handleSeekBarClick`) and pre-click hover tooltip (`handleSeekBarHover`/`Leave`). Calls `POST /api/vlc/seek/to?position_pct=N` — VLC's `seek` uses `val=N%` for absolute and `val=±Ns` for relative. **Don't mix the two**.

### Episode picker (`epPlayFrom`)

Per-episode ▶ button: slices `epFiles` from the tapped index forward, respects resume position on the first file, plays as a playlist. This means "press play on episode 3" plays 3 → 4 → 5 …, not just 3.

### Handoff to Device (offline playback)

The frontend keeps an `offlineSaved: Set<"itemId|filePath">` populated from IndexedDB on init. Episode-picker rows show a save/remove toggle and an `OFFLINE` pill when a key is present. All Play surfaces (`epPlay`, `epPlayFrom`, `continueLibraryItem`, the `lib-restart-btn` listener) route through `playLibraryWithChooser(itemId, files, seekTo, label)`:

- If the device is offline → `lpPlay` directly (or error if no offline copy of the first file).
- If the first file in the playlist is saved offline → open the chooser modal so the user picks "On TV (VLC)" vs "On This Device".
- Otherwise → fall through to the existing `playLibraryFiles` (VLC).

Local playback is handled by `lpPlay` / `_lpLoadIndex` / `lpStop` etc. There is **one** `<video id="lpVideo">` inside `#localPlayer`. The container has two visual modes toggled via class:
- default (no `.lp-tiny`) — fullscreen overlay, uses native browser `<video>` controls.
- `.lp-tiny` — corner tile (96×56 video + huge fullscreen button + close), repositioned via CSS only (no DOM move, no video re-load).

Single-element design avoids iOS Safari's per-page video budget and the audio desync that two synchronized videos would create. iOS-friendly: `playsinline`, `<track>` for VTT subs, no MediaSource.

The **Offline tab** (`#offlineTab`) is a peer of Search/Library. `loadOfflineTab()` pulls all `videos` IDB rows, groups by `itemId`, and renders rows with size/duration plus Play/Delete buttons; see `renderOfflineTab` and `offlinePlayOne`/`offlineDeleteOne`/`offlineDeleteGroup`. Also see [OFFLINE.md](OFFLINE.md#offline-tab).

The IndexedDB schema (`streamlink-offline`):
- `videos` keyPath `key` = `"<itemId>|<filePath>"` — `{blob, subs[], skipData, codecInfo, sourceVideoUrl, savedAt, sizeBytes, name, duration}`.
- `meta` — small caches (currently unused in v1; reserved for client-side hints).
- `outbox` autoIncrement `id` — queued progress writes when offline.

`saveProgress(itemId, filePath, posSec, durSec)` is called every 15 s by `#lpVideoFull`'s `timeupdate` handler. When `navigator.onLine === true` it POSTs `/api/library/{id}/progress`; otherwise it `outboxPush()`es. The window's `online` event fires `outboxFlush()`, which drains the outbox.

The skip-intro / skip-credits offer logic in `lpEvaluateSkipOffer` mirrors the backend `_maybe_emit_skip_offer` (intro when `start-2 ≤ t < end-2`, credits when `t ≥ credits_start - 1`). Dismissed offers are remembered for the current `<filePath>#<type>` so they don't re-emit.

The service worker is registered in `init` via `registerServiceWorker()` → `/sw.js` at root scope; see `static/sw.js` for the cache strategy.

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
