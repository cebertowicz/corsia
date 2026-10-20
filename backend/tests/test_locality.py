"""Local-only classification, DB policy, and email-screen compatibility tests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest


def _row(**overrides) -> dict:
    row = {
        "selector": "omlx@local/model", "litellm_model": "omlx@local/model",
        "connection": "omlx", "provider": "omlx", "backend": "network",
    }
    row.update(overrides)
    return row


@pytest.mark.parametrize(
    ("row", "private_servers", "local_servers", "expected"),
    [
        (_row(), set(), {"local"}, True),
        (_row(), set(), set(), False),
        (_row(), {"local"}, set(), True),
        (_row(selector="external@cloud/model", litellm_model="external@cloud/model", connection="external", provider="external"), set(), set(), False),
        (_row(selector="external@cloud/model", litellm_model="external@cloud/model", connection="external", provider="external"), {"cloud"}, set(), True),
        (_row(backend="api", selector="anthropic/model", litellm_model="anthropic/model", connection="anthropic", provider="anthropic"), set(), set(), False),
        (_row(connection="codex", provider="codex"), set(), {"local"}, False),
    ],
)
def test_row_is_local_matrix(row, private_servers, local_servers, expected):
    from llm.locality import row_is_local

    assert row_is_local(row, private_servers=private_servers, local_servers=local_servers) is expected


@pytest.mark.parametrize(
    ("api_base", "expected"),
    [("http://127.0.0.1:8000/v1", True), ("http://192.168.1.2:8000/v1", False), (None, False)],
)
def test_serverless_network_row_reads_its_endpoint(monkeypatch, api_base, expected):
    from llm import capabilities
    from llm.locality import row_is_local

    monkeypatch.setattr(capabilities, "make_provider_params", lambda _model: {"api_base": api_base})
    row = _row(selector="lmstudio/model", litellm_model="lmstudio/model", provider="lmstudio", connection="lmstudio")
    assert row_is_local(row) is expected


def test_row_is_local_fails_closed_when_endpoint_cannot_be_read(monkeypatch):
    from llm import capabilities
    from llm.locality import row_is_local

    monkeypatch.setattr(capabilities, "make_provider_params", lambda _model: (_ for _ in ()).throw(ValueError()))
    assert row_is_local(_row(selector="lmstudio/model", litellm_model="lmstudio/model")) is False


def _project(db, project_id="project"):
    db.execute("INSERT INTO projects(id,name,created_at,updated_at) VALUES (?,?,?,?)", (project_id, project_id, "t", "t"))


def _server(db, server_id="remote", *, private=False):
    metadata = '{"private": true}' if private else "{}"
    db.execute("INSERT INTO network_servers(id,provider,kind,instance_id,instance_name,port,status,metadata_json,last_seen_at,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)", (server_id, "omlx", "managed", server_id, server_id, 8000, "online", metadata, "t", "t", "t"))


def test_locality_refusal_reads_private_mark_and_project_policy(isolated_db):
    from infra.database import get_db
    from llm.locality import locality_refusal, set_project_cloud_models_off

    db = get_db()
    _project(db)
    _server(db)
    set_project_cloud_models_off("project", cloud_models_off=True, db=db)
    row = {"litellm_model": "omlx@remote/model", "backend": "network", "provider": "omlx", "harness": "native"}
    assert locality_refusal(row, thread_local_only=False, project_id="project", db=db) == "project_cloud_off"
    db.execute("UPDATE network_servers SET metadata_json='{\"private\": true}' WHERE id='remote'")
    db.commit()
    assert locality_refusal(row, thread_local_only=False, project_id="project", db=db) is None
    assert locality_refusal({"backend": "api"}, thread_local_only=True, project_id="project", db=db) == "thread_local_only"
    set_project_cloud_models_off("project", cloud_models_off=False, db=db)
    assert locality_refusal({"backend": "api"}, thread_local_only=False, project_id="project", db=db) is None
    assert locality_refusal({"backend": "api"}, thread_local_only=False, project_id=None, db=db) is None


def test_project_cloud_off_text_is_governance_wording():
    from prompts.cloud_off import project_cloud_off_text

    head = "project_cloud_off: This project uses local models only, so codex/gpt-5.6-terra can't run here. "
    assert project_cloud_off_text("codex/gpt-5.6-terra", ["omlx@local/a", "omlx@local/b"]) == (
        head + "Use a ready local model: omlx@local/a, omlx@local/b."
    )
    assert project_cloud_off_text("codex/gpt-5.6-terra", [], spawn=True) == (
        head + "No local model is ready now. Do the work in a thread already on a local model,"
        " or check model_catalog later. Local workers run one at a time."
    )


def test_held_text_moves_the_thread_instead_of_staying_on_its_cloud_model():
    from prompts.cloud_off import HOLD_ACTION, held_error

    action = (
        "Move the thread to a ready local model from model_catalog (delegate_set_model). "
        "The held message runs once it is on one. "
        "If none is ready, do the work in a thread already on a local model."
    )
    assert HOLD_ACTION == action
    assert held_error("codex/gpt-5.6-terra") == (
        "Held: this project uses local models only, so codex/gpt-5.6-terra can't run here. " + action
    )
    assert held_error("codex/gpt-5.6-terra", "thread_local_only") == (
        "Held: codex/gpt-5.6-terra can't run here: this chat is set to local models only;"
        " it runs once moved to a local model. " + action
    )
    for cause in ("project_cloud_off", "thread_local_only"):
        assert "current model" not in held_error("codex/gpt-5.6-terra", cause)


def test_email_screen_keeps_external_loopback_behavior():
    from connections.google.email_screen import local_endpoint_refusal

    target = SimpleNamespace(harness="native", backend="network", transport_model="external@cloud/model")
    assert local_endpoint_refusal(target, lambda _model: {"api_base": "http://127.0.0.1:8000/v1"}) is None
