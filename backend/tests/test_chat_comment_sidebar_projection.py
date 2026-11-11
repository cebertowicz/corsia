"""Comment-only inputs are not the chat's last-message preview."""
import json

from infra.database import get_db
from infra.sidebar_preview import sidebar_update
from test_chat_comment_answers import _seed


def test_comment_only_input_does_not_replace_the_sidebar_preview(isolated_db):
    db = get_db()
    try:
        _seed(db)
        metadata = json.dumps({'reply_context': {'comments': [{'id': 'comment'}]}})
        db.execute("INSERT INTO messages(id,thread_id,role,content,metadata,created_at) "
                   "VALUES ('comment-only','thread','user','',?,'2030-01-01')", (metadata,))
        db.commit()
        assert sidebar_update('thread')['last_message'] == 'text'
        db.execute("UPDATE messages SET content='Also do this' WHERE id='comment-only'")
        db.commit()
        assert sidebar_update('thread')['last_message'] == 'Also do this'
    finally:
        db.close()
