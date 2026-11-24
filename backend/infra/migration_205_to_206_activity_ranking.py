"""Schema 205 -> 206: activity ordering and its durable nudge throttle."""

from infra.activity_ranking_schema import ACTIVITY_RANKING_SQL


def migrate_205_to_206(conn) -> None:
    """Create the activity tables inside the migration runner's transaction."""
    for statement in ACTIVITY_RANKING_SQL.split(";"):
        if statement.strip():
            conn.execute(statement)
