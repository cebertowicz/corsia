"""One visit's window-focus state, for Measurement attention.

``infra.presence`` owns it: this module holds the in-memory half of a visit's focus
(each claiming session's report, the closed spans, the open span's start) and the
rules for opening and closing spans. Focus never changes a claim, so no other consumer
of presence sees it. A span closes at the back-dated stamp that ends the claim, and
every write carries the full span list, so a failed write only marks the state
``dirty`` and the next settle or sweep repairs it. NULL spans mean the visit predates
focus tracking and counts whole.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime

import infra.presence_records as presence_records
from infra.presence_time import iso, parse

log = logging.getLogger(__name__)


@dataclass
class FocusState:
    """Each session's reported focus (None: unreported, counts as focused), the visit's
    closed spans (None: it predates focus tracking), the open span's start, and whether
    a focus write failed and memory is ahead of the row."""

    reported: dict[str, bool | None] = field(default_factory=dict)
    spans: list[presence_records.Span] | None = field(default_factory=list)
    since: datetime | None = None
    dirty: bool = False

    def columns(self) -> presence_records.Focus:
        """The (focus_spans, focus_since) pair as stored."""
        if self.spans is None:
            return None, None
        return presence_records.encode_spans(self.spans), iso(self.since) if self.since else None

    def load(self, stored: presence_records.Focus) -> None:
        """Take the row's columns for a newly opened visit, unless memory is ahead of them."""
        if not self.dirty:
            self.spans = None if stored[0] is None else presence_records.decode_spans(stored[0])
            self.since = parse(stored[1]) if stored[1] else None

    def retarget(self, claiming: Iterable[str], stamp: datetime) -> bool:
        """Open or close the span to match the claiming sessions; True when it changed."""
        if self.spans is None:
            return False
        wanted = any(self.reported.get(session) is not False for session in claiming)
        if wanted and self.since is None:
            self.since = stamp
            return True
        if not wanted and self.since is not None:
            if stamp > self.since:
                self.spans.append((self.since, stamp))
            self.since = None
            return True
        return False

    def write(self, db: sqlite3.Connection, user_id: str, thread_id: str, now: datetime) -> None:
        """Write the full span list; a failed write leaves the state dirty for the next settle."""
        try:
            presence_records.write_focus(db, user_id, thread_id, self.columns(), now)
        except sqlite3.Error:
            self.dirty = True
            log.warning("presence: focus write for %s failed; the next settle repairs it", thread_id,
                        exc_info=True)
        else:
            self.dirty = False
