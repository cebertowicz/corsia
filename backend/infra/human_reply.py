"""Content-free count of human-started Turns: the claim side of the reply stamp.

The stamp is written at admission (``infra.input_admission``); this module reads
it back when a claim brings a human message into a Turn.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any

from core.message_history import REPLY_VIA_POST_UPDATE_KEY
from infra.write_effects import after_commit

log = logging.getLogger(__name__)


def _is_stamped(raw_metadata: str | None) -> bool:
    """Whether a stored input's metadata carries the reply stamp."""
    try:
        metadata: Any = json.loads(raw_metadata or "{}")
    except ValueError:
        return False
    return isinstance(metadata, dict) and metadata.get(REPLY_VIA_POST_UPDATE_KEY) is True


def record_human_turn(
    db: sqlite3.Connection, thread_id: str, turn_id: str, rows: list[sqlite3.Row],
    result: dict,
) -> dict:
    """Log, after commit, that a claim brought a human message into a Turn.

    Content-free: a thread and a Turn id. Count distinct ``turn`` values for
    human-started Turns; a Turn with no ``post_update.sent`` line ended with no
    send. Returns ``result`` so the claim's caller passes it through.
    """
    if any(_is_stamped(row["metadata"]) for row in rows):
        after_commit(
            db,
            lambda: log.info(
                "post_update.human_turn thread=%s turn=%s", thread_id, turn_id,
            ),
        )
    return result
