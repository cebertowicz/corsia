"""Signed HTTP ingress proves raw refusal and code-fault recovery without a scheduler stub."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from events.connectors import github_outcomes as outcomes
from infra import drain
from test_github_raw_fact_fault_classification import _rows
from test_github_raw_facts_at_admission import delivery
from webhook_ingress_fixtures import WEBHOOK_SECRET, _isolated_db, _make_signature  # noqa: F401


@pytest.fixture(autouse=True)
def own_admission(monkeypatch):
    monkeypatch.setattr(drain, "_turn_admissions", 0)
    monkeypatch.setattr("api.webhooks._get_webhook_secret", lambda: WEBHOOK_SECRET)


def send(client, event_type, payload, delivery_id):
    body = json.dumps(payload).encode()
    return client.post("/webhooks/github", content=body, headers={
        "X-GitHub-Event": event_type,
        "X-GitHub-Delivery": delivery_id,
        "X-Hub-Signature-256": _make_signature(body),
        "Content-Type": "application/json",
    })


@pytest.mark.parametrize("field,value", [("changes", "bad"), ("repository", "bad")])
def test_http_wrong_shape_refuses_facts_but_real_operational_mapper_ignores_edit(
    _isolated_db, field, value,
):
    from main import app

    event_type, payload = delivery("pr_retargeted")
    payload[field] = value
    client = TestClient(app, raise_server_exceptions=False)
    response = send(client, event_type, payload, "d-http-refusal")
    assert response.status_code == 200
    assert response.json()["status"] == "ignored", "edited is not an operational routing event"
    assert _rows(_isolated_db) == {
        "deliveries": ["d-http-refusal"], "facts": [], "counters": ["mapping_failed"],
    }
    duplicate = send(client, event_type, payload, "d-http-refusal")
    assert duplicate.status_code == 200 and duplicate.json()["status"] == "duplicate"
    assert _rows(_isolated_db)["counters"] == ["mapping_failed"]
    assert drain._turn_admissions == 0


@pytest.mark.parametrize("fault", [KeyError, TypeError, AttributeError])
def test_http_code_fault_aborts_and_same_id_repairs_through_real_scheduler(
    _isolated_db, monkeypatch, fault,
):
    from main import app

    event_type, payload = delivery("pr_retargeted")
    real = outcomes.map_github_outcomes

    def broken(*args):
        assert real(*args), "inject only after valid real mapping"
        raise fault("private marker")

    client = TestClient(app, raise_server_exceptions=False)
    with monkeypatch.context() as patch:
        patch.setattr(outcomes, "map_github_outcomes", broken)
        failed = send(client, event_type, payload, "d-http-fault")
    assert failed.status_code == 500
    assert _rows(_isolated_db) == {"deliveries": [], "facts": [], "counters": []}
    assert drain._turn_admissions == 0
    repaired = send(client, event_type, payload, "d-http-fault")
    assert repaired.status_code == 200 and repaired.json()["status"] == "ignored"
    rows = _rows(_isolated_db)
    assert rows["deliveries"] == ["d-http-fault"] and len(rows["facts"]) == 1
    assert rows["counters"] == []
    assert drain._turn_admissions == 0


def test_non_object_envelope_current_receipt_path_fails_before_scheduler(_isolated_db):
    from main import app

    response = send(TestClient(app, raise_server_exceptions=False), "check_suite", [], "d-array")
    assert response.status_code == 500, "object-envelope metadata access precedes operational 422"
    assert _rows(_isolated_db) == {"deliveries": [], "facts": [], "counters": []}
    assert drain._turn_admissions == 0
