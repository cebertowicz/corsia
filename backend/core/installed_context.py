"""What one conversation was installed with, retained until its next install.

The installed-context snapshot is the responsibility map plus every static
context source (system, project, role, thread bootloaders, skills index) as
rendered at one install boundary, and the role facts those sources were
selected for. Every harness reads it through the shared collector, so a
source edit reaches a running conversation only at the next checkpoint
install, model switch, scope change, or conductor designation, and never
because a Turn started, a process relaunched, or the backend restarted.

The boundary record is the latest installed checkpoint's receipt, else the
thread's first message. The snapshot rides on that record's metadata: the map
and role facts under the public map key, the source bytes under a private
``_corsia_`` key that transcript projections never publish.

Collection is read-only. When the boundary already retains sources for this
execution, the collector restores them and reads no context file; otherwise
it reads disk once and returns an ``InstalledDelivery`` naming the boundary
record it read and the exact rows it delivered. That delivery rides on the
prepared context as a private attribute, and whatever delivers it persists
exactly those rows through ``persist_installed_delivery``: the send dispatch,
the Native mid-Turn checkpoint continuation, the Native cache warm, every
Claude Code process launch and spawn, and every cold Codex start.

- a checkpoint install creates its receipt with the map alone;
- the first dispatch after a boundary retains the rows it delivered, and
  binds the runtime epoch if the receipt left it unbound (``None``: a model
  switch whose destination commits its epoch later);
- a dispatch whose key no longer matches (scope change, conductor
  designation, abandoned switch) crossed a boundary itself, so it replaces
  the snapshot with what it delivered;
- the write is a compare-and-set: when the boundary record, or its snapshot,
  changed after collection, nothing is written (``reason=boundary-moved``)
  and the next dispatch collects against the new boundary;
- writing a boundary drops the source bytes from every superseded record.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, fields
from typing import Any

from core.effective_context import ContextSourceEntry

log = logging.getLogger(__name__)

RESPONSIBILITY_MAP_SNAPSHOT = "responsibility_map_snapshot"
INSTALLED_SOURCES = "_corsia_installed_context_sources"
ROLE_FACTS = "role_facts"
ADOPTION_RETAINED = "retained"
ADOPTION_NO_ANCHOR = "current-no-anchor"
ADOPTION_KEY_MISMATCH = "key-mismatch"
_ENTRY_FIELDS = tuple(item.name for item in fields(ContextSourceEntry))


@dataclass(frozen=True)
class InstalledDelivery:
    """The static rows one collection delivered, and where they are retained.

    Never published: it rides on a prepared context, not in ``agent_facts``.
    ``stored`` is the boundary snapshot as read at collection; ``snapshot`` is
    what the dispatch delivered. They are equal when nothing needs writing.
    """

    thread_id: str
    adoption: str
    boundary_id: str | None
    stored: dict[str, Any] | None
    snapshot: dict[str, Any]

    @property
    def pending(self) -> bool:
        return self.boundary_id is not None and self.snapshot != self.stored


@dataclass(frozen=True)
class _PreparedInstalledWrite:
    """The compare-and-set values prepared before acquiring the write lock."""

    message_id: str
    expected_raw_metadata: Any
    serialized_metadata: str


def serialize_sources(sources: list[ContextSourceEntry]) -> list[dict[str, Any]]:
    """Render exact, restorable source rows."""
    rows: list[dict[str, Any]] = []
    for source in sources:
        frozen = source.frozen_copy()
        rows.append({name: getattr(frozen, name) for name in _ENTRY_FIELDS})
    return rows


def restore_sources(rows: list[dict[str, Any]]) -> list[ContextSourceEntry]:
    """Rebuild installed rows while accepting legacy workspace origins."""
    from core.legacy_context_origin import normalize_legacy_row

    return [
        ContextSourceEntry(**{name: normalized.get(name) for name in _ENTRY_FIELDS
                              if name in normalized})
        for row in rows
        for normalized in (normalize_legacy_row(row),)
    ]


def map_only(snapshot: dict[str, Any]) -> dict[str, Any]:
    """The publishable responsibility map of one snapshot."""
    return {
        key: value for key, value in snapshot.items()
        if key not in {"sources", ROLE_FACTS}
    }


def installed_parts(rows: list[ContextSourceEntry], origin: str) -> list[str]:
    """Delivered file bytes of one static origin, in collection order."""
    return [
        row.prompt_content for row in rows
        if row.origin_section == origin and row.inclusion == "included"
        and row.prompt_content is not None
    ]


def installed_project_context(
    workspace: Any, project_id: str, rows: list[ContextSourceEntry],
) -> str | None:
    """Render project context from installed rows under live project headers."""
    from core.project_context import load_project_context

    return load_project_context(
        workspace, project_id, file_parts=installed_parts(rows, "project"),
    )


def _metadata(raw: Any) -> dict[str, Any] | None:
    if raw is None:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def snapshot_from_metadata(raw: Any) -> dict[str, Any] | None:
    """Join one record's public map and private source bytes."""
    metadata = raw if isinstance(raw, dict) else _metadata(raw)
    if not metadata:
        return None
    snapshot = metadata.get(RESPONSIBILITY_MAP_SNAPSHOT)
    if not isinstance(snapshot, dict):
        return None
    sources = metadata.get(INSTALLED_SOURCES)
    return {**snapshot, "sources": sources} if isinstance(sources, list) else snapshot


def with_installed_snapshot(
    metadata: dict[str, Any], snapshot: dict[str, Any],
) -> dict[str, Any]:
    """Store one snapshot: map and role facts publicly, source bytes privately."""
    public = {key: value for key, value in snapshot.items() if key != "sources"}
    updated = {**metadata, RESPONSIBILITY_MAP_SNAPSHOT: public}
    updated.pop(INSTALLED_SOURCES, None)
    if isinstance(snapshot.get("sources"), list):
        updated[INSTALLED_SOURCES] = snapshot["sources"]
    return updated


def _first_message(thread_id: str, db):
    row = db.execute(
        "SELECT id,metadata FROM messages WHERE thread_id=? AND seq IS NOT NULL "
        "ORDER BY seq LIMIT 1",
        (thread_id,),
    ).fetchone()
    if row is None:
        row = db.execute(
            "SELECT id,metadata FROM messages WHERE thread_id=? AND input_order=1",
            (thread_id,),
        ).fetchone()
    return row


def _checkpoint_receipt(thread_id: str, db):
    row = db.execute(
        "SELECT c.id FROM checkpoints AS c JOIN threads AS t ON t.id=c.thread_id "
        "WHERE c.thread_id=? AND c.history_epoch=t.history_epoch "
        "AND c.status='installed' ORDER BY c.generation_order DESC LIMIT 1",
        (thread_id,),
    ).fetchone()
    if row is None:
        return None
    return db.execute(
        "SELECT id,metadata FROM messages WHERE thread_id=? AND id=?",
        (thread_id, f"checkpoint-receipt:{row['id']}"),
    ).fetchone()


def _boundary_record(thread_id: str, db) -> tuple[str, dict[str, Any], Any] | None:
    row = _checkpoint_receipt(thread_id, db) or _first_message(thread_id, db)
    if row is None:
        return None
    metadata = _metadata(row["metadata"])
    return (str(row["id"]), metadata, row["metadata"]) if metadata is not None else None


def read_boundary(thread_id: str, db) -> tuple[str | None, dict[str, Any] | None]:
    """Return the current boundary record id and the snapshot it stores."""
    record = _boundary_record(thread_id, db)
    if record is None:
        return None, None
    return record[0], snapshot_from_metadata(record[1])


def _matches(snapshot: dict[str, Any] | None, expected: dict[str, Any]) -> bool:
    # An unbound (None) runtime epoch matches; the next dispatch binds it.
    return bool(
        snapshot
        and snapshot.get("model") == expected.get("model")
        and snapshot.get("execution_epoch") == expected.get("execution_epoch")
        and snapshot.get("project_id") == expected.get("project_id")
        and snapshot.get("runtime_context_epoch") in (
            None, expected.get("runtime_context_epoch"),
        )
    )


def installed_map(
    stored: dict[str, Any] | None, current: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    """Choose the map this execution delivers and name why.

    ``retained`` means the boundary also holds sources and role facts, so the
    collector restores them and reads no context file.
    """
    if stored is None:
        return current, ADOPTION_NO_ANCHOR
    if not _matches(stored, current):
        return current, ADOPTION_KEY_MISMATCH
    bound = {
        **map_only(stored),
        "runtime_context_epoch": current.get("runtime_context_epoch"),
    }
    retained = isinstance(stored.get("sources"), list) and ROLE_FACTS in stored
    return bound, ADOPTION_RETAINED if retained else ADOPTION_NO_ANCHOR


def _prepare_installed_write(
    db, delivery: InstalledDelivery,
) -> tuple[_PreparedInstalledWrite | None, str]:
    """Read and serialize a delivery before the write lock is acquired."""
    record = _boundary_record(delivery.thread_id, db)
    if record is None or record[0] != delivery.boundary_id:
        return None, "boundary-moved"
    message_id, metadata, raw_metadata = record
    stored = snapshot_from_metadata(metadata)
    if stored == delivery.snapshot:
        # Another write point for this same dispatch already retained it.
        return None, "retained"
    if stored != delivery.stored:
        return None, "boundary-moved"
    return _PreparedInstalledWrite(
        message_id=message_id,
        expected_raw_metadata=raw_metadata,
        serialized_metadata=json.dumps(
            with_installed_snapshot(metadata, delivery.snapshot), sort_keys=True,
        ),
    ), "ready"


def _retain_prepared_installed_delivery(
    db, delivery: InstalledDelivery, prepared: _PreparedInstalledWrite,
) -> str:
    """Write a prepared snapshot only if the canonical boundary is unchanged."""
    record = _boundary_record(delivery.thread_id, db)
    if (
        record is None
        or record[0] != prepared.message_id
        or record[2] != prepared.expected_raw_metadata
    ):
        return "boundary-moved"
    db.execute(
        "UPDATE messages SET metadata=? WHERE thread_id=? AND id=?",
        (prepared.serialized_metadata, delivery.thread_id, prepared.message_id),
    )
    # Superseded boundaries keep their public map but drop their source bytes.
    db.execute(
        "UPDATE messages SET metadata=json_remove(metadata, ?) "
        "WHERE thread_id=? AND id<>? AND instr(metadata, ?) > 0 "
        "AND json_valid(metadata)",
        (f'$."{INSTALLED_SOURCES}"', delivery.thread_id, prepared.message_id,
         INSTALLED_SOURCES),
    )
    return "written"


def persist_installed_delivery(
    delivery: InstalledDelivery | None, *, mode: str = "cold", db=None,
) -> bool:
    """Retain what one dispatch delivered and log why it was or was not.

    A live provider resume sends no sources, so it is logged but never
    written. ``db`` names an enclosing write transaction's connection.
    """
    from infra.database import get_db, write_transaction

    if delivery is None:
        reason, adoption = "unrecorded", "unrecorded"
    else:
        adoption = delivery.adoption
        if mode == "provider_resume":
            reason = "provider-resume"
        elif not delivery.pending:
            reason = "no-boundary" if delivery.boundary_id is None else "retained"
        else:
            prepared, reason = _prepare_installed_write(
                db if db is not None else get_db(), delivery,
            )
            if prepared is not None:
                with write_transaction(db, scope="persist_installed_context") as conn:
                    reason = _retain_prepared_installed_delivery(conn, delivery, prepared)
    written = reason == "written"
    log.info(
        "installed_context.dispatch thread=%s mode=%s adoption=%s written=%s "
        "reason=%s",
        getattr(delivery, "thread_id", None), mode, adoption, written, reason,
    )
    return written


def _prepared_delivery(prepared: Any) -> InstalledDelivery | None:
    rendered = getattr(prepared, "rendered", None)
    payload = getattr(rendered, "payload", prepared)
    for carrier in (
        payload,
        getattr(payload, "seed_reconstruction", None),
        getattr(payload, "reconstruction", None),
    ):
        delivery = getattr(carrier, "installed", None)
        if isinstance(delivery, InstalledDelivery):
            return delivery
    return None


def persist_prepared_installed_context(thread_id: str, prepared: Any) -> bool:
    """Retain the static rows one prepared dispatch is about to deliver."""
    delivery = _prepared_delivery(prepared)
    if delivery is not None and delivery.thread_id != thread_id:
        raise ValueError(
            f"installed delivery for {delivery.thread_id} dispatched on {thread_id}"
        )
    return persist_installed_delivery(
        delivery, mode=getattr(prepared, "mode", None) or "cold",
    )
