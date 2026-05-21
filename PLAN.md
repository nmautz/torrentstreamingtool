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
  - 2026-05-18 follow-up: the Retry button has been removed from the fullscreen controls per user request. The `/api/retry` endpoint + `_retry_task` helper remain in `main.py` but are no longer reachable from the UI; the slow-network playback work in 2.2.0 covered the original "VLC opens before the file is ready" footgun.
- [x] **4.5** VLC Focus: On playback, VLC is focused and set to fullscreen
  - 2026-05-17 follow-up: at boot via the system service, the idle background video was coming up in a small windowed view because the post-`in_play` HTTP fullscreen toggle raced the desktop. VLC is now spawned with `--fullscreen` in all three launch paths, `vlc_focus_and_fullscreen` retries the toggle until VLC reports `state=playing` (up to 6 attempts), and the macOS path now also hides every other visible app via AppleScript so VLC owns the screen.


---

## Milestone 5 — Advanced / Power Features

- [x] **5.1** Local DNS: configure mDNS so the tool is accessible at `http://remote.local`, update project to use port 80 (or 443 if https is enabled)
- [x] **5.2** Smart Skip: audio fingerprinting to detect and skip intro/credit sequences on library files
- [-] **5.3** Control API: documented JSON POST endpoints for external play/pause/seek/volume control
- [x] **5.4** Subtitle Download: Find subtitles for the track by hash or name
- [x] **5.5** Windows targetted Full Setup and Startup Automation: Setup should install ALL dependencies including optional ones. setup should also install the service and the service should be able to startup all depencies on its own. (assuming vpn handles itself starting, just dont start qbittorrent until vpn is on and connected)
  - `setup.py` now auto-installs core apps (VLC/qBittorrent/Jackett/Mullvad) via winget on Windows (brew casks on macOS), in addition to the existing ffmpeg/fpcalc install. winget "already installed / no upgrade" exit codes are treated as success.
  - Jackett detection (setup.py, run.py, watchdog.py) now scans every Program Files / LocalAppData / ProgramData location the Windows installer may use, for both `JackettConsole.exe` and `jackett.exe`.
  - `setup.py` offers to register the system service at the end (defaults to yes on Windows); on Windows, service install self-elevates via UAC when not run from an admin shell. The service's watchdog starts all deps on its own, gating qBittorrent on VPN connection.


---

## Milestone 12 — Netflix-Style Episode Page

- [x] **12.1** Replace the bottom-sheet episode-picker **modal** with a full-screen **page** (`#episodePage`) that uses the entire viewport on mobile and looks like a streaming-app season view. Hero banner (TMDb backdrop + poster + title + overview), season tabs row, scrollable episode list, sticky bulk-action footer (Close · Download · Offline · ▶ Play).
- [x] **12.2** **TMDb integration** for show + episode metadata. Adds an optional `TMDB_API_KEY` setting (env or admin-panel override) and two endpoints: `GET /api/library/{id}/metadata` (auto-fetches + caches on first call) and `POST /api/library/{id}/metadata/refresh` (admin-only manual rematch). Cached on `library.json → items[].metadata` so the UI loads instantly after the first fetch.
- [x] **12.3** Episode rows show a 16:9 still (TMDb `/w300<still_path>` when available, else a "S01·E02" placeholder tile), the headline `S01·E02 · Episode Title`, a 2-line episode overview, a green watch-progress bar, and per-row Watched / Offline / Download buttons. Tapping the still plays from that episode forward.
- [x] **12.4** **Season tabs** (`#epSeasonTabs`) — one tab per detected season, hidden when an item has zero/one season. Default tab is the season of the currently-playing file → first season with unwatched episodes → first available.
- [x] **12.5** Filename-only fallback: if TMDb is unconfigured or no match is found, the page still works using the existing `parseEpisodeInfo` filename parser. Backdrops / posters / stills simply don't render.

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
- [/] **10.2** Per-control in-flight indicators for VLC controls: when a request is slow (busy CPU / weak network), show a per-button loading state and ignore further clicks of that same control until it resolves. Other controls remain clickable independently (e.g. clicking volume-up shows it loading while pause stays usable). Applies to pause, seek ±, volume ± / slider, prev / next, stop, retry, audio / subtitle selects, seek bar click, and the skip / resume offer buttons.

---

## Milestone 11 — Handoff to Device (Offline Playback) **— superseded by Milestone 13**

The downloaded-to-device variant was retired because the IndexedDB blob save
frequently truncated long episodes (e.g. the first ~5 minutes were playable but
the tail of the file was unreachable). Items 11.1–11.3 still describe the
backend pieces that remain in use; items 11.4–11.12 are no longer current.

- [x] **11.1** Backend `/api/library/{id}/offline-prepare`: ffprobe the source file, fast-path Safari-compatible MP4s with a direct URL, otherwise spawn an ffmpeg remux (rewrap container, no re-encode) or transcode (H.264 + AAC) job. Job state polled via `/api/library/offline-job/{job_id}`; output served from `.offline_cache/<sha>.mp4`.
- [x] **11.2** Backend `/api/library/{id}/subtitle?file=...` — sidecar SRT/VTT lookup; SRTs are converted to WebVTT on the fly so the browser `<track>` element can use them.
- [x] **11.3** Backend `/api/library/{id}/skip-data?file_path=...` — non-admin read-only access to per-file intro/credits times.
- [-] **11.4** Service worker + manifest (replaced by Milestone 13.1 — a one-shot unregister/eviction SW now ships in its place).
- [-] **11.5** IndexedDB `streamlink-offline` store (deleted in Milestone 13.2).
- [-] **11.6** Per-row Save/Remove buttons (replaced by per-row Prep buttons in Milestone 13.3).
- [-] **11.7** Chooser modal "saved offline copy" gate (replaced by Milestone 13.4 — chooser is always available online).
- [-] **11.8** Local player wired against IndexedDB blobs (replaced by Milestone 13.5 — server URLs + HTTP range).
- [-] **11.9** Skip-intro / subtitles / progress for offline blobs (Milestone 13.5 wires the same UI to streamed sources).
- [-] **11.10** Outbox queue for offline progress writes (Milestone 13.6 drops the outbox — progress is always best-effort POST).
- [x] **11.11** "Prep" button on each library card (kept; renamed "Prep for Streaming" in Milestone 13.3).
- [-] **11.12** Dedicated Offline tab (deleted in Milestone 13.7 — there's nothing device-resident to list anymore).

---

## Milestone 13 — Stream to Device

Replaces Milestone 11's "download episode to device" Handoff with HTTP-range
streaming from the server's preconverted MP4. Keeps the existing
`/offline-prepare` + `.offline_cache/<sha>.mp4` pipeline (it produces a
browser-friendly file regardless), but the device's `<video>` plays the URL
directly instead of fetching the blob into IndexedDB. Watch progress continues
to POST to `/api/library/{id}/progress` every 15 s + on pause/seek/exit, so a
device that quits playback abruptly resumes from the right spot on next play.

- [x] **13.1** Service worker + PWA shell removed. `static/sw.js` now ships a one-shot eviction stub (`registration.unregister()` + `caches.keys().delete(*)` on activate); `/manifest.json` is gone, the `<link rel="manifest">` is dropped, and `index.html` registers the stub once on load so legacy devices get clean state on next visit.
- [x] **13.2** IndexedDB / outbox / blob-URL plumbing removed from `static/index.html`. No more `streamlink-offline` DB, no `offlineSaved` Set, no `_lpBgPrefetch` map, no `osm*` modal helpers. `lp.videoUrl`/`lp.subUrls` blob-revoke logic dropped.
- [x] **13.3** Per-row "Save Offline" toggle replaced with a per-file **Prep** button driven by the existing `/offline-prepare` + `/offline-job/{id}` endpoints. Tri-state UI: **Prep** (default), **Prepping…** (spinner while a job is in flight), **Stream Ready** (green check) once the cache file exists. State sourced from `/prep-status` so the picker reflects truth across reloads. Library-card bulk button renamed "Prep for Streaming"; `prepItemForStreaming` (was `prepItemForOffline`).
- [x] **13.4** Chooser modal stays (VLC vs On Device); "On Device" is now always available when online. The "Saved offline copy" caption became "Stream to this device's browser".
- [x] **13.5** Local player rewritten: `_lpLoadIndex` POSTs `/offline-prepare`, optionally polls `/offline-job/{id}` while an in-player "Preparing for streaming…" overlay shows, then sets `<video>.src` to the returned URL. Subtitle `<track>` elements load straight from `/subtitle` (server still converts SRT→VTT on the fly). Skip-intro, auto-advance, and resume logic all keep working unchanged against the streamed source.
- [x] **13.6** `saveProgress` simplified — single best-effort POST, no outbox. `_lpFlushProgress` still flushes on `pause`/`seeked`/`visibilitychange`/`pagehide` (with `navigator.sendBeacon` for `pagehide`) so the server's resume position stays within 15 s of reality.
- [x] **13.7** Offline tab + bulk "Save Offline (N)" footer button on the episode picker deleted. Tab nav is now Search + Library only.
- [x] **13.8** `docs/OFFLINE.md` rewritten as `docs/STREAMING.md`; `docs/API.md`, `docs/FRONTEND.md`, `docs/GOTCHAS.md`, and `CLAUDE.md` updated. Backend left intact apart from docstring/log copy edits.

---

## Milestone 8 — Mobile UX & Playback Fixes 

- [x] **8.1** Fullscreen UI: buttons fill the entire screen with no gaps; reserve space at top/bottom for device safe-area cutouts (env(safe-area-inset-*))
- [x] **8.2** Bug — Partial download playback: if the first file in a torrent hasn't downloaded yet, "Play All" and individual file play both fail silently; handle this gracefully
- [x] **8.3** Bug — Watch history not tracked when episode is launched from the episode list; history should be recorded regardless of how playback was initiated (stream-only excluded)
- [x] **8.4** Auto-open fullscreen player on mobile/small screens when something is already playing
- [x] **8.5** Next Episode continuity: hitting Next Episode should always advance to the next episode in series order, regardless of how the current episode was started
- [x] **8.6** Current track title display: show Season × Episode × and episode name (when name is unique to that episode) as the most prominent label for the playing track
- [x] **8.7** Optional Resume: per-profile setting controlling resume behavior when replaying an episode that has saved watch progress. Three modes:
  - **Auto** (default) — immediately seek to the saved position, no prompt
  - **Prompt** — start from the beginning; after playback starts a dismissible "Resume from X:XX?" tile appears in the player controls (similar to the Smart Skip offer tile) so the user can choose to jump to the saved position or stay at the start
  - **Off** — always start from the beginning, no prompt
  - Setting lives in the profile object (`resume_mode: "auto" | "prompt" | "off"`, default `"auto"`) and is toggled in the profile-settings modal (gear icon). Backend `/api/library/{id}/play` reads `resume_mode` and either seeks immediately, skips seeking entirely, or returns `seek_to=null` and also sends a `resume_offer` SSE event. Frontend renders the offer tile and `POST /api/resume-now` / `DELETE /api/resume-now` to accept/dismiss.

---

## Milestone 6 — Admin & Security

- [x] **6.1** Admin Dashboard: password-protected `/admin` panel (HTTPS needed, flag it)
- [x] **6.2** Content Lock: "admin-only" flag on library items, hidden from standard profiles
- [x] **6.3** Profile PINs: optional 4-digit PIN per profile, prompted before access
- [x] **6.4** Indexer Management: admin UI to view/add/remove Jackett indexers without editing `.env`
- [x] **6.5** Idle Background Video: admin uploads a single video file (saved under `.background/`) that plays on the TV in VLC whenever nothing else is. Admin-tunable volume. Replaced automatically the moment any stream or library item starts; resumed by `background_video_loop` whenever VLC reports stopped/idle. Settings live under `library.json → settings.background_video`.
- [x] **6.6** Server shutdown from admin: new **System** tab in `/admin` with a "Shut Down" button → `POST /api/admin/shutdown`. SIGTERMs every `uvicorn main:app` process (siblings first, self last), with a 3 s `os._exit(0)` fallback. Lets users stop StreamLink from a phone without SSH.

---

## Milestone 7 — System & Daemon

- [x] **7.1** Daemonization: `run.py --install` registers a launchd/systemd service for startup launch
- [x] **7.2** Watchdog: background process monitors VLC, qBit, Jackett; auto-restarts crashed services

---

## Milestone 14 — Per-Profile Visibility

- [x] **14.1** Per-profile default visibility: when downloading a torrent the user can uncheck profiles who should not see it by default. Stored as `default_visible_profiles` on the library item. If empty, item is visible to all.
- [x] **14.2** Hidden tab: profiles excluded by default (or who personally hide an item) see it in a "Hidden (N)" tab in the Library. The tab toggle shows/hides that view.
- [x] **14.3** Per-item hide/show button (eye icon): any profile can hide a visible item (moves it to their hidden tab) or restore a hidden item to the main list. Distinct from admin content lock.
- [x] **14.4** `POST /api/library/{id}/visibility` endpoint: `{profile_id, hidden}`. Stores overrides in `default_visible_profiles` / `hidden_by_profiles` on the library item.
- [x] **14.5** Profile deletion cleans up visibility lists across all library items.

---

## Milestone 15 — Slow-Network Playback Responsiveness

- [x] **15.1** Server: make `/api/library/{id}/play`, `/api/vlc/prev`, `/api/vlc/next`, `/api/stop`, and `/api/stream` non-blocking. They update state + broadcast a `buffering` (or `idle`) state event, then return 202 and run the VLC `in_play`/`in_enqueue` and qBit deletes in background tasks. New `state.library_play_task` is cancelled by subsequent Play / Stop so a slow handoff can't race a newer action.
- [x] **15.2** Client: optimistic buffering UI. New `_optimisticBuffering(label, itemId)` flips the player UI to `buffering` the instant the user clicks Play; on mobile the fullscreen overlay opens immediately so the user always has visible feedback while the server is still mid-handoff.
- [x] **15.3** Client: in-flight guards on Play. `continueLibraryItem` and `playLibraryFiles` run under `withInflight("play_${itemId}", …)` so double-taps during a slow handoff are dropped instead of racing extra `in_play` requests to VLC.
- [x] **15.4** Client: visible SSE-connection state + Play guard. The navbar SSE pill (`LIVE` / `OFFLINE`) is no longer mobile-hidden, and after a 4 s reconnect grace the app blocks new Play actions and shows a "Lost connection to host — reconnecting…" toast until SSE re-opens.

---

## Milestone 16 — Multi-Track Stream-to-Device (HLS)

Replaces the single-MP4 prep with an HLS bundle so every MKV audio track and every text-based subtitle survives into the in-browser local player, and users can switch between them at runtime.

- [x] **16.1** Backend: rewrite `_run_offline_job` to emit per-source HLS bundles (`<sha>/master.m3u8` + per-rendition `.m3u8` + fmp4 segments + `meta.json`). One ffmpeg invocation with `-var_stream_map` maps the video, every audio (transcoded to AAC stereo), and every text subtitle (transcoded to WebVTT). Image-based subs (`pgs`/`vobsub`/`dvb`) are filtered out and surfaced via `meta.json:skipped_image_subs`. Video stream-copies when source is already H.264 yuv420p with a non-Hi10 profile; otherwise `libx264 -preset veryfast -crf 23` (or `h264_nvenc` when available). ffmpeg ≥ 4.3 is required and enforced via `_ffmpeg_version()` probe — older builds fail-fast with a clear error.
- [x] **16.2** Backend: bump `OFFLINE_CACHE_VERSION` to `v3-hls`. Cache layout changes from `<sha>.mp4` to `<sha>/` directory. Old MP4 caches become orphans and are surfaced in the admin Offline Cache tab under a new `legacy` kind for one-click purge.
- [x] **16.3** Backend: replace `/api/library/offline-cache/{name}` with `/api/library/offline-cache/{cache_key}/{filename}`. Strict regex validation on both segments (`[a-f0-9]{24}` + `[A-Za-z0-9._-]+`) prevents path traversal. Per-extension media types for `.m3u8` / `.m4s` / `.vtt`.
- [x] **16.4** Backend: `_build_offline_cache_inventory` / `_delete_cache_artifacts` / `_offline_cache_path_active` are now directory-aware. `_dir_size_bytes` helper recurses into bundle dirs for accurate per-item size totals.
- [x] **16.5** Backend: new `LocalTracksReq` model and `/api/library/{id}/local-tracks` endpoint persist the in-browser player's audio/subtitle picks per-profile per-file under `local_audio_idx` / `local_subtitle_idx`. Kept separate from VLC's `audio_track` / `subtitle_track` because the two systems use different addressing (ES IDs vs HLS rendition indices). `update_progress` and `mark_watched` preserve these fields across writes.
- [x] **16.6** Frontend: ship `static/vendor/hls.min.js` (hls.js v1.5.17, ~100 KB gz). Lazy-loaded on first local-player open via `_ensureHlsLib()` so it doesn't slow the dashboard's initial paint.
- [x] **16.7** Frontend: rewrite `_lpLoadIndex` to branch on `Hls.isSupported()` — hls.js for Chrome/Firefox/Edge (MSE-based), `<video>.src = master_url` for Safari (native HLS). Audio/sub dropdowns populated from the bundle's `meta.json` (returned by `/offline-prepare`). Per-row sidecar `.srt`/`.vtt` files coexist with bundle subs as additional dropdown options.
- [x] **16.8** Frontend: `lpSetAudio(idx)` and `lpSetSubtitle(idx)` switch tracks at runtime — `hls.audioTrack` / `hls.subtitleTrack` on the MSE path, `AudioTrackList.enabled` / `TextTrackList.mode` on Safari. Each switch POSTs to `/local-tracks` so the pick comes back the next time the user plays this file.
- [x] **16.9** Docs: rewrite `docs/STREAMING.md` for the HLS flow; add HLS-specific footguns to `docs/GOTCHAS.md` (ffmpeg version floor, segment-boundary seek granularity, hls.js-vs-native branching, sidecar sub index offset, cache-version migration).
- [x] **16.11** Backend observability: add Python `logging` to `main.py` (`_init_logging`) with rotating file handlers — `logs/streamlink_app.log` (all) and `logs/hls.log` (HLS, via the `streamlink.hls` child logger) — plus a `WARNING`-capped stderr handler. Instrument `_run_offline_job` to log job START/DONE, the exact ffmpeg command, and on failure the return code + elapsed + last 300 lines of ffmpeg stderr. Drain ffmpeg's stderr concurrently (fixes a latent full-pipe-buffer deadlock). Docs updated: `docs/BACKEND.md` (new Logging section), `docs/GOTCHAS.md` (stderr-drain + "read hls.log" footguns), `docs/STREAMING.md` (debug pointer).
- [x] **16.12** Fix: HLS prep had *never* succeeded on a file with subtitles (every real MKV) — `logs/hls.log` exposed `No streams to mux were specified` / `Could not write header`. ffmpeg's HLS muxer can't package multi-track WebVTT (verified on 8.1.1, both `fmp4` and `mpegts`). Reworked `_build_hls_ffmpeg_args` to emit a **video+audio-only** bundle plus one standalone `sub_<i>.vtt` sidecar per text sub in the same ffmpeg pass; `meta.json` subtitle entries now carry `file` not `playlist`. Player wires bundle subs as `<track>` children alongside on-disk sidecars (`lp.subTracks`, `_lpApplySubIdx` toggles `track.mode`); the `hls.subtitleTrack` path is gone. `OFFLINE_CACHE_VERSION` → `v4-hls`. Docs: STREAMING.md + GOTCHAS.md updated.
- [x] **16.13** Stream-to-device gated off on macOS hosts: ffmpeg/ffprobe (children of the non-GUI server process) are blocked by macOS TCC from reading `~/Downloads`/`~/Desktop`/`~/Documents` (`Operation not permitted`), so prep always failed at the probe step. `HLS_AVAILABLE = platform.system() != "Darwin"` short-circuits the prep endpoints + job with a clear message, `state_snapshot` exposes `hls_available`, and the UI hides all Prep / On-Device controls (`no-hls` body class) and routes the play chooser straight to VLC.
- [ ] **16.10 (deferred)** Render `ass`/`ssa` subtitle styling via [libass.js](https://github.com/libass/libass) so karaoke effects, positioning, and fonts survive into the browser. Adds ~200 KB JS + a WebAssembly font renderer. Currently SSA/ASS text is converted to plain WebVTT, losing styling. Worth doing only if a user actually complains about plain-text subs.

---

