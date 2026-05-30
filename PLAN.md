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
  - **Boot-timing fix (v2.10.1):** the installed service registered mDNS once at startup, but at boot it runs before Wi-Fi has a LAN IP, so `remote.local` was silently skipped and never resolved until a manual relaunch (the IP still worked). `start_mdns_resilient()` now registers from a background daemon thread that waits for the LAN IP and re-registers if it changes; used by both `run.py` and the service wrapper. See [docs/GOTCHAS.md](docs/GOTCHAS.md).
- [x] **5.2** Smart Skip: audio fingerprinting to detect and skip intro/credit sequences on library files
  - Auto-skip now warns on the TV before acting: a VLC `marq` sub-source renders a bottom-right countdown popup (10 s before credits auto-advance, 5 s before intro seek). See [docs/ANALYZER.md](docs/ANALYZER.md#auto-skip-countdown-on-tv-marquee).
  - Per-file failure tracking (v3.10.0): every file the analyzer touches now lands in `skip_data` — failures carry `analysis.source = "failed"` with an `error_code` + `error`. Users see a "⚠ Intro/credits skip not available" chip on affected items; admins get full per-file error detail in the Smart Skip editor + a new Fingerprint Log panel fed by `state.analyzer_log` (in-memory ring buffer of recent fingerprint events). Failed files auto-retry on the next ready-flip in the same series. See [docs/ANALYZER.md § Failure tracking](docs/ANALYZER.md#failure-tracking).
- [-] **5.3** Control API: documented JSON POST endpoints for external play/pause/seek/volume control
- [x] **5.4** Subtitle Download: Find subtitles for the track by hash or name
- [x] **5.5** Windows targetted Full Setup and Startup Automation: Setup should install ALL dependencies including optional ones. setup should also install the service and the service should be able to startup all depencies on its own. (assuming vpn handles itself starting, just dont start qbittorrent until vpn is on and connected)
  - `setup.py` now auto-installs core apps (VLC/qBittorrent/Jackett/Mullvad) via winget on Windows (brew casks on macOS), in addition to the existing ffmpeg/fpcalc install. winget "already installed / no upgrade" exit codes are treated as success.
  - Jackett detection (setup.py, run.py, watchdog.py) now scans every Program Files / LocalAppData / ProgramData location the Windows installer may use, for both `JackettConsole.exe` and `jackett.exe`.
  - `setup.py` offers to register the system service at the end (defaults to yes on Windows); on Windows, service install self-elevates via UAC when not run from an admin shell. The service's watchdog starts all deps on its own, gating qBittorrent on VPN connection.
  - Re-running setup and declining the "reuse .env" prompt now pre-fills every prompt with the current `.env` value (Enter keeps it; secrets are masked, not echoed), so it doubles as a config health check (v2.11.0).


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

- [/] **10.1** UI warning if any dependency/service is unreachable (VLC, qBittorrent, Jackett): currently only shown at startup in the terminal; surface these as persistent in-app banners so users on mobile know why things aren't working
  - Jackett reachability is now published (`state.jackett_ok` in `state_snapshot()` + the `jackett_status` SSE event, from `jackett_health_monitor`). The frontend banner still needs wiring; VLC/qBit reachability not yet surfaced.
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
  - **Jackett hardening (v2.9.0):** liveness is now an HTTP probe (`GET /UI/Login`), not a bare port check — a hung Jackett holds the port open while it stops serving. Restart force-stops the wedged service/process (`pre_restart`) before relaunching, since `sc start` is a 1056 no-op on a hung service. `setup.py grant_jackett_service_control()` grants the non-elevated account `sc` start/stop rights (+ `sc failure` recovery) so the watchdog can recover Jackett without a reboot. Mirrored in `run.py` startup and `main.py`'s `jackett_health_monitor` backstop.

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
- [x] **16.14** Fix: HLS playback stalled with a fatal `fragLoadError` even though the manifest + audio/sub dropdowns loaded — the fmp4 init segment 404'd. Two parts: (a) v3.2.1 pinned `-hls_fmp4_init_filename "init_%v.mp4"` so the init *name* matches the `#EXT-X-MAP:URI=` (ffmpeg's default `%v` expansion didn't); the on-screen alert + console now log the failing URL + HTTP code, which pinpointed it. (b) v3.2.2 fixed the init *location* on Windows: ffmpeg parses the playlist path to decide where to write the init file, and a backslash playlist path misdirected it out of the bundle. Now **every output is a bare filename** and ffmpeg runs with `cwd=<bundle .part dir>` (only `-i` is absolute); `_build_hls_ffmpeg_args` dropped its `out_dir` param. `OFFLINE_CACHE_VERSION` → `v6-hls`. Docs: STREAMING.md + GOTCHAS.md.
- [ ] **16.10 (deferred)** Render `ass`/`ssa` subtitle styling via [libass.js](https://github.com/libass/libass) so karaoke effects, positioning, and fonts survive into the browser. Adds ~200 KB JS + a WebAssembly font renderer. Currently SSA/ASS text is converted to plain WebVTT, losing styling. Worth doing only if a user actually complains about plain-text subs.
---

## Milestone 17 — Bidirectional Handoff (TV ⇄ device, time-synced)

Move an in-progress library play between the TV (VLC) and the requesting browser
in either direction, resuming at the exact same position. Distinct from
Milestone 11's retired download-to-device "Handoff" — this is a live transfer
over the Stream-to-Device path.

- [x] **17.1** Server: publish `library_item_id` and the full ordered `library_playlist` in `state_snapshot()` so the client can reconstruct the remaining-playlist tail at handoff time.
- [x] **17.2** Client (TV → device): `handoffToDevice()` captures VLC's live position (`GET /api/vlc/tracks`), slices the playlist tail from `library_current_file` forward, stops VLC (`POST /api/stop`, 202), and starts the local `<video>` player (`lpPlay`) seeked to the captured time. Gated by `withInflight("handoff")`.
- [x] **17.3** UI (TV → device): emerald **Device** button in the player footer (next to Stop) + **To Device** tile in the fullscreen controls (next to Stop), both shown only during library playback (`is_library_playback && library_item_id`). Both are hold-to-activate (0.5 s `.hold-btn` fill) so an accidental tap can't pull playback off the TV.
- [x] **17.3a** Prep gating: the handoff-to-device button greys out (with a "Not prepped for on-device streaming" note) when the current file has no `.offline_cache` MP4 / isn't Safari-native. Readiness from `prepFileState` + a per-file `/prep-status` check (`_handoffReadyState` / `_maybeRefreshHandoffReady`); flips to active once prepped. Tapping while not prepped shows a toast.
- [x] **17.4** Client (device → TV): `lpHandoffToVlc()` captures the local `<video>` position + remaining playlist tail, stops the device player (flushing progress), and starts VLC at the captured time via `playLibraryFiles` (`POST /api/library/{id}/play` with `seek_first_to`). Gated by `withInflight("handoff_vlc")`.
- [x] **17.5** UI (device → TV): indigo **To TV** button in the local player's fullscreen header (next to Stop).

---

## Milestone 18 — Machine Reboot & Scheduled Restart (Jackett hard-reset)

A full host reboot is the only reliable cure for some wedged-Jackett states.
Surfaces both a manual reboot and an automated nightly idle-gated one in the
admin **System** tab. Requires host auto-login + the installed service for the
box to come back on its own (documented in README).

- [x] **18.1** Server: `POST /api/admin/reboot` reboots the whole host. `_reboot_machine()` tries a platform command chain (macOS System Events restart / `sudo -n shutdown` / `shutdown`; Linux `systemctl reboot` / sudo / shutdown; Windows `shutdown /r /t 0`), fires ~0.5 s after the response flushes, and logs a hint if it lacks permission.
- [x] **18.2** Server: `scheduled_reboot_loop` background task + `GET`/`POST /api/admin/scheduled-reboot`. Config in `library.json → settings.scheduled_reboot` (`enabled`, `time` HH:MM, `timezone`, `idle_minutes`). At the configured local time it reboots when idle for `idle_minutes`, else waits and re-checks until idle. Persisted `last_fired` date guards against a post-reboot re-arm loop; saving config resets it.
- [x] **18.3** Server: idle detection. `track_activity` middleware stamps `state.last_activity` on mutating verbs + `/api/search` (routine GET polling excluded). `_machine_in_use()` also treats live VLC playback/pause of non-background content and any active stream/download as in-use, so a reboot never interrupts watching or a download.
- [x] **18.4** Admin UI: **Reboot Machine** (confirm-gated) + **Scheduled Restart** panel (enable toggle, time, timezone select, idle window, host-time display) in the System tab.
- [x] **18.5** README: documents the auto-login + `run.py --install` requirement so the host returns to a running StreamLink after a reboot.

---

## Milestone 19 — HLS Quality Selection (ABR + in-player menu)

Adds adaptive-bitrate streaming to the on-device HLS player: each bundle carries
multiple video renditions and the player offers a YouTube-style quality menu
(Auto + manual). Trades longer prep + ~1.7× storage for live quality switching
and graceful degradation on weak links.

- [x] **19.1** Backend: `_hls_video_variants(info)` builds the ladder **Original + 720p + 480p**, capped at source height (a rung is added only when the source is taller — ≤480p source ⇒ single variant). VBV caps per rung (`HLS_ABR_LADDER`: 720p≈3 Mbps, 480p≈1.2 Mbps).
- [x] **19.2** Backend: `_build_hls_ffmpeg_args` maps `0:v:0` once per rung and sets index-qualified per-output codec/scale (`-c:v:i`, `-filter:v:i scale=-2:<h>`, `-maxrate:v:i`/`-bufsize:v:i`). The original rung stream-copies when browser-safe **even with NVENC present** (decoupled from `use_nvenc`); down-rungs always transcode (NVENC on GPU else libx264). `-var_stream_map` emits one `v:i,agroup:aud,name:…` entry per rung sharing one audio group. Returns the variant list; `meta.json` + `/offline-prepare` + `/offline-job` gain `videos[]`.
- [x] **19.3** Backend: bump `OFFLINE_CACHE_VERSION` to `v7-hls-abr` so single-rendition `v6-hls` bundles rebuild; old dirs surface as orphans for purge.
- [x] **19.4** Frontend: add a **Res** dropdown to `#lpTrackRow`. `_lpRenderTrackRows` builds it from `lp.hls.levels` (Auto + each resolution, hidden when ≤1 level); `lpSetQuality(idx)` sets `lp.hls.currentLevel` (`-1` = Auto). hls.js-only — Safari native HLS stays auto (no manual-level API). Quality is session-only (not persisted).
- [x] **19.5** Docs: STREAMING.md (output layout, ffmpeg invocation, player flow, storage cost), GOTCHAS.md (ABR var_stream_map shape + quality-menu engine branch), API.md (offline endpoints `videos[]`), FRONTEND.md (`_lpRenderTrackRows` / `lpSetQuality`). Version → 3.3.0.

---

## Milestone 20 — Interruptible & Scheduled Stream-Prep

Stream-prep pegs the host CPU, so warn before it starts, let anyone pause/resume
it from the non-admin UI, and let the admin schedule it to run overnight.

- [x] **20.1** Client warning: `confirmStreamPrepWarning()` shows `#prepWarnModal` before a user-triggered prep (`prepItemForStreaming` / `prepForStreaming`) explaining the host will get laggy. Acknowledged once per session (`_prepWarnAcked`). The interactive play-on-device path does not warn.
- [x] **20.2** Server pause gate: prep jobs are tagged `queue:"bulk"` (per-item / per-row / overnight) vs `"interactive"` (play-on-device). `state.prep_paused` gates bulk jobs in `_run_offline_job` — a paused bulk job marks itself `"paused"` and exits its task, *releasing* the concurrency slot so interactive prep still runs; `_resume_prep()` re-spawns it. `_pause_prep(kill)` supports "finish current file then stop" (`kill=False`) and "stop now" (`kill=True`, terminates ffmpeg via `job["_proc"]` + `_paused_kill`, restarts from scratch on resume).
- [x] **20.3** Non-admin pause/resume UI: `#globalPrepBar` gains Pause (→ choice modal: finish-current vs stop-now) and Resume buttons. `POST /api/offline-prep/pause {kill}` + `POST /api/offline-prep/resume`. Paused state surfaced in `/api/offline-active`, `/prep-status`, `state_snapshot`; per-card chip shows "Prep paused".
- [x] **20.4** Overnight auto-prep: `overnight_prep_loop` queues a bulk job for every un-prepped library file during an admin window, then on window end either pauses (in-flight file finishes) or continues to completion. Config in `library.json → settings.overnight_prep` (`enabled`, `start`/`end`, `timezone`, `on_end`); window-crossing-midnight supported. `GET/POST /api/admin/overnight-prep`; admin **System → Overnight Stream Prep** panel.
- [x] **20.5** Docs + version → 3.4.0: STREAMING.md (pause/resume + overnight section), ADMIN.md (System tab subsection), API.md (new endpoints + paused fields), LIBRARY_DATA.md (`settings.overnight_prep`), CHANGELOG.
- [x] **20.6** Keep the dashboard responsive during prep (v3.4.1): ffmpeg runs at lowered OS priority on all platforms (`nice -n 10` on POSIX via `_ffmpeg_nice_prefix`, `BELOW_NORMAL_PRIORITY_CLASS` on Windows); recursive bundle FS ops in `_run_offline_job` offloaded with `asyncio.to_thread`; `/prep-all` + `_enqueue_library_prep` yield (`await asyncio.sleep(0)`) between files so kicking off prep on a large pack/library never stalls the event loop.
- [x] **20.7** Make StreamLink the box's top priority (v3.4.2): the server raises its own OS priority at startup (`_raise_own_priority` in `lifespan` — `HIGH_PRIORITY_CLASS` on Windows, negative `nice` on POSIX) so controls/UI/VLC-control stay responsive even on a saturated box. All background CPU work is kept below it — prep ffmpeg (already low) plus the Smart-Skip analyzer subprocesses (`analyzer._lp` / `_LOWPRIO_KW`), which would otherwise inherit the server's raised priority. Net: server ≫ VLC/qBit ≫ prep/analyzer.
- [x] **20.8** Idle-triggered auto-prep (v4.4.0): `overnight_prep_loop` is folded into a unified `auto_prep_loop` driving two triggers (`want = overnight window open OR idle-prep enabled AND host idle ≥ idle_minutes`); `state.overnight_active` → `state.auto_prep_engaged`. The new idle trigger preps whenever `_machine_in_use(idle_minutes*60)` is False and pauses with `kill=True` (discarding the in-flight encode) the moment activity returns; activity always overrides overnight Continue. Config in `library.json → settings.idle_prep` (`enabled`, `idle_minutes` clamped 1–720); `GET/POST /api/admin/idle-prep`; admin **System → Idle Auto-Prep** panel. Docs: STREAMING.md, ADMIN.md, API.md, LIBRARY_DATA.md, BACKEND.md, CHANGELOG.

---

## Milestone 21 — YouTube on TV

Play any YouTube link on the TV (the host display) and drive it from the
dashboard. Not VLC (its `youtube.lua` is broken) and not a download — the video
plays in a fullscreen Chrome kiosk via the YouTube IFrame Player API, controlled
remotely over SSE. See [docs/YOUTUBE.md](docs/YOUTUBE.md).

- [x] **21.1** Backend endpoints: `POST /api/youtube {url}` (extract id, take over now-playing state, stop VLC, broadcast `yt_command:load`, launch kiosk if `/tv` not seen <6 s), `POST /api/youtube/control {action,value}` (relay as `yt_command`), `POST /api/youtube/tv-state` (heartbeat + mirror time/duration/title/volume onto reused display fields, rebroadcast `state`), `GET /tv` (serve the kiosk page). New `AppState` fields `youtube_active` / `youtube_video_id` / `youtube_playback` / `youtube_tv_seen_at`; both surfaced in `state_snapshot`.
- [x] **21.2** Kiosk lifecycle: `_extract_youtube_id` (watch / `youtu.be` / shorts / live / embed / bare id), `_find_chrome` (Chrome → Chromium → Edge, or `_CHROME_BIN`), `_launch_tv_browser` (`--kiosk --app=http://localhost/tv?v=<id>` + `--autoplay-policy=no-user-gesture-required` + isolated `--user-data-dir=.tv_chrome_profile`), `_kill_tv_browser` (kills only the kiosk, matched by profile dir in cmdline). `/api/stop` clears YouTube state, broadcasts `yt_command:close`, hard-kills the kiosk.
- [x] **21.3** Poller gating: `stat_broadcaster` skips its VLC status read while `youtube_active` (so the YouTube-reported time/volume isn't clobbered); `background_video_loop` skips entirely (no idle background video over the kiosk). `vlc_progress_tracker` already no-ops without a library item.
- [x] **21.4** TV player page `static/tv.html`: YouTube IFrame API player, reads `?v=<id>` + listens for `yt_command` over `/api/events`, autoplay, POSTs `tv-state` every 1 s + after each command, queues a pre-ready `load`, ignores a `load` for the current id, `close` → pause + `window.close()`.
- [x] **21.5** Frontend: red **Play on TV** input at the top of the Search tab + `playYoutube()`; `ytControl()` relay; `vlcPause` / `vlcSeek` / `handleSeekBarClick` / `vlcSetVolume` / `vlcVolumeStep` / `vlcVolume` branch on `app.youtube_active`; save-to-library / handoff / episode-nav / track menus hidden in YouTube mode.
- [x] **21.6** Docs + version → 3.5.0: new docs/YOUTUBE.md, CLAUDE.md index, API.md (3 endpoints + `/tv` + `yt_command` SSE event), GOTCHAS.md (broken VLC youtube.lua, autoplay flag, reused display fields + poller gating, kiosk kill-by-profile), ARCHITECTURE.md (code map + data-flow), CHANGELOG. `.gitignore` for `.tv_chrome_profile/`.
- [x] **21.7** Fix "never starts on Windows" (v3.5.1): `_find_chrome` was too narrow (3 hard-coded Program Files paths) so a per-user `%LOCALAPPDATA%` Chrome / non-default Edge returned None → 500 → background-video bounce. Rewrote discovery to use the **`App Paths` registry (HKCU+HKLM)** + `%LOCALAPPDATA%` + both Program Files trees + PATH for Chrome/Edge/Brave/Chromium, with logging on both discovery and launch. Added `_youtube_kiosk_healthcheck` (12 s heartbeat watchdog → `stream_status:error` instead of a silent background fallback). CLAUDE.md now declares **Windows = primary platform** (then Linux, then macOS).
- [x] **21.8** Fix "kiosk launches but stays behind VLC / only opens when you click the taskbar" on Windows (v3.5.2): focus-stealing prevention denied the spawned browser window focus. `_bring_tv_to_front` (spawned by `/api/youtube`) now minimizes VLC (`vlc_minimize`) and forces the kiosk window forward by **title match** (`_TV_WINDOW_MARKER` == `tv.html` `<title>`) using the `_vlc_focus_windows` cocktail, retrying ~10 s. `vlc_focus_and_fullscreen` now bails when `youtube_active` so a lingering background focus loop can't minimize the kiosk / re-grab focus for VLC. Window helpers `_find_tv_browser_hwnds_windows` / `_focus_tv_browser_windows`.
- [x] **21.9** Fix "kiosk launched but `/tv` never checked in" on Windows + drive volume from the OS mixer (v3.6.1): the kiosk URL was `http://localhost/tv?v=…`; Windows resolves localhost to **both `::1` and `127.0.0.1`** with IPv6 first, and uvicorn binds `0.0.0.0` (v4 only), so Chromium hit ECONNREFUSED on `::1` and stalled past the healthcheck. Pinned the URL to `http://127.0.0.1`; added Edge first-run/signin/welcome-modal suppression flags (`--disable-fre`, `--disable-features=msImplicitSignin,SigninInterceptBubbleV2,…`, `--noerrdialogs`). Volume: the IFrame `setVolume` only scales pre-mixer gain, so videos played at system max regardless. Volume is now driven by the **host OS mixer** during YouTube — `_set_system_volume_sync` / `_get_system_volume_sync` (pycaw on Windows, `osascript` on macOS, `pactl`/`amixer` on Linux), IFrame locked at 100 % unmuted in `tv.html`'s `onReady`, `POST /api/youtube/control` handles `volume_*` server-side (no SSE relay), dashboard slider's `max` flips to 100. New setting **`settings.system_volume_default`** (0-100, default 70) restored on Stop after polling the kiosk process to actually exit (4 s deadline); `state.system_volume_before_yt` as fallback snapshot. New endpoints `GET`/`POST /api/settings/system-volume-default`, new request model `SystemVolumeDefaultReq`, **System Volume After YouTube** slider in the profile-settings panel. `pycaw` dep added to `requirements.txt` under `sys_platform == "win32"`.
- [x] **21.10** Fix "volume controls don't work on Windows" (v3.7.1): pycaw uses COM, and `asyncio.to_thread` dispatches to `ThreadPoolExecutor` workers without COM initialized → `AudioUtilities.GetSpeakers()` raised *"CoInitialize has not been called"* and the helper's `except` swallowed it. Volume helpers now run through `_windows_volume_op`, which calls `comtypes.CoInitialize()` / `CoUninitialize()` around each operation, and distinguishes a missing-pycaw ImportError ("run `pip install -r requirements.txt`") from a runtime failure. The volume endpoint now returns **503** with the diagnostic instead of silent `ok:false`, and `ytControl` shows the server's error message as a one-shot session-scoped alert so the user isn't left guessing why the slider doesn't move.
- [x] **21.11** Windows volume polish (v3.7.2 → v3.7.4): pinned `comtypes>=1.2.0` explicitly in `requirements.txt` (pycaw's transitive dep can get skipped on real boxes — older standalone installs, `--no-deps`, stale venvs); the ImportError diagnostic now names the *actual* missing module via `ImportError.name`; replaced `AudioUtilities.GetSpeakers()` (API-unstable — returns `IMMDevice` in some releases, an `AudioDevice` Python wrapper without `.Activate()` in others) with a direct `CoCreateInstance(CLSID_MMDeviceEnumerator).GetDefaultAudioEndpoint(eRender, eMultimedia)`, reusing only pycaw's COM interface *definitions* (which have been stable); moved the COM work into an inner closure `_do_com_work` so local COM pointers `Release()` before the outer `finally` calls `CoUninitialize` (without this, every successful volume change spammed *"COM method call without VTable / Exception ignored in __del__"* into the logs — order-of-teardown bug).
- [x] **21.12** YouTube starting volume (v3.8.0): new **global** setting `settings.youtube_start_volume` (0-100, default 30) pre-sets the OS mixer to a known level the moment a YouTube play starts — before the `yt_command:load` broadcast and before Chrome paints — so the IFrame player can't produce a first audio frame at whatever the OS happened to be at (often max). New `_youtube_start_volume`, `YouTubeStartVolumeReq`, `GET`/`POST /api/settings/youtube-start-volume`. UI: new red slider in the profile-settings panel under a new **GLOBAL — Applies to every profile** divider that clarifies the existing pattern (Max Volume + the two YouTube volumes are all global; auto-skip + resume mode are per-profile).

---

## Milestone 23 — Auto-Updater + Post-Update Env-Key Gating

Pull-and-restart so a remote box can keep itself current without an admin
needing shell access. Branch picker is locked to main / beta / alpha to make
sure casual one-button updates can't drag the box onto a development branch.

- [x] **23.1** Backend `updater.py` — async `git fetch / switch / reset --hard` plumbing + a non-interactive `setup.py` subprocess invoker + `service_is_installed()` so the apply path knows whether SIGTERM-ing uvicorn will actually relaunch.
- [x] **23.2** Background `updater_loop` (1-min tick, fires every `interval_hours`) reads `library.json → settings.autoupdate` and, when `auto_apply` is on, calls `_run_apply` only after `_machine_in_use(window_secs=300)` returns False. Active streams + downloads + recent admin clicks all defer the apply to the next cycle.
- [x] **23.3** Admin endpoints — `GET /api/admin/updater` (config + git state + live phase), `POST /api/admin/updater/config` (partial update), `POST /api/admin/updater/check` (force fetch + compare), `POST /api/admin/updater/apply` (full sequence with optional `restart:false`), `POST /api/admin/updater/switch-branch` (hard checkout, no setup, no restart).
- [x] **23.4** Admin UI — new **Updates** tab (`static/admin.html`) with branch picker (main / beta / alpha), check interval (1-168 h), auto-apply toggle, status panel (current branch / commit / last check / last applied / live phase message), and **Check Now** / **Apply Now** / **Switch Branch** / **Save** controls. Last `setup.py` output is collapsible for diagnostics.
- [x] **23.5** Env-key feature registry — `ENV_KEY_FEATURES` declares which `.env` keys gate which features (`ADMIN_PASSWORD` + `INDEXER_API_KEY` required, `JACKETT_PASSWORD` + `TMDB_API_KEY` optional). `_missing_env_keys()` exposes the gaps to the client via `state_snapshot()`. `_write_env_keys` atomically merges into `.env` (preserves comments + key order); `_reload_settings` re-instantiates the pydantic Settings so live writes take effect in-process.
- [x] **23.6** Endpoints `GET`/`POST /api/admin/env-keys` plus the **Required API Keys** card on the Updates tab. Sensitive keys render as `type="password"`; "(Already set — type to replace)" prefilling stops the form from accidentally clearing a non-empty key on save.
- [x] **23.7** Non-admin UX — sticky banner below the navbar (`#serverAttentionBanner`) driven by `renderServerAttention(d)` on the SSE `state` event. Amber for missing required env keys ("Server needs admin attention: …"), indigo while an update is being applied, **red while the host is rebooting** so non-admin viewers see the disruption coming.
- [x] **23.8** (v3.9.1) Full-cleanup apply: after the git pull + setup.py, the apply path now also **uninstalls + reinstalls the OS service** (`updater.reinstall_service()` → `daemon.uninstall()` + `daemon.install()` on a worker thread, regenerating `streamlink_service.py` from the new code) and then **reboots the host** instead of SIGTERM-ing uvicorn. Service reinstall is best-effort — a failure is logged but the reboot still fires; the previously-installed service definition keeps the dashboard alive after reboot. Phases reported via SSE: `applying → setup → reinstalling-service → rebooting`. `_kill_self_for_restart` removed.
- [x] **23.9** (v3.9.1) Switch-back-to-older-branch is first-class through Apply Now — the button uses the picker's current value (not the saved config), so `alpha → main` is one confirm-gated click. `git switch -C origin/<branch>` + `git reset --hard` is symmetric in both directions; gitignored state survives. Confirmation dialog explicitly names the direction.
- [x] **23.10** (v3.13.0) **Reset Hard** button + `POST /api/admin/updater/reset-hard` (`updater.reset_hard()`) — recovery for a wedged/diverged checkout: `git fetch` + `git reset --hard origin/<current-branch>`, discarding local commits + tracked-file edits while staying on the same branch (no switch, no `git clean`, no setup, no reboot). Gated to `ALLOWED_BRANCHES`; untracked/gitignored state survives. Confirm-gated.

---

## Milestone 22 — Remote log download

Pull the server's rotating log files off the host from the admin panel, so an
operator on a remote (especially Windows) box can diagnose problems without
SSH / RDP.

- [x] **22.1** Backend: `GET /api/admin/logs` (listing with size + mtime, newest first), `GET /api/admin/logs/{name}` (single file as attachment — `_safe_log_path` rejects slashes / `..` / absolute paths / anything that resolves outside `LOG_DIR`), `GET /api/admin/logs/_bundle` (streamed ZIP via the `os.pipe()` pattern shared with `/api/library/{id}/zip`, filename `streamlink-logs-<timestamp>.zip`). All three require admin auth; the per-file route accepts the token via `?admin_token=` query param so plain `<a download>` anchors work.
- [x] **22.2** Admin UI: **Server Logs** card in the System tab. Lists every file in `logs/` with size + mtime, per-row Download, plus **Download All (.zip)** at the top. `loadLogs()` runs alongside `loadScheduledReboot()` / `loadOvernightPrep()` when the tab opens; Refresh button re-reads.
- [x] **22.3** Docs + version → 3.6.0: API.md (the 3 new endpoints), ADMIN.md (new "Server Logs" subsection under System), CHANGELOG.
- [x] **22.4** Clear logs (v3.7.0): `DELETE /api/admin/logs` + **Clear All** button in the Server Logs card (confirm-gated). Active rotating handlers are **truncated in-place** via `handler.stream.truncate(0)` (deleting would orphan the open FD on Windows / leave POSIX writes vanishing into a disconnected inode); non-active siblings (rotated `.1`/`.2`/`.3`, `streamlink.err`) are unlinked with a write-mode-truncate fallback for the Windows-service-holds-the-handle case. Returns `{ok, cleared, errors}`.

---

## Milestone 24 — AI Auto-Generated Subtitles (speech-to-text)

Transcribe audio into a subtitle track for sources that ship none usable, so
both VLC and the on-device player always have subtitles available. whisper.cpp
on the host; output is a sidecar `.srt` that reuses the existing sidecar
plumbing (no manifest/bundle change). Preprocess (prep + overnight) **and**
on-demand. See [docs/STT.md](docs/STT.md).

- [x] **24.1** New `stt.py` module — `_extract_wav` (ffmpeg → 16 kHz mono PCM), `_run_whisper` (whisper-cli `-osrt`, `-tr` for translate, auto-detect language, stderr progress parse), `generate()` (transcribe in source language always; add an English-translated track when the detected language isn't English). Output `<stem>.<lang>.ai.srt` next to the source. Self-contained low-priority subprocess discipline (`_LOWPRIO_KW` / `_lp`) like analyzer.py.
- [x] **24.2** Trigger + jobs in `main.py` — `_needs_stt_subs(info, default_lang)` (no text sub / image-only / no language match → generate), `_stt_cfg` + `_canon_lang`, `_stt_jobs` + `_run_stt_job` + `_maybe_start_stt_job` (share the offline-prep semaphore + bulk pause gate), `_ensure_stt_for`. `_list_sidecar_subs` gains an `ai` flag. Post-HLS hook in `_run_offline_job`; overnight backfill for already-cached files in `_enqueue_library_prep`; `state.stt_available` (cached probe).
- [x] **24.3** Endpoints — `POST /api/library/{id}/generate-subtitles` (on-device, interactive), `POST /api/subtitles/generate` (VLC current file, loads + selects on done), `GET /api/stt-job/{id}` (status + `tracks` + `subs`). `offline-job` done response now also returns `subs[]`.
- [x] **24.4** Admin — `GET`/`POST /api/admin/stt` + **Auto-Generated Subtitles** card in the System tab: enable toggle, preferred default language, English-translation toggle, unavailable banner. `settings.stt` in library.json.
- [x] **24.5** Setup — `install_stt_deps` downloads portable whisper.cpp + the multilingual `ggml-base` model into `tools/whisper/` (Windows), brew on macOS, model-only on Linux. `detect_tools` finds binary + model; `.env` gains `_WHISPER_BIN` / `_WHISPER_MODEL`.
- [x] **24.6** Frontend — VLC subtitle modal **Generate with AI** action + translate checkbox (`generateSubsVlc`), on-device player **AI** button in the sub row (`lpGenerateSubs` → `_lpAttachSidecarSubs`, non-blocking), shared `_pollSttJob`, `sttAvailable` from `/api/state`, "(AI)" labels on generated tracks.
- [x] **24.7** Docs + version → 4.0.0: new docs/STT.md, CLAUDE.md index, STREAMING.md (post-prep STT hook), SETUP.md (whisper deps + env), API.md (endpoints), GOTCHAS.md (multilingual model + English-only translate), ARCHITECTURE.md (code map), CHANGELOG.
- [x] **24.8** (v4.0.1) Fix: admin Offline Cache tab froze the server — `_build_offline_cache_inventory` ran the full-cache `_dir_size_bytes` walk inline on the event loop; now offloaded via `asyncio.to_thread` (`_offline_cache_inventory_sync`, job list snapshotted). GOTCHAS note added.
- [x] **24.9** (v4.0.2) Fix: Generate-subtitle affordances only appeared after a full restart+reload once whisper was installed — `_stt_available()` now re-probes on a 60 s TTL (was cached forever), and the dashboard updates `sttAvailable` live from the SSE `state` stream.
- [x] **24.10** (v4.0.3) Fix: whisper.cpp binary download 404'd (pinned a release tag that doesn't publish the Windows zip). `setup._resolve_whisper_win_url()` now resolves the asset URL from the GitHub releases API, pinned fallback v1.8.4.

## Milestone 25 — Admin Optional-Components installer

The auto-updater runs `setup.py` non-interactively (skips all `install_*`), so portable deps the box lacked — chiefly whisper.cpp + model — never landed without a manual terminal run. Let the admin install/update them from the web instead.

- [x] **25.1** Backend — `_run_component_install` (httpx-streamed download with progress → `setup._extract_archive` → `setup._find_in_tree` → `_write_env_keys` → clear ffmpeg-version/NVENC/STT caches) for `ffmpeg` / `fpcalc` / `whisper` / `whisper_model`; `_component_status_payload` reuses `setup`'s candidate finders + `_resolve_whisper_win_url`. ffmpeg/whisper binaries Windows-only; fpcalc+model any OS. Persists across auto-update via `detect_tools()`+`merge_tool_paths()`.
- [x] **25.2** Endpoints — `GET /api/admin/components` (status + in-flight job), `POST /api/admin/components/install {component, model?}`.
- [x] **25.3** Frontend — **Optional Components** card in the System tab (`loadComponents`/`_renderComponents`/`installComponent`): per-component Installed/Missing badge + path + Install/Reinstall, whisper model size picker (base/small/medium), live progress bar, polls while installing.
- [x] **25.4** Docs + version → 4.1.0: ADMIN.md (Optional Components subsection), SETUP.md (admin-install path + autoupdate skip rationale), API.md (2 endpoints), STT.md (no-terminal install note), CHANGELOG.
- [x] **25.5** (v4.2.0) GPU (CUDA) whisper builds: `setup._resolve_whisper_win_url(build)` resolves cpu/cuda12/cuda11 cuBLAS assets; Components card gains a whisper build picker (recommends CUDA when `nvenc`); `_run_whisper` retries with `-ng` on failure for a graceful CPU fallback; `/api/admin/components` reports `nvenc`, install accepts `build`. Docs: STT.md (GPU section), ADMIN.md, API.md.

---

## Milestone 26 — Regenerate subtitles on model change

Generated subs are tagged with the model that produced them so a model upgrade (base→medium, CPU→GPU build, etc.) can be detected and re-run on demand, and the model is visible in the track name.

- [x] **26.1** (v4.3.0) Sidecars renamed `<stem>.<lang>.ai.<model>.srt`; `stt.model_name()`, `_list_ai_subs()`, `ai_subs_stale()`; `generate()` tags new files with the current model and removes superseded ones (different model / legacy untagged) after success. `_run_whisper` unchanged.
- [x] **26.2** (v4.3.0) `_maybe_start_stt_job` treats same-model subs as `cached`, different-model as regenerable (explicit requests only — `_ensure_stt_for` preprocess stays idempotent on existence so a model change never mass-re-transcodes). `_list_sidecar_subs` exposes per-sub `model` + `stale`.
- [x] **26.3** (v4.3.0) Frontend: on-device subtitle button flips **AI** → **Regen** when stale; track dropdown + `<track>` labels show the model (“English (AI · base)”); VLC "Generate with AI" regenerates on model change (cached when matched). Docs: STT.md, CHANGELOG.

---

## Milestone 27 — AI subtitle timing precision

Generated subs were timed wrong around long pauses (lines lingering across silence / starting early). whisper's native segment timestamps are coarse and don't respect pauses.

- [x] **27.1** (v4.5.0) `_run_whisper` adds **DTW token-level timestamp alignment** (`-dtw <preset>`, cross-attention warped against audio — accurate boundaries that respect pauses; no extra download) + **word-boundary cue splitting** (`-ml STT_MAX_LEN` + `-sow`) so each cue carries its own accurate timing. `stt._dtw_preset()` maps `model_name()` → preset via `_DTW_PRESETS`, disabling DTW for unmappable models (a mismatched preset errors the run). CPU-fallback retry keeps the flags. Docs: STT.md (Timing precision), GOTCHAS.md, CHANGELOG.

---

## Milestone 28 — AI subtitle cross-window drift mitigation (VAD + max-context)

DTW fixed *within-cue* timing but not the **cross-window drift** that accumulates over long (~1 hr) media: whisper decodes in 30s windows and advances its cursor by its own timestamp tokens, so one misjudgment shifts everything after it and compounds.

- [x] **28.1** (v4.6.0) `_run_whisper` adds optional **`--vad --vad-model`** (Silero speech-region segmentation — each region transcribed at its own correct offset). VAD gated on `stt.whisper_vad_model()` + cached `_whisper_supports_vad()` `--help` probe; retry cascade sheds VAD then GPU independently. Never part of `is_available()`. **(v4.6.1: the `-mc 0` lever that also shipped in 4.6.0 was reverted — it tanked transcription quality; pipeline bumped to `g3` so bad subs flag stale. VAD is the sole drift lever. See GOTCHAS.)**
- [x] **28.2** (v4.6.0) Silero VAD model (`ggml-silero-v5.1.2.bin`, ~1 MB) bundled by `setup.py` into `tools/whisper/vad/` (excluded from the transcription-model glob), `.env` `_WHISPER_VAD_MODEL`, installable on every OS via the `whisper_vad` Optional Component. `main._vad_active()` surfaced in `/api/admin/stt` + `/api/admin/components`; admin STT card shows VAD status.
- [x] **28.3** (v4.6.0) Pipeline-generation tag `g<N>[v]` in sidecar names (`sub_gen_tag()`); `ai_subs_stale` / `_list_sidecar_subs` flag a sub stale on a different model **or** older pipeline, so existing subs offer **Regenerate** to pick up the new timing.
- [x] **28.4** (v4.6.0) Fix: auto-update reset the whisper model to `base` — `detect_tools()` now prefers the model already in `.env` when it exists, only auto-picking when gone. Docs: STT.md (Timing precision), GOTCHAS.md (silero glob + model-reset), SETUP.md, ADMIN.md, CHANGELOG.

---

