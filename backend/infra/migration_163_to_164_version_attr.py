"""Schema 163 -> 164: retain each declaration's version rule."""
import sqlite3


def migrate_163_to_164(conn: sqlite3.Connection) -> None:
    """Existing declarations have no version rule until their next sync."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(measure_definitions)")}
    if "version_attr" not in columns:
        conn.execute("ALTER TABLE measure_definitions ADD COLUMN version_attr TEXT")
