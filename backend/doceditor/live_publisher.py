"""Publish ordered live-write evidence off the lane, with one application commit."""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import replace
import logging

from doceditor import comments, comment_anchoring
from doceditor.live_publication_record import OwedPublication, Publication
from doceditor.live_store import LiveStore, StoreRefused
from doceditor.live_failure_budget import FailureBudget
from doceditor.live_text import clone
from infra.database import get_db, write_transaction
from registry import service, write_preparation
from registry.identity_metadata import parse_metadata
from registry.registration_origin import RegistrationOrigin
from registry.shared_document_events import (
    prepare_owner_report,
    record_post_write_events,
    report_owner_write,
)

log = logging.getLogger(__name__)
_STAMP = "live_publication"
PUBLICATION_ATTEMPTS = 3


def publish_one(store: LiveStore, row: OwedPublication, *, db=None,
                stopped: Callable[[], bool] = lambda: False) -> None:
    """Commit registration, exact owner events and stamp before deleting evidence."""
    if stopped():
        return
    connection = db if db is not None else get_db()
    try:
        after = str(clone(row.mirror_state)["text"])
        register_publication(row.publication, after, row.result_hash,
                             stamp={"store_id": store.store_id, "seq": row.seq}, db=connection)
        if not stopped():
            store.delete_publication(row.seq)
    finally:
        if db is None:
            connection.close()


def register_publication(publication: Publication, text: str, result_hash: str, *,
                         stamp: dict | None, db) -> None:
    """Register ``text`` as written under ``publication``'s actor and decision.

    With a store stamp, a row this store already published is skipped. With
    none (a damaged store's one save), the registration always runs.
    """
    path = publication.target_path()
    shared = publication.shared_target()
    owner = (shared.actor_principal if shared else None) or publication.actor
    prepared = write_preparation.prepare_file_write(str(path), owner, db=db)
    if prepared is None:
        raise ValueError("Live publication target is outside registrable roots")
    # The disk may already hold a newer write. Hash and event bytes come
    # from this row, never from the current disk or artifact metadata.
    prepared = replace(prepared, local_sha256=result_hash)
    before = str(clone(publication.prior_mirror)["text"]).encode("utf-8")
    after = text.encode("utf-8")
    artifact = service.find_file_identity(prepared.identity, db=db)
    positions = comment_anchoring.prepare_positions(artifact["id"], text, db,
                                                    ranges=publication.ranges) if artifact else []
    authority = publication.authority()
    payload = prepare_owner_report(authority, artifact, path=path, before=before, after=after, db=db,
                                   classification=shared.classification if shared else None)
    origin = RegistrationOrigin(**publication.origin) if publication.origin else None
    with write_transaction(db, scope="publish_live_document"):
        current = service.find_file_identity(prepared.identity, db=db)
        prior = (parse_metadata(current["metadata"]) if current else {}).get(_STAMP, {})
        published = stamp is not None and prior.get("store_id") == stamp["store_id"] \
            and prior.get("seq", 0) >= stamp["seq"]
        if not published:
            artifact = service.register_write_strict(
                owner, prepared, publication.description, origin=origin, db=db,
                actor_thread_id=publication.actor,
                metadata={"last_written_sha256": result_hash, **({_STAMP: stamp} if stamp else {})},
            )
            if positions:
                comments.update_positions(artifact["id"], positions, db=db)
            if shared:
                record_post_write_events(shared, artifact, before=before, after=after,
                                         actor_thread_id=publication.actor, db=db, prepared=payload)
            else:
                report_owner_write(authority, artifact, path=path, before=before, after=after,
                                   thread_id=publication.actor, db=db, prepared=payload)
    notification = service.find_file_identity(prepared.identity, db=db)
    if notification is not None:
        service.notify_changed(notification)


class DocumentPublisher:
    """One background owner drains rows in order; failure retains them for retry."""

    def __init__(self, store: LiveStore):
        self.store = store
        self.task: asyncio.Task | None = None
        self.retry: asyncio.TimerHandle | None = None
        self.delay = 1.0
        self.closed = False
        self.stop_cause: StoreRefused | None = None
        self.requested = False
        self.drained = False
        self.blocked = lambda kind, message, provenance: None
        self.on_drained: Callable[[], None] = lambda: None
        self.budget = FailureBudget(store, "publication_failed", PUBLICATION_ATTEMPTS,
                                    lambda kind, message, provenance: self.blocked(kind, message, provenance))
        # Set and replaced whenever a row drains or the budget blocks; writes waiting
        # for a drain wake on it instead of polling.
        self.changed = asyncio.Event()

    def _signal(self) -> None:
        self.changed.set()
        self.changed = asyncio.Event()

    def require_running(self) -> None:
        """A permanent stop cannot present its retained debt as a healthy drain."""
        if not self.closed:
            return
        if self.stop_cause is not None:
            raise self.stop_cause
        from doceditor.document_save import DocumentSaveError
        raise DocumentSaveError("publication_failed", "Live updates couldn't be shared for this document.")

    async def retry_blocked(self) -> None:
        """Only an explicit Retry resets the durable failure budget."""
        self.require_running()
        await self.budget.clear()
        self.delay = 1.0
        self.notify()

    def notify(self) -> None:
        """Schedule publication without extending an operation or its Stop join."""
        if self.closed:
            return
        self.requested = True
        self.drained = False
        if self.retry:
            self.retry.cancel()
            self.retry = None
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._drain())
            # The lifetime owner may retire a stopped idle publisher only after join.
            self.task.add_done_callback(lambda task: self.on_drained())

    async def _drain(self) -> None:
        try:
            while not self.closed:
                self.requested = False
                if await self.budget.blocked_reason():
                    return
                row = await asyncio.to_thread(self.store.next_publication)
                if row is None:
                    if self.requested:
                        continue
                    self.delay = 1.0
                    self.drained = True
                    self.on_drained()
                    return
                if self.closed:
                    return
                await asyncio.to_thread(publish_one, self.store, row, stopped=lambda: self.closed)
                if self.closed:
                    return
                self._signal()
                await self.budget.clear()
        except Exception as error:
            log.warning("Live document publication remains owed", exc_info=True)
            if self.closed:
                return
            from doceditor.live_admission import falls_back_to_plain, store_refusal
            if isinstance(error, StoreRefused) and falls_back_to_plain(store_refusal(error).kind):
                # Even readable meta must stay untouched once any store page is damaged.
                self.stop(error)
                return
            reason = await self.budget.record(error)
            if reason:
                self._signal()
            elif not self.closed:
                self.retry = asyncio.get_running_loop().call_later(self.delay, self.notify)
                self.delay *= 2

    def stop(self, cause: StoreRefused | None = None) -> None:
        """Stop new publication immediately; an in-flight registration still needs joining."""
        if cause is not None and self.stop_cause is None:
            self.stop_cause = cause
        self.closed = True
        if self.retry:
            self.retry.cancel()
            self.retry = None
        self._signal()

    async def close(self) -> None:
        """Stop the retry timer and finish any transaction already started."""
        self.stop()
        if self.task:
            await asyncio.shield(self.task)
