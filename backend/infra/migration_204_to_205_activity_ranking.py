"""Schema 204 -> 205: activity ordering and its durable nudge throttle."""

from infra.activity_ranking_schema import ACTIVITY_RANKING_SQL


def migrate_204_to_205(conn) -> None:
    """Create the activity tables inside the migration runner's transaction."""
    for statement in ACTIVITY_RANKING_SQL.split(";"):
        if statement.strip():
            conn.execute(statement)
