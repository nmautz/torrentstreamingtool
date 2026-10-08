# CLAUDE.md

Guidance for Claude Code working in this repo. This file is intentionally **terse**: the real documentation lives in `docs/`. Read the doc that matches your task before exploring source.

---

## ⚠️ Target platform priority — Windows first

**Windows is the primary deployment target. Linux is second. macOS is last (dev convenience only).** This ordering is non-negotiable and overrides any instinct to optimise for the Mac you may be developing on.

Concretely, for every change that touches the OS — launching processes, file paths, service/daemon install, firewall, browser/VLC launch, priorities, signals:

1. **Make it correct on Windows first.** A feature that works on macOS but not Windows is a **bug**, not a partial success. Verify the Windows code path (exe discovery incl. per-user `%LOCALAPPDATA%` installs and the registry, `creationflags`, backslash paths, no reliance on POSIX-only APIs) before considering the task done.
2. **Then Linux** (systemd, `nice`, `/usr/bin` paths, `start_new_session`).
3. **macOS last.** Don't let a macOS-only convenience (or a macOS limitation like the TCC HLS block) shape the design in a way that weakens Windows.

When a capability can't be identical across all three, Windows wins. Note any platform gaps explicitly in the relevant `docs/` file and `docs/GOTCHAS.md`.

---

## ⚠️ Keeping documentation current — read this first

Keep the reference docs current as the code changes:

- **`docs/*.md`** — topic-specific reference docs. When you change behaviour the docs describe (an endpoint signature, a state field, the skip algorithm, the auth flow, etc.), update the relevant doc in the same patch. If you introduce a new gotcha, add it to `docs/GOTCHAS.md`.
- **`README.md`** — this is the **Windows-first setup guide** and must always function as one: a user should be able to go from a clean machine to a running dashboard by following it top to bottom. Whenever you change anything that affects install or first-run — `setup.py`/`run.py` steps, what's automated vs. manual, ports, dependencies, the `.env` keys, service install, VPN/Jackett requirements — **update `README.md` in the same patch** so it never drifts from reality. Keep its "what's automatic vs. what you do by hand" distinction accurate. Keep it user-facing (install/quickstart); deep reference belongs in `docs/`.

Default to editing existing docs. Only create a new `docs/<topic>.md` if a genuinely new subsystem appears that doesn't fit anywhere existing.

---

## Documentation index

Each entry is a short hook so future Claude instances can jump straight to the right file instead of grepping. **Don't explore the codebase before checking the relevant doc.**

| Doc | When to read it |
|-----|-----------------|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Always read this first if you're unfamiliar with the repo. Service topology, process model, code map, lifecycle, key invariants. |
| [docs/BACKEND.md](docs/BACKEND.md) | Working on `main.py`. Section map by line range, `AppState` field reference, background-task descriptions, pipeline flow, qBit/VLC client notes. |
| [docs/FRONTEND.md](docs/FRONTEND.md) | Working on `static/index.html` or `static/admin.html`. HTML section map, JS function list, SSE handlers, render functions, init flow. |
| [docs/API.md](docs/API.md) | Adding/modifying an endpoint, or building a new UI feature that calls one. Every route with method, path, request shape, notes. SSE event catalog. |
| [docs/LIBRARY_DATA.md](docs/LIBRARY_DATA.md) | Touching `library.json` schema (profiles, items, progress, skip_data, settings). Includes the migration logic. |
| [docs/SETUP.md](docs/SETUP.md) | Changing `setup.py` — venv, deps install, qBit ini, SSL cert, service registration. |
| [docs/RUNTIME.md](docs/RUNTIME.md) | Changing `run.py` — venv relaunch, service launchers, LAN/SSID detection, mDNS, firewall, dashboard launch (HTTP + HTTPS). |
| [docs/DAEMON_WATCHDOG.md](docs/DAEMON_WATCHDOG.md) | Working on `daemon.py` (system service install) or `watchdog.py` (crash supervisor + VPN-gated qBit). |
| `updater.py` (top-level) | Auto-updater. Async `git fetch / switch / reset` + non-interactive `setup.py` invoker + `service_is_installed()`. Triggers live in `main.py` (`updater_loop`, `/api/admin/updater/*`, `ENV_KEY_FEATURES`). See [docs/ADMIN.md § Updates](docs/ADMIN.md). |
| `episodes.py` (top-level) | Season/episode attribution. Folder-aware structural parse (`attribute_paths`/`parse_slot`) + TMDb-aware absolute-number resolution (`resolve_absolute`) + canonical `sort_key`. Pure and dependency-free. Wired in `main.py` via `parse_season_episode`, `build_file_list`, `_migrate_item`, `_reattribute_item_files`, `_settle_attribution`. See [docs/LIBRARY_DATA.md § Season/episode attribution](docs/LIBRARY_DATA.md). |
| `diagnostics.py` (top-level) | Runtime health instrumentation. Leaf module (stdlib + optional psutil, no `main` import). Wired in `main.py` (init, `diag_track_requests` middleware, `/healthz`, `InstrumentedLock` on `_lib_lock`, three lifespan tasks), `run.py` and `daemon.py` (`uvicorn_log_config`). See [docs/DIAGNOSTICS.md](docs/DIAGNOSTICS.md). |
| `dvprobe.py` (top-level) | Green-picture / Dolby Vision detection. Reads the `DOVIDecoderConfigurationRecord` out of a Matroska or MP4 header with stdlib only (no ffmpeg) and flags **Profile 5**, the one DV profile with no displayable base layer. Pure. Wired in `main.py` via `_probe_item_video`, `video_probe_backfill`, `green` on `/files`, `dv_risk` on `/api/search`. See [docs/GOTCHAS.md](docs/GOTCHAS.md). |
| `relquality.py` / `racerules.py` (top-level) | **Parallel download racing.** `relquality.py` estimates a release's real quality from its title (resolution / source / codec) cross-checked against file size vs TMDb runtime — name-first, and the size check only ever *demotes*. `racerules.py` holds the race's pure decision arithmetic (who leads, who to drop). Both leaf modules (stdlib only, no `main` import) with unit tests in `tests/`. The engine itself lives in `main.py` (`_race_start`, `_reconcile_item_race`, `_apply_race_upgrade`, `/api/stream/race`) inside `library_download_monitor`'s tick. See [docs/BACKEND.md § Parallel download racing](docs/BACKEND.md) and [docs/GOTCHAS.md](docs/GOTCHAS.md). |
| `stallrule.py` (top-level) | **When has a PART-download stopped for good?** The dead-swarm retry only fires at zero bytes; this is the other half. Idle time is **accrued** per tick (never measured from a timestamp, so restarts and VPN drops are not charged to the torrent), patience grows with the bytes that would be thrown away, and the verdict is never an error — only "start looking for another copy". Leaf module (stdlib only, no `main` import), tests in `tests/test_stallrule.py`; the engine it drives is exercised end to end in `tests/test_race_rescue.py`. Wired in `main.py` (`item["stall"]` in `library_download_monitor`, `_rescue_stalled_download`, `_race_start(rescue=True)`, the first-to-finish rule in `_reconcile_item_race`, `stalled` on `library_progress`). See [docs/BACKEND.md § Stalled part-downloads](docs/BACKEND.md) and [docs/GOTCHAS.md](docs/GOTCHAS.md). |
| `gpugate.py` (top-level) | **Is the all-GPU prep path worth trying on the next encode?** That path can wedge ffmpeg; a watchdog kills it and the episode re-encodes from zero on the transparent path, so a stall never fails a prep, it only doubles its time. The gate counts watchdog kills (two in four attempts closes it for eight encodes, then one probe). Per process, never persisted. Leaf module (stdlib only, no `main` import) with tests in `tests/test_gpugate.py`. Wired in `main.py` (`_GPU_GATE`, `GPU_STALL_TIMEOUT_SECS`, the stall report in `_run_offline_job`). The cause of the stall is unknown. See [docs/STREAMING.md](docs/STREAMING.md) and [docs/GOTCHAS.md](docs/GOTCHAS.md) § The all-GPU prep path stalls without a copy rung too. |
| `srcevict.py` (top-level) | **May this source file be deleted, now the bundle can play without it?** A prepped episode is on disk twice; the bundle is what every phone and browser plays, so the source is ~37% of the pair that only VLC (5.1/HDR/image subs), repair, re-prep and fingerprinting still need. The policy splits in two and the split is the whole design: **age** decides what is ELIGIBLE (per **series**, not per file and not per item — touching one episode protects the show), **free space** decides what is TAKEN. Missing evidence never reads as permission. Leaf module (stdlib only, no `main` import) with tests in `tests/test_srcevict.py`. Wired in `main.py` (`_src_evict_cfg`, `_series_clocks_sync`, `_evict_candidates_sync`, `_build_evict_plan`, `_evict_one_source` / `_run_source_eviction` / `source_eviction_loop`, `/api/admin/source-eviction*`, and the source-optional addressing block `_file_evicted` / `_bundle_dir_for_file` / `_assert_source_present`). See [docs/STREAMING.md § Source eviction](docs/STREAMING.md) and [docs/GOTCHAS.md](docs/GOTCHAS.md). |
| `watchpurge.py` (top-level) | **Delete Watched: which finished episodes may go.** Selected means `completed` for the chosen profile(s) (`match` all/any), never a position. A file is kept if ANY profile is part-way through it, it's in use or prepping, or it's compressed. Deletes source **and** bundle (unlike `srcevict`, which keeps the bundle). Leaf module (stdlib only, no `main` import) with tests in `tests/test_watchpurge.py`. Wired in `main.py` (`_watched_purge_rows_sync`, `_watched_purge_scope`, `/api/library/watched-purge[/preview]`, the shared delete core `_delete_files_now`) and in the UI (`openDeleteWatched` in `static/index.html`, the Storage-tab card in `static/admin.html`). See [docs/API.md](docs/API.md) and [docs/GOTCHAS.md](docs/GOTCHAS.md). |
| `devactivity.py` / `devstore.py` (top-level) | **Admin Devices tab**: who is connected, what they're doing, and every request they sent. `devactivity.py` turns raw requests into activities (`classify`, `extends`, `describe`), redacts secrets from query strings, labels User-Agents. `devstore.py` is the SQLite store (`logs/devices/activity.sqlite3`: devices forever, activities 365 d, raw requests 30 d / 1.5 M rows; a hashed-token → device map unused since pairing was removed in 19.8.0). Both leaf modules (stdlib only, no `main` import), tests in `tests/test_devactivity.py`. Wired in `main.py` (`_dev_capture` from `diag_track_requests`, which must stay I/O-free; `device_activity_loop` / `_dev_drain`; `_dev_connected` in the SSE handler; `_dev_watch_hint` in `/api/playback/session`; `/api/admin/devices*`), in the dashboard's fetch wrapper (`X-Device-Id`, `_devIdSync` cookie) and in `static/admin.html` (Devices tab). See [docs/DIAGNOSTICS.md § Devices](docs/DIAGNOSTICS.md) and [docs/GOTCHAS.md](docs/GOTCHAS.md). |
| `reaper.py` (top-level) | **What may be deleted after a torrent is removed.** qBit's delete-with-files leaves our sidecars, the `.streamlink_cache` folder, files Windows had locked, and `.parts` files. The reaper (`_reap_worker` / `_qbit_delete_reaped` / `pending_deletes` in `main.py`) finishes the job. This leaf module holds the guard (`may_reap`: inside a root, never a root, never owned), sidecar matching, `.parts` parsing and the retry backoff. Stdlib only, tests in `tests/test_reaper.py`. See [docs/GOTCHAS.md](docs/GOTCHAS.md) § qBit "delete with files" is not a delete. |
| `bundlecheck.py` (top-level) | **Is a long stretch of this prepped bundle dead?** A source with holes in it (qBit writes **sparse** files, so a half-fetched episode is full-length) makes ffmpeg duplicate the last frame and pad silence — and exit `0`. The result is a picture frozen over silence, keyed on the size the finished file will have, so it is never rebuilt. This answers the question from **segment sizes alone** — no ffmpeg, no decode — and only condemns a bundle where dead video and dead audio *overlap* (a credits roll is cheap to encode; a quiet passage is silent; neither kills both at once). Leaf module (stdlib only, no `main` import) with tests in `tests/test_bundlecheck.py`. Wired in `main.py` (`_scan_bundle_dir`, the pre-swap check in `_run_offline_job`, `_run_bundle_audit`, `/api/admin/bundle-audit`, `files[].bundle_check`). See [docs/STREAMING.md § Bundle integrity](docs/STREAMING.md) and [docs/GOTCHAS.md](docs/GOTCHAS.md). |
| `refiner.py` / `mediabin.py` (top-level) | Smart Skip boundary evidence. `refiner.py` is a leaf module holding every non-fingerprint detector — chapters, silence, subtitles, shot boundaries — plus the shared `snap_boundary`. `mediabin.py` holds ffmpeg/ffprobe/fpcalc discovery and `run_capture`. Both pure (stdlib + the media binaries); `analyzer` imports them, never the reverse. See [docs/ANALYZER.md](docs/ANALYZER.md). |
| `animemap.py` (top-level) | **Anime season grids.** TMDb files a long-running anime under a season split no release group uses (Hunter x Hunter 2011: TMDb 62/74/12, iAHD 58/78/12, Netflix six seasons numbered absolute, fansubs absolute). Holds the cached Anime-Lists `anime-list-full.xml`, the absolute↔TMDb arithmetic, and `remap_slots` — the **third** attribution pass, the one that can move a file whose `SxxExx` is authoritative and still wrong. Leaf module (stdlib only, no `main` import) with tests in `tests/test_animemap.py`. Wired in `main.py` (`_anime_map_refresh`, `_anime_entries`, `_anime_facts`, `_reattribute_item_files`, `/metadata/refresh`). See [docs/LIBRARY_DATA.md § Anime season mapping](docs/LIBRARY_DATA.md) and [docs/GOTCHAS.md](docs/GOTCHAS.md). |
| `reltracks.py` (top-level) | **Release language + track richness.** Reads a torrent title ONCE (`lang_facts`) and answers two questions off that one parse: which audio a release carries (`classify_audio` → `dual`/`dub`/`sub`/`other`/`""`, the filter) and how much choice it gives you (`track_rank` → 0-3, audio counted double, the tiebreak). Parsing them separately is how Erai-raws' `[Multiple Subtitle][ENG][POR-BR]` becomes "three audio tracks". Leaf module (stdlib only, no `main` import) with tests in `tests/test_reltracks.py`. Wired in `main.py` (`_parse_release_audio` delegate, `tracks` on `_group_search_results`) and consumed by `_pickCmp`/`_packCmp` in `static/index.html`. See [docs/GOTCHAS.md](docs/GOTCHAS.md) and [docs/API.md](docs/API.md). |
| `subsearch.py` / `subsync.py` (top-level) | **Subtitle search.** `subsearch.py` builds the (unforgiving) OpenSubtitles URLs, decides which results really are this episode, ranks them by what predicts a subtitle that fits *this file*, and reads/re-times subtitle files in their own format. `subsync.py` aligns a subtitle to the episode's speech via ffmpeg + FFT cross-correlation and says how confident it is — the automatic fetch keeps nothing the audio hasn't verified. Both leaf modules (`subsync` needs numpy, optional) with tests in `tests/test_subsearch.py`; every weight was measured with the eval kit in `tests/subs_eval/` (52 library episodes graded against their own embedded tracks — read its README before changing a rule). Wired in `main.py` (`_subtitle_target`, `_subtitle_search`, `_fetch_subtitle`, `_auto_subtitle_fetch`, `/api/subtitles/*`, `/api/library/{id}/subtitles/*`). See [docs/API.md](docs/API.md) and [docs/GOTCHAS.md](docs/GOTCHAS.md). |
| `watchrule.py` (top-level) | **What counts as "watched".** An episode completes when the playhead reached its tail **and** enough of it was genuinely *played* — `played_sec`, accrued per progress write as the position advance capped by the wall clock since the last write, so a scrub to the end can't credit it. Pure (stdlib only, no `main` import), tests in `tests/test_watchrule.py`. Every position writer in `main.py` goes through `_watch_state` / `_offline_watch_state` — never compute `completed` inline. **The iOS `OfflineStore.swift` carries a copy of its constants** (it measures offline play on the device) — change one, change both. See [docs/LIBRARY_DATA.md § What counts as watched](docs/LIBRARY_DATA.md) and [docs/GOTCHAS.md § A position is not evidence](docs/GOTCHAS.md). |
| `clientlog.py` (top-level) | **Client diagnostic transcripts.** The phone uploads its WHOLE log every time; this merges it into an append-only per-device transcript, taking only rows never seen (identity = the hashed line, not `t` — two rows can share a millisecond, and a device that clears its log sends older timestamps that must still land). Also the summary/filter the admin read endpoints serve. Leaf module (stdlib only, no `main` import) with tests in `tests/test_clientlog.py`. Wired in `main.py` (`CLIENT_LOG_DIR`, `_migrate_client_logs`, `/api/diag/client-log`, `/api/admin/client-logs*`). **Client logs live in `logs/client/` and NOTHING deletes them but `DELETE /api/admin/client-logs`** — the subdirectory is what hides them from every top-level sweep. See [docs/DIAGNOSTICS.md § Client logs](docs/DIAGNOSTICS.md) and [docs/GOTCHAS.md](docs/GOTCHAS.md). |
| `eplabel.py` (top-level) | **What a file is CALLED.** One label for every surface: `line1` "Breaking Bad · S01E03", `line2` the episode's name, `short` the one-line form. The file name is the label only when neither number nor name is known. TMDb show/episode names first, a name parsed from the file second; specials, `OVA`/`OAD` buckets, two-episode files, anime absolute numbers, spin-off sections and films each have a rule. Leaf module (stdlib + `episodes`, no `main` import), tests in `tests/test_eplabel.py`. Wired in `main.py` (`_file_label` / `_LABEL_MEMO`, `label` on `/files` + `/series`, `label` in bundle meta, `library_current_label` / `tv_local_label` in the state, `/api/admin/file-labels`) and mirrored in `static/index.html` (`fileLabel`, `_composeLabel`, `labelNowPlaying`, `labelInShow`). **Never render `f.name` to a person** — see [docs/GOTCHAS.md](docs/GOTCHAS.md) § A file's name is not what it is called. |
| `epgroups.py` (top-level) | **TMDb episode groups** (story arcs, DVD order, production order). Two jobs: the episode page's **View** picker (`summarize`/`normalize` — a group is a VIEW, every entry a real TMDb `(season, episode)`, files never renumbered), and **attribution pass 4** (`season_homes`/`place_files`) — specials the whole-season groups agree belong INSIDE a season get `home: {season, after}`, and a file stuck at `(season, 0)` (AoT's `Season 4 - Finale 1`) is placed onto its special (S00E36). Offline over the TMDb disk cache; `episodes.sort_key` honours `home`. Leaf module (stdlib + `episodes`), tests in `tests/test_epgroups.py` over real TMDb data. Wired in `main.py` (`_ep_groups_fetch`, `_ep_group_homes`, `_reattribute_item_files`, `_settle_attribution`, `/api/tmdb/tv/{id}/episode-groups`, `/api/tmdb/episode-group/{id}`, `/api/profiles/{id}/episode-view`). See [docs/LIBRARY_DATA.md § Season/episode attribution](docs/LIBRARY_DATA.md) and [docs/GOTCHAS.md](docs/GOTCHAS.md). |
| `titleslot.py` (top-level) | **Which episode a release IS, by the name it states** (20.14.0). A release's number is its group's idea of the show: SpongeBob's "S02E09 Dying for Pie" is TMDb's E10, and a half-hour file holds two TMDb episodes. `slot` reads the episode name(s) after the `SxxExx` marker against the season's TMDb names (whole phrase, a word of 4+ letters, and a move only when the title shares no word with its own number's name); `place_files` is attribution **pass 5** and writes `episode`, `also`, `ts_from`; `cross` (20.15.0) lets a file numbered in another season, or as a special, be filed as the episode its item was fetched for; `roam` (20.16.0) does the same for a pack file that is not at home in its season (Futurama's Blu-ray "S01E10" is TMDb's S02E01), and `ranged` makes "S01E04-E05" hold both; `held` is what every "which episodes do we hold" question counts. Derived from the file name each time, never remembered. Leaf module (stdlib only, no `main` import), tests in `tests/test_titleslot.py` over real TMDb names and real indexer titles. Wired in `main.py` (`_reattribute_item_files`, `_title_slots_pending`, `_roam_seasons_wanted`, `/api/library/coverage`, `_pack_slice_apply`, `_pack_available`, `pack-fetch`, `_retry_candidates`, `also` on `/files`), in `eplabel.py` (the `S02E14-E15` code) and **mirrored in `static/index.html` (`_titleSlot`, `_tsRanged`, `_reslot`, `_srcHolds`, `_bgPlanJobs`) - change one, change both.** See [docs/LIBRARY_DATA.md § Season/episode attribution](docs/LIBRARY_DATA.md) and [docs/GOTCHAS.md](docs/GOTCHAS.md) § A release's number is not its episode. |
| `discovery.py` (top-level) | **Being found by the iOS app** (19.14.0): the app lists servers, nobody types an address. This is the host's half: a stable id (`.server_id`), a name (hostname), and every address the box answers at, with **Tailscale picked out by subnet** (`100.64/10`), not by adapter name. Leaf module (stdlib + optional psutil, no `main` import), tests in `tests/test_discovery.py`. Wired in `main.py` (`GET /api/discovery`) and `run.py` (`start_mdns`, the `_streamlink._tcp` Bonjour service). The phone's half is `ios-app/ios/App/App/ServerDiscovery.swift` (Bonjour + Wi-Fi sweep + a sweep of the subnets a VPN routes, read from the kernel routing table) and the Connect screen in `ios-app/www/index.html`. See [docs/RUNTIME.md § Discovery](docs/RUNTIME.md), [docs/STREAMING.md](docs/STREAMING.md) and [docs/GOTCHAS.md](docs/GOTCHAS.md) § The app cannot list a tailnet. |
| `appchannel.py` / `promote.py` (top-level) | **Release channels** (20.1.0): the iOS app follows the server's branch, so a phone is never offered an app newer than its server. `appchannel.py` maps a branch to its SideStore source file (`apps.json` / `apps-beta.json` / `apps-alpha.json`), picks the newest app not newer than the server (`pick`), and finds what the changelog admits was never checked (`unverified_claims`). Leaf module (stdlib only, no `main` import), tests in `tests/test_appchannel.py`. `promote.py` is the one way a build reaches `beta` or `main`: tests at the commit, the `release/<channel>/<version>` tag that records what was checked, the branch, then the app. Its `candidates` and `logs --box` commands gather the evidence for choosing a build (`changelog_entries`, `log_health`, `new_signatures`); the choosing is `.claude/skills/release-candidate/SKILL.md`. Wired in `main.py` (`_app_channel`, `_app_latest_fetch`, `/api/app/latest`), `ios-app/publish-ipa.sh` (`--channel`, `--promote`) and the Connect screen's `MIN_SERVER` notice in `ios-app/www/index.html`. See [docs/GOTCHAS.md § Release channels](docs/GOTCHAS.md). |
| `voicestatus.py` / `voicepick.py` (top-level) | **Siri: asking about the library and downloading a film by voice** (20.4.0 - 20.6.0). `voicestatus.py`: which library title a spoken name means (`rank`) and every sentence handed to Siri (`describe`, `overview`, `confirm_line`, `refusal`, …), which only claim what their facts carry. `voicepick.py`: which FILM spoken words mean (`parse_request`, `choose_title`: "the original one", a year, and TMDb vote counts so an obscure namesake never wins) and which RELEASE to start (`candidates`, `shortlist`) - **a port of the dashboard's one-press Get (`grpGetFilm` / `_pickCmp` / `_ssAutoPickRace`); change one, change both.** Both leaf modules (stdlib only, no `main` import), tests in `tests/test_voicestatus.py` and `tests/test_voicepick.py`. Wired in `main.py` (`_voice_groups`, `_voice_facts`, `_DL_PROGRESS_LAST`, `_voice_find`, `_voice_download_film`, `_VOICE_JOBS`, `/api/voice/status`, `/api/voice/find`, `/api/voice/download`; the download goes through `library_download` itself) and in the app (`ios-app/ios/App/App/SiriIntents.swift`: `DownloadsOverviewIntent`, `DownloadStatusIntent`, `DownloadFilmIntent`, `StreamLinkShortcuts`; host address from `MainViewController.watchHostURL`). Siri routes on an intent's title + description and only fills plain `String` parameters. See [docs/API.md](docs/API.md) and [docs/GOTCHAS.md](docs/GOTCHAS.md) § A Siri intent has no web view. |
| `tmdbcache.py` (top-level) | On-disk cache of TMDb API responses (`.tmdb_cache/`) under `_tmdb_get`: per-kind TTLs (`ttl_for`), stale fallback when TMDb is unreachable, API key never stored. **Also the six-month retention rule** TMDb's terms impose on everything cached from it (`MAX_AGE`, `age_state`, `metadata_state`, `expire_metadata`), applied by `tmdb_retention_loop` in `main.py` to the response cache, the artwork cache and `item["metadata"]`. Leaf module (stdlib only, no `main` import) with tests in `tests/test_tmdbcache.py` and `tests/test_tmdb_retention.py`. **Never add a store of TMDb data without an age, and never remove the credits** (`static/vendor/tmdb-logo.svg` + the notice). See [docs/LIBRARY_DATA.md § TMDb retention](docs/LIBRARY_DATA.md) and [docs/GOTCHAS.md](docs/GOTCHAS.md) § TMDb data has a shelf life. |
| `bookmarks.py` (top-level) | **Explore bookmarks** (per-profile watch-later). Release status from TMDb (`movie_status`: digital-release countdown for a film in theaters; `tv_status`: premiere / next season) and the NEW-flag transition (`advance`: only an *awaited* release raises it; `None` evidence changes nothing). Leaf module (stdlib only, no `main` import) with tests in `tests/test_bookmarks.py`. Wired in `main.py` (`_bookmark_status`, `_refresh_bookmarks`, `bookmark_release_loop`, `/api/profiles/{id}/bookmarks*`, SSE `bookmarks_update`) and `static/index.html` (`loadBookmarks`, `toggleBookmark`, `_bmBadge`, `exOpenBookmarks`, `#ssBookmarkBtn`). See [docs/LIBRARY_DATA.md § Bookmarks](docs/LIBRARY_DATA.md). |
| `subpack.py` (top-level) | Timed subtitle **image packs** — styled ASS and bitmap PGS/VOBSUB pre-rendered to transparent PNGs + a timing manifest, so clients that can't run libass (native iOS `AVPlayer`) or can't decode image subs at all can still show them. Leaf module (stdlib + `mediabin`, no `main` import). Purely additive to a bundle — no re-prep, no cache-version bump. Wired in `main.py` (`/api/library/offline-cache/<key>/subpack/*`). See [docs/STREAMING.md § 2c](docs/STREAMING.md). |
| [docs/ANALYZER.md](docs/ANALYZER.md) | Touching Smart Skip — `analyzer.py`, the orchestrator in `main.py`, skip-offer UI logic, or the admin editor. Algorithm + thresholds + fallback chain. |
| [docs/ADMIN.md](docs/ADMIN.md) | Working on `/admin` panel — auth flow, HTTPS redirect, Jackett admin auth, the four tabs, content-lock semantics. |
| [docs/STREAMING.md](docs/STREAMING.md) | Working on Stream-to-Device — `/offline-prepare`, `.offline_cache/`, per-row Prep buttons, the local `<video>` player, progress sync, **and iOS background / external-display playback (§ 2b)**. (Successor to the old `OFFLINE.md`.) |
| `ios-app/ios/App/App/NativePlayback.swift` | iOS background + external-display playback. A native `AVPlayer` relief pitcher for the WKWebView player: WebKit pauses a video-bearing `<video>` on background, so locking the phone would otherwise stop playback. JS pre-arms the plugin continuously and the **native** side performs the handoff from `didEnterBackgroundNotification`. Owns the audio session, Now Playing / remote commands, native progress POSTing (JS timers are frozen while backgrounded), external-display detection, **AirPlay** (`airplay()`; the receiver fetches the stream itself, through the token-gated Wi-Fi `AirPlayDoor` in `LocalMediaServer.swift`, 18.26.0), and `setAwake` (holds the idle timer open while the phone itself is presenting, so mirroring is not killed by an auto-lock — all that remains of **TV Mode**, removed in 18.13.2). Live Activity in `PlaybackLiveActivity.swift` + `StreamLinkLiveActivities/PlaybackWidget.swift`; its buttons reach the player through the `PlaybackCommandBus` seam in `Shared/PlaybackIntents.swift`. See [docs/STREAMING.md § 2b](docs/STREAMING.md) and [docs/GOTCHAS.md](docs/GOTCHAS.md). |
| `ios-app/ios/App/App/CastSession.swift` | **Chromecast / Google TV casting** (18.27.0 spike). Cast v2 spoken directly: no Google SDK. Bonjour discovery (`CastDiscovery`), the TLS + hand-rolled protobuf session that launches the Default Media Receiver (`CastSession`), and `SilentKeepAlive`, which holds the process up while the TV fetches through the phone's `AirPlayDoor`. A second transport inside `NativePlaybackManager` (`startCast`, `castStatus`, the `cast` branches in `setPaused`/`seekTo`/`replaceItem`). See [docs/STREAMING.md § 2b Chromecast](docs/STREAMING.md) and [docs/GOTCHAS.md](docs/GOTCHAS.md). |
| `ios-app/ios/App/App/AppShell.swift` | **Haptics, home-screen quick actions, orientation lock** (19.5.0). `Haptics.fire` for the page's `_hap(kind)`, and `downloadFinished` from `BundleDownloader.markComplete`. `QuickActions` holds a long-press action (Info.plist `UIApplicationShortcutItems`, delivered by `SceneDelegate`) until the host page takes it (`_appInitQuickActions` in `static/index.html`). See [docs/FRONTEND.md](docs/FRONTEND.md) and [docs/GOTCHAS.md](docs/GOTCHAS.md). `OrientationLock` (20.3.0) is the player's lock button in the app: it turns the real interface and pins one landscape side, through `MainViewController.supportedInterfaceOrientations` (`_lpOrientApply` in `static/index.html`). |
| [docs/STT.md](docs/STT.md) | **Retired in 17.0.0** (hidden, off by default, whisper no longer installed; code intact). Working on AI auto-subtitles — `stt.py` (whisper.cpp), the `_needs_stt_subs` trigger, STT jobs, `/api/.../generate-subtitles`, the Generate-with-AI UI, whisper bundling in `setup.py`. |
| [docs/YOUTUBE.md](docs/YOUTUBE.md) | Working on YouTube-on-TV — `/api/youtube*`, the Chrome kiosk + `static/tv.html` IFrame player, the `yt_command` SSE relay, the dashboard control routing (`app.youtube_active`). |
| [docs/REMOTE.md](docs/REMOTE.md) | Working on HID wireless remote (air-mouse) support or the Firestick-style TV UI — `remote_input.py` (pynput input hooks), `_remote_key_action` / `_remote_should_handle` / the `tv_ui_*` block in `main.py`, `?tv=1` in `index.html`, button map (incl. 🏠 Home), Windows key suppression, screen arbitration. |
| [docs/DIAGNOSTICS.md](docs/DIAGNOSTICS.md) | **Read when the server is unreachable, slow, or "crashed" with no traceback.** `diagnostics.py` — access/vitals logging, the in-process self-probe (`/healthz`), event-loop lag, thread-pool + library-lock instrumentation, stall stack dumps. Includes how to read an incident. |
| [docs/ALT_VERSIONS_PLAN.md](docs/ALT_VERSIONS_PLAN.md) | **Deferred design, not built.** Alternate versions of an episode/pack (competing dubs, richer audio/subs): per-profile default, player "Version" row, verify-before-delete replace, per-slot collapse. Read before starting that work. |
| [docs/EXTERNAL_SERVICES.md](docs/EXTERNAL_SERVICES.md) | **Read when a provider changes its price, terms or limits, or before adding an outside dependency.** Every service we lean on (TMDb, OpenSubtitles, Jackett's indexers, GitHub, Anime-Lists, YouTube, SideStore, Tailscale), what breaks without it, what already survives on disk, and the backup plan. A planning doc: nothing under "Backup plan" is built. |
| [docs/GOTCHAS.md](docs/GOTCHAS.md) | **Read before any non-trivial change.** VLC ES-ID quirks, qBit sequential-download traps, VPN dual-enforcement, Jackett `Category[]=0`, canonical path matching, etc. |

---

## Quick commands

```bash
python3 setup.py          # first-time configuration (or re-run to refresh)
python3 run.py            # launch all services + dashboard
make setup / make run     # shortcuts

make test                 # pure unit tests for the leaf modules (no deps, no venv,
                          # no running services). On Windows, where `make` and
                          # `python3` usually aren't on PATH, run them directly:
                          #   python tests/test_relquality.py
                          #   python tests/test_race_rules.py
                          #   python tests/test_tmdbcache.py
                          #   python tests/test_tmdb_retention.py
                          #   python tests/test_animemap.py
                          #   python tests/test_http_clients.py
                          #   python tests/test_reltracks.py
                          #   python tests/test_watchrule.py
                          #   python tests/test_bundlecheck.py
                          #   python tests/test_srcevict.py
                          #   python tests/test_packslice.py
                          #   python tests/test_stallrule.py
                          #   python tests/test_gpugate.py
                          #   python tests/test_race_rescue.py
                          #   python tests/test_reaper.py
                          #   python tests/test_epgroups.py
                          #   python tests/test_eplabel.py
                          #   python tests/test_titleslot.py
                          #   python tests/test_retry_candidates.py
                          #   python tests/test_watchpurge.py
                          #   python tests/test_delete_locked.py
                          #   python tests/test_storage_volumes.py
                          #   python tests/test_devactivity.py
                          #   python tests/test_bookmarks.py
                          #   python tests/test_diagnostics.py
                          #   python tests/test_analyzer_fp.py
                          #   python tests/test_discovery.py
                          #   python tests/test_appchannel.py
                          #   python tests/test_voicestatus.py
                          #   python tests/test_voicepick.py
                          # tests/search_eval/ is the LIVE search-accuracy kit
                          # (69 hand-labelled shows) - run verify.py against a
                          # box before trusting a change to episode matching.
                          # tests/race_harness.py is the LIVE integration driver
                          # for download racing - needs a running StreamLink +
                          # qBittorrent and its own magnets file. See its docstring.

python3 promote.py        # where main / beta / alpha stand (server + app)
python3 promote.py candidates   # every build since main: changes, later fixes, unchecked
python3 promote.py logs --box https://<box>   # what the box's logs say per version
python3 promote.py main   # the plan for marking alpha's newest build safe for main
                          # (prints checks, changes nothing; --verified "..." --go acts)

python3 run.py --install  # register as a system service (delegates to daemon.py)
python3 run.py --status   # service status
```

Both `setup.py` and `run.py` must be invoked with the **system** Python — they use `from __future__ import annotations` for 3.9 compatibility. `run.py` `os.execv`s itself into `.venv/bin/python` so the rest of execution runs in the venv.

---

## Versioning — mandatory on every change

The version badge in `static/index.html` (bottom-right corner `<div>`) and the entry in `CHANGELOG.md` **must** be updated in the same patch as any code change.

Scheme: **x.y.z**

| Part | When to bump | Examples |
|------|--------------|---------|
| `x`  | Major feature — new top-level capability, architectural overhaul | new streaming mode, new admin tab |
| `y`  | Minor feature — new user-visible behaviour within an existing subsystem | hold-to-large-step vol, new skip threshold option |
| `z`  | Bug fix — correcting wrong behaviour, no new capability | off-by-one in seek, crash fix |

Current version lives in the `<div>` at the very bottom of `static/index.html`. After bumping, add a bullet to `CHANGELOG.md` under the new version heading.

### Side branches take their number at merge, not before

The rule above is for work that lands on `alpha`. A branch that will sit for a while
before it merges (`graphicalsetup` is the example) can't know which number it will ship
as, and a number taken early collides with whatever alpha ships in the meantime.

- **On the branch:** write the changelog entry under `## [Unreleased]` and do **not**
  take the next version. Leave the badge at what alpha had when the branch was cut,
  with a suffix naming the branch (`20.8.13-gsetup`) so a test install is recognisable.
  Commit titles on the branch carry no version number either.
- **At merge into `alpha`:** bring alpha in first, then rename `[Unreleased]` to the
  next free version with the merge date, move it to the top of the changelog, and set
  the badge. That is the only moment the number is assigned.
- **A number that has been pushed anywhere is spent.** Never reuse it, even if the
  branch that took it never shipped. Gaps in the sequence are fine; two different
  builds with one name are not. (20.9.0 is such a hole: it was taken on
  `graphicalsetup` before this rule, so alpha went from 20.8.13 to 20.10.0.)
- Before picking a version on alpha, check no other branch already used it:
  `git log --all --oneline | grep " <version> "`.

---

## iOS app changes — ALWAYS flag the rebuild (mandatory)

The app is built on a **Mac**, from `ios-app/build-ipa.sh`. That script is the whole
path: `npm install` → re-vendor `www/capacitor.js` from `@capacitor/core` →
`npx cap sync ios` → `xcodebuild` → an **unsigned** `.ipa` the user re-signs with a
sideloader (Sideloadly / AltStore / ESign). Building straight from Xcode also works and
installs directly to the device, but it does **not** run `cap sync`, so it alone can
ship stale web assets.

**Do not run `npx cap sync ios` yourself to "fix" this.** `ios-app/ios/App/App/public/`
is a build artifact and is **gitignored** (see `ios-app/.gitignore`) — nothing you
generate on Windows reaches the Mac. The Mac regenerates it from `ios-app/www/` on its
own `cap sync`. The source of truth is `ios-app/www/`; sync locally only if you need to
verify the copy step, never as a delivery mechanism.

So after any change under `ios-app/`, work out which invocation is safe and **end your
summary with an unmissable callout saying so**:

| What changed | Callout |
|---|---|
| Swift only | **📱 App rebuild needed:** Swift changed — `./build-ipa.sh --fast` is enough (`--fast` skips `npm install` + `cap sync`, neither of which has anything to do). |
| `ios-app/www/*` | **📱 App rebuild needed:** web assets changed — run the **plain** `./build-ipa.sh`. Do **not** use `--fast` or `--no-sync`; they skip the `cap sync` that copies `www/` in, and the build silently ships the old assets. |
| `package.json` / plugins | **📱 App rebuild needed:** dependencies changed — plain `./build-ipa.sh` (needs the `npm install`). |
| Host-side only (`static/`, `main.py`, …) | **📱 No rebuild** — the dashboard is served by the box; deploy it there instead. |

Never let an `ios-app/` change ship without this callout — a stale `public/` looks like
"my change didn't work" and has bitten three times (see docs/GOTCHAS.md § stale builds).

**The app's version is the dashboard badge, but that proves nothing about a change.**
Since 19.4.0 `build-ipa.sh` stamps the badge into `CFBundleShortVersionString`
(an Xcode build still says `1.0`). The badge also moves for host-only changes, so the
closing `Version:` line doesn't show whether a change compiled in. Confirm an app
change landed by its behaviour on-device instead.

**Publishing to SideStore:** `ios-app/publish-ipa.sh` builds, uploads a GitHub Release
to the public `nmautz/streamlink-ios` repo, and adds it to the source file of the
**channel of the branch it was built on** (`apps-alpha.json` from `alpha`). A version
can be built once, so bump the badge first. See docs/GOTCHAS.md § SideStore source.

## Release channels — work lands on `alpha`; `main` only by promotion

`main` is what other people run. When asked what is ready for `main`, to find a
release candidate, or to promote, follow `.claude/skills/release-candidate/SKILL.md`:
you gather the evidence and recommend, the user has the final say.
Never push to `main` or `beta` directly, and never
publish an app build straight to their sources. A build gets there with
`python3 promote.py <channel>`, which checks it, tags it `release/<channel>/<version>`
with what was verified, moves the branch, and moves the matching app, in that order.
Run it without `--go` first and read the "NOT checked" list it prints: `--verified`
has to say what was actually checked on a box and on a phone, in the user's words or
from evidence in this session, not a guess. Pushing a branch and publishing an app
source are outward-facing: confirm with the user before `--go`.

If a change makes the app stop working with older servers, raise `MIN_SERVER` in
`ios-app/www/index.html` in the same patch. See docs/GOTCHAS.md § Release channels.

## Style conventions

- **Metro UI** throughout the frontend — flat tiles, no rounded corners, bold uppercase typography, square status dots, no `backdrop-blur`. See [docs/FRONTEND.md](docs/FRONTEND.md).
- **No emoji/dingbat glyphs in the UI** — they render differently on every OS. Use the inline SVG icon sprite (`<use href="#i-NAME">` in HTML, `ic("NAME")` in JS templates). See [docs/FRONTEND.md § Iconography](docs/FRONTEND.md).
- Backend uses `asyncio` everywhere — never `time.sleep` inside a request handler or background task. Use `await asyncio.sleep(...)`.
- Library access goes through `get_library()` / `put_library()` (both hold `_lib_lock`) — never read/write `library.json` raw outside that lock.
- VLC track IDs are **ES IDs** from the `"Stream N"` keys, not 1/2/3 counters. See [docs/GOTCHAS.md](docs/GOTCHAS.md).

---

## Working memory: where to put what

- **In-flight task plan / todos** → ephemeral, not persisted (use TodoWrite during work).
- **Reference docs about how the system works** → `docs/*.md`. Update alongside code changes.
- **Non-obvious behaviours / footguns discovered during work** → `docs/GOTCHAS.md`.
- **README.md** → the user-facing, **Windows-first setup guide**; keep it accurate and runnable top-to-bottom (see "Keeping documentation current" above). Don't put architecture details here; link to `docs/` if needed.

If something doesn't fit any of the above, ask before creating a new file at the repo root.
