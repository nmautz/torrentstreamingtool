# Smart Skip (`analyzer.py` + orchestration in `main.py`)

Audio-fingerprint-driven intro/credits detection. Runs per-series; results stored as `skip_data` on each item.

## Dependencies

- **`ffmpeg`** — audio decode + ffprobe duration + blackdetect fallback
- **`fpcalc`** (chromaprint) — fingerprinting (`-raw` mode emits integer frames)
- Both are detected by `setup.py` and stored as `_FFMPEG_BIN` / `_FPCALC_BIN` in `.env`
- `analyzer.is_available()` returns False if either is missing — feature degrades to manual entry only (admin editor still works)

## Algorithm overview

Chromaprint emits ~7.8 32-bit hash frames per second of audio.

1. **Fingerprint** ([analyzer.py:69](../analyzer.py#L69)): For each episode, call `fpcalc -raw -length 360 <path>` for the head (first 6 min) and `ffmpeg -ss <tail_start> -t 600 | fpcalc -raw -length 600 -` for the tail (last 10 min)
2. **Greedy clustering** ([analyzer.py:307](../analyzer.py#L307)): pick first un-clustered episode as anchor; pairwise `_find_longest_match` against every other. The longest run ≥ `MIN_MATCH_FRAMES` (~15 s) with Hamming distance ≤ 6 bits per frame is kept. Unmatched episodes recurse on the next pass (new anchor)
3. **Intersection** ([analyzer.py:209](../analyzer.py#L209)): within a cluster, the anchor's intro/outro range is the intersection of anchor-side windows across all pair matches. Per-non-anchor episodes use the `offset_in_other` from their pair match — so cold opens of different lengths still align correctly
4. **Credits fallback chain** ([analyzer.py:480](../analyzer.py#L480)):
   - Outro cluster matched → `credits_start = tail_start + frames_to_seconds(offset)`, `source="auto"`
   - No outro cluster → `_detect_blackframe` scans the last 5 min for the first ≥0.5 s black segment, `source="auto-blackframe"`
   - Nothing → `credits_start = duration * 0.92`, `source="auto-fallback"` (matches the 92 % completion threshold so progress and credits agree)

## Constants ([analyzer.py:22](../analyzer.py#L22))

| Name | Value | Meaning |
|------|-------|---------|
| `ANALYZER_VERSION` | 2 | Bumped to force re-analysis when the algorithm changes |
| `FP_FRAMES_PER_SEC` | 7.8 | Chromaprint's emission rate |
| `INTRO_SEARCH_SECS` | 360 | Look for intro in first 6 min |
| `OUTRO_SEARCH_SECS` | 600 | Look for outro in last 10 min |
| `MIN_INTRO_SEC` | 15 | Smallest segment we'll call an intro |
| `MAX_INTRO_SEC` | 180 | Cap to avoid runaway matches |
| `MIN_OUTRO_SEC` | 15 | Same for credits |
| `MAX_OUTRO_SEC` | 180 | |
| `FRAME_HAMMING_MAX` | 6 | ≤6 bits differ in a 32-bit hash → "same" frame |
| `MIN_MATCH_FRAMES` | int(15 × 7.8) | Minimum consecutive frames for a match |
| `CREDITS_FALLBACK_PCT` | 0.92 | Time-based fallback when no outro is found |

## Greedy clustering ([analyzer.py:307](../analyzer.py#L307))

The greedy approach handles three failure modes the original single-anchor approach couldn't:
- **Specials/OVAs** mixed into the torrent — they match nothing, form no cluster, get no false intro skip
- **Mid-season intro changes** — eps with the new opening drop out of the first cluster and form their own on the second pass
- **Episode 0 is a special** — first pass finds an empty cluster and moves on; the real intro group still gets detected from ep 1+

## Concurrency

- One series at a time within a series — `lock_for_series(key)` returns a per-series `asyncio.Lock`
- Different series can run in parallel (different locks)
- All blocking work (subprocess calls) goes through `asyncio.to_thread` so the event loop stays responsive

## Progress reporting

`analyze_series(items, progress_cb)` invokes `progress_cb(stage, current, total, message, episode_name)` at each step. `main.py`'s `_set_analysis_status` broadcasts these as SSE `analysis_status` events. Stages: `starting` → `fingerprinting` → `matching-intros` → `matching-outros` → `finalizing` → `done`.

## Trigger flow (in `main.py`)

1. `library_download_monitor` flips an item to `status="ready"`
2. Calls `_schedule_series_analysis_if_eligible(item, lib)` ([main.py:1320](../main.py#L1320))
3. Checks: analyzer available? AND at least one file in this series bucket still needs analysis (no `skip_data`, or stale `analysis.version`)? AND user did NOT manually mark it (manual entries are never overwritten)
4. If yes, `asyncio.create_task(_run_series_analysis(key))`
5. `_run_series_analysis` ([main.py:1244](../main.py#L1244)) acquires the lock, calls `analyzer.analyze_series`, writes results back into `library.json` under each item's `skip_data`, broadcasts `analysis_status`

## `_series_key` ([main.py:1192](../main.py#L1192))

- Items with non-empty `series` field → `series:<lowercased>` (cross-episode matching)
- Empty `series` → `item:<id>` (movies / one-offs get their own singleton bucket → credits fallback only)

## Runtime offer logic ([main.py:1355](../main.py#L1355))

`vlc_progress_tracker` runs every 2 s. For the current file:
1. Look up `_find_file_meta(item, file_path)` from `skip_data`
2. **Intro window**: if `start - 2s ≤ pos < end` — with auto-skip on (profile pref + still has >1 s left) start the **intro countdown** (see below); otherwise set `state.skip_offer = {type:"intro", end_at, file_path}` and broadcast
3. **Credits window**: if `pos ≥ credits_start - 2s` and not at the very end — with auto-skip on (profile pref + `pos ≥ credits_start`) start the **credits countdown**; otherwise set `state.skip_offer = {type:"credits", credits_start, file_path, has_next, next_file_path}`
4. **Outside any window** → clear offer

The `SKIP_PREROLL_SEC = 2.0` ([main.py:1352](../main.py#L1352)) gives the user 2 s of visual time to react before the range starts.

`state.skip_offer_file` carries the file path while an offer is active. After acting/dismissing, it gets a `#intro-done` / `#credits-done` suffix so the same offer doesn't re-emit on the next tick.

## Auto-skip countdown (on-TV marquee)

When auto-skip is enabled, Smart Skip does **not** cut instantly — it counts down on the TV over the `lead` seconds *before* the skip point, then acts the moment playback reaches it, so the intro/credits is skipped in full. Leads: `SKIP_COUNTDOWN_INTRO_SEC = 5`, `SKIP_COUNTDOWN_CREDITS_SEC = 10`. The skip **point** (`target`) is the **intro start** (skip → `seek` to intro end+1) and the **credits start** (skip → `vlc_next_file`, else `pl_stop`). So an intro at 1:30 counts 5→1 from 1:25 and seeks at 1:30.

- `_maybe_emit_skip_offer` calls `_start_skip_countdown(kind, item, file_path, end_at, target, lead)` once the position enters `[target − lead, …)`. While `state.skip_countdown_task` is alive, the helper early-returns so the tracker doesn't fight it. (The manual, auto-skip-off button still uses the narrower `[target − SKIP_PREROLL_SEC, …)` window.)
- `_run_skip_countdown` is a dedicated coroutine that is **position-driven** (polls `vlc_status` every 0.5 s). The displayed number is `ceil(target − pos)` clamped to `[1, lead]`, so it tracks real playback — it **freezes while paused** (pos is frozen) and grows/shrinks as the viewer seeks; updates marquee + broadcasts `state` (`state.skip_countdown = {type, file_path, n}`) only when the number changes. It **fires** when `pos ≥ target` and **aborts** (clearing the popup) if the file changes, the viewer seeks back so `target − pos > lead + preroll`, or — for an intro — seeks past the intro end (`pos ≥ end_at`). On fire it re-checks the live playlist URI, performs the skip, and sets the `#…-done` marker. The `finally` always clears the marquee file.
- `_cancel_skip_countdown()` cancels the task and clears the popup; it's called from Stop / Next / Prev / a new Play, and as a backstop in the tracker's playback-ended branch.

### Deferred "watched" when the credits guess is wrong

Smart Skip's `credits_start` can land **early** (a `auto-fallback` at `duration*0.92`, or an outro cluster that matched real content). To make a wrong-early guess harmless, advancing to the next episode (from **any** position) does **not** mark the episode watched immediately — `_arm_credit_skip_watch` arms a `CREDIT_SKIP_WATCH_DELAY_SEC = 60` grace timer (`state.pending_watch`). If the viewer realises they skipped real content and returns to that file, `vlc_progress_tracker` cancels the timer and their real progress stands; otherwise `_mark_file_watched_internal` marks it `completed`. Armed from the **Next** button (`/api/vlc/next`), the credits **Skip** offer (`/api/skip-now`), and the auto-skip-credits countdown — i.e. every path that jumps to the next episode. See [LIBRARY_DATA.md § Progress](LIBRARY_DATA.md#progress-per-profile).

### How the popup reaches the TV

The popup is a VLC **`marq` sub-source**, not dashboard UI — it draws on the video output itself. VLC is launched with `--sub-source=marq --marq-file=<repo>/.vlc_marquee.txt --marq-refresh=200 --marq-position=10 …` (bottom-right, opaque white text, padded). VLC re-reads the file ~5×/s; `main.py` writes the countdown text into it (`_marquee_write` / `_vlc_marquee`, atomic `os.replace`) and empties it to clear. The launch args live in three places that must stay in sync — `main.py` `_vlc_marquee_args()`, `run.py` `start_vlc`, `watchdog.py` `vlc_spec`. See [GOTCHAS.md](GOTCHAS.md#smart-skip-countdown-marquee).

## Endpoints

User-facing:
- `POST /api/skip-now {type}` — execute. Intro = seek to `end_at + 1`. Credits = `vlc_next_file` (or `pl_stop`)
- `DELETE /api/skip-now` — dismiss without acting

Admin:
- `GET /api/admin/library/{id}/skip-data` — per-file editor data (now also returns `error_code` / `error` for failed files)
- `PATCH /api/admin/library/{id}/skip-data` — manual override (sets `analysis.source="manual"`)
- `POST /api/admin/library/{id}/analyze` — force re-run for the item's series
- `GET /api/admin/analyzer-status` — `{available, ffmpeg, fpcalc}`
- `GET /api/admin/analyzer-log?limit=N` — ring buffer of fingerprint events; each entry is `{ts, level, series_key, item_id, file_path, error_code, message}`

## Skip data shape (stored per item)

```jsonc
"skip_data": {
  "<absolute file path>": {
    "intro": { "start": 12.0, "end": 105.0 },     // or null
    "credits_start": 2940.0,                       // or null
    "analysis": {
      "version": 2,
      "source": "auto" | "auto-blackframe" | "auto-fallback" | "manual" | "failed",
      // Only present when source == "failed":
      "error_code": "no_binary" | "file_missing" | "no_duration" |
                    "fp_empty"  | "too_short"    | "no_skip_points" | "exception",
      "error":      "Human-readable message describing why fingerprinting failed."
    }
  }
}
```

## Failure tracking

`analyze_series` always returns an entry per input file — successes carry the
`intro` / `credits_start` shape above, failures carry `analysis.source ==
"failed"` with an `error_code` and `error` message. The orchestrator in
`main.py` persists every entry (success or failure) into `library.json`, then
emits one `_log_analyzer_event` per failure into `state.analyzer_log` (200-deep
in-memory ring buffer). Three downstream consumers read this:

1. **User-facing chip** — `/api/library` now returns a per-item `skip_status`
   summary (`"ok"` / `"partial"` / `"failed"` / `"pending"` / `"none"`).
   `static/index.html` renders an amber "⚠ Intro/credits skip not available"
   chip next to the title when `skip_status == "failed"`, and a softer
   "Skip partial" chip when only some files in a multi-file item failed.
2. **Admin editor** — `/api/admin/library/{id}/skip-data` now carries
   `error_code` and `error` per file. The Smart Skip tab's editor shows a
   red error block above the time inputs so the admin can see exactly why
   the file failed.
3. **Admin log panel** — `/api/admin/analyzer-log` returns the ring buffer
   (newest first). The Smart Skip tab renders a scrolling log under the item
   list with timestamp · error_code · file basename · message per row,
   refreshing whenever an `analysis_status` SSE event lands a terminal status.

Error codes (defined as constants at the top of `analyzer.py`):

| `error_code`       | Cause | Typical fix |
|--------------------|-------|-------------|
| `no_binary`        | ffmpeg or fpcalc missing on host | Re-run `setup.py` after installing the dep |
| `file_missing`     | Path exists in library.json but not on disk | Library scan / clean orphans |
| `no_duration`      | ffprobe couldn't read the container | Re-encode or remux; check codec support |
| `fp_empty`         | fpcalc produced no fingerprint for the head | Unsupported audio codec, silent track, corruption |
| `too_short`        | < 60 s — fallback heuristic is meaningless | None (expected for trailers, recap clips) |
| `no_skip_points`   | Fingerprinted but no cluster and no black frame | Often resolves when more peers in the series arrive |
| `exception`        | Unhandled error inside `analyze_series` | See `streamlink_app.log` for the traceback |

`_schedule_series_analysis_if_eligible` treats `source == "failed"` as
eligible for re-analysis on the next ready-flip in the series, so a new
sibling arriving can unlock a previously-failed file without an admin click.
Manually-edited entries (`source == "manual"`) are still never overwritten.

## Frontend

`renderSkipOffer(offer)` displays a fixed-position amber tile at the bottom of the viewport (above the player footer / fullscreen controls). Renders whenever `state.skip_offer` is non-null. The label is "Skip intro" or "Skip credits" depending on `offer.type`. `triggerSkip()` POSTs `/api/skip-now`; `dismissSkip()` DELETEs.

Per-profile `auto_skip_intro` / `auto_skip_credits` toggles live in the profile-settings modal (gear icon next to the navbar avatar).

## See also

- [BACKEND.md](BACKEND.md) — `vlc_progress_tracker` and `_maybe_emit_skip_offer`
- [LIBRARY_DATA.md](LIBRARY_DATA.md) — full skip_data schema in context
- [ADMIN.md](ADMIN.md) — the Smart Skip admin tab
