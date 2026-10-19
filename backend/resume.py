"""Where Resume lands: one algorithm, per section, for every caller.

Pure: reads an item's files and a profile's progress, never the library or the
network. The rules are in the comment block below.
"""
from pathlib import Path
from typing import Optional

import episodes


# ── Resume ───────────────────────────────────────────────────────────────────────
# ONE resume algorithm, used by every caller. There used to be two — an item-level
# one that followed `last_file` forward, and a merged-series one that picked the
# most-recently-touched unfinished file anywhere in the show — and they disagreed,
# which is why the Resume button could offer one episode and play another. Worse,
# neither was section-aware: Attack on Titan's resume landed on **Junior High
# ep 2** because a half-watched spin-off episode outranked the main run.
#
# The rules, in one place:
#   * Resume is computed **per section** (episodes.sections_for). A section never
#     resumes into another one — finishing S04 does not roll on into Extras.
#   * Within a section it follows the viewer FORWARD from their anchor (the file
#     they last had open), never backwards to an episode they deliberately
#     skipped, and steps past it once it is completed.
#   * The show-level hint is simply its most-recently-played section's hint, so
#     the tile's Resume button and the play it triggers are one computation.


def _resume_anchor(files: list, fp_for, last_file: str = "") -> Optional[int]:
    """Index of the file the viewer last had open in this list, or None.

    The anchor is whichever file carries the newest ``updated_at`` — data every
    progress write already produces, and the only signal available across a
    merged series whose members each keep their own ``last_file``. A stored
    ``last_file`` still wins when it names a file in this list that has no
    progress yet (started, then stopped inside the 5 s save window).

    **Ties are the normal case, not an edge case.** `_now_iso()` has one-second
    resolution and `mark_watched` stamps every file it touches, so "mark the
    season watched" gives twenty-five episodes the identical timestamp — and any
    resume in that same second sees a flat field. Picking the first match there
    sent the viewer back to episode 1 immediately after they marked the season
    watched. So when the clock cannot separate them, separate them by how far
    the viewer actually got: the LAST completed file, else the LAST one they are
    genuinely part-way into, else the FIRST (a batch reset stamps every file at
    position 0, and the anchor for that is the beginning).
    """
    best_at = ""
    for f in files:
        at = (fp_for(f) or {}).get("updated_at", "")
        if at > best_at:
            best_at = at
    if best_at:
        tied = [i for i, f in enumerate(files)
                if (fp_for(f) or {}).get("updated_at", "") == best_at]
        done = [i for i in tied if (fp_for(files[i]) or {}).get("completed")]
        if done:
            return done[-1]
        started = [i for i in tied
                   if (fp_for(files[i]) or {}).get("position_sec", 0) > 5]
        return started[-1] if started else tied[0]
    if last_file:
        for i, f in enumerate(files):
            if f.get("path", "") == last_file:
                return i
    return None


def _resume_from_files(files: list, fp_for, last_file: str = "",
                       extra: Optional[dict] = None) -> Optional[dict]:
    """Best file+position to resume within one ordered list of files."""
    if not files:
        return None
    extra = extra or {}

    def _hint(f: dict, *, all_completed: bool = False) -> dict:
        path = f.get("path", "")
        fp = {} if all_completed else (fp_for(f) or {})
        pos = fp.get("position_sec", 0)
        dur = fp.get("duration_sec", 0)
        return {
            "item_id": f.get("item_id", ""),
            "file_path": path,
            "episode_name": f.get("name", Path(path).name),
            "position_sec": pos,
            "duration_sec": dur,
            "pct": round(pos / dur * 100, 1) if dur else (100 if all_completed else 0),
            **({"all_completed": True} if all_completed else {}),
            **extra,
        }

    idx = _resume_anchor(files, fp_for, last_file)
    if idx is not None:
        anchor = fp_for(files[idx]) or {}
        # Still mid-episode → resume the anchor itself.
        if not anchor.get("completed") and anchor.get("position_sec", 0) > 5:
            return _hint(files[idx])
        # Finished it (or barely started) → first un-completed at/after it. Skip
        # the anchor only when it is actually completed.
        for f in files[idx + 1 if anchor.get("completed") else idx:]:
            if not (fp_for(f) or {}).get("completed"):
                return _hint(f)
        # Nothing left ahead — fall through so a genuinely skipped earlier
        # episode (or the all-completed rewatch) is still surfaced.

    for f in files:
        if not (fp_for(f) or {}).get("completed"):
            return _hint(f)

    return _hint(files[0], all_completed=True)


def _progress_for(items: list, profile_id: str):
    """``(fp_for, last_file)`` — a per-file progress lookup that works for a
    single item and for a merged series alike, plus the newest ``last_file``
    among the members. Files must carry ``item_id`` when more than one item is
    passed (see ``_merged_series_files``)."""
    by_item = {it.get("id", ""): it.get("progress", {}).get(profile_id, {}) for it in items}
    single = by_item.get(items[0].get("id", ""), {}) if len(items) == 1 else None

    def fp_for(f: dict) -> dict:
        prof = single if single is not None else by_item.get(f.get("item_id", ""), {})
        return (prof or {}).get("file_progress", {}).get(f.get("path", ""), {})

    last_file, last_at = "", ""
    for prof in by_item.values():
        lf = (prof or {}).get("last_file", "")
        if not lf:
            continue
        at = ((prof.get("file_progress", {}) or {}).get(lf, {}) or {}).get("updated_at", "")
        if not last_file or at > last_at:
            last_file, last_at = lf, at
    return fp_for, last_file


def _section_hints(items: list, files: list, profile_id: str,
                   show_title: str = "") -> list:
    """Every section of a show with its own independent resume hint and watched
    count. Ordered main-first, Extras last (episodes.sections_for)."""
    if profile_id and items:
        fp_for, last_file = _progress_for(items, profile_id)
    else:
        fp_for, last_file = (lambda f: {}), ""
    out = []
    for sec in episodes.sections_for(files, show_title):
        sec_files = sec["files"]
        watched = sum(1 for f in sec_files if (fp_for(f) or {}).get("completed"))
        newest = max(((fp_for(f) or {}).get("updated_at", "") for f in sec_files),
                     default="")
        out.append({
            "key":     sec["key"],
            "label":   sec["label"],
            "kind":    sec["kind"],
            "bucket":  sec["bucket"],
            "counted": sec["counted"],
            "count":   sec["count"],
            "watched": watched,
            "last_at": newest,
            "resume":  _resume_from_files(sec_files, fp_for, last_file) if profile_id else None,
        })
    return out


def _pick_active_section(sections: list) -> Optional[dict]:
    """The section whose Resume the show-level button should offer: the most
    recently played one, else the first with something left to watch, else the
    main run. Never Extras unless it is genuinely all there is.

    Sections tie on `last_at` for the same reason files do — one-second
    timestamps, and a batch write stamps a whole section at once (see
    `_resume_anchor`). A plain `max` then returns whichever section happens to
    come first, which is the main run by construction. So rank by engagement
    before position: a section the viewer is mid-episode in beats one whose
    files merely carry the same timestamp."""
    played = [s for s in sections if s.get("last_at")]
    if played:
        def rank(s):
            r = s.get("resume") or {}
            mid = 1 if (r.get("position_sec", 0) > 5 and not r.get("all_completed")) else 0
            return (s["last_at"], mid, s.get("watched", 0))
        return max(played, key=rank)
    for s in sections:
        if s["counted"] and s["watched"] < s["count"]:
            return s
    return next((s for s in sections if s["counted"]), sections[0] if sections else None)


def find_resume_hint(item: dict, profile_id: str) -> Optional[dict]:
    """Best file+position to resume one item, scoped to its active section."""
    if not profile_id or not item.get("files"):
        return None
    prof = item.get("progress", {}).get(profile_id, {})
    sections = _section_hints([item], item["files"], profile_id, item.get("series", ""))
    active = _pick_active_section(sections)
    hint = (active or {}).get("resume")
    if not hint:
        return None
    return {
        **hint,
        "section": active["key"],
        "section_label": active["label"],
        # Whether this item's last play (for this profile) was Shuffle Play, so
        # Resume can offer to keep shuffling. Ephemeral playback state is gone by
        # now; this preference is what survives the stop. See set_shuffle_pref.
        "shuffle": bool(prof.get("shuffle")),
        "shuffle_scope": prof.get("shuffle_scope", "") or "",
    }


def _section_summary(sec: dict, meta: Optional[dict] = None) -> dict:
    """A section without its file list — what the grid and the group page need to
    render a row (name, how much of it there is, how far in the viewer got), plus
    its own cached TMDb binding when one has resolved. `meta` is the parent
    item's `metadata.sections` map; see _fetch_section_metadata."""
    out = {k: sec[k] for k in
           ("key", "label", "kind", "bucket", "counted", "count", "watched", "resume")}
    blob = (meta or {}).get(sec["key"]) or {}
    if blob.get("source") and blob.get("source") != "none":
        # The section's own title wins over the folder name — "Attack on Titan:
        # Junior High" rather than the release's "Attack On Titan Junior High".
        out["label"] = blob.get("title") or out["label"]
        out["meta"] = blob
    return out


def _files_in_section(files: list, section: str) -> list:
    """The files of one section, in canonical order.

    A playlist built from a resume hint has to stop at its own section's edge.
    The flat file list runs main → spin-off → Extras → Movies → OAD, so resuming
    Junior High ep 2 and slicing to the end used to queue up the creditless
    openings and the compilation films behind it."""
    if not section:
        return list(files)
    return sorted((f for f in files if episodes.section_key(f) == section),
                  key=episodes.sort_key)


def _merged_series_files(items: list) -> list:
    """Flatten every member item's files into one (season, episode)-ordered list,
    tagging each file with its owning ``item_id`` — the join key that lets the
    merged-series UI drive play/prep/progress against the right item."""
    out = []
    for it in items:
        iid = it.get("id", "")
        for f in it.get("files", []):
            g = dict(f)
            g["item_id"] = iid
            out.append(g)
    out.sort(key=episodes.sort_key)
    return out


def find_series_resume_hint(items: list, profile_id: str) -> Optional[dict]:
    """Series-level resume across a show whose episodes live in separate items.
    Identical rules to ``find_resume_hint`` — the same function, in fact — with
    the owning ``item_id`` carried on the hint so the caller knows which item to
    seek."""
    if not profile_id or not items:
        return None
    files = _merged_series_files(items)
    if not files:
        return None
    title = next((it.get("series") for it in items if (it.get("series") or "").strip()), "")
    sections = _section_hints(items, files, profile_id, title)
    active = _pick_active_section(sections)
    hint = (active or {}).get("resume")
    if not hint:
        return None
    # Shuffle Play, same as find_resume_hint — but read across the members. The
    # flag is written to all of them (_set_shuffle_pref), so `any` only matters
    # for runs recorded before that was true; it costs nothing and heals them.
    shuffling = any(bool((it.get("progress", {}).get(profile_id) or {}).get("shuffle"))
                    for it in items)
    scope = next((str((it.get("progress", {}).get(profile_id) or {}).get("shuffle_scope") or "")
                  for it in items
                  if (it.get("progress", {}).get(profile_id) or {}).get("shuffle_scope")), "")
    return {**hint, "section": active["key"], "section_label": active["label"],
            "shuffle": shuffling, "shuffle_scope": scope if shuffling else ""}
