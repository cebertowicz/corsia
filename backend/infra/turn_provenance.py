"""The one rule for how a semantic message row is bound to a Turn.

Writers (``insert_message``) and readers (portable history) share it, so a row
the projection would refuse can never be written.
"""

from __future__ import annotations

from typing import Any

UNREADABLE_METADATA_REASON = "unreadable_metadata"
UNUSABLE_TURN_PROVENANCE_REASON = "unusable_turn_provenance"
SERVER_SCRIPTED_REASON = "server_scripted"
LEGACY_REASONS = frozenset(
    {
        "legacy_import",
        SERVER_SCRIPTED_REASON,
        "pre_turn_binding_migration",
        UNREADABLE_METADATA_REASON,
        UNUSABLE_TURN_PROVENANCE_REASON,
    }
)
# Labels only the projection may assign to a row it could not bind. Readers
# accept them; a writer must never persist one.
READER_ONLY_REASONS = frozenset({UNUSABLE_TURN_PROVENANCE_REASON})


class TurnProvenanceError(ValueError):
    """A row's Turn binding is missing, conflicting, or invalid."""


def refuse_reader_only_reason(message_id: str, meta: dict[str, Any]) -> None:
    """Raise if ``meta`` carries a label only the projection may assign."""
    if meta.get("turn_unbound_reason") in READER_ONLY_REASONS:
        raise TurnProvenanceError(
            f"semantic message {message_id!r} has invalid legacy provenance"
        )


def turn_provenance(row: dict[str, Any]) -> tuple[str | None, str | None]:
    """Return ``(turn_id, legacy_reason)`` for a ``{id, active_turn_id, _meta}`` row."""
    meta = row["_meta"]
    column_turn = row.get("active_turn_id")
    metadata_turn = meta.get("turn_id")
    for value in (column_turn, metadata_turn):
        if value is not None and (not isinstance(value, str) or not value):
            raise TurnProvenanceError(
                f"semantic message {row['id']!r} has invalid Turn provenance"
            )
    if column_turn is not None and metadata_turn not in {None, column_turn}:
        raise TurnProvenanceError(
            f"semantic message {row['id']!r} has conflicting Turn provenance"
        )
    turn_id = column_turn or metadata_turn
    has_legacy = "turn_unbound_reason" in meta
    if turn_id is not None:
        if has_legacy:
            raise TurnProvenanceError(
                f"semantic message {row['id']!r} has conflicting Turn provenance"
            )
        return str(turn_id), None
    if not has_legacy:
        raise TurnProvenanceError(
            f"semantic message {row['id']!r} has no Turn provenance"
        )
    legacy_reason = meta.get("turn_unbound_reason")
    if legacy_reason not in LEGACY_REASONS:
        raise TurnProvenanceError(
            f"semantic message {row['id']!r} has invalid legacy provenance"
        )
    return None, str(legacy_reason)
