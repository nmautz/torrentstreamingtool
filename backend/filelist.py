"""A torrent's file list, as the library stores it.

`build_file_list` turns qBittorrent's files API answer into the sorted,
season/episode-attributed list an item keeps (attribution is `episodes.py`'s;
TMDb-aware corrections happen later in `_reattribute_item_files`). Pure.
"""
from pathlib import Path
from typing import Optional

import episodes


VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".mov", ".wmv", ".m4v", ".ts", ".m2ts", ".webm"}


def build_file_list(qbit_file_list: list, save_path: str) -> list[dict]:
    """Build a sorted video-file list from qBittorrent's files API response.

    Season/episode attribution is done over the whole list at once (qBit's
    `name` is relative to the save path, so the torrent's own folder structure
    is intact here — that's where an anime batch keeps its seasons). Numbers
    that turn out to be series-absolute are corrected later, once TMDb's season
    inventory is known; see `_reattribute_item_files`.
    """
    videos = [f for f in qbit_file_list
              if Path(f.get("name", "")).suffix.lower() in VIDEO_EXTS]
    rels = [f.get("name", "") for f in videos]
    result = [{
        "name": Path(rel).name,
        "path": str(Path(save_path) / rel),
        "size_bytes": f.get("size", 0),
        "season": 0,
        "episode": 0,
    } for rel, f in zip(rels, videos)]
    for entry, slot in zip(result, episodes.attribute_paths(rels)):
        episodes.apply_slot(entry, slot)
    result.sort(key=episodes.sort_key)
    return result


def _file_by_index(files: list, idx: int) -> Optional[dict]:
    """Return the file dict whose 'index' field equals idx (falls back to enumerate position)."""
    for i, f in enumerate(files):
        if f.get("index", i) == idx:
            return f
    return None


def largest_video(files: list) -> Optional[dict]:
    videos = [f for f in files if Path(f["name"]).suffix.lower() in VIDEO_EXTS]
    pool = videos or files
    return max(pool, key=lambda f: f.get("size", 0), default=None)


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"
