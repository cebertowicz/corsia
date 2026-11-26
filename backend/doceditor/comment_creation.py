"""Create-time live range ownership shared by the three comment adapters."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from doceditor import comments
from doceditor.comment_action_errors import CommentSelectionChanged, passage_gone
from doceditor.doc_images import widen_to_image
from doceditor.image_regions import check_region_target
from doceditor.live_document import IdentityInUse
from doceditor.live_text import utf16_slice
from doceditor.quote_anchor import anchor_context, kept_place, resolve_quote
import infra.app_loop as app_loop
from infra import write_effects
from infra.db_async import run_in_db


@dataclass(frozen=True)
class Anchor:
    quote: str
    start: int
    end: int
    prefix: str | None
    suffix: str | None
    text: str


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


def kept_sides(request) -> bool:
    """A person's new comment sent with its kept draft's sides, which place it when no capture is held for it."""
    return (request.browser and bool(request.quote) and not request.parent_id and not request.review
            and request.kept_prefix is not None and request.kept_suffix is not None)


def kept_anchor(text: str, request) -> Anchor:
    """The kept draft's passage in ``text``, found by its quote with both kept sides, or a refusal."""
    place = kept_place(text, request.quote, prefix=request.kept_prefix, suffix=request.kept_suffix)
    if place is None:
        raise passage_gone()
    return Anchor(request.quote, place[0], place[1], request.kept_prefix, request.kept_suffix, text)


def browser_anchor(live, identifier: str | None, request=None) -> Anchor:
    """The span this comment's own capture reserved, anchored on the text the live document holds there now.

    With no capture held (the page that made it reloaded or went), a ``request`` carrying its kept draft's
    sides is placed by them instead (`kept_anchor`).
    """
    text = live.text
    span = live.held_span(identifier)
    if span is None and request is not None and kept_sides(request):
        return kept_anchor(text, request)
    quote = utf16_slice(text, *span) if span is not None else None
    context = anchor_context(text, *span, quote) if span is not None and quote else None
    if span is None or not quote or context is None:
        live.release(identifier)
        raise CommentSelectionChanged("The text you selected changed or was removed. Select it again; your draft is kept.")
    return Anchor(quote, span[0], span[1], context[0], context[1], text)


def quote_anchor(live, quote: str, near: str | None) -> Anchor:
    text = live.text
    start, end = resolve_quote(text, quote, near)
    prefix, suffix = anchor_context(text, start, end, quote) or (None, None)
    return Anchor(quote, start, end, prefix, suffix, text)


def image_anchor(live, quote: str, near: str | None) -> Anchor:
    """An agent's region in a document: the whole markdown of the image its quote is inside."""
    text = live.text
    start, end = resolve_quote(text, quote, near)
    image_quote, image_start, image_end, prefix, suffix = widen_to_image(text, start, end)
    return Anchor(image_quote, image_start, image_end, prefix, suffix, text)


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
    prepare = None
    if request.quote and not request.parent_id and not request.review:
        if request.browser:
            prepare = lambda live: browser_anchor(live, client_id, request)
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
