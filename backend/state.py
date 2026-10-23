"""AppState: the server's in-memory state, and `state`, its one instance.

Never reassigned (code mutates its fields), so `from backend.state import
state` is safe everywhere. What a field means is in docs/BACKEND.md § AppState
fields; `state_snapshot` (main.py, SSE Helpers) is what clients see of it.
"""
import asyncio
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ── Global State ──────────────────────────────────────────────────────────────

@dataclass
class AppState:
    vpn_secure: bool = True
    vpn_status_text: str = "Checking…"
    # VPN kill-switch reach: True ⇒ block the whole UI behind the overlay when the
    # VPN drops; False ⇒ only kill qBit (qBit is killed either way). Mirror of
    # settings.vpn_killswitch.block_ui; seeded at lifespan, updated by the admin POST.
    vpn_block_ui: bool = True
    # VPN verification mode: "mullvad" (require the CLI's Connected), "generic"
    # (any VPN tunnel interface up — provider-agnostic), or "off" (kill-switch
    # disabled). Mirror of settings.vpn_killswitch.mode; seeded at lifespan,
    # updated by the admin POST. See vpncheck.py.
    vpn_mode: str = "mullvad"
    # Admin-selected primary network adapter (interface *name*, "" = auto). The
    # network_adapter_redirect middleware bounces a client that connected on any
    # other adapter's IP to this adapter's current IP. Mirror of
    # settings.network.preferred_adapter; seeded at lifespan, updated by the POST.
    preferred_adapter: str = ""
    jackett_ok: bool = True               # last known Jackett HTTP reachability
    # Indexer degradation. Set from /api/search when Jackett reports that some
    # configured indexers errored while others still returned results — search
    # stays usable, but we flag it. Drives a non-specific user banner (never
    # names which indexers); the admin Indexers tab shows the per-indexer detail.
    indexers_degraded: bool = False
    indexers_total: int = 0
    indexers_failing: int = 0
    indexer_health: list = field(default_factory=list)  # [{id, name, ok, error, results}] — last observed per-indexer health (search-derived or test-derived)
    indexer_health_checked_at: float = 0.0
    active_hash: Optional[str] = None
    active_title: Optional[str] = None
    active_file: Optional[Path] = None
    stream_status: str = "idle"   # idle | buffering | playing | error
    progress: float = 0.0
    downloaded_mb: float = 0.0
    total_mb: float = 0.0
    dl_speed_bps: int = 0
    ul_speed_bps: int = 0
    stream_task: Optional[asyncio.Task] = None
    library_play_task: Optional[asyncio.Task] = None  # in-flight VLC handoff for a library play / prev / next
    library_item_id: Optional[str] = None
    library_profile_id: Optional[str] = None
    # Identity of the profile that *started* the current playback, snapshotted so
    # SSE clients can show "who's watching" without a library lookup. Progress is
    # always attributed to library_profile_id regardless of who drives the shared
    # VLC controls afterward — these just make the owner visible. Cleared on stop.
    library_profile_name: str = ""
    library_profile_color: str = ""
    library_item_file_count: int = 0                     # total files in the playing item
    library_playlist: list = field(default_factory=list)  # ordered file paths
    # The playing item's FULL natural file order — the same list `_nav_order` and
    # `_item_all_paths` already fall back to, cached in state so `state_snapshot`
    # can report it without a library read. Deliberately the whole ITEM, not the
    # played `playlist`: playing one episode of a season pack still navigates the
    # whole pack (long-standing behaviour). `library_playlist` can't serve this —
    # it's the remaining VLC tail and is rebuilt as `all_paths[idx:]` on every
    # advance, so its index is permanently 0 and its length falls by one per
    # episode. Set at play time, never shrunk, cleared on stop; the shuffle and
    # merged-series orders below outrank it (see `_nav_order`).
    library_nav_order: list = field(default_factory=list)
    # Shuffle Play: the full temp random order of file paths for the active item.
    # Empty when not shuffling. When set, next/prev (and the credit "skip to next"
    # auto-advance) navigate this order instead of the item's natural file order —
    # and unlike library_playlist (which is the remaining VLC tail and shrinks on
    # advance), this keeps the *whole* order so prev can walk back through history.
    library_shuffle_order: list = field(default_factory=list)
    # Pool the live shuffle order was drawn from ("all" | "unwatched"). Travels
    # alongside library_shuffle_order so a VLC→device handoff can carry it (and
    # keep the persisted Resume-on-shuffle scope intact). Empty when not shuffling.
    library_shuffle_scope: str = ""
    # Merged-series playback: a single show whose episodes live in *separate*
    # library items (each a single-episode torrent) is played as one playlist that
    # spans those items. `library_series_map` maps each playlist path → its owning
    # item id; the progress tracker re-points `library_item_id` at the owner of the
    # file VLC is actually playing so progress/skip/track-prefs key off the right
    # item as playback crosses item boundaries. `library_series_order` is the full,
    # stable ordered path list (like library_shuffle_order — doesn't shrink) so
    # prev/next and the credit auto-advance can span items. Both empty for a normal
    # single-item play. See docs/GOTCHAS.md § cross-item series playback.
    library_series_map: dict = field(default_factory=dict)
    library_series_order: list = field(default_factory=list)
    library_current_file: Optional[str] = None            # path VLC is playing now
    # Last position/duration/file observed while VLC was genuinely mid-playback.
    # At end-of-media VLC reports time=0 AND length=0, so `vlc_time`/`vlc_duration`
    # are useless for crediting the file that just finished — `_finalize_stopped_file`
    # bails on `dur <= 0`. These keep the last good pair so the EOF handler can
    # finalise what was actually watched. See `_handle_playback_ended`.
    last_play_pos: float = 0.0
    last_play_dur: float = 0.0
    last_play_file: Optional[str] = None
    # Consecutive `background_video_loop` polls that found VLC stopped while the
    # app still believed something was playing. Two in a row (~6 s) is the
    # end-of-media signal; one can be a track change or a seek landing.
    vlc_stopped_ticks: int = 0
    # Monotonic stamp of the most recent `in_play` handoff, and whether VLC has
    # been observed actually playing since it. Together they separate "the media
    # ENDED" from "VLC hasn't OPENED it yet" — two states that look identical in
    # `status.json` (both report `stopped` with length 0). Without them the
    # end-of-media detector credited a slow demuxer spin-up as a finished film
    # ~6 s after the handoff and handed the screen back to the idle background
    # video. See `_handle_playback_ended` / `background_video_loop`.
    vlc_handoff_at: float = 0.0
    vlc_ever_played: bool = False
    # Absolute path of the configured idle background video while it is on
    # screen. `vlc_progress_tracker` reads VLC's current URI every 2 s and used
    # to adopt it as `library_current_file` unconditionally — during a stream's
    # buffer wait that is still the BACKGROUND video, so the tracker wrote the
    # bg clip's position into the viewer's watch history and left every
    # `library_current_file`-guarded step (resume seek, track prefs) comparing
    # against the wrong file. See GOTCHAS.md.
    background_video_path: str = ""
    downloading_count: int = 0                            # active library downloads (ALL, incl. admin-only — drives host-busy/idle gating)
    downloading_count_visible: int = 0                    # active downloads a non-elevated viewer may know about (excludes admin_only) — drives the user-facing badge
    racing_count: int = 0                                 # items running a parallel download race (ALL, incl. admin-only)
    racing_count_visible: int = 0                         # …and the subset a non-elevated viewer may know about
    play_when_ready_item_id: Optional[str] = None        # auto-play this item on download complete
    play_when_ready_profile_id: Optional[str] = None
    play_when_ready_file_path: Optional[str] = None      # if set, wait for this specific file
    current_audio_track: int = -1                         # last track ID sent to VLC
    current_subtitle_track: int = -1                      # last track ID sent to VLC
    # ES ID → {"lang","ai","path"} for the current file's subtitle tracks, as
    # annotated by _load_all_local_subs / _download_and_attach_subtitle. Lets a
    # manual VLC pick be persisted as a resolvable descriptor (subtitle_sel)
    # instead of just a bare ES ID — the descriptor is what the on-device
    # player restores from. Rebuilt on every sidecar-load pass.
    vlc_sub_meta: dict = field(default_factory=dict)
    track_pref_applied_file: Optional[str] = None         # path for which track prefs were last applied
    vlc_time: int = 0                                     # VLC current position (seconds)
    vlc_duration: int = 0                                 # VLC total duration (seconds)
    vlc_volume: int = 100                                 # VLC volume 0-200 (100 = normal)
    prepare_hash: Optional[str] = None                    # hash added by /stream/prepare, pending user selection
    # Info-hashes of the candidates a /api/stream/race is currently running.
    # In-memory and transient (the race lives inside one request), but it is the
    # anchor everything else hangs off: admin Cleanup must not call them orphans
    # while the race runs, and /api/stop has to tear them down if the user
    # bails mid-race. A crash loses this set, which is what the `streamlink-race`
    # qBit tag + the startup sweep exist to catch.
    race_hashes: set = field(default_factory=set)
    # Bumped to abandon an in-flight stream race: the race loop checks it every
    # tick. DELETE /api/stream/race used to drop the torrents but leave the loop
    # polling them until its own timeout, then answer a 504 nobody was waiting for.
    race_gen: int = 0
    # The detached "Play now" job (/api/library/stream-now): race the candidates,
    # then hand the winner to play-now. Cancelled by Stop and by any other play,
    # so a race that finishes late can never take the screen back.
    stream_now_task: Optional[asyncio.Task] = None
    # The detached `_finalize_stopped_file` task `stop()` fires. `stop()` clears
    # the playback fields SYNCHRONOUSLY but flushes the outgoing file's final
    # position in the background, so "library_item_id is None" does NOT mean the
    # progress write has landed. Anything that renames or deletes that file
    # straight after a stop must await this first, or the finalize resurrects
    # the old path's progress key afterwards. See `_apply_race_upgrade`.
    finalize_task: Optional[asyncio.Task] = None
    skip_offer: Optional[dict] = None                     # {"type": "intro"|"credits", "end_at": s, "next_item_id": id?, "next_file_path": p?}
    skip_offer_file: Optional[str] = None                 # path the current skip offer corresponds to
    skip_countdown: Optional[dict] = None                 # {"type", "file_path", "n"} active auto-skip countdown (TV marquee)
    skip_countdown_task: Optional[asyncio.Task] = None    # in-flight countdown coroutine
    resume_offer: Optional[dict] = None                   # {"position_sec": N, "file_path": "..."} when resume_mode="prompt"
    analysis_jobs: dict = field(default_factory=dict)     # series_key → {status, stage, current, total, message, item_ids, started_at, finished_at}
    # Ring buffer of Smart Skip events (most recent first). Each entry:
    # {ts, level: "info"|"warn"|"error", series_key, item_id, file_path,
    #  error_code, message}. Surfaces in the admin "Smart Skip" tab so the
    # operator can see *why* fingerprinting failed for individual files without
    # tailing logs/streamlink_app.log.
    analyzer_log: deque = field(default_factory=lambda: deque(maxlen=200))
    background_playing: bool = False                      # True when the idle background video is the active VLC playlist
    user_volume_before_bg: int = 100                      # snapshot of state.vlc_volume taken when bg first took over
    # Non-admin "let me use the computer" override. While set, the idle background
    # video loop AND every focus/minimize/fullscreen assertion stand down so VLC
    # stops grabbing the screen and other windows stop being minimized. Values:
    # 0.0 = active (normal behaviour); a future time.time() = paused until then;
    # float('inf') = paused until the user resumes. See window_mgmt_paused() and
    # POST /api/window-control.
    window_mgmt_paused_until: float = 0.0
    vlc_night_mode: bool = False                          # VLC compressor (dynamic-range) filter; persisted in library.json → settings.vlc_night_mode, seeded at lifespan startup. VLC must relaunch to apply (no runtime HTTP command for audio filters)
    vlc_night_mode_preset: str = "medium"                 # intensity preset (light|medium|max); persisted in library.json → settings.vlc_night_mode_preset, remembered independently of the on/off toggle
    subtitle_default_language: str = "eng"                # preferred subtitle language (settings.subtitles.default_language; "" = Any), mirrored here for state_snapshot/UI defaults; seeded at lifespan, updated by the admin subtitles POST
    subtitle_upgrade_late: bool = True                    # settings.subtitles.upgrade_late_subs, mirrored for the on-device upgrade poller
    subtitle_single_option: bool = True                   # settings.subtitles.single_option, mirrored for client/UI
    missing_content_enabled: bool = True                  # settings.missing_content.enabled — library surfaces not-yet-downloaded seasons/episodes; mirrored for state_snapshot
    missing_content_unaired: bool = False                 # settings.missing_content.show_unaired — render future-dated TMDb episodes as "Upcoming" instead of hiding them
    download_race_enabled: bool = False                   # settings.download_race.enabled — mirrored so the UI knows whether to send candidate shortlists
    download_race_ceiling: int = 1080                     # settings.download_race.quality_ceiling — the HQ track's target tier
    # ── Stream focus ─────────────────────────────────────────────────────────
    # Set while VLC is playing a library file that has NOT finished downloading.
    # Two effects hang off it, both unwound the moment the file completes or
    # playback stops: the other files in the SAME torrent are deselected (so
    # sequential order means "this file from its head" instead of "the pack from
    # episode one"), and every OTHER downloading torrent is throttled. The
    # authoritative per-item half is persisted as item["stream_focus"] because
    # the download scheduler is the single writer of file priorities; these
    # fields are the in-memory anchor for the throttle + the unwind.
    stream_focus_item: Optional[str] = None               # library item whose file is being streamed ahead of completion
    stream_focus_path: Optional[str] = None               # …the file itself (abs path)
    stream_focus_hash: Optional[str] = None               # …and its torrent, the one hash the throttle must never touch
    stream_throttled_count: int = 0                       # how many other torrents currently carry a stream-focus download limit
    stream_focus_pack_enabled: bool = True                # settings.stream_focus.pack_focus — mirrored
    pack_first_enabled: bool = True                       # settings.pack_first.enabled — mirrored so the UI knows it may reach for a pack
    client_log_auto: int = 0                              # settings.client_log_auto — minutes between the app's automatic log uploads; 0 = off
    pack_first_max_bytes: int = 200 * 1024 ** 3           # settings.pack_first.max_bytes — the size above which it may not
    stream_focus_sibling_bps: int = 512 * 1024            # settings.stream_focus.sibling_kbps × 1024 — total budget the OTHER torrents share while streaming (0 = no throttle)
    shotscan_current: str = ""                            # basename of the file the shot-boundary credit scan is decoding ("" = idle); surfaced in the admin Activity tab so the most expensive pass in the app is never invisible
    sub_auto_ai_path: str = ""                            # abs path of the AI sidecar currently auto-applied in VLC ("" = none); the upgrade loop watches this and swaps in a real sub when one arrives
    sub_manual_at: float = 0.0                            # time.time() of the last manual subtitle pick on the TV — a background OpenSubtitles fetch that finishes AFTER this must not override the viewer's choice
    last_activity: float = 0.0                            # time.time() of last user-initiated interaction (drives scheduled-reboot idle check)
    prep_paused: bool = False                             # True ⇒ bulk stream-prep jobs hold (set by the non-admin Pause control / auto-prep falling edge)
    admin_prep_stop: bool = False                         # True ⇒ admin force-prep ("admin" queue) jobs cancel at the gate (set by the admin Stop control). Cleared when a new force-prep batch starts. Independent of prep_paused — force-prep ignores the bulk gate + activity-kill by design
    auto_prep_engaged: bool = False                       # True while the unified auto_prep_loop has prep running (always mode OR idle trigger) — edge flag for resume/pause
    play_prep_task: Optional[asyncio.Task] = None         # the chain task prepping the currently-playing series' tail for on-device (settings.play_prep); cancelled + replaced on each new VLC play
    download_idle_open: bool = False                      # last computed idle/night DOWNLOAD window state (set by download_scheduler_loop) — drives the "waiting for idle" UI
    download_idle_configured: bool = False                # True if Automatic Stream Prep is enabled (auto_prep mode != off) — so idle-only downloads have a window to run in
    idle_prep_on: bool = False                            # cached: auto_prep mode == "idle" (set each auto_prep_loop tick) — lets the activity hook decide cheaply
    idle_prep_hard: bool = False                          # cached: auto_prep idle mode with on_activity == "hard" (set each tick) — only then does the activity hook kill the in-flight encode immediately
    sys_status: dict = field(default_factory=dict)        # latest CPU/GPU/RAM/network sample + ok/degraded/overloaded classification (set by system_monitor_loop)
    cache_autopurge_last: dict = field(default_factory=dict)  # last auto-purge result (set by cache_autopurge_loop): {at, deleted, bytes_freed, total_bytes_before} — drives the admin card's "last run" line
    # ── Source-file validator (admin System tab) ─────────────────────────────
    # Decodes library video files with ffmpeg to find CORRUPT/unreadable SOURCES,
    # independent of qBit's piece recheck (a torrent only proves the bytes match
    # its hashes — it happily seeds a perfect copy of an already-damaged file).
    # Snapshot of the current/last run for the admin card to poll; the runner
    # coroutine task + the in-flight proc handle so Stop can terminate a long
    # deep decode mid-file.
    file_validation: dict = field(default_factory=dict)   # {running, deep, scope, total, scanned, current_name, damaged:[…], missing:[…], started_at, finished_at, stopped, error}
    file_validation_task: Optional[asyncio.Task] = None
    file_validation_stop: bool = False
    file_validation_proc: Optional["asyncio.subprocess.Process"] = None
    # Best-effort in-place repair of the files the validator flagged (remux, then
    # optional lossy re-encode); replaces the original only when the candidate
    # decodes clean, and purges the stale HLS bundle. Same job-state shape.
    file_repair: dict = field(default_factory=dict)       # {running, reencode, total, scanned, current_name, repaired:[…], failed:[…], started_at, finished_at, stopped, error}
    file_repair_task: Optional[asyncio.Task] = None
    file_repair_stop: bool = False
    file_repair_proc: Optional["asyncio.subprocess.Process"] = None
    # In-place lossy re-encode of source files to reclaim disk space (admin
    # Storage & Compression card). Replaces each source only when the re-encode
    # decodes clean AND came out smaller, then purges the stale HLS bundle. Same
    # job-state shape as the validator/repair.
    file_compression: dict = field(default_factory=dict)  # {running, scope, level, codec, total, scanned, current_name, results:[…], bytes_before, bytes_after, started_at, finished_at, stopped, error}
    file_compression_task: Optional[asyncio.Task] = None
    file_compression_stop: bool = False
    file_compression_proc: Optional["asyncio.subprocess.Process"] = None
    # Normalised paths of source files currently mid-compress. A robust playback
    # LOCK: while a file is here, every playback/read entry point (VLC play,
    # on-device prep, on-demand JIT, clip) refuses it, and the compressor stops
    # any in-flight playback of it first — because the in-place replace (os.replace)
    # fails on Windows while anything holds the file open (WinError 5).
    compressing_paths: set = field(default_factory=set)
    # Item ids flagged "on-demand stream only": on-device browser playback uses the
    # just-in-time pipeline and NO permanent HLS bundle is ever built (saves disk).
    # Seeded at lifespan from library.json (item["ondemand_only"]) and rebuilt on
    # toggle. Does NOT affect VLC (TV) playback — that reads the source directly.
    ondemand_only_items: set = field(default_factory=set)
    # Idle-gated automatic background validation (separate from the admin scan so
    # they never collide). Walks files whose persisted `validation` verdict is
    # missing/stale, one at a time, ONLY while the box is idle — and persists each
    # verdict into library.json, so it RESUMES after a restart/auto-update instead
    # of starting over (the resumable counterpart to the in-memory admin scan).
    auto_validate: dict = field(default_factory=dict)     # {running, total, scanned, current_name, started_at}
    auto_validate_task: Optional[asyncio.Task] = None
    auto_validate_stop: bool = False
    auto_validate_proc: Optional["asyncio.subprocess.Process"] = None
    # ── HLS bundle integrity audit (structural, no decode — see bundlecheck.py) ──
    bundle_audit: dict = field(default_factory=dict)      # {running, scope, auto, total, scanned, current_name, damaged:[…], repaired, stopped, error, started_at, finished_at}
    bundle_audit_task: Optional[asyncio.Task] = None
    bundle_audit_stop: bool = False
    # Source eviction (delete a prepped file's source, keep its bundle).
    source_eviction: dict = field(default_factory=dict)   # {running, deleted, bytes_freed, error, manual, stopped, started_at, finished_at}
    source_eviction_last: dict = field(default_factory=dict)  # the last completed sweep, for the admin card
    source_eviction_task: Optional[asyncio.Task] = None
    source_eviction_stop: bool = False
    # ── YouTube-on-TV (browser playback on the host display, remote-controlled) ──
    youtube_active: bool = False                          # True while a YouTube video is the active TV playback (browser, not VLC)
    youtube_video_id: Optional[str] = None               # 11-char YouTube id currently loaded on the TV page
    youtube_playback: str = ""                            # last player state from /tv: unstarted|buffering|playing|paused|ended
    youtube_tv_seen_at: float = 0.0                       # time.time() of last /tv heartbeat (drives relaunch-vs-load decision)
    system_volume_before_yt: Optional[int] = None        # OS volume (0-100) snapshot at YouTube start; falls back when no default configured
    system_volume_before_trailer: Optional[int] = None   # OS volume (0-100) snapshot when a TV-kiosk inline trailer ducked the host mixer; restored on close (or adopted by youtube_play on Watch-on-TV handoff)
    # ── TV UI (Firestick-style dashboard kiosk on the host display) ──
    tv_ui_active: bool = False                            # True while the dashboard kiosk should hold the screen; gates every VLC focus assertion (see docs/REMOTE.md)
    tv_input_last: float = 0.0                            # time.time() of the last HID input (key/click/move) — drives the TV UI idle hand-back
    ambient_active: bool = False                          # True while the kiosk shows the idle slideshow instead of the grid. Only meaningful with tv_ui_active; see _ambient_visible
    idle_mode: str = ""                                   # "slideshow" | "video" | "off": what the TV shows when idle (settings.idle_screen.mode, mirrored for the hook thread)
    host_volume_expected: Optional[int] = None            # last OS volume WE set/accepted (0-100). Only consumed by the OPT-IN remote_volume_guard (REMOTE_VOLUME_GUARD=1, Windows) which reverts any other change
    # ── On-device TV playback (the kiosk's own <video>, host display) ──
    # The THIRD playback surface, alongside VLC and YouTube-on-TV. Deliberately a
    # SEPARATE AXIS from tv_ui_active rather than a replacement for it: the kiosk
    # must keep holding the screen while its player runs, so tv_ui_active STAYS
    # TRUE throughout (it is what makes vlc_focus_and_fullscreen and
    # background_video_loop stand down). See docs/GOTCHAS.md.
    tv_local_active: bool = False                         # True while the kiosk's <video> is the active TV playback
    tv_local_playback: str = ""                           # last player state from the kiosk: playing|paused|buffering|ended
    tv_local_seen_at: float = 0.0                         # time.time() of the last /api/tv-local/state heartbeat (stale ⇒ the page died). ALSO refreshed by _tv_local_open_pump to hold the reaper off during a cold kiosk launch — so it does NOT mean "the page replied"
    tv_local_ack_at: float = 0.0                          # time.time() of the last beat that genuinely came FROM the page — the only thing _tv_local_open_pump treats as acknowledgement
    # The kiosk player's audio/subtitle tracks + current picks, reported on the
    # heartbeat. Lets GET /api/vlc/tracks answer for the on-device surface in the
    # SAME shape VLC uses, so the dashboard's track dropdowns (and the Clip
    # timestamp) work against either surface with no client-side branching.
    tv_local_tracks: dict = field(default_factory=dict)
    # {"count": N, "index": i} — the kiosk player's OWN playlist position, which
    # can be a cross-item merged-series run. Overrides the VLC-derived nav pair in
    # state_snapshot while this surface is active, so the dashboard's prev/next
    # episode controls and "3 / 25" counter work against it.
    tv_local_nav: dict = field(default_factory=dict)
    tv_local_shuffle: bool = False                        # the kiosk player's run is shuffled (shuffle lives in the page, not in library_shuffle_order)
    tv_local_item_id: Optional[str] = None                # library item currently on the kiosk player
    tv_local_file_path: Optional[str] = None              # and which file of it
    # Which surface a TV library play uses: "device" (kiosk <video>) | "vlc".
    # Persisted in library.json → settings.tv_playback_mode, seeded at lifespan.
    # VLC stays the automatic fallback whichever way this is set.
    tv_playback_mode: str = "device"
    # ── Cross-device playback sessions ("playing elsewhere" + pull-over) ──
    # Every on-device player (phone / tablet / another browser / the iOS app,
    # foreground or backgrounded-native) beats into /api/playback/session while
    # it plays. This is the ONLY place the host learns that a device is watching
    # something: a browser <video> is entirely client-side, and before 17.8.0 the
    # only trace it left was a progress POST every 15 s. Keyed by the client's own
    # localStorage UUID. DELIBERATELY EPHEMERAL — in-memory only, never written to
    # library.json: a session is a live fact about right now, and a host restart
    # genuinely does end every session it knew about. See docs/STREAMING.md § 8.
    playback_sessions: dict = field(default_factory=dict)
    # Bumped whenever the *material* shape of the session set changes (a session
    # opens/closes/changes file or play-state) — NOT on a position-only beat.
    # Rides in state_snapshot so a dashboard knows to re-fetch its own filtered
    # list; the broadcast itself carries no session detail, because `state` goes
    # to every profile and the list is same-profile-only. See GET /api/playback/sessions.
    playback_sessions_rev: int = 0
    # ── Auto-updater (transient view of the current update operation) ──
    # All persisted updater state lives in library.json → settings.autoupdate.
    # These fields just expose the live state of an in-flight check/apply so
    # the admin UI can render progress without polling git directly.
    updater_phase: str = "idle"            # idle | checking | applying | setup | restarting | error
    updater_message: str = ""              # human-readable detail for the current phase
    updater_busy: bool = False             # True while a check/apply is running (admin UI disables buttons)
    updater_last_output: str = ""          # last 8 KiB of setup.py stdout/stderr — for diagnostics
    sse_queues: list = field(default_factory=list)


state = AppState()
