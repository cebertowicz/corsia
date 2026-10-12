"""Child provider-refusal notices after real terminal rollover settlement."""

from __future__ import annotations

import pytest

from automation import plan_liveness_terminal
from automation.turn_end_effects import PendingTurnEndEffects
from connections import harness_usage
from connections.account_evidence import mark_discovery
from connections.account_policy import select_account
from connections.account_receipts import capture_receipt
from connections.models import upsert_connection
from connections.vendor_accounts import add_account, default_account, set_account_state
from infra.input_admission import accept_thread_input
from infra.thread_driver import finish_turn
from infra.thread_inputs import start_idle_turn
from tests.child_parent_notice_helpers import db, delegated, wakes
from vendor_account_fixtures import set_legacy_policy

_REFUSED = "You've hit your session limit."


def _complete(db, turn_id):
    return plan_liveness_terminal.on_thread_turn_complete(
        "worker",
        outcome="error",
        error=_REFUSED,
        turn_id=turn_id,
        db=db,
        effects=PendingTurnEndEffects(),
    )


def _rollover_accounts(db, *, enabled=True, destination_ready=True):
    upsert_connection(
        type="cli", provider="codex", status="available", config={"binary": "/bin/test"}, db=db,
    )
    first = default_account("codex", db=db)
    second = set_account_state(
        add_account("codex", "Second", db=db)["id"], "signed_in", expected_revision=0, db=db,
    )
    db.execute(
        """INSERT INTO models
           (id,display_name,litellm_model,provider,provider_model_id,harness,created_at,updated_at)
           VALUES ('codex/test','Test','codex/test','codex','test','codex','t','t')""",
    )
    db.execute("UPDATE threads SET model='codex/test' WHERE id='worker'")
    db.execute("UPDATE models SET context_window=262144 WHERE id='codex/test'")
    for account in (first, second):
        receipt = capture_receipt("codex", account_id=account["id"], db=db)
        assert receipt is not None
        assert mark_discovery(
            account["id"], receipt=receipt, status="ready",
            models={"codex/test": {"available": True, "resolved_identity": "synthetic"}}, db=db,
        )
    db.commit()
    for account in (first, second):
        if account["id"] == first["id"] or destination_ready:
            harness_usage.record_allowed(
                provider="codex", receipt=capture_receipt("codex", account_id=account["id"], db=db),
                source="test", db=db,
            )
    set_legacy_policy(
        "project-1", "codex", allowlist=[first["id"], second["id"]],
        default_account_id=first["id"], switch_on_limit=enabled, db=db,
    )
    assert select_account("worker", "codex", db=db) == first["id"]
    return first, second


def _finish_refused_turn(db):
    accept_thread_input(
        "worker", role="user", content="Continue", intent="apply_now", message_id="brief", db=db,
    )
    started = start_idle_turn(
        "worker", turn_id="failed-turn", boot_epoch="test", started_at="2026-10-11T16:00:00+00:00", db=db,
    )
    assert type(started).__name__ == "ClaimedBatch"
    settlement = finish_turn(
        "failed-turn", outcome="error", error=_REFUSED,
        ended_at="2026-10-11T16:01:00+00:00", successor_turn_id="successor-turn", boot_epoch="test", db=db,
    )
    return settlement


def test_rollover_success_after_finish_turn_suppresses_the_parent_refusal(db):
    delegated(db)
    first, second = _rollover_accounts(db)

    settlement = _finish_refused_turn(db)

    assert settlement.successor is not None
    successor = db.execute(
        "SELECT turn_order,vendor_account_id FROM active_turns WHERE id='successor-turn'",
    ).fetchone()
    assert successor is not None and successor["vendor_account_id"] == second["id"]
    _complete(db, "failed-turn")

    assert wakes(db) == []
    assert db.execute(
        "SELECT vendor_account_id FROM active_turns WHERE id='failed-turn'",
    ).fetchone()[0] == first["id"]


@pytest.mark.parametrize("enabled,destination_ready", [(False, True), (True, False)])
def test_unrecoverable_rollover_paths_wake_the_parent_once(db, enabled, destination_ready):
    delegated(db)
    _rollover_accounts(db, enabled=enabled, destination_ready=destination_ready)

    settlement = _finish_refused_turn(db)

    assert settlement.successor is not None
    assert type(settlement.successor).__name__ == "HeldAdmission"
    _complete(db, "failed-turn")

    assert len(wakes(db)) == 1
    assert str(wakes(db)[0]["id"]).startswith("plan-wake:child_provider_refused:")
