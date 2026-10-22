"""Request bodies: the pydantic models the routes in main.py take.

Pure declarations. FastAPI builds the OpenAPI schema from these, so a field's
name, type or default here is part of the API the dashboard and the iOS app
speak (docs/API.md); tests/surface_snapshot.py pins it by hash.
"""
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

import srcevict

from backend.packslice import _PACK_FIRST_MAX_BYTES


# ── Request Models ────────────────────────────────────────────────────────────

class StreamPrepareReq(BaseModel):
    magnet: str
    title: str


class StreamReq(BaseModel):
    magnet: str
    title: str
    file_index: Optional[int] = None     # specific file to stream (None = largest video)
    torrent_hash: Optional[str] = None   # hash from /stream/prepare (skips re-adding)


class DownloadReq(BaseModel):
    magnet: str
    title: str
    series: str = ""
    season: int = 0
    episode: int = 0
    save_path: str = ""
    torrent_hash: str = ""          # pre-added hash from /api/library/prepare
    selected_file_indices: list[int] = []  # if non-empty, skip all other files
    default_visible_profiles: list[str] = []  # if non-empty, only these profiles see it by default
    download_mode: str = "now"      # "now" = download immediately; "idle" = only during idle/night window
    profile_id: str = ""            # who is asking — only meaningful alongside admin_only
    admin_only: bool = False        # start the item content-locked (elevated profiles only)
    # The TMDb entry the user actually picked, when the caller knows it. Smart
    # search opens a show page FROM a TMDb candidate, so the right answer is in
    # hand at download time — and throwing it away meant the server re-guessed
    # the show from the release name and could land somewhere else entirely
    # (a "Regular Show" season pack bound "Regular Show: The Movie"). Empty from
    # Classic search / a pasted magnet, which still fall back to the guess.
    tmdb_id: int = 0
    tmdb_kind: str = ""             # "tv" | "movie"
    # Download it even though TMDb says it isn't out yet. Honoured only for a
    # PIN-verified elevated profile (the content-lock permission) — see
    # _unreleased_gate.
    allow_unreleased: bool = False
    # ── Parallel download racing (16.0.0). All optional, all default-off, so a
    # request that doesn't set them behaves exactly as it always did.
    #
    # `auto_picked` is THE gate: racing only happens where the USER did not
    # choose a torrent. The download modal and the per-episode source sheet
    # never set it, so those paths are untouched by construction.
    auto_picked: bool = False
    # Ordered alternatives for the same content, best first. `candidates[0]`
    # must be `magnet` — it is the release the frontend auto-picked and the one
    # the normal pipeline adds; the rest are only ever added BESIDE it. Sent by
    # the client rather than re-derived here because the pick depends on page
    # state the server doesn't have (audio preference, size/seeder filters, the
    # relevance cut), and a second implementation of that ranking would
    # disagree with the one the user can actually see.
    candidates: list[dict] = []
    # TMDb runtime (minutes) and episode count for the bytes-per-minute quality
    # cross-check. 0 means "unknown", which `relquality` handles by letting the
    # release name stand — never guessed. The server falls back to
    # `_race_runtime_hint` when the client doesn't know either.
    runtime_min: float = 0
    episode_count: int = 0
    # ── Pack slicing (17.8.0). The caller asked for these episodes and this
    # torrent is a whole-season pack, so keep only their files and set every
    # other one to "skip". Stored on the item as `pack_slice` and re-resolved
    # each monitor tick until the attribution settles — see _pack_slice_apply.
    #
    # Season/episode rather than file indices ON PURPOSE: a pack's numbering is
    # not final until TMDb metadata binds and passes 2 and 3 have run, which is
    # after the add. `selected_file_indices` remains the right shape for the
    # download modal, where a person picked rows off a resolved file list.
    want_episodes: list[list[int]] = []
    # ...or whole SEASONS of it (19.12.0): a multi-season / complete-series pack
    # picked to answer "get Season 1". Every file attributed to one of these
    # seasons is kept and the rest are skipped. See _pack_slice_want_seasons.
    want_seasons: list[int] = []
    # The single-episode release to fall back to if the episode turns out not to
    # be identifiable inside the pack. Same slim shape as `candidates`.
    pack_fallback: dict = {}


class VisibilityReq(BaseModel):
    profile_id: str
    hidden: bool


class ProfileReq(BaseModel):
    name: str
    color: str = "indigo"


class ProgressReq(BaseModel):
    profile_id: str
    file_path: str
    position_sec: float
    duration_sec: float


class YouTubeReq(BaseModel):
    url: str


class YouTubeControlReq(BaseModel):
    action: str
    value: Optional[float] = None        # ±seconds (seek) / 0-100% (seek_to) / volume


class YouTubeTvStateReq(BaseModel):
    # Heartbeat + playback report posted by the /tv kiosk page. All optional —
    # an early beat (before the IFrame player is ready) carries only video_id.
    video_id: Optional[str] = None
    title: Optional[str] = None
    time: Optional[float] = None
    duration: Optional[float] = None
    volume: Optional[float] = None
    playback: Optional[str] = None


class TvLocalStateReq(BaseModel):
    # Heartbeat + playback report posted by the ?tv=1 kiosk while ITS OWN <video>
    # is the active TV playback. The on-device twin of YouTubeTvStateReq; all
    # optional so an early beat (player constructed, metadata not in yet) is valid.
    active: bool = True
    item_id: Optional[str] = None
    file_path: Optional[str] = None
    # Who this playback belongs to. LOAD-BEARING: the kiosk is signed in as one
    # household profile but a play pushed from a phone belongs to whoever pressed
    # it there, so the page reports the owning profile and the server pins
    # state.library_profile_id to it (see _set_playback_owner). Without this every
    # phone-pushed TV play was credited to the kiosk's own profile — the viewer's
    # own resume position never moved, so their next Play restarted the episode.
    profile_id: Optional[str] = None
    title: Optional[str] = None
    time: Optional[float] = None
    duration: Optional[float] = None
    playback: Optional[str] = None
    # The page's live Smart Skip offer, mirrored onto state.skip_offer so every
    # dashboard's fullscreen controls can show (and act on) it — the skip window
    # is evaluated by the page against its own <video>, not by vlc_progress_tracker
    # which has no VLC to poll on this surface. Shape matches what the dashboard
    # already renders, plus `label` so an auto-skip countdown reads identically on
    # the phone and on the TV. Sent on EVERY beat (null when there is no offer),
    # so a cleared offer clears server-side too.
    skip_offer: Optional[dict] = None
    # True while the page's playlist is a shuffled run. Shuffle on this surface
    # lives only in the page (lp.playlist order), so state.library_shuffle_order
    # is empty and the fullscreen Exit-Shuffle tile would never appear.
    shuffle: Optional[bool] = None
    # {"audio":[{id,label}], "subtitle":[{id,label}], current_audio, current_subtitle}
    tracks: Optional[dict] = None
    nav_count: Optional[int] = None      # length of the page's playlist
    nav_index: Optional[int] = None      # its position in it
    # The page's ordered playlist, sent only when it CHANGES (a new run), not on
    # every 2 s beat — it can be a hundred-plus paths. Mirrored onto
    # state.library_playlist because that is what a TV→phone handoff slices to
    # build its tail; without it the handoff carries one file and auto-advance
    # dies at the end of the episode.
    playlist: Optional[list[str]] = None
    # Waiting for data mid-episode, and seconds buffered ahead (20.25.0, #94).
    # Logged as it changes; `playback` stays "playing" through a stall.
    stalled: Optional[bool] = None
    fwd: Optional[float] = None
    # The media element's own gain as 0-100, so the dashboard's volume slider
    # reflects the surface actually making the sound.
    volume: Optional[float] = None


class TvPlaybackModeReq(BaseModel):
    mode: str


class TvLocalOpenReq(BaseModel):
    # Push a file onto the kiosk's own player — the VLC → on-device half of the
    # fullscreen More-panel switch.
    item_id: str
    file_path: str
    profile_id: str = ""
    seek: float = 0.0


class PlaybackSessionReq(BaseModel):
    """Heartbeat from an on-device player, ~every 2 s (5 s from the backgrounded
    native iOS player, to spare the battery).

    This is what makes a phone's playback visible to the household's other
    devices. All optional but `device_id` and `profile_id` — an early beat, sent
    the moment a play is requested and before metadata is in, is valid and
    wanted: a cold JIT start can take seconds, and a session nobody can see yet
    is a session nobody can pull.
    """
    device_id: str                            # this browser's localStorage UUID
    device_name: str = ""                     # "Nathan's iPhone" / auto from the UA
    profile_id: str = ""                      # whose session this is (lp.profileId or the signed-in one)
    active: bool = True                       # false ⇒ the player stopped; drop the session
    item_id: Optional[str] = None
    file_path: Optional[str] = None
    title: Optional[str] = None               # human label for the banner ("Frieren S01E12")
    position_sec: Optional[float] = None
    duration_sec: Optional[float] = None
    playback: Optional[str] = None            # playing | paused | buffering
    # "server" = streaming HLS from this host; "offline" = the iOS app playing its
    # OWN downloaded bundle while still on the LAN. Both are pullable (the host has
    # the same file), but the banner says which, because stopping an offline play
    # frees nothing on the host.
    source: Optional[str] = None
    # ── Continuity payload — everything a pull must carry across ──────────────
    # Sent only when it CHANGES (a new run / a new pick), not on every beat: the
    # playlist alone can be a hundred-plus paths and this beats at 2 s. The
    # session record keeps the last value, so the pull always has the full set
    # even though the beat that triggered it was a bare position update.
    playlist: Optional[list[str]] = None
    playlist_items: Optional[list[str]] = None   # merged-series: owning item id per path
    shuffle: Optional[bool] = None
    shuffle_scope: Optional[str] = None
    audio_sel: Optional[dict] = None             # the resolvable descriptors, not raw indices —
    subtitle_sel: Optional[dict] = None          # indices drift between releases (LIBRARY_DATA.md)
    audio_offset_ms: Optional[int] = None


class PlaybackYieldReq(BaseModel):
    """The source device's answer to a yield command: its EXACT final playhead
    plus whatever continuity it holds, posted the instant before it tears its
    player down. The puller is blocked on this (up to PLAYBACK_YIELD_WAIT_SECS),
    which is what makes a pulled-over episode land on the same frame instead of
    up to a beat behind."""
    device_id: str = ""              # must match the session's owner, else ignored
    position_sec: Optional[float] = None
    duration_sec: Optional[float] = None
    playlist: Optional[list[str]] = None
    playlist_items: Optional[list[str]] = None
    shuffle: Optional[bool] = None
    shuffle_scope: Optional[str] = None
    audio_sel: Optional[dict] = None
    subtitle_sel: Optional[dict] = None
    audio_offset_ms: Optional[int] = None


class PlaybackPullReq(BaseModel):
    session_id: str                  # the session to take over
    device_id: str = ""              # the puller, so it never yields itself
    device_name: str = ""            # shown on the source device's "moved to …" notice
    profile_id: str = ""             # the puller's profile — checked against the session's


class LibraryPlayReq(BaseModel):
    profile_id: str
    files: list[str] = []         # ordered list of absolute paths to play as a playlist
    seek_first_to: Optional[float] = None  # seek into the first file (seconds)
    shuffle: bool = False         # Shuffle Play: `files` is a temp random order — record
                                  # it so next/prev navigate the shuffle, not natural order
    shuffle_scope: str = ""       # last-used pool ("all" | "unwatched"), persisted so a
                                  # later Resume can re-shuffle the same scope
    items: list[str] = []         # merged-series play: the owning item id for each entry in
                                  # `files` (same length/order). When set, playback spans
                                  # these items and the tracker re-targets library_item_id.
    force_vlc: bool = False       # ignore settings.tv_playback_mode and play on VLC. Set by
                                  # every "switch to VLC" path — the More-panel surface
                                  # toggle and the automatic on-device fallback — which
                                  # would otherwise be routed straight back to the surface
                                  # they are trying to leave (an infinite bounce).


class MarkWatchedReq(BaseModel):
    profile_id: str
    watched: bool
    file_paths: list[str] = []    # specific paths; empty = all files in item
    season: Optional[int] = None  # if set (and file_paths empty), filter to this season


class AdminLoginReq(BaseModel):
    password: str


class AdminItemLockReq(BaseModel):
    admin_only: bool


class AdminSettingsReq(BaseModel):
    indexer_categories: Optional[str] = None
    tmdb_api_key: Optional[str] = None
    hls_ladder: Optional[list[int]] = None   # ABR down-rung heights for new preps


class ScheduledRebootReq(BaseModel):
    enabled: bool = False
    time: str = "00:00"                       # local HH:MM in `timezone`
    timezone: str = "America/Los_Angeles"     # IANA name; "" = system local
    idle_minutes: int = 15                     # idle window before a reboot fires
    catch_up_hours: int = 4                    # how long after `time` the reboot may still fire; clamped 1–24


class AutoPrepReq(BaseModel):
    mode: str = "off"                          # "always" | "idle" | "off"
    idle_minutes: int = 30                     # idle mode: prep after this many minutes with no activity; clamped 1–720
    on_activity: str = "hard"                  # idle mode: "hard" ⇒ kill in-flight on activity · "soft" ⇒ let it finish


class PlayPrepReq(BaseModel):
    enabled: bool = True                       # auto on-device prep of the playing episode (+ playlist tail) on every VLC play


class DownloadRaceReq(BaseModel):
    enabled: bool = False                      # off by default: racing multiplies bandwidth and disk
    size: int = 3                              # candidates per race (2-4)
    max_items: int = 2                         # concurrent races, box-wide
    quality_ceiling: int = 1080                # the HQ track's target tier (720/1080/2160)
    hq_upgrade: bool = True                    # keep a better copy and swap it in when it lands


class ClientLogAutoReq(BaseModel):
    enabled: bool = False                      # apps send their diagnostic log on their own


class PackFirstReq(BaseModel):
    enabled: bool = True                       # auto picker may answer a one-episode request with a season pack
    max_bytes: int = _PACK_FIRST_MAX_BYTES     # hard refusal above this torrent size, whatever the per-episode share


class StreamFocusReq(BaseModel):
    pack_focus: bool = True                    # deselect the other files in a pack while one is streamed
    sibling_kbps: int = 512                    # total KB/s the OTHER downloads share meanwhile (0 = no throttle)


class MissingContentReq(BaseModel):
    enabled: bool = True                       # library shows seasons/episodes the show has but this box hasn't downloaded
    show_unaired: bool = False                 # include not-yet-aired TMDb episodes (as "Upcoming") instead of hiding them


class PrepValidateReq(BaseModel):
    mode: str = "off"                          # "off" | "before" | "after" — auto deep-validate/remux-repair a source as it rides through bulk prep


class NetworkReq(BaseModel):
    preferred_adapter: str = ""                # physical interface NAME to make primary; "" = auto-pick


class ForcePrepReq(BaseModel):
    item_id: Optional[str] = None              # force-prep one library item; None/"" ⇒ whole library


class ForcePrepStopReq(BaseModel):
    hard: bool = False                         # True ⇒ kill the in-flight encode now; False ⇒ let the current file finish, then cancel the rest


class CacheAutopurgeReq(BaseModel):
    enabled: bool = False
    max_gb: float = 50.0                        # purge orphan offline-cache bundles once .offline_cache/ reaches this many GB; clamped 1–10000


class SourceEvictionReq(BaseModel):
    """settings.source_eviction — delete a prepped file's SOURCE, keep its bundle.

    `enabled` only arms the sweep; the dry run works while it is off, which is how
    you size the feature before letting it delete anything. Every field is clamped
    server-side by `srcevict.policy_from` (and `target_gb` is forced above
    `floor_gb`, so a disk sitting on the line can't sweep one file per tick)."""
    enabled: bool = False
    idle_days: int = srcevict.DEFAULT_IDLE_DAYS                   # since a series was last PLAYED
    never_played_days: int = srcevict.DEFAULT_NEVER_PLAYED_DAYS   # since DOWNLOAD, for a series nobody opened
    floor_gb: float = srcevict.DEFAULT_FLOOR_GB                   # free space that starts a sweep
    target_gb: float = srcevict.DEFAULT_TARGET_GB                 # free space that stops it


class ValidateFilesReq(BaseModel):
    scope: Optional[str] = None                 # None/""/"all" ⇒ whole library; else a library item id
    deep: bool = True                           # True ⇒ full ffmpeg decode (catches mid-file corruption); False ⇒ quick ffprobe header check


class BundleAuditReq(BaseModel):
    scope: Optional[str] = None                 # None/""/"all" ⇒ every prepped file; else a library item id
    dry_run: bool = False                       # report damage WITHOUT purging the bundle or re-queuing prep


class RepairFilesReq(BaseModel):
    paths: Optional[list[str]] = None           # specific files to repair; defaults to the last validation scan's damaged list
    reencode: bool = False                      # allow a lossy re-encode fallback when a lossless remux can't fix it


class CompressFilesReq(BaseModel):
    scope: Optional[str] = None                 # None/""/"all" ⇒ whole library; else a library item id
    level: str = "balanced"                     # light | balanced | maximum (ignored when `crf` is set)
    codec: str = "h264"                         # h264 | hevc
    crf: Optional[int] = None                   # Advanced override (18–32); disables the preset's down-scaling
    recompress: bool = False                    # also re-encode files this tool already compressed (20.26.7)


class CompressEstimateReq(BaseModel):
    scope: Optional[str] = None
    level: str = "balanced"
    codec: str = "h264"
    recompress: bool = False


class HlsTrimReq(BaseModel):
    heights: list[int] = []                     # ABR down-rung heights to KEEP (original is always kept)
    dry_run: bool = False                       # True ⇒ only report bytes that WOULD be freed
    scope: Optional[str] = None                 # None/""/"all" ⇒ every bundle; else a library item id


class HlsResyncReq(BaseModel):
    dry_run: bool = False                       # True ⇒ only count bundles that LOOK misaligned
    scope: Optional[str] = None                 # None/""/"all" ⇒ every bundle; else a library item id


class OndemandOnlyReq(BaseModel):
    item_id: str
    enabled: bool                               # True ⇒ on-device playback uses JIT only; no permanent bundle


class OndemandOnlyLockReq(BaseModel):
    item_id: str
    locked: bool                                # True ⇒ non-admin dashboard users can't change the on-demand-only flag


class SeriesLockReq(BaseModel):
    locked: bool                                # series-wide on-demand-only lock (series key is in the path)


class OndemandOnlyUserReq(BaseModel):
    enabled: bool                               # user-facing toggle (item id is in the path); honours the admin lock


class AutoMaintReq(BaseModel):
    # `validate` is the wire/JSON key (UI sends {fingerprint, validate}); the Python
    # attribute is renamed via alias so it doesn't shadow pydantic BaseModel.validate.
    model_config = ConfigDict(populate_by_name=True)
    fingerprint: bool = True                              # auto-run Smart Skip analysis for never-fingerprinted series while idle
    validate_files: bool = Field(True, alias="validate")  # auto deep-validate never-validated source files while idle
    bundles: bool = True                                  # auto-audit prepped HLS bundles for frozen-picture damage while idle


class SttConfigReq(BaseModel):
    enabled: bool = True
    translate: bool = True                     # also emit an English track for non-English audio


class SubsConfigReq(BaseModel):
    # Unified subtitle policy (settings.subtitles). `default_language` is the
    # preferred subtitle language (3-letter code; "" = Any). `on_by_default` is
    # the admin default for subs on/off (a profile may override). `auto_search`
    # fetches a preferred-language sub online on playback when none is embedded.
    default_language: str = "eng"
    on_by_default: bool = False
    auto_search: bool = True
    upgrade_late_subs: bool = True
    single_option: bool = True


class BookmarkReq(BaseModel):
    # One TMDb title, in the /api/tmdb/search candidate shape (bookmarks.normalize
    # keeps only what a bookmark stores).
    id: int
    kind: str
    title: str
    year: str = ""
    poster_path: str = ""
    date: str = ""


class BookmarkSeenReq(BaseModel):
    # "kind:tmdb_id" keys to clear the NEW flag on; None = every bookmark.
    keys: Optional[list[str]] = None


class EpisodeViewReq(BaseModel):
    # Which arrangement of a show this profile sees on the episode page: a TMDb
    # episode group id, or None for TMDb's own seasons (the default).
    tmdb_id: int
    group_id: Optional[str] = None


class ProfileSubsReq(BaseModel):
    # Per-profile override of the admin subs-on/off default.
    # None ⇒ inherit the admin default; True/False ⇒ force on/off for this profile.
    subtitles_on: Optional[bool] = None


class QbitLimitsReq(BaseModel):
    ratio_enabled: bool = False                # stop seeding once max_ratio is reached
    ratio: float = 1.0                         # share ratio (uploaded / downloaded); clamped 0–9998
    dl_limit_bytes: int = 0                    # global download cap, bytes/sec (0 = unlimited)
    up_limit_bytes: int = 0                    # global upload cap, bytes/sec (0 = unlimited)


class PrepPauseReq(BaseModel):
    # True ⇒ terminate the file ffmpeg is encoding right now (instant relief, partial
    # work is discarded); False ⇒ let the in-flight file finish, then hold the rest.
    kill: bool = False


class MetadataRefreshReq(BaseModel):
    tmdb_id: Optional[int] = None     # manual override; pairs with `kind`
    kind: Optional[str] = None        # "tv" | "movie"


class MetadataSetReq(BaseModel):
    """User-driven metadata correction (any profile — not admin-gated).

    mode="tmdb"   → force-bind a chosen TMDb entry (tmdb_id + kind); pulls full
                    episode data and is stamped source="manual".
    mode="custom" → hand-entered metadata; needs no TMDb key, stamped
                    source="custom". Art is supplied as absolute image URLs."""
    mode: str = "tmdb"                     # "tmdb" | "custom"
    tmdb_id: Optional[int] = None          # mode=tmdb
    kind: Optional[str] = None             # "tv" | "movie"
    # custom fields (mode=custom) ↓
    title: Optional[str] = None
    overview: Optional[str] = None
    year: Optional[str] = None
    vote_average: Optional[float] = None
    poster_url: Optional[str] = None       # absolute image URL
    backdrop_url: Optional[str] = None     # absolute image URL
    genres: Optional[list[str]] = None


class RenameReq(BaseModel):
    name: str                         # new series name (or movie title for one-offs)


class MoveReq(BaseModel):
    dest_dir: str                     # destination directory for the series' files


class ProfilePinReq(BaseModel):
    pin: str          # 4 digits to set, "" to clear
    current_pin: str = ""  # required when changing an existing PIN without admin token


class ProfileElevatedReq(BaseModel):
    elevated: bool    # whether this profile can view admin-only library items


class ProfileIndexersReq(BaseModel):
    allowed: list[str] = []   # indexer IDs this profile may search; [] = unrestricted


class ProfileAutoSkipReq(BaseModel):
    auto_skip_intro: Optional[bool] = None
    auto_skip_credits: Optional[bool] = None


class ProfileResumeModeReq(BaseModel):
    resume_mode: str  # "auto" | "prompt" | "off"


class MaxVolumeReq(BaseModel):
    max_volume: int   # 0-200; 200 = no cap


class VlcStartVolumeReq(BaseModel):
    vlc_start_volume: int   # 0-100; % of the max-volume cap applied to VLC at startup


class NightModeReq(BaseModel):
    night_mode: Optional[bool] = None   # VLC dynamic-range compressor on/off (omit to leave unchanged)
    preset: Optional[str] = None        # intensity preset: light|medium|max (omit to leave unchanged)


class SystemVolumeDefaultReq(BaseModel):
    system_volume_default: int   # 0-100; OS volume restored when YouTube stops


class YouTubeStartVolumeReq(BaseModel):
    youtube_start_volume: int   # 0-100; OS volume pre-set before a YouTube play starts


class HostVolumeReq(BaseModel):
    host_volume: int  # 0-100; live host OS mixer volume


class WindowControlReq(BaseModel):
    action: str = "toggle"        # "pause" | "resume" | "toggle"
    seconds: int = 0              # pause duration; 0 (or omitted) = until the user resumes


class SkipNowReq(BaseModel):
    type: str         # "intro" or "credits"


class AdminSkipDataReq(BaseModel):
    file_path: str
    intro_start: Optional[float] = None    # null = clear intro
    intro_end:   Optional[float] = None
    credits_start: Optional[float] = None  # null = clear credits


class BackgroundVolumeReq(BaseModel):
    volume: int       # 0-200


class BackgroundEnabledReq(BaseModel):
    enabled: bool

class ProfileSimpleUiReq(BaseModel):
    simple_ui: Optional[bool] = None   # None clears the override (back to the default)
