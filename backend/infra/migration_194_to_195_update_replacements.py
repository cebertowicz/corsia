"""Conservatively register legacy updates with unknown posting evidence."""
from infra.message_update_schema import MESSAGE_UPDATE_SCHEMA_SQL
from infra.time import utc_now_iso


def migrate_194_to_195(db):
    """Create replacement state and retain legacy rows as unknown/seen."""
    for statement in MESSAGE_UPDATE_SCHEMA_SQL.split(";"):
        if statement.strip():
            db.execute(statement)
    db.execute("INSERT OR IGNORE INTO message_update_state "
               "(message_id,thread_id,posted_at,update_kind,presence_at_post,seen_at,seen_reason) "
               "SELECT m.id,m.thread_id,m.created_at,'legacy_update','unknown',?,'unknown' "
               "FROM messages m JOIN thread_updates u ON u.source_id=m.id "
               "WHERE u.kind='marked' AND u.source_kind='message' AND m.role='assistant'",
               (utc_now_iso(),))
    db.execute("INSERT OR IGNORE INTO message_update_nodes "
               "SELECT source_id,node_id FROM thread_updates WHERE kind='marked' "
               "AND source_kind='message' AND node_id IS NOT NULL")
