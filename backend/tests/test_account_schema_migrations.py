"""The account schema migrations, replayed against canonical database shapes.

Pins: the v81/v82/v83 account migrations, the precursor and cleanup migrations and the
deployed-v80 upgrade, each compared to a frozen canonical fixture rather than a hand-written
expectation."""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
from pathlib import Path
import re
import sqlite3

import pytest

from connections.harness_usage import stored_usage
from connections.vendor_accounts import get_account, record_auth_cleanup
from infra.database import initialize_schema
from infra.database_migrations import migrate_to_current, MigrationError, MIGRATIONS
from infra.migration_78_to_79_stored_names import scan_stored_names
from infra.migration_79_to_80_bulletins import migrate_79_to_80
from infra.migration_80_to_81_vendor_accounts import migrate_80_to_81
from infra.migration_81_to_82_vendor_policy import migrate_81_to_82
from infra.schema import SCHEMA_VERSION
from stored_names_fixtures import (
    dead_letter,
    DEAD_LETTER_SEED,
    derived,
    HISTORY,
    installed_metadata,
    message,
    NEW,
    OLD,
    raw_metadata,
    restart_registration,
    RESTART_SEED,
    RESTART_TURN,
    THREAD_ID,
    usage_metadata,
)
from vendor_account_fixtures import accounts, claude, default_id, KEY  # noqa: F401


def _shape(db, table):
    columns = sorted(tuple(row)[1:] for row in db.execute(f"PRAGMA table_info({table})"))
    foreign_keys = sorted(tuple(row)[2:] for row in db.execute(f"PRAGMA foreign_key_list({table})"))
    indexes = sorted((row[1], row[2], row[3], row[4]) for row in db.execute(f"PRAGMA index_list({table})"))
    return columns, foreign_keys, indexes


_LADDER_END = 166  # the last step before 166 -> 167, which converts every ambient account to account-owned homes
_LEGACY_POLICY_TABLES = ("project_vendor_accounts", "thread_vendor_account_holds")
_CURRENT_POLICY_TABLES = ("provider_policies", "project_policy_overrides", "thread_vendor_account_holds")


def _legacy_78(path):
    """Seed only historical rows on the frozen canonical v78 schema."""
    with closing(sqlite3.connect(path)) as db:
        _precursor(db, 78)
        db.execute("INSERT INTO projects(id,name,created_at,updated_at) VALUES ('proj','Project','t0','t0')")
        db.execute("INSERT INTO threads(id,user_id,model,created_at,updated_at) "
                   "VALUES ('legacy','test-user','codex/gpt-5.6-terra','t0','t0')")
        db.commit()


def _snapshot(db):
    return {
        "connections": db.execute("SELECT id,provider FROM connections ORDER BY id").fetchall(),
        "threads": db.execute("SELECT id,model FROM threads ORDER BY id").fetchall(),
        "projects": db.execute("SELECT id,name FROM projects ORDER BY id").fetchall(),
    }


# Genuine canonical79 plus the unchanged accepted 3f26c286 schema80/81 DDL.
# Composition applies deployed bulletin79→80 first, then that account DDL at81/82.
# This predecessor is not a fresh schema83 database relabelled as schema82.
def canonical82():
    source = (Path(__file__).parent / 'fixtures' / 'canonical_dev79.sql').read_bytes()
    assert hashlib.sha256(source).hexdigest() == '978b0c5d0856ba71219d19b273482c1b4a91363c378fc41a1326a0caabcd1615'
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    db.executescript(source.decode())
    assert db.execute('PRAGMA user_version').fetchone()[0] == 79
    migrate_79_to_80(db)
    migrate_80_to_81(db)
    migrate_81_to_82(db)
    db.execute('PRAGMA user_version=82')
    db.commit()
    assert 'switch_on_limit' not in [row['name'] for row in db.execute('PRAGMA table_info(project_vendor_accounts)')]
    return db


def policy_shape(db):
    return [tuple(row) for row in db.execute('PRAGMA table_info(project_vendor_accounts)')]


def populated_policy(db):
    db.execute("INSERT INTO projects(id,name,path,created_at,updated_at) VALUES ('p','P','/synthetic','t','t')")
    db.execute("INSERT INTO connections(id,type,provider,config,created_at,updated_at) VALUES ('c','cli','codex','{}','t','t')")
    for identity in ('a', 'b'):
        db.execute("INSERT INTO vendor_accounts(id,connection_id,name,is_default,state,created_at,updated_at) "
                   "VALUES (?,'c',?,0,'signed_in','t','t')", (identity, identity))
    db.execute("INSERT INTO project_vendor_accounts(project_id,provider,allowlist_json,default_account_id,revision) "
               "VALUES ('p','codex','[\"b\",\"a\"]','a',7)")
    db.commit()
    return [tuple(row) for row in db.execute('SELECT * FROM project_vendor_accounts')]


def unrelated_rows(db):
    return {table: [tuple(row) for row in db.execute(f'SELECT * FROM {table}')]
            for table in ('projects', 'connections', 'vendor_accounts')}


_CANONICAL_DEV78_PATH = Path(__file__).parent / "fixtures" / "canonical_dev78.sql"


_DIGEST = "2cf7e22968d668eed6de7232ccb63f9efb7a6e0c4e026d816780ec9d1634544c"


_OLD_ACCOUNT = """
CREATE TABLE vendor_accounts (
 id TEXT PRIMARY KEY, connection_id TEXT NOT NULL REFERENCES connections(id),
 name TEXT NOT NULL CHECK(length(trim(name))>0), source_home TEXT, runtime_home TEXT,
 is_default INTEGER NOT NULL DEFAULT 0 CHECK(is_default IN (0,1)),
 state TEXT NOT NULL DEFAULT 'signed_out' CHECK(state IN ('legacy','signed_in','signed_out','removed')),
 revision INTEGER NOT NULL DEFAULT 0,
 identity_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(identity_json)),
 discovery_json TEXT CHECK(discovery_json IS NULL OR json_valid(discovery_json)),
 usage_json TEXT CHECK(usage_json IS NULL OR json_valid(usage_json)),
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, removed_at TEXT,
 CHECK((source_home IS NULL AND runtime_home IS NULL) OR
 (source_home IS NOT NULL AND runtime_home IS NOT NULL AND source_home != runtime_home))
);
"""


_RECEIPT = {"status": "failed", "cause": "vendor_logout_failed", "at": "t1"}


def _baseline(db):
    source = _CANONICAL_DEV78_PATH.read_bytes()
    assert hashlib.sha256(source).hexdigest() == _DIGEST
    db.executescript(source.decode())
    assert db.execute("PRAGMA user_version").fetchone()[0] == 78
    assert db.execute("SELECT name FROM sqlite_master WHERE name='vendor_accounts'").fetchone() is None
    assert {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE name LIKE 'outcome_event%'")} == {
        "outcome_events", "outcome_event_counters"}
    assert len(db.execute("SELECT name FROM sqlite_master WHERE name LIKE 'idx_measurement_%'").fetchall()) == 4


def _checks(sql):
    result = []
    for match in re.finditer(r"CHECK\s*\(", sql, re.I):
        start, depth, end = match.end(), 1, match.end()
        while depth:
            depth += (sql[end] == "(") - (sql[end] == ")")
            end += 1
        result.append(re.sub(r"\s+", "", sql[start:end - 1]).lower())
    return sorted(result)


def _synthetic_preexisting(db):
    _baseline(db)
    db.executescript(_OLD_ACCOUNT)
    db.execute("INSERT INTO connections(id,type,provider,config,created_at,updated_at) "
               "VALUES ('c','cli','codex','{}','t0','t0')")
    for index, state in enumerate(("legacy", "signed_in", "signed_out", "removed")):
        identity = {"email": f"synthetic-{index}", "hint": "keep", "auth_cleanup": _RECEIPT}
        db.execute("INSERT INTO vendor_accounts(id,connection_id,name,is_default,state,revision,identity_json,"
                   "discovery_json,usage_json,created_at,updated_at,removed_at) VALUES (?,'c',?,?,?,?,?,'{}',?,'t0','t0',?)",
                   (str(index), state, int(index == 0), state, 4, json.dumps(identity),
                    json.dumps({"version": 3, "facts": {"plan": state}, "windows": {}}), "t0" if state == "removed" else None))
    db.commit()


_FIXTURES = Path(__file__).parent / "fixtures"


_DEPLOYED80 = ("canonical_dev80_459.sql", "86ec9526498f9587aa183f2117008d08cb10541a0ccf359d9ce5d4734945daa7")


_CANONICAL79 = ("canonical_dev79.sql", "978b0c5d0856ba71219d19b273482c1b4a91363c378fc41a1326a0caabcd1615")


_BULLETIN_TABLES = {"system_bulletins", "system_bulletin_identity"}


_ACCOUNT_TABLE_SET = {"vendor_accounts", "thread_vendor_accounts", "project_vendor_accounts",
                   "thread_vendor_account_holds"}


_USAGE = {"version": 1, "facts": {"plan": "p"}}


_LEDGER = [
    ("b-pending", "system_change", "pending", None, None, None, None, None, "t1"),
    ("b-delivered", "live", "delivered", None, "delivery-1", "t2", None, None, "t2"),
    ("b-acted", "system_change", "acted", "applied", "delivery-2", "t3", "t4", None, "t4"),
    ("b-withdrawn", "live", "withdrawn", "superseded", "delivery-3", "t5", None, "t6", "t6"),
]


def _load(db: sqlite3.Connection, fixture: tuple[str, str], version: int) -> None:
    name, digest = fixture
    source = (_FIXTURES / name).read_bytes()
    assert hashlib.sha256(source).hexdigest() == digest
    db.executescript(source.decode())
    assert db.execute("PRAGMA user_version").fetchone()[0] == version


def _columns(db: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in db.execute(f'PRAGMA table_info("{table}")')}


def _no_future_artifacts(db: sqlite3.Connection) -> None:
    assert not _tables(db) & _ACCOUNT_TABLE_SET
    assert "vendor_account_id" not in _columns(db, "llm_usage")
    assert "vendor_account_id" not in _columns(db, "active_turns")


def _deployed80(db: sqlite3.Connection) -> None:
    """Genuine schema80 with a populated ledger, connections, a bound thread and old spend."""
    _load(db, _DEPLOYED80, 80)
    assert _BULLETIN_TABLES <= _tables(db)
    _no_future_artifacts(db)
    db.execute("INSERT INTO system_bulletin_identity(id,version,digest,files_json,reconciled_at) "
               "VALUES (1,'v1','d1','[\"a.md\"]','t0')")
    db.executemany("INSERT INTO system_bulletins(id,source,state,disposition,delivery_id,delivered_at,"
                   "acted_at,withdrawn_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)", _LEDGER)
    db.execute("INSERT INTO settings(key,value,updated_at) VALUES ('system_bulletins_enabled','true','t0')")
    db.execute("INSERT INTO connections(id,type,provider,config,created_at,updated_at) "
               "VALUES ('c-codex','cli','codex',?,'t0','t0')",
               (json.dumps({"binary": "/bin/synthetic", "usage": _USAGE}),))
    db.execute("INSERT INTO connections(id,type,provider,config,created_at,updated_at) "
               "VALUES ('c-gh','api','github','{\"owner\": \"synthetic\"}','t0','t0')")
    db.execute("INSERT INTO models(id,provider,provider_model_id,litellm_model,display_name,harness,"
               "created_at,updated_at) VALUES ('m','codex','gpt','codex/gpt','GPT','codex','t0','t0')")
    db.execute("INSERT INTO threads(id,user_id,title,model,next_message_seq,created_at,updated_at) "
               "VALUES ('t','u','T','m',1,'t0','t0')")
    db.execute("INSERT INTO llm_usage(id,thread_id,turn_id,model,created_at) "
               "VALUES ('spend','t',NULL,'codex/gpt','t0')")


def _ledger(db: sqlite3.Connection) -> list[tuple]:
    return list(db.execute("SELECT id,source,state,disposition,delivery_id,delivered_at,acted_at,"
                           "withdrawn_at,updated_at FROM system_bulletins ORDER BY id"))


def _ddl(db: sqlite3.Connection, names: set[str]) -> dict[str, str]:
    return {name: sql for name, sql in db.execute("SELECT name,sql FROM sqlite_master") if name in names}


_FROZEN = {
    78: ("canonical_dev78.sql", "2cf7e22968d668eed6de7232ccb63f9efb7a6e0c4e026d816780ec9d1634544c"),
    79: ("canonical_dev79.sql", "978b0c5d0856ba71219d19b273482c1b4a91363c378fc41a1326a0caabcd1615"),
}


_ACCOUNT_TABLE_ORDER = ("vendor_accounts", "thread_vendor_accounts", "project_vendor_accounts",
                   "thread_vendor_account_holds")


def _precursor(db: sqlite3.Connection, version: int) -> None:
    name, digest = _FROZEN[version]
    source = (_FIXTURES / name).read_bytes()
    assert hashlib.sha256(source).hexdigest() == digest
    db.executescript(source.decode())
    assert db.execute("PRAGMA user_version").fetchone()[0] == version
    assert not _tables(db) & set(_ACCOUNT_TABLE_ORDER)
    db.execute("INSERT INTO connections(id,type,provider,config,created_at,updated_at) "
               "VALUES ('c','cli','codex',?,'t0','t0')",
               (json.dumps({"binary": "/bin/synthetic", "usage": _USAGE}),))
    db.execute("INSERT INTO threads (id,user_id,title,model,next_message_seq,created_at,updated_at) "
               "VALUES (?,'u','T',NULL,9,'t0','t0')", (THREAD_ID,))
    db.execute("INSERT INTO llm_usage(id,thread_id,turn_id,model,created_at) "
               "VALUES ('u',?,NULL,'native/synthetic','t0')", (THREAD_ID,))


def _former_names(db: sqlite3.Connection) -> None:
    """Rows every stored-names owner converts, beside rows that only mention the name."""
    message(db, "m-installed", installed_metadata(OLD), seq=1)
    message(db, "m-usage", usage_metadata(OLD), seq=2)
    message(db, "m-raw", raw_metadata(OLD), seq=3)
    for message_id, metadata in HISTORY.items():
        message(db, message_id, metadata)
    dead_letter(db, derived(OLD, DEAD_LETTER_SEED, "delivery-old"), "delivery-old")
    restart_registration(db, derived(OLD, RESTART_SEED, f"external:{THREAD_ID}:{RESTART_TURN}"),
                         RESTART_TURN, "pending")


def _tables(db: sqlite3.Connection) -> set[str]:
    return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _whole_database_shape(db: sqlite3.Connection) -> dict:
    result = {}
    for name, sql in db.execute("SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
        columns = sorted(tuple(row)[1:] for row in db.execute(f'PRAGMA table_info("{name}")'))
        fks = sorted(tuple(row)[2:] for row in db.execute(f'PRAGMA foreign_key_list("{name}")'))
        indexes = sorted(tuple(row)[1:] for row in db.execute(f'PRAGMA index_list("{name}")'))
        result[name] = columns, fks, indexes, _checks(sql)
    return result


def _metadata(db: sqlite3.Connection) -> dict[str, str | None]:
    return dict(db.execute("SELECT id, metadata FROM messages"))


def _converted_source(db: sqlite3.Connection) -> None:
    # 166 -> 167 leaves no account signed in, which is what makes the source need sign-in;
    # the connection row itself is left as it was.
    status, config = db.execute("SELECT status,config FROM connections").fetchone()
    assert (status, json.loads(config)) == ("available", {"binary": "/bin/synthetic"})
    assert db.execute("SELECT COUNT(*) FROM vendor_accounts WHERE state='signed_in'").fetchone() == (0,)


def _accounts_adopted(db: sqlite3.Connection) -> None:
    # The uniform sign-in upgrade (166 -> 167) clears an adopted row's usage with its comparison and discovery.
    assert db.execute("SELECT usage_json FROM vendor_accounts").fetchone() == (None,)
    assert db.execute("SELECT auth_cleanup_json FROM vendor_accounts").fetchall() == [(None,)]
    _converted_source(db)
    assert db.execute("SELECT vendor_account_id FROM llm_usage WHERE id='u'").fetchone() == (None,)
    for table in _CURRENT_POLICY_TABLES:
        assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_full_startup_78_to_current_parity_once_and_r1_usage_survives(tmp_path):
    path = tmp_path / "schema78.db"
    usage = {"version": 1,
             "windows": {"claude-code:five_hour:account": {"used_percent": 38, "observed_at": "2026-09-30T00:00:00Z"}},
             "facts": {"subscription_type": "max"},
             "vendor_verdicts": {"null": {"state": "limited", "observed_at": "2026-09-30T00:00:00Z"},
                                 "opus": {"state": "allowed", "observed_at": "2026-09-30T01:00:00Z"}},
             "last_check": {"outcome": "unknown", "error": "test timeout"}}
    with closing(sqlite3.connect(path)) as db:
        _precursor(db, 78)
        db.execute("UPDATE connections SET config=? WHERE id='c'", (json.dumps({"binary": "/bin/test", "usage": usage}),))
        for provider in ("claude-code", "github"):
            db.execute("INSERT INTO connections(id,type,provider,config,created_at,updated_at) "
                       "VALUES (?,'cli',?,?,'t0','t0')", (provider, provider, json.dumps({"binary": "/bin/test", "usage": usage})))
        db.execute("INSERT INTO models(id,harness,provider,provider_model_id,litellm_model,display_name,created_at,updated_at) "
                   "VALUES ('claude-code/opus','claude-code','claude-code','opus','claude-code/opus','Opus','t0','t0')")
        db.execute("INSERT INTO threads(id,user_id,model,created_at,updated_at) "
                   "VALUES ('legacy','test-user','claude-code/opus','t0','t0')")
        db.commit()
    with closing(sqlite3.connect(path)) as db, closing(sqlite3.connect(":memory:")) as fresh:
        db.row_factory = sqlite3.Row
        result = migrate_to_current(db, _LADDER_END)
        assert (result.migrated, result.from_version, result.to_version) == (True, 78, _LADDER_END)
        assert MIGRATIONS[80] is migrate_80_to_81
        initialize_schema(fresh)
        billing_objects = {
            "vendor_account_billing_revisions",
            "vendor_account_billing_facts",
            "idx_vendor_account_billing_facts_account",
            "idx_vendor_account_billing_facts_supersedes",
        }
        for table in ("vendor_accounts", "thread_vendor_accounts", "active_turns", "llm_usage"):
            assert _shape(db, table) == _shape(fresh, table)
        def normalized_ddl(objects: dict[str, str]) -> dict[str, str]:
            return {name: " ".join(sql.split()) for name, sql in objects.items()}

        assert normalized_ddl(_ddl(db, billing_objects)) == normalized_ddl(_ddl(fresh, billing_objects))
        accounts = [tuple(row) for row in db.execute("SELECT id,connection_id,state,source_home,runtime_home FROM vendor_accounts ORDER BY connection_id")]
        assert len(accounts) == 2
        assert [row[1:] for row in accounts] == [("c", "legacy", None, None), ("claude-code", "legacy", None, None)]
        for provider in ("claude-code", "codex"):
            query = ("SELECT a.usage_json FROM vendor_accounts a JOIN connections c ON c.id=a.connection_id "
                     "WHERE c.provider=? AND a.is_default=1")
            raw = db.execute(query, (provider,)).fetchone()[0]
            assert json.loads(raw) == usage
            assert json.loads(raw)["version"] == 1
            assert stored_usage(provider=provider, db=db, account_id=default_id(provider, db=db)) == {**usage, "version": 3}
            assert db.execute(query, (provider,)).fetchone()[0] == raw
            config = json.loads(db.execute("SELECT config FROM connections WHERE provider=?", (provider,)).fetchone()[0])
            assert config == {"binary": "/bin/test"}
        assert "usage" in json.loads(db.execute("SELECT config FROM connections WHERE provider='github'").fetchone()[0])
        assert db.execute("SELECT model FROM threads WHERE id='legacy'").fetchone()[0] == "claude-code/opus"
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert not migrate_to_current(db, _LADDER_END).migrated
        assert [tuple(row) for row in db.execute("SELECT id,connection_id,state,source_home,runtime_home FROM vendor_accounts ORDER BY connection_id")] == accounts
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO vendor_accounts(id,connection_id,name,is_default,state,created_at,updated_at) "
                       "VALUES ('duplicate','c','Default',1,'legacy','t0','t0')")
        db.rollback()
        # The upgrade keeps the row that carried a login fact, signed out on its own homes; the never-used row goes.
        upgraded = migrate_to_current(db, SCHEMA_VERSION)
        assert (upgraded.migrated, upgraded.from_version, upgraded.to_version) == (True, _LADDER_END, SCHEMA_VERSION)
        rows = {row[0]: row for row in db.execute(
            "SELECT connection_id,state,source_home,usage_json FROM vendor_accounts")}
        assert rows["c"][1:3] == ("removed", None)
        assert rows["claude-code"][1] == "signed_out" and rows["claude-code"][2]
        assert all(row[3] is None for row in rows.values())
        assert not migrate_to_current(db, SCHEMA_VERSION).migrated


def test_empty_fresh_schema_does_not_invent_vendor_connections():
    with closing(sqlite3.connect(":memory:")) as db:
        initialize_schema(db)
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert db.execute("SELECT COUNT(*) FROM vendor_accounts").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM connections").fetchone()[0] == 0


def test_full_startup_78_to_current_parity_once_and_existing_rows_survive(tmp_path):
    path = tmp_path / "schema78.db"
    _legacy_78(path)
    with closing(sqlite3.connect(path)) as db:
        before = _snapshot(db)
    with closing(sqlite3.connect(path)) as db, closing(sqlite3.connect(":memory:")) as fresh:
        result = migrate_to_current(db, SCHEMA_VERSION)
        assert (result.migrated, result.from_version, result.to_version) == (True, 78, SCHEMA_VERSION)
        assert MIGRATIONS[81] is migrate_81_to_82
        initialize_schema(fresh)
        for table in _CURRENT_POLICY_TABLES:
            assert _shape(db, table) == _shape(fresh, table)
            assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        assert _snapshot(db) == before
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert not migrate_to_current(db, SCHEMA_VERSION).migrated
        assert _snapshot(db) == before
        for table in _CURRENT_POLICY_TABLES:
            assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_migration_runs_inside_caller_transaction_without_committing(tmp_path):
    path = tmp_path / "schema80.db"
    with closing(sqlite3.connect(path)) as db:
        _deployed80(db)
        db.commit()
    with closing(sqlite3.connect(path, isolation_level=None)) as db:
        db.execute("BEGIN")
        migrate_81_to_82(db)
        assert set(_LEGACY_POLICY_TABLES) <= _tables(db)
        db.execute("ROLLBACK")
        assert not set(_LEGACY_POLICY_TABLES) & _tables(db)
        assert db.execute("PRAGMA user_version").fetchone()[0] == 80


def test_empty_fresh_schema_has_no_policy_or_hold_rows():
    with closing(sqlite3.connect(":memory:")) as db:
        initialize_schema(db)
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        for table in _CURRENT_POLICY_TABLES:
            assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_genuine88_upgrade_maps_legacy_policy_to_fresh89_and_is_idempotent():
    with canonical82() as db:
        populated_policy(db)
        MIGRATIONS[82](db)
        db.execute('PRAGMA user_version=83')
        MIGRATIONS[83](db)
        db.execute('PRAGMA user_version=84')
        db.commit()
        migrate_to_current(db, 88)
        db.execute("UPDATE project_vendor_accounts SET switch_on_limit=1")
        db.commit()
        unrelated = unrelated_rows(db)['projects']
        result = migrate_to_current(db, SCHEMA_VERSION)
        assert (result.migrated, result.from_version, result.to_version) == (True, 88, SCHEMA_VERSION)
        assert 'project_vendor_accounts' not in _tables(db)
        assert [tuple(row) for row in db.execute('SELECT project_id,provider_key,rollover,order_json,disabled_json,revision FROM provider_policies')] == [('p', 'codex', 1, '["a", "b"]', '[]', 7)]
        assert [tuple(row) for row in db.execute('SELECT project_id,revision FROM project_policy_overrides')] == [('p', 7)]
        assert unrelated_rows(db)['projects'] == unrelated
        # 166 -> 167 signs the ambient accounts out and leaves the connection row as it was.
        assert tuple(db.execute("SELECT status FROM connections").fetchone()) == ('available',)
        assert db.execute("SELECT COUNT(*) FROM vendor_accounts WHERE state='signed_in'").fetchone()[0] == 0
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
        with sqlite3.connect(':memory:') as fresh:
            initialize_schema(fresh)
            assert fresh.execute('PRAGMA user_version').fetchone()[0] == SCHEMA_VERSION
            assert _whole_database_shape(db) == _whole_database_shape(fresh)
        assert not migrate_to_current(db, SCHEMA_VERSION).migrated


def test_88_to_89_preserves_allowlist_default_rollover_turn_and_denial_fixture():
    """The migrated producer contract retains the legacy fixture's turn source and denials."""
    from connections.account_policy import AccountAdmissionError, select_account, turn_account

    with canonical82() as db:
        populated_policy(db)
        db.execute("INSERT INTO vendor_accounts(id,connection_id,name,is_default,state,created_at,updated_at) "
                   "VALUES ('denied','c','Denied',0,'signed_in','t','t')")
        db.execute("INSERT INTO models(id,provider,provider_model_id,litellm_model,display_name,harness,created_at,updated_at) "
                   "VALUES ('codex/test','codex','test','codex/test','Test','codex','t','t')")
        db.execute("INSERT INTO threads(id,user_id,project_id,model,created_at,updated_at) "
                   "VALUES ('turn','u','p','codex/test','t','t')")
        MIGRATIONS[82](db)
        db.execute('PRAGMA user_version=83')
        MIGRATIONS[83](db)
        db.execute('PRAGMA user_version=84')
        db.commit()
        migrate_to_current(db, 88)
        db.execute("UPDATE project_vendor_accounts SET switch_on_limit=1")
        db.commit()

        # The v88 fixture names a as default, permits a/b, and denies the third account.
        assert json.loads(db.execute("SELECT allowlist_json FROM project_vendor_accounts WHERE project_id='p'").fetchone()[0]) == ['b', 'a']
        assert db.execute("SELECT default_account_id FROM project_vendor_accounts WHERE project_id='p'").fetchone()[0] == 'a'
        migrate_to_current(db, SCHEMA_VERSION)

        # The upgrade keeps the referenced account a on its own homes, signed out until a person signs in.
        assert turn_account('codex', thread_id='turn', db=db) == ('a', 'account_signed_out')
        with pytest.raises(AccountAdmissionError, match='account_project_denied'):
            select_account('turn', 'codex', account_id='denied', db=db)


def test_88_to_89_migrates_two_provider_rows_to_one_project_override():
    with canonical82() as db:
        populated_policy(db)
        db.execute("INSERT INTO connections(id,type,provider,config,created_at,updated_at) VALUES ('cc','cli','claude-code','{}','t','t')")
        db.execute("INSERT INTO vendor_accounts(id,connection_id,name,is_default,state,created_at,updated_at) "
                   "VALUES ('claude','cc','Claude',1,'signed_in','t','t')")
        MIGRATIONS[82](db); db.execute('PRAGMA user_version=83')
        MIGRATIONS[83](db); db.execute('PRAGMA user_version=84'); db.commit()
        migrate_to_current(db, 88)
        db.execute("INSERT INTO project_vendor_accounts(project_id,provider,allowlist_json,default_account_id,revision,switch_on_limit) "
                   "VALUES ('p','claude-code','[\"claude\"]','claude',9,1)")
        db.commit()

        migrate_to_current(db, SCHEMA_VERSION)

        assert [tuple(row) for row in db.execute('SELECT project_id,revision FROM project_policy_overrides')] == [('p', 9)]
        assert [tuple(row) for row in db.execute(
            'SELECT project_id,provider_key,revision FROM provider_policies ORDER BY provider_key'
        )] == [('p', 'claude-code', 9), ('p', 'codex', 7)]


def test_88_to_89_migrates_with_a_plain_tuple_connection():
    with canonical82() as db:
        populated_policy(db)
        MIGRATIONS[82](db); db.execute('PRAGMA user_version=83')
        MIGRATIONS[83](db); db.execute('PRAGMA user_version=84'); db.commit()
        migrate_to_current(db, 88)
        db.row_factory = None

        migrate_to_current(db, SCHEMA_VERSION)

        assert db.row_factory is None
        assert db.execute('SELECT project_id,revision FROM project_policy_overrides').fetchall() == [('p', 7)]


def test_88_to_89_mapping_is_required(monkeypatch):
    original = MIGRATIONS[88]
    def omit_mapping(connection):
        connection.execute('DROP TABLE project_vendor_accounts')
    with canonical82() as db:
        populated_policy(db)
        MIGRATIONS[82](db); db.execute('PRAGMA user_version=83')
        MIGRATIONS[83](db); db.execute('PRAGMA user_version=84'); db.commit()
        migrate_to_current(db, 88)
        monkeypatch.setitem(MIGRATIONS, 88, omit_mapping)
        migrate_to_current(db, SCHEMA_VERSION)
        # The mutant removed the legacy table but omitted the required mapped target.
        with pytest.raises(AssertionError):
            assert 'provider_policies' in _tables(db)
    monkeypatch.setitem(MIGRATIONS, 88, original)


def test_88_to_89_failure_rolls_back_legacy_table(monkeypatch):
    original = MIGRATIONS[88]
    def fail_after_mapping(connection):
        original(connection)
        raise RuntimeError('synthetic provider-policy failure')
    with canonical82() as db:
        populated_policy(db)
        MIGRATIONS[82](db); db.execute('PRAGMA user_version=83')
        MIGRATIONS[83](db); db.execute('PRAGMA user_version=84'); db.commit()
        migrate_to_current(db, 88)
        monkeypatch.setitem(MIGRATIONS, 88, fail_after_mapping)
        with pytest.raises(MigrationError, match='synthetic provider-policy failure'):
            migrate_to_current(db, SCHEMA_VERSION)
        assert db.execute('PRAGMA user_version').fetchone()[0] == 88
        assert 'project_vendor_accounts' in _tables(db)
        assert db.execute("SELECT COUNT(*) FROM project_vendor_accounts WHERE project_id='p'").fetchone()[0] == 1


def test_upgrade_rolls_back_column_version_and_existing_policy(monkeypatch):
    migration = MIGRATIONS[82]
    def fail(connection):
        migration(connection)
        raise RuntimeError('synthetic migration refusal')
    monkeypatch.setitem(MIGRATIONS, 82, fail)
    with canonical82() as db:
        policy = populated_policy(db)
        unrelated = unrelated_rows(db)
        before = policy_shape(db)
        dump = list(db.iterdump())
        with pytest.raises(MigrationError):
            migrate_to_current(db, 83)
        assert policy_shape(db) == before
        assert [tuple(row) for row in db.execute('SELECT * FROM project_vendor_accounts')] == policy
        assert unrelated_rows(db) == unrelated
        assert list(db.iterdump()) == dump
        assert db.execute('PRAGMA user_version').fetchone()[0] == 82


def test_optin_accepts_no_saved_order_preserves_on_unrelated_save_and_is_strict(accounts):
    from connections.provider_policy import effective_policy, set_policy
    from connections.vendor_accounts import AccountError

    db, _, first, second = accounts
    assert effective_policy('destination-project', 'codex', db=db)['rollover'] is False
    order = [second['id'], first['id']]
    enabled = set_policy('destination-project', 'codex', rollover=True, order=order, disabled=[],
                         expected_revision=0, db=db)
    assert enabled['rollover'] is True and enabled['order'] == order
    unchanged = set_policy('destination-project', 'codex', rollover=True, order=order, disabled=[],
                           expected_revision=1, db=db)
    assert unchanged['rollover'] is True and unchanged['order'] == order
    for invalid in (0, 1, 'true'):
        with pytest.raises(AccountError, match='boolean'):
            set_policy('destination-project', 'codex', rollover=invalid, order=order, disabled=[],
                       expected_revision=2, db=db)
    unsaved = set_policy('destination-project', 'codex', rollover=True, order=None, disabled=[],
                         expected_revision=2, db=db)
    assert unsaved['rollover'] is True and unsaved['order'] is None
    disabled = set_policy('destination-project', 'codex', rollover=False, order=None, disabled=[],
                          expected_revision=3, db=db)
    assert disabled['rollover'] is False and disabled['order'] is None


def test_genuine_frozen_canonical78_startup_matches_every_fresh81_table(tmp_path):
    path = tmp_path / "canonical78.db"
    with closing(sqlite3.connect(path)) as db:
        _baseline(db)
        db.execute("INSERT INTO connections(id,type,provider,config,created_at,updated_at) "
                   "VALUES ('c','cli','codex',?,'t0','t0')", (json.dumps({"binary": "/bin/synthetic", "usage": {"version": 1, "facts": {"plan": "p"}}}),))
        db.execute("INSERT INTO threads(id,user_id,model,created_at,updated_at) VALUES ('t','u',NULL,'t0','t0')")
        db.execute("INSERT INTO llm_usage(id,thread_id,turn_id,model,created_at) VALUES ('u','t',NULL,'native/synthetic','t0')")
        db.commit()
    with closing(sqlite3.connect(path)) as db, closing(sqlite3.connect(":memory:")) as fresh:
        assert initialize_schema(db) is False
        initialize_schema(fresh)
        assert _whole_database_shape(db) == _whole_database_shape(fresh)
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("SELECT model FROM threads WHERE id='t'").fetchone() == (None,)
        assert db.execute("SELECT vendor_account_id FROM llm_usage WHERE id='u'").fetchone() == (None,)
        assert db.execute("SELECT usage_json FROM vendor_accounts").fetchone() == (None,)  # cleared by 166 -> 167
        _converted_source(db)
        once = list(db.iterdump())
        assert initialize_schema(db) is False
        assert list(db.iterdump()) == once
    assert list(tmp_path.glob("*.bak*"))  # startup's canonical runner made the precursor backup


def test_synthetic_canonical78_preexisting_accounts_convert_all_states_once(tmp_path):
    with closing(sqlite3.connect(tmp_path / "conversion.db")) as db:
        _synthetic_preexisting(db)
        db.row_factory = sqlite3.Row
        before = [tuple(row) for row in db.execute("SELECT id,state,revision,usage_json,discovery_json,created_at,updated_at,removed_at FROM vendor_accounts ORDER BY id")]
        assert migrate_to_current(db, 82).from_version == 78
        assert [tuple(row) for row in db.execute("SELECT id,state,revision,usage_json,discovery_json,created_at,updated_at,removed_at FROM vendor_accounts ORDER BY id")] == before
        for index in range(4):
            account = get_account(str(index), db=db)
            assert account["identity"] == {"email": f"synthetic-{index}", "hint": "keep"}
            assert account["auth_cleanup"] == _RECEIPT
        once = list(db.iterdump())
        migrate_80_to_81(db)
        db.commit()
        assert list(db.iterdump()) == once
        assert not migrate_to_current(db, 82).migrated


@pytest.mark.parametrize("old", [_RECEIPT, None, "absent"])
def test_retry_preserves_canonical_receipt_for_equal_null_or_absent_old_key(tmp_path, old):
    with closing(sqlite3.connect(tmp_path / "retry.db")) as db:
        _synthetic_preexisting(db)
        migrate_80_to_81(db)
        identity = {"email": "keep"}
        if old != "absent":
            identity["auth_cleanup"] = old
        canonical = json.dumps(_RECEIPT, indent=2)
        db.execute("UPDATE vendor_accounts SET identity_json=?,auth_cleanup_json=? WHERE id='0'", (json.dumps(identity), canonical))
        migrate_80_to_81(db)
        assert db.execute("SELECT identity_json,auth_cleanup_json FROM vendor_accounts WHERE id='0'").fetchone() == (json.dumps({"email": "keep"}), canonical)


@pytest.mark.parametrize("old,cause", [({**_RECEIPT, "at": "other"}, "account_auth_cleanup_conflict"), ([], "account_auth_cleanup_invalid")])
def test_invalid_or_conflicting_receipt_rolls_back_full_startup(tmp_path, old, cause):
    with closing(sqlite3.connect(tmp_path / "bad.db")) as db:
        _synthetic_preexisting(db)
        db.execute("ALTER TABLE vendor_accounts ADD COLUMN auth_cleanup_json TEXT")
        db.execute("UPDATE vendor_accounts SET auth_cleanup_json=?,identity_json=? WHERE id='0'", (json.dumps(_RECEIPT), json.dumps({"auth_cleanup": old})))
        db.commit()
        before = list(db.iterdump())
        with pytest.raises(MigrationError, match=cause):
            migrate_to_current(db, 82)
        assert list(db.iterdump()) == before
        assert db.execute("PRAGMA user_version").fetchone()[0] == 78


def test_later_step_failure_rolls_back_cleanup_column_and_conversion(tmp_path, monkeypatch):
    with closing(sqlite3.connect(tmp_path / "rollback.db")) as db:
        _synthetic_preexisting(db)
        before = list(db.iterdump())
        original = MIGRATIONS[81]
        def fail(connection):
            assert json.loads(connection.execute("SELECT auth_cleanup_json FROM vendor_accounts WHERE id='0'").fetchone()[0]) == _RECEIPT
            raise ValueError("synthetic_after_cleanup")
        monkeypatch.setitem(MIGRATIONS, 81, fail)
        with pytest.raises(MigrationError, match="synthetic_after_cleanup"):
            migrate_to_current(db, 82)
        assert list(db.iterdump()) == before
        monkeypatch.setitem(MIGRATIONS, 81, original)
        # A retry gets a distinct backup timestamp; never overwrite the failed run's backup.
        from datetime import datetime, timedelta, timezone
        from infra import database_migrations
        retry_at = datetime.now(timezone.utc) + timedelta(seconds=10)
        class RetryClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return retry_at
        monkeypatch.setattr(database_migrations, "datetime", RetryClock)
        assert migrate_to_current(db, 82).to_version == 82
        assert len(list(tmp_path.glob("*.bak"))) == 2


def test_column_writer_and_public_projection_keep_cleanup_outside_identity(tmp_path):
    from api.vendor_accounts import _safe
    from connections.vendor_accounts import AccountError

    with closing(sqlite3.connect(tmp_path / "writer.db")) as db:
        _synthetic_preexisting(db)
        migrate_to_current(db, 82)
        db.row_factory = sqlite3.Row
        before = get_account("3", db=db)
        raw_usage = db.execute("SELECT usage_json FROM vendor_accounts WHERE id='3'").fetchone()[0]
        record_auth_cleanup("3", {"status": "done", "cause": None, "at": "t2"}, db=db)
        after = get_account("3", db=db)
        assert after["state"] == before["state"] == "removed"
        assert after["revision"] == before["revision"] == 4
        assert after["identity"] == before["identity"]
        assert db.execute("SELECT usage_json FROM vendor_accounts WHERE id='3'").fetchone()[0] == raw_usage
        assert set(_safe(after)["identity"]) == {"email"}
        assert "auth_cleanup" not in _safe(after)
        with pytest.raises(AccountError, match="account_auth_cleanup_invalid"):
            record_auth_cleanup("3", {"status": "failed", "cause": "vendor output", "at": "t3"}, db=db)
        record_auth_cleanup("3", None, db=db)
        assert get_account("3", db=db)["auth_cleanup"] is None


def test_genuine_deployed80_upgrades_to_fresh83_once_and_keeps_every_row(tmp_path) -> None:
    path = tmp_path / "deployed80.db"
    with closing(sqlite3.connect(path)) as db:
        _deployed80(db)
        db.commit()
        ledger, ddl = _ledger(db), _ddl(db, _BULLETIN_TABLES)
        identity = db.execute("SELECT * FROM system_bulletin_identity").fetchall()
        setting = db.execute("SELECT * FROM settings WHERE key='system_bulletins_enabled'").fetchall()

        result = migrate_to_current(db, _LADDER_END)

        assert (result.migrated, result.from_version, result.to_version) == (True, 80, _LADDER_END)
        assert (_ledger(db), _ddl(db, _BULLETIN_TABLES)) == (ledger, ddl)
        assert db.execute("SELECT * FROM system_bulletin_identity").fetchall() == identity
        assert db.execute("SELECT * FROM settings WHERE key='system_bulletins_enabled'").fetchall() == setting
        accounts = db.execute("SELECT id,connection_id,name,is_default,state,usage_json FROM vendor_accounts").fetchall()
        assert [row[1:5] for row in accounts] == [("c-codex", "Default", 1, "legacy")]
        assert json.loads(accounts[0][5]) == _USAGE
        configs = dict(db.execute("SELECT id,config FROM connections"))
        assert json.loads(configs["c-codex"]) == {"binary": "/bin/synthetic"}
        assert configs["c-gh"] == '{"owner": "synthetic"}'
        assert db.execute("SELECT thread_id,provider,account_id FROM thread_vendor_accounts").fetchall() == [
            ("t", "codex", accounts[0][0])]
        assert db.execute("SELECT model FROM threads WHERE id='t'").fetchone() == ("m",)
        assert db.execute("SELECT vendor_account_id FROM llm_usage WHERE id='spend'").fetchone() == (None,)
        for table in _CURRENT_POLICY_TABLES:
            assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        # 167 -> 168 changes shape only (measure keys), so apply it before comparing with the current schema.
        from infra.migration_167_to_168_measure_keys import migrate_167_to_168
        migrate_167_to_168(db)
        db.commit()
        with closing(sqlite3.connect(":memory:")) as fresh:
            initialize_schema(fresh)
            assert _whole_database_shape(db) == _whole_database_shape(fresh)
        # The upgrade keeps the bound row, signed out on its own homes, with its usage cleared.
        assert migrate_to_current(db, SCHEMA_VERSION).to_version == SCHEMA_VERSION
        assert db.execute("SELECT state,usage_json FROM vendor_accounts").fetchall() == [("signed_out", None)]
        once = list(db.iterdump())
        again = migrate_to_current(db, SCHEMA_VERSION)
        assert (again.migrated, again.from_version, again.to_version) == (False, SCHEMA_VERSION, SCHEMA_VERSION)
        assert list(db.iterdump()) == once
    backups = list(tmp_path.glob("deployed80.db.pre-migration-v80-*.bak"))
    assert len(backups) == 1 and backups[0] == result.backup_path
    assert backups[0].stat().st_mode & 0o777 == 0o600, "the copy holds credentials: owner-only"
    with closing(sqlite3.connect(backups[0])) as backup:
        assert backup.execute("PRAGMA user_version").fetchone()[0] == 80
        assert _ledger(backup) == ledger
        _no_future_artifacts(backup)


@pytest.mark.parametrize("failing", [80, 81, 82])
def test_a_failure_after_each_local_step_leaves_deployed80_unchanged(tmp_path, monkeypatch, failing) -> None:
    with closing(sqlite3.connect(tmp_path / "rollback.db")) as db:
        _deployed80(db)
        db.commit()
        before = list(db.iterdump())
        step = MIGRATIONS[failing]
        seen = []

        def after(connection):
            step(connection)
            seen.append(failing)
            raise ValueError("synthetic_after_step_failure")

        with monkeypatch.context() as patched:
            patched.setitem(MIGRATIONS, failing, after)
            with pytest.raises(MigrationError, match="synthetic_after_step_failure"):
                migrate_to_current(db, SCHEMA_VERSION)
        assert seen == [failing]
        assert list(db.iterdump()) == before
        assert db.execute("PRAGMA user_version").fetchone()[0] == 80
        _no_future_artifacts(db)
        assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 0
        assert not db.in_transaction


def test_an_open_transaction_or_newer_schema_refuses_before_any_effect(tmp_path) -> None:
    with closing(sqlite3.connect(tmp_path / "refuse.db")) as db:
        _deployed80(db)
        db.commit()
        before = list(db.iterdump())
        db.execute("UPDATE settings SET updated_at='t9' WHERE key='system_bulletins_enabled'")
        assert db.in_transaction
        with pytest.raises(MigrationError, match="clean startup connection"):
            migrate_to_current(db, SCHEMA_VERSION)
        db.rollback()
        assert list(db.iterdump()) == before
        with pytest.raises(MigrationError, match="newer than this build"):
            migrate_to_current(db, 79)
        assert list(db.iterdump()) == before
    assert not list(tmp_path.glob("*.bak"))


def test_deployed80_through_82_then_the_rollover_step_keeps_ordered_policy(tmp_path, monkeypatch) -> None:
    with closing(sqlite3.connect(tmp_path / "prefix.db")) as db:
        _deployed80(db)
        db.commit()
        assert migrate_to_current(db, 82).to_version == 82
        assert "switch_on_limit" not in _columns(db, "project_vendor_accounts")
        legacy_account_id = db.execute("SELECT id FROM vendor_accounts WHERE connection_id='c-codex'").fetchone()[0]
        db.execute("INSERT INTO projects(id,name,path,created_at,updated_at) VALUES ('p','P','/synthetic','t','t')")
        for identity in ("a", "b"):
            db.execute("INSERT INTO vendor_accounts(id,connection_id,name,is_default,state,created_at,updated_at) "
                       "VALUES (?,'c-codex',?,0,'signed_in','t','t')", (identity, identity))
        db.execute("INSERT INTO project_vendor_accounts(project_id,provider,allowlist_json,default_account_id,"
                   "revision) VALUES ('p','codex','[\"b\",\"a\"]','a',7)")
        db.commit()
        policy = db.execute("SELECT * FROM project_vendor_accounts").fetchall()
        ledger = _ledger(db)
        before = list(db.iterdump())

        def fail(connection):
            raise ValueError("synthetic_rollover_failure")

        with monkeypatch.context() as patched:
            patched.setitem(MIGRATIONS, 82, fail)
            with pytest.raises(MigrationError, match="synthetic_rollover_failure"):
                migrate_to_current(db, SCHEMA_VERSION)
        assert list(db.iterdump()) == before
        assert db.execute("PRAGMA user_version").fetchone()[0] == 82
        # The failed attempt kept its backup; a retry in the same second refuses to overwrite it.
        failed_backups = list(tmp_path.glob("prefix.db.pre-migration-v82-*.bak"))
        assert len(failed_backups) == 1
        failed_backups[0].unlink()

        db.row_factory = sqlite3.Row
        result = migrate_to_current(db, SCHEMA_VERSION)
        assert (result.migrated, result.from_version, result.to_version) == (True, 82, SCHEMA_VERSION)
        assert 'project_vendor_accounts' not in _tables(db)
        assert [tuple(row) for row in db.execute("SELECT project_id,provider_key,rollover,order_json,disabled_json,revision FROM provider_policies")] == [
            ('p', 'codex', 0, '["a", "b"]', json.dumps([legacy_account_id]), 7)]
        assert [tuple(row) for row in db.execute("SELECT project_id,revision FROM project_policy_overrides")] == [('p', 7)]
        assert [tuple(row) for row in _ledger(db)] == ledger
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE provider_policies SET rollover=2")
        once = list(db.iterdump())
        assert not migrate_to_current(db, SCHEMA_VERSION).migrated
        assert list(db.iterdump()) == once


def test_genuine79_bulletin_step_is_the_fixed_79_to_80_owner_and_rolls_back_with_its_caller() -> None:
    assert MIGRATIONS[79] is migrate_79_to_80
    with closing(sqlite3.connect(":memory:", isolation_level=None)) as db:
        _load(db, _CANONICAL79, 79)
        assert not _tables(db) & (_BULLETIN_TABLES | _ACCOUNT_TABLE_SET)
        before = list(db.iterdump())
        db.execute("BEGIN")
        migrate_79_to_80(db)
        assert _BULLETIN_TABLES <= _tables(db)
        assert db.in_transaction
        db.execute("ROLLBACK")
        assert list(db.iterdump()) == before
        assert not _tables(db) & _BULLETIN_TABLES


def test_the_ladder_keeps_stored_names_at_78_and_the_account_steps_after_it() -> None:
    from infra.migration_78_to_79_stored_names import migrate_78_to_79
    from infra.migration_79_to_80_bulletins import migrate_79_to_80
    from infra.migration_80_to_81_vendor_accounts import migrate_80_to_81
    from infra.migration_81_to_82_vendor_policy import migrate_81_to_82
    from infra.migration_82_to_83_account_rollover import migrate_82_to_83
    from infra.migration_166_to_167_uniform_signin import migrate_166_to_167

    assert SCHEMA_VERSION == 168
    assert MIGRATIONS[_LADDER_END] is migrate_166_to_167
    assert (MIGRATIONS[78], MIGRATIONS[79], MIGRATIONS[80], MIGRATIONS[81]) == (migrate_78_to_79, migrate_79_to_80, migrate_80_to_81, migrate_81_to_82)
    assert MIGRATIONS[82] is migrate_82_to_83
    assert max(MIGRATIONS) == SCHEMA_VERSION - 1


@pytest.mark.parametrize("version", sorted(_FROZEN))
def test_a_blank_canonical_precursor_starts_up_as_every_fresh_81_table_once(tmp_path, version) -> None:
    path = tmp_path / "precursor.db"
    with closing(sqlite3.connect(path)) as db:
        _precursor(db, version)
        db.commit()
    with closing(sqlite3.connect(path)) as db, closing(sqlite3.connect(":memory:")) as fresh:
        assert initialize_schema(db) is False
        initialize_schema(fresh)
        assert fresh.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert _whole_database_shape(db) == _whole_database_shape(fresh)
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        _accounts_adopted(db)
        once = list(db.iterdump())
        assert initialize_schema(db) is False
        assert not migrate_to_current(db, SCHEMA_VERSION).migrated
        assert list(db.iterdump()) == once
    assert list(tmp_path.glob("*.bak*"))


@pytest.mark.parametrize("version", sorted(_FROZEN))
@pytest.mark.parametrize("failing", [80, 81])
def test_a_failing_account_step_leaves_the_precursor_unchanged(
        tmp_path, monkeypatch, version, failing) -> None:
    with closing(sqlite3.connect(tmp_path / "rollback.db")) as db:
        _precursor(db, version)
        db.commit()
        before = list(db.iterdump())

        def fail(connection):
            raise ValueError("synthetic_account_step_failure")

        with monkeypatch.context() as patched:
            patched.setitem(MIGRATIONS, failing, fail)
            with pytest.raises(MigrationError, match="synthetic_account_step_failure"):
                migrate_to_current(db, SCHEMA_VERSION)
        assert list(db.iterdump()) == before
        assert db.execute("PRAGMA user_version").fetchone()[0] == version
        assert not _tables(db) & set(_ACCOUNT_TABLE_ORDER)


def test_former_name_rows_in_canonical_78_convert_and_the_accounts_are_adopted(tmp_path) -> None:
    with closing(sqlite3.connect(tmp_path / "populated.db")) as db, closing(sqlite3.connect(":memory:")) as fresh:
        _precursor(db, 78)
        _former_names(db)
        db.commit()
        planned = scan_stored_names(db).counts()
        assert (planned["metadata_rows"], planned["dead_letters"], planned["restart_registrations"],
                planned["refusals"]) == (3, 1, 1, 0)

        result = migrate_to_current(db, SCHEMA_VERSION)

        assert (result.migrated, result.from_version, result.to_version) == (True, 78, SCHEMA_VERSION)
        stored = _metadata(db)
        assert stored["m-installed"] == installed_metadata(NEW)
        assert stored["m-usage"] == usage_metadata(NEW)
        assert stored["m-raw"] == raw_metadata(NEW)
        assert {key: stored[key] for key in HISTORY} == HISTORY
        assert [row[0] for row in db.execute("SELECT id FROM dead_letter_events")] == [
            derived(NEW, DEAD_LETTER_SEED, "delivery-old")]
        assert [row[0] for row in db.execute("SELECT id FROM restart_wake_registrations")] == [
            derived(NEW, RESTART_SEED, f"external:{THREAD_ID}:{RESTART_TURN}")]
        after = scan_stored_names(db).counts()
        assert (after["metadata_rows"], after["dead_letters"], after["restart_registrations"],
                after["refusals"]) == (0, 0, 0, 0)
        initialize_schema(fresh)
        assert _whole_database_shape(db) == _whole_database_shape(fresh)
        _accounts_adopted(db)
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        once = list(db.iterdump())
        assert not migrate_to_current(db, SCHEMA_VERSION).migrated
        assert list(db.iterdump()) == once


def test_a_refused_former_name_row_stops_before_any_account_table_exists(tmp_path) -> None:
    path = tmp_path / "refused.db"
    with closing(sqlite3.connect(path)) as db:
        _precursor(db, 78)
        _former_names(db)
        message(db, "bad", json.dumps({"other_owner": [{"origin_section": OLD}]}))
        db.commit()
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    with closing(sqlite3.connect(path)) as db:
        with pytest.raises(MigrationError, match="rolled back, database unchanged"):
            migrate_to_current(db, SCHEMA_VERSION)
        assert db.execute("PRAGMA user_version").fetchone()[0] == 78
        assert not _tables(db) & set(_ACCOUNT_TABLE_ORDER)
        assert "usage" in json.loads(db.execute("SELECT config FROM connections").fetchone()[0])
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


@pytest.mark.parametrize("failing", [80, 81])
def test_a_failing_account_step_also_undoes_the_former_name_conversion(tmp_path, monkeypatch, failing) -> None:
    with closing(sqlite3.connect(tmp_path / "undo.db")) as db:
        _precursor(db, 78)
        _former_names(db)
        db.commit()
        before = list(db.iterdump())

        def fail(connection):
            # The conversion has already run inside this same transaction.
            assert dict(connection.execute("SELECT id, metadata FROM messages"))["m-installed"] == installed_metadata(NEW)
            raise ValueError("synthetic_account_step_failure")

        with monkeypatch.context() as patched:
            patched.setitem(MIGRATIONS, failing, fail)
            with pytest.raises(MigrationError, match="synthetic_account_step_failure"):
                migrate_to_current(db, SCHEMA_VERSION)
        assert list(db.iterdump()) == before
        assert _metadata(db)["m-installed"] == installed_metadata(OLD)
        assert db.execute("PRAGMA user_version").fetchone()[0] == 78
        assert not _tables(db) & set(_ACCOUNT_TABLE_ORDER)


def test_canonical_79_keeps_a_row_that_names_the_former_product_exactly_as_stored(tmp_path) -> None:
    """Schema 79 is past the conversion: the account steps read and rewrite no message."""
    with closing(sqlite3.connect(tmp_path / "past.db")) as db:
        _precursor(db, 79)
        for message_id, metadata in HISTORY.items():
            message(db, message_id, metadata)
        db.commit()
        result = migrate_to_current(db, SCHEMA_VERSION)
        assert (result.migrated, result.from_version, result.to_version) == (True, 79, SCHEMA_VERSION)
        assert _metadata(db) == HISTORY
        _accounts_adopted(db)
