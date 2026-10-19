"""A film's place in its saga's story, read from its titles.

TMDb has no story-order field; `_story_index` finds an "Episode N" in any of a
film's titles (primary or alternative). Pure. `_SPLIT_TITLE_RE` is kept only so
`_migrate_item` (backend/library.py) can clear numbers it once stored.
"""
import re


_ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7,
          "viii": 8, "ix": 9, "x": 10, "xi": 11, "xii": 12}
_EPISODE_TITLE_RE = re.compile(
    r"\bepisode\s+([ivx]{1,4}|\d{1,2})\b", re.IGNORECASE)
# What the rule used to read as well, and must not: "Part 1" splits ONE story
# into two films, and "Chapter 2" follows a first film that carries no number.
# Kept only so _migrate_item can clear the numbers it once stored.
_SPLIT_TITLE_RE = re.compile(
    r"\b(?:chapter|part)\s+([ivx]{1,4}|\d{1,2})\b", re.IGNORECASE)


def _story_index(*titles: str) -> int:
    """A film's position in its saga's STORY, or 0 when nothing says.

    TMDb has no story-order field, and its primary titles are maddeningly
    inconsistent about it: the Star Wars prequels are titled "Star Wars: Episode
    I - The Phantom Menace" while the originals are plain "Star Wars" and "The
    Empire Strikes Back". Ordering on the primary title alone would put Phantom
    Menace first and then fall back to release dates for the very films the
    numbering exists to place — worse than not trying.

    The numbers do exist, in `alternative_titles` ("Star Wars: Episode IV - A New
    Hope"), which the details call already appends. So every title we know gets
    scanned, and the first episode number found wins. Harry Potter has none
    anywhere, so it keeps release order — which for Harry Potter is story order.

    Only "Episode" counts. "Part" and "Chapter" used to as well, and both put a
    saga out of order: "Mockingjay - Part 1" / "Part 2" became films 1 and 2 of
    The Hunger Games, ahead of the two films before them (Deathly Hallows did the
    same to Harry Potter), and "John Wick: Chapter 2" ran ahead of "John Wick".
    A prequel released later (The Ballad of Songbirds and Snakes) carries no
    number anywhere on TMDb, so it stays in release order: nothing here can place it.
    """
    for t in titles:
        m = _EPISODE_TITLE_RE.search(t or "")
        if not m:
            continue
        tok = m.group(1).lower()
        n = _ROMAN.get(tok) if not tok.isdigit() else int(tok)
        if n:
            return n
    return 0
