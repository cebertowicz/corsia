import sqlite3
import pytest
from infra.schema import SCHEMA_SQL
from tests.test_attention_queue import _seed


class WeakrefConnection(sqlite3.Connection):
    pass


@pytest.fixture
def db():
    connection = sqlite3.connect(':memory:', factory=WeakrefConnection)
    connection.row_factory = sqlite3.Row
    connection.executescript(SCHEMA_SQL)
    _seed(connection)
    connection.commit()
    yield connection
    connection.close()
from tests.test_user_ask_one_open import _block, _document
from api.attention import _authorized_raised
from plans import public, service
from doceditor.activity_reply_rows import reply_rows
from plans import activity_rows as candidate


def test_suppression_follows_actual_emitted_authorized_reviews(db):
    plan = service.ensure_plan('thread', 'root', db=db)
    node = service.add_node(plan.id, kind='task', title='Review', acceptance_criteria='Done', db=db)
    artifact = _document(db)
    db.execute("INSERT INTO doc_comments(id,artifact_id,author,author_kind,body,created_at,updated_at) VALUES ('root-comment',?,'person','user','question','1','1')", (artifact,))
    db.execute("INSERT INTO doc_comments(id,artifact_id,parent_id,author,author_kind,author_thread_id,body,created_at,updated_at) VALUES ('reply',?,'root-comment','agent','agent','root','answer','2','2')", (artifact,))
    db.commit()
    _block(db, plan.id, node.id, artifact)
    def project():
        annotated = public.annotate_attention(_authorized_raised(None, 'user-1', db=db), db=db)
        rows = candidate.rows(db, 'user-1', annotated)
        return {r['detail']['artifact_id'] for r in rows if r['kind'] == 'review'}
    reviewed = project()
    assert reviewed == {artifact}
    assert reply_rows(db, 'user-1', reviewed_artifact_ids=reviewed) == []
    db.execute("UPDATE plans SET state='archived' WHERE id=?", (plan.id,))
    reviewed = project()
    assert reviewed == set()
    assert len(reply_rows(db, 'user-1', reviewed_artifact_ids=reviewed)) == 1
