"""Historical PR state recovery uses the same terminal writer as live webhooks."""

from __future__ import annotations

import json

import pytest

from infra.database import get_db
from registry import models, pinning, pr_history_reconciliation as history
from tests.registry_helpers import make_session


@pytest.fixture()
def db(isolated_db):
    return get_db()


def _register(db, number: int, *, pinned: bool = False) -> tuple[str, str]:
    thread_id = make_session(db, prefix=f"pr-history-{number}")
    artifact = models.register_artifact(
        integration="github", type="pull-request", external_id=f"o/r#{number}",
        parent_thread_id=thread_id, title=f"PR #{number}", db=db,
    )
    if pinned:
        pinning.update_pinned(
            artifact["id"], pinned=True, parent_thread_id=thread_id, db=db,
        )
    db.commit()
    return artifact["id"], thread_id


def _subscribe(db, number: int) -> None:
    """Give PR ``number`` a second registration from another thread."""
    models.register_artifact(
        integration="github", type="pull-request", external_id=f"o/r#{number}",
        parent_thread_id=make_session(db, prefix=f"pr-history-sub-{number}"),
        title=f"PR #{number}", db=db,
    )
    db.commit()


def _state(db, artifact_id: str) -> tuple[str, str | None, str | None]:
    row = db.execute(
        """SELECT r.surfacing, r.retired_at, i.metadata
             FROM artifact_registrations AS r
             JOIN artifact_identities AS i ON i.id = r.artifact_id
            WHERE i.id = ?""",
        (artifact_id,),
    ).fetchone()
    metadata = json.loads(row["metadata"] or "{}")
    return row["surfacing"], row["retired_at"], metadata.get("pr_state")


def test_dry_run_then_apply_retires_historical_closed_and_merged_rows(db):
    closed, _ = _register(db, 1)
    merged, _ = _register(db, 2)
    opened, _ = _register(db, 3)
    pinned, _ = _register(db, 4, pinned=True)
    _subscribe(db, 2)
    states = {"o/r#1": "closed", "o/r#2": "merged", "o/r#3": "open", "o/r#4": "closed"}

    dry_run = history.reconcile(db, lookup_state=states.__getitem__)
    assert dry_run == {
        "candidates": 4, "open": 1, "merged": 1, "closed": 2,
        "terminal_identities": 3, "terminal_registrations": 4, "applied": 0,
    }
    assert all(_state(db, artifact)[1] is None for artifact in (closed, merged, opened, pinned))

    # Apply is gated on the identity count the dry run printed, not the registration count.
    with pytest.raises(ValueError, match="count changed"):
        history.reconcile(
            db, lookup_state=states.__getitem__, apply=True,
            expected_terminal_count=dry_run["terminal_registrations"],
        )
    applied = history.reconcile(
        db, lookup_state=states.__getitem__, apply=True,
        expected_terminal_count=dry_run["terminal_identities"],
    )
    assert applied["applied"] == 3
    assert _state(db, closed)[0:3:2] == ("settled", "closed")
    assert _state(db, merged)[0:3:2] == ("settled", "merged")
    assert _state(db, opened) == ("visible", None, None)
    assert _state(db, pinned)[0:3:2] == ("visible", "closed")
    assert all(_state(db, artifact)[1] is not None for artifact in (closed, merged, pinned))

    repeat = history.reconcile(db, lookup_state=states.__getitem__)
    assert repeat == {
        "candidates": 1, "open": 1, "merged": 0, "closed": 0,
        "terminal_identities": 0, "terminal_registrations": 0, "applied": 0,
    }


def test_changed_dry_run_count_prevents_every_write(db):
    first, _ = _register(db, 5)
    second, _ = _register(db, 6)
    with pytest.raises(ValueError, match="count changed"):
        history.reconcile(
            db, lookup_state=lambda _external_id: "closed",
            apply=True, expected_terminal_count=1,
        )
    assert _state(db, first)[1] is None
    assert _state(db, second)[1] is None


def test_lookup_error_prevents_every_write(db):
    first, _ = _register(db, 7)
    _register(db, 8)

    def lookup(external_id: str) -> str:
        if external_id == "o/r#8":
            raise RuntimeError("GitHub unavailable")
        return "closed"

    with pytest.raises(RuntimeError, match="GitHub unavailable"):
        history.reconcile(db, lookup_state=lookup, apply=True, expected_terminal_count=2)
    assert _state(db, first)[1] is None


def test_github_lookup_uses_current_state_without_pr_content(monkeypatch):
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return type("Result", (), {"stdout": '{"state":"closed","merged_at":null}'})()

    monkeypatch.setattr(history.subprocess, "run", fake_run)
    assert history._github_state("o/r#9") == "closed"
    assert calls[0][0] == [
        "gh", "api", "repos/o/r/pulls/9", "--jq", "{state,merged_at}",
    ]
    assert calls[0][1]["check"] is True
