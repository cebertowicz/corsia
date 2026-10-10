"""Outcome events: the one write primitive and retraction.

Writers take the caller's connection and transaction, like
``infra.thread_updates``; nothing here commits. Reads and delete-by-project are
deferred (spine r5): measures read the table in their own queries.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from host_access.credential_sanitizer import contains_credential
from infra.outcome_event_attributes import filter_attributes
from infra.outcome_event_input import OutcomeInputRefusal

TOMBSTONE = '{"retracted":true}'
_COLUMNS = (
    "id,dedupe_key,event_name,source,provenance,ts,observed_ts,subject_key,"
    "project_id,agent_thread_id,attribution,corroborated_by,attributes,retracted_at"
)


@dataclass(frozen=True)
class RecordResult:
    """What a write did: whether the row is new, and which keys were dropped."""

    inserted: bool
    event_id: str | None
    dropped_keys: tuple[str, ...]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _utc(ts: str) -> str:
    """Normalize a source timestamp to UTC ISO form so ``ts`` indexes order as text."""
    try:
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ValueError(f"ts is not an ISO 8601 timestamp: {ts!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def count_outcome_event(
    db: sqlite3.Connection, counter: str, event_name: str, n: int = 1,
) -> None:
    """Add ``n`` to a durable intake counter; an unknown ``counter`` is refused by the schema.

    Counters are ``duplicate_ignored``, ``attribute_dropped`` and ``mapping_failed``;
    adapters call this to count their own failures. ``n`` must be positive: the counters
    only grow.
    """
    if n < 1:
        raise ValueError("outcome event counters only grow; n must be positive")
    db.execute(
        "INSERT INTO outcome_event_counters (counter,event_name,n) VALUES (?,?,?) "
        "ON CONFLICT(counter,event_name) DO UPDATE SET n=n+excluded.n",
        (counter, event_name, n),
    )


def record_outcome_event(
    db: sqlite3.Connection,
    *,
    dedupe_key: str,
    event_name: str,
    source: str,
    provenance: str,
    ts: str,
    subject_key: str | None = None,
    project_id: str | None = None,
    agent_thread_id: str | None = None,
    attribution: str | None = None,
    corroborated_by: str | None = None,
    attributes: dict[str, Any] | None = None,
) -> RecordResult:
    """Insert one event keyed on ``dedupe_key``; the first write wins.

    A later write with the same key is ignored and counted
    (``duplicate_ignored``). Attributes are filtered through the event's
    allowlist; dropped keys are counted (``attribute_dropped``) and returned.
    ``ts`` is stored as UTC ISO text (a naive value is taken as UTC). Raises
    ``OutcomeInputRefusal`` (a ``ValueError``) only at the credential-identity and
    kept-attribute budget guards; an unparseable ``ts`` raises ordinary ``ValueError``.
    SQL constraints, including invalid source, provenance or attribution, raise
    ``sqlite3.IntegrityError``. Incidental errors are never wrapped as refusals.
    """
    for field in (dedupe_key, subject_key, project_id, agent_thread_id, corroborated_by):
        if field and contains_credential(field):
            raise OutcomeInputRefusal("outcome event identity fields must not carry credentials")
    kept, dropped = filter_attributes(event_name, attributes)
    event_id = str(uuid.uuid4())
    cursor = db.execute(
        f"INSERT INTO outcome_events ({_COLUMNS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,NULL) "
        "ON CONFLICT(dedupe_key) DO NOTHING",
        (event_id, dedupe_key, event_name, source, provenance, _utc(ts), _now(),
         subject_key, project_id, agent_thread_id, attribution, corroborated_by,
         json.dumps(kept, separators=(",", ":"))),
    )
    if cursor.rowcount != 1:
        count_outcome_event(db, "duplicate_ignored", event_name)
        return RecordResult(False, None, ())
    if dropped:
        count_outcome_event(db, "attribute_dropped", event_name, len(dropped))
    return RecordResult(True, event_id, tuple(dropped))


def retract_outcome_event(db: sqlite3.Connection, dedupe_key: str) -> bool:
    """Withdraw one event: set ``retracted_at`` and replace its attributes.

    True when a live row was retracted; a missing or already retracted row is a no-op.
    """
    cursor = db.execute(
        "UPDATE outcome_events SET retracted_at=?, attributes=? "
        "WHERE dedupe_key=? AND retracted_at IS NULL",
        (_now(), TOMBSTONE, dedupe_key),
    )
    return cursor.rowcount == 1
