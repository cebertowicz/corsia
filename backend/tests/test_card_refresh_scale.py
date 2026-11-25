"""Scale budget for one batched card-refresh read (EAS21: a server read names its ceiling).

Measured with a sqlite trace callback on every connection, against a thread shaped like a
real card-refresh selection: 12 carded assistant messages, each with a registered file
(the card reads are what the refresh exists to redo), plus one 51-step turn.

  request                     statements  ownership reads  rows returned
  one message range                 17              2               1
  thirteen message ranges          161              2              62
  thirteen separate ranged GETs    247             39              62   <- what the batch replaces

The thirteen-range count did not move across the volumes the read can meet: with the read
thread at 1,033 / 3,524 (production p99) / 100,639 (production's largest parent) /
1,006,390 rows (10x that), and with the whole database at 2,158,639 messages (the
2026-10-08 census). Statements are constant because every read is a seek on
``idx_messages_session_seq`` or on the message id, with the row count bounded by the
limits in ``card_refresh_limits.json``.

Rows is the axis that is not constant. A single-message range inspects one row. Only the turn
range can be wide: it inspects the rows between its endpoints, drawn or not (the window was
counted at 51 rows when the turn's steps are all anchors and 49,051 when 50,000 tool rows sit
inside the same 51 anchors; the window was counted, not timed). At most ``turn_range_limit`` + 1
rows leave SQLite per range, so one request returns at most 13 x 51 = 663 rows, and
at most one range is wide. That traversal is the ranged GET's own, unchanged by batching.

Not measured: a database 10x the census (21.6M messages). The seeks above would gain at most
one B-tree level (a probe index went 1 -> 2 levels between 11k and 111k rows); the statement
count would not change.
"""
from __future__ import annotations

import gc
import json
import sqlite3
import threading
from pathlib import Path

import pytest
from auth_fixtures import OWNER_TOKEN

_AUTH = {"token": OWNER_TOKEN}
_ANCHOR = json.dumps({'for_user': True})
_TOOL = json.dumps({'tool_call_id': 'call-1'})
_INSERT = (
    'INSERT INTO messages (id, thread_id, role, content, metadata, created_at, seq) VALUES (?,?,?,?,?,?,?)'
)
_OWNERSHIP = 'SELECT * FROM threads WHERE id'

#: Measured above, with slack for a SQLite build that reports statements differently.
SINGLE_RANGE_STATEMENTS = 24
THIRTEEN_RANGE_STATEMENTS = 176
#: The authorization reads one request may cost, however many ranges it carries.
OWNERSHIP_READS = 3


class StatementTally:
    """Counts the SQL statements every live SQLite connection runs while armed.

    A trace callback belongs to one connection, and the request's reads run on a pooled
    read-lane connection that may be opened later, so both existing connections and ones
    opened inside the window are armed.
    """

    def __init__(self) -> None:
        self.records: list[tuple[str, str]] = []
        self.active = False
        self.armed: list[sqlite3.Connection] = []

    def arm_new_connections(self, monkeypatch) -> None:
        from infra import database

        real = database._make_connection

        def opening(path):
            conn = real(path)
            conn.set_trace_callback(self._record)
            self.armed.append(conn)
            return conn

        monkeypatch.setattr(database, "_make_connection", opening)
        for obj in gc.get_objects():
            if isinstance(obj, sqlite3.Connection):
                try:
                    obj.set_trace_callback(self._record)
                except sqlite3.ProgrammingError:
                    continue
                self.armed.append(obj)

    def disarm(self) -> None:
        for conn in self.armed:
            try:
                conn.set_trace_callback(None)
            except sqlite3.ProgrammingError:
                pass  # the connection closed with the thread that owned it
        self.armed.clear()

    def _record(self, sql: str) -> None:
        if not self.active:
            return
        flat = ' '.join(sql.split())
        if flat.startswith('--'):  # trigger bodies are reported as comments
            return
        self.records.append((threading.current_thread().name, flat))

    def measure(self, call):
        """Run one request and return (response, statements, ownership reads, statement shapes)."""
        self.records.clear()
        self.active = True
        try:
            response = call()
        finally:
            self.active = False
        shapes: dict[str, int] = {}
        for _thread, sql in self.records:
            shapes[sql[:70]] = shapes.get(sql[:70], 0) + 1
        ownership = sum(count for sql, count in shapes.items() if sql.startswith(_OWNERSHIP))
        return response, len(self.records), ownership, shapes


def _seed(client, *, turn_anchors: int = 51, hidden_per_step: int = 25, carded: int = 12):
    """A thread shaped for card refresh; returns (thread_id, carded ids, turn start, turn end)."""
    from infra import config
    from infra.database import get_db
    from registry import service as registry_service

    thread = client.post('/api/threads', params=_AUTH, json={'title': 'refresh scale'}).json()['thread_id']
    db = get_db()
    seq = 0
    carded_ids: list[str] = []
    for index in range(carded):
        seq += 1
        mid = f'{thread}-c{index}'
        db.execute(_INSERT, (mid, thread, 'assistant', f'carded {index}', _ANCHOR, '2026-01-01T00:00:00', seq))
        carded_ids.append(mid)
    seq += 1
    db.execute(_INSERT, (f'{thread}-u1', thread, 'user', 'do it', _ANCHOR, '2026-01-01T00:00:01', seq))
    turn_ids: list[str] = []
    for index in range(turn_anchors):
        seq += 1
        mid = f'{thread}-a{index}'
        db.execute(_INSERT, (mid, thread, 'assistant', f'step {index}', _ANCHOR, '2026-01-01T00:00:02', seq))
        turn_ids.append(mid)
        for _ in range(hidden_per_step):
            seq += 1
            db.execute(_INSERT, (f'{thread}-t{seq}', thread, 'tool', 'x' * 40, _TOOL, '2026-01-01T00:00:03', seq))
    db.commit()
    for index in range(carded):
        path = Path(config.WORKSPACE) / f'refresh-scale-{thread}-{index}.md'
        path.write_text('card', encoding='utf-8')
        registry_service.register_write_strict(thread, str(path), title=f'Scale card {index}', db=db)
    db.commit()
    return thread, carded_ids, turn_ids[0], turn_ids[-1]


def _post_refresh(client, thread: str, message_ids: list[str], turn_range: dict | None):
    return client.post(f'/api/threads/{thread}/messages/refresh', params=_AUTH, json={
        'message_ids': message_ids, 'turn_range': turn_range})


@pytest.mark.integration
def test_refresh_statement_budget(integration_client, monkeypatch):
    """One request authorizes once, and one more range costs a fixed number of statements."""
    client = integration_client
    thread, carded_ids, turn_from, turn_to = _seed(client)
    tally = StatementTally()
    # Warm the DB lanes so the request's pooled connection exists before the tally arms.
    _post_refresh(client, thread, carded_ids[:1], None)
    tally.arm_new_connections(monkeypatch)
    try:
        def measured(message_ids, turn_range):
            """The same request twice: the count must not depend on which run it is."""
            counts = []
            for _ in range(2):
                response, statements, ownership, shapes = tally.measure(
                    lambda: _post_refresh(client, thread, message_ids, turn_range))
                assert response.status_code == 200, response.text
                counts.append((statements, ownership, shapes))
            assert counts[0][0] == counts[1][0], 'the statement count is not reproducible'
            return counts[0]

        one, one_ownership, _ = measured(carded_ids[:1], None)
        thirteen, thirteen_ownership, shapes = measured(
            carded_ids, {'from_id': turn_from, 'to_id': turn_to})
    finally:
        tally.disarm()

    assert one <= SINGLE_RANGE_STATEMENTS, f'one range cost {one} statements'
    assert thirteen <= THIRTEEN_RANGE_STATEMENTS, f'thirteen ranges cost {thirteen} statements'
    # The finding: authorization is per request, not per range.
    assert one_ownership <= OWNERSHIP_READS
    assert thirteen_ownership <= OWNERSHIP_READS, (
        f'thirteen ranges cost {thirteen_ownership} ownership reads; the batch authorizes once per request')
    marginal = (thirteen - one) / 12
    assert marginal <= 13, f'each extra range costs {marginal:.1f} statements'
