"""The E2E backend returns to the seed captured before the first spec."""

import sqlite3
from contextlib import closing

import pytest

import e2e_reset


@pytest.fixture
def roots(tmp_path, monkeypatch):
    state, workspace = tmp_path / "state", tmp_path / "workspace"
    state.mkdir()
    workspace.mkdir()
    monkeypatch.setenv("CORSIA_STATE_DIR", str(state))
    monkeypatch.setenv("CORSIA_WORKSPACE", str(workspace))
    with closing(sqlite3.connect(state / "corsia.db")) as db:
        db.execute("CREATE TABLE threads (id TEXT)")
        db.execute("INSERT INTO threads VALUES ('seed-thread')")
        db.commit()
    (workspace / "seed.md").write_text("seed")
    return state, workspace


def _thread_ids(state) -> list[str]:
    with closing(sqlite3.connect(state / "corsia.db")) as db:
        return [row[0] for row in db.execute("SELECT id FROM threads ORDER BY id")]


def test_restore_puts_back_database_state_root_and_workspace(roots, monkeypatch):
    state, workspace = roots
    e2e_reset.capture_seed()

    with closing(sqlite3.connect(state / "corsia.db")) as db:
        db.execute("INSERT INTO threads VALUES ('left-by-a-spec')")
        db.commit()
    (state / "uploads").mkdir()
    (state / "uploads" / "leftover.bin").write_text("leftover")
    (workspace / "seed.md").write_text("edited")
    (workspace / "created-by-a-spec.md").write_text("new")

    monkeypatch.setenv(e2e_reset.RESTORE_ENV, "1")
    e2e_reset.restore_if_requested()

    assert _thread_ids(state) == ["seed-thread"]
    assert not (state / "uploads").exists()
    assert (workspace / "seed.md").read_text() == "seed"
    assert not (workspace / "created-by-a-spec.md").exists()
    assert (state / "backups" / "e2e-seed").is_dir(), "the seed survives its own restore, so the next reset has it"


def test_a_process_not_asked_to_restore_keeps_its_state(roots):
    state, _ = roots
    e2e_reset.capture_seed()
    with closing(sqlite3.connect(state / "corsia.db")) as db:
        db.execute("INSERT INTO threads VALUES ('kept')")
        db.commit()

    e2e_reset.restore_if_requested()

    assert _thread_ids(state) == ["kept", "seed-thread"]


def test_restore_without_a_captured_seed_fails_loudly(roots, monkeypatch):
    monkeypatch.setenv(e2e_reset.RESTORE_ENV, "1")
    with pytest.raises(RuntimeError, match="no captured seed"):
        e2e_reset.restore_if_requested()


def test_restart_without_a_captured_seed_is_refused(roots):
    with pytest.raises(RuntimeError, match="captured seed"):
        e2e_reset.restart_from_seed()
