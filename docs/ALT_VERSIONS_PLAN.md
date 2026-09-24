# Alternate Episode Versions — Plan

> **Status: DESIGNED, DEFERRED (2026-09-24).** Nothing here is built. The design
> questions were settled with the user and are recorded below so the work can
> start from a decision rather than a blank page. When it lands it is a major
> feature (`19.0.0` at the time of writing) and its reference material moves into
> [LIBRARY_DATA.md](LIBRARY_DATA.md), [API.md](API.md), [FRONTEND.md](FRONTEND.md),
> [STREAMING.md](STREAMING.md) and [GOTCHAS.md](GOTCHAS.md) the usual way.

## 1. What it is for

Keeping more than one copy of an episode and choosing which one plays. The
motivating case is anime with competing English dubs (Vinland Saga: Netflix vs.
Sentai), but it covers any "this other release has the audio / subtitles I
want" swap, for a single episode or a whole pack.

Hard requirement: **no extra clicks for anyone who never uses it.** Every
existing download / play flow stays exactly as it is; versions are an optional
side door.

## 2. Decisions (settled with the user)

| Question | Decision |
|---|---|
| Where to switch between kept versions | **Player track menu** (a "Version" row beside audio/subs — VLC, on-device, iOS native) **and** a picker on the episode page. Mid-episode switch resumes at the same timestamp. |
| Whose default | **Per profile**, remembered per show; box-wide default until a profile picks. |
| Default choice when adding a version | **Keep old, new becomes default.** Other options on the same sheet: keep old as default · replace (delete old). |
| When "replace" deletes | **Only after the new copy is complete and validates.** A failed/stalled replacement loses nothing. |
| Pack swapped for a pack with different coverage | **Per-episode fallback.** Episodes the new pack lacks keep the old copy (never deleted); extras it adds are added. |
| Prep / storage for non-default versions | **Box default only** gets an HLS bundle and syncs to iOS; others play via VLC / JIT and prep on first pick. |
| Suggestions | **Quiet chip only** ("Dub available") when the default copy mismatches the profile's audio preference and a matching release exists. Never auto-downloads. |
| Labels | **Auto + editable** — from the release title (group, DUAL/DUB, resolution), upgraded to the probed track list; user can rename ("Netflix dub"). |
| Permissions | **Anyone adds, admin deletes** — any profile can add a version and set its own default; deleting a version needs elevated/admin. |
| iOS offline, default switched | **Keep old until new ready** — the phone keeps its offline copy, queues the new bundle, swaps when it lands, carries offline progress. |
| Merge a dub's audio track into the existing file | **Not now.** Needs frame-accurate cross-release sync; a separate project. |

## 3. Findings — what exists today

- **Two copies of one episode already "work", badly.** `_merged_series_files`
  ([main.py](../main.py), ~L6543) flattens every member item's files and sorts
  by `episodes.sort_key`; nothing collapses a repeated `(season, episode)`, so a
  show holding S01E05 twice lists it twice and plays it twice in a run.
- **Precedents to reuse, not reinvent:**
  - `_apply_race_upgrade` (~L11126) — swaps an item's files for another
    release's. It already encodes the rules a version switch needs: progress
    keys are **moved** (so `audio_sel` / `subtitle_sel` / `audio_offset_ms`
    survive) and `skip_data` is **dropped** (releases differ by frames; see
    GOTCHAS § the race-upgrade transaction).
  - **Find a replacement** for green (DV P5) files — `epReplaceGreen` passes
    `replace:true` so `_ssOwns` / `_ssOwnedEps` exempt that one episode from the
    "already in your library" suppression. The versions picker needs the same
    exemption, widened to a season for a pack swap. Its rule "never delete the
    old copy for you" is the ancestor of the verify-before-delete decision.
  - `pack_slice` — fetches one episode out of a pack; an alternate for a single
    episode found only in a pack should ride it.
  - `/delete-files` + the reaper (`reaper.py`) — removing *some* files of a
    torrent, and finishing a whole-torrent delete.
  - `reltracks.lang_facts` / `classify_audio` / `track_rank` — the auto label
    and the suggestion chip's mismatch test.
  - `_release_key` / `_release_group` (~L9730) — release identity for "same
    release across episodes" defaults.
  - `_audio_sig_of` / `_sub_sig_of` + `series_*_prefs.groups` — track picks are
    already remembered **per layout signature**, so a show stitched from two
    releases restores each version's own audio pick with no new work.

## 4. Design

### 4.1 Data model

A version is **an ordinary library item** — its own torrent, its own `files`,
same `series`. So races, pack-slicing, the dead-swarm retry, the reaper and
source eviction all apply unchanged.

- **Slot** = `(series_key, season, episode, bucket)`. Files sharing a slot are
  versions of one another. No explicit link is stored; it is derived.
- `item.version` (optional): `{label, label_custom, added_at, replaces?}`.
  `replaces = {item_id, paths, policy: "keep"|"keep_default"|"delete"}` is the
  pending instruction the download monitor executes per slot once the new
  file is complete + validated.
- `settings.version_defaults[series_key]` — box default: `{release, slots:{"s:e": path}}`.
- `profile.version_prefs[series_key]` — `{release, slots:{"s:e": path}, updated_at}`.
- `file.tracks_label` (optional) — probed audio/sub summary for labelling.

**Resolution order** for which version plays: profile slot pick → profile
release pick (if that release has the slot) → box slot/release default →
newest added. Missing/evicted-and-unplayable versions are skipped.

**Progress and watched are per slot**, read as the newest record across the
slot's paths (`updated_at`), and a switch writes the position onto the target
path. A version you haven't played must not read as "unwatched".

A new leaf module **`versions.py`** (stdlib only, no `main` import, tests in
`tests/test_versions.py`) holds the pure parts: slot grouping, default
resolution, per-slot progress merge, and "may this superseded file be deleted
now" (complete + validated + not the last copy + policy says delete).

### 4.2 The collapse — the bulk of the work and the risk

Every consumer that assumes one file per episode must see one row per slot
(the viewer's resolved version, with `versions[]` attached). Audit list:

- `_merged_series_files`, `GET /api/library/series/{key}`, `/files`
- `find_series_resume_hint` / `find_resume_hint`, next-episode, playlists,
  `_item_all_paths`, `library_series_map` / `library_nav_order`
- shuffle pool (`_shuffle_pool_for_active`)
- missing-episode / coverage / gap fill, `_pack_available`, `_ssOwns`
- prep enqueue (`_enqueue_library_prep`) — default version only
- watched counts, section counts, TV UI (`?tv=1`), iOS sync manifest
- source eviction series clocks (touching any version touches the show)

### 4.3 Flows

- **Entry:** episode-row overflow → **Versions…**; season header → **Switch
  release…**. Both open the existing show-page source sheet in replace mode,
  showing the current copy's tracks for comparison.
- **Add sheet:** Keep old · new is default *(preselected)* / Keep old as
  default / Replace (delete old once verified). Downloads as a normal item.
- **Pack replace:** per slot; old pack files are deleted only for slots the
  new pack covers, via `/delete-files`; a fully superseded torrent goes through
  the reaper. Uncovered slots keep the old copy.
- **Player:** "Version" row in the track menu. Switching writes the pref and
  relaunches at the same timestamp (approximate — releases can be offset by a
  few seconds).
- **Suggestion chip:** lazy search when the show page opens, cached per show
  for 7 days; shown only on an audio-preference mismatch.
- **iOS:** native player Version row; `OfflineStore` keeps the old bundle until
  the new default's bundle downloads, then swaps and carries progress.

## 5. Phasing

1. **Server:** `versions.py` + tests, data model, the collapse audit (§4.2),
   API (list versions, switch, set default, rename, replace-policy execution).
2. **Dashboard:** Versions sheet, replace-mode search, add sheet, player
   Version row (on-device + VLC).
3. **Suggestion chip.**
4. **iOS:** native Version row + offline swap (Swift + `www/` → plain
   `./build-ipa.sh`).

Each phase is shippable on its own with its own version bump.

## 6. Open risks

- Missing one consumer in §4.2 shows up as a duplicated or skipped episode in a
  run — the test suite for `versions.py` can't catch it; walk a two-version
  show through every surface by hand.
- A same-timestamp switch between releases with different intros lands a few
  seconds off. Acceptable; don't try to align without evidence (that is the
  deferred audio-merge problem).
- Races and version adds on the same slot: a version item that races is fine,
  but `replaces` must follow the winner, not the entry that started it.
