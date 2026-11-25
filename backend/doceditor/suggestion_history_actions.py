"""Undo and redo non-live suggestion decisions under the decision owner's lock."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Awaitable
from typing import Any, Protocol

from doceditor import comments as store
from doceditor.comment_action_errors import CommentActionError
from doceditor.comment_actions import accept_lock
from doceditor.comment_permissions import check_decider, owner_row
from doceditor.doc_text import read_doc_text
from infra.database import get_db, write_transaction
from infra.db_async import run_in_db, run_off_db
from registry import models as registry_models


class HistoryWriter(Protocol):
    def __call__(
        self, path: str, text: str, project_id: str | None, reason: str, expected_sha256: str | None,
        suggestion_author: str | None, accepted: tuple[str, ...], /, *, suppress_feedback: bool = False,
    ) -> Awaitable[dict[str, Any] | None]: ...


def _refused() -> CommentActionError:
    return CommentActionError('stale', 'The document or suggestions changed. Reload and try again.')


def _not_recorded(direction: str) -> str:
    """The write was undone: the text is as it was. The reload that follows drops the step, so the next move is the card or the text."""
    if direction == 'undo':
        return 'Couldn’t undo that. The document is unchanged. To take the suggestion back, edit the text yourself.'
    return 'Couldn’t redo that. The document is unchanged. You can accept the suggestion again from its card.'


def _not_restored(direction: str) -> str:
    """Neither the suggestion nor the text could be put back: the document reloads to what was saved."""
    return (f'Couldn’t {direction} that, and the document may not match the suggestion. '
            'The document reloads to show what was saved; check the suggestion on its card.')


def _load(artifact_id: str, event_id: str, ids: list[str], user_id: str) -> tuple[dict, dict, str | None]:
    db = get_db()
    try:
        artifact = registry_models.get_artifact(artifact_id, db=db)
        if artifact is None:
            raise CommentActionError('not_found', 'Document not found')
        check_decider(user_id=user_id, actor_thread_id=None, artifact=artifact, db=db)
        event = db.execute('SELECT * FROM doc_suggestion_history WHERE event_id=? AND artifact_id=?',
                           (event_id, artifact_id)).fetchone()
        if event is None or not ids or len(set(ids)) != len(ids):
            raise _refused()
        event = dict(event)
        owner = owner_row(db, artifact['parent_thread_id'])
        return artifact, event, owner[1] if owner else None
    finally:
        db.close()


def _event_state(db, event: dict, ids: list[str]) -> str:
    """What the event's suggestion rows say: 'accepted' or 'undone'.

    The request's ids, the rows still carrying this event and the suggestions whose metadata the receipt holds must be the
    same set: a row decided again, or deleted, would otherwise leave a smaller set that undo or redo applies in full.
    """
    rows = db.execute('SELECT id, decision FROM doc_comments WHERE decision_event_id=? AND artifact_id=?',
                      (event['event_id'], event['artifact_id'])).fetchall()
    held = sorted(json.loads(event['decision_metadata']))
    if not rows or sorted(row['id'] for row in rows) != held or sorted(ids) != held:
        raise _refused()
    decisions = {row['decision'] for row in rows}
    if decisions == {'accepted'}:
        return 'accepted'
    if decisions == {None}:
        return 'undone'
    raise _refused()


def _current_state(event: dict, ids: list[str]) -> str:
    db = get_db()
    try:
        return _event_state(db, event, ids)
    finally:
        db.close()


def _record(event: dict, ids: list[str], direction: str, *, db=None) -> None:
    previous = 'accepted' if direction == 'undo' else 'undone'
    metadata = json.loads(event['decision_metadata'])
    owns_db = db is None
    db = db or get_db()
    try:
        with write_transaction(db, scope='suggestion_history_transition') as connection:
            if _event_state(connection, event, ids) != previous:
                raise _refused()
            for comment_id in ids:
                if direction == 'undo':
                    fields: dict[str, Any] = {'decision': None, 'decided_by': None, 'decided_by_thread_id': None,
                                              'decided_at': None, 'decided_in_bulk': None, 'resolved': False}
                else:
                    replied = connection.execute('SELECT 1 FROM doc_comments WHERE parent_id=? LIMIT 1',
                                                 (comment_id,)).fetchone() is not None
                    fields = {**metadata[comment_id], 'decision': 'accepted', 'resolved': not replied,
                              'closed_by_decision': not replied}
                store.update(comment_id, fields, db=connection)
    finally:
        if owns_db:
            db.close()


async def transition_suggestion_history(
    artifact_id: str, event_id: str, ids: list[str], direction: str, expected_sha256: str, text: str,
    *, user_id: str, write: HistoryWriter,
) -> dict[str, Any]:
    """Validate both document states, then use the existing writer and decision transaction."""
    if direction not in {'undo', 'redo'}:
        raise _refused()
    async with accept_lock(artifact_id):
        artifact, event, project_id = await run_in_db(_load, artifact_id, event_id, ids, user_id)
        before, after = event['before_sha256'], event['after_sha256']
        source_hash, target_hash = (after, before) if direction == 'undo' else (before, after)
        if expected_sha256 != source_hash or hashlib.sha256(text.encode()).hexdigest() != target_hash:
            raise _refused()
        current_text = await run_off_db(read_doc_text, artifact['path'])
        current_hash = hashlib.sha256(current_text.encode()).hexdigest()
        target_state = 'undone' if direction == 'undo' else 'accepted'
        state = await run_in_db(_current_state, event, ids)
        if state == target_state and current_hash == target_hash:
            return {'text': current_text, 'revision': target_hash}
        if state == target_state or current_hash not in {source_hash, target_hash}:
            raise _refused()
        result = None
        if current_hash == source_hash:
            result = await write(artifact['path'], text, project_id, f'{direction.title()} suggestion acceptance',
                                 source_hash, None, (), suppress_feedback=True)
        try:
            await run_in_db(_record, event, ids, direction)
        except (sqlite3.Error, CommentActionError) as exc:
            if result is not None:
                try:
                    await write(artifact['path'], current_text, project_id,
                                f'Restore after failed {direction}', target_hash, None, (), suppress_feedback=True)
                except Exception as restore_error:
                    raise CommentActionError('unrecorded', _not_restored(direction)) from restore_error
            raise CommentActionError('unrecorded', _not_recorded(direction)) from exc
        return {'text': text, 'revision': result.get('revision') if result else target_hash}
