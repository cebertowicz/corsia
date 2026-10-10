"""Recover pending artifact relocation intents without growing the writer module."""

from __future__ import annotations

from loguru import logger

from infra.database import TransactionOwnershipError, get_db


def recover_interrupted_relocations(*, db=None) -> list[str]:
    """Forward-complete resolvable relocation intents and retain ambiguous ones."""
    from registry.relocate import _perform_relocation

    connection = db if db is not None else get_db()
    if connection.in_transaction:
        raise TransactionOwnershipError(
            "relocation recovery requires a clean connection before file work"
        )
    try:
        rows = connection.execute(
            "SELECT id FROM artifact_relocation_intents ORDER BY created_at"
        ).fetchall()
        recovered = []
        for row in rows:
            try:
                _perform_relocation(connection, row["id"])
            except Exception as exc:
                logger.warning(
                    f"relocation intent {row['id']} could not be recovered: {exc!r}"
                )
                continue
            recovered.append(row["id"])
        return recovered
    finally:
        if db is None:
            connection.close()
