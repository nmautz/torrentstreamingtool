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
| `srcevict.py` (top-level) | **May this source file be deleted, now the bundle can play without it?** A prepped episode is on disk twice; the bundle is what every phone and browser plays, so the source is ~37% of the pair that only VLC (5.1/HDR/image subs), repair, re-prep and fingerprinting still need. The policy splits in two and the split is the whole design: **age** decides what is ELIGIBLE (per **series**, not per file and not per item — touching one episode protects the show), **free space** decides what is TAKEN. Missing evidence never reads as permission. Leaf module (stdlib only, no `main` import) with tests in `tests/test_srcevict.py`. Wired in `main.py` (`_src_evict_cfg`, `_series_clocks_sync`, `_evict_candidates_sync`, `_build_evict_plan`, `_evict_one_source` / `_run_source_eviction` / `source_eviction_loop`, `/api/admin/source-eviction*`, and the source-optional addressing block `_file_evicted` / `_bundle_dir_for_file` / `_assert_source_present`). See [docs/STREAMING.md § Source eviction](docs/STREAMING.md) and [docs/GOTCHAS.md](docs/GOTCHAS.md). |
| `bundlecheck.py` (top-level) | **Is a long stretch of this prepped bundle dead?** A source with holes in it (qBit writes **sparse** files, so a half-fetched episode is full-length) makes ffmpeg duplicate the last frame and pad silence — and exit `0`. The result is a picture frozen over silence, keyed on the size the finished file will have, so it is never rebuilt. This answers the question from **segment sizes alone** — no ffmpeg, no decode — and only condemns a bundle where dead video and dead audio *overlap* (a credits roll is cheap to encode; a quiet passage is silent; neither kills both at once). Leaf module (stdlib only, no `main` import) with tests in `tests/test_bundlecheck.py`. Wired in `main.py` (`_scan_bundle_dir`, the pre-swap check in `_run_offline_job`, `_run_bundle_audit`, `/api/admin/bundle-audit`, `files[].bundle_check`). See [docs/STREAMING.md § Bundle integrity](docs/STREAMING.md) and [docs/GOTCHAS.md](docs/GOTCHAS.md). |
| `refiner.py` / `mediabin.py` (top-level) | Smart Skip boundary evidence. `refiner.py` is a leaf module holding every non-fingerprint detector — chapters, silence, subtitles, shot boundaries — plus the shared `snap_boundary`. `mediabin.py` holds ffmpeg/ffprobe/fpcalc discovery and `run_capture`. Both pure (stdlib + the media binaries); `analyzer` imports them, never the reverse. See [docs/ANALYZER.md](docs/ANALYZER.md). |
| `animemap.py` (top-level) | **Anime season grids.** TMDb files a long-running anime under a season split no release group uses (Hunter x Hunter 2011: TMDb 62/74/12, iAHD 58/78/12, Netflix six seasons numbered absolute, fansubs absolute). Holds the cached Anime-Lists `anime-list-full.xml`, the absolute↔TMDb arithmetic, and `remap_slots` — the **third** attribution pass, the one that can move a file whose `SxxExx` is authoritative and still wrong. Leaf module (stdlib only, no `main` import) with tests in `tests/test_animemap.py`. Wired in `main.py` (`_anime_map_refresh`, `_anime_entries`, `_anime_facts`, `_reattribute_item_files`, `/metadata/refresh`). See [docs/LIBRARY_DATA.md § Anime season mapping](docs/LIBRARY_DATA.md) and [docs/GOTCHAS.md](docs/GOTCHAS.md). |
| `reltracks.py` (top-level) | **Release language + track richness.** Reads a torrent title ONCE (`lang_facts`) and answers two questions off that one parse: which audio a release carries (`classify_audio` → `dual`/`dub`/`sub`/`other`/`""`, the filter) and how much choice it gives you (`track_rank` → 0-3, audio counted double, the tiebreak). Parsing them separately is how Erai-raws' `[Multiple Subtitle][ENG][POR-BR]` becomes "three audio tracks". Leaf module (stdlib only, no `main` import) with tests in `tests/test_reltracks.py`. Wired in `main.py` (`_parse_release_audio` delegate, `tracks` on `_group_search_results`) and consumed by `_pickCmp`/`_packCmp` in `static/index.html`. See [docs/GOTCHAS.md](docs/GOTCHAS.md) and [docs/API.md](docs/API.md). |
| `subsearch.py` / `subsync.py` (top-level) | **Subtitle search.** `subsearch.py` builds the (unforgiving) OpenSubtitles URLs, decides which results really are this episode, ranks them by what predicts a subtitle that fits *this file*, and reads/re-times subtitle files in their own format. `subsync.py` aligns a subtitle to the episode's speech via ffmpeg + FFT cross-correlation and says how confident it is — the automatic fetch keeps nothing the audio hasn't verified. Both leaf modules (`subsync` needs numpy, optional) with tests in `tests/test_subsearch.py`; every weight was measured with the eval kit in `tests/subs_eval/` (52 library episodes graded against their own embedded tracks — read its README before changing a rule). Wired in `main.py` (`_subtitle_target`, `_subtitle_search`, `_fetch_subtitle`, `_auto_subtitle_fetch`, `/api/subtitles/*`, `/api/library/{id}/subtitles/*`). See [docs/API.md](docs/API.md) and [docs/GOTCHAS.md](docs/GOTCHAS.md). |
| `watchrule.py` (top-level) | **What counts as "watched".** An episode completes when the playhead reached its tail **and** enough of it was genuinely *played* — `played_sec`, accrued per progress write as the position advance capped by the wall clock since the last write, so a scrub to the end can't credit it. Pure (stdlib only, no `main` import), tests in `tests/test_watchrule.py`. Every position writer in `main.py` goes through `_watch_state` / `_offline_watch_state` — never compute `completed` inline. **The iOS `OfflineStore.swift` carries a copy of its constants** (it measures offline play on the device) — change one, change both. See [docs/LIBRARY_DATA.md § What counts as watched](docs/LIBRARY_DATA.md) and [docs/GOTCHAS.md § A position is not evidence](docs/GOTCHAS.md). |
| `clientlog.py` (top-level) | **Client diagnostic transcripts.** The phone uploads its WHOLE log every time; this merges it into an append-only per-device transcript, taking only rows never seen (identity = the hashed line, not `t` — two rows can share a millisecond, and a device that clears its log sends older timestamps that must still land). Also the summary/filter the admin read endpoints serve. Leaf module (stdlib only, no `main` import) with tests in `tests/test_clientlog.py`. Wired in `main.py` (`CLIENT_LOG_DIR`, `_migrate_client_logs`, `/api/diag/client-log`, `/api/admin/client-logs*`). **Client logs live in `logs/client/` and NOTHING deletes them but `DELETE /api/admin/client-logs`** — the subdirectory is what hides them from every top-level sweep. See [docs/DIAGNOSTICS.md § Client logs](docs/DIAGNOSTICS.md) and [docs/GOTCHAS.md](docs/GOTCHAS.md). |
| `tmdbcache.py` (top-level) | On-disk cache of TMDb API responses (`.tmdb_cache/`) under `_tmdb_get`: per-kind TTLs (`ttl_for`), stale fallback when TMDb is unreachable, API key never stored. Leaf module (stdlib only, no `main` import) with tests in `tests/test_tmdbcache.py`. See [docs/LIBRARY_DATA.md § TMDb response cache](docs/LIBRARY_DATA.md). |
| `subpack.py` (top-level) | Timed subtitle **image packs** — styled ASS and bitmap PGS/VOBSUB pre-rendered to transparent PNGs + a timing manifest, so clients that can't run libass (native iOS `AVPlayer`) or can't decode image subs at all can still show them. Leaf module (stdlib + `mediabin`, no `main` import). Purely additive to a bundle — no re-prep, no cache-version bump. Wired in `main.py` (`/api/library/offline-cache/<key>/subpack/*`). See [docs/STREAMING.md § 2c](docs/STREAMING.md). |
| [docs/ANALYZER.md](docs/ANALYZER.md) | Touching Smart Skip — `analyzer.py`, the orchestrator in `main.py`, skip-offer UI logic, or the admin editor. Algorithm + thresholds + fallback chain. |
| [docs/ADMIN.md](docs/ADMIN.md) | Working on `/admin` panel — auth flow, HTTPS redirect, Jackett admin auth, the four tabs, content-lock semantics. |
| [docs/STREAMING.md](docs/STREAMING.md) | Working on Stream-to-Device — `/offline-prepare`, `.offline_cache/`, per-row Prep buttons, the local `<video>` player, progress sync, **and iOS background / external-display playback (§ 2b)**. (Successor to the old `OFFLINE.md`.) |
| `ios-app/ios/App/App/NativePlayback.swift` | iOS background + external-display playback. A native `AVPlayer` relief pitcher for the WKWebView player: WebKit pauses a video-bearing `<video>` on background, so locking the phone would otherwise stop playback. JS pre-arms the plugin continuously and the **native** side performs the handoff from `didEnterBackgroundNotification`. Owns the audio session, Now Playing / remote commands, native progress POSTing (JS timers are frozen while backgrounded), external-display detection, and **TV Mode** (phone screen blanked, app foreground, so mirroring keeps libass styling). Live Activity in `PlaybackLiveActivity.swift` + `StreamLinkLiveActivities/PlaybackWidget.swift`; its buttons reach the player through the `PlaybackCommandBus` seam in `Shared/PlaybackIntents.swift`. See [docs/STREAMING.md § 2b](docs/STREAMING.md) and [docs/GOTCHAS.md](docs/GOTCHAS.md). |
| [docs/STT.md](docs/STT.md) | **Retired in 17.0.0** (hidden, off by default, whisper no longer installed; code intact). Working on AI auto-subtitles — `stt.py` (whisper.cpp), the `_needs_stt_subs` trigger, STT jobs, `/api/.../generate-subtitles`, the Generate-with-AI UI, whisper bundling in `setup.py`. |
| [docs/YOUTUBE.md](docs/YOUTUBE.md) | Working on YouTube-on-TV — `/api/youtube*`, the Chrome kiosk + `static/tv.html` IFrame player, the `yt_command` SSE relay, the dashboard control routing (`app.youtube_active`). |
| [docs/REMOTE.md](docs/REMOTE.md) | Working on HID wireless remote (air-mouse) support or the Firestick-style TV UI — `remote_input.py` (pynput input hooks), `_remote_key_action` / `_remote_should_handle` / the `tv_ui_*` block in `main.py`, `?tv=1` in `index.html`, button map (incl. 🏠 Home), Windows key suppression, screen arbitration. |
| [docs/DIAGNOSTICS.md](docs/DIAGNOSTICS.md) | **Read when the server is unreachable, slow, or "crashed" with no traceback.** `diagnostics.py` — access/vitals logging, the in-process self-probe (`/healthz`), event-loop lag, thread-pool + library-lock instrumentation, stall stack dumps. Includes how to read an incident. |
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
                          #   python tests/test_animemap.py
                          #   python tests/test_http_clients.py
                          #   python tests/test_reltracks.py
                          #   python tests/test_watchrule.py
                          #   python tests/test_bundlecheck.py
                          #   python tests/test_srcevict.py
                          #   python tests/test_packslice.py
                          # tests/search_eval/ is the LIVE search-accuracy kit
                          # (69 hand-labelled shows) - run verify.py against a
                          # box before trusting a change to episode matching.
                          # tests/race_harness.py is the LIVE integration driver
                          # for download racing - needs a running StreamLink +
                          # qBittorrent and its own magnets file. See its docstring.

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

**The app's own version is not the dashboard's.** `CFBundleShortVersionString` is
`$(MARKETING_VERSION)`, pinned at **1.0** in `App.xcodeproj` and never bumped — so
`build-ipa.sh`'s closing `Version:` line always prints `1.0` and proves nothing about
whether a change compiled in. Don't point the user at it. The dashboard badge in
`static/index.html` is the version that moves; confirm an app change landed by its
behaviour on-device instead.

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
