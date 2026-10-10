"""A pull request's own end retires the rows that tracked it (#1822).

Plan-node settlement spares a registered PR, so its merge or close is the only
thing that takes it off a pane. Every thread that tracked the PR loses the row
in the same write that records the terminal state; an owner pin still wins.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from infra.database import get_db
from registry import models, pinning, pr_lifecycle, view
from tests.registry_helpers import make_session


@pytest.fixture()
def db(isolated_db):
    return get_db()


def _register_pr(db, thread_id: str, number: int) -> dict:
    return models.register_artifact(
        integration="github",
        type="pull-request",
        external_id=f"o/r#{number}",
        parent_thread_id=thread_id,
        title=f"PR #{number}",
        db=db,
    )


def _rows(db, artifact_id: str) -> dict[str, str]:
    return {
        row["parent_thread_id"]: row["surfacing"]
        for row in db.execute(
            "SELECT parent_thread_id, surfacing FROM artifact_registrations "
            "WHERE artifact_id=?",
            (artifact_id,),
        ).fetchall()
    }


def _stamp(db, artifact_id: str, thread_id: str):
    return db.execute(
        "SELECT retired_at FROM artifact_registrations "
        "WHERE artifact_id=? AND parent_thread_id=?",
        (artifact_id, thread_id),
    ).fetchone()[0]


def _events(db, artifact_id: str) -> list:
    return db.execute(
        "SELECT metadata FROM artifact_registration_events "
        "WHERE artifact_id=? AND event_type='artifact_ended' ORDER BY rowid",
        (artifact_id,),
    ).fetchall()


def _merge(db, number: int):
    return pr_lifecycle.record_pull_request_ended(
        "github", "pull-request", f"o/r#{number}", pr_state="merged", db=db,
    )


def test_merge_retires_every_thread_that_tracked_the_pull_request(db):
    owner = make_session(db, prefix="pr-end-owner")
    other = make_session(db, prefix="pr-end-other")
    pr = _register_pr(db, owner, 101)
    _register_pr(db, other, 101)
    db.commit()

    row = pr_lifecycle.record_pull_request_ended(
        "github", "pull-request", "o/r#101", pr_state="merged", db=db,
    )

    assert row is not None
    assert json.loads(row["metadata"])["pr_state"] == "merged"
    assert set(_rows(db, pr["id"]).values()) == {"settled"}
    assert len(_events(db, pr["id"])) == 2


def test_close_without_merge_also_retires_the_row(db):
    owner = make_session(db, prefix="pr-end-closed")
    pr = _register_pr(db, owner, 102)
    db.commit()

    row = pr_lifecycle.record_pull_request_ended(
        "github", "pull-request", "o/r#102", pr_state="closed", db=db,
    )

    assert row is not None
    metadata = json.loads(row["metadata"])
    assert (metadata["pr_state"], metadata["merged"]) == ("closed", False)
    assert _rows(db, pr["id"])[owner] == "settled"


def test_pinned_at_merge_stays_until_unpinned_then_retires(db):
    owner = make_session(db, prefix="pr-end-pinned")
    pr = _register_pr(db, owner, 103)
    pinning.update_pinned(pr["id"], pinned=True, parent_thread_id=owner, db=db)

    _merge(db, 103)

    assert _rows(db, pr["id"])[owner] == "visible"
    assert _stamp(db, pr["id"], owner) is not None
    assert len(_events(db, pr["id"])) == 1

    pinning.update_pinned(pr["id"], pinned=False, parent_thread_id=owner, db=db)

    assert _rows(db, pr["id"])[owner] == "settled"


def test_pinning_after_merge_shows_the_row_and_unpinning_retires_it_again(db):
    owner = make_session(db, prefix="pr-end-pin-after")
    pr = _register_pr(db, owner, 107)
    db.commit()
    _merge(db, 107)
    stamped = _stamp(db, pr["id"], owner)

    pinning.update_pinned(pr["id"], pinned=True, parent_thread_id=owner, db=db)
    assert _rows(db, pr["id"])[owner] == "visible"

    pinning.update_pinned(pr["id"], pinned=False, parent_thread_id=owner, db=db)
    assert _rows(db, pr["id"])[owner] == "settled"
    assert _stamp(db, pr["id"], owner) == stamped


def test_a_user_quieted_row_keeps_its_surfacing(db):
    owner = make_session(db, prefix="pr-end-quiet")
    pr = _register_pr(db, owner, 108)
    db.execute(
        "UPDATE artifact_registrations SET surfacing='quiet' WHERE artifact_id=?",
        (pr["id"],),
    )
    db.commit()

    _merge(db, 108)

    assert _rows(db, pr["id"])[owner] == "quiet"
    assert _stamp(db, pr["id"], owner) is not None


def test_replaying_the_same_merge_settles_nothing_twice(db):
    owner = make_session(db, prefix="pr-end-replay")
    pr = _register_pr(db, owner, 104)
    db.commit()

    for _ in range(2):
        pr_lifecycle.record_pull_request_ended(
            "github", "pull-request", "o/r#104", pr_state="merged", db=db,
        )

    assert _rows(db, pr["id"])[owner] == "settled"
    assert len(_events(db, pr["id"])) == 1


def test_an_unregistered_pull_request_is_a_no_op(db):
    assert pr_lifecycle.record_pull_request_ended(
        "github", "pull-request", "o/r#999", pr_state="merged", db=db,
    ) is None


def test_a_non_terminal_state_is_rejected(db):
    with pytest.raises(ValueError, match="pr_state"):
        pr_lifecycle.record_pull_request_ended(
            "github", "pull-request", "o/r#105", pr_state="open", db=db,
        )


def test_the_merge_nudges_and_counts_like_node_settlement(db):
    owner = make_session(db, prefix="pr-end-signal")
    pr = _register_pr(db, owner, 109)
    db.commit()
    nudges: list[dict] = []

    with patch(
        "plans.notifications.state.broadcast_to_user",
        lambda _user_id, message: nudges.append(message),
    ), patch("infra.product_events.emit_product_event") as emit:
        _merge(db, 109)

    assert [(m["type"], m["artifact_id"]) for m in nudges] == [
        ("artifacts_changed", pr["id"]),
    ]
    emit.assert_called_once()
    assert emit.call_args.args[:2] == ("artifact_pane", "auto_retired")
    properties = emit.call_args.kwargs["properties"]
    assert properties["trigger"] == "pull_request_merged"
    assert properties["retired_count"] == 1
    assert properties["type_counts"] == {"github/pull-request": 1}


def test_the_merged_row_leaves_the_pane(db):
    owner = make_session(db, prefix="pr-end-pane")
    pr = _register_pr(db, owner, 106)
    db.commit()
    before = {n["id"] for n in view.pane_for_thread(owner)["nodes"]}
    assert pr["id"] in before

    pr_lifecycle.record_pull_request_ended(
        "github", "pull-request", "o/r#106", pr_state="merged", db=db,
    )

    after = {n["id"] for n in view.pane_for_thread(owner)["nodes"]}
    assert pr["id"] not in after
