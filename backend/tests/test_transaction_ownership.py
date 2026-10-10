"""Transaction ownership on the shared pooled connection (#948).

``get_db()`` hands every caller on a thread the same connection, so
``connection.in_transaction`` answers "is a transaction open?" but never "is it
mine?". These tests pin the declared-ownership contract that replaces the old
guess, and the nesting the canonical writers depend on.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from delegation_fixtures import deleg_db  # noqa: F401 - fixture import


@pytest.fixture
def scratch(isolated_db):
    """A pooled connection with one disposable table, reset between tests."""
    from infra.database import get_db

    connection = get_db()
    connection.execute("CREATE TABLE scratch (v TEXT)")
    connection.commit()
    return connection


def _rows():
    from infra.database import get_db

    return [row[0] for row in get_db().execute("SELECT v FROM scratch ORDER BY rowid")]


class TestOutermostOwnerClaim:
    """``db=None`` claims the outermost boundary and must be able to keep it."""

    def test_refuses_a_connection_another_frame_already_owns(self, scratch):
        """#948: the claim used to be silently downgraded to a savepoint, and the
        owning frame's work was then destroyed by this helper's close()."""
        from infra.database import TransactionOwnershipError, write_transaction

        scratch.execute("BEGIN IMMEDIATE")
        scratch.execute("INSERT INTO scratch (v) VALUES ('owner-work')")

        with pytest.raises(TransactionOwnershipError):
            with write_transaction(None, scope="probe"):
                pytest.fail("the false ownership claim must not reach the body")

        # The owner still holds its transaction and can still commit it.
        assert scratch.in_transaction is True
        scratch.commit()
        assert _rows() == ["owner-work"]

    def test_error_names_the_scope_and_the_repair(self, scratch):
        from infra.database import TransactionOwnershipError, write_transaction

        scratch.execute("BEGIN IMMEDIATE")
        try:
            with pytest.raises(TransactionOwnershipError) as caught:
                with write_transaction(None, scope="thread_inbox"):
                    pass
            assert "thread_inbox" in str(caught.value)
            assert "db=" in str(caught.value)
        finally:
            scratch.rollback()

    def test_a_scope_that_is_not_an_identifier_is_rejected(self, scratch):
        """The scope reaches SQL, so it may only ever be a bare identifier."""
        from infra.database import write_transaction

        with pytest.raises(ValueError):
            with write_transaction(None, scope="probe; DROP TABLE scratch"):
                pass
        assert scratch.execute("SELECT count(*) FROM scratch").fetchone()[0] == 0

    def test_commits_and_releases_a_clean_connection(self, scratch):
        from infra.database import write_transaction

        with write_transaction(None, scope="probe") as connection:
            connection.execute("INSERT INTO scratch (v) VALUES ('owned')")
        assert _rows() == ["owned"]
        assert scratch.in_transaction is False

    def test_rolls_back_its_own_transaction_on_failure(self, scratch):
        from infra.database import write_transaction

        with pytest.raises(ValueError):
            with write_transaction(None, scope="probe") as connection:
                connection.execute("INSERT INTO scratch (v) VALUES ('doomed')")
                raise ValueError("boom")
        assert _rows() == []
        assert scratch.in_transaction is False


class TestNamedOwnerNesting:
    """``db=<connection>`` names the owner, so a savepoint is valid by construction."""

    def test_nests_without_committing_the_owner(self, scratch):
        from infra.database import write_transaction

        scratch.execute("BEGIN IMMEDIATE")
        scratch.execute("INSERT INTO scratch (v) VALUES ('owner')")
        with write_transaction(scratch, scope="inner") as connection:
            connection.execute("INSERT INTO scratch (v) VALUES ('nested')")

        # Nothing was committed or closed under the owner.
        assert scratch.in_transaction is True
        scratch.commit()
        assert _rows() == ["owner", "nested"]

    def test_sequential_savepoints_still_work(self, scratch):
        """The canonical message writer opens one savepoint per persisted item."""
        from infra.database import write_transaction

        scratch.execute("BEGIN IMMEDIATE")
        for index in range(13):
            with write_transaction(scratch, scope="inner") as connection:
                connection.execute(
                    "INSERT INTO scratch (v) VALUES (?)", (f"item-{index}",)
                )
        scratch.commit()
        assert _rows() == [f"item-{index}" for index in range(13)]

    def test_inner_failure_rolls_back_only_the_inner_work(self, scratch):
        from infra.database import write_transaction

        scratch.execute("BEGIN IMMEDIATE")
        scratch.execute("INSERT INTO scratch (v) VALUES ('kept')")
        with pytest.raises(ValueError):
            with write_transaction(scratch, scope="inner") as connection:
                connection.execute("INSERT INTO scratch (v) VALUES ('discarded')")
                raise ValueError("boom")
        scratch.commit()
        assert _rows() == ["kept"]

    def test_a_clean_named_connection_gets_its_own_committed_transaction(self, scratch):
        from infra.database import write_transaction

        with write_transaction(scratch, scope="probe") as connection:
            connection.execute("INSERT INTO scratch (v) VALUES ('delegated')")
        assert _rows() == ["delegated"]
        assert scratch.in_transaction is False


class TestProductionWriterOwnership:
    """A production outermost writer must reject a caller-owned pooled transaction."""

    def test_restart_webhook_write_refuses_an_unowned_pooled_transaction(self, scratch):
        from automation.restart_webhooks import retry_dead_lettered_webhook
        from infra.database import TransactionOwnershipError

        scratch.execute("BEGIN IMMEDIATE")
        scratch.execute("INSERT INTO scratch (v) VALUES ('caller-work')")

        with pytest.raises(TransactionOwnershipError):
            retry_dead_lettered_webhook("missing-delivery")

        scratch.commit()
        assert _rows() == ["caller-work"]


class TestDbJobBoundary:
    """Executor jobs must start and finish without inheriting a pooled write."""

    def test_rejects_an_inherited_open_transaction(self, scratch):
        from infra.database import TransactionOwnershipError, db_job_boundary

        scratch.execute("BEGIN IMMEDIATE")
        scratch.execute("INSERT INTO scratch (v) VALUES ('prior-job-work')")

        with pytest.raises(TransactionOwnershipError, match="inherited"):
            with db_job_boundary("job_probe"):
                pytest.fail("a dirty job must not reach its body")

        assert scratch.in_transaction is False
        assert _rows() == []

    def test_rejects_and_rolls_back_a_body_that_returns_dirty(self, scratch):
        from infra.database import TransactionOwnershipError, db_job_boundary

        with pytest.raises(TransactionOwnershipError, match="returned with"):
            with db_job_boundary("job_probe"):
                scratch.execute("INSERT INTO scratch (v) VALUES ('uncommitted')")

        assert scratch.in_transaction is False
        assert _rows() == []


class TestDelegatedSendFixtureIsolation:
    """The delegated-send fixtures share one connection, so DB work must not fan out.

    Two threads owning transactions on one connection destroy each other's
    savepoints; that is what produced ``no such savepoint`` in CI.
    """

    def test_run_in_db_stays_on_the_calling_thread(self, deleg_db):  # noqa: F811
        from infra.db_async import run_in_db

        async def _check():
            return await run_in_db(lambda: threading.current_thread().name)

        assert asyncio.run(_check()) == threading.current_thread().name
