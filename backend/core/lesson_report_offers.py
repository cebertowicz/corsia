"""Period offers kept in SQLite instead of as hundreds of thousands of Python dicts.

This module is the one definition of an offer row: its fields, the SQL that selects it, the
order it is listed in and the chunk sizes. The retained-offer read in ``lesson_report_receipts``
imports them, so a change to the row is made here once.

A report needs three things from the period offers: per-(id, version) counts, the snapshot
fingerprint, and (for one selected lesson) a 20-reference page. An offer is a lesson-bearing
call times one lesson identity of its manifest, so a period holds about lessons-per-manifest
times more offers than calls; neither counts nor fingerprint read offer rows. Counts come from
the period's calls grouped by (manifest, task, attempt), expanded by each manifest's identities.
The fingerprint digests the identities and the period calls' canonical JSON text built by
SQLite, which together determine every offer. The selected lesson's offers are read by
LIMIT/OFFSET inside the collecting snapshot.
"""
from __future__ import annotations

import hashlib
import json
from threading import Event
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

# An offer's fields from its call's own row: all but the lesson identity and the manifest's harness.
_CALL_COLUMNS = (
    "u.id AS usage_id,u.thread_id AS thread_id,u.turn_id AS turn_id,"
    "u.context_manifest_id AS manifest_id,u.plan_node_id AS task_id,u.plan_attempt_id AS attempt_id,"
    "u.plan_commitment_id AS commitment_id,u.created_at AS created_at,u.model AS model"
)
# One row per (call of a lesson-bearing manifest, distinct lesson of that manifest). The first
# parameter carries the manifests' distinct identities, which Python derived once per manifest.
_COLUMNS = ("SELECT i.lid AS id,i.ver AS version," + _CALL_COLUMNS +
            ",m.harness AS harness,i.ord AS ord,o.chunk AS chunk ")
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

#: Every period call of the owners, owners first. Calls whose manifest offers no lesson are
#: included: filtering them costs more than reading them, and they carry no offer.
#: Parameters: owners, user, from, to.
_PERIOD_CALLS = (
    "FROM " + _OWNERS + " CROSS JOIN threads t ON t.id=o.tid "
    "CROSS JOIN llm_usage u ON u.thread_id=t.id "
    "WHERE t.user_id=? AND u.created_at>=? AND u.created_at<?"
)
#: One row per (manifest, task, attempt) of the period's calls: every offer count follows from it.
_COUNTS = (
    "SELECT u.context_manifest_id AS manifest_id,u.plan_node_id AS task_id,"
    "u.plan_attempt_id AS attempt_id,COUNT(*) AS n " + _PERIOD_CALLS +
    " GROUP BY u.context_manifest_id,u.plan_node_id,u.plan_attempt_id"
)
_GROUP_FIELDS = ("manifest_id", "task_id", "attempt_id", "n")
#: Every call field an offer carries; with the identities and harnesses they determine the offers.
_CALLS = "SELECT " + _CALL_COLUMNS + " " + _PERIOD_CALLS
_CALL_FIELDS = tuple(field for field in FIELDS if isinstance(field, str) and field not in ("id", "version", "harness"))
#: The harness of each lesson-bearing manifest, the one offer field not on the call.
_HARNESSES = "SELECT id,harness FROM context_manifests WHERE id IN (SELECT value FROM json_each(?)) ORDER BY id"
_CALL_ORDER = "created_at,usage_id"


def _present(value: Any) -> bool:
    """A recorded task or attempt id, as SQL's ``IS NOT NULL AND <> ''`` reads it."""
    return value is not None and value != ""


@dataclass(frozen=True)
class DetailRequest:
    """The lesson a caller asked to page, so its offers are read inside the collecting snapshot."""

    lesson_id: str
    version: str | None
    offset: int


class LessonReportCollectionCancelled(Exception):
    """Stop an abandoned report at the next SQLite chunk boundary."""


class DeferredOffers:
    """Counts, a digest of what determines the offers, and the requested lesson's offers for the period."""

    def __init__(self, owners: list[str], ident: list[list[Any]], user_id: str, from_ts: str, to_ts: str) -> None:
        self._ident = ident
        self._params = (json.dumps(ident), json.dumps(owners), user_id, from_ts, to_ts)
        self._manifests = json.dumps(sorted({row[0] for row in ident}))
        self._call_params = (json.dumps(owners), user_id, from_ts, to_ts)
        self.counts: dict[tuple[str, str], dict[str, int]] = {}
        self._digest = ""
        self.text_chunks = 0
        self._windows: dict[tuple[str, str], tuple[int, list[dict[str, Any]]]] = {}

    def collect(self, db, *, cancellation_event: Event | None = None) -> None:
        if not self._ident:
            self._digest = hashlib.sha256(b"[]").hexdigest()
            return  # No lesson-bearing manifest: no call can carry an offer.
        harnesses = [list(row) for row in db.execute(_HARNESSES, (self._manifests,))]
        digest = hashlib.sha256(json.dumps([self._ident, harnesses], separators=(",", ":"),
                                           ensure_ascii=False).encode())
        by_manifest: dict[Any, dict[str, Any]] = {}
        for rows in json_row_chunk_lists(db, _COUNTS, self._call_params, _GROUP_FIELDS,
                                         "manifest_id,task_id,attempt_id"):
            for row in rows:
                group = by_manifest.setdefault(row["manifest_id"], {
                    "calls": 0, "calls_without_task": 0, "tasks": set(), "attempts": set()})
                group["calls"] += row["n"]
                if _present(row["task_id"]):
                    group["tasks"].add(row["task_id"])
                else:
                    group["calls_without_task"] += row["n"]
                if _present(row["attempt_id"]):
                    group["attempts"].add(row["attempt_id"])
            if cancellation_event is not None and cancellation_event.is_set():
                raise LessonReportCollectionCancelled
        self.counts = _identity_counts(self._ident, by_manifest)
        for text in json_text_chunks(db, _CALLS, self._call_params, _CALL_FIELDS, _CALL_ORDER, size=CHUNK_ROWS):
            # Identities and harnesses, then every call's object after a comma, whatever the chunk size.
            digest.update(b"," + text[1:-1].encode())
            self.text_chunks += 1
            if cancellation_event is not None and cancellation_event.is_set():
                raise LessonReportCollectionCancelled
        self._digest = digest.hexdigest()

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
        """Stand in for the offers with the digest of what determines them, as one JSON string."""
        digest.update(b'"' + self._digest.encode() + b'"')


def _identity_counts(ident: list[list[Any]], by_manifest: dict[Any, dict[str, Any]]) -> dict[tuple[str, str], dict[str, int]]:
    """Each offered identity's counts over the manifests carrying it.

    A call offers each distinct identity of its manifest once, so an identity's offers, and
    its distinct calls, number the period calls of those manifests; tasks and attempts are
    distinct over all of them. Identities with no period call are left out, as a grouped
    offer scan leaves them out.
    """
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for manifest_id, _order, lesson_id, version in ident:
        group = by_manifest.get(manifest_id)
        if group is None:
            continue
        row = merged.setdefault((lesson_id, version), {
            "calls": 0, "calls_without_task": 0, "tasks": set(), "attempts": set()})
        row["calls"] += group["calls"]
        row["calls_without_task"] += group["calls_without_task"]
        row["tasks"] |= group["tasks"]
        row["attempts"] |= group["attempts"]
    return {identity: {"n": row["calls"], "calls": row["calls"], "tasks": len(row["tasks"]),
                       "attempts": len(row["attempts"]), "calls_without_task": row["calls_without_task"]}
            for identity, row in merged.items()}


#: The one snapshot field a DeferredOffers object stands for, by its own key path.
OFFERS_PATH = ("period", "offers")


def snapshot_fingerprint(payload: dict[str, Any], canonical) -> str:
    """The snapshot digest, with the period offers fed by the deferred object at their own key.

    Only the containers on ``OFFERS_PATH`` are written here, key by key in the order
    ``canonical`` sorts them; every other value goes to ``canonical`` whole, and the offers
    field is the array the deferred object feeds. The bytes are therefore those of the
    canonical encoding of the payload with that array in place of the offers, and no part of
    the serialized document is searched for a marker -- a preserved free-text field can hold
    any string.
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
