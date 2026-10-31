"""Known impossible checkpoint caps cannot acquire a new FIFO input."""

from __future__ import annotations

import sqlite3

import pytest

from infra.input_admission import accept_thread_input
from infra.send_types import SendRejected


@pytest.fixture
def capacity_db():
    from infra.schema import SCHEMA_SQL

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA_SQL)
    db.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
        "VALUES ('thread-1','user-1','source','t0','t0')"
    )
    for model in ("source", "pending"):
        db.execute(
            "INSERT INTO models (id,provider,provider_model_id,litellm_model,"
            "display_name,context_window,created_at,updated_at) "
            "VALUES (?,'test',?,?,?,262144,'t0','t0')",
            (model, model, model, model),
        )
    db.commit()
    yield db
    db.close()


def _accept(db, message_id="input-1"):
    return accept_thread_input(
        "thread-1", role="user", content="keep this input", intent="apply_now",
        message_id=message_id, db=db,
    )


@pytest.mark.parametrize("harness", ["native", "claude-code", "codex"])
@pytest.mark.parametrize("pending", [False, True])
def test_impossible_profile_cap_refuses_before_fifo_mutation(capacity_db, harness, pending, isolated_db):
    db = capacity_db
    destination = "pending" if pending else "source"
    db.execute("UPDATE models SET harness=? WHERE id=?", (harness, destination))
    db.execute(
        "INSERT INTO model_profiles (model_id,context_window) VALUES (?,16384)",
        (destination,),
    )
    if pending:
        db.execute(
            "UPDATE threads SET pending_model='pending',pending_execution_epoch=1,"
            "switch_after_input_order=0 WHERE id='thread-1'"
        )
    db.commit()
    if harness != "native":
        # A model with no signed-in account holds its input before its cap is read.
        from connections.models import upsert_connection
        from vendor_account_fixtures import signed_in_default

        upsert_connection(type="cli", provider=harness, status="available",
                          config={"binary": "/bin/synthetic"}, db=db)
        signed_in_default(harness, db=db, models=("source", "pending"))
    before = db.execute("SELECT next_input_order FROM threads").fetchone()[0]

    with pytest.raises(SendRejected) as rejected:
        _accept(db)

    assert rejected.value.metadata["cause"] == "checkpoint_configuration_impossible"
    assert db.execute("SELECT count(*) FROM messages").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM active_turns").fetchone()[0] == 0
    assert db.execute("SELECT next_input_order FROM threads").fetchone()[0] == before


def test_duplicate_receipt_survives_later_impossible_cap(capacity_db):
    first = _accept(capacity_db)
    capacity_db.execute("UPDATE models SET context_window=16384 WHERE id='source'")
    capacity_db.commit()

    duplicate = _accept(capacity_db)

    assert duplicate.duplicate
    assert duplicate.message_id == first.message_id
    assert duplicate.input_order == first.input_order
    assert capacity_db.execute("SELECT count(*) FROM messages").fetchone()[0] == 1


@pytest.mark.parametrize("harness", ["native", "claude-code", "codex"])
def test_viable_131k_cap_does_not_require_an_online_provider(capacity_db, harness):
    capacity_db.execute(
        "UPDATE models SET harness=?,available=0,context_window=131072 WHERE id='source'",
        (harness,),
    )
    capacity_db.commit()

    accepted = _accept(capacity_db)

    assert not accepted.duplicate
    assert accepted.target_model == "source"
    assert capacity_db.execute("SELECT delivery_state FROM messages").fetchone()[0] == "queued"
