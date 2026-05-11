# Implementation Plan

> Status markers: `[ ]` = not started · `[/]` = in progress · `[x]` = complete · `[-]` = deferred/skipped

---

## Milestone 1 — Quick UI Fixes *(frontend-only, low risk)*

- [x] **1.1** Fix Fullscreen UI: correct hitboxes on buttons and fix seek/volume slider accuracy
- [x] **1.2** Add Volume ±5% increment buttons flanking the volume slider
- [x] **1.3** Fix Volume lag: fire `POST /api/vlc/volume/set` only on `mouseup`/`touchend`, not every `input` event
- [x] **1.4** Library Logic Fix: show series grouping even when `series_name` metadata is empty/null

---

## Milestone 2 — Player Enhancements *(frontend + minor backend)*

- [x] **2.1** Add Next / Previous episode buttons in the player footer
- [x] **2.2** Highlight "currently playing" episode in the Episode Picker modal (poll VLC filename via state)
- [x] **2.3** Playback Fix: "Play" on a specific episode selection reliably starts that file
  - ✅ Confirmed working for single-file items with watch progress.

---

## Milestone 3 — Library Enhancements *(backend + frontend)*

- [x] **3.1** Disk Space Utility: show free/total space for the download path in the UI
- [x] **3.2** "Add to Library": button while streaming to save the active torrent file to persistent library
- [x] **3.3** Upload System: web UI for uploading local files/folders directly to the library
- [x] **3.4** Precision Selection: folder/subfolder/file picker for library downloads (not just full torrent)
- [x] **3.5** Web Downloads: browser "Download" button to pull a library file back to the client

---

## Milestone 4 — Core Functional Fixes *(backend-heavy)*

- [x] **4.1** Cleanup: auto-delete torrent + temp files when a new stream replaces the current one
- [x] **4.2** Priority Downloads: expose qBit priority controls; "Play when ready" for queued items
- [x] **4.3** Multi-Disk Support: configure multiple `LIBRARY_PATH_*` entries; show per-disk free space

---

## Milestone 5 — Advanced / Power Features

- [ ] **5.1** Local DNS: configure mDNS so the tool is accessible at `http://tool.local`
- [-] **5.2** Smart Skip: audio fingerprinting to detect and skip intro/credit sequences on library files *(backlogged — more complex than expected)*
- [ ] **5.3** Control API: documented JSON POST endpoints for external play/pause/seek/volume control

---

## Milestone 8 — Mobile UX & Playback Fixes *(new batch)*

- [x] **8.1** Fullscreen UI: buttons fill the entire screen with no gaps; reserve space at top/bottom for device safe-area cutouts (env(safe-area-inset-*))
- [x] **8.2** Bug — Partial download playback: if the first file in a torrent hasn't downloaded yet, "Play All" and individual file play both fail silently; handle this gracefully
- [x] **8.3** Bug — Watch history not tracked when episode is launched from the episode list; history should be recorded regardless of how playback was initiated (stream-only excluded)
- [x] **8.4** Auto-open fullscreen player on mobile/small screens when something is already playing
- [x] **8.5** Next Episode continuity: hitting Next Episode should always advance to the next episode in series order, regardless of how the current episode was started
- [x] **8.6** Current track title display: show Season × Episode × and episode name (when name is unique to that episode) as the most prominent label for the playing track

---

## Milestone 6 — Admin & Security

- [ ] **6.1** Admin Dashboard: password-protected `/admin` panel (no HTTPS needed for LAN, flag it)
- [ ] **6.2** Content Lock: "admin-only" flag on library items, hidden from standard profiles
- [ ] **6.3** Profile PINs: optional 4-digit PIN per profile, prompted before access
- [ ] **6.4** Indexer Management: admin UI to view/add/remove Jackett indexers without editing `.env`

---

## Milestone 7 — System & Daemon

- [ ] **7.1** Daemonization: `run.py --install` registers a launchd/systemd service for startup launch
- [ ] **7.2** Watchdog: background process monitors VLC, qBit, Jackett; auto-restarts crashed services

---

## Suggested First Chunk → Milestone 1 (all four tasks)

All four M1 tasks are **frontend-only** changes to `static/index.html`. No server restart is needed — a browser refresh is sufficient after each sub-task.

**Why start here:**
- Highest impact-to-risk ratio (no backend changes, no data migration)
- Volume lag fix (1.3) and fullscreen hitbox fix (1.1) are actively annoying UX bugs
- Library series fix (1.4) prevents content from disappearing for users without metadata
- All four can be verified instantly in the browser with the server already running
