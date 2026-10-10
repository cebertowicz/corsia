"""Answering a question that already left the queue says so in plain words.

A decision can be withdrawn by its agent, or answered from another surface,
while it still sits in someone's dock. That answer is refused with 410 and one
sentence the dock can show as written, never the state machine's wording.
"""

from __future__ import annotations

import plans.decisions as decisions
from tests.test_attention_queue import _actor, _mid, db, raise_decision  # noqa: F401
from tests.test_attention_queue_api import _plans_client


def _answer(client, decision: dict, *, mutation_id: str, revision: int | None = None):
    return client.post(
        f"/api/plans/decisions/{decision['id']}/answer",
        json={
            "option_id": "A",
            "expected_revision": decision["revision"] if revision is None else revision,
            "mutation_id": mutation_id,
        },
    )


def _withdraw(db, decision: dict) -> dict:
    withdrawn = decisions.invalidate(
        decision["id"], expected_revision=decision["revision"], mutation_id=_mid(),
        actor=_actor("child"), provenance="test.closed", db=db,
    ).result["decision"]
    db.commit()
    return withdrawn


def test_answering_a_withdrawn_question_is_refused_in_plain_words(db, monkeypatch):
    decision = raise_decision(db, "child", "child")
    _withdraw(db, decision)
    client = _plans_client(db, monkeypatch)

    response = _answer(client, decision, mutation_id="late-1")

    assert response.status_code == 410
    assert response.json()["detail"] == "This question was withdrawn."


def test_withdrawn_is_reported_even_with_the_current_revision(db, monkeypatch):
    decision = raise_decision(db, "child", "child")
    withdrawn = _withdraw(db, decision)
    client = _plans_client(db, monkeypatch)

    response = _answer(client, decision, mutation_id="late-2", revision=withdrawn["revision"])

    assert response.status_code == 410
    assert response.json()["detail"] == "This question was withdrawn."


def test_answering_an_already_answered_question_is_refused_in_plain_words(db, monkeypatch):
    decision = raise_decision(db, "child", "child")
    client = _plans_client(db, monkeypatch)
    assert _answer(client, decision, mutation_id="first").status_code == 200

    response = _answer(client, decision, mutation_id="second")

    assert response.status_code == 410
    assert response.json()["detail"] == "This was already answered."


def test_a_lost_response_retry_of_the_same_answer_still_succeeds(db, monkeypatch):
    decision = raise_decision(db, "child", "child")
    client = _plans_client(db, monkeypatch)
    assert _answer(client, decision, mutation_id="same").status_code == 200

    retried = _answer(client, decision, mutation_id="same")

    assert retried.status_code == 200
    assert retried.json()["decision"]["state"] == "answered"


def test_a_live_decision_with_a_stale_revision_is_still_a_conflict(db, monkeypatch):
    decision = raise_decision(db, "child", "child")
    client = _plans_client(db, monkeypatch)

    response = _answer(client, decision, mutation_id="stale", revision=decision["revision"] + 5)

    assert response.status_code == 409
