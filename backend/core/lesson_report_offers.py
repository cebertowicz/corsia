"""Period offers, read from task-start guidance receipts and kept in SQLite.

An offer is one same-user ``guidance_loads`` row with ``variant='with'`` and a lesson identity:
the receipt the task-start writer records when it delivers a trial lesson. This module is the one
definition of an offer row: its fields, the SQL that selects it, the order it is listed in and
the chunk sizes.

A report needs three things from the period offers: per-(id, version) counts, the snapshot
fingerprint, and (for one selected lesson) a 20-reference page. Counts are grouped in SQL, the
fingerprint is the digest of the offers' canonical JSON text built by SQLite (kept compressed,
since the digest cannot start until the render-time coverage that precedes it is known), and
the selected lesson's offers are read by LIMIT/OFFSET inside the collecting snapshot.
"""
from __future__ import annotations

import hashlib
import json
import zlib
from dataclasses import dataclass
from threading import Event
from typing import Any

from core.lesson_report_sql import json_row_chunk_lists, json_row_chunks, json_text_chunks

#: Owners are read in groups of this many, which is also SQLite's bound-parameter limit per statement.
OWNER_CHUNK = 499
CHUNK_ROWS = 100_000
PAGE_SIZE = 20
FIELDS: tuple[str | tuple[str, str], ...] = (
    ("kind", "'lesson'"), "id", "version", ("offered", "json('true')"), "receipt_id", "thread_id",
    "turn_id", "task_id", "checkpoint_generation_id", "created_at", "path", "raw_sha256", ("raw_version", "NULL"),
    ("descriptor_tokens", "NULL"), ("version_reason", "'task-start receipt records behavioral identity'"),
)
# Python's canonical form sorts keys, so the text SQLite builds must as well.
CANONICAL_FIELDS = tuple(sorted(FIELDS, key=lambda field: field if isinstance(field, str) else field[0]))
_OWNERS = "(SELECT value AS tid,key/" + str(OWNER_CHUNK) + " AS chunk FROM json_each(?) LIMIT -1) o"
_COLUMNS = (
    "SELECT g.lesson_id AS id,g.lesson_behavioral_sha256 AS version,g.id AS receipt_id,"
    "g.thread_id,g.turn_id,g.node_id AS task_id,g.checkpoint_generation_id,g.created_at,"
    "g.path,g.raw_sha256,o.chunk AS chunk "
)
_BASE = (
    "FROM guidance_loads g JOIN threads t ON t.id=g.thread_id "
    "JOIN " + _OWNERS + " ON o.tid=t.id "
    "WHERE t.user_id=? AND g.variant='with' AND g.lesson_id IS NOT NULL "
    "AND g.lesson_behavioral_sha256 IS NOT NULL"
)
PERIOD_SQL = _COLUMNS + _BASE + " AND g.created_at>=? AND g.created_at<?"
COUNTS_SQL = (
    "SELECT id,version,COUNT(*) AS n,COUNT(DISTINCT receipt_id) AS receipts,"
    "COUNT(DISTINCT CASE WHEN task_id IS NOT NULL AND task_id<>'' THEN task_id END) AS tasks "
    "FROM (" + PERIOD_SQL + ") GROUP BY id,version"
)
ORDER = "chunk,created_at,receipt_id"
SQL_ORDER = "o.chunk,g.created_at,g.id"


@dataclass(frozen=True)
class DetailRequest:
    lesson_id: str
    version: str | None
    offset: int


class LessonReportCollectionCancelled(Exception):
    """Stop an abandoned report at the next SQLite chunk boundary."""


class DeferredOffers:
    """Counts, streamable canonical text and one lesson's receipt page."""

    def __init__(self, owners: list[str], user_id: str, from_ts: str, to_ts: str) -> None:
        self._params = (json.dumps(owners), user_id, from_ts, to_ts)
        self.counts: dict[tuple[str, str], dict[str, int]] = {}
        self._chunks: list[bytes] = []
        self._windows: dict[tuple[str, str], tuple[int, list[dict[str, Any]]]] = {}

    def collect(self, db, *, cancellation_event: Event | None = None) -> None:
        for rows in json_row_chunk_lists(db, COUNTS_SQL, self._params, ("id", "version", "n", "receipts", "tasks"), "id,version"):
            for row in rows:
                self.counts[(row["id"], row["version"])] = {key: row[key] for key in ("n", "receipts", "tasks")}
            if cancellation_event is not None and cancellation_event.is_set():
                raise LessonReportCollectionCancelled
        for text in json_text_chunks(db, PERIOD_SQL, self._params, CANONICAL_FIELDS, ORDER, size=CHUNK_ROWS):
            self._chunks.append(zlib.compress(text[1:-1].encode(), 1))
            if cancellation_event is not None and cancellation_event.is_set():
                raise LessonReportCollectionCancelled

    def prefetch(self, db, request: DetailRequest, reviews_at_most: int) -> None:
        """Read the offers a detail page of the requested lesson can show.

        A page lists the lesson's reviews first, then its offers, 20 references from the request's
        offset. With ``r`` reviews the offers on that page start at ``offset - r``, so the window
        from ``offset - reviews_at_most`` to the end of the page holds them for every ``r`` up to
        the bound, and no count of the rows' reviews has to be predicted here.
        """
        for (lesson_id, version), counts in self.counts.items():
            if lesson_id != request.lesson_id or request.version not in (None, version):
                continue
            start, end = max(request.offset - reviews_at_most, 0), min(request.offset + PAGE_SIZE, counts["n"])
            if end > start:
                self._windows[(lesson_id, version)] = (start, list(json_row_chunks(
                    db, PERIOD_SQL + " AND g.lesson_id=? AND g.lesson_behavioral_sha256=? ORDER BY " + SQL_ORDER + " LIMIT ? OFFSET ?",
                    (*self._params, lesson_id, version, end - start, start), FIELDS, ORDER,
                )))

    def total(self, identity: tuple[str, str]) -> int:
        return self.counts.get(identity, {}).get("n", 0)

    def page(self, identity: tuple[str, str], start: int, end: int) -> list[dict[str, Any]]:
        """The identity's offers from ``start`` to ``end``, which must lie inside its prefetched window."""
        if end <= start:
            return []
        first, rows = self._windows.get(identity, (0, []))
        if start < first or end > first + len(rows):
            raise LookupError("offers outside the detail request this report was collected for")
        return rows[start - first:end - first]

    def feed(self, digest) -> None:
        """Hash the offers exactly as ``json.dumps`` joins a list's canonical elements."""
        for index, chunk in enumerate(self._chunks):
            if index:
                digest.update(b",")
            digest.update(zlib.decompress(chunk))


#: The one snapshot field a DeferredOffers object stands for, by its own key path.
OFFERS_PATH = ("period", "offers")


def snapshot_fingerprint(payload: dict[str, Any], canonical) -> str:
    """The snapshot digest, with the period offers fed from SQLite text at their own key.

    Only the containers on ``OFFERS_PATH`` are written here, key by key in the order
    ``canonical`` sorts them; every other value goes to ``canonical`` whole, and the offers
    field is the deferred object's own array. The bytes are therefore those of the canonical
    encoding of the payload with the offers as a list, and no part of the serialized document
    is searched for a marker -- a preserved free-text field can hold any string.
    """
    digest = hashlib.sha256()
    _stream_fields(digest, payload, OFFERS_PATH, canonical, payload["period"]["offers"].feed)
    return digest.hexdigest()


def _stream_fields(digest, value: dict[str, Any], path: tuple[str, ...], canonical, feed) -> None:
    """Write ``value`` the way ``canonical`` writes a dict, feeding the field at ``path`` in."""
    if not path:
        digest.update(b"[")
        feed(digest)
        digest.update(b"]")
        return
    digest.update(b"{")
    for position, name in enumerate(sorted(value)):
        if position:
            digest.update(b",")
        digest.update(canonical(name) + b":")
        if name == path[0]:
            _stream_fields(digest, value[name], path[1:], canonical, feed)
        else:
            digest.update(canonical(value[name]))
    digest.update(b"}")
