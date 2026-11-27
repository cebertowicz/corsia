"""Activity's Notifications rows: one row per document with unread replies to the person.

Requires a sqlite3 connection configured with sqlite3.Row, as in the backend.
No writes are made; the read reuses the canonical unread SQL, but a row counts only
the unread agent replies in threads the person commented in — the replies the row
and the Activity bar speak of. The document's own header and jump control keep
counting the whole canonical unread set through ``feedback_state`` directly.
"""
from __future__ import annotations

import json
from pathlib import Path

from doceditor import doc_sections
from doceditor.feedback_state import _SCOPE_CHUNK, unread_comment_rows
from infra.activity_queue_base import clip_title, flat as _flat
from infra.auth import may_access_artifact
from plans.public import UNKNOWN_THREAD_SOURCE, thread_sources

#: How much of a document's text is scanned for its first heading, capped so a
#: queue read never pays for a whole large file.
_TITLE_HEAD_BYTES = 16_384


def _agent(source: dict) -> str:
    """An untitled or unknown thread is still an agent; never call it 'Untitled thread replied'."""
    label = source.get('thread_label')
    return 'An agent' if not source.get('thread_id') or label in (None, '', 'Untitled thread', UNKNOWN_THREAD_SOURCE['thread_label']) else label


def _first_heading(path: str) -> str:
    """The document's own title: the text of its first heading, '' when none is within the head.

    A bounded read only — the first heading sits near the top of every real document, and an
    unreadable or heading-less file keeps its registry title. Only regular files are opened:
    a fifo would block the reader (the doc_text read route guards the same way). Live buffers
    are not consulted: the title names the row, and the saved file is what the registry
    points at.
    """
    try:
        document = Path(path)
        if not document.is_file():
            return ''
        with document.open('rb') as handle:
            head = handle.read(_TITLE_HEAD_BYTES)
    except OSError:
        return ''
    text = head.decode('utf-8', errors='replace')
    if len(head) == _TITLE_HEAD_BYTES:  # the window may cut a line: parse whole lines only
        text = text[:text.rfind('\n') + 1]
    for _, section in doc_sections.headings(text):
        if section and (title := section.lstrip('#').strip()):
            return title
    return ''


def resolve_row_titles(rows: list[dict]) -> None:
    """Name each given row by its document's own heading when the registry holds only the
    file's name (the approved naming rule). The queue calls this on the page it is returning,
    so a poll pays at most one bounded head read per returned row; rows the cut drops were
    never read. Internal: ``detail.heading_path`` marks a candidate row and is consumed here.
    """
    for row in rows:
        path = row['detail'].pop('heading_path', '')
        if path and (heading := clip_title(_flat(_first_heading(path)))):
            row['title'] = heading


def _document_row(artifact: str, rows: list, sources: dict) -> dict:
    """One document's Notifications row from its qualifying unread replies, newest first."""
    latest = rows[0]
    threads: dict[str, str] = {}
    agents: list[str] = []
    for row in rows:
        threads.setdefault(row['root_id'], row['reply_id'])
        label = _agent(sources[row['author_thread_id']])
        if label not in agents:
            agents.append(label)
    # The naming rule, written once: the registry names the row unless it holds only the
    # file's name — compared raw, so a long or repeated-space name still matches itself —
    # and then the document's own heading replaces it, on the returned page only.
    registered = str(latest['artifact_title'] or '').strip()
    name = str(latest['artifact_path'] or '').replace('\\', '/').rsplit('/', 1)[-1]
    detail = {'artifact_id': artifact, 'unread': len(rows), 'latest_reply_id': latest['latest_unread_id'],
              'agents': agents, 'threads': [{'root_id': root, 'reply_id': reply} for root, reply in threads.items()]}
    if registered in ('', name) and (path := str(latest['artifact_path'] or '')):
        detail['heading_path'] = path  # resolve_row_titles consumes it on the returned page
    return {
        'renderer': 'document', 'kind': 'document', 'key': f'doc:{artifact}',
        'external_id': f'doc:{artifact}',
        'title': clip_title(_flat(registered or name or 'A document')),
        'raised_at': latest['created_at'], 'source': sources[latest['author_thread_id']],
        'open': {'artifact_id': artifact, 'comment_id': latest['root_id']},
        'detail': detail,
        'priority': 0,
    }


def _person_documents(db) -> list[str]:
    """The documents holding a comment by the person, in id order.

    Only these can hold a qualifying reply: one needs the person to have written its
    thread's root or a reply under it, on the same document. A comment by the person is
    ``author_kind='user'``, as in the qualifying test below. ``INDEXED BY``: the person's
    comments by time (idx_doc_comments_user_created) hold only their rows, so the read
    never visits an agent's comment; with no ANALYZE the planner would scan every comment.
    """
    rows = db.execute("""SELECT DISTINCT artifact_id FROM doc_comments INDEXED BY idx_doc_comments_user_created
                         WHERE author_kind='user' ORDER BY artifact_id""").fetchall()
    return [str(row[0]) for row in rows]


def _replies(db, user_id: str, scope: tuple[str, ...]) -> list:
    """The qualifying unread replies on the given documents, by document, newest first."""
    unread_sql, unread_params = unread_comment_rows(user_id, scope)
    # The attention authorization seam requires a current routing owner; a
    # released owner's registration is not an owner, even if comments can read it.
    # One JSON list binds the scope here, so a full chunk stays under SQLite's bind limit.
    sql = f"""
    WITH owner_candidates AS (
      SELECT r.artifact_id, r.parent_thread_id,
             ROW_NUMBER() OVER (PARTITION BY r.artifact_id ORDER BY
               CASE r.relationship_state WHEN 'owner-pinned' THEN 0 ELSE 1 END,
               r.updated_at DESC, r.id ASC) AS pos
      FROM artifact_registrations r
      WHERE r.relationship_state IN ('owner-pinned','owner-active')
        AND r.artifact_id IN (SELECT value FROM json_each(?))
    ), unread AS (
      SELECT root.id AS root_id, item.id AS reply_id, item.artifact_id,
             item.created_at, item.author_thread_id,
             (item.parent_id IS NOT NULL AND item.author_kind='agent'
              AND item.artifact_id=root.artifact_id
              AND EXISTS (SELECT 1 FROM threads author WHERE author.id=item.author_thread_id AND author.user_id=?)
              -- The person commented in this thread: they wrote its root, or a reply under it.
              -- Each side is an index lookup (the root's own row, idx_doc_comments_parent); one
              -- EXISTS over the whole document re-scanned every comment in it for every reply.
              AND (root.author_kind='user'
                   OR EXISTS (SELECT 1 FROM doc_comments person WHERE person.parent_id=root.id
                              AND person.author_kind='user' AND person.artifact_id=root.artifact_id))
             ) AS qualifies
      {unread_sql}
    ), counted AS (
      SELECT u.*,
             FIRST_VALUE(u.reply_id) OVER (PARTITION BY u.artifact_id
               ORDER BY u.created_at DESC, u.reply_id DESC) AS latest_unread_id
      FROM unread u
    )
    SELECT c.root_id, c.reply_id, c.artifact_id, c.created_at, c.author_thread_id, c.latest_unread_id,
           i.title AS artifact_title, i.path AS artifact_path, own.parent_thread_id AS owner_thread
    FROM counted c
    JOIN artifact_identities i ON i.id=c.artifact_id AND i.state='active'
    JOIN owner_candidates own ON own.artifact_id=i.id AND own.pos=1
    WHERE c.qualifies AND own.parent_thread_id IS NOT NULL AND own.parent_thread_id<>''
    ORDER BY c.artifact_id, c.created_at DESC, c.reply_id DESC
    """
    return db.execute(sql, (json.dumps(scope), user_id, *unread_params)).fetchall()


def document_rows(db, user_id: str) -> list[dict]:
    """Return one row per document holding an unread agent reply to the person's comment.

    A document qualifies when an agent answered in a thread the person commented in, and
    the answer is unread. Its count is the unread replies that qualify — answers to the
    person's own comments — not every unread item in the document: an unrelated unread
    agent comment never rides the row's number. ``latest_reply_id`` stays the newest
    unread item of any kind (#3358): Done passes it as the read mark, and since it is
    never older than the newest counted reply, one press clears every counted reply and
    the row with it; an item that arrives after the press stays unread until the next
    qualifying reply starts a new row.

    Only the documents the person commented in are read, a chunk at a time, each through
    its own index range, so a poll's cost follows those documents, not every comment.
    """
    documents = _person_documents(db)
    # Access is the artifact routes' own predicate, asked once per owner thread.
    access: dict[str, bool] = {}

    def allowed(owner: str) -> bool:
        if owner not in access:
            access[owner] = may_access_artifact(owner, user_id, db=db)
        return access[owner]

    replies = [row for start in range(0, len(documents), _SCOPE_CHUNK)
               for row in _replies(db, user_id, tuple(documents[start:start + _SCOPE_CHUNK]))
               if allowed(row['owner_thread'])]
    sources = thread_sources(db, {str(r['author_thread_id']) for r in replies})
    groups: dict[str, list] = {}
    for row in replies:  # newest first within each document
        groups.setdefault(row['artifact_id'], []).append(row)
    return [_document_row(artifact, rows, sources) for artifact, rows in groups.items()]
