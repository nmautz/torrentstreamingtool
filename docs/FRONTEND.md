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
| 153–218 | Profile settings modal (auto-skip toggles, resume mode, per-profile subtitle on/off override, global max-volume, change-PIN button) |
| 220–240 | Profile picker (full-screen on first load). Acts as a lock screen: a `body:has(#profilePicker:not(.hidden))` rule in `<style>` hides the player footer, skip/resume offers, and `#localPlayer` while the picker is open, so background playback chrome doesn't bleed through over the bottom-row login buttons on short mobile viewports. |
| 242–274 | Profile add/delete modal |
| 276–372 | Download modal (with metadata fields + file picker) |
| 374–447 | Upload modal (drop zone, progress bar) |
| 449–480 | Storage paths modal |
| 482–550 | Episode page (full-screen, Netflix-style — hero / season tabs / episode cards / sticky action bar). Replaced the legacy bottom-sheet modal in Milestone 12 |
| 552–575 | Stream file picker modal (`/api/stream/prepare` picker) |
| 577–609 | Subtitle search modal (query box + **language filter** `#subSearchLang`, defaulting to `subtitleDefaultLang`, "All languages" option) |
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

Single `EventSource('/api/events')`. Every handler first calls `_noteSSEMsg()` (stamps `app._lastSSEMsg` for the liveness watchdog — see "SSE reconnect supervision"). Handlers:
- `ping` — server keep-alive (≥ every 20 s), no payload; only proves the pipe is alive.
- `state` — full snapshot; `Object.assign(app, d)`, then `renderVpn` + `renderPlayer` + `updateDlBadge`. Also re-renders the library (`loadLibrary()`) when `download_idle_open` flips while on the Library tab, so the cards' "Idle — waiting" ↔ "Idle download" chips update as the idle/night window opens/closes
- `vpn_status` — show alert; update VPN pill + overlay
- `stream_status` — phase transitions (`buffering`/`playing`/`error`/`idle`); push progress fields
- `library_progress` — per-item dl speed/ETA (~every 5 s while item is downloading). Stored in `libDownloadStats` and rendered into `#dl-stat-<itemId>` (`formatDlStat` shows "Waiting for idle window" when `paused`). Also calls `refreshDownloadFiles(item_id)` so an expanded per-file list's progress bars + ✓complete badges stay live
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
- `renderPerfBanner(d)` — shows `#perfBanner` (sticky, amber=degraded / red=overloaded) from the `state` event's `sys_status.overall`, naming the hot resource(s) (CPU/GPU/RAM/network). Non-blocking; auto-hides the moment `overall` returns to `ok`. This is the user-facing "host is busy — performance may be reduced" warning while the box sheds/finishes background work after a viewer arrives. Admin sees the full per-resource breakdown in **Admin → System → System Health**.

### Server notices — always visible, never blocking

The three top notice surfaces — `#serverAttentionBanner` (reboot/update + missing-key), `#perfBanner` (host busy/overloaded incl. network), and `#globalToast` (error/warning/info via `showAlert`) — are all raised to **`z-[70]`** so they paint **above modals (`z-[60]`)**: a notice is never lost behind an open dialog. Because `z-[70]` also sits above the on-device player (`#localPlayer`, `z-50`) and its top Stop / To TV control bar, all three are **`pointer-events:none`** so they can never swallow a tap meant for those buttons — the rule is set **by ID** in the `<style>` block so it survives the `className` rewrites in `renderServerAttention`/`renderPerfBanner` (`#globalToast` already sets it on its container); the lone interactive child, `#serverAttentionLink` (Admin Panel), re-enables `pointer-events:auto`. The banner/toast backgrounds are translucent (`/70`) so they sit lightly over the video.

The fullscreen VLC controls overlay (`#fullscreenControls`, `z-50`, opaque) is the exception — a `z-[70]` banner pinned to the top would cover its Close/Night header buttons. So `openFullscreenControls`/`closeFullscreenControls` toggle **`body.fc-open`**, whose CSS **hides** those three elements and reveals `#fcNotice` instead — a dedicated strip inside the overlay, **absolutely positioned over the title row** (`top: safe-top + 48px`). Being absolute it **never reflows the control tiles below** (Play/Seek/Volume keep their positions — users rely on muscle memory there) and it's `pointer-events:none` so it can never block a tile.

`renderFcNotice()` is the single painter: it reads the live banner/toast DOM as the source of truth (no parallel state), collects the visible ones in priority order (reboot/update → host/network → latest alert), and renders one tone-coloured `.fc-notice-line` per notice (`_fcNoticeTone` maps the source element's Tailwind colour → amber/red/indigo/green). It's a no-op (hides the strip) when the overlay is closed. **Every notice mutation calls it** — `showAlert` (+ its timeout), `hideAlert`, `renderServerAttention`, `renderPerfBanner`, and both fullscreen open/close — so the strip and the normal banners never diverge. (Native-fullscreen on the on-device `<video>` player escapes DOM stacking and is **not** covered by this strip.)

### Download scheduling (per-item)

A downloading library card shows a download-mode badge ("↓ Downloading", or "↓ Idle download" / "⏸ Idle — waiting" when `item.download_mode==="idle"`, gated on `app.download_idle_open`) plus a Pause↔Resume button: **⏸ Idle** (`setDownloadSchedule(id,"idle")` — download only during the idle/night window) ↔ **▶ Resume** (`setDownloadSchedule(id,"now")`). The download modal's **Download at idle/night only** toggle (`#dlIdleOnly`) sends `download_mode:"idle"` on submit and warns (`#dlIdleWarn`) when `app.download_idle_configured===false` (no admin prep window enabled).

The **Files** expander appears on a downloading item and **also on a finished partial item** (`download_partial && torrent_hash`), so the full per-file controls stay available after the kept files complete — clicking **Now**/**Top** on a ⊘-skipped row re-downloads it (the server flips the item back to `downloading`). `renderLibrary` re-populates any open expander after a re-render (a status flip rebuilds the card).

**Episode picker download scheduling.** For a torrent-backed item (`/files` returns `has_torrent`, stored in `epHasTorrent`; `epDownloadMode` holds the item mode), `renderEpList` shows a control bar above the list: **This season / All files** → ⬇ Now · 🌙 Idle · ⊘ Skip (`epSchedSeason(mode)` → `setFileSchedule` on the visible season's paths), and on a multi-season item an **All** → ⬇ Now · 🌙 Idle (`epSchedItem(mode)` → `setDownloadSchedule(…, resetFiles=true)`, which clears per-file overrides so the *whole torrent* — including skipped files — follows the mode). Per episode, `_epCardHtml` shows the device-download `<a>` **only when `complete`**; a not-on-disk episode shows ⊘ Not downloaded (skipped) / 🌙 Idle — deferred with a **⬇ Download** button (`epDownloadSkipped` → fetch to host now), or ⬇ %  when actively downloading (no button) — and play is blocked either way. All picker actions `refreshEpFiles()` after.

`renderDownloadFiles(itemId, files)` (the "Files" expander, `#dl-files-<id>`) is folder-grouped: `_dlFolderGroups(files)` splits each path (handling Windows `\` too) and groups files by their sub-folder beneath the torrent's common root — a flat torrent renders one unlabeled group (no header), a season pack gets per-folder headers with bulk controls. Each file row (`_dlFileRow`) shows name + size, a download progress bar + `dl_pct`%, a **✓ Complete** badge, and a per-row segmented schedule control (`_schedControl`: **Top**/`high` · **Now**/`now` · **Idle**/`idle` · **Skip**/`skip`, highlighting the active mode — replaces the old scroll-to-bottom "Prioritize Selected"). A **▶ Play** button appears on `complete` files and plays that single file to VLC via `playLibraryFiles` even while the rest of the torrent downloads; incomplete files keep the queue-when-ready ▶ toggle.

- `setFileSchedule(itemId, paths, mode)` → `POST /file-schedule`; optimistically updates the cached `f.mode` then re-renders. Folder header buttons pass every file's path in the group.
- `setDownloadSchedule(itemId, mode)` → `POST /download-schedule`; then `loadLibrary()`.
- `refreshDownloadFiles(itemId)` — no-spinner re-fetch of `/files`, called from the `library_progress` SSE handler so progress bars/complete badges stay live. No-op unless the list is expanded.
- Schedule + play buttons carry `data-idxs` / `data-idx` (indices into the cached `downloadFilesData` array) and are wired via `addEventListener` after render — never inline `onclick` with a JSON path (the established pattern; see `renderEpList`).

### Volume

VLC's volume slider is debounced — `oninput="updateVolumeDisplay"` updates label only, `onmouseup`/`ontouchend="vlcSetVolume"` sends the actual request. This was a fix for VLC lag when scrubbing the slider. Hard cap is the global `settings.max_volume` (fetched once at startup into `globalMaxVolume`, also refreshed when the profile-settings modal opens); `applyMaxVolumeToSliders` enforces it on the slider `max` attribute.

### Night mode (VLC dynamic-range compression)

A global on/off toggle reachable from **two** controls: the subtle moon button in the fullscreen-overlay header (`#fcNightBtn`, opposite the Close button) and a checkbox in the **Global** section of profile settings (`#psNightMode`). Both call `toggleNightMode(el)`, which flips `app.vlc_night_mode` optimistically, `renderNightMode()`s the controls, then `POST`s `/api/settings/night-mode` with `{night_mode}`.

The **intensity picker** (`#psNightModePreset`, Light/Medium/Max) is **settings-menu only** — deliberately not in the fullscreen UI. `setNightModePreset(preset)` POSTs `{preset}` only (so it never clobbers the on/off state) and persists independently of the toggle, so the chosen intensity is remembered the next time night mode is switched on. `NIGHT_PRESET_DESC` drives the one-line blurb under the picker.

`renderNightMode()` (also called from the `state` SSE handler, since every snapshot carries `vlc_night_mode`) recolors the moon + fills its icon when active, checks the checkbox, and syncs the preset `<select>` + blurb. The server relaunches VLC to apply the filter (see [GOTCHAS.md](GOTCHAS.md)), so playback briefly re-buffers — these are intentionally low-prominence controls, not hot-path tiles (a preset change *while off* doesn't relaunch). `openProfileSettings` fetches the fresh `night_mode` + `preset` so both controls are correct even before the first SSE state event.

### Profile Settings modal — progressive load

`openProfileSettings()` is **synchronous**: it shows `#profileSettingsModal` immediately, calls `_psSetLoading()` (dims every control via `.ps-loading`, sets the value labels to "…"), then kicks off seven independent loaders that run **concurrently** (`_psLoadProfilePrefs`, `_psLoadMaxVolume`, `_psLoadVlcStartVolume`, `_psLoadSysVolume`, `_psLoadYtStartVolume`, `_psLoadHostVolume`, `_psLoadNightMode`). Each loader fetches its own setting and calls `_psReady(...ids)` to clear the loading state on just the controls it owns, so the window feels responsive instead of blocking on the slowest request. `_psLoadHostVolume` is the exception: it leaves the slider `disabled` when the host mixer is unavailable ("N/A") and only drops the `.ps-loading` pulse. `_PS_CONTROLS` / `_PS_VALUE_LABELS` list the affected element ids.

### Subtitle defaults (per-profile override + search filter)

Profile Settings has a **Subtitles** `<select>` (`#psSubtitles`: Default / On / Off). `openProfileSettings` seeds it from the profile's `subtitles_on` (`true`→On, `false`→Off, null→Default); `saveSubtitlesPref()` POSTs `/api/profiles/{id}/subtitles` with `subtitles_on` = `true`/`false`/`null`. This is just the *preference* — VLC track selection happens server-side in `_apply_subtitle_policy` on the next play (see [GOTCHAS.md](GOTCHAS.md)), so there's no live VLC call here.

The **Find Subtitles** modal's language filter (`#subSearchLang`) is populated from a small common-language list plus `subtitleDefaultLang` (the admin preferred language, carried in every `state` snapshot as `subtitle_default_language`), and defaults to it (or "All languages" when Any). `runSubtitleSearch` passes the selected `lang` to `/api/subtitles/search`; the per-result download still uses each result's own language.

### Optimistic Play UI + in-flight guards

`continueLibraryItem` and `playLibraryFiles` run under `withInflight("play_${itemId}", …)` so a frustrated double-tap during a slow VLC handoff is dropped client-side instead of racing extra `in_play` requests to VLC. Before the fetch they call `_optimisticBuffering(label, itemId)` which:

- Flips `app.stream_status="buffering"` + `is_library_playback=true` immediately and calls `renderPlayer`.
- Seeds `app.active_title` from the optional `label` (e.g. the episode-specific "S01·E04 · Name") so the user sees what's loading, while seeding `_fcAutoTitle` from the cached item title (so the server's confirming state event with the canonical title doesn't trip "new track → re-open fullscreen" and pop the overlay back up if the user has dismissed it).
- Opens the mobile fullscreen overlay so the user always has something on screen while the server is mid-handoff.

If the Play fetch errors, `_revertOptimistic()` restores `stream_status="idle"`. If the fetch succeeds the server's `buffering` → `playing` state events overwrite the optimistic values.

Both functions also bail with a warn-toast if `app._connected === false` (SSE has been disconnected past its 4 s grace timer — see "SSE pill" below).

### SSE pill ↔ `app._connected`

`connectSSE()`'s open / error handlers maintain `app._connected` and the navbar `#sseLabel`. On `error`, a 4 s grace timer runs — brief reconnect hiccups don't flag the app as offline. Once the timer fires, `app._connected=false`, a "Lost connection to host — reconnecting…" toast shows, and Play guards block new actions until SSE re-opens. The pill itself is no longer mobile-hidden — it shows **LIVE** (green) / **OFFLINE** (red) on every viewport so the connection state is always visible.

### SSE reconnect supervision (mobile resilience)

The native `EventSource` only auto-reconnects while it's in the `CONNECTING` state. Two mobile failure modes defeat that, so `connectSSE()` adds its own supervision (all state in module-level `_sse`, `_sseReconnectTimer`, `_sseBackoff`, `app._lastSSEMsg`, `app._sseStarted`):

1. **Browser closed the stream** (it gives up after the device locks / the app is backgrounded). The `error` handler checks `es.readyState === EventSource.CLOSED` and, if so, calls `_scheduleSSEReconnect()` — a self-rebuilding reconnect with **exponential backoff** (1 s → 15 s cap, reset to 1 s on a successful `open`).
2. **Half-open connection** (the socket silently died while suspended but still reads `OPEN`, so no `error` ever fires and the UI freezes on stale data). Caught by a **liveness watchdog**: the server emits a `ping` event at least every 20 s, every SSE handler calls `_noteSSEMsg()` to stamp `app._lastSSEMsg`, and a 15 s `setInterval` (foreground-only) forces a reconnect via `_ensureSSEConnected()` if nothing has arrived for `_SSE_STALE_MS` (50 s).

`_ensureSSEConnected()` reconnects immediately (backoff reset to 1 s) when the stream is closed, stale, or `app._connected===false` — gated on `app._sseStarted` (set true on first `connectSSE`) and `navigator.onLine`. It's wired to **`visibilitychange`→visible, `pageshow` (bfcache restore), `focus`, and `online`** — so the connection is re-checked the instant the user returns to the tab or the radio comes back, which is the high-value path on mobile. `connectSSE()` always `close()`s the prior `_sse` first so reconnects never leave a duplicate stream (each open `EventSource` holds a server-side queue).

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
returned `master_url` directly (cache hit) or polls
`/api/library/offline-job/{id}` while the `#lpPreparing` overlay shows
"Building stream… 42%". Once the bundle is ready, the player loads `master_url`
via **hls.js** (`Hls.isSupported()` → `loadSource`/`attachMedia`) or **Safari
native HLS** (`<video>.src = master_url`). See [STREAMING.md](STREAMING.md) for
the engine branch.

`_lpRenderTrackRows` (run on `MANIFEST_PARSED` / `loadedmetadata`) populates the
three `#lpTrackRow` selectors: **Res** (quality), **Aud**, **Sub** — each row
hidden when it has ≤1 option. Quality is hls.js-only: the Res dropdown is built
from `lp.hls.levels` (sorted high→low) as `Auto` + each resolution, and
`lpSetQuality(idx)` sets `lp.hls.currentLevel` (`-1` = Auto/ABR; session-only,
not persisted). Safari's Res row stays hidden (no manual-level API). `lpSetAudio`
/ `lpSetSubtitle` persist their picks via `/api/library/{id}/local-tracks`.

Subtitle `<track>` elements are wired from the bundle's `subtitles[]` (each a
`sub_<i>.vtt` in the cache dir) plus on-disk `subs[].url` sidecars (each points
at `/api/library/{id}/subtitle` — server converts SRT→VTT on the fly). Skip-data
is fetched in parallel from `/api/library/{id}/skip-data?file_path=…` and
assigned to `lp.skipData` for the skip-intro / skip-credits logic in
`lpEvaluateSkipOffer`.

There is **one** `<video id="lpVideo">` inside `#localPlayer`. The container
has two visual modes toggled via class:
- default (no `.lp-tiny`) — fullscreen overlay, uses native browser `<video>` controls.
- `.lp-tiny` — corner tile (96×56 video + huge fullscreen button + close), repositioned via CSS only (no DOM move, no video re-load).

Single-element design avoids iOS Safari's per-page video budget and the audio
desync that two synchronized videos would create. iOS-friendly: `playsinline`,
`<track>` for VTT subs, no MediaSource.

The header (`.lp-chrome`, hidden in tiny mode) carries **Prev** / **Next**
episode buttons (`#lpPrevEpBtn` / `#lpNextEpBtn`), both **hold-to-activate**
(`_holdStart`). `lpPrevEp` / `lpNextEp` → `lpNavEp(±1)` saves the current
position then `_lpLoadIndex(0)`s the neighbour; the hold works whether or not
that episode is prepped. `_lpRenderNavButtons` (called from `_lpLoadIndex`)
shows/hides each button for the current `lp.pi` and paints a square dot from
`prepFileState` — green = ready, amber = prepping, gray = not prepped.
`_lpWarmNextEp` (also from `_lpLoadIndex`) fires an interactive `/offline-prepare`
for the next episode so auto-advance / a Next hold resumes instantly. See
[STREAMING.md § Auto-advance](STREAMING.md).

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
first awaits `confirmStreamPrepWarning()` (the once-per-session lag warning),
then POSTs `/offline-prepare` **with `bulk:true`** (so the job honors the global
pause gate), polls `/offline-job/{id}` to completion, and flips the row to
"Stream Ready". The map is also hydrated from `/api/library/{id}/prep-status`
whenever the picker opens or `/prep-all` runs on the library card.

`#globalPrepBar` (the persistent top-right indicator) is now interactive:
`_renderGlobalPrep(d)` toggles a **Pause** button (`openPrepPauseChooser` →
`confirmPausePrep(kill)` → `/api/offline-prep/pause`) and a **Resume** button
(`resumePrep` → `/api/offline-prep/resume`) from `d.paused`. Pausing a job that's
mid-encode shows "Pausing — finishing current file" until it completes (or, if
the user chose "Stop now", it's cancelled at once). The per-card chip
(`prepChipHtml`) shows "Prep paused" when `processing === 0 && paused > 0`.

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

**Prep gating (footer button).** The footer **Device** button is greyed
(`.handoff-disabled`) with a "Not prepped for on-device streaming" note (via
`title`) when the current file isn't stream-ready. Readiness is
`_handoffReadyState(s)` → `true | false | null`: it reads `prepFileState` first
(instant when the file was prepped via the picker / prep-all), else the resolved
result of `_maybeRefreshHandoffReady(s)`, which fetches
`GET /api/library/{id}/prep-status` once per current file and repaints. Only a
*known-not-ready* (`=== false`) greys the button and blocks the click (with a
toast); `null` (unknown) stays clickable to avoid false-blocking. The async
result is cached in `app._handoffReady` / `app._handoffReadyFile` (guarded by
`app._handoffInflightFile`).

**Fullscreen To-Device tile (`#fcHandoffBtn`) — hold-to-prep.** Rather than just
greying when not prepped, the fullscreen tile invites prepping in place.
`_renderFcHandoff(s)` (called from `renderPlayer` and the prep poll loop) paints
its `#fcHandoffLabel` / `#fcHandoffNote` / `#fcHandoffBar` for four states:
**ready** ("Play To Device"), **not-ready** ("Prep for Device?", `.handoff-prep`),
**prepping** ("Prepping 42%", `.handoff-busy` + the `#fcHandoffBar` fill from
`app._fcPrepPct`; an indeterminate "Prepping…" when a prep started elsewhere owns
the job), and **unknown / macOS-no-HLS** (neutral, or the legacy greyed "Not
prepped" note). `fcDeviceTileHold(btn)` dispatches the hold: ready/unknown →
`handoffToDevice`; known-not-ready → `prepCurrentForDevice()`, which POSTs
`/offline-prepare {bulk:false}` (interactive, so it starts **while VLC keeps
playing** — the TV is *not* stopped), polls `/offline-job/{id}` for progress, and
`_finishFcPrep` flips it to "Play To Device". The first prep in a session still
shows `confirmStreamPrepWarning()`. State lives in `app._fcPrepFile` /
`app._fcPrepPct`.

`lpHandoffToVlc(btn)` is the reverse — it pushes the on-device play back onto the
TV. It captures the local `<video>` position + remaining playlist tail, calls
`lpStop()` (flushes progress, tears down the device player), then
`playLibraryFiles(itemId, tail, capturedTime, label)` (`POST
/api/library/{id}/play` with `seek_first_to`). VLC plays the original source
seeked to the same moment. The **To TV** button sits in the local player's
fullscreen header next to Stop (part of `.lp-chrome`, hidden in tiny mode).
Guarded by `withInflight("handoff_vlc")`.

### Clip (save & share the last N seconds)

Two entry points, one core. `_doClip(itemId, filePath, endSec, seconds,
audioIdx, btn)` POSTs `/api/library/{id}/clip`, then `_shareOrDownload(url,
filename)` fetches the result and offers it via the OS share sheet
(`navigator.canShare({files})` — iOS/Android) or a download (desktop).

- **Fullscreen (TV):** `#fcClipRow` (Row D2). `fcClip(seconds, btn)` reads the
  freshest position from a live `GET /api/vlc/tracks` and clips audio track 0.
  `renderPlayer` shows the row under the same `canHandoff` gate and greys the
  tiles until the file is prepped (`_handoffReadyState(s)===false`).
- **On-device:** `#lpClipRow`, directly under `#lpSubRow` (the track row is now
  always shown). `lpClip(seconds, btn)` clips the local `<video>.currentTime`
  with the selected audio (`lp.pendingAudioIdx`).

`fcClipCustom` / `lpClipCustom` prompt for a length via `_clipPromptSeconds()`
(clamped 1–300 s). See [STREAMING.md § Clip](STREAMING.md#clip).

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
5. **System** (`#panelSystem`) — **Shut Down** (`doShutdownServer`), **Reboot Machine** (`doRebootMachine`, confirm-gated → `POST /api/admin/reboot`), and **Scheduled Restart** (`loadScheduledReboot` / `toggleScheduledReboot` / `saveScheduledReboot` → `GET`/`POST /api/admin/scheduled-reboot`). The scheduled-restart panel has an enable toggle, time input, timezone select, idle-window field, and a live host-time readout; loaded on tab switch. (Offline Cache and Background Video tabs also exist — see [ADMIN.md](ADMIN.md).)

### Admin SSE ([static/admin.html:483](../static/admin.html#L483))

`ensureAdminSSE()` connects to `/api/events?admin_token=...` to receive `analysis_status` events while a Smart Skip run is in progress — the Smart Skip tab live-updates progress bars.

## See also

- [BACKEND.md](BACKEND.md) — what each endpoint actually does
- [API.md](API.md) — endpoint signatures
- [ADMIN.md](ADMIN.md) — admin auth, indexer flow, content lock semantics
