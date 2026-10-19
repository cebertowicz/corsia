"""Create-time live range ownership shared by the three comment adapters."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from doceditor import comments
from doceditor.doc_images import check_image_quote, widen_to_image
from doceditor.image_regions import check_region_target
from doceditor.live_document import IdentityInUse
from doceditor.quote_anchor import anchor_context, resolve_quote
from infra import app_loop, write_effects
from infra.db_async import run_in_db


class CommentSelectionChanged(ValueError):
    """The selection a browser captured can no longer be anchored; its draft is kept and it selects again."""


@dataclass(frozen=True)
class Anchor:
    quote: str
    start: int
    end: int
    prefix: str | None
    suffix: str | None


@dataclass
class CreateReceipt:
    """Commit is reported by the canonical transaction, never by cancellation."""
    committed: bool = False
    rolled_back: bool = False

    def bind(self, db) -> None:
        write_effects.after_commit(db, self.commit)

    def commit(self) -> None:
        self.committed = True

    def failed(self, identifier: str, db, *, artifact_id: str | None = None, author: str | None = None) -> None:
        # An exception after commit is not rollback. Verify the exact row after
        # the canonical transaction has unwound; unknown outcomes keep identity.
        if not db.in_transaction:
            row = comments.get_comment(identifier, db=db)
            if row is not None and (artifact_id is None or row["artifact_id"] == artifact_id) and (author is None or row["author"] == author):
                self.committed = True
            else:
                self.rolled_back = True


def browser_anchor(live, identifier: str | None, quote: str) -> Anchor:
    """The span this comment's own capture reserved, which the live text has carried since."""
    span = live.held_span(identifier)
    context = anchor_context(live.text, *span, quote) if span is not None else None
    if context is None:
        live.release(identifier)
        raise CommentSelectionChanged("The text you selected changed or was removed. Select it again; your draft is kept.")
    return Anchor(quote, *span, *context)


def quote_anchor(live, quote: str, near: str | None) -> Anchor:
    text = live.text
    start, end = resolve_quote(text, quote, near)
    return Anchor(quote, start, end, *(anchor_context(text, start, end, quote) or (None, None)))


def image_anchor(live, quote: str, near: str | None) -> Anchor:
    """An agent's region in a document: the whole markdown of the image its quote is inside."""
    start, end = resolve_quote(live.text, quote, near)
    return Anchor(*widen_to_image(live.text, start, end))


async def create_comment_owned(*, path: str | None, artifact_id: str | None,
                               client_id: str | None, author: str, user_id: str, prepare, worker):
    """Adapters validate shape/auth/access first, then await owner work before DB create.

    ``worker`` gets immutable anchor coordinates and a per-call commit receipt.
    Only the owner touches ranges. The comment mutex never holds browser durability.
    """
    from doceditor.live_runtime import get_documents

    identifier = client_id or uuid4().hex
    manager = get_documents()
    if manager is None:
        return await run_in_db(worker, None, identifier, CreateReceipt())
    return await app_loop.await_on_owner(
        _create_on_owner(manager, path, artifact_id, identifier, author, user_id, prepare, worker), owner=manager._loop,
    )


async def _create_on_owner(manager, path, artifact_id, identifier, author, user_id, prepare, worker):
    if manager._closed:
        raise app_loop.AppLoopUnavailable("document owner is closed")
    if path is None:
        return await run_in_db(worker, None, identifier, CreateReceipt())
    # Existing lifetime orders opening/eviction too; it never holds durability.
    lifetime = manager._lifetime(Path(path))
    async with lifetime.lock:
        if manager._closed:
            raise app_loop.AppLoopUnavailable("document owner closed before comment preparation")
        return await _reserve_and_create(manager, path, artifact_id, identifier, author, user_id, prepare, worker)


async def _reserve_and_create(manager, path, artifact_id, identifier, author, user_id, prepare, worker):
    entry = next((item for item in manager.documents.values()
                  if path is not None and item.live.path == Path(path)), None)
    if entry is None or prepare is None:
        return await run_in_db(worker, None, identifier, CreateReceipt())
    live = entry.live
    async with live.comment_mutex:
        if manager._closed or manager.documents.get(entry.artifact_id) is not entry:
            raise app_loop.AppLoopUnavailable("document was evicted before comment preparation")
        duplicate = await run_in_db(comments.get_comment, identifier)
        if duplicate is not None:
            if duplicate["artifact_id"] != artifact_id or duplicate["author"] != author:
                raise comments.CrossArtifactIdError("client_id belongs to another artifact or author")
            # Idempotency precedes anchoring and never replaces identity; a reservation held under it is spent.
            live.release(identifier)
            return await run_in_db(worker, None, identifier, CreateReceipt())
        try:
            live.reserved_by(identifier, user_id)
        except IdentityInUse as exc:
            raise comments.CrossArtifactIdError("client_id belongs to another artifact or author") from exc
        anchor = prepare(live)
        if identifier in live.ranges:
            raise CommentSelectionChanged("Comment identity is still settling; try again.")
        # The capture's reserved span becomes the comment's own range.
        live.adopt(identifier, anchor.start, anchor.end)
        receipt = CreateReceipt()
        try:
            return await run_in_db(worker, anchor, identifier, receipt)
        finally:
            # db_async has joined this exact worker even on repeated cancellation.
            if receipt.rolled_back and not receipt.committed:
                live.ranges.pop(identifier, None)
                live.snapshot = (live.text, dict(live.ranges))


async def create_validated_comment(request, client_id):
    """One producer path after transport-specific shape, author and access checks."""
    from functools import partial
    from doceditor.comment_create_db import create_in_db

    if request.region is not None:
        check_region_target(request.path, request.quote)
        if request.browser and request.quote:
            check_image_quote(request.quote)
    prepare = None
    if request.quote and not request.parent_id and not request.review:
        if request.browser:
            prepare = lambda live: browser_anchor(live, client_id, request.quote)
        elif request.region is not None:
            prepare = lambda live: image_anchor(live, request.quote, request.near)
        else:
            prepare = lambda live: quote_anchor(live, request.quote, request.near)
    result = await create_comment_owned(
        path=request.path, artifact_id=request.artifact_id, client_id=client_id,
        author=request.author, user_id=request.user_id, prepare=prepare, worker=partial(create_in_db, request),
    )
    if request.author_kind == "user":
        from jobs.corrections_recording import notify_doc_comment

        notify_doc_comment(str(result[0]["id"]))
    return result
