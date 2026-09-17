# Frontend

Vanilla JS, Tailwind CDN, no build step. Two pages: `static/index.html` (main dashboard) and `static/admin.html` (admin panel).

## Design system

Metro UI throughout — flat tiles, no rounded corners, bold uppercase typography, accent stripes, sharp dividers. No `backdrop-blur`. All status dots are square. Modals are bottom-sheets on mobile, centered on desktop.

### Iconography — no emoji, ever (10.8.0)

**Emoji and dingbat characters are banned in all rendered UI.** They draw from the OS emoji font, so the same page looks different on Windows / iOS / Android / TV Chrome (and iOS force-promotes many codepoints — `▶ ⚠ ⏸ ↩ ⚡ 🌙 …` — to rounded colour emoji that clash with Metro). Every pictographic glyph comes from the **inline SVG sprite** instead:

- **The sprite** — a hidden `<svg>` of `<symbol id="i-NAME" viewBox="0 0 24 24">` defs, right after `<body>` in `index.html` **and** `admin.html`. The two copies must stay **byte-identical**; when you add a symbol, add it to both. `tv.html` currently uses no glyphs — copy the sprite there too if it ever needs one.
- **Names** — `play pause resume hourglass check x warn bolt star menu gear up down moon ban lock unlock key phone folder dots chev-up chev-down chev-right ext monitor`. Semantic mapping: `resume` = the old `↩` (continue-from-position), `ban` covers both `🚫` and `⊘` (not-downloaded / skipped / never), `x` covers close ✕ **and** failed ✗, `dots` is the Metro square-dot "More" ellipsis, `ext` = external link ↗, `monitor` = a desktop display (the TV kiosk's Use-My-PC toggle).
- **Static HTML** — `<svg class="ic" aria-hidden="true"><use href="#i-NAME"/></svg>`.
- **JS-built HTML** — `${ic("NAME")}` (or `ic("NAME","extra-classes")`); the helper sits at the top of each page's main script. Icons are markup, so the sink must be `innerHTML` / a template — **never** `textContent`.
- **Sizing/colour** — `.ic` (in each page's `<style>` head) is `1.1em` square, `fill:currentColor`, baseline-aligned, `flex-shrink:0`, `pointer-events:none`. Icons therefore inherit the surrounding text size and colour automatically — size them with the text (`text-2xl` etc. via `ic("warn","text-2xl")`), don't add width/height.
- **Plain-text sinks** (`confirm()`, `title="…"`, `textContent`, the sync debug log) can't carry markup: use words (`"Download"`, `"! conflict"`, `"ERR"`) — do **not** reintroduce glyph characters there.
- **Allowed as text**: pure typography with no emoji presentation — `— … · → ⇒ ↓ ↑ ★ ‹ ›`. (`★` appears in `textContent`-built meta lines; that's fine, it has no emoji form.)
- **Drawing new symbols** — keep them Metro: single `fill` path(s) on the 24×24 grid, hard corners, geometric (square dots, straight bars), `fill-rule="evenodd"` for cutouts. No strokes, no rounded caps, no multi-colour.
- **Enforcement** — this grep must return 0 matches on `static/*.html`: `grep -cE '⚠|▶|⏸|⏳|↩|✓|✗|✕|⚡|☰|⚙|⬆|⬇|🌙|🚫|⊘|🔒|🔓|🔑|📱|🏠|📁|⋯|↗|▼|▲|▴|▾|▸' static/*.html`

**Layout — mobile-first, use all available space.** The app shell (navbar, `<main>`, player footer rows) is capped at `max-w-screen-2xl` (1536px) and centered, so it fills wide desktops/TVs (the Windows-primary target) instead of stranding margins. The three list surfaces are **responsive grids** that stay single-column on phones and add columns as the viewport grows: search results (`#resultsGrid`) → `grid-cols-1 sm:grid-cols-2 xl:grid-cols-3`; library cards (the per-series group wrapper in `renderLibrary`) → `grid-cols-1 lg:grid-cols-2`; the episode list (unwatched/watched wrappers in `renderEpList`) → `grid-cols-1 xl:grid-cols-2`. All use `items-start` so a card growing (e.g. the library Files expander) doesn't stretch its row neighbour. Don't reintroduce a narrower fixed-width column or revert these to `space-y-*` / `divide-y` single-column stacks.

- **Single-item series go full width.** `renderLibrary` only uses the 2-col grid when a series group has **more than one** item (`gridCls = gItems.length>1 ? grid… : "space-y-2"`); a single-item series would otherwise strand an empty second column.
- **Library card buttons.** Primary actions accumulate in `buttons`; icon-only buttons (download / prep / hide / delete) accumulate in `iconBtns` and render as one cluster that follows the primaries (no `ml-auto` — it absorbs the row's slack and leaves a gap). The whole row is `.lib-card-actions`; its CSS forces every primary button to a uniform 44px single-line height so nothing wraps to two lines or mismatches. Symbol glyphs in the primary buttons (resume / play / pause / hourglass) are sprite icons — see § Iconography above; the old U+FE0E variation-selector trick is retired.
- **Height-locked app shell — the document never scrolls (with one exception).** `html`/`body` are `100dvh` + `overflow:hidden` + `overscroll-behavior:none`; `<main>` (`flex-1 overflow-y-auto`) and the overlay lists are the **only** scroll containers. This is what stops mobile rubber-banding/pull-to-refresh from detaching the fixed footer and overlays or scrolling the page behind a modal. Consequences when adding UI: the body must **not** get `min-h-screen` back (`100vh` overshoots `100dvh` while the mobile URL bar is shown and clips the bottom); `sticky top-0` at body level is inert — top bars stay visible because they're `flex-shrink-0` siblings above `<main>`; never attach scroll listeners to `window` (scroll `<main>` or the specific container); any **new scrollable region** must get `overscroll-behavior:contain` (the `.overflow-y-auto` Tailwind class is already covered by a blanket rule in the `<style>` head — elements made scrollable via bespoke CSS, like `#epHero`/`#fcTileGrid`/`#lpTrackRow`, must be added to that rule). `touch-action:manipulation` on the shell disables double-tap zoom app-wide (pinch still works). **The exception:** while the on-device player is open full-screen (`#localPlayer.lp-active:not(.lp-tiny)`, coarse pointers only), an `html:has(...)` rule re-enables root scrolling by making `<body>` 45vh taller than the viewport, because the browser URL bar only auto-hides on a *document* scroll — swiping up on the video minimizes the browser chrome. The overflow must be the body's own box (an absolutely-positioned spacer below body doesn't reliably extend Safari's root scroll range); the extra height and its scroll offset vanish when the player closes or minimizes. True fullscreen on iPhone Safari (no element-fullscreen API) comes from the player's fullscreen button falling back to `video.webkitEnterFullscreen()` — see [STREAMING.md](STREAMING.md).
- **Mobile landscape.** A `@media (orientation:landscape) and (max-height:500px)` block in the `<style>` head keeps short landscape phones usable (tablets ≥744px tall are excluded). The episode page flips to a **two-pane split** (`#episodePage{flex-direction:row}`): the hero (`#epHero`) is a scrollable ~¼-width left column with the series art + description (`#epHeroContent` stacks vertically), and the season tabs + episode list + actions — wrapped in `#epRightPane` — fill the right ¾. (`#epRightPane` is a `flex-col flex-1` wrapper that's transparent in portrait, so the same DOM serves both orientations.) The block also strips the playback UI to **transport-only** so nothing scrolls.

- **Footer → name + fullscreen button on one line.** The seek bar (`#seekBarWrapper`), the Audio/Subs row (`#trackControls`), the PLAYING badge (`#statusBadge`), and the whole transport row (`#playerControlsRow`) are hidden; `_applyPhoneLandLayout()` moves the fullscreen button (`#fullscreenBtn`) into the title row (`#playerStatusRow`) so it shares that line. The button is compacted (overrides its `flex-[2]`) and gets `z-index` so it wins on overlap; the title is `flex-1 min-w-0 truncate` so it shrinks beside it rather than overlapping.
- **Fullscreen overlay.** The seek bar + transport rows (play/seek, ep-nav, stop/device) keep their default `flex-1` so they grow to fill the grid (`#fcTileGrid`) — no dead space at the bottom (`overflow-y:auto` is a safety net only). **Volume** stays reachable without opening More: the ep-nav row carries inline `−`/`+` buttons (`.fc-land-vol`, shown via `#fcEpNav > .fc-land-vol`), which shrink Prev/Next to share the line; the standalone slider row `#fcVolRow` is dropped when ep-nav is present (`#fcEpNav:not(.hidden) ~ #fcVolRow { display:none }`) and kept visible for single-file playback (ep-nav hidden) where there's room. The **Audio/Subs row (`#fcTrackRow`)** is **relocated into the "More" sheet** (`#fcMoreBody`, above Night Mode) by `_applyPhoneLandLayout()` — it moves the **real DOM node** (not a copy, so the single `loadTracks` sync path is untouched) and restores it to its grid slot (before `#fcStopRow`) in portrait/large layouts.

`_applyPhoneLandLayout()` runs at init, on `openFullscreenControls()`, and on a `matchMedia("(orientation:landscape) and (max-height:500px)")` change listener so live rotation is handled. If the More sheet outgrows one screen as controls are added, it can be paginated later.

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
            library_profile_id, library_profile_name, library_profile_color,
            library_item_file_count, is_library_playback,
            play_when_ready_item_id, play_when_ready_file_path,
            skip_offer, resume_offer };
```
- `profile` — currently selected profile object (persisted to `localStorage.streamlink_profile`)
- `allProfiles` — fetched list from `/api/profiles`
- `activeTab` — `"search"`, `"explore"`, `"library"`, or `"offline"`
- `expandedDownloads: Set<itemId>` — which library download cards have file list expanded
- `downloadFilesData: Map<itemId, files[]>` — cached file lists
- `libDownloadStats: Map<itemId, payload>` — latest `library_progress` event per item
- `epChecked`, `epFiles` — selection state for the episode picker modal

### SSE event handlers ([static/index.html:3494](../static/index.html#L3494))

Single `EventSource('/api/events')`. Every handler first calls `_noteSSEMsg()` (stamps `app._lastSSEMsg` for the liveness watchdog — see "SSE reconnect supervision"). Handlers:
- `ping` — server keep-alive (≥ every 20 s), no payload; only proves the pipe is alive.
- `state` — full snapshot; `Object.assign(app, d)`, then `renderVpn` + `renderPlayer` + `updateDlBadge`. Also re-renders the library (`loadLibrary()`) when `download_idle_open` flips while on the Library tab, so the cards' "Idle — waiting" ↔ "Idle download" chips update as the idle/night window opens/closes
- `vpn_status` — show alert; update VPN pill + overlay (`renderVpn` → `applyVpnGate`); re-render the library so downloading rows flip their badge to/from "⚠ VPN down — paused"
- `stream_status` — phase transitions (`buffering`/`playing`/`error`/`idle`); push progress fields
- `library_progress` — per-item dl speed/ETA (~every 5 s while item is downloading). Stored in `libDownloadStats` and rendered into `#dl-stat-<itemId>` (`formatDlStat` shows "Waiting for idle window" when `paused`, and "Finding peers…" when `awaiting_metadata` — qBit has no file list yet, so nothing is actually transferring). Also calls `refreshDownloadFiles(item_id)` so an expanded per-file list's progress bars + ✓complete badges stay live, **and `_epLiveRefresh()` when the open episode page shows that item** (see § Keeping the episode page live)
- `library_update` — item status changed (`downloading`→`ready`/`error`); triggers `loadLibrary()` if on the Library tab, and `_epLiveRefresh()` (via `_epConcerns(id, true)`) when the episode page is open — `loose`, because the payload carries no series and a brand-new member is by definition not in the list yet
- `progress_saved` — quiet refresh of the library tab so watch-progress bars update, plus `_epLiveRefresh()` when the open episode page is concerned. Before 12.7.2 this was gated on `epItemId`, so a show opened as a **merged series** (where `epItemId` is null) never refreshed at all

### Key render functions

- `renderPlayer(s)` — drives the footer + fullscreen overlay. The seek bar shows **VLC position** when `stream_status==="playing"` and `vlc_duration > 0`; otherwise download progress. On mobile (<768 px) the fullscreen overlay auto-opens on **buffering OR playing** (not just `playing`) so slow-network Play taps get immediate visible feedback. The overlay is **never auto-closed** on `stream_status==="idle"` — the server can take seconds to publish the next track, and the volume slider must stay reachable during that gap. Closing the fullscreen is manual only (the X button, which sets `_fcDismissed=true`). For library plays the buffering badge says **"Loading…"** and the fullscreen status says **"Starting playback…"** (instead of the misleading "Buffering…" / "Connecting…" copy — those imply network work, but the file is already local).
- `renderPlaybackOwner(s)` — paints the **"started by" chip** (`#ownerBadge` in the footer status row + `#fcOwner` in the fullscreen overlay): a colored profile dot + the owner's name (`· you` when `s.library_profile_id === profile.id`). Driven by the `library_profile_*` snapshot fields and shown only while a library item is playing. Surfaces *whose* progress the shared VLC controls feed — a second viewer driving pause/seek/next can't reattribute the session, so this tells them their controls save to the starter's profile, not their own. Called from `renderPlayer`; seeded optimistically in `_optimisticBuffering` (the local profile owns a play it just started).
- `renderSkipOffer(offer)` / `renderResumeOffer(offer)` — manage the floating amber/blue offer tiles.
- `renderLibrary(items)` — groups items by `series`, then renders in one of **two view modes** (toggled per-device in Settings → This Device; see § Library view mode). Both modes build every item from the **shared `_libItemChrome(item)`** (returns the badges + the `buttons`/`iconBtns` action set + progress/meta/flags), so **no action is view-only** — that shared source is the invariant that keeps the two views at feature parity. `_libItemChrome` is the single place per-item actions are defined; edit it, not two copies.
  - **List view** (`_libItemListHtml`) — the original grouped layout: per-series header + a wide row per item with the full `.lib-card-actions` strip inline. The row's **title/meta block is a clickable surface** (`.lib-tile-open`, wired via `data-item-id`/`data-title` + `addEventListener` after render, same escaping-safe pattern as `.lib-restart-btn`) that opens the info page (`openEpisodePicker`) — episode list for a series, **movie panel** for a single-file item.
  - **Card view (default)** (`_libItemCardHtml`) — a flat responsive **poster grid** (no per-series headers; the poster + title identify the show). Each card is a 2:3 poster with a ▶ Play/Resume overlay (ready items), the status badges + a watch-progress bar overlaid, and a **⋯ button** (`_libCardMore`) that toggles an in-card **drawer** holding the *identical* `.lib-card-actions` set the list shows (Episodes / Download / Prep / Hide / Delete / download Pause↔Idle / When-Ready / the partial-download Files expander are all present; the primary Play/Resume itself opens the On-TV-vs-On-Device chooser, so there's no separate "On Device" action). Posters **lazy-load** per card (`_libWirePosters` → IntersectionObserver → `_libLoadPoster`) from `/api/library/{id}/metadata` (`poster_url` or `img_base`+`poster_path`), cached in `_libPosterCache` (cleared in `renameSeries`/`_applyMetaResult`); a flat Metro title tile (`.lib-poster-ph`) shows until/unless art arrives (retries a couple times while a first-ever TMDb fetch is `pending`). Merged multi-item series collapse into one poster card via `_libShowCardHtml` (reuses `.lib-show-open`/`.lib-show-play` so the existing post-render delegation wires it).
  - A **downloading multi-file** item shows an **☰ Episodes** action (opening the episode page, which manages in-flight downloads) instead of the old inline Files expander; the Files expander now renders only for the finished-partial case (`download_partial && torrent_hash`). In-progress downloads show a live ETA chip.
  - A **still-downloading** item (single- or multi-file) also carries a card-level **▶ Play now** / **⟳ Resume now** button (11.4.0): it streams a file on the TV while the torrent is still downloading via `streamLibraryFile` → `POST /api/library/{id}/stream-file` (sequential + first/last-piece → play once buffered). Target = the resume file when the item has progress (`hasProgress`, reads "Resume now"), else `first_file` (the earliest video). It's distinct from the **When Ready** queue button, which instead waits for the whole download to finish. Only rendered when the item is torrent-backed and not compressed.

#### Library view mode (device-local)

`libViewMode` (`"card"` default | `"list"`) is a **device-local** UI preference persisted to `localStorage.streamlink_libview` — like Dev Mode / Locked Progress Bar it is never per-profile or server-stored. The **Settings → This Device → Library View** segmented control (`#psLibViewCard`/`#psLibViewList`) calls `setLibView(mode)`, which persists, repaints the control (`_renderLibViewToggle`, seeded in `openProfileSettings`), and re-renders the library tab live from `window._libCache`. `renderLibrary` branches on `libViewMode` right after grouping; only card mode opens the outer grid `<div>` and calls `_libWirePosters`. All post-render event wiring (`.lib-restart-btn`, `.lib-tile-open`, `.lib-show-open`, `.lib-show-play`, the `expandedDownloads` re-populate) runs for both views because both reuse the same class names + data attributes.
- `renderEpList()` — episode picker rows with progress bars, per-episode ▶ button, checkbox selection, watched toggle. The watched toggle is an element-based handler (`epToggleWatched(this)` reading `data-path`/`data-watched`); the prior inline `JSON.stringify(f.path)` form blew up the `onclick="…"` quoting and broke the button silently. **Movie mode:** when `epIsMovie` (single-file item — set in `openEpisodePicker`), it renders `_epMoviePanel(epFiles[0])` instead of the list — a detail panel (status + watch/download progress + Download-to-device / ⚡ Prep / 🗑 Delete; for a still-downloading file, Download-to-host + Now/Idle schedule). `_epApplyMovieChrome(isMovie)` hides `#epBulkChips` + the multi-select bottom buttons (`#epDownloadSelBtn`/`#epPlaySelBtn`) and shows the single `#epMoviePlayBtn` (→ `epPlayFrom(0)`, label reflects resume). **TV mode (`?tv=1`) renders a browse+play-only episode page**: `TV_MODE` guards in `renderEpList`/`_epCardHtml`/`_epMoviePanel` drop the scheduling/recheck bar, Save ZIP, checkboxes, per-episode action rows, priority rows, and the movie panel's secondary actions, while `.tv-mode` CSS hides the hero's rename/metadata/on-demand buttons, the bulk chips, and the selection-driven bottom buttons; the library is forced to card view. See [REMOTE.md](REMOTE.md) § TV layout.
- **Keeping the episode page live (12.7.2).** `_epLiveRefresh()` is the single repaint entry point for SSE-driven change while `#episodePage` is open, and `_epConcerns(itemId, loose)` decides whether an event applies (exact in single-item mode; `epFileItem` membership in merged-series mode; `loose` for callers that may be announcing a new member; an event with no `item_id` is treated as ours). Two constraints shape it and both are load-bearing: `renderEpList` rebuilds `#epList.innerHTML` and `#epList` is the **scroll container**, so `_epLiveRefresh` captures and restores `scrollTop` — without that, a live page yanks itself to the top every few seconds, which is worse than a stale one; and `library_progress` fires per downloading item, so repaints coalesce behind one `EP_LIVE_MS` (1.5 s) trailing timer rather than issuing a fetch per event.
- `refreshEpFiles()` — re-fetches `/api/library/{id}/files` for the open picker and re-renders. Called after `epToggleWatched` and from the `progress_saved` SSE handler. Preserves `epChecked` selections that still exist; does not touch the modal title (so it's safe to call mid-session).
- `setFcTitle(title, filePath)` — sets the fullscreen overlay's title. Uses `parseEpisodeInfo` to extract "S01E04 · Episode Name" when possible.
- `renderVpn(secure, statusText)` — updates the navbar pill + toggles the full-screen red overlay, then calls `applyVpnGate`.
- Episode **Prev/Next** (footer `#prevEpBtn`/`#nextEpBtn` and the fullscreen `#fcEpNav` row + `#fcEpCounter`) appear when `library_nav_count > 1` on a live library playback. Read that, **not** `library_item_file_count` — a show downloaded one episode per library item reports `1` there for every episode, which hid the controls outright (11.19.1). The counter uses `library_nav_index`/`library_nav_count`; `library_playlist_count`/`library_current_index` describe the shrinking VLC tail and count *down*. See [GOTCHAS.md](GOTCHAS.md) § cross-item series playback.
- `applyVpnGate(secure)` — VPN-down feature gate. Toggles `body.vpn-down` (CSS greys every `.vpn-gated` control: Search box/button, the Save/Play buttons on results, the "Recheck hashes" button) and swaps each gated control's `title` to an explanatory message (restoring the original when the VPN returns). Greying is pure CSS off the body class, so it also covers controls rendered *after* the drop; `applyVpnGate()` is additionally called at the end of `doSearch` and `renderEpList` to title freshly-rendered controls. Clicks are blocked by a capture-phase `click` listener (installed in init) that stops any `.vpn-gated` click and shows an alert. `vpnBlocked()` is the matching guard for keyboard/programmatic paths (`doSearch` on Enter, `epRecheckSelected`). Downloading library rows also swap their badge to "⚠ VPN down — paused" — the `vpn_status` handler re-renders the library so this updates live.
- Host-performance banner — **removed (7.4.1).** There is no longer a user-facing "host is busy/overloaded" banner. The `state` event's `sys_status` (CPU/GPU/RAM/network + `overall`) still flows; the admin sees the full per-resource breakdown in **Admin → System → System Health**. (Transient background-work load wasn't user-actionable, so it was dropped to cut notification noise.)

### Search source picker

A collapsible **Sources** control under the search box (`#srcWrap` → `#srcToggle` summary button + `#srcPanel` checklist) lets the viewer narrow which indexers a search queries. `loadSearchIndexers()` (called from `_doSelectProfile` and the saved-profile restore path) fetches `GET /api/search/indexers?profile_id=…` — the indexers this profile is *allowed* to search — and renders each with caps-derived content-type chips (`_SRC_TYPE_LABELS`/`_SRC_TYPE_STYLE`: movies/tv/anime/music/books/games/apps/xxx/other). The picker only un-hides when there are **2+** sources.

`selectedSources` (a `Set`) holds the chosen subset, **persisted per profile** in `localStorage` (`streamlink_search_sources_<id>`). Conventions: an **empty set means "all"** (so `toggleSource` materialises the full set before removing one, and collapses back to empty once everything is re-ticked); the **None** button stores a `"__none__"` sentinel (since empty already means all). `_selectedSourcesParam()` maps this to the `&indexers=` value `doSearch` sends — `null` (omit the param) for "all", a CSV for a strict subset, `""` for none. `doSearch` also always appends `&profile_id=` so the server can enforce the admin allowlist. See [API.md § Search](API.md).

### Server notices — always visible, never blocking

The two top notice surfaces — `#serverAttentionBanner` (reboot/update + missing-key) and `#globalToast` (error/warning/info via `showAlert`) — are both raised to **`z-[70]`** so they paint **above modals (`z-[60]`)**: a notice is never lost behind an open dialog. Because `z-[70]` also sits above the on-device player (`#localPlayer`, `z-50`) and its top Stop / To TV control bar, both are **`pointer-events:none`** so they can never swallow a tap meant for those buttons — the rule is set **by ID** in the `<style>` block so it survives the `className` rewrites in `renderServerAttention` (`#globalToast` already sets it on its container); the interactive children — `#serverAttentionLink` (Admin Panel) and `#serverAttentionDismiss` (the `×`) — re-enable `pointer-events:auto`. The banner/toast backgrounds are translucent (`/70`) so they sit lightly over the video.

**Dismiss/auto-hide:** `#serverAttentionBanner` is **dismissible** — `dismissServerAttention()` stores the active notice's key (`update:<phase>` / `missing:<labels>`, written to `el.dataset.noticeKey` on each render) in `serverAttentionDismissedKey` and hides it; `renderServerAttention` re-shows the banner only when the freshly-computed key differs, so a dismissed notice reappears on its own when the underlying condition changes (a new update phase, a different missing key). It **also auto-hides 5s** after a notice first appears (`serverAttentionTimer`) — treated as a dismiss (sets `serverAttentionDismissedKey`) so the same condition won't immediately re-pop it; the timer is (re)armed only on a genuine notice-key change so per-tick re-renders of the same notice don't keep resetting it. The `degraded` ("some search sources") notice was **removed (7.4.1)**. The admin panel's `#indexerHealthBanner` is still dismissible via `dismissIndexerHealthBanner()` / `_indexerHealthDismissedKey` (keys `degraded`/`healthy`).

The fullscreen VLC controls overlay (`#fullscreenControls`, `z-50`, opaque) is the exception — a `z-[70]` banner pinned to the top would cover its Close/Night header buttons. So `openFullscreenControls`/`closeFullscreenControls` toggle **`body.fc-open`**, whose CSS **hides** those three elements and reveals `#fcNotice` instead — a dedicated strip inside the overlay, **absolutely positioned over the title row** (`top: safe-top + 48px`). Being absolute it **never reflows the control tiles below** (Play/Seek/Volume keep their positions — users rely on muscle memory there) and it's `pointer-events:none` so it can never block a tile.

`renderFcNotice()` is the single painter: it reads the live banner/toast DOM as the source of truth (no parallel state), collects the visible ones in priority order (reboot/update → latest alert), and renders one tone-coloured `.fc-notice-line` per notice (`_fcNoticeTone` maps the source element's Tailwind colour → amber/red/indigo/green). It's a no-op (hides the strip) when the overlay is closed. **Every notice mutation calls it** — `showAlert` (+ its timeout), `hideAlert`, `renderServerAttention`, and both fullscreen open/close — so the strip and the normal banners never diverge. (Native-fullscreen on the on-device `<video>` player escapes DOM stacking and is **not** covered by this strip.)

### Help tips (user-facing jargon)

The dashboard ports admin.html's `help-tip` component: a small square "?" chip (`class="help-tip" tabindex="0" data-tip="…"`) served by one shared `#tipPop` popover. Hover/keyboard-focus shows a tip transiently; **tapping the chip pins it** so it works on touch (the dashboard is used from phones, where `title=` tooltips never appear). `helpTip(text)` builds the escaped chip markup for JS templates (near `ic()`); the popover logic (`_tipShow`/`_tipHide` + document-level mouseover/focusin/click/scroll/resize listeners) mirrors admin.html. Attached to the top jargon offenders: **prep** (defined in the prep-warning modal + a `helpTip` on the "Prep Prio" label), the **play chooser** (relabelled "On the TV" / "On this device" with plain descriptions — no chip needed), the **Search Sources** picker, the **Smart/Classic** toggle, and **seeders** ("sorted by seeders"). The "Compressed" badge tooltips were reworded off "no longer torrent-backed" to plain "this copy was shrunk to save space…".

### First-run setup checklist

`refreshSetupChecklist()` (called fire-and-forget in the `DOMContentLoaded` init) fetches `GET /api/setup-status` and, when a prerequisite is missing, paints `#setupChecklist` (a card at the top of `#searchTab`) so a fresh install explains *why* its tabs are empty instead of showing blank screens. It lists each not-ok item with its label/detail/how-to-fix and a **"Fix"** button that deep-links to `/admin#<admin_tab>` (admin.html's `showDashboard` honours the hash → `switchTab`). Hides itself once every `required` item is ok. `dismissSetupChecklist()` sets a `sessionStorage` flag to hide it, but a failing **required** item (e.g. no indexer) re-shows it on reload so the real blocker can't be permanently hidden. No-op in the offline/app snapshot (`_appOffline`). The old Search empty-state copy ("Public domain archives…") was replaced with accurate "Search for movies & shows" text pointing at this checklist.

### Download scheduling (per-item)

A downloading library card shows a download-mode badge ("↓ Downloading", or "↓ Idle download" / "⏸ Idle — waiting" when `item.download_mode==="idle"`, gated on `app.download_idle_open`) plus a Pause↔Resume button: **⏸ Idle** (`setDownloadSchedule(id,"idle")` — download only during the idle/night window) ↔ **▶ Resume** (`setDownloadSchedule(id,"now")`). The download modal's **Download at idle/night only** toggle (`#dlIdleOnly`) sends `download_mode:"idle"` on submit and warns (`#dlIdleWarn`) when `app.download_idle_configured===false` (no admin prep window enabled).

### Content lock at download time (11.21.0)

`#dlLockBtn` (Add-to-Library / Save-Stream modal header), `#ssLockBtn` (show view source sheet) and `#ssBulkLockBtn` (bulk sheet) are small lock/unlock icon toggles that send **`admin_only:true`** on `POST /api/library/download` / `/api/stream/save-to-library`, creating the item already content-locked instead of leaving it in the open library until someone flips it in the admin Content Lock tab.

Two flags back them — `_dlLocked` (modal) and `_ssLocked` (one shared choice across both show-view sheets). `syncLockButtons()` repaints all three via `_paintLockBtn(id,on)` (swaps the sprite `<use href>` between `#i-unlock` and `#i-lock`, toggles `text-amber-400`, sets `aria-pressed` and `title`) and shows `#dlLockNote`, the one amber line that explains the state — the bare icon is cryptic and `title=` never renders on touch. It is called every time one of the three surfaces opens; `_dlLocked` resets on each modal open and `_ssLocked` on each `openSearchShow`/`openSearchShowFromTmdb`, so a lock is never carried silently into the next download.

**`canLockContent()`** decides whether the icon is drawn at all: the selected profile must be `elevated` *in the freshly-fetched `allProfiles`* **and** equal to `_verifiedProfileId` from `GET /api/profiles`. If it returns false, `syncLockButtons` also clears both flags, so an expired 12 h PIN session can't leave a stale toggle riding along into a request the server will 403. This is presentation only — the endpoints re-check with `_is_elevated`. See [ADMIN.md § Content Lock](ADMIN.md).

The **Files** expander now appears **only on a finished partial item** (`download_partial && torrent_hash`), so the full per-file controls stay available after the kept files complete — clicking **Now**/**Top** on a ⊘-skipped row re-downloads it (the server flips the item back to `downloading`). `renderLibrary` re-populates any open expander after a re-render (a status flip rebuilds the card). A **downloading** item no longer uses this expander at all — it routes to the episode page via the **☰ Episodes** button (and the clickable tile), which carries the per-season download/prep scheduling bar.

**Episode picker scheduling bar.** For a torrent-backed item (`/files` returns `has_torrent`, stored in `epHasTorrent`; `epDownloadMode` holds the item mode), `renderEpList` shows a multi-row control bar above the list (`_visibleSeasonFiles`/`_visibleSeasonPaths`/`_visibleSeasonPrepMode` scope it to the active season):

- **Download row** — **This season / All files** → ⬇ Now · 🌙 Idle · ⊘ Skip (`epSchedSeason(mode)` → `setFileSchedule` on the visible season's paths), and on a multi-season item an **All** → ⬇ Now · 🌙 Idle (`epSchedItem(mode)` → `setDownloadSchedule(…, resetFiles=true)`, clearing per-file overrides so the *whole torrent* follows the mode). **Hidden once every episode in the item is on disk** (`epFiles.every(f=>f.complete!==false)`) — scheduling what to download is moot then.
- **Stream-prep row** (only when `hlsAvailable`) — **PREP · This season** → ⚡ Now · 🌙 Idle · ⊘ Never (`epSchedPrep(mode)` → `POST /prep-schedule`; `_epPrepBtn(mode,…,cur)` highlights the active segment from `_visibleSeasonPrepMode()`). `now` also `_startPrepPolling(epItemId)` so per-row Prep icons update live. Mirrors the download row but for HLS prep — see [STREAMING.md § Per-file prep schedule](STREAMING.md).
- **Recheck row** — **Recheck hashes (N)** (`epRecheckSelected` → `POST /recheck` on the **checked** episodes `epChecked`; `#epRecheckCount` tracks the count via `updateEpCount`). Confirms, then toasts the damaged/cache-purged result.

Per episode, `_epCardHtml` shows the device-download `<a>` **only when `complete`**; a not-on-disk episode shows ⊘ Not downloaded (skipped) / 🌙 Idle — deferred with a **⬇ Download** button (`epDownloadSkipped` → fetch to host now), or ⬇ %  when actively downloading (no button). **Since 11.1.0 an incomplete torrent-backed episode is playable**: tapping its still calls `epStreamNow` → `streamLibraryFile` → `POST /api/library/{id}/stream-file` (forces the file `high`, torrent sequential, plays to VLC once the beginning is buffered — also un-skips a deselected file); only a compressed/torrentless missing file keeps the blocked ⊘ overlay. All picker actions `refreshEpFiles()` after.

`renderDownloadFiles(itemId, files)` (the "Files" expander, `#dl-files-<id>`) is folder-grouped: `_dlFolderGroups(files)` splits each path (handling Windows `\` too) and groups files by their sub-folder beneath the torrent's common root — a flat torrent renders one unlabeled group (no header), a season pack gets per-folder headers with bulk controls. Each file row (`_dlFileRow`) shows name + size, a download progress bar + `dl_pct`%, a **✓ Complete** badge, and a per-row segmented schedule control (`_schedControl`: **Top**/`high` · **Now**/`now` · **Idle**/`idle` · **Skip**/`skip`, highlighting the active mode — replaces the old scroll-to-bottom "Prioritize Selected"). A **▶ Play** button appears on `complete` files and plays that single file to VLC via `playLibraryFiles` even while the rest of the torrent downloads; incomplete files get a **▶ Play now** button (`.dl-snow-btn` → `streamLibraryFile` → `/stream-file`, sequential fetch → play when buffered) plus the queue-when-ready ⏳ toggle.

- `setFileSchedule(itemId, paths, mode)` → `POST /file-schedule`; optimistically updates the cached `f.mode` then re-renders. Folder header buttons pass every file's path in the group.
- `setDownloadSchedule(itemId, mode)` → `POST /download-schedule`; then `loadLibrary()`.
- `refreshDownloadFiles(itemId)` — no-spinner re-fetch of `/files`, called from the `library_progress` SSE handler so progress bars/complete badges stay live. No-op unless the list is expanded.
- Schedule + play buttons carry `data-idxs` / `data-idx` (indices into the cached `downloadFilesData` array) and are wired via `addEventListener` after render — never inline `onclick` with a JSON path (the established pattern; see `renderEpList`).

### Volume

VLC's volume slider is debounced — `oninput="updateVolumeDisplay"` updates label only, `onmouseup`/`ontouchend="vlcSetVolume"` sends the actual request. This was a fix for VLC lag when scrubbing the slider. Hard cap is the global `settings.max_volume` (fetched once at startup into `globalMaxVolume`, also refreshed when the profile-settings modal opens); `applyMaxVolumeToSliders` enforces it on the slider `max` attribute.

### Night mode (dynamic-range compression)

A global on/off toggle reachable from **two** controls: the moon tile inside the fullscreen **More** sheet (`#fcNightBtn`, in `#fcMorePanel` — relocated there in 7.10.0 from the header, which now holds the **More** button) and a checkbox in the **Global** section of profile settings (`#psNightMode`). Both call `toggleNightMode(el)`, which flips `app.vlc_night_mode` optimistically, `renderNightMode()`s the controls, then `POST`s `/api/settings/night-mode` with `{night_mode}`.

The **intensity picker** (`#psNightModePreset`, Light/Medium/Max) is **settings-menu only** — deliberately not in the fullscreen UI. `setNightModePreset(preset)` POSTs `{preset}` only (so it never clobbers the on/off state) and persists independently of the toggle, so the chosen intensity is remembered the next time night mode is switched on. `NIGHT_PRESET_DESC` drives the one-line blurb under the picker.

`renderNightMode()` (also called from the `state` SSE handler, since every snapshot carries `vlc_night_mode`) recolors the moon + fills its icon when active, checks the checkbox, and syncs the preset `<select>` + blurb. The server relaunches VLC to apply the filter (see [GOTCHAS.md](GOTCHAS.md)), so playback briefly re-buffers — these are intentionally low-prominence controls, not hot-path tiles (a preset change *while off* doesn't relaunch). `openProfileSettings` fetches the fresh `night_mode` + `preset` so both controls are correct even before the first SSE state event.

**It applies to on-device playback too** (14.3.0). A VLC launch argument can't reach a browser `<video>`, so the local player builds the equivalent in Web Audio — `<video>` → `DynamicsCompressor` → makeup `Gain`, with a bypass path for off (`_lpNightSync`, driven from `renderPlayer` and the video's `play` event). The compressor settings are *derived from* the VLC presets server-side and served as `webaudio` on `GET /api/settings/night-mode`, so there is one set of numbers, not two. The moon tile is therefore no longer hidden while the TV plays on-device, and `toggleNightMode`'s status line drops the “restarting VLC” wording when a local player is what will apply it. Skipped inside the iOS app (native `AVPlayer` handoff). See [STREAMING.md](STREAMING.md) and [GOTCHAS.md](GOTCHAS.md).

### Profile Settings modal — progressive load

`openProfileSettings()` is **synchronous**: it shows `#profileSettingsModal` immediately, calls `_psSetLoading()` (dims every control via `.ps-loading`, sets the value labels to "…"), then kicks off seven independent loaders that run **concurrently** (`_psLoadProfilePrefs`, `_psLoadMaxVolume`, `_psLoadVlcStartVolume`, `_psLoadSysVolume`, `_psLoadYtStartVolume`, `_psLoadHostVolume`, `_psLoadNightMode`). Each loader fetches its own setting and calls `_psReady(...ids)` to clear the loading state on just the controls it owns, so the window feels responsive instead of blocking on the slowest request. `_psLoadHostVolume` is the exception: it leaves the slider `disabled` when the host mixer is unavailable ("N/A") and only drops the `.ps-loading` pulse. `_PS_CONTROLS` / `_PS_VALUE_LABELS` list the affected element ids.

The modal ends with a **This Device** section — settings persisted in the browser's `localStorage`, never on the server (they're not in `_PS_CONTROLS`, so they have no loading state), seeded synchronously in `openProfileSettings`: the **Dev Mode** checkbox (`#psDevMode`, `toggleDevMode`, `streamlink_devmode`) enabling the on-device player's diagnostics HUD (see § Dev Mode HUD below); the **Locked Progress Bar** checkbox (`#psSeekLock`, `toggleSeekLock`, `streamlink_seeklock`); and the **Library View** segmented control (`#psLibViewCard`/`#psLibViewList`, `setLibView`, `streamlink_libview` — see § Library view mode).

### Use My Computer (window-control pause)

The **Global** section has a **Use My Computer** tile (`#wcButtons`: **30 Min** / **2 Hours** / **Until Disabled**) that tells the server to pause the idle background video + every VLC focus/minimize/fullscreen assertion so the user can use the desktop. `pauseWindowControl(seconds)` POSTs `/api/window-control` `{action:"pause", seconds}` (0 = until resume) optimistically; `resumeWindowControl()` POSTs `{action:"resume"}`. State rides the SSE `state` event as `app.window_mgmt_paused` / `app.window_mgmt_pause_remaining` (-1 = until resume). `renderWindowControl()` (called from the `state` handler and `openProfileSettings`) swaps `#wcButtons` for `#wcResume` (a "Paused — resumes in m:ss" line + Resume button) and runs a 1 s local countdown ticker (`wcCountdownTimer`) between SSE updates, flipping back to the buttons when it hits 0 (the server auto-expires server-side — note a timed pause's deadline *slides* while the desktop is in use, so the next SSE `state` may re-assert paused with ~2 min remaining). `closeProfileSettings` clears the ticker.

On the **TV kiosk** the settings panel is hidden, so the pause has its own entry point: a TV-mode-only nav button (`#tvUseComputerBtn`, `i-monitor` icon, shown via `body.tv-mode` CSS). `tvUseComputer()` confirms, then starts a **2 h timed** pause (never indefinite — the remote is dead while paused, so a forgotten pause must expire on its own); while paused the button reads **Resume TV** (label synced in `renderWindowControl`) and toggles `resumeWindowControl()`. The server minimizes the kiosk on pause; restore it from the taskbar (or use any other device) to resume.

### Subtitle defaults (per-profile override + search filter)

Profile Settings has a **Subtitles** `<select>` (`#psSubtitles`: Default / On / Off). `openProfileSettings` seeds it from the profile's `subtitles_on` (`true`→On, `false`→Off, null→Default); `saveSubtitlesPref()` POSTs `/api/profiles/{id}/subtitles` with `subtitles_on` = `true`/`false`/`null`. This is just the *preference* — VLC track selection happens server-side in `_apply_subtitle_policy` on the next play (see [GOTCHAS.md](GOTCHAS.md)), so there's no live VLC call here.

The **Find Subtitles** modal's language filter (`#subSearchLang`) is populated from a small common-language list plus `subtitleDefaultLang` (the admin preferred language, carried in every `state` snapshot as `subtitle_default_language`), and defaults to it (or "All languages" when Any). `runSubtitleSearch` passes the selected `lang` to `/api/subtitles/search`; the per-result download still uses each result's own language.

### Pending-state feedback (the responsiveness standard)

Every control that waits on a server round-trip must show an **immediate** pending state so the UI never looks dead. The single mechanism is `_markLoading(el, on)` + `withInflight(key, el, fn)` (defined just after `doSearch` in `static/index.html`):

- `_markLoading(el, on)` toggles the spinner: buttons get `.ctrl-loading` (an inline 18px spinner overlay + dim + `pointer-events:none`), `<select>`/`<input>` get `.ctrl-loading-form` (dim + `disabled`), and `aria-busy` is set either way. Since 12.7.5 it **also sets `el.disabled`** on anything that supports it, remembering the prior value in `dataset.wasDisabled` and restoring it when cleared. `pointer-events:none` blocks a mouse and nothing else — a marked `<button>` kept `disabled === false`, stayed focusable, and still fired its `onclick` from a native **Enter**, which is exactly what the `?tv=1` remote's OK button sends. Don't re-add manual `disabled` bookkeeping around a `_markLoading` call; it owns that now, and setting `disabled` yourself *before* clearing busy will be captured as the "prior" state and stick. It's a **no-op when `el` is falsy**, which is why functions called both manually (passing `this`) and programmatically (passing nothing) — e.g. `loadLibrary`, `loadComponents` — can share one body.
- `withInflight(key, el, fn)` wraps `fn` in `_markLoading(el, true/false)` (always cleared in `finally`) **and** dedupes by `key`: while a `key` is in flight, repeat invocations are dropped. Use it for any fetch-backed action; pass the clicked element so its spinner shows. For inline handlers add `this` to the `onclick` (`foo()` → `foo(this)`); for `addEventListener`-wired buttons pass the `btn` you already have.

The CSS (`@keyframes ctrl-spin`, `.ctrl-loading`, `.ctrl-loading-form`) lives in both `static/index.html` and `static/admin.html`. **`admin.html` carries its own copy of `_markLoading`/`withInflight`** (ported verbatim, same names) so admin buttons get the identical treatment. When adding a new server-waiting control, route it through this — don't invent a per-button `disabled` toggle. Controls with their own richer feedback (upload progress bars; in-region "Loading…/Searching…" text painted into a results box) are the only exceptions and are intentionally left without the overlay.

### Optimistic Play UI + in-flight guards

`continueLibraryItem`, `playLibraryFiles` **and `streamLibraryFile`** (12.7.5 — it was the odd one out, with neither guard nor optimistic paint) run under `withInflight("play_${itemId}", …)` — one shared key, so Play and Stream-this-episode cannot race each other either so a frustrated double-tap during a slow VLC handoff is dropped client-side instead of racing extra `in_play` requests to VLC. Before the fetch they call `_optimisticBuffering(label, itemId)` which:

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

Both the footer bar (`#seekBarWrapper`) and the fullscreen-controls bar share the `.seekBar` class and unified **Pointer Event** handlers (`handleSeekPointerDown`/`Up`/`Leave`) plus the hover tooltip (`handleSeekBarHover`/`Leave`). Use pointer events (not `onclick`/`ontouch*`): on `touchend` `e.touches` is an empty-but-truthy `TouchList`, so reading `e.touches[0].clientX` throws and used to leave the tooltip stuck over the bar on mobile — pointer events always carry `clientX`. A seek calls `POST /api/vlc/seek/to?position_pct=N` — VLC's `seek` uses `val=N%` for absolute and `val=±Ns` for relative. **Don't mix the two**.

**Accidental-click lock.** The bar defaults to **LOCKED** ("HOLD TO UNLOCK" padlock badge; the track keeps its colour at a mild 75% opacity so playback progress stays readable, and the badge background is translucent so the progress line shows through it — the badge, not a greyed track, signals the lock); taps are ignored and the tooltip is suppressed. Unlocking is a guided three-phase gesture, all phases still locked (`_seekPhase` 0 idle / 1 holding / 2 ready / 3 settling): **phase 1** `holding` (0.5s press-and-hold via `_seekHoldTimer`, indigo `seekUnlockFill` grows; a too-short release or mouse-leave aborts via `_cancelArming`), **phase 2** `ready` (hold satisfied — badge turns green and pulses "RELEASE", then **waits for `pointerup` with no timer**, so gripping the whole time can't seek), **phase 3** `settling` (started **only on release**, `_seekSettleTimer` runs a 0.5s green fill, badge "UNLOCKING…", then `unlockSeekBar`). Once unlocked, `_armSeekRelock` re-locks 5s after the last interaction, and `updateState` calls `lockSeekBar()` whenever playback isn't active. Note touch keeps implicit pointer capture, so its `pointerleave` only fires after release (phase 3, no abort); mouse leave during phase 1/2 aborts.

The whole lock is opt-out per device via **Settings → This Device → Locked Progress Bar** (`psSeekLock`, `toggleSeekLock`). The preference lives in `localStorage` (`streamlink_seeklock`, defaults ENABLED — only an explicit `"0"` disables) and is held in `_seekLockEnabled`. When disabled, `lockSeekBar()` keeps the bar permanently `unlocked` (single tap seeks, no gesture); `init`/`DOMContentLoaded` calls `lockSeekBar()` once so a page opened mid-playback honours a disabled lock immediately.

### Episode page (`#episodePage`, full-screen)

Replaces the legacy `#episodeModal` (Milestone 12). `openEpisodePicker(itemId, title)` opens it; under the hood it now reveals a full-screen view. Key DOM:

- `#epHero` — backdrop + poster + show title + meta line + 3–4-line overview. Backdrop uses TMDb `/w1280<backdrop_path>`; poster uses `/w342<poster_path>`. Backdrop/poster prefer custom metadata's absolute `backdrop_url`/`poster_url` over the TMDb paths (so hand-entered art renders with no key). Painted by `renderEpHero`. The title carries a pencil button (`#epRenameBtn`, revealed once the hero paints) → `renameSeries()`: prompts for a new series name (or movie title for a one-off), `POST`s `/api/library/{id}/rename`, then repaints the hero with the corrected metadata and `loadLibrary()`s the grouping label. This is the fix for a badly-named download that auto-matched the wrong show — the name drives the TMDb query. Next to it, a **Fix Metadata** button (`#epMetaFixBtn`) → `openMetaFix()` opens `#metaFixModal` for a finer correction: a **Pick a match** tab searches TMDb (`metaSearch()` → `GET …/metadata/search`) and lists candidates (`metaPick(id, kind)` → `POST …/metadata/set {mode:"tmdb"}`, pinned `source="manual"`); a **Custom** tab hand-enters title/year/rating/genres/overview + poster & backdrop image URLs (`metaSaveCustom()` → `POST …/metadata/set {mode:"custom"}`, pinned `source="custom"`, no TMDb key needed). Both share `_applyMetaResult()`, which repaints hero/season tabs/list and `loadLibrary()`s just like `renameSeries()`. Pinned metadata survives auto-refetch and rename. Below the meta line sits the **On-Demand Only** toggle (`#epOndemandBtn`, painted by `_renderEpOndemandBtn` from `epOndemandOnly`/`epOndemandLocked`/`epHlsAvailable` set in `loadFiles`) → `toggleEpOndemandOnly()`: `POST`s `/api/library/{id}/ondemand-only {enabled}` so a viewer can flip on-demand-only themselves. Hidden when HLS isn't available (macOS); rendered disabled with a lock glyph when the admin has locked the item (`ondemand_only_locked`) — the server also 403s a locked change. Admin-side counterpart lives in `admin.html` (`toggleOndemandOnly` + `toggleOndemandLock`).
- `#epSeasonTabs` — one button per season (hidden when there are none). `epSwitchSeason(s)` updates `epCurrentSeason` and re-renders. `renderEpSeasonTabs` calls `_scrollActiveSeasonIntoView` (via `requestAnimationFrame`) to horizontally centre the active tab in the strip — so on a many-season show the season the viewer is on is on-screen, not scrolled off (only the strip's `scrollLeft` is touched, never the page). Since 11.18.0 the list is the **union** of seasons on disk and seasons TMDb says the show has (`_epRebuildSeasonList`), and a season that is not fully downloaded carries a second line — a square dot plus `8 of 12` (`_epSeasonCounts`, `.ep-season-count` / `.ep-season-dot`), amber via `.incomplete` or red via `.absent` when nothing of it is here.
- `#epList` — scrollable episode list. Each card is a 16:9 still (TMDb `/w300<still_path>` or "S01·E02" placeholder), headline `S01·E02 · Episode Title`, 2-line overview, watch-progress bar overlaid on the still, plus inline Watched / Offline / Download buttons. Tapping the still calls `epPlayFrom(idx)`.

It also serves **downloading** series (opened from the card's ☰ Episodes button) — `/files` returns in-flight per-episode `dl_pct`/`complete`/`mode`, and the per-season scheduling bar manages what downloads — and **single-file movies / one-episode series** in **movie mode** (see `renderEpList` above; `epIsMovie` + `_epApplyMovieChrome` + `_epMoviePanel`).

**Bulk delete (free space, re-downloadable).** `#epBulkChips` has a red **🗑 Delete (N)** chip; `epDeleteSelected(btn)` → `epDeleteFiles([...epChecked], btn)` (the movie panel's Delete passes a single path). `epDeleteFiles` confirms, `POST`s `/api/library/{id}/delete-files`, then `refreshEpFiles()` + `loadLibrary()` and toasts the freed bytes (wrapped in `withInflight("ep_delete")`). The server marks the files Skip + removes the bytes, so the rows flip to "⊘ Not downloaded" with a ⬇ Download button. `updateEpCount` keeps `#epDeleteCount` synced with `#epSelectedCount`/`#epRecheckCount`.

State additions:
- `epMetadata` — cached TMDb payload for the open item (or `null`).
- `epMetaImgBase` — artwork base URL returned by the metadata endpoint (the host proxy `/api/metadata/img`).
- `epHeroTitle` — fallback title for late metadata repaints (SSE `metadata_update` → `_epApplyMetadata`); `_epMetaRetryTimer` backs up a missed event while the server fetch is `pending`.
- `epCurrentSeason` — visible season number, set by `pickDefaultSeason()`: season of the currently-playing file → season of the **most-recently-watched** episode (latest `progress.updated_at`) → first season with unwatched episodes → **first season that actually holds files** (never open on a season owned nothing of — a wall of "not downloaded" is a poor way to open a show) → first available. Landing on last-watched (not first-unwatched) means an early episode skipped/saved on purpose doesn't pull the picker back to an earlier season.
- `epSeasonList` — sorted seasons for the strip: positive seasons present in `epFiles`, **unioned with the show's TMDb seasons** when the missing-content diff is trustworthy, with `0` appended as a trailing bucket tab only when season-0 files exist. That tab is **named after its contents** (`_epSeason0Label`): one bucket ⇒ that folder's name (*Movies*, *OAD*, a spin-off's title); several ⇒ *Extras*; none ⇒ *Specials*. Rebuilt by `_epRebuildSeasonList()` on open, whenever metadata lands, and when the admin flips the policy.
- `epIsMovie` — single-file item flag; switches the page to the movie panel + chrome. `_epReconsiderMovieMode()` un-sets it once metadata lands if the item is a TV entry with actual gaps — one episode of a twelve-episode season is not a movie, and the panel has nowhere to list the other eleven.
- `missingContentEnabled` / `missingUnaired` — the admin missing-content policy, mirrored from `/api/state` and refreshed by every `state` SSE event (an open episode page repaints on change).

Per-episode ▶: `epPlayFrom(globalIndex)` slices `epFiles` from the tapped index forward (using the original full-list index, *not* the per-season filtered index), respects resume position on the first file, plays as a playlist. This means "press play on episode 3" plays 3 → 4 → 5 …, not just 3.

**Shuffle Play.** The bottom action bar's **Shuffle** button (`#epShuffleBtn`, hidden in movie mode via `_epApplyMovieChrome`) → `epShuffle()` opens `#shuffleScopeModal`, which asks **Unwatched only** vs **All episodes** (live counts; the unwatched option is `disabled` when nothing's unwatched). `startShuffle(scope)` builds the pool from `epFiles` (filtering `!progress.completed` for unwatched), Fisher–Yates shuffles the paths (`_shuffleInPlace`), and plays via `playLibraryWithChooser(itemId, paths, 0, label, /*shuffle*/true)` — always from the top (seek 0; shuffle is a fresh run, not a resume). The `shuffle` flag **and `scope`** thread through `pcCtx` → `playLibraryFiles`/`lpPlay` so the server records the random order (TV Next/Prev follow it); the on-device path shuffles for free since the shuffled `paths` become `lp.playlist` verbatim (multi-file lists are kept as-is), with `lp.shuffle` flagging the session.

**Shuffle persists across a stop (the *preference*, not the order).** The live shuffle order is ephemeral (cleared on stop), but whether a play was Shuffle Play — and its `scope` — is recorded per (item, profile) in `library.json` and surfaced on the resume hint (`resume.shuffle` / `resume.shuffle_scope`). The VLC `/play` path persists it inline (`shuffle_scope` in the body); the device path and the leave-shuffle controls call `setShufflePref(itemId, shuffle, scope)` → `POST /api/library/{id}/shuffle-pref` (the device has no `/play` round-trip). When **Resume / Play All** (`resumeLibraryItemWithChooser`) is tapped on a show whose last session was shuffled, `#shuffleResumeModal` asks **Continue shuffle** (`startShuffleForItem` — fetches `/files`, rebuilds the pool by scope, `_shuffleInPlace`, re-routes through the chooser) vs **Normal order** (`_resumeNormal`, the original natural-order resume). Any normal play clears the flag. **Continue shuffle resumes the in-progress episode**: `startShuffleForItem` takes the resume hint (threaded from `_shuffleResumeCtx.resume` by `shuffleResumeContinue`) and, when it points at a mid-episode file (`pct > 3`, not all-completed), pins that file to the front of the shuffled pool and passes its `position_sec` as `seekTo` — so the current episode continues where it stopped (matching Normal order) with the rest still shuffled, rather than starting a fresh random run on a different episode. (playlist[0] is then the resume file, so the explicit `seek_first_to` seeks it correctly — no conflict with the backend hint-match guard in GOTCHAS § shuffle-seek.)

**Leave Shuffle mid-playback** (no rebuffer — only the upcoming queue flips to natural order). *TV:* the fullscreen header's new **More** button (`#fcMoreBtn` → `fcToggleMore()`) opens the `#fcMorePanel` sheet that now houses **Night mode** (relocated `#fcNightBtn`), **Clip** (relocated `#fcClipRow`), and **Exit Shuffle** (`#fcExitShuffleBtn`, shown by `renderPlayer` only while `s.library_shuffle`) → `fcExitShuffle()` → `POST /api/library/unshuffle`. *Device:* an **Exit Shuffle** row (`#lpExitShuffleRow`) in the gear menu, shown by `_lpRenderExitShuffle()` (in `lpToggleOpts`) only when `lp.shuffle` → `lpExitShuffle()` reorders the tail of `lp.playlist` client-side and `setShufflePref(false)`.

`closeEpisodeModal()` is kept as a back-compat alias for `closeEpisodePage()` so existing callers (refreshEpFiles, mark-watched, keyboard Escape handler) continue to work without changes.

#### Missing content — seasons & episodes not on disk (11.18.0)

The episode page diffs the show's TMDb season/episode inventory against what is actually on disk, so a season you own nothing from **and** a single-episode hole mid-season both show up as rows instead of the list quietly closing over them. Gated by the admin policy (`settings.missing_content`, see [ADMIN.md](ADMIN.md)).

**Shared gap-set core.** The search picker asked the same question first, so the primitives live once and both sides call them: `_tmdbSeasonNumbers(meta)` (every season the show has — prefers the metadata's `all_seasons` inventory over the `seasons` map, which only covers seasons episodes were fetched for), `_tmdbSeasonEpisodes(meta, season)`, `_tmdbEpUnaired(te)`, `_tmdbAbsoluteNumbered(meta, seasonsSeen)`, and `_missingTmdbEpisodes(meta, season, have, includeUnaired)`. All that differs between the callers is what `have` means: `_ssMissingEpisodes` passes the episodes some search found a torrent for; `_epMissingEpisodes` passes the episodes with a file on disk.

**Trust gate — `_epMissingBlockedReason()`** returns `""` when the diff may run, else why not:

- `"off"` — the admin policy is disabled.
- `"nometa"` — no metadata, not a TV entry, or no seasons known.
- `"absolute"` — `_tmdbAbsoluteNumbered`: a long-running anime TMDb files as one 1000-episode "Season 1". Diffing it invents hundreds of phantom gaps.
- `"unparsed"` — more than `EP_MISSING_MAX_UNATTRIBUTED` (25%) of the item's files have no parsed season/episode (`_epUnattributedFiles`). A file carrying a `bucket` does **not** count: the backend placed it outside the numbered run deliberately, and counting specials/extras as parse failures stood the diff down on exactly the well-organised batches it handles best. Those are real episodes hiding outside the season model, so every season would read as short. This was the GitHub #15 case before 11.19.0 taught the backend to read the season off the **folder** (see [LIBRARY_DATA.md](LIBRARY_DATA.md) § Season/episode attribution); it still guards genuinely unnumbered releases, and is surfaced as a visible note in the list rather than silence — silence there is indistinguishable from "nothing missing".

Season 0 never participates in the diff (specials/OVAs/extras would leave nearly every anime show perpetually short). Unaired episodes (`air_date` in the future or absent) are dropped unless `missingUnaired`, in which case they get a third state — **Upcoming**, with no Find button.

**Rendering.** `renderEpList` builds `rows` as the union of the season's unwatched files and `_epMissingEpisodes(epCurrentSeason)`, sorted by episode number (only re-sorted when there IS something to interleave, so a complete season keeps its existing order). A missing episode renders as `_epMissingCardHtml(season, te)` — the same `.ep-card` geometry with `.ep-missing` (dimmed, dashed still frame, greyscale still, square status dot, **no** play affordance). Above the grid a season-level strip shows either the `"unparsed"` note or "N episodes not downloaded" plus a **Find sources** button. A season with no files *and* no fetched episode list gets a dedicated empty state ("None of this season's 12 episodes are on this box") rather than "No episodes in this season". **The season-0 tab renders differently:** when its files come from more than one folder it is grouped, with a full-width `xl:col-span-2` heading per bucket (`_epSeason0Buckets` sets both the order and the headings), so *Extras* / *Movies* / *OAD* / a spin-off read as named runs instead of one undifferentiated scroll. No missing-row interleave there — a diff only ever runs on a real season.

**Handoff — the library grows no hunting logic of its own.** `epFindEpisode(season, ep)` and `epFindSeason(season)` build a TMDb candidate from `epMetadata` (`_epSearchCandidate`) and call `openSearchShowFromTmdb(cand, opts)`, which now accepts `{season, episode, findMissing, episodes, section}`: it lands on that season's tab and fires the existing `ssSearchEpisode` / `ssFindMissing` / `ssSearchSeasonPacks` (a thin wrapper over `ssSearchSeason`). **The candidate's `title` must be `epMetadata.title`, not `epHeroTitle`** — the latter is the item's display name, which for an unrenamed pack is the raw release name, and it becomes every indexer query the show page makes (see [GOTCHAS.md](GOTCHAS.md) § a display name is not a search query). Since 12.7.1 `_ssQueryTitle()` is the single resolver for query titles and backstops this. `epFindSeason` passes the gap set **measured against disk** in `opts.episodes` (the show screen only knows which torrents it found, so left alone it would sweep every episode of the season, owned ones included), and picks `section:"packs"` when nothing of the season is owned — one season pack beats twelve episode downloads. `#searchShowPage` stacks over `#episodePage` (both `z-50`, it is later in the DOM), so its Back returns to the library page intact; `closeSearchShow` keeps the body scroll lock while the episode page is still open, and the Escape handler closes the stack top-down.

**Metadata top-up.** `_epTopUpSeasons(stillOpen, title)` fetches every season's episode list from `/api/tmdb/lookup` (by `tmdb_id` when known, else by title) when the cache is short of them — measured against the TMDb **inventory**, not the parsed season count, since the parsed count is exactly what is unreliable. Used by both the single-item path (`_epApplyMetadata`) and the merged-series path (`openSeriesPage`), replacing the old merged-series-only title lookup.

#### Library coverage — "do I already have this?" (11.22.0)

Search had no way to ask the library anything, so the Death Note poster card and every one of its episode rows read identically whether all 37 episodes were downstairs or none were. **`GET /api/library/coverage`** (see [API.md](API.md)) is the join, and one small module in `index.html` owns the client side of it:

- **`loadCoverage()`** fetches once and memoises on `_covPromise`; **`refreshCoverage()`** drops the cache and refetches. `loadLibrary` folds `refreshCoverage()` into its existing `Promise.all`, so the grid's chips paint with the **first** render rather than repainting after it. `doSearch` rides it alongside the `/api/tmdb/search` call; `openSearchShowFromTmdb` tops it up when a deep link or Explore reaches the show page before Search ever rendered.
- Two indexes, because callers arrive from two directions: **`_covFor(kind, tmdbId)`** (`_covByKey`, `"tv:124101"`) for a TMDb candidate, **`_covForSeries(seriesKey)`** (`_covBySeries`) for a library tile.
- Readers: `_covSeasonEps(row, season)` (a `Set` of owned episode numbers — `have` ∪ `pending`, since either answers "do I still need to fetch this?"), `_covHasEp`, `_covEpPending`, `_covEpisodeCount`, `_covSeasonRange` (`"S1-S5"`), `_covSummary` (`"37 episodes · S1"` / `"Downloaded"`), `_libNewSeasons(row)` (the server-computed `missing_seasons`).

**Search results.** `_renderTmdbResults` paints a green **In Library** badge + a green left edge + an owned summary line on any card `_covFor` matches, and appends `"· N seasons to get"` when `missing_seasons` is non-empty. Explore's `_exBuildLibKeys` now unions `_covByKey`'s keys into `_exLibKeys` (and `_exEnsureLib` awaits `loadCoverage`), so a show whose newest episodes haven't resolved TMDb metadata still counts as owned for the badge and the Hide-owned filter.

**Search show page.** `_ssOwned` holds the coverage row for the open show — set straight from the candidate id on the TMDb-first path, and only once metadata lands (`_ssResolveOwned`) on the Jackett-fallback path, which starts with nothing but a release title. Everything downstream reads it through `_ssOwnedEps(season)` / `_ssOwns(season, ep)`:

- **`#ssLibraryBanner`** in the hero (`_ssRenderLibraryBanner`, painted from `_ssRenderHero`) — "Already in your library — 37 episodes · S1" + **Open in library** (`ssOpenInLibrary`: closes the page; when it was stacked over `#episodePage` by a library handoff, closing is all that's needed, else it switches to the Library tab and opens `openSeriesPage` / `openEpisodePicker`).
- **Season tabs** carry `owned/total` (`seasons_total` from coverage, falling back to `_tmdbSeasonEpisodes`), green once complete.
- **`_ssEpRowHtml`** adds `_ssOwnedBadge` (**Downloaded** / **Downloading**) and an emerald left edge.
- **`_ssOwnedRowHtml`** replaces `_ssMissingRowHtml` for an owned episode no search found a source for. The old greyed "No source found" row was untrue in spirit and an invitation to fetch it again; the owned row offers a quiet **Replace** instead.
- **`_ssMissingEpisodes`** folds owned episodes into its `have` set, so they are excluded from the "N missing a source" banner and from `ssFindMissing`'s sweep. **`_ssBulkScopeEpisodes`** skips them too — Auto would otherwise re-download a season you already hold every time it ran — and `_ssRenderBulkChoose` reports the skipped count and renders "Nothing left to download" instead of an empty scope.
- **`_ssPackGroupHeader(label, count, owned)`** takes a third arg; `_ssRenderPacksGrouped` passes "Complete in your library" / "You have 8 of 10" per season heading.

**Library grid.** `_libNewSeasonChip(row)` renders a green **"Season 5 available"** / **"2 new seasons"** chip whenever `missing_seasons` is non-empty (hidden in `TV_MODE`, and when the show has no `tmdb_id` to search with). It is wired in **three** places so no view loses it: `_libItemChrome` exposes it as `c.newSeasonChip` (covering both `_libItemListHtml` and `_libItemCardHtml` — a show downloaded as one season pack is a single item and never reaches the show-tile renderers), and `_libShowTileHtml` / `_libShowCardHtml` append it to their own `badges`. `libGetSeason(seriesKey, season)` builds a TMDb candidate straight from the coverage row and calls `openSearchShowFromTmdb(cand, {season, section:"packs"})` — Packs first, because one season pack beats ten episode downloads.

#### Merged-series mode (one cohesive show)

A show whose episodes were downloaded individually is many separate items sharing one `series`. In the library `renderLibrary` collapses such a group (`gItems.length>1 && gItems[0].series`) into **one show tile** (`_libShowTileHtml`, or `_libShowCardHtml` in card view) — Episodes → `openSeriesPage(seriesKey)`, ▶ Play/Resume → `playSeries(seriesKey, mode)`. Both show-tile renderers append a shared **`_libShowIconBtns(gItems)`** cluster (hide/restore eye + delete trash) so a whole show — **including one still downloading** — can be hidden or deleted, matching the single-item chrome. These fan out over every item in the series: `toggleSeriesVisibility(ids, hidden)` calls `POST /api/library/{id}/visibility` per item, `deleteSeries(ids, title, btn)` calls `DELETE /api/library/{id}?delete_file=true` per item (confirm-gated, `withInflight`). All items in a show tile share one visibility bucket (`renderLibrary` splits visible vs hidden *before* grouping), so `gItems[0].hidden` is authoritative for the eye's direction. `openSeriesPage` reuses the *same* `#episodePage` renderers but sources `epFiles` (each carrying `item_id`) from `GET /api/library/series/{key}`, sets `epSeriesKey` + `epFileItem` (path→item_id), and enriches metadata via `/api/tmdb/lookup` (member items only cache their own seasons). Play actions (`epPlay`/`epPlayFrom`/`startShuffle`) compute a parallel `items[]` via `_epItemsForPaths` and pass it through `playLibraryWithChooser` → `playLibraryFiles`/`lpPlay` → the `/play` body's `items[]` (TV) or `lp.playlistItems` (device) so **both** players span item boundaries. Per-file/per-bulk management stays item-aware: `_epCardHtml` keys off `f.item_id`, `epToggleWatched` uses `_epItemFor(path)`, and `epMarkWatched`/`epDeleteFiles` fan out per item via `_epGroupByItem`. **Rename / Fix-Metadata / On-Demand-Only now work in merged mode too** — they fan across the whole series server-side: `renameSeries` hits the group-aware `/rename` (any member id) and re-opens the page under the new key; `metaPick`/`metaSaveCustom` route through `_epMetaSetUrl()` → `POST /api/library/series/{key}/metadata/set`; `toggleEpOndemandOnly` hits `POST /api/library/series/{key}/ondemand-only` and renders from the aggregate `ondemand_only`/`ondemand_only_locked` returned by `/series/{key}`. `_epTargetId()` picks a representative member for reads (metadata search) and `_epStillOpen(itemId, seriesKey)` guards against navigating away mid-request (single-item mode tracks `epItemId`, series mode tracks `epSeriesKey`). `renderEpHero` un-hides all three in both modes (On-Demand still hidden when `!hls_available`). Only the ZIP "Download selected" remains per-item-only (hidden in merged mode). **Device:** `lp.playlistItems` + `_lpLoadIndex` re-point `lp.itemId` to the current file's owner on every advance so prep/stream/progress target the right item (see [GOTCHAS.md](GOTCHAS.md) § cross-item series playback); only the app's *offline bundle* prefetch-ahead stays per-item.

#### TMDb-first search + show detail (`#searchShowPage`)

**Search is TMDb-first.** `doSearch(q)` first hits `GET /api/tmdb/search?query=…` (kind narrowed by the Categories picker via `_searchTmdbKind`) and renders the candidates as **poster cards** (`_renderTmdbResults`, stored in `_tmdbResultsById`) — the user picks a real show/movie *before* any indexer is touched. A card click → `openSearchShowFromTmdb(cand)`.

**Smart / Classic toggle.** A **`#searchModeWrap`** segmented control sits beside the Sources/Categories selectors under the search box (`#searchModeSmart` / `#searchModeClassic`, painted by `_renderSearchModeToggle`). `searchMode` is a **device-local** preference (`localStorage.streamlink_search_mode`, `"smart"` default | `"classic"`), seeded at parse time, painted at DOMContentLoaded and re-painted from `loadSearchIndexers`. `setSearchMode(mode)` persists + repaints. When `searchMode==="classic"`, `doSearch` short-circuits to **`doClassicSearch`** — the **pre-9.0 flat indexer list** (before the `d68008e` show-grouping): it renders `/api/search`'s flat `results[]` as one card per raw torrent (title · size · seeders/peers/tracker) with **Save** (→ `openDownloadModal`) and **▶ Play** (→ `openStreamPicker`) buttons, no grouping and no TMDb. This is the user-facing escape hatch for TMDb mismatches / wrong grouping. Note this is distinct from `doJackettSearch` (the *grouped* legacy search) used by the automatic no-key / no-match fallback below, which is unchanged.

- **No-key / no-match fallback.** When `/api/tmdb/search` returns `enabled:false` (no TMDb key) **or** zero results, `doSearch` falls back to `doJackettSearch(q, …)` — the **legacy Jackett-first grouped search** (`groups[]` from `/api/search` rendered as one card per show via `_ssGroupsById`; a movie-only single-torrent group taps straight to `openDownloadModal`, otherwise `openSearchShow(group)`). This path is unchanged, so nothing regresses without a key.

`#searchShowPage` is a `#episodePage`-styled full-screen view with a TMDb hero and two sections — **Single Episodes** and **Packs**. It opens from **two entry points**:
- **`openSearchShowFromTmdb(cand)`** (TMDb-first) — builds a synthetic `_ssGroup={title,year,results:[]}`, sets `_ssKind` (`"tv"`/`"movie"`), fetches the **exact** metadata by id (`/api/tmdb/lookup?tmdb_id=&kind=`), and renders the full season/episode skeleton with **no torrents yet**. Jackett runs only on explicit action (below).
- **`openSearchShow(group)`** (Jackett fallback) — seeds `_ssEpisodes`/`_ssPacks` from the group's already-fetched results and looks up metadata by title. `_ssKind=""` here.

**Trailers.** The metadata payload carries YouTube trailer keys (`metadata.trailer` + per-season `seasons[n].trailer` — see [API.md](API.md) `/api/tmdb/lookup`). The hero meta-line row holds a **Trailer** button (`#ssTrailerBtn`, shown/labelled by `_ssUpdateTrailerBtn` — called from `_ssRenderHero` *and* `_ssRenderBody` so it tracks season-tab switches): a movie plays its film trailer; a TV show follows the selected season (`_ssTrailerFor` — season trailer when TMDb has one, labelled "S*n* Trailer", else the show trailer; hidden when neither exists). `ssPlayTrailer` → `openTrailerModal(key, title)` opens `#trailerModal` (z-[60], `youtube-nocookie.com/embed` iframe, autoplay; `closeTrailerModal` blanks the iframe `src` — that's what stops the audio; backdrop click and the TV-remote Back both close it via the `*Modal` convention). Its **Watch on TV** button (`trailerToTv`) POSTs the bare video id to `/api/youtube` — the existing YouTube-on-TV kiosk path — and closes the embed. Works on both entry points (the Jackett-fallback page gets the same metadata shape by title).

**Theater-only banner.** `_ssRenderHero` calls `_ssRenderTheaterBanner`, which shows `#ssTheaterBanner` (amber, "In theaters only — no home release yet; downloads may be cam-quality", plus the `digital_date` when TMDb has one) whenever the movie metadata carries **`theatrical_only:true`** — a movie with a past theatrical release but no digital/physical/TV release yet, so only cam torrents exist (see [API.md](API.md) `/api/tmdb/lookup`; computed by `_movie_release_flags`). Hidden for TV and released movies; reset to hidden on every show open.

**Explicit in-show Jackett search.** Opening from TMDb shows Search buttons instead of pre-loaded torrents:
- **Search packs** (`#ssSearchPacksBtn` → `ssSearchPacks`) — the broad query, then a targeted `<title> S<NN>` sweep (`ssSearchSeason`) for every season still without a pack, because the broad show-title query misses later seasons routinely (see [GOTCHAS.md](GOTCHAS.md) § season packs). Since 12.7.0 the sweep runs **detached** in the background pill, so the button returns as soon as the broad pass lands, and the targeted query buckets that season's **episodes** as well as its packs — one request answers both tabs. The tab renders via `_ssRenderPacksGrouped` — one heading per season in ascending order (seasons with nothing found get a **Find** button), then multi-season/complete, then the unrelated `movie`-kind rows behind an "Other results" disclosure. `_ssPackRowHtml(p, i)` takes the index into `_ssPacks`, not the rendered position, since `ssDownloadPack`/`ssPlayPack` resolve by it. Movies and absolute-numbered anime keep the flat list.
- **Search episodes** (`#ssSearchEpBtn` → `ssSearchShowEpisodes`) — one broad `/api/search?q=<title year>` query, buckets `kind==="episode"` into `_ssEpisodes` (merged/deduped by magnet) **and** the non-episode results into `_ssPacks` (the broad query is shared, so the Packs tab and the bulk sheet's pack recommendation are ready without a second tap).
- **Search packs** (`#ssSearchPacksBtn` / the packs-section empty-state button → `ssSearchPacks`) — keeps the non-episode results (`kind ∈ season|multiseason|movie`) into `_ssPacks`. `_ssPacksSearched` distinguishes "not searched yet" (shows the Search-packs CTA) from "searched, none found".
- Both buttons share **one cached** request via `_ssBroadSearch()` (result stashed in `_ssBroadResults`) so they never double-hit the indexers. `_ssRender` broadens tab/section visibility for the TMDb-seeded page (`_ssKind` set ⇒ show the sections even while empty); movies default to the Packs (**"Downloads"**) section with no episodes tab.

**Audio-language preference.** Backend-enriched results carry `audio` (`dual`|`dub`|`sub`|`other`|`""`) + `audio_lang` (see [API.md](API.md), [GOTCHAS.md](GOTCHAS.md) § audio-language classification). An **Audio** chip row (`#ssAudioChips`, rendered by `_ssRenderAudioChips` from `_ssRenderBody`) appears **only when the loaded results mix audio variants** (`_ssHasAudioVariety` — any `dual`/`dub`/`other`; plain English shows never see it): **Any / English / Original + Subs**. The pick is device-local (`localStorage.streamlink_audio_pref`, `_ssAudioPref`) and steers everything: `_ssAudioMatch` (dual satisfies both non-Any modes; untagged/subbed count for English **iff** the show's TMDb `original_language` is `en` — `_ssOrigEnglish`, unknown ⇒ English — since plain English releases never say "English"; untagged always counts for Original), `_ssPreferredSource` (episode rows headline the best *matching* source with a square audio badge — `_ssAudioBadgeHtml` — plus a red "No Eng audio"/"Dub only" flag when nothing matches), `ssOpenSourceSheet` (matching sources first, non-matching dimmed, badges everywhere), `_ssPackCmp` (rel bucket → pref → rel → seeders) and the bulk Auto picker below.

Each row → `ssOpenSourceSheet` picks a torrent → `_ssDownloadOne`; **Bulk download** `ssOpenBulkSheet` → Auto (`ssBulkAuto`) or Choose-per-episode (`ssBulkManualStart`). Since 12.7.0 both **close the sheet immediately and run detached** (`_bgRun` → the `#globalFindBar` pill; see § Background source-finding below): `_bgEnsureSources` does a broad search if `_ssEpisodesSearched` is false, then **one targeted `<title> S<NN>` query per season in scope** (the pass that actually finds anything on a long-running show), then a per-episode `<title> SxxEyy` mop-up for whatever is still bare — and Auto goes straight on to `_bgStartDownloads`. Choose-per-episode reopens the sheet on the picker when the find lands (or toasts, if the user has navigated away). Scope is `_ssBulkScopeEpisodes()` — TMDb’s episode list unioned with found episodes, so an episode with **no** sources stays in scope and is reported rather than dropped (see [GOTCHAS.md](GOTCHAS.md) § bulk download counting); a pack → `ssDownloadPack` → the Add-to-Library modal. **Stream-now (11.1.0, persists since 11.2.0):** every episode row, source-sheet row, and pack/movie row also carries a ▶ **Play** button (`ssPlayEpisode` — preference-best source, tagged with `{series,season,episode}` — / `ssPlaySource` / `ssPlayPack`) that opens the stream file picker (`openStreamPicker(magnet,title,btn,ctx)` → `/api/stream/prepare`); `selectStreamFile` then POSTs **`/api/library/play-now`** (not the old transient `/api/stream`), so the play **persists to the library** and Stop no longer deletes it. A multi-file pack shows the picker so the user chooses the episode; the whole pack downloads and persists. Because the Explore tab opens this same show page, Explore gets persisting stream-now for free. **Streamability flags (11.3.0):** `_streamHealth(bytes, seeders)` scores how well a source plays *while it downloads* (seeders needed ≈ 8/GB, floor 6 → `good`/`fair`/`slow`/`dead`; `null` when seeders unknown), rendered as a square Metro badge (`_streamHealthBadge`) on classic-search cards, episode rows (`_ssEpRowHtml`), source-sheet rows (`ssOpenSourceSheet`) and every video row in the picker (`renderStreamPickerList`). All the stream-now callers thread `{seeders,size}` into `openStreamPicker` → `streamPrepData.seeders`, which gates two things: a **single-file torrent auto-plays only when `good`** (`_streamHealthAutoPlay`; `fair`/`slow`/`dead` keep the picker open with a `_streamHealthSubtitle` warning instead of instantly casting to the TV), and `selectStreamFile` **warns before streaming** a `slow`/`dead` file (`_streamHealthRisky` → `window.confirm`), scored on the **selected file's** size so packs are judged per-episode. See [GOTCHAS.md](GOTCHAS.md) § streamability scoring. The bulk choose sheet leads with a **"Season pack available" recommendation card** when a pack covers the selected scope (`_ssBestPackForScope`: season pack matching the scoped season, or a multiseason/Complete pack spanning it; `rel ≥ 0.7` required since `_ssPacks` carries every group from the broad search; audio-preference-matching packs win, then `_ssPackCmp`) — "Download the pack instead" (`ssBulkUsePack`) routes to the normal Add-to-Library modal. **Auto** honours the **Audio preference first** (`_ssAutoPick` runs its whole cascade inside the matching-source subset when one exists — language consistency beats seeders; episodes with no matching source fall back to best-seeded and are counted in `_bgStartDownloads`'s final toast via its `note` arg), then three optional limits set in the choose sheet (`_ssBulkFilters`, persisted across scope/re-render): **Min seeders**, **Min size (GB)**, **Max size (GB)**. `_ssAutoPickFrom` selects with seeders always breaking ties: (1) best-seeded that clears the seeder floor **and** fits the size window; (2) else best-seeded that clears the floor (size relaxed — **seeders win over size**); (3) else best-seeded outright. Limits apply to Auto only; Choose-per-episode is unaffected — but its per-episode `<select>`s default to Auto's (audio-aware) pick and prefix each option with its audio class (`[DUAL]`/`[DUB]`/`[SUB]`/`[<LANG>]`). Every episode/pack download is tagged `series = group.title` so it coheres into one library show (via `openDownloadModal(magnet,title,{series})`).

**Missing-episode detection + follow-up search.** The Single Episodes list cross-references the show's TMDb episode list (`_ssMissingEpisodes(season)` — a thin wrapper over the shared `_missingTmdbEpisodes`, see § Missing content): episodes TMDb lists but no torrent was found for render as greyed **"No source found"** rows (`_ssMissingRowHtml`) with a **Find** button → `ssSearchEpisode(season,episode)`, which fires a targeted `/api/search?q=<show> SxxExx` (or `<show> <n>` for absolute-numbered anime — `season≤1 && episode≥100`), keeps only results whose parsed `(season,episode)` match, and merges them into `_ssEpisodes` (dedup by magnet, re-sorted by seeders). A season-level **Find sources** banner (`ssFindMissing(season, only?)`) walks every missing episode throttled, re-rendering as each lands; `only` narrows the sweep to an explicit set of episode numbers, which is how the library page hands over the gaps it measured against disk. The season tabs use `_ssAllSeasons()` (TMDb ∪ available) so an entirely-missing season still gets a tab. `_ssSearching` guards duplicate in-flight per-episode searches.

#### Explore tab (`#exploreTab`)

Query-less TMDb browsing — the third top-level tab (Search · **Explore** · Library, `#tabExplore` / `switchTab("explore")` → lazy `loadExplore()`, one-time init guarded by `_exLoaded`; a no-key result resets the guard so configuring a key later just needs a tab re-visit). Backed by `GET /api/tmdb/genres` + `GET /api/tmdb/explore` (see [API.md](API.md) § Search). Not VPN-gated — it's pure TMDb metadata; the torrent actions inside the show page keep their own gating.

- **Rails view (default, `#exploreRails`)** — one horizontal poster row per curated list, from `_exRailDefs()`: with no kind filter → Trending Now (mixed), New Movies, New On TV, Popular Shows, Popular Movies, Top Rated Shows, Top Rated Movies; with a kind filter → the four lists for that kind. Rails load concurrently (`_exFillRail`, skeleton tiles via `_exSkeletonTiles` while pending; an empty/failed rail removes itself), each capped at 20 tiles with an **All ›** header button → the grid view for that list. Rows are `overflow-x-auto` (covered by the blanket `overscroll-behavior-x:contain` rule).
- **Filters** — an **All / Shows / Movies** kind row (`exSetKind`) plus a scrollable **multi-select genre chip** row (`#exGenreChips`, from the merged `/api/tmdb/genres` list, filtered to chips valid for the active kind; `exToggleGenre` adds/removes from `_exSelGenres`, sent comma-joined = **AND** on the server). Switching kind re-renders chips and drops selected genres the new kind lacks. Below the kind row a **filter bar** (`_exRenderFilterBar`): **Sort** (`#exSort` → `exSetSort`: Popular / Top Rated / Newest), **min rating** (`#exMinRating` → `exSetMinRating`), **year/decade** (`#exYear`, built by `_exBuildYearOptions` relative to the current year → `exSetYear`, encodes a `min-max` range), and a **Hide owned** toggle (`#exHideOwned` → `exToggleHideOwned`). Any active genre/sort/year/rating filter (`_exFiltersActive`) switches to the grid view.
- **Inline search** — `#exSearch` (top of the filter block) → `exSearchInput` (300 ms debounce) sets `_exSearchQ` and calls `_exApply`, which fetches **`/api/tmdb/search`** (kind-scoped) and renders the poster grid via `_exShowSearch` (own `_exSearchSeq` guard). `exClearSearch` / the ✕ button empties it. Search wins over filters in the `_exApply` dispatch order (search → filter grid → rails).
- **Grid view (`#exploreGridWrap`)** — a paged poster grid. `_exOpenGrid(st)` takes the full param object `{kind, section, genres[], sort, yearMin, yearMax, minRating, title}`; `_exFetch(p, page)` builds the query from it; `exLoadMore` fetches page N+1 under `withInflight("ex_grid")`, `#exMoreBtn` hides at `total_pages`. Two entry points: a rail's **All ›** (`_exOpenGrid` with just kind+section) and the active filter set (`_exOpenFilterGrid`). The Sort control replaces the old per-genre section sub-chips (`#exGridSections` stays hidden). **‹ Back** (`exCloseGrid`) clears **all** filters + search and returns to rails. `_exSeq` invalidates in-flight fetches on any view/filter change.
- **In-library / theater badges** — `_exBuildLibKeys` builds `_exLibKeys` (a `kind:tmdb_id` set) from `window._libCache` (the `/api/library` list, now carrying `tmdb_id`/`tmdb_kind`; `_exEnsureLib` fetches it once if absent). A tile whose candidate is in the set gets a green **In Library** badge (`_exInLib`); **Hide owned** filters those out of every rail/grid/search. A movie released within ~45 days (`_exInTheaters`, from the candidate's full `date`) gets an amber **In Theaters** badge — a heuristic the show page confirms precisely (see the theater-only banner below).
- **Tiles** (`_exTile(c, grid)`) — 2:3 poster (w185, native `loading="lazy"`) with the overlaid badges, title, TV/Movie chip, year, ★ rating; fixed-width in rails, fluid in the grid. A click calls **`openSearchShowFromTmdb(c)`** — the candidate shape matches `/api/tmdb/search`, so the whole show page (trailer, episode skeleton, torrent search, downloads) is shared with Search.
- **No TMDb key** — `#exploreDisabled` ("Explore needs a TMDb key") instead of filters/rails.

#### TMDb metadata

**Never gates the page.** `openEpisodePicker` kicks the `/metadata` fetch off in parallel with `/files` but renders hero/tabs/episodes as soon as `/files` returns; `_epApplyMetadata(itemId, title, respPromise)` applies the metadata response whenever it lands and repaints (`renderEpHero` + `renderEpSeasonTabs` + `renderEpList`). If the server answers `pending:true` (first-ever TMDb fetch still running against a slow/dead internet link), the page waits for the `metadata_update` SSE event (handler re-calls `_epApplyMetadata` with `epHeroTitle`) with an 8 s `_epMetaRetryTimer` re-pull as fallback. `img_base` is now the host's artwork proxy (`/api/metadata/img`), so posters/backdrops/stills keep working on the LAN with the internet down once cached (see [API.md](API.md)).

Returns `{enabled, img_base, metadata, pending}`. When `enabled=false` (no TMDb API key configured) or no match was found, the page degrades gracefully:
- No backdrop / poster / stills.
- Episode headlines fall through to `parseEpisodeInfo` (filename parsing).
- Season tabs still work because they're built from `f.season` parsed off disk, not from TMDb.

Admin sets the key under **Admin → Indexers → TMDb Metadata** (`POST /api/admin/settings { tmdb_api_key }`).

### Group page (`#groupPage`) and sections (15.0.0)

One full-screen "shelf" serves two shapes: a **franchise** (`openGroupPage({kind:"group", id})`
→ `/api/library/group/{id}`) and a **multi-section show** (`{kind:"show", seriesKey, itemId}`
→ `/api/library/series/{key}`). Rows (`_grpRowHtml`) open the member's or section's own normal
page (`_grpOpenRow`) or play it (`_grpPlayRow`); the shelf never renders episodes itself.
Franchise-only chrome: Story/Release toggle (`setGroupOrder`, shown only when
`has_story_order`), rename (`renameGroup`), and "+ Add title" (`openGroupAdd` →
`#groupEditModal`, `_grpEditMembers`). **Show not downloaded** (15.1.0, `grpShowMissing`,
localStorage `streamlink_grp_missing`, default on; shown only for a group with a
`collection_id` while `missingContentEnabled`) fetches with `include_missing=1`;
`_grpMissingRows` rows are merged with the owned rows by `_grpSortRows` (a JS mirror of
`_member_sort_key`) and drawn by `_grpMissingRowHtml`. **Get** → `grpGetFilm`: a detached
movie search (`/api/search?q=title&year=`), top relevance tier only, `_ssAutoPick` with the
bulk filters, `POST /api/library/download` with `tmdb_id`/`tmdb_kind:"movie"`, then pokes
`/metadata` so it binds at once; `_grpFilmQueued` blocks a second press. **Choose** →
`openSearchShowFromTmdb` (Full profiles only).

Library grid: `loadLibrary` also calls `_libFetchGroups` (`libGroups`, `libGroupedKeys`);
`renderLibrary` drops claimed tiles and emits `_libGroupTileHtml` per group (not in the Hidden
view). Tile routing goes through `_libOpenItem` / `_libOpenSeries`: **more than one section →
shelf, otherwise the episode picker exactly as before** (Hacks, Futurama never see a shelf).

Sections reuse the episode page unchanged. `openEpisodePicker(itemId, title, section, filePath)`
and `openSeriesPage(key, title, section, filePath)` call `_applySection`, which narrows `epFiles`
to the section (`_fileSection` mirrors `episodes.section_key`) and swaps `epMetadata` for the
section binding (`_sectionMetaAsShow` mounts its episodes at season `"0"`, where `_tmdbEpisode`
looks for them). A Movies section hangs each file's film binding on `f.film` and renders
`_epFilmListHtml` poster rows; `_epOpenFilm` reopens the page narrowed to that one file, which
makes it `epIsMovie`. **Guards:** `_epApplyMetadata` and `_epTopUpSeasons` must not overwrite
`epMetadata` while `epSection` is set — see GOTCHAS.

### Not-out-yet releases (15.2.0)

`_airInfo(date, verb)` → `{out, label}` and `_releaseBadge(date)` (amber chip) mark unreleased
episodes/films: `_ssEpRowHtml`, `_ssMissingRowHtml`, `_epMissingCardHtml`, collection missing
films. **All** library downloads go through `postLibraryDownload(body, {silent})`: on a 409
`unreleased` it shows `#unreleasedModal` via `_unreleasedPrompt(info)` (big Cancel; small
"Download anyway" only when `info.can_override`) and resends with `allow_unreleased`. Silent
callers (background runs) get `{unreleased:true}` and report the skip. `_errDetail` renders an
object `detail`. Stream-now rows ask via `_ssConfirmOut(season, episode, magnet)`.

**Contents check (15.3.0).** When `info.magnet` is set (`postLibraryDownload` passes
`body.magnet`; `_ssConfirmOut` passes the chosen source), "Download anyway" keeps the dialog
open and calls `POST /api/torrent/inspect`, showing a checking note in `#unrelInspect`. Has a
video → resolves true. No video → red note with the file list, the override button hidden,
Cancel relabelled Close (resolves false). Unknown → amber note with the reason, and the button
becomes "Download without checking" (the next press resolves true).

### Stream to Device

All Play surfaces (`epPlay`, `epPlayFrom`, the `lib-restart-btn` listener, and —
as of 11.6.0 — the library card's primary **Play/Resume** for *both* multi-episode
shows and single-file movies via `resumeLibraryItemWithChooser`) route through
`playLibraryWithChooser(itemId, files, seekTo, label, shuffle?)`. Movies used to
call `continueLibraryItem` (straight to VLC) with a separate green "On Device"
button; that button is gone and the primary Play now opens the chooser like shows
do. When the host is
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
`#lpTrackRow` controls: **Res** (quality), **Aud**, **Sync**, **Sub** — the three
selectors each hidden when they have ≤1 option. **Sync** is the manual audio-delay
slider (`#lpSyncRow`, `lpSetAudioOffset`); it is deliberately **not** gated on track
count (a single-audio file can be desynced too), only on the browser having
a working mechanism for the current file (`_lpOffsetMode()`: a WebAudio DelayNode on Chromium/Firefox, an audio-SourceBuffer `timestampOffset` bias on macOS Safari, neither on iOS or when WebKit is playing on-demand — see [GOTCHAS.md](GOTCHAS.md)) — and its own state is rendered by `_lpRenderSyncRow` so a live drag doesn't
rebuild every dropdown on the panel. Quality is hls.js-only: the Res dropdown is built
from `lp.hls.levels` (sorted high→low) as `Auto` + each resolution, and
`lpSetQuality(val)` sets `lp.hls.currentLevel` (`-1` = Auto/ABR; session-only,
not persisted). Safari's Res row stays hidden (no manual-level API). **iOS app,
device-copy playback** (`lp.source === "device"`): the Res dropdown is built from
the downloaded bundle's `meta.json` ladder instead — `"— On device"` (value
`dev`) + the other rungs as `"— Server"` (`srv:<height>` / `srv:auto`); those
values make `lpSetQuality` switch the *source* (a per-file `lp._srvOverride` +
full `_lpLoadIndex` reload at position). While overridden, the server menu adds
the `dev` switch-back option. See [STREAMING.md](STREAMING.md). `lpSetAudio`
/ `lpSetSubtitle` persist their picks via `/api/library/{id}/local-tracks`, as does
`lpSetAudioOffset` (`audio_offset_ms`, per-file only — see
[STREAMING.md § Manual audio offset](STREAMING.md)).

Subtitle `<track>` elements are wired from the bundle's `subtitles[]` (each a
`sub_<i>.vtt` in the cache dir) plus on-disk `subs[].url` sidecars (each points
at `/api/library/{id}/subtitle` — server converts SRT→VTT on the fly). Skip-data
is fetched in parallel from `/api/library/{id}/skip-data?file_path=…` and
assigned to `lp.skipData` for the skip-intro / skip-credits logic in
`lpEvaluateSkipOffer`.

On the TV kiosk that offer is also **mirrored to every phone**: `_tvSkipOffer()` packages it (with the tile's literal text, so an auto-skip countdown reads the same on both) into the `/api/tv-local/state` heartbeat → `state.skip_offer`, and `POST` / `DELETE /api/skip-now` relay `skip_accept` / `skip_dismiss` back. `renderSkipOffer` prefers `offer.label` and no-ops in `TV_MODE` so the kiosk doesn't stack the phone banner over its own tile. See [STREAMING.md § Smart Skip on this surface](STREAMING.md).

There is **one** `<video id="lpVideo">` inside `#localPlayer`, wrapped in
`#lpStage`. In full mode the stage is `absolute inset:0` — **the video takes
the entire screen** and every piece of chrome (header `#lpHeader`, transport
`#lpControls`, options panel `#lpTrackRow`) is an absolute overlay on top, so
nothing can shrink the video or overflow a short mobile viewport. The
container has two visual modes toggled via class:
- default (no `.lp-tiny`) — fullscreen overlay with **custom Metro controls**
  (see below); the native `controls` attribute is deliberately absent.
- `.lp-tiny` — corner tile (96×56 stage, `position:static` back in the flex
  row + huge fullscreen button + close), repositioned via CSS only (no DOM
  move, no video re-load). All overlays hide; a tap on the tile maximizes
  (`lpStageTap`).

Single-element design avoids iOS Safari's per-page video budget and the audio
desync that two synchronized videos would create. iOS-friendly: `playsinline`,
`<track>` for VTT subs, no MediaSource.

**Custom controls (`#lpControls`).** The player never uses the browser's native
`<video>` controls — they differ per OS/browser (seek bar style, sub menus, iOS
fullscreen hijack), so a custom overlay guarantees the identical UI everywhere.
Don't re-add the `controls` attribute. Pieces:
- **Transport** — center cluster: ±10 s tiles (`lpSeekBy`) around a play/pause
  tile (`lpTogglePlay`, icons synced by `_lpCtlSync`).
- **Seek bar** (`#lpSeekBar`) — bottom strip; played fill + buffered fill +
  square handle, updated by `_lpCtlTick` via `_lpClockTick`
  (`timeupdate` **plus** the 500 ms `_lpClockPump` interval — iOS MMS gaps
  `timeupdate` for seconds at a time while the clock advances, which used to
  freeze the bar and the skip offers; see [GOTCHAS.md](GOTCHAS.md)) and on
  `progress`/`durationchange`/`seeked`. Pointer
  scrub (`_lpSeekBarInit`): dragging previews via `_lpScrub.t` without touching
  `currentTime`; the seek commits **once on release** (matters in on-demand
  mode, where each cold seek restarts the JIT ffmpeg). Time labels use
  `fmtTimeSecs`. All user-intent seeks (±10 s, scrub commit, skip-intro) go
  through `_lpCommitSeek`, which verifies via `requestVideoFrameCallback` that
  the pipeline actually jumped and rebuilds it at the target if the element
  accepted the seek but kept presenting the old position (10.10.3; see
  [GOTCHAS.md](GOTCHAS.md) § ManagedMediaSource) — never set `currentTime`
  directly for a user seek.
- **Options panel** — the **gear button** (`#lpOptsBtn`, `lpToggleOpts`)
  toggles `.lp-opts` on `#localPlayer`, showing `#lpTrackRow` (quality /
  audio / subtitle selectors, the **Sync** audio-delay slider, AI button, Clip
  row) as an absolute panel
  anchored above the bottom strip — scrollable, ≤380px wide. The panel is
  **never** visible otherwise (`_lpRenderTrackRows` doesn't unhide it; the
  gear owns visibility). While open, the overlay won't auto-hide
  (`_lpCtlShow`/`_lpCtlIdle` guard on `_lpOptsOpen`); a tap on the video
  closes the panel first, and `lpStop`/`lpMinimize` clear `.lp-opts`.
- **Mute** (`lpToggleMute`) and **fullscreen** (`lpToggleFullscreen`) buttons.
  Fullscreen requests OS fullscreen on **the whole `#localPlayer` container**,
  never the bare `<video>` — so the header, transport, and track selectors stay
  usable inside fullscreen. iPhone Safari has no element-fullscreen API; the
  button is hidden there at init (the player is already a full-viewport
  overlay). See [GOTCHAS.md](GOTCHAS.md).
- **Visibility** — `.lp-idle` on `#localPlayer` fades **all** chrome (overlay +
  header + track rows) opacity-only, no reflow. Tap the video to toggle
  (`lpStageTap`, guarded so taps on buttons/selects/the overlay never
  double-act); auto-hides after 3 s of playback (`_lpCtlShow`/`_lpCtlIdle`) but
  never while paused, scrubbing, buffering, or reconnecting. Desktop: mouse
  move reveals; keyboard Space/K, ←/→ (±10 s), F, M in the global `keydown`
  handler.
- **Buffering** — `.lp-buffering` (set on `waiting`, cleared on `playing`)
  shows the square Metro spinner `#lpBuffSpin`, independent of overlay
  visibility.

The header (`#lpHeader`, `.lp-chrome`, hidden in tiny mode; on phones ≤480px
the Min / To TV text labels collapse to icons via `.lp-btn-label` so the bar
never overflows) carries **Prev** / **Next**
episode buttons (`#lpPrevEpBtn` / `#lpNextEpBtn`), both **hold-to-activate**
(`_holdStart`). `lpPrevEp` / `lpNextEp` → `lpNavEp(±1)` saves the current
position then `_lpLoadIndex(0)`s the neighbour; the hold works whether or not
that episode is prepped. `_lpRenderNavButtons` (called from `_lpLoadIndex`)
shows/hides each button for the current `lp.pi` and paints a square dot from
`prepFileState` — green = ready, amber = prepping, gray = not prepped.
`_lpWarmNextEp` (also from `_lpLoadIndex`) fires an interactive `/offline-prepare`
for the next episode so auto-advance / a Next hold resumes instantly — unless
(iOS app) that episode is already downloaded to the device, in which case it just
marks the row "ready" with no host prep. See
[STREAMING.md § Auto-advance](STREAMING.md).

`saveProgress(itemId, filePath, posSec, durSec)` is called every 15 s by
`_lpClockTick` (`#lpVideo`'s `timeupdate` handler + the `_lpClockPump`
interval) and POSTs `/api/library/{id}/progress`.
**Writes with `posSec < 5` or `durSec ≤ 0` are dropped** — the server
recomputes `completed` from `pct = position/duration`, so a t≈0 write would
wipe a watched episode back to unwatched. The same guard lives in
`_lpFlushProgress` (which also fires on `pause`, `seeked`,
`visibilitychange`→hidden, and `pagehide`, using `sendBeacon` for the last
one). `_lpLoadIndex` seeds `lp.lastSaveAt = Date.now()` so the first throttle
window starts at load, giving the resume seek time to land before any save
can fire. There is no offline outbox — a single best-effort POST is the
entire write path.

**iOS background playback (`_np*`, app only).** A block next to the other native
glue pre-arms the `NativePlayback` plugin so playback survives a lock:
`_appPlaybackPrefs()` reads the two Settings toggles (`streamlink_app_bgplay`,
`streamlink_app_tvmode`; default on); `_npOk()` gates everything on the app + plugin
+ a `lp._nativeMaster` for the current file; `_npPayload()` builds the state (using
the project's trusted-playhead rule — `currentTime > 2 ? currentTime : lp.lastKnownT`);
`_npArm()` pushes it on load, play, pause, seeked, and every `_lpSaveLocalTracks`
track pick; `_npTick()` refreshes position at 1 Hz from `_lpClockTick`; `_npDisarm()`
runs in `lpUnloadCurrent`/`lpStop`. `_npHandBack()` reclaims the playhead on
foreground — **the `visibilitychange`→visible branch now chains
`_lpRecoverActiveSub` / `_lpRecoverMediaPipeline` after it** instead of racing them on
independent timers, because those probes read the playhead and the frame-counting one
would misfire against a mid-seek element. `_npSetTvMode()` / `_npOnDisplayChange()`
drive TV Mode (the `#lpTvVeil` touch shield + the self-retiring `#lpTvHint` chip + the
`#lpTvBtn` control, shown only while a display is connected). **`#lpTvVeil` is
transparent and must stay that way** — the monitor is mirroring this framebuffer, so
an opaque veil blanks the TV too (the 14.1.1 bug); the phone is darkened by the native
backlight lever alone. All of it is inert in a browser. See
[STREAMING.md § 2b](STREAMING.md) and [GOTCHAS.md](GOTCHAS.md).

Per-file Prep state for the picker rows lives in `prepFileState:
Map<offKey, "prepping"|"ready">`. `prepForStreaming(itemId, filePath, fileName)`
first awaits `confirmStreamPrepWarning()` (the once-per-session lag warning),
then POSTs `/offline-prepare` **with `bulk:true`** (so the job honors the global
pause gate), polls `/offline-job/{id}` to completion, and flips the row to
"Stream Ready". `_startPrepPolling(itemId)` polls every 3 s and, since 12.7.3, **knows how to give up**: 404/410 (item deleted) and 401/403 (no longer visible to this profile) are terminal and call `_stopPrepPolling`, while transient failures get a budget of `PREP_POLL_MAX_TRANSIENT` (20) rather than retrying forever — deleting an item used to leave every client that had it on screen polling a dead id every 3 s indefinitely (see [GOTCHAS.md](GOTCHAS.md) § a poll loop needs to know what "never" looks like). The map is also hydrated from `/api/library/{id}/prep-status`
whenever the picker opens or `/prep-all` runs on the library card.

`#globalPrepBar` (the persistent top-right indicator) is now interactive:
`_renderGlobalPrep(d)` toggles a **Pause** button (`openPrepPauseChooser` →
`confirmPausePrep(kill)` → `/api/offline-prep/pause`) and a **Resume** button
(`resumePrep` → `/api/offline-prep/resume`) from `d.paused`. Pausing a job that's
mid-encode shows "Pausing — finishing current file" until it completes (or, if
the user chose "Stop now", it's cancelled at once). The per-card chip
(`prepChipHtml`) shows "Prep paused" when `processing === 0 && paused > 0`.
It's also **collapsible**: `.prep-collapsed` (the default) shrinks the pill to just
its pulsing dot + a chevron tab; `togglePrepBar()` (the `#globalPrepToggle` chevron)
expands the full pill (bar, detail, Pause/Resume) and flips the chevron up. The
`prepBarExpanded` flag survives `_renderGlobalPrep`'s re-renders (it only mutates
`hidden`/`textContent`, never the `className`).

### Background source-finding (12.7.0)

Hunting sources for a season is a sequence of indexer round trips — one per season,
then one per episode the season queries left bare — and on a long-running show that is
minutes of work with nothing for the user to decide while it happens. It used to run
inside the bulk sheet behind a progress bar, pinning the user to a modal for the
duration. It now runs detached.

- **`#globalFindBar`** — indigo pill, bottom-left, `z-[58]` (above the show page's `z-50`
  and its sheets' `z-55`, below the download modal's `z-60`), with a label, progress bar,
  detail line and a × that calls `bgFindStop()`. Bottom-left keeps it clear of
  `#globalPrepBar` (top-right) and the footer nav.
- **`_bgRun(label, fn, {quiet})`** — starts a run, or returns `false` if one is already in
  flight (the indexers are shared and rate-limited; two concurrent sweeps halve both). An
  explicit run **preempts** an automatic one: `ctl.auto` is set by `quiet`, and a
  non-quiet caller cancels the running sweep and chains itself onto `ctl.done`.
  Cancellation is cooperative — every loop checks `ctl.cancel` between queries.
- **`_bgCtx()`** — the crux. `closeSearchShow()` clears `_ssGroup`, `_ssEpisodes`,
  `_ssMeta` and `_ssOwned`, so a run that reads live page state dies the moment the user
  walks away. A run therefore snapshots title/kind/meta/owned/locked/audio-pref/scope/
  seasons/episodes up front and works only against that; `_bgIngest` mirrors results into
  the live page **only while `_bgLive(ctx)`** (same show still open), which is also what
  makes the seasons fill in live for a user who stays put. `_bgScopeEpisodes` /
  `_bgAudioMatch` / `_bgAutoPick` are the snapshot-taking twins of `_ssBulkScopeEpisodes` /
  `_ssAudioMatch` / `_ssAutoPick`, and `_ssDownloadOne` takes `{series, locked}` overrides
  so a detached run can queue for a show the page has since forgotten.
- **Three entry points:** `_ssSweepBareSeasons()` (after the broad "Search episodes" pass,
  quiet), the pack sweep inside `ssSearchPacks` (quiet), and `_bgEnsureSources` (the bulk
  run, explicit). `_ssSeasonHinted` is the shared "already asked for this season" set that
  keeps the tab-level hint (`_ssEpisodeSeasonHint`) and the sweeps from duplicating work.
- **`_bgStartDownloads`** only calls `closeSearchShow()`/`switchTab("library")` when
  `_bgLive(ctx)` — a background run must never yank the screen out from under the user.

#### Dev Mode HUD

**Settings → This Device → Dev Mode** (`#psDevMode` → `toggleDevMode`; persisted
device-locally in `localStorage.streamlink_devmode` as the `devMode` flag — the
stats are properties of *this* browser's session, so the toggle never touches the
server). While on, the local player shows `#lpDevHud` — a pointer-through
monospace overlay top-left under the header, repainted at 1 Hz by
`_lpDevHudTick` (interval started by `_lpDevHudStart` in `lpPlay`, cleared by
`_lpDevHudStop` in `lpStop`; toggling mid-playback applies live). Rows: engine +
mode (hls.js/native · bundle/ondemand, `RECONNECTING` while `lp.netDown`),
**stream source** (`on-device bundle · 127.0.0.1:<port> · 480p` when the iOS app
plays a downloaded copy via the loopback `LocalMediaServer`, else
`server · <host> · bundle|ondemand`),
active quality rung (`lp.hls.levels[currentLevel]` height + bitrate, auto vs.
pinned), the hls.js `bandwidthEstimate`, the decoded `videoWidth×videoHeight`,
the last segment's transfer stats (`_lpDevNoteFrag`, recorded from `FRAG_LOADED`
only while `devMode` is on), forward buffer (seconds past the playhead +
buffered-range count), dropped/total frames (`getVideoPlaybackQuality`), and a
stall counter (`lp._devStalls`, bumped by the `waiting` listener) + `readyState`.
Deliberately **not** `.lp-chrome` — it stays visible through the controls'
auto-hide; hidden in tiny mode by CSS. Safari native HLS has no level/bandwidth
API, so those rows degrade to a "auto (native HLS)" note. Everything reads from
objects already in hand (the `<video>`, `lp.hls`) — zero extra network traffic,
and no interval or stat collection while off.

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

- **Fullscreen (TV):** `#fcClipRow` (relocated into the **More** sheet `#fcMorePanel` in 7.10.0; was Row D2 in the main grid). `fcClip(seconds, btn)` reads the
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

## Profile session token

`POST /api/profiles/{id}/verify-pin` returns a **token** alongside the profile. It is the server's only proof that the PIN was actually entered — a bare `profile_id` proves nothing, since `GET /api/profiles` hands out every UUID unauthenticated. Two things need it: seeing admin-locked ("content lock") items, and the delete endpoints.

- Held in `_profileToken`, mirrored to **both** `localStorage.streamlink_profile_token` and a host cookie of the same name, managed only through **`setProfileToken()`**. The cookie is what makes it work across schemes: `localStorage` is per-origin, so `http://<host>` and `https://<host>` do not share it — without the cookie, a PIN entered on HTTPS left HTTP logged in but unelevated, with the locked content silently missing.
- Attached as **`X-Profile-Token`** by a single `window.fetch` wrapper installed next to the `profile` declaration, scoped to same-origin `/api/` URLs so it can never leak to a third party. Individual call sites don't (and shouldn't) know about it — don't start adding the header by hand.
- **Cleared** when a PIN-less profile is selected (`_doSelectProfile`), otherwise the last PIN entered would keep unlocking content for whoever picks a different profile afterwards.
- **Revalidated** on every `fetchProfiles()`: the response carries `verified_profile_id`, and a token the server no longer recognises (expired 12 h TTL, deleted profile, wiped session file) is dropped so the next pick re-prompts. Without that, an expired session degrades into "some shows are missing and Delete fails with a 403" and nothing tells the user to re-enter their PIN.

- **Re-prompted on boot** by `_maybePromptForPin()` when the restored profile `has_pin` but the server reports no verified session. The boot path restores a profile from `localStorage` *without* re-verifying, so a login carried across the 11.20.0 upgrade — or one whose 12 h token has expired — is signed in with no PIN proof behind it. Nothing used to ask; the locked shows were just gone. Dismissing the prompt leaves the user logged in, minus that content.

Delete failures surface the server's `detail` rather than a generic message — a 403 here means "no PIN-verified profile", which is actionable in a way "could not delete" isn't.

### One-press Get, from the library (14.1.0)

The missing-episode rows and the "none of this season is here" banner used to offer **Find
sources**, which opened the search show screen scoped to the gap — leaving the user to run
a search and pick a release. They now offer **Get**, which finds *and* downloads in one
press, without the search screen opening.

- `_epOwnedRow()` builds a `{have, pending}` coverage row from `epFiles` (not the
  `/coverage` snapshot — the episode page already holds the authoritative per-file truth,
  and files still downloading must count as owned so a second press can't re-queue them).
- `_epBgCtx(scope, only)` builds the detached-run context from the **library** item in the
  same shape `_bgCtx` builds from the search screen, so `_bgEnsureSources` /
  `_bgAutoPick` / `_bgStartDownloads` are reused unchanged. `searched: true` skips the
  broad show-title query — no search page is open to inherit one, and the per-season
  targeted query the sweep runs is the better one anyway (see `ssSearchSeason`).
- `only` narrows a run to a single episode by marking that season's **other** episodes as
  owned, rather than filtering afterwards: the gap set is what the sweep searches, so this
  is the difference between one indexer query and one per episode in the season.
- `epGetSeason` / `epGetEpisode` are the entry points; `epFindSeason` / `epFindEpisode`
  (the old handoff) survive behind a **Choose** button rendered only for Full profiles.

### Live download readouts on library cards (14.1.0)

Two gaps closed. `POST /api/library/download` now broadcasts `library_update` on creation
— the grid is only repainted by that event, so a download started from the library page
was previously invisible until `library_download_monitor`'s next tick. And a **merged
show** tile never reported progress at all: a show fetched episode by episode is many
items behind one card, while the `library_progress` handler only knew `dl-stat-<id>` on a
single-item card.

Both card shapes now tag their stat line and progress bar with `data-dl-items` (a
space-separated id list). `_libDlAgg(ids)` rolls several items' stats into one — summed
bytes and speed, the slowest ETA, "finding peers" only while *nothing* has a size yet —
and `_libRefreshDlStats(itemId)` repaints every readout covering the item that moved. One
pass serves both shapes.

**Use `innerHTML`, not `textContent`, for `formatDlStat` output.** It embeds an icon for
the "Finding peers…" and "waiting for idle window" states; the old handler assigned it as
text and printed a literal `<svg …>` into the card on every update after the first paint.
Every value in that string is ours (byte counts and fixed labels), never user input.

## Simple mode (per-profile, 14.0.0)

StreamLink's controls were built for someone who knows what a torrent is, and that
assumption leaked into every screen. Simple mode is the interface for everyone else, and
it is the **default**: `_profile_simple_ui` (main.py) returns `not elevated` unless the
profile carries an explicit `simple_ui`. It is presentation only — never an authorisation
check; the endpoints behind every hidden control keep their own auth.

- **Plumbing.** `let SIMPLE` + `applySimpleUi()` (declared beside `TV_MODE`) read
  `profile.simple_ui` and toggle a `simple-ui` body class. Called from `_doSelectProfile`
  (so the PIN path and the picker are both covered) and from the boot restore. Nothing is
  removed from the DOM, so flipping a profile needs no reload.
- **Hiding.** Same pattern as `tv-mode`: a CSS block hides `.adv-only` plus the named
  advanced regions — `#srcWrap`, `#catWrap`, `#searchModeWrap`, `#diskSpaceContainer`, the
  storage gear, `#epBulkChips`, `#epDownloadSelBtn`, and the show page's
  `#ssSectionToggle` / `#ssEpisodesTools` / `#ssPacksTools`. Render-time guards cover what
  CSS can't: the episode page's scheduling bar (`if(epHasTorrent && !TV_MODE && !SIMPLE
  …)`) and the per-episode selection checkbox (everything it feeds is hidden, so it would
  tick and do nothing).
- **Classic search is forced off.** `applySimpleUi()` calls `setSearchMode("smart")` when a
  Simple profile has a stored `classic` — its toggle is hidden, so it would otherwise be a
  one-way trap into the raw indexer list.
- **The show page is replaced, not trimmed.** `_ssRenderBody()` routes to
  `_ssRenderSimple()`, which renders the seasons and what you own of each, with **Get this
  season** / **Get the rest** beside the ones you don't (or **Add to my library** for a
  movie). No tabs, no search buttons, no release names, no seeder columns.
- **`ssSimpleGetSeason(season, btn)`** does the work in the order a person would want it.
  Owning none of the season it runs the targeted `ssSearchSeason(sn)` query and takes
  `_ssBestPackForScope(sn)` — one whole-season copy, consistent audio and quality, which is
  the advice the advanced UI already gave but made you find another tab to act on. Owning
  part of it (or with no whole-season copy available) it falls through to
  `ssBulkAuto(sn)`, the existing detached auto-picker, which fetches only the gaps.
  `ssBulkAuto` and `_ssBestPackForScope` both gained an optional explicit scope for this;
  everything else is reused unchanged.
- **`epStartOver(btn)`** is Simple mode's only bulk control, rendered where the scheduling
  bar would be. Resetting a show for a rewatch is "Select all" + "Unwatched" in the
  advanced UI — both chips Simple hides — so without it someone would be un-ticking thirty
  episodes by hand. Scoped to the visible season (or the whole show when it has none) so
  resetting Season 2 doesn't wipe Season 1.
- **Switching a profile** is a `Simple` / `Full` button per row in the profile management
  sheet (`renderExistingProfilesList`) → `POST /api/profiles/{id}/simple-ui`. Gated
  server-side like the deletes, and it refreshes the signed-in profile's `localStorage`
  snapshot so the change lands without a reload.

### Vocabulary (applies to BOTH modes)

There is one vocabulary, not two. Raw seeder counts are gone from every result, pack and
source row: `_availLabel(seeders)` / `_availHtml(seeders)` render **Excellent / Good / Low
/ Unavailable** on the thresholds the colour coding always used, with the exact count in
the tooltip. A bare number can't tell a reader whether something will play smoothly unless
they already know what a good number looks like. Likewise "Season Packs" → **Whole
Seasons**, "Search packs" → **Find whole seasons**, "Pick a torrent to download" → **Pick a
copy to download**, and the seeder/indexer help tips were rewritten in plain words.

## TV mode (`/?tv=1`)

The same `index.html`, loaded by the backend's TV UI kiosk (a fullscreen Chrome on the host display, driven by the air-mouse remote — see [REMOTE.md](REMOTE.md)). Detected at boot via `const TV_MODE = new URLSearchParams(location.search).get("tv") === "1"` (declared next to `hlsAvailable`):

- Sets `document.title = "StreamLink TV Dashboard"` — this is the **window-title marker** the backend's Windows focus code matches (`main.py _TVUI_WINDOW_MARKER`); keep the two strings in sync and never retitle the page in TV mode.
- Adds `tv-mode` + `no-hls` body classes and forces `hlsAvailable = false` (also in the `/api/state` fetch callback, which would otherwise overwrite it). On the TV, VLC *is* "on device", so all Prep / On-Device / play-chooser affordances hide through the existing `no-hls` machinery and the play chooser collapses straight to VLC (the macOS no-HLS path).
- `.tv-mode` CSS additionally hides the handoff buttons (`#handoffBtn`, `#fcHandoffBtn`), and the library-card download-to-device buttons are skipped in the renderer (`if(isReady && !TV_MODE)`).
- **D-pad spatial navigation** (`_tvNavKey` + `_tvCandidates`/`_tvNavScope`, registered only in TV mode): arrows move focus to the nearest actionable element in the pressed direction (cone + distance scoring), Enter/OK activates (synthesized `click()` for `[onclick]` tiles, native for buttons/links/inputs). Scoped to the topmost open `[id$="Modal"]:not(.hidden)`; arrows that edit a control are ignored; unmatched arrows fall through to native scroll. The `.tv-mode :focus` rule draws the indigo ring (plain `:focus`, not `:focus-visible`, so programmatic focus always shows it).
- **TV layout** (`.tv-mode` CSS block): hides the library toolbar (storage gear, Upload, `#libHiddenToggle`, Refresh — matched by `onclick` value), the per-card `⋯` drawers (`.lib-cardv-more`), `#fullscreenBtn` + `#fullscreenControls` (`openFullscreenControls()` also early-returns in TV mode), and the whole `footer` player bar — the remote is the transport on the TV. `zoom: 1.15` upscales for 10-foot readability (kiosk is Chrome-only) and `main`'s `pb-36` footer clearance is overridden back to `2rem`.
- **TV card overlay** (`_libItemCardHtml` / `_libShowCardHtml`, rendered only when `TV_MODE`): a `.tv-card-ov` (`data-tv-group`) covering the poster with full-card `.tv-card-btn` actions — Play/Resume (`data-tv-default`) on top, Episodes below for multi-file items/shows — invisible (opacity) until `:hover`/`:focus-within`, replacing the centered `.lib-cardv-play` (CSS-hidden on TV). Buttons reuse the `lib-tile-open`/`lib-show-open`/`lib-show-play` delegation; inline handlers stop **Enter/Space** keydown + click propagation (poster has its own open handlers) but must let arrows bubble to `_tvNavKey`. Nav integration: `_tvCandidates` skips any candidate containing a `.tv-card-btn` (the poster defers to its buttons), and `_tvNavKey` redirects cross-group focus entry to the group's `data-tv-default`.
- Everything else is the stock dashboard: the kiosk keeps its own Chrome profile (`.tvui_chrome_profile`), so the profile pick and device-local prefs persist across wakes.

## `static/admin.html` (990 lines)

Password-protected at `/admin`. Token stored in `sessionStorage.admin_token` and sent via `Authorization: Bearer …`. The dashboard auto-redirects HTTP → HTTPS for `/admin*` ([main.py:1772](../main.py#L1772)).

**Help tips (7.16.0).** Metro-flat `?` chips (`.help-tip`) plus a single shared popover (`#tipPop`) render any element's `data-tip` text on hover / keyboard focus / tap-to-pin (touch-friendly, unlike `title`). Settings loaders rewrite the key tips from the currently-saved config via `setTip(id, text)` so they describe the admin's actual setup. See [ADMIN.md § Help tips](ADMIN.md) for the full list of contextual hooks; prefer a contextual `setTip` in a loader over a static multi-mode description when adding settings.

### Tabs

1. **Indexers** ([line 95](../static/admin.html#L95)) — `INDEXER_CATEGORIES` override; list of configured Jackett indexers with delete; "Add indexer" modal that lists available indexers from Jackett and renders the config form for each.
2. **Content Lock** ([line 142](../static/admin.html#L142)) — toggle `admin_only` per library item. Profiles can be marked `elevated` to also see admin-only items.
3. **Smart Skip** ([line 155](../static/admin.html#L155)) — list items with their skip-data status; per-item `Analyze` button (force re-run); `Edit` opens inline editor with three numeric fields per file (intro start, intro end, credits start). Manual edits set `analysis.source="manual"` so they survive re-analysis.
4. **Profile PINs** ([line 182](../static/admin.html#L182)) — set/clear PIN per profile (admin overrides current-PIN check); toggle the `elevated` flag; **Search Sources** button (`openSrcAllow`) opens `#srcAllowModal` — a checklist of every configured indexer (`/api/admin/indexers/catalog`, content-type chips) whose save (`submitSrcAllow` → `POST /api/profiles/{id}/set-indexers`) writes `profile.allowed_indexers` (all/none ticked ⇒ unrestricted).
5. **System** (`#panelSystem`) — **Shut Down** (`doShutdownServer`), **Reboot Machine** (`doRebootMachine`, confirm-gated → `POST /api/admin/reboot`), and **Scheduled Restart** (`loadScheduledReboot` / `toggleScheduledReboot` / `saveScheduledReboot` → `GET`/`POST /api/admin/scheduled-reboot`). The scheduled-restart panel has an enable toggle, time input, timezone select, idle-window field, and a live host-time readout; loaded on tab switch. (Offline Cache and Background Video tabs also exist — see [ADMIN.md](ADMIN.md).)

### Admin SSE ([static/admin.html:483](../static/admin.html#L483))

`ensureAdminSSE()` connects to `/api/events?admin_token=...` to receive `analysis_status` events while a Smart Skip run is in progress — the Smart Skip tab live-updates progress bars.

## See also

- [BACKEND.md](BACKEND.md) — what each endpoint actually does
- [API.md](API.md) — endpoint signatures
- [ADMIN.md](ADMIN.md) — admin auth, indexer flow, content lock semantics
