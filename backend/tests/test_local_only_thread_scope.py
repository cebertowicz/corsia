"""Local-only thread scope admission, inheritance, route, and send contracts."""

from __future__ import annotations

import sqlite3

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
    spawned_by: str | None = None,
    parent_thread_id: str | None = None,
    pending_model: str | None = None,
    project_id: str | None = None,
) -> None:
    db.execute(
        "INSERT INTO threads("
        "id,user_id,model,pending_model,local_only,project_id,spawned_by,parent_thread_id,created_at,updated_at"
        ") VALUES (?,?,?,?,?,?,?,?,?,?)",
        (thread_id, user_id, model, pending_model, local_only, project_id, spawned_by, parent_thread_id, "t", "t"),
    )


def _local_only(db, thread_id: str) -> int:
    return db.execute("SELECT local_only FROM threads WHERE id=?", (thread_id,)).fetchone()[0]


def _skip_account_binding(monkeypatch) -> None:
    # Import model_switches first so its module-level copy holds the real
    # function; otherwise it would keep this stub after monkeypatch undoes it.
    from infra import model_switches

    monkeypatch.setattr(
        "connections.account_selection.bind_model_account", lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(model_switches, "bind_model_account", lambda *_args, **_kwargs: None)


@pytest.fixture
def cloud_named_cloud(monkeypatch):
    """Treat the selector ``cloud`` as a cloud model and one local model as ready."""
    from infra import local_only

    monkeypatch.setattr(local_only, "_is_cloud", lambda selector, **_kwargs: selector == "cloud")
    monkeypatch.setattr(local_only, "_ready_local_selectors", lambda: ("omlx@local/a",))
    _skip_account_binding(monkeypatch)


@pytest.fixture
def events(monkeypatch) -> list[tuple[str, str | None, dict]]:
    # The Local only route copies emit_product_event at import; patch that copy
    # too, importing it first so the copy is restored afterwards.
    from api import thread_mutations

    recorded: list[tuple[str, str | None, dict]] = []

    def record(_category, action, *, thread_id=None, user_id=None, properties=None) -> None:
        recorded.append((action, thread_id, properties or {}))

    monkeypatch.setattr("infra.product_events.emit_product_event", record)
    monkeypatch.setattr(thread_mutations, "emit_product_event", record)
    return recorded


def test_refusal_is_typed_and_not_a_value_error() -> None:
    from infra.local_only import LocalityRefused

    refusal = LocalityRefused("thread_local_only", "codex/gpt-6.1-sol", ())

    assert not isinstance(refusal, ValueError)
    assert (refusal.cause, refusal.selector, refusal.ready_local_selectors) == (
        "thread_local_only", "codex/gpt-6.1-sol", (),
    )


def test_agent_text_renders_from_the_refusal_fields() -> None:
    from infra.local_only import LocalityRefused

    refusal = LocalityRefused("thread_local_only", "codex/gpt-6.1-sol", ("omlx@local/a", "omlx@local/b"))

    assert str(refusal) == (
        "local_only_requires_local_model: codex/gpt-6.1-sol can't run here: "
        "this chat is set to local models only. Use a ready local model: "
        "omlx@local/a, omlx@local/b."
    )


def test_agent_text_with_no_local_model_ready_and_spawn_suffix() -> None:
    from infra.local_only import LOCAL_ONLY_REFUSAL_CODE, LocalityRefused
    from prompts.cloud_off import NO_LOCAL_READY_TEXT

    refusal = str(LocalityRefused("thread_local_only", "codex/gpt-6.1-sol", (), spawn=True))

    # With no local model ready, the recovery is never to stay on the refused model.
    assert refusal == (
        "local_only_requires_local_model: codex/gpt-6.1-sol can't run here: "
        f"this chat is set to local models only. {NO_LOCAL_READY_TEXT} "
        "Local workers run one at a time."
    )
    assert refusal.startswith(f"{LOCAL_ONLY_REFUSAL_CODE}: ")
    assert "thread_local_only" not in refusal


def test_project_cloud_off_is_not_refused_at_bind_time(monkeypatch) -> None:
    from infra import fresh_threads

    db = _db()
    db.execute("INSERT INTO projects (id,name,created_at,updated_at) VALUES ('p','P','t','t')")
    db.execute("INSERT INTO project_inference_policies(project_id,cloud_models_off) VALUES ('p',1)")
    _skip_account_binding(monkeypatch)

    # Cloud-off stays a turn-time hold, as before Local only.
    created = fresh_threads.create_fresh_thread(
        user_id="u", thread_id="cloud-in-cloud-off", model="codex/gpt-6.1-sol", project_id="p", db=db,
    )

    assert created["model"] == "codex/gpt-6.1-sol"


def test_refusals_never_carry_a_secret(monkeypatch) -> None:
    from infra import local_only

    db = _db()
    secret = "sk-test-not-a-real-secret"
    monkeypatch.setattr("llm.resolution.catalog_row", lambda model, **_kwargs: {
        "litellm_model": model, "is_local": False, "api_key": secret,
        "api_base": f"https://user:{secret}@cloud.example/v1",
    })
    monkeypatch.setattr(local_only, "_ready_local_selectors", lambda: ("omlx@local/a",))

    with pytest.raises(local_only.LocalityRefused) as refused:
        local_only.require_local_only_model(
            "codex/gpt-6.1-sol", thread_local_only=True, db=db, spawn=True,
        )
    send_refusal = local_only.send_confirmation_refusal(
        {"local_only": 1},
        {"id": "r", "title": "Reviewer", "model": "codex/gpt-6.1-sol"},
        no_sensitive_content=None,
        db=db,
    )

    assert send_refusal
    assert secret not in str(refused.value)
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


def test_turning_on_covers_family_created_before_and_off_clears_it() -> None:
    from infra.local_only import local_only_sources, write_family_local_only

    db = _db()
    _thread(db, "root")
    _thread(db, "child", spawned_by="root")
    _thread(db, "grandchild", parent_thread_id="child")
    _thread(db, "actor-chat")  # opened by an agent: no family link
    _thread(db, "other")

    write_family_local_only("root", True, "t2", db=db)

    assert [_local_only(db, t) for t in ("root", "child", "grandchild", "actor-chat", "other")] == [
        1, 1, 1, 0, 0,
    ]
    assert local_only_sources(["grandchild"], db=db) == {"grandchild": ("child", "New Chat")}
    write_family_local_only("root", False, "t3", db=db)
    assert [_local_only(db, t) for t in ("root", "child", "grandchild")] == [0, 0, 0]
    assert local_only_sources(["grandchild"], db=db) == {}


def test_create_fresh_thread_refuses_cloud_child_without_writing(cloud_named_cloud, events) -> None:
    from infra import fresh_threads
    from infra.local_only import LocalityRefused

    db = _db()
    _thread(db, "root", local_only=1)

    with pytest.raises(LocalityRefused) as refused:
        fresh_threads.create_fresh_thread(
            user_id="u",
            thread_id="refused-child",
            model="cloud",
            spawned_by="root",
            db=db,
        )

    assert refused.value.spawn is True
    assert db.execute("SELECT id FROM threads WHERE id='refused-child'").fetchone() is None
    assert events == [("local_only.refused", "root", {"cause": "thread_local_only", "spawn": True})]


def test_request_switch_refuses_cloud_for_local_only_thread(cloud_named_cloud) -> None:
    from infra import model_switches
    from infra.local_only import LocalityRefused

    db = _db()
    _thread(db, "thread", local_only=1)

    with pytest.raises(LocalityRefused):
        model_switches.request_switch(
            "thread", "cloud", operation_id="op", requested_at="t", db=db,
        )

    assert db.execute(
        "SELECT pending_model FROM threads WHERE id='thread'",
    ).fetchone()[0] is None


def test_adopt_first_model_refuses_cloud_for_local_only_thread(cloud_named_cloud) -> None:
    from infra import model_switch_inputs
    from infra.database import write_transaction
    from infra.local_only import LocalityRefused

    db = _db()
    _thread(db, "thread", model=None, local_only=1)

    with write_transaction(db, scope="test") as connection:
        with pytest.raises(LocalityRefused):
            model_switch_inputs.adopt_first_model(connection, "thread", "cloud", now="t")

    assert db.execute("SELECT model FROM threads WHERE id='thread'").fetchone()[0] is None


def test_new_threads_copy_local_only_from_every_parent_and_creator(cloud_named_cloud) -> None:
    from infra import fresh_threads

    db = _db()
    _thread(db, "root", local_only=1)
    _thread(db, "child", local_only=1, spawned_by="root")
    _thread(db, "ordinary")

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

    assert [row["local_only"] for row in (grandchild, inherited_later, agent_chat)] == [1, 1, 1]
    assert [row["local_only"] for row in (unaffected, ordinary_agent_chat)] == [0, 0]
    assert _local_only(db, "agent-chat") == 1


def test_local_only_agent_cannot_open_a_cloud_chat(cloud_named_cloud) -> None:
    from infra import fresh_threads
    from infra.local_only import LocalityRefused

    db = _db()
    _thread(db, "root", local_only=1)

    with pytest.raises(LocalityRefused):
        fresh_threads.create_fresh_thread(
            user_id="u", thread_id="cloud-chat", model="cloud", actor_thread_id="root", db=db,
        )

    assert db.execute("SELECT id FROM threads WHERE id='cloud-chat'").fetchone() is None


@pytest.mark.parametrize("lineage_field", ["spawned_by", "parent_thread_id"])
def test_child_inherits_through_each_parent_path(cloud_named_cloud, lineage_field) -> None:
    from infra import fresh_threads

    db = _db()
    _thread(db, "root", local_only=1)

    child = fresh_threads.create_fresh_thread(
        user_id="u", thread_id=f"child-{lineage_field}", model="local", db=db,
        **{lineage_field: "root"},
    )

    assert child["local_only"] == 1


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


def _app(*routers) -> FastAPI:
    app = FastAPI()
    for router in routers:
        app.include_router(router)
    return app


def _seed(*rows: dict) -> None:
    from infra.database import get_db

    db = get_db()
    try:
        for row in rows:
            _thread(db, **row)
        db.commit()
    finally:
        db.close()


def _read_local_only(*thread_ids: str) -> list[int]:
    from infra.database import get_db

    db = get_db()
    try:
        return [_local_only(db, thread_id) for thread_id in thread_ids]
    finally:
        db.close()


@pytest.mark.asyncio
async def test_local_only_route_requires_a_person_and_owns_the_thread(
    isolated_db, cloud_named_cloud, events,
) -> None:
    from api import thread_mutations
    from auth_fixtures import OWNER_SESSION_TOKEN, OWNER_TOKEN

    # The child exists before the root is turned on: no backfill is needed.
    _seed(
        {"thread_id": "root", "user_id": "steve"},
        {"thread_id": "child", "user_id": "steve", "spawned_by": "root"},
        {"thread_id": "foreign", "user_id": "leksi"},
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(thread_mutations.router)), base_url="http://test",
    ) as client:
        assert (await client.put(
            "/api/threads/root/local-only?token=" + OWNER_TOKEN,
            json={"local_only": True},
        )).status_code == 403
        on_response = await client.put(
            "/api/threads/root/local-only?token=" + OWNER_SESSION_TOKEN,
            json={"local_only": True},
        )
        assert on_response.json()["local_only"] is True
        on = _read_local_only("root", "child")
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

    assert on == [1, 1]
    assert {row["id"]: row for row in on_response.json()["threads"]}["child"] == {
        "id": "child",
        "local_only": 1,
        "local_only_effective": True,
        "local_only_from_project": False,
        "local_only_inherited_from_thread_id": "root",
        "local_only_inherited_from_title": "New Chat",
    }
    assert _read_local_only("root", "child") == [0, 0]
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
    assert [(action, props) for action, _thread_id, props in events] == [
        ("local_only.changed", {"local_only": True}),
        ("local_only.changed", {"local_only": False}),
    ]


@pytest.mark.asyncio
async def test_local_only_route_returns_project_cause_after_turning_off(
    isolated_db, cloud_named_cloud,
) -> None:
    from api import thread_mutations
    from auth_fixtures import OWNER_SESSION_TOKEN
    from infra.database import get_db

    db = get_db()
    try:
        db.execute("INSERT INTO projects(id,name,created_at,updated_at) VALUES ('p','P','t','t')")
        db.execute("INSERT INTO project_inference_policies(project_id,cloud_models_off) VALUES ('p',1)")
        db.commit()
    finally:
        db.close()
    _seed({"thread_id": "root", "user_id": "steve", "local_only": 1, "project_id": "p"})

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(thread_mutations.router)), base_url="http://test",
    ) as client:
        response = await client.put(
            "/api/threads/root/local-only?token=" + OWNER_SESSION_TOKEN,
            json={"local_only": False},
        )

    assert response.status_code == 200
    assert response.json()["threads"] == [{
        "id": "root",
        "local_only": 0,
        "local_only_effective": True,
        "local_only_from_project": True,
        "local_only_inherited_from_thread_id": None,
        "local_only_inherited_from_title": None,
    }]


@pytest.mark.asyncio
async def test_local_only_route_turns_on_a_child_of_a_non_local_root(
    isolated_db, cloud_named_cloud,
) -> None:
    from api import thread_mutations
    from auth_fixtures import OWNER_SESSION_TOKEN

    _seed(
        {"thread_id": "root", "user_id": "steve"},
        {"thread_id": "child", "user_id": "steve", "spawned_by": "root"},
        {"thread_id": "worker", "user_id": "steve", "spawned_by": "child"},
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(thread_mutations.router)), base_url="http://test",
    ) as client:
        on = await client.put(
            "/api/threads/child/local-only?token=" + OWNER_SESSION_TOKEN,
            json={"local_only": True},
        )
        after_on = _read_local_only("root", "child", "worker")
        off = await client.put(
            "/api/threads/child/local-only?token=" + OWNER_SESSION_TOKEN,
            json={"local_only": False},
        )

    assert (on.status_code, off.status_code) == (200, 200)
    assert after_on == [0, 1, 1]
    assert _read_local_only("root", "child", "worker") == [0, 0, 0]


@pytest.mark.asyncio
@pytest.mark.parametrize("column", ["model", "pending_model"])
async def test_local_only_route_refuses_cloud_current_or_pending_model(
    isolated_db, cloud_named_cloud, column,
) -> None:
    from api import thread_mutations
    from auth_fixtures import OWNER_SESSION_TOKEN

    _seed({
        "thread_id": "root",
        "user_id": "steve",
        "model": "cloud" if column == "model" else "local",
        "pending_model": "cloud" if column == "pending_model" else None,
    })
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(thread_mutations.router)), base_url="http://test",
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
    assert _read_local_only("root") == [0]


@pytest.mark.asyncio
async def test_local_only_route_uses_person_copy_when_no_local_model_is_ready(
    isolated_db, cloud_named_cloud, monkeypatch,
) -> None:
    from api import thread_mutations
    from auth_fixtures import OWNER_SESSION_TOKEN

    _seed({"thread_id": "root", "user_id": "steve", "model": "cloud"})
    monkeypatch.setattr("infra.local_only._ready_local_selectors", lambda: ())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(thread_mutations.router)), base_url="http://test",
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
async def test_model_switch_route_refuses_through_the_real_switch(
    isolated_db, cloud_named_cloud, monkeypatch, events,
) -> None:
    from api import thread_mutations
    from auth_fixtures import OWNER_SESSION_TOKEN

    # The thread already has a model, so the switch takes request_switch's path.
    _seed({"thread_id": "root", "user_id": "steve", "model": "local", "local_only": 1})

    async def prepared(*_args):
        return None

    monkeypatch.setattr("connections.harness_refresh.prepare_selection", prepared)
    monkeypatch.setattr(thread_mutations, "is_dispatchable_model", lambda *_args: True)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(thread_mutations.router), raise_app_exceptions=False),
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
    assert events == [("local_only.refused", "root", {"cause": "thread_local_only", "spawn": False})]


@pytest.mark.asyncio
async def test_delegate_set_model_tool_returns_the_agent_text(monkeypatch) -> None:
    from infra.local_only import LocalityRefused
    from tools import delegation
    from tools.delegation_model import DelegateSetModelTool

    refusal = LocalityRefused("thread_local_only", "cloud", ("omlx@local/a",))

    async def refuse(*_args, **_kwargs):
        raise refusal

    async def in_db(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(delegation, "_require_context", lambda _kwargs: ("caller", {"user_id": "steve"}))
    monkeypatch.setattr(delegation, "_delegation_target_exists", lambda *_args: True)
    monkeypatch.setattr(delegation, "eligible_delegation_model", lambda model, **_kwargs: (model, None))
    monkeypatch.setattr("tools.delegation_model.run_in_db", in_db)
    monkeypatch.setattr("thread.model.switch_model", refuse)

    result = await DelegateSetModelTool().execute("worker", "cloud", _thread_id="caller")

    assert result == f"Error: model switch rejected: {refusal}"


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
        "infra.local_only._is_cloud", lambda selector, **_kwargs: selector == "cloud",
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
    monkeypatch.setattr(local_only, "_is_cloud", lambda *_args, **_kwargs: True)

    refusal = local_only.send_confirmation_refusal(
        {"local_only": True},
        {"id": "recipient", "title": "Researcher", "model": "cloud", "agent_enabled": 0, "spawned_by": None},
        no_sensitive_content=False,
        db=db,
    )

    assert refusal == (
        "local_only_send_needs_confirm: Researcher runs on a cloud model (cloud). Remove anything "
        "sensitive from the message (keys, secrets, personal data, private file contents), then call "
        "again with no_sensitive_content: true (on `corsia delegate send`, add --no-sensitive-content)."
    )


def test_send_confirmation_uses_pending_model_when_present(monkeypatch) -> None:
    from infra import local_only

    db = _db()
    monkeypatch.setattr(local_only, "_is_cloud", lambda selector, **_kwargs: selector == "cloud")

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
    from infra.local_only import LocalityRefused
    from tools import delegate_task

    sent: list[str] = []
    refusal = LocalityRefused("thread_local_only", "cloud", ("omlx@local/a",), spawn=True)

    async def reject_cloud_child(**_kwargs):
        raise refusal

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

    assert result == f"Error delegating task: {refusal}"
    assert sent == []


def test_send_confirmation_records_the_refusal_and_the_confirmation(monkeypatch, events) -> None:
    from infra import local_only

    db = _db()
    monkeypatch.setattr(local_only, "_is_cloud", lambda *_args, **_kwargs: True)
    sender = {"id": "sender", "local_only": True}
    recipient = {"id": "recipient", "title": "Researcher", "model": "cloud"}

    assert local_only.send_confirmation_refusal(sender, recipient, no_sensitive_content=None, db=db)
    assert local_only.send_confirmation_refusal(sender, recipient, no_sensitive_content=True, db=db) is None
    assert events == [
        ("local_only.send_needs_confirm", "sender", {"recipient_thread_id": "recipient"}),
        ("local_only.send_confirmed", "sender", {"recipient_thread_id": "recipient"}),
    ]
