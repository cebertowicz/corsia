"""After a failed review, the author restarts the same reviewer on the revision with delegate_send."""

from __future__ import annotations

import json

import pytest

from delegation_fixtures import deleg_db  # noqa: F401
from plans import service
from tests.review_settle_helpers import AUTHOR, build_done, ended, hand, harvest, revision_of, run, submit, thread

pytestmark = pytest.mark.asyncio


@pytest.fixture
def sent(deleg_db, monkeypatch):
    """Record each delivered message; the real delegate_send runs up to the transport."""
    delivered: list[tuple[str, str]] = []

    async def _send(thread_id, _row, text, **_kwargs):
        delivered.append((thread_id, text))
        return type("Sent", (), {"driver": "turn_engine", "duplicate": False})()

    monkeypatch.setattr("tools.delegation.send_thread_input", _send)
    return delivered


async def test_the_author_restarts_the_reviewer_on_the_revision_and_the_run_completes(deleg_db, sent):
    from tools.delegate_send import DelegateSendTool

    db = deleg_db
    db.execute("INSERT OR IGNORE INTO projects (id,name,created_at,updated_at) VALUES ('project-1','P','t','t')")
    thread(db, AUTHOR, spawned_by=None)
    plan, phase, steps = run(db)
    build_done(db, plan.id, steps["build"])
    hand(db, steps["review"], "reviewer")
    submit(db, "reviewer", verdict="fail")
    assert steps["review"] in harvest(db)
    (revision,) = revision_of(db, steps["review"])
    db.commit()

    receipt = json.loads(await DelegateSendTool().execute(
        "reviewer", "Fixed both; review again.", _thread_id=AUTHOR, plan_node_id=revision["id"],
    ))

    assert receipt["plan_task"]["attached"] is True
    assert service._node_row(db, revision["id"])["spawned_thread_id"] == "reviewer"
    assert [target for target, _text in sent] == ["reviewer"]

    submit(db, "reviewer", verdict="pass", evidence="Both fixed.")
    assert revision["id"] in harvest(db)

    assert service._node_row(db, revision["id"])["state"] == "accepted"
    (record,) = ended(db, phase)
    assert record["outcome"] == "completed"
    review_rounds = [s for s in record.get("steps", []) if s.get("step") == "review"]
    assert review_rounds and review_rounds[0].get("rounds") == 1
