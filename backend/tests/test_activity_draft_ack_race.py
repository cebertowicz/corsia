"""A competing send transition and acknowledgement serialize at the DB boundary."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from api.attention import _acknowledge_draft
from infra.database import get_db, write_transaction
from tests.test_email_drafts_api import _artifact, _owner
from tests.test_google_email import _draft, client_file, conductor, google, mail  # noqa: F401


@pytest.mark.asyncio
@pytest.mark.parametrize('final_state', ['open', 'sent'])
async def test_transition_wins_before_acknowledgement(mail, final_state):
    saved = await _draft(mail)
    _artifact(saved)
    draft_id = saved['draft_id']
    owner = _owner(mail.thread)['id']
    db = get_db()
    db.execute("UPDATE action_drafts SET state='sending' WHERE id=?", (draft_id,))
    db.commit()
    started = Event()

    def acknowledge():
        started.set()
        return _acknowledge_draft(draft_id, owner)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with write_transaction(db, scope='test_draft_transition'):
            db.execute('UPDATE action_drafts SET state=? WHERE id=?', (final_state, draft_id))
            future = pool.submit(acknowledge)
            assert started.wait(5)
        assert future.result(timeout=10) == 409
    assert db.execute('SELECT count(*) FROM activity_draft_acknowledgements').fetchone()[0] == 0
