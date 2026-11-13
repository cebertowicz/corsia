"""Comment-only inputs are not the chat's last-message preview."""
import json

from infra.chat_comment_index import place_comments
from infra.database import get_db
from infra.sidebar_preview import sidebar_update
from test_chat_comment_answers import _seed


def _input(db, message_id, content, created_at):
    metadata = {'reply_context': {'comments': [{'cid': f'{message_id}.1', 'seq': 0}]}}
    db.execute("INSERT INTO messages(id,thread_id,role,content,metadata,created_at) "
               "VALUES (?,'thread','user',?,?,?)", (message_id, content, json.dumps(metadata), created_at))
    place_comments(db, 'thread', message_id, metadata, created_at)
    db.commit()


def test_comment_only_input_does_not_replace_the_sidebar_preview(isolated_db):
    db = get_db()
    try:
        _seed(db)
        _input(db, 'comment-only', '', '2030-01-01')
        assert sidebar_update('thread')['last_message'] == 'text'
        # Text sent with a comment is a chat message, so it is the preview.
        _input(db, 'with-text', 'Also do this', '2030-01-02')
        assert sidebar_update('thread')['last_message'] == 'Also do this'
    finally:
        db.close()
