"""The account store: passwords, tokens, expiry, revocation, and the migration.

Every test here runs against a real database. The store is what makes a token
mean something, so stubbing it would only prove the stub works.
"""

from __future__ import annotations

from infra.corsia_agent import ensure_corsia_agent, get_corsia_agent, set_corsia_agent
from infra.conductor import (
    ensure_project_conductor, get_project_conductor, get_scope_coordinator,
    set_project_conductor, set_scope_coordinator,
)

import os
import sqlite3
import sys
from datetime import timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from infra import account_validation, accounts, token_admin  # noqa: E402
from infra.accounts_schema import ACCOUNTS_SCHEMA_SQL  # noqa: E402
from infra.passwords import (  # noqa: E402
    MIN_PASSWORD_LENGTH,
    PasswordFormatError,
    hash_password,
    verify_password,
)
from infra.time import utc_now, utc_now_iso  # noqa: E402

GOOD_PASSWORD = "a-long-enough-secret"


class TestPasswordHashing:

    def test_round_trip_and_wrong_password(self):
        encoded = hash_password(GOOD_PASSWORD)
        assert verify_password(GOOD_PASSWORD, encoded) is True
        assert verify_password(GOOD_PASSWORD + "x", encoded) is False
        assert verify_password("", encoded) is False

    def test_encoded_form_carries_its_own_parameters(self):
        scheme, n, r, p, salt, digest = hash_password(GOOD_PASSWORD).split("$")
        assert scheme == "scrypt"
        assert (int(n), int(r), int(p)) == (1 << 17, 8, 1)
        assert salt and digest

    def test_each_hash_gets_a_fresh_salt(self):
        first, second = hash_password(GOOD_PASSWORD), hash_password(GOOD_PASSWORD)
        assert first != second
        assert verify_password(GOOD_PASSWORD, first)
        assert verify_password(GOOD_PASSWORD, second)

    def test_stored_hash_created_with_other_parameters_still_verifies(self):
        # The point of storing N/r/p: raising the cost later must not lock
        # anyone out. Build a cheap hash by hand and verify it unchanged.
        import base64
        import hashlib
        import secrets

        salt = secrets.token_bytes(16)
        digest = hashlib.scrypt(
            GOOD_PASSWORD.encode(), salt=salt, n=1 << 12, r=8, p=1,
            maxmem=128 * (1 << 12) * 8 + (1 << 20), dklen=32,
        )
        b64 = lambda raw: base64.b64encode(raw).decode().rstrip("=")  # noqa: E731
        encoded = f"scrypt${1 << 12}$8$1${b64(salt)}${b64(digest)}"
        assert verify_password(GOOD_PASSWORD, encoded) is True

    @pytest.mark.parametrize(
        "encoded",
        [
            "",
            "not-a-hash",
            "scrypt$1$2$3",
            "bcrypt$131072$8$1$c2FsdA$ZGlnZXN0",
            "scrypt$notanumber$8$1$c2FsdA$ZGlnZXN0",
        ],
    )
    def test_unverifiable_stored_hash_is_an_error_not_a_refusal(self, encoded):
        # A malformed stored hash is an operator problem to see, not a wrong
        # password to report.
        with pytest.raises(PasswordFormatError):
            verify_password(GOOD_PASSWORD, encoded)

    def test_empty_password_cannot_be_hashed(self):
        with pytest.raises(ValueError):
            hash_password("")


class TestUsernames:

    @pytest.mark.parametrize(
        "raw,expected",
        [("Steve", "steve"), ("  steve  ", "steve"), ("a_b-1", "a_b-1")],
    )
    def test_normalized(self, raw, expected):
        assert account_validation.normalize_username(raw) == expected

    @pytest.mark.parametrize("raw", ["", "   ", "has space", "hé", "x" * 33, "a/b"])
    def test_rejected(self, raw):
        with pytest.raises(account_validation.AccountError):
            account_validation.normalize_username(raw)


class TestAccounts:

    def test_create_then_read_back(self, isolated_db):
        assert accounts.create_user("newcomer", GOOD_PASSWORD) == "newcomer"
        assert accounts.get_user("newcomer") == {"id": "newcomer", "name": "newcomer"}
        assert accounts.has_users() is True
        assert "newcomer" in [u["id"] for u in accounts.list_users()]

    def test_first_account_creates_corsia_agent(self, isolated_db):
        from infra.conductor import WORKSPACE_SCOPE, get_scope_coordinator
        from infra.database import get_db

        db = get_db()
        try:
            db.execute("DELETE FROM auth_tokens")
            db.execute("DELETE FROM settings WHERE key='corsia_agent_thread_id'")
            db.execute("DELETE FROM threads")
            db.execute("DELETE FROM users")
            db.commit()
        finally:
            db.close()

        assert accounts.create_user("newcomer", GOOD_PASSWORD) == "newcomer"
        db = get_db()
        try:
            conductor_id = get_corsia_agent(db=db)
            row = db.execute(
                "SELECT user_id,title,project_id,model FROM threads WHERE id=?",
                (conductor_id,),
            ).fetchone()
            assert dict(row) == {
                "user_id": "newcomer",
                "title": "CORSIA",
                "project_id": None,
                "model": None,
            }
        finally:
            db.close()

    def test_first_account_enforces_only_the_fresh_scaffolded_shared_tree(
        self, isolated_db, tmp_path, monkeypatch,
    ):
        from core.workspace_scaffolding import scaffold_workspace
        from infra import config
        from infra.conductor import WORKSPACE_SCOPE, get_scope_coordinator
        from infra.database import get_db
        from infra.platform_paths import WorkspaceSelection
        from infra.settings import get_setting
        from registry import service
        from registry.shared_ownership_assignment import run_startup_assignment
        from registry.shared_principals import artifact_owner

        workspace = tmp_path / "workspace"
        scaffold_workspace(workspace)
        monkeypatch.setattr(config, "WORKSPACE", workspace)
        monkeypatch.setattr(
            config,
            "WORKSPACE_SELECTION",
            WorkspaceSelection(
                workspace_id="fresh-workspace", root=workspace, name="Fresh workspace",
            ),
        )

        db = get_db()
        try:
            db.execute("DELETE FROM auth_tokens")
            db.execute("DELETE FROM settings WHERE key='corsia_agent_thread_id'")
            db.execute("DELETE FROM threads")
            db.execute("DELETE FROM users")
            db.commit()

            accounts.create_user("newcomer", GOOD_PASSWORD, db=db)
            conductor = get_corsia_agent(db=db)
            shared_index = workspace / "shared" / "INDEX.md"
            artifact = service.find_file_identity(str(shared_index), db=db)

            assert get_setting("shared_ownership:workspace", db=db)
            assert artifact is not None
            assert artifact_owner(str(artifact["id"]), db=db) == conductor
            assert db.execute(
                "SELECT 1 FROM plan_nodes WHERE title=?",
                ("Claim your shared documents: keep, hand off, or archive",),
            ).fetchone() is None

            run_startup_assignment(workspace)

            assert db.execute(
                "SELECT 1 FROM plan_nodes WHERE title=?",
                ("Claim your shared documents: keep, hand off, or archive",),
            ).fetchone() is None
        finally:
            db.close()

    def test_later_account_preserves_workspace_conductor_owner(self, isolated_db):
        from infra.conductor import WORKSPACE_SCOPE, get_scope_coordinator
        from infra.database import get_db

        db = get_db()
        try:
            db.execute("DELETE FROM auth_tokens")
            db.execute("DELETE FROM settings WHERE key='corsia_agent_thread_id'")
            db.execute("DELETE FROM threads")
            db.execute("DELETE FROM users")
            db.commit()
        finally:
            db.close()

        first = accounts.create_user("first", GOOD_PASSWORD)
        conductor_id = get_corsia_agent()
        assert accounts.create_user("second", GOOD_PASSWORD) == "second"
        assert get_corsia_agent() == conductor_id
        assert first == "first"

    def test_created_name_is_normalized(self, isolated_db):
        assert accounts.create_user("  NewComer ", GOOD_PASSWORD) == "newcomer"
        assert accounts.get_user("newcomer") is not None

    def test_duplicate_is_refused(self, isolated_db):
        accounts.create_user("newcomer", GOOD_PASSWORD)
        with pytest.raises(account_validation.AccountError, match="already exists"):
            accounts.create_user("newcomer", GOOD_PASSWORD)

    def test_short_password_is_refused(self, isolated_db):
        with pytest.raises(account_validation.AccountError, match="at least"):
            accounts.create_user("newcomer", "x" * (MIN_PASSWORD_LENGTH - 1))
        assert accounts.get_user("newcomer") is None

    def test_check_password(self, isolated_db):
        accounts.create_user("newcomer", GOOD_PASSWORD)
        assert accounts.check_password("newcomer", GOOD_PASSWORD) == {
            "id": "newcomer", "name": "newcomer",
        }
        assert accounts.check_password("NEWCOMER", GOOD_PASSWORD) is not None
        assert accounts.check_password("newcomer", "wrong-but-long") is None

    def test_unknown_account_and_wrong_password_are_indistinguishable(
        self, isolated_db
    ):
        accounts.create_user("newcomer", GOOD_PASSWORD)
        assert accounts.check_password("nobody", GOOD_PASSWORD) is None
        assert accounts.check_password("newcomer", "wrong-but-long") is None
        # An invalid username must not raise where a wrong password returns.
        assert accounts.check_password("not a username", GOOD_PASSWORD) is None

    def test_unknown_account_costs_a_password_check(self, isolated_db, monkeypatch):
        # Returning early for an unknown name would let response time reveal
        # which usernames exist.
        checked = []
        real_verify = accounts.verify_password
        monkeypatch.setattr(
            accounts, "verify_password",
            lambda password, encoded: checked.append(encoded) or real_verify(password, encoded),
        )
        assert accounts.check_password("nobody", GOOD_PASSWORD) is None
        assert len(checked) == 1 and checked[0].startswith("scrypt$131072$")

    def test_data_owners_without_accounts(self, isolated_db):
        db = accounts._db(None)
        now = utc_now_iso()
        db.execute(
            "INSERT INTO threads (id, user_id, title, created_at, updated_at)"
            " VALUES ('t1', 'legacy-owner', 'Old', ?, ?)",
            (now, now),
        )
        db.commit()
        assert "legacy-owner" in accounts.data_owners_without_accounts()
        accounts.create_user("legacy-owner", GOOD_PASSWORD)
        assert "legacy-owner" not in accounts.data_owners_without_accounts()

    @pytest.mark.parametrize(
        ("name", "status"), [("someone-else", 2), ("legacy-owner", 0)]
    )
    def test_first_account_must_keep_existing_data(
        self, isolated_db, monkeypatch, name, status
    ):
        """On an install with data, the first account takes an owner's name."""
        import argparse
        import io

        import cli_accounts

        db = accounts._db(None)
        now = utc_now_iso()
        db.execute("DELETE FROM users")
        db.execute(
            "INSERT INTO threads (id, user_id, title, created_at, updated_at)"
            " VALUES ('t1', 'legacy-owner', 'Old', ?, ?)",
            (now, now),
        )
        db.commit()
        # The fixture already built the current schema without versioning it.
        monkeypatch.setattr(cli_accounts, "_ensure_schema", lambda: None)
        monkeypatch.setattr(sys, "stdin", io.StringIO(GOOD_PASSWORD + "\n"))
        args = argparse.Namespace(name=name, password_stdin=True)
        assert cli_accounts.cmd_user_add(args) == status
        assert (accounts.get_user(name) is not None) == (status == 0)


class TestTokens:

    def test_issued_token_resolves_and_is_never_stored_in_the_clear(
        self, isolated_db
    ):
        accounts.create_user("newcomer", GOOD_PASSWORD)
        minted, expires_at = accounts.issue_token("newcomer", kind="api")
        assert expires_at is None
        assert accounts.resolve_token(minted) == {
            "id": "newcomer", "name": "newcomer", "token_kind": "api",
        }
        stored = accounts._db(None).execute(
            "SELECT token_hash FROM auth_tokens WHERE user_id = 'newcomer'"
        ).fetchall()
        assert [row["token_hash"] for row in stored] == [
            accounts.token_digest(minted)
        ]
        assert minted not in str(stored)

    def test_each_issue_returns_a_different_value(self, isolated_db):
        accounts.create_user("newcomer", GOOD_PASSWORD)
        first, _ = accounts.issue_token("newcomer", kind="api")
        second, _ = accounts.issue_token("newcomer", kind="api")
        assert first != second
        assert accounts.resolve_token(first) is not None
        assert accounts.resolve_token(second) is not None

    def test_session_and_service_tokens_carry_their_ttl(self, isolated_db):
        accounts.create_user("newcomer", GOOD_PASSWORD)
        _, session_expiry = accounts.issue_token("newcomer", kind="session")
        _, service_expiry = accounts.issue_token("newcomer", kind="service")
        assert session_expiry is not None and service_expiry is not None
        assert session_expiry > service_expiry

    def test_unknown_empty_and_none_do_not_resolve(self, isolated_db):
        assert accounts.resolve_token("no-such-value") is None
        assert accounts.resolve_token("") is None
        assert accounts.resolve_token(None) is None

    def test_unknown_kind_is_refused(self, isolated_db):
        accounts.create_user("newcomer", GOOD_PASSWORD)
        with pytest.raises(account_validation.AccountError, match="unknown token kind"):
            accounts.issue_token("newcomer", kind="forever")

    def test_token_for_missing_account_is_refused(self, isolated_db):
        with pytest.raises(account_validation.AccountError, match="no account named"):
            accounts.issue_token("nobody")

    def test_expired_token_does_not_resolve(self, isolated_db):
        accounts.create_user("newcomer", GOOD_PASSWORD)
        minted, _ = accounts.issue_token("newcomer", kind="session")
        past = (utc_now() - timedelta(seconds=1)).isoformat()
        db = accounts._db(None)
        db.execute("UPDATE auth_tokens SET expires_at = ?", (past,))
        db.commit()
        assert accounts.resolve_token(minted) is None

    def test_issuing_purges_expired_rows(self, isolated_db):
        accounts.create_user("newcomer", GOOD_PASSWORD)
        stale, _ = accounts.issue_token("newcomer", kind="session")
        db = accounts._db(None)
        db.execute(
            "UPDATE auth_tokens SET expires_at = ?",
            ((utc_now() - timedelta(days=1)).isoformat(),),
        )
        db.commit()
        accounts.issue_token("newcomer", kind="api")
        remaining = token_admin.list_tokens("newcomer")
        assert len(remaining) == 1
        assert remaining[0]["kind"] == "api"
        assert accounts.resolve_token(stale) is None

    def test_revoke_one_token(self, isolated_db):
        accounts.create_user("newcomer", GOOD_PASSWORD)
        minted, _ = accounts.issue_token("newcomer", kind="api")
        assert token_admin.revoke_token(minted) is True
        assert accounts.resolve_token(minted) is None
        assert token_admin.revoke_token(minted) is False

    def test_revoke_by_label_leaves_other_labels(self, isolated_db):
        accounts.create_user("newcomer", GOOD_PASSWORD)
        doomed, _ = accounts.issue_token("newcomer", kind="api", label="old")
        kept, _ = accounts.issue_token("newcomer", kind="api", label="keep")
        assert token_admin.revoke_label("newcomer", "old") == 1
        assert accounts.resolve_token(doomed) is None
        assert accounts.resolve_token(kept) is not None

    def test_list_tokens_never_returns_a_value(self, isolated_db):
        accounts.create_user("newcomer", GOOD_PASSWORD)
        minted, _ = accounts.issue_token("newcomer", kind="api", label="cli")
        listed = token_admin.list_tokens("newcomer")
        assert [row["label"] for row in listed] == ["cli"]
        assert minted not in str(listed)
        assert not any("hash" in key for row in listed for key in row)

    def test_password_change_revokes_sessions_but_keeps_api_tokens(
        self, isolated_db
    ):
        accounts.create_user("newcomer", GOOD_PASSWORD)
        browser, _ = accounts.issue_token("newcomer", kind="session")
        tooling, _ = accounts.issue_token("newcomer", kind="api")
        assert accounts.set_password("newcomer", GOOD_PASSWORD + "-new") == 1
        assert accounts.resolve_token(browser) is None
        assert accounts.resolve_token(tooling) is not None
        assert accounts.check_password("newcomer", GOOD_PASSWORD) is None
        assert accounts.check_password("newcomer", GOOD_PASSWORD + "-new")

    def test_password_change_for_missing_account_is_refused(self, isolated_db):
        with pytest.raises(account_validation.AccountError, match="no account named"):
            accounts.set_password("nobody", GOOD_PASSWORD)


class TestAccountsMigration:

    def _at_25(self, path) -> sqlite3.Connection:
        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA user_version = 25")
        conn.execute(
            "CREATE TABLE threads (id TEXT PRIMARY KEY, user_id TEXT)"
        )
        conn.execute("INSERT INTO threads VALUES ('t1', 'legacy-owner')")
        conn.commit()
        return conn

    def test_forward_step_adds_the_tables_and_keeps_existing_rows(self, tmp_path):
        from infra.migration_25_to_26_accounts import migrate_25_to_26

        conn = self._at_25(tmp_path / "old.db")
        migrate_25_to_26(conn)
        conn.commit()

        tables = {
            row["name"] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert {"users", "auth_tokens"} <= tables
        assert conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] == 0
        assert conn.execute(
            "SELECT user_id FROM threads WHERE id = 't1'"
        ).fetchone()["user_id"] == "legacy-owner"
        conn.close()

    def test_forward_step_is_idempotent(self, tmp_path):
        from infra.migration_25_to_26_accounts import migrate_25_to_26

        conn = self._at_25(tmp_path / "old.db")
        migrate_25_to_26(conn)
        migrate_25_to_26(conn)
        conn.commit()
        conn.close()

    def test_registered_in_the_ladder_at_the_current_version(self):
        from infra.database_migrations import MIGRATIONS
        from infra.migration_25_to_26_accounts import migrate_25_to_26
        from infra.schema import SCHEMA_VERSION

        assert SCHEMA_VERSION == 143
        assert MIGRATIONS[25] is migrate_25_to_26

    def test_migrated_shape_matches_a_freshly_created_one(self, tmp_path):
        from infra.migration_25_to_26_accounts import migrate_25_to_26

        migrated = self._at_25(tmp_path / "old.db")
        migrate_25_to_26(migrated)
        migrated.commit()

        fresh = sqlite3.connect(str(tmp_path / "new.db"))
        fresh.row_factory = sqlite3.Row
        fresh.executescript(ACCOUNTS_SCHEMA_SQL)
        fresh.commit()

        def shape(conn: sqlite3.Connection) -> set:
            return {
                (row["type"], row["name"], " ".join((row["sql"] or "").split()))
                for row in conn.execute(
                    "SELECT type, name, sql FROM sqlite_master"
                    " WHERE name IN ('users', 'auth_tokens')"
                    "    OR name LIKE 'idx_auth_tokens%'"
                )
            }

        assert shape(migrated) == shape(fresh)
        migrated.close()
        fresh.close()
