"""Shape existing decision and held-message rows for the Activity contract."""
from __future__ import annotations

# The longest title an Activity row carries. A closed row shows its title on one line; the open
# row and the item pane's heading show all of it. The list is 320-455 px wide, leaving 252-387 px
# for a title, where one line of 13 px system font holds 42-69 characters of ordinary text
# (measured with SF Pro), so 160 keeps the open row to four lines at the narrowest list and three
# at the widest.
TITLE_MAX_CHARS = 160
ELLIPSIS = '…'


def flat(text: str | None) -> str:
    return ' '.join((text or '').split())


def clip_title(text: str | None, room: int = TITLE_MAX_CHARS) -> str:
    """Flatten whitespace and shorten to ``room`` characters, ending with an ellipsis when cut."""
    text = flat(text)
    return text if len(text) <= room else text[:room - 1].rstrip() + ELLIPSIS


def shape(row: dict) -> dict:
    detail = row['detail']
    renderer = row['renderer']
    return {
        'key': f"{renderer}:{row['external_id']}",
        'external_id': row['external_id'],
        'renderer': renderer,
        'kind': 'message' if renderer == 'held-reply' else 'decision',
        'title': detail.get('title') or 'Untitled',
        'source': detail['source'],
        'raised_at': row.get('raised_at') or None,
        'open': None,
        'detail': detail,
        'priority': row.get('priority', 0),
    }
