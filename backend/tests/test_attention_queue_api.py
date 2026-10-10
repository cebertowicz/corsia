"""HTTP contract for the one attention queue, plus the 422/409 error split.

The journey proven here names no plan id at any point: a subagent raises, the
user reads their queue, answers, and the queue advances.

Red at `19ea844a`: `GET /api/attention/queue` does not exist, and every
validation refusal is reported as `409 conflict`.
"""

from __future__ import annotations

import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from infra.schema import SCHEMA_SQL
from plans import decisions
from plans.boundary import PlanActor
from tests.test_attention_queue import (  # noqa: F401 - shared fixtures
    _mid,
    _seed,
    db,
    raise_decision,
)


class _DbProxy:
    """Keep the fixture connection open when a route closes its handle."""

    def __init__(self, connection):
        self._connection = connection

    def __getattr__(self, name):
        return getattr(self._connection, name)

    def close(self):
        return None


async def _immediate(callable_):
    return callable_()


def _attention_client(db, monkeypatch, user_id: str = "user-1") -> TestClient:
    from api import attention as api_attention

    monkeypatch.setattr(api_attention, "get_db", lambda: _DbProxy(db))
    monkeypatch.setattr(api_attention, "run_in_db", _immediate)
    app = FastAPI()
    app.include_router(api_attention.router)
    app.dependency_overrides[api_attention.require_user] = lambda: {"id": user_id}
    return TestClient(app)


def _plans_client(db, monkeypatch, user_id: str = "user-1") -> TestClient:
    from api import plans as api_plans

    monkeypatch.setattr(api_plans, "get_db", lambda: _DbProxy(db))
    monkeypatch.setattr(api_plans, "run_in_db", _immediate)
    app = FastAPI()
    app.include_router(api_plans.router)
    app.dependency_overrides[api_plans.require_user] = lambda: {
        "id": user_id, "token_kind": "session",
    }
    return TestClient(app)


# --- the journey, with no plan id anywhere -----------------------------------


def test_the_queue_serves_a_subagent_decision_with_no_plan_in_the_request(
    db, monkeypatch,
):
    decision = raise_decision(db, "child", "child")
    client = _attention_client(db, monkeypatch)

    response = client.get("/api/attention/queue")

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    assert body["current"]["external_id"] == decision["id"]
    assert body["current"]["renderer"] == "decision"
    assert body["current"]["detail"]["source"]["thread_label"] == "Deep subagent"


def test_the_queue_lists_every_waiting_item_in_order_headed_by_current(
    db, monkeypatch,
):
    for title in ("First question", "Second question", "Third question"):
        raise_decision(db, "child", "child", title=title)
    client = _attention_client(db, monkeypatch)

    body = client.get("/api/attention/queue").json()

    assert body["count"] == 3
    assert [i["detail"]["title"] for i in body["items"]] == [
        "First question", "Second question", "Third question",
    ]
    assert body["items"][0] == body["current"]


def test_the_queue_items_are_capped_while_count_stays_true(db, monkeypatch):
    from api import attention as api_attention

    monkeypatch.setattr(api_attention, "QUEUE_ITEMS_CAP", 2)
    for n in range(3):
        raise_decision(db, "child", "child", title=f"Question {n}")
    client = _attention_client(db, monkeypatch)

    body = client.get("/api/attention/queue").json()

    assert body["count"] == 3
    assert len(body["items"]) == 2
    assert body["items"][0] == body["current"]


def test_fetch_then_answer_then_the_queue_advances(db, monkeypatch):
    first = raise_decision(db, "child", "child", title="First question")
    second = raise_decision(db, "child", "child", title="Second question")
    assert second["id"] != first["id"]
    queue_client = _attention_client(db, monkeypatch)
    plans_client = _plans_client(db, monkeypatch)

    head = queue_client.get("/api/attention/queue").json()
    assert head["count"] == 2
    assert head["current"]["detail"]["title"] == "First question"

    answered = plans_client.post(
        f"/api/plans/decisions/{head['current']['external_id']}/answer",
        json={"option_id": "A",
              "expected_revision": head["current"]["detail"]["revision"],
              "mutation_id": "journey-1"},
    )
    assert answered.status_code == 200

    advanced = queue_client.get("/api/attention/queue").json()
    assert advanced["count"] == 1
    assert advanced["current"]["detail"]["title"] == "Second question"


def test_answering_the_last_item_empties_the_queue(db, monkeypatch):
    decision = raise_decision(db, "child", "child")
    queue_client = _attention_client(db, monkeypatch)
    plans_client = _plans_client(db, monkeypatch)

    plans_client.post(
        f"/api/plans/decisions/{decision['id']}/answer",
        json={"option_id": "B", "expected_revision": decision["revision"],
              "mutation_id": "journey-2"},
    )

    empty = queue_client.get("/api/attention/queue").json()
    assert empty == {"current": None, "count": 0, "items": []}


def test_another_user_sees_none_of_it(db, monkeypatch):
    raise_decision(db, "child", "child")
    stranger = _attention_client(db, monkeypatch, user_id="user-2")

    assert stranger.get("/api/attention/queue").json() == {
        "current": None, "count": 0, "items": [],
    }


def test_the_queue_never_echoes_an_internal_artifact_id(db, monkeypatch):
    raise_decision(db, "child", "child")
    client = _attention_client(db, monkeypatch)

    body = client.get("/api/attention/queue").text
    artifact_id = db.execute(
        "SELECT id FROM artifact_identities WHERE type='plan-decision'"
    ).fetchone()["id"]

    assert artifact_id not in body


# --- 422 for what the user can fix, 409 only for real movement ---------------


def test_a_listed_option_carrying_leftover_other_text_is_a_422_not_a_conflict(
    db, monkeypatch,
):
    """The head defect: this was reported to the user as staleness, forever."""
    decision = raise_decision(db, "child", "child")
    client = _plans_client(db, monkeypatch)

    response = client.post(
        f"/api/plans/decisions/{decision['id']}/answer",
        json={"option_id": "A", "other_text": "Actually, split this into two.",
              "expected_revision": decision["revision"], "mutation_id": "v-1"},
    )

    assert response.status_code == 422
    assert "Other" in response.json()["detail"]


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ({"option_id": "Z"}, "unknown option"),
        ({"option_id": ""}, "empty option"),
        ({"option_id": "other"}, "other with no text"),
        ({"option_id": "other", "other_text": "   "}, "other with blank text"),
        ({"option_id": "other", "other_text": "x" * 2001}, "other text too long"),
    ],
)
def test_every_fixable_answer_refusal_is_422(db, monkeypatch, payload, reason):
    decision = raise_decision(db, "child", "child")
    client = _plans_client(db, monkeypatch)

    response = client.post(
        f"/api/plans/decisions/{decision['id']}/answer",
        json={**payload, "expected_revision": decision["revision"],
              "mutation_id": f"v-{reason}"},
    )

    assert response.status_code == 422, reason


def test_a_genuinely_stale_revision_is_still_409(db, monkeypatch):
    decision = raise_decision(db, "child", "child")
    client = _plans_client(db, monkeypatch)

    response = client.post(
        f"/api/plans/decisions/{decision['id']}/answer",
        json={"option_id": "A", "expected_revision": decision["revision"] + 5,
              "mutation_id": "v-stale"},
    )

    assert response.status_code == 409


def test_a_withdrawn_decision_is_gone_with_a_plain_sentence(db, monkeypatch):
    decision = raise_decision(db, "child", "child")
    decisions.invalidate(
        decision["id"], expected_revision=decision["revision"], mutation_id=_mid(),
        actor=PlanActor("child", "user-1", "project-1"), provenance="p", db=db,
    )
    db.commit()
    client = _plans_client(db, monkeypatch)

    response = client.post(
        f"/api/plans/decisions/{decision['id']}/answer",
        json={"option_id": "A", "expected_revision": decision["revision"] + 1,
              "mutation_id": "v-gone"},
    )

    assert response.status_code == 410
    assert response.json()["detail"] == "This question was withdrawn."


def test_validation_refusal_leaves_the_decision_answerable(db, monkeypatch):
    decision = raise_decision(db, "child", "child")
    client = _plans_client(db, monkeypatch)

    client.post(
        f"/api/plans/decisions/{decision['id']}/answer",
        json={"option_id": "A", "other_text": "leftover",
              "expected_revision": decision["revision"], "mutation_id": "v-2"},
    )
    recovered = client.post(
        f"/api/plans/decisions/{decision['id']}/answer",
        json={"option_id": "A", "other_text": None,
              "expected_revision": decision["revision"], "mutation_id": "v-3"},
    )

    assert recovered.status_code == 200
    assert recovered.json()["decision"]["state"] == "answered"


def test_a_422_never_echoes_an_internal_identifier(db, monkeypatch):
    decision = raise_decision(db, "child", "child")
    client = _plans_client(db, monkeypatch)

    response = client.post(
        f"/api/plans/decisions/{decision['id']}/answer",
        json={"option_id": "A", "other_text": "leftover",
              "expected_revision": decision["revision"], "mutation_id": "v-4"},
    )

    assert decision["id"] not in response.text
    assert decision["plan_id"] not in response.text


# --- the generic list keeps everything the queue does not draw ---------------


def test_generic_raised_list_keeps_rows_no_typed_renderer_claims(db, monkeypatch):
    from registry import attention, models

    raise_decision(db, "child", "child")
    from infra.database import write_transaction
    with write_transaction(db, scope="test_attention_setup"):
        models.register_artifact(
            integration="local", type="note", external_id="ordinary-1",
            parent_thread_id="root", title="Ordinary", db=db,
        )
        ordinary = db.execute(
            "SELECT id FROM artifact_identities WHERE external_id='ordinary-1'"
        ).fetchone()["id"]
        attention.raise_attention(ordinary, db=db)
    db.commit()
    client = _attention_client(db, monkeypatch)

    rows = client.get("/api/attention/raised").json()["items"]

    assert [row["external_id"] for row in rows] == ["ordinary-1"]


def test_the_generic_list_no_longer_carries_a_routed_to_assertion(db, monkeypatch):
    """`routed_to` claimed a renderer existed; the payload now proves it."""
    from registry import attention, models

    from infra.database import write_transaction
    with write_transaction(db, scope="test_attention_setup"):
        models.register_artifact(
            integration="local", type="note", external_id="ordinary-1",
            parent_thread_id="root", title="Ordinary", db=db,
        )
        ordinary = db.execute(
            "SELECT id FROM artifact_identities WHERE external_id='ordinary-1'"
        ).fetchone()["id"]
        attention.raise_attention(ordinary, db=db)
    db.commit()
    client = _attention_client(db, monkeypatch)

    rows = client.get("/api/attention/raised").json()["items"]

    assert rows and "routed_to" not in rows[0]
