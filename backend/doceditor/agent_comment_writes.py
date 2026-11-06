"""The one gate every agent write to a comment thread passes through: the thread's writer turn.

Both agent transports, the native tools (``tools/documents.py``) and the ``corsia doc`` routes
(``api/delegation_docs.py``), call these instead of the shared writers. An agent's reply, accept,
reject, resolve, re-open or edit on a thread someone else holds is saved in line (``writer_line``)
and not run; the caller returns the line's answer. An Agree and a new top-level comment are not
waits on a thread and run at once. ``post_as_is`` replays the saved writes through this same gate.
Person writes never wait: a person sending or clearing a reply releases their own turn.
"""

from __future__ import annotations

import dataclasses
import json
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


def _admit(root_id: str, artifact_id: str, thread_id: str, user_id: str, op: dict | None, claim: bool = False):
    db = get_db()
    try:
        with write_transaction(db, scope="writer_line_admit") as connection:
            admission = writer_line.admit(
                connection, root_id=root_id, artifact_id=artifact_id, thread_id=thread_id, user_id=user_id, op=op)
            if claim and isinstance(admission, writer_line.Admitted) and admission.entry_id is not None:
                # post_as_is takes its row in the same write, so a second call finds nothing left to post.
                connection.execute(
                    "UPDATE doc_comment_writer_line SET state='posted', settled_at=?, settled_reason='posted' WHERE id=?",
                    (writer_line._now(), admission.entry_id))
            return admission
    except BaseException:
        writer_turns.agent_exit(root_id, thread_id)
        raise
    finally:
        db.close()


def _finish(root_id: str, thread_id: str, entry_id: str | None, posted: bool) -> list[str]:
    db = get_db()
    try:
        with write_transaction(db, scope="writer_line_finish") as connection:
            return writer_line.finish(connection, root_id=root_id, thread_id=thread_id, entry_id=entry_id, posted=posted)
    finally:
        db.close()


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
    posted = False
    try:
        result = await run()
        posted = True
        return result
    finally:
        # The write is accepted here, before any typing animation, so the next agent is woken now.
        try:
            await _drive(await run_in_db(_finish, root_id, thread_id, admission.entry_id, posted))
        except Exception:
            writer_turns.agent_exit(root_id, thread_id)
            logger.exception("writer turn on {} did not settle after {}'s write", root_id, thread_id)


# -- replies -----------------------------------------------------------------------------------


async def _create(request: CommentInput, client_id: str | None):
    row, registered, feedback, created = await create_validated_comment(request, client_id)
    if registered:
        registry_service.notify_changed(row["artifact_id"])
    await _drive(feedback["woken"])
    return row, registered, feedback, created


async def reply(request: CommentInput, client_id: str | None):
    """Create an agent comment; a reply waits for its thread's turn. Returns the creation result or ``Queued``."""
    if not request.parent_id:
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
    held: list[tuple[str, str | None]] = []

    async def select(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        admitted = []
        for row in rows:
            op = {"kind": "decide", "comment_id": row["id"], "decision": decision, "reason": reason,
                  "decided_by": decided_by}
            admission = await run_in_db(_admit, row["id"], row["artifact_id"], thread_id, user_id, op)
            if isinstance(admission, Queued):
                queued.append({"comment_id": row["id"], "message": admission.text})
            else:
                held.append((row["id"], admission.entry_id))
                admitted.append(row)
        return admitted

    posted = False
    try:
        result = await decide_all_from_author(
            comment_id, decision, user_id=user_id, decided_by=decided_by, actor_thread_id=thread_id,
            write=agent_document_writer(thread_id, user_id), reason=reason, select=select)
        posted = True
    finally:
        drives: list[str] = []
        for root_id, entry_id in held:
            drives += await run_in_db(_finish, root_id, thread_id, entry_id, posted)
        await _drive(drives)
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
            return update_comment(connection, comment_id, patch, actor_thread_id=thread_id), art["path"]
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


def _saved(comment_id: str, thread_id: str, user_id: str):
    target = _target(comment_id, user_id)
    if target is None:
        return None
    db = get_db()
    try:
        row = writer_line.saved_entry(db, target[0], thread_id)
        return (target, json.loads(row["ops"])) if row is not None else (target, None)
    finally:
        db.close()


async def post_as_is(comment_id: str, *, thread_id: str, user_id: str) -> dict[str, Any] | Queued:
    """Post this agent's saved writes on the thread, in the order it saved them, exactly once.

    A write that fails is reported with its saved text, so nothing is lost.
    """
    saved = await run_in_db(_saved, comment_id, thread_id, user_id)
    if saved is None:
        raise CommentActionError("not_found", "Comment not found")
    (root_id, artifact_id), ops = saved
    if ops is None:
        raise CommentActionError("nothing_saved", "You have nothing saved in line on this thread.")
    admission = await run_in_db(_admit, root_id, artifact_id, thread_id, user_id, None, True)
    if isinstance(admission, Queued):
        return admission
    posted: list[dict[str, Any]] = []
    failed: list[dict[str, str]] = []
    try:
        for op in ops:
            try:
                posted.append(await _replay(op, thread_id, user_id))
            except (CommentActionError, DocumentSaveError) as exc:
                failed.append({"write": writer_line.describe(op), "error": exc.message})
    finally:
        await _drive(await run_in_db(_finish, root_id, thread_id, None, False))
    result: dict[str, Any] = {"posted": posted}
    if failed:
        result["failed"] = failed
    return result
