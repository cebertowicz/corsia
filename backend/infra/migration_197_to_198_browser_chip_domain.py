"""Schema 197 -> 198: give every stored browser chip the registrable domain new chips carry.

From 198 on, a visible ``browser_open`` stores ``{"host", "domain"}`` on its Step
(``tools.tool_display._browser_chip``), and the chip leads with the domain so the part that names the
site stays whole under the chip's ellipsis. Chips stored before carry only ``host``. This step sets
``domain`` on each of them from the stored host through ``host_access.browser_domain.registrable_domain``,
the same function new chips use, so a chat already holding a chip words it as a new one would. A chip
with no host gets ``domain`` null, as a new one would.

It updates ``messages.metadata`` only; no schema changes. It logs one INFO line with the chips it
updated, the chips that already had a domain and the elapsed time, which is how a production run is
read. Runs inside the runner's transaction; it never commits or rolls back.
"""

from __future__ import annotations

import json
import sqlite3
import time

from loguru import logger

from host_access.browser_domain import registrable_domain

_CHIPS = (
    "SELECT id, json_extract(metadata, '$.browser_chip.host'), "
    "json_type(metadata, '$.browser_chip.domain') IS NOT NULL "
    "FROM messages WHERE instr(metadata, '\"browser_chip\"') > 0 AND json_valid(metadata) "
    "AND json_type(metadata, '$.browser_chip') = 'object'"
)


def migrate_197_to_198(conn: sqlite3.Connection) -> None:
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "messages" not in tables:
        return
    started = time.monotonic()
    updates: list[tuple[str, str]] = []
    already = 0
    for message_id, host, has_domain in conn.execute(_CHIPS).fetchall():
        if has_domain:
            already += 1
            continue
        domain = registrable_domain(host) or None if isinstance(host, str) and host else None
        updates.append((json.dumps(domain), message_id))
    conn.executemany(
        "UPDATE messages SET metadata=json_set(metadata, '$.browser_chip.domain', json(?)) WHERE id=?", updates,
    )
    logger.info(
        "migration 197->198 browser chip domain: updated={} already={} elapsed_ms={}",
        len(updates), already, round((time.monotonic() - started) * 1000),
    )
