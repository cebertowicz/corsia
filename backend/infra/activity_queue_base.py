"""Shape existing decision and held-message rows for the Activity contract."""
from __future__ import annotations


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
