"""Period offers kept in SQLite instead of as hundreds of thousands of Python dicts.

This module is the one definition of an offer row: its fields, the SQL that selects it, the
order it is listed in and the chunk sizes. The retained-offer read in ``lesson_report_receipts``
imports them, so a change to the row is made here once.

A report needs three things from the period offers: per-(id, version) counts, the snapshot
fingerprint, and (for one selected lesson) a 20-reference page. Counts are grouped in SQL, the
fingerprint is the digest of the offers' canonical JSON text built by SQLite (kept compressed,
since the digest cannot start until the render-time coverage that precedes it is known), and
the selected lesson's offers are read by LIMIT/OFFSET inside the collecting snapshot.
"""
from __future__ import annotations

import hashlib
import json
from threading import Event
import zlib
from dataclasses import dataclass
from typing import Any

from core.lesson_report_sql import json_row_chunk_lists, json_row_chunks, json_text_chunks

#: Owners are read in groups of this many, which is also SQLite's bound-parameter limit per statement.
OWNER_CHUNK = 499
CHUNK_ROWS = 100_000
PAGE_SIZE = 20

FIELDS: tuple[str | tuple[str, str], ...] = (
    ("kind", "'lesson'"), "id", "version", ("offered", "json('true')"), "usage_id", "thread_id", "turn_id",
    "manifest_id", "task_id", "attempt_id", "commitment_id", "created_at", "model", "harness",
    ("raw_version", "NULL"), ("descriptor_tokens", "NULL"),
    ("version_reason", "'per-call manifest records behavioral identity only'"),
)
# Python's canonical form sorts keys, so the text SQLite builds must as well.
CANONICAL_FIELDS = tuple(sorted(FIELDS, key=lambda field: field if isinstance(field, str) else field[0]))

# One row per (call of a lesson-bearing manifest, distinct lesson of that manifest). The first
# parameter carries the manifests' distinct identities, which Python derived once per manifest.
_COLUMNS = (
    "SELECT i.lid AS id,i.ver AS version,u.id AS usage_id,u.thread_id AS thread_id,u.turn_id AS turn_id,"
    "u.context_manifest_id AS manifest_id,u.plan_node_id AS task_id,u.plan_attempt_id AS attempt_id,"
    "u.plan_commitment_id AS commitment_id,u.created_at AS created_at,u.model AS model,m.harness AS harness,"
    "i.ord AS ord,o.chunk AS chunk "
)
_IDENTITIES = (
    "(SELECT json_extract(value,'$[0]') AS mid,json_extract(value,'$[1]') AS ord,"
    "json_extract(value,'$[2]') AS lid,json_extract(value,'$[3]') AS ver FROM json_each(?)) i"
)
# LIMIT stops SQLite flattening this into the join as a bare json_each, which it would then rescan
# for every candidate row; unflattened, it is materialized once and probed through an automatic index.
_OWNERS = "(SELECT value AS tid,key/" + str(OWNER_CHUNK) + " AS chunk FROM json_each(?) LIMIT -1) o"
_LEFT_JOIN_MANIFEST = "LEFT JOIN context_manifests m ON m.id=u.context_manifest_id "

#: Offers of the calls inside a time window. CROSS JOIN pins the plan: identities drive, llm_usage
#: is probed by manifest, then thread ownership; left to itself SQLite scans every usage row of
#: every owner (about 3x slower). Parameters: identities, owners, user, from, to.
PERIOD_SQL = (
    _COLUMNS + "FROM " + _IDENTITIES + " "
    "CROSS JOIN llm_usage u ON u.context_manifest_id=i.mid "
    "CROSS JOIN threads t ON t.id=u.thread_id "
    "CROSS JOIN " + _OWNERS + " ON o.tid=t.id " + _LEFT_JOIN_MANIFEST +
    "WHERE t.user_id=? AND u.created_at>=? AND u.created_at<?"
)
#: Offers of named calls, which are few. Parameters: calls, identities, owners, user.
CALLS_SQL = (
    _COLUMNS + "FROM (SELECT value AS call_id FROM json_each(?)) c "
    "CROSS JOIN llm_usage u ON u.id=c.call_id "
    "JOIN " + _IDENTITIES + " ON i.mid=u.context_manifest_id "
    "JOIN threads t ON t.id=u.thread_id "
    "JOIN " + _OWNERS + " ON o.tid=t.id " + _LEFT_JOIN_MANIFEST +
    "WHERE t.user_id=?"
)
ORDER = "chunk,created_at,usage_id,ord"

_COUNTS = (
    "SELECT id,version,COUNT(*) AS n,COUNT(DISTINCT usage_id) AS calls,"
    "COUNT(DISTINCT CASE WHEN task_id IS NOT NULL AND task_id<>'' THEN task_id END) AS tasks,"
    "COUNT(DISTINCT CASE WHEN attempt_id IS NOT NULL AND attempt_id<>'' THEN attempt_id END) AS attempts,"
    "COUNT(DISTINCT CASE WHEN task_id IS NULL OR task_id='' THEN usage_id END) AS calls_without_task "
    "FROM (" + PERIOD_SQL + ") GROUP BY id,version"
)


@dataclass(frozen=True)
class DetailRequest:
    """The lesson a caller asked to page, so its offers are read inside the collecting snapshot."""

    lesson_id: str
    version: str | None
    offset: int


class LessonReportCollectionCancelled(Exception):
    """Stop an abandoned report at the next SQLite chunk boundary."""


class DeferredOffers:
    """Counts, a streamable canonical text and the requested lesson's offers for the period."""

    def __init__(self, owners: list[str], ident: list[list[Any]], user_id: str, from_ts: str, to_ts: str) -> None:
        self._params = (json.dumps(ident), json.dumps(owners), user_id, from_ts, to_ts)
        self.counts: dict[tuple[str, str], dict[str, int]] = {}
        self._chunks: list[bytes] = []
        self._windows: dict[tuple[str, str], tuple[int, list[dict[str, Any]]]] = {}

    def collect(self, db, *, cancellation_event: Event | None = None) -> None:
        for rows in json_row_chunk_lists(
            db, _COUNTS, self._params, ("id", "version", "n", "calls", "tasks", "attempts", "calls_without_task"),
            "id,version",
        ):
            for row in rows:
                self.counts[(row["id"], row["version"])] = {
                    key: row[key] for key in ("n", "calls", "tasks", "attempts", "calls_without_task")}
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
            start = max(request.offset - reviews_at_most, 0)
            end = min(request.offset + PAGE_SIZE, counts["n"])
            if end > start:
                self._windows[(lesson_id, version)] = (start, list(json_row_chunks(
                    db, PERIOD_SQL + " AND i.lid=? AND i.ver=? ORDER BY " + ORDER + " LIMIT ? OFFSET ?",
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
