"""Decision-answer delivery proofs at post-commit and harness boundaries."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock

import pytest

from core.turn_core import TurnIdentity
from tests.test_plan_decision_answer import (
    _client,
    _raised_decision,
    _seed,
    db,
)


def _answer_rows(connection, decision_id):
    return connection.execute(
        "SELECT * FROM messages WHERE id=?",
        (f"plan-decision-answer:{decision_id}",),
    ).fetchall()


def _fail_broadcast(*_args, **_kwargs):
    raise RuntimeError("injected thread broadcast failure")


class _HarnessProbe:
    """Capture the exact preflight and adapter inputs for one execution class."""

    def __init__(self, execution_class: str, adapter_name: str):
        self.execution_class = execution_class
        self.adapter_name = adapter_name
        self.preflight_inputs: list[str] = []
        self.adapter_inputs: list[str] = []

    async def read_truth(self, *_args, **_kwargs):
        return {
            "addressable": True,
            "execution_class": self.execution_class,
            "codex_has_thread": False,
        }

    async def preflight(self, **kwargs):
        from shared_send_fixtures import prepared_context_with_fit_ledger

        self.preflight_inputs.append(kwargs["current_user_content"])
        return {"status": "allowed", "prepared_context": prepared_context_with_fit_ledger(kwargs["target"])}

    async def adapter(self, *args, **kwargs):
        content_index = 1 if self.adapter_name == "dispatch_cc_sub" else 2
        self.adapter_inputs.append(args[content_index])
        await kwargs["settle_delivery"]()
        identity = kwargs["preadmitted_turn"]
        assert isinstance(identity, TurnIdentity)
        if self.adapter_name == "dispatch_native_event":
            return "native.test", None, identity
        return f"{self.adapter_name}.test", identity


@pytest.mark.parametrize(
    ("model", "execution_class", "adapter_name"),
    [
        ("anthropic/claude-3-5-sonnet", "native", "dispatch_native_event"),
        ("claude-code/sonnet", "cc_sub", "dispatch_cc_sub"),
        ("codex/gpt-5-codex", "codex_sub", "dispatch_codex"),
    ],
)
def test_http_answer_reaches_each_harness_adapter(
    isolated_db, monkeypatch, model, execution_class, adapter_name,
):
    from infra import (
        runtime_truth,
        send_broadcast,
        send_dispatch,
        send_effects,
        send_preflight,
    )
    from infra.database import get_db

    connection = get_db()
    try:
        _seed(connection)
        connection.commit()
        _plan, _source, decision = _raised_decision(connection)
        connection.execute("UPDATE threads SET model=? WHERE id='owner'", (model,))
        connection.commit()
        probe = _HarnessProbe(execution_class, adapter_name)

        monkeypatch.setattr(runtime_truth, "read_runtime_truth", probe.read_truth)
        monkeypatch.setattr(send_preflight, "context_preflight", probe.preflight)
        monkeypatch.setattr(send_dispatch, adapter_name, probe.adapter)
        monkeypatch.setattr(send_broadcast, "broadcast_message_rows", AsyncMock())
        monkeypatch.setattr(send_effects, "run_batch_accepted_effects", AsyncMock())

        response = _client(connection, monkeypatch).post(
            f"/api/plans/decisions/{decision['id']}/answer",
            json={
                "option_id": "A",
                "expected_revision": decision["revision"],
                "mutation_id": f"decision-delivery-adapter-{execution_class}",
            },
        )

        expected = (
            "[PLAN DECISION ANSWERED]\n"
            'Decision: "Where should decision state live?"\n'
            f"Decision ID: {decision['id']}\n"
            'Answer: "A — Ship the twin"'
        )
        assert response.status_code == 200
        assert len(probe.preflight_inputs) == len(probe.adapter_inputs) == 1
        assert probe.preflight_inputs == probe.adapter_inputs
        assert probe.preflight_inputs[0].startswith(
            "CORSIA is delivering 2 queued inbound inputs together."
        )
        assert probe.preflight_inputs[0].endswith(f"[2/2]\n{expected}")
    finally:
        connection.close()


def test_post_commit_transcript_broadcast_failure_keeps_success_and_redrives(
    db, monkeypatch, caplog,
):
    from infra import state, thread_execution
    from plans import decisions

    _plan, _source, decision = _raised_decision(db)
    drive = AsyncMock()
    monkeypatch.setattr(thread_execution, "drive_thread", drive)
    monkeypatch.setattr(state, "broadcast_to_thread", _fail_broadcast)
    with caplog.at_level(logging.WARNING, logger="plans.notifications"):
        response = _client(db, monkeypatch).post(
            f"/api/plans/decisions/{decision['id']}/answer",
            json={
                "option_id": "A",
                "expected_revision": decision["revision"],
                "mutation_id": "decision-delivery-failed-transcript-broadcast",
            },
        )

    assert decisions.read(decision["id"], db=db)["state"] == "answered"
    assert len(_answer_rows(db, decision["id"])) == 1
    assert response.status_code == 200
    drive.assert_awaited_once_with("owner")
    assert "post-commit plan transcript delivery failed" in caplog.text.lower()
