"""Schema 78 -> 79: stored copies of the former product name become ``corsia``.

The code that reads these values was renamed in the same change, and a reader
that expects the new name finds nothing under the old one: the System tier of
every existing thread would render empty, and a retried webhook dead letter or
a repeated causal registration event would be written twice. So the data moves
here, in the startup transaction, and a build that reads only the new name
cannot start on a store that still holds the old one.

What converts, and only this:

- the System section key and System source ids in the two ``messages.metadata``
  owners (``migration_78_to_79_section_key``);
- ``dead_letter_events.id`` of restart dead letters, re-derived from
  ``external_id`` under the renamed seed;
- ``artifact_registration_events.id`` of causal events, re-derived from the
  row's own artifact, event type and causal id under the renamed seed;
- ``restart_wake_registrations.id`` of pending rows, re-derived from the row's
  own operation, thread and turn, so a continuation registered before the
  update is found again after it.

Everything else that mentions the former name is history and is kept as
written: message text, tool calls, project names, paths, settled restart
registrations, and a causal event whose registration has since been deleted
(no caller can re-derive its id, so it is counted and left).

Anything this cannot classify refuses. The step raises, the ladder rolls the
transaction back and the service stays down on an unchanged database. Counts
are read from the store at run time; none is pinned here.
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from dataclasses import dataclass, field

from infra.migration_78_to_79_section_key import (
    EXACT_TOKEN,
    NATIVE_ID_TOKEN,
    NEW_NAME,
    OLD_NAME,
    SOURCE_ID_TOKEN,
    SectionKeyRefusal,
    convert_document,
    refuse_section_key,
)

log = logging.getLogger(__name__)

_REFUSAL_SAMPLES = 20
_DERIVED_ID_LENGTH = 36
_DEAD_LETTER_SEED = "-webhook-dead-letter"
_REGISTRATION_EVENT_SEED = "-artifact-registration"
_RESTART_REGISTRATION_SEED = "-restart"
# JSON columns that carry context rows but have no owner of the section key:
# a hit there is unclassified, so they are scanned to refuse, never to convert.
_REFUSAL_ONLY_COLUMNS = (
    ("context_manifests", "entries_json"),
    ("checkpoints", "artifact_snapshot_json"),
    ("context_bootloader_events", "files_json"),
)


class StoredNameRefusal(RuntimeError):
    """The store holds the former name somewhere this migration cannot classify."""


class _NoCandidateKey(StoredNameRefusal):
    """No key the row holds derives its id, under either seed."""


@dataclass
class StoredNamesReport:
    """What one pass found. ``refusals`` is zero for a store that converts."""

    metadata_rows: int = 0
    section_values: int = 0
    source_ids: int = 0
    dead_letters: int = 0
    dead_letters_current: int = 0
    registration_events: int = 0
    registration_events_current: int = 0
    registration_events_unmatched: int = 0
    restart_registrations: int = 0
    restart_registrations_current: int = 0
    refusals: int = 0
    refusal_samples: list[str] = field(default_factory=list)

    def refuse(self, where: str, why: object) -> None:
        """Count one refusal and keep the first few as samples for the error."""
        self.refusals += 1
        if len(self.refusal_samples) < _REFUSAL_SAMPLES:
            self.refusal_samples.append(f"{where}: {why}")

    def counts(self) -> dict[str, int]:
        """The integer counters by name, for the log line and a dry run."""
        return {
            name: value for name, value in vars(self).items() if isinstance(value, int)
        }


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """The table's columns; empty when a historical schema has no such table."""
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _carries(column: str, *tokens: str) -> str:
    """SQL: the column carries at least one of the tokens."""
    quoted = [token.replace("'", "''") for token in tokens]
    return "(" + " OR ".join(f"instr({column},'{token}')>0" for token in quoted) + ")"


def _convert_messages(conn: sqlite3.Connection, report: StoredNamesReport, apply: bool) -> None:
    """One pass over ``messages``: convert ``metadata``, refuse ``delivery_options``."""
    columns = _columns(conn, "messages")
    if "metadata" not in columns:
        return
    metadata = (
        f"instr(metadata,'{OLD_NAME}')>0 AND "
        + _carries("metadata", EXACT_TOKEN, SOURCE_ID_TOKEN, NATIVE_ID_TOKEN)
    )
    options = _carries("delivery_options", EXACT_TOKEN) if "delivery_options" in columns else "0"
    select = (
        f"SELECT rowid, id, CASE WHEN {metadata} THEN metadata END, "
        f"CASE WHEN {options} THEN {'delivery_options' if options != '0' else 'NULL'} END "
        f"FROM messages WHERE ({metadata}) OR {options}"
    )
    # The whole table is read before any row is written. A refusal then costs
    # no write at all, and no read has to look through the pages this
    # transaction has already rewritten: on a large store that, not the
    # conversion, is what takes the time. The converted texts are held in
    # memory until then, so memory grows with the rows that carry the name,
    # not with the table: 918 such rows on the production store this step
    # was written for (2026-10-02).
    updates: list[tuple[str, int]] = []
    for rowid, message_id, stored_metadata, stored_options in conn.execute(select):
        try:
            if stored_options is not None:
                refuse_section_key(stored_options)
            if stored_metadata is None:
                continue
            result = convert_document(stored_metadata)
        except SectionKeyRefusal as refusal:
            report.refuse(f"messages[{message_id}]", refusal)
            continue
        if result.text is not None:
            report.metadata_rows += 1
            report.section_values += result.section_values
            report.source_ids += result.source_ids
            updates.append((result.text, rowid))
    if apply and not report.refusals:
        conn.executemany("UPDATE messages SET metadata=? WHERE rowid=?", updates)


def _refuse_other_columns(conn: sqlite3.Connection, report: StoredNamesReport) -> None:
    for table, column in _REFUSAL_ONLY_COLUMNS:
        if column not in _columns(conn, table):
            continue
        cursor = conn.execute(
            f"SELECT rowid, {column} FROM {table} WHERE {_carries(column, EXACT_TOKEN)}"
        )
        for rowid, stored in cursor:
            try:
                refuse_section_key(stored)
            except SectionKeyRefusal as refusal:
                report.refuse(f"{table}.{column}[rowid {rowid}]", refusal)


def _derived(seed: str, key: str) -> tuple[str, str]:
    """The id ``key`` derives to under the former seed and under the renamed one."""
    return (
        str(uuid.uuid5(uuid.NAMESPACE_URL, f"{OLD_NAME}{seed}:{key}")),
        str(uuid.uuid5(uuid.NAMESPACE_URL, f"{NEW_NAME}{seed}:{key}")),
    )


def _renamed_id(stored: str, seed: str, keys: list[str]) -> str | None:
    """The id to store, ``None`` when it is already current.

    Exactly one key must derive ``stored``, under either seed. No match means
    the id was not derived from what the row holds; several mean the row does
    not say which key it was derived from. Both raise.
    """
    matches = [
        (old == stored, new)
        for old, new in (_derived(seed, key) for key in dict.fromkeys(keys))
        if stored in (old, new)
    ]
    if len(matches) != 1:
        raise (StoredNameRefusal if matches else _NoCandidateKey)(
            f"{len(matches)} of {len(keys)} candidate key(s) derive this id; exactly one must"
        )
    is_old, new = matches[0]
    return new if is_old else None


def _rename_ids(
    conn: sqlite3.Connection, table: str, seed: str, rows: list[tuple[str, list[str]]],
    report: StoredNamesReport, apply: bool, *,
    taken: set[str] | None = None, keep_unmatched: bool = False,
) -> tuple[int, int, int]:
    """Re-derive each (id, candidate keys) row: (converted, already current, unmatched).

    ``taken`` is every id the table holds when ``rows`` is only part of it.
    ``keep_unmatched`` leaves a row no key derives as it is, and counts it.
    """
    renames: list[tuple[str, str]] = []
    current = unmatched = 0
    stored_ids = taken if taken is not None else {stored for stored, _keys in rows}
    for stored, keys in rows:
        try:
            renamed = _renamed_id(stored, seed, keys)
            if renamed in stored_ids:
                raise StoredNameRefusal("the id is stored under both the former and the renamed seed")
        except _NoCandidateKey as refusal:
            if keep_unmatched:
                unmatched += 1
            else:
                report.refuse(f"{table}[{stored}]", refusal)
            continue
        except StoredNameRefusal as refusal:
            report.refuse(f"{table}[{stored}]", refusal)
            continue
        if renamed is None:
            current += 1
        else:
            renames.append((renamed, stored))
    if apply and not report.refusals:
        conn.executemany(f"UPDATE {table} SET id=? WHERE id=?", renames)
    return len(renames), current, unmatched


def _convert_dead_letters(conn: sqlite3.Connection, report: StoredNamesReport, apply: bool) -> None:
    """Restart dead letters: ``uuid5(seed:external_id)``. Parked ids are 12 characters."""
    if not {"id", "external_id"} <= _columns(conn, "dead_letter_events"):
        return
    rows = [
        (str(stored), [str(external_id)])
        for stored, external_id in conn.execute(
            "SELECT id, external_id FROM dead_letter_events WHERE length(id)=?",
            (_DERIVED_ID_LENGTH,),
        )
    ]
    report.dead_letters, report.dead_letters_current, _ = _rename_ids(
        conn, "dead_letter_events", _DEAD_LETTER_SEED, rows, report, apply,
    )


def _convert_registration_events(
    conn: sqlite3.Connection, report: StoredNamesReport, apply: bool,
) -> None:
    """Causal events: ``uuid5(seed:artifact:type:causal[:scope])``.

    The scope is not stored. The only caller that passes one passes an
    ``artifact_registrations.id`` of the same artifact, so the candidates are
    the unscoped key and one key per registration of that artifact.

    Registrations are deleted while their events are kept, so a scoped event
    can outlive its scope. No caller can derive that id again; it is left as
    written and counted, not refused.
    """
    needed = {"id", "artifact_id", "event_type", "causal_event_id"}
    if not needed <= _columns(conn, "artifact_registration_events"):
        return
    scopes: dict[str, list[str]] = {}
    if {"id", "artifact_id"} <= _columns(conn, "artifact_registrations"):
        for artifact_id, registration_id in conn.execute(
            "SELECT artifact_id, id FROM artifact_registrations ORDER BY rowid"
        ):
            scopes.setdefault(str(artifact_id), []).append(str(registration_id))
    rows: list[tuple[str, list[str]]] = []
    for stored, artifact_id, event_type, causal in conn.execute(
        "SELECT id, artifact_id, event_type, causal_event_id "
        "FROM artifact_registration_events "
        "WHERE length(id)=? AND causal_event_id IS NOT NULL AND causal_event_id<>''",
        (_DERIVED_ID_LENGTH,),
    ):
        key = f"{artifact_id}:{event_type}:{causal}"
        rows.append(
            (str(stored), [key, *(f"{key}:{scope}" for scope in scopes.get(str(artifact_id), ()))])
        )
    (
        report.registration_events,
        report.registration_events_current,
        report.registration_events_unmatched,
    ) = _rename_ids(
        conn, "artifact_registration_events", _REGISTRATION_EVENT_SEED, rows, report, apply,
        keep_unmatched=True,
    )


def _convert_restart_registrations(
    conn: sqlite3.Connection, report: StoredNamesReport, apply: bool,
) -> None:
    """Pending continuations: ``uuid5(seed:operation:thread:turn)``.

    Only a pending row is registered again, and its wake message is written in
    the transaction that settles it, so no message yet names a pending id.
    Settled rows are named by the messages they woke and stay as written.
    """
    needed = {"id", "thread_id", "status", "operation_id", "turn_id"}
    if not needed <= _columns(conn, "restart_wake_registrations"):
        return
    taken = {str(row[0]) for row in conn.execute("SELECT id FROM restart_wake_registrations")}
    rows = [
        (str(stored), [f"{operation_id or 'external'}:{thread_id}:{turn_id}"])
        for stored, operation_id, thread_id, turn_id in conn.execute(
            "SELECT id, operation_id, thread_id, turn_id "
            "FROM restart_wake_registrations WHERE status='pending'"
        )
    ]
    report.restart_registrations, report.restart_registrations_current, _ = _rename_ids(
        conn, "restart_wake_registrations", _RESTART_REGISTRATION_SEED, rows, report, apply,
        taken=taken,
    )


def _run(conn: sqlite3.Connection, *, apply: bool) -> StoredNamesReport:
    report = StoredNamesReport()
    _convert_messages(conn, report, apply)
    _refuse_other_columns(conn, report)
    _convert_dead_letters(conn, report, apply)
    _convert_registration_events(conn, report, apply)
    _convert_restart_registrations(conn, report, apply)
    return report


def scan_stored_names(conn: sqlite3.Connection) -> StoredNamesReport:
    """Read-only: what the migration would convert, and every refusal it would raise."""
    return _run(conn, apply=False)


def migrate_78_to_79(conn: sqlite3.Connection) -> None:
    """Convert the stored names, or raise with the store unchanged."""
    report = _run(conn, apply=True)
    if report.refusals:
        raise StoredNameRefusal(
            f"{report.refusals} stored value(s) cannot be converted: "
            + "; ".join(report.refusal_samples)
        )
    log.info("Schema 78 -> 79 stored names: %s", report.counts())
