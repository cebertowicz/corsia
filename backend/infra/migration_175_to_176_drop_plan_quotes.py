"""Remove the retired plan quote store."""

import sqlite3


def migrate_175_to_176(conn: sqlite3.Connection) -> None:
    """Keep plan intents and criteria while dropping stored quotes."""
    conn.execute("DROP TABLE IF EXISTS plan_node_intent_words")
