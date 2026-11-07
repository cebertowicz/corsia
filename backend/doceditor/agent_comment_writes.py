"""The one gate every agent write to a comment thread passes through: the thread's writer turn.

Both agent transports, the native tools (``tools/documents.py``) and the ``corsia doc`` routes
(``api/delegation_docs.py``), call these instead of the shared writers. An agent's reply, accept,
reject, resolve, re-open or edit on a thread someone else holds is saved in line (``writer_line``)
and not run; the caller returns the line's answer. An Agree and a new top-level comment are not
waits on a thread and run at once; a new suggestion on an open document waits instead for the turns
of the sections it spans (``section_writes``). ``post_as_is`` replays the saved writes holding the turn.
Person writes never wait: a person sending or clearing a reply releases their own turn. A write made
holding the turn settles its saved row inside the comment write's own transaction
(``writer_line.landed``), so a posted write is never left in line.
"""

from __future__ import annotations

import dataclasses
from typing import Any

from loguru import logger

from doceditor import comments as store, doc_images, writer_line, writer_turns
from doceditor.comment_action_errors import CommentActionError
from doceditor.comment_actions import check_agent_update, decide_suggestion, reopen_fields
from doceditor.comment_batch_actions import decide_all_from_author
from doceditor.comment_create_db import CommentInput
from doceditor.comment_creation import create_validated_comment
from doceditor.comment_permissions import check_decider
from doceditor.comment_state_updates import update_comment
from doceditor.document_save import DocumentSaveError, agent_document_writer
from doceditor.writer_line import Queued
from infra.auth import may_access_artifact
from infra.database import get_db, write_transaction
from infra.db_async import run_in_db
from infra.failure_reference import new_error_reference
from registry import models as registry_models
from registry import service as registry_service

__all__ = ["Queued", "decide", "decide_all", "post_as_is", "reply", "update"]


def _is_agree(fields: dict[str, Any]) -> bool:
    return fields == {"acked": True}


def _target(comment_id: str, user_id: str, check=None) -> tuple[str, str] | None:
    """The thread root and document of a comment the user may reach, after the write's own checks."""
    db = get_db()
    try:
        comment = store.get_comment(comment_id, db=db)
        artifact = registry_models.get_artifact(comment["artifact_id"], db=db) if comment else None
        if comment is None or artifact is None or not may_access_artifact(artifact["parent_thread_id"], user_id, db=db):
            return None
        if check is not None:
            check(db, comment, artifact)
        return str(comment.get("parent_id") or comment["id"]), str(comment["artifact_id"])
    finally:
        db.close()


def _admit(root_id: str, artifact_id: str, thread_id: str, user_id: str, op: dict):
    db = get_db()
    try:
        with write_transaction(db, scope="writer_line_admit") as connection:
            return writer_line.admit(
                connection, root_id=root_id, artifact_id=artifact_id, thread_id=thread_id, user_id=user_id, op=op)
    except BaseException:
        writer_turns.agent_exit(root_id, thread_id)
        raise
    finally:
        db.close()


def _admit_as_is(root_id: str, thread_id: str):
    db = get_db()
    try:
        with write_transaction(db, scope="writer_line_admit_as_is") as connection:
            return writer_line.admit_as_is(connection, root_id=root_id, thread_id=thread_id)
    except BaseException:
        writer_turns.agent_exit(root_id, thread_id)
        raise
    finally:
        db.close()


def _finish(root_id: str, thread_id: str) -> list[str]:
    db = get_db()
    try:
        with write_transaction(db, scope="writer_line_finish") as connection:
            return writer_line.finish(connection, root_id=root_id, thread_id=thread_id)
    finally:
        db.close()


async def _finish_and_drive(root_id: str, thread_id: str) -> None:
    """Release the turn and wake the next agent; a write that landed is already settled.

    A failure here strands no one: the turn is released in memory, and the continuation sweep
    serves any line still waiting (``writer_line.serve_waiting``).
    """
    try:
        await _drive(await run_in_db(_finish, root_id, thread_id))
    except Exception:
        writer_turns.agent_exit(root_id, thread_id)
        logger.exception("writer turn on {} was not passed on after {}'s write", root_id, thread_id)


def _serve_in_db(root_ids: list[str]) -> list[str]:
    db = get_db()
    try:
        with write_transaction(db, scope="writer_line_serve") as connection:
            return [thread for root_id in root_ids for thread in writer_line.serve(connection, root_id)]
    finally:
        db.close()


async def serve_freed(root_ids: list[str]) -> None:
    """A person left these threads: wake the first agent waiting on each."""
    if root_ids:
        await _drive(await run_in_db(_serve_in_db, root_ids))


def recover_line() -> list[str]:
    """At startup, after crashed turns are ended: wake again every saved write whose wake was lost."""
    db = get_db()
    try:
        with write_transaction(db, scope="writer_line_recover") as connection:
            return writer_line.recover(connection)
    finally:
        db.close()


async def _drive(thread_ids: list[str]) -> None:
    if thread_ids:
        from plans import public

        await public.drive_threads(thread_ids)


async def _through(root_id: str, artifact_id: str, thread_id: str, user_id: str, op: dict, run):
    """Run ``run`` holding the turn, or save ``op`` in line and return the line's answer."""
    admission = await run_in_db(_admit, root_id, artifact_id, thread_id, user_id, op)
    if isinstance(admission, Queued):
        return admission
    try:
        return await run()
    finally:
        # The write is accepted here, before any typing animation, so the next agent is woken now.
        await _finish_and_drive(root_id, thread_id)


# -- replies -----------------------------------------------------------------------------------


async def _create(request: CommentInput, client_id: str | None):
    row, registered, feedback, created = await create_validated_comment(request, client_id)
    if registered:
        registry_service.notify_changed(row["artifact_id"])
    await _drive(feedback["woken"])
    return row, registered, feedback, created


async def _suggest(request: CommentInput, client_id: str | None):
    """A new suggestion on an open document waits for the turns of the sections its quote spans."""
    from doceditor import section_writes
    from doceditor.live_tools import find_live_artifact

    entry = find_live_artifact(request.artifact_id) if request.artifact_id and request.author_thread_id else None
    call = {"tool": "doc_comment_create", "quote": request.quote, "near": request.near, "body": request.body,
            "replacement": request.replacement, "insert_after": request.insert_after}
    if entry is None:
        note = None
        if request.artifact_id and request.author_thread_id:  # closed: a woken agent's suggestion answers its wake
            note = await section_writes.settle_closed(None, request.author_thread_id, call, request.artifact_id)
        created = await _create(request, client_id)
        if note is not None:
            created[2]["note"] = note  # the feedback the tool result carries
        return created
    text = entry.live.snapshot[0]
    headings = section_writes.quote_sections(text, request.quote, request.near)
    op = section_writes.saved_op("suggest", call, text, headings)
    return await section_writes.through(request.artifact_id, headings, request.author_thread_id, op,
                                        lambda: _create(request, client_id))


async def reply(request: CommentInput, client_id: str | None):
    """Create an agent comment; a reply waits for its thread's turn, a suggestion for its sections'.

    Returns the creation result or ``Queued``. A plain new comment never waits.
    """
    if not request.parent_id:
        if request.replacement is not None and not request.review:
            return await _suggest(request, client_id)
        return await _create(request, client_id)
    target = await run_in_db(_target, request.parent_id, request.user_id)
    if target is None:
        return await _create(request, client_id)
    op = {"kind": "reply", "input": dataclasses.asdict(request), "client_id": client_id}
    root_id, artifact_id = target
    return await _through(root_id, artifact_id, request.author_thread_id, request.user_id, op,
                          lambda: _create(request, client_id))


# -- decisions ---------------------------------------------------------------------------------


def _decidable(user_id: str, thread_id: str):
    def check(db, comment, artifact):
        check_decider(user_id=user_id, actor_thread_id=thread_id, artifact=artifact, db=db)
        if comment.get("replacement") is None:
            raise CommentActionError("not_suggestion", "This comment is not a suggestion.")
    return check


async def _decide_now(comment_id: str, decision: str, *, thread_id: str, user_id: str, decided_by: str,
                      reason: str | None):
    return await decide_suggestion(
        comment_id, decision, user_id=user_id, decided_by=decided_by, actor_thread_id=thread_id,
        write=agent_document_writer(thread_id, user_id), reason=reason)


async def decide(comment_id: str, decision: str, *, thread_id: str, user_id: str, decided_by: str,
                 reason: str | None = None):
    """Accept or reject one suggestion as an agent, or save the decision in line."""
    target = await run_in_db(_target, comment_id, user_id, _decidable(user_id, thread_id))
    run = lambda: _decide_now(comment_id, decision, thread_id=thread_id, user_id=user_id,  # noqa: E731
                              decided_by=decided_by, reason=reason)
    if target is None:
        return await run()
    op = {"kind": "decide", "comment_id": comment_id, "decision": decision, "reason": reason, "decided_by": decided_by}
    root_id, artifact_id = target
    return await _through(root_id, artifact_id, thread_id, user_id, op, run)


async def decide_all(comment_id: str, decision: str, *, thread_id: str, user_id: str, decided_by: str,
                     reason: str | None = None) -> dict[str, Any]:
    """Decide every open suggestion by an author; those on threads someone else holds are saved in line and listed."""
    queued: list[dict[str, Any]] = []
    held: list[str] = []

    async def select(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        admitted = []
        for row in rows:
            op = {"kind": "decide", "comment_id": row["id"], "decision": decision, "reason": reason,
                  "decided_by": decided_by}
            admission = await run_in_db(_admit, row["id"], row["artifact_id"], thread_id, user_id, op)
            if isinstance(admission, Queued):
                queued.append({"comment_id": row["id"], "message": admission.text})
            else:
                held.append(row["id"])
                admitted.append(row)
        return admitted

    try:
        result = await decide_all_from_author(
            comment_id, decision, user_id=user_id, decided_by=decided_by, actor_thread_id=thread_id,
            write=agent_document_writer(thread_id, user_id), reason=reason, select=select)
    finally:
        for root_id in held:
            await _finish_and_drive(root_id, thread_id)
    if queued:
        result["queued"] = queued
    return result


# -- state updates -----------------------------------------------------------------------------


def _updatable(fields: dict[str, Any], thread_id: str):
    def check(db, comment, artifact):
        check_agent_update(comment, fields, actor_thread_id=thread_id, owner_thread_id=artifact["parent_thread_id"], db=db)
    return check


def _update_in_db(comment_id: str, fields: dict[str, Any], thread_id: str, user_id: str, regions: bool):
    db = get_db()
    try:
        comment = store.get_comment(comment_id, db=db)
        if comment is None:
            return None
        art = registry_models.get_artifact(comment["artifact_id"], db=db)
        if art is None or not may_access_artifact(art["parent_thread_id"], user_id, db=db):
            return None
        check_agent_update(comment, fields, actor_thread_id=thread_id, owner_thread_id=art["parent_thread_id"], db=db)
        moved = doc_images.region_patch(
            db, comment, dict(fields), doc_path=art["path"], owner_thread_id=art["parent_thread_id"]) if regions else fields
        patch = reopen_fields(comment, moved)
        with write_transaction(db, scope="patch_agent_comment") as connection:
            row = update_comment(connection, comment_id, patch, actor_thread_id=thread_id)
            if row is not None:
                writer_line.landed(connection, str(comment.get("parent_id") or comment["id"]), thread_id)
            return row, art["path"]
    finally:
        db.close()


async def update(comment_id: str, fields: dict[str, Any], *, thread_id: str, user_id: str, regions: bool = True):
    """Resolve, re-open, edit or move an agent's comment; returns (row, path), None when not found, or ``Queued``.

    An Agree on its own never waits. ``regions`` checks a moved image region (the native tool; the CLI has none).
    """
    run = lambda: run_in_db(_update_in_db, comment_id, fields, thread_id, user_id, regions)  # noqa: E731
    if _is_agree(fields):
        return await run()
    target = await run_in_db(_target, comment_id, user_id, _updatable(fields, thread_id))
    if target is None:
        return await run()
    op = {"kind": "update", "comment_id": comment_id, "fields": fields, "regions": regions}
    root_id, artifact_id = target
    return await _through(root_id, artifact_id, thread_id, user_id, op, run)


# -- post as is --------------------------------------------------------------------------------


async def _replay(op: dict[str, Any], thread_id: str, user_id: str):
    if op["kind"] == "reply":
        data = dict(op["input"])
        if data.get("region") is not None:
            data["region"] = tuple(data["region"])
        return {"comment": (await _create(CommentInput(**data), op.get("client_id")))[0]}
    if op["kind"] == "decide":
        return {"comment": await _decide_now(
            op["comment_id"], op["decision"], thread_id=thread_id, user_id=user_id,
            decided_by=op["decided_by"], reason=op.get("reason"))}
    updated = await run_in_db(_update_in_db, op["comment_id"], op["fields"], thread_id, user_id, op.get("regions", True))
    if updated is None:
        raise CommentActionError("not_found", "Comment not found")
    return {"comment": updated[0]}


async def post_as_is(comment_id: str, *, thread_id: str, user_id: str) -> dict[str, Any] | Queued:
    """Post this agent's saved writes on the thread, in the order it saved them, exactly once.

    The saved row is read and settled in the admitting transaction, so a second call finds nothing.
    A write that fails, for any reason, is reported with its saved text and the rest are still tried.
    """
    target = await run_in_db(_target, comment_id, user_id)
    if target is None:
        raise CommentActionError("not_found", "Comment not found")
    root_id = target[0]
    admission = await run_in_db(_admit_as_is, root_id, thread_id)
    if admission is None:
        raise CommentActionError("nothing_saved", "You have nothing saved in line on this thread.")
    if isinstance(admission, Queued):
        return admission
    posted: list[dict[str, Any]] = []
    failed: list[dict[str, str]] = []
    try:
        for op in admission.ops:
            try:
                posted.append(await _replay(op, thread_id, user_id))
            except (CommentActionError, DocumentSaveError) as exc:
                failed.append({"write": writer_line.describe(op), "error": exc.message})
            except Exception:
                reference = new_error_reference()
                logger.exception("saved write on {} by {} failed to post as is (reference {})",
                                 root_id, thread_id, reference)
                failed.append({"write": writer_line.describe(op), "error": (
                    f"It couldn't be posted (reference {reference}). Write it again if it still applies.")})
    finally:
        await _finish_and_drive(root_id, thread_id)
    result: dict[str, Any] = {"posted": posted}
    if failed:
        result["failed"] = failed
    return result
