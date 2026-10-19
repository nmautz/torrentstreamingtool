"""Pack slicing: one episode out of a whole-season torrent.

The knobs only, for now. The design notes and the functions (`_pack_slice_want`,
`_pack_slice_apply`, `_pack_slice_settle`, ...) are still in main.py under
"Pack slicing"; they move here once the library/state core they lean on has
left main.py. Re-exported by main, so every caller there is unchanged.
"""

# Non-video files ride along free (a Subs/ folder, an .nfo) as long as they are
# small — they are what `_discover_local_subs` goes looking for, and losing them
# means an episode that arrives without its own subtitles. Anything bigger is an
# extra nobody asked for.
_PACK_EXTRA_MAX_BYTES = 8 * 1024 * 1024
# How long an unresolved slice may hold the whole torrent at priority 0 before we
# give up on it and fall back to a single-episode release. Generous: it has to cover
# a TMDb round trip, the anime-mapping fetch, and a slow metadata resolve.
_PACK_SLICE_GRACE_SECS = 300
# The ceiling ships as an admin setting; this is its default. It clears the
# long-running-anime Blu-ray packs the feature exists for (Hunter x Hunter's complete
# run is 144.7 GB) and stops only genuinely absurd complete-franchise torrents, which
# cost real qBit bookkeeping for one episode.
_PACK_FIRST_MAX_BYTES = 200 * 1024 ** 3
