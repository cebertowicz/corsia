"""Keep the historical confinement migrations' lookups on the thread index."""
import sqlite3

from infra.schema import SCHEMA_SQL
from infra import migration_154_to_155_thread_confinement as m155
from infra import migration_156_to_157_rehome_repair as m157


def test_lookups_start_at_the_thread_registrations():
    # The speedup: each thread's lookup is an index search on its own registrations,
    # never a scan that starts at artifact_identities.
    db = sqlite3.connect(':memory:')
    db.executescript(SCHEMA_SQL)
    for sql in (f'SELECT id FROM threads WHERE EXISTS (SELECT 1 {m155._REVIEW_RECORD})',
                f'SELECT id FROM threads WHERE EXISTS ({m157._OWN_CLOSED_RECORD})'):
        plan = ' '.join(row[-1] for row in db.execute('EXPLAIN QUERY PLAN ' + sql))
        assert 'SEARCH r USING INDEX idx_artifact_registrations_thread' in plan, plan
        assert 'SCAN i' not in plan, plan
