"""Which event rules a script's approval covers: one fingerprint per rule, read from its stored row.

A ``run_script`` rule can start an approved script, and outside data comes in
with that run. The person approves a script with saved items once, so the
approval names the rules that may start it with those items: each rule's id
and a fingerprint of its stored columns (``event_rules``), read as stored.
``"match"`` is its stored JSON text, never parsed and re-serialised. ``enabled``,
``note``, ``mode``, ``origin``, ``authority_ref`` and the timestamps are left out,
so turning a rule off and on doesn't change it.

:func:`rules_hash` is what the approval panel loads and sends back with the
person's decision; ``script_decisions.decide`` compares it inside its write.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable

_COLUMNS = 'id, kind, scope, connector, event_type, "match", action, "to", created_by, script_id'


def _fingerprint(row: sqlite3.Row | tuple) -> str:
    return hashlib.sha256(json.dumps(list(tuple(row)), separators=(",", ":")).encode("utf-8")).hexdigest()


def bindings_for(schedule_ids: Iterable[str], db: sqlite3.Connection) -> dict[str, list[tuple[str, str]]]:
    """Each schedule's enabled ``run_script`` rules as ``(rule_id, fingerprint)``, sorted by id, in one query."""
    ids = sorted(set(schedule_ids))
    found: dict[str, list[tuple[str, str]]] = {schedule_id: [] for schedule_id in ids}
    if not ids:
        return found
    rows = db.execute(
        f"SELECT {_COLUMNS} FROM event_rules WHERE action='run_script' AND enabled=1 "
        f"AND script_id IN ({','.join('?' * len(ids))}) ORDER BY id",
        ids,
    ).fetchall()
    for row in rows:
        found[row[-1]].append((row[0], _fingerprint(row)))
    return found


def script_rule_bindings(schedule_id: str, db: sqlite3.Connection) -> list[tuple[str, str]]:
    """The schedule's enabled ``run_script`` rules as ``(rule_id, fingerprint)``, sorted by id."""
    return bindings_for([schedule_id], db)[schedule_id]


def rules_hash(bindings: list[tuple[str, str]]) -> str:
    """The hash of a schedule's rule list, as the panel loads it and the decision sends it back."""
    return hashlib.sha256(json.dumps([list(pair) for pair in bindings]).encode("utf-8")).hexdigest()


def rule_fingerprint(rule_id: str, db: sqlite3.Connection) -> str | None:
    """The rule's fingerprint now, read from its row at run start; None when the rule is gone."""
    row = db.execute(f"SELECT {_COLUMNS} FROM event_rules WHERE id=?", (rule_id,)).fetchone()
    return None if row is None else _fingerprint(row)
