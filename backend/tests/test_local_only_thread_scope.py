"""Local-only thread scope admission, inheritance, route, and send contracts."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing

import httpx
import pytest
from fastapi import FastAPI


def _db() -> sqlite3.Connection:
    from infra.schema import SCHEMA_SQL

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA_SQL)
    return db


def _thread(
    db,
    thread_id: str,
    *,
    user_id: str = "u",
    model: str | None = "local",
    local_only: int = 0,
    inherited_from: str | None = None,
    spawned_by: str | None = None,
    pending_model: str | None = None,
) -> None:
    db.execute(
        "INSERT INTO threads("
        "id,user_id,model,pending_model,local_only,"
        "local_only_root_thread_id,spawned_by,created_at,updated_at"
        ") VALUES (?,?,?,?,?,?,?,?,?)",
        (
            thread_id,
            user_id,
            model,
            pending_model,
            local_only,
            inherited_from,
            spawned_by,
            "t",
            "t",
        ),
    )


def test_model_refusal_uses_governance_wording(monkeypatch) -> None:
    from infra import local_only

    db = _db()
    monkeypatch.setattr(local_only, "locality_refusal", lambda *_args, **_kwargs: "thread_local_only")
    monkeypatch.setattr(local_only, "_ready_local_selectors", lambda: ["omlx@local/a", "omlx@local/b"])

    assert local_only.local_only_model_refusal(
        "codex/gpt-6.1-sol", thread_local_only=True, project_id=None, db=db,
    ) == (
        "local_only_requires_local_model: codex/gpt-6.1-sol can't run here: "
        "this chat is set to local models only. Use a ready local model: "
        "omlx@local/a, omlx@local/b."
    )


def test_model_refusal_uses_governance_no_ready_wording_and_spawn_suffix(monkeypatch) -> None:
    from infra import local_only
    from prompts.cloud_off import NO_LOCAL_READY_TEXT

    db = _db()
    monkeypatch.setattr(local_only, "locality_refusal", lambda *_args, **_kwargs: "thread_local_only")
    monkeypatch.setattr(local_only, "_ready_local_selectors", lambda: [])

    refusal = local_only.local_only_model_refusal(
        "codex/gpt-6.1-sol", thread_local_only=True, project_id=None, db=db, spawn=True,
    )

    # With no local model ready, the recovery is never to stay on the refused model.
    assert refusal == (
        "local_only_requires_local_model: codex/gpt-6.1-sol can't run here: "
        f"this chat is set to local models only. {NO_LOCAL_READY_TEXT} "
        "Local workers run one at a time."
    )
    assert refusal.startswith(f"{local_only.LOCAL_ONLY_REFUSAL_CODE}: ")
    assert "thread_local_only" not in refusal


def test_model_refusal_delegates_project_cloud_off_wording_to_locality(monkeypatch) -> None:
    from infra import local_only

    db = _db()
    monkeypatch.setattr(local_only, "locality_refusal", lambda *_args, **_kwargs: "project_cloud_off")
    monkeypatch.setattr(local_only, "_ready_local_selectors", lambda: ["omlx@local/a"])

    assert local_only.local_only_model_refusal(
        "codex/gpt-6.1-sol", thread_local_only=False, project_id="project", db=db, spawn=True,
    ) == (
        "project_cloud_off: This project uses local models only, so codex/gpt-6.1-sol "
        "can't run here. Use a ready local model: omlx@local/a. Local workers run one at a time."
    )


def test_refusals_never_carry_a_secret(monkeypatch) -> None:
    from infra import local_only

    db = _db()
    secret = "sk-local-only-sentinel"
    monkeypatch.setattr(local_only, "_catalog_row", lambda model, **_kwargs: {
        "litellm_model": model, "is_local": False, "api_key": secret,
        "api_base": f"https://user:{secret}@cloud.example/v1",
    })
    monkeypatch.setattr(local_only, "_ready_local_selectors", lambda: ["omlx@local/a"])

    model_refusal = local_only.local_only_model_refusal(
        "codex/gpt-6.1-sol", thread_local_only=True, project_id=None, db=db, spawn=True,
    )
    send_refusal = local_only.send_confirmation_refusal(
        {"local_only": 1},
        {"id": "r", "title": "Reviewer", "model": "codex/gpt-6.1-sol"},
        no_sensitive_content=None,
        db=db,
    )

    assert model_refusal and send_refusal
    assert secret not in model_refusal
    assert secret not in send_refusal


def test_scope_to_thread_resolves_a_local_only_thread_through_the_canonical_resolver(
    monkeypatch,
) -> None:
    from llm import locality
    from llm.resolution import resolve_thread_target

    db = _db()
    _thread(db, "local-thread", local_only=1)
    db.execute(
        "INSERT INTO models (id,harness,provider,provider_model_id,litellm_model,display_name,"
        "backend,enabled,available,context_window,created_at,updated_at) "
        "VALUES ('cloud','native','external','cloud','cloud','Cloud',"
        "'network',1,1,1,'t','t')"
    )
    monkeypatch.setattr(
        locality,
        "locality_refusal",
        lambda _row, *, thread_local_only, **_kwargs: (
            "thread_local_only" if thread_local_only else None
        ),
    )
    resolved = resolve_thread_target("cloud", thread_id="local-thread", db=db)

    assert (resolved.available, resolved.availability_reason) == (False, "thread_local_only")


def test_child_effective_local_only_follows_its_root_without_propagation() -> None:
    from connections.account_policy import thread_scope

    db = _db()
    _thread(db, "root", local_only=0)
    _thread(db, "child", inherited_from="root", spawned_by="root")

    assert thread_scope("child", db=db)["local_only"] == 0
    db.execute("UPDATE threads SET local_only=1 WHERE id='root'")
    assert thread_scope("child", db=db)["local_only"] == 1
    assert db.execute("SELECT local_only FROM threads WHERE id='child'").fetchone()[0] == 0


def test_create_fresh_thread_refuses_cloud_child_without_writing(monkeypatch) -> None:
    from infra import fresh_threads

    db = _db()
    _thread(db, "root", local_only=1)
    monkeypatch.setattr(
        "infra.local_only.local_only_model_refusal",
        lambda model, **_kwargs: "local_only_requires_local_model" if model == "cloud" else None,
    )

    with pytest.raises(ValueError, match="local_only_requires_local_model"):
        fresh_threads.create_fresh_thread(
            user_id="u",
            thread_id="refused-child",
            model="cloud",
            spawned_by="root",
            db=db,
        )

    assert db.execute("SELECT id FROM threads WHERE id='refused-child'").fetchone() is None


def test_request_switch_refuses_cloud_for_local_only_thread(monkeypatch) -> None:
    from infra import model_switches

    db = _db()
    _thread(db, "thread", local_only=1)
    monkeypatch.setattr(
        "infra.local_only.local_only_model_refusal",
        lambda *_args, **_kwargs: "local_only_requires_local_model",
    )
    monkeypatch.setattr(model_switches, "bind_model_account", lambda *_args, **_kwargs: None)

    with pytest.raises(ValueError, match="local_only_requires_local_model"):
        model_switches.request_switch(
            "thread", "cloud", operation_id="op", requested_at="t", db=db,
        )

    assert db.execute(
        "SELECT pending_model FROM threads WHERE id='thread'",
    ).fetchone()[0] is None



def test_adopt_first_model_refuses_cloud_for_local_only_thread(monkeypatch) -> None:
    from infra import model_switch_inputs
    from infra.database import write_transaction

    db = _db()
    _thread(db, "thread", model=None, local_only=1)
    monkeypatch.setattr(
        "infra.local_only.local_only_model_refusal",
        lambda *_args, **_kwargs: "local_only_requires_local_model",
    )

    with write_transaction(db, scope="test") as connection:
        with pytest.raises(ValueError, match="local_only_requires_local_model"):
            model_switch_inputs.adopt_first_model(connection, "thread", "cloud", now="t")

    assert db.execute("SELECT model FROM threads WHERE id='thread'").fetchone()[0] is None


def test_inheritance_uses_root_and_every_creator_candidate(monkeypatch) -> None:
    from infra import fresh_threads

    db = _db()
    _thread(db, "root", local_only=1)
    _thread(db, "child", local_only=1, inherited_from="root", spawned_by="root")
    _thread(db, "ordinary")
    monkeypatch.setattr("infra.local_only.local_only_model_refusal", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        "connections.account_selection.bind_model_account",
        lambda *_args, **_kwargs: None,
    )

    grandchild = fresh_threads.create_fresh_thread(
        user_id="u", thread_id="grandchild", model="local", spawned_by="child", db=db,
    )
    inherited_later = fresh_threads.create_fresh_thread(
        user_id="u",
        thread_id="later",
        model="local",
        parent_thread_id="ordinary",
        actor_thread_id="root",
        db=db,
    )
    unaffected = fresh_threads.create_fresh_thread(
        user_id="u", thread_id="unaffected", model="cloud", spawned_by="ordinary", db=db,
    )
    agent_chat = fresh_threads.create_fresh_thread(
        user_id="u", thread_id="agent-chat", model="local", actor_thread_id="child", db=db,
    )
    ordinary_agent_chat = fresh_threads.create_fresh_thread(
        user_id="u", thread_id="ordinary-chat", model="cloud", actor_thread_id="ordinary", db=db,
    )

    assert (grandchild["local_only"], grandchild["local_only_root_thread_id"]) == (0, "root")
    assert (inherited_later["local_only"], inherited_later["local_only_root_thread_id"]) == (0, "root")
    assert (unaffected["local_only"], unaffected["local_only_root_thread_id"]) == (0, "ordinary")
    assert agent_chat["local_only_root_thread_id"] == "root"
    assert ordinary_agent_chat["local_only_root_thread_id"] is None


def test_local_only_agent_cannot_open_a_cloud_chat(monkeypatch) -> None:
    from infra import fresh_threads
    from infra.local_only import LocalOnlyRefused

    db = _db()
    _thread(db, "root", local_only=1)
    monkeypatch.setattr(
        "infra.local_only.local_only_model_refusal",
        lambda model, *, thread_local_only, **_kwargs: (
            "local_only_requires_local_model" if thread_local_only and model == "cloud" else None
        ),
    )

    with pytest.raises(LocalOnlyRefused):
        fresh_threads.create_fresh_thread(
            user_id="u", thread_id="cloud-chat", model="cloud", actor_thread_id="root", db=db,
        )

    assert db.execute("SELECT id FROM threads WHERE id='cloud-chat'").fetchone() is None


@pytest.mark.parametrize("lineage_field", ["spawned_by", "parent_thread_id"])
def test_child_root_link_covers_each_parent_path(monkeypatch, lineage_field) -> None:
    from infra import fresh_threads

    db = _db()
    _thread(db, "root", local_only=1)
    monkeypatch.setattr("infra.local_only.local_only_model_refusal", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        "connections.account_selection.bind_model_account",
        lambda *_args, **_kwargs: None,
    )

    child = fresh_threads.create_fresh_thread(
        user_id="u", thread_id=f"child-{lineage_field}", model="local", db=db,
        **{lineage_field: "root"},
    )

    assert (child["local_only"], child["local_only_root_thread_id"]) == (0, "root")


def test_non_local_thread_can_switch_to_a_cloud_model(monkeypatch) -> None:
    from infra import model_switches

    db = _db()
    _thread(db, "ordinary")
    monkeypatch.setattr(model_switches, "bind_model_account", lambda *_args, **_kwargs: None)

    request = model_switches.request_switch(
        "ordinary", "cloud", operation_id="op", requested_at="t", db=db,
    )

    assert request.destination_model == "cloud"
    assert db.execute(
        "SELECT pending_model FROM threads WHERE id='ordinary'",
    ).fetchone()[0] == "cloud"


@pytest.mark.asyncio
async def test_local_only_route_requires_a_person_and_owns_the_thread(
    isolated_db,
    monkeypatch,
) -> None:
    from api import thread_mutations
    from auth_fixtures import OTHER_TOKEN, OWNER_SESSION_TOKEN, OWNER_TOKEN
    from infra.database import get_db

    db = get_db()
    try:
        _thread(db, "root", user_id="steve")
        _thread(
            db,
            "child",
            user_id="steve",
            local_only=1,
            inherited_from="root",
            spawned_by="root",
        )
        _thread(db, "foreign", user_id="leksi")
        db.commit()
    finally:
        db.close()

    monkeypatch.setattr(
        "infra.local_only.local_only_model_refusal",
        lambda *_args, **_kwargs: None,
    )
    app = FastAPI()
    app.include_router(thread_mutations.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        assert (await client.put(
            "/api/threads/root/local-only?token=" + OWNER_TOKEN,
            json={"local_only": True},
        )).status_code == 403
        assert (await client.put(
            "/api/threads/root/local-only?token=" + OWNER_SESSION_TOKEN,
            json={"local_only": True},
        )).json()["local_only"] is True
        inherited = await client.put(
            "/api/threads/child/local-only?token=" + OWNER_SESSION_TOKEN,
            json={"local_only": False},
        )
        assert (await client.put(
            "/api/threads/root/local-only?token=" + OWNER_SESSION_TOKEN,
            json={"local_only": False},
        )).json()["local_only"] is False
        invalid = await client.put(
            "/api/threads/root/local-only?token=" + OWNER_SESSION_TOKEN,
            json={"local_only": "yes"},
        )
        foreign = await client.put(
            "/api/threads/foreign/local-only?token=" + OWNER_SESSION_TOKEN,
            json={"local_only": True},
        )

    assert inherited.status_code == 409
    assert inherited.json() == {
        "code": "local_only_set_by_parent",
        "message": (
            "This chat is Local only because the chat that started it is. "
            "Turn Local only off there, or start a new chat."
        ),
    }
    assert invalid.status_code == 422
    assert foreign.status_code == 404
    assert OTHER_TOKEN  # Keep the seeded foreign-account contract explicit.


@pytest.mark.asyncio
async def test_local_only_route_refuses_child_of_non_local_root(isolated_db) -> None:
    from api import thread_mutations
    from auth_fixtures import OWNER_SESSION_TOKEN
    from infra.database import get_db

    db = get_db()
    try:
        _thread(db, "root", user_id="steve")
        _thread(db, "child", user_id="steve", inherited_from="root", spawned_by="root")
        db.commit()
    finally:
        db.close()
    app = FastAPI()
    app.include_router(thread_mutations.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        response = await client.put(
            "/api/threads/child/local-only?token=" + OWNER_SESSION_TOKEN,
            json={"local_only": True},
        )

    assert response.status_code == 409
    assert response.json() == {
        "code": "local_only_set_by_parent",
        "message": "Only the chat that started this chat can change Local only. "
        "Turn Local only on or off there, or start a new chat.",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("column", ["model", "pending_model"])
async def test_local_only_route_refuses_cloud_current_or_pending_model(
    isolated_db,
    monkeypatch,
    column,
) -> None:
    from api import thread_mutations
    from auth_fixtures import OWNER_SESSION_TOKEN
    from infra.database import get_db

    db = get_db()
    try:
        _thread(
            db,
            "root",
            user_id="steve",
            model="cloud" if column == "model" else "local",
            pending_model="cloud" if column == "pending_model" else None,
        )
        db.commit()
    finally:
        db.close()
    monkeypatch.setattr(
        "infra.local_only.local_only_model_refusal",
        lambda model, **_kwargs: (
            "local_only_requires_local_model: choose local" if model == "cloud" else None
        ),
    )
    app = FastAPI()
    app.include_router(thread_mutations.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        response = await client.put(
            "/api/threads/root/local-only?token=" + OWNER_SESSION_TOKEN,
            json={"local_only": True},
        )

    assert response.status_code == 409
    assert response.json() == {
        "code": "local_only_requires_local_model",
        "message": "Switch this chat to a local model first, then turn on Local only.",
    }


@pytest.mark.asyncio
async def test_local_only_route_uses_person_copy_when_no_local_model_is_ready(
    isolated_db, monkeypatch,
) -> None:
    from api import thread_mutations
    from auth_fixtures import OWNER_SESSION_TOKEN
    from infra.database import get_db
    from prompts.cloud_off import NO_LOCAL_READY_TEXT

    db = get_db()
    try:
        _thread(db, "root", user_id="steve", model="cloud")
        db.commit()
    finally:
        db.close()
    monkeypatch.setattr(
        "infra.local_only.local_only_model_refusal",
        lambda *_args, **_kwargs: f"local_only_requires_local_model: {NO_LOCAL_READY_TEXT}",
    )
    app = FastAPI()
    app.include_router(thread_mutations.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        response = await client.put(
            "/api/threads/root/local-only?token=" + OWNER_SESSION_TOKEN,
            json={"local_only": True},
        )

    assert response.status_code == 409
    assert response.json() == {
        "code": "local_only_requires_local_model",
        "message": "No local model is ready. Set up a local model in Settings, then try again.",
    }


@pytest.mark.asyncio
async def test_model_switch_route_returns_local_only_refusal_as_409(
    isolated_db,
    monkeypatch,
) -> None:
    from api import thread_mutations
    from auth_fixtures import OWNER_SESSION_TOKEN
    from infra.database import get_db

    db = get_db()
    try:
        _thread(db, "root", user_id="steve", local_only=1)
        db.commit()
    finally:
        db.close()

    async def refuse(*_args, **_kwargs):
        from infra.local_only import LocalOnlyRefused

        raise LocalOnlyRefused("local_only_requires_local_model: choose local")

    async def prepared(*_args):
        return None

    monkeypatch.setattr("connections.harness_refresh.prepare_selection", prepared)
    monkeypatch.setattr(thread_mutations, "is_dispatchable_model", lambda *_args: True)
    monkeypatch.setattr(thread_mutations, "switch_model", refuse)
    app = FastAPI()
    app.include_router(thread_mutations.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        response = await client.patch(
            "/api/threads/root?token=" + OWNER_SESSION_TOKEN,
            json={"model": "cloud"},
        )

    assert response.status_code == 409
    assert response.json() == {
        "code": "local_only_requires_local_model",
        "message": "This chat is Local only, so choose a local model and try again.",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sender_local_only", "recipient_model", "confirmed", "refused"),
    [
        (True, "cloud", False, True),
        (True, "cloud", True, False),
        (True, None, False, True),
        (True, None, True, False),
        (True, "local", False, False),
        (False, "cloud", False, False),
    ],
)
async def test_delegate_send_enforces_cloud_confirmation_at_send_time(
    isolated_db,
    monkeypatch,
    sender_local_only,
    recipient_model,
    confirmed,
    refused,
) -> None:
    from infra.database import get_db
    from tools import delegate_send

    db = get_db()
    try:
        _thread(db, "sender", user_id="steve", local_only=int(sender_local_only))
        _thread(
            db,
            "recipient",
            user_id="steve",
            model=recipient_model,
            spawned_by="sender",
        )
        db.commit()
    finally:
        db.close()
    sent: list[str] = []

    async def deliver(*_args, **_kwargs):
        sent.append("delivered")
        return {"ok": True}

    async def no_plan(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        "infra.local_only.local_only_model_refusal",
        lambda model, **_kwargs: "cloud" if model == "cloud" else None,
    )
    monkeypatch.setattr(delegate_send, "native_send_scope_error", lambda **_kwargs: None)
    monkeypatch.setattr(delegate_send, "_prepare_plan_task", no_plan)
    monkeypatch.setattr(delegate_send, "_send", deliver)

    result = await delegate_send.DelegateSendTool().execute(
        "recipient",
        "private-key-material",
        _thread_id="sender",
        no_sensitive_content=confirmed,
    )

    assert ("no_sensitive_content: true" in result) is refused
    if refused:
        assert "private-key-material" not in result
    assert sent == ([] if refused else ["delivered"])


def test_post_update_needs_no_local_only_confirmation_flag() -> None:
    from tools.post_update import PostUpdateTool

    assert "no_sensitive_content" not in PostUpdateTool().parameters["properties"]


def test_person_chat_on_a_cloud_model_still_needs_confirmation(monkeypatch) -> None:
    from infra import local_only

    db = _db()
    monkeypatch.setattr(
        local_only,
        "local_only_model_refusal",
        lambda *_args, **_kwargs: "cloud",
    )

    refusal = local_only.send_confirmation_refusal(
        {"local_only": True},
        {"id": "recipient", "title": "Researcher", "model": "cloud", "agent_enabled": 0, "spawned_by": None},
        no_sensitive_content=False,
        db=db,
    )

    assert refusal == (
        "local_only_send_needs_confirm: Researcher runs on a cloud model (cloud). Remove anything "
        "sensitive from the message (keys, secrets, personal data, private file contents), then call "
        "again with no_sensitive_content: true."
    )


def test_send_confirmation_uses_pending_model_when_present(monkeypatch) -> None:
    from infra import local_only

    db = _db()
    monkeypatch.setattr(
        local_only,
        "local_only_model_refusal",
        lambda model, **_kwargs: "cloud" if model == "cloud" else None,
    )

    refusal = local_only.send_confirmation_refusal(
        {"local_only": True},
        {"id": "recipient", "title": "Researcher", "model": "local", "pending_model": "cloud"},
        no_sensitive_content=False,
        db=db,
    )

    assert refusal is not None
    assert "cloud model (cloud)" in refusal


@pytest.mark.asyncio
async def test_delegate_task_relies_on_fresh_thread_admission(monkeypatch) -> None:
    from infra.local_only import LocalOnlyRefused
    from tools import delegate_task

    sent: list[str] = []

    async def reject_cloud_child(**_kwargs):
        raise LocalOnlyRefused("local_only_requires_local_model: choose local")

    async def send(*_args, **_kwargs):
        sent.append("turn")
        return type("Sent", (), {"driver": "turn", "duplicate": False})()

    monkeypatch.setattr(delegate_task, "eligible_delegation_model", lambda selected, **_kwargs: (selected, None))
    monkeypatch.setattr(delegate_task.delegation, "_is_cc_model", lambda _model: False)
    monkeypatch.setattr(delegate_task.delegation, "_is_delisted", lambda _model: False)
    monkeypatch.setattr(delegate_task, "create_visible_fresh_thread", reject_cloud_child)
    monkeypatch.setattr(delegate_task, "send_thread_input", send)

    result = await delegate_task._spawn(
        "parent",
        {"user_id": "steve", "project_id": None},
        "safe task",
        {"model": "cloud"},
    )

    assert result == "Error delegating task: local_only_requires_local_model: choose local"
    assert sent == []
