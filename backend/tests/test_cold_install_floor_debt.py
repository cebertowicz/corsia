"""Cold install debt checks distinguish the already-installed history floor."""
from __future__ import annotations

import json

import pytest

from test_external_cold_install_race import _codex_events, _event
from test_external_cold_tool_debt import _context, debt_db  # noqa: F401


def _request(*, checkpoint_id, through_seq, previous_generation_id=None):
    from core.checkpoint_types import CheckpointRequest

    return CheckpointRequest(
        id=checkpoint_id,
        thread_id="debt",
        history_epoch=0,
        previous_generation_id=previous_generation_id,
        through_seq=through_seq,
        through_message_id=("older-result" if through_seq == 3 else f"candidate-{through_seq}"),
        trigger="pressure",
        mode="cold",
        source_harness="codex",
        source_model="codex/test",
        writer_harness="codex",
        writer_model="codex/test",
        originating_turn_id="current",
        first_included_seq=1 if previous_generation_id is None else 4,
        started_at="t",
    )


def _capture_and_install(db, request):
    from infra.checkpoints import capture, create_running, install

    record = create_running(request, db=db)
    capture(record.id, raw_result="saved", usage_json=None, captured_at="t", db=db)
    return install(record.id, installed_at="t", db=db)


def _append_candidate(db):
    seq = db.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 FROM messages WHERE thread_id='debt'"
    ).fetchone()[0]
    db.execute(
        "INSERT INTO messages (id,thread_id,role,content,metadata,created_at,seq) "
        "VALUES (?,'debt','assistant','next',?,'t',?)",
        (f"candidate-{seq}", json.dumps({"turn_id": "current"}), seq),
    )
    db.execute("UPDATE threads SET next_message_seq=? WHERE id='debt'", (seq + 1,))
    db.commit()
    return seq


def _bind_provider_turn(db, step_id):
    row = db.execute("SELECT metadata FROM messages WHERE id=?", (step_id,)).fetchone()
    metadata = json.loads(row["metadata"])
    metadata["provider_turn_id"] = "provider-turn"
    db.execute("UPDATE messages SET metadata=? WHERE id=?", (json.dumps(metadata), step_id))
    db.commit()


async def _previous_floor(db, monkeypatch):
    _context(db, monkeypatch, "codex", outstanding=False)
    previous = _capture_and_install(db, _request(checkpoint_id="previous", through_seq=3))
    assert previous.status == "installed"
    return previous, _append_candidate(db)


@pytest.mark.asyncio
async def test_prior_floor_excludes_unpaid_legacy_debt_from_clean_candidate(debt_db, monkeypatch):
    previous, candidate_seq = await _previous_floor(debt_db, monkeypatch)
    live, on_event = _codex_events(debt_db)

    await on_event(_event("tool_use", "legacy"))
    assert live.tool_step_ids == {"legacy": "older-step"}

    candidate = _capture_and_install(
        debt_db,
        _request(
            checkpoint_id="candidate", through_seq=candidate_seq,
            previous_generation_id=previous.id,
        ),
    )
    assert candidate.status == "installed"


@pytest.mark.asyncio
async def test_prior_floor_refuses_unpaid_debt_declared_in_candidate_range(debt_db, monkeypatch):
    previous, candidate_seq = await _previous_floor(debt_db, monkeypatch)
    _bind_provider_turn(debt_db, "later-step")
    live, on_event = _codex_events(debt_db, open_step="later-step")

    await on_event(_event("tool_use", "in-range"))
    assert live.tool_step_ids == {"in-range": "later-step"}

    candidate = _capture_and_install(
        debt_db,
        _request(
            checkpoint_id="candidate", through_seq=candidate_seq,
            previous_generation_id=previous.id,
        ),
    )
    assert candidate.status == "overtaken"
    assert "tool" in candidate.error


@pytest.mark.asyncio
async def test_first_cold_install_still_refuses_unpaid_debt(debt_db, monkeypatch):
    _context(debt_db, monkeypatch, "codex", outstanding=False)
    candidate_seq = _append_candidate(debt_db)
    live, on_event = _codex_events(debt_db)

    await on_event(_event("tool_use", "legacy"))
    assert live.tool_step_ids == {"legacy": "older-step"}

    candidate = _capture_and_install(
        debt_db, _request(checkpoint_id="candidate", through_seq=candidate_seq),
    )
    assert candidate.status == "overtaken"
    assert "tool" in candidate.error
