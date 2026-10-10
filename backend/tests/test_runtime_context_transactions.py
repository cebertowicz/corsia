"""Runtime model observations respect the declared transaction owner."""

from contextlib import closing
import json
import sqlite3
from types import SimpleNamespace

import pytest

from connections import harness_models
from infra import database


@pytest.fixture(params=["codex", "claude-code"])
def observed_model(isolated_db, request):
    provider = request.param
    metadata = {
        "resolved_identity": "reported-model", "harness_version": "test-version",
        "context_observation": {
            "source": "discovery", "raw_tokens": 200000,
            "max_tokens": 400000, "effective_percent": 95,
        },
        "preserved": "other metadata",
    }
    with closing(sqlite3.connect(str(isolated_db))) as db:
        db.execute(
            "INSERT INTO models (id,harness,provider,provider_model_id,litellm_model,"
            "display_name,context_window,metadata_json,created_at,updated_at) "
            "VALUES ('model',?,?,'selector','test/model','Test model',190000,?,'t','t')",
            (provider, provider, json.dumps(metadata)),
        )
        db.execute("INSERT INTO settings (key,value,updated_at) VALUES ('outer','original','t')")
        db.commit()
    return isolated_db, provider, metadata


def _observe(provider, *, db=None, **overrides):
    kwargs = {
        "selector": "selector", "reported_identity": "reported-model",
        "effective_tokens": 180000, "source": "runtime",
        "observed_at": "2026-09-13T00:00:00+00:00",
    }
    kwargs.update(overrides)
    return harness_models.record_runtime_context_observation(provider, db=db, **kwargs)


def _stored(path):
    with closing(sqlite3.connect(str(path))) as db:
        window, metadata = db.execute(
            "SELECT context_window,metadata_json FROM models WHERE id='model'"
        ).fetchone()
        outer = db.execute("SELECT value FROM settings WHERE key='outer'").fetchone()[0]
    return window, json.loads(metadata), outer


def test_implicit_observation_cannot_commit_or_discard_foreign_work(observed_model):
    path, provider, metadata = observed_model
    db = database.get_db()
    db.execute("UPDATE settings SET value='caller' WHERE key='outer'")
    try:
        with pytest.raises(database.TransactionOwnershipError):
            _observe(provider)
        assert db.in_transaction
        assert _stored(path) == (190000, metadata, "original")
        db.commit()
        assert _stored(path) == (190000, metadata, "caller")
    finally:
        db.rollback()
        db.close()


def test_explicit_clean_observation_is_durable_without_closing_caller(observed_model):
    path, provider, _ = observed_model
    with closing(sqlite3.connect(str(path))) as db:
        db.row_factory = sqlite3.Row
        assert _observe(provider, db=db) == 180000
        assert not db.in_transaction
        assert db.execute("SELECT 1").fetchone()[0] == 1
        window, metadata, outer = _stored(path)
        assert window == 180000
        assert outer == "original"
        assert metadata["preserved"] == "other metadata"
        observation = metadata["context_observation"]
        assert observation["effective_tokens"] == 180000
        assert observation["source"] == "runtime"
        assert observation["raw_tokens"] == (200000 if provider == "codex" else 180000)
        assert observation["effective_percent"] == (95 if provider == "codex" else 100)


def test_outer_rollback_owns_successful_observation(observed_model):
    path, provider, metadata = observed_model
    with pytest.raises(ValueError, match="abort caller"):
        with database.write_transaction(None, scope="observation_test") as db:
            assert _observe(provider, db=db) == 180000
            raise ValueError("abort caller")
    assert _stored(path) == (190000, metadata, "original")


def test_partial_observation_failure_does_not_leak_into_caller_commit(observed_model):
    path, provider, metadata = observed_model
    with closing(sqlite3.connect(str(path))) as db:
        db.execute(
            "CREATE TRIGGER reject_observation AFTER UPDATE ON models "
            "BEGIN SELECT RAISE(FAIL, 'observation rejected'); END"
        )
        db.commit()
    with database.write_transaction(None, scope="observation_test") as db:
        db.execute("UPDATE settings SET value='caller' WHERE key='outer'")
        with pytest.raises(sqlite3.IntegrityError, match="observation rejected"):
            _observe(provider, db=db)
        assert db.in_transaction
    assert _stored(path) == (190000, metadata, "caller")


@pytest.mark.parametrize("explicit", [False, True])
def test_observation_commit_failure_releases_writer(observed_model, monkeypatch, explicit):
    path, provider, metadata = observed_model

    class FailedCommit(sqlite3.Connection):
        closed = False

        def commit(self):
            raise sqlite3.OperationalError("observation commit failed")

        def close(self):
            self.closed = True
            super().close()

    db = sqlite3.connect(str(path), factory=FailedCommit)
    db.row_factory = sqlite3.Row
    monkeypatch.setattr(database, "get_db", lambda: db)
    monkeypatch.setattr(harness_models, "get_db", lambda: db)
    try:
        with pytest.raises(sqlite3.OperationalError, match="observation commit failed"):
            _observe(provider, db=db if explicit else None)
        assert db.closed == (not explicit)
        if explicit:
            assert not db.in_transaction
            assert db.execute("SELECT 1").fetchone()[0] == 1
        assert _stored(path) == (190000, metadata, "original")
        with closing(sqlite3.connect(str(path), timeout=0.05)) as writer:
            writer.execute("BEGIN IMMEDIATE")
            writer.rollback()
    finally:
        db.close()


@pytest.mark.parametrize("overrides", [
    {"selector": "missing"}, {"reported_identity": "different"},
    {"effective_tokens": 0}, {"selector": ""},
])
def test_invalid_or_unmatched_observation_preserves_outer_work(observed_model, overrides):
    path, provider, metadata = observed_model
    with database.write_transaction(None, scope="observation_test") as db:
        db.execute("UPDATE settings SET value='caller' WHERE key='outer'")
        assert _observe(provider, db=db, **overrides) is None
        assert db.in_transaction
    assert _stored(path) == (190000, metadata, "caller")


@pytest.mark.parametrize("observed_model", ["codex"], indirect=True)
def test_terminal_observation_failure_is_logged_without_settling_foreign_work(
    observed_model, caplog,
):
    path, _provider, metadata = observed_model
    from subagents.codex_runtime_terminal import _observe_terminal_context

    db = database.get_db()
    db.execute("UPDATE settings SET value='caller' WHERE key='outer'")
    try:
        _observe_terminal_context(
            "synthetic-thread", SimpleNamespace(model="selector", provider_reported_model="reported-model"),
            {"provider_reported_model_context_window": 180000}, source="terminal",
        )
        assert "runtime context observation failed" in caplog.text
        assert db.in_transaction
        assert _stored(path) == (190000, metadata, "original")
        db.commit()
        assert _stored(path) == (190000, metadata, "caller")
    finally:
        db.rollback()
        db.close()


@pytest.mark.parametrize("observed_model", ["codex"], indirect=True)
def test_terminal_observation_persists_measured_effective_window(observed_model, caplog):
    path, _provider, _metadata = observed_model
    from subagents.codex_runtime_terminal import _observe_terminal_context

    _observe_terminal_context(
        "synthetic-thread", SimpleNamespace(model="selector", provider_reported_model="reported-model"),
        {"provider_reported_model_context_window": 180000}, source="terminal",
    )
    window, metadata, outer = _stored(path)
    assert (window, outer) == (180000, "original")
    assert metadata["context_observation"]["effective_tokens"] == 180000
    assert metadata["context_observation"]["source"] == "terminal"
    assert "runtime context observation failed" not in caplog.text
