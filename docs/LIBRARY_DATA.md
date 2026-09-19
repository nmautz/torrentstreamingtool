# `library.json` schema

The only persistent server-side state. Lives at the project root. Accessed via `asyncio.Lock` (`_lib_lock`); never read/write raw from multiple coroutines.

## Top-level

```jsonc
{
  "profiles": [ … ],   // up to 6
  "items":    [ … ],   // library entries
  "settings": {
    "library_paths": [ … ],            // UI-added paths (POST /api/settings/library-paths)
    "library_paths_no_auto": [ … ],    // paths opted out of the automatic save-path pick (POST /api/settings/library-paths/auto).
                                       // Holds STATIC .env roots too — the flag is ours, not .env's. Still valid as an explicit
                                       // save_path and still shown as a destination chip; only `_auto_save_path()` skips them
    "max_volume": 200,                  // global VLC volume cap 0–200; default 200 (no cap)
    "vlc_start_volume": 50,             // VLC startup volume as % of max_volume; default 50 (half max)
    "vlc_night_mode": false,            // VLC dynamic-range compressor (night mode) on/off; default off
    "vlc_night_mode_preset": "medium",  // night-mode intensity: light|medium|max; remembered independently of on/off
    "system_volume_default": 70,        // host OS volume (0–100) restored when a YouTube play stops; default 70
    "youtube_start_volume":  30,        // host OS volume (0–100) pre-set the moment a YouTube play starts; default 30
    "background_video": {               // idle background video (admin upload, .background/<name>)
      "path":    "/abs/path/to/.background/loop.mp4",
      "name":    "loop.mp4",
      "volume":  50,                    // 0–200, separate from user playback volume
      "enabled": true                   // when false, VLC stays idle on stop
    },
    "admin_overrides": {
      "indexer_categories": "0",        // overrides .env INDEXER_CATEGORIES at search time
      "tmdb_api_key": "abc123",         // overrides .env TMDB_API_KEY; set/cleared in admin UI
      "hls_ladder": [720]               // ABR down-rung heights new stream preps emit; absent = default [720,480]
    },
    "scheduled_reboot": {               // daily idle-gated host reboot (admin System tab)
      "enabled":      false,
      "time":         "00:00",          // local HH:MM in `timezone`
      "timezone":     "America/Los_Angeles",  // IANA name; "" = system local
      "idle_minutes": 15,               // no usage for this long ⇒ reboot; clamped 1–720
      "catch_up_hours": 4,              // may only fire within this many hours of `time`; clamped 1–24
      "last_fired":   ""                // internal: tz date last fired; loop guard, reset on save
    },
    "auto_prep": {                      // unified Automatic Stream Prep (admin System tab)
      "mode":         "off",            // "always" ⇒ prep regardless of activity · "idle" ⇒ prep only when idle · "off" ⇒ manual only
      "idle_minutes": 30,               // idle mode: idle this long ⇒ start; clamped 1–720
      "on_activity":  "hard"            // idle mode: "hard" ⇒ kill in-flight on activity · "soft" ⇒ let it finish then hold
    },
    "play_prep": {                      // auto on-device prep on VLC play (admin System tab)
      "enabled":  true                  // prep the playing episode + playlist tail (interactive; ignores pause gate + activity). Default ON
    },
    "missing_content": {                // library shows not-yet-downloaded seasons/episodes (admin System tab)
      "enabled":      true,             // diff the show's TMDb inventory against what's on disk. Absent ⇒ true
      "show_unaired": false             // future/undated TMDb episodes render as "Upcoming" instead of being hidden. Absent ⇒ false
    },
    "download_race": {                  // parallel candidate downloads for an auto-picked title (admin System tab)
      "enabled":         false,         // OFF by default: racing multiplies bandwidth + disk for the length of the race
      "size":            3,             // candidates per race; 2 | 3 | 4
      "max_items":       2,             // concurrent races, box-wide; 1 | 2 | 3
      "quality_ceiling": 1080,          // highest tier the HQ track may aim at; 720 | 1080 | 2160
      "hq_upgrade":      true           // keep a better copy beside the fast one and swap it in when it lands
    },
    "pack_first": {                     // may a request for ONE episode be answered with a whole-season torrent (admin System tab, 17.9.0)
      "enabled":   true,                // on by default. Off affects only the NEXT request — packs already sliced keep their episodes
      "max_bytes": 214748364800         // hard ceiling on the whole torrent (200 GB). The Auto-pick size window judges the PER-EPISODE
                                        //   share, which is what actually downloads — so this is the only thing stopping a
                                        //   complete-franchise torrent being adopted to get one episode. See `_pack_first_cfg`
    },
    "stream_focus": {                   // what "play this now" does to everything else downloading (admin System tab)
      "pack_focus":   true,             // deselect the other unfinished files in the SAME torrent while one is streamed. Absent ⇒ true
      "sibling_kbps": 512               // TOTAL KB/s every OTHER downloading torrent shares meanwhile; 0 | 256 | 512 | 1024 | 2048 (0 = no throttle)
    },
    "prep_validate": {                  // validate-and-repair source files during bulk/idle prep (admin System tab)
      "mode": "off"                     // "off" | "before" | "after" — deep-decode + remux-repair as prep rides through. Default "off"
    },
    "cache_autopurge": {                // auto-evict orphan offline-cache bundles (admin Offline Cache tab)
      "enabled": false,
      "max_gb":  50                     // purge all orphans once total .offline_cache/ size ≥ this many GB; clamped 1–10000
    },
    "vpn_killswitch": {                 // how far the Mullvad kill switch reaches (admin System tab)
      "block_ui": true                  // true ⇒ a VPN drop locks the whole UI (overlay); false ⇒ only qBit is killed. qBit is killed either way. Absent ⇒ true
    },
    "subtitles": {                      // unified subtitle policy (admin System tab)
      "default_language": "eng",        // 3-letter code; "" = Any. Absent ⇒ "eng"
      "on_by_default":    false,        // start playback with subs on? (profile may override)
      "auto_search":      true,         // fetch a preferred-lang sub online on play when none embedded
      "upgrade_late_subs": true,        // swap an auto-applied AI sub for a real one once it downloads. Absent ⇒ true
      "single_option":    true          // treat a lone subtitle as the preferred language. Absent ⇒ true
    },
    "stt": {                            // AI auto-subtitle generation (admin System tab)
      "enabled":          true,
      "translate":        true          // add an English track for non-English audio
      // default_language is UNIFIED — sourced from settings.subtitles, not stored here
    },
    "groups": [                         // franchise shelves (15.0.0) — see "Groups" below
      {
        "id": "coll:10",                // "coll:<tmdb collection id>" for a materialised auto group, else a hex id
        "name": "Star Wars",
        "collection_id": 10,            // absorb every film of this TMDb collection; 0 = none
        "order": "story",               // "story" | "release"
        "members": ["series:obi-wan kenobi"],  // _series_key values, in addition to the collection's films
        "member_order_pinned": false,   // true once a caller sends a full `members` list (their order wins)
        "created_at": "..."
      }
    ],
    "autoupdate": {                     // dashboard auto-updater (admin Updates tab)
      "enabled":        false,
      "branch":         "main",         // main/beta/alpha — or any branch when dev_mode
      "dev_mode":       false,          // "show all branches": relax the branch gate for development
      "interval_hours": 6,              // auto-check cadence; clamped 1–168
      "auto_apply":     true            // apply (when idle) on detect, vs. just banner
      // …plus internal fields: last_check_at, last_check_status,
      //    last_applied_at, last_applied_commit, last_error
    }
  }
}
```

## Profile

```jsonc
{
  "id": "uuid",
  "name": "Nathan",                    // max 30 chars
  "color": "indigo|purple|green|red|orange|pink",
  "pin_hash": "sha256(pin)",            // optional; 6-digit pin
  "elevated": true,                     // optional; can view admin_only items
  "simple_ui": false,                   // optional (14.0.0). Does this profile get the SIMPLIFIED interface?
                                        //   absent  = default: `not elevated` (so the household is Simple, the admin is Full)
                                        //   true/false = explicit override in either direction
                                        // Set via POST /api/profiles/{id}/simple-ui (admin or PIN-verified).
                                        // Presentation only — it hides controls, it does not protect them;
                                        // every endpoint behind those controls keeps its own auth.
  "auto_skip_intro":   true,            // optional; default false
  "auto_skip_credits": true,            // optional; default false
  "resume_mode": "auto|prompt|off",     // default "auto"
  "subtitles_on": true,                 // optional; per-profile override of settings.subtitles.on_by_default. absent/null = inherit; true/false = force
  "allowed_indexers": ["idx_a","idx_b"],// optional; Jackett indexer IDs this profile may search. Absent/empty = unrestricted (all configured indexers). Admin-set via Profile PINs tab; enforced by /api/search
  "series_subtitle_prefs": {            // optional; remembered subtitle pick per series (this profile).
                                        // Key = item.series, or "item:<item id>" for untagged items (_series_of_item)
    "Series Title S01": {
      "off": false, "lang": "eng", "ai": false, "name": "...",   // v1 fields
      "title": "full subtitles",        // v2 (8.5.0): normalized container track title
      "idx": 1,                         // v2: slot in the episode's embedded-TEXT-sub list (-1 = sidecar pick)
      "sig": "eng:full subtitles|eng:signs/songs",  // v2: that episode's track-layout signature (_sub_sig_of)
      "at": "...",                      // v2: pick time (newest-wins vs the per-file pick)
      "updated_at": "...",
      "groups": {                       // v2: one remembered pick per distinct track layout —
        "<sig>": { "...same descriptor fields..." }   // what makes mixed-release series resolve per episode group
      }
    }
  },
  "series_audio_prefs": {               // optional; remembered AUDIO pick per series (this profile).
                                        // Same shape/key rules as series_subtitle_prefs, simpler descriptor:
    "Series Title S01": {
      "lang": "jpn",                    // canonical language of the chosen audio track
      "title": "commentary",            // normalized container track title
      "idx": 1,                         // slot in the episode's audio-track list
      "sig": "jpn:|eng:commentary",     // that episode's audio layout signature (_audio_sig_of)
      "at": "...", "updated_at": "...",
      "groups": { "<sig>": { "...same fields..." } }   // per-layout pick for mixed-release series
    }
  },
  "audio_language_pref": {              // optional (8.9.3); self-learning per-profile audio fallback.
    "lang": "jpn",                     // canonical language of the last-picked audio track
    "idx": 1,                          // slot index of that pick (for untagged multi-audio releases)
    "count": 2,                        // # audio tracks in that episode — idx only reused when it matches
    "at": "..."
  }
}
```

PIN hash is plain SHA-256 of the 6-digit string (no salt). PIN protection is "soft" — anyone with filesystem access can read the JSON, and there's no rate limiting on `verify-pin`. It is not a security boundary against someone with access to the box.

It **is** enforced server-side against the network, though (11.20.0). `verify-pin` issues a **profile session token** (`X-Profile-Token`, 12 h, persisted in `profile_sessions.json`), and that token — not a claimed `profile_id` — is what unlocks admin-locked items (`_is_elevated`) and the delete endpoints (`_require_delete_auth`). Before that, the PIN was purely client-side: `GET /api/profiles` hands out every UUID unauthenticated, so quoting an elevated profile's id was enough. Consequence to keep in mind: **a profile with no PIN can never be elevated and can't delete anything** — there's nothing for it to prove. See [GOTCHAS.md § `profile_id` is a claim, not proof](GOTCHAS.md). All profiles (PIN-protected or not) are shown in the profile picker; selecting a PIN-protected one prompts for that profile's PIN (verified via `POST /api/profiles/{id}/verify-pin`) before logging in.

`settings.max_volume`: VLC is uncapped (0–200, where 200 % is overdrive). Capping it system-wide stops anyone from accidentally blowing the speakers. Lives under `settings` because it applies to the physical playback host, not to individual viewers. Enforced server-side in `_global_max_volume`.

`settings.vlc_start_volume`: the volume VLC opens at, expressed as a **% of `max_volume`** (0–100, default 50). At startup the backend computes `round(max_volume * vlc_start_volume / 100)`; the historical behaviour was a hard-coded half-max, which the default preserves. Read via `_global_vlc_start_volume_pct`.

`settings.vlc_night_mode` / `settings.vlc_night_mode_preset`: night mode — VLC's `compressor` audio filter, which narrows the gap between the quietest and loudest sounds so dialogue stays clear at low room volume. `vlc_night_mode` is the on/off flag (default off); `vlc_night_mode_preset` is the intensity (`light` / `medium` / `max`, default `medium`) and is **remembered independently** of the on/off toggle — turning night mode off and back on reuses the same intensity. The on/off toggle is reachable from both the fullscreen-controls moon button and the profile-settings panel; the **intensity picker is settings-menu only** (`POST /api/settings/night-mode` accepts `night_mode` and/or `preset`, merged). Both are global host settings, not per-viewer. **There is no VLC HTTP command to add/remove an audio filter at runtime**, so the compressor is a launch arg (`NIGHT_MODE_PRESETS[preset]`) and changing it relaunches VLC — `_apply_night_mode` snapshots the current file + position, relaunches via `_restart_vlc_process` (which reads `state.vlc_night_mode` + `state.vlc_night_mode_preset`), then replays and seeks back. A preset change *while night mode is off* just persists (no relaunch). `run.py` (`start_vlc`) and `watchdog.py` (`vlc_spec`) read these same settings independently when they launch VLC (boot / crash recovery), so the three `NIGHT_MODE_PRESETS` dicts must stay in sync. Seeded into `state` at lifespan startup and exposed in `state_snapshot`. See [GOTCHAS.md](GOTCHAS.md).

`settings.tv_playback_mode`: which surface a library play opens on **at the host's TV**
— `"device"` (the `?tv=1` kiosk's own `<video>`, HLS, driven by the remote) or `"vlc"`
(the classic VLC window). Default `"device"` since 13.0.0. Deliberately **global, not
per-profile**: the TV is one physical screen, so a per-viewer value would mean the same
TV behaved differently depending on who last picked a profile on it. Normalised on read
by `_tv_playback_mode()` — anything unrecognised (including a `library.json` written
before this key existed) reads as the default. Seeded into `state.tv_playback_mode` at
lifespan startup and exposed in `state_snapshot` so the User Settings toggle and the
fullscreen More-panel switch stay in sync across clients. It only decides where the
**next** play opens; VLC remains the automatic fallback when prep or the just-in-time
stream can't serve a file, whichever way this is set. Turn it to `"vlc"` for a 5.1 setup
or HDR content — the browser path downmixes to AAC stereo and does no tone mapping. See
[STREAMING.md § On-device as the TV surface](STREAMING.md) and [REMOTE.md](REMOTE.md).

`settings.system_volume_default`: the host's OS mixer volume (0–100) restored when a YouTube-on-TV play stops. Headphones at 100 % can blow eardrums and a movie session shouldn't leave the room loud, so on Stop `_stop_cleanup` calls `set_system_volume(target)` (pycaw on Windows / `osascript` on macOS / `pactl`/`amixer` on Linux). Default 70. Edited via **System Volume After YouTube** in the profile-settings panel (`POST /api/settings/system-volume-default`). **Global** (lives under `settings`, not per-profile). See [YOUTUBE.md](YOUTUBE.md).

`settings.youtube_start_volume`: the host's OS mixer volume (0–100) pre-set the *moment* a YouTube play starts — `youtube_play` calls `set_system_volume(target)` before the `yt_command:load` broadcast and before Chrome paints the kiosk, so the IFrame player can never produce a first audio frame at system max. Default 30. Edited via **YouTube Starting Volume** in the profile-settings panel (`POST /api/settings/youtube-start-volume`). **Global** (lives under `settings`, not per-profile). See [YOUTUBE.md](YOUTUBE.md).

`settings.background_video`: managed by the **Background** admin tab (see [ADMIN.md](ADMIN.md)). The file lives under `.background/<name>` at the repo root; the directory is wiped on each upload so only one file ever exists. The `background_video_loop` task in `main.py` plays it on VLC any time VLC reports `stopped` and a stream pipeline isn't actively buffering. Any user `vlc("in_play", …)` replaces it and restores the user's pre-bg volume.

`settings.scheduled_reboot`: managed by the **System** admin tab (see [ADMIN.md §7](ADMIN.md)). The `scheduled_reboot_loop` task reboots the host daily at `time` (in `timezone`) once it's been idle for `idle_minutes`, but only within `catch_up_hours` of `time` — past that it stands down until tomorrow rather than chasing an idle gap all day (which is how a 02:00 reboot once landed at 14:44, mid-session). `last_fired` is an internal guard (the tz date of the last fire) that stops the just-rebooted machine from re-arming and looping; it's reset to `""` whenever the config is saved so a newly-set time can arm the same day. Lives under `settings` because it applies to the physical host, not an individual viewer.

`settings.auto_prep`: managed by the **System** admin tab's *Automatic Stream Prep* card (see [ADMIN.md § Automatic Stream Prep](ADMIN.md)); read via `_auto_prep_cfg`. One `mode` drives the `auto_prep_loop` task — `"always"` preps every un-prepped library file regardless of activity (re-enqueuing new content ~every 5 min while engaged); `"idle"` preps only while the host has been idle (`_machine_in_use`) for `idle_minutes` and stops on activity, with `on_activity` choosing the stop kind (`"hard"` ⇒ `_pause_prep(kill=True)` discards the in-flight encode — restarts from scratch later, no mid-file checkpoint; `"soft"` ⇒ `kill=False` lets it finish then holds); `"off"` never auto-preps. `idle_minutes` is clamped 1–720. Engagement is tracked in-memory (`state.auto_prep_engaged`), so there's no persisted fire-guard. **Lazy migration:** if `auto_prep` is absent, `_auto_prep_cfg` derives a default from the legacy `idle_prep`/`overnight_prep` keys (either enabled ⇒ `mode:"idle"`, else `"off"`); the new key is written on first save and the legacy keys are then ignored. Replaces the former separate `overnight_prep` + `idle_prep` (the fixed nightly time-window is gone). Lives under `settings` because it applies to the physical host.

`settings.play_prep`: managed by the **System** admin tab's *Auto-Prep on Play* card; read via `_play_prep_cfg`. **Default ON.** When enabled, every VLC library play preps the playing episode for on-device then the rest of the playlist one episode at a time (`_maybe_start_play_prep` → `_play_prep_chain`, tracked on `state.play_prep_task`). The episode is skipped if resumed with <5 min left (`PLAY_PREP_TAIL_SECS`). Unlike `auto_prep`'s bulk jobs, its jobs are queued **interactive**, so they run regardless of the `auto_prep` mode and live activity (the bulk pause gate and activity-kill don't touch them). Lives under `settings` because it applies to the physical host. See [STREAMING.md § Auto-prep on play](STREAMING.md).

`settings.prep_validate`: managed by the **System** admin tab's *Validate & Repair on Prep* card; read via `_prep_validate_cfg`. **Default `off`.** `mode ∈ {off, before, after}` makes **bulk/idle** stream-prep jobs deep-decode the source (`_validate_one_file`) and, if damaged, **remux-repair** it in place (`_repair_one_file`, lossless — no lossy re-encode) — `before` heals the file ahead of the encode (then re-points `out`/`tmp_dir` at the healed file's new `_offline_cache_key`), `after` validates post-encode (a repair purges the just-built bundle so it re-preps next cycle). Interactive play-on-device preps never validate. GPU-accelerated when NVENC is present. An unknown mode falls back to `off`. Lives under `settings` because it applies to the physical host. See [ADMIN.md § Validate & Repair on Prep](ADMIN.md) and [STREAMING.md](STREAMING.md).

`settings.cache_autopurge`: managed by the **Offline Cache** admin tab's *Auto-Purge Orphans* card; read via `_cache_autopurge_cfg`. **Default OFF.** The `cache_autopurge_loop` task re-checks every 5 min: when `enabled` and the total `.offline_cache/` size is at/above `max_gb` GB, it deletes every orphan bundle (the same set the manual "Purge All Orphans" clears — cache/partial dirs + legacy MP4s that no longer map to a live library file). Bundles backing current library files are never touched, and active prep jobs are skipped, so it can only reclaim already-safe space. `max_gb` is clamped 1–10000. The last run's `{deleted, bytes_freed, …}` is held in-memory on `state.cache_autopurge_last` (not persisted). Lives under `settings` because it applies to the physical host's disk. See [ADMIN.md](ADMIN.md).

`settings.admin_overrides.hls_ladder`: managed by the **System** admin tab's *Storage & Compression* card (Default On-Device Resolutions); read via `_hls_ladder_heights`. A list of ABR **down-rung** heights (subset of `[1080,720,480,360]`) that new HLS stream preps emit — the source-resolution rung is always emitted and is **not** listed here. Absent (or a default `[720,480]` pick) ⇒ the override is removed and `DEFAULT_HLS_LADDER_HEIGHTS = [720,480]` applies. Invalid heights are dropped on read and on save. **Forward-only:** changing it shapes future preps but doesn't bump `OFFLINE_CACHE_VERSION`, so existing bundles are untouched (slim those with the admin *Drop HLS Resolutions* tool → `POST /api/admin/hls-trim`). Lives under `admin_overrides` alongside the other admin-set overrides. See [STREAMING.md § Configurable ABR ladder](STREAMING.md) and [ADMIN.md § Storage & Compression](ADMIN.md).

`settings.autoupdate`: managed by the **Updates** admin tab (see [ADMIN.md §8](ADMIN.md)). Drives the `updater_loop` background task + the `/api/admin/updater/*` endpoints. `branch` is sanitised on read (`_autoupdate_cfg`): when `dev_mode` is false a non-canonical value snaps back to `main`; when `dev_mode` is true any structurally-valid branch name survives (so a developer can pin a feature branch). All branch operations route through `updater.branch_allowed(branch, allow_any=dev_mode)`. Lives under `settings` because the updater acts on the whole host install, not an individual viewer.

`settings.subtitles`: managed by the **System** admin tab's *Subtitles* card; read via `_subs_cfg`. The single source of truth for the preferred subtitle language. `default_language` is a 3-letter code (`_canon_lang`-normalized) or `""` (Any). **Migration / defaults:** if the `subtitles` block is absent it's seeded from the legacy `settings.stt.default_language` when that's set, else `"eng"` — so an unconfigured box defaults to English; once the block exists its value is used verbatim (so an admin who picks "Any" → `""` keeps it). `on_by_default` is whether playback starts with subs on (default false; a profile's `subtitles_on` overrides). `auto_search` lets playback fetch a preferred-language sub from OpenSubtitles when none is embedded (default true). `upgrade_late_subs` (default true) swaps an auto-applied **AI** sub for a real preferred-language one once it finishes downloading — driven by the `subtitle_upgrade_loop` task (VLC) and the on-device player's poller (`GET /api/library/{id}/subs`). `single_option` (default true) treats a lone real subtitle as the preferred language even when its filename carries no language tag. This is the central subtitle setting: `_stt_cfg` re-sources its `default_language` from here, the search endpoint defaults to it, and `_apply_subtitle_policy` selects tracks by it (preferring a **real** track over an AI one for the same language).

**Subtitle pick memory.** A subtitle choice is remembered as a *resolvable descriptor* — `{off, lang, ai, name, title, idx, sig, at}` (v2, 8.5.0; v1 entries lack the last four fields and still work) — not a VLC ES ID or HLS sidecar index (both drift between replays, and a late-downloaded sidecar shifts the list). `sig` is the episode's embedded-**text**-subtitle layout signature (`_sub_sig_of`, one `lang:title` token per track — identical between the VLC and bundle players) and `idx` the pick's slot in that list, so episodes with the same track layout restore the exact slot even with no language/title tags; `at` is the pick time. It's stored two ways: per file (`file_progress[path].subtitle_sel`) and per profile+series (`profile.series_subtitle_prefs[<key>]`, written by `_save_series_sub_sel` — **only on a deliberate viewer pick**, see the `sub_series` flag; the entry also accumulates a `groups` map, one remembered pick per distinct layout signature, so a series stitched from several releases resolves each episode group to its own pick). The key (`_series_of_item`) is the series name when the item has one, else the stable fallback `"item:<item id>"` (7.16.3) — previously an untagged item (season pack, movie, the default `""` series from the download/upload flows) produced an empty key and the per-series save silently no-oped, so picks never carried from one episode of those items to the next. **Both players write it and both players read it** (7.16.1): a VLC pick builds the descriptor from `state.vlc_sub_meta` (`_remember_vlc_sub_pick`) and a device pick posts it via `/local-tracks` (which also drops any stale per-file `subtitle_track` ES ID so the newest pick wins) — this is what carries a pick across a device↔VLC switch. On the next play `_apply_track_prefs`/`_apply_subtitle_policy` (VLC) / the on-device resolver pick the **newest** of the file pick (`at`) and the series pick (`updated_at`) — legacy timestamp-less picks count as oldest, which heals per-file `{off}` descriptors older builds wrote by accident — then match by exact `name` → same `sig`+`idx` → `groups[sig]` → fuzzy episode-invariant `name` → `lang`+kind → any-kind in that language → `title` → lone-option → **last-resort real track** (a remembered ON pick never silently reverts to off), before falling back to the default policy; an explicit descriptor is honoured **even when subs default off**. The legacy `file_progress[path].local_subtitle_idx` is kept only for the bundle-index path; sidecar/AI picks live in `subtitle_sel` (the old on-device save dropped them, persisting `-1`). **Every writer of a `file_progress` entry must spread the existing entry's track keys** (`_TRACK_PREF_KEYS` in `main.py`: `audio_track`, `subtitle_track`, `local_audio_idx`, `local_subtitle_idx`, `subtitle_sel`, `audio_sel`, `audio_offset_ms`) into the replacement dict — the periodic progress saves clobbered them before 7.16.1, which is exactly the "subs reset to off after stop/start" bug. See [GOTCHAS.md](GOTCHAS.md).

**Audio pick memory (8.9.0).** The audio track is remembered the same way, with a simpler descriptor — `{lang, title, idx, sig, at}` — since audio tracks are always embedded (no sidecars, no AI, never "off"). `sig` is the episode's audio-track layout signature (`_audio_sig_of`, one `lang:title` token per track, identical between VLC's status list and the HLS bundle meta) and `idx` the pick's slot, so any episode with the same layout restores the exact track even with untagged tracks. Stored per file (`file_progress[path].audio_sel`) and per profile+series (`profile.series_audio_prefs[<key>]`, written by `_save_series_audio_sel` with a `groups` map; an audio pick is always deliberate, so — unlike subtitles — there's no `sub_series`-style gate). Both players write it (VLC via `_remember_vlc_audio_pick`; a device pick posts `audio_sel` to `/local-tracks`, which drops the stale per-file `audio_track` ES ID) and both read it: on the next play `_apply_track_prefs` (VLC) / `_lpResolveAudioSel` (on-device) take the **newest** of the file pick (`at`) and series pick (`updated_at`), then match by same `sig`+`idx` → `groups[sig]` → `lang` → `title`, before falling back to the raw per-file `audio_track` / `local_audio_idx` / the source's `default` rendition. The raw `audio_track` ES ID is only used when it isn't outranked by a newer series pick (embedded audio ES IDs are stable across replays of the *same* file but meaningless on a different episode). `series_audio_prefs` is re-keyed on a series rename, and `audio_sel` survives the offline sync round-trip and the iOS fully-offline store (`audioSel`).

**Manual audio delay (11.10.0).** `file_progress[path].audio_offset_ms` is a plain integer (ms, 0–1000, 25 ms steps) written by the on-device player's **Sync** slider through `/local-tracks`. It is **not** a descriptor and has **no** series-level fallback, on purpose: it corrects one encode's bad track delay (a release group shipping `start_time=0.5` on the dub, which the HLS bundle reproduces as a cross-rendition gap naive players drop), not a viewer preference — and a season is not uniformly broken. 0 removes the key instead of storing a zero. The device also mirrors it into `localStorage` (`streamlink_audio_offsets`) so the fully-offline iOS player restores it without the host; the native offline store carries no field for it. See [STREAMING.md § Manual audio offset](STREAMING.md).

**Profile-level audio-language fallback (8.9.3).** Per-series memory only crosses episodes that share a series key, so episodes downloaded as individual library items (empty `series` → unique `item:<id>` key) never carried an audio pick between them — the "audio doesn't keep between episodes" report. `profile.audio_language_pref` = `{lang, idx, count, at}` is the audio analog of the subtitle language default: updated on **every** deliberate audio pick on both players (`_learn_profile_audio_pref`, fed the same descriptor VLC/device build) and applied by `_apply_track_prefs` (`_resolve_profile_audio_pref`) **only as a fallback** when no per-file/per-series descriptor resolves. It matches by `lang` first (release-stable), then reuses the remembered slot `idx` **only when the current episode's audio track `count` matches**. **Both players honour it** (8.9.4): the on-device player gets `audio_language_pref` in its `saved_tracks` payload (from `/saved-tracks` and `/offline-prepare`) and resolves it via `_lpResolveAudioPref` after its descriptor chain; for fully-offline playback (host `library.json` unreachable) the app mirrors the pref to `localStorage` per profile on every pick (`_appLearnAudioPref`) and restores it in `_appOfflineSavedTracks`. See [GOTCHAS.md](GOTCHAS.md).

`settings.stt`: managed by the **System** admin tab's *Auto-Generated Subtitles (AI)* card. Gates AI auto-subtitle generation (whisper.cpp): `enabled` + `translate` (adds an English-translated track for non-English audio). The **preferred language is unified** — `_stt_cfg.default_language` is read from `settings.subtitles` (above), not stored here. Consumed by `_needs_stt_subs` / `_ensure_stt_for`. See [STT.md](STT.md).

`settings.vpn_killswitch`: managed by the **System** admin tab's *VPN Kill Switch* card; read via `_vpn_killswitch_cfg`. `block_ui` (default `true`) decides how far the Mullvad kill switch reaches when the VPN drops: `true` locks the whole dashboard behind the full-screen overlay; `false` suppresses the overlay so only qBittorrent is killed and the rest of the UI stays usable. **It does not gate the qBit kill** — `vpn_guard` (in-process) and `watchdog.py` (process level) always terminate qBittorrent on a VPN drop regardless, and the P2P stream/download endpoints stay 403'd in both modes. Mirrored into `state.vpn_block_ui` at lifespan + on save, and broadcast in the `state` / `vpn_status` SSE events. Lives under `settings` because it applies to the physical host. See [ADMIN.md § VPN Kill Switch](ADMIN.md) / [GOTCHAS.md § VPN](GOTCHAS.md).

`resume_mode`:
- `"auto"` (default) — immediately seek to saved position
- `"prompt"` — start at beginning, show resume offer tile, user accepts via `/api/resume-now`
- `"off"` — always start from beginning, no prompt

`subtitles_on`: per-profile override of `settings.subtitles.on_by_default`. Absent/`null` ⇒ inherit the admin default; `true`/`false` ⇒ force subs on/off for this viewer. Set via `POST /api/profiles/{id}/subtitles`; read by `_apply_subtitle_policy` on each play.

## Library item

```jsonc
{
  "id": "uuid",
  "title": "Series Title - S01E03 - Episode Name (1080p…)",
  "series": "Series Title S01",         // empty for movies/one-offs
  "season": 1,                          // 0 if not detected
  "episode": 3,
  "files": [ /* see "File" below */ ],
  "size_bytes": 1234567890,
  "added_at": "2026-05-13T01:58:59+00:00",
  "status": "downloading|ready|error",
  "torrent_hash": "abc123...",          // empty for uploaded items
  "error": "",                          // optional; why a status=="error" item failed — shown as the Error badge's tooltip
  "pending_download": { /* optional; restart-recovery params, see below */ },
  "download_source": { /* optional; magnet + save_path kept while downloading, see below */ },
  "download_attempts": [],              // optional; releases the dead-swarm retry has already tried, see below
  "stalled_since": "2026-09-12T20:54:53+00:00",   // optional; when this download was first seen at zero bytes — the dead-swarm clock, see below
  "download": { /* download schedule; see below */ },
  "prep": { /* stream-prep schedule; see below */ },
  "progress": { /* per-profile; see below */ },
  "admin_only": false,                  // optional; hides from non-elevated profiles — excluded from GET /api/library AND from the global prep bar / download badge (no "content is downloaded" evidence leaks).
                                        // Set either by the admin Content Lock tab (POST /api/library/{id}/admin-lock) or, since 11.21.0, at creation time by POST /api/library/download {admin_only:true}
                                        // from a PIN-verified elevated profile. See ADMIN.md § Content Lock
  "ondemand_only": false,               // optional; on-device playback uses JIT only — no permanent HLS bundle is built. User- or admin-settable (Storage tab + episode page). VLC unaffected
  "ondemand_only_locked": false,        // optional; admin lock — when true, non-admin dashboard users can't change ondemand_only (server 403s, UI toggle disabled)
  "default_visible_profiles": [],       // optional; if non-empty, only these profile IDs see item by default
  "hidden_by_profiles": [],            // optional; profile IDs that personally hid this item
  "skip_data": { /* per-file; see below */ },
  "attrib_v": 2,                        // optional (14.1.0). Which structural-attribution pass this item has
                                        // been through. `_migrate_item` re-runs `episodes.attribute_paths`
                                        // over any item below `_ATTRIB_VERSION` that still has a non-bucketed
                                        // file with no episode number, then stamps it — so the regex work
                                        // costs one run per item, not one per library load. Bump the constant
                                        // to re-run the pass over every existing item.
  "tmdb_pick": {"id": 31132, "kind": "tv"},  // optional (14.0.0). The TMDb entry the CALLER resolved, sent as
                                        // {tmdb_id, tmdb_kind} on POST /api/library/download. Smart search opens a show
                                        // page from a TMDb candidate, so the right answer exists before the download
                                        // starts; `_fetch_item_metadata` binds it directly INSTEAD of running the fuzzy
                                        // match, and stamps the result `source: "picked"` (pinned). Absent for Classic
                                        // search and pasted magnets, which still fall back to matching.
  "metadata": { /* optional; TMDb cache — see below */ }
}
```

**`series` is the cohesion key.** `_series_key(item)` = `series:<lowercased series>` (or `item:<id>` when empty) groups items into one show. The grouped-search download flow tags every episode/pack of a show with `series = <show title>` (no trailing season), so episodes downloaded individually **collapse into one library tile** and play as one merged, cross-item show. `GET /api/library` surfaces `series_key` per item; `GET /api/library/series/{key}` returns the merged, item-tagged file list + a series-level resume hint (`find_series_resume_hint`). A season-pack download is still a single multi-file item; a show can mix pack items and single-episode items under the same `series`. See [BACKEND.md](BACKEND.md) § merged series and [GOTCHAS.md](GOTCHAS.md) § cross-item series playback.

**Series-wide management fans across members.** Rename, Fix-Metadata, and On-Demand-Only are per-item operations, but a merged show (many single-episode items) can invoke them across the whole group: `/rename` already loops the group; `POST /api/library/series/{key}/metadata/set` writes each member's `metadata` (respecting `manual`/`custom` pins); `POST /api/library/series/{key}/ondemand-only` flips `ondemand_only` on every member the caller may change (admin-locked members are skipped, not errored). `GET /api/library/series/{key}` exposes aggregate `ondemand_only` (all members on) / `ondemand_only_locked` (any member locked) for the toggle's state. (ZIP "Download selected" stays per-item — it can't span items.)

### `pending_download` (restart-recovery params)

```jsonc
"pending_download": {
  "magnet": "magnet:?xt=...",      // the source magnet, so the add can be replayed
  "save_path": "C:\Media2",       // the RESOLVED folder (user pick, else `_auto_save_path()` — emptiest configured root)
  "torrent_hash": "",              // pre-added hash from /api/library/prepare, if any
  "selected_file_indices": []      // file-index subset; [] ⇒ all files
}
```

Written by `library_download` the instant a download item is created (`status="downloading"`),
**cleared by `library_download_pipeline` the moment it records `torrent_hash`** (or on a failed
add). Its only purpose is restart recovery: an item created but whose pipeline hadn't yet recorded
a hash would otherwise be a permanent orphan (the monitor skips hash-less items and the magnet
lived only in the now-dead task). On startup `_recover_interrupted_downloads` re-drives the pipeline
for any `downloading` item still carrying a `pending_download.magnet`. Normal, fully-added items
**do not** carry this field. See [BACKEND.md](BACKEND.md) and [GOTCHAS.md](GOTCHAS.md).

### `download_source` (re-add params, kept for the life of the download)

```jsonc
"download_source": {
  "magnet": "magnet:?xt=...",      // the source magnet, so the torrent can be re-added
  "save_path": "C:\\Users\\...\\StreamLink"
}
```

Written by `library_download_pipeline` alongside `torrent_hash` and cleared when the item flips
to `ready`. Distinct from `pending_download`, which covers a *restart* mid-add and is dropped as
soon as the hash lands: this one covers qBittorrent **losing a torrent it had already accepted**.
qBit keeps no resume data for a magnet whose metadata never arrived, so a kill — which the VPN
kill-switch performs on every drop — silently loses it, and the item would otherwise sit at
`downloading` forever polling a hash nothing holds. `library_download_monitor` re-adds from this
field at 30 s and 90 s, then errors the item at 3 min. Items from before 11.13.0 have no
`download_source`; recovery falls back to a bare `magnet:?xt=urn:btih:<hash>`.
See [BACKEND.md](BACKEND.md) and [GOTCHAS.md](GOTCHAS.md).

### `download_attempts` (dead-swarm retry history)

```jsonc
"download_attempts": [
  {"key": "<info-hash or release key>", "title": "Hacks S04E02 …-STC",
   "at": "2026-09-12T17:08:26+00:00", "outcome": "added"}    // or "add_failed"
]
```

Appended by `_retry_dead_download` each time a release is abandoned for fetching
zero bytes in 10 minutes, and by `_race_drop` for every candidate a download race
throws out (those carry `"raced": true` and an `outcome` of `raced_out:<reason>`).
It is the exclusion list for the next search — matched on **both** the info-hash
and `_release_key(title)`, because the same release is routinely indexed under two
hashes by two trackers and re-downloading its twin would waste an attempt on
identical, identically-dead content. Persisted, so a restart mid-retry does not
start the cycle over. `len()` of it is surfaced as `retry_count` on `/api/library`.

It is **no longer the retry budget** — see `retry_rounds` below.

### `retry_rounds` (dead-swarm retry budget, 16.0.0)

```jsonc
"retry_rounds": 1
```

How many times `_retry_dead_download` has replaced this item's release. Capped at
`_MAX_DOWNLOAD_RETRIES` (3), after which the item errors out.

This used to be `len(download_attempts)`, which was equivalent only while each
round tried exactly **one** release. A retry round can now start a whole race
behind its replacement, so three raced titles would exhaust a three-release budget
in a single round and a second dead pick could never be replaced. Counting rounds
restores the intent and leaves `download_attempts` as the pure de-dupe ledger it
was always documented to be.

Note the item is swapped **in place** — same `id`, same `progress`, same position in
the library — so `title`, `torrent_hash`, `download_source`, `files` and `size_bytes`
all change together while everything the user had set up survives.

### `stalled_since` (dead-swarm clock)

Set by `_note_download_stall` the first tick a `downloading` item is seen having
fetched **zero bytes**, and cleared the moment it fetches any — and on every exit
from `downloading` (ready, error, retry, manual re-add), so a replacement torrent
never inherits its predecessor's clock. Once it is `_DOWNLOAD_STALL_SECS`
(10 min) old, `_retry_dead_download` swaps the release.

It is **persisted deliberately**. This was an in-memory tick counter until 11.15.1,
which meant every service restart reset the clock — and between auto-updates, the
scheduled reboot and the VPN watchdog, a dead download could restart the clock
forever and never reach 10 minutes. Because it now outlives the process, the
monitor additionally ignores it for `_DOWNLOAD_STALL_BOOT_GRACE` (3 min) after
startup: qBit restarts with the service and deserves a moment to find peers before
being judged on a stamp written before the reboot.

### `stream_focus` (transient, 16.1.0)

One absolute path — the file of a **multi-file** torrent currently being watched
ahead of its own download. Written by `_begin_library_file_stream`, read by
`_reconcile_item_downloads`, which then drops every *unfinished* sibling in that
torrent to qBit priority 0 and puts the focused file at 7.

It exists because sequential download walks pieces in index order and only
priority 0 removes a piece from that walk — see
[GOTCHAS](GOTCHAS.md#sequential-download-ignores-file-priority--only-priority-0-reorders-a-pack).
It is **persisted** only because the download scheduler re-reads the model every
15 s; semantically it is transient, and a value that outlives its playback is a
bug. Three things guarantee it goes away: the owners listed in GOTCHAS clear it,
`_clear_all_stream_focus()` sweeps it at startup, and the reconciler refuses to
apply a focus whose file has finished or is no longer in the torrent.

It is deliberately **not** `download.files[path] = "skip"` for the siblings:
`_all_nonskip_complete` would then see a one-file item and flip it to `ready` as
soon as the watched episode landed, abandoning the rest of the season. Nothing in
the ready-gate looks at `stream_focus`.

The in-memory half (`state.stream_focus_item` / `_path` / `_hash`) additionally
drives the sibling-torrent throttle, which touches qBit only and is never
persisted.

### `race` (parallel download race, 16.0.0)

Present only on an item whose download was started **without the user choosing a
torrent** (one-press Get, or the auto-picked Stream Now source), and only while
`settings.download_race.enabled`.

```jsonc
"race": {
  "v": 1,
  "state": "racing",              // racing | upgrading | settled | exhausted | skipped
  "reason": "",                   // when skipped: cap | single
  "started_at": "2026-09-17T…", "settled_at": "",
  "ceiling": 1080,                // the HQ track's target tier for this race
  "runtime_min": 42.0,            // divisors for the bytes-per-minute cross-check
  "episode_count": 1,
  "upgraded_from": {"title": "…", "label": "720p WEBRip x264", "at": "…"},
  "entries": [{
    "hash": "abc…", "title": "Show.S04E02.1080p.WEB-DL.x265-NTb",
    "magnet": "magnet:?xt=…",     // per entry: a qBit kill loses a metadata-less magnet
    "save_path": "D:\\Media2",    // shared, so a loser delete never crosses drives
    "role": "primary",            // primary | hq | challenger
    "quality": { /* relquality.score() */ },
    "label": "1080p WEB-DL HEVC",
    "status": "live",             // live | paused | dropped | failed | won
    "added_at": "…", "first_bytes_at": "",
    "completed": 0, "total": 0,
    "rate_ewma": 0.0, "samples": 0, "last_sample_at": 0.0,
    "under_ratio_ticks": 0, "miss_ticks": 0,
    "meta_ok": false, "file_count": 0,
    "drop_reason": "", "dropped_at": ""
  }]
}
```

**`item["torrent_hash"]` still names exactly one torrent** — the *incumbent*. Every
other entry is a challenger that exists in qBittorrent and here, and nowhere else
in the item: `files`, `size_bytes`, `download`, `prep` and `progress` all describe
only the incumbent. See [GOTCHAS.md](GOTCHAS.md) § "A racing item still has exactly
ONE `torrent_hash`" for the consumers that must know about the challengers anyway.

`state` transitions: `racing` → `settled` (one winner left, or the incumbent
finished), → `upgrading` (the winner is low-quality and a better copy is still
coming), or → `exhausted` (every candidate died, which hands straight back to
`_retry_dead_download`). `skipped` records that a race was *wanted* but not run —
the global cap was full, or only one usable candidate was offered.

`rate_ewma` is smoothed from `completed` **deltas**, never qBit's `dlspeed`.
`last_sample_at` is a bare epoch float and is deliberately not trusted across a
restart — `_recover_races` re-anchors every sampler on startup.

`upgraded_from` survives the swap so the card can show an "Upgraded" chip for 24 h;
it is the only part of `race` that outlives a settled race in a meaningful way.

### `pack_slice` (one episode out of a pack, 17.9.0)

Present only on an item whose torrent is a **whole-season pack adopted to answer a
request for specific episodes** — the library's Get or Play now on a missing episode,
or a gap fill. Everything the request did not ask for is written into
[`download.files`](#download-download-schedule) as `"skip"`, so the bandwidth cost is
the episodes, not the season.

```jsonc
"pack_slice": {
  "want":       [[1, 5]],             // [[season, episode], …] — what was asked for
  "settled":    false,                // slots are final; stop re-deriving
  "since":      "2026-09-18T20:11:04Z",
  "skipped":    ["D:\\media\\Show S01\\Show.S01E01.mkv", …],  // the paths WE wrote
  "fallback":   {"magnet": "magnet:?…", "title": "Show.S01E05.1080p…"}
}
```

**Why season/episode and not file indices.** `selected_file_indices` resolves once,
from qBit's file order, at add time — and a pack's episode numbering is not final at
add time. Passes 2 and 3 of the [attribution chain](#seasonepisode-attribution) only
run once TMDb metadata binds, which is seconds to minutes later, and for every
long-running anime they *move files across seasons*. Slicing on the add-time numbering
would therefore keep the wrong episode on exactly the shows packs matter most for. So
the want list is re-resolved against `item["files"]` on every monitor tick
(`_pack_slice_apply`) until it settles.

**While it is unresolved, every file is skipped — not none.** A 144 GB pack that
fetched ten gigabytes during the grace window would have cost more than the feature
saves. Priority 0 across the board costs nothing and is undone the tick the episode is
identified.

| Field | Meaning |
|-------|---------|
| `want` | The `[season, episode]` pairs to keep. Empty ⇒ the slice is inert. |
| `settled` | Set by `_pack_slice_settle` once the episodes are identified **and** the metadata driving their attribution has bound — or the moment a person edits the schedule (`/api/library/pack-fetch`). From then on `_pack_slice_apply` is a permanent no-op and the schedule belongs to the user. |
| `since` | When the slice was requested. After `_PACK_SLICE_GRACE_SECS` (300 s) still unresolved, `_pack_slice_fallback` abandons the pack. |
| `skipped` | The ledger of paths this slice wrote. Re-derivation only ever revises **these**, so a file the user un-skipped by hand is never quietly re-skipped. |
| `fallback` | The single-episode release the picker had in reserve. Used by `_pack_slice_fallback` when the episode turns out not to be identifiable inside the pack: the torrent is dropped (nothing of it downloaded, by construction) and this takes its place, with the item keeping its id, its progress history and its place in the library. |

**Once sliced, the rest of the season is one flag flip away** — `POST
/api/library/pack-fetch` moves a skipped file to a live tier and
`_apply_item_schedule` flips the item `ready` → `downloading` and resumes the torrent.
No indexer query, no second torrent, and the season stays on one release group. The
auto flows ask `_pack_available` before they ask an indexer.

### `download` (download schedule)

```jsonc
"download": {
  "mode": "now",                 // "now" = download anytime · "idle" = only during idle/night
  "files": {                     // per-file overrides (by absolute path); inherit `mode` if absent
    "/abs/path/S01E01.mkv": "high",   // High priority — Maximal (download first)
    "/abs/path/S01E02.mkv": "mid",    // Mid priority — High (the default tier)
    "/abs/path/S01E03.mkv": "low",    // Low priority — Normal (download after mid/high)
    "/abs/path/S01E09.mkv": "idle",   // only during the idle/night window
    "/abs/path/extras.mkv": "skip"    // never download
  }
}
```

The per-file mode is BOTH a schedule (`idle`/`skip`) and a **download-priority tier**
(`low`/`mid`/`high`) — the three tiers order which episodes in one torrent fetch first,
by mapping onto qBit's own file-priority levels (`_file_mode_to_priority`). `mid` is the
default; legacy `now` reads as `mid`. The `dl_priority` field in the `/files` response
collapses the effective mode to one of the three tiers for the UI segmented control.

Controls **when** an item's torrent (and individual files) download. The
`download_scheduler_loop` task ([main.py](../main.py)) is the **single source of
truth** that translates this model into live qBittorrent file priorities + torrent
pause/resume every 15 s — never write qBit `filePrio`/`pause` for a scheduled item
outside the reconcile path or the next tick reverts it (see
[GOTCHAS.md](GOTCHAS.md)). Effective per-file schedule = `files[path]` if present,
else `mode`. Mapping to a qBit priority depends on whether the idle/night DOWNLOAD
window is open right now (`_download_idle_open`, derived from the admin
`auto_prep` mode — **Always** ⇒ always open, **When Idle** ⇒ open while idle,
**Never** ⇒ closed — see [ADMIN.md](ADMIN.md)):

| effective mode | qBit priority |
|----------------|---------------|
| `skip` | 0 (never) |
| `low`  | 1 (Normal) |
| `mid`  | 6 (High) — the default tier; legacy `now` maps here too |
| `high` | 7 (Maximal — download first) |
| `idle` | 1 when the idle window is open, else 0 |

If no managed file should download right now, the torrent is paused (`qbit_pause`,
with a 5.x `/stop` fallback); resumed when something becomes eligible. The
item-level **Pause** (`mode=idle`) / **Resume** (`mode=now`) sweep `now`/`high`↔`idle`
on the per-file overrides too, leaving explicit `skip` choices alone. Written by
`POST /api/library/{id}/download-schedule` + `/file-schedule`, the download modal's
"Download at idle/night only" toggle (`DownloadReq.download_mode`), and
`library_download_pipeline` (which seeds `skip` for files the picker deselected).
Missing/legacy items read as `{mode: "now", files: {}}` (plain anytime download).

### `prep` (stream-prep schedule)

```jsonc
"prep": {
  "files": {                     // per-file stream-prep mode (by absolute path)
    "/abs/path/S01E01.mkv": "now",    // prep immediately (the bar's "⚡ Now")
    "/abs/path/S01E09.mkv": "idle",   // prep during the idle/always auto-prep window (default)
    "/abs/path/extras.mkv": "never"   // opt out of all auto-prep
  },
  "priority_default": "mid",     // optional; item/series-level prep-QUEUE tier (low|mid|high). Absent ⇒ "mid"
  "priority": {                  // optional; per-episode prep-queue tier override (by absolute path)
    "/abs/path/S01E01.mkv": "high"    // preps ahead of other bulk/auto-prep work
  }
}
```

The HLS-prep sibling of `download`, governing the `.offline_cache/<sha>/` bundles
instead of the qBit download. Read via `_prep_cfg`; effective per-file mode =
`files[path]` if present, else **`idle`** (the implicit default — eligible for
Automatic Stream Prep, so a brand-new item preps exactly as it did before this
control existed). Modes (`_PREP_MODES`):

| mode | meaning |
|------|---------|
| `now`   | Prep immediately — `POST /prep-schedule` enqueues a bulk job per file (a scoped `/prep-all`). |
| `idle`  | Let `auto_prep_loop` build the bundle during the idle/always window (the default). |
| `never` | Exclude from **all** auto-prep — both `_enqueue_library_prep` (idle/always) and the play-driven `_play_prep_chain` skip these files. **Non-destructive:** an already-built bundle is kept. |

Written by `POST /api/library/{id}/prep-schedule`. The per-file mode is surfaced as
`prep_mode` in the `/files` response so the episode-picker prep bar can highlight the
active segment. Missing/legacy items read as `{files: {}}` (everything defaults to
`idle`). Note `never` only suppresses *building a bundle ahead of time* — playing a
"never" file still works via the on-demand (JIT) streaming path. Admin **Force Stream
Prep** ignores `never` by design (it's an explicit "prep everything" override).

**Prep priority (queue order).** Independent of the schedule, `priority_default` (the
item/series-level tier every episode inherits) and the per-file `priority` map set a
three-tier **prep-queue priority** (`low`/`mid`/`high`, `mid` default) read via
`_effective_prep_priority` / `_prep_cfg`. It orders the single-slot **bulk / auto-prep**
queue only: `_maybe_start_prep_job` stamps each bulk job's numeric tier as `_prep_prio`,
and `_run_offline_job` parks a lower-tier bulk job at the encode-slot gate while any
strictly-higher-tier bulk job is `pending`/`processing` (`_higher_priority_bulk_pending`)
— so a "high" series/episode builds ahead of "mid"/"low" auto-prep. **Interactive**
(play-on-device) and **admin force-prep** still outrank *all* bulk work regardless of
tier, and an in-flight bulk encode is never preempted for a higher-tier *bulk* job (HLS
can't checkpoint) — only the queued order changes. Set via
`POST /api/library/{id}/prep-priority` (`scope:"item"` for the series default, else
per-file); surfaced as `prep_priority` per file + `prep_priority_default` (item-level)
in the `/files` response. See [STREAMING.md](STREAMING.md).

**Moving a series** (`POST /api/library/{id}/move`, admin). Each file's `path` is rewritten to the new directory; for torrent-backed items the data is relocated via qBittorrent `setLocation` (seeding continues from the new path), and each file's co-located HLS bundle (`<file_dir>/.streamlink_cache/<key>/`) rides along. The cache key is path/mtime-independent (`version | filename | size`), so the move never invalidates a bundle. See [STREAMING.md](STREAMING.md) and [ADMIN.md § Content Lock](ADMIN.md).

### File

```jsonc
{
  "name": "The.Boys.S01E01.mkv",
  "path": "/abs/path/to/file.mkv",       // canonical for matching against VLC playlist; rewritten by a series move
  "size_bytes": 1329062039,
  "season": 1,
  "episode": 1,
  "bucket": "Extras",                    // optional; set only on season-0 files that sit OUTSIDE the numbered run — the folder they came from ("Specials"/"Movies"/"OAD"/a spin-off's own name). Groups + labels them in the UI
  "abs_episode": true,                   // optional; transient — "this episode number may be series-absolute". Cleared by the TMDb-aware pass, see below
  "abs_no": 59,                          // optional (17.1.0); series-absolute episode number. Written only by the anime pass, only for a show
                                         //   the mapping table covers. Doubles as that pass's "this season is settled" mark — see below
  "rel_season": 2,                       // optional (17.1.0); what the RELEASE called this file, kept only where the anime pass moved it.
  "rel_episode": 1,                      //   Never read by the decode; it is the record, and what `animemap.reset_files` rewinds to
  "compressed": true,                    // optional; set true when this file was re-encoded IN PLACE by the compression tool — see below
  "compressed_at": "2026-06-25T18:00:00Z", // optional; when the in-place re-encode replaced the original
  "validation": {                        // optional; written by the file validator
    "status": "ok",                      //   ok | damaged | missing
    "error":  "",                        //   ffmpeg/ffprobe tail when damaged
    "sig":    "1718900000:1329062039",   //   mtime:size — re-validate when it changes
    "at":     "2026-06-09T17:40:00Z"
  }
}
```

### Season/episode attribution

Attribution lives in **[episodes.py](../episodes.py)** (dependency-free, unit-testable on its own) and
runs in **two passes**. `parse_season_episode()` in [main.py](../main.py) is a thin wrapper over pass 1,
kept for the callers that only ever see a bare filename (uploads, non-torrent moves).

**Pass 1 — structural** (`episodes.attribute_paths`, offline, no network). Called by
`build_file_list()` at download time and by the load-time migration below. It is an **item-level**
call, not per-file: the release root has to be identified (deepest directory shared by every path,
minus any trailing season/bucket component) before a directory can be read as meaningful.

Per file, in priority order:

| Read | Yields | Example |
|------|--------|---------|
| `SxxExx` / `NxNN` on the basename | season + episode, **authoritative** | `Hacks.S05E01.…mkv`, `Death Note - 01x02 - …mkv` |
| Nearest enclosing directory naming a season, + an episode marker on the basename (`- 07`, `E07`, `Ep07`, `#07`, or a guarded bare number) | season + episode, possibly **absolute** | `…/Attack on Titan Season 2/[Anime Time] Attack on Titan - 26.mkv` |
| Nearest enclosing directory naming a **bucket** (`Specials`, `Extras`, `Movies`, `OVA`, `OAD`, `ONA`, `NCOP`/`NCED`…) | season 0 + `bucket` label + a bucket-local index | `…/Attack On Titan OAD/… - 03.mkv` |
| No season and no known bucket, but the file sits in its **own subfolder** | season 0 + `bucket` = that folder's name | `…/Attack On Titan Junior High/… - 04.mkv` |
| No season anywhere, strong episode marker only | season 0 + episode, flagged `abs_episode` | `[Grp] One Piece - 1068.mkv` |
| Nothing | `(0, 0)` | `The Matrix (1999) 1080p.mkv` |

The directory read is **nearest-first and stops at the first match**, so a release root that mentions
other seasons or buckets in its blurb can never override the real folder. A folder naming a *range*
(`…(S01-S04+OVA+Movies+Junior High)…`) or two different seasons is rejected outright — it is not one
season. Bucket words match only at the **end** of a component, so that same root isn't read as
"Movies" either. See [GOTCHAS.md](GOTCHAS.md) § season/episode attribution.

**Pass 2 — TMDb-aware** (`episodes.resolve_absolute`, via `_reattribute_item_files` /
`_settle_attribution` in [main.py](../main.py)). Anime batches number episodes across the whole run:
`…/Season 2/Show - 26.mkv` is **S02E01**, not S02E26, and nothing in the filename says so. Once the
show's `all_seasons` inventory is known, this pass compares each season's numbers against that
season's real `episode_count`:

* **Case A** — the season came from a folder but the numbers exceed its episode count. If the whole
  season's run falls inside the cumulative absolute window `(offset, offset+count]`, the offset is
  subtracted. Decided **per season, not per file**, so one outlier can't split a season across both
  readings.
* **Case B** — *no* file in the item carries a season at all, so every number is absolute: each is
  walked against the cumulative counts to find its season. Skipped the moment any file has a real
  season, which is what stops a spin-off folder from overwriting the main run.

Only files flagged `abs_episode` are ever touched, so a number read off an `SxxExx` — or corrected by
hand — is safe. The flag is cleared once resolved, making the pass a no-op on every later call.

**Pass 3 — anime season grids (17.1.0)** (`animemap.remap_slots`, same call site). The case pass 2
deliberately won't touch: an `SxxExx` that is authoritative *and wrong*, because the release counts
its seasons on a different grid than TMDb does. See § Anime season mapping below. It runs only for a
show the mapping table covers, so no Western show can reach it, and it never touches a season pass 2
resolved — those numbers are already TMDb slots, and reading them as release labels would shift them
a second time.

`_settle_attribution` also **tops up the episode lists for any season the correction reveals**: the
season list sent to TMDb is derived from the files, so correcting the files can surface seasons whose
episodes were never fetched (they'd gain a tab but no titles or stills). Bounded to 12 extra seasons
per call.

### Anime season mapping (17.1.0)

**TMDb's season split for a long-running anime is one convention among several, and the release
groups use the others.** Hunter x Hunter (2011) is one 148-episode run, and every one of these was
live on the indexers on 2026-09-18:

| Source | Seasons | `S02E01` means |
|---|---|---|
| TMDb | 3 — 62 / 74 / 12 | absolute 63 |
| iAHD (Blu-ray) | 3 — 58 / 78 / 12 | absolute **59** |
| ZigZag (Netflix) | 6, numbered **absolute** | its files are `S02E27`…`S02E38` = absolute 27–38 |
| scene (W4F) | one season, forever | `S01E59` = absolute 59 |
| sam / Judas / Refrain | none — `- 059`, `(01-148)` | — |

Pass 1 reads all of those numbers correctly. What it cannot know is *which grid they are counted
on*, and an `SxxExx` is authoritative — so iAHD's 78-file S02 pack lands four episodes out of true
across the whole Chimera Ant arc, with its last four files falling off the end of a 74-episode
season. The mismatch runs the other way too: TMDb folds all three cours of 【OSHI NO KO】 into one
35-episode Season 1, so an ordinary `S02E01` names a season TMDb hasn't got.

Neither AniList nor AniDB answers this alone — AniList *merges* where TMDb splits (one 148-episode
entry for Hunter x Hunter) and *splits* where TMDb merges (11/13/11 for OSHI NO KO). It is a third
grid, not an arbiter. What does answer it is **Anime-Lists' `anime-list-full.xml`**, the community
table behind Sonarr's and Jellyfin's anime handling, which records each AniDB entry's landing place
on both TVDB and TMDb *with offsets*. [`animemap.py`](../animemap.py) caches it in `.anime_map/`
(refreshed weekly, best-effort — a box that never reaches GitHub keeps the old behaviour) and reads
two shapes out of it:

* **An absolute run** (`tmdbseason="a"`) — TMDb's seasons subdivide one continuous run, so the
  series-absolute number is the real coordinate. The table's `start`/`end` windows are the
  boundaries the release groups cut their packs on (for HxH: 1-58 / 59-136 / 137-148, i.e. exactly
  iAHD's split). A pack whose numbers start at 1 is season-local and gets its window's offset added;
  a pack whose numbers start higher *and* fall outside the window its label claims is already
  absolute and the label is decoration (ZigZag).
* **A folded cour** (`tmdboffset`) — the release's season N is a real cour that TMDb merged into a
  bigger season. OSHI NO KO's S2 is TMDb S1 from episode 12; Frieren's is S1 from episode 29.

Every decode is **all-or-nothing**: if any file would land outside TMDb's grid the whole pack is
refused, because a half-remapped season is worse than an honestly mislabelled one. Where the two
grids agree (Code Geass, Attack on Titan, Demon Slayer) nothing moves.

`abs_no` is the settled mark as well as the absolute number — the pass is **not** idempotent
without one, since re-reading its own output as release labels would shift the numbers again. It is
also what the missing-episode search needs: no indexer publishes "Hunter x Hunter S01E59", plenty
publish "059" (measured live, that query is the difference between one 86 MB 1-seeder dubbed HDTV
rip and the 159-seeder Blu-ray batch). The admin **Refresh** button on an item re-fetches the table
and calls `animemap.reset_files` first, which rewinds to `rel_season`/`rel_episode` so a corrected
mapping upstream can actually reach files the pass already moved.

An existing library never re-fetches metadata it already has, so nothing would apply the pass to
shows downloaded before it existed. `anime_map_backfill` (a lifespan task, 45 s after boot) sweeps
them once per start: offline, one library write for everything that moved, and a no-op for every
show the table doesn't cover or that already carries `abs_no`.

The shape is served to the frontend as `anime: {mapped, absolute, total, packs}` on
`GET /api/library/{id}/metadata`, `/api/tmdb/lookup` and `/api/library/series/{key}` — computed at
serve time (`_anime_facts`), never stored on `metadata`, because most `metadata` blobs are pinned
and would never pick it up.

**`packs` (17.2.0)** is `animemap.release_packs` — the season packs the release groups publish,
expressed as TMDb ranges, and **empty whenever the two grids agree**. That emptiness is the
feature: Code Geass, Attack on Titan and Demon Slayer say nothing, because their season 2 and
TMDb's season 2 are the same episodes. Where they disagree, each entry is
`{grid_season, label, from:[season,episode], to:[season,episode]}` — Hunter x Hunter's season 2
pack is `from [1,59] to [2,74]`, which is exactly the sentence the episode page needs: *the four
episodes missing off the end of season 1 are in the season 2 pack.* `animemap.pack_for` (mirrored
in the frontend as `_animePackFor`) does the lookup by ordinary tuple comparison, because a pack
spans a season boundary and neither number decides on its own.

Agreement is judged per shape: an absolute run agrees when its windows end exactly where TMDb's
cumulative counts do, and a cour-mapped show agrees when every cour lands on the TMDb season with
its own number. **An offset alone is not a disagreement** — Attack on Titan's season 3 aired in
two parts and TMDb numbers them 1-12 and 13-22 inside one season 3, which is exactly how the
releases label them. What disagrees is a cour landing on a *different* season, the way
【OSHI NO KO】's season 2 is TMDb's season 1 from episode 12.

**What this makes safe to download (17.3.0).** Because a pack's files are reattributed onto TMDb's
grid on arrival (`_reattribute_item_files` → `animemap.remap_slots`), a pack whose season numbering
disagrees with TMDb's is safe to fetch as-is. That is what lets the library's **Get** button offer a
whole-season download for a show like Hunter x Hunter, where the iAHD `S02` Blu-ray pack holds 78
episodes against TMDb's 74: the download is queued with `season = <the TMDb season>, episode = 0`,
which only sets the item's initial *shape* — where each individual file lands is decided afterwards,
across season boundaries, by the three attribution passes. Don't "fix" that season number to the
pack's own. Absolute-numbered *batches* (`Episodes 1-148`) are still refused as season coverage, on
measured evidence — see [GOTCHAS.md](GOTCHAS.md).

**Sections (15.0.0).** A `bucket` promoted to a first-class unit by
`episodes.sections_for(files, show_title)` — derived, never persisted, so no migration.
`section_key(f)` is `"main"` for a file with no bucket (including season-0 absolute-numbered
anime, which *is* the main run) and the lowercased bucket otherwise. `section_kind`:
`Specials`/`OVA`/`OAD`/`ONA` → `specials`, `Movies` → `movies`, `Extras` → `extras`, any other
folder name → `spinoff`. Order is main, specials, movies, spin-off, **extras last**. Every kind
except `extras` is `counted` into a show's watched total. Each section carries its own resume
hint and watched count (`_section_hints`); progress storage itself is unchanged — sections scope
how it is *read*. A show with more than one section opens a group page ("shelf") instead of the
episode picker.

Canonical file order is `episodes.sort_key` — seasons ascending, season-0 buckets last and grouped by
label, then episode, then name. Used by `build_file_list`, the migration, `/files` and
`/series/{key}`.

`compressed` marks a file the Storage & Compression tool re-encoded **in place**. Because the new
bytes no longer match the torrent's pieces, the file is **no longer torrent-backed** even though the
item keeps its `torrent_hash` (the marker is **per-file** — one torrent can hold several files, only
some compressed — so we never clear the item-level hash). Helpers `_file_is_compressed(f)` /
`_item_has_compressed(item)` ([main.py](../main.py)) read it, and every qBit-dependent path honours it:
`/files` reports a compressed file complete from disk (qBit's view is stale/damaged); the download
scheduler forces it to priority 0 and never re-fetches it; and recheck, Cleanup recover, and
delete-to-free-space **refuse** compressed files (re-downloading would overwrite the smaller result,
which can't be recovered). The marker survives the download monitor's `build_file_list` rebuild. See
[GOTCHAS.md](GOTCHAS.md) and [API.md](API.md).

`validation` is the persisted verdict from the source-file validator (the manual admin scan **and** the idle `background_maintenance_loop` auto-validator both write it). It lets the validator skip already-checked files, drives the Activity tab's "never-validated" backlog count, and makes auto-validation **resume after a restart**. A file is re-validated only when its `sig` (mtime:size) changes — i.e. it was re-downloaded, repaired, or re-encoded — or, for a `missing` verdict, once the file exists again. See [BACKEND.md](BACKEND.md) and [ADMIN.md § Automatic Maintenance](ADMIN.md).

### `video` (colour signalling, per file, optional)

Written by `dvprobe.probe_path` (see [GOTCHAS.md](GOTCHAS.md) § Dolby Vision Profile 5) on the download-ready transition, and backfilled for older files by `video_probe_backfill`. Small and JSON-safe:

```json
"video": {
  "container": "mkv", "codec": "V_MPEGH/ISO/HEVC",
  "width": 3840, "height": 2160, "bit_depth": 10,
  "matrix": 9, "transfer": 16, "primaries": 9,
  "dv_profile": 5, "dv_level": 6, "dv_compat": 0, "dv_el": 0,
  "green": true
}
```

`green` is the only field any caller acts on: Dolby Vision Profile 5 with no compatible base layer, which renders with a heavy green cast in VLC and on device. Everything else is diagnostic.

**Presence is the "already asked" marker.** A file that probes to nothing still gets a record (`{"green": false}`), so an unreadable or exotic container is asked once and then left alone — `_probe_item_video` skips any file that already has the key, which is also what lets the backfill resume after a restart without redoing work. A file header doesn't change, so there is no refresh path; a *replaced* file arrives as a new item with no record and is probed normally.

### Progress (per profile)

```jsonc
"progress": {
  "<profile_uuid>": {
    "last_file": "/abs/path/last/file.mkv",
    "shuffle": false,                      // optional; was this item's last play Shuffle Play (for this profile)?
    "shuffle_scope": "",                   // optional; "all" | "unwatched" — pool the last shuffle drew from
    "file_progress": {
      "/abs/path/file.mkv": {
        "position_sec": 1234.5,
        "duration_sec": 4174.0,
        "completed": false,                // true once the tail was reached BY PLAYING — see "What counts as watched" below
        "played_sec": 1190.0,              // 17.5.0; seconds genuinely played (seeks earn ~nothing). Absent on older
                                           // records — the rule seeds it from position_sec on the next write
        "updated_at": "2026-05-13T02:47:57+00:00",
        "audio_track": 3,                  // optional; VLC ES ID
        "subtitle_track": -1,              // optional; -1 = off (VLC ES ID — drifts between replays)
        "local_audio_idx": 0,              // optional; on-device HLS bundle audio index
        "local_subtitle_idx": -1,          // optional; LEGACY on-device bundle sub index (sidecar picks can't be addressed here)
        "subtitle_sel": {                  // optional; resolvable subtitle descriptor — the robust per-file pick
          "off": false, "lang": "eng", "ai": false, "name": "Movie.eng.srt",
          "title": "", "idx": -1, "sig": "eng:|eng:sdh", "at": "2026-07-03T…"  // v2 fields (8.5.0)
        },
        "audio_sel": {                     // optional; resolvable AUDIO descriptor (8.9.0) — the robust per-file pick
          "lang": "jpn", "title": "", "idx": 1, "sig": "jpn:|eng:commentary", "at": "2026-07-04T…"
        },
        "audio_offset_ms": 475             // optional (11.10.0); manual audio delay for the on-device
                                           // player, ms, 0–1000. Per-file ONLY — no series fallback.
                                           // Absent (not 0) when there is no nudge
      }
    }
  }
}
```

Written by `vlc_progress_tracker` every 15 s, always under **`state.library_profile_id`** — the profile that *started* the active VLC playback (set by `_set_playback_owner`). **Snapshot consistency (8.0.3):** the 15 s save takes a *fresh, self-consistent* read right before writing — `status.json` (position/duration) and `playlist.json` (the active file URI) are re-read back-to-back and the write is skipped for that tick unless (a) the resolved file still equals `state.library_current_file`, and (b) VLC reports `playing`/`paused`. Position and file must come from the same instant: during an automatic episode advance the top-of-loop `pos/dur` read and the separate URI read straddle the transition, and the pre-8.0.3 code wrote the finishing episode's position under the *next* episode's key — the root cause of the "restarts a finished episode" / "jumps to the wrong episode" progress corruption. A skipped tick doesn't advance the 15 s timer, so it retries on the next 2 s pass. The on-device player's `lpStop` has the mirror guard: it only persists on stop when `currentTime ≥ 5`, so backing out before the resume seek lands can't overwrite a real position with ≈0 (every other on-device saver already guards this). The shared fullscreen controls (pause/seek/next/prev) carry no profile, so a second viewer driving them feeds the starter's progress, never their own; the dashboard surfaces the owner as a "started by" chip (snapshotted into `state.library_profile_name`/`library_profile_color`). The on-device player is the exception — it streams per-device and posts to `POST /api/library/{id}/progress` under *that* device's selected profile. `mark_watched` writes `completed: true` with `position_sec = played_sec = duration_sec` (or all three reset to 0 / `false` for unwatched — `played_sec` too, so a rewatch can't complete off the first watch's credit).

**`updated_at` also orders the library (17.12.0).** The newest `updated_at` across a profile's `file_progress` records is that item's *last watched* time (`_item_last_watched_at`), rolled up per `series_key` to sort `GET /api/library` most-recently-watched first — so the show you were watching leads the grid when you come back. A write that bumps `updated_at` therefore moves a show to the top; see [GOTCHAS.md](GOTCHAS.md) § `updated_at` is now the library's sort key.

#### What counts as watched (17.5.0)

**A file completes when the playhead reached its tail _and_ enough of it was genuinely played.** The rule is `watchrule.watch_state` (pure, unit-tested in `tests/test_watchrule.py`); `main.py` wraps it as `_watch_state` / `_offline_watch_state` to add the file's detected `credits_start`. **Every** writer that records a playback position goes through it: the VLC tracker's 15 s save, `_finalize_stopped_file` (Stop, supersede, `/api/vlc/next`, credit skips, end-of-playlist, merged-series crossings), `POST /api/library/{id}/progress` (on-device player and the iOS native background player), and the offline sync.

- **Tail reached:** `pos ≥ credits_start − STOP_OUTRO_WINDOW_SEC` (10 s) when credits were detected, else `pos ≥ FINISH_TAIL_PCT × duration` (**0.90**). The old no-credits rule was "within 10 s of the real end", which almost nobody reaches — an anime episode's last ~95 s are the ED and a next-episode preview, so an episode stopped in its ED at 93 % stayed unwatched forever.
- **Actually played:** `played_sec ≥ MIN_PLAYED_PCT × duration` (**0.60**) — room to skip an intro, a recap and the credits, not to scrub past the plot.

**Why played time, not position.** Until 17.5.0 completion was read off the position alone, and a position is not evidence: the playhead reaches the end identically whether the episode was watched or the scrub bar was dragged. Driven live against the box on a 23:36 episode nobody had watched, that gave four false positives — see [GOTCHAS.md § A position is not evidence](GOTCHAS.md).

**How `played_sec` accrues.** Each write adds `min(Δposition, Δwall-clock × PLAYED_RATE_CAP, PLAYED_STEP_CAP_SEC)` since the previous write, with Δwall-clock from the record's `updated_at`. Playback moves the playhead at about real time and is credited in full; a seek moves it minutes in a second and earns only that second. `PLAYED_RATE_CAP = 2.5` covers 2× playback plus poll jitter. `PLAYED_STEP_CAP_SEC = 60` stops a *stale* record's age paying for a scrub (stopped at 5:00 last week, open it, drag to the end — the week-long gap would otherwise buy the whole episode); every live writer saves every 15 s, so it barely touches real playback. No client has to label a write as a seek, so the rule holds for every client and every stale build of one.

**New and legacy records.** A brand-new record starts at `played_sec = 0` — which is what stops a first-ever write that is a scrub to the end from counting. A record written before 17.5.0 (no `played_sec`) is seeded from its own `position_sec` on the next write: the old rule already believed that position was watched, and without the seed a half-finished episode could never accrue enough to finish.

**Offline sync: the device measures play itself (17.6.0).** `/api/sync/progress` and `/api/sync/resolve` receive **one coalesced position per file** (the device's `OfflineStore` keeps a single record), so the host has no history to measure. So the store does it: every `OfflineStore.saveProgress` accrues `playedSec` by the same rule (position advance capped by the wall clock since the previous save — the **device's** clock on both ends, so no host/device skew enters it), and sends it as the event's `played_sec`. The host merges it through `watchrule.reported_watch_state`: **max** with what it already credits (the device seeded its count from the host's via `/sync/pull`, the download manifest or a server-wins resolve — all return `played_sec = watchrule.played_of(record)` — so summing would count that baseline twice, and a re-sent event whose ack was lost would count everything twice), capped at the duration, then the full rule. The Swift constants mirror `watchrule.py` and must stay in step. An event **without** `played_sec` (an app build from before 17.6.0, or a record it left dirty) falls back to `offline_watch_state`: the tail test alone, with the position taken as played so an episode begun offline and finished online can still complete.

**One-shot backfill (17.6.0).** `_backfill_legacy_tail_stops()` runs from `lifespan` after `_purge_background_video_progress()`. A record with **no** `played_sec` (written before 17.5.0), not completed, and parked inside today's tail (`watchrule.legacy_stopped_in_tail`) is marked completed with `played_sec` seeded from its position and `position_sec` pinned to the duration — exactly what its next write would decide anyway, applied now for every profile. That is the "stopped in the ending theme, stuck unwatched" set the old last-10-s rule produced. `updated_at` is deliberately left alone (it's a reinterpretation, not a watch, and bumping it would raise spurious iOS sync conflicts). Idempotent without a marker: the stamped `played_sec` disqualifies a record, so later starts pre-check, find nothing, and never rewrite `library.json`.

**Leaving an episode for the next one** (`/api/vlc/next`, the credits branch of `/api/skip-now`, the auto-skip-credits countdown) just calls `_finalize_stopped_file` at the live position, so it completes the episode when it was reached by playing — skipping the credits does, pressing Next 30 s in leaves a resume point. Until 17.5.0 these armed a 60 s deferred-watch timer (`_arm_credit_skip_watch`, `state.pending_watch`) that marked the episode watched from **any** position; the on-device player never did, so the TV and the phone disagreed. Removed.

**End-of-playlist.** `_handle_playback_ended` passes the last *real* position (`state.last_play_pos`), not `last_play_dur`: "VLC went idle" is also what a crash, a closed VLC window or a still-downloading file running out of bytes looks like, and crediting those at 100 % marked the episode watched and destroyed its resume point. A genuine end has the last 2 s sample inside the tail anyway.

The 15 s periodic saver only writes while VLC reports `playing`/`paused`, so it never fires at Stop or during an end-of-file transition. `_finalize_stopped_file` fills that gap: it's called from `stop()`, `_handle_playback_ended()` (see [GOTCHAS.md § End-of-media](GOTCHAS.md) — VLC zeroes `time` *and* `length` at EOF, hence the `last_play_*` pair), `POST /api/library/{id}/play` when the new episode differs from the outgoing one, the merged-series crossing, and the leave-for-next paths above. A non-finished finalise just refreshes `position_sec` and **never regresses** a newer saved position (which also makes an end-of-file t≈0 race a no-op).

**Natural auto-advance.** A **within-item** natural auto-advance — VLC reaching a file's EOF and stepping to the next entry in the same playlist — has no handler of its own, so `vlc_progress_tracker` tracks the previous tick's file + position and, when VLC has moved to a *different* file within the **same** item, calls `_finalize_stopped_file` for the outgoing one at that live position (≈ its real end). Cross-item merged-series crossings pass the same live position into `_series_finalize_and_switch`.

**`completed` is monotonic on the online writer.** `POST /api/library/{id}/progress` (`update_progress`) never un-finishes a file (`watch_state` returns `completed` for a record that already has it), and a finished file's `position_sec` stays pinned at `duration_sec` rather than being stamped with an incoming older mid-position. A late/out-of-order flush from the on-device player (a stale position sent after a resume) otherwise flipped a finished episode back to `completed:false`, resurrecting it as a resume target. Mirrors the batch-sync endpoint's rule. Track preferences are saved by `_save_track_pref` ([main.py:944](../main.py#L944)) whenever the user picks an audio/subtitle track. They're re-applied on next playback of the same file by `_apply_track_prefs` (with a 2 s delay so VLC has opened the file).

The profile-level **`shuffle`** / **`shuffle_scope`** fields are the *persisted* Shuffle Play preference (the live random order itself is ephemeral — `state.library_shuffle_order`, gone on stop). Written by `_set_shuffle_pref` (via the `/play` body for VLC, or `POST /api/library/{id}/shuffle-pref` for the device and the in-player shuffle controls): set true on a shuffle play, cleared on any normal play or `/api/library/unshuffle`. `find_resume_hint` echoes them onto the resume hint so **Resume / Play All** can offer to keep shuffling.

**15.6.0 — the pref is series-wide.** `_set_shuffle_pref` writes it to *every* item that shares the named item's `_series_key`, not just that one. A show downloaded as separate per-episode torrents shuffles as one merged series, but `/play` only ever names the item that happens to own the first random file — and the shuffle walks straight out of it on the next episode. Storing the flag on one member meant the very next episode belonged to an item that had never heard of the shuffle, so Resume offered natural order and the run silently ended. `find_series_resume_hint` reads it back with `any()` across the members (which also heals runs recorded before this). A show that lives in one item is a bucket of one, so nothing changes for it.

**`base_synced_at` watermark (iOS offline sync, M3).** This is **not** a stored library field — it lives only in the iOS device's native `OfflineStore` (and on the wire). It records, per `(profile, item, file)`, the server's `updated_at` at the moment the device last successfully synced *that file*. `POST /api/sync/progress` uses it to decide each incoming offline event: if the server's `file_progress[path].updated_at` is **≤ `base_synced_at`**, the server hasn't moved since the device's last sync → the device value is **applied**; if it's **>** (both sides advanced), the endpoint auto-resolves close/`completed` cases (newest timestamp wins, `completed` monotonic) and reports a **conflict** for genuine divergence (writing nothing). Each applied/acknowledged file's response `server_updated_at` becomes the device's new `base_synced_at`, so a server-won case isn't re-sent forever. Genuine **conflicts** are not written by `/sync/progress`; the device shows a "keep mine / keep server" UI (M4) and posts the choice to **`POST /api/sync/resolve`** (plan A3), which writes the device values when the user keeps theirs (bumping `updated_at`) or leaves the host untouched when they keep the server's — returning the authoritative `server` values + `server_updated_at` the device records as the file's new watermark (forcibly re-baselining its own unsynced record on a server win). The library's own progress entry is unchanged in shape — only `updated_at` is the watermark's reference. See [API.md](API.md) "POST /api/sync/progress" / "sync/resolve".

**A download-race upgrade MOVES these keys, it does not rebuild them (16.0.0).** When
`_apply_race_upgrade` swaps a finished high-quality copy in for the low-quality one
that won the race, the file paths change, so every `file_progress` key and any
`last_file` pointing at an old path is re-pointed at its counterpart in the new
copy. The migration is a `fp[new] = fp.pop(old)` — the whole record moves — because
the sibling `audio_sel` / `subtitle_sel` / `audio_offset_ms` keys live inside it and
rebuilding a bare `{position_sec, duration_sec}` would silently discard every track
preference the viewer had set. If both keys somehow exist, the newer `updated_at`
wins and a position is never regressed. An old path with no counterpart in the new
release is left behind (harmless — nothing resolves it). The old copy's `skip_data`
is deliberately **dropped** rather than migrated; see [GOTCHAS.md](GOTCHAS.md)
§ "Upgrading to the better copy".

### `skip_data` (per file)

```jsonc
"skip_data": {
  "/abs/path/file.mkv": {
    "intro": { "start": 12.0, "end": 105.0 },    // or null
    "credits_start": 2940.0,                     // or null
    "analysis": {
      "version": 4,                              // analyzer.ANALYZER_VERSION
      "source": "auto" | "manual" | "failed",    // credits time is fingerprint-only ("auto")
      // Only present when source == "failed":
      "error_code": "no_binary" | "file_missing" | "no_duration" |
                    "fp_empty"  | "no_skip_points" | "exception",
      "error":      "Human-readable detail surfaced in the admin editor + log."
    }
  }
}
```

`source="manual"` entries are never overwritten by re-runs. Entries with `analysis.version < ANALYZER_VERSION` are eligible for re-analysis. `source="failed"` entries are also retried on the next ready-flip in the series — when a new sibling episode arrives, the larger fingerprint pool can unlock a previously-failed file. See [ANALYZER.md](ANALYZER.md#failure-tracking) for the full table of error codes and how the failure is surfaced to users + admins.

### `metadata` (TMDb cache, optional)

```jsonc
"metadata": {
  "source":        "tmdb" | "picked" | "manual" | "custom",  // see "pinning" below
  "tmdb_id":       12345,                  // absent on custom entries
  "tmdb_kind":     "tv" | "movie",
  "title":         "Monster",
  "overview":      "...",
  "poster_path":   "/abc.jpg",            // join with /api/library/{id}/metadata → img_base
  "backdrop_path": "/xyz.jpg",
  "poster_url":    "https://.../p.jpg",   // custom only: absolute image URL (wins over poster_path)
  "backdrop_url":  "https://.../b.jpg",   // custom only: absolute image URL
  "first_air_date":"2004-04-07",          // tv only
  "origin_country":"JP",                  // tv only; ISO country, from TMDb details.
                                          // The show page rejects a release tagged
                                          // for another country's version with it.
  "release_date":  "1999-09-30",          // movie only
  "vote_average":  8.7,
  "genres":        ["Drama", "Mystery"],
  "trailer":       "dQw4w9WgXcQ",          // YouTube key (movie/show trailer; "" = none) — _tmdb_pick_trailer
  "all_seasons": [                         // tv only — the show's FULL season inventory
    {"season": 0, "name": "Specials", "overview": "…",
     "episode_count": 4, "air_date": "2005-01-08", "poster_path": "/sp.jpg"},
    {"season": 1, "name": "Season 1", "overview": "…",
     "episode_count": 74, "air_date": "2004-04-07", "poster_path": "/s1.jpg"}
  ],
  "seasons": {                             // tv only — only the seasons we FETCHED episodes for
    "1": {
      "name":     "Season 1",
      "overview": "...",
      "poster_path": "/season1.jpg",
      "trailer":  "aBcDeFgHiJk",           // YouTube key for this season's trailer ("" = none)
      "episodes": [
        {"season": 1, "episode": 1, "name": "Herr Dr. Tenma",
         "overview": "...", "still_path": "/...jpg",
         "air_date": "2004-04-07", "runtime": 24}
      ]
    }
  },
  "collection":   {"id": 10, "name": "Star Wars Collection",   // movie only (15.0.0): TMDb belongs_to_collection;
                   "poster_path": "…", "backdrop_path": "…"}, //   {} when none. Drives automatic groups
  "story_index":  4,                       // movie only (15.0.0): saga number mined from every title (_story_index); 0 = unknown
  "sections": {                            // 15.0.0 — per-section bindings, _fetch_section_metadata
    "attack on titan junior high": {       // spin-off: its OWN show
      "source": "tmdb", "kind": "spinoff", "tmdb_id": 63510, "tmdb_kind": "tv",
      "title": "Attack on Titan: Junior High", "overview": "…", "poster_path": "…",
      "backdrop_path": "…", "first_air_date": "…", "vote_average": 0, "genres": [], "trailer": "",
      "episodes": [ {"episode": 1, "name": "Starting School! …", "still_path": "…", …} ]
    },
    "oad":    {"source": "tmdb", "kind": "specials", "tmdb_id": 1429, "parent_season": 0,
               "title": "OAD", "poster_path": "…", "episodes": []},   // titles only when counts line up
    "movies": {"source": "tmdb", "kind": "movies", "title": "Movies",
               "files": {"<abs path>": {"tmdb_id": 379088, "tmdb_kind": "movie", "title": "…",
                                        "poster_path": "…", "release_date": "…", "runtime": 0,
                                        "vote_average": 0, "genres": [], "overview": "…",
                                        "trailer": "", "collection": {}}}},
    "extras": {"source": "none", "kind": "extras", "title": "Extras"} // stamped so it is never searched again
  },
  "fetched_at": "2026-05-15T01:23:45+00:00"
}
```

**Section bindings** are resolved in the background (`_spawn_section_fetch`, fired by
`/files`, `/metadata` and `/series/{key}` for any multi-section item) and cached under
`metadata.sections[<section key>]`. A miss is stamped `source: "none"` so it is not re-searched
on every open; `manual`/`custom` entries are pinned. The main section *is* the item's own
metadata and has no entry. `metadata/refresh` replaces `metadata` wholesale, so a forced refresh
re-resolves sections on the next open.

### Groups (franchise shelves, 15.0.0)

`_build_groups(lib, visible_items)` merges two sources, computed on every read:
* **automatic** — any TMDb `collection.id` shared by **two or more** visible movie items
  becomes `{id: "coll:<id>", source: "tmdb_collection"}`;
* **manual** — `settings.groups`. A record with `collection_id` absorbs that collection (so an
  edited auto group stays one shelf), plus its listed `members`.

Members are `_series_key` values, so a show spanning many single-episode items joins once.
Built only from items the caller may see, so a content-locked item never shows in a count.
Ordering (`_member_sort_key`): films first — by `story_index` in story mode (unnumbered films
after the numbered ones, by date), by date in release mode — then shows by first air date.

**Missing films (15.1.0).** `_group_missing` diffs a group's TMDb collection
(`_tmdb_collection_parts`, in-memory, 12 h) against every item's film id
(`_item_movie_tmdb_id`: the resolved `metadata` binding, else the queued `tmdb_pick`). Nothing
is persisted. A queued film whose metadata hasn't resolved yet still joins its shelf:
`_collection_of` falls back to the parts cache (`_cached_collection_for_movie`).

**`all_seasons` vs `seasons`.** `all_seasons` is every season TMDb knows the show has, taken straight off `/tv/{id}` (so it's free — no extra request) and including season 0. `seasons` only holds the seasons someone actually fetched episode lists for: `_fetch_item_metadata` asks for the seasons present *on disk*, so a show can HAVE season 4 while `seasons` covers 1–3. Keeping "this season exists" separate from "we have its episode list" is what lets the library page show a season you own nothing from (count from `episode_count`) and then fill in its real episode rows once the lazy top-up lands. `/api/tmdb/lookup` (by `tmdb_id` or title) fetches **every** season (in parallel, off the TMDb response cache below), and is the top-up the frontend uses — see [FRONTEND.md](FRONTEND.md) § missing content.

Populated by `_fetch_item_metadata` ([main.py](../main.py)) on first hit of `GET /api/library/{id}/metadata`, then served from cache. Per-id `asyncio.Lock` coalesces concurrent first-loads. **The first-access fetch never blocks the endpoint**: it runs as a background task (`_spawn_metadata_fetch`) the endpoint waits on for ≤4 s before answering `pending:true`; a `metadata_update` SSE event fires when the fetch lands. Cached entries answer instantly regardless of internet state. Force refresh via `POST /api/library/{id}/metadata/refresh` (admin); the same endpoint accepts an optional `{tmdb_id, kind}` to manually bind the item to a TMDb entry when auto-match picks the wrong show.

Each successful fetch also **pre-warms the on-disk artwork cache** (`.tmdb_img_cache/`, `_prefetch_metadata_images`): poster w342, backdrop w1280, season posters w342, episode stills w300 — served to clients via the `/api/metadata/img` proxy (the `img_base` all metadata endpoints now return), so artwork keeps rendering on the LAN when the internet is down.

#### TMDb response cache (16.2.0)

The per-item `metadata` above only covers the seasons that item's files are in. Everything else, including the episode lists of seasons you own nothing from, Search, Explore and the search show page, comes from TMDb when you ask for it. **`tmdbcache.py`** caches every raw TMDb API response on disk in `.tmdb_cache/<k[:2]>/<k>.json` (`k` = SHA-1 of path + sorted params, **excluding `api_key`**, which is never written). It sits inside `_tmdb_get`, so every caller gets it without any change on their side.

- **Fresh:** served with no network call. TTL by kind (`tmdbcache.ttl_for`): a season whose every episode aired >60 days ago, 30 d; an airing or undated season, 12 h; `/tv/{id}` 12 h, or 7 d once `status` is Ended/Canceled (this is where a new season's inventory entry comes from); `/movie/{id}` 12 h within 180 days of release (theatrical-only flags), else 7 d; `/search/*` 24 h; `/genre/*` and `/collection/*` 7 d; trending/discover/popular lists 1 h; anything else 6 h.
- **Stale + refetch fails** (transport error, 429, 5xx): the stale copy is served **at any age**. A 404 returns None as before.
- **Offline backoff:** a transport error sets `_tmdb_offline_until` (30 s). Until it passes, calls go straight to the cache, so a 28-season fan-out doesn't wait out 28 connect timeouts.
- **Explicit refresh bypasses the fresh path:** `_fetch_item_metadata(force=True)` without an override id (the Refresh button, a rename) sets the `_tmdb_fresh` ContextVar. A re-bind to a picked id is allowed to use the cache.
- **Pruned** once per start (`TmdbCache.prune` via `asyncio.to_thread`): entries not rewritten within 180 days, then the oldest beyond 20 000 files, plus orphaned `.tmp` files.

Above it, `_tmdb_lookup_by_id` / `_tmdb_lookup_by_title` memoise the assembled show for 15 min (`_TMDB_LOOKUP_TTL`) and **never memoise an incomplete TV result**, meaning any inventory season missing from `seasons` because its fetch failed (`_tmdb_memo_put`). Season lists are fetched concurrently (`_tmdb_fetch_seasons`, bounded by `_tmdb_net_sem` = 8) over one shared keep-alive client (`_tmdb_http()`, closed in `lifespan`).

**`source` and pinning.** Every cached entry records how it was chosen: `"tmdb"` (auto-matched from the release name), `"picked"` (14.0.0 — the caller supplied the binding at download time via `tmdb_pick`, i.e. the user chose this show from the Smart-search poster list), `"manual"` (a user force-bound a specific TMDb entry after the fact), or `"custom"` (a user hand-entered the fields). `picked`, `manual` and `custom` are **pinned**: `_fetch_item_metadata` returns them unchanged on any non-forced access, and `rename` leaves them intact (see below). Only `tmdb` (or missing-`source`) entries are re-resolved. `custom` entries have **no `tmdb_id`** and use `poster_url`/`backdrop_url` (absolute) instead of TMDb paths; `seasons` is `{}` so episode titles/stills fall back to filename parsing.

**User-facing correction (any profile — not admin-gated, unlike `/refresh`).** Two endpoints back the "Fix metadata" control on the episode-page hero (`openMetaFix()`):
- `GET /api/library/{id}/metadata/search?query=&kind=` — returns TMDb candidate matches (`{id, kind, title, year, overview, poster_path}`) so the user can pick the correct one. Empty `query` derives from the item name; `kind` (`tv`/`movie`/empty=both) filters.
- `POST /api/library/{id}/metadata/set` — `mode:"tmdb"` force-binds the chosen `{tmdb_id, kind}` (stamped `source="manual"`, pulls full episode data, needs a key); `mode:"custom"` stores the hand-entered fields (stamped `source="custom"`, needs no key — the offline / no-good-match path).

The query the auto-match runs is built from `item.series` (or `item.title` for one-offs) by `_search_terms_for_item`, so a badly-named download (e.g. "AOT") can match the wrong show. `POST /api/library/{id}/rename` ([main.py](../main.py)) fixes this: it renames the series across the whole group (or the title for a movie/one-off), **drops the cached `metadata` on every renamed entry (except pinned `manual`/`custom` ones, which are preserved)**, and re-fetches the requested item immediately — the next access of the siblings re-matches lazily. It also re-keys each profile's `series_subtitle_prefs[<series>]` so a remembered subtitle pick survives the rename. Surfaced in the UI by the pencil button on the episode page hero (`renameSeries()`).

When no TMDb API key is configured (env or admin override), the metadata/search/refresh endpoints return `{enabled: false}` and the frontend gracefully falls back to filename parsing — but `metadata/set` with `mode:"custom"` still works, letting users supply metadata by hand with no key.

## Migration ([main.py:77](../main.py#L77))

`_migrate_item` runs on every load. Two migrations:
- **v2.0 → v2.1**: flat `file_path` → `files` list
- **v2.0 → v2.1**: flat per-profile progress (`position_sec`/`duration_sec` at the top level of the profile entry) → `file_progress` keyed by path

- **11.19.0 — season/episode backfill**: any file still sitting at `S0E0` is re-attributed through
  `episodes.attribute_paths` (pass 1 above). Only `(0, 0)` files are touched, so a value the old
  parser got right — or one corrected by hand — is never overwritten. The item's files are re-sorted
  into canonical order afterwards. This is what fixes an existing library **without re-downloading**;
  the absolute-numbering correction (pass 2) then settles on the next metadata access. Because
  `_migrate_item` runs on *every* load (hot loops re-read every few seconds), the check is a plain
  O(n) scan and the regex work only happens while something is actually still unattributed.

The migration is in-place and silent. No version field on items.

### Metadata cache migration: `all_seasons` (11.18.0)

Not part of `_migrate_item` — it needs a network round trip, so it's **lazy and per-item**, in `_fetch_item_metadata`. A cached entry with `tmdb_kind == "tv"` and a `tmdb_id` but **no `all_seasons` key** is treated as stale: the next access re-fetches it **by the existing `tmdb_id`** (never a fresh auto-match, so a wrong binding can't change under the user) and preserves the existing `source`, so a pinned `manual` entry stays pinned. It then writes `all_seasons` and is never re-fetched again.

A merged series never touches `GET /api/library/{id}/metadata`, so `GET /api/library/series/{key}` fires the same self-heal in the background (`_spawn_metadata_fetch` on the member it served metadata from) without delaying its own response. Until either lands, the frontend tops up from `/api/tmdb/lookup`, so the UI is correct on the very first open.

### Derived view: library coverage (11.22.0)

> **A file at `"skip"` is not owned (17.9.0).** Priority 0 means qBittorrent will never
> fetch it, so the bytes are not on disk and no amount of waiting brings them.
> `/api/library/coverage` therefore excludes skip-moded files from `have`/`pending`
> and reports them in a third bucket, **`in_pack`** — missing, but sitting in a torrent
> this box still holds, so one priority write away rather than an indexer hunt. Before
> this, a pack sliced to one episode reported its whole season owned. The rule applies
> to files deselected by hand in the download modal too, which had the same bug.

`GET /api/library/coverage` ([API.md](API.md)) answers "do we already have this, and which episodes" for Search and for the library grid's new-season chip. Nothing is persisted for it — it is computed on every call from fields documented above: `files[].season` / `.episode` / `.bucket` (a bucketed file sits outside the numbered run and never counts as owning an episode), `item["status"]` (`downloading` → `pending`, anything else → `have`), `_series_key` for grouping, `metadata.tmdb_kind` (a `movie` binding switches the diff off entirely — see [GOTCHAS.md](GOTCHAS.md) § A show can be matched as a MOVIE, and the `_movie_binding_is_stale` repair that re-opens one), and `metadata.all_seasons` for the `missing_seasons` diff — which is why the `all_seasons` self-heal above matters to Search as well as to the episode page.

## Concurrency

All access goes through `get_library()` / `put_library()` which hold `_lib_lock`. **Don't read raw `LIBRARY_FILE`** outside that lock — concurrent SSE-driven updates can clobber each other. The lock is created in the FastAPI `lifespan` because `asyncio.Lock()` needs an event loop.

## File location

`LIBRARY_FILE = Path(__file__).parent / "library.json"` — repo root, alongside `main.py`. Auto-created on first save. The schema doesn't need a version field; migrations are detected by missing/legacy keys.

**Writes are atomic** (7.16.3): `_save_lib_raw` writes to `library.json.tmp` then `os.replace`s it over the real file (same directory ⇒ same volume, so the swap is atomic on Windows/NTFS and POSIX). The file is rewritten every ~15 s during playback; the previous in-place `write_text` truncated it first, so a crash / service restart / power cut mid-write left corrupt JSON — and `_load_lib_raw`'s exception fallback then booted with an **empty library** (every profile, watch position, and subtitle pick gone). Keep any future writer on `_save_lib_raw`; never write `LIBRARY_FILE` in place.

**Writes are durable, snapshotted, and never read back as empty** (15.2.2). Atomic alone only survives a *process* crash: on 2026-09-16 a hard reset seconds after a progress save left `library.json` at full size and **every byte zero** (the rename was journaled, the data was still in the OS cache). Three layers now:

- **fsync before replace** — `_write_durable(path, blob)` flushes and `os.fsync`s the temp file before `os.replace`. Use it for any file the user would miss.
- **Rolling snapshots** — after a successful save, `_maybe_snapshot_library` copies the library into `library_backups/library-YYYYMMDD-HHMMSS.json` at most once per 15 min (`_BACKUP_INTERVAL_S`). Pruned to the newest 16 plus the newest per day for 30 days (~45 files x library size). Empty libraries are never snapshotted.
- **Recovery on load** — `_load_lib_raw` treats a missing file as a fresh install, retries transient read errors and **raises** if they persist (an empty dict returned inside `mutate_library()` would be saved). A file that doesn't parse goes to `_recover_library`: it is renamed to `library.corrupt-<ts>.json` (never deleted) and the newest parseable snapshot is written back, logged at CRITICAL. With no usable snapshot it starts empty, but the damaged file is already out of the way, so the first save can't overwrite it.

To restore by hand: stop the service, copy the chosen `library_backups/library-*.json` over `library.json`, start it.

## See also

- [BACKEND.md](BACKEND.md) — `AppState` (the in-memory complement to library.json)
- [ANALYZER.md](ANALYZER.md) — how `skip_data` is generated
- [API.md](API.md) — endpoints that read/write each section

## One-shot repair: background-video watch history

`_purge_background_video_progress()` runs once from `lifespan` (right after
`_recover_interrupted_downloads`). Until 15.6.2 `vlc_progress_tracker` adopted VLC's current
URI as `library_current_file` unconditionally, so during any stream's buffer wait it saved
progress for the **idle background video** against whichever profile started the stream, and
`_finalize_stopped_file` credited that position to the library item on the next file change.
Profiles ended up with resume markers for a file belonging to no item, which `find_resume_hint`
can surface.

The repair deletes `progress[<profile>].file_progress[<path>]` entries whose path is the
configured background video or lies in a `.background` folder, and re-points a `last_file` that
named the clip at the profile's most recently updated remaining file (so Resume keeps working)
rather than blanking it.

It is intentionally **not** a general "drop progress for paths not in `item["files"]`" sweep:
`_canonical_item_path` legitimately re-maps stored paths after a rename or an in-place
compress, and those orphans must survive. It pre-checks outside `mutate_library()` and raises
`LibraryUnchanged` when nothing matched, so the common case never rewrites `library.json`.
