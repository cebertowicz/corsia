"""Canonical DB create/feedback transaction for validated comment adapters."""
from __future__ import annotations

from dataclasses import dataclass

from doceditor import comments, doc_images, feedback_batches, image_regions
from doceditor.comment_creation import Anchor, CommentSelectionChanged, CreateReceipt
from doceditor.doc_text import read_doc_text
from doceditor.quote_anchor import anchor_context, resolve_quote
from infra.auth import may_access_artifact
from infra.database import get_db, write_transaction
from registry import models as registry_models, service as registry_service


class CommentAccessDenied(PermissionError):
    """The validated target stopped being accessible before its DB create."""


@dataclass(frozen=True)
class CommentInput:
    artifact_id: str | None
    path: str | None
    body: str
    author: str
    author_kind: str
    user_id: str
    author_thread_id: str | None = None
    parent_id: str | None = None
    review: bool = False
    replacement: str | None = None
    quote: str | None = None
    near: str | None = None
    start: int | None = None
    end: int | None = None
    browser: bool = False
    live_capture: bool = False
    registration: dict | None = None
    submit: bool = False
    # A checked (x, y, w, h) in fractions of the image (DCOL-FR13.3).
    region: tuple[float, float, float, float] | None = None


def _anchor(request: CommentInput, anchor: Anchor | None, duplicate: dict | None) -> tuple:
    if duplicate is not None:
        return (duplicate["anchor_text"], duplicate["from_off"], duplicate["to_off"],
                duplicate["anchor_prefix"], duplicate["anchor_suffix"])
    if anchor is not None:
        return anchor.quote, anchor.start, anchor.end, anchor.prefix, anchor.suffix
    if request.live_capture:
        raise CommentSelectionChanged("The live document closed. Reopen it; your draft is kept.")
    if request.parent_id or request.review or not request.quote:
        return request.quote, request.start, request.end, None, None
    if request.browser:
        try:
            context = anchor_context(read_doc_text(request.path), request.start, request.end, request.quote)
        except (OSError, ValueError):
            context = None
        return request.quote, request.start, request.end, *(context or (None, None))
    text = read_doc_text(request.path)
    start, end = resolve_quote(text, request.quote, request.near)
    if request.region is not None:
        return doc_images.widen_to_image(text, start, end)
    return request.quote, start, end, *(anchor_context(text, start, end, request.quote) or (None, None))


def _image(db, request: CommentInput, quote: str | None) -> tuple[str | None, str]:
    """The (image_src, image_rev) a new region comment is placed on.

    An image artifact is its own image; in a document it is the file the
    anchored markdown shows, resolved here and never taken from the client.
    """
    if image_regions.is_image_path(request.path):
        return None, image_regions.image_revision(image_regions.require_image(request.path))
    if request.artifact_id is not None:
        artifact = registry_models.get_artifact(request.artifact_id, db=db)
        owner = artifact["parent_thread_id"] if artifact else None
    else:
        owner = request.author_thread_id
    image = doc_images.image_file(db, doc_path=request.path, markdown=quote, owner_thread_id=owner)
    return str(image), image_regions.image_revision(image)


def create_in_db(request: CommentInput, anchor: Anchor | None, identifier: str, receipt: CreateReceipt):
    """The commit receipt belongs to the outer transaction that creates row and feedback."""
    db = get_db()
    try:
        if request.artifact_id is not None:
            artifact = registry_models.get_artifact(request.artifact_id, db=db)
            if artifact is None or not may_access_artifact(artifact['parent_thread_id'], request.user_id, db=db):
                raise CommentAccessDenied('Artifact not found')
        duplicate = comments.get_comment(identifier, db=db)
        quote, start, end, prefix, suffix = _anchor(request, anchor, duplicate)
        image_src = image_rev = None
        if request.region is not None and duplicate is None:
            image_src, image_rev = _image(db, request, quote)
        artifact_id = request.artifact_id
        registered = request.registration is not None
        with write_transaction(db, scope="create_document_comment") as connection:
            receipt.bind(connection)
            if request.registration is not None:
                artifact_id = registry_service.register_write_strict(
                    request.author_thread_id, request.registration["path"], db=connection,
                )["id"]
            row, created = comments.create_with_status(
                artifact_id, body=request.body, author=request.author, author_kind=request.author_kind,
                author_thread_id=request.author_thread_id, parent_id=request.parent_id,
                anchor_text=quote, from_off=start, to_off=end, anchor_prefix=prefix, anchor_suffix=suffix,
                review=request.review, replacement=request.replacement, client_id=identifier,
                region=request.region, image_rev=image_rev, image_src=image_src, db=connection,
            )
            feedback = feedback_batches.record_comment(connection, row, created=created, submit=request.submit)
        return row, registered, feedback
    except BaseException:
        receipt.failed(identifier, db, artifact_id=request.artifact_id, author=request.author)
        raise
    finally:
        db.close()
