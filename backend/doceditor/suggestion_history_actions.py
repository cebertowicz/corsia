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
        recorded_ids = json.loads(event['suggestion_ids'])
        if sorted(recorded_ids) != sorted(ids):
            raise _refused()
        owner = owner_row(db, artifact['parent_thread_id'])
        return artifact, event, owner[1] if owner else None
    finally:
        db.close()


def _check_rows(db, event: dict, ids: list[str], state: str) -> None:
    marks = ','.join('?' for _ in ids)
    rows = db.execute(f'SELECT id, artifact_id, decision, decision_event_id FROM doc_comments WHERE id IN ({marks})', ids).fetchall()
    decision = 'accepted' if state == 'accepted' else None
    if len(rows) != len(ids) or any(
        row['artifact_id'] != event['artifact_id'] or row['decision'] != decision
        or row['decision_event_id'] != event['event_id'] for row in rows
    ):
        raise _refused()


def _check_current_rows(event: dict, ids: list[str]) -> None:
    db = get_db()
    try:
        _check_rows(db, event, ids, event['state'])
    finally:
        db.close()


def _record(event: dict, ids: list[str], direction: str, *, db=None) -> None:
    previous = 'accepted' if direction == 'undo' else 'undone'
    target = 'undone' if direction == 'undo' else 'accepted'
    metadata = json.loads(event['decision_metadata'])
    owns_db = db is None
    db = db or get_db()
    try:
        with write_transaction(db, scope='suggestion_history_transition') as connection:
            current = connection.execute('SELECT state FROM doc_suggestion_history WHERE event_id=?',
                                         (event['event_id'],)).fetchone()
            if current is None or current['state'] != previous:
                raise _refused()
            _check_rows(connection, event, ids, previous)
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
            connection.execute('UPDATE doc_suggestion_history SET state=? WHERE event_id=?', (target, event['event_id']))
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
        await run_in_db(_check_current_rows, event, ids)
        if event['state'] == target_state and current_hash == target_hash:
            return {'text': current_text, 'revision': target_hash}
        if event['state'] == target_state or current_hash not in {source_hash, target_hash}:
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
                    raise CommentActionError(
                        'unrecorded', 'The suggestion state could not be recorded and the document could not be restored. Reload the document and comments.',
                    ) from restore_error
            raise CommentActionError('unrecorded', 'The suggestion state could not be recorded.') from exc
        return {'text': text, 'revision': result.get('revision') if result else target_hash}
