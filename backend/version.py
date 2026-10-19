"""The server's version: the badge at the bottom of static/index.html.

Read once at import. /api/version serves it, and clients force a hard reload
when their cached page's badge is older (see the comment below).
"""
import re

from backend.paths import ROOT


# The version badge at the bottom of `static/index.html` is the single source of
# truth for the UI version; this reads it rather than duplicating it.
#
# It *was* a hand-maintained literal "kept in sync" with the badge, and it drifted
# almost immediately — the constant sat at 11.12.1 while the shipped page said
# 11.17.0. That is not cosmetic: clients fetch this via /api/version and force a
# hard reload when their cached page's badge is older, so a lagging constant means
# dashboards silently keep serving a stale frontend after an update. The literal
# below is only the fallback for a missing/unreadable page.
_UI_VERSION_BADGE_RE = re.compile(r'<div[^>]*\bdata-ui-version\b[^>]*>([^<]*)</div>')


def _read_ui_version(default: str = "11.18.0") -> str:
    """Parse the UI version out of the shipped `static/index.html` badge."""
    try:
        html = (ROOT.resolve() / "static" / "index.html").read_text(
            encoding="utf-8", errors="replace")
    except OSError:
        return default
    m = _UI_VERSION_BADGE_RE.search(html)
    return m.group(1).strip() if m else default


UI_VERSION = _read_ui_version()
