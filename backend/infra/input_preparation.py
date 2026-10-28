"""Prepare document filesystem facts off the DB worker before atomic admission."""
from __future__ import annotations

import asyncio
import json
import logging

from core.message_history import is_human_input
from core.viewing_context import VIEWING_DOCUMENT_KEY, VIEWING_DOCUMENT_TARGET_KEY
from infra import product_events
from infra.database import TransactionOwnershipError, get_db
from infra.db_async import run_in_db
from infra.document_viewing import (
    PreparedViewingDocument, ViewingDocumentSource, materialize_viewing_document,
    resolve_viewing_source, viewing_document_target, viewing_shown_line,
)
from infra.thread_driver import admit_input

logger = logging.getLogger(__name__)

# Reports still being written. Holding them here keeps each one alive until it ends.
_pending_reports: set[asyncio.Future] = set()


def _viewing_target(role, metadata):
    """Pure check: only a human send that names a document has a target."""
    if not is_human_input(role, metadata):
        return None
    return viewing_document_target((metadata or {}).get(VIEWING_DOCUMENT_KEY))


def prepare_viewing_document(thread_id, role, message_id, metadata, db=None):
    """Return a frozen retry note or authorized path; perform only DB work here."""
    target = _viewing_target(role, metadata)
    if target is None:
        return None
    connection = db if db is not None else get_db()
    try:
        if connection.in_transaction:
            raise TransactionOwnershipError("Prepare viewing context before opening a write transaction")
        stored = connection.execute("SELECT metadata FROM messages WHERE id=?", (message_id,)).fetchone()
        prior = json.loads(stored["metadata"] or "{}") if stored else {}
        # Retries need no disk read, including after deletion. Admission still
        # checks the prior target again atomically with the duplicate decision.
        if prior.get(VIEWING_DOCUMENT_TARGET_KEY) == target:
            return PreparedViewingDocument(target, prior.get(VIEWING_DOCUMENT_KEY))
        shown_line = viewing_shown_line((metadata or {}).get(VIEWING_DOCUMENT_KEY))
        source = resolve_viewing_source(connection, thread_id, target, shown_line)
        if source is None:
            return PreparedViewingDocument(target, None, "inaccessible")
        return source
    finally:
        if db is None:
            connection.close()


def _report(thread_id: str | None, outcome: str) -> None:
    """Emit the one ``document.viewing_note`` event: a bounded outcome, no path.

    Called once the send is admitted, for a note resolved fresh for it: never
    for a retry or duplicate that keeps its stored note, never for a send
    admission refused, and always off the event loop.
    """
    product_events.emit_product_event(
        "document", "viewing_note", thread_id=thread_id,
        properties={"outcome": outcome, "contract_version": 1},
    )


def _report_ended(report: asyncio.Future) -> None:
    _pending_reports.discard(report)
    if not report.cancelled() and report.exception() is not None:
        logger.warning("document.viewing_note was not recorded", exc_info=report.exception())


def _report_without_waiting(thread_id: str | None, outcome: str) -> None:
    """Start the report and return: the send is committed and owes its caller the admission.

    Waiting here would hold every such send on an analytics write, and a caller
    cancelled during the wait would lose the admission of a row already stored.
    """
    report = asyncio.get_running_loop().run_in_executor(None, _report, thread_id, outcome)
    _pending_reports.add(report)
    report.add_done_callback(_report_ended)


async def admit_prepared_input(thread_id, *, role, message_id, metadata=None, db=None, **kwargs):
    """One entry point: DB access check, off-pool file work, then atomic admission."""
    # Most sends name no document, so they skip both the extra pool queue and
    # the thread hop; only a fresh source needs the disk.
    source = (
        await run_in_db(prepare_viewing_document, thread_id, role, message_id, metadata, db)
        if _viewing_target(role, metadata) is not None
        else None
    )
    viewing_document = (
        await asyncio.to_thread(materialize_viewing_document, source)
        if isinstance(source, ViewingDocumentSource)
        else source
    )
    admission = await run_in_db(
        admit_input, thread_id, role=role, message_id=message_id, metadata=metadata,
        viewing_document=viewing_document, db=db, **kwargs,
    )
    outcome = viewing_document.outcome if viewing_document is not None else None
    if outcome is not None and not admission.accepted.duplicate:
        _report_without_waiting(thread_id, outcome)
    return admission
