"""Add Activity's per-person draft acknowledgements."""

from infra.activity_draft_ack_schema import ACTIVITY_DRAFT_ACK_SCHEMA_SQL


def migrate_211_to_212(conn):
    conn.execute(ACTIVITY_DRAFT_ACK_SCHEMA_SQL)
