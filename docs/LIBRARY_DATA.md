# `library.json` schema

The only persistent server-side state. Lives at the project root. Accessed via `asyncio.Lock` (`_lib_lock`); never read/write raw from multiple coroutines.

## Top-level

```jsonc
{
  "profiles": [ … ],   // up to 6
  "items":    [ … ],   // library entries
  "settings": {
    "library_paths": [ … ],            // UI-added paths (POST /api/settings/library-paths)
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
      "tmdb_api_key": "abc123"          // overrides .env TMDB_API_KEY; set/cleared in admin UI
    },
    "scheduled_reboot": {               // daily idle-gated host reboot (admin System tab)
      "enabled":      false,
      "time":         "00:00",          // local HH:MM in `timezone`
      "timezone":     "America/Los_Angeles",  // IANA name; "" = system local
      "idle_minutes": 15,               // no usage for this long ⇒ reboot; clamped 1–720
      "last_fired":   ""                // internal: tz date last fired; loop guard, reset on save
    },
    "overnight_prep": {                 // nightly auto stream-prep window (admin System tab)
      "enabled":  false,
      "start":    "02:00",              // local HH:MM in `timezone`; window may cross midnight
      "end":      "06:00",
      "timezone": "America/Los_Angeles",
      "on_end":   "pause"               // "pause" ⇒ hold at window end · "continue" ⇒ run to completion
    },
    "idle_prep": {                      // activity-gated auto stream-prep (admin System tab)
      "enabled":      false,
      "idle_minutes": 30                // idle for this long ⇒ start; any activity pauses + discards in-flight; clamped 1–720
    },
    "play_prep": {                      // auto on-device prep on VLC play (admin System tab)
      "enabled":  true                  // prep the playing episode + playlist tail (interactive; ignores pause gate + activity). Default ON
    },
    "cache_autopurge": {                // auto-evict orphan offline-cache bundles (admin Offline Cache tab)
      "enabled": false,
      "max_gb":  50                     // purge all orphans once total .offline_cache/ size ≥ this many GB; clamped 1–10000
    },
    "subtitles": {                      // unified subtitle policy (admin System tab)
      "default_language": "eng",        // 3-letter code; "" = Any. Absent ⇒ "eng"
      "on_by_default":    false,        // start playback with subs on? (profile may override)
      "auto_search":      true          // fetch a preferred-lang sub online on play when none embedded
    },
    "stt": {                            // AI auto-subtitle generation (admin System tab)
      "enabled":          true,
      "translate":        true          // add an English track for non-English audio
      // default_language is UNIFIED — sourced from settings.subtitles, not stored here
    },
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
  "auto_skip_intro":   true,            // optional; default false
  "auto_skip_credits": true,            // optional; default false
  "resume_mode": "auto|prompt|off",     // default "auto"
  "subtitles_on": true                  // optional; per-profile override of settings.subtitles.on_by_default. absent/null = inherit; true/false = force
}
```

PIN hash is plain SHA-256 of the 6-digit string (no salt). PIN protection is "soft" — anyone with filesystem access can read the JSON. It's a UI gate, not a security boundary. Profiles with a PIN are hidden from the normal profile picker; users select them via the "Log in with PIN" button.

`settings.max_volume`: VLC is uncapped (0–200, where 200 % is overdrive). Capping it system-wide stops anyone from accidentally blowing the speakers. Lives under `settings` because it applies to the physical playback host, not to individual viewers. Enforced server-side in `_global_max_volume`.

`settings.vlc_start_volume`: the volume VLC opens at, expressed as a **% of `max_volume`** (0–100, default 50). At startup the backend computes `round(max_volume * vlc_start_volume / 100)`; the historical behaviour was a hard-coded half-max, which the default preserves. Read via `_global_vlc_start_volume_pct`.

`settings.vlc_night_mode` / `settings.vlc_night_mode_preset`: night mode — VLC's `compressor` audio filter, which narrows the gap between the quietest and loudest sounds so dialogue stays clear at low room volume. `vlc_night_mode` is the on/off flag (default off); `vlc_night_mode_preset` is the intensity (`light` / `medium` / `max`, default `medium`) and is **remembered independently** of the on/off toggle — turning night mode off and back on reuses the same intensity. The on/off toggle is reachable from both the fullscreen-controls moon button and the profile-settings panel; the **intensity picker is settings-menu only** (`POST /api/settings/night-mode` accepts `night_mode` and/or `preset`, merged). Both are global host settings, not per-viewer. **There is no VLC HTTP command to add/remove an audio filter at runtime**, so the compressor is a launch arg (`NIGHT_MODE_PRESETS[preset]`) and changing it relaunches VLC — `_apply_night_mode` snapshots the current file + position, relaunches via `_restart_vlc_process` (which reads `state.vlc_night_mode` + `state.vlc_night_mode_preset`), then replays and seeks back. A preset change *while night mode is off* just persists (no relaunch). `run.py` (`start_vlc`) and `watchdog.py` (`vlc_spec`) read these same settings independently when they launch VLC (boot / crash recovery), so the three `NIGHT_MODE_PRESETS` dicts must stay in sync. Seeded into `state` at lifespan startup and exposed in `state_snapshot`. See [GOTCHAS.md](GOTCHAS.md).

`settings.system_volume_default`: the host's OS mixer volume (0–100) restored when a YouTube-on-TV play stops. Headphones at 100 % can blow eardrums and a movie session shouldn't leave the room loud, so on Stop `_stop_cleanup` calls `set_system_volume(target)` (pycaw on Windows / `osascript` on macOS / `pactl`/`amixer` on Linux). Default 70. Edited via **System Volume After YouTube** in the profile-settings panel (`POST /api/settings/system-volume-default`). **Global** (lives under `settings`, not per-profile). See [YOUTUBE.md](YOUTUBE.md).

`settings.youtube_start_volume`: the host's OS mixer volume (0–100) pre-set the *moment* a YouTube play starts — `youtube_play` calls `set_system_volume(target)` before the `yt_command:load` broadcast and before Chrome paints the kiosk, so the IFrame player can never produce a first audio frame at system max. Default 30. Edited via **YouTube Starting Volume** in the profile-settings panel (`POST /api/settings/youtube-start-volume`). **Global** (lives under `settings`, not per-profile). See [YOUTUBE.md](YOUTUBE.md).

`settings.background_video`: managed by the **Background** admin tab (see [ADMIN.md](ADMIN.md)). The file lives under `.background/<name>` at the repo root; the directory is wiped on each upload so only one file ever exists. The `background_video_loop` task in `main.py` plays it on VLC any time VLC reports `stopped` and a stream pipeline isn't actively buffering. Any user `vlc("in_play", …)` replaces it and restores the user's pre-bg volume.

`settings.scheduled_reboot`: managed by the **System** admin tab (see [ADMIN.md §7](ADMIN.md)). The `scheduled_reboot_loop` task reboots the host daily at `time` (in `timezone`) once it's been idle for `idle_minutes`. `last_fired` is an internal guard (the tz date of the last fire) that stops the just-rebooted machine from re-arming and looping; it's reset to `""` whenever the config is saved so a newly-set time can arm the same day. Lives under `settings` because it applies to the physical host, not an individual viewer.

`settings.overnight_prep`: managed by the **System** admin tab (see [ADMIN.md §7](ADMIN.md)). The unified `auto_prep_loop` task auto-preps every un-prepped library file for on-device streaming during the `[start, end)` window (in `timezone`; the window may cross midnight). At the window end, `on_end == "pause"` lets the in-flight file finish then holds the rest until the next window, while `"continue"` runs to completion. Engagement is tracked in-memory (`state.auto_prep_engaged`), so there's no persisted fire-guard. Lives under `settings` because it applies to the physical host.

`settings.idle_prep`: managed by the **System** admin tab (see [ADMIN.md §7](ADMIN.md)). The activity-gated companion to `overnight_prep`, served by the same `auto_prep_loop` task: any time the host has been idle (`_machine_in_use`) for `idle_minutes`, it queues bulk HLS-prep for the whole un-prepped library; the first sign of activity pauses it and **discards the in-flight encode** (`_pause_prep(kill=True)` — that file restarts from scratch on the next idle stretch, since HLS prep has no mid-file checkpoint). `idle_minutes` is clamped 1–720. Lives under `settings` because it applies to the physical host.

`settings.play_prep`: managed by the **System** admin tab's *Auto-Prep on Play* card; read via `_play_prep_cfg`. **Default ON.** When enabled, every VLC library play preps the playing episode for on-device then the rest of the playlist one episode at a time (`_maybe_start_play_prep` → `_play_prep_chain`, tracked on `state.play_prep_task`). The episode is skipped if resumed with <5 min left (`PLAY_PREP_TAIL_SECS`). Unlike the two triggers above, its jobs are queued **interactive**, so they run regardless of `overnight_prep`/`idle_prep` and live activity (the bulk pause gate and activity-kill don't touch them). Lives under `settings` because it applies to the physical host. See [STREAMING.md § Auto-prep on play](STREAMING.md).

`settings.cache_autopurge`: managed by the **Offline Cache** admin tab's *Auto-Purge Orphans* card; read via `_cache_autopurge_cfg`. **Default OFF.** The `cache_autopurge_loop` task re-checks every 5 min: when `enabled` and the total `.offline_cache/` size is at/above `max_gb` GB, it deletes every orphan bundle (the same set the manual "Purge All Orphans" clears — cache/partial dirs + legacy MP4s that no longer map to a live library file). Bundles backing current library files are never touched, and active prep jobs are skipped, so it can only reclaim already-safe space. `max_gb` is clamped 1–10000. The last run's `{deleted, bytes_freed, …}` is held in-memory on `state.cache_autopurge_last` (not persisted). Lives under `settings` because it applies to the physical host's disk. See [ADMIN.md](ADMIN.md).

`settings.autoupdate`: managed by the **Updates** admin tab (see [ADMIN.md §8](ADMIN.md)). Drives the `updater_loop` background task + the `/api/admin/updater/*` endpoints. `branch` is sanitised on read (`_autoupdate_cfg`): when `dev_mode` is false a non-canonical value snaps back to `main`; when `dev_mode` is true any structurally-valid branch name survives (so a developer can pin a feature branch). All branch operations route through `updater.branch_allowed(branch, allow_any=dev_mode)`. Lives under `settings` because the updater acts on the whole host install, not an individual viewer.

`settings.subtitles`: managed by the **System** admin tab's *Subtitles* card; read via `_subs_cfg`. The single source of truth for the preferred subtitle language. `default_language` is a 3-letter code (`_canon_lang`-normalized) or `""` (Any). **Migration / defaults:** if the `subtitles` block is absent it's seeded from the legacy `settings.stt.default_language` when that's set, else `"eng"` — so an unconfigured box defaults to English; once the block exists its value is used verbatim (so an admin who picks "Any" → `""` keeps it). `on_by_default` is whether playback starts with subs on (default false; a profile's `subtitles_on` overrides). `auto_search` lets playback fetch a preferred-language sub from OpenSubtitles when none is embedded (default true). This is the central subtitle setting: `_stt_cfg` re-sources its `default_language` from here, the search endpoint defaults to it, and `_apply_subtitle_policy` selects tracks by it.

`settings.stt`: managed by the **System** admin tab's *Auto-Generated Subtitles (AI)* card. Gates AI auto-subtitle generation (whisper.cpp): `enabled` + `translate` (adds an English-translated track for non-English audio). The **preferred language is unified** — `_stt_cfg.default_language` is read from `settings.subtitles` (above), not stored here. Consumed by `_needs_stt_subs` / `_ensure_stt_for`. See [STT.md](STT.md).

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
  "download": { /* download schedule; see below */ },
  "progress": { /* per-profile; see below */ },
  "admin_only": false,                  // optional; hides from non-elevated profiles
  "default_visible_profiles": [],       // optional; if non-empty, only these profile IDs see item by default
  "hidden_by_profiles": [],            // optional; profile IDs that personally hid this item
  "skip_data": { /* per-file; see below */ },
  "metadata": { /* optional; TMDb cache — see below */ }
}
```

### `download` (download schedule)

```jsonc
"download": {
  "mode": "now",                 // "now" = download anytime · "idle" = only during idle/night
  "files": {                     // per-file overrides (by absolute path); inherit `mode` if absent
    "/abs/path/S01E01.mkv": "high",   // download now, first
    "/abs/path/S01E09.mkv": "idle",   // only during the idle/night window
    "/abs/path/extras.mkv": "skip"    // never download
  }
}
```

Controls **when** an item's torrent (and individual files) download. The
`download_scheduler_loop` task ([main.py](../main.py)) is the **single source of
truth** that translates this model into live qBittorrent file priorities + torrent
pause/resume every 15 s — never write qBit `filePrio`/`pause` for a scheduled item
outside the reconcile path or the next tick reverts it (see
[GOTCHAS.md](GOTCHAS.md)). Effective per-file schedule = `files[path]` if present,
else `mode`. Mapping to a qBit priority depends on whether the idle/night DOWNLOAD
window is open right now (`_download_idle_open`, which **reuses** the admin
`overnight_prep` window + `idle_prep` idleness — see [ADMIN.md](ADMIN.md)):

| effective mode | qBit priority |
|----------------|---------------|
| `skip` | 0 (never) |
| `now`  | 1 (download now) |
| `high` | 7 (download now, first) |
| `idle` | 1 when the idle window is open, else 0 |

If no managed file should download right now, the torrent is paused (`qbit_pause`,
with a 5.x `/stop` fallback); resumed when something becomes eligible. The
item-level **Pause** (`mode=idle`) / **Resume** (`mode=now`) sweep `now`/`high`↔`idle`
on the per-file overrides too, leaving explicit `skip` choices alone. Written by
`POST /api/library/{id}/download-schedule` + `/file-schedule`, the download modal's
"Download at idle/night only" toggle (`DownloadReq.download_mode`), and
`library_download_pipeline` (which seeds `skip` for files the picker deselected).
Missing/legacy items read as `{mode: "now", files: {}}` (plain anytime download).

### File

```jsonc
{
  "name": "The.Boys.S01E01.mkv",
  "path": "/abs/path/to/file.mkv",       // canonical for matching against VLC playlist
  "size_bytes": 1329062039,
  "season": 1,
  "episode": 1
}
```

Season/episode are extracted by `parse_season_episode()` ([main.py:807](../main.py#L807)) — matches `S01E03`, `s2e5`, `1x03`. Returns `(0, 0)` if no match.

### Progress (per profile)

```jsonc
"progress": {
  "<profile_uuid>": {
    "last_file": "/abs/path/last/file.mkv",
    "file_progress": {
      "/abs/path/file.mkv": {
        "position_sec": 1234.5,
        "duration_sec": 4174.0,
        "completed": false,                // true when pct > 0.92
        "updated_at": "2026-05-13T02:47:57+00:00",
        "audio_track": 3,                  // optional; VLC ES ID
        "subtitle_track": -1               // optional; -1 = off
      }
    }
  }
}
```

Written by `vlc_progress_tracker` every 15 s. `mark_watched` ([main.py:2221](../main.py#L2221)) writes `completed: true` with `position_sec = duration_sec` (or 0 for unwatched). Track preferences are saved by `_save_track_pref` ([main.py:944](../main.py#L944)) whenever the user picks an audio/subtitle track. They're re-applied on next playback of the same file by `_apply_track_prefs` (with a 2 s delay so VLC has opened the file).

### `skip_data` (per file)

```jsonc
"skip_data": {
  "/abs/path/file.mkv": {
    "intro": { "start": 12.0, "end": 105.0 },    // or null
    "credits_start": 2940.0,                     // or null
    "analysis": {
      "version": 2,                              // analyzer.ANALYZER_VERSION
      "source": "auto" | "auto-blackframe" | "auto-fallback" | "manual" | "failed",
      // Only present when source == "failed":
      "error_code": "no_binary" | "file_missing" | "no_duration" |
                    "fp_empty"  | "too_short"    | "no_skip_points" | "exception",
      "error":      "Human-readable detail surfaced in the admin editor + log."
    }
  }
}
```

`source="manual"` entries are never overwritten by re-runs. Entries with `analysis.version < ANALYZER_VERSION` are eligible for re-analysis. `source="failed"` entries are also retried on the next ready-flip in the series — when a new sibling episode arrives, the larger fingerprint pool can unlock a previously-failed file. See [ANALYZER.md](ANALYZER.md#failure-tracking) for the full table of error codes and how the failure is surfaced to users + admins.

### `metadata` (TMDb cache, optional)

```jsonc
"metadata": {
  "tmdb_id":       12345,
  "tmdb_kind":     "tv" | "movie",
  "title":         "Monster",
  "overview":      "...",
  "poster_path":   "/abc.jpg",            // join with /api/library/{id}/metadata → img_base
  "backdrop_path": "/xyz.jpg",
  "first_air_date":"2004-04-07",          // tv only
  "release_date":  "1999-09-30",          // movie only
  "vote_average":  8.7,
  "genres":        ["Drama", "Mystery"],
  "seasons": {                             // tv only
    "1": {
      "name":     "Season 1",
      "overview": "...",
      "poster_path": "/season1.jpg",
      "episodes": [
        {"season": 1, "episode": 1, "name": "Herr Dr. Tenma",
         "overview": "...", "still_path": "/...jpg",
         "air_date": "2004-04-07", "runtime": 24}
      ]
    }
  },
  "fetched_at": "2026-05-15T01:23:45+00:00"
}
```

Populated by `_fetch_item_metadata` ([main.py](../main.py)) on first hit of `GET /api/library/{id}/metadata`, then served from cache. Per-id `asyncio.Lock` coalesces concurrent first-loads. Force refresh via `POST /api/library/{id}/metadata/refresh` (admin); the same endpoint accepts an optional `{tmdb_id, kind}` to manually bind the item to a TMDb entry when auto-match picks the wrong show.

When no TMDb API key is configured (env or admin override), the endpoint returns `{enabled: false}` and the frontend gracefully falls back to filename parsing.

## Migration ([main.py:77](../main.py#L77))

`_migrate_item` runs on every load. Two migrations:
- **v2.0 → v2.1**: flat `file_path` → `files` list
- **v2.0 → v2.1**: flat per-profile progress (`position_sec`/`duration_sec` at the top level of the profile entry) → `file_progress` keyed by path

The migration is in-place and silent. No version field on items.

## Concurrency

All access goes through `get_library()` / `put_library()` which hold `_lib_lock`. **Don't read raw `LIBRARY_FILE`** outside that lock — concurrent SSE-driven updates can clobber each other. The lock is created in the FastAPI `lifespan` because `asyncio.Lock()` needs an event loop.

## File location

`LIBRARY_FILE = Path(__file__).parent / "library.json"` — repo root, alongside `main.py`. Auto-created on first save. The schema doesn't need a version field; migrations are detected by missing/legacy keys.

## See also

- [BACKEND.md](BACKEND.md) — `AppState` (the in-memory complement to library.json)
- [ANALYZER.md](ANALYZER.md) — how `skip_data` is generated
- [API.md](API.md) — endpoints that read/write each section
