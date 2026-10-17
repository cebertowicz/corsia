"""Schema v101 gives bill facts a charge day and lets an External endpoint, not only an account, owe them."""
from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest

from infra.database_migrations import MIGRATIONS
from infra.migration_100_to_101_billing_charge_day import migrate_100_to_101
from infra.schema import SCHEMA_VERSION

# The schema-100 bill tables exactly as the v94 -> v95 step created them.
_V100_BILL = """
CREATE TABLE vendor_account_billing_revisions (
    account_id TEXT PRIMARY KEY REFERENCES vendor_accounts(id) ON DELETE RESTRICT,
    revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0)
);
CREATE TABLE vendor_account_billing_facts (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES vendor_accounts(id) ON DELETE RESTRICT,
    kind TEXT NOT NULL CHECK (kind IN ('subscription_rate','overage_paid')),
    amount_cents INTEGER NOT NULL CHECK (amount_cents >= 0),
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    provenance TEXT NOT NULL DEFAULT 'user_recorded' CHECK (provenance='user_recorded'),
    account_revision INTEGER NOT NULL CHECK (account_revision >= 0),
    receipt_revision INTEGER,
    receipt_login_seq INTEGER,
    supersedes_id TEXT REFERENCES vendor_account_billing_facts(id),
    created_at TEXT NOT NULL,
    CHECK (start_date < end_date),
    CHECK ((receipt_revision IS NULL AND receipt_login_seq IS NULL)
        OR (receipt_revision >= 0 AND receipt_login_seq >= 0))
);
CREATE INDEX idx_vendor_account_billing_facts_account
    ON vendor_account_billing_facts(account_id,kind,start_date,end_date,created_at);
CREATE UNIQUE INDEX idx_vendor_account_billing_facts_supersedes
    ON vendor_account_billing_facts(supersedes_id) WHERE supersedes_id IS NOT NULL;
"""
_V100 = """
CREATE TABLE vendor_accounts (id TEXT PRIMARY KEY);
CREATE TABLE network_servers (id TEXT PRIMARY KEY);
""" + _V100_BILL + """
INSERT INTO vendor_accounts VALUES ('acct');
INSERT INTO network_servers VALUES ('ns-1');
"""
_ROWS = """
INSERT INTO vendor_account_billing_revisions VALUES ('acct', 2);
INSERT INTO vendor_account_billing_facts VALUES
    ('f1','acct','subscription_rate',2400,'2026-02-17','2026-03-01','user_recorded',4,3,7,NULL,'t1'),
    ('f2','acct','subscription_rate',2500,'2026-02-09','2026-03-01','user_recorded',4,NULL,NULL,'f1','t2'),
    ('f3','acct','overage_paid',900,'2026-02-01','2026-03-01','user_recorded',4,NULL,NULL,NULL,'t3');
"""


def _v100(*, rows: bool) -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(_V100 + (_ROWS if rows else ""))
    return db


def test_step_is_registered_at_100() -> None:
    assert SCHEMA_VERSION == 124
    assert MIGRATIONS[100] is migrate_100_to_101


def test_every_fact_survives_and_a_subscription_gets_its_charge_day_from_its_start_date() -> None:
    with closing(_v100(rows=True)) as db:
        before = [dict(row) for row in db.execute("SELECT * FROM vendor_account_billing_facts ORDER BY id")]
        migrate_100_to_101(db)
        # The only addition is the cleared fact that ends the bounded period (see the bounded-period test).
        after = [dict(row) for row in db.execute(
            "SELECT * FROM vendor_account_billing_facts WHERE kind != 'price_cleared' ORDER BY id")]
        extra = ("network_server_id", "charge_day")
        assert [{k: v for k, v in row.items() if k not in extra} for row in after] == before
        assert [row["charge_day"] for row in after] == [17, 9, None]
        assert all(row["network_server_id"] is None for row in after)
        revision = db.execute("SELECT account_id,network_server_id,revision FROM vendor_account_billing_revisions").fetchall()
        assert [tuple(row) for row in revision] == [("acct", None, 2)]


def test_an_empty_database_migrates_and_the_step_is_safe_to_run_again() -> None:
    with closing(_v100(rows=False)) as db:
        migrate_100_to_101(db)
        migrate_100_to_101(db)
        assert db.execute("SELECT COUNT(*) FROM vendor_account_billing_facts").fetchone()[0] == 0
    with closing(_v100(rows=True)) as db:
        migrate_100_to_101(db)
        migrate_100_to_101(db)
        # Three migrated facts plus one cleared fact for the current subscription's ended period.
        assert db.execute("SELECT COUNT(*) FROM vendor_account_billing_facts").fetchone()[0] == 4


_BOUNDED = """
INSERT INTO vendor_accounts VALUES ('ended'), ('replaced');
INSERT INTO vendor_account_billing_facts VALUES
    ('b1','ended','subscription_rate',2000,'2026-01-10','2026-03-10','user_recorded',1,NULL,NULL,NULL,'t1'),
    ('b2','replaced','subscription_rate',3000,'2026-01-05','2026-05-05','user_recorded',1,NULL,NULL,NULL,'t2'),
    ('b3','replaced','subscription_rate',3500,'2026-05-05','9999-12-31','user_recorded',1,NULL,NULL,NULL,'t3');
"""


def test_a_bounded_period_still_ends_where_it_did_and_one_that_is_replaced_stays_unbroken() -> None:
    from datetime import date

    from connections.billing_prices import charges_in_range, fact_in_effect, price_facts_by_payer

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(_V100 + _BOUNDED)
    with closing(db):
        migrate_100_to_101(db)
        cleared = [dict(row) for row in db.execute(
            "SELECT account_id,start_date,end_date,amount_cents,charge_day,supersedes_id,provenance "
            "FROM vendor_account_billing_facts WHERE kind='price_cleared'")]
        # Only the period nothing takes over from gets one, starting on its end date.
        assert cleared == [{"account_id": "ended", "start_date": "2026-03-10", "end_date": "9999-12-31",
                            "amount_cents": 0, "charge_day": None, "supersedes_id": None,
                            "provenance": "user_recorded"}]
        facts = price_facts_by_payer(db, "account_id")
        ended, replaced = facts["ended"], facts["replaced"]
        assert fact_in_effect(ended, date(2026, 3, 9)).kind == "subscription_rate"
        assert fact_in_effect(ended, date(2026, 3, 10)).kind == "price_cleared"
        assert fact_in_effect(ended, date(2027, 1, 1)).kind == "price_cleared"
        # Charge dates stop with the old period: Jan 10 and Feb 10 only, then the days read unrecorded.
        through_end = charges_in_range(ended, date(2026, 1, 1), None, date(2026, 1, 10), date(2026, 3, 10))
        assert (through_end.cents, through_end.unrecorded) == (4000, False)
        after = charges_in_range(ended, date(2026, 1, 1), None, date(2026, 3, 10), date(2026, 7, 1))
        assert (after.cents, after.unrecorded) == (0, True)
        # The replaced period hands over to its successor with no gap, and keeps charging.
        assert [fact.kind for fact in replaced] == ["subscription_rate", "subscription_rate"]
        run = charges_in_range(replaced, date(2026, 1, 1), None, date(2026, 1, 5), date(2026, 7, 1))
        assert (run.cents, run.unrecorded) == (3000 * 4 + 3500 * 2, False)


def test_rebuilt_tables_enforce_the_payer_and_charge_day_checks() -> None:
    with closing(_v100(rows=True)) as db:
        migrate_100_to_101(db)
        db.commit()  # the pragma is a no-op inside a transaction
        db.execute("PRAGMA foreign_keys=ON")
        insert = ("INSERT INTO vendor_account_billing_facts(id,account_id,network_server_id,kind,amount_cents,"
                  "start_date,end_date,charge_day,created_at) VALUES (?,?,?,?,?,?,?,?,?)")
        sub = ("subscription_rate", 1, "2026-03-01", "9999-12-31")
        db.execute(insert, ("e1", None, "ns-1", *sub, 31, "t"))
        db.execute(insert, ("e2", None, "ns-1", "pay_per_use", 0, "2026-03-02", "9999-12-31", None, "t"))
        db.execute(insert, ("e3", None, "ns-1", "price_cleared", 0, "2026-03-03", "9999-12-31", None, "t"))
        for args in (("x1", None, None, *sub, 1, "t"), ("x2", "acct", "ns-1", *sub, 1, "t"),
                     ("x3", None, "ns-1", *sub, 0, "t"), ("x4", None, "ns-1", *sub, 32, "t"),
                     ("x5", None, "ns-1", *sub, None, "t"),
                     ("x6", None, "ns-1", "pay_per_use", 0, "2026-03-02", "9999-12-31", 5, "t"),
                     ("x7", None, "ns-1", "bogus", 0, "2026-03-02", "9999-12-31", None, "t")):
            with pytest.raises(sqlite3.IntegrityError):
                db.execute(insert, args)
        schema = db.execute("SELECT sql FROM sqlite_master WHERE name='vendor_account_billing_facts'").fetchone()[0]
        assert "_old" not in schema
        with pytest.raises(sqlite3.IntegrityError):  # no cascade: a payer with facts cannot be deleted
            db.execute("DELETE FROM network_servers WHERE id='ns-1'")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO vendor_account_billing_revisions(account_id,network_server_id,revision) "
                       "VALUES ('acct','ns-1',0)")
        with pytest.raises(sqlite3.IntegrityError):  # one revision row per payer
            db.execute("INSERT INTO vendor_account_billing_revisions(account_id,revision) VALUES ('acct',0)")


def test_the_indexes_exist_after_the_rebuild() -> None:
    with closing(_v100(rows=True)) as db:
        migrate_100_to_101(db)
        names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        assert {"idx_vendor_account_billing_facts_account", "idx_vendor_account_billing_facts_server",
                "idx_vendor_account_billing_facts_supersedes", "idx_vendor_account_billing_revisions_account",
                "idx_vendor_account_billing_revisions_server"} <= names


def test_a_fresh_install_and_a_migrated_database_agree_on_the_bill_tables() -> None:
    from infra.schema import SCHEMA_SQL

    def shape(db: sqlite3.Connection) -> dict[str, list[tuple]]:
        return {table: [tuple(c) for c in db.execute(f"PRAGMA table_info({table})")]
                for table in ("vendor_account_billing_facts", "vendor_account_billing_revisions")}

    with closing(sqlite3.connect(":memory:")) as fresh, closing(_v100(rows=True)) as migrated:
        fresh.executescript(SCHEMA_SQL)
        migrate_100_to_101(migrated)
        assert shape(fresh) == shape(migrated)


@pytest.mark.parametrize("start", [97, 98, 99, 100])
def test_the_whole_chain_from_97_ends_current_with_the_bill_facts_intact(start: int) -> None:
    from infra.database import initialize_schema
    from infra.database_migrations import migrate_to_current

    with closing(sqlite3.connect(":memory:")) as db:
        initialize_schema(db)
        # Wind a fresh database back to its schema-`start` shape.
        db.execute("DROP TABLE vendor_account_billing_facts")
        db.execute("DROP TABLE vendor_account_billing_revisions")
        db.executescript(_V100_BILL)
        if start <= 98:
            db.execute("DROP TABLE plan_status_dirty")
        if start <= 97:
            db.execute("ALTER TABLE llm_usage DROP COLUMN reasoning_effort")
            db.execute("DROP TABLE limit_observations")
        db.execute("INSERT INTO connections(id,type,provider,config,created_at,updated_at) "
                   "VALUES ('conn','cli','codex','{}','t','t')")
        db.execute("INSERT INTO vendor_accounts(id,connection_id,name,created_at,updated_at) "
                   "VALUES ('chain-acct','conn','Account','t','t')")
        db.execute(
            "INSERT INTO vendor_account_billing_facts(id,account_id,kind,amount_cents,start_date,end_date,"
            "account_revision,created_at) VALUES ('c1','chain-acct','subscription_rate',2000,'2026-02-17','9999-12-31',1,'t')")
        db.execute(f"PRAGMA user_version={start}")
        db.commit()

        migrate_to_current(db, SCHEMA_VERSION)

        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 124
        assert db.execute("SELECT kind,amount_cents,charge_day FROM vendor_account_billing_facts WHERE id='c1'").fetchone() == (
            "subscription_rate", 2000, 17)
        assert db.execute("SELECT COUNT(*) FROM plan_status_dirty").fetchone() == (0,)
        assert not migrate_to_current(db, SCHEMA_VERSION).migrated
