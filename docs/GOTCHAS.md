# Gotchas

Non-obvious behaviours and footguns. Read before changing anything load-bearing.

## VLC

### VLC can't open Windows paths ≥ 260 chars — build every input MRL with `vlc_file_uri()`

qBittorrent writes files at any path length (it uses `\\?\` extended-length paths internally), but VLC opens its inputs with the plain Win32 API, which hard-fails past MAX_PATH (259 usable chars). The symptom is maddening: one episode in a pack throws *"unable to open the MRL"* while its siblings play fine, the file exists on disk, and nothing is in any log — the difference is just an extra-long filename (multi-part finales are the classic case; the Avatar S03E18-E21 four-parter is 271 chars total). The fix is `vlc_file_uri()` ([main.py](../main.py)): identical to `Path.resolve().as_uri()` except that on Windows an over-259-char resolved path is swapped for its **8.3 short form** (`GetShortPathNameW`) before URI-encoding. Never call `.resolve().as_uri()` directly for a VLC input. Round-tripping is safe because every consumer that maps VLC's reported URI back to a library path (`_vlc_wait_until_ready`, `_canonical_item_path`, the bg-video check, `vlc_progress_tracker`) compares via `Path.resolve()`, which expands 8.3 names back to the canonical long path — so progress keys, skip data, and resume hints never see the short form. Caveat: if 8.3 name generation is disabled on the volume (`fsutil 8dot3name query`), there's no short form; the helper logs a warning and falls back to the long URI, which VLC will still fail on — the real cure then is enabling 8.3 names or shortening the filename.

### Track IDs are ES IDs, not 1/2/3 counters

VLC's `audio_track` / `subtitle_track` commands accept **elementary stream IDs** — the number N in each `"Stream N"` key of `vs.information.category`. Using sequential per-type counters (1, 2, 3 for audio; 1, 2, 3 for subs) sends the wrong ID and the command silently does nothing. The `<audiotrack>`/`<subtitletrack>` values in the XML status are also ES IDs, so the "current" highlight in the UI dropdown only works if they're compared as ES IDs.

See `get_tracks()` ([main.py:2799](../main.py#L2799)) — `es_id = int(key.split()[-1])`.

### VLC track *labels* must include the title (`Description`), and the sub menu must match the device

Two ways the VLC subtitle menu silently diverged from the on-device menu — both fixed, keep them fixed:

1. **Label the track by its title, not just its language.** VLC's `status.json` exposes the container track title (an MKV's `title` tag — e.g. "Full Subtitles" vs "Signs/Songs") in the per-stream **`Description`** field, but *only* when the track actually carries one (untitled tracks have no `Description` key at all). The old `_parse_track_streams` ignored it and labelled by `Language` alone, so two English subs both rendered as bare "English" — while the on-device player (which labels from ffprobe titles via `_track_label`) showed the real names. `_vlc_es_label(lang, title, codec, fallback)` now builds `"<Language> (<title>)"` exactly like `_track_label`, reading `Description` as the title. Keep the two label builders in sync.
2. **Load every sidecar even when subs default OFF.** The on-device menu lists *all* discovered subs (bundle + `_list_sidecar_subs`) regardless of the on/off default; VLC only loaded sidecars (`_load_all_local_subs`) on the subs-*on* / saved-pick branches, so with the admin/profile default off the VLC menu showed **only embedded tracks** and was missing the `Subs/`-folder, downloaded, and AI sidecars (the "device has 4 sub options, VLC has 2" report). `_apply_subtitle_policy` now loads all sidecars in the subs-off branch too, then forces the selection to `-1` (`addsubtitle` auto-enables the last one added). Costs a few `addsubtitle` round-trips per play even when subs stay off — accepted for menu parity.

### VLC 3.x has no current-track in status

`status.xml` / `status.json` don't include `<audiotrack>` or `<subtitletrack>` in VLC 3.x. We track it ourselves in `state.current_audio_track` / `state.current_subtitle_track`, reset to `-1` on every new `in_play`. The `POST /api/vlc/track/*` endpoints update this state.

### End-of-media must be detected explicitly — nothing else returns the dashboard to idle

`stream_status` only ever went back to `"idle"` via an explicit `POST /api/stop`, a YouTube close, or the stream pipeline. **Nothing watched for the media simply ending.** So when a movie — or the last episode of a playlist — finished, AppState kept reporting `playing` with the old title while VLC sat on a dead playlist entry, and the dashboard showed `PLAYING <title> — 0:00 / 0:00` indefinitely (VLC reports `time=0` *and* `length=0` at EOF).

It was self-sustaining, which is why it never recovered. `background_video_loop` would have put the idle video back within ~3 s, but `_play_background_video()` refuses to stomp VLC while `_real_playback_active()` is true — and that reads `stream_status` and `library_item_id`, **the very fields only `_play_background_video()` clears**. The loop retried every 3 s forever and could never win. Only a manual Stop (or starting something else) broke it.

`_handle_playback_ended()` ([main.py](../main.py)) is the explicit detector, and there are three things to keep right about it:

1. **It runs before every other guard in `background_video_loop`.** Returning to idle is not a background-video concern — it has to happen with no bg video configured, one disabled, window control paused, or the TV UI holding the screen. Those are exactly the setups with no bg video to recover them, so gating the detector behind the bg-video checks would leave the stall in place for them. Don't move it below them.
2. **It can't use `_real_playback_active()` to decide it's safe to act** — that would be circular, since the stale fields it must clear are what that function reads. `_play_handoff_in_flight()` is the narrower test: YouTube, `buffering`, or a live `library_play_task` / `stream_task`. Those are genuinely in motion and must never be treated as "ended".
3. **Two consecutive stopped polls (~6 s) are required.** VLC briefly reports not-playing during an in-playlist auto-advance (the same transition `vlc_progress_tracker` guards against — see [§ A progress save's position and file must come from the same instant](#a-progress-saves-position-and-file-must-come-from-the-same-instant--dont-straddle-an-auto-advance)). One stopped tick is not the end; dropping the counter re-introduces "playback stops itself between episodes".

**Crediting the finished file needs the remembered position, not the live one.** `_finalize_stopped_file` ignores a zero duration, and VLC has already zeroed both by the time EOF is observed — so before this fix nothing was ever marked watched by finishing it normally, only by the 15 s periodic saver happening to land near the outro. `stat_broadcaster` keeps `state.last_play_pos` / `last_play_dur` / `last_play_file` updated whenever VLC is genuinely mid-playback, and the EOF path finalises against those. If you add another playback surface, keep that trio fresh or its files will never complete.

### Cross-item series playback — the path→item map is the source of truth mid-playlist, not `state.library_item_id`

A show whose episodes were **downloaded individually** is many separate library items (each a 1-file torrent) that share one `series` string. The library collapses them into one tile and plays them as a single playlist that **spans items** — but almost everything in the playback core (progress save, skip offers, track prefs, next/prev natural-order, auto-advance) historically re-derived its context from the single global `state.library_item_id`. Crossing an episode boundary would otherwise write the next episode's progress under the *first* episode's item and rebuild next/prev from the wrong file list.

The fix keeps `state.library_item_id` pointed at the item that owns **the file VLC is actually playing**. `POST /api/library/{item_id}/play` accepts an optional `items[]` parallel to `files[]`; when present it stores `state.library_series_map` (path→item_id) and `state.library_series_order` (the full, stable cross-item order — the analogue of `library_shuffle_order`, and what `_nav_order`/`_item_all_paths` return so prev/next/credits-advance span items). Each `vlc_progress_tracker` tick, `_series_owner_for_path(current_file)` is checked against `state.library_item_id`; on a change, `_series_finalize_and_switch` finalizes the outgoing episode under its *own* item and re-points `library_item_id` (and clears `track_pref_applied_file` so the new episode's saved audio/subs re-apply). Because every downstream consumer still reads `library_item_id`, they all "just work" once it's re-pointed — don't reintroduce a hard-coded item lookup on the advance path.

**On the device (HLS `lp` player) the same trick applies:** `lp.playlistItems` (parallel to `lp.playlist`) holds each file's owner, and `_lpLoadIndex` — the single choke point every load/advance/prev/next routes through — re-points `lp.itemId` to `playlistItems[pi]` before it preps/streams/saves. Because the device's per-current-file calls (`/offline-prepare`, `/stream-ondemand`, `/progress`, saved-tracks, `/skip-data`, all `offKey(lp.itemId, path)`) reference `lp.itemId` + `lp.filePath` together, re-pointing the one id keeps `file ∈ item` valid as playback auto-advances across episode-torrents. The few sites that touch a *non-current* file resolve the owner explicitly from `lp.playlistItems[idx]`: `_lpWarmNextEp` (warm the next episode under its own item), the Prev/Next nav-dot painter, and the app's offline auto-manage window. Handoffs carry the map both ways — `lpHandoffToVlc` passes `lp.playlistItems.slice(pi)` as `/play`'s `items[]`; `handoffToDevice` rebuilds `items` from the `library_series_map` published in `state_snapshot`. `pcChoose("local")` hands `lp` the full cross-item playlist + `items`.

**Remaining device gap:** the iOS-app *offline bundle* prefetch-ahead (`_appAutoManage`, `_cap.dl`) stays **per-item** — it pre-caches only same-item ahead episodes for airplane mode (foreign-item ahead paths are skipped, pre-cached by that item's own pass). Streaming auto-advance is unaffected; only pre-download-for-offline doesn't span items.

**Never gate cross-item UI on `library_item_file_count`.** It is the *current item's* file count, which for a show downloaded one episode per torrent is **1** — for every episode of a twenty-episode run. That is what hid the dashboard's Prev/Next episode buttons on exactly the shows that need them most (11.19.1): the backend navigation was correct the whole time, the button's visibility test was asking the wrong field. Ask `library_nav_count` (published by `state_snapshot` from the same shuffle → series → `library_nav_order` precedence `_nav_order` uses) instead. For the same reason, don't take a position from `library_playlist` / `library_current_index`: `vlc_next_file` rebuilds `library_playlist` as `all_paths[idx:]`, so the current index is permanently `0` and the count falls by one per episode — use `library_nav_index` / `library_nav_count`.

Clearing: `library_series_map`/`library_series_order` are set fresh on every play (empty for a normal single-item play, and by Shuffle Play which owns navigation) and cleared in every full playback-state reset — but **not** in `library_unshuffle` (leaving shuffle keeps the series active).

### Search-result grouping keys on the show name *before* the first S/E marker

`parse_torrent_title` ([main.py](../main.py)) extracts a result's show name from the substring **before** the first structural marker (`S01E03`, `S01`, `S01-S05`, `Season 2`, `Complete Series`, an absolute-ep marker, …), then strips release tags. This is deliberate: a torrent titled `Better Call Saul S06E11 Breaking Bad …` or `Moonshiners S15E12 Braking Badly …` matches a "breaking bad" search but is a *different show* — cutting at the first marker yields "Better Call Saul" / "Moonshiners" so they group into their own cards instead of polluting the searched show.

Robustness rules baked in (all learned from real indexer titles — **validate any regex change against live results, not synthetic ones**):
- **Multi-season packs**: chained list `S01-S02-S03`, `+`/`&`-lists `S1+S2+S3` / `S01&02`, and the word form `(Season 1 + 2)` are preferred over the 2-season range so the full span is captured. Every range/list's *second* number carries a trailing `(?!\d)` so a **resolution/year can't bleed in** — `S01 - 720p` is NOT "S1–72", `S08 … - 2019` is NOT "S8–20". `_tt_bounded_span` then rejects implausible spans (max season >50, or span >30).
- **Sequel-season fansub episodes**: `[SubsPlease] Show S2 - 05` / `Show Season 2 - 05` — a **spaced dash + 2+-digit second number with no S prefix** is season 2 *episode* 5, not a seasons-2-to-5 pack (`_TT_SEASON_EP_DASH_RE`, checked before the multiseason ranges; real packs write `S01-S05`/`S01-05` tight or plural `Seasons 1-5`). Likewise the **ordinal form** `Sousou no Frieren 2nd Season - 10` (`_TT_ORDINAL_SEASON_RE`) is S2E10 with the ordinal cut *out* of the show name — before this, season-2 episodes landed under season 1 in a separate `… 2nd Season` group.
- **Kind order**: sequel-season episode dash → multi-season → episode (`SxxExx`/`NxNN`) → single season (incl. ordinal `2nd Season`) → **absolute-episode-range batch** (`One Piece 0001-1071`, `1094-1099` — `_TT_EP_RANGE_RE`, upper bound ≥100 and > lower, year-pairs/resolution-pairs rejected; → `multiseason` pack with `ep_from`/`ep_to`, UI label "Episodes A–B", excluded from the bulk pack recommendation since coverage is unverifiable) → **absolute-numbered anime** (`EP1168`, `Show - 1168`, `Show 1168 [`; season 1, episode = the number; skips years/resolutions; the bare-trailing-number form needs 3–4 digits so `Ocean's 11` isn't an episode) → movie. The range check must precede the single-abs forms — `" - 574"` inside `001 - 574` would otherwise read as one episode.
- **Grouping key** lower-cases the show, drops **all punctuation** (so `Frieren: Beyond Journey's End` and `Frieren Beyond Journeys End` merge — scene styling varies per group), drops **CJK decoration when a Latin name remains** (`DanDaDan ダンダダン` → `dandadan`; a purely-CJK title keeps its CJK key, and the group's display title upgrades to the first clean Latin variant seen), then strips a trailing file extension and a **trailing bare year** so `Inception 2010` and `Inception (2010)` collapse to one card. (Trade-off: this also merges e.g. `Blade Runner` and `Blade Runner 2049` — acceptable; they're both "blade runner" sources.) Country tags are *not* stripped, so `The Office US` stays distinct from a bare `The Office`. A cut mid-parens (`Show (Season 1) …`) has its dangling `(` trimmed so it doesn't mint a phantom `Show (` group. Titles are HTML-unescaped first (`&amp;` splintered groups).

### Rank Downloads by relevance, not raw seeders — a franchise word + huge seed count buries the right release

The show-detail **Downloads** list flattens **every** group from one broad `/api/search` (`_ssBroadSearch` — it doesn't filter to the opened title's group), so a brand-new high-seed release that merely shares a franchise word rides in alongside the real one. A pure seeder sort then puts it first: searching *Star Wars: Episode II – Attack of the Clones* surfaced *Star Wars: The Mandalorian and Grogu* (7869 seeders) at the top. Fix: the backend attaches a per-result `rel` (`_title_relevance`) and `_ssPackCmp` sorts `_ssPacks` by `rel` (in coarse 0.25 buckets), then the **audio preference** (see § audio-language classification below), then raw `rel`, then seeders — the bucketing lets a Dual-Audio pack at 0.9 beat a subbed 1.0 for a dub household while a genuinely unrelated release (rel ≤ 0.5) can never float up just for carrying a DUB tag. `rel` is a **recall + precision blend** scored against the result's **parsed show name** (`parse_torrent_title`'s `show`, release tags already stripped — *not* the raw title, so `1080p`/`x264`/`BluRay` never count as words), plus the year signal:

- **recall** = fraction of the query's distinctive tokens (stop-words + bare years stripped via `_rel_tokens`) present in the name — the original signal.
- **precision** = fraction of the *name's* tokens the query accounts for. This is what stops the score from **saturating**: before, every `The Matrix …` release tied at the same `rel` (all contain "matrix" + "1999"), so a soundtrack, a *Rifftrax* commentary, a parody, or *The Matrix Reloaded* ranked level with the clean film. The extra distinctive words they carry now push their precision — and `rel` — below an exact-title match.
- Score = `0.7·recall + 0.3·precision` (**recall-heavy** on purpose: a longer *official* title whose every query word is present — "Star Wars Episode II Attack of the Clones" — must still score high, not be punished for its length). Then **+0.5** for a matching release year, **−0.7** for a *different* one.

The **−0.7 different-year** penalty is still the key lever for *cross-franchise* junk: same-franchise-different-film releases almost always carry a different year, and it alone drives the wrong result below every legit match. Don't drop the year signal to "just token overlap" — `Star Wars` overlap alone still scored the Mandalorian result positive. Groups also order by best-member `rel`. Nothing is filtered out — low-`rel` results still render, just lower.

### Don't put the year in the indexer query, and score anime against its romaji name — else seasons and bulk packs vanish

Two anime-specific traps in the show-detail broad search (`_ssBroadSearch`):

1. **Year in the query nukes recall.** The page used to query `"{title} {year}"`. Movies carry the year in the release name, but anime/TV episodes and season packs almost never do — so Jackett's word-AND matching drops nearly everything: a multi-season anime went **~190 → 7 results, 0 episodes**, so the season tabs never populated ("only 1 season shows"). Fix: the year is **not** appended to `q`. It's passed as a **relevance-only** `year=` param, and only for **movies** (`_ssKind==="movie"`) — for TV the `−0.7 different-year` penalty would wrongly demote later seasons (S2, S3… span different years), so TV omits it entirely.

2. **JP vs EN naming buries bulk packs.** The scene indexes anime under both the English title and the romaji original. Scored against the English query alone, a romaji-named season pack got `rel=0` and sank below everything — e.g. a high-seed late-season pack ranked dead last. Fix: TMDb metadata now carries `aka` (Latin-script `alternative_titles`, romaji/JP-region first, via `_tmdb_akas`) + `original_title`; `_ssBroadSearch` also **queries** the top aliases (merged, de-duped by magnet) and passes the full alias list as `aka=`, so `_title_relevance` takes the **max** over query+aliases. Delimiter-less scene names (all spaces/punctuation stripped) are caught by the squashed-name fallback (`_rel_squash`). Keep the alias query count small (primary + 2) — each is a full indexer fan-out.

3. **TMDb absolute numbering breaks the season breakdown.** Some long-running anime are catalogued on TMDb as a single very long season (absolute episode numbering — one 60-episode "Season 1") while the scene ships S01–S05. The episode list used to render TMDb's list, so **opening the show** dumped all 60 episodes (`S01E01`…`S01E60`, most greyed "NO SOURCE FOUND") and the scene's per-season split was lost. `_ssTmdbAbsolute()` (frontend) detects the case from **TMDb structure alone** — exactly one season of **>26** episodes (so it holds *before* the user searches, not just after), or the found torrents already spanning **>1** season — and then `_ssAllSeasons`/`_ssRenderEpisodes` drive the tabs and rows from the **scene** episodes, and `_ssMissingEpisodes` returns `[]` (TMDb's absolute list can't be mapped to scene seasons, so the "missing" enumeration would be noise). The empty pre-search state shows a **Search episodes** prompt (gated on `_ssEpisodesSearched`) instead of the skeleton. Don't extend this to remap absolute→seasonal episode *names* — season lengths vary and the guess is fragile; only Season 1 (where absolute == seasonal for eps 1–12) still resolves names via `_ssEpName`. **Normal shows** (TMDb seasons match the scene) keep the old skeleton-with-Find behaviour — the >26 threshold and the multi-season signal both avoid firing on them.

### Audio-language classification — "DUB" without a language check downloads the Arabic dub

Anime results mix audio variants under one show: untagged fansubs (SubsPlease/Erai-raws = original audio + English subs), `DUBBED` (English dub), `Dual Audio`/`Multi-Audio`/`MULTi`, and **foreign** releases (`ArabicDub`, `Korean Dub`, `VOSTFR`, `LATINO`…). A seeder sort is blind to all of it — a dub household bulk-grabbing a season got top-seeded *subbed* episodes (live Solo Leveling data: SubsPlease at ~300 seeders vs the dub at ~20). `_parse_release_audio` ([main.py](../main.py)) classifies every enriched search result (`audio` ∈ `dual`\|`dub`\|`sub`\|`other`\|`""` + `audio_lang`); the frontend audio preference (`_ssAudioPref`, `localStorage.streamlink_audio_pref`) filters/sorts on it. Traps baked in:

- **Precedence is dual > other(foreign) > dub > sub.** A naïve "prefer `dub`" match grabs `ArabicDub` and `Korean Dub` — the language word (incl. the **fused** `<lang>dub` form, split by regex before the token scan) must force `other` before the bare `dub` token is consulted.
- **An explicit English-audio token beside foreign ones means dual, not foreign**: `ENG-ITA`, `[Eng-Hindi-Tam-Tel]`, `[AUDIO #1 ENGLISH] … Polish` all carry English audio (`_AUD_ENG_RE`, checked only after `eng…subs` phrases are stripped so `ENG SUBS` never counts as audio).
- **A multi-SUB marker means the language tokens are the subs list**: Erai-raws `[Multiple Subtitle] [ENG][POR-BR][RUS]…` is original audio + subtitles → `sub`, not a Portuguese/Russian release.
- **`MULTi` does NOT imply English.** The French scene tags FR+original releases `MULTi` (Tsundere-Raws — a known-French group hint forces `other:French` even when the title says only `Multi-Subs`); bare `MULTi` with no language context (ToonsHub) is dual. `Multi-Subs` never counts as multi-audio (`(?![ ._-]?subs?)` lookahead).
- **Untagged (`""`) means "the show's original language" — resolve it with TMDb `original_language`, not a constant.** Fansub releases carry no marker (original audio) *and* plain English content (Breaking Bad) carries no marker (English audio — titles never say "English", it's implied). The 9.12.0 rule "untagged never matches English" flagged every Breaking Bad episode "No Eng audio". Fix: metadata carries `original_language`; `_ssAudioMatch` counts untagged/`sub` as English **iff** the show's original language is English (unknown ⇒ treated as English — the common case outside anime, and anime virtually always resolves TMDb metadata here). The preference chips still only render when results contain `dual`/`dub`/`other` (`_ssHasAudioVariety`).
- The foreign-language token map is **deliberately conservative** (full words + unambiguous scene tags only — no `GER`/`SPA`/`POR` abbreviations, which collide with title words).
- In bulk **Auto**, the audio preference outranks *everything* (an episode with any matching source never falls back to another language — that's the whole point); the seeder-floor/size cascade runs inside the matching subset. Episodes with **no** matching source fall back to best-seeded and are **counted in the final toast** ("N had no English-audio source") — silent fallback is the exact surprise this feature exists to kill.

### Size tags ("- 1.85GB", "- 700 MB") were misparsed as absolute episode 1 — hiding YIFY movies from the packs page

`parse_torrent_title` first normalises `._` → spaces, so a YIFY-style `The Matrix (1999) … - 1.85GB -YIFY` became `… - 1 85GB -YIFY`. The absolute-episode heuristic (`_TT_ABS_DASH_RE`, `\s-\s+(\d{1,4})`) then read the `- 1` as **episode 1** → `kind="episode"`. On a **movie** show page (opened from a TMDb movie card) the packs list filters to `kind !== "episode"`, so the misclassified release — often the *highest-seed* one — silently vanished. Fix: `_TT_SIZE_RE` strips size tokens (incl. the dot-split `1 85GB` form) from `norm` **before** structural parsing. Keep the strip ahead of the marker scan; anime absolute-numbering (`One Piece - 1043`, `Attack on Titan - 12`) must still parse as episodes, and they carry no size unit so they're untouched.

### TMDb `/search/tv` ranking is unreliable — score matches, don't take `results[0]`

TMDb's text search sometimes floats a low-signal partial above the obvious show: `query="Big Brother US"` returns **Celebrity Big Brother** (popularity 3.7) *ahead of* the canonical **Big Brother** (popularity 68, 28 seasons). Both TMDb matchers used to bind `results[0]`, so the search-show screen and the library metadata locked onto the 3-season Celebrity show and every real season the wrong show didn't list (4–16, 18–26) had no season tab and no "Find sources" row. Fix = the shared `_tmdb_pick_tv(query, results)` ([main.py](../main.py)): score by title-match **tier** — exact (after country-suffix strip) → candidate-extends-query prefix → substring — with **popularity as the tie-breaker**, then take the max. Used by both `_tmdb_lookup_by_title` (search page) and `_tmdb_match_show` (library).

Note the deliberate split from the grouping rule above: `_tmdb_norm_title` **strips** a trailing country suffix (`US`/`UK`/`AU`/`CA`/`NZ`/…) for *scoring only* (so `Big Brother US` scores an exact match against TMDb's `Big Brother`), while the search **grouping key keeps** country tags (so `The Office US` stays a distinct card from a bare `The Office`). Two different layers — don't unify them. The country strip never touches the query we actually send to `/search/tv`; the right candidate is already in the result set, we just have to pick it.

### A release that spaces its channel layout donates an episode number

`Futurama-1999-S03 1080p WEBRip 10bit EAC3 2 0 x265-iVy.mkv` parsed as **episode 0**, and
its season-1 sibling (`… EAC3 5 1 …`) parsed as **episode 1**. Neither number came from
anything to do with episodes: both came from the **audio channel layout**.

Season packs routinely name their first file after the RELEASE rather than the episode, so
that one file carries no `SxxEyy` and falls through to `_episode_from_name`'s bare-number
fallback, which takes the last number in the cleaned stem. `_NOISE_RE` stripped only the
**glued** audio spellings (`AAC2.0`, `DDP5.1`) — a release that spaces or dots the layout
apart left `2 0` / `5 1` standing. 2.0 audio gave episode 0; 5.1 audio gave episode 1. The
5.1 seasons therefore looked *correct*, which is the worst possible outcome: it hid the
bug until someone compared seasons. `_AUDIO_CH_RE` now strips a separated layout, anchored
on the codec word and limited to real layouts (`<1-8> <0-1>`) so it can never eat a real
episode number. The same applies to the video codec: `[xh]\.?26[45]` allowed only a dot,
so an indexer that normalises separators to spaces left `H 264` → **264** behind.

**The general rule: anything left standing after the noise strip becomes a candidate
episode number.** When adding a release tag to `_NOISE_RE`, cover every separator the
indexers produce (`.`, ` `, `-`, `_`), not just the one in the release you are looking at.

### An unnumbered file in an otherwise complete season is deducible — but only just

The other half of the same bug: nothing ever recovered the file that had no number. It sat
at episode 0, first in the season, nameless, while the TMDb diff reported episode 1
MISSING on a season the box holds complete — and offered to go and download it again.

`_fill_season_gaps` (run at the end of `attribute_paths`) fills it: when a season holds
exactly **one** numberless file and the numbered ones leave exactly **one** hole in the run
`1..N` (N = how many files that season has), the hole is the answer. Every one of those
conditions is load-bearing, so resist widening it:

- **two numberless files** could go either way round — path order is not evidence;
- **numbers past N** (2, 3, 17) mean this isn't a clean run, so a lone hole proves nothing;
- **`abs`-flagged siblings** are series-absolute anime numbering, which `resolve_absolute`
  rewrites once TMDb's counts are known — arithmetic run *before* that would fight it.

Matching migration trap: the 11.19.0 re-attribution pass keyed on "no season **AND** no
episode", so a file that got its season from the folder and nothing from its basename
(season 3, episode 0) was never re-examined. The test is now "no episode, not bucketed",
and the result is stamped `attrib_v` on the item so the regex work is once per item, not
once per library load (`_load_lib_raw` runs on every load). Bucketed files are exempt:
`Specials/Behind the Scenes.mkv` has no episode number because there isn't one.

### `/search/movie` needs the same scoring — and an empty file list is not evidence of a movie

Two halves of the same 2026-09-15 report ("I downloaded Regular Show and the library showed *Regular Show: The Movie*; the first option in the swap dialog was the right one").

**1 — the movie branch took `results[0]`.** The fix above only ever applied to TV. `_tmdb_match_show`'s movie leg bound `mv_results[0]` verbatim, and TMDb's raw movie ranking is popularity-shaped, not title-shaped: `query="Regular Show"` returns **Regular Show: The Movie** (354857) *ahead of* the movie actually titled **Regular Show** (1650340). Fix = `_tmdb_pick_movie(query, results)`, the exact mirror of `_tmdb_pick_tv` (tier, then popularity), used by both `_tmdb_match_show` and `_tmdb_lookup_by_title`.

**2 — `is_movieish` is a guess made from nothing.** An item is matched against TMDb the instant it is added, when qBittorrent has resolved **no files at all**, so `len(files) <= 1` is true for every brand-new item; a season pack downloaded from the packs list also carries `season: 0` (the Add-to-Library modal derives season from an `SxxEyy` in the title, and a pack has none). A whole-season pack therefore looks *exactly* like a one-shot movie and goes to `/search/movie`. 12.6.1 added `_title_says_series` (an `S01` / `Season 1` marker in the name overrules the guess) and `_movie_binding_is_stale` (re-open the binding once the files resolve into a numbered run) — but neither helps a pack whose name carries no season marker at all ("Complete Series", most anime). So `_tmdb_match_show` now also compares **across kinds**: when the guess says movie but a TV show is named *exactly* what we searched for and the best movie only extends the query, the show wins.

**3 — and the answer was already known.** The deepest version of this bug is that Smart search opens a show page **from** a TMDb candidate the user picked, and `POST /api/library/download` then discarded that id and re-derived the show from the release name. Nothing about an inference is better than the fact it is inferring. `DownloadReq.tmdb_id`/`tmdb_kind` → `item["tmdb_pick"]` → `_fetch_item_metadata` binds it directly, stamped `source: "picked"` and pinned. **When a caller knows the binding, pass it — don't make the server guess it back.**

### Un-configuring a storage path is invisible, so it must be confirmed

`F:\StreamLink` disappeared from Storage Paths on 2026-09-14. The archived access log has the whole story in two lines: a `DELETE /api/settings/library-paths?path=F%3A%5CStreamLink` returning 200, and **one second later** a `POST /api/settings/library-paths/auto?path=F%3A%5CStreamLink&auto=false` returning **404** — the Auto toggle the user was actually aiming for, arriving after their previous tap had already removed the path. The two controls sat flush against each other on the same row and the ✕ had no confirmation.

What makes this worse than an ordinary mis-tap is that **nothing appears to break**. Removing a root does not touch the disk, and library items store **absolute** paths, so every file there keeps playing exactly as before. The drive simply stops being offered as a destination, stops reporting free space, and drops out of `_auto_save_path()` — discovered weeks later as "where did my folder go?". (In this case it was also the *only* auto-eligible root left, because the C: root had been set `auto:false` moments earlier; `_auto_save_path` logs a warning and ignores the opt-outs rather than failing, so even that was silent.)

Rules: a control whose damage is invisible needs a prompt **more** than a loud one does, and a destructive control must never sit flush against a benign toggle. The ✕ is now divider-separated and confirms, in words that say what does and doesn't happen to the files.

### Audio memory needs a language default — per-series memory can't cross separate items

`series_audio_prefs` (and `series_subtitle_prefs`) key on `_series_of_item` — the series name, or `item:<id>` for an untagged item. Episodes **downloaded individually** are each a separate library item with an empty `series`, so each gets a *unique* `item:<id>` key and per-series memory never carries a pick between them. Subtitles hid this: the admin subtitle policy re-selects the preferred *language* (`settings.subtitles.default_language`) on every episode regardless of item grouping, so subs "just worked" across episodes while audio (which had no default) reverted every time — the "audio isn't keeping between episodes" report (8.9.3). Fix = the audio analog of the subtitle language default: a **self-learning per-profile `audio_language_pref`** (`{lang, idx, count, at}`, [main.py](../main.py) `_learn_profile_audio_pref`), updated on every deliberate audio pick on both players and applied by `_apply_track_prefs` (`_resolve_profile_audio_pref`) whenever no per-file/per-series descriptor resolves. It matches by language first (release-stable), then the remembered slot index **only when the episode's audio track count matches** (so it can't grab the wrong slot on a differently-laid-out episode). Both players honour it (8.9.4): VLC via `_apply_track_prefs`, and the on-device player via `_lpResolveAudioPref` (fed `audio_language_pref` in its `saved_tracks` payload; fully-offline it reads a per-profile `localStorage` mirror written by `_appLearnAudioPref`, since `library.json` is unreachable in airplane mode). Don't assume per-series memory covers cross-episode persistence — it only does when the episodes actually share a series key.

### VLC populates its audio-track list *asynchronously* — resolve must poll, not snapshot

VLC exposes an opened stream's audio tracks in `status.json` only *after* it has parsed them, which can lag the file-open by a second or more (network/HLS inputs are the worst). `_apply_track_prefs` runs on a fixed post-`in_play` delay (2.0 s auto-advance, 3.5 s restart), and originally resolved the remembered audio descriptor against a **single** `vlc_status()` snapshot at that instant. When the snapshot landed before the tracks existed, `_parse_track_streams` returned `[]`, `_resolve_audio_descriptor` found no candidate, and audio silently stayed on VLC's default — a **race**, so cross-episode audio memory "sometimes" worked (8.9.2). Subtitles dodge this because they route through `_apply_subtitle_policy`, which loads sidecars and always sends an explicit track. The audio branch now **polls (up to ~4 s)** until the track list appears and the descriptor resolves, breaking early on a match or once the list is fully populated with no match (a genuinely absent track still falls through harmlessly). Any future code that reads VLC's track list right after a play must not assume one snapshot is complete.

### VLC re-runs its default audio selection after our command — re-assert the pick

Sending `audio_track` once during stream open is not enough: VLC re-runs its **own** default audio selection as the input finishes buffering (common with sequential torrent streaming, where the head trickles in), silently overriding the ES ID `_apply_track_prefs` just sent. The symptom is exactly a **dropdown/playback split** — the audio dropdown's "current" highlight mirrors `state.current_audio_track` (which we set alongside the command, so they can't disagree on their own), yet VLC plays its default: e.g. Japanese under an English dub. VLC 3.x's HTTP API has no current-audio field (see above), so nothing catches the override — hence intermittent (8.9.5). Fix: after the initial pick, `_apply_track_prefs` fires a background **`_reassert_audio`** that re-sends the chosen track a few times over the next few seconds (re-resolving each pass in case ES IDs drifted) so a late default-select loses. It **must** stop the instant `state.current_audio_track` diverges from what it last set — that means a manual `set_audio_track` (or a newer `_apply_track_prefs`, or a new video resetting it to -1) has superseded it, and continuing would fight the viewer's own choice. Don't "simplify" this to a single post-open send; the override is the whole reason it exists.

**The on-device / offline HLS player has the identical failure mode** (8.9.6), for the analogous reason. Its audio dropdown is rendered from `lp.pendingAudioIdx` while the actual rendition is set once by `_lpApplyAudioIdx` — but **Safari native HLS (iOS) populates `v.audioTracks` asynchronously** as it loads the alternate-audio rendition, so at `loadedmetadata` the list is often still empty and the old early-return dropped the pick silently (dropdown = dub, playback = default). Safari can also re-assert its own default *after* a selection applied. `_lpApplyAudioIdx` now delegates to `_lpApplyAudioIdxRetry`, which retries (~3 s) until the track list is populated, then selects it, and on the Safari path re-checks after 500 ms and re-applies if the track was disabled by a late default-select. Same supersession guard as VLC: it bails the moment `lp.pendingAudioIdx` or `lp.filePath` changes (a manual `lpSetAudio` pick or a new file), so a stale retry never fights the viewer. Keep both players' re-assert logic — neither player's audio command "sticks" from a single call during stream open.

### VLC auto-enables subtitles — "off" must be sent explicitly, every play

VLC turns on its first/forced subtitle track on its own when a file opens, so "subs off" in the UI was a lie unless we *told* VLC to turn them off. The subtitle-default policy (`_apply_subtitle_policy`, called from `_apply_track_prefs` on every play / prev / next / re-apply) therefore **always sends an explicit `subtitle_track`** — `-1` to disable, or a real ES ID to enable — it never leaves the choice to VLC. A saved per-file user pick (`file_progress[...].subtitle_track`) still wins; only when there's no saved pick does the policy run.

The on/off decision is `profile["subtitles_on"]` (per-profile override) falling back to `settings.subtitles.on_by_default` (admin default, off out-of-the-box) — but a remembered **descriptor** (the newest of this file's `subtitle_sel` and the per-series pref) **beats the off default**: an explicit user pick is honoured even when subs default off, exactly like a saved per-file ES-ID pick (7.16.1; previously the off gate returned before the descriptor was consulted, so with the out-of-the-box off default a remembered pick was never restored). Since 8.5.0 an unresolved **ON** descriptor no longer stays off either: the pick *is* the on switch for that series, so the policy falls through to the generic chain and, failing that, a last-resort real track (avoiding signs/commentary titles) — subs must never silently revert to off for a series the viewer enabled them on. When **on**, the policy first **aggressively loads every local sidecar sub** for the file into VLC via `addsubtitle` (`_load_all_local_subs` → `_discover_local_subs`; see next gotcha) so all of them are selectable, then picks: a remembered descriptor first (resolved by `_resolve_sub_descriptor` — see the descriptor gotcha below), else (a) a **real** (embedded/downloaded) track in the preferred language (`settings.subtitles.default_language`; any track if "Any") → (a′) if no exact match and `single_option` is on, a lone real sub regardless of its tag → (a″) an **AI** sidecar in the preferred language (the fallback) → (b) an OpenSubtitles auto-search download in that language (`auto_search`, on out-of-the-box) → (c) the last-resort track when an ON descriptor is pending → (d) otherwise off. **Real beats AI for the same language**: `_load_all_local_subs` annotates each track `ai`/`path` (a sidecar whose stem contains the `.ai.` marker is AI), and the policy ranks real ahead of AI. When it deliberately lands on an AI track it records `state.sub_auto_ai_path`; `subtitle_upgrade_loop` then swaps in a real preferred-language sub once one downloads (`upgrade_late_subs`) and broadcasts `subtitle_upgraded`. Auto-search uses `save_pref=False` so it stays a *live* policy decision (a later profile/admin off-toggle still wins); the manual download endpoint persists the pick. The policy runs at the same ~3.5 s post-`in_play` delay as audio track prefs (plus ~0.3 s per sidecar loaded), so a brief sub flash before a forced-off file settles is possible and accepted.

### VLC's own sidecar autodetect must be disabled — it loads AI subs *untagged*

VLC auto-loads any sidecar `.srt` whose name starts with the video's stem (e.g. `Show.S01E01.eng.ai.base.srt` for `Show.S01E01.mkv`) **as soon as the file opens**, and parses `eng` from the filename so the track reports `Language: English`. That happens *before* `_load_all_local_subs` runs — and that function only tags a sidecar as AI (`ai: true`) when **it** is the one that calls `addsubtitle` and a *new* track appears. A sub VLC already loaded therefore sits in the initial `_vlc_subtitle_tracks()` snapshot looking like an ordinary **embedded** track, with no `ai` flag. The policy then treats the AI sub as a *real* English track and ranks it **above** a genuine sub whose language VLC couldn't read (e.g. one titled *"Text with various tags"*, `lang == ""`) — the AI sub wins auto-selection on every play, and the misclassification also defeats the `single_option` lone-real-sub fallback that would otherwise carry the choice across episodes. Fix: launch VLC with **`--no-sub-autodetect-file`** (all three launch sites — `main.py` restart, `run.py`, `watchdog.py`) so `_load_all_local_subs` is the *only* path that loads sidecars and every AI sub is tagged correctly. Consequence: anything present at file-open is now an embedded (in-container) track, and a resumed file must load its sidecars itself — `_apply_track_prefs` calls `_load_all_local_subs` up front even on the saved-pick branch (the no-pick branch gets it via the policy), so the subtitle menu stays complete and a saved sidecar ES ID still resolves.

### A subtitle pick must be remembered as a descriptor, not an index

VLC **ES IDs** and on-device **sidecar indices** both drift between replays — and a late-downloaded sidecar shifts the list — so persisting "subtitle = ES 7" or "sidecar:2" silently re-applies the *wrong* track next time (or, for the on-device player pre-v4.27, nothing at all: `_lpSaveLocalTracks` saved every non-bundle pick as `-1`/off, so a chosen `.srt`/AI sub was never remembered). The fix is a resolvable **descriptor** `{off, lang, ai, name, title, idx, sig, at}` (`subtitle_sel`; the last four are v2, 8.5.0 — see [LIBRARY_DATA.md](LIBRARY_DATA.md)), saved per-file *and* per profile+series (`profile.series_subtitle_prefs`, keyed by `_series_of_item` — the series name, or the `"item:<id>"` fallback for untagged items, without which season packs/movies with the default `""` series never carried a pick across episodes; 7.16.3). The per-series entry also accumulates **`groups`** — one pick per distinct embedded-text-sub layout signature — so a series stitched from several releases resolves each episode group to its own remembered pick, even when tracks carry no language/title tags (the sig+idx pair pins the exact slot). On the next play the resolver (backend `_resolve_sub_descriptor` ↔ client `_lpResolveSubSel`, **keep the chains in sync**) matches exact `name` → same `sig`+`idx` → `groups[sig]` → fuzzy episode-invariant `name` (`_sub_fuzzy_name`) → `lang`+kind → any-kind in that language → `title` → lone-option → last-resort-when-ON, against the *current* track list; the newer of the per-file pick (`at`) and the series pick (`updated_at`) wins, legacy timestamp-less picks counting as oldest. Signatures must be **identical across players**: they cover embedded **text** subs only (bundles never carry image subs — VLC filters PGS/VobSub via codec heuristics), and `_canon_lang` folds VLC's full language *names* ("English") to ISO codes, without which device picks never language-matched on the TV. Three consequences to keep in mind: (1) the **late-sub upgrade only fires on an auto-applied AI sub** — a manual pick clears `state.sub_auto_ai_path` / `lp.subAutoApplied`, so a deliberate choice (even choosing the AI track yourself) is never overridden; (2) since 7.16.1 a **VLC pick also writes the full per-file descriptor**: `_load_all_local_subs` / `_download_and_attach_subtitle` record each track's `{lang, ai, path}` in `state.vlc_sub_meta`, and `_remember_vlc_sub_pick` builds the descriptor from it (VLC's own language tag is the fallback), saving it per-file *and* per-series — so a VLC pick now carries over to the on-device player, and vice versa (`_apply_track_prefs` resolves the winning descriptor; the raw ES ID fast-path only applies to an embedded pick not outranked by a newer series pick, since sidecar ES IDs drift with load order); (3) **only a deliberate subtitle pick may write the series-wide pref** — see the next gotcha. To keep "latest pick wins" across engines, `/api/library/{id}/local-tracks` **drops the stale `subtitle_track` ES ID** whenever it saves a fresh device descriptor. See [LIBRARY_DATA.md](LIBRARY_DATA.md), [STT.md](STT.md).

### Never persist subtitle *state* as if it were a subtitle *pick* — the "series reverts to off" bug

The on-device save path (`_lpSaveLocalTracks`) snapshots `lp.pendingSubtitleIdx` to build the descriptor it persists. Pre-8.5.0 it ran the **same full snapshot for every trigger** — so changing the **audio** track (and the automatic late-AI-sub upgrade) also persisted the *current subtitle state*. If the remembered pick had failed to resolve on that episode (subs sitting at off), the audio change wrote that accidental `{off:true}` per-file **and into `series_subtitle_prefs`** — silently turning subtitles off for the entire series on both players, which looked exactly like "my subtitle pick keeps reverting". The rule: **state is not intent.** Only a deliberate subtitle action may touch the series-wide pref. Enforced in three layers: `_lpSaveLocalTracks(what)` omits the subtitle fields entirely for `"audio"` saves (both online and the offline native-store branch) and sends `sub_series:false` for `"sub-auto"` (upgrade) saves; `/api/library/{id}/local-tracks` only calls `_save_series_sub_sel` when `sub_series` is true; and the newest-wins timestamps (`at`) mean any already-polluted per-file `{off}` descriptor (timestamp-less) loses to the next deliberate series pick, so old damage heals itself. When adding **any** new save path that includes `subtitle_sel`, ask: did the user just *choose a subtitle*? If not, don't send it.

### Every `file_progress` writer must preserve the sibling track-pick keys

`file_progress[path]` holds watch position **and** the track picks (`_TRACK_PREF_KEYS`: `audio_track`, `subtitle_track`, `local_audio_idx`, `local_subtitle_idx`, `subtitle_sel`, `audio_sel`, `audio_offset_ms`) as sibling keys in one dict. Any writer that replaces the whole entry with just `{position_sec, duration_sec, completed, updated_at}` silently wipes the picks — and progress writers fire *constantly*: `vlc_progress_tracker` saves every 15 s during VLC playback, and the on-device player POSTs `/api/library/{id}/progress` on a similar cadence (plus a `sendBeacon` on page hide). This was exactly the "subtitles default back to off after stop/start" bug (fixed 7.16.1): the pick was saved correctly at selection time, then clobbered by the next periodic progress write, so the replay found no pick and fell back to the subs-off default. When adding or touching **any** code that writes a `file_progress` entry, spread the existing entry's track keys into the new dict (use `_TRACK_PREF_KEYS`, don't retype the tuple — `mark_watched`, `update_progress`, `sync_progress`, `sync_resolve`, and the tracker all spread it; the four hand-copied duplicates were folded back into the constant in 11.10.0, when `audio_offset_ms` had to be added in five places at once). `audio_sel` (8.9.0) is the audio equivalent of `subtitle_sel` — a resolvable descriptor remembered per-file and per profile+series (`series_audio_prefs`); spread and preserve it exactly the same way.

### A progress save's position and file must come from the same instant — don't straddle an auto-advance

`vlc_progress_tracker` reads position/duration from `status.json` but resolves the *current file* from a **separate** `playlist.json` call. When VLC advances to the next episode on its own, those two reads land on opposite sides of the transition — and writing the finishing episode's position under the *next* episode's key corrupts progress two ways at once: the finished episode "restarts" (its key inherits the incoming file's `time≈0`) and playback "jumps to the wrong episode" (the next file is stamped `completed` at the previous file's position). Fixed 8.0.3: the 15 s save re-reads `status.json` + the active URI **back-to-back** immediately before writing and skips the tick unless the resolved file still equals `state.library_current_file` **and** VLC is `playing`/`paused` (an end-of-file transition briefly reports a non-playing state / `time≈0`). A skipped tick doesn't advance `last_progress_save`, so it retries on the next 2 s pass rather than waiting a full 15 s. Rule: any progress writer must take position, duration, and file identity from one consistent snapshot — never mix a stale `pos/dur` with a freshly-resolved file. The on-device `lpStop` has the same class of bug in miniature: saving `currentTime` on stop *before the resume seek lands* writes ≈0 over a real position (fixed 8.0.3 with the same `t ≥ 5` guard the other on-device savers use).

The same straddle also bit the **Smart Skip offer evaluation** (fixed 10.10.1): `_maybe_emit_skip_offer` was called every tick with the top-of-tick `pos_sec/dur_sec` (status) but the freshly-resolved `cur_file` (playlist). On the transition tick that pairs the *outgoing* episode's near-credits position with the *incoming* episode's skip metadata — and sibling episodes have similar `credits_start` times, so the auto-skip-credits countdown fired the instant the new episode started, could chain, and the "skip credits" landed a few episodes ahead of the intended one. The tracker now computes `file_changed` (current file differs from the previous tick's) and skips the offer evaluation on that tick; the next 2 s tick is consistent. The rule generalizes: **any consumer of the tracker's `pos_sec` must not act on it in the same tick the file identity changed.**

### Subtitles live in odd places — search aggressively, not just next to the video

Releases stash sidecar subs in several layouts, and VLC's own autodetect (which we **disable** anyway — see the autodetect gotcha above) would only ever load the exact-stem one in the video's own folder — so anything in a `Subs/` folder is ours to find. `_discover_local_subs(video)` covers the real-world cases: subs next to the file (`Movie.srt`, `Movie.eng.srt`), and `Subs/` / `Subtitles/` / `Sub/` / `Subtitle/` folders beside the video **and one level up** (video-in-its-own-subfolder releases), recursing into per-episode subfolders. A file is claimed when its name carries the video stem, it sits in a folder named after the stem, **or** the release holds a single video (then loose subs must be its — but this "take everything" fallback is restricted to the video's *own* directory, so a shared `Subs/` one level up in a multi-episode pack can't leak a neighbour's subs). Extensions: `.srt .vtt .ass .ssa .sub`. Languages are read from filenames including full names (`2_English.srt`, `3_Brazilian.Portuguese.srt`) via `_parse_sub_lang`, since VLC rarely tags loose sidecars. Discovery is sorted so loaded-sidecar ES IDs stay stable across replays.

### File repair's re-encode leg must re-map subtitles — `-c copy` only protects the remux

The remux repair attempt (`_repair_one_file`) preserves everything with `-map 0 -c copy`, but the lossy **re-encode** fallback re-builds the output stream-by-stream and originally mapped only `-map 0:v:0? -map 0:a?` — so re-encoding a damaged file to drop corrupt frames also silently dropped **all embedded subtitle tracks**. The re-encode now also maps `-map 0:s?` (and `-map 0:t?` for MKV attachments / ASS fonts), copying subs in MKV (`-c:s copy`, image subs included) and transcoding to `mov_text` for MP4/MOV (copying subrip into MP4 fails). Attachments are MKV-only — mapping `0:t?` into an MP4 hard-fails the encode, which would have aborted the whole repair. Sidecar subs (online-downloaded, AI, `Subs/`-folder) need no special handling: repair `os.replace`s the source in place, keeping the same path/stem the sidecars are keyed on, so they're never moved or deleted.

### Absolute vs relative seek

- Absolute: `val=N%` (percentage) or `val=Ns` (seconds). Our `/api/vlc/seek/to` uses `val=N%`
- Relative: `val=+Ns` / `val=-Ns`. Our `/api/vlc/seek?delta=N` uses this

`val=N` with no suffix is interpreted as a **0–1 fraction**, not seconds. Don't confuse them.

### Don't `await vlc("in_play")` to gate the UI flip or the resume seek — fire it detached and poll

VLC's HTTP reply to the `in_play` command lags **several seconds** behind actual playback: VLC starts decoding the file in <1 s, but the `status.xml` response to the command that started it doesn't come back until well after. The `in_play` path also makes *two* `timeout=5.0` calls (the volume pre-roll, then `in_play`), so a stalled reply pins things for "at least 10 s." The old `_library_play_launch` awaited that reply before flipping `stream_status` to "playing" and before even *creating* the resume-seek task — so both landed ~10 s late even though VLC had been playing the whole time.

Fix (v3.4.4): `_library_play_launch` and `_vlc_relaunch_playlist` now `asyncio.create_task(vlc("in_play", …))` **detached** and detect the real start by polling the much lighter `status.json` (and `playlist.json`) via `_vlc_wait_until_ready(expected_file=…)`. The "playing" flip and the resume seek both fire the instant VLC reports it's playing the **new** file. The `expected_file` URI match is load-bearing: without it the poll would latch onto the *previously* playing file (background video / prior episode) and flip/seek too early. The remaining-episodes `in_enqueue` loop still `await play_task`s first so the tail appends in the right order, but that wait is invisible (you're already watching). The detached `play_task` is `.cancel()`ed on supersede/error so a slow reply can't resurrect a stale file.

### Use the shared `_vlc_http()` client for VLC — never a per-call `httpx.AsyncClient()`

The detached-poll fix above was *necessary but not sufficient*: in practice **every** VLC HTTP call was slow, status polls included, so the poll-based flip still took ~10 s. Root cause (fixed v3.4.5): the old code opened a brand-new `httpx.AsyncClient()` (→ new TCP connection) on every single call. VLC's built-in HTTP interface is a tiny, effectively single-threaded server, and three background loops (`stat_broadcaster`, `vlc_progress_tracker`, `background_video_loop`) hammer it every 2–3 s — so it spent all its time accepting/tearing down sockets and every call took seconds.

All VLC calls now go through the module-level persistent `_vlc_http()` client (`base_url` + client-level BasicAuth, a 4-connection keep-alive pool, `connect=2 s`/`read=5 s`). Built in `lifespan`, closed on shutdown, lazily rebuilt if `is_closed`. **Do not** reintroduce a per-call `async with httpx.AsyncClient()` for VLC, and pass only the relative path (`/requests/status.json`) — the `base_url`/`auth` live on the client. If you add a new VLC endpoint call, use `_vlc_http()`. This depends on VLC honoring HTTP/1.1 keep-alive (it does); if a VLC build ever closes every connection, the connect cost returns and the next lever is pinning the host to an explicit IPv4 (`--http-host=127.0.0.1` in `run.py`/`watchdog.py` + the same in `VLC_URL`) to kill any `localhost`→IPv6 resolution stall.

### Resume seek must wait for VLC to open the file — poll, don't sleep

A `seek` issued right after `in_play` is silently dropped: VLC can't honour it until its demuxer is up, which is when `status.json`'s `length` becomes non-zero. The resume path therefore **polls** via `_vlc_wait_until_ready(expected_file=…)` (state `playing`/`paused`, `length > 0`, **and** the current playlist URI matches the target file; every 0.2 s) and seeks the instant VLC is ready, instead of the old blind `asyncio.sleep(3)`. On a local file VLC opens in well under a second, so the old fixed wait left the user staring at 0:00 for ~3 s before the jump; a slow open could miss the 3 s window entirely and never resume. `_library_play_launch` re-issues the seek **once** (guarded: only if `time` is still >15 s behind target) because VLC occasionally ignores a seek fired the very moment the demuxer comes up, and each step re-checks `state.library_current_file` so a superseded play bails instead of seeking the wrong file. Don't revert this to a fixed sleep. The `resume_mode="prompt"` offer uses the same gate so it appears as soon as playback is live.

### File path → URI

Always use `Path(p).resolve().as_uri()` when sending to VLC. This:
- Handles symlinks (important — VLC plays the resolved path, so `library_current_file` is also stored resolved)
- Generates correct `file:///C:/...` on Windows and `file:///...` on macOS/Linux without extra string surgery

### Volume scale mismatch

VLC uses 0–512 (256 = 100 %). Our API uses 0–200 (100 = normal). Conversion is `raw = volume / 100 * 256`. The global `settings.max_volume` cap is also 0–200. `state.vlc_volume` is in our scale.

`vlc("in_play", ...)` pushes a `volume` command first so VLC's default doesn't blast briefly. Important when the global cap is low.

### Volume cap must be re-applied at every track start

`state.vlc_volume` is polled directly from VLC every 2 s, so it tracks VLC's reality — which can drift above the user's `max_volume` cap (e.g., VLC defaults to 100 on a fresh start, and `user_volume_before_bg` is seeded to 100 before the user ever touches the slider). Two defenses, both required:

1. `vlc("in_play")` clamps `state.vlc_volume` by the current cap **before** sending the pre-play `volume` command. Otherwise a low cap (say 60) plus a 100-default `user_volume_before_bg` blasts at 100 on every bg→content handoff.
2. The state broadcaster ([main.py:1112](../main.py#L1112)) checks the polled VLC volume against the cap each tick and pushes a correction if VLC is over. This self-heals against VLC's occasional snap-to-100 on playlist advance.

Don't drop either one thinking the other covers it — #1 is fast (no audible blast), #2 is the safety net for mid-playback drift.

### Dashboard state desyncs from VLC on restart — `_sync_state_from_vlc` reconciles

`AppState` is purely in-memory in the uvicorn process. If `main.py` restarts (admin Shut Down, watchdog kick, manual relaunch) while VLC keeps playing, every state field is back at its dataclass default — `stream_status="idle"`, `active_title=None`, `library_item_id=None`. `background_video_loop` sees VLC already in `state=playing` and stays out of the way (its job is to start bg when VLC is *stopped*), so the dashboard sits at "No active stream" forever even though real content is on screen.

`_sync_state_from_vlc` ([main.py](../main.py), called from `lifespan` right after the volume init) fixes this: it queries `status.json` + `playlist.json`, matches the playing URI against the background-video path (→ `background_playing=True` and bail) or each library item's files (→ seed `active_title` + `library_item_id` + `library_playlist` + `library_current_file` + `active_hash`), or falls back to the file stem as title for unmatched playback (external VLC plays / stream-now items whose torrent has been GC'd from `library.json`).

**`library_profile_id` is intentionally left unset.** The profile that originally started the playback isn't recoverable from disk state alone, and the wrong guess would mis-key progress writes. `vlc_progress_tracker` therefore skips progress saves and skip offers for the restored session (its first check is `if not state.library_item_id or not state.library_profile_id: continue`). Title display, next/prev, stop, the seek bar, and skip-back-by-30s all still work; resume + skip-credits offers come back the next time the user starts a play.

### Restart-on-retry

`POST /api/retry` ([main.py:2610](../main.py#L2610)) calls `_restart_vlc_process()` which kills all `vlc`/`VLC` processes, sleeps 1.5 s, relaunches with `--extraintf=http`, waits for the port. Then replays the current file + remainder of playlist. Used when VLC freezes on a partially-downloaded file.

### `in_play` appends to the playlist — empty it first or VLC plays stale items

VLC's HTTP `in_play` **adds the input to the playlist and plays it; it never clears what's already there.** So across a session VLC's playlist silently grows — `[bg, epA, epB, epC, …]` — and every leftover entry is a live auto-advance target. Two symptoms, both "the wrong thing plays":

1. **After Stop, an episode plays instead of the background video.** `/api/stop`'s `pl_stop` leaves the enqueued episodes in the list. When `background_video_loop` then plays the bg video, `in_play` of a URI **already in the playlist** (the bg video is usually at index 0 from an earlier idle period) plays that *existing* entry mid-list; when it ends VLC auto-advances into the next item — a stale episode.
2. **On prev/next, the bg video plays instead of the next episode.** A leftover bg entry left in the list can win an end-of-file auto-advance during the transition.

A leak has a second-order casualty: content VLC starts **on its own** (auto-advance into a leftover entry) leaves `stream_status == "idle"` + `background_playing == True`, and the HID remote's gates then misroute every button — ← Back is never claimed, ⏻/🏠 take the idle-surface branches instead of stopping playback. `background_video_loop` therefore **re-adopts** any non-bg file it finds VLC playing while state says idle (`_sync_state_from_vlc`, the same restore used after a server restart), within ~3 s.

Fix: `vlc_clear_playlist()` (`pl_empty`) is called, **awaited, immediately before every fresh `in_play`** so VLC's playlist is always a faithful mirror of `state.library_playlist` (or just the bg video) — the only auto-advance target is the intended tail. Call sites: `_library_play_launch`, `_vlc_relaunch_playlist` (prev/next), `vlc_next_file` (natural auto-advance), the stream-now single-file play, `_play_background_video` (raw `pl_empty` GET), and `_stop_cleanup` (after `pl_stop`). The VLC-process-restart paths (`/api/retry`, night-mode relaunch) **don't** need it — a freshly launched VLC starts with an empty playlist. If you add a new `in_play` caller that doesn't restart VLC, clear the playlist first.

### The idle background video must re-check for real playback *immediately* before it touches VLC

`background_video_loop` decides to start the idle video, then `_play_background_video()` does more `await`s of its own (`get_library()` for the configured path, `_global_max_volume()`) before issuing `pl_empty` + `in_play`. Every one of those is a window in which a real play can start — and an episode auto-advance is exactly the kind of thing that lands in it, because the loop's "VLC is stopped" reading is *taken* during the gap between two episodes. The loop's `stream_status == "buffering"` guard doesn't help: it's read before the awaits.

Observed (v11.9.1): the next episode's `in_play` and the background video's `in_play` were issued in the **same second**, the reef video won, and the TV showed the idle video while the app believed BoJack was playing. The screen glitch was the mild half — see [§ Anything that clears `library_item_id`](#anything-that-clears-library_item_id-must-clear-active_hash-too) for how it went on to delete the series.

Fix: `_real_playback_active()` (status not idle / YouTube up / a library item set / a live `library_play_task` or `stream_task`) is re-checked inside `_play_background_video()` **after** its awaits and **before** the VLC calls, with no `await` in between. If you add a new caller or a new await to that function, keep the guard as the last thing before VLC.

### Leaving Shuffle without a rebuffer — edit the tail, don't `pl_empty`

`/api/library/unshuffle` must swap the upcoming queue from the random tail to natural order while the **current episode keeps playing**. `pl_empty` is out — it drops the playing item and forces a relaunch (rebuffer). `_vlc_replace_upcoming` instead reads `playlist.json`, finds the `current` leaf, `pl_delete`s only the leaves **after** it, then `in_enqueue`s the natural tail. Clearing server state alone is *not* enough: VLC physically auto-advances through its **own** enqueued list at true end-of-file (the credits-skip path re-derives order via `_nav_order`, but bare end-of-file does not), so the stale shuffle tail would still play next unless the queue itself is rewritten. The endpoint keeps a `_vlc_relaunch_playlist`+reseek fallback for when the in-place edit fails, trading a brief rebuffer for guaranteed-correct order.

### Device Shuffle has no server mirror

On-device Shuffle Play is **purely client-side** — `startShuffle` Fisher–Yates-shuffles the paths and they become `lp.playlist` verbatim; the device never reads `state.library_shuffle_order`, and its Next/Prev just walk `lp.playlist`/`lp.pi`. Consequences: (1) leaving shuffle on device (`lpExitShuffle`) is a client-side `lp.playlist` reorder, **not** a call to `/api/library/unshuffle` (that endpoint is VLC-only); (2) the *persisted* shuffle preference is the one thing that crosses the boundary — the device records it via `POST /api/library/{id}/shuffle-pref` (since it has no `/play` round-trip) so Resume's "Resume on shuffle?" offer works regardless of where the last session played. Don't assume `state.library_shuffle_order` reflects device playback — it's always empty for on-device sessions.

**Handoff is the one place shuffle state must be re-established explicitly.** A TV↔device handoff slices the *already-shuffled* remaining tail (so the order survives for free), but the destination otherwise has no idea it's a shuffle. Both handoffs therefore thread the **shuffle flag + scope**: `handoffToDevice` → `lpPlay(..., app.library_shuffle, app.library_shuffle_scope)` (the scope comes from `state_snapshot`'s `library_shuffle_scope`, masked to `""` when not shuffling); `lpHandoffToVlc` → `playLibraryFiles(..., lp.shuffle, lp.shuffleScope)` (its `/play` body re-arms `state.library_shuffle_order`). Forget either and the new side plays the right tail *once* but snaps Next/Prev back to natural order, hides Exit Shuffle, and clears the persisted Resume-on-shuffle pref. Also: don't let `lpPlay` expand a 1-file shuffled tail into the natural-order series tail — it's gated on `!shuffle`.

### The resume-hint seek fallback must match `playlist[0]` — and `seekTo` of 0 ≠ null

`/api/library/{id}/play` resolves the start position from `req.seek_first_to`, falling back to `find_resume_hint(item, profile)` when it's `None`. That hint's `position_sec` belongs to the profile's **last-watched** file — which is only `playlist[0]` for the auto-select case (caller passes `files: []` and the backend builds the playlist *starting from* the resume file). For an explicit file list (**Shuffle Play**, "play from episode N", a single-file pick) `playlist[0]` is a *different* file, so the hint's position is meaningless there. Applying it anyway seeks the new first file partway in — the classic "shuffle randomly starts ¼ of the way through" bug. Two guards, both required: (1) the backend only adopts the hint when `hint["file_path"] == playlist[0]`; (2) the frontend play funnel sends `seek_first_to: (seekTo==null?null:seekTo)`, **not** `seekTo||null` — Shuffle and play-from-start pass an explicit `0` ("start at the top, do NOT resume"), and `0||null` collapses it to `null`, re-triggering the hint fallback. Keep both: the frontend guard fixes the common case, the backend match guard is the safety net for any caller that omits `seek_first_to`.

### Boot-time fullscreen — pass `--fullscreen` AND loop the focus pass

When StreamLink launches via the system service at boot/login, the dashboard's `background_video_loop` kicks `in_play` to VLC within a few seconds of the desktop coming up, then calls `vlc_focus_and_fullscreen()`. A single focus + minimize-others pass is **not enough**: startup apps (Discord, Steam, OneDrive, the browser, etc.) launch on a staggered schedule across the first ~20 s after logon and pop up *after* our pass already ran, leaving them on top of VLC with the taskbar/Dock visible. Pressing Stop in the UI doesn't have this problem because by then the desktop is fully settled, so a single pass catches every window.

Defenses (all required):
1. **VLC is launched with `--fullscreen`** in every spawn path (`run.py start_vlc`, `watchdog.py vlc_spec.build_args`, `main.py _restart_vlc_process`). This makes VLC come up fullscreen even before any media is loaded, so there's no race with the desktop on the cold start.
2. **`vlc_focus_and_fullscreen` loops for ~24 s** on a slowing cadence (6× 0.5 s, then 8× 1 s, then 6× 2 s). Each iteration: re-runs `_vlc_assert_focus` (Windows: `_minimize_other_windows_windows` + `_vlc_focus_windows`; macOS: AppleScript `activate VLC` + hide-other-apps; Linux: `wmctrl -a VLC`), polls `vlc_status`, re-issues the HTTP `fullscreen` toggle if VLC is `playing`/`paused` but not fullscreen, and on Windows re-runs `_stop_vlc_flash_windows` to clear any taskbar attention flash. The loop bails early if `state.stream_status == "buffering"` so it doesn't fight a new pipeline taking over. Total wall-clock comfortably outlasts a typical Windows logon's startup-app churn.
3. On macOS, the focus pass hides every other visible app via AppleScript (`set visible of (every process whose visible is true and name is not "VLC" and frontmost is false) to false`). This is the macOS counterpart to the Windows `_minimize_other_windows_windows` call — without it, the user sees the menu bar / Dock / Finder windows on top of VLC.

Don't shorten the loop back to a single pass — the visible regression is "tiny VLC window at boot with the taskbar still showing and Discord/Steam/etc. on top." Don't drop `--fullscreen` either — without it, the very first frame after `in_play` is windowed and the user sees a flash before the toggle settles.

### Windows window control needs ctypes

`_find_vlc_hwnds_windows` uses `EnumWindows` via ctypes; the EnumWindowsProc wrapper must be kept alive (`cb = EnumWindowsProc(_cb)` and pass `cb`, not `_cb` directly) or ctypes will GC it and the callback will crash.

### Focus-stealing prevention → flashing taskbar → visible taskbar

When VLC is relaunched in the background (DETACHED_PROCESS, e.g. after `/api/retry`), a plain `SetForegroundWindow` is usually blocked by Windows' focus-stealing prevention. The fallback is a **taskbar attention flash** on VLC's icon — and a flashing icon also forces the taskbar to stay visible **even over a fullscreen window**, so the user sees both the flashing icon and the taskbar until they click it.

`_vlc_focus_windows` ([main.py:707](../main.py#L707)) defeats this with the full cocktail: zero `SPI_SETFOREGROUNDLOCKTIMEOUT`, synthesize an ALT keypress (any keystroke releases the foreground lock), AttachThreadInput, `BringWindowToTop` + `SetForegroundWindow`, then `_stop_vlc_flash_windows` (FlashWindowEx with `FLASHW_STOP`) to clear any flash that was already raised. `vlc_focus_and_fullscreen` calls `_stop_vlc_flash_windows` a second time after toggling fullscreen, because Explorer can re-raise the flash when the window changes state. Don't drop either flash-stop call — without them the retry-then-flash bug returns.

### OS-volume (pycaw/COM) on Windows MUST run out-of-process — never in the server process

The host OS mixer is driven via pycaw/COM on Windows (the YouTube-on-TV volume path and the global Host Volume slider, via `set_system_volume`/`get_system_volume`). Calling pycaw/COM **inside the server process crashes the whole server**: after a handful of rapid OS-volume calls the process vanishes with a **native access violation and NO Python traceback** — nothing to catch, nothing in the logs. Symptom: the first few volume changes work, then the server is just gone.

Things that **don't** fix it (we tried, they don't): scoping the COM pointers tightly; doing all calls on one dedicated thread with `CoInitialize` once + **never** `CoUninitialize`; serializing calls behind a lock. comtypes still takes the process down. A native crash can only be contained by an **OS process boundary**.

The fix (in place since 3.12.2): all Windows volume ops run in a long-lived child process, [winvol_helper.py](../winvol_helper.py). `main.py` spawns one instance and talks to it over stdin/stdout — one JSON line per request (`{"op":"get"}` → `{"ok":true,"value":N}`; `{"op":"set","pct":N}` → `{"ok":true}`). `_winvol_request` (serialized by an `asyncio.Lock`, 5 s timeout) respawns the child if it dies; `poll()` detects a dead child, a closed pipe / timeout triggers `kill()` + respawn. If COM blows up again it kills only the helper, never the server. The child `CoInitialize`s once and never uninitializes, on its own main thread — the canonical-safe pattern, now isolated so a failure is survivable. **If you add a new OS-volume caller, go through `set_system_volume`/`get_system_volume` — never import pycaw/comtypes into the server process.**

### Smart Skip countdown marquee

The auto-skip countdown popup is drawn by VLC, not the dashboard. VLC's HTTP interface has **no marquee command** (see its `requests/README.txt` — there's no `marq`/OSD verb), so the only way to put dynamic text on the video output is the **`marq` sub-source** configured at launch with `--marq-file=<path>`: VLC re-reads that file every `--marq-refresh` ms. `main.py` writes the countdown text into `<repo>/.vlc_marquee.txt` (`_marquee_write`, atomic `os.replace`) and empties it to clear.

Four traps:
- **Emptying the file does NOT clear the marquee.** `marq` reads `--marq-file` with `getdelim()`, which returns EOF on an empty file — so the filter keeps the *previously-rendered* text (and logs a read error every refresh tick). To clear, write a single **space**, not `""` (`_marquee_write` maps empty → `" "`). A space is a valid non-empty line that forces the update but renders no glyph (we draw no background box). Proof it works: the visible 5→1 count is a series of non-empty→non-empty updates; the space makes the final clear one too. `run.py` / `watchdog.py` also seed the file with a space.
- **The launch args live in three places** — `main.py` `_vlc_marquee_args()` (used by `_restart_vlc_process`), `run.py` `start_vlc`, and `watchdog.py` `vlc_spec`. All three must pass the same `--marq-*` flags and point `--marq-file` at the same `<repo>/.vlc_marquee.txt`. Change one, change all three.
- **The marquee file path must resolve identically across processes.** It's anchored to the repo root via `Path(__file__).parent` (all three modules live there) — *not* `tempfile.gettempdir()`, which can differ between the system-Python `run.py`, the venv `main.py`, and a service-launched `watchdog.py`. Create it empty before launch so `marq` has something to open.
- **Don't add `--freetype-background-*` for an opaque box.** The freetype background opacity/color is a *global* text-renderer setting — turning it on to box the marquee also boxes every regular subtitle line. The countdown is intentionally text-only (opaque white + VLC's default outline). `--marq-position=10` is natively Bottom-Right; `--marq-x`/`--marq-y` add the corner padding.

### The three intro-seek sites must stay in sync — and none of them may re-add padding

An intro skip lands from three independent places: the auto-skip countdown
(`_run_skip_countdown`), the manual **Skip intro** button (`POST /api/skip-now`), and the
on-device HLS player (`lpAcceptSkipOffer` in `static/index.html`). Same hazard class as
the marquee launch args — three copies of one number, and nothing fails loudly when they
drift.

All three used `int(end_at) + 1`: floor, then add a whole second, so the real landing
point was +1.00 to +1.99 s past the detected boundary. That was deliberate padding for a
boundary nobody trusted. It is obsolete now that boundaries come from chapter markers
(exact) or from the silence between theme and dialogue (measured +1.33 s mean error, i.e.
already at or just past the first content audio) — stacking the old fudge on a corrected
boundary simply skips one to two seconds of the episode.

The server side now goes through `_intro_seek_target()` (`SKIP_INTRO_PAD_SEC = 0.5`,
round-to-nearest because VLC's HTTP `seek` takes whole seconds); the device player uses
`LP_SKIP_INTRO_PAD = 0.25` because it seeks with sub-second precision. **If a skip ever
looks like it lands slightly early, fix the detection, not the pad** — a bigger pad hides
a detection bug on every show in the library to paper over one.

**The START pad is a separate number and follows the opposite rule.** Don't read the
paragraph above as "keep every pad at zero": it is about where a skip *lands* (intro
end). Where a skip *fires* (intro start) is governed by `SKIP_INTRO_START_PAD_SEC = 1.5`
in `main.py`, mirrored by `LP_SKIP_INTRO_START_PAD` in `static/index.html` — a fourth
copy of a shared number, same drift hazard as the three above.

The two ends are not symmetric in cost, which is why they get opposite treatment:

| | firing late | firing early |
|---|---|---|
| **intro start** | a second of theme the viewer was skipping anyway | **cuts the last moment of the preceding scene — real content, silently gone** |
| **intro end** | lands inside the episode, skipping content | lands back in the theme, obvious and self-correcting |

So the start deliberately errs late and the end deliberately errs tight. Both auto-skip
paths used to fire at exactly `intro.start`, which handed the viewer every bit of the
marker's authoring slack: chapter markers are placed by hand and routinely sit a beat
before the first frame of the opening (on the outgoing scene's fade rather than the cut).
Reported on Attack on Titan S4 as about a second of the pre-OP scene disappearing. Go
through `_intro_skip_at()` rather than comparing against `intro.start` directly, and
don't "fix" a late-feeling intro skip by driving this back to zero.

### Smart Skip fingerprinting triggers on stream prep, not on download — and failures are sticky

Audio fingerprinting (`analyzer.py`) is kicked off by `_ensure_analysis_for` at the end of `_run_offline_job` (a successful HLS bundle), **not** by `library_download_monitor`'s ready-flip — that call was deliberately removed. Two consequences to keep in mind: (a) **content that is never stream-prepped never gets `skip_data`** — fine because auto-prep is on by default, but don't "restore" a download-ready trigger expecting both. (b) The hook is **fire-and-forget**: awaited only to schedule (one `get_library` + `create_task`), never to run the pass, so it must never block the prep job or fail the bundle — keep it wrapped in try/except like the STT hook. A `/prep-all` fires the hook once per file; `_schedule_series_analysis_if_eligible` has a running-guard (skip if the series job is already `running`) so they don't stack.

**Failures don't auto-retry.** `_needs_reanalysis` used to return `True` for `source=="failed"`, so a failed file re-ran on every sibling prep — churning the analyzer pointlessly. Now a failed file re-runs only when `ANALYZER_VERSION` bumps (failed entries store the current version, so the `version < ANALYZER_VERSION` check qualifies them after a bump) or when an admin clicks **Analyze** in the Smart Skip tab (`_run_series_analysis` directly, bypassing the guard). Don't add a `failed → retry` shortcut back into `_needs_reanalysis`.

**macOS: no fingerprinting at all.** Since prep is the only trigger and prep is disabled on macOS (`HLS_AVAILABLE` false, the TCC `~/Downloads` block), fingerprinting never runs on a macOS host. This is acceptable — the analyzer's ffmpeg/fpcalc children read the source from the same protected dirs and would hit the identical TCC block. Windows/Linux (the real targets) are unaffected.

**Analysis coalesces behind prep — don't trigger per file.** Every analysis pass re-fingerprints the *whole* series (new files need peers to match against), so kicking a pass per prepped file made the analyzer restart from episode 1 after every bundle — quadratic work and an admin progress bar that visibly kept restarting. `_schedule_series_analysis_if_eligible` therefore defers while `_series_prep_active` (any `pending`/`processing` prep job for the series); `_watch_prep_then_analyze` backstops prep exit paths that never fire the post-prep hook (crash, sibling-race `done`). `"paused"` prep intentionally does **not** defer (a parked queue can sit for hours).

### `subprocess.run(text=True)` on Windows silently returns EMPTY stdout with rc=0 — always pass `encoding="utf-8"` to a media tool

The most expensive hour in this repo so far. *Hacks S04E06* failed HLS prep every 5 minutes for hours with "no video stream in source", while the same ffprobe read it perfectly from a shell and the file turned out to be a flawless Matroska (H.264 + EAC3 + 29 subtitle tracks).

The chain:

1. The release names its subtitle tracks in their **own scripts** — `Български`, `Ελληνικά`, `ไทย`, `中文（繁體）` — where every other file in the library used ASCII names (`Bulgarian`, `Greek`). ffprobe returns those titles inside its UTF-8 JSON.
2. `text=True` decodes the child's output with **`locale.getpreferredencoding()`** — the Windows **ANSI code page**, cp1252 here — in **strict** mode. Bytes `0x81 / 0x8D / 0x8F / 0x90 / 0x9D` have no cp1252 mapping, so the decode raises `UnicodeDecodeError`.
3. On Windows `subprocess` drains the pipes from **reader threads**. The exception is raised *there*, kills that thread, and **never propagates to the caller**. `subprocess.run` returns `returncode=0` with an **empty `stdout`** and an empty `stderr`.

So a healthy 2 GB file is indistinguishable from one with no streams, and every error message downstream is a lie — the API genuinely handed us rc=0 and no output. `-v error` guarantees stderr is empty too, removing the last clue.

**Rule: any subprocess whose output can carry media metadata, a file path, or human text gets `encoding="utf-8", errors="replace"` — never bare `text=True`.** ffprobe, ffmpeg, fpcalc and whisper.cpp all emit UTF-8 regardless of the host locale. This is not hygiene; omitting it is a silent data-loss bug that only fires on non-Latin content, which is exactly the content you will not have in your test library.

Note the asymmetry that hid this for so long: the **async** ffmpeg paths (`create_subprocess_exec` + `out.decode("utf-8", "replace")`) were always correct, because they decode by hand. Only the blocking `subprocess.run(..., text=True)` sites were wrong — which is why encoding worked while probing didn't.

**Deliberately NOT converted:** OS-tool calls (`sc.exe`, `netsh`, `mullvad`, `osascript`, `pactl`, `nvidia-smi`, the reboot commands) in `run.py` / `daemon.py` / `watchdog.py` / `main.py`. Those emit locale-encoded text, so forcing UTF-8 would corrupt them on a non-English Windows. The split is by *what the child writes*, not by convenience.

### Derive the ffprobe path from ffmpeg's FILENAME — a blanket `str.replace` breaks every Windows install

`ffmpeg_bin()` on Windows returns `tools/ffmpeg/ffmpeg-8.1.1-essentials_build/bin/ffmpeg.exe`. `ff.replace("ffmpeg", "ffprobe")` rewrites **all three** segments → `tools/ffprobe/ffprobe-…/bin/ffprobe.exe`, which never exists. That silently demoted every Windows host to the stderr-parsing fallback in `_media_duration`, whose `re.search` then raised `TypeError: expected string or bytes-like object, got 'NoneType'` on a `None` stderr — surfacing as `Analysis crashed` for **every series in the library**, with no hint of the real cause. Use `Path(ff).with_name(Path(ff).name.replace(...))`, and keep the fallback `None`-safe.

### A chromaprint frame index is a window START, and the rate is 8.0768 — not 7.8

`FP_FRAMES_PER_SEC` was `7.8` for the analyzer's whole life and it was simply wrong. fpcalc
resamples to 11025 Hz and hops 4096/3 ≈ 1365.33 samples per chroma frame → **8.0775 frames/s**.
Measured against the fpcalc 1.5.1 build `setup.py` installs, on synthetic clips of exactly known
length: `frames = 8.07677 × seconds − 21.43`, stable across 30 s → 360 s. (The old comment beside
the constant said "8192 samples @ 11025 Hz", which computes to 1.35 frames/s — the number never
followed from its own arithmetic, which is the tell.)

Three things follow, and each has bitten:

1. **It was a SCALE error, not an offset, so it grew with the timestamp.** An intro *start* at
   12 s was off by +0.4 s (invisible); the *end* at 105 s by +3.7 s (the "skips too far past the
   intro" report); a credits point 480 s into the tail window by +17 s (never reported — you just
   watch more credits). If a Smart Skip bug ever presents as "the start is fine but the end is
   wrong", suspect a scale error before you suspect the matcher.
2. **Self-calibration must be AFFINE.** The classifier needs a 16-chroma-frame window and the
   first FFT frame must fill, so a fingerprint is short by a constant ~21.4 frames regardless of
   window length. A naive `len(fp) / window_sec` reads 7.37 at a 30 s window and 8.02 at 360 s —
   it would under-correct by 8.8% on short windows. `_rate_for()` adds `FP_FRAME_LEADIN` before
   dividing, and falls back to the constant outside `FP_RATE_SANITY`.
3. **A frame index is the START of its ~2.23 s analysis window, not a point in time — and you must
   NOT compensate for that.** It's tempting to add half a window, or a whole one. Don't: a frame
   only matches cross-episode while its window is *mostly* shared audio, so the last matching frame
   already sits ~1 s **before** the true boundary. Compensating pushes boundaries later, which is
   the wrong direction for the bug this all exists to fix. Treat the coarse value as ±3 s uncertain
   in both directions and resolve it against a measured silence edge instead.

### The tail fingerprint's time base must be the `-ss` value you actually used

`_fingerprint_one` seeked the outro window at `int(duration − OUTRO_SEARCH_SECS)` and the finalize
loop converted the matched offsets back using the **float** `duration − OUTRO_SEARCH_SECS`. Those
are different numbers, so every `credits_start` was late by `frac(duration)` — always late, never
early, and invisible because it's sub-second next to a 17 s scale error. The seek value is now
carried through `_fingerprint_one`'s return into `tail_starts[]` rather than re-derived, and
`_fpcalc_raw` formats `-ss` at millisecond precision. **Any future window that seeks before
fingerprinting must return its own base the same way** — never recompute it at the other end.

### Trim a gap-bridged run's ENDS, never its middle — and never tighten the gap

Gap bridging is load-bearing (see the entry below) but it cuts both ways at the *ends* of a run.
Post-intro silence is correctly refused as match evidence by `_informative_mask` — and then bridged
over anyway, so whatever coincidentally matches within 4 s on the far side (a shared sting, a stock
transition) drags the reported boundary out with it. The run still legitimately "ends on a true
match", so `_best_gap_run`'s own construction can't catch it. `_trim_weak_edges` drops a run's
**outermost** segment when it is both shorter than `MATCH_EDGE_MIN_FRAMES` and separated by a gap
of at least `MATCH_EDGE_GAP_FRAMES`. Interior structure is deliberately untouched — that's what
keeps a "theme, 2 s voiceover, 30 s more theme" match whole. Don't "simplify" this into a smaller
`MATCH_GAP_FRAMES`.

### Every cluster member must be expressed through the SAME consensus window

`_resolve_offset_in_cluster` used to give the anchor the intersected window and every *other*
episode its raw pairwise `(offset_in_ep, length)`. So a single pair that bridged too far became
that episode's intro end directly, with nothing to check it. Measured on a real library before the
fix: 101 Attack on Titan episodes sharing one fixed 90 s opening reported intro durations from
25.6 s to 102.8 s — a 77 s spread, 93 of them longer than the opening actually is. `_project()`
now maps the anchor consensus window into each member's own frame coordinates through its pair
alignment (a constant shift), clamped to that member's matched evidence and fingerprint length.

Two things to preserve if you touch it: (a) a member the projection can't place falls back to its
raw match — **never** drop it, because a missing "skip intro" button is a worse regression than a
loose one; (b) `_intersect_match`'s quantile trim uses `int(n * CLUSTER_TRIM_Q)`, which is 0 for
n < 5, so small clusters stay bit-identical to the old hard intersection. That's deliberate: the
least-evidenced clusters are the ones you least want to change.

### Smart Skip matcher needs gap tolerance — and uses numpy

Each chromaprint frame is computed over a **~2.4 s audio window** (hopped ~0.128 s), so a single second of episode-specific audio inside the theme (title-card voiceover, an SFX) corrupts **~20 consecutive fingerprint frames**. A matcher that demands strictly consecutive matching frames truncates real intros at the first such blip (the "skip ends mid-intro" report) or misses them entirely. `_find_longest_match_np` bridges mismatch gaps up to `MATCH_GAP_FRAMES` (~4 s) requiring `MATCH_MIN_RATIO` (60 %) matched frames overall — safe because a random cross-episode frame match at Hamming ≤ 6/32 is ~0.03 % likely. Don't "tighten" the gap back to zero.

**But stationary audio breaks the "random match is unlikely" assumption.** Silence, drones, and sustained tones make chromaprint emit runs of *near-identical* hash frames, and those degenerate stretches bogus-match each other across episodes for tens of seconds (observed: a 22 s *consecutive* false run between two different sine sweeps — long enough to fool even the strict matcher). That's why a frame only counts as match evidence when it's **informative** — ≥ `MIN_FRAME_DELTA_BITS` changed vs its own predecessor, in **both** episodes (`_informative_mask`). If you loosen the matcher further, re-test against low-entropy audio, not just noise.

The fast path needs **numpy** (vectorized XOR + popcount LUT, ~50-100× the pure-Python loop); a venv missing numpy silently falls back to the slow, strict `_find_longest_match_py` — if analysis is mysteriously slow on a host, check `python -c "import numpy"` in its venv before profiling anything else.

### Fingerprint subprocess work must NOT ride the default loop executor

The analyzer's blocking calls (`_media_duration`, `_fpcalc_raw`) go through `analyzer._fp_thread` → a **dedicated** `_FP_EXECUTOR` thread pool — *not* `asyncio.to_thread`. This is load-bearing: `asyncio.to_thread` runs on the event loop's single default `ThreadPoolExecutor`, and `get_library()` / `put_library()` ride that **same** pool. Each fingerprint parks its worker for the entire decode (head 6 min, tail 10 min of audio → seconds each), so fingerprinting several series at once flooded the default pool; library reads/writes — which the progress tracker (every 2 s), the download monitor, and essentially every HTTP handler depend on — then queued behind the decodes and the **whole dashboard stalled while the host (and RDP) stayed responsive** ("fingerprinting many shows stalls the server"). The event loop was never blocked; its only thread pool was. A separate pool for analyzer subprocess work keeps the default pool free. Don't "simplify" `_fp_thread` back to `asyncio.to_thread`. Belt-and-suspenders: `main.py`'s `_analysis_gate` semaphore (`ANALYSIS_CONCURRENCY=2`) also caps how many series fingerprint at once so a bulk **Analyze** can't oversubscribe CPU with match processes either.

### Smart Skip credits: never fabricated — and cross-episode agreement is NOT proof

**Rewritten in 12.5.0, after the 12.4.0 version of this entry was proved wrong in the
field.** The rule below still stands and the two removed fallbacks must stay removed.
What follows is why the first attempt to relax it failed, because the reasoning error is
much easier to repeat than the code.

12.4.0 added a structural pass (`refiner.credits_candidate`) for shows that use a
different song every episode. It took the last **gap-free block of sound** running to the
end of the file, on the theory that speech has sub-second pauses and a music bed does
not. It was vetoed unless the fingerprint had broadly failed, it passed the same two
acceptance gates a fingerprint match must pass, and it required peer episodes to agree on
the roll **length**. That sounded like evidence. It was not.

It shipped, and a viewer reported the Hacks credits skip firing about a minute early.
Measured across 11 episodes it was early **every single time**, by 6.6 s to 69.1 s.

Two lessons, both worth more than the deleted code:

1. **The boundary was not in the audio at all.** These shows run a song across the seam
   from the last scene into the credit roll. On Hacks S05E05 the music starts ~6 s before
   the cut and never drops below −40 dB again, so "the last gap-free block" begins
   wherever the dialogue's last pause happened to be — 19 s inside real content. No
   threshold tuning fixes a boundary that leaves no trace in the signal being measured.
2. **Cross-episode consensus validates PRECISION, NOT ACCURACY.** Every episode was wrong
   in the same direction, so they agreed with each other and the consensus check ratified
   the bias. Median detected roll was 111.6 s; the episodes whose credits came from the
   trustworthy fingerprint path showed 24.8 s and 52.0 s. **Agreement among estimates
   that share a systematic error is not corroboration.** If a new detector's only
   safeguard is "the peers agree", it has no safeguard.

The replacement (`refiner.credits_from_subtitles`) measures a different thing entirely:
where the **dialogue** stops. A subtitle track states that directly, costs nothing to
read (no decoding — the cues are demuxed as text), and on the same episode lands within
0.1 s of the true cut. It is self-validating per episode rather than by committee, which
is also why it works on single-episode library items where a peer-agreement rule would
reject everything.

Getting credits EARLY destroys content the viewer can never get back; getting them LATE
costs a few seconds of credits. Those are not equivalent, so the detector errs late
(`SUB_LATE_PAD`) and every guard fails closed to "no credits" rather than to a guess.

The removed fallbacks had none of those properties — `duration * 0.92` and "this frame is
dark" are formulas, not measurements. Don't re-add those.



The matcher finds **any** recurring audio in the tail window, not specifically credits. So when an episode's real credits are absent/short/different, it used to latch onto whatever else recurs — a stinger, a repeated gag (the "credits kick in early and cut off the end of the show" report, e.g. an American Dad tag line) — and the old black-frame / flat-`duration*0.92` fallbacks **fabricated** a `credits_start` when nothing matched. Both are gone. Credits time now comes **only** from a confirmed cross-episode match that passes two correctness tests in the finalize loop: it must **start late enough** (`credits_start ≥ duration × MIN_CREDITS_PCT`) **and run to ~the end** (`credits_end ≥ duration − OUTRO_END_MARGIN_SEC`) — real credits reach the file end; a recurring mid/late cue is followed by more content, so its run ends early and is rejected. `_filter_cluster_consensus` additionally prunes a cluster member whose anchor-side offset disagrees with the cluster median (it matched a *different* region than the real credits). Consequences to keep in mind: a single file with no peers gets **no** credit skip (nothing to match), and an episode whose credits genuinely don't recur gets **no** skip rather than a guess. Don't re-add a `%`-of-duration or black-frame fallback — "no outro unless matched" is the intended behaviour.

### A subtitle/media subprocess timeout is a SILENT wrong answer, not an error

`refiner.subtitle_cues` extracts cues with `-ss … -i <file> -map 0:<n> -f srt -`. ffmpeg
reads the container from the seek point to EOF, so the wall clock tracks **how fast the
file can be read**, not how much subtitle text there is. Local disk: seconds. A remote or
slow mount: minutes — measured at 240 s for a single 2.2 GB episode over a LAN HTTP mount.

The failure is silent and looks exactly like a correct negative. `run_capture` raises
`TimeoutExpired`, the `except Exception: return []` swallows it, zero cues come back, and
the detector concludes "this file has no usable subtitles" and returns no credits. No
error, no log line, no chip. It cost a debugging cycle here: the shipped function returned
`None` on the very file the prototype had just resolved correctly, and the only difference
was a 180 s timeout vs 600 s.

`SUB_CUES_TIMEOUT = 600` is sized for a slow mount on purpose. If you ever tighten it, or
add another detector that shells out over a path that might not be local, remember that
"empty result" and "timed out" are indistinguishable to every caller downstream — the same
hazard as the `-loglevel error` trap documented above.

### The shot-boundary credit scan must stay OUT of `analyze_series`

`shot_scan_loop` is a separate worker, and the temptation to fold it in as another
`_STAGE_SPAN` stage should be resisted every time. Inside the analysis pass it would:
persist nothing until the whole series returned (so a 3-minute-per-file pass would show
no result for hours), hold one of the two `_analysis_gate` slots, keep the series
`status="running"` — which blocks `_any_analysis_running()` and the maintenance loop's
idle checks, the exact opposite of "runs last" — and park the progress bar where users
read it as a hang. That last one is not hypothetical: the 12.4.0 structural pass did it
and cost a real debugging cycle (see the 12.4.2 entry).

The ordering it enforces is the point, and all three conditions matter:
`_any_analysis_running()` false, `_fingerprint_backlog(lib) == 0` (no audio work left
*anywhere* in the library, not just this series), and `_machine_in_use(120)` false. Then
ONE file per tick.

It also calls `analyzer.run_offloaded()`, **not** `asyncio.to_thread`. A multi-minute
video decode on the event loop's default pool would queue `get_library()`/`put_library()`
behind it and stall the dashboard — the same failure documented above for fingerprinting.

The write-back re-checks every premise under the library lock and **discards** its result
rather than merging when anything changed underneath it: a re-analysis, a manual edit, or
a cheaper detector landing credits while the scan ran. Audio and chapters outrank it; a
hand edit outranks everything.

### Night mode toggles by relaunching VLC — there's no runtime audio-filter command

Night mode is VLC's `compressor` audio filter (dynamic-range compression: pull loud peaks down, lift quiet dialogue up), with three user-selectable intensity presets (`light`/`medium`/`max`). VLC's Lua HTTP interface has **no command to add or remove an audio filter on a running instance** — `--audio-filter` is read only at launch. So changing night mode (`POST /api/settings/night-mode`) cannot be a live VLC command; `_apply_night_mode` snapshots the current file + position, calls `_restart_vlc_process` (which appends `NIGHT_MODE_PRESETS[state.vlc_night_mode_preset]` when `state.vlc_night_mode` is set), then replays the file + playlist tail and seeks back so it's seamless mid-movie. A no-op (already in the requested state), **or a preset change while night mode is off**, persists the setting but skips the relaunch — so the user isn't kicked out of playback for nothing.

Three consequences:
- **The preset args live in three places** — `main.py` `NIGHT_MODE_PRESETS` (used by `_restart_vlc_process`), `run.py` `night_mode_args()` (boot), and `watchdog.py` `night_mode_args()` (crash recovery). Same rule as the marquee args: change one, change all three. `run.py`/`watchdog.py` read both `settings.vlc_night_mode` + `settings.vlc_night_mode_preset` straight from `library.json` (they don't import `main.py`), so the persisted settings are the single source of truth and boot/crash relaunches honour them.
- **The preset is remembered independently of the on/off toggle.** `vlc_night_mode` (on/off) and `vlc_night_mode_preset` (intensity) are separate persisted keys; turning night mode off and back on reuses the last intensity. The POST merges whichever field(s) the caller sent — the fullscreen moon button sends `night_mode` only, the settings-menu picker sends `preset` only — so neither control clobbers the other.
- **A change restarts VLC**, so it's deliberately low-frequency. The on/off toggle is a subtle moon button in the fullscreen overlay header **and** a checkbox in the global section of profile settings; the **intensity picker is settings-menu only** (not in the fullscreen UI). The audio/subtitle track selection resets on the relaunch; `_apply_night_mode` re-applies the saved library track prefs via `_apply_track_prefs` to compensate.

## qBittorrent

### A teardown delete must re-derive ownership from `library.json` — never trust `state`

Stream teardown deletes the ad-hoc torrent **and its files** (`deleteFiles=true`), and decides "is this disposable?" from a pair of `AppState` fields: `active_hash` set + `library_item_id` clear ⇒ temporary stream. That pair desyncs, and when it does the delete lands on a saved series. It has happened for real: a background-video takeover cleared `library_item_id` while `active_hash` still pointed at the library torrent, and the next 🏠 Home press removed a 76-file, 33.8 GB item from qBit and from disk (v11.9.2).

So **every** stream/prepare teardown delete goes through `_qbit_delete_transient()` ([main.py:1388](../main.py#L1388)), which calls `_hash_backs_library_item()` and refuses when any library item carries that info-hash — whatever `state` claims. Two rules:

- **Never call `qbit_delete(h)` bare in a teardown path.** Its `delete_files` defaults to **`True`**, so the destructive form is the one you get by forgetting an argument. `qbit_delete` direct is only for *explicit user intent* (delete a library item, admin Cleanup).
- **The guard is not redundant with the `state` check** — it exists precisely because the `state` check is known-fallible. Keep both.

This also covers a second, independent path to the same loss: **qBit dedupes adds by info-hash**, so `/api/stream/prepare` or `/api/library/prepare` on a torrent already in your library returns that *same* hash — and dismissing the file picker (`DELETE /api/stream/cancel`) then deleted the library copy.

### Anything that clears `library_item_id` must clear `active_hash` too

They're read as a pair (above). `_play_background_video()` and `_sync_state_from_vlc()`'s no-match branch both blank the playback fields; both must blank `active_hash` as well, or they mint the exact "ad-hoc stream holding a library torrent" state that gets files deleted.

### `setSequentialDownload` doesn't exist

The qBittorrent API endpoint is `toggleSequentialDownload`. It's a toggle, so check `seq_dl` from `qbit_info` before calling — see `qbit_streaming_mode` ([main.py:344](../main.py#L344)). Sequential is also passed at add-time as the `sequentialDownload=true` form field to `/torrents/add`.

### Enable first/last-piece priority when streaming — the last piece carries the index

Sequential download alone only puts the **head** of the file on disk. That is not enough for a tail-index container — an MP4 with its `moov` atom at the end (the common, **non**-web-optimised torrent) or an MKV whose Cues are appended last. VLC opens such a file, finds no seek table, and stops right after the buffer gate **without ever reporting a duration** (`length` stays 0), so `_vlc_wait_until_ready` times out and the rebuffer guard — which keys off a known duration — can't recover it either. That is the "play now fails after buffering even on good torrents" report: it's not a speed problem, so a fast swarm doesn't help.

Fix: `qbit_first_last_piece_prio` (`toggleFirstLastPiecePrio`, a toggle — read `f_l_piece_prio` first) is enabled **alongside** `qbit_streaming_mode` on every stream-while-downloading play (`_begin_library_file_stream`, `stream_pipeline`). The last piece is a single piece regardless of file size, so grabbing it first costs almost nothing while the rest still arrives sequentially from the head — the standard webtorrent/peerflix approach. `_sequential_off_when_complete` flips both back off once the streamed file finishes so the rest of a library download proceeds normally.

The earlier policy here said the opposite ("fetching the last piece breaks piece-order streaming") — that reasoning was wrong for tail-index containers and was the actual cause of the bug. Don't re-disable it.

### Download-priority tiers use qBit 1/6/7 — and the default tier "mid" is 6, not 1

The three exposed download-priority levels map onto qBit's file-priority tiers in `_file_mode_to_priority`: `low → 1` (Normal), `mid → 6` (High), `high → 7` (Maximal). **`mid` is the default and maps to 6, not 1** — and legacy `now` now reads as `mid` (6) too. This is only ever observable *relative to other files in the same torrent* (qBit orders by priority; an all-equal torrent behaves identically whether every file is 1 or 6), and `_reconcile_item_downloads` still leaves a plain "download now, no per-file overrides" item completely untouched — so nothing changes for un-prioritised torrents. Don't "fix" `mid` back to 1 thinking 6 is a bug: a `mid` vs `low` split must produce a real qBit ordering difference, which 6-vs-1 gives and 1-vs-1 wouldn't. The `download_scheduler_loop` remains the single writer of these priorities (see LIBRARY_DATA.md).

### Prep priority orders QUEUED bulk work only — it never preempts a running encode

`item.prep.priority_default` + per-file `priority` (low/mid/high, mid default) order the single-slot **bulk/auto-prep** queue via the park gate in `_run_offline_job` (`_higher_priority_bulk_pending`). Two hard boundaries to preserve: (1) **interactive (play-on-device) + admin force-prep still outrank every bulk tier** — they're counted by `_priority_hls_pending`, a separate gate, and `_preempt_running_bulk` boots an in-flight bulk encode for them; a *higher bulk tier* does **not** get that preemption (HLS can't checkpoint, so killing a running encode to reorder bulk-vs-bulk just wastes work). (2) The gate keys on a **strictly-higher** tier, so equal tiers stay FIFO and the top tier never parks — no starvation, no deadlock. `POST /api/library/{id}/prep-priority` re-stamps already-queued jobs' `_prep_prio` so a change reorders the queue immediately without touching the running encode. See STREAMING.md.

### LocalHost auth is disabled

`setup.py` writes `WebUI\LocalHostAuth=false` to qBit's ini. Localhost requests never need a cookie. `qbit_login` is still called on startup and `qreq` retries on 403 for safety, but the cookie is mostly cosmetic.

### An unchosen save path must be the emptiest drive, not always `QBIT_DOWNLOAD_PATH`

Every add path used to fall back to `settings.qbit_download_path`, so a box with
`LIBRARY_PATH_2..4` configured still put **everything** on the primary drive: it filled to
100% (stalling downloads, breaking prep and playback) while a second, empty drive sat idle.
When no folder is picked, `_auto_save_path()` now ranks every root from `_all_library_paths()`
by `shutil.disk_usage(...).free` and returns the emptiest. Details that matter if you touch it:

- **Free space is compared at GB granularity**, then ties break on the configured primary and
  then on configuration order — so several folders on *one* physical drive (identical free
  space) behave exactly as before, and two near-equal drives don't flap on a few bytes.
- **A root that can't be stat'd is not a candidate** (unplugged USB drive, a path deleted after
  it was configured); if none are usable it falls back to `settings.qbit_download_path`, never
  to an empty string.
- **It reads the library** (via `_all_library_paths`), so it must never be called while
  `_lib_lock` is held — `library_download` resolves the path *before* opening its transaction.
- **It is resolved once, at add time, and persisted** (`pending_download.save_path`,
  `download_source.save_path`), so a restart-recovery re-drive resumes to the same drive rather
  than re-rolling the dice on whatever is emptiest now.
- The dashboard pre-fills its Save Location from `GET /api/settings/download-path`, which
  returns the same auto-pick, and re-fetches it **every time the modal opens** — a cached value
  would keep aiming downloads at a drive that has since filled up.
- **A root can opt out** (`settings.library_paths_no_auto[]`, toggled per path in the Storage
  modal): it stays a library path, a destination chip and a valid explicit `save_path`, but the
  automatic pick skips it. That's the NAS / archive-drive / read-only-disc case — being a good
  place to *keep* media doesn't make it a good place to *dump downloads*. Two things to preserve
  if you touch this: the flag covers **static .env roots too** (it lives in `library.json`, not
  .env, so `QBIT_DOWNLOAD_PATH` itself can be opted out), and **opting everything out ignores the
  opt-outs** rather than failing — a download has to land somewhere, and a config that says
  "nowhere" is a mistake, not an instruction. Removing a path drops its opt-out with it, or
  re-adding that path later would come back silently excluded.

### A pre-added torrent ignores the save path unless you `setLocation` it

`/api/library/prepare` adds the magnet just to read its file list, long before the user presses
Download — so it cannot know the destination, and the torrent lands in qBit's default folder.
`library_download_pipeline` then skips `qbit_add_magnet` entirely when a `torrent_hash` is passed,
which meant **any download made through the file picker silently ignored the chosen Save
Location** — including the auto-picked one. The pipeline now `qbit_set_location`s a pre-added
torrent onto the resolved path before real data is written (cheap then; a full content move
later). If qBit refuses (409 — unwritable path), it logs and keeps the torrent's *current* path
as `save_path`, because `build_file_list` must describe where the files really are.

### Sequential vs library downloads

Stream-now uses sequential. Library downloads do NOT — they should download normally so all files arrive. See [BACKEND.md](BACKEND.md#pipelines).

### The download scheduler is the single writer of scheduled items' file priority + pause

For any item with `download.mode=="idle"` or per-file overrides, `download_scheduler_loop` reconciles qBit **every 15 s** from `library.json → item.download`. So a raw `qbit_set_file_priority` / `qbit_pause` / `qbit_resume` written **outside** `_reconcile_item_downloads` for such an item is reverted on the next tick. If you add a new "boost this file" / "pause this torrent" path, write the **model** (`download.files[path]=…` or `download.mode=…`) and call `_reconcile_item_downloads` — don't poke qBit directly. This is exactly why `queue-play` and `library_download_pipeline` were rewritten to set the model instead of calling `filePrio` (v4.7.0). Plain `mode=="now"` items with no overrides are left untouched (fast path), so unscheduled downloads behave exactly as before.

### A download's magnet must be persisted, or a restart orphans it

`library_download` creates the item with `status="downloading"` **synchronously** and runs the actual qBit magnet-add in a **detached** `library_download_pipeline` task — the hash isn't known until that task finishes (it waits up to ~60 s for torrent metadata). If the process restarts in that gap, the item is stuck forever: `library_download_monitor` skips items with no `torrent_hash` (`if not h: continue`) and nothing re-runs the pipeline, so the download sits in the ongoing list at 0% (the "queued downloads stuck after restart" bug). The magnet itself lived only in the now-dead task's arguments — it was never on disk — so there was nothing to recover from either. Fix: `library_download` persists the magnet + add params in `item["pending_download"]`; the pipeline **clears it the instant it records the hash** (so a fully-added item never carries it); and `_recover_interrupted_downloads` (awaited in `lifespan` before the monitor spawns) re-drives the pipeline for any `downloading` item still carrying a `pending_download.magnet`. Re-adding a magnet qBit already has is a no-op, so the recovery is idempotent even if the add had actually gone through before the crash. **If you add another path that creates a `downloading` item, persist its recovery params the same way** — the monitor will not resurrect a hash-less item on its own.

### `/torrents/add` returns `"Fails."` for an already-present torrent — that's not a failure, adopt it

qBittorrent's `/api/v2/torrents/add` returns the literal body `"Ok."` on success and `"Fails."` (still HTTP 200) when it *rejects* the add — and the most common reject reason is that **the torrent is already in the session**: a duplicate bulk add of the same episode, a prior errored attempt still sitting in qBit, or a torrent whose files are already on disk. `qbit_add_magnet` used to `return h if r.text.strip()=="Ok." else None`, so any of those made the caller think the add failed. In `library_download_pipeline` a `None` return marks the item `status="error"` with **no `torrent_hash`** — even though the torrent is present and its file is downloading/complete in qBit. The item never renders as playable and the download is orphaned (the "bulk-downloaded episode finished but the UI won't play it" bug — seen as duplicate `error` rows with empty hashes while the file sits on disk). Fix: `qbit_add_magnet` extracts the info-hash from the magnet up front, so on a non-`"Ok."` reply it falls back to polling `qbit_info(h)` a few times and **returns `h` if qBit is already tracking that hash** (adopt the existing torrent); only a hash that genuinely isn't present returns `None`. `library_download` also dedups on the resolved info-hash so a repeated bulk add returns the existing live item instead of minting a second one that then errors on the qBit reject. Base32 magnets are normalised to hex by `extract_hash`, so the comparison matches qBit's lowercase hash for v1 torrents. Don't revert `qbit_add_magnet` to a bare `"Ok."` check. Pre-existing orphans from before this fix are reclaimable via the admin **Cleanup** tab (`_adopt_torrent_to_library` / recover-item).

**Link-style results (11.1.0):** some indexers hand Jackett no magnet at all — `_shape_search_results` then falls back to the raw `Link` (a Jackett `/dl/…` `.torrent` proxy URL). There is **no `btih` in that URL**, so `extract_hash` returns `None` and the old `qbit_add_magnet` returned `None` even on a **successful** `"Ok."` add — the caller thought the add failed (Search-tab Play errored with "qBittorrent rejected the magnet" while the torrent quietly downloaded; library downloads of such results orphaned the same way). Fix: when the hash can't be pre-extracted, `qbit_add_magnet` snapshots qBit's torrent set **before** the add and, after `"Ok."`, polls up to 30 s for a hash that wasn't in the snapshot (newest `added_on` wins if several appear). A duplicate link-add that qBit silently drops still returns `None` after the poll — rare and surfaced as a normal error.

### Play-now persists (11.2.0) — don't route the dashboard Play through the transient `/api/stream`

Play-now from Search/Explore/the show page **must not delete on Stop**. The transient `/api/stream` (`stream_pipeline`) leaves `state.library_item_id` `None`, so `_stop_cleanup` deletes the torrent + files — great for a throwaway watch, wrong for "I played it, keep it." The dashboard now routes every picker Play through **`/api/library/play-now`**, which adopts the prepared torrent into the library (an item with `library_item_id`), so Stop leaves it in place. Two footguns baked into that endpoint: **(1)** it must clear `state.prepare_hash` when it adopts that hash, or Stop's *prepare* cleanup (`if ph: qbit_delete(ph, delete_files=True)`) deletes the freshly-adopted torrent's files out from under the new item. **(2)** it dedups on the resolved info-hash (reuse a live item on the same hash) so repeat-Play doesn't mint duplicates. `/api/stream` is intentionally kept (a client may still want delete-on-stop), so don't delete it — just don't point the dashboard's Play at it.

### Streaming a still-downloading file: VLC reads the download head as EOF — a rebuffer guard resumes it

A sequentially-downloading file only has its first N% on disk. When VLC's playhead reaches that downloaded head mid-file it reads it as **EOF** and goes to `stopped`/`ended` (or, on some builds, sits "playing" but frozen at the head) — and **VLC never retries on its own** (the "it fails in VLC and doesn't retry" report). `_stream_rebuffer_guard` (spawned by both `_library_stream_file_launch` and the transient `stream_pipeline`, tracked in the module-level `_rebuffer_guard_task`) watches for either signature, flips the UI to `buffering`, waits until `_REBUFFER_AHEAD_SEC` (25 s) of new data is on disk ahead of the last position, then re-issues `in_play` + seek back to where it ran dry. It self-cancels via `_stream_anchor_ok` (anchored on `state.active_hash` + `state.active_file`, which Stop / a superseding play reset) and exits the moment the file finishes downloading (`progress ≥ 0.999`) — so it never fights a fully-downloaded file's normal EOF/auto-advance. Note the initial start gate is still just `BUFFER_MIN_MB`/`BUFFER_MIN_PCT`, so on a link slower than the bitrate the guard will cycle play→buffer→play — that's correct (you can't play faster than you download), not a bug to "fix" by disabling it.

### Streamability scoring (11.3.0) — seeders×size gate, and only a "good" source auto-plays

`_streamHealth(bytes, seeders)` decides whether a source can be watched *while it downloads*: seeders needed scales with file size (`need = max(6, round(GB*8))`), bucketed to `good`/`fair`/`slow`/`dead`. Three footguns: **(1)** it returns **`null` when seeders are `undefined`/`null`** (not 0 — 0 seeders is `dead`), and `null` means "unknown" → no badge, and the auto-play gate stays *open* (`_streamHealthAutoPlay(null)===true`). So every stream-now caller must actually thread `{seeders,size}` into `openStreamPicker`; drop it and you silently regress to the old always-auto-play behaviour. **(2)** Score the **file being streamed, not the whole torrent** — `selectStreamFile` uses the *selected file's* `size_bytes`, so a big multi-file pack is judged per-episode (using it whole would flag every pack "slow"). The badge on a *pack row* is the exception (it shows whole-pack size, since no file is chosen yet). **(3)** `openStreamPicker` only skips the picker for a single-file torrent when health is `good`; `fair`/`slow`/`dead` **keep the picker open** so it can't "flash and instantly cast to the TV" — that flash was the single-file auto-select firing on a slow source. `slow`/`dead` additionally `window.confirm` before play. Backend mirrors this only as feedback: the buffer loops (`stream_pipeline`, `_library_stream_file_launch`) emit `stalled:true` + a message note after 40 s under 50 KB/s — they never hard-fail, because the user explicitly chose to play anyway.

### Idle-download window must ignore downloads, or it self-closes

`_download_idle_open` (in `idle` mode) calls `_machine_in_use(idle_minutes*60, ignore_downloads=True)`. Without `ignore_downloads=True`, an idle-only download that *starts* during idle would immediately set `downloading_count > 0` → `_machine_in_use` True → window "closed" → scheduler pauses it → next tick it's idle again → restart… a flap. The `ignore_downloads` flag breaks that loop (a running download is not "activity" for the *download* window). Note `auto_prep_loop` and `scheduled_reboot_loop` deliberately do **not** pass it — for them a running download *is* a reason to hold off. Consequence: while an idle-only download is pending (paused, but `status=="downloading"`), `downloading_count > 0` keeps the box "in use" for prep/reboot, so idle-prep + the scheduled reboot won't fire until it completes (which it does on the next idle stretch). Acceptable: downloads take priority over prep; it self-resolves when the box next goes idle. (`auto_prep.mode == "always"` makes the download window always open regardless of this.)

### qBit pause/resume renamed in 5.x — `qbit_pause`/`qbit_resume` fall back

qBittorrent 5.x renamed the WebUI endpoints `pause`→`stop` and `resume`→`start` (old verbs kept as deprecated aliases). `qbit_pause`/`qbit_resume` POST the 4.x verb and fall back to `/stop`·`/start` on a 404 — keep that fallback (Windows is the primary target and may run either major version). `_reconcile_item_downloads` only pauses/resumes torrents in **download-phase** states (`downloading`/`stalledDL`/`metaDL`/…), never a finished/seeding one.

### Moving a series uses `setLocation` (keep seeding) — and the content move is ASYNC

`POST /api/library/{id}/move` relocates a series via `qbit_set_location`
(`/api/v2/torrents/setLocation`), which keeps the torrent seeding from the new path
— don't `shutil.move` a torrent-backed file yourself (you'd break the piece map and
halt seeding; that's only for non-torrent uploads).

**qBit's content move is ASYNCHRONOUS — never commit the library paths until the files
verifiably exist at the destination.** The v7.0.0 first cut rewrote `item.files[*].path`
*immediately* (and by string arithmetic), so for a large pack — especially cross-drive on
Windows — the library pointed at files qBit hadn't finished moving and the whole series read
as **missing** in Cleanup / on the TV / on-device (the v7.0.1 bug report). The endpoint now:
(1) fires `setLocation` and returns `status:"moving"` right away; (2) a background task
`_settle_series_move` polls `qbit_files(h)` + the current `save_path`, rebuilds the new paths
with **`build_file_list`** (qBit's authoritative layout — *not* `old_path.relative_to(old_save)`,
which could flatten the folder if the save paths didn't match), and **only `_commit_item_move`
once `all(Path(f).exists())`**. Until then the library is left untouched. Don't "optimise" this
back to an immediate path rewrite. (3) The co-located `.streamlink_cache/<key>` sidecar is moved
in `_commit_item_move` (qBit doesn't know about it), keyed by name+size (`_offline_cache_key_for`)
which the move preserves. Recovery for an item stuck missing: re-run the move to the same dest —
the settle reads qBit's real location and repairs the paths.

### "Ready" is gated on per-file completion, NOT qBit torrent state, when files are skipped/idle

A skipped file (priority 0) and an idle-deferred file (priority 0 while the window is shut) are both *not-wanted* as far as qBittorrent is concerned, so qBit reports the torrent **complete** (`uploading`/`stalledUP`) the moment the *wanted* files finish — even though the skipped files will never arrive and the idle files haven't fetched yet. The old monitor flipped the item to `ready` on that state, which (a) made a partial selection look fully downloaded, (b) ran audio fingerprinting against a missing set, and (c) abandoned idle-deferred files (a `ready` item is excluded from the scheduler). The monitor now flips `ready` only when **`_all_nonskip_complete`** is true — every non-skip file ≥ 99.9% downloaded — so ready + fingerprint always wait for the complete kept set. Don't revert this to a bare `qstate in (uploading, …)` check. Skipped files are also filtered out of analysis (`_analyzable_files`) and the `_item_skip_status` chip so they don't show as perpetually "pending"/"failed".

### Play a complete file from a still-downloading torrent — gate on `complete`, not `exists`

`/api/library/{id}/play` filters the playlist to `Path(p).exists()`, but qBit **pre-allocates** files, so a half-downloaded file *exists* on disk and would play as a stuttering/truncated stream. The "▶ Play" affordance on a download card's file row therefore renders **only** when the enriched `/files` reports `complete: true` (qBit per-file `progress >= 0.999`). Don't surface Play off mere existence. Playing sets `library_item_id`, so a subsequent `/api/stop` won't delete the still-downloading torrent (the usual library-playback guard).

### qBit keeps `progress=1.0` after a file is deleted out from under it — `delete-files` grounds skip-file completeness in disk existence

`POST /api/library/{id}/delete-files` (the "delete to free space, keep re-downloadable" action) marks files `skip` and `unlink`s their bytes, but qBittorrent's per-file `progress` stays at the cached `1.0` until a full recheck. So `get_item_files`, which derives `complete`/`dl_pct` from that qBit progress, would keep reporting a just-deleted file as complete and playable. The fix: for a file whose effective `mode == "skip"`, completeness is grounded in **`Path(path).exists()`** — `complete = qp>=0.999 and exists`, else `(0.0, False)`. Only skip-mode files are stat-ed (the only ones that can be stale this way), keeping the hot path cheap. Don't move the disk check ahead of the mode test for every file, and don't drop it — without it a freed file masquerades as on-disk and the "⊘ Not downloaded → ⬇ Download" UI never appears.

### A torrent can vanish from qBittorrent — the download monitor must notice, or the item hangs at "Downloading" forever

qBit keeps **no resume data for a magnet whose metadata never arrived**, so killing the process loses those torrents outright — and the VPN kill-switch kills it on every drop (see [§ VPN](#vpn)). `library_download_monitor`'s miss path used to be a bare `continue`, so the item kept polling a hash qBit no longer had, every 5 s, indefinitely: no error, no retry, no UI signal. (Two *Hacks S04* episodes sat like that for an hour.)

The rule: **`qbit_info(h) is None` is ambiguous** — it means "qBit is down" *and* "the torrent is gone". `_handle_missing_torrent` disambiguates with `qbit_info_all()` (which returns `None` only when qBit is unreachable, `[]` when it's up and empty) and counts only the genuinely-gone case, so a qBit restart or VPN blip never trips the error path. Then: re-add from `item["download_source"].magnet` at 30 s and 90 s, error the item at 3 min. Any new code that treats a missing torrent as "skip this tick" needs the same disambiguation.

### Auto-retrying a dead release: gate on BYTES, match the episode, and de-dupe by release identity

Three traps in `_retry_dead_download`, each of which turns a helpful feature into a destructive one:

1. **Gate on bytes fetched, not on qBit's peer counts.** "0 seeders" is what the user sees, but `num_seeds` is connected seeds — qBit reports a seed that never serves a piece, and a perfectly healthy torrent can be mid-handshake with none. `_note_download_stall` gates on `completed == 0` and resets on *any* progress, so a slow-but-live download is never abandoned.
2. **Zero-progress only.** A torrent that stalls half-downloaded has bytes worth keeping and seeders that may come back. The retry deletes the old torrent *with its files*, which is only safe because it can't fire above 0 B. Don't relax the trigger without revisiting that delete.
3. **An episode item must only ever be offered the same episode.** A search for `Hacks S04E02` returns the season pack too, and a retry that silently pulled eight episodes would be far worse than the stalled card it fixed.

And the one that only shows up against real data: **de-duplicate candidates by release identity, not just info-hash.** The same release is routinely indexed under two hashes by two trackers, so a hash-only exclusion list hands back the dead release's twin on the very next attempt and burns a retry on identical content. `_release_key` collapses the title to bare alphanumerics for that comparison, and `download_attempts` stores both keys.

Finally: the swap is **in place** (same item id), which keeps the user's progress and library position — but it also silently rewrites the card's title. Surface it (`retry_count` → the "Release N" chip) or the UI looks haunted.

**Don't rank candidates by the indexer's seeder count alone.** That number is advertised, not measured, and it was wrong every time it mattered here: one release claimed 47 seeders with none reachable, its replacement claimed 50 and sat at zero. Sorting by it means working down a list ordered by a figure that doesn't predict success, and with a 3-retry cap you can exhaust the budget before reaching a release that would have worked. `_proven_release_groups` — the groups of every `ready` item, i.e. torrents that actually completed on this box over this VPN — outranks it; seeders stay the tiebreaker. When parsing the group, strip the tracker tag and extension first (`…-successfulcrab[EZTVx.to].mkv`) and deny-list format tags, or half the library's group is "dl" from `WEB-DL`.

### A show's own title is a weak query for its later seasons

The Packs tab was filled purely from the broad `?q=<show title>` search, and it simply did
not contain what the user wanted. `q=Hacks` returned season packs for S01-S03 and none for
S04. `q=Hacks S04` returned four, the best at 182 seeders — more than any pack the broad
query found. Nothing was broken; the title is just a bad query for a specific season,
because every episode, franchise sibling and same-word film competes for the same result
budget, and the newest seasons lose.

**Anything that needs per-season coverage must ask per season.** `ssSearchSeason`
does for a season what `ssSearchEpisode` does for an episode — and both exist for the
same reason, which is worth noticing as a pattern rather than two coincidences.

**12.7.0 — on a long-running show the budget doesn’t skew, it excludes, and it takes the
episodes with it.** Hacks was the mild version: the packs were missing but the episodes
were there. `q=Futurama` returns episodes for seasons 1, 5 and 7–14 — and *nothing at all*
for S02, S03, S04 and S06, because the 2023–2026 revival’s releases fill the result budget
on their own. Four entire seasons of a show whose every season is sitting on the indexers
read as “No source found”, on the one screen whose job is to say what you can still get.
`q=Futurama S02` returns them instantly.

So the targeted per-season query now buckets its **episodes** as well as its packs
(`ssSearchSeason` → `_ssMergeEpisodes` + `_ssMergePacks`), and it is fired in three places
rather than waiting to be clicked: after the broad "Search episodes" pass, for every season
that came back bare (`_ssSweepBareSeasons`, detached); when the user lands on an empty
season tab (`_ssEpisodeSeasonHint`); and as the first phase of the bulk run
(`_bgEnsureSources`). One query per season, not twenty per season — the per-episode sweep
is the mop-up, not the mechanism.

**Don’t diagnose this from the flat `results[]`.** `/api/search` de-duplicates into ~30 flat
rows but the group carries the lot (192 for Futurama), so a shallow look at the response
suggests a truncation that isn’t there. Count per season inside `groups[].results` — that is
where the hole is visible.

The second half of the problem was presentation: that query also returned 154 non-episode
rows, ~144 of them `movie`-kind noise, against 10 real season packs. A flat
relevance-then-seeders list buries a 14:1 minority no matter how it is sorted — group by
the axis the user is actually choosing along (season), and give the noise its own
collapsed section. Render a heading for every season the show HAS, not just the ones with
results, or "no pack exists" and "nobody looked" are indistinguishable.

### A shared HTTP client in a proxy is a session-laundering machine

`https_proxy.py` sends every request through one long-lived `httpx.AsyncClient`. An
`httpx.AsyncClient` **keeps a cookie jar by default**, and a jar on a shared client is not
a cache — it is a credential shared between every caller the proxy serves.

`verify-pin` sets `streamlink_profile_token` as a cookie (not `Secure`, so one PIN entry
covers the http and https origins of the same host). So one PIN entry over HTTPS made the
proxy replay that session for **every device on the LAN**, handing out the two things a
bare `profile_id` is explicitly not trusted for: admin-locked items, and the delete
endpoints.

**The tell was a difference between ports, not an error.** Nothing failed; the same
unauthenticated request simply answered differently:

```
GET /api/library   :80  (direct)  → 34 items,  verified_profile_id = null
GET /api/library   :443 (proxied) → 37 items,  verified_profile_id = <someone>
```

When a proxy sits in front of an app, **compare the two ports before trusting either**.

Two traps in the fix:

1. **Subclass `http.cookiejar.CookieJar`, not `httpx.Cookies`.** `Cookies.__init__`
   rebuilds its state from what you hand it — a `Cookies` instance is copied
   cookie-by-cookie into a fresh jar, so a `Cookies` subclass is silently discarded and
   the leak survives. Only the `else` branch keeps the object verbatim (`self.jar =
   cookies`), and it wants a raw `CookieJar`. The first attempt at this fix did it the
   wrong way and looked right until it was tested against a real `Set-Cookie`.
2. **Don't collapse response headers through a `dict`.** `Set-Cookie` legally repeats, and
   a dict keeps only the last. It survived unnoticed because the app only ever sets one at
   a time — exactly the kind of latent bug that surfaces the day something sets two.

A proxy must be transparent about credentials: forward what the client sent, remember
nothing of its own. Its state must never outlive a single request.

**Related, not fixed:** changing a profile's PIN does **not** invalidate that profile's
existing session tokens (12 h TTL, persisted). Deleting the profile does neutralise them —
both `_is_elevated` and `_require_delete_auth` require the profile to still exist.

### `os.replace` is not atomic-on-demand on Windows — it needs a retry

`_save_lib_raw` writes `library.json.tmp` and `os.replace`s it into place. On Windows that
raises `PermissionError` — `ERROR_ACCESS_DENIED` (5) or `ERROR_SHARING_VIOLATION` (32) —
whenever **any** process holds a handle on the target without `FILE_SHARE_DELETE`:
Defender scanning the file we just wrote, the Search indexer, a backup/sync agent. It
clears in milliseconds. POSIX `rename` has no equivalent failure, so this is invisible on
a dev Mac and routine on the deployment target.

It is not rare. The archived logs carry **584** of them, **564 from `update_progress`** —
`library.json` is rewritten every ~15 s during playback, so the hot path is the exposed
one, and every failure is one silently discarded resume position.

**Any Windows atomic-write needs a bounded retry** (`_SAVE_RETRY_DELAYS`, ~1.9 s), and the
retry must cover the *temp write* as well as the replace — the previous tick's `.tmp` can
still be held by the same scanner, and failing there loses the write just as completely.
Other `os.replace` sites in the tree (marquee, metadata cache, HLS repair) have the same
exposure; the marquee one swallows all exceptions, which is fine for a cosmetic file and
would not be for anything the user owns.

### `fetch` does not throw on 4xx/5xx — `try/catch` is not error handling

The watch-progress save wrapped its POST in `try/catch` and put the offline-stash fallback
in the `catch`. `fetch` rejects only on a *network* failure, so the 500 returned by a lost
library-write race **resolved normally** and was treated as a successful save: no stash, no
retry, position gone. 564 of them.

The same file already documented this trap two lines above, for the offline case:
*"the POST below would RESOLVE as a 404 (not throw), so the catch-based fallback never
fires"*. It was never generalised. **Check `r.ok`. A `catch` block is not an error path.**

### A poll loop needs to know what "never" looks like

Two instances, both found by counting repeated log lines rather than reading errors:

- **prep-status.** The only exit was `r.ok` + "nothing processing"; everything else fell
  through to an unconditional `setTimeout(tick, 3000)`. Deleting a library item therefore
  left every client that had it on screen polling a dead id forever — **675 requests each
  for two items, one every 3 s for 76 minutes, from two devices**, all 404.
- **The whole admin panel.** Every tab is a 1.5–4 s poller sending `authHeader()`, each
  treating non-OK as "try again later". When the token died, `/api/admin/updater` alone
  answered 401 every minute for **nearly eight hours**, with nothing on screen saying the
  session had ended.

**Classify the failure before deciding to retry.** 404/410 = gone, 401/403 = not yours:
terminal, stop. Everything else is transient and gets a *budget*, never infinity — a poll
loop with no failure ceiling is how one blip becomes a permanent background request. For a
page with ~100 call sites, catch it once at the transport (wrap `window.fetch`) rather than
editing every caller.

### Reject the impossible request; don't persist it as a broken row

`POST /api/library/download` accepted `{"magnet": "not-a-magnet"}` and minted a library
item, which then failed and settled into `status: "error"` with an **empty** error string —
a permanent row the user can only delete, with nothing saying why.

The guard has to be loose, though: `extract_hash` returning `None` is **normal**, because
indexers without magnets hand out a Jackett `/dl/` .torrent proxy URL that carries no
info-hash (`qbit_add_magnet` snapshots qBit's torrent set to find the new hash afterwards).
So the check is "link-shaped" (`magnet:` / `http://` / `https://`), not "has a hash" —
rejecting on the hash would have broken every hashless indexer.

### A screen that never updates reads as a broken feature, not a stale screen

Downloads reported their progress over SSE to the library grid and to the library card's
file expander. The **episode page** — the screen you are actually on after tapping into a
show, and the obvious place to watch a download you just started — got none of it. The
percentage sat at whatever it was when the page opened. Leaving and re-entering fixed it.

Nobody reports that. They report "the download is stuck", or they press Download again,
which is how a refresh bug turns into a duplicate-download bug. **Ask, for any screen that
can be open while its subject changes: what repaints it?** A screen with no answer is a
photograph.

Two traps in the fix, both of which make it worse than the bug:

1. **The repaint must preserve where the user is.** `renderEpList` rebuilds
   `#epList.innerHTML` and `#epList` is the scroll container, so repainting resets
   `scrollTop` to 0. A list that jumps to the top every few seconds while you read it is a
   worse screen than one that never moves. `_epLiveRefresh` carries `scrollTop` across.
2. **Per-item events arrive per item.** `library_progress` fires for every downloading
   item every few seconds; a season coming down eight episodes at a time is eight events a
   tick. Coalesce behind one trailing timer, or the "fix" is a fetch storm.

### Making a repeat action safe is only half of it — say what happened

`/api/library/download` dedupes by info-hash and returns the existing item with
`duplicate:true`, so double-clicking Download has never created two torrents. The client
ignored the flag and said "Downloading — switch to Library" either way. That is the wrong
half of the problem solved: the person clicking twice is doing it *because nothing looked
like it was happening*, and the reply tells them they have now started a second copy.

Worse at bulk scale — a re-run of a season already coming down reported "Queued 20/20"
for twenty no-ops, because the run counted HTTP successes. It now counts `duplicate`
separately and says "Queued 0/20 — 20 were already downloading".

**An idempotent endpoint still owes the caller the difference between "done" and "already
done".** Silent idempotency is indistinguishable from a UI that ignored the click.

### A display name is not a search query

The library’s "find the rest of this season" handoff built its TMDb candidate as
`title: epHeroTitle || epMetadata.title`, and `epHeroTitle` is what **this box** calls the
item. For anything the user never renamed that is the release name, so the search show
page opened believing the show was called
`Futurama-1999-S01 1080p WEBRip 10bit EAC3 5 1 x265-iVy`, and every query it then built —
`<title> S02`, `<title> S02E01`, … — was a guaranteed miss. Twenty of them, reported as
"no source found for S02E01…S02E20", which is indistinguishable from a season that really
has no releases.

**The tell is in `access.log`, not in the UI.** The screen can only say "nothing found";
`grep -o '/api/search?q=[^ "]*' access.log` says *what was asked*, and the bad title is
obvious the moment you look. Reach for it before theorising about indexers — an empty
result and a nonsense query look identical from the client.

Two names, two jobs, and they are only usually the same string:

- **`_ssQueryTitle()`** — what an indexer is asked. TMDb’s title wins; every query builder
  on the show page (`_ssBroadSearch`, `ssSearchSeason`, `ssSearchEpisode`, `_bgCtx().query`)
  goes through it.
- **`_ssGroup.title`** — the show’s identity in the library, used as the `series` tag on a
  queued download. Left as the caller’s name deliberately: episodes fetched from a series
  page must land back in *that* series, whatever it is called.

Anything that derives a query from a title the user could have renamed needs the same
split.

### Never let a batch count itself against the subset it managed to build

Bulk season download derived its scope from `_ssEpisodes` — the episodes that searching
had found a torrent for. So an episode with no source was not merely un-downloadable, it
was **invisible**: filtered out of the job list, and then absent from the denominator too.
A ten-episode season with two unsourced episodes reported *"Queued 8/8 — success"*.

**The denominator must come from what the user asked for, not from what the code managed
to resolve.** Scope is now TMDb’s episode list unioned with found episodes
(`_ssBulkScopeEpisodes`), so a sourceless episode is still counted and named in the toast.
The same applies to a download that fails to start — name it, don’t drop it from the tally.

The trigger was environmental and worth knowing on its own: **an indexer returning zero
results is not evidence that none exist.** Jackett fans out to five indexers and returns
200 with whatever came back; a bare pass is common and usually transient. Hacks S05E09/E10
came back empty at 21:19:23 and were found by the identical query at 21:20:50. Anything
that treats one empty search as “no such release” will be wrong regularly — retry the gaps
before concluding anything, which is what `_bgEnsureSources`’ second pass does.

### A timeout that lives in memory never expires on a box that restarts

The dead-swarm retry shipped with its 10-minute clock as an in-memory tick counter, and
on this machine it never once fired. The logic was correct; it simply never got to run.
StreamLink restarts for auto-updates, the scheduled reboot and every VPN watchdog bounce,
and each restart reset the counter to zero. *Hacks S04E02* sat in `metaDL` at 0 B through
three restarts in one evening, each one wiping the clock well before 120 ticks.

**Any countdown measured in monitor ticks is really measuring uptime, not elapsed time.**
If the threshold is longer than the gaps between restarts, it is unreachable. Persist the
start (`stalled_since` on the item) and compare wall-clock instead.

Then mind the other edge: a stamp that outlives the process can condemn a torrent for time
it was never given. qBit restarts alongside StreamLink and needs a moment to re-resolve
DHT, so an item carrying yesterday's stamp would be retried within 5 s of boot.
`_DOWNLOAD_STALL_BOOT_GRACE` (3 min of uptime) covers that, and the stamp is cleared on
every exit from `downloading` so a replacement torrent starts with a clean clock. The same
reasoning applies to `_missing_torrent_ticks`, which is still tick-based — deliberately, as
its thresholds (30 s / 90 s / 3 min) are short enough to complete inside one uptime.

### A magnet stuck in `metaDL` moves no bytes — don't render it as "Downloading"

A magnet whose swarm is dead parks in qBit's `metaDL` state: no file list, no peers, 0 B, forever. The card read a confident "↓ Downloading" the whole time, which is exactly how two dead releases hid for an hour. The monitor now sets `awaiting_metadata` on the `library_progress` event (`qstate == "metaDL"` or an empty `qbit_files`) — the card reads **"Finding peers…"** — and, once it has fetched nothing for `_DOWNLOAD_STALL_TICKS` (10 min), switches to a different release rather than waiting it out (see the entry above). Note that `torrents/add` answering `"Ok."` proves nothing about whether the swarm will ever deliver metadata.

### Admin Cleanup must never touch an in-use torrent or escape the download folder

The Cleanup tab (`/api/admin/cleanup*`, see [ADMIN.md § Cleanup](ADMIN.md)) deletes torrents, files, and library items, so two guards are load-bearing:
- **In-use protection.** `_cleanup_in_use_hashes(lib)` is the single source of truth for "don't touch": the live stream/prepare torrent (`state.active_hash`, `state.prepare_hash`) and every `status=="downloading"` item's torrent. Those are excluded from the orphan list, flagged **In use** on a broken row, and their recover/delete endpoints return **409**. If you add another way a torrent can be "live" (a new pipeline, a new active-hash field), add it here or Cleanup can delete a torrent out from under a running playback/download.
- **Stray-file path guard.** `DELETE /api/admin/cleanup/stray` resolves the target and requires `target.relative_to(settings.qbit_download_path)` (rejects traversal, the folder itself, anything outside) **and** refuses a path a current torrent owns. A trailing `.!qB` (qBit's incomplete-file marker) is stripped before the ownership test in `_cleanup_inventory_sync` — otherwise an actively-downloading single file looks unowned and would be offered as stray. Never relax these to string-prefix-only checks; use `Path.resolve()` containment (Windows path/case variants bite).
- **qBit unreachable ⇒ `qbit_ok:false`, not "everything is broken".** `qbit_info_all()` returns `None` (vs `[]`) on failure so the inventory flags the offline state instead of classifying every library torrent as broken/orphan.

## VPN

### Two enforcement points

1. `vpn_guard` in `main.py` ([main.py:997](../main.py#L997)) — kills qBit when VPN drops; gates `/api/stream` and `/api/library/download` via `state.vpn_secure`
2. `watchdog.py` ([watchdog.py:343](../watchdog.py#L343)) — kills qBit if it's running while VPN is down, AND refuses to restart it until VPN reconnects

If you're tempted to remove one, **don't**. They cover different failure modes:
- `vpn_guard` runs inside the dashboard process and protects the API
- `watchdog.py` runs in a thread (or as a separate service) and protects the process

### Verification mode: `mullvad` / `generic` / `off` (shared via `vpncheck.py`)

`settings.vpn_killswitch.mode` (admin: **VPN Kill Switch → Verify VPN Using**, mirrored to `state.vpn_mode`) selects HOW all three enforcement points verify the VPN. The mode-read (`vpncheck.vpn_mode()`, reads `library.json`) and the provider-agnostic tunnel check (`vpncheck.generic_tunnel_up()`, psutil interface scan) live in the leaf module **`vpncheck.py`** so `main.py`, `run.py`, and `watchdog.py` agree — `run.py`/`watchdog.py` deliberately don't import `main.py`, so the shared logic can't live there.

- **`mullvad`** (default) — `mullvad status` must report `Connected`.
- **`generic`** — any VPN tunnel interface up (WireGuard/OpenVPN/NordLynx/…). For non-Mullvad VPNs, no vendor CLI.
- **`off`** — kill-switch disabled: `vpn_guard` forces `vpn_secure=True`, never kills qBit, and the UI shows a muted "VPN OFF" pill instead of the overlay. `watchdog._vpn_connected()` returns True. **Only** `off` may let qBit run without a VPN — never weaken `mullvad`/`generic`.

### Mullvad CLI missing → treated as unsafe (in `mullvad` mode)

In `mullvad` mode, both guards return `vpn=False` if `mullvad` is not in PATH. Cannot-verify = unsafe. Put the CLI on PATH (or set `_MULLVAD_BIN` in `.env`) — **or** switch the mode to `generic`/`off` in the admin panel if you don't use Mullvad.

### Kill-switch `block_ui` governs the UI lockout ONLY — never the qBit kill

`settings.vpn_killswitch.block_ui` (admin toggle, default `true`) decides whether a VPN drop locks the whole dashboard behind the full-screen overlay (`true`) or only the qBit kill happens with the rest of the UI left usable (`false`). **It does not gate the qBit kill or the P2P endpoint 403s** — those are unconditional in both `vpn_guard` and `watchdog.py`. If you ever wire `block_ui` into the kill path you've reintroduced a leak: a VPN drop must always terminate qBittorrent regardless of this setting. The overlay is purely a frontend concern driven by `state.vpn_block_ui` (mirrored from the setting, broadcast in the `state` + `vpn_status` SSE events).

### VPN-down feature greying must NOT use `pointer-events:none`, and the click guard runs in the CAPTURE phase

When the VPN drops in `block_ui:false` mode, the dashboard greys the controls that need qBittorrent (Search, result Save/Play, Recheck) via `body.vpn-down .vpn-gated { opacity:.4 }` and blocks them. Two non-obvious constraints:

1. **Don't grey with `pointer-events:none`.** That would kill the hover `title` tooltip (the whole point — telling the user *why* it's disabled) and stop the click guard from ever seeing the event. Greying is opacity/cursor only; pointer-events stay on.
2. **The click guard is a `document`-level listener registered with `capture=true`.** Capture fires on `document` before the event reaches the target, so `e.stopPropagation()` there prevents the gated element's own inline `onclick`/`addEventListener` handlers from running at all. A bubble-phase guard would fire *after* the target's handlers (too late). Don't "simplify" it to a bubble listener or to setting `disabled` on each button (native `disabled` also suppresses the title tooltip and isn't reachable for dynamically-rendered result rows). Keyboard/programmatic paths that don't go through a pointer click (Enter in the search box, `epRecheckSelected`) call `vpnBlocked()` directly. The server still 403s (`/api/stream*`, `/api/library/download|prepare`, and now `/api/library/{id}/recheck`) — the greying is UX, not the security boundary. See [FRONTEND.md](FRONTEND.md), [ADMIN.md § VPN Kill Switch](ADMIN.md).

### qBittorrent pops over TV playback after a VPN blip

Launch it minimized + non-activating. The classic sequence: the internet drops while the **LAN stays up**, so Mullvad disconnects → the watchdog kills qBittorrent (the unconditional kill-switch invariant above). When the internet comes back the VPN reconnects and the watchdog **relaunches qBit** — and on Windows a plain `subprocess.Popen(qbittorrent.exe)` brings its GUI window up in the **foreground**, covering the fullscreen VLC playback / idle background video on the TV. Nobody's at the keyboard to dismiss it.

Fix is two-layered, and you need **both**:

1. **Launch minimized + non-activating.** `_launch_bg` (both [watchdog.py](../watchdog.py) and [run.py](../run.py)) takes a `no_activate` flag; when set it passes a `STARTUPINFO` with `dwFlags |= STARTF_USESHOWWINDOW` and `wShowWindow = 7` (`SW_SHOWMINNOACTIVE`) — minimized **and** it never grabs foreground focus. Only the qBit `ServiceSpec` / `start_qbittorrent()` set it; VLC must still come up fullscreen/foreground, so don't flip it globally. This is the layer that actually saves you when qBit's tray config isn't honoured.
2. **Config: start in the tray, no window at all.** `setup.py`'s qBit ini writes `General\SystrayEnabled=true`, `StartMinimized=true`, `MinimizeToTray=true`, `CloseToTray=true`. On a configured box qBit lands silently as a tray icon — there's no window to steal focus in the first place. This only takes effect after `setup.py` runs (the auto-updater invokes it non-interactively), and needs a qBit restart to load; the launch-level fix covers the gap and any box where the tray isn't available.

Don't rely on config alone (a pre-existing box or a suppressed tray still flashes a window) or on the launch flag alone (some Qt builds re-activate; the tray config makes it truly windowless). `STARTUPINFO`/`STARTF_USESHOWWINDOW` are Windows-only symbols — they're referenced only inside the `SYSTEM == "Windows"` branch so `setup.py`/`run.py` still import on macOS/Linux.

## Jackett

### `Category[]=0` returns no results

Jackett treats `0` as an unknown category ID, not "all". To search all categories, omit the `Category[]` parameter entirely. See `/api/search` ([main.py:2272](../main.py#L2272)) — only passes `Category[]` when `INDEXER_CATEGORIES != "0"`.

### Remote Jackett vs local

`INDEXER_URL` hostname is parsed in `run.py` and `watchdog.py`. If it's `localhost`/`127.0.0.1`/`::1` → try to launch + monitor locally. Otherwise → reachability check only, never launch. This is the correct behavior — remote Jackett shouldn't be launched from the local machine.

### Windows service vs tray exe

The Jackett Windows installer registers a `Jackett` Windows service that runs as LocalSystem and actually serves port 9117. `JackettTray.exe` is cosmetic — it shows the icon and offers a "Start background service" menu item. Both `setup.py` and `watchdog.py` prefer the service (via `sc.exe start Jackett`) and only fall back to launching the tray exe.

Service config files live under LocalSystem's profile: `C:\Windows\System32\config\systemprofile\AppData\Roaming\Jackett` or `C:\ProgramData\Jackett`. **Not** the interactive user's `%APPDATA%`. The `--verbose` mode of `run.py` searches all five candidate locations.

### Port-open is NOT a Jackett health check

A hung Jackett keeps its TCP listener socket bound (so a port-connect "succeeds") while it has stopped answering HTTP. A bare port check therefore reports a wedged Jackett as healthy forever and never restarts it — the long-standing "Jackett stops after a while, only a reboot fixes it" bug. The watchdog (and `run.py`'s startup reachability check, and `main.py`'s `jackett_health_monitor`) now probe **HTTP** `GET {INDEXER_URL}/UI/Login` (served without auth — any HTTP status proves the web stack is alive) via `_http_ok()`. Liveness = "answers HTTP", not "port open".

### …but one slow HTTP probe must NOT trigger a restart — the watchdog needs a `failure_grace`

The flip side of the HTTP health check: Jackett's mono/.NET web stack can **briefly** stop answering `/UI/Login` within the probe timeout while it's merely *busy* — fanning a `/api/search` out to every indexer, or under CPU load from the idle auto-fingerprint/auto-validate maintenance. The Jackett restart is **destructive** (force-kill + a ~40 s mono cold start), so a single false-negative probe took the indexer offline for ~40 s; the user saw `502 Indexer unreachable` from `/api/search` that "eventually works after a few retries." Fix: `ServiceSpec.failure_grace` (Jackett = 2, others 0) — the watchdog requires **consecutive** failed probes (`_health_misses` > grace) before it acts, so ~1 min of *sustained* failure still recovers a genuinely wedged Jackett but a transient slow response doesn't. The probe timeout was also bumped 4 s → 6 s. This mirrors `main.py`'s `jackett_health_monitor`, which already waited `FAIL_BEFORE_RESTART=6` probes before its backstop restart — the watchdog killing on the *first* miss was the asymmetry that caused the regression. **Don't drop the grace back to 0 for any HTTP-health-checked service.**

### Restarting a hung Jackett needs a force-down first

`sc.exe start Jackett` is a **no-op** (returns 1056 ALREADY_RUNNING) when the service is wedged-but-RUNNING — that's why `sc start` alone never recovered it and a reboot was required. The watchdog's Jackett `ServiceSpec` has a `pre_restart` hook (`_force_stop_jackett_windows` / `_kill_by_name`) that forces the old process down (service stop, waiting for STOPPED; hard-kill fallback) **before** relaunching, so the port frees and the restart actually takes. `ServiceSpec.start()` then waits on the HTTP health check (not just the port) so it doesn't tight-loop while Jackett's web stack is still warming up.

### Controlling the LocalSystem Jackett service needs admin

A non-elevated StreamLink (the normal install: Task Scheduler at logon, no `/RL HIGHEST`) **cannot** `sc stop`/`sc start` a LocalSystem `Jackett` service — Windows returns access-denied (you see a UAC prompt). So the watchdog can *detect* a hung Jackett but not recover it without rights. `setup.py`'s `grant_jackett_service_control()` additively grants Authenticated Users `SERVICE_START`+`SERVICE_STOP` via `sc sdset Jackett "(A;;RPWP;;;AU)…"` (one-time, elevated) and sets `sc failure` restart actions, so the non-elevated watchdog can recover Jackett with no UAC and no reboot. Re-run `setup.py` to apply. The access-denied paths log an actionable hint instead of failing silently. If Jackett runs as a **tray/user process** instead of a service, no grant is needed — the watchdog kills+relaunches it directly.

### FlareSolverr must be wired into Jackett by hand — there's no API for it

StreamLink can install + launch FlareSolverr (`/api/admin/components/install {component:"flaresolverr"}` → `_spawn_flaresolverr`) and report its status, but **Jackett's "FlareSolverr API URL" setting has no public API** — it lives in Jackett's `ServerConfig.json` / dashboard config and only the Jackett UI writes it cleanly. So the admin **must** paste the URL into Jackett's *Configure Jackett* dialog manually; the Indexers-tab card is deliberate about saying so (Copy button + Open-Jackett link). Don't try to "automate" it by poking Jackett's config file — it'll be clobbered and the format isn't stable.

FlareSolverr binds via the **`HOST`/`PORT` environment variables**, not CLI flags — both `run.py`'s `start_flaresolverr()` and `main.py`'s `_spawn_flaresolverr()` set them from `FLARESOLVERR_URL` (loopback hostnames normalised to `127.0.0.1`). It is **not** watchdog-supervised (only VLC/qBit/Jackett are) and **not** VPN-gated (matches Jackett — only qBit is killed on a VPN drop). If it dies, the Start button (`POST /api/admin/flaresolverr/start`) relaunches it; otherwise it comes back on the next `run.py` startup.

## Library

### Every library mutation goes through `mutate_library()` — `get_library` + `put_library` is a lost-update race

`get_library()` and `put_library()` each take `_lib_lock`, but **neither holds it across your mutation**. So the pattern that was used nearly everywhere —

```python
lib = await get_library()      # lock taken, released
...mutate...                   # ← anyone else can read here
await put_library(lib)         # lock taken, releases a snapshot that predates their write
```

— silently loses whichever write finishes first. Both callers get a success response, so nothing surfaces it: no error, no log, no conflict. The only evidence is the change you made not being there later.

Measured on the live box before the fix (v11.20.0): three concurrent writes to three **different** profiles landed one change and dropped two. Six concurrent identical `POST /api/library/download`s got past the info-hash dedup guard (its read-check-append is the same race), minting six items from one torrent — and then five of those were clobbered out of `library.json` on write-back, leaving an item pinned at `downloading` with no `torrent_hash`, no `error`, and no recovery path (the admin Cleanup tab classifies it as neither orphan nor missing, so it isn't even visible as broken).

This is not an exotic multi-user scenario. The progress tracker writes every 15 s and the download monitor every 5 s, so **any** user action has a standing chance of landing inside a background writer's read-modify-write window — and the app is explicitly multi-device (phone + TV + iOS app).

**Use `mutate_library()`**, which holds `_lib_lock` across the whole transaction:

```python
async with mutate_library() as lib:
    item["status"] = "ready"
```

Rules that come with it:

- **`get_library()` is still correct for read-only callers.** `put_library()` survives only for the rare caller that builds a library dict from scratch rather than editing the current one. If you are editing, you want `mutate_library()`.
- **Not reentrant.** `_lib_lock` is a plain `asyncio.Lock`, so never call `get_library()` / `put_library()` / `mutate_library()` inside the block — nor any helper that does. `_all_library_paths()` is the one that bites (and `_item_save_root()`, which calls it): hoist those *before* the transaction opens. `add_library_path` and `admin_cleanup_recover_item` show the shape — read outside, re-check the dynamic part inside.
- **Keep network and disk IO out of the block.** It is the single global library lock; holding it across a qBit round trip or an N-file delete stalls every other reader and writer, including the 2 s stat broadcaster. Do the IO before or after (`delete_library_item` drops the item under the lock and calls `qbit_delete` after; `delete_item_files` commits the skip marks, then unlinks outside).
- **A plain `return` inside the block COMMITS.** The context manager sees a clean exit. That's harmless for the usual "bail before mutating" guard — it rewrites what it just read — but it means an early return can no longer be used to *discard* a mutation you already made. Raise `LibraryUnchanged` for that; it's swallowed by the context manager and writes nothing. Any other exception (an `HTTPException` validation guard, say) also aborts the write, exactly as before.
- **Use `LibraryUnchanged` in hot paths that might find nothing to do.** The 15 s progress saver and the 5 s download monitor open a transaction and only then discover the tick is stale; committing there would rewrite a multi-megabyte `library.json` for nothing.
- **Long-running loops must not hold the lock across their polling.** `library_download_monitor` reads a snapshot, spends a tick doing qBit IO, then re-reads under the lock and **merges back only the items it touched** — so a concurrent edit to any *other* item survives, and an item deleted mid-tick stays deleted instead of being resurrected by the merge. Copy that shape for any new loop that mutates after slow work.

### `get_library`/`put_library` MUST keep the disk I/O off the event loop

`get_library()` reads `library.json`, `json.loads`es it, and runs `_migrate_item` over **every** item; `put_library()` `json.dumps(indent=2)`es and writes the whole file. That cost is O(library size) and grows after every download (the analyzer writes per-file audio-fingerprint `skip_data`). These are called from hot loops — `vlc_progress_tracker` (every 2 s while playing), `library_download_monitor` (every 5 s while downloading), `download_scheduler_loop`, plus ~110 request handlers. When this ran **inline on the asyncio loop**, a large library stalled the entire event loop every few seconds: the dashboard went "incredibly laggy" while CPU/RAM and the box itself (RDP) stayed perfectly fine — because a blocked *event loop* is invisible to CPU% and the OS. The tell-tale collateral was a flood of `httpx.ReadError`s from `https_proxy.py` (the HTTPS→HTTP proxy's upstream read timing out while the app loop was blocked). Fixed in v4.26.1: both helpers run the blocking part via `await asyncio.to_thread(...)` **inside** `_lib_lock` (the lock still serialises access; the loop stays free). **Don't** revert these to inline I/O, and **don't** read/parse/serialize `library.json` synchronously anywhere on the request/loop path — route through these helpers.

**Regression in the `/api/sync/*` + track-pref writers (fixed v7.9.5).** Five handlers that mutate a few fields still grabbed `_lib_lock` and then called the bare `_load_lib_raw()` / `_save_lib_raw()` **inline on the loop** instead of going through the threaded helpers: `/api/sync/progress`, `/api/sync/resolve`, `/api/library/{id}/local-tracks`, `_save_track_pref`, and `_save_series_sub_sel`. They read+rewrote the *whole* file on the loop — the exact v4.26.1 anti-pattern. It hid until the **iOS app foregrounded a download**: backgrounded, the WebView is suspended and posts nothing; on foreground it flushes its **backlog** of queued progress events to `/api/sync/progress` all at once, so a burst of whole-library load+save froze the loop in repeated bursts (multi-minute "server not responding to anyone" — the background VLC `status.json` poller stayed alive between calls, its cadence just degrading ~1 s→~3 s, which is the signature of burst-blocking vs a deadlock). Fix: wrap the load/save in `asyncio.to_thread`, same as `get_library`/`put_library`. The lesson generalises: a handler that "only updates one field" is **not** exempt — it still loads and writes the entire `library.json`, so it must never call `_load_lib_raw`/`_save_lib_raw` directly on the loop. Use `get_library`/`put_library`, or `to_thread` the raw calls when you need the read+mutate+write under one `_lib_lock` hold.

### No synchronous filesystem walks / blocking I/O on the event loop — `await asyncio.to_thread(...)`

The `library.json` stall above is the canonical case, but the rule is general: **any blocking call inside an `async def` that runs on the request/SSE/background-loop path must be off-loaded to a thread.** A blocked event loop is invisible to CPU% and to the OS (RDP/SSH stay responsive), so the symptom is a mysteriously laggy dashboard that "needs a reboot" while the box looks healthy — exactly the report that drove v4.26.1. The expensive offenders are **recursive directory walks** (`Path.rglob`/`iterdir`/`os.walk`), **`shutil.rmtree`/`disk_usage`/`copy`**, **`ffprobe`/`ffmpeg` and other `subprocess.run`**, and **full-file `read_text`/`write_text`/`json.load`/`json.dump`** — all of which scale with media/cache size and can each pin the loop for seconds. As of v4.26.1 the known ones are wrapped: `_discover_local_subs` (playback sub policy), `_delete_cache_artifacts` / `_dir_size_bytes` (cache purge + job-status polling), `_list_sidecar_subs`, `_ffprobe_full`, `_offline_cache_inventory_sync`, the prep `shutil.rmtree`s, and `shutil.disk_usage`. **When you add code that touches the disk from an async handler or loop, wrap the blocking part in `await asyncio.to_thread(fn, …)`** (keep the heavy work in a pure sync helper so it threads cleanly). Brief one-shot `stat`/`exists`/`mkdir`/tiny-JSON reads are fine to leave inline. Heavy CPU children (prep ffmpeg, analyzer) already run as separate low-priority processes — see [§ Server runs at raised OS priority](#server-runs-at-raised-os-priority--keep-heavy-children-below-it).

### `asyncio.to_thread` does NOT help pure-Python CPU work — use a process

`to_thread` only frees the event loop when the offloaded call **releases the GIL** — which subprocesses (ffmpeg/fpcalc/ffprobe) and most C-extension I/O do, but **pure-Python CPU loops do not**. A long pure-Python computation in a worker thread still holds the GIL for seconds at a stretch, and on multi-core hosts the **GIL convoy effect** starves the event-loop thread far worse than the 5 ms switch interval suggests — the dashboard freezes for seconds-to-a-minute while CPU%/RAM/RDP all look perfectly healthy (identical symptom to the `library.json` stall above, different cause). This bit Smart Skip: the intro/credits **matcher** (`analyzer._find_longest_match`, ~10-20 s of tight Python per episode pair) was dispatched with `asyncio.to_thread`, so the UI went unresponsive for a minute or two once a fingerprint pass reached the matching stage. The fix is a separate **process**, not a thread: `analyze_series` runs the matcher in a one-worker `ProcessPoolExecutor` (`_new_match_executor`, dropped to BELOW_NORMAL via `_match_worker_init`), with a `to_thread` fallback only if the host can't fork a pool. Rule of thumb: **subprocess or GIL-releasing C call → `to_thread` is fine; heavy pure-Python → run it in a process.** (The pool is created inside the `analyzer` module and its worker imports only `analyzer`, so Windows `spawn` re-importing the worker never touches the guarded `run.py`/uvicorn entrypoint — no risk of re-launching the server.)

### Season/episode attribution: the season is often in the *folder*, and the number is often *absolute*

Two traps, both from GitHub #15 (*Attack on Titan*: 131 files, every one of them `S0E0`).

**1. Only reading the basename loses whole shows.** A large slice of what lands on disk — anime
batches above all — states no `SxxExx` per file. The season is the directory
(`…/Attack on Titan Season 2/…`). `episodes.parse_slot` reads the enclosing directory, but the read
is **nearest-first and stops at the first component that resolves**, because the release *root* is
full of lies: `[Anime Time] Attack On Titan (Complete Collection) (S01-S04+OVA+Movies+Junior High) [BD]…`
names four seasons, an OVA folder and a Movies folder, none of which describe the file under it. Two
rules keep it honest, and **both must survive any edit to the patterns**:
* A component naming a season **range** (`S01-S04`, `Seasons 1-5`, `S1+S2`) or two different seasons
  is rejected — it is not one season.
* Bucket words (`Specials`, `Movies`, `OVA`, `OAD`…) match only at the **end** of a component, so
  `Attack On Titan OAD` is a bucket and that release root is not.

Directory reads are also bounded to the **item-relative** path (below the deepest shared directory),
which is what stops a file in an unrecognised subfolder — `Attack On Titan Junior High` — from
walking up into the root and inheriting its lies. That shared-root computation deliberately **backs
out of a trailing season/bucket component**: an item whose files all sit in one `Season 2` folder
would otherwise have the only statement of its season stripped as "the release root".

**2. The episode number is frequently series-absolute, and no filename parser can tell.**
`…/Attack on Titan Season 2/[Anime Time] Attack on Titan - 26.mkv` is **S02E01**. Reading it as
S02E26 is worse than not parsing at all: the season looks 25 episodes short *and* every episode
misses its TMDb name. This can only be resolved against the show's real per-season episode counts, so
it is a **second pass** (`episodes.resolve_absolute`) run once `all_seasons` is cached — never at
download time. Three invariants:
* Only files flagged `abs_episode` are eligible. A number read off an `SxxExx`, or corrected by hand,
  is never touched. The flag is cleared on resolve, so the pass is idempotent.
* The absolute-vs-within-season decision is made **per season, not per file** — a whole season's run
  either fits `1..count` or fits the absolute window, and one outlier must not split a season across
  both readings.
* The "no season anywhere, walk every number against cumulative counts" case is **skipped the moment
  any file in the item carries a real season**. Without that guard, AoT's `Junior High/… - 01.mkv`
  through `- 12.mkv` would map onto S01E01–E12 and overwrite the main run.

**Anything that persists `season`/`episode` must go through this.** They're stored in `library.json`,
so a parser improvement alone fixes nothing for existing rows — it needs the `_migrate_item` backfill
(which only touches `(0, 0)` files, preserving hand-corrections). See
[LIBRARY_DATA.md](LIBRARY_DATA.md) § Season/episode attribution.

### Bucketed files are not parse failures

A file with a non-empty `bucket` was deliberately placed outside the numbered run (a `Specials`,
`Movies` or `OAD` folder, or a spin-off's own). Anything reasoning about "how much of this item
failed to parse" must exclude them — the frontend's `_epUnattributedFiles` does. Counting them as
failures tripped the missing-content diff's 25% unattributed threshold on AoT (42 of 131 files) and
stood the feature down on exactly the well-organised batches it handles best.

### `library_item_id` is the "don't auto-delete" flag

`/api/stop` ([main.py:2576](../main.py#L2576)) checks `if state.active_hash and not state.library_item_id` before deleting the torrent. If you're streaming a torrent and then call `/api/stream/save-to-library`, that sets `library_item_id` and the next `/api/stop` will leave files alone.

### `track_pref_applied_file` prevents double-apply

`vlc_progress_tracker` triggers `_apply_track_prefs` when `state.library_current_file != state.track_pref_applied_file`. Without this guard, every 2 s tick would re-send the audio/subtitle commands and the user couldn't override them mid-playback. The check runs on the tracker's **2 s** cadence (7.16.3) — it used to sit under the 15 s progress-save throttle, which left an auto-advanced episode subtitle-less for up to ~17 s, and permanently if the viewer stopped playback before the throttle came around.

### Canonical path matching

VLC plays `Path(p).resolve().as_uri()` (resolved). The stored item file path may not be resolved. `_canonical_item_path` ([main.py:868](../main.py#L868)) compares both as resolved Paths and returns the stored path — so progress and skip-data lookups key correctly against `item.files[].path`.

### Resume hint continues *forward* from the last-watched episode

`find_resume_hint` ([main.py](../main.py)) resolves the show-card Play target. The key rule is **never go backwards to an episode the viewer skipped** — it continues forward from whatever they last played:
1. If `last_file` is **in-progress** (>5 s, not completed) → resume it.
2. If `last_file` is present, scan **from `last_file` onward** and return the first not-completed file (skipping `last_file` itself only when it's completed). So watching ep6 and skipping ep5 resumes ep6; finishing ep6 advances to ep7 — **not** back to the still-unwatched ep5.
3. Only if nothing ahead of `last_file` is unwatched does it fall through to the global walk: first not-completed file anywhere (this is also the cold-start path with no `last_file`, and is what finally surfaces a genuinely-skipped earlier episode once the rest of the series is done).
4. If all completed → return file[0] with `all_completed: true` (UI lets user rewatch from start).

This only applies *within* one library item (e.g. a season pack). A series split into one item per episode has an independent per-item hint each.

### Frontend drops saveProgress writes under t=5 s

The server recomputes `completed` on every `/api/library/{id}/progress` write as `pct = position/duration > 0.92`. A save at `t≈0` therefore wipes a previously-watched episode back to unwatched. The local player can fire those near-zero writes from at least three places: the very first `timeupdate` event before the resume seek lands, the `pause` event that browsers fire during initial load, and `lpStop` if the user opens the player and closes immediately. `saveProgress` and `_lpFlushProgress` both early-return when `posSec < 5` to keep watched marks stable. The 5 s threshold matches the resume hint's "meaningful in-progress" cutoff, so dropping these writes also has no resume-UX cost.

### `profile_id` is a claim, not proof — elevation and deletes need a PIN-verified session token

`profile_id` is a plain string the client puts in a query param, and `GET /api/profiles` hands out **every profile UUID unauthenticated**. So any check shaped like

```python
is_elevated = bool(profile_id) and profile_id in elevated_ids    # WRONG
```

is satisfied by reading a UUID off a public endpoint and quoting it back. That was the content lock until v11.20.0: the PIN screen was pure client-side decoration, and passing the elevated profile's id returned the admin-locked items with no PIN ever entered.

Two separate holes, both worth remembering:

- **The listing check was bypassable** (above).
- **The per-item routes had no check at all.** The lock only ever filtered `GET /api/library`. Every route addressed by item id — `/files`, `/metadata`, and friends — served a locked item to anyone who asked, with *no* `profile_id` and *no* auth of any kind. Filtering a list is not access control if the ids are addressable.

The rules now:

- `POST /api/profiles/{id}/verify-pin` mints a **profile session token** (`_new_profile_session`, 12 h). The client sends it as `X-Profile-Token` (or `?profile_token=`).
- **`_is_elevated(request, lib, profile_id)`** is the only correct elevation test: the profile must be flagged `elevated` **and** the request must carry a valid token for it. An admin session passes on its own.
- **`_assert_item_visible(request, lib, item, profile_id)`** goes on every per-item route that can return a locked item. It raises **404, not 403** — a 403 confirms the item exists, which is exactly what the lock is hiding.
- **`_require_delete_auth(request, lib)`** gates the content-destroying non-admin endpoints (`DELETE /api/library/{id}` — whose `delete_file` defaults to **true** — plus `/delete-files`, `DELETE /api/profiles/{id}`, `DELETE /api/settings/library-paths`). Before v11.20.0 these were completely unauthenticated: anyone on the LAN could take the media off disk.
- **A profile with no PIN can never be elevated and can't delete.** There is nothing to verify, so no token is ever issued for it. That's deliberate — an elevated profile anyone can select by name isn't a lock. Setting a PIN is what grants both.

The tokens are **persisted** (`profile_sessions.json`, gitignored), unlike `_admin_sessions`. This box reboots nightly on a schedule and the auto-updater restarts it too; having the whole household silently lose content access and delete rights every morning — with only a 403 to explain it — is worse than the tokens outliving a process. The TTL is what bounds them.

**The token must not live in `localStorage` alone.** `localStorage` is scoped to the *origin*, and this box serves the same dashboard on `http://<host>` **and** `https://<host>` — two origins. The first cut stored it there only, so entering the PIN on one scheme left the other signed in as the same profile but unelevated, with the admin-locked shows simply absent and no message. It is mirrored into a `streamlink_profile_token` **cookie** (no `Secure` flag, on purpose — a Secure cookie is withheld from the HTTP origin, which is the exact split being closed; cookies also ignore port). The server accepts header or cookie.

**And a restored profile must be re-verified.** Boot restores the active profile straight from `localStorage` without asking for the PIN, so a session that predates this feature — or one whose 12 h token has expired — is logged in with no proof behind it. Left alone that silently drops everyone's locked content once a day. `_maybePromptForPin` asks when `verified_profile_id` doesn't match the restored profile.

Client side: `static/index.html` attaches the header in a single same-origin `fetch` wrapper, so call sites don't have to know. `GET /api/profiles` echoes `verified_profile_id`, and the UI clears a token the server no longer recognises — without that, an expired session degrades into "some content is missing and deletes fail" with no prompt to re-enter the PIN.

### "Watched" has one rule — the outro window — and client-reported positions must use it too

Host playback credits a file only when playback reached the **outro**: `_position_is_finished()`, a 10 s window before the detected `credits_start`, or before the real end when no credits were detected. That's deliberate (see the comments on `_finalize_stopped_file`) — an episode stopped in the middle, even well past the old 0.92 mark, stays resumable.

The client-reported paths didn't follow it. `POST /api/library/{id}/progress` (the on-device player), `POST /api/sync/progress` (iOS batch sync) and `POST /api/sync/resolve` all used a flat `pct > 0.92`. Same profile, same file, two different answers: on a 45-minute episode the two rules sit **~3.4 minutes apart**, so finishing on the phone marked it watched while stopping at the same position on the TV left it resumable — and the resume hint then jumped back into an episode the viewer considered done.

All three now go through **`_device_reported_finished(item, file_path, pos, dur)`**, which canonicalises the path (clients key progress by whatever path they were handed, and the `credits_start` lookup needs the stored form) and defers to `_position_is_finished`. If you add another surface that reports a position, use it — don't reintroduce a percentage.

`completed` stays **monotonic** everywhere regardless: a late or out-of-order position must never un-finish an episode.

### `_current_playback_path()` returns None while the idle background video is on screen

It resolves "what is VLC playing", and the idle background video genuinely *is* what VLC is playing — but it is not *content*. Without the guard, the subtitle endpoints treated it as the current movie: a search from an idle dashboard hashed `damn.mp4` against OpenSubtitles, and a download would have written its `.srt` sidecar **next to the background video file** and attached the track to it.

Anything that asks "what is the user watching" must get `None` when the answer is "nothing" — the check is `state.background_playing and not _real_playback_active()`.

### Deleting a library item must purge its HLS bundles first

The prep bundle lives in a hidden folder beside the media file and its cache key is derived from the file's **name + size** (`_offline_cache_dir`) — so once the media is gone, the bundle can no longer be located. `delete_library_item` and `admin_cleanup_delete_item` resolve and remove bundles **before** calling `qbit_delete(delete_files=True)`, via `_purge_offline_bundles()`. Get the order wrong and the bundles are orphaned: hundreds of MB per prepped film, reclaimable only by the admin Cleanup tab's orphan sweep. `delete_item_files` has the same ordering for the same reason.

### Validate enum-ish settings — don't coerce an unknown value to a default

`POST /api/admin/auto-prep` used `mode if mode in (...) else "off"`, so a typo'd or stale client value returned **200 OK having quietly disabled automatic prep**. Silent coercion turns a client bug into a config change nobody made. Reject with a 400 instead (its sibling `/prep-validate` always did). Same class: `GET /api/search?limit=-5` reached `shaped[:limit]` → `shaped[:-5]`, returning *almost everything* instead of almost nothing — bound numeric query params with `Query(..., ge=, le=)` rather than trusting them into a slice.

### `/api/library/coverage` groups by `_series_key`, not `tmdb_id` — and trusts item *status*, not qBit

Two decisions in the coverage endpoint that look wrong until you hit the case each one exists for:

- **Grouping by `_series_key`.** A freshly-added episode has no cached metadata yet, so its `tmdb_id` is `0` for as long as the TMDb fetch takes. Keying coverage on `tmdb_id` would drop exactly the newest episodes out of their own show's coverage — the ones a viewer is most likely to go searching for again, and the ones that would then be offered back to them as "missing". Grouping by series key and taking the identity from whichever member *has* resolved metadata fixes it. (On the live box this was visible immediately: three of the ten `Hacks` S04 items carried `tmdb_id: 0`.)
- **Completeness from `item["status"]`, not `_build_item_files`.** The accurate answer needs a qBit `info` + `files` round trip **per item**, and this endpoint is called on every search render. It is a hint — "you already have this, don't fetch it again" — not a contract, so a `downloading` item's episodes come back under `pending` and are treated as owned. The per-file truth still lives in `/files` and the episode page, which is where it matters.

Also: a file carrying a `bucket` (Specials, Extras, a spin-off folder — outside the numbered run) **never** counts as owning `SxxExx`. Without that, an `Extras/03.mkv` filed under season 3 would mark the real S03E03 owned and hide it from every search surface.

### An episode you own is not "missing a source" — exclude it before sweeping

`_ssMissingEpisodes` originally meant "TMDb lists it, no torrent found". Applied to a show already in the library that reads as a hole: the row renders greyed as **"No source found"**, the season banner counts it, `ssFindMissing` sweeps the indexers for it, and bulk **Auto** queues a fresh download of a file already on disk. The gap set now unions the coverage row's owned episodes into its `have`, and `_ssBulkScopeEpisodes` skips them outright. The per-row **Replace** action is the deliberate way back in — re-downloading is a choice, never a default.

### A green picture is Dolby Vision Profile 5 — and nothing here can decode it

A release that plays with a lurid **green** cast in VLC *and* in the prepped HLS bundle is Dolby Vision **Profile 5**, every time. P5 is single-layer and stores the picture in **IPT-PQ-c2**, not YCbCr, with the mapping back to something displayable carried in a per-frame RPU. Its `bl_signal_compatibility_id` is **0**: there is deliberately no HDR10 or SDR fallback baked into the base layer. Any decoder that ignores the RPU reads IPT chroma planes as if they were Cb/Cr, and green is what that looks like.

Do **not** generalise this to "Dolby Vision is broken". Every other profile carries a usable base layer — 8.1 → HDR10, 8.2 → SDR, 8.4 → HLG, 7 → an HDR10 BL — and plays fine. `dvprobe.is_green` therefore flags **only** `dv_profile == 5 && dv_compat == 0`; widening it would condemn a large slice of perfectly good releases and turn the warning into noise.

Distinguish it from plain HDR10 by the symptom: un-tone-mapped HDR10 looks **washed out / flat**, never green. (The prep pipeline does no colour conversion at all — `scale` → `yuv420p` — so HDR10 sources do come out flat. Separate, milder, still open.)

**Why we don't fix it in ffmpeg.** Applying a DV RPU needs the `libplacebo` filter. `setup.py` installs gyan.dev's `ffmpeg-release-essentials.zip`, which doesn't carry it. Even with the full build it would only fix the *prepped* path: VLC plays the raw file on the TV and would still be green unless DV items were forced to on-demand-only and tone-mapped ahead of every watch. Swapping the release fixes it everywhere, instantly, at no CPU cost — which is why the feature detects and routes around P5 rather than trying to render it.

### Release titles are not evidence of Dolby Vision — probe the file

Of the DV files observed here, the only one that announced itself did so as a bare `DV`, while plenty of healthy **P8.1** releases advertise `DV` just as loudly (usually beside `HDR10`). A title filter therefore both misses real P5 files and rejects good ones.

So there are two separate mechanisms and they are not interchangeable:

- **`dvprobe.probe_path`** reads the actual `DOVIDecoderConfigurationRecord` out of the container header. This is the authority, and the only thing the **Green — Dolby Vision** badge is allowed to reflect. It needs no ffmpeg (the record is a fixed 5-byte layout in the MKV `BlockAdditionMapping` / MP4 `dvcC` box), which matters because `analyzer.ffmpeg_bin()` can legitimately be absent.
- **`dvprobe.title_dv_risk`** is a title heuristic used **only** to rank search results, where no file exists yet. It sorts a suspect release to the back of every tier in `_ssAutoPickFrom` and `_retry_candidates` and renders an advisory "May play green" chip — it never removes a release, because a green episode still beats no episode when it is the only source.

A corollary for the MP4 path: `moov` is not always at the front. `_probe_mp4` checks the **tail** as well, or a non-faststart mux probes to "unknown" and is silently never flagged.

### Replacing a green file must not delete the old one for you

The **Find a replacement** action starts a download and stops there. The green copy stays in the library until the user removes it, and the toast says so. Deleting media on the strength of a header byte is not a call to make silently, and a replacement that fails or turns out to be *also* P5 would otherwise leave them with nothing. (Contrast `_retry_dead_download`, which *does* swap in place — it only ever runs at **zero bytes fetched**, so there is nothing to lose.)

One non-obvious consequence: 11.22.0 taught the search show page to suppress episodes you already own, which would make a replacement search show "already in your library" and offer nothing. `epReplaceGreen` passes `replace:true`, and `_ssOwns` / `_ssOwnedEps` exempt exactly that one episode — not the whole show.


## SSE

### Per-client queues, dead-queue cleanup

Every `/api/events` connection creates its own `asyncio.Queue(maxsize=100)`. `broadcast` iterates `state.sse_queues`, drops any that raise `QueueFull`. Disconnected clients are cleaned up in the `finally` block of the stream generator.

### EventSource can't set headers

For admin SSE, the token is passed via `?admin_token=…` query param. The middleware accepts it from query string too.

### EventSource won't auto-reconnect a *closed* or *half-open* stream — the client supervises it

The browser's built-in reconnect only fires while the `EventSource` is `CONNECTING`. On mobile, the two failure modes that actually strand the UI both dodge it: after the device locks / the app backgrounds, the browser **fully closes** the stream (`readyState === CLOSED`, never auto-reconnects), and a suspended socket can die silently and come back **half-open** (`readyState` still `OPEN`, so no `error` event ever fires — the UI just freezes on stale data). Don't assume "EventSource handles reconnection for us."

The dashboard handles both in `connectSSE()` (`static/index.html`): a CLOSED stream triggers a self-rebuilding backoff reconnect (`_scheduleSSEReconnect`), and a liveness watchdog detects the half-open case — the server's keep-alive is a **named `ping` event** (not a `: comment`, which is invisible to JS) emitted ≥ every 20 s, every handler stamps `app._lastSSEMsg`, and a foreground-only 15 s interval reconnects if nothing arrived for 50 s. Reconnects also fire on `visibilitychange`→visible / `pageshow` / `focus` / `online`. **If you add a new SSE event handler, call `_noteSSEMsg()` first** or long-lived events won't count as liveness. **Don't revert the server heartbeat to a bare `: comment`** — that re-blinds the watchdog. Every reconnect `close()`s the prior `EventSource` first; skipping that leaks a server-side queue per stale connection. See [FRONTEND.md § SSE reconnect supervision](FRONTEND.md).

### Slow-network Play must be non-blocking

`/api/library/{id}/play`, `/api/vlc/prev`, `/api/vlc/next`, `/api/stop`, and `/api/stream` all return **202** and do their VLC `in_play`/`in_enqueue` (and qBit deletes on stop/stream) in background tasks. They synchronously update `state`, broadcast a `buffering` / `idle` state event, then return. The SSE-driven UI repaints from that broadcast within ~tens of ms even when VLC is taking seconds to actually open the file.

Don't be tempted to "simplify" any of these handlers back to inline `await vlc("in_play", …)` — on flaky links each VLC HTTP roundtrip can take 1–5 s, and a 5-episode playlist with `in_play` + 4× `in_enqueue` would block the response for that whole window. The frontend's optimistic-buffering UI (`_optimisticBuffering` in `index.html`) also assumes the buffering broadcast lands fast — bringing back inline VLC blocks would leave the user staring at "Loading…" with no confirming state event.

The handoff tasks are tracked on `state.library_play_task`. `/api/stop` and any subsequent Play / prev / next cancels the prior task before kicking off its own so a slow `in_play` can't keep going after the user has already moved on (otherwise VLC would end up playing whatever the *previous* request was reaching for).

### Flip `stream_status` to "playing" right after `in_play`, not after the enqueue loop

`_library_play_launch` and `_vlc_relaunch_playlist` set `state.stream_status = "playing"` and broadcast the state event the instant VLC accepts the first track. The remaining `in_enqueue` calls then run **in parallel via `asyncio.gather`**, not sequentially after the state flip.

Why this ordering matters: VLC is local, but its HTTP API still serializes per call, and a "continue watching" play on a long show easily ends up with 50+ files in the playlist tail. If the state flip waits for a sequential enqueue loop to finish, VLC is already playing the first episode but the UI stays pinned to "buffering" / "Loading…" for many seconds — exactly the regression that 2.2.1 fixed. Don't reorder these.

Failures inside the parallel `gather(..., return_exceptions=True)` are silently absorbed because the user-visible playback already started; a missing enqueue just means a future Next would fall through to `item.files`.

## Stream to Device (HLS)

### H.264 `-level` must scale with the OUTPUT resolution — 4.1 kills 4K encodes

H.264 level 4.1 caps at ~1080p (2.1 MP frame). Hardcoding `-level:v 4.1` on a
2160p rung makes NVENC (and libx264) abort at init with
`InitializeEncoder failed: invalid param (8): Invalid Level` → task error `-22`
→ **the whole ffmpeg job dies** with a bare `Conversion failed!` and Windows
`rc=4294967274`. This killed *both* the all-GPU and the CPU-decode fallback
paths, so a 4K source (e.g. *Your Name* 2160p BD) never produced a bundle. The
level is now derived from the output height in `_encode_video`: `4.1` ≤1080p,
`5.0` ≤1440p, `5.2` for 4K. Same rule applies to any path that encodes at
**source** resolution with no scale filter — `_od_build_ffmpeg_args` (on-demand
JIT) takes a `src_height`; `_build_clip` sidesteps it entirely by capping clips
at 1080p (`scale=-2:'min(1080,ih)'`). If you add a new encode path, don't copy
the old flat `4.1` — pick the level from the actual output height.

### NEVER prep a file qBittorrent hasn't finished — the corrupt bundle takes the FINISHED file's cache key

The single nastiest failure in this subsystem, because every individual step reports success.

1. qBit writes pieces into a **sparse** file whose reported length reaches its final value long before the last piece lands. `Path.exists()` is true and `st_size` is final while the middle is still holes — the same trap as [§ Play a complete file from a still-downloading torrent](#play-a-complete-file-from-a-still-downloading-torrent--gate-on-complete-not-exists), one layer down.
2. ffmpeg stream-copies straight through the holes, writes a full-duration playlist and exits **0**. `hls.log` says `DONE`. Playback runs 5–10 s, stalls, cuts to black.
3. `_offline_cache_key` is `sha256(version|name|size)[:24]` — and the size is already final — so that bundle lands on **exactly the key the completed file resolves to**. `_maybe_start_prep_job` reports `cached` from then on and it is **never rebuilt**.

So an unlucky 30-second overlap between the encode and the last piece produces a permanently-broken episode. (*Hacks S04E01*: encode finished 16:14:55, download finished 16:15:24. Bundle 1,637 MB; clean rebuild 2,112 MB.)

**And `progress == 1.0` is itself half a step early.** qBit flips it when the last piece *verifies*, then writes to disk asynchronously, so `st_size` settles a beat later. Prepping in that window reads good data but keys the bundle to a size the finished file never has — the bundle is then invisible to every lookup (a wasted encode plus an orphan, on every completed download). Require the on-disk size to match qBit's `size` too, and re-derive the key at encode time since a queued job's key can go stale while it waits.

Gate on qBit's **per-file `progress`**, never on the filesystem: `_incomplete_download_paths` at enqueue time (`_enqueue_library_prep`) and `_src_still_downloading` immediately before the encode (`_run_offline_job`, so play-driven / interactive / admin jobs are covered too). Both fail *closed* — if the item is downloading and qBit can't confirm, everything is treated as incomplete. Existing damage does not self-heal: delete the bundle and re-prep.

### Bundles live BESIDE the media now (v8) — resolve via `_offline_cache_dir`, never `OFFLINE_CACHE / key`

As of `OFFLINE_CACHE_VERSION = "v8-colocated"` each bundle lives in
`<file_dir>/.streamlink_cache/<key>/`, not the central `.offline_cache/`. Three
consequences that bite if you forget them:

1. **Never compose `OFFLINE_CACHE / key` again.** Use `_offline_cache_dir(src)` to
   build/look-up a bundle path, and `_resolve_bundle_dir(key)` (the `_bundle_index`)
   to serve one by key — the serving route only has the key, and the dir could be in
   any media folder. The index is discovered by *directory name* and refreshed on
   prep-done / cached-hit / move / migrate / delete; if you add a new path that
   creates or deletes a bundle, call `_bundle_index_register` / `_invalidate_bundle_index`.
2. **The key dropped path AND mtime — it's now `version | filename | size`.** That's
   deliberate: it makes the bundle **move-stable** (a series move, even cross-device,
   keeps the key valid). Don't "add mtime back for correctness" — the in-place rewrite
   paths (repair / compress) already purge the bundle explicitly, so mtime was
   redundant, and re-adding it would break the move feature. Two same-name+size files
   can only collide inside one directory, which the filesystem forbids.
3. **Migration is one-time, at startup, BEFORE the prep loops spawn.**
   `_migrate_offline_cache_layout` (awaited in `lifespan` before
   `asyncio.create_task(auto_prep_loop())` etc.) moves pre-v8 central bundles to their
   co-located home with a plain directory move — **no re-encode**. Running it after the
   loops are up would let auto-prep see a "missing" bundle and rebuild it. Keep it
   ordered before the task spawns. It uses `_offline_cache_key_legacy` to find the old
   central dirs; stale ones (source changed/removed) aren't matched and are left for the
   orphan purge — never silently rebuilt.

### Cleanup must ignore `.streamlink_cache` — co-located bundles are inside media folders

Now that each bundle sits in `<media_dir>/.streamlink_cache/`, the admin Cleanup
tab's stray-file scan would see those dirs as "owned by no torrent" and offer to
**delete** them. `_cleanup_inventory_sync` skips `_STREAMLINK_RESERVED_DIRS`
(`.streamlink_cache` / `.offline_cache` / `.ondemand_cache`) for exactly this reason,
and the scan now walks **every** configured root (`_all_library_paths()`), not just
`qbit_download_path` — with a guard that a configured root nested in another isn't
flagged when the parent is scanned. If you add another StreamLink-managed dir inside
media folders, add it to the reserved set or cleanup will offer to nuke it.

### Output is an HLS bundle directory — not a single MP4 anymore

The cache layout switched in Milestone 16. Each prepped source produces `.offline_cache/<sha>/` with `master.m3u8`, per-rendition playlists, fmp4 segments, and `meta.json`. The pre-v3 single-MP4 cache (`<sha>.mp4`) is dead code on disk — surfaced as `kind: "legacy"` orphans in Admin → Offline Cache for purge. Don't reintroduce code that assumes "a prepped file is one MP4" — every endpoint, admin tool, and cleanup path now walks the directory.

### Subtitles can NOT live in the HLS manifest — they're standalone `.vtt` sidecars

ffmpeg's HLS muxer cannot package multi-track WebVTT. Exactly *one* subtitle works if you declare it inline on the video variant (`v:0,a:0,s:0,sgroup:…`); declaring two or more as their own `s:N,sgroup:…` variants fails unconditionally with `[mpegts/mp4] No streams to mux were specified` → `Could not write header (incorrect codec parameters ?)` → `Conversion failed!`. This holds for **both** `fmp4` and `mpegts` segment types (verified on ffmpeg 8.1.1). Because virtually every release MKV ships many subtitle tracks, the old in-manifest design meant HLS prep failed on essentially every real file — it had never once succeeded (fixed in v3.2.0).

The fix: `_build_hls_ffmpeg_args` builds a **video + audio only** HLS bundle, then emits one standalone `sub_<i>.vtt` per text sub via extra outputs in the *same* ffmpeg pass (`… <out>/%v.m3u8 -map 0:s:0 -c:s webvtt -f webvtt <out>/sub_0.vtt …`). The player attaches them as `<track>` children. Do **not** "re-add subtitles to `-var_stream_map`" — it will silently break prep again. If you ever need a single inline sub, the one-subtitle inline form is the only var_stream_map shape that works.

### ffmpeg's ASS→WebVTT conversion mangles typeset fansub subs — `_clean_webvtt` sanitises it

The `-c:s webvtt` conversion above (and the on-demand `_sub_to_vtt` ffmpeg path for `.ass`/`.ssa` sidecars) is a **lossy, faithful-to-a-fault** dump of complex ASS. On a heavily-typeset anime/fansub track it produces three distinct on-screen defects, all seen together in the v7.9.6 Attack on Titan report (every line doubled, `\h` text, blocks of numbers flashing):

1. **Doubled/tripled cues.** Typesetting layers a sign — e.g. a blurred border copy *under* a sharp fill copy — as **two `Dialogue` events with identical text** but different style/`\pos`. WebVTT has no positioning/layer concept, so both flatten to the **same cue at the same time** and the player stacks them: the line renders 2–3×, overlapping. (On the real AOT episode, `sub_0.vtt` = 1262 cues, of which **169 were exact duplicates**.)
2. **`\p`-mode vector drawings dumped as text.** An ASS drawing (`{\p1}m 270 -403.5 l 277.5 …{\p0}`) is the *shape* of an on-screen sign/map. ffmpeg strips the `{\p1}` override but keeps the coordinate path as the cue body, so a giant `m -222 -409.5 l -220.5 …` blob shows as a "subtitle" (29 of them in that file).
3. **Leaked ASS escapes.** `\h` (hard space) and occasionally `\N` survive verbatim (`Currently\h\hDisclosable\h\hInformation`).

`_clean_webvtt(text)` fixes all three: it drops drawing-blob cues (`_is_ass_drawing`: body is only path commands `m/n/l/b/s/p/c` + ≥6 coordinate numbers), **de-duplicates** cues sharing the *same timing and text*, converts `\h`→space / `\N`→newline, and strips stray `{…}` blocks. It is **idempotent**, which is load-bearing: it runs at three points without churn — at prep finalize (born-clean `sub_<i>.vtt`), inside `_sub_to_vtt` (sidecar path), and **in-place when serving a `.vtt`** from `offline_cache_bundle_file`, so bundles built before the fix self-heal on the next fetch (and the iOS download path, which pulls bundle files through that same route, gets the cleaned copy). Styling is still lost — that's the unrelated libass.js deferral — but the track is now *readable*. Don't "simplify" the dedup to text-only or timing-only: two genuinely different lines can share text (different times) or share a timestamp (different text); only same-both is a real duplicate.

### Subtitle image packs: two ffmpeg defaults that fail SILENTLY and look like success

`subpack.py` pre-renders styled ASS (and bitmap PGS/VOBSUB) to transparent PNGs so a
client that can't run libass — the native iOS `AVPlayer`, which renders only real media
tracks — can still show them. Both traps below produce a plausible-looking result and no
error, which is exactly why they cost a debugging cycle each.

**1 — `ass`/`subtitles` filter: `alpha` defaults to FALSE.** The filter blends glyphs into
RGB and leaves the alpha channel *exactly as it found it*. Over a transparent base that
means alpha stays 0 everywhere, so every PNG in the pack carries the text in RGB and is
100% transparent. Nothing reports a problem: the files are the right size, and every
**luma**-based tool agrees there is ink — `cropdetect` happily returns correct bounding
boxes for glyphs no client will ever see. It only shows up when you composite one over a
background and get nothing. **Always pass `alpha=1`.**

**2 — `-dump_attachment:t ""` stops at the first filename it can't write, and says
nothing.** The bulk form names each output from the attachment's `filename` tag; a space
(`ObeliskMdITC TT.ttf`) was enough to abandon a 9-font release after 3, with a zero exit
status. Missing fonts don't fail a render either — libass substitutes, so you get a pack
that is *subtly in the wrong typeface*. Dump **per index** (`-dump_attachment:t:<n> <our
own name>`) and count what landed; libass matches on the font's internal family name, so
renaming the file costs nothing. The manifest records `fonts` for exactly this reason.

**Corollary — `-f null -` after an attachment dump decodes the WHOLE file.** Attachments
are written when the input is opened, but the null muxer then dutifully decodes every
frame; doing that once per attachment turned a ~1 s job into minutes on a 1.4 GB HEVC
source. Disable the streams you don't want (`-vn -an -sn`) and it drops to ~0.1 s each.

### The pre-render is per SUBTITLE STREAM index, and a pack is additive — never re-prep for one

`subpack_<i>/` lives *inside* the existing bundle dir and nothing else is touched: no
segment is rewritten, `meta.json` is unchanged, and **`OFFLINE_CACHE_VERSION` does not
move**, so no existing bundle is invalidated and nothing re-preps. That property is the
whole reason this approach was chosen over burning subtitles into the video (which would
re-encode every file). If you ever find yourself bumping the cache version to ship a
subtitle feature, you've lost the plot.

`sub_idx` is the index among **subtitle streams** (`0:s:<i>`), matching `meta.json`'s
`subtitles[]` ordering — never the absolute stream index. The serving route whitelists
filenames to `manifest.json` / `c_<n>.png` so the extracted `sub.ass` and the dumped fonts
inside the pack dir stay off the network.

### The fmp4 init filename MUST be templated, or playback dies with `fragLoadError`

Symptom: prep "succeeds", the manifest parses (the audio/subtitle dropdowns populate, so `MANIFEST_PARSED` / `loadedmetadata` already fired), then playback never starts and hls.js throws a fatal `fragLoadError` (black player). It is **not** a server bug — `offline_cache_bundle_file` serves every real file fine (200 for `.m3u8`/`.m4s`, 206 for Range). The failing fetch is the **fmp4 init segment**: the variant playlist's `#EXT-X-MAP:URI="…"` points at an init file ffmpeg never wrote under that name, so it 404s, hls.js exhausts its frag retries, and the error goes fatal *before any frame decodes*.

There are **two** independent ways to hit this, fixed in two steps:

1. **Wrong init *name* (v3.2.1).** We templated `-hls_segment_filename` (`seg_%v_%05d.m4s`) and `%v.m3u8` but originally left `-hls_fmp4_init_filename` at ffmpeg's default. ffmpeg's own `%v` expansion for the init segment is version-dependent and doesn't always match the URI it writes into the playlist (e.g. it may number inits `init_0.mp4`/`init_1.mp4` while segments use the `name:` tag). Fix: pin `-hls_fmp4_init_filename "init_%v.mp4"` so inits are `init_video.mp4` / `init_audio_0.mp4`, matching the segment scheme. **Do NOT give it a full path** — ffmpeg prepends the playlist's directory to the init filename, so a full path becomes a doubled/invalid path and the encode dies with `Failed to open segment … Could not write header`.

2. **Wrong init *location* on Windows (v3.2.2).** Even with the right name, the init still 404'd on a Windows host. ffmpeg derives the init segment's *output directory* by parsing the **playlist** path; a Windows backslash playlist path defeats that parse, so `init_video.mp4` is written to the server's working directory instead of the bundle `.part/` dir — segments are fine because we pass them as absolute paths ffmpeg uses verbatim. Fix: make **every output a bare filename** (init / segments / playlists / subs) and run ffmpeg with `cwd=<bundle .part dir>` (`_run_offline_job` passes `cwd=str(tmp_dir)`). Now everything lands in the bundle on every OS; only `-i <source>` is absolute. `_build_hls_ffmpeg_args` no longer takes `out_dir`.

If you change any output naming or the cwd handling, bump `OFFLINE_CACHE_VERSION` so old bundles rebuild. Debug tip: a fatal `fragLoadError` is logged with the exact failing URL + HTTP code in the browser console (and the on-screen alert shows the filename + code) — a `404` on `init_*.mp4` is this bug; a `404` on `seg_*.m4s` means segment naming drifted; code `0` means a transport/TLS failure, not a 404.

### On-demand (JIT) streaming: mpegts + forced keyframes, never stream-copy

The on-demand path (`stream-ondemand` + the `_od_*` helpers — see [STREAMING.md](STREAMING.md) § On-Demand) is a *different* pipeline from the full-prep bundle and has its own non-negotiable shape:

- **mpegts segments, NOT fmp4.** JIT restarts ffmpeg seeked to an arbitrary segment whenever the user seeks. fmp4's separate `EXT-X-MAP` init segment would differ between independently-seeked encodes, so a segment from encode A can't be decoded against encode B's init → playback breaks. TS segments are self-contained (no init), so any segment from any session plays. Don't "switch JIT to fmp4 for consistency with the bundle path" — it reintroduces the cross-session init mismatch.
- **Video ALWAYS transcodes with forced keyframes — never `-c:v copy`.** `_od_build_ffmpeg_args` uses `-ss <n*6>` before `-i` (resets output PTS to 0) **plus** `-force_key_frames expr:gte(t,n_forced*OD_SEGMENT_SECS)`. That pair is what guarantees segment *N* covers exactly `[N*6,(N+1)*6)` — which is what makes the *virtual* `media.m3u8` (generated from duration alone, segment times assumed exact) correct and seeking land on the right frame. Stream-copy can't force keyframe boundaries, so copied segments would drift off the assumed grid and the virtual playlist's timing would be wrong. Copy stays a full-prep-only optimisation.
- **`-forced-idr 1` is mandatory on the NVENC branch — `-force_key_frames` alone is a silent no-op there.** This was a big "JIT is buggy" root cause: `h264_nvenc` **ignores** `-force_key_frames` unless `-forced-idr 1` tells it to emit IDR frames at the forced points. Without it NVENC falls back to its **default GOP** (~250 frames ≈ **10.4 s**), so segments came out ~10 s long while the virtual playlist still assumed 6 s. The index↔time map then drifts further into every encode: `seg_N` actually starts seconds past `N*6`, so **seeks land wrong, subtitles desync, and each segment's PTS runs ahead of its playlist slot → hls.js sees buffer holes and stalls.** Symptom is invisible in `hls.log` (ffmpeg exits 0) — you only catch it by `ffprobe`-ing a served segment (`format=duration` ≈ 10 s, one keyframe). libx264 honors `-force_key_frames` unaided, so the flag is gated behind `if use_nvenc`. Verify a fix by confirming a served `seg_N.ts` has `duration≈6` and first video PTS == `N*6`.
- **Zero the MPEG-TS `muxdelay`/`muxpreload` or every segment's PTS is skewed +1.4 s.** The mpegts muxer defaults to starting the first PTS at ~1.4 s (0.7 muxdelay + 0.5 muxpreload, rounded). Combined with `-output_ts_offset <n*6>` that puts `seg_N`'s first PTS at `N*6 + 1.4`, i.e. 1.4 s **above** the slot the virtual playlist assigned it → a gap at every segment boundary and after every seek. `-muxpreload 0 -muxdelay 0` pins the first PTS to exactly `N*6`. (Verified: with both zeroed, `ffprobe` reads first PTS `12.000000` for `seg_2` of a `-ss 12` encode; without, `13.4`.)
- **`temp_file` in `-hls_flags` — segments must appear atomically.** The serve gate is just `seg_path.exists()`, so if ffmpeg grows `seg_N.ts` in place a request whose 150 ms poll lands mid-write streams a **truncated** segment (decode gap / re-buffer). `temp_file` writes `seg_N.ts.tmp` and renames on finalize, so only complete segments are ever visible to the serve gate and to `_od_max_seg_on_disk`. `_od_wipe_segments` also clears `.ts.tmp` so a killed encoder's partial can't linger. The finalize lag (a segment is renamed only when the next opens) is absorbed by the request being held open up to `OD_SEG_WAIT_TIMEOUT`.
- **Segment requests are held open — bounded.** `seg_<n>.ts` blocks (async-polls) until the file lands, capped at `OD_SEG_WAIT_TIMEOUT` (then `504`). The wait is all `await asyncio.sleep`, so it never actually blocks a worker. Client-side, hls.js's `fragLoadingTimeOut` is raised to 45 s (> the server cap) so a still-progressing cold encode isn't aborted mid-transcode.
- **JIT and the background full prep run concurrently.** `stream-ondemand` also enqueues a **bulk** full-bundle prep. Both transcode at once — acceptable because both run BELOW-normal priority (`_FFMPEG_SUBPROCESS_KW` / `_ffmpeg_nice_prefix`) under the HIGH-priority server, and if idle-prep governs, activity pauses the bulk job anyway. Don't make the background prep `interactive` — that would defeat the pause/idle controls and double the CPU during active watching.

### Re-encoded AAC audio desyncs from stream-copied video unless locked to the clock

In the full-prep bundle the original video rung is stream-**copied** (`-c:v:0 copy`) while every audio track is **re-encoded** to AAC, and they're emitted as **separate HLS renditions sharing one timeline** — the player aligns them purely by media timestamps. So any source quirk that skews the audio clock relative to the video — a per-stream `start_time` offset, broken/missing PTS, or audio gaps — leaves the re-encoded audio misaligned in the bundle. It's **intermittent** (source-dependent), which is exactly why it was hard to catch. The fix in `_build_hls_ffmpeg_args`: `-fflags +genpts` on the input (clean monotonic clock for every stream) plus a per-output `-filter:a:{i} aresample=async=1:min_hard_comp=0.100000` (stretches/squeezes the audio and pads gaps with silence so it tracks the video). Two footguns to respect:

- **`aresample=async=1` fixes drift, NOT a constant offset.** Two distinct failure modes. **Drift / gaps** (audio gradually slips) → the async-resample filter handles it. **Constant offset** (audio a *steady* amount early/late, e.g. 0.25–0.5s) → the dominant cause is a video **edit list** in the source (common in `-ss` stream-copies, web-DL, many MP4 muxers; the container still reports `start_time=0`, so it's NOT a start-time skew and a start-time check misses it entirely). Stream-copying the video mishandles the edit list while the re-encoded audio is normalized to zero, baking in a fixed offset — reproduced: an edit-list source built with `-c:v copy` lands ~0.5s out, the same source re-encoded lands ~0. `aresample` can't remove it. The fix is to **re-encode the original rung** (applying the edit list to the frames): `_source_has_editlist(src)` (first video pts with vs without `-ignore_editlist 1`, diff > `EDITLIST_REENCODE_SECS=0.05s`) gates it for new preps, and it's forced on every bundle the repair tool rebuilds. (`-copyts -start_at_zero` would be the alternative lever, but it interacts unpredictably with the HLS muxer + copy; re-encoding the one rung is the safe, verified path.)
- **`aresample:first_pts` pins audio to the VIDEO rung's first PTS — on BOTH paths, copy included.** (Rule changed in 11.12.0; it used to be `first_pts=0`, re-encode path only.) A source whose audio starts *after* the video — EMBER anime BDRips carry audio first-PTS ≈ +1.0s vs video 0; *Promised Neverland S02* carries **+0.5s on the English dub track only** — is faithfully reproduced by ffmpeg as a **cross-rendition `baseMediaDecodeTime` gap**: that rendition's first *sample* is real dialog timestamped ~0.5–1s in, with no leading silence. A correct player honors the gap; but **Safari / AVPlayer / hls.js playing SEPARATE audio+video fmp4 renditions ignore it and anchor each rendition's first sample to playback start → the dialog plays that much EARLY** (single-program on-demand and direct VLC never hit this — that's why the symptom is bundle-only; verified with `av_probe.py`: source A/V offset +1.0s, bundle faithfully +0.976s, yet the bundle plays early). `first_pts` pads the rendition's start with real silence up to the pin, so it starts where the video does — no gap for the naive player to drop, dialog still at its true time on faithful and naive players alike. **The anchor must be the video's own first PTS, not 0** — that's the whole reason the copy path was excluded before (issue #13): a stream-copied rung keeps its own arbitrary first PTS, so forcing audio to 0 against it would *introduce* an offset, while pinning both to the same value can't. `first_pts` takes any value (unit: **1/sample_rate of that rendition's output rate**, so compute it per track — a 44.1k track and a 48k track need different integers for the same instant; verified end-to-end). Guard: `HLS_AUDIO_PAD_MAX_SECS = 1.0` — past that the pin would **trim real audio** rather than pad silence, so prep leaves the copy path unpinned instead. Measured on a synthetic 2-track source (eng delayed 0.5s, 44.1k): copy-path gap **+0.476s → −0.024s**, no re-encode. The old escape hatch `-copyts -start_at_zero` *preserved* the gap rather than removing it, so it would NOT have cured this.
- **meta `default` flags are NOT exclusive — never treat "the default audio" as "the one that matters".** `_ffprobe_full` copies each stream's `disposition.default` verbatim and prep writes it straight into `meta.json:audios[]`, so a release MKV that marks *every* track default yields `"default": true` on all of them. `next((x for x in audios if x.get("default")), audios[0])` then always returns `audios[0]` — which is exactly how issue #13 hid: the offset detector probed one rendition, that expression could only ever pick the first, and the desynced track was the third. Anything that judges a bundle must iterate **every** rendition (a viewer can select any of them, and a per-track source delay desyncs that track alone). Only the master playlist's `default:yes` is single-valued, and that's computed separately (`default_audio_i`).
- **The offset detector flags the RAW cross-rendition gap, NOT a diff against the source, and measures EVERY rendition.** **Detect & Repair Audio Sync** (`POST /api/admin/hls-resync`) flags on *either*: (1) `_bundle_audio_sync_delta` — audio-vs-video total `#EXTINF` duration divergence (the drift class); (2) `_bundle_introduced_av_offset` — the worst audio-vs-video first-pts gap `apts − vpts` across **all** audio renditions (returns `(gap, "audio_2/eng")` so the log names the culprit; `stop_at` short-circuits on the first rendition over threshold). The duration check alone is blind to a constant offset (both renditions are the same length) — which is why the first cut of the tool reported "no issue" on a visibly-desynced show. **An earlier version diffed the gap against the source's intended offset, on the theory that faithfully reproducing the source = correct. `av_probe.py` disproved that for the bundle player** (source A/V +1.0s reproduced as bundle +0.976s, yet audio plays early): single-program on-demand and direct VLC honor a cross-rendition gap, but Safari / iOS AVPlayer / hls.js playing SEPARATE renditions ignore it. The detector therefore wants the gap ≈ 0 regardless of source. Thresholds: `AV_OFFSET_FLAG_SECS=0.12s`, or the looser `AV_OFFSET_PAD_FLAG_SECS=0.35s` when meta `video_reencoded` (pre-11.12 bundles: `audio_padded_to_zero`, which meant the same thing) — a **re-encoded** original rung carries a frame-reorder residual (~0.125s on B-frame sources) that re-prep can't remove, while a **stream-copied** one doesn't (measured ≈0.02s) and keeps the tight bound. Repair is **cheap-first**: an offset-only flag on a bundle that was never pinned re-preps at *remux* speed (the pin now works on the copy path), and only drift — or an offset that survived a pin (`audio_padded_to_video_start`) — escalates to `_force_reencode_video=True`. That escalation is what bounds it at two passes. Keep both detectors.

### macOS hosts can't run HLS prep — TCC blocks ffmpeg from `~/Downloads`

ffmpeg / ffprobe run as children of the (non-GUI) Python server process. macOS TCC denies that process access to the user's protected folders (`~/Downloads`, `~/Desktop`, `~/Documents`) — `ffprobe` returns empty JSON + `Operation not permitted`, so `_ffprobe_full` yields `video: None` and prep aborts with a misleading "no video stream" (the file is fine; the process just can't open it). VLC and qBittorrent work on the same files because they're separate `.app`s the user individually granted. Rather than chase per-process Full-Disk-Access grants, `HLS_AVAILABLE = platform.system() != "Darwin"` short-circuits the prep endpoints with a clear message, `state_snapshot` exposes `hls_available`, and the UI hides the controls. If you ever want HLS on a Mac host, the file would need to live outside the TCC-protected folders **and** the responsible app (Terminal / the service binary) would need Full Disk Access.

### ffmpeg ≥ 4.3 is required for multi-rendition HLS

`-var_stream_map` with subtitle groups is unreliable on ffmpeg 4.0–4.2 — the master playlist sometimes drops audio renditions, sometimes mis-tags `agroup`. `_run_offline_job` calls `_ffmpeg_version()` (cached per process) and fail-fast errors the job before launching ffmpeg if the version is too old. Don't drop this check — the silent-bad-manifest failure mode is hard to diagnose from the UI side (the player just shows "no audio" or stalls on a missing rendition).

### ABR ladder: map the video once per rung, mix copy + transcode in one pass

Since `v7-hls-abr`, `_build_hls_ffmpeg_args` emits multiple video variants (Original + 720p + 480p, capped at source height by `_hls_video_variants`). The shape that works in a single ffmpeg pass:
- **Map `0:v:0` once per rung** (`-map 0:v:0 -map 0:v:0 -map 0:v:0`), then the audios. Output video stream index `i` then lines up with `videos[i]` and the `v:i` entries in `-var_stream_map`.
- **Per-output codec/scale options are index-qualified** — `-c:v:0 copy`, `-c:v:1 libx264 -filter:v:1 scale=-2:720 -crf:v:1 23 …`. A global `-c:v`/`-crf`/`-vf` would apply to *all* video outputs and break the mix. The original rung (`i==0`) can `copy` while the down-rungs transcode in the **same** invocation — that's intentional and supported.
- **`scale=-2:<h>` not `scale=W:<h>`** — the `-2` lets ffmpeg pick an even width preserving aspect ratio; libx264 / yuv420p reject odd dimensions.
- **Set `-maxrate:v:i` / `-bufsize:v:i` on the down-rungs.** Without a VBV cap, CRF alone leaves the master playlist `BANDWIDTH` as the measured peak and the rungs barely shrink — ABR then picks badly. The caps (720p≈3 Mbps, 480p≈1.2 Mbps) make the ladder real.
- **The original copies even when NVENC is present.** This is decoupled from `use_nvenc` (unlike the pre-ABR code, where any NVENC availability forced a full re-encode) — only the scaled down-rungs need the encoder, so a browser-safe source still gets a cheap remux at full quality. Don't re-tie `copy` to `not use_nvenc`.
- **NVENC ⇒ GPU decode (and, when safe, GPU scaling) — two tiers, with a deadlock trap.** On the NVENC path the builder routes decode onto NVDEC; without it the CPU does full-res decode + every software scale and pegs at 80-90% while the GPU idles ~50% (the symptom that prompted this). Two forms, picked in `_run_offline_job`:
  - **All-GPU** (`full_gpu`: `-hwaccel cuda -hwaccel_output_format cuda -extra_hw_frames 8` + `scale_cuda=-2:H:format=yuv420p`, no `-pix_fmt`) keeps frames in VRAM end-to-end. Used **only** when `_has_cuda_scale()` (build has the filter), `_source_nvdec_safe(info)` (h264/hevc/mpeg2/vc1/vp9, 4:2:0 8/10-bit), **and `not copy_original`** (so the ladder is all-encode — no `-c:v copy` rung).
  - **Transparent** (`-hwaccel cuda` only) is everything else — copyable H.264, no `scale_cuda`, exotic formats: NVDEC decodes, frames download to system memory for the CPU `scale`. **It is gated on the NVDEC decode probe below** (`hw_decode`) — see the next gotcha: `-hwaccel cuda` does NOT reliably fall back for codecs whose hwaccel init hard-fails (AV1 on older cards), so the probe decides whether it's added at all.
  - **THE TRAP — never mix `-c:v copy` with cuda-filtered rungs.** A stream-copy rung in the same invocation as `scale_cuda` rungs (under `-hwaccel_output_format cuda`) **deadlocks**: the copy stream races ahead while the muxer / NVDEC surface pool backs up, and ffmpeg wedges at low CPU+GPU with **no progress and no exit** (so the failure-retry never fires — this is why a hang, not a crash, is the danger). That's the whole reason `full_gpu` requires `not copy_original`. The `GPU_STALL_TIMEOUT_SECS=90` watchdog (kills + retries transparent if `out_time` stalls) is the backstop, not the primary defence.
  Either decode form is added only when a rung actually decodes (`needs_decode`). Don't set `-pix_fmt yuv420p` on the all-GPU rungs (forces a hwdownload, defeats the VRAM-resident pipeline — the format is pinned inside `scale_cuda`). And don't loosen `full_gpu` to allow `copy_original`: that reintroduces the deadlock.

### `-hwaccel cuda` does NOT gracefully fall back — probe NVDEC per codec before adding it
The long-standing assumption (baked into old comments) that transparent `-hwaccel cuda` "silently falls back to software for any codec NVDEC can't handle" is **false** for codecs whose hwaccel *initialisation* fails. On a Pascal GTX 1060 (no AV1 decode block) an AV1 source produces, per frame:
```
Your platform doesn't support hardware accelerated AV1 decoding.
Failed setup for format cuda: hwaccel initialisation returned error.
Error submitting packet to decoder: Function not implemented
```
and ffmpeg exits non-zero — it does **not** fall back. Symptoms: on-demand sessions tear down with `ffmpeg rc=1`, bulk prep `FAILED rc=69`, compress fails, and the file-validation scan marks every AV1 file **damaged**. So decode hwaccel is gated on `_gpu_can_hwdecode(sample, codec, pix_fmt)` — a lazy probe (cached per codec+pixfmt, or per path when the codec is unknown) that hardware-decodes **one frame of the actual source** and returns False on failure. Every GPU-decode site passes this through:
- `_build_hls_ffmpeg_args(..., hw_decode=)` — transparent rung + `full_gpu` are both ANDed with it in `_run_offline_job`.
- `_od_build_ffmpeg_args(..., hw_decode=)` — on-demand streaming (`_od_start_encode`).
- `_decode_hwaccel_args(sample=, info=)` — validate/repair decode scan, compress, repair re-encode. Call it **with a `sample`** to get the gating; the bare `_decode_hwaccel_args()` form no longer exists.
NVENC still **encodes** on the GPU when the source can't be hw-decoded — only the decode drops to CPU. This is generation-aware: AV1 keeps NVDEC on cards that support it (RTX 30+), because the probe tests the real GPU, not a static codec list. Don't "optimise" by removing the probe and trusting `-hwaccel cuda`'s fallback, and don't reuse the static `_NVDEC_SAFE_CODECS` list for this (it's GPU-generation-blind — it would force AV1 to CPU even on cards that can decode it).

`%v` in the playlist / segment / init templates expands to each `name:` tag, so the bundle gets `video.m3u8` / `video_720.m3u8` / `video_480.m3u8` plus matching `init_*`/`seg_*` — bare names + `cwd=<bundle>` still load-bearing (see the fmp4-init gotcha above).

### Rung membership: match segments by counter, not `startswith` — `video` is a prefix of `video_720`

The source video rung is named `video`; the down-rungs `video_720` / `video_480`. Their segments are `seg_video_00001.m4s`, `seg_video_720_00001.m4s`, etc. A naive `name.startswith(f"seg_{rung}_")` membership test is **wrong** because `"seg_video_720_00001.m4s".startswith("seg_video_")` is `True` — the `video` rung swallows the down-rungs' segments. This bit the iOS **reduced-quality offline download**: `_bundle_select_rung` keeps one rung and drops the others, and when the kept rung was a down-rung the dropped source rung `video` (in `drop_names`) matched the kept rung's own `seg_video_720_*` segments → they were excluded from the download `files[]` and never fetched. The kept playlist + fmp4 init still downloaded (matched by **exact** name, which is correct), so the on-device bundle had a playlist referencing segments that weren't there → 404 on the first fragment → fatal `fragLoadError` → "RECONNECTING" / endless buffering. Source-quality downloads were unaffected (`video` is a prefix of nothing that gets dropped). Fix: `_bundle_rung_name` now anchors the segment test to the numeric counter (`re.fullmatch(rf"seg_{name}_\d+\.m4s", …)`), and `_bundle_select_rung._is_dropped` reuses it. The admin **Drop HLS Resolutions** tool (`_trim_one_bundle`, glob `seg_{name}_*.m4s`) has the same latent prefix hazard but is safe today only because it never drops idx 0 (`video`) — if that ever changes, give it the anchored match too.

### Configurable ABR ladder is forward-only — don't bump `OFFLINE_CACHE_VERSION`

The admin-selected down-rung set (`settings.admin_overrides.hls_ladder` → `_hls_ladder_heights` → `_hls_video_variants(info, heights)` → `_build_hls_ffmpeg_args(ladder_heights=…)`) only shapes **new** preps. It deliberately does **not** feed `_offline_cache_key` / `OFFLINE_CACHE_VERSION` — folding it in would invalidate every existing bundle the moment an admin changes the default, forcing a full library re-encode for a setting that's supposed to be cheap. Existing bundles keep their rungs; the **Drop HLS Resolutions** tool slims them without re-encoding. So a bundle on disk can have a *different* rung set than the current default — that's expected, not a bug. The cache key keys on filename|size (v8), so re-encoding a source (repair / compression) changes its size → new key → the bundle rebuilds.

### Trimming a bundle: rewrite `master.m3u8` as STREAM-INF/URI **pairs**, keep the audio `#EXT-X-MEDIA` lines

`_trim_one_bundle` drops a rung by deleting its `video_<H>.m3u8` + `init_video_<H>.mp4` + `seg_video_<H>_*.m4s` **and** removing its entry from `master.m3u8`. A variant in the master is a **two-line unit**: an `#EXT-X-STREAM-INF:…` line immediately followed by its URI line (`video_720.m3u8`). The rewrite walks lines and, on a STREAM-INF whose *next* line is a dropped rung's URI, skips **both**; everything else (notably the audio `#EXT-X-MEDIA:TYPE=AUDIO,…` lines, which are single lines with no following URI) is copied verbatim. Don't filter on the URI line alone — you'd orphan the STREAM-INF header and produce an invalid playlist. If the master rewrite throws, `_trim_one_bundle` returns early and leaves the bundle **untouched** (segments not deleted) rather than orphaning segments behind a half-written manifest. Bundles with an active prep job are skipped (`_offline_cache_path_active`).

### Source compression / repair rewrite the file in place — torrent seeding stops, and only replace when *smaller* and clean

`_compress_one_file` (and the repair re-encode) `os.replace` the source with a re-encoded copy, so a **torrent-backed** file stops matching its pieces and seeding halts — same caveat as repair, surfaced per-row in the UI. Two guards that must stay: (a) the candidate is deep-decoded (`_ffmpeg_decode_scan`) and the original is replaced **only if it decodes clean** — a re-encode that introduced corruption must never overwrite a good source; (b) compression additionally replaces **only if the result is smaller** (`after < before`) — an already-efficient source can re-encode *larger*, in which case it's reported `skipped` and left untouched. On replace, purge the stale HLS bundle for the **old** file (stash `_offline_cache_dir(src)` *before* the replace, like repair, since the size — and thus the key — changes) or it orphans. Audio is `-c:a copy` (same container ⇒ always muxable) — don't switch to re-encoding audio without re-checking container compatibility.

### A compressed file is no longer torrent-backed — mark it per-file or qBit features will destroy it

`compress` differs from `repair` in one critical way: it writes a **per-file `compressed: true`** marker into `library.json` ([main.py](../main.py) `_run_file_compression`, under the `get_library`/`put_library` lock). This exists because the item keeps its `torrent_hash`, but the re-encoded bytes no longer match the torrent — so without the marker every qBit-dependent feature treats the file as fully torrent-backed and one of them will **overwrite it**: the moment qBit rechecks (the recheck button, Cleanup "recover", or just a qBit restart re-verifying resume data), the file reads as *damaged/incomplete*, `/files` hides Play, and the download scheduler re-fetches the original on top of the smaller copy. The marker is **per-file, never per-item** — one torrent can hold many episodes with only some compressed, so clearing the item-level `torrent_hash` would break the rest. Honour `_file_is_compressed` / `_item_has_compressed` everywhere qBit progress or re-download is involved: `/files` reports compressed files complete from disk (ignore qBit), `_reconcile_item_downloads` forces them to priority 0, `_all_nonskip_complete` + the reactivation gate skip them, and **recheck / Cleanup recover (item + torrent) / delete-to-free-space refuse them** (`409` or a `blocked` list — deleting is permanent since they can't be re-downloaded). The download monitor's `build_file_list` rebuilds `item["files"]` from qBit (which knows nothing of the re-encode), so it must **carry the marker forward** or a single monitor pass silently drops the protection.

### Compressing a file in place needs a playback LOCK — Windows `os.replace` fails while anyone holds it open

On Windows `os.replace(tmp, src)` throws `[WinError 5] Access is denied` if **any** process has `src` open — and the reported failure was exactly that: a viewer was watching the episode (VLC) while it compressed. Don't "just retry forever" or skip the file — the robust fix is a **lock** so nothing can be holding it. While a file compresses it lives in `state.compressing_paths` (normalised key via `_compress_lock_key` — `normcase`+`normpath`+`abspath`, so Windows case/separator variants collide). Two halves:
- **Refuse new opens.** `_assert_not_compressing(path)` (HTTP **423**) gates *every* path that opens the source — not just the button the user clicked: VLC `/play`, on-device `/offline-prepare`, on-demand `/stream-ondemand`, `/clip`, **and** the start of `_run_offline_job` (so warm-prep / auto-prep / the play-prep chain all defer too). There are many ways to start playback; gate the **source path**, the one chokepoint they all share.
- **Release existing opens.** `_free_file_for_compression(path)` runs *before* the encode: `stop()`s VLC if it's the current file, `_od_teardown`s any on-demand session reading it, and `terminate()`s any in-flight prep encoding it (tagged `_compress_block` so the prep re-queues instead of erroring). The `os.replace` then retries a few times across the brief post-`terminate` handle-release latency.

Always release the lock in a `finally` (`_compress_one_file` is a thin wrapper that adds/removes the key around `_compress_one_file_inner`) — an errored or cancelled encode must never leave a file permanently unplayable. Note an on-device viewer watching a **fully-prepped** bundle holds no handle on the source (segments are static files), so it isn't force-stopped — but compression purges that bundle, so their stream ends and re-preps; that's expected, not a bug.

### Server runs at raised OS priority — keep heavy children below it

`_raise_own_priority()` (first call in `lifespan`) bumps the StreamLink server to `HIGH_PRIORITY_CLASS` (Windows) / negative `nice` (POSIX) so controls/UI/VLC-control never lag behind a background encode. **The catch: child processes inherit the parent's priority** (Windows: at creation unless a creationflag overrides; POSIX: the nice value). So any *new* CPU-heavy subprocess spawned by the server must explicitly drop itself below normal, or it runs at HIGH and re-creates the exact lag this fixes. Today that's prep ffmpeg (`_ffmpeg_nice_prefix` + `_FFMPEG_SUBPROCESS_KW`) and every analyzer subprocess (`analyzer._lp` + `analyzer._LOWPRIO_KW`). If you add another heavy spawn (a new transcode, a thumbnailer, …), give it the same `nice -n 10` / `BELOW_NORMAL_PRIORITY_CLASS` treatment. Brief one-shots (ffprobe, the `_has_nvenc` probe, `mullvad status`) are fine to leave — they finish in well under a second. Don't reach for `REALTIME_PRIORITY_CLASS`/very-negative nice on the server: it can starve OS/driver threads and needs privilege; `HIGH` is the intended ceiling.

### Pausing prep: a paused bulk job *exits its task* (and releases the slot)

The global pause (`state.prep_paused`, set by `/api/offline-prep/pause`) gates only **bulk** jobs (`queue == "bulk"` — per-item / per-row / overnight). When a bulk job reaches the pause gate at the top of `_run_offline_job`, it marks itself `"paused"` and **returns** — it does *not* sit in a `while paused: sleep` loop holding the `OFFLINE_JOB_CONCURRENCY` semaphore. That's deliberate: holding the single slot would block an interactive play-on-device prep (`queue == "interactive"`, which bypasses the gate) from ever running while the queue is paused. Because the task exits, **resume must re-spawn it** — `_resume_prep()` walks `_offline_jobs`, flips every `"paused"` job back to `"pending"`, and `asyncio.create_task(_run_offline_job(...))` again. If you add a new place that pauses jobs, route resume through `_resume_prep()` or the paused jobs will never restart.

"Stop now" (`_pause_prep(kill=True)`) terminates the in-flight encode via the `job["_proc"]` handle and sets `job["_paused_kill"]` so `_run_offline_job` reads the non-zero ffmpeg return code as an intentional pause (re-queue as `"paused"`, delete the `.part` dir) rather than a real `"error"`. HLS prep has no mid-file checkpoint, so a killed file restarts from scratch on resume — don't assume partial segments are reusable. Never serialize a job dict straight to JSON: `_proc` is a non-picklable `Process` (and `_paused_kill` is transient) — every endpoint extracts explicit fields, keep it that way.

### Validate-and-repair-on-prep: a "before" repair changes the cache key, an "after" repair throws away the bundle

`settings.prep_validate` (admin *Validate & Repair on Prep*) folds the File Validator into bulk prep via `_prep_validate_repair` inside `_run_offline_job`. Three traps:

1. **A `before`-prep repair rewrites the source, so its `_offline_cache_key` (filename|size) changes** (the re-encode changes the size). The job's `out`/`tmp_dir` were computed by the `/offline-prepare` endpoint from the **old** source, so if you encoded into them the bundle would be immediately stale (the next playback computes the new key and re-preps). `_run_offline_job` therefore **re-points `out_dir`/`tmp_dir` at `_offline_cache_dir(src)` after a successful before-repair** (and re-runs the "bundle already exists" short-circuit + re-probes the healed file). If you move the before-hook, keep that re-point — or every healed file double-preps.
2. **An `after`-prep repair purges the bundle that was just built.** `_repair_one_file` deletes the stale HLS dir keyed on the *old* source, which is exactly the one this job produced. That's intentional (the bundle encoded a damaged source), and the file re-preps from the healed source next idle cycle — but don't "optimise" the after-hook expecting the just-built bundle to survive.
3. **The scan/repair ffmpeg must be the prep job's `_proc`, not the admin validator's.** `_prep_validate_repair` binds `set_proc`/`is_stopped` to `job["_proc"]` / `job["_paused_kill"]` (not `state.file_validation_proc` / `file_repair_proc`), so it (a) is terminated by the existing `_pause_prep(kill=True)` / activity-kick loops for free, and (b) never cross-wires with a concurrently-running **manual** admin validate/repair run (which still owns the `state.file_*` slots). This is why `_validate_one_file`/`_repair_one_file` take the callables as params rather than hardcoding the state fields.

GPU note: the validator/repair decode uses transparent `-hwaccel cuda` (`_decode_hwaccel_args`) when NVENC is present, with a per-frame CPU fallback. Hardware decode can surface/conceal decode errors slightly differently from software — acceptable for the bulk corruption scan, but it's why we keep the transparent form (never `-hwaccel_output_format cuda`, which has no fallback and would hard-fail odd sources, producing false "damaged" verdicts).

### Pausing prep must kill running **STT (whisper)** too — gating alone leaves it churning

The `prep_paused` gate is checked only at the *top* of `_run_offline_job` / `_run_stt_job`, **before** the heavy work starts. Once whisper is transcribing it ignores the flag entirely. whisper is the single heaviest background load (a 45-min episode is minutes of CPU on `base`, far longer than the HLS encode), so for a long time `_pause_prep(kill=True)` killed the HLS ffmpeg but left whisper running — the box stayed barely usable after idle-prep "paused", sometimes until a reboot (the original bug report). Fix (v4.10.0): bulk STT is **cancellable** — `_run_stt_job` passes a `threading.Event` + `on_proc` into `stt.generate`, `_pause_prep(kill=True)` iterates `_stt_jobs` and sets the event + `.kill()`s the registered subprocess, the job re-queues `"paused"`, and `_run_whisper` **skips its GPU→CPU fallback retry when cancelled** (else the kill just relaunches whisper). If you add another long-running child anywhere, make it killable the same way — gating the *start* is not enough.

### Activity should shed background load instantly, not on the next 15 s tick

`auto_prep_loop` only re-evaluates every 15 s, so before v4.10.0 a user who arrived mid-idle-prep waited up to a tick (plus the un-killed whisper) before the box recovered. `_activity_kick` (called from the `track_activity` middleware on every genuine interaction **and on SSE connect** — a page load is a GET that wouldn't otherwise count as activity) now pauses+kills immediately — but **only** when `state.idle_prep_hard` (Automatic Stream Prep in **idle mode with a hard stop**). `always` mode is deliberately exempt (its whole point is to prep through activity), and idle **soft** stop is too (the loop's falling-edge graceful pause lets the in-flight file finish). It's a no-op once `prep_paused`, so it's cheap to call per request. It reads the cached `idle_prep_on`/`idle_prep_hard` flags (stamped each loop tick) — don't make it read `library.json` (it's on the hot request path). Idle prep also treats an open dashboard as in-use via `_machine_in_use(for_prep=True)`, so it stays paused while a tab is open (and resumes only once every tab closes + the box is idle). The scheduled reboot deliberately does **not** pass `for_prep` — a forgotten-open tab must not block the nightly reboot.

### When a conversion fails, read `logs/hls.log` — not the UI

The prep UI only shows the last 500 chars of `job["error"]` (an ffmpeg stderr tail). The **full** diagnosis — the exact ffmpeg command line, return code, elapsed time, and the last 300 lines of stderr — goes to `logs/hls.log` (and `logs/streamlink_app.log`) via `hls_log`. A conversion that "fails 3-4 s after starting" is almost always ffmpeg rejecting an argument or a stream mapping at startup; the stderr in `logs/hls.log` names the cause. See [BACKEND.md § Logging](BACKEND.md#logging).

### ffmpeg's stderr must be drained *while* it runs, not after `proc.wait()`

`_run_offline_job` reads ffmpeg's stderr concurrently into a bounded `deque` via a `_drain_stderr` task that runs alongside `proc.wait()`. Do **not** "simplify" this back to reading `proc.stderr.read()` after the process exits: ffmpeg writes stream mapping + warnings + errors to stderr even with `-nostats`, and if nobody drains the pipe the OS buffer (~64 KB) fills, ffmpeg blocks on `write()`, and `proc.wait()` hangs forever — the job sits at "processing" with no timeout. Same rule applies to the `-progress pipe:1` stdout drain. Both tasks end naturally on pipe EOF once the process exits; we `wait_for(..., timeout=5)` them afterward purely as a wedge guard.

### hls.js vs Safari native is a runtime branch, not a build-time pick

`_lpLoadIndex` checks `window.Hls.isSupported()`. With the bundled hls.js 1.5, that's true on every classic-MSE browser **and on iOS/macOS Safari 17.1+** (via ManagedMediaSource — see the next gotcha), so modern iPhones take the hls.js path too. The Safari-native branch is now the fallback for old Safari (< 17.1 iOS) or a failed hls.js load. The two paths read/write **different APIs** for **audio** selection:
- **hls.js**: `hls.audioTrack = idx`, `hls.recoverMediaError()`. The element's `<video>.audioTracks` will be empty — hls.js owns audio-rendition selection.
- **Safari native**: `<video>.audioTracks[i].enabled`. There is no hls.js instance — `lp.hls` is null.

**Subtitles are the exception and are now engine-agnostic:** they're `<track>` children of `<video>` (bundle `sub_<i>.vtt` + on-disk sidecars), so `_lpApplySubIdx` toggles `tr.el.track.mode` the same way regardless of `lp.hls`. Don't route subtitles back through `hls.subtitleTrack` — there are no in-manifest subtitle renditions to select.

### ManagedMediaSource silently caps the forward buffer at ~30 s — set `preferManagedMediaSource: false`

Safari 17.1+ ships **ManagedMediaSource** (MMS) — on iOS as the *only* MSE, on macOS *alongside* classic `MediaSource` — and hls.js 1.5 prefers MMS by default. Under MMS the **OS owns the fetch cadence**: it fires `endstreaming` once it deems the buffer full (~30 s), hls.js pauses segment loading, the buffer drains to ~10–15 s, then `startstreaming` tops it back up. The symptom is a sawtooth buffer that never exceeds ~30 s **no matter what `maxBufferLength` says** — the 180 s outage buffer is silently defeated, with no error and a healthy-looking bandwidth estimate. The fix (v5.28.1) is `preferManagedMediaSource: false` in the Hls config: browsers with classic MSE (macOS Safari, Chrome, Firefox, Edge) then honor the full buffer target. **iOS can't be fixed *that* way** — classic MSE doesn't exist there, hls.js falls back to MMS regardless of the flag (the fallback is built into its `getMediaSource()`).

**iOS preroll override (v5.42.0).** The MMS `streaming = false` signal is only a *hint*, though — appending past it is allowed — so the ~30 s cap **can** be pushed back from the hls.js layer. In hls.js 1.5 the OS hooks are wired exactly as `endstreaming → hls.pauseBuffering()` (stop the fragment loaders) and `startstreaming → hls.resumeBuffering()`, and **nothing internal calls either method** — they exist *only* for these two MMS hooks. So `_lpInstallIosPreroll` (in [static/index.html](../static/index.html), called right after `lp.hls = hls`) overrides the instance's `pauseBuffering` **on true iOS devices only** (`_isIOSDevice`) to no-op the OS stop until the forward buffer (`_lpFwdBuffer`) reaches `IOS_PREROLL_TARGET_SECS` (120 s), then defers to the original. iOS therefore prerolls ~120 s instead of ~30 s; `backBufferLength` is dropped to 30 s on iOS to offset the extra retained memory, and hls.js's own `QuotaExceededError` backoff is the safety valve if WebKit genuinely evicts under memory pressure. **Don't** make `pauseBuffering` an unconditional no-op or apply this off-iOS: on classic-MSE browsers the methods aren't even called, and an unbounded forward buffer on a phone invites quota thrash. The reconnect loop is still the outage backstop on iPhone.

**iOS cold-start kick (v8.11.1) — MMS sometimes never *starts* fetching until a real seek.** The same OS-owns-the-fetch-cadence design has a *cold-start* failure mode, distinct from the buffer-cap one above: at initial load MMS occasionally never fires the *first* `startstreaming`, so hls.js's loaders sit idle from the beginning. `play()` resolves and the first frame decodes (the `<video>` *looks* ready) but the forward buffer stays empty and playback is wedged **with no error** — no fatal `NETWORK_ERROR`, nothing for the reconnect loop or `_lpStallWatch` to catch. The cure users found by hand is to **skip ±10 s**: a real seek to a NEW `currentTime` forces MMS to reassess and start streaming. (This is also why *resuming* mid-episode — which seeks on `loadedmetadata` — tended to work while starting a fresh episode from 0 stalled.) `_lpArmColdStartKick` / `_lpColdStartKickTick` automate it: armed after the cold-start `play()` in `_lpLoadIndex`, a 900 ms poll runs while `!lp.everPlayed`; once a genuine sustained stall is confirmed (not paused — a paused element is autoplay-blocked awaiting the user's tap, which a nudge can't fix — not scrubbing, `readyState < 3`, and the resume-seek already applied), it applies a tiny **relative** forward `currentTime` nudge (~0.35 s, imperceptible at the start; ≤ ~2 s accumulated) each tick, giving up after ~6 s. It's **iOS-only** and **disarmed on the first `playing`** (and on teardown in `_lpDestroyHls`). Crucially, `_lpStallWatch` **cannot** cover this — it early-returns until `lp.everPlayed`, i.e. it only guards stalls *after* playback has once started, which is exactly the state this kick exists to reach. **Don't** widen the nudge off-iOS (every other platform cold-starts fine) or drop the `!v.paused` / `readyState` guards (they keep it from seeking during a legitimate slow first-segment fetch or an autoplay-blocked pause).

### hls.js Auto quality sticks to the lowest rung on fast links — seed `abrEwmaDefaultEstimate`, don't trust the estimator to climb

hls.js's ABR bandwidth estimator is an EWMA whose samples are **weighted by download duration in seconds**. On a LAN, a whole 6 s segment arrives in ~20 ms, so even dozens of back-to-back fragment loads accumulate a fraction of a second of total weight against the 3 s / 9 s VoD half-lives — the estimate essentially **never moves off `abrEwmaDefaultEstimate`** (stock: 500 kbps). Auto therefore starts on the lowest rung (the only one under 500 kbps), fills the whole 180 s forward buffer with it in seconds, and then loads go sparse so it *stays* there for the entire episode — "on-device playback defaults to lowest quality even though the network could load the episode in seconds" (fixed 10.10.2). The asymmetry matters: down-switching always worked, because slow loads have *large* duration weight and crash the estimate quickly. The fix (in [static/index.html](../static/index.html)): `abrEwmaDefaultEstimate` is seeded from `_lpBwSeed()` — the last **self-measured** throughput persisted in `localStorage` (`lpBwEstimate`), defaulting to 20 Mb/s (LAN-class) when unmeasured. `_lpBwNoteFrag` (on `FRAG_LOADED`) records bytes/duration samples from fragments ≥ 300 KB and persists the median (early after 5 samples, again in `_lpDestroyHls`). Two poisoning traps if you touch this: **never sample in on-demand mode** (segment delivery is encode-bound — the "throughput" you'd measure is the transcoder) and **never sample loopback device-copy playback** (that's disk, not network); both are gated in `_lpBwNoteFrag`. A stale-high seed self-corrects: each episode builds a fresh `Hls` instance, and hls.js's abandon rules bail out of a too-ambitious first fragment mid-load, so a genuinely slow link sees at most a brief initial down-switch, not a stall. Safari-native HLS is unaffected (Apple's ABR starts from the first `#EXT-X-STREAM-INF`, which is the original rung).

### The on-device player draws its own controls — never re-add the `controls` attribute, never fullscreen the bare `<video>`

`#lpVideo` deliberately has **no `controls` attribute**: the custom Metro overlay (`#lpControls` — seek bar, ±10 s, play/pause, mute, fullscreen) is the only transport UI, so the player is pixel-identical on every OS/browser (native controls differ wildly — seek bar style, subtitle menus, and iOS hijacks fullscreen into its own player with its own sub rendering). Three traps:
- **Fullscreen targets the whole `#localPlayer` container, not the `<video>`.** `lpToggleFullscreen` calls `requestFullscreen` on the container so the header, transport, and audio/sub/quality selectors stay visible and usable inside OS fullscreen. Fullscreening the `<video>` element summons the native controls and (on iOS) the system player — exactly what this feature removes.
- **iPhone Safari has no element-fullscreen API** (only `<video>.webkitEnterFullscreen`, which is the native player). The FS button is hidden at init when `requestFullscreen`/`webkitRequestFullscreen` is missing on the container; nothing is lost because `#localPlayer` is already a `position:fixed; inset:0` overlay.
- **Scrub commits the seek on pointer-release only.** While dragging, `_lpScrub.t` previews the position; `currentTime` is set once on `pointerup`. Seeking per `pointermove` would, in on-demand mode, restart the JIT ffmpeg on every pixel of drag (each cold seek tears down and re-launches the encoder — see § On-demand).
- **Never drive clock-dependent UI from `timeupdate` alone (fixed 10.8.2).** On iOS, HLS ManagedMediaSource routinely gaps `timeupdate` for many seconds — or stops it outright — while `currentTime` keeps advancing (the same quirk behind the libass clock pump, § "finished cue can linger" below). Everything on the playback clock used to hang off the `timeupdate` listener, so mid-episode the seek bar froze, drags looked ignored (the seek *committed* but nothing repainted; a drag into an unbuffered region stalled playback until dragged back into buffer), skip-intro/credits offers never appeared or auto-fired, and progress saves stopped. The handler body is now `_lpClockTick(v)` (bar repaint + `lpEvaluateSkipOffer` + `lp.lastKnownT` + throttled save), driven by `timeupdate` **and** a 500 ms `_lpClockPump` interval that no-ops unless the player is actively playing; `seeked` also repaints the bar immediately. If you add new time-driven player UI, put it in `_lpClockTick`, not a bare `timeupdate` listener.
- **The element can accept a seek that the decode pipeline silently drops (fixed 10.10.3).** The wedge 10.8.2's repaint fix didn't cover: rarely on iOS the media element keeps honoring seeks *virtually* — `currentTime` jumps, `seeked` fires, the bar repaints — while the underlying pipeline ignores them and keeps presenting the old position, so the seek bar and ±10 s buttons visibly do nothing until the player is stopped and restarted. No error or event fires, and the dead-decoder probe (`_lpRecoverMediaPipeline`) can't see it: frames *are* decoding, just at the wrong position — and that probe only runs on foregrounding anyway. Every user-intent seek (±10 s `lpSeekBy`, scrub commit, skip-intro accept) therefore funnels through `_lpCommitSeek`, whose `_lpVerifySeek` watches the composited frames via `requestVideoFrameCallback`: a real seek halts presentation until the target frame decodes (both engines are frame-accurate), so a sustained run of fresh frames still advancing along the **pre-seek trajectory** is proof the seek was dropped → `_lpPipelineRebuild(target)` rebuilds in place (hls.js `recoverMediaError()` / native `load()`) with the resume machinery armed at the user's target, escalating to a full `_lpLoadIndex` (the manual stop+restart, automated) if a second seek is swallowed within a minute. Cold/unbuffered seeks present no frames while buffering, so the detector can't false-positive there; small seeks (<5 s) are exempt (indistinguishable from normal playback). **Never write `v.currentTime = t` directly for a user-intent seek — go through `_lpCommitSeek`.** Machinery seeks (resume, cold-start nudge) stay direct on purpose: they run against a freshly-rebuilt element where verification would misfire.

Layout: `#lpStage` is `absolute inset:0` — the video owns the whole screen and the header / transport / options panel are absolute overlays, so the UI can never overflow a mobile viewport (the 5.25.0 flex-sibling layout did exactly that). The track selectors + Clip live in the gear-toggled options panel (`.lp-opts` → `#lpTrackRow`); don't make `_lpRenderTrackRows` unhide it — the gear owns its visibility. Overlay visibility is the `.lp-idle` class (opacity-only fade — no reflow, the video never resizes); `.lp-buffering` shows the square spinner on `waiting`. All these classes (plus `.lp-scrub`, `.lp-opts`) are cleared in `lpStop` — if you add a new teardown path, clear them there too.

### A connection that drops *mid-fetch* never fires a fatal `NETWORK_ERROR` — the reconnect loop won't catch it; a stall watchdog must

The on-device player's outage recovery (`_lpNetLost`/`_lpNetRetryNow`) is driven entirely by hls.js's **fatal `NETWORK_ERROR`** event. That event fires when a request *fails* — but a connection that dies **while a fragment is in flight** (the classic tunnel / Wi-Fi↔LTE swap) doesn't fail the XHR, it **hangs** it over the now-dead TCP socket. hls.js keeps waiting on that one request (and won't fetch anything else), fires **no** error, so the reconnect loop never engages and `online` (which short-circuits that loop) does nothing. The request finally times out only at `fragLoadingTimeOut` — which is raised to **45 s** for on-demand cold seeks — so the player keeps "buffering" for up to 45 s *after the connection is already back*. The dead giveaway in a report: "it buffers through a tunnel and won't recover, but **stop + restart loads instantly**" — restart works because it tears down hls.js and builds a fresh request on a live socket.

Fix (v5.36.1): a **stall watchdog** (`_lpStallWatch`, polled every 3 s) plus an `online`-event kick (`_lpKickIfStalled`). Once playback has started (`lp.everPlayed`, armed on the first `playing`), if the playhead is starved (`readyState < 3`, no forward progress) past a threshold, `_lpKickLoader` aborts the hung fragment (`hls.stopLoad()` + `startLoad(-1)`; Safari reloads at the death position) and re-requests from the current position. **The thresholds are mode-dependent and load-bearing:** **9 s in bundle mode** (prepped segments load near-instantly, so any multi-second stall is a dead request) but **35 s in on-demand mode** — *above* the server's 30 s `OD_SEG_WAIT_TIMEOUT`, because a legitimate JIT cold seek holds the request open that long and then either delivers the segment or returns a 504 that hls.js retries itself; kicking sooner would abort a still-progressing transcode wait. Don't lower the OD threshold below `OD_SEG_WAIT_TIMEOUT`, and don't arm the watchdog before `everPlayed` or the initial buffering / first-segment encode reads as a stall.

### Recovery paths must never trust a `currentTime` of 0 — and `recoverMediaError()` resets it without restoring

Two ways the on-device player's recovery machinery can read the playhead as **0** mid-watch, silently restarting playback (and, 15 s later, the progress save — which then **clobbers the server's resume position**) from the beginning. This was the "resuming an on-demand-only show often starts back at the beginning" bug (fixed 9.12.2); on-demand is the visible victim because its 90 s session reaper guarantees a 410 → `_lpReloadOnDemand` reload after any real backgrounding, but the same holes applied to bundle playback:

1. **iOS resets the media element while the WKWebView is suspended.** The OD session reap happens *while the app is backgrounded* — the same stretch in which iOS may tear down the media pipeline — so on return, exactly when `_lpReloadOnDemand` (or a Safari-native `load()` retry) needs the playhead, `video.currentTime` can read 0. `(v.currentTime) || lp.resumeSec` then reloads at the *original* seek target: the last explicit Resume point (stale) or **0** for a fresh play / auto-advanced episode.
2. **`hls.recoverMediaError()` detaches + re-attaches the MediaSource**, which resets `currentTime` to 0 and hls.js **never restores it** (its `startLoad(-1)` lastCurrentTime override only helps callers of `startLoad`). Both call sites — the fatal `MEDIA_ERROR` handler and `_lpRecoverMediaPipeline`'s foreground dead-decoder probe — restarted playback at 0:00.

Fix: the player tracks a per-file **last-known-good playhead** — `lp.lastKnownT`, fed by `_lpClockTick` (`timeupdate` + the 500 ms `_lpClockPump`, so it stays fresh through iOS MMS timeupdate gaps; only when `t > 2`, so a post-reset transient 0 is never captured) and by every `seeked` (unconditionally, so a deliberate jump back to 0:00 isn't overridden; a suspend reset fires no `seeked`), reset by `_lpLoadIndex`. Every recovery path falls back to it when `currentTime` reads ≤ 2 s: `_lpReloadOnDemand`, `_lpNetRetryNow`/`_lpKickLoader`'s Safari-native reloads, the OD audio-switch reload in `lpSetAudio`. The `recoverMediaError` sites additionally arm the existing resume machinery (`lp.resumeSec = t; lp.resumeApplied = false`) *before* recovering, so the rebuilt MediaSource's `loadedmetadata` seeks back to the death position — the same pattern the Safari-native `load()` reload has always used. If you add a new recovery/reload path, route its position through this fallback; never write `v.currentTime || 0` and call it the playhead.

### The quality (Res) menu: `hls.levels` for server streams, `meta.json:videos[]` for the device copy

`_lpRenderTrackRows` builds the **Res** dropdown from `lp.hls.levels` (the hls.js master-playlist parse) and `lpSetQuality` sets `lp.hls.currentLevel` (`-1` = Auto/ABR; a level index pins a rung). Things to keep in mind:
- **Server streams: don't drive the menu from `meta.json:videos[]`.** hls.js owns the level array and its indices; reading `hls.levels` keeps the dropdown values aligned with `currentLevel` no matter how hls.js orders them.
- **iOS-app device-copy playback is the one exception.** The downloaded bundle's `master.m3u8` is trimmed to a SINGLE rung, so `hls.levels` has one entry and can't populate a menu — there the dropdown IS built from the bundle's `meta.json` `videos[]` ladder, offering the other rungs as *server* streams (values `srv:<height>`/`srv:auto`; `dev` = the local copy). Those picks switch the **source** via a full `_lpLoadIndex` reload, not `currentLevel`.
- **Rung identity across a device↔server switch is the HEIGHT, never a level index.** No hls.js instance spans the switch, so an index means nothing on the other side; `MANIFEST_PARSED` pins `hls.levels.findIndex(l => l.height === h)` (no match — the admin changed the ladder since the download — degrades to Auto).
- **Safari native HLS gets no per-level Res options.** Safari auto-adapts among the variants but exposes no reliable API to *pin* a level, so plain server playback keeps `#lpQualityRow` hidden and level-pinning no-ops. Device-copy playback still shows the source-switch options (`dev` / `Auto — Server`) there, since those are full reloads. Don't try to wire a manual level selector to `<video>` for Safari — there isn't one.
- **Quality is session-only.** Unlike audio/sub picks, it's not persisted via `/local-tracks` — the right rung depends on the current connection, so every session starts at Auto (or the device copy, when downloaded). The `srv:*` override (`lp._srvOverride`) is additionally **per-file**: `_lpLoadIndex` drops it when a different file loads, so back-to-back episodes each default to their own device copy.

### Always destroy the previous hls.js instance before re-using `<video>`

When advancing to the next episode or switching files, `_lpDestroyHls()` MUST run before assigning a new `<video>.src` or `attachMedia`-ing a fresh hls.js instance. Otherwise the old hls.js keeps a reference to the media element and can fight the new pipeline (especially on Safari, where a leftover hls.js error handler will fire on the new native-HLS playback). `lpUnloadCurrent` does this; if you add a new code path that swaps the source, call `_lpDestroyHls` there too.

### Bundle subs and sidecar subs share the `<video>.textTracks` array

When hls.js is active, it surfaces the bundle's subtitle renditions through its own `hls.subtitleTracks` API. We also append sidecar `.srt`/`.vtt` files (from `_list_sidecar_subs`) as `<track>` children on the `<video>`, which lands them in `video.textTracks` **after** the bundle's tracks. The frontend uses a sentinel `"sidecar:N"` string for sidecar picks in the dropdown so the index space doesn't collide with bundle indices. If you add a new subtitle source, follow the same naming convention or the audio/sub-pick persistence will save garbage indices.

### Image subs (PGS / VOBSUB / DVB) are intentionally not in the bundle

`_ffprobe_full` flags subs with `codec_name in {hdmv_pgs_subtitle, pgssub, dvd_subtitle, dvdsub, dvb_subtitle, vobsub, xsub}` as `image_based: True`. `_build_hls_ffmpeg_args` filters them out before mapping streams — HTML5 `<video>` can't render bitmap subs through `<track>`, and ffmpeg can't transmux them to WebVTT (would need OCR). They surface in `meta.json:skipped_image_subs` for the UI to flag. If a user complains "my subs are missing on the phone but show in VLC", check this list first. The VLC path reads the source MKV directly so image subs work there.

### Cache key is sha256(VERSION | path | mtime | size), and VERSION includes layout

`OFFLINE_CACHE_VERSION = "v3-hls"`. Bumping the version invalidates every existing bundle because it changes the key. Old `<sha>.mp4` cache files map to *different* keys under v3 (since v3 keys never resolve to a `.mp4`), so they auto-orphan and surface in the admin tab. If you change the ffmpeg invocation in a way that breaks compatibility (segment naming, codec, container), bump the version — don't try to be clever about partial invalidation.

### Path traversal in `/offline-cache/{key}/{filename}`

`offline_cache_bundle_file` enforces `_CACHE_KEY_RE = ^[a-f0-9]{24}$` and `_BUNDLE_FILE_RE = ^[A-Za-z0-9._-]+$`. The cache_key check kills obvious traversal (`..`, `/`, leading dots); the filename check kills the same plus URL-decoded variants. Don't relax these — even though FastAPI's path-param parser doesn't pass `/` through `{filename}` by default, Path arithmetic with a malicious filename could still resolve outside the cache root.

### `/prep-all` must serialize ffmpeg jobs

`/api/library/{id}/prep-all` enumerates every video file in a library item. Without a global concurrency cap, that fires `asyncio.create_task(_run_offline_job(...))` for each file in one tight loop — a 77-episode pack instantly spawns 77 ffmpeg processes. Two failure modes both trip:
1. **NVENC session limit.** Consumer NVIDIA encoders (Pascal/Turing) reject NVENC sessions past the driver's 2–3-encoder cap. Excess jobs ffmpeg-exit immediately with `Cannot load nvcuda.dll`-style errors, the job's `error` field is set, and the UI tallies them as "prep errors".
2. **CPU/IO storm on the libx264 path.** Even with `-threads 2`, 77 concurrent ffmpegs is 150+ encoder threads plus 77 decoders fighting over the same disk, OOM-killing some and timing out others.

Keep the `_offline_job_sem()` semaphore in place (`OFFLINE_JOB_CONCURRENCY = 1`). Jobs sit in `status="pending"` until they acquire it; both `/prep-status` and `/api/offline-active` already treat `pending` as in-progress, so the UI behaves correctly. If you ever raise the cap, also re-baseline `started_at` inside the semaphore (already done) so per-job ETAs don't include queue time.

### Resume seek lands on segment boundaries

HLS playback seeks land on the nearest fmp4 segment boundary, then plays from there. With 6-second segments, the resume position can drift up to ~6 s after the saved position. The browser handles the within-segment offset automatically after the segment loads, so this is mostly invisible — but if a user reports "my resume is always a few seconds late on the browser player but not VLC", this is why. Don't shrink the segment size to compensate (you'd just multiply the segment count without solving the underlying snap-to-boundary behavior).

### Local-player track picks ≠ VLC track picks

Three persistence layers live in `file_progress`:
- `audio_track` / `subtitle_track` — VLC's elementary-stream IDs (from `"Stream N"` keys of `vs.information.category`). Set via `/api/vlc/track/audio/{id}`, applied by `_apply_track_prefs` after a short delay on VLC playback start.
- `local_audio_idx` / `local_subtitle_idx` — 0-based indices into the HLS bundle's `meta.json.audios` / `subtitles` arrays. Set via `/api/library/{id}/local-tracks`, applied by the frontend on `MANIFEST_PARSED` / `loadedmetadata`.
- `audio_sel` / `subtitle_sel` — the **resolvable descriptors** (8.9.0 / 8.5.0). These are the cross-player, cross-episode layer: they carry a pick across a device↔VLC switch and onto the next episode (also stored per profile+series). They **outrank** the raw ES-ID / bundle-index layers, which remain the same-file/legacy fallback.

The raw ES-ID and bundle-index layers are engine-local (a VLC ES ID means nothing to the phone and vice versa), which is why they're separate keys — but the descriptor layer is deliberately shared, so a deliberate audio/subtitle pick now follows the viewer between the TV and their phone. A fourth, unrelated key sits in the same dict: `audio_offset_ms`, the on-device player's **manual audio delay** (11.10.0) — a plain scalar in milliseconds, per-file only, with no series fallback and no VLC counterpart (VLC honours the source's track delay correctly, so it never needs one). `update_progress` and `mark_watched` preserve **every** one of these keys across writes (spread `_TRACK_PREF_KEYS`). Don't collapse the raw ES-ID and bundle-index keys into one field thinking "they mean the same thing" — they don't.

### `createMediaElementSource` is a one-way door — build the audio graph lazily

The on-device player's **Sync** slider (manual audio delay, issue #14) works by splicing a WebAudio `DelayNode` between `#lpVideo` and the speakers. Two things about that are irreversible and easy to get wrong:

- **You cannot un-route the element.** Once `createMediaElementSource(video)` has been called, that element's audio goes through the graph for the life of the page. Disconnecting the node doesn't hand playback back to the element — it just produces silence. So the only safe "off" is `delayTime = 0` with the node still in the chain, and there is **one** `<video id="lpVideo">` for the whole page, so the graph outlives every file change. That is why `_lpLoadIndex` calls `lpSetAudioOffset(..., false)` **unconditionally, including with 0** — otherwise an episode advance inherits the previous episode's delay.
- **On iOS it changes the audio session.** A routed element plays through the WebAudio session, where the hardware ringer/silent switch can mute it — behaviour a plain `<video>` doesn't have. So `_lpEnsureAudioGraph()` is called **only when a non-zero offset is first requested**: a viewer who never touches the slider keeps the element's native audio path and never pays for a feature they didn't use. Don't "simplify" this by building the graph at load time.

**WebKit can't be tapped at all — which is why there are two mechanisms.** Safari accepts `createMediaElementSource()` on an MSE-backed element, throws nothing, and then simply never routes it: the node sits connected to silence while the element plays on through its own output. Measured on Safari 26.5.2 — a plain `<audio>` element taps fine (analyser RMS **0.43**) while the identical hls.js `blob:` element reads **0.00** with playback running and `currentTime` advancing. Every StreamLink playback path is MSE-backed (hls.js in bundle, on-demand and device-copy modes), so on WebKit the slider could only ever be a lie. `_LP_WEBKIT_ONLY` routes WebKit to the **SourceBuffer `timestampOffset`** mechanism instead (`_lpOffsetMode()` → `"buffer"`), which is verified to work on **macOS Safari**. **iOS gets no control at all** — the same bias has no audible effect on iPhone/iPad in either mobile Safari or the app's WKWebView (iOS is the one platform running hls.js over `ManagedMediaSource` instead of classic `MediaSource`), so `_lpSbSupported()` excludes `_isIOSDevice()` rather than presenting a third dead slider. The real cure there is issue #13's prep-time fix. Don't "fix" this by feature-detecting `AudioContext`: the API is present and fully functional on WebKit, it just won't tap MSE — so any capability probe has to test the *tap*, not the API. Note the two mechanisms are not interchangeable: WebAudio is instant and works in on-demand mode, while the buffer bias needs a separate audio rendition and a re-load to apply. See [STREAMING.md](STREAMING.md).

What routing the element *doesn't* break: `video.muted` and `video.volume` still reach the speakers through the graph (verified in Chrome with real audio flowing — a muted element feeds silence into the `MediaElementAudioSourceNode`), so `lpToggleMute` needs no `GainNode` shim.

Also note the direction is not a UI choice: a `DelayNode` can push audio later and nothing can push video later, so the control is 0…+1000 ms and the UI labels it as a *delay*. See [STREAMING.md § Manual audio offset](STREAMING.md).

### Styled ASS/SSA subtitles: libass overlay in bundle mode, VTT everywhere else

ffmpeg's `-c:s webvtt` strips karaoke, positioning, custom fonts, and animations from ASS/SSA down to plain WebVTT — jarring for anime fansubs. **Milestone 16.10 (implemented):** for a **styled** sub (codec `ass`/`ssa`) the prep pipeline now ALSO emits the raw `sub_<i>.ass` (stream-copied) next to the flattened `sub_<i>.vtt`, and extracts the source's embedded fonts as flat `font_<n>.<ext>`. The browser/on-device player renders the raw ASS with **libass-wasm (SubtitlesOctopus)** on a `<canvas>` overlay (`_lpApplyStyledSub` / `_ensureLibassLib`, vendored under `static/vendor/subtitles-octopus*`). Footguns:

- **The VTT stays the universal fallback — never drop it.** Old bundles (no `ass_file`), on-demand/JIT mode (no bundle dir), and any libass load/construct failure all fall back to the flattened VTT `<track>`. `_lpApplySubIdx` only takes the overlay path when `_lpStyledSubFor(key)` returns truthy (bundle mode + numeric key + `meta.subtitles[i].styled && ass_file`). Don't pipe unstyled ASS into a `<track>` — players render it as broken WebVTT.
- **Fonts must be FLAT files, not a `fonts/` subdir.** `offline_cache_bundle_file` serves a single path segment (`{filename}` captures no slashes) and `_enumerate_bundle_files` (iOS) lists one level deep — a subdir font can't be fetched. Hence `font_<n>.<ext>` in the bundle root; `_BUNDLE_FILE_RE` already allows those names.
- **`-dump_attachment` exits NON-ZERO after writing.** `_extract_bundle_fonts` gates success on the output file existing, not ffmpeg's return code. It's a separate ffmpeg pass (only run when a styled sub was kept) — no mkvtoolnix dependency (Windows-first: reuse the bundled ffmpeg).
- **Old bundles need a re-prep to gain styling** — `OFFLINE_CACHE_VERSION` is intentionally NOT bumped (a bump would force-re-prep every file); existing bundles just keep using VTT until re-prepped.
- **Overlay teardown is load-bearing.** The libass instance is disposed in `lpUnloadCurrent`, on file change, and whenever the sub selection moves off a styled track (`_lpTeardownOctopus`). Leaking it leaves a stale canvas over the next file; teardown also sweeps any stray `.libassjs-canvas-parent` nodes as insurance.
- **The apply race guard must be an attempt TOKEN, not the subUrl (fixed 7.16.5).** `_lpApplyStyledSub` awaits the ~3 MB libass download; during that window it re-enters — at playback start `_lpApplySubIdx` fires up to 3× for the same default sub (MANIFEST_PARSED/`loadedmetadata` + the `loadeddata`/`playing` re-asserts), and a user toggle-dance does the same. Every re-entry sees `lp.octopus === null` and the *same* subUrl, so a URL-equality guard let **each** attempt construct its own SubtitlesOctopus; each overwrote `lp.octopus` and the earlier instances leaked — canvas + worker rendering ASS forever, which showed as *ghost ASS drawn under the flattened VTT* after switching tracks (teardown could only dispose the last instance). Guard with a monotonic counter (`_lpOctopusSeq`, bumped by every apply AND every teardown): duplicate applies for the in-flight sub no-op on `lp._octopusSub`, and a superseded attempt (including its late `fallbackToVtt`) returns without constructing or touching `<track>` modes. Same pattern in the offline iOS player (`_octopusSeq` in `downloads.html`).
- **An overlay constructed while `videoWidth` is 0 can stay blank forever (fixed 7.16.5).** The vendored lib sizes its canvas on ONE `loadedmetadata` and on *layout* resizes only. hls.js/ManagedMediaSource (and iOS native HLS) can expose intrinsic dimensions a beat after `loadedmetadata` — an instance constructed in that window keeps a hidden 0×0 canvas until the user toggles the track (rebuild). The `<video>` fires a `resize` event when intrinsic dimensions change, which the lib does NOT listen for: `_lpOctopusNudge` (host) / `_octopusNudge` (offline iOS) call `octopus.resizeWithTimeout()` from `resize` + `loadeddata`/`playing` backstops.
- **While the overlay is up, `<track>` modes must be actively ENFORCED, not just set once.** The browser's automatic track selection / iOS's native player UI can flip a VTT track back to `"showing"` at any time, double-drawing the flattened VTT under the styled overlay. Both players re-disable every non-disabled track from the `TextTrackList` `change` event whenever `octopus`/`_octopusSub` is set (converges — only tracks that aren't already disabled are touched).
- **A finished cue can linger until the next cue — feed the worker's clock yourself (fixed 9.10.3).** SubtitlesOctopus only ERASES a cue when its worker re-renders past the cue's end time, and the worker re-renders continuously *only while it thinks the video is playing* (`setIsPaused(false)`, driven off the `<video>`'s `playing` event). Two things silence that, each leaving the last styled cue painted until the NEXT cue forces a fresh render — "the sub stays on screen, sometimes for a minute" over a quiet stretch: (1) the overlay is constructed **after** the ~3 MB wasm downloads, often *after* `playing` already fired, so `onPlaying` is missed and the worker stays in its initial paused state, re-rendering only once per browser `timeupdate`; (2) even when free-running, the worker **self-pauses** on its own watchdog (`getCurrentTime`: "Didn't receive currentTime > 5 seconds. Assuming video was paused.") whenever `timeupdate` messages gap — which iOS/HLS ManagedMediaSource does routinely, for many seconds. Both make cue-clearing hostage to the browser's irregular `timeupdate` cadence. Fix: `_lpOctopusPumpStart` runs a **250 ms interval** while the overlay is up that re-asserts the real play/paused state (`octopus.setIsPaused(!v.paused…)` — idempotent, the lib no-ops when unchanged) and pushes `octopus.setCurrentTime(v.currentTime)`, so the 5 s watchdog never trips and the worker keeps erasing cues on time. Stopped by `_lpTeardownOctopus`. Don't lean on the lib's own `timeupdate` listener — its cadence is not guaranteed and gaps for seconds on iOS.
- **A STATIONARY cue still lingered after the pump fix — the DEFAULT `wasm-blend` renderer doesn't post the "now empty" frame; use `renderMode:"js-blend"` (fixed 9.11.1).** The 9.10.3 pump above keeps the worker *rendering*, but that wasn't the whole bug: libass only erases a cue when its worker **posts** a "now empty" canvas, and a *stationary* cue posts exactly twice — once when it appears, once when it ends. The default `wasm-blend` renderer (`renderBlend()`, SubtitlesOctopus's own blend function) does **not** reliably flag the visible→empty transition as `changed`, so that single end-of-cue post never fires and the cue stays painted until the *next* cue forces a fresh render (up to a full minute over a quiet stretch). **Moving/animated cues are immune** — they post a canvas every frame, so their end is always caught (this asymmetry — "moving fine, stationary linger" — is the tell). Fix: construct the overlay with `renderMode:"js-blend"` in `_lpApplyStyledSub`'s `opts`, which routes rendering through stock libass `renderImage()` + libass's own `detect_change` (flags the disappearance correctly). Costs a bit more main-thread compositing than wasm-blend; correctness wins. Keep the pump too — the two fix independent freezes.
- **THE ACTUAL FIX (9.11.2): the stuck cue was a REPAINT problem, not a render problem — patch the vendored lib to draw synchronously instead of via `requestAnimationFrame`.** The pump (9.10.3) and `renderMode` (9.11.1) fixes were aimed at the *worker*, but on-device diagnosis proved the worker was fine: it logged renders continuously the whole time a cue was stuck, and **opening dev tools instantly cleared the cue** (a page resize forces a canvas repaint). So the worker had already advanced past the cue — the stale frame was simply **not being repainted**. Cause: the vendored `subtitles-octopus.js` draws each frame from a `window.requestAnimationFrame(renderFrames)` callback in its worker-message handler, and **while a `<video>` is actively presenting frames the page's rAF callbacks get starved/coalesced**. A moving cue posts a fresh frame every tick so the canvas stays current; a *stationary* cue's single "now empty" draw sat on one un-serviced rAF until an unrelated event (devtools, a tap, the next cue) forced a repaint — up to a minute. Fix: **patched `static/vendor/subtitles-octopus.js`** so the `renderCanvas`/`renderFastCanvas` handlers call `renderFrames()`/`renderFastFrames()` **directly** (the worker message runs on a normal task, not rAF-gated; the canvas-2D draw still composites on the video's own frame cadence). **This is a local modification to a vendored file — grep for `StreamLink patch` and RE-APPLY it after any SubtitlesOctopus re-vendor/upgrade**, or this bug silently returns. (The `wasm-blend`→`js-blend` diagnosis in the bullet above may have been a red herring; the sync-draw patch is renderer-independent. Both are kept as belt-and-braces.) The lib is served from `/vendor/` to every surface (host browser, in-app dashboard, offline snapshot via the manifest in `main.py`), so no app rebuild; the `ios-app/www/subtitles-octopus.js` copy is orphaned (nothing loads it) and left unpatched.
- **iOS native fullscreen shows no overlay.** OS fullscreen targets `#localPlayer` (a container), so the canvas — a descendant — renders in fullscreen on desktop/Android. But iPhone Safari falls back to `video.webkitEnterFullscreen()` (native player), which only shows in-manifest tracks, so the libass overlay disappears in iOS native fullscreen. Non-fullscreen (inline) playback still shows it. This applies to **both** iOS surfaces: the online in-app dashboard (host `static/index.html` in the WKWebView) and the **offline downloads player**.
- **Offline iOS player (`ios-app/www/downloads.html`) has its own libass copy.** The offline player is a bare native `<video>` served from the app bundle (the host dashboard can't load offline), so it can't reuse `static/index.html`'s overlay. It ships a parallel implementation (`_setupOfflineSubs`/`_applyStyledOfflineSub`) with the octopus assets **vendored into `ios-app/www/`** (they bundle via `cap sync`; not the host's `/vendor/`). The raw `sub_<i>.ass` + `font_<n>` files download with the bundle (they aren't rung-specific, so `_bundle_select_rung` keeps them) and are served by the native `LocalMediaServer` — whose MIME map mirrors `_HLS_MIME` (add new suffixes there too) and which sends `Access-Control-Allow-Origin: *`. The `<video>` needs `crossorigin="anonymous"` or the out-of-band VTT `<track>` fetch is CORS-blocked. Any native `www/` or `.swift` change needs a full `./build-ipa.sh` rebuild.
- **The octopus WORKER must never fetch from the loopback `LocalMediaServer` — prefetch on the main thread (fixed 7.17.1).** When the ASS/fonts live at `http://127.0.0.1:<port>` (a downloaded bundle — both the offline player and the dashboard's device-copy playback), passing their URLs to SubtitlesOctopus makes the **worker** fetch them with synchronous XHR — a cross-origin request from a host-/`capacitor://`-origin worker that WKWebView doesn't reliably allow, and its failure surfaces only as `onError` → the silent VTT fallback, i.e. "styled subs quietly render unstyled". The *page's* loopback fetches provably work (`meta.json`/`master.m3u8` load that way), so both players prefetch the `.ass` as text and every font as a Blob on the main thread, then hand octopus `subContent` + same-origin `blob:` font URLs (revoked on teardown — the worker copies them into its FS at init). Host-bundle playback keeps plain `subUrl`/font URLs (same-origin for the worker, proven path). Also fixed 7.17.1: the dashboard's synthesized local prep omitted `meta.fonts`, so even a working overlay lost the embedded fansub typefaces on the device copy.

### iOS background playback — the rules that make it work at all

Eleven constraints, each of which independently breaks the feature. See
[STREAMING.md § 2b](STREAMING.md) for the design and `NativePlayback.swift`.

- **`UIBackgroundModes: audio` does NOT keep a WKWebView `<video>` playing.** WebKit
  interrupts any media session with a *video* track when the app backgrounds — the
  background mode keeps the *process* alive, not the web element. That's the whole
  reason a native `AVPlayer` exists here. Don't "simplify" this away by deleting the
  plugin and just adding the plist key; it will look right and play nothing.
- **A backgrounded app cannot draw — so libass on an external display is impossible.**
  No CADisplayLink ticks, no CoreAnimation commits, no *app-drawn* content on a second
  `UIScreen`. Styled ASS therefore cannot survive a true lock by any route. TV Mode
  (backlight down, app *foreground*, mirroring alive) is the answer, and is also the
  fallback if wired external playback disappoints. **This does not extend to an
  `AVPlayerLayer`**: its frames come from the media server, not the app's render loop,
  which is what lets the backgrounded handoff put video on an external window at all.
  Don't read this bullet as "nothing can appear on the monitor while locked".
- **`AVVideoComposition` is unsupported for HLS assets**, so the "burn the subtitles
  into the frames inside AVFoundation" idea is a dead end. Every StreamLink playback
  path is HLS.
- **`Activity.request` from a background handler is unreliable.** The playback Live
  Activity is therefore started on the first `arm()` — while foreground — not at
  handoff time. iOS surfaces it in the Island on minimise, the same behaviour
  `TVRemote.swift` already relies on.
- **Never push a Live Activity's clock at 1 Hz.** ActivityKit budgets updates and
  starts dropping them, which freezes the very clock the pushes were meant to drive.
  Seed SwiftUI's self-advancing `ProgressView(timerInterval:)` / `Text(timerInterval:)`
  from a sampled `(position, stamp)` pair — they count up with *no* pushes — and push
  only on real state changes plus a slow heartbeat. `PlaybackLiveActivity.changeKey()`
  deliberately excludes `position` for this reason.
- **`Shared/` compiles into BOTH targets, so its intents can't name App-target types.**
  Referencing `NativePlaybackManager` from `PlaybackIntents.swift` fails the widget
  extension build with "cannot find in scope". The seam is `PlaybackCommandBus`: the
  shared file declares only a protocol + weak static sink, the App target registers
  the real player, and the intent — which runs in the *app's* process — finds it at
  tap time. A command arriving with no sink (app was killed, activity still on screen)
  is parked in the App Group and drained on `didBecomeActive`.
- **`AVAudioSession.setCategory(.playback)` is PROCESS-WIDE.** Activating it at launch
  would make every WKWebView sound ignore the ringer switch from boot. It's set
  lazily, at the moment of handoff, and deactivated with
  `.notifyOthersOnDeactivation` on hand-back so WebKit gets its session back.
- **TV Mode must restore brightness on every exit path, including a crash.** iOS does
  **not** put brightness back after a force-quit, so a crash while dimmed leaves the
  user with an apparently dead phone and no clue why. Covered: explicit exit,
  `willResignActive` (call / Control Centre / power button), `willTerminate`, and a
  `strandedBrightness` value in the App Group restored on the next `load()`. If you
  add a new path into TV Mode, add its exit too.
- **`AVMediaSelectionGroup` ordering is not guaranteed to match `meta.json`.** Match
  audio/subtitle options by `displayName` then `locale.languageCode` — never by index,
  or some bundles silently play the wrong language.
- **An `AVPlayer` with no `AVPlayerLayer` is AUDIO-ONLY — the routing flags alone do
  nothing** (fixed 14.1.1). `allowsExternalPlayback` /
  `usesExternalPlaybackWhileExternalScreenIsActive` describe how an already-*presented*
  video is routed; they do not conjure a presentation. The handoff built a bare
  `AVPlayer`, so locking the phone with a monitor attached kept the audio and lost the
  picture: `isExternalPlaybackActive` never flipped and the monitor went on mirroring,
  which at lock means mirroring the **lock screen**. `attachVideoSurface()` gives the
  player a layer on every path — either a `UIWindow` of our own on the external
  `UIScreen` (**Direct**, the default; creating it replaces mirroring, and it must be
  built at `willResignActive`, the last guaranteed composite pass, not inside
  `didEnterBackground`) or a layer at the back of the app's own window behind the
  opaque webview (**Mirrored**, leaving AVFoundation's route to take over). Which one a
  given adapter honours through a lock isn't decidable from the host, so it's the
  `streamlink_app_extmode` setting. The flip side: **attach no layer at all when no
  display is connected.** A main-screen `AVPlayerLayer` is the classic way to make
  AVFoundation *suspend* video on background (the documented cure for background audio
  is `playerLayer.player = nil`), so an unconditional attach would trade the plain
  locked-phone-listening case for a monitor that isn't there. Layerless is wrong with a
  display and right without one — `attachVideoSurface()` gates on exactly that.
- **TV Mode may not draw ANYTHING opaque — the monitor is mirroring that framebuffer**
  (fixed 14.1.1). `#lpTvVeil` was a full-screen black `<div>`, so "blank the phone"
  blanked the TV as well and the episode disappeared from both screens at once. The
  only lever that darkens the phone without touching a mirrored pixel is the
  **backlight** (`UIScreen.main.brightness = 0`), which the native side already pulls;
  the veil is now a *transparent* touch shield that exists solely to swallow taps and
  carry the double-tap exit. Anything else TV Mode puts on screen — the `#lpTvHint`
  chip — must retire itself. Same rule for the transport bar, which is why entering TV
  Mode adds `lp-idle`.

Two more, on the JS side:

- **`visibilitychange`→hidden cannot be trusted to complete before suspension.** It is
  a best-effort freshness push only; the native side extrapolates the playhead from
  the last arm using wall-clock elapsed time, with a 0.3 s rewind so error always
  lands *behind* the true position. Don't make the handoff depend on that event.
- **The prefs live in host-origin `localStorage`, and proxied playback has its own
  empty origin.** `streamlink_app_bgplay` / `streamlink_app_tvmode` /
  `streamlink_app_extmode` must be seeded
  through the `am=` param in `_appTryLocalHandoff` **and** read back in
  `_appProxiedSeedStorage` — otherwise background playback reads as "off" in exactly
  the common case (playing a *downloaded* episode online, which always routes through
  a proxied session). Same trap as the auto-manage prefs.

### iOS drops a showing `<track>`'s cues when the WKWebView is suspended — re-showing won't re-fetch, recreate the element

On iOS (Safari and the Capacitor app), backgrounding the app / opening another app while a subtitle `<track>` is set to `mode="showing"` makes WebKit discard that track's parsed cues. On return the element reports `readyState === 2` (LOADED) but `track.cues` is empty (or `readyState === 3` ERROR) — and setting `mode="showing"` again does **not** trigger a re-fetch, because WebKit considers the resource already loaded. The active subtitle therefore renders nothing while every other track still works: tracks that weren't showing at suspend time are untouched (they fetch fresh on first selection), which is exactly why "switch to the AI track and it appears" and "stop + resume fixes it" (a full reload rebuilds every `<track>`). The only reliable recovery is to **remove the `<track>` element and append a fresh one** so the browser re-fetches the VTT — re-assigning `.src` or toggling `.mode` is not enough. `_lpSubTrackBroken` / `_lpRecreateSubTrack` / `_lpRecoverActiveSub` in `static/index.html` do this for the active track on `visibilitychange`→visible (and when the user re-selects a dropped track); they guard on cues actually being empty so healthy tracks never flicker. See [STREAMING.md](STREAMING.md).

### The browser's automatic text-track selection resets an early `mode="showing"`

Setting the pending subtitle's `track.mode = "showing"` at `MANIFEST_PARSED` / `loadedmetadata` is too early: when media data loads, the browser runs its own *automatic text-track selection* pass and can silently flip track modes back to `disabled`. Symptom (fixed 7.16.4): the on-device player's dropdown names the correct sub (UI state is `lp.pendingSubtitleIdx`), but nothing renders until the user toggles to another sub/off and back — the toggle re-sets the mode *after* the browser's pass. The play-init therefore re-asserts `_lpApplySubIdx(lp.pendingSubtitleIdx)` on one-shot `loadeddata` + `playing` listeners; it's idempotent, and `pendingSubtitleIdx` tracks any user change made in between, so re-asserting never fights the viewer.

### Unlocking the phone can leave subs playing over a dead video/audio pipeline

The inverse of the dropped-cues gotcha above: a suspend (screen lock, long background) can kill the media **decoder** while the element still reports `playing` and `currentTime` keeps advancing — so `<track>` cues and the libass overlay keep rendering over a black, silent video. No `error` fires and `readyState` looks healthy, so it can only be detected empirically: `_lpRecoverMediaPipeline` (on `visibilitychange`→visible, after a settle delay) snapshots `getVideoPlaybackQuality().totalVideoFrames` (fallback `webkitDecodedFrameCount`), and if the clock has advanced ~1.5 s later with **zero** new decoded frames, the pipeline is dead — `hls.recoverMediaError()` rebuilds the MediaSource in place (Safari-native falls back to `_lpNetLost()`'s reload-at-position machinery). Guards: skips when paused/ended, when `lp.netDown` (the network-recovery loop owns that case), and when no frame counter exists. Fixed 7.16.4.

### Service worker is an eviction stub — keep it that way

`static/sw.js` exists only to unregister itself and `caches.delete` everything it ever cached, so devices with the old "Handoff" SW installed don't stay pinned to a stale app shell. Don't reintroduce caching strategies, navigation fallbacks, or API caches in `sw.js`. Once enough time has passed that no device has the old SW alive, the file and the `evictLegacyServiceWorker` call in `index.html` can be deleted entirely. (The offline cached player does NOT use a service worker — it's a device-side snapshot served by the native loopback server; see below.)

### Offline cached-player mode (`?offline=1`) — the rules that keep it working

The iOS app serves a device snapshot of the dashboard from `LocalMediaServer` when the host is unreachable ([PLAYER_CACHE_PLAN.md](PLAYER_CACHE_PLAN.md), [STREAMING.md](STREAMING.md)). Footguns, learned the careful way:

- **Never restart LMS while it's serving the page.** In player mode the server *is* the page's origin: any `lms.start`/`lms.stop` kills every later asset load (lazy hls.js/octopus, the next episode's segments). That's why bundles are mounted same-origin at `/StreamLinkBundles/<sha>/` and offline `_appStartLocalPlayback` just builds that URL — and why offline never sets `lp._lmsActive` (so `lpStop`'s teardown can't fire either).
- **A dead `/api` RESOLVES, it doesn't throw.** Offline, `location.origin` is the loopback server: `fetch("/api/…")` returns a fast 404 — `.catch()`-based offline fallbacks never fire. Every write path must branch on `_appOffline` explicitly (`saveProgress`, `_lpSaveLocalTracks`, `_lpFlushProgress` — a `sendBeacon` to the loopback is a silently lost write).
- **Nothing can live in loopback-origin localStorage.** The LMS port is ephemeral, so the origin — and its storage — changes between runs. The profile comes from `OfflineStore.getProfile()`, and the host URL rides the `?host=` query param (for the Reconnect probe).
- **The `data-ui-version` badge is load-bearing for staleness.** `/api/player-manifest` reads the served `index.html`'s badge as the snapshot version (the `UI_VERSION` constant historically drifts — it sat at 7.15.0 while the badge said 7.17.2). Same-size edits inside one version slip past the size-match heal; any real change bumps the badge per CLAUDE.md, which wipes the snapshot.
- **Filter the `__player__` sentinel EVERYWHERE bundles are listed.** The snapshot rides `BundleDownloader` like a bundle, so it appears in `list()`, fires `bundleProgress/Complete/Error`, and would be "resurrected" by anything reading the index. Current filters: the three event listeners + hydrate in `_appInitOfflineBundles`, `_appRenderDashboard`'s grouped list, `lpPlay`'s offline playlist expansion, and downloads.html's list. A new bundle-listing surface must skip it too.
- **downloads.html is feature-frozen.** It exists only for "no snapshot yet". Player features go in static/index.html, where offline mode inherits them — that asymmetric drift is the whole reason the cached player exists.

## Settings

### Two layers of settings

1. **`.env`** (loaded by `pydantic-settings`) — service URLs, credentials, buffer thresholds, admin password
2. **`library.json` → `settings`** — UI-managed library paths, admin overrides (`indexer_categories`, `tmdb_api_key`)

`/api/search` reads `indexer_categories` from the admin override first, falling back to `.env`. Library paths are unioned across both. `_tmdb_effective_key()` follows the same admin-beats-env precedence.

## TMDb metadata

### The metadata endpoint must never block on TMDb (episode pages on dead internet)

`GET /api/library/{id}/metadata` used to run the whole first-access TMDb chain inline (search + details + one request per season, each with a 15 s timeout) — with the internet down, opening a never-fetched item hung the episode page for the full timeout chain. Now the first-access fetch runs as a background task (`_spawn_metadata_fetch`) the endpoint waits on for at most 4 s before answering `pending:true`; a `metadata_update` SSE event fires when the background fetch lands and the open episode page re-pulls. The frontend independently never gates the episode list on `/metadata` (`openEpisodePicker` renders on `/files` alone; `_epApplyMetadata` fills metadata in). **If you add another endpoint that may trigger a TMDb fetch, don't await it inline in a request that also carries local data** — split it the same way. `_tmdb_get` uses a 5 s *connect* timeout so unreachable links fail fast; keep that split (read stays 15 s for slow-but-working networks).

Fire-and-forget tasks here (`_prefetch_metadata_images`, the SSE notify) go through `_tmdb_bg()`, which holds a strong reference until completion — a bare `asyncio.create_task()` result can be **garbage-collected mid-flight** and the task silently vanishes.

### Artwork is proxied through the host — img_base is /api/metadata/img, not image.tmdb.org

All metadata endpoints hand out `img_base = "/api/metadata/img"`; the route serves a permanent on-disk cache (`.tmdb_img_cache/{size}/{filename}`) and fetches from TMDb only on a miss, so posters/backdrops/stills work for every LAN client (including ones that never saw the show) with the internet down. Size + filename are strictly whitelisted (`_TMDB_IMG_SIZES`, `_TMDB_IMG_FILE_RE`) — the proxy can only ever hit `image.tmdb.org`, so this is NOT the SSRF surface the custom-art rule below worries about; keep user-supplied absolute `poster_url`/`backdrop_url` **unproxied**. One exception keeps the absolute `TMDB_IMG_BASE`: the iOS bundle's `meta.img_base` (`_bundle_meta_for_file`) — the offline player has no host to resolve a relative path against (it uses the inlined `poster_data_url` anyway, which now also reads through the disk cache).

### Auto-match grabs the most-popular result

`_tmdb_match_show` ([main.py](../main.py)) calls `/search/tv` (or `/search/movie` for single-file no-season items **whose name carries no season/episode marker** — see the movie-misbinding rule above) and takes the **first** result. TMDb's search ranks by popularity, so for ambiguous titles ("Monster", "The Office", "It") the match may be the wrong show. Recovery paths: an admin POSTs `/api/library/{id}/metadata/refresh` with `{tmdb_id: <correct>, kind: "tv"|"movie"}`, OR **any user** uses the episode-page "Fix Metadata" control (`/metadata/search` → `/metadata/set`) to pick the right entry or hand-enter custom fields. The result is cached on `item["metadata"]`.

### A show can be matched as a MOVIE — and that silently kills the missing-seasons diff (12.6.1)

`_tmdb_match_show` decides movie-vs-TV with `is_movieish = len(files) <= 1 and not item.get("season")`. That file list comes from qBit, and **an item is matched the moment it's added — usually before the torrent's file list has resolved at all.** A season pack with zero files therefore looks exactly like a one-shot movie, goes to `/search/movie`, and gets a movie binding cached forever.

This is not hypothetical, and it is worse than a wrong *id*: TMDb files several pilots as standalone films, so the movie search **succeeds**. `Futurama-1999-S01` bound to `Futurama: Welcome to the World of Tomorrow` (a real 1999 movie entry) and `South.Park.S28` to `South Park: Bigger, Longer & Uncut`.

The damage is entirely downstream. Every missing-content path is gated on `tmdb_kind == "tv"` — `library_coverage` skips the `missing_seasons` block, `_epMissingBlockedReason()` returns `"nometa"`, `all_seasons` is never even fetched. **The show reads as complete.** Futurama showed season 1 and nothing else, with no "Season 2 available" chip and no hint anything was absent, which is the failure mode you cannot spot by looking — the UI is identical to a genuinely complete show.

Three guards now, and understand which does what before touching any of them:

- `_title_says_series(item)` — a name carrying `S01E02` / `1x02` / `S01` / `Season 1` / `2nd Season` overrules the empty-file-list guess. This is the only signal available *before* files resolve.
- `_movie_binding_is_stale(item, cached)` — after the fact: an **auto** (`source == "tmdb"`) movie binding on an item whose files hold ≥2 distinct numbered episode slots is re-matched from scratch. Bucketed files (Specials/Extras) never count, and a **`manual`/`custom` pick is never re-opened** (see the pinning rule below — a deliberate "this is a movie" must stand).
- `kind_recheck` — stamped when the re-match *still* returns a movie. Without it, a name TMDb genuinely has no TV show for would re-query TMDb on every single page open, forever.

Repair fires from `_nudge_metadata_health` (`/files`, `/metadata`, the series endpoint) **and from `/api/library/coverage`**, capped at `COVERAGE_NUDGE_PER_CALL`. The coverage hook is the one that matters: a show that looks complete is precisely the show nobody opens, so the three per-item endpoints would never run for it.

### Manual/custom metadata is PINNED — auto-refetch and rename must not clobber it

The metadata cache carries a `source`: `"tmdb"` (auto), `"manual"` (user picked a TMDb entry), `"custom"` (hand-entered). A user's deliberate choice must survive the two things that otherwise re-resolve metadata: (1) `_fetch_item_metadata` returns `manual`/`custom` unchanged on any **non-forced** call — critical for `custom`, which has **no `tmdb_id`** and would otherwise fail the old `cached.get("tmdb_id")` guard and get auto-re-matched; (2) `rename` skips `pop("metadata")` for pinned entries and does **not** force-refetch them (it hands the existing metadata straight back). If you add another code path that drops or force-refetches `item["metadata"]`, check `source` first or you'll silently wipe a user's fix. Custom entries use absolute `poster_url`/`backdrop_url` (not TMDb `poster_path`) and empty `seasons` — `renderEpHero` prefers the absolute URLs, and episode titles/stills fall back to filename parsing.

### Custom poster/backdrop URLs must be https (mixed-content block)

Custom metadata art is whatever URL the user pastes, loaded directly by the `<video>` hero's `<img>`/CSS `background-image`. The dashboard is served over **HTTPS** (and the iOS app runs under `capacitor://`), so an `http://` image URL is blocked as insecure mixed content and silently shows the placeholder. The Custom tab warns the user to use `https`. Don't proxy these through the host (that would re-introduce an SSRF surface for no real benefit) — just document the https requirement.

### Season tabs are the UNION of disk and TMDb — and the diff fails closed (11.18.0)

Before 11.18.0 `epSeasonList` was built purely from `parse_season_episode` on the file paths: if TMDb said season 4 existed but only seasons 1–3 were on disk, season 4 never appeared. It now unions the on-disk seasons with the show's TMDb seasons (`_epRebuildSeasonList`), and `renderEpList` renders the union of files and the season's missing TMDb episodes — that is the whole "show missing content" feature.

The thing to be careful about is **when not to do it**. A diff against TMDb is only as good as the per-file season/episode attribution, and that attribution fails silently and completely on releases that do not use `SxxExx` (see GitHub #15 — every one of Attack on Titan's 131 files parses as `season: 0, episode: 0`). Diffing in that state reports *every episode of every season* as missing, which is far worse than showing nothing. So `_epMissingBlockedReason()` gates the entire feature and returns a reason instead of silently doing nothing:

- **`"unparsed"`** — more than 25% of the item's files (`EP_MISSING_MAX_UNATTRIBUTED`) have no positive season AND episode. Those are real episodes hiding outside the season model, so every season would read as short. The episode list shows an explicit note; do not "fix" this by lowering the threshold — fix the parser (#15) instead.
- **`"absolute"`** — TMDb files the show as a single 1000-episode "Season 1" (long-running anime). Its season numbers do not map onto the scene's, so a diff invents hundreds of phantom gaps. Detected by `_tmdbAbsoluteNumbered`, shared with the search picker's `_ssTmdbAbsolute`.
- **`"nometa"`** — no TMDb match, or not a TV entry. **Fail closed**: no metadata means no missing rows, never invented gaps.
- **`"off"`** — the admin turned it off (`settings.missing_content.enabled`).

Two more deliberate exclusions: **season 0 never participates** (specials/OVAs/extras are a bottomless pit — almost every anime show would read as perpetually short), and **unaired episodes are dropped by default** (`_tmdbEpUnaired`: future or absent `air_date`), because otherwise a currently-airing show looks permanently incomplete. The admin can opt them into a distinct "Upcoming" state instead, but they are never presented as something to download.

### `metadata.seasons` is NOT the show's season list — `all_seasons` is

`_fetch_item_metadata` asks TMDb only for the seasons **present on disk**, so `metadata.seasons` is "seasons whose episode lists we happened to fetch", not "seasons the show has". Reading a show's season count off `Object.keys(metadata.seasons)` therefore under-reports exactly when it matters most — a show you own one season of looks like a one-season show. `_tmdb_fetch_tv` now also stores **`all_seasons`** (the full inventory, taken free from the `/tv/{id}` response it already makes, with `episode_count` per season), and `_tmdbSeasonNumbers()` prefers it. Use `all_seasons` for "does this season exist", `seasons[N].episodes` for "what are its episodes".

Caches written before 11.18.0 have no `all_seasons`, so `_fetch_item_metadata` treats a TV entry missing that key as stale and re-fetches it once — **by its existing `tmdb_id`**, never a fresh auto-match (a re-match could silently re-bind the item to a different show), preserving `source` so pinned `manual`/`custom` entries stay pinned. A merged series never hits `GET /api/library/{id}/metadata`, so `get_series_files` fires the same self-heal in the background; until it lands the frontend tops up from `/api/tmdb/lookup`, which fetches every season. The old merged-series top-up condition compared the cached season count against the **parsed** season count — useless here, since the parsed count is the unreliable number and a season owned nothing of has no files to count; `_epTopUpSeasons` measures against the inventory instead.

### Episode stills are joined by (season, episode) pair

`_tmdbEpisode(file)` matches the file's `(season, episode)` against `metadata.seasons[N].episodes[*]`. If the filenames are mis-labelled — e.g. an anime cour where the on-disk numbering restarts each cour but TMDb uses one continuous season — the still and overview will be wrong even though the show match is right. The TMDb episode overview is still better than nothing; the user can always rename files or override the match. Don't add complex episode-offset heuristics without a clear failure case.

## Python compatibility

`setup.py` and `run.py` are run by **system Python** (any version 3.9+). They use `from __future__ import annotations` so they parse on 3.9. `main.py`, `analyzer.py`, `watchdog.py`, `daemon.py` run inside the venv (also 3.9+ baseline but the project doesn't pin newer syntax).

### Windows: Microsoft Store Python / per-user Python breaks multi-user use

A Windows venv's `.venv\Scripts\python.exe` is a tiny launcher that re-executes the **base** Python recorded in `pyvenv.cfg`. If the base Python was installed per-user (e.g. Microsoft Store Python at `C:\Users\<name>\AppData\Local\Microsoft\WindowsApps\PythonSoftwareFoundation.Python.3.x_...\python.exe`), that path is only readable by `<name>`. Any other user — including the scheduled task running as a different account — gets `Access is denied` and the wrapper silently fails (no log written because the wrapper process never starts).

Symptoms:
- `python run.py` from a different user fails with `did not find executable at 'C:\Users\<other>\AppData\Local\Microsoft\WindowsApps\...python.exe': Access is denied.`
- `run.py --install` succeeds but the service never runs and `logs\streamlink_service.log` stays empty.

Fix: install Python from python.org with "Install Python for all users" checked (lands in `C:\Program Files\Python3xx\` — world-readable), uninstall the Microsoft Store Python, turn off the `python.exe`/`python3.exe` app-execution aliases (Settings → Apps → Advanced app settings → App execution aliases), `Remove-Item -Recurse -Force .venv`, then `py -3 -m venv .venv` and `python setup.py` again.

### Windows: don't use `/RL HIGHEST` on the scheduled task

`daemon.py` deliberately omits `/RL HIGHEST` from the `schtasks /Create` call. On Windows, ports below 1024 do not require admin to bind (the "privileged ports" concept is Unix-only), so the wrapper doesn't actually need elevation to serve port 80/443. Adding HIGHEST would force Task Scheduler to try to elevate the user's token at trigger time — which fails silently for Standard Users (they have no admin to elevate to), leaving the task registered but never running. Firewall rules (which DO need admin) are added once during `_windows_install` while the install process holds the admin token from UAC.

### Windows: `powercfg /set*valueindex` needs elevation — verify via the registry, elevate via a one-shot credentialed task

Three traps around the power/sleep-button neuter (`run.apply_windows_power_settings`, 10.9.0):

1. **`powercfg /setacvalueindex` (and `/setdcvalueindex`) fail with Access Denied from a non-elevated process** — and the installed service task deliberately runs un-elevated (see the `/RL HIGHEST` gotcha above). So the tweak is applied where a token exists: the elevated `run.py --install` path applies it directly, and the runtime paths fall back to a **one-shot Scheduled Task** registered with `WINDOWS_ADMIN_USER`/`WINDOWS_ADMIN_PASSWORD` (`schtasks /Create /RU <user> /RP <pass> /RL HIGHEST` → `/Run` → `/Delete`). A batch logon at HIGHEST run level gets the full (non-UAC-filtered) admin token with no prompt — `runas` can't do this (no password argument), and PowerShell `Start-Process -Credential` gets the *filtered* token.
2. **Never parse `powercfg /query` output to check state** — the text is localized ("Current AC Power Setting Index" only exists on English Windows). `windows_power_buttons_disabled()` reads `HKLM\SYSTEM\CurrentControlSet\Control\Power\User\PowerSchemes\<ActivePowerScheme>\<sub>\<setting>` (`ACSettingIndex`/`DCSettingIndex`) instead — locale-independent, readable without elevation. An absent value key means "no override recorded" = still on the Windows default (sleep), **not** 0.
3. The `schtasks /Create` line briefly exposes the password in the process list — accepted trade-off on a single-user media box; the credentials themselves live only in the host's `.env`.

### Windows: scheduled task `/RU` must be the console user, not `USERNAME`

When `_windows_install` runs after a UAC bounce (or from any "Run as Administrator" shell), `os.environ['USERNAME']` is the admin account that accepted the prompt, not the regular user logged in at the keyboard. Registering with `/RU <admin>` ties the task to the admin's logon trigger, so the task never fires for the actual user. `_windows_console_user()` queries `WTSGetActiveConsoleSessionId` + `WTSQuerySessionInformationW` to find the real interactive user (PowerShell `Win32_ComputerSystem.UserName` fallback). The install output prints the detected `RunAs` so the user can verify.

### HTTPS port (443) is a reverse proxy, not a second FastAPI instance

Port 443 serves `https_proxy:app`, a tiny FastAPI app that streams every request to `127.0.0.1:80` and the response back. Port 80 serves the real `main:app`. **Do not** revert to mounting `main:app` on both ports "for performance" — even though they live in the same Python process and share module globals in theory, in practice that arrangement produced intermittent state divergence between clients on `https://remote.local` and `http://<lan-ip>` (different SSE buffers, different startup race timings, different event-loop scheduling between the two `uvicorn.Server` instances). With the proxy in place there is provably one `AppState` in the process. Implications: (a) the proxy must forward request bodies as a stream (`request.stream()`) so large uploads aren't buffered into memory, and the response with `aiter_raw()` so SSE messages reach the browser instantly; (b) `admin_https_redirect` in `main.py` MUST honor `X-Forwarded-Proto` / `X-Forwarded-Host`, otherwise every admin hit through the proxy redirects to `https://127.0.0.1/admin` and loops; (c) if you ever add a WebSocket route to `main:app`, `https_proxy.py` needs WebSocket handling — it currently only proxies HTTP methods. See [https_proxy.py](../https_proxy.py).

### Windows: service wrapper must `os.chdir(HERE)` before importing `main:app`

Task Scheduler launches the wrapper with **CWD = `C:\Windows\System32`** — there is no `schtasks` flag that sets a working directory the way launchd's `WorkingDirectory` plist key or systemd's `WorkingDirectory=` does. `main.py` mounts `app.mount("/static", StaticFiles(directory="static"))` with a *relative* path, so `StaticFiles.__init__` immediately raises `RuntimeError: Directory 'static' does not exist`. Symptom: the service starts, logs `Server 0/1 exited with exception: RuntimeError: Directory 'static' does not exist` ~5× in a second, hits the fast-death circuit breaker, and stops. `streamlink_service.py` (and the `_WRAPPER_CONTENT` template in `daemon.py`) now does `os.chdir(HERE)` right after defining `HERE`, before any of the `from run import ...` calls or `_launch_servers()` runs uvicorn. Don't move it later — uvicorn imports `main` at `serve()` time, and `main` resolves `static/`, `cert.pem`, `library.json` etc. relative to CWD. macOS/Linux were unaffected because both unit files set `WorkingDirectory={HERE}`.

## Admin logs

### A pipe-streamed ZIP can't be extracted by Windows Explorer

The admin "Download All (.zip)" log bundle (`admin_download_logs_bundle`) used to build the ZIP in a writer thread feeding an `os.pipe()` and stream the read end to the client. A pipe is **non-seekable**, so `zipfile` can't go back and patch each local file header after it knows the CRC/compressed size — it sets **general-purpose bit 3** and emits a *data descriptor* after each member, leaving the local header's CRC and sizes as zero. macOS Archive Utility and 7-Zip read the **central directory** (which has the real values) so they extract fine — which is exactly why the bug was invisible on the dev Mac. **Windows Explorer's built-in extractor trusts the local headers**, sees zero sizes, and refuses the archive as "invalid." Since Windows is the primary target, build the ZIP into a **seekable temp file** instead (then serve it with `FileResponse` + a `BackgroundTask` to unlink it); `zipfile` back-patches real local headers and Windows extracts it. The same trap applies to any future "stream a ZIP through a pipe/socket" idea — verify on Windows, not just macOS.

### Live logs are served as snapshot copies, not the live file

`streamlink_service.log` is written by the **service-wrapper process** (`streamlink_service.py`), a different process from the uvicorn worker that serves `/api/admin/logs/{name}`, and it grows continuously. Serving the live file (the old `FileResponse(path)`) was unreliable for it — it "never downloaded" while the other logs (written by the worker itself) were fine. Both the per-file endpoint and the bundle now read each log into a **temp snapshot** first (per-file: `shutil.copyfileobj`; bundle: `open(p,"rb").read()` then `zf.writestr`), so an actively-appended log yields a stable, complete download. Don't revert to streaming the live file.

### Post-update log archival must happen at startup, not inside the update flow

When the updater applies a new version it would be natural to zip + clear the logs right there in `_run_apply`. **Don't** — the live uvicorn process holds `streamlink_app.log` / `hls.log` open via their `RotatingFileHandler`s, and on Windows you can't archive-then-clear (let alone `unlink`) a file the running process has open for writing. Instead `_run_apply` only drops a `logs/.rotate_pending` marker after a successful `setup.py`, and `_init_logging()` consumes it on the **next process start** — *before it adds any handler*, i.e. while no file is open yet — calling `_archive_old_logs()` to zip everything into `logs_old_<timestamp>.zip` and clear the originals. Keep the archive **best-effort per file**: reading an open file into the zip is safe on every OS, but clearing it isn't — a file another live process holds open (e.g. `streamlink_service.log` on Windows) is left in place rather than aborting the whole rotation. The marker is a dotfile and the `logs_old_*.zip` archives are name-excluded, so neither gets swept into the next archive. The marker also covers the `reboot=false` dev path: the rotation just waits until that process actually restarts.

## Scheduled reboot

### Scheduled-reboot loop guard

The single most dangerous bug here is a **reboot loop**: the machine reboots at the scheduled time, comes back up (auto-login + service) still past that time, re-arms, sees itself idle, and reboots again every couple of minutes. `scheduled_reboot_loop` prevents this by persisting `settings.scheduled_reboot.last_fired = <tz date>` to `library.json` **before** calling `_reboot_machine()`. On the way back up the loop reads `last_fired == today` and stands down until tomorrow. If you ever refactor this, keep the write-then-reboot order and make sure the `put_library` completes (it's `await`ed) before the reboot fires. Saving config from the admin UI resets `last_fired` to `""` so a newly-set time can still arm the same day.

There is intentionally **no upper time window** on arming: if the host was powered off at the scheduled time and only came up hours later, it still gets one daily reboot when next idle. The `last_fired` guard caps that at one per tz-day, so the worst case is a single "catch-up" reboot, not a loop.

### Reboot needs host permission; "in use" must include the TV

`_reboot_machine()` tries a platform chain (macOS System Events restart works from a launchd *user agent* without sudo; Linux/Windows may need passwordless `sudo`/elevation). If none succeed it logs a hint rather than throwing — a failed reboot must not crash the loop. Separately, `_machine_in_use()` must check **live VLC state**, not just `state.last_activity`: someone watching on the TV makes no HTTP requests for the whole episode, so an activity-timestamp-only check would call the box idle and reboot mid-movie. Active streams and downloads count as in-use too, so a nightly reboot never interrupts a download. **In-progress stream prep also defers the reboot** (a separate `_prep_in_progress()` check in `scheduled_reboot_loop`, *not* folded into `_machine_in_use` — see below): idle HLS-prep / STT jobs run precisely when the box looks idle (the reboot's target window), and HLS prep can't checkpoint, so rebooting mid-encode discards the work and it restarts from scratch on the next idle stretch. Deliberately kept out of `_machine_in_use` because `auto_prep_loop`'s idle mode computes `want = not _machine_in_use(...)` — if prep counted as "in use" there, the first prep job would flip the box "busy" and stop idle auto-prep from wanting any more (a self-defeating feedback loop). The reboot loop ORs the two checks instead.

## Networking / mDNS

### Preferred adapter selects an IP — it does NOT bind uvicorn to one NIC

The admin's "primary network adapter" pick (System → Network Adapter) must **not**
narrow the uvicorn bind from `0.0.0.0`. Binding to a single adapter's IP would
(a) make every other adapter dead — but the requirement is that they still serve
*something* (a redirect), and (b) risk an unreachable server when that IP isn't
up yet at boot — the exact fragility we already fight for mDNS. Instead the bind
stays `0.0.0.0` and the `network_adapter_redirect` middleware ([main.py](../main.py))
307-redirects a client that connected on a *non-preferred* adapter's LAN IP to
the preferred adapter's current IP. The preferred adapter is stored by interface
**name**, not IP (IPs move with DHCP), and resolved to a live IP at request time
via [`netadapters.py`](../netadapters.py). When the preferred adapter is offline
the resolver falls back to the route-table heuristic and logs once — never hard-
fail. Only bare-IPv4 hosts are redirected; `remote.local` and `localhost` /
`127.0.0.1` (the loopback the 443 proxy rides) pass through. The middleware
honours `X-Forwarded-Host`/`-Proto` so a request arriving via the port-443
reverse proxy redirects on the real client host + scheme, not `127.0.0.1`.

### `remote.local` doesn't resolve after a reboot

mDNS registration must be **resilient, not one-shot**. The installed service (launchd/systemd) starts at login/boot **before Wi-Fi has associated and the interface has a LAN IP**. A single `start_mdns(get_local_ip(), …)` at startup sees `get_local_ip() == ""` and silently skips registration — so `remote.local` never resolves, even though uvicorn binds `0.0.0.0` and becomes reachable **by IP** the moment the network comes up. Classic symptom: "remote.local works right after `run.py --install` (network was already up) but not after a reboot; the IP still works." Both `run.py` and the service wrapper use `start_mdns_resilient()`, which registers from a daemon thread that waits for the IP and re-registers if it changes. Don't revert either call to the bare one-shot `start_mdns()`. After changing the wrapper, re-run `python3 run.py --install` to regenerate `streamlink_service.py`. See [RUNTIME.md](RUNTIME.md#mdns-runpy734).

### Windows: a dropped client must not kill the listening socket (Proactor accept-loop bug)

On Windows uvicorn runs on the asyncio **ProactorEventLoop** (uvloop is Linux/macOS-only, and we *need* Proactor for `asyncio.create_subprocess_exec` — ffmpeg, whisper, git — so switching to the SelectorEventLoop is not an option). The Proactor loop has a long-standing CPython bug: in `BaseProactorEventLoop._start_serving`, when a *per-connection* accept future fails — `f.result()` raising `OSError`, most often `[WinError 64] The specified network name is no longer available` (also 121/1236, ECONNRESET/ECONNABORTED) because a client vanished between `AcceptEx` completing and asyncio reading it — control falls into the `except OSError` branch that calls `sock.close()` on the **listening** socket. The server then accepts **no** new connections; every client hangs and only a process restart / reboot recovers it. Field symptom: `Accept failed on a socket` on `laddr=('0.0.0.0', 80)` followed by total unresponsiveness. The fix is [`winaccept_patch.py`](../winaccept_patch.py), applied at import in [main.py](../main.py) (so both `run.py` and the `daemon.py` service get it): it reinstalls `_start_serving` with an accept loop that catches the per-connection `f.result()` error separately, logs it as recovered, and **re-arms a fresh accept** — keeping the listener alive — while still closing the socket on a genuine listening-socket failure (arming a new accept raising), exactly as upstream does. No-op off Windows. The patch's `_start_serving` signature mirrors CPython's and absorbs version-specific SSL kwargs (e.g. 3.13's `ssl_shutdown_timeout`) via `**ssl_kw`; if CPython ever restructures that method, the patch is wrapped so a failure to apply logs and falls back to stock asyncio rather than blocking startup. You may still see a benign `Task exception was never retrieved` from asyncio's internal `accept_coro` for the same dropped connection — harmless; the listener stays up.

### Windows: two instances fighting for :80/:443 (single-instance guard)

Windows can't share a listening socket (no usable `SO_REUSEADDR` for this — it enables hijacking, not sharing), so if a **second** StreamLink launch happens while one is already serving, the newcomer's bind fails with `[Errno 10048] ... only one usage of each socket address ... permitted` (`WSAEADDRINUSE`). The daemon wrapper's supervisor then crash-loops (5 fast deaths → gives up → Task Scheduler relaunches the wrapper → repeat), and the whole service looks dead until a reboot leaves exactly one survivor. The diagnostic tell in `streamlink_service.log` is a *healthy* instance's qBit/VLC polling (`200 OK`) interleaved with a second process's 10048 / `HTTP server (port 80) failed to start` at the same timestamps — both write the same log file. A second launch happens innocently: the `/SC ONLOGON` task firing on a fresh logon while the prior session's process still lives, a manual `python run.py` alongside the installed service, or a double `schtasks /Run`. Guard: `run.dashboard_already_serving(port)` ([run.py](../run.py)) — it confirms a live instance via the public `/api/version` (so a *foreign* occupant of :80 is told apart from our own app) and the newcomer **yields cleanly**. It's wired into `run.py` (won't start a duplicate) and the `daemon.py` wrapper template at the **top of every supervise iteration**, which also settles the two-wrappers-started-together race (the loser sees the winner next pass and exits before `_MAX_FAST_DEATHS`). Don't move the check after the bind, and don't "fix" 10048 by setting `SO_REUSEADDR` on Windows — that makes *both* instances appear to bind and serves requests from whichever wins each accept, which is worse. A legitimate self-restart (our own server died, port now free) passes straight through. **After changing the wrapper template, re-run `python run.py --install`** to regenerate `streamlink_service.py`.

## YouTube on TV

### VLC 3.0's bundled `youtube.lua` is broken — don't route YouTube through VLC

Feeding a YouTube watch URL to VLC's `in_play` looks like it works for ~8 s then dies: `status.json` reports `state: playing` but `length: -1` and the now-playing stays the raw `watch?v=…` filename (a successful resolve would set the real title), then it stops. That's the bundled `youtube.luac` failing to extract the stream — it breaks every time YouTube changes its page, and the shipped script always lags. yt-dlp-into-VLC is more reliable but adds a fragile Python dep and caps at 720p muxed without an `:input-slave` audio hack. So YouTube-on-TV plays in a **browser** (Chrome kiosk + IFrame API), not VLC. Don't "simplify" it back to `vlc("in_play", input=<youtube url>)`. See [YOUTUBE.md](YOUTUBE.md).

### The kiosk needs `--autoplay-policy=no-user-gesture-required`

The TV has no mouse/keyboard, so the IFrame player must autoplay **with sound** on a fresh page load. Chrome blocks that by default. `_launch_tv_browser` passes `--autoplay-policy=no-user-gesture-required`; without it the kiosk loads but sits paused/muted with no way to start it. (The page also calls `playVideo()` in `onReady` as a belt-and-braces.)

### YouTube reuses the VLC display fields — so the VLC pollers must be gated

To render YouTube in the existing footer/fullscreen scrubber with zero UI branching, `/api/youtube/tv-state` writes the player's position/duration/volume/title onto the **same** `state.vlc_time` / `vlc_duration` / `vlc_volume` / `active_title` fields VLC uses. That means anything that polls VLC and writes those fields will clobber the YouTube values while the kiosk is up. Two loops are gated on `state.youtube_active`: `stat_broadcaster` skips its VLC `status.json` read, and `background_video_loop` skips entirely (otherwise it sees VLC stopped and starts the idle background video *over* the YouTube kiosk). If you add another VLC poller, gate it the same way. (`vlc_progress_tracker` is already safe — it no-ops without a `library_item_id`.)

### "Background video stops → VLC loading anim → background restarts" means the kiosk launch FAILED

This exact symptom (most-reported on Windows) is not a playback bug — it's the **launch-failure signature**. `POST /api/youtube` sets `youtube_active=True` and calls `vlc("pl_stop")` (background stops) *before* launching the browser. If `_launch_tv_browser` returns False, the endpoint's 500 path resets `youtube_active=False`; ~3 s later `background_video_loop` sees VLC stopped and ungated, so it reloads the background video (the "loading anim" is VLC reopening it). So whenever you see the background bounce, the browser never launched — check `logs/streamlink_app.log` for the `_find_chrome` / `_launch_tv_browser` warnings.

### Windows browser discovery must include the registry + `%LOCALAPPDATA%`

The v3.5.0 bug: `_find_chrome` only checked three hard-coded `Program Files` paths, so a **per-user Chrome install under `%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe`** (the default when Chrome is installed without admin) was invisible → None → the launch-failure signature above. Don't trim discovery back to a couple of absolute paths. Windows discovery now goes: `_CHROME_BIN` → **`App Paths` registry (HKCU + HKLM** for chrome/msedge/brave/chromium, via `winreg`) → `%ProgramFiles%` / `%ProgramFiles(x86)%` / **`%LOCALAPPDATA%`** filesystem candidates → PATH. Edge ships on Win10/11 and registers `App Paths`, so this should always resolve. `_windows_chrome_from_registry` imports `winreg` lazily inside a try so the module still imports on macOS/Linux.

### `Popen` success ≠ kiosk visible — the heartbeat health-check is the real signal

`subprocess.Popen` returning only means the process spawned. On Windows a **session-0 service has no interactive desktop**, so the browser (and VLC) launch invisibly; a locked `--user-data-dir` or an instant exit also "launch" but render nothing. `_youtube_kiosk_healthcheck` waits 12 s and, if the `/tv` page never heartbeats (`youtube_tv_seen_at` didn't advance past launch time), reports a `stream_status:error`. The `/tv` page POSTs `tv-state` every second starting the moment it loads (even before the IFrame player is ready — the body carries just `video_id`), so a real launch checks in within ~1 s; 12 s of silence reliably means the page never opened. Don't shorten the window much — the IFrame API script loads from youtube.com, which can be slow on a cold cache.

### The kiosk URL must be `http://127.0.0.1/…`, never `http://localhost/…`

Windows' hosts file resolves `localhost` to **both** `::1` (IPv6) and `127.0.0.1` (IPv4), and Chromium tries IPv6 first. uvicorn binds `0.0.0.0:80` (IPv4 only), so the kiosk hits ECONNREFUSED on `::1` and either shows a "this site can't be reached" page or stalls long enough that `_youtube_kiosk_healthcheck` fires (logs full of *"launched but never reported in within 12 s"*) before the page ever loads. Pinning v4 in `_launch_tv_browser`'s URL sidesteps it entirely. Don't switch this back to `localhost` for "consistency" — the v4 form is intentional and load-bearing on Windows.

### Edge's first-run / signin / welcome modals block a fresh kiosk

A first launch with a brand-new `--user-data-dir` triggers Edge's First Run Experience + signin / "make Edge your default" / "import from Chrome" modals that **cover the requested URL until dismissed by a human** — and the kiosk has no human. `--no-first-run` + `--no-default-browser-check` aren't enough on modern Edge: `_launch_tv_browser` also passes `--disable-fre`, `--disable-features=msImplicitSignin,SigninInterceptBubbleV2,DesktopPWAsRunOnOsLogin`, `--disable-default-apps`, `--disable-component-update`, and `--noerrdialogs`. If a future Edge / Chrome release surfaces a new welcome modal, add the corresponding `--disable-features=…` flag here; do **not** rely on muscle-memory dismissals on the TV.

### `CoUninitialize` must run *after* COM pointers are released — use an inner closure

The straightforward `try: CoInitialize() / do work / finally: CoUninitialize()` pattern is **wrong** in Python. The function's local COM pointers (`device_enum`, `speakers`, `interface`, `vol`) are still alive when `finally` runs — Python destroys the frame *after* finally — so `CoUninitialize` runs first, then the pointers' `__del__` calls `Release()` against a torn-down apartment and raises *"COM method call without VTable"*. Volume changes still work (the call already landed), but each one pollutes the log with three "Exception ignored in __del__" tracebacks. Fix: do all COM work inside an inner closure (`_do_com_work` in `_windows_volume_op`); when *it* returns, its frame is destroyed first, the pointers `Release()` while the apartment is alive, *then* the outer `finally` Uninits. Make sure `op(vol)` returns plain values (bool / int) and never the COM pointer itself, or a ref leaks back out and you're back where you started.

### pycaw `AudioUtilities.GetSpeakers()` is API-unstable — go through `CoCreateInstance` directly

Different pycaw releases return different objects from `AudioUtilities.GetSpeakers()`: older ones the raw `IMMDevice` COM pointer (has `.Activate()`), newer ones a Python `AudioDevice` wrapper that **doesn't**, producing `'AudioDevice' object has no attribute 'Activate'`. Pinning a pycaw version doesn't help long-term — the wrapper has been added, removed, and reshaped multiple times. Use only pycaw's **COM interface definitions** (`pycaw.api.endpointvolume.IAudioEndpointVolume`, `pycaw.api.mmdeviceapi.IMMDeviceEnumerator`), which have been stable, and obtain the device via `comtypes.CoCreateInstance(CLSID_MMDeviceEnumerator).GetDefaultAudioEndpoint(eRender, eMultimedia)`. `CLSID_MMDeviceEnumerator` / `EDataFlow` / `ERole` are imported from `pycaw.constants` when available, with a hard-coded GUID + numeric values as a fallback so a future rename in pycaw can't break this path.

### COM must be initialized on every thread that calls pycaw — `asyncio.to_thread` workers don't

pycaw (Windows OS-volume control) calls `CoCreateInstance` under the hood, which requires the calling thread to have called `CoInitialize` (or `CoInitializeEx`). `asyncio.to_thread` runs the function on Python's default `ThreadPoolExecutor` — those worker threads have **no COM init**, so the first call raises *"CoInitialize has not been called"*, and a generic `except Exception` swallows it as a warning. The dashboard slider then silently no-ops. `_windows_volume_op` wraps each pycaw call in `comtypes.CoInitialize()` / `CoUninitialize()` so it works regardless of which pool worker fires (and tolerates a thread the pool reuses across calls — Init/Uninit are ref-counted). If you add another pycaw / COM call site, route it through `_windows_volume_op` or duplicate the same wrapper — don't call pycaw directly from a `to_thread`.

### Don't fail volume silently — return 503 with a diagnostic

Volume helpers can fail for two distinct reasons: **pycaw not installed** (operator upgraded without `pip install -r requirements.txt`) or **a COM/audio API error** (session-0 service, locked endpoint, etc.). `_windows_volume_op` caches the last error in `_PYCAW_LAST_ERROR` and `_PYCAW_IMPORT_FAILED` flips True after the first ImportError so we log the "install pycaw" hint once instead of every second. `POST /api/youtube/control` returns the cached diagnostic in a 503 response, and the dashboard's `ytControl` shows a one-shot toast. Don't go back to a generic "failed" — the user can't act on that.

### YouTube volume is the OS system volume, not the IFrame `setVolume`

The IFrame Player API's `setVolume` only scales the audio the player emits *before* the OS mixer — the TV's actual loudness is whatever the host's system volume is set to. So `setVolume(50)` with system at 100 % is **still** room-loud, and the user complained "it plays at system max." Volume control during YouTube goes through `_set_system_volume_sync` (pycaw on Windows, `osascript` on macOS, `pactl`/`amixer` on Linux) and the IFrame is locked at 100 % unmuted in `tv.html`'s `onReady`. If you ever wire a "use IFrame volume too" path, also clamp the IFrame to 100 — running both knobs in series gives multiplicative behaviour the user can't reason about.

### Restore system volume only **after** the kiosk process is gone

`/api/stop` restores the OS volume to `settings.system_volume_default` (the "expected max"), but doing that immediately would twist the volume knob underneath a kiosk that's still playing for the half-second it takes Chrome to shut down — audibly weird. `_stop_cleanup` first kills the kiosk (`_kill_tv_browser`), then **polls for the process to actually exit** by matching `TV_CHROME_PROFILE` in cmdlines on a 4 s deadline, *then* calls `set_system_volume(target)`. Don't reorder. Similarly, `state.system_volume_before_yt` (the pre-YT snapshot, used as fallback when no default is configured) must be captured **before** the play takes over state, not after.

### The kiosk window won't take focus on Windows — force it forward + minimize VLC

Symptom: the kiosk browser launches (taskbar icon appears) but stays *behind* VLC; the idle background video keeps the screen and the user has to click the taskbar icon to surface the video. Cause: **focus-stealing prevention** — the server (uvicorn) isn't the foreground process, so the window it spawns is denied focus and Windows just flashes its taskbar button. Same problem the VLC path already solves. Fix (`_bring_tv_to_front`, spawned by `/api/youtube` on both launch and hot-swap): minimize VLC (`vlc_minimize`) **and** find the kiosk window by title (`_TV_WINDOW_MARKER`, == `static/tv.html` `<title>` — Chrome's multi-process model makes PID→HWND matching unreliable, so match on title) and force it forward with the `_vlc_focus_windows` cocktail, retrying ~10 s while the window is created. Don't drop the retry loop — the window doesn't exist for the first ~1–2 s after `Popen`.

### `vlc_focus_and_fullscreen` must yield to the YouTube kiosk

The background-video focus loop runs for ~24 s on a slowing cadence, re-asserting VLC focus and **minimizing every other window** (`_minimize_other_windows_windows`) each tick. If a YouTube play starts while that loop is still running, it would minimize the freshly-opened kiosk and yank focus back to VLC. The loop now returns immediately when `state.youtube_active` is set (checked at the top of each iteration, before the minimize-others pass). If you add another "take over the screen for VLC" loop, gate it on `youtube_active` the same way.

### The focus cocktail's synthetic ALT press is "input" to the pynput hook — filter `LLKHF_INJECTED` or the TV UI wakes itself

Symptom: the TV UI idles out, the background video comes up fullscreen for ~1.5 s, then the dashboard kiosk pops right back over it — every time, forever. Cause: `_vlc_focus_windows` (and `_focus_tv_browser_windows`) defeats Windows focus-stealing prevention with a **synthetic ALT keypress** (`keybd_event`). Low-level keyboard hooks (`WH_KEYBOARD_LL`, what pynput installs) see injected events too, so `remote_input.py`'s filter counted that ALT as generic key activity → `_tv_input_event("key")` → nothing playing + `tv_ui_active` just cleared → `_tv_ui_show("input")` — the idle hand-back undid itself as a direct side effect of its own `vlc_focus_and_fullscreen` call. Fix: `_win32_event_filter` skips the **activity callback** for injected keydowns (`data.flags & LLKHF_INJECTED (0x10)`). Do NOT widen that into skipping injected events entirely (10.7.3 and earlier did): remote input is *not* always non-injected — Windows' HID input service translates consumer-page usages (AC Home/Back, sleep, media) into VK keystrokes **via SendInput**, so on many remotes the handled action keys themselves arrive injected. Blanket-skipping them sent 🏠 Home through to the default browser and made ← Back / ⏻ (VK_SLEEP form) completely dead — action keys are therefore claimed/suppressed/dispatched regardless of the injected flag; only the generic wake-activity path ignores injected input. If you add any other synthetic input (SendInput, `keybd_event`, mouse_event) anywhere in the host-side window management, remember the global hook is listening to you too — and if you ever synthesize one of the *handled* keys, the hook will claim it.

### Kill the kiosk by its `--user-data-dir`, never by process name

`_kill_tv_browser` matches the dedicated `--user-data-dir=.tv_chrome_profile` path in each process's cmdline and kills only those. Don't switch to killing by image name ("Google Chrome") — that would nuke the user's normal browser windows. The isolated profile is what makes the kiosk individually addressable (and keeps it out of the user's real Chrome session); it's git-ignored.

### A play hot-swaps if `/tv` is already open — don't unconditionally relaunch

`POST /api/youtube` broadcasts `yt_command:load` **and** only launches Chrome when no `/tv` heartbeat (`state.youtube_tv_seen_at`) arrived in the last 6 s. If the page is already up, the broadcast swaps the video in place (smooth); relaunching every time would stack kiosk windows. The freshly-launched page also reads `?v=<id>` so it autoplays even if it missed the broadcast — the two mechanisms are intentionally redundant, keep both.

### Auto-updater ends with a full host reboot — needs auto-login + the service installed

`/api/admin/updater/apply` (and the `updater_loop` auto-apply path) finishes its sequence with `_reboot_machine()` — a full host restart. The new code runs on the way back up via the OS service supervisor (launchd / systemd / Task Scheduler). **Two prerequisites** the dashboard can't enforce on its own:
- **The system service must already be installed** (`python run.py --install`). The apply path *also* re-runs that registration itself (step 3, via `daemon.uninstall()` + `daemon.install()`) so the wrapper script matches the new code, but if no service ever existed the reboot ends with nothing to relaunch the dashboard. The admin UI surfaces an amber warning when `service_installed=false`.
- **The OS must be configured for auto-login.** User-level launchd / systemd-user / Task Scheduler entries don't run until the user is logged in; an unattended reboot ends at the login screen and StreamLink stays down. The README has per-OS steps; if your box isn't set up for auto-login, leave **Auto-apply** off and use **Apply Now** manually so you can be physically at the box to log in.

### setup.py crashes with `UnicodeEncodeError` when stdout is piped on Windows

On Windows + Python 3.13, a `subprocess.run([..., "setup.py"], stdout=PIPE)` opens the child's `sys.stdout` with the host's legacy ANSI code page (cp1252 in en-US), *not* UTF-8. setup.py's banner prints `┌─┐` (box-drawing chars) and the rest of the script prints `✓ ✗ → ⚠` — none of which exist in cp1252 — so the very first `print()` raises `UnicodeEncodeError: 'charmap' codec can't encode...` and the process exits rc=1 before doing anything. Symptom from the auto-updater: `setup.py exited rc=1` in the admin UI, even though `git pull` already succeeded. Fixed two ways: setup.py reconfigures stdout/stderr to UTF-8 at startup (idempotent), AND `updater.run_setup` passes `PYTHONIOENCODING=utf-8` in the subprocess env. Belt and braces — either is sufficient on its own, but the env var also fixes the bug on a setup.py copy that predates the in-script reconfigure (which matters during an update *from* a buggy version, since the old setup.py is what runs first).

### Auto-updater calls setup.py with `STREAMLINK_AUTOUPDATE=1`

That env var (set by `updater.run_setup`) puts setup.py into a more conservative mode: it reuses the existing `.env` without prompting, skips OS-app installs (winget/brew can't run from a service account anyway), treats `pip install` failures as warnings instead of hard errors, and **skips `offer_service_install()`** because the updater handles the supervisor wrapper refresh itself right after setup exits. If you add a new step to setup.py that would be wrong to run during an automated update — anything interactive, anything that touches the system OS, anything that prompts for new config — guard it with `if not AUTOUPDATE:`. The flag is also read at module-import time, so it's safe to use in module-level constants.

### `merge_tool_paths()` must NOT re-detect `_WHISPER_MODEL` — it's a user choice, not an auto-detected path

`merge_tool_paths()` refreshes the `_*_BIN` paths in `.env` on every reuse-`.env` run (which includes every auto-update). For real binaries that's right — there's one of each and re-detecting keeps the path current. **The whisper model is different: the admin can have several `ggml-*.bin` installed at once** (switching `base`→`medium` in the Components card downloads `ggml-medium.bin` alongside the existing `ggml-base.bin`). `detect_tools()` resolves `whisper_model` as `next(iter(whisper_model_candidates()))` — the *first* file `rglob` returns, usually `base` — so a blind refresh on auto-update silently reverts the admin's choice back to `base`. The model file itself survives the branch switch (gitignored under `tools/`), so the only thing lost is the `.env` pointer. Fix: `merge_tool_paths()` keeps the existing `_WHISPER_MODEL` when that file still exists on disk, only falling back to a detected candidate when the configured model is gone. If you ever add another "pick one of several installed variants" setting (e.g. a chosen ffmpeg build), it needs the same preserve-don't-redetect treatment. See [SETUP.md](SETUP.md) / [STT.md](STT.md).

### Auto-update refreshes the supervisor wrapper *in place* — never re-registers the OS service

`updater.refresh_service_wrapper()` rewrites `streamlink_service.py` from the freshly-pulled `daemon._WRAPPER_CONTENT`. **It deliberately does NOT call `daemon.install()`** — on Windows that requires admin and tries to UAC-elevate via `ShellExecute(..."runas"...)`, which can't display a prompt from a service-launched uvicorn (no interactive desktop), so the auto-update would either hang on a prompt that never appears or fail outright. The OS service registration (Task Scheduler task / launchd plist / systemd unit) points at the wrapper *by path*, and that path is stable across versions — so rewriting the file is enough to make the OS supervisor run the new wrapper after the reboot, no re-registration needed. If `daemon.py` itself changes its OS-service registration logic (new plist key, different schtasks args), that needs a manual `python run.py --install` from an elevated shell; routine updates don't touch that code path so this is rare. The admin UI shows "Wrapper already up to date" in the diagnostic panel when the rewrite is a no-op (identical content), which is the most common case.

### Switching to any allowed branch (forwards or backwards) goes through the same Apply Now path

`updater.switch_branch()` / `apply_update()` use `git switch -C <target> origin/<target>` + `git reset --hard origin/<target>` — same operation regardless of direction. `alpha → main` and `main → alpha` are symmetric. State files (`library.json`, `.env`, `.offline_cache/`, `.background/`) are gitignored and survive the switch. The Apply Now button uses the picker's current value (not the saved config), so a downgrade is one confirm-gated click; the confirm dialog calls out the direction explicitly. Don't try to "be clever" by detecting the downgrade and rewriting library.json — forward-only migrations are the contract.

### Branch picker is locked to main / beta / alpha — for a reason

`updater.ALLOWED_BRANCHES = ("main", "beta", "alpha")` is enforced at every entry point: config save, switch-branch, check, apply. Don't widen it without thinking — accepting an arbitrary branch from the admin UI would let one mis-click drag a production box onto a dev branch (or a feature branch that's been force-pushed to point at unreviewed code). If you genuinely need to ship from a fourth branch, *add it to the tuple in updater.py* rather than punching a hole in the validation.

### `_run_apply` mutates the running uvicorn — long-running state goes with it

When `restart=true` (the default), the apply path SIGTERMs uvicorn while the HTTP response is still in flight. The admin UI knows this and treats a closed SSE connection as "restart in progress", but **anything in-process that you expected to outlive the response is gone too** — in-flight ffmpeg prep jobs (their parent dies, ffmpeg becomes a zombie until reaped by `subprocess.Popen` finalisation), `_lib_lock`-held writes (writes are atomic JSON; in-flight ones get torn but `library.json` itself stays consistent because the file write is atomic per-call), the analyzer task. The intended use case is "admin clicks Apply when the box is otherwise idle", which is also what the loop's `_machine_in_use` gate enforces. If you grow the updater to do something during an active stream, you'll need a per-task save-and-resume protocol that doesn't exist today.

### `_reload_settings()` rebinds the module global — it works because every caller uses `settings.foo`

Pydantic Settings reads `.env` only on `__init__`, so changing `.env` at runtime needs a re-instantiation. `_reload_settings()` does `global settings; settings = Settings()`. This works because the entire codebase references the setting via the module-level binding (`settings.indexer_api_key`, etc.) rather than capturing the object into a local. **Don't bind it as a default argument or stash it in a closure** — those captures would freeze to the pre-reload instance. If you ever need to add `from main import settings as _s` into a hot path, switch it to `import main; main.settings.foo` so re-loading propagates.

### AI subtitles (STT): the whisper model MUST be multilingual, and translate is English-only

The auto-subtitle feature uses whisper.cpp. Two non-obvious constraints:
- **The bundled GGML model must be multilingual** (`ggml-base`, not `ggml-base.en`). Whisper's *translate* task — which we use to produce an English track for foreign audio — only works on multilingual models; an `.en` model silently can't translate. `setup.py` downloads the multilingual `ggml-base.bin` for this reason.
- **Whisper can only translate *to* English.** It transcribes the spoken language, and optionally translates that to English — there is no "translate to Spanish". So the admin's "preferred subtitle language" setting can *trigger* generation when no matching sub exists, but for a non-English target whose audio is in a different language, STT can only deliver the spoken-language transcription (+ an English translation). Don't add UI that implies arbitrary target-language synthesis.

Also: STT output is a **sidecar `<stem>.<lang>.ai.srt` next to the source**, not a bundle artifact — it's picked up by VLC (`addsubtitle`) and the HLS player (`_list_sidecar_subs`) through existing plumbing, and the `.ai` filename segment is how we detect "already generated" (idempotency) and label tracks "(AI)". STT jobs share the HLS-prep concurrency semaphore + pause gate and run at lowered OS priority — never run whisper at the server's inherited HIGH priority or it lags the UI. See [STT.md](STT.md).

### AI subtitles (STT): the `-dtw` preset must match the loaded model or whisper errors the run

For subtitle *timing* accuracy `_run_whisper` passes `-dtw <preset>` (Dynamic Time Warping token alignment — fixes lines lingering across long pauses) plus `-ml`/`-sow` (word-boundary cue splitting). The footgun is `-dtw`: its value names the model's **architecture** (e.g. `base`, `large.v3`), and whisper.cpp **fails the whole run** if the preset's alignment heads don't match the loaded model. So never pass `-dtw` blindly off the model filename — `stt._dtw_preset()` maps `model_name()` through `_DTW_PRESETS` and returns `""` (DTW disabled) for anything it can't map confidently. If you add a new model size/variant to the Components picker, add its preset to `_DTW_PRESETS` too, or it'll silently lose DTW precision (still works, just coarser timing). `-ml`/`-sow` are always safe to pass. DTW needs no extra download (heads are built into whisper.cpp) — unlike `--vad`, which would need a Silero model bundled. See [STT.md](STT.md) § Timing precision.

### Never walk the whole `.offline_cache` inline on the event loop

`_build_offline_cache_inventory` (admin → Offline Cache tab) sums every file in every HLS bundle via `_dir_size_bytes` (recursive `rglob` + `stat`) and `stat()`s every library file. Doing that **synchronously in the async handler** blocks the asyncio event loop for the whole walk — and since the ABR ladder (v3.3.0) tripled the segment count per bundle, a real-world cache makes that long enough to freeze the *entire* server (SSE, VLC polling, all requests) until it looks crashed and the service restarts. The symptom is the tab stuck on "Loading cache inventory…" forever (the request never returns; the frontend *does* handle 500/network errors, so a permanent "Loading…" means a blocked loop, not an error). Fixed in v4.0.1 by running the walk in `asyncio.to_thread` (`_offline_cache_inventory_sync`), snapshotting `_offline_jobs` first so the thread doesn't iterate the live dict. Rule: any admin/inventory path that touches the full cache or many files on disk must offload to a thread — same as `_run_offline_job` already does for `_dir_size_bytes` / `shutil.rmtree`.

## HID remote (media keys)

### Media keys must be *suppressed* on Windows, or every press double-fires

`remote_input.py` hooks the air-mouse remote's media keys globally. On Windows the same keypress is otherwise **also** delivered to the OS (volume keys change the system mixer natively) and to the focused app (the dashboard force-fullscreens VLC, and VLC's Qt UI acts on media keys) — so without suppression a volume press changes both OS *and* VLC volume, and play/pause toggles twice (netting out to nothing). The pynput listener therefore uses `win32_event_filter` + `listener.suppress_event()` to swallow **only** the handled VK codes (`_VK_ACTIONS`), and **only while `_remote_should_handle()` is true** (real playback active). Two traps baked into that filter:

- **Dispatch must happen inside the filter** — pynput never delivers a suppressed event to `on_press` (`suppress_event()` raises before `_convert` posts to the message loop). If you "clean it up" by moving the action into `on_press`, suppressed keys silently stop working on Windows.
- **`suppress_event()` raises** (`SystemHook.SuppressException`) — it must be the *last* statement in the filter path.

On Linux (X11) and macOS pynput has no selective suppression, so keys are observed only and the DE may double-handle volume — accepted per the Windows-first policy (documented in [REMOTE.md](REMOTE.md)). macOS additionally needs the Input Monitoring TCC grant. The hook also receives nothing in a session-0 Windows service (hooks are per-desktop); `start_listener` failing/receiving nothing is by design non-fatal. Finally: the filter runs on the low-level hook thread with a hard OS time budget — `_remote_should_handle()` and `_tv_input_event()` must stay plain attribute reads (no locks, no awaits) or Windows silently unhooks the listener.

### The 🏠 Home key (VK_BROWSER_HOME) must always be claimed, or it opens Edge

The remote's Home button sends `VK_BROWSER_HOME` (0xAC) — Windows' default handler for it is "launch the default browser", so an unclaimed press pops Edge over the TV. `_remote_should_handle("home")` therefore claims it whenever the TV UI is enabled **or** playback is active (i.e. effectively always with default settings), independent of the media keys' playback-only rule. The one deliberate exception is `window_mgmt_paused()` ("Use My Computer") — the user asked for their desktop back, so Home may open their browser again.

### The ⏻ Power button is NOT a keyboard key — it needs powercfg + Raw Input, not the hook

The remote's power button (on the reference remote, and on most air mice) emits a HID **System Control** usage (Generic Desktop page 0x01, usage-0x80 collection — "System Sleep"), which the HID class driver hands **straight to the Windows power manager**: it never enters the keyboard stack, so a low-level keyboard hook can neither see nor suppress it. Symptom of getting this wrong (v10.7.0 did — it mapped only `VK_SLEEP`): the box shows "Locking…" and sleeps/hibernates on the press, then wakes to the lock screen where autologin doesn't run (autologin is boot-only). The working interception (v10.7.1) is split:

- `_neuter_power_buttons()` (`main.py`, startup) sets the power plan's sleep-button **and power-button** actions to *Do nothing* via `powercfg` (`SBUTTONACTION` + `PBUTTONACTION`, AC + DC + `/setactive`). Both, learned the hard way: Windows maps System Control "System Sleep" (0x82) to the *sleep* button but "System Power Down" (0x81) — what the reference remote actually sends — to the *power* button, so neutering `SBUTTONACTION` alone (v10.7.1) left the box still locking + sleeping. This is a persistent machine-wide change (deliberate — a stray press must not sleep a media box even when StreamLink is down; side effect: a short press of the physical chassis power button now also does nothing — Start menu / admin panel shut down instead, 4 s hold still hard-cuts) and **requires elevation** — since 10.9.0 it delegates to `run.apply_windows_power_settings()` (registry-verified idempotent; also run by the elevated `--install` path, every `run.py` launch, and the service wrapper), which falls back to a one-shot elevated task using `WINDOWS_ADMIN_USER`/`WINDOWS_ADMIN_PASSWORD` from `.env` — see the powercfg gotcha under *Python compatibility*. Only when every path fails does a warning log the manual commands and the button keeps sleeping the box.
- `PowerButtonListener` (`remote_input.py`) observes the press via **Raw Input** (`RegisterRawInputDevices` on page 0x01 / usage 0x80, `RIDEV_INPUTSINK`, hidden window + message pump thread). Raw Input is observation-only — it cannot block the power manager, which is why the powercfg step must exist. Release reports (all-zero past the report ID) are skipped, or every press would toggle twice.

The keyboard hook still maps `VK_SLEEP` (0x5F) → `power` for remotes that emit the keyboard form; remotes emitting **both** forms are deduped by the 1 s cross-path debounce in `_remote_key_action`'s power branch (`_remote_power_ts` — neither path shares the `RemoteListener` per-action debounce). `_remote_should_handle("power")` claims unconditionally except during `window_mgmt_paused()`. One more trap: **the playback branch must not start the background video itself** — `stop()` schedules its VLC teardown (`pl_stop` + `pl_empty` + minimize) as a background task; a `_play_background_video()` right after `await stop()` races it and the teardown kills the just-started video. The branch just stops and lets `background_video_loop` restart the idle video on its ≤3 s tick.

### The remote's volume must never reach the host OS mixer

`_remote_should_handle` claims `volume_up`/`volume_down` **unconditionally** (except during window-pause) — an unclaimed press would hit Windows' default handler and change the system volume (overlay pops, volume stays changed after the session). Idle presses route to VLC's amp; **during YouTube** they broadcast the `player_volume_step` yt_command, which steps the IFrame player's own gain in `tv.html`. That case is a deliberate exception to tv.html's "the IFrame must not attenuate — the OS mixer is the amp" invariant (which still holds for the dashboard slider's `volume_set`/`volume_step`, ignored by the page): don't "clean it up" by removing the case or by routing the remote through `set_system_volume`. Note the two YouTube volume paths are independent attenuators (slider = OS mixer, remote = player gain) and the player gain resets to 100 on each new video via the onReady lock.

### …and the OS-mixer volume guard is OPT-IN — an always-on guard fights quantizing audio endpoints

History, because this burned twice in opposite directions. v10.2.0 shipped an always-on `remote_volume_guard` on the theory that air-mouse volume arrives as HID consumer-control usages the keyboard hook can't suppress. **On the reference hardware that theory was wrong**: the hook's suppression alone had kept the mixer untouched since v9.13.0 (verified live), and the always-on guard actively *broke* volume during VLC playback — audio endpoints (HDMI outputs especially) **quantize** master volume, so "revert to 43" can land on 42, which the next 0.35 s tick reads as a fresh outside change → revert again, forever: the mixer flickers, the OSD reappears, and once the ~1.2 s hook-dedupe window lapses each phantom delta **drip-steps VLC's volume** on its own. v10.3.1 therefore made the guard **opt-in** (`REMOTE_VOLUME_GUARD=1`, `Settings.remote_volume_guard`, default off) and gave it two anti-fight defences for when it is needed: a ±1 tolerance band that *adopts* drift instead of reverting it, and re-reading the endpoint after each revert to adopt the value the device actually stored.

Rules that still stand: the default path is hook-only (claim + suppress + route to VLC/player-gain — the proven v9.13.0 behaviour plus the always-claim rule); only enable the guard for a remote whose presses provably change the system volume *while remote support is running*; every legitimate OS-volume writer must go through `set_system_volume` (it records `state.host_volume_expected` — a bypassing writer gets fought by an enabled guard); and the guard resyncs instead of reverting during `window_mgmt_paused()`.

### TV UI screen arbitration: `state.tv_ui_active` gates every VLC focus assertion

The host display is contested by four surfaces: fullscreen VLC (playback), the idle background video (also VLC), the YouTube kiosk, and the TV UI dashboard kiosk. VLC's side is enforced by `vlc_focus_and_fullscreen()` — a ~24 s loop that **minimizes every other window** — plus `background_video_loop` restarting the idle video. Rules that keep them from fighting the dashboard kiosk:

- `vlc_focus_and_fullscreen` bails while `state.tv_ui_active` (same pattern as `youtube_active`). Consequence: **any real-play path must release the claim before its focus task's first check runs** — that's centralized in the `vlc("in_play")` branch (all VLC plays go through it; the bg video posts `in_play` directly via the raw client precisely so it does NOT trip this) and in `youtube_play`. If you add a new play path that focuses VLC without an `in_play` through `vlc()`, clear `tv_ui_active` yourself or the screen never leaves the dashboard.
- `background_video_loop` skips while `tv_ui_active` (the idle video would otherwise start playing — audibly — underneath the kiosk), and `_tv_ui_show` pauses a playing bg video with `pl_forcepause` (NOT stop: `background_playing` stays True and the VLC state reads `paused`, which both keep the loop out and let `_tv_ui_hide` simply `pl_forceresume`).
- `tv_ui_loop`'s playback-janitor only clears the claim when the last input is >10 s old — a just-pressed Home shows the UI *then* calls `stop()`, and the janitor must not hand the screen back to the dying playback in between.
- The dashboard kiosk uses its **own Chrome profile dir** (`.tvui_chrome_profile`). Sharing `.tv_chrome_profile` would (a) merge it into the YouTube kiosk's browser instance and (b) get it killed by YouTube's Stop, which matches that path in process cmdlines.
- The Windows focus code finds the kiosk by **window title marker** (`_TVUI_WINDOW_MARKER` = "StreamLink TV Dashboard", set via `document.title` when `?tv=1`). The markers are substring matches — neither marker may contain the other ("StreamLink TV Player" is the YouTube one), and the frontend must never retitle the page in TV mode.

### "Use My Computer" + TV UI: minimizing VLC reveals the kiosk, and a timed pause must not hard-expire under a busy desktop

Two traps found in 10.10.0, both presenting as "I paused window control but the web UI keeps taking the screen":

- **The hidden kiosk sits fullscreen *behind* VLC.** `_tv_ui_hide` deliberately leaves the kiosk Chrome running (instant next wake), and `_tv_ui_show` may have launched it at any point in the session. So a pause handler that only calls `vlc_minimize()` doesn't produce a desktop — it produces the dashboard, fullscreen, because that's the next window in the Z-order. `/api/window-control` pause therefore also clears `tv_ui_active` and minimizes every `_TVUI_WINDOW_MARKER` window (`_minimize_tv_browser_windows`); `_bring_tvui_to_front` additionally aborts-and-minimizes if the pause engages during its ~10 s reinforcement loop (a freshly launched kiosk window can appear seconds after the pause starts).
- **A timed pause used to hard-expire mid-typing.** The moment `window_mgmt_paused()` flipped false, `background_video_loop` restarted the idle video with a full focus grab and the very next keystroke woke the TV UI (`_tv_input_event`) — a fullscreen dashboard over the user's work, seemingly at random. `window_mgmt_paused()` now slides an expired deadline forward while `state.tv_input_last` is within `WINDOW_MGMT_EXPIRY_GRACE_SECS` (120 s), so a timed pause ends only after the desktop has actually gone quiet. Two consequences to respect: read `state.window_mgmt_paused_until` **after** calling `window_mgmt_paused()` (the call mutates it — `state_snapshot` does this for the countdown), and remember the slide relies on the input listener (`REMOTE_CONTROL=1`) stamping `tv_input_last`; with the listener off, timed pauses expire on schedule. Since remote presses are HID input too, a dead-remote press during a pause also extends it — accepted.

Related pre-existing trap fixed at the same time: the endpoint clamped timed pauses to **3600 s while the settings panel offers a "2 Hours" button** — the button silently became a 1-hour pause. Clamp is 7200 now; keep the UI's longest option ≤ the clamp.

## Frontend layout

### The dashboard is a height-locked app shell — the document must never become scrollable again (except the player escape hatch)

`html`/`body` in `static/index.html` are `100dvh` + `overflow:hidden` + `overscroll-behavior:none`; `<main>` and the overlay lists are the only scroll containers. This is deliberate (v5.26.2): when the body was the scroller, mobile overscroll rubber-banded the whole page — the fixed player footer and `fixed inset-0` overlays visibly detached, pull-to-refresh fired mid-list, and flicking past the end of a modal scrolled the page behind it. Footguns: don't put `min-h-screen` back on `<body>` (`100vh` > `100dvh` while the mobile URL bar is visible → the shell overflows the locked viewport and the bottom of `<main>` gets clipped); don't attach scroll listeners or `window.scrollTo` to the document (it no longer scrolls — target `<main>` or the specific container); any new scrollable region needs `overscroll-behavior:contain` (the Tailwind `.overflow-y-auto` class is blanket-covered in the `<style>` head; elements made scrollable by bespoke CSS must be added to that rule).

**The one sanctioned exception** is the on-device player: a locked document means mobile browsers can never auto-hide their URL bar (that only happens on real *root* scrolls — an inner scroll container never collapses it, which is why the rule targets `html:has(...)`, not `body`'s own `overflow`). While `#localPlayer.lp-active:not(.lp-tiny)` is open (coarse pointers only), the root regains `overflow-y:auto` and `<body>` grows 45vh taller than the viewport, so a swipe on the video minimizes the browser chrome. Two traps baked into that rule: the scroll range must come from the **body's own box** — a `position:absolute` spacer hanging below body does *not* reliably extend Safari's root scroll range (the first implementation used `body::after` and iOS Safari never scrolled) — and don't widen the rule to other overlays without the same justification, since every root-scrollable state reintroduces a slice of the loose-page behavior. Separately: **true fullscreen on iPhone Safari** is `video.webkitEnterFullscreen()` (native player takes over; the only API that exists there) — the player's fullscreen button falls back to it when element fullscreen is unavailable; don't use it on platforms that *have* element fullscreen, it forfeits the custom chrome. See [FRONTEND.md](FRONTEND.md) § Layout and [STREAMING.md](STREAMING.md).

### Orientation lock rotates the player with a CSS transform — two traps

The player's orientation lock (`#lpRotBtn` → `.lp-lock-landscape`) needs a CSS fallback because iPhone Safari has no `screen.orientation.lock()`: in a portrait viewport the whole `#localPlayer` is sized to the swapped viewport dimensions and `rotate(90deg)`-ed (the native lock and the CSS rule can't fight — when the native lock holds, the `(orientation:portrait)` media query never matches). Traps: **(1)** the `transform` makes `#localPlayer` the containing block for `position:fixed` descendants — every child of the player must stay `position:absolute` (they all are today; a `fixed` child would silently anchor to the rotated box on lock and to the viewport otherwise). **(2)** Browser hit-testing follows the transform, but any **manual screen-coordinate math** does not: the seek bar renders vertically while rotated, so `_lpSeekPosFromEvent` swaps to `clientY`/`r.height` when `_lpRotated()` is true — any new drag/scrub interaction inside the player must do the same.

## iOS client app (Capacitor)

See [IOS_APP_PLAN.md](IOS_APP_PLAN.md). The app lives in `ios-app/` (separate
Node/Xcode project; exempt from the repo's Windows-first rule — but its *server*
endpoints, added in later milestones, are not).

### A pure *background* `URLSession` throttles downloads — run a hybrid (fast while foreground)
iOS runs a background `URLSession`'s transfers **out-of-process (`nsurlsessiond`)
at background QoS and rate-limits them even in the foreground**, with
`isDiscretionary = false` set. So a downloader built only on a background session
crawls — while HLS playback of the *same* segments streams instantly, because
AVPlayer/WKWebView fetch in-process at full link speed. That asymmetry is exactly
the "streams load fast but downloads lag" report (fixed v7.9.0). `BundleDownloader`
now keeps **two sessions sharing one delegate**: a `URLSessionConfiguration.default`
(`fgSession`) used while the app is foreground, and the background session used
while suspended (so downloads still complete minimized / across a kill). `enqueue`
routes by an `appActive` flag; on each `didBecomeActive` / `didEnterBackground`,
`migrateTasks` moves in-flight tasks between the two by **cancel + re-enqueue** —
HLS segments are small 6 s fmp4 chunks, so restart-from-scratch is cheap and avoids
the brittle resume-data path. The delegate methods key off `taskDescription`
(`sha\0file`), so they handle tasks from either session identically; `cancelLocked`
must sweep **both** sessions. Don't "simplify" back to a single background session —
that reintroduces the lag.

**The hand-off MUST be synchronous — never `getAllTasks` (v7.9.4).** The
`didEnterBackground` migration has to finish inside iOS's brief post-background
execution window: an in-process default-session task freezes the instant the app
suspends, so any file still on `fgSession` when the window closes is stranded with
nothing on the background session → downloads "stall when backgrounded and only
recover on a full app restart" (the restart re-drives the durable queue). The
original `migrateTasks` used `URLSession.getAllTasks` — an **async round-trip to
`nsurlsessiond`** plus another queue hop — and routinely didn't complete in time.
It now drives the manager's **own per-file `job.tasks[name]` references**
(`URLSessionDownloadTask`) synchronously on the state `queue`, with no daemon
round-trip, so the cancel + re-enqueue onto the destination session is deterministic
within the window. Keep `job.tasks` accurate: set it in `enqueue` (after `resume()`),
and clear the entry on file completion AND when a file enters retry backoff (so a
backoff file with no live task is skipped by migrate and re-routed by its own timer
instead — re-enqueuing it in migrate would race the backoff into a duplicate task).

### `LocalMediaServer` binds loopback-only — by design, and to dodge the privacy prompt
The `NWListener` sets `params.requiredInterfaceType = .loopback`, so the media
server is reachable only from the device itself (nothing on the LAN). A bonus:
loopback listeners do **not** trigger the iOS 14+ Local Network privacy prompt,
so no `NSLocalNetworkUsageDescription` is needed. If you ever widen it to the LAN,
you must add that key and handle the prompt.

### Native iOS HLS requires byte-`Range` — a 200-only static server silently fails
iOS plays HLS via `<video>.src` (not hls.js). For fmp4 (`.m4s`) segments the
native player issues `Range` requests; a server that ignores `Range` and always
returns `200` makes playback stall or error with no useful console message.
`LocalMediaServer` answers `206 Partial Content` with `Content-Range` (and
`416` for unsatisfiable ranges). Mirror `_HLS_MIME` exactly — the wrong MIME on
`master.m3u8` (`application/vnd.apple.mpegurl`) also makes the native player
refuse the stream.

### ATS cleartext is opened for `127.0.0.1`/`localhost` ONLY — the host stays HTTPS
`Info.plist` excepts only loopback for insecure HTTP loads. The remote host is
reached over HTTPS; for a self-signed host cert, install the host's **CA profile**
on the device (Settings → VPN & Device Management, then enable full trust under
Certificate Trust Settings). Do **not** flip `NSAllowsArbitraryLoads` on to "make
it work" — that defeats the point and isn't needed once the CA is trusted.

### The Capacitor bridge survives navigating the WebView to the remote host
The shell (`www/index.html`) navigates the WKWebView to `https://<host>` and the
existing dashboard loads there. Capacitor injects its bridge at the WebView level,
so `window.Capacitor` + native plugins remain available on the remote origin —
that's what lets the dashboard's (future) B5 glue call `LocalMediaServer`.
`server.allowNavigation: ["*"]` in `capacitor.config.json` is required, or
Capacitor opens the host in Safari instead of in-app.

### `100dvh` corrupts the height-locked shell in WKWebView — use `vh` in the app
The dashboard locks its shell to the viewport (`html,body{overflow:hidden}` +
`body{height:100dvh}`) so the document never scrolls (only `<main>`/overlays do).
`dvh` (dynamic viewport) is right **in a browser** — it shrinks with the collapsing
URL bar so the shell isn't clipped. **In the Capacitor WebView it's a trap:** there
is no URL bar, but WKWebView still shrinks the dynamic viewport when the **soft
keyboard** appears (any text input — search, profile name, episode field) and
**often fails to restore it on dismiss**, so the locked shell stays sized wrong —
the layout visibly breaks (misaligned/overlapping/clipped) and buttons in the
shifted region go dead **until the app is relaunched**. This looked like a random
"renders incorrectly during use" bug and dated all the way back to M2. Fix: mark the
document `is-app` **before first paint** (inline `Capacitor.isNativePlatform()`
check — the runtime is injected at document-start) and pin `html.is-app, html.is-app
body { height: 100vh }`. The **large**-viewport `vh` ignores the keyboard, so it's
both correct and stable in the app (no chrome to collapse). Browsers keep `dvh`.
Same reasoning excludes the app from the browser-only "swipe to hide the URL bar"
body-growth hack (`html:not(.is-app):has(#localPlayer.lp-active…)`).

### Don't double up safe-area insets — `contentInset:"never"` when the CSS already uses `env(safe-area-inset-*)`
The dashboard pads itself with `.safe-top`/`.safe-bottom` (`env(safe-area-inset-*)`),
so the page already handles the notch/home-indicator. Capacitor's
`ios.contentInset: "always"` *also* insets the WebView's scroll view — applying the
top inset **twice**, and worse, Capacitor's `scrollView.contentInset` **goes stale on
orientation change**: rotate to landscape and back and the (white) scroll-view
background is exposed above the content while the whole UI sits shifted down, until
the app is relaunched. Fix: **`contentInset: "never"`** in `capacitor.config.json`
(both the root and the synced `ios/App/App/` copy) so the web content owns the full
`viewport-fit=cover` viewport and the CSS `env()` insets — which WebKit *does*
recompute on rotation — are the only inset source. Also set the WebView
`ios.backgroundColor` and the `html`/`body` background to the app's dark
(`#030712`) so any momentary reflow gap is dark, never white. Rule of thumb: pick
**one** inset owner — native `contentInset` *or* CSS `env()`, never both.

### …and `env(safe-area-inset-top)` is unreliable in WKWebView — in the app, PIN the inset to a constant, don't use live `env()` (both orientations)
Having made CSS `env()` the sole inset owner (above), don't trust it inside the app —
it's corruptible in **both** directions on the navigated host dashboard:
- It frequently resolves `env(safe-area-inset-top)` to **~0**, so `.safe-top` chrome
  (the fullscreen remote header, the top app bar) gets no padding and the status bar /
  Dynamic Island **overlaps** it. (This is why a floor existed at all.)
- After the **soft keyboard** (search box, profile/episode fields) it often
  *over-reports* on dismiss and never restores — same family as the `dvh` keyboard
  bug — so the old `max(env(...), 59px)` jumped *above* 59 and pushed the top chrome
  (top app bar, **non-fullscreen player header**) visibly **down**, stuck until
  relaunch. This presented as "the top UI drifts down after I use search."

Because env() under-reports to ~0 in the steady state, the 59px floor was already the
only thing showing — so the fix is to drop the live env() term entirely in the app and
**pin a constant**: `html.is-app .safe-top { padding-top: 59px }` (and `34px` for
`.safe-bottom`). Visually identical in normal use, but the corruptible dynamic value
can no longer drift it. **Pin per orientation, but pin both** — the first cut scoped
the pin to `@media (orientation: portrait)` only and left landscape on the live
`env()`; landscape over-reports the same way (reproduced on-device after **on-device
playback + rotating** between landscape/portrait and fullscreen/non-fullscreen — the
corrupted landscape inset stuck and shoved the top chrome down). So pin landscape too:
`@media (orientation: landscape) { html.is-app .safe-top { padding-top: 0px }`
(legitimate landscape top inset — Island sits on the side) `.safe-bottom { padding-bottom: 21px } }`
(home-indicator band). Portrait stays `59px`/`34px`.
Trade-off: 59pt is the Dynamic-Island inset (the largest current top inset); notch
phones (~47pt) sit comfortably under it — a small dark band beats either the status
bar covering the controls or the intermittent drift. (Browsers are untouched — they
keep `.safe-top { padding-top: env(safe-area-inset-top) }`.)

### In the app, lock the viewport (`maximum-scale=1, user-scalable=no`) — injected natively, not in the page
iOS auto-zooms the WebView whenever an input with `font-size < 16px` gains focus (the
PIN pad, the search box, profile/episode fields). The host dashboard's `<meta
viewport>` pins `initial-scale=1` but **not** `maximum-scale`, so the focus zoom-in
**sticks** with no working zoom-out — users were double-tapping to escape. WKWebView
(unlike mobile Safari since iOS 10) **honors `user-scalable=no`**, so the fix is to
force a locked viewport. Do it **natively** in `MainViewController.injectViewportLock()`
(a `WKUserScript` at `.atDocumentEnd` for every navigation that rewrites the viewport
meta to `maximum-scale=1.0, user-scalable=no, viewport-fit=cover`) — *not* in the page
markup, so it also covers the **remote-served** dashboard, and only inside the app so
browser users keep pinch-to-zoom. Same injection pattern as `injectCapacitorRuntime()`.

### Cross-origin data between the shell and the host dashboard must go through a native plugin — NOT `localStorage`
The connect shell (`capacitor://localhost/index.html`) and the host dashboard
(`https://<host>`) are **different web origins**, so `localStorage` written by one is
invisible to the other. This bit M5's pairing token: the shell pairs and gets a
token, but the dashboard (which actually makes the device-facing fetches) can't read
it from `localStorage`. Fix: store such shared state **natively** —
`OfflineStore.set/getPairingToken` persists it in the app sandbox, readable from
both origins (same reason the offline progress log is native). The shell writes the
token after pairing; the dashboard reads it at startup into `_pairToken` and sends
`Authorization: Bearer …`.

### The app WKWebView has no visible JS console — surface uncaught errors on-screen
A bug that only reproduces inside the Capacitor shell is effectively undebuggable by
report alone: WKWebView shows no console, so a thrown exception just blanks a feature
(the "Library tab doesn't open in the app, but does on the web" report was a silent
throw/rejection with no on-device feedback). Two standing mitigations (both app-only,
browsers keep devtools + the clean UI): (1) `installAppErrorSurface()` registers global
`error` + `unhandledrejection` handlers that paint a tappable red banner and exposes
`window.__appShowError(msg)` for explicit `catch` blocks — gated on **Dev mode**
(`streamlink_devmode`, read live inside the handler so toggling is instant and there's no
dependency on the `devMode` global being initialised yet), so normal users never see raw
errors; (2) fire-and-forget async UI
loaders (e.g. `loadLibrary`) must `catch` and render their failure **inline** (with the
error text) rather than leaving a blank pane — a blank tab reads as "doesn't work" with
no clue, an inline error is self-diagnosing. To get the full stack with line numbers,
attach **Safari → Develop → [device] → the StreamLink WebView → Console** (no rebuild
needed). When chasing an app-only bug, reach for these before guessing.

### Self-heal a corrupt `streamlink_profile` in `localStorage`, don't just swallow the parse error
The profile-restore on load reads `localStorage["streamlink_profile"]` and `JSON.parse`s
it. A historical server bug (pre-7.8.1) persisted the literal string `"undefined"`, which
throws on every parse and forced the profile picker forever ("login not remembered").
The restore now explicitly clears `"undefined"`/`"null"`/empty/unparseable values (so a
later successful login re-seeds cleanly) and surfaces *why* a restore was skipped
(corrupt value / id not on server / `GET /api/profiles` failed) via `__appShowError` —
because in the app each of those silently drops to the picker and is indistinguishable
from "WKWebView dropped my storage" without the signal. Note the app's WKWebView storage
is a **separate** store from any desktop browser, so a value corrupted before a fix lingers
in the app until it logs in once post-fix (or is cleared).

### In the app, file delivery (`navigator.share`/download) is dead — open the host URL in Safari instead
The dashboard runs in the WebView over a **plain-http host origin**, which is not a
**secure context**. So `navigator.share` (and `navigator.canShare`) — the Web Share
**file** API — is **undefined**, `<a download>` is **ignored** by WKWebView, and
`window.open(url)` for a same-origin host URL just navigates in-WebView (no save/share
sheet). The Clip feature hit all three. Fix: when `isApp`, `_shareOrDownload` hands the
clip's **host URL** to **Safari** via the native `BundleDownloader.openExternal({url})`
(`UIApplication.shared.open`), where iOS previews the MP4 with a native
Save-to-Files/Photos + Share sheet. Works because the clip URL's random 16-hex token is
the capability (`GET /api/library/clip/{token}/{filename}` has no `_require_device_auth`),
so Safari needs no `Authorization` header. Any future "save/share a host file from the
app" should reuse `openExternal`, not the Web Share / download paths.

### In-app navigation back to the shell pages is a full cross-origin navigation to `capacitor://localhost`
Once the WebView is on the host dashboard there's no browser chrome, so the M5 `☰ App`
menu (and `downloads.html`'s buttons) navigate with `window.location.href =
"capacitor://localhost/index.html?setup=1"` (or `/downloads.html`). That's a real
page load across origins — allowed only because `server.allowNavigation: ["*"]` and
`limitsNavigationsToAppBoundDomains:false` are set. The local origin is always
`capacitor://localhost` (the `iosScheme` + default hostname); don't assume `file://`.

### A new native Swift file must be added to the Xcode project — `cap sync` won't do it
The generated `App.xcodeproj` is a classic file-reference project (`objectVersion 60`,
no `PBXFileSystemSynchronizedRootGroup`), so a `.swift` file dropped into
`ios/App/App/` is **not compiled** until it's listed in `project.pbxproj` (a
`PBXBuildFile`, a `PBXFileReference`, the `App` `PBXGroup` children, and the
`PBXSourcesBuildPhase` files list). `npx cap copy`/`cap sync` only touches web
assets + the `public` folder — it never adds native sources. **Symptom:** the app
builds with no error but the plugin call rejects with `"<Name>" plugin is not
implemented on ios`. Easiest correct way to add one: open the project in Xcode and
drag the file into the **App** group with "Add to target: App" ticked. (We did it
by hand in `project.pbxproj` for `LocalMediaServer.swift`.)

### App-local native plugins are NOT auto-discovered — register them in a `CAPBridgeViewController` subclass
Capacitor 8 does **not** scan the Obj-C runtime for `CAPBridgedPlugin` classes.
`CapacitorBridge.registerPlugins()` only loads the class names in
`capacitor.config.json`'s `packageClassList`, which `cap sync` generates **from
installed Capacitor npm packages only**. A plugin written as a plain Swift class in
the app target is never in that list, so it's compiled but never registered —
the JS call rejects with `"<Name>" plugin is not implemented on ios` (same message
as a missing file, so it's easy to misdiagnose). Fix: subclass
`CAPBridgeViewController`, override `capacitorDidLoad()`, and call
`bridge?.registerPluginInstance(MyPlugin())` (see `MainViewController.swift`); point
the storyboard's root view controller at the subclass (`customClass` +
`customModule="App"` + `customModuleProvider="target"`). `registerPluginInstance`
is on `CAPBridgeProtocol` and works regardless of `autoRegisterPlugins`. The
alternative — making the plugin a local Swift Package added to `CapApp-SPM` — is
heavier; manual registration is the right call for a couple of app-local plugins.
**Every new app-local plugin must be added to that `capacitorDidLoad()` list** — it's
easy to add the `.swift` file + pbxproj refs and forget the registration, in which
case the plugin compiles but the JS helper that wraps it silently no-ops. This bit
M3's `OfflineStore` (added to the project but not registered → offline progress
never saved/resumed/synced, with no error surfaced) — fixed in `preview.3.1.2`.

### A no-bundler web page needs `capacitor.js` — the injected bridge has no `registerPlugin`/`Plugins`
The native runtime injects `native-bridge.js` (it provides `nativePromise`/`toNative`
on `window.Capacitor`), but it does **not** create `Capacitor.registerPlugin` or the
`Capacitor.Plugins` proxy — those live in `@capacitor/core` and are meant to be
bundled into the web app. Our `www/` has no bundler, so it vendors
`node_modules/@capacitor/core/dist/capacitor.js` and loads it with a `<script>` in
`<head>`. That IIFE reads the existing `window.Capacitor` (the bridge) and adds
`registerPlugin` + the `Plugins` proxy, which route calls back to the native bridge.
**Symptom when it's missing:** `LocalMediaServer plugin not registered` /
`Capacitor.registerPlugin is not a function`, even though the native plugin built
fine. Keep the vendored copy version-matched with `@capacitor/core` (re-copy on
upgrade). **The remote dashboard (`static/index.html`) is served by the host and
can't load `capacitor.js` itself**, so M2 instead injects the vendored core
runtime into every page from the native side: `MainViewController.injectCapacitorRuntime()`
adds it as a `WKUserScript` at `.atDocumentStart`. Without it, the dashboard's
`isApp` detection is false and all offline glue is silently inert.

### Offline glue on the dashboard must be gated on `isApp` — never assume the bridge
`static/index.html` is BOTH the browser dashboard and the in-app UI. Every M2
addition (Download button, `master_url` swap, progress hooks) is gated behind
`const isApp = !!(window.Capacitor && Capacitor.isNativePlatform?.() && both plugins)`,
so a plain browser is byte-for-byte unaffected. `_appDlBtnHTML()` returns `""`
and `_appLocalBundle()` returns null off-app. Don't call a native plugin without
the `isApp` guard — `Capacitor` may be undefined (browser) or the plugin missing.

### In-app "tabs" must be overlays on the host page — never full-page navigations — or in-flight downloads die
The dashboard (`static/index.html`, served by the host) is the page that
*orchestrates* every download: `_appPrepBundle` polls the host's `/offline-job`
while it builds the HLS bundle, and `_appRunPooled` drives the multi-episode pool,
all in this page's JS, **before** each file is handed to the durable background
`URLSession`. A full-page navigation away (the old `_appGoDownloads` /
`_appGoSettings` → `capacitor://localhost/...`) **tears this page down**, killing
the SSE link AND every in-flight pool lane / prep-poll — so any series episode not
yet handed to the native downloader is lost. The in-app **Downloads** and **Change
Server** menu items therefore open **overlays on the live host page**
(`_appOpenDashboard`, `_appOpenChangeServer`), not navigations. The ONLY legitimate
disconnect is connecting to a *different* server (inside the Change Server overlay).
`downloads.html` / `index.html` stay as the **offline** entry points (host
unreachable) — those are separate local-origin pages by necessity.

### Keep-alive during prep is a ~30s bridge, not a long-running guarantee — `holdBackground`/`releaseBackground`
JS can't take a `UIApplication` background assertion, so `BundleDownloader` exposes
ref-counted `holdBackground()`/`releaseBackground()` (a *separate* assertion from
the job-keyed `bgTask`), and the JS orchestration brackets its prep/handoff window
with them. This only buys the ~30s iOS grants a background task — enough to survive
a brief background mid-prep. It does NOT keep the page alive through a long
host-side prep while minimized; for that the JS pool **self-resumes on foreground**
(suspended `setTimeout`s fire late but still fire, and the host prep `job_id` stays
valid up to its 3h ceiling). The durable path is the background `URLSession`: once a
file is handed off, it transfers to completion while suspended regardless.

### On-device (loopback) playback is SAME-ORIGIN only — it works offline but stalls from a remote (connected) host page
The on-device bundle is served from the native `http://127.0.0.1:<port>` loopback
`LocalMediaServer`, and the player is pointed at
`http://127.0.0.1:<port>/master.m3u8`. Whether that plays hinges entirely on
whether it's a **same-origin** load:
- **Offline** the dashboard *itself* is served from that same loopback (player-mode
  LMS — snapshot at `/`, bundles mounted at `/StreamLinkBundles/<sha>/`), so the
  media is **same-origin** → plays instantly.
- **Online** the dashboard is a **remote host page** (`http://<host>` / `https://<host>`),
  so the device copy would be a **cross-origin** loopback load. **WKWebView stalls
  that media pipeline indefinitely** — and misleadingly, a plain `fetch()` to the
  loopback from the same page *succeeds* (the LMS sends `Access-Control-Allow-Origin: *`),
  so a fetch-based reachability probe can't detect it. The stall then surfaces as a
  fatal hls.js `NETWORK_ERROR`, which the player treats as a recoverable tunnel drop
  and **retries forever** — so a downloaded episode "never loads while connected."

**So the device copy is only ever loaded when page and media share an origin.**
`_lpLoadIndex`'s device branch (gated `_appOffline || !hlsAvailable`) runs when the
page is already the loopback: offline, or a no-HLS macOS host. **Never re-enable
online loopback playback with a fetch probe or a CORS tweak** — the block is on the
cross-origin (and, HTTPS host, mixed-content) *media* pipeline, not on `fetch`, so
neither can fix it. This is why the 7.17.0 "play the downloaded copy while online"
convenience was reverted to offline-only in 8.0.1: it never actually worked in
WKWebView.

**The reliable way to prefer the device copy online is the proxied playback session
(8.7.0):** don't try to load the loopback bundle into the remote host page — instead
move the *page* to the loopback first, and reverse-proxy everything else back to the
host so no feature is lost. `lpPlay` → `_appTryLocalHandoff` starts the LMS in **proxy
mode** (`lms.start({playerRoot, proxyHost, proxyToken})`) over the `__player__` snapshot
and `location.replace`s to it with `?proxied=1&host=<origin>&item=&file=&seek=&profile=&am=`.
On that same-origin snapshot page the bundle plays exactly like offline, **but
`_appOffline` stays false** so it boots as a full online dashboard — SSE, Play-to-TV,
library, `/api` progress/track sync — because the native server forwards every `/api/*`
+ SSE + server-media request to the host (bearer token injected). `lpStop` →
`_appProxiedReturnHost()` returns to the direct host origin. So "serve the page from the
loopback" isn't a dead end — it's the mechanism, scoped to a single downloaded-episode
playback session (the user chose "only during playback"; running the *whole* app from
the proxy is a bigger, deferred option).

**Native-proxy gotchas** (`ProxyForwarder` / `HLSStaticServer` in LocalMediaServer.swift):
- **Set `Accept-Encoding: identity` on the outbound request** and drop
  `Content-Encoding`/`Transfer-Encoding` from the relayed response — otherwise URLSession
  transparently gunzips the body while the host's `Content-Length` still describes the
  *compressed* size, and the relayed length mismatches the bytes.
- **SSE must stream, not buffer** — `/api/events` never completes; forward each
  `didReceive data` chunk immediately and cancel the upstream task when the client
  `NWConnection` drops (`conn.stateUpdateHandler` → `clientGone()`), else the task lingers
  until the resource timeout.
- **Back-pressure or the proxy balloons memory on a slow client** — the delegate queue is
  serial and blocks on a semaphore until `conn.send` completes, throttling the pull from
  the host (mirrors `streamBody`).
- **URLSession needs the self-signed-TLS opt-in explicitly** — `NSAllowsArbitraryLoads`
  covers WKWebView/ATS but *not* URLSession; the server-trust challenge must return
  `.useCredential`.
- **Snapshot-version gotcha (unchanged from 8.6.0):** the `proxied=1` page runs the device
  *snapshot* of static/index.html, so the proxied-boot/auto-play code only exists there
  after the snapshot refreshes (version-badge bump via `/api/player-manifest`). An app on
  an upgraded host that hasn't re-snapshotted degrades safely (old snapshot ignores the
  unknown params). **But the native proxy needs the new Swift build** — a stale app binary
  can't start proxy mode at all, so the handoff `lms.start({proxyHost})` param is simply
  ignored and it behaves like a plain player-mode start (api calls 404) → keep the app
  build and the host version in step.

(The M1 ATS exception, scoped to loopback, only governs plain `http` cleartext loads
— it's unrelated to this same-vs-cross-origin media behavior.)

### Bundle downloads are durable, but only *completed files* survive a kill — partials resume
`BundleDownloader` writes each finished file straight into the final
`StreamLinkBundles/<sha>/` dir, then flips `complete` in `index.json` only once
**all** files landed. A kill mid-download leaves a partial `<sha>/` dir;
`getLocal()` reports `complete:false` (so the player won't try to serve it), and
re-calling `download()` **resumes** by skipping files already on disk at their
manifest size. So "resumable" means *file-granular*, not *byte-granular within a
file* — an interrupted single large segment re-downloads from scratch. The dir is
marked `isExcludedFromBackup` and lives in Application Support (not Caches), so iOS
won't evict it under storage pressure.

### Background URLSession needs the AppDelegate `handleEventsForBackgroundURLSession` hook — without it, progress "deferred to next launch"
`BundleDownloader` uses `URLSessionConfiguration.background(withIdentifier:)` so
downloads continue and complete while the app is suspended (the "downloads work
when minimized" requirement, `preview.6.0.0`). An **earlier** background attempt
looked broken — the UI sat at 0% and the bundle only "appeared" complete after a
restart — because the app never implemented
`application(_:handleEventsForBackgroundURLSession:completionHandler:)`. Without
that hook the OS has nowhere to deliver the finished-transfer events while the app
is away, so `nsurlsessiond` holds them until the next *cold* launch (which then
redelivers them, moving the files + writing the index — hence "complete after
restart"). The fix is **not** to abandon the background session but to add the
hook: `AppDelegate` stashes the system completion handler on
`BundleDownloadManager`, which fires it from `urlSessionDidFinishEvents(forBackgroundSession:)`
once events flush. Behaviour now: foreground → `didWriteData` fires live (smooth
progress); suspended → byte progress is coarse/batched but **per-file completion
still advances** and transfers genuinely finish. The old `beginBackgroundTask`
assertion is kept as a harmless foreground tail. Don't switch back to a foreground
`default` session — it dies ~30 s after backgrounding.

> **Completion must be reconciled from disk, not from the in-memory `Job`.** The
> `jobs` map is lost on a headless background relaunch, so a file that finishes
> then is **moved to disk** (the move in `didFinishDownloadingTo` runs before the
> `jobs[sha]` lookup) but `markComplete` is **skipped** — the `guard let job =
> self.jobs[sha]` bails. For a long time this left `index.json` `complete:false`
> forever (there was no "resume scan" that finalized it, despite an earlier claim
> here), and since `downloads.html` lists **only** `complete` bundles, a bulk
> download whose tail finished while suspended **silently vanished from Downloads
> even though every segment was on disk** (`preview.6.1.1` fix). The cure is to
> never trust the in-memory job for finalization: `reconcileIndexLocked()` flips a
> bundle `complete` whenever **all** its expected files are present at their
> expected size, and it runs whenever the index is read for display (`list()` /
> `getLocal()`) and after the background session flushes (`urlSessionDidFinishEvents`,
> which also emits the deferred `bundleComplete`). Data was never lost; only the
> `complete` flag was — and it now self-heals on the next Downloads refresh. **If
> you add any new download finalization path, make it disk-truth, not job-state.**

### A brief connectivity loss must retry, never drop — on both the JS and native sides
A bulk download is a long, network-fragile operation; "lose Wi-Fi for two seconds"
must **not** wedge the queue or discard episodes (`preview.6.1.3` fix). Two
independent layers each had a way to fail hard, so both are hardened:
- **JS orchestration (online-only, runs before native takes over).** The
  per-episode `bundle-manifest` + `offline-prepare` poll `fetch`es had no timeout —
  a WKWebView request caught mid-blip can **hang indefinitely** (it doesn't always
  reject), and since bulk downloads now pipeline through `_appRunPooled`, one hung
  lane strands every episode queued behind it at "Queued…". Always wrap these
  fetches in `_appFetchT` (AbortController timeout) so a hang becomes a *retryable
  error*, classify it with `_appIsNetErr` (abort/timeout/`TypeError` = transient;
  a thrown-on HTTP error = permanent), and retry the manifest/handoff with backoff
  in `appDownloadBundle` — keeping the episode marked in-flight ("Waiting for
  connection…"), **never deleting it on a network error.**
- **Native transfer (`BundleDownloader`).** The `URLSession` delegate must not
  cancel the whole bundle on a transport error or a flaky-tunnel 5xx/429. Route
  every per-file failure through `retryOrFail`, which re-enqueues transient ones
  with a capped backoff. Crucially this works *because* the session is a
  **background** `URLSession`: a freshly-created task **waits for connectivity** and
  the OS starts it when the link returns — so a re-enqueue during an outage is the
  resume mechanism, no manual reachability polling needed. (Foreground/`default`
  sessions don't get this, another reason not to switch back — see the background
  URLSession gotcha above.)

### …and a *queued* download must survive hours offline + an app kill — persist the intent, don't rely on page/job memory (`7.8.0`)
The connectivity hardening above keeps a download alive through a *brief* blip, but
it left two holes for a **long** outage (tunnel for hours, Airplane Mode overnight,
app force-quit): (1) the JS retry cap (`MAX_ATTEMPTS`, ~80 s of backoff) eventually
gave up and **deleted** the episode, and (2) the entire pre-handoff queue lived only
in the page's in-memory `offlineBundles` map + the native job's in-memory state, so
an app kill lost everything not already mid-transfer. The fix is to make the user's
**intent durable, separate from any in-memory driver**:
- **Native `queue.json`** (in `BundleDownloader`, beside `index.json`): `enqueue`/
  `dequeue`/`queueList`. The dashboard calls `enqueue` the instant Download is
  tapped — *before any network* — so the wish outlives the page and the process. An
  entry is cleared only on full completion (`bundleComplete`), cancel, or a
  *permanent* failure. `index.json` still tracks bundles already handed to the
  session; `queue.json` is the wishlist that re-creates that hand-off if it was lost.
- **JS resumer `_appResumeDownloadQueue()`** drains `queueList()` and re-drives every
  still-wanted, not-complete entry through the normal `appDownloadBundle`
  orchestration (idempotent — the native resume scan skips files already on disk). It
  runs on **launch** (`_appInitOfflineBundles`) and on every **`online`** event, so a
  season queued before a long outage finishes whenever the link returns. Guard it
  with an in-flight flag so launch + `online` firing together don't double-start.
- **Native transient retries now never give up.** `retryOrFail` dropped its
  `maxFileRetries` cap for *transient* errors — a handed-off file keeps retrying with
  capped backoff indefinitely (the background session waits for connectivity), so a
  multi-hour outage can't exhaust it. Only a *non-transient* error (bad URL/404/move
  failure) still fails fast. Consequently a `bundleError` event is now **terminal** —
  the JS handler treats it as permanent (clears the intent + toasts), instead of
  assuming the native side might still recover.
- **Footgun:** `appDownloadBundle`'s "already in flight ⇒ no-op" guard must exempt a
  `waiting` entry (`cur.downloading && !cur.waiting`), or the resumer can never
  re-drive a download stranded by an outage (a `waiting` entry carries
  `downloading:true`). And `remove()` must clear the `queue.json` entry too, or the
  resumer resurrects a download the user just deleted.
- **Footgun (7.9.3):** at launch, `_appInitOfflineBundles()` rebuilds `offlineBundles`
  from the native `list()` (bytes on disk). A *partial* bundle (incomplete,
  `bytesDone > 0`) must be marked **`waiting: true`**, not bare `downloading: true` —
  because after an app restart **nothing is actually transferring**: the page + native
  `jobs` died with the previous session, and **iOS cancels background-`URLSession`
  tasks on app termination**. Marking it bare `downloading` makes the resumer's
  "already being driven this session" guard (`st.downloading && !st.waiting`) skip it,
  so it shows "downloading" in the ongoing list forever with nothing behind it. `waiting`
  is the correct "stranded — re-drive me" state and the resumer/in-flight guards both
  exempt it; the persisted `queue.json` intent makes the re-drive idempotent.

### Live Activities need a separate Widget Extension target + App Group — and `LiveActivityIntent.perform()` runs in the *app* process
The download-progress and TV-remote Live Activities (`preview.6.0.0`) live in a
new **`StreamLinkLiveActivities`** widget-extension target (`product-type
app-extension`, bundle `com.streamlink.client.LiveActivities`, deployment iOS 17,
embedded via an "Embed Foundation Extensions" copy-files phase on the App target).
Both the App and the extension carry the **App Group** `group.com.streamlink.client`
in their `.entitlements`, and the main app's `Info.plist` has
`NSSupportsLiveActivities = true`. The shared sources
(`Shared/LiveActivityAttributes.swift`, `AppGroupConfig.swift`,
`TVRemoteIntents.swift`) have **membership in both targets**. Gotchas:
- The interactive Dynamic-Island buttons are `LiveActivityIntent`s whose
  `perform()` runs in the **app's** process (the system briefly spins it up even
  when suspended), which is *why* they can POST to the host control endpoints.
  They read the host URL + token + VLC-vs-YouTube flag from the App Group
  (`AppGroupConfig`), written by the `TVRemote` plugin on start/update — there is
  no other way to get that config into the intent.
- All ActivityKit code is availability-gated (`@available(iOS 16.1/17)`); the
  extension's min deployment is 17 so its `WidgetBundle` doesn't need `if
  #available`. iOS <17 devices simply never start an activity.
- The `.appex` is built automatically when the **App** scheme builds (it's a
  target dependency), so `build-ipa.sh -scheme App` covers it — no script change.
- Distribution caveat: the unsigned-IPA + sideloader path must re-sign **with App
  Groups enabled** (AltStore/Sideloadly rewrite the group id consistently across
  app + extension, so the shared suite still resolves). With automatic signing in
  Xcode, confirm the App Group capability provisions for both targets once.

### Live Activities outlive the app process — reconcile against `Activity.activities`, never trust an in-memory handle
ActivityKit activities persist on the lock screen / Dynamic Island **across app
relaunches** (and headless background launches). The `TVRemote` plugin originally kept
its activity in a single in-memory `_activity` handle, which is `nil` after any
relaunch — so `stop()` couldn't end the on-screen activity (it lingered stale after
playback ended) and the next `start()` requested a **fresh** one, leaving the previous
orphan live: "the Dynamic Island goes stale and stacks up." Fix: treat
`Activity<TVRemoteAttributes>.activities` (the live system list) as the source of
truth, not the cached handle —
- the `activity` getter falls back to `.activities.first` when `_activity` is nil, so a
  relaunch-orphaned activity is re-adopted instead of leaked;
- `start()` adopts the existing one (and ends any extras) rather than stacking a
  duplicate;
- `stop()` ends **all** live activities, not just the tracked one;
- the plugin's `load()` ends every stray on **cold launch** — nothing is playing yet,
  so any leftover is from a session that ended while the app was backgrounded/killed
  (the dashboard's `stop()` never reached it). The dashboard re-starts one via
  `_tvRemoteSync()` if a session is genuinely active.

Rule of thumb for any future ActivityKit usage: an in-memory `Activity` reference is a
convenience cache, never the authority — always be able to recover from `.activities`.

### The remote dashboard CANNOT load offline — `downloads.html` is the offline entry point
The whole dashboard UI (`static/index.html`) is **served by the host**. The shell
navigates the WKWebView to `https://<host>/`, so with no connection (Airplane Mode
/ host down) the WebView can't load the page at all — it hangs on "connecting to
server" and *none* of the in-page offline glue runs. So offline playback can't live
only in the dashboard. The app bundles a self-contained **`www/downloads.html`**
that lists `BundleDownloader.list()` and plays a bundle via `LocalMediaServer`
(native HLS) with zero network. `www/index.html` **probes host reachability**
(`hostReachable`: a `no-cors` fetch of `/api/version` with a 2.5 s `AbortController`
timeout) on launch and routes to `downloads.html` when the host is unreachable.
The dashboard's own `_lpLoadIndex` offline swap still helps *online* (play a local
copy instead of re-streaming), but it is **not** the offline path — it's only
reachable when the dashboard loaded, i.e. when online.

### `www/` is the source; `ios/App/App/public/` is generated — don't edit public/
`npx cap copy ios` copies `www/` into the app bundle's `public/` dir (gitignored).
Edit `www/`, then re-copy and rebuild. A fresh clone has no `public/` until you run
`npx cap copy ios` (or `cap sync`).

### After editing `www/` OR a `.swift` file you MUST do a full rebuild — stale builds look like "my change didn't ship"
This has bitten **three** times. Two separate copy steps gate what actually runs on device:
1. **Web (`www/`) changes** only reach the app via `npx cap copy`/`cap sync`. The
   `public/` dir is what's bundled — if it's stale, the app shows the OLD UI (e.g.
   a missing offline button, an old connect screen that hangs). `build-ipa.sh`
   runs `cap sync` **by default**, but `--fast` / `--no-sync` SKIP it.
2. **Native (`.swift`) changes** (e.g. the `BundleDownloader` foreground-session
   fix) require recompiling — only `xcodebuild` includes them. **Re-signing an
   existing `.ipa` ships none of this.**
**Building straight from Xcode is the sneaky variant (bite #3, v8.0.0):** Xcode
recompiles every `.swift` (half your change works, which *hides* the problem) but
bundles whatever is already sitting in `public/` — it never runs `cap sync`. The
offline-cached-player rollout shipped fresh native code with a stale shell +
downloads.html that way, so the device kept routing to the frozen fallback player.
If you build from Xcode instead of `./build-ipa.sh`, run `npx cap sync ios` in
`ios-app/` first, every time `www/` changed.
So to make any change take effect: `./build-ipa.sh` **with no `--fast`/`--no-sync`**
(or `cap sync` + Xcode), then reinstall. Symptom of skipping it: the device behaves
like the code was never changed — or, worse, like only *half* of it was. Quick
check: `grep <your-new-symbol> ios/App/App/public/index.html`.

## Diagnosing "the server went unreachable"

The **2026-09-13** outage is the worked example, and the lesson is what the logs
*couldn't* say. Symptoms: dashboard and TV UI dead, operator eventually rebooted
the host by hand at 14:44. What the logs showed:

- The process was **healthy throughout** — a flat ~168 log lines/min of VLC and
  qBittorrent polling right up to the manual reboot. No stall, no memory
  symptom, no traceback anywhere, event loop plainly responsive.
- Inbound requests **arriving over HTTPS were still being served** as late as
  14:44:11 (a `200` on `/api/admin/status`), seconds before the reboot.

And what they could **not** say — the blind spots that made it undiagnosable:

1. **Port 80 had no request logging at all.** `run.py` starts both uvicorn
   servers with `log_level="warning"`, which suppresses uvicorn's access log
   entirely. Every inbound request to the dashboard and the TV UI was invisible.
   The only reason *any* inbound traffic showed up is an accident: the HTTPS
   proxy on :443 forwards to `127.0.0.1:80` with `httpx`, and **httpx's own INFO
   logging** recorded those hops. Plain-HTTP clients bypass the proxy, so they
   left no trace whatsoever.
2. **`print()` output was unreachable.** The updater and reboot paths report via
   `print()`, not the `streamlink` logger — so `[reboot]`/`[updater]` lines
   appear nowhere in `streamlink_service.log`.
3. **Nothing proved reachability from inside the process.** A server that is
   alive but not accepting looks identical to a healthy one in these logs.
4. **Signal was buried.** At ~168 lines/min of `httpx` poll chatter, a real event
   is invisible without `grep`.

`diagnostics.py` exists to close exactly these gaps — see [DIAGNOSTICS.md](DIAGNOSTICS.md).
When something like this recurs, read `logs/access.log` (did requests arrive?),
`logs/vitals.log` (was the loop lagging, the thread pool saturated, the library
lock stuck?), and any `logs/stall_*.txt` (what every task was doing at the time).

**Do not read a `logs_old_<ts>.zip` as evidence of an update-triggered restart.**
`_archive_old_logs()` only runs when the updater left a `.rotate_pending` marker,
and that marker can sit unconsumed for hours — it means "an update was applied at
some point before this start", not "this restart was the update".

**Applying an update without the reboot leaves a split-brain process.** A manual
`POST /api/admin/updater/apply` with `reboot` off swaps the git checkout *and*
re-runs `setup.py` under the still-running interpreter: in-memory code is the old
build, while `static/*.html` and anything imported lazily now come off disk from
the new one. The 13:50 apply on 2026-09-13 did exactly this ~5 min before the
trouble started. Unproven as the cause, but reboot promptly after applying.

## ctypes on 64-bit Windows: always declare `restype`

`ctypes` defaults every foreign function's `restype` to **`c_int`** — 32-bit
signed. On 64-bit Windows any undeclared call that returns a **handle or
pointer** therefore silently **truncates** it, and the corrupt value faults the
moment something dereferences it.

`remote_input.py` had exactly this: `kernel32.GetModuleHandleW` with no
`restype`, its truncated `HMODULE` stored in `WNDCLASSW.hInstance` and passed to
`CreateWindowExW`. The result, three times within seconds of a boot:

```
Windows fatal exception: access violation
Current thread 0x00002bf4 (most recent call first):
  File "F:	orrentstreamingtool
emote_input.py", line 410 in _thread_main
```

**Why nobody noticed for so long:** ctypes wraps foreign calls in an SEH handler
and converts the access violation into an ordinary Python exception, which
`_thread_main`'s broad `except BaseException` swallowed into `self._error`. No
traceback, no log line, a listener that just quietly didn't work. It only became
visible once `diagnostics.enable_faulthandler()` started writing
`logs/faulthandler.log` — `faulthandler` sees the fault *before* ctypes converts
it.

So: declare `restype` **and** `argtypes` for every Win32 call, without
exception. And when a Windows feature "just doesn't do anything", check
`logs/faulthandler.log` before assuming the logic is wrong.

## A stale `streamlink_service.py` silently ignores your `daemon.py` change

`daemon.py` only **generates** the wrapper. `updater.refresh_service_wrapper()`
regenerates it from `daemon._WRAPPER_CONTENT` — but `updater.service_is_installed()`
imports `daemon` during the routine updater status poll (the admin UI hits it
every few seconds), so `daemon` is nearly always already in `sys.modules`
holding the **pre-update** template. A plain `import` hands back that stale
module, the refresh compares old-against-old, reports **"Wrapper already up to
date"**, and writes nothing — while reporting success.

Fixed in 11.24.1 with `importlib.reload`. Two lessons that outlive the fix:

1. **Never trust a launcher to carry a critical setting.** Access logging now
   attaches from `main.py` (`diag.attach_access_log`), which uvicorn imports
   fresh every boot, precisely so an old wrapper can't disable it.
2. **Verify a deploy by its effect, not its response.** The apply returned
   `"ok": true, "service_reinstalled": true`. The thing that proved it hadn't
   worked was `logs/access.log` simply not existing.

## H.264 levels cap macroblocks per SECOND, not per frame

The HLS ladder used to pick its level from output height alone — `4.1` for
anything at or below 1080p. That silently assumes ≤30 fps. Level 4.1 allows
**245,760 macroblocks/second**; 1920×1080 is 8,160 macroblocks, so:

| fps | MB/s | fits 4.1? |
|---|---|---|
| 23.976 | 195,644 | yes |
| 30 | 244,800 | only just |
| 50 | 408,000 | **no** — needs 4.2 |
| 60 | 489,600 | **no** — needs 4.2 |

NVENC does not quietly raise the level for you. It refuses:

```
InitializeEncoder failed: invalid param (8): Invalid Level.
[vost#0:0/h264_nvenc] Task finished with error code: -22 (Invalid argument)
```

which fails the whole job — and because prep re-queues on a timer, **the same
file retries forever**. One file did exactly that every ~5 minutes, and the
error was invisible until 11.24.0 started capturing ffmpeg's stderr tail.

`_h264_level_for(width, height, fps)` now uses the real Annex-A table. Two
things worth keeping in mind if you touch it:

- **Only ever raise the level.** A level also caps *bitrate* (MaxBR: 3.0 allows
  10 Mbps, 4.1 allows 50). Dropping a 480p rung from 4.1 to "correct" 3.0
  tightens a constraint that has been slack for years, and risks rejecting
  encodes that currently work. The function returns the higher of the legacy
  pick and the throughput requirement, deliberately.
- **`fps` can be absent or nonsense.** ffprobe reports `0/0` for some streams.
  Unknown fps falls back to 30 (what the old rule implicitly assumed), and the
  `omit_level` retry rung re-runs with no `-level` if the encoder still objects
  — so a bad probe can't permanently wedge a file.

## See also

### `pointer-events:none` is not a guard — it only stops a mouse

`.ctrl-loading` (the busy-spinner class behind `_markLoading`) sets
`pointer-events:none`. That blocks a click *and nothing else*: the element keeps
`disabled === false`, stays focusable and in the tab order, and still fires its
`onclick` from a native **Enter** press on a focused `<button>` or from a scripted
`.click()`.

This mattered most exactly where it was least visible. On the `?tv=1` kiosk the
remote's OK button *is* Enter on `document.activeElement` (`_tvNavKey`), so the user
with no cursor and the weakest feedback was the only one the guard never covered —
hold OK on a "busy" Download and it fires again each press. `_tvCandidates` already
filters out `disabled` **and** `pointer-events:none`, so you cannot *navigate* to a
busy control; but a control that was already focused when it went busy keeps focus.

Since 12.7.5 `_markLoading` sets real `disabled` too, saving the previous value in
`dataset.wasDisabled` so clearing busy restores it rather than enabling something
that was greyed for its own reasons. **Corollary:** don't manage `disabled` by hand
around a `_markLoading` call. Three callers used to do `btn.disabled=false;
_markLoading(btn,false)` — with state-saving that ordering captures `true` as the
prior state and the button sticks disabled forever. They now let `_markLoading` own it.

Verify a busy state with `el.disabled`, never with the class or the computed
`pointer-events`.

### Profile PINs are unsalted SHA-256 — the throttle is the real control

`_pin_hash` is a bare `sha256(pin)`, and a PIN is exactly 6 digits. The whole
keyspace is a million entries: a rainbow table for it is trivial, so `pin_hash` in
`library.json` should be treated as **reversible**, not as a protected secret. It is
not worth "fixing" in place — rehashing with a salt/KDF invalidates every existing
PIN unless paired with a migration, and anyone who can read `library.json` already
has the box.

What actually bounds guessing is the `verify-pin` throttle added in 12.7.5
(`PIN_BACKOFF`, per profile+client, 429 over the limit). It is deliberately gentle —
five free attempts, then seconds, never a lockout — because this is a television and
the realistic offender is a child mashing the pad, not an attacker with a LAN
foothold. If you ever tighten it, keep it un-lockable: a PIN that cannot be retried
is a TV the household cannot use.

### Non-finite floats: `NaN` defeats `max(0, min(cap, x))`

Pydantic accepts `NaN` and `±Infinity` for a plain `float` field (`allow_inf_nan`
defaults on), so any float that arrives over the wire can be non-finite — query
param or body alike. Verified live: `POST /api/vlc/seek/to?position_pct=NaN`
returned `200 OK`.

The trap is that the usual clamp does **not** save you:

```python
max(0.0, min(100.0, float("nan")))   # -> 100.0, not 0.0
```

`min` compares `nan < 100.0`, gets False, and keeps its first argument. So NaN
clamps to the **top** of the range: a NaN seek went to the end of the episode
(marking it watched and advancing). `int(nan)` / `int(inf)` raise instead, which
surfaces as a bare 500.

Use `_finite(value, default)` (in `main.py`, next to `_pin_hash`) *before* clamping
on anything that arrives as a float.

**Worst case is the persistent one.** `position_sec` / `duration_sec` on
`POST /api/library/{id}/progress` and the iOS batch `/api/sync/progress` are written
into `library.json` verbatim. `json.dumps` emits a bare `NaN` token — not valid JSON
— and Starlette's `JSONResponse` uses `allow_nan=False`, so it *raises*. One bad
progress POST would make `/api/library` fail for every client on every request until
the value was hand-edited out. Python's own `json.loads` accepts `NaN` on the way
back in, so the file would keep loading internally and hide the cause.

The client can generate these without malice: the seek bar computes
`(clientX - rect.left) / rect.width`, and a bar with no width yet divides by zero.

### Deleting the file that is currently playing

Both delete paths (`DELETE /api/library/{id}` and `POST /{id}/delete-files`) now call
`stop()` first when the target is `state.library_item_id` / `state.library_current_file`.
Two separate reasons, and the Windows one is the sharper:

* **Windows will not unlink a file another process holds open.** With VLC playing it,
  the unlink failed, the bytes stayed on disk, and the library row was already gone —
  producing exactly the orphan the admin Cleanup tab exists to find.
* Pulling the bytes from under a live player is an unexplained freeze for whoever is
  watching, and `state.library_item_id` kept referencing a deleted item, so the player
  went on POSTing progress for it — the runs of progress 404s in the access logs.

Bulk delete on the episode page reaches this by accident: select-all includes the
episode currently open.


### On-device TV playback must NOT clear `tv_ui_active`

When the kiosk plays library content in its own `<video>` (13.0.0), the obvious move is
to treat it like VLC and release the kiosk's screen claim. Do not. `tv_ui_active` is the
**only** thing that makes `vlc_focus_and_fullscreen` bail, keeps `background_video_loop`
from restarting the idle video underneath, and stops `_play_background_video` calling
focus. Clear it and the idle background video comes up over the film within ~3 s.

The two flags are **separate axes**:

* `tv_ui_active` — "the kiosk owns the display" (true whether it is showing the grid **or**
  playing).
* `tv_local_active` — "the kiosk's `<video>` is the active playback" (so transport keys mean
  playback, not D-pad navigation).

This is why `_remote_should_handle`'s `ok` arm reads
`state.tv_local_active or (playing and not state.tv_ui_active)` rather than the simpler
`not tv_ui_active` — during on-device playback the kiosk IS foreground, and OK still has
to mean ⏯.

### …and the 120 s idle hand-back will interrupt a film

`tv_ui_loop` hands the screen back to the background video after
`max(TV_UI_MIN_IDLE_SECS, settings.tv_ui_idle_secs)` (default 120 s) with no HID input,
when "nothing is playing". A two-hour film generates **no keypresses at all**, so before
13.0.0's guard this would have dropped the idle video over it, twice an hour.

Two halves, both needed: `tv_ui_loop` skips the hand-back entirely while `tv_local_active`
(the `continue` there is load-bearing — falling through also clears `tv_ui_active`), and
the page's heartbeat refreshes `state.tv_input_last` on every beat.

The heartbeat is also what makes the claim safe: a kiosk that crashes, reloads, or
navigates away stops beating, and `tv_ui_loop` reaps the surface after
`TV_LOCAL_STALE_SECS` (15 s). Without that reaper a dead page would hold the TV hostage —
no idle video, and every remote key relayed into the void.

### The on-device → VLC fallback needs a one-shot guard

`tvFallbackToVlc` fires from four places (prep failure, on-demand failure, terminal
hls.js fatal, repeated stalls). More than one can fire for the same dying pipeline, and
a VLC-side failure is perfectly capable of bouncing playback back at the device. The
guard is keyed on `lp.filePath` and cleared in `_lpLoadIndex`, so **one** fallback per
file and a fresh budget on the next episode.

Two details that look optional and aren't:

* It prefers `lp.lastKnownT` over `v.currentTime` when the latter reads ≲2 s. A pipeline
  that just died reports 0, and handing VLC a 0 restarts the episode from the top — then
  the next progress save writes that 0 over the real position.
* The stall trigger counts **two** consecutive kicks (`_lpStallKicks`), not one. A single
  kick is the normal recovery for a dead request, and a cold on-demand seek legitimately
  waits tens of seconds.

### SSE is fire-and-forget — you cannot "send" a command to a page that is still launching

`POST /api/tv-local/open` brings the kiosk up with `_tv_ui_show` and then broadcasts
`tv_command:open`. When the kiosk browser was **already running** that works. When it
wasn't, `_tv_ui_show` *launches* Edge/Chrome — and the broadcast goes out into a browser
that does not exist yet. There is no queue and no replay: only pages connected at that
instant receive an SSE event.

The symptom is maximally confusing, because the server side all succeeds: the endpoint
returns `{ok:true}`, the surface is claimed, `tv_ui_active` is true — and nothing plays,
until the staleness reaper quietly releases it ~15 s later. From the live log:

```
00:24:14  TV UI: showing dashboard kiosk (switch to on-device playback)
00:24:14  TV UI: launched kiosk (msedge.exe) at http://127.0.0.1/?tv=1
00:24:31  tv-local: releasing surface (heartbeat stale)
```

`_tv_local_open_pump` re-offers the command every 2 s for up to 45 s and stops on the
page's first acknowledgement. YouTube-on-TV dodges the same trap differently — it checks
`youtube_tv_seen_at` to decide relaunch-vs-hotswap — so if you add a third kiosk command
path, pick one of the two patterns deliberately.

Two traps inside the fix:

* **The reaper-hold field cannot double as the ack field.** The pump refreshes
  `tv_local_seen_at` each iteration (the page legitimately isn't beating yet), so testing
  `tv_local_seen_at > started` makes the pump satisfy its own exit condition on iteration
  two and send exactly one retry. `tv_local_ack_at` is written **only** by a real
  `/api/tv-local/state` beat.
* **The page cannot acknowledge with `_tvLocalBeat()`.** At that moment `lpPlay` hasn't
  run, so `_tvLocalLive()` is false — the beat either no-ops (pump keeps firing) or posts
  `active:false` and releases the surface out from under the play it is about to start.
  `_tvLocalOpen` posts a hand-rolled claim beat instead, and ignores a duplicate `open`
  for the same file within 60 s so a crossing retry can't restart the episode.

### "Not playing yet" and "stopped playing" look identical to a heartbeat

The kiosk's `/api/tv-local/state` beat decides claim-vs-release from `_tvLocalLive()`,
which needs `#localPlayer.lp-active` — and `lpPlay` only sets that once prep has
resolved. Between the play starting and the picture appearing the beat sees exactly what
it sees after a stop, and the naive version released the surface mid-launch.

Observed as a flap on the live state stream: claimed → released → claimed, with
`stream_status` going `buffering → idle → playing` and the log saying
`releasing surface (page reported inactive)`. It is not cosmetic — for those seconds the
remote's transport keys route to **VLC** instead of the launching player, and
`stream_status: idle` is the condition `background_video_loop` uses to decide it may put
the idle video on screen.

`_tvOpenPendingUntil` is the disambiguator: while a start is in flight a not-live beat
reports `buffering` and holds the claim. Armed by both `_tvLocalOpen` and `lpPlay` (TV
only) and cleared by `lpStop` — that last one matters, or a stop keeps claiming
"buffering" for the rest of the window and holds the TV on a dead player.

Claiming from the *start* of a play, not from first picture, is also what makes a slow
start cancellable: Back and Home are gated on `_tv_anything_playing()`, so with nothing
claimed the couch has no way to abandon a cold transcode.

### A kiosk that looks fine but runs the previous build's JavaScript

Starlette's `StaticFiles` sends `etag` and `last-modified` but **no `Cache-Control`**.
With no explicit freshness a browser falls back to *heuristic* caching — roughly 10% of
the document's age — and serves the page from disk **without revalidating**. So a TV
kiosk relaunched after an update can render the previous build's `index.html`.

The symptom gives you nothing: the page looks completely normal, the version badge reads
whatever that old build baked in, and only the behaviour is wrong. This is how "Play on
the TV still opens VLC" survived the build that fixed it — a stale page still has the old
`hlsAvailable = !TV_MODE`, which routes every TV play to VLC.

The `no_heuristic_html_cache` middleware sets `Cache-Control: no-cache, must-revalidate`
on any `text/html` response. Middleware, not a `StaticFiles` subclass: `file_response` is a
Starlette internal whose name and shape are not a stable contract across versions, and a
subclass of the static mount cannot cover `/tv` and `/admin` — both bare `FileResponse`s
with the same gap. Content-type covers all of them.

### Measuring a deploy against the process you just replaced

`apply` with `reboot:true` responds **before** the host goes down (~1.5 s later), so a
readiness loop that starts polling immediately can get its first 200 from the **old**
process and declare the box "back" without a reboot having happened. Anything measured in
that window describes the previous build. This produced a confident, wrong "the fix
didn't apply" — the fix was fine.

Wait for `/api/version` to **change**, not for the server to answer:

```bash
before=$(curl -s http://HOST/api/version | python -c "import sys,json;print(json.load(sys.stdin)['version'])")
# …apply…
until [ "$(curl -s --max-time 4 http://HOST/api/version 2>/dev/null | python -c "import sys,json;print(json.load(sys.stdin)['version'])" 2>/dev/null)" != "$before" ]; do sleep 5; done
``` `no-cache`
does **not** mean "don't cache" — it means "cache, but revalidate every time", which on a
LAN is one conditional request answered 304 with no body. `checkUiVersion` (badge vs
`/api/version`, then a cache-busting hard reload) is the second line of defence, but it
only runs **on page load** — useless for a kiosk that has been open for three days.

Which is the other half: `static/` is read from disk per request, so an updater apply
with `reboot:false` is live for every client that loads a page afterwards — except the
kiosk, which never reloads on its own. The updater broadcasts `tv_command:reload` for it,
and the page ignores that while `_tvLocalLive()` so an auto-update can't interrupt a film.

**When a frontend change "didn't work" on the TV, check the running page before you check
your code** — drive the kiosk over CDP and read the variable, or intercept the decision:

```js
const picked=[]; const real=window.pcChoose; window.pcChoose=t=>picked.push(t);
_resumeNormal("<item-id>"); window.pcChoose=real; picked;   // ["local"] or ["vlc"]
```

### Every "switch to VLC" must send `force_vlc`, or it bounces forever

`POST /api/library/{id}/play` now picks the TV surface from
`settings.tv_playback_mode`. That makes it the wrong call to use, unqualified, for
*leaving* the on-device surface — the server routes it straight back to the player you are
trying to escape, which then fails or falls back and asks again.

Two callers must always set `force_vlc:true`:

* `fcSwitchTvSurface`'s device→VLC half — the More-panel toggle IS "switch to VLC".
* `tvFallbackToVlc` — the automatic fallback. Without the flag a file the on-device player
  cannot handle is handed to VLC, routed back to on-device, fails again… The one-shot
  `_tvFallbackDoneFor` guard limits the damage to one round trip per file, but the flag is
  what makes it correct.

The mirror-image trap: **the surface decision belongs after playlist/seek resolution and
before any VLC state is touched.** Decide it earlier and the two surfaces disagree about
what plays and from where; later and you have already half-started VLC.

### Fixing the surface nobody actually uses

13.0.0–13.1.2 made the TV kiosk play on its own player *when you press Play on the kiosk*,
and shipped a setting for it. Nobody presses Play on the kiosk — the household starts
things from a phone, which posts `/api/library/{id}/play`, which hardcoded VLC. The
setting read "device", the kiosk was ready, and every real play still went to VLC.

Worth remembering when a feature "works" in testing but not in use: **check which entry
point the user actually touches.** `?tv=1` has its own play path, and it is not the one
that matters.

### On-demand carries ONE audio track — so the server's pick *is* the UI's selection

The just-in-time encoder muxes a single audio stream (`-map 0:a:<idx>`); the stream
physically contains no other. So in on-demand mode the client cannot "select" audio at
all — whatever the server muxed is what plays, and anything else the dropdown shows is a
lie.

It lied because the two sides resolved the preference differently: the server used only
the legacy per-file `audio_idx`, the client used the full chain (per-file descriptor →
per-series descriptor, newest intent winning → profile language preference → legacy
index → source default). On a **first** play there is no per-file index, so the server
muxed the source default while the dropdown showed the remembered language.

Two rules keep this honest, and both are needed:

* `_resolve_saved_audio_idx` mirrors `_lpResolveAudioSel` / `_lpResolveAudioPref` exactly.
  **Keep the three in step** — they are one algorithm in two languages.
* In on-demand mode the client takes `pendingAudioIdx` from the response's
  `default_audio_idx`. Bundle mode keeps every rendition, so the client's richer
  resolution wins there instead.

### …and switching audio raced the save it depended on

`lpSetAudio` in on-demand mode re-enters `_lpLoadIndex`, and the server was expected to
read the new pick out of `library.json` — written by a **fire-and-forget**
`_lpSaveLocalTracks`. The reload usually won that race, so the new session re-muxed the
*previous* track and the switch looked like it did nothing until you picked again. The
pick now rides on the `stream-ondemand` request (`lp._odAudioPick`, keyed by file so an
episode advance can't inherit it); `req.audio_idx` already outranks everything else
server-side. **Never depend on a fire-and-forget write being visible to the very next
request you make.**

### Which surfaces a "play" can land on

Making the TV surface configurable means any code path that starts playback by calling
`vlc()` directly now silently ignores the setting. When adding one, route it through
`/api/library/{id}/play` instead — that is where the surface is decided, and it also
carries resume resolution, track prefs, shuffle persistence and auto-prep.

`_auto_play_item` ("play when the download finishes") was exactly this: its own partial
copy of the play path, hardcoded to VLC. Deliberate exceptions that must stay on VLC:
`/api/library/play-now` and `/stream-file` stream a **still-downloading** file, and an
incomplete file must never be HLS-prepped (see the prep gates above).

Also check, whenever a second surface can own the TV, that taking it over **releases the
first**. `youtube_play` cleared `tv_ui_active` but not `tv_local_active`, and
`_remote_key_action` tests the latter first — so the remote would have driven the
backgrounded dashboard player while YouTube was on screen, with two audio streams running.

### The kiosk is signed in as somebody — that is not who is watching

The TV kiosk keeps one household profile in its `localStorage` forever. Every play it
runs therefore *looks* like that profile's, and any code in the page that reaches for
`profile.id` will credit it. But a play pushed from a phone ("On the TV") belongs to
whoever pressed it there, and `/api/library/{id}/play` says so — it puts the requesting
`profile_id` in the `open` command.

`_tvLocalOpen` dropped that field for three releases. The symptom was not "wrong name on a
chip": the viewer's **progress silently went to the kiosk's profile**, so their own resume
position never moved and their next Play restarted the episode from the beginning. It
reads exactly like a broken progress save, and it only reproduces when the play was pushed
from a phone — start the same episode from the TV itself and everything works.

The rule for anything the kiosk player does on the server's behalf: read `_lpProfileId()`,
never `profile.id`. `lp.profileId` holds the override, `lpPlay` sets it from the `open`
payload before anything else runs, `lpStop` clears it, and the heartbeat reports it so
`state.library_profile_id` agrees. `saveProgress` takes it as a 5th argument for the same
reason. Note that `/api/library/{id}/play` **is** the profile-aware path — when it routes
to VLC nothing is lost, because VLC playback is attributed server-side. Only this surface
has a client that can disagree with the server about who is watching.

### A surface with no VLC gets no VLC-derived state — and the poller will lie about it

`vlc_progress_tracker` polls `vlc_status()` and `vlc_playlist_uri()` every 2 s. During
on-device TV playback there is nothing meaningful for it to read — VLC is idle under the
kiosk — so anything it derives is either absent or stale:

- **`state.skip_offer` stayed null**, so the dashboard's Skip Intro / Skip Credits tile
  never appeared on any phone while the TV played on-device. The TV drew its own tile, so
  from the couch the feature looked fine; only a phone revealed it was missing.
- **`vlc_playlist_uri()` could stomp `library_current_file`** with whatever VLC last held.

Both are fixed by the same move: the surface that owns the playhead owns the state. The
kiosk page reports its live skip offer (and its shuffled-ness) on the heartbeat, and the
tracker now `continue`s immediately while `tv_local_active`.

The general shape to watch for: **a second surface doesn't just need the control endpoints
relayed to it — everything the server *derived* from the first surface has to come back
the other way, or it quietly reads as "off".** The Exit-Shuffle tile was the same bug in
miniature (`library_shuffle_order` is empty when shuffle lives in the page).

### A media element has no launch-time settings — the ones VLC gets at startup will be missed

`settings.vlc_start_volume` is applied once, to VLC, at host startup. A `<video>` just
comes up at `volume = 1.0`. On a host configured for 35% of a 75 cap, the kiosk played
every film about **four times louder** than the same file through VLC — and the setting
gave no clue it wasn't being honoured, because it *was* being honoured, on a surface that
wasn't playing. Night mode was the same class of problem, admitted rather than hidden.

When you add a second playback surface, audit the *launch arguments* of the first, not
just its runtime API. Anything VLC is configured with at spawn (`--audio-filter`,
`--volume`, `--sub-*`) is a setting a user believes is global.

### Routing a `<video>` through Web Audio is a one-way door

`AudioContext.createMediaElementSource(v)` permanently redirects that element's audio into
the graph. There is no un-capture: disconnect the graph and the film goes silent; let the
context suspend and the film goes silent. So the on-device night-mode compressor
(`_lpNightSync`):

- builds the graph **only when night mode is switched on** — an element that never needs
  the compressor is never captured at all;
- builds it only after `ctx.resume()` and a confirmed `ctx.state === "running"`, and
  closes the context and gives up if that fails (leaving the element untouched is always
  better than muting it);
- treats "off" as a **bypass** (`source → destination`), never a teardown;
- re-resumes on `statechange`, because an OS audio-route change can suspend it later;
- is skipped inside the iOS app entirely, where playback can hand off to a native
  `AVPlayer` that the WKWebView element's graph has no part in.

The compressor numbers are **derived from `NIGHT_MODE_PRESETS` server-side**
(`_night_mode_webaudio`, served on `GET /api/settings/night-mode`) rather than restated in
JS. That dict is already mirrored by hand into `run.py` and `watchdog.py`; a fourth copy
in another language would have drifted within a release.

### Mirroring state to a second client means the first can render it twice

Once the kiosk's skip offer reaches `state.skip_offer`, the kiosk — which is a dashboard
like any other — renders `#skipOffer` *as well as* its own in-player `#lpSkipOffer`, two
tiles stacked over the film. `renderSkipOffer` no-ops in `TV_MODE` for that reason. Same
family as the "To TV" button the kiosk was offering itself: **the page that is the TV must
opt out of the affordances aimed at the TV.**

- [BACKEND.md](BACKEND.md) — invariants enforced by `main.py`
- [DAEMON_WATCHDOG.md](DAEMON_WATCHDOG.md) — VPN guard at the process level
- [ANALYZER.md](ANALYZER.md) — Smart Skip algorithm details and fallback chain
- [STT.md](STT.md) — AI auto-subtitle pipeline
