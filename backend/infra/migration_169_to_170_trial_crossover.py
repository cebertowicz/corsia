"""Schema 169 -> 170: lesson trial receipts stored as ``crossover`` become ``without``.

Older delivery stored ``crossover`` only for a without draw whose thread had already seen the lesson. The
verdict read now decides exposure itself, for both halves alike, so the stored value is the raw draw: these
rows are without draws. Data only: the column's CHECK keeps admitting the old value because changing it
means rebuilding ``guidance_loads``, a receipt per context load, at startup; no code writes or reads it.

Idempotent: a second run finds no row to change. Runs inside the runner's transaction; it never commits or
rolls back.
"""

from __future__ import annotations

import sqlite3


def migrate_169_to_170(conn: sqlite3.Connection) -> None:
    """Rewrite stored ``crossover`` trial receipts as the ``without`` draw they were."""
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(guidance_loads)")}
    if "variant" in columns:
        conn.execute("UPDATE guidance_loads SET variant='without' WHERE variant='crossover'")
