"""Keep first bind, all subsequent account moves, a pending choice and the deferred move receipt's release
as distinct single writers."""
from __future__ import annotations

from pathlib import Path


def test_thread_vendor_account_runtime_writers_are_explicit_and_unique():
    connections = Path(__file__).parents[1] / 'connections'
    writes = []
    for path in connections.glob('*.py'):
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            compact = line.lower().replace(' ', '')
            if 'thread_vendor_accounts' in compact and ('insertinto' in compact or 'update' in compact):
                writes.append((path.name, number))
    assert {path for path, _ in writes} == {'account_policy.py', 'account_moves.py'}
    assert len(writes) == 4
