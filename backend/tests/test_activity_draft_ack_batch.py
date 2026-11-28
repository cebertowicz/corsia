"""The draft projection retains bounded identity lookups at larger list sizes."""
import pytest

from infra.activity_draft_rows import draft_rows
from infra.database import get_db
from tests.test_email_drafts_api import _owner
from tests.test_google_email import _draft, client_file, conductor, google, mail  # noqa: F401


@pytest.mark.asyncio
async def test_projection_queries_are_batched(mail):
    saved = await _draft(mail)
    owner = _owner(mail.thread)['id']
    db = get_db()
    copies = []
    for i in range(251):
        path = f'batch-{i}.md'
        (mail.workspace / path).write_text('Subject: Batch\n')
        copies.append((f'batch-{i}', path, saved['draft_id']))
    db.executemany("""INSERT INTO action_drafts
        (id, thread_id, kind, path, target_json, state, recorded_revision, recorded_at, created_at)
        SELECT ?, thread_id, kind, ?, target_json, state, recorded_revision, recorded_at, created_at
        FROM action_drafts WHERE id=?""", copies)
    db.commit()
    statements = []
    db.set_trace_callback(statements.append)
    try:
        assert len(draft_rows(db, owner, subjects=False)) == 252
    finally:
        db.set_trace_callback(None)
    selects = [s for s in statements if s.lstrip().upper().startswith(('SELECT', 'WITH'))]
    assert len(selects) == 2  # one draft read, one identity lookup
