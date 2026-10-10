"""Schema 100 -> 101: dated price facts with a charge day, payable by an account or an endpoint.

SQLite cannot relax a NOT NULL column or add a CHECK in place, so both bill
tables are rebuilt with every row copied. A fact's payer is exactly one of
``account_id`` (a vendor account) or ``network_server_id`` (an endpoint). A
``subscription_rate`` fact gains a ``charge_day``, backfilled from the day of its
start date. Two new kinds, ``pay_per_use`` and ``price_cleared``, record an
endpoint's mode and a cleared price from their start date on. Neither payer
column cascades: removing an endpoint is a soft delete, and its history outlives
it. Nothing is dropped.

An old price fact could be bounded (``start_date`` to ``end_date``), but the new
reader lets a price run until a later fact replaces it and ignores ``end_date``.
So each bounded price fact that nothing replaces by its end gets a
``price_cleared`` fact starting on that end date: the old period still ends there.

Runs inside the runner's transaction and never commits: statements go through
``execute`` one at a time because ``executescript`` would commit. The DDL is a
copy of the schema-101 tables in ``infra.vendor_account_schema`` so this step
keeps producing schema 101 after that constant changes.
"""

from __future__ import annotations

import sqlite3

_REVISIONS = """
CREATE TABLE vendor_account_billing_revisions (
    account_id TEXT REFERENCES vendor_accounts(id) ON DELETE RESTRICT,
    network_server_id TEXT REFERENCES network_servers(id) ON DELETE RESTRICT,
    revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0),
    CHECK ((account_id IS NULL) != (network_server_id IS NULL))
)"""

_FACTS = """
CREATE TABLE vendor_account_billing_facts (
    id TEXT PRIMARY KEY,
    account_id TEXT REFERENCES vendor_accounts(id) ON DELETE RESTRICT,
    network_server_id TEXT REFERENCES network_servers(id) ON DELETE RESTRICT,
    kind TEXT NOT NULL CHECK (kind IN ('subscription_rate','overage_paid','pay_per_use','price_cleared')),
    amount_cents INTEGER NOT NULL CHECK (amount_cents >= 0),
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    charge_day INTEGER CHECK (charge_day IS NULL OR charge_day BETWEEN 1 AND 31),
    provenance TEXT NOT NULL DEFAULT 'user_recorded' CHECK (provenance='user_recorded'),
    account_revision INTEGER CHECK (account_revision IS NULL OR account_revision >= 0),
    receipt_revision INTEGER,
    receipt_login_seq INTEGER,
    supersedes_id TEXT REFERENCES vendor_account_billing_facts(id),
    created_at TEXT NOT NULL,
    CHECK ((account_id IS NULL) != (network_server_id IS NULL)),
    CHECK ((kind = 'subscription_rate') = (charge_day IS NOT NULL)),
    CHECK (start_date < end_date),
    CHECK ((receipt_revision IS NULL AND receipt_login_seq IS NULL)
        OR (receipt_revision >= 0 AND receipt_login_seq >= 0))
)"""

_INDEXES = (
    "CREATE UNIQUE INDEX idx_vendor_account_billing_revisions_account "
    "ON vendor_account_billing_revisions(account_id) WHERE account_id IS NOT NULL",
    "CREATE UNIQUE INDEX idx_vendor_account_billing_revisions_server "
    "ON vendor_account_billing_revisions(network_server_id) WHERE network_server_id IS NOT NULL",
    "CREATE INDEX idx_vendor_account_billing_facts_account "
    "ON vendor_account_billing_facts(account_id,kind,start_date,end_date,created_at) WHERE account_id IS NOT NULL",
    "CREATE INDEX idx_vendor_account_billing_facts_server "
    "ON vendor_account_billing_facts(network_server_id,kind,start_date,end_date,created_at) "
    "WHERE network_server_id IS NOT NULL",
    "CREATE UNIQUE INDEX idx_vendor_account_billing_facts_supersedes "
    "ON vendor_account_billing_facts(supersedes_id) WHERE supersedes_id IS NOT NULL",
)

#: Mirrors billing_prices.PRICE_KINDS and OPEN_END, copied so this step keeps its meaning.
_PRICE_KINDS = "('subscription_rate','pay_per_use','price_cleared')"
_OPEN_END = "9999-12-31"

# A current price fact whose period ended, with no other current price fact of
# the payer taking over between its start (exclusive) and its end (inclusive).
_CLEAR_ENDED_PERIODS = f"""
INSERT INTO vendor_account_billing_facts(id,account_id,kind,amount_cents,start_date,end_date,charge_day,
    provenance,supersedes_id,created_at)
SELECT lower(hex(randomblob(16))),f.account_id,'price_cleared',0,f.end_date,'{_OPEN_END}',NULL,
    'user_recorded',NULL,strftime('%Y-%m-%dT%H:%M:%SZ','now')
FROM vendor_account_billing_facts f
WHERE f.kind IN {_PRICE_KINDS} AND f.end_date < '{_OPEN_END}' AND f.account_id IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM vendor_account_billing_facts r WHERE r.supersedes_id=f.id)
  AND NOT EXISTS (
      SELECT 1 FROM vendor_account_billing_facts o
      WHERE o.account_id=f.account_id AND o.kind IN {_PRICE_KINDS} AND o.id != f.id
        AND NOT EXISTS (SELECT 1 FROM vendor_account_billing_facts r WHERE r.supersedes_id=o.id)
        AND o.start_date > f.start_date AND o.start_date <= f.end_date)
GROUP BY f.account_id,f.end_date
"""

_FACT_COLUMNS = (
    "id,account_id,kind,amount_cents,start_date,end_date,provenance,account_revision,"
    "receipt_revision,receipt_login_seq,supersedes_id,created_at"
)


def migrate_100_to_101(conn: sqlite3.Connection) -> None:
    """Rebuild both bill tables with the payer CHECK and charge day, keeping every fact.

    The old tables are renamed aside and dropped, rather than the new ones being
    renamed into place, so the stored DDL of the new tables is the same text a
    fresh install writes. A database that already has the charge day column is
    left alone, so a repeated run is safe.
    """
    if any(row[1] == "charge_day" for row in conn.execute("PRAGMA table_info(vendor_account_billing_facts)")):
        return
    conn.execute("ALTER TABLE vendor_account_billing_revisions RENAME TO vendor_account_billing_revisions_old")
    conn.execute("ALTER TABLE vendor_account_billing_facts RENAME TO vendor_account_billing_facts_old")
    conn.execute(_REVISIONS)
    conn.execute(_FACTS)
    conn.execute(
        "INSERT INTO vendor_account_billing_revisions(account_id,revision) "
        "SELECT account_id,revision FROM vendor_account_billing_revisions_old"
    )
    conn.execute(
        f"INSERT INTO vendor_account_billing_facts({_FACT_COLUMNS},charge_day) "
        f"SELECT {_FACT_COLUMNS},"
        "CASE WHEN kind='subscription_rate' THEN CAST(substr(start_date,9,2) AS INTEGER) END "
        "FROM vendor_account_billing_facts_old ORDER BY rowid"
    )
    conn.execute(_CLEAR_ENDED_PERIODS)
    conn.execute("DROP TABLE vendor_account_billing_facts_old")
    conn.execute("DROP TABLE vendor_account_billing_revisions_old")
    for statement in _INDEXES:
        conn.execute(statement)
