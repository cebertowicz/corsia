"""Schema 161: retain the root of a sent chat comment reply."""

import sqlite3


def migrate_160_to_161(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute('PRAGMA table_info(chat_comments)')}
    if not columns:
        return
    for name in ('root_user_message_id', 'root_cid'):
        if name not in columns:
            conn.execute(f'ALTER TABLE chat_comments ADD COLUMN {name} TEXT')
