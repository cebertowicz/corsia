"""Project unsettled email drafts into Activity's existing task queue."""
from __future__ import annotations

from pathlib import Path
import stat

from connections.mail.email_document import parse_document
from infra import config
from infra.activity_queue_base import clip_title
from infra.workspace_paths import WorkspaceReference, workspace_selection_for_root
from tools.action_gate import _read_bytes


_IDENTITY_BATCH = 500


def _identities(db, external_ids: set[str], user_id: str) -> dict[str, dict]:
    """Resolve accessible document identities in batches, never one query per draft."""
    found = {}
    ids = sorted(external_ids)
    for start in range(0, len(ids), _IDENTITY_BATCH):
        batch = ids[start:start + _IDENTITY_BATCH]
        marks = ','.join('?' for _ in batch)
        rows = db.execute(
            f"""WITH docs AS (
                  SELECT i.id, i.external_id, i.title,
                         (SELECT r.parent_thread_id FROM artifact_registrations r
                           WHERE r.artifact_id=i.id AND r.relationship_state IN ('owner-pinned','owner-active')
                           ORDER BY CASE r.relationship_state WHEN 'owner-pinned' THEN 0 ELSE 1 END,
                                    r.updated_at DESC, r.id ASC LIMIT 1) AS owner
                    FROM artifact_identities i
                   WHERE i.integration='workspace' AND i.type='document'
                     AND (i.state='active' OR (i.state IN ('archived', 'tombstone')
                          AND json_valid(i.metadata)
                          AND json_extract(i.metadata, '$.retired_cause')='file_gone'))
                     AND i.external_id IN ({marks})
                )
                SELECT docs.id, docs.external_id, docs.title FROM docs
                 LEFT JOIN threads owner_thread ON owner_thread.id=docs.owner
                WHERE docs.owner='ui' OR substr(docs.owner,1,8)='project:' OR owner_thread.user_id=?""",
            (*batch, user_id),
        )
        found.update({row['external_id']: dict(row) for row in rows})
    return found


def _subject(raw: bytes | None, fallback: str) -> str:
    try:
        parsed = parse_document(raw.decode('utf-8')) if raw is not None else None
    except (UnicodeError, ValueError):
        parsed = None
    return (parsed or {}).get('subject') or fallback


def merge_draft_reviews(rows: list[dict], drafts: list[dict]) -> tuple[list[dict], set]:
    """Replace review rows for these drafts while retaining their artifact identities."""
    identities = {row['detail']['artifact_id'] for row in drafts if row['detail']['artifact_id']}
    reviewed = {row['detail']['artifact_id'] for row in rows
                if row['kind'] == 'review' and row['detail']['artifact_id'] in identities}
    return ([row for row in rows if row['kind'] != 'review' or row['detail']['artifact_id'] not in identities]
            + drafts, reviewed)


def draft_rows(db, user_id: str, *, subjects: bool = True) -> list[dict]:
    """Return unsettled email drafts in owned threads; callers run this off the event loop.

    The keys-only sweep sets ``subjects=False`` and never reads draft files.
    Identity and draft state are read in batches; the document's current bytes,
    including an unsaved live edit, are the source for the displayed subject.
    """
    rows = db.execute(
        """SELECT d.id, d.path, d.state, d.created_at, t.id AS thread_id, t.title AS thread_title,
                  t.project_id, t.spawned_by, p.name AS project_name
             FROM action_drafts d JOIN threads t ON t.id=d.thread_id
             LEFT JOIN projects p ON p.id=t.project_id
            WHERE d.kind='email' AND d.state IN ('open', 'sending') AND t.user_id=?
              AND (d.state='open' OR NOT EXISTS (
                  SELECT 1 FROM activity_draft_acknowledgements ack
                   WHERE ack.user_id=t.user_id AND ack.draft_id=d.id
              ))
              AND d.id = (
                  SELECT newest.id FROM action_drafts newest
                   WHERE newest.kind=d.kind AND newest.path=d.path
                   ORDER BY newest.created_at DESC LIMIT 1
              )""", (user_id,),
    ).fetchall()
    if not rows:
        return []
    selection = workspace_selection_for_root(config.WORKSPACE.resolve())
    candidates = []
    external_ids: set[str] = set()
    for row in rows:
        path = str(row['path'])
        try:
            logical = WorkspaceReference(selection.workspace_id, tuple(Path(path).parts)).serialize()
        except ValueError:
            continue
        absolute = str(config.WORKSPACE / path)
        candidates.append((row, logical, absolute))
        external_ids.update((logical, absolute))
    identities = _identities(db, external_ids, user_id)
    result = []
    for row, logical, absolute in candidates:
        artifact = identities.get(logical) or identities.get(absolute)
        artifact_id = artifact['id'] if artifact else None
        if not artifact_id:
            # An absent file with no identity has no action. Check metadata even in the
            # keys-only sweep so it sees the same membership without reading contents.
            try:
                if not stat.S_ISREG(Path(absolute).stat().st_mode):
                    continue
            except (FileNotFoundError, NotADirectoryError):
                continue
        fallback = (artifact['title'] if artifact else None) or 'email draft'
        raw = _read_bytes(row['path']) if subjects else None
        missing = subjects and raw is None
        if missing and not artifact_id:
            continue
        title = ('email draft' if missing else _subject(raw, fallback)) if subjects else fallback
        result.append({
            'key': f"draft:{row['id']}", 'external_id': row['id'],
            'renderer': 'draft', 'kind': 'draft', 'title': 'Email draft' if missing else clip_title(f'Send: {title}'),
            'raised_at': row['created_at'],
            'source': {'thread_id': row['thread_id'], 'thread_label': row['thread_title'] or 'Untitled thread',
                       'project_id': row['project_id'], 'project_label': row['project_name'],
                       'parent_reviewed': row['spawned_by'] is not None},
            'open': {'path': absolute, 'artifact_id': artifact_id} if not missing else None,
            'detail': {'artifact_id': artifact_id, 'missing': missing, 'unconfirmed': row['state'] == 'sending'}, 'priority': 0,
        })
    return result
