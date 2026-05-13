# Implementation Plan

> Status markers: `[ ]` = not started · `[/]` = in progress · `[x]` = complete · `[-]` = deferred/skipped

---

## Milestone 1 — Quick UI Fixes

- [x] **1.1** Fix Fullscreen UI: correct hitboxes on buttons and fix seek/volume slider accuracy
- [x] **1.2** Add Volume ±5% increment buttons flanking the volume slider
- [x] **1.3** Fix Volume lag: fire `POST /api/vlc/volume/set` only on `mouseup`/`touchend`, not every `input` event
- [x] **1.4** Library Logic Fix: show series grouping even when `series_name` metadata is empty/null
- [x] **1.5** Don't open web UI on server start


---

## Milestone 2 — Player Enhancements 

- [x] **2.1** Add Next / Previous episode buttons in the player footer
- [x] **2.2** Highlight "currently playing" episode in the Episode Picker modal (poll VLC filename via state)
- [x] **2.3** Playback Fix: "Play" on a specific episode selection reliably starts that file
  - ✅ Confirmed working for single-file items with watch progress.
- [x] **2.4** Audio/Sub Track State Mem: Remember what audio track and subtitle track were selected for an episode and select those when playing it again


---

## Milestone 3 — Library Enhancements 

- [x] **3.1** Disk Space Utility: show free/total space for the download path in the UI
- [x] **3.2** "Add to Library": button while streaming to save the active torrent file to persistent library
- [x] **3.3** Upload System: web UI for uploading local files/folders directly to the library
- [x] **3.4** Precision Selection: folder/subfolder/file picker for library downloads (not just full torrent)
- [x] **3.5** Web Downloads: browser "Download" button to pull a library file back to the client
- [x] **3.6** Mark as Watched: Ability to mark an episode(or multiple from a clean selector UI), season, series, or torrent as watched. 

---

## Milestone 4 — Core Functional Fixes 

- [x] **4.1** Cleanup: auto-delete torrent + temp files when a new stream replaces the current one
- [x] **4.2** Priority Downloads: expose qBit priority controls; "Play when ready" for queued items
- [x] **4.3** Multi-Disk Support: configure multiple `LIBRARY_PATH_*` entries; show per-disk free space
- [x] **4.4** Retry Playback: VLC can run into issues if the file is not fully ready, add a button next to stop in the fullscreen controls UI to relanch VLC and retry playback
- [x] **4.5** VLC Focus: On playback, VLC is focused and set to fullscreen


---

## Milestone 5 — Advanced / Power Features

- [/] **5.1** Local DNS: configure mDNS so the tool is accessible at `http://remote.local`, update project to use port 80 (or 443 if https is enabled)
- [-] **5.2** Smart Skip: audio fingerprinting to detect and skip intro/credit sequences on library files *(backlogged — more complex than expected)*
- [-] **5.3** Control API: documented JSON POST endpoints for external play/pause/seek/volume control

---

## Milestone 9 — Metro UI Redesign 

- [x] **9.1** Full Metro/Win8 design language: flat tiles, no rounded corners, bold uppercase type, accent colors throughout
- [x] **9.2** CSS design system: `.tab-active`/`.tab-inactive` underline indicator, all square status dots, no `backdrop-blur`
- [x] **9.3** HTML structural sections: navbar, footer, all modals converted to flat bottom-sheet style with top accent stripe
- [x] **9.4** JS-generated HTML: search results, library cards, episode picker, profile grids, alerts/toasts all converted to Metro classes
- [x] **9.5** Metro UI for bottom player footer controls (seek bar row, control tiles, status row)

---

## Milestone 10 — Reliability & Visibility

- [ ] **10.1** UI warning if any dependency/service is unreachable (VLC, qBittorrent, Jackett): currently only shown at startup in the terminal; surface these as persistent in-app banners so users on mobile know why things aren't working

---

## Milestone 8 — Mobile UX & Playback Fixes 

- [x] **8.1** Fullscreen UI: buttons fill the entire screen with no gaps; reserve space at top/bottom for device safe-area cutouts (env(safe-area-inset-*))
- [x] **8.2** Bug — Partial download playback: if the first file in a torrent hasn't downloaded yet, "Play All" and individual file play both fail silently; handle this gracefully
- [x] **8.3** Bug — Watch history not tracked when episode is launched from the episode list; history should be recorded regardless of how playback was initiated (stream-only excluded)
- [x] **8.4** Auto-open fullscreen player on mobile/small screens when something is already playing
- [x] **8.5** Next Episode continuity: hitting Next Episode should always advance to the next episode in series order, regardless of how the current episode was started
- [x] **8.6** Current track title display: show Season × Episode × and episode name (when name is unique to that episode) as the most prominent label for the playing track

---

## Milestone 6 — Admin & Security

- [ ] **6.1** Admin Dashboard: password-protected `/admin` panel (HTTPS needed, flag it)
- [ ] **6.2** Content Lock: "admin-only" flag on library items, hidden from standard profiles
- [ ] **6.3** Profile PINs: optional 4-digit PIN per profile, prompted before access
- [ ] **6.4** Indexer Management: admin UI to view/add/remove Jackett indexers without editing `.env`

---

## Milestone 7 — System & Daemon

- [x] **7.1** Daemonization: `run.py --install` registers a launchd/systemd service for startup launch
- [x] **7.2** Watchdog: background process monitors VLC, qBit, Jackett; auto-restarts crashed services

---

