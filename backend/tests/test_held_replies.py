"""A held reply: a Local only agent's message to a cloud-bound thread waits for the person.

The text stored at the hold is the text delivered on approval; decline delivers nothing; a retry
with the same key returns the first hold; no setting releases a hold; a worker's final result is
unheld; and a worker can't start inside a Local only project its parent is not in.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from delegation_fixtures import _TOKEN, client, deleg_db, insert_thread  # noqa: F401

CLOUD = "anthropic/claude-3-5-sonnet"


@pytest.fixture
def held_client(client, deleg_db, monkeypatch):
    """The delegation client plus the decision routes, a local-only sender and a cloud-bound asker."""
    import main
    from starlette.exceptions import HTTPException as StarletteHTTPException
    from api import held_replies as held_api
    from infra import local_only

    client.app.add_exception_handler(StarletteHTTPException, main._emit_http_error_api)
    monkeypatch.setattr(held_api, "authenticate", lambda t: {"id": "u-test", "token_kind": "session"} if t == _TOKEN else None)
    client.app.include_router(held_api.router)
    deleg_db.execute("UPDATE threads SET local_only=1 WHERE id='sender-self'")
    insert_thread(deleg_db, "asker", model=CLOUD, title="Researcher")
    monkeypatch.setattr(local_only, "_is_cloud", lambda selector, **_kwargs: selector == CLOUD)
    return client


@pytest.fixture
def seen(monkeypatch):
    """What the sender is told and what is delivered, in order."""
    from events import delivery
    from tools import delegate_send

    log: dict[str, list] = {"delivered": [], "told": []}

    async def deliver(_thread_id, _target, text, *_args, **_kwargs):
        log["delivered"].append(text)
        return {"ok": True, "driver": "turn"}

    async def told(thread_id, event, summary, _details=None, **kwargs):
        log["told"].append((thread_id, event, summary, kwargs.get("chip")))
        return True

    async def no_plan(*_args, **_kwargs):
        return None

    monkeypatch.setattr(delegate_send, "_send", deliver)
    monkeypatch.setattr(delegate_send, "_prepare_plan_task", no_plan)
    monkeypatch.setattr(delegate_send, "native_send_scope_error", lambda **_kwargs: None)
    monkeypatch.setattr(delivery, "deliver_to_thread", told)
    return log


async def _send_by_tool(message: str = "the answer", **kwargs) -> dict:
    from tools.delegate_send import DelegateSendTool

    result = await DelegateSendTool().execute("asker", message, _thread_id="sender-self", **kwargs)
    return json.loads(result)


def _post(held_client, held_id: str, action: str):
    return held_client.post(f"/api/held-replies/{held_id}/{action}", json={"token": _TOKEN})


def _rows(db, *columns: str) -> list[tuple]:
    return [tuple(r) for r in db.execute(f"SELECT {', '.join(columns)} FROM held_replies").fetchall()]


@pytest.mark.asyncio
async def test_the_tool_holds_and_approval_delivers_the_stored_text_once(held_client, deleg_db, seen):
    held = await _send_by_tool("the answer", idempotency_key="k1")

    assert held["status"] == "held"
    assert seen["delivered"] == []
    assert _rows(deleg_db, "text", "state") == [("the answer", "held")]

    approved = _post(held_client, held["held_reply_id"], "approve")

    assert approved.status_code == 200
    assert approved.json()["state"] == "sent"
    assert seen["delivered"] == ["the answer"]
    assert _rows(deleg_db, "state", "text") == [("sent", "")]
    assert [(event, chip) for _t, event, _s, chip in seen["told"]] == [
        ("held_reply.sent", "The reply to the Researcher agent was sent."),
    ]
    assert _post(held_client, held["held_reply_id"], "approve").status_code == 409
    assert seen["delivered"] == ["the answer"]


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["approve", "decline"])
@pytest.mark.parametrize("token_kind", ["api", "service"])
async def test_machine_credentials_cannot_decide(held_client, deleg_db, seen, monkeypatch, action, token_kind):
    from api import held_replies as held_api
    from infra.accounts import issue_token
    from infra.auth import authenticate

    held = await _send_by_tool()
    credential, _ = issue_token("u-test", kind=token_kind, db=deleg_db)
    monkeypatch.setattr(held_api, "authenticate", authenticate)
    response = held_client.post(f"/api/held-replies/{held['held_reply_id']}/{action}", json={"token": credential})
    assert response.status_code == 403
    assert _rows(deleg_db, "state") == [("held",)]
    assert seen["delivered"] == []


@pytest.mark.asyncio
async def test_person_session_can_approve(held_client, deleg_db, seen, monkeypatch):
    from api import held_replies as held_api
    from infra.accounts import issue_token
    from infra.auth import authenticate

    held = await _send_by_tool()
    credential, _ = issue_token("u-test", kind="session", db=deleg_db)
    monkeypatch.setattr(held_api, "authenticate", authenticate)
    response = held_client.post(f"/api/held-replies/{held['held_reply_id']}/approve", json={"token": credential})
    assert response.status_code == 200
    assert response.json()["state"] == "sent"


@pytest.mark.asyncio
async def test_cancelled_decline_finishes_notifying_sender_and_asker(held_client, deleg_db, seen, monkeypatch):
    import asyncio
    from api import held_replies as held_api

    held = await _send_by_tool()
    claimed, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_claim, original_note = held_api._claim, held_api.note_asker

    async def claim(*args):
        result = await original_claim(*args)
        claimed.set()
        await release.wait()
        return result

    async def note(*args):
        await original_note(*args)
        finished.set()

    monkeypatch.setattr(held_api, "_claim", claim)
    monkeypatch.setattr(held_api, "note_asker", note)
    request = asyncio.create_task(held_api.decline(held["held_reply_id"], held_api.DecisionBody(token=_TOKEN)))
    await asyncio.wait_for(claimed.wait(), 2)
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    release.set()
    await asyncio.wait_for(finished.wait(), 2)
    assert _rows(deleg_db, "state") == [("declined",)]
    assert [event for _t, event, _s, _c in seen["told"]] == ["held_reply.declined"]
    rows = deleg_db.execute("SELECT metadata FROM messages WHERE thread_id='asker'").fetchall()
    assert any(json.loads(row[0] or "{}").get("event_type") == "held_reply.declined" for row in rows)


@pytest.mark.asyncio
async def test_decline_delivers_nothing_and_tells_the_sender(held_client, deleg_db, seen):
    held = await _send_by_tool("the answer")

    declined = _post(held_client, held["held_reply_id"], "decline")

    assert declined.status_code == 200
    assert seen["delivered"] == []
    assert _rows(deleg_db, "state") == [("declined",)]
    assert [event for _t, event, _s, _c in seen["told"]] == ["held_reply.declined"]
    assert _post(held_client, held["held_reply_id"], "approve").status_code == 409
    assert seen["delivered"] == []


@pytest.mark.asyncio
async def test_a_retry_with_the_same_key_returns_the_existing_hold(deleg_db, held_client, seen):
    first = await _send_by_tool("the answer", idempotency_key="same")
    again = await _send_by_tool("the answer", idempotency_key="same")

    assert again["held_reply_id"] == first["held_reply_id"]
    assert len(_rows(deleg_db, "id")) == 1


@pytest.mark.asyncio
async def test_api_hold_uses_api_delivery_attribution(deleg_db, held_client, monkeypatch):
    from infra import held_replies, send_service
    from infra.send_types import SendResult

    async def delivered(*_args, **_kwargs):
        return SendResult(driver="test")

    monkeypatch.setattr(send_service, "_deliver_admission", delivered)
    held = held_replies.hold(user_id="u-test", sender_thread_id="sender-self", recipient_thread_id="asker",
                             text="api answer", via="api", db=deleg_db)
    assert _post(held_client, held["id"], "approve").json()["state"] == "sent"
    row = deleg_db.execute("SELECT metadata FROM messages WHERE thread_id='asker' AND content='api answer'").fetchone()
    metadata = json.loads(row["metadata"])
    assert metadata["from_thread"] == "sender-self"
    assert metadata["from_title"] == "Sender Thread"
    assert metadata["scope"] == "workspace"


@pytest.mark.asyncio
async def test_real_app_delivery_failure_shares_one_reference(deleg_db, held_client, seen, monkeypatch, caplog):
    import logging
    import main
    from api import held_replies as held_api
    from infra import failure_reference
    from tools import delegate_send

    held = await _send_by_tool()
    minted = []
    events = []

    def mint():
        minted.append("K7QM2X")
        return minted[-1]

    async def refused(*_args, **_kwargs):
        return "Error: target rejected this send"

    monkeypatch.setattr(held_api, "new_error_reference", mint)
    monkeypatch.setattr(failure_reference, "new_error_reference", mint)
    monkeypatch.setattr(delegate_send, "_send", refused)
    monkeypatch.setattr(main, "emit_product_event", lambda *args, **kwargs: events.append((args, kwargs)))
    caplog.set_level(logging.ERROR, logger=main.logger.name)
    app_client = TestClient(main.app, raise_server_exceptions=False)
    try:
        result = _post(app_client, held["held_reply_id"], "approve")
    finally:
        app_client.close()

    assert result.status_code == 502
    assert result.json()["reference"] == "K7QM2X"
    assert _rows(deleg_db, "error_ref") == [("K7QM2X",)]
    assert minted == ["K7QM2X"]
    assert "API error reference=K7QM2X" in caplog.text
    assert any(args == ("error", "api") and kwargs["properties"]["reference"] == "K7QM2X" for args, kwargs in events)


@pytest.mark.asyncio
async def test_delivery_refusal_is_retryable_and_logged(deleg_db, held_client, seen, monkeypatch):
    from api import held_replies as held_api
    from infra.failure_reference import REFERENCE
    from plans.held_attention import queue_rows
    from tools import delegate_send

    async def refused(*_args, **_kwargs):
        return "Error: target rejected this send"

    held = await _send_by_tool()
    warnings = []
    monkeypatch.setattr(held_api.logger, "warning", lambda *args: warnings.append(args))
    monkeypatch.setattr(delegate_send, "_send", refused)
    result = _post(held_client, held["held_reply_id"], "approve")
    assert result.status_code == 502
    assert result.json()["detail"] == "Couldn’t send the reply to the Researcher agent because of an unexpected error. Approve once to try again."
    assert _rows(deleg_db, "state", "text") == [("held", "the answer")]
    ref = result.json()["reference"]
    assert REFERENCE.fullmatch(ref)
    assert _rows(deleg_db, "error_ref") == [(ref,)]
    assert queue_rows(deleg_db, "u-test")[0]["detail"]["error_ref"] == ref
    assert any(held["held_reply_id"] in args and "Error: target rejected this send" in args and ref in args for args in warnings)


@pytest.mark.asyncio
async def test_auto_approve_does_not_release_a_hold(deleg_db, held_client, seen):
    from infra import settings

    settings.save_setting_merge(
        "agents", {"defaults": {"host_access_auto_approve": True}}, db=deleg_db,
    )
    assert settings.get_host_access_auto_approve() is True

    held = await _send_by_tool("the answer")

    assert held["status"] == "held"
    assert seen["delivered"] == []
    assert _rows(deleg_db, "state") == [("held",)]


@pytest.mark.asyncio
async def test_the_asker_sees_a_note_then_a_receipt_and_is_not_sent_the_reply_early(
    deleg_db, held_client, seen,
):
    held = await _send_by_tool("the answer")
    notes = deleg_db.execute(
        "SELECT metadata FROM messages WHERE thread_id='asker' AND role='system'",
    ).fetchall()
    assert [json.loads(n["metadata"])["type"] for n in notes] == ["held_reply_note"]

    _post(held_client, held["held_reply_id"], "approve")
    kinds = [
        json.loads(n["metadata"]).get("event_type") or json.loads(n["metadata"])["type"]
        for n in deleg_db.execute(
            "SELECT metadata FROM messages WHERE thread_id='asker' AND role='system' ORDER BY rowid",
        )
    ]

    assert kinds == ["held_reply_note", "held_reply.sent"]


@pytest.mark.asyncio
async def test_a_missing_recipient_is_not_sent_and_a_failed_delivery_returns_to_held(
    deleg_db, held_client, seen, monkeypatch,
):
    from api import held_replies as held_api
    from infra.failure_reference import REFERENCE
    from tools import delegate_send

    failed = await _send_by_tool("first")
    errors = []
    monkeypatch.setattr(held_api.logger, "exception", lambda *args: errors.append(args))

    async def broken(*_args, **_kwargs):
        raise RuntimeError("provider down")

    monkeypatch.setattr(delegate_send, "_send", broken)
    refused = _post(held_client, failed["held_reply_id"], "approve")

    assert refused.status_code == 502
    ref = refused.json()["reference"]
    assert REFERENCE.fullmatch(ref)
    assert any(failed["held_reply_id"] in args and ref in args and str(args[-1]) == "provider down" for args in errors)
    assert _rows(deleg_db, "state", "error_ref") == [("held", ref)]
    assert "Approve once to try again" in _rows(deleg_db, "error")[0][0]

    deleg_db.execute("DELETE FROM threads WHERE id='asker'")
    deleg_db.commit()
    gone = _post(held_client, failed["held_reply_id"], "approve")

    assert gone.json()["state"] == "not_sent"
    assert _rows(deleg_db, "state") == [("not_sent",)]


@pytest.mark.asyncio
async def test_explicit_and_automatic_completion_delivers_without_hold(deleg_db, held_client, monkeypatch):
    from infra import send_service
    from infra.send_types import SendResult
    from infra.thread_driver import _parent_completion
    from test_clean_turn_terminal import _running_turn
    from tools.delegate_send import DelegateSendTool

    async def delivered(*_args, **_kwargs):
        return SendResult(driver="test")

    monkeypatch.setattr(send_service, "_deliver_admission", delivered)
    deleg_db.execute("UPDATE threads SET spawned_by='asker', parent_thread_id='asker' WHERE id='sender-self'")
    deleg_db.commit()
    _running_turn(deleg_db, "sender-self", "child-turn")
    first = json.loads(await DelegateSendTool().execute(
        "asker", "final result", intent="completion", _thread_id="sender-self",
    ))
    assert first.get("status") != "held"
    assert _rows(deleg_db, "id") == []
    assert deleg_db.execute(
        "SELECT count(*) FROM messages WHERE thread_id='asker' AND json_extract(metadata,'$.completion')=1"
    ).fetchone()[0] == 1
    deleg_db.execute(
        "INSERT INTO messages (id,thread_id,role,content,metadata,created_at,delivery_state,seq) "
        "VALUES ('answer','sender-self','assistant','final result',?,'t3','delivered',2)",
        (json.dumps({"turn_id": "child-turn"}),),
    )
    assert _parent_completion(deleg_db, "child-turn", "sender-self") is None
    assert _rows(deleg_db, "id") == []
    assert deleg_db.execute(
        "SELECT count(*) FROM messages WHERE thread_id='asker' AND json_extract(metadata,'$.completion')=1"
    ).fetchone()[0] == 1


def test_the_hold_names_a_worker_by_its_visible_parent(deleg_db, held_client):
    from infra.held_reply_messages import visible_name

    insert_thread(deleg_db, "worker", spawned_by="sender-self", model=CLOUD, title="Worker 7")

    assert visible_name("worker", db=deleg_db) == "Sender Thread"


def test_a_worker_cannot_start_inside_a_local_only_project_its_parent_is_not_in(
    deleg_db, held_client,
):
    deleg_db.execute("INSERT INTO projects (id,name,created_at,updated_at) VALUES ('wall','Wall','t','t')")
    deleg_db.execute(
        "INSERT INTO project_inference_policies(project_id,cloud_models_off) VALUES ('wall',1)",
    )
    deleg_db.commit()

    outside = held_client.post(
        "/api/delegation/threads", params={"token": _TOKEN},
        json={"model": "omlx@local/qwen", "thread_kind": "subagent", "project_id": "wall",
              "spawned_by": "sender-self"},
    )
    chat = held_client.post(
        "/api/delegation/threads", params={"token": _TOKEN},
        json={"model": "omlx@local/qwen", "project_id": "wall", "from_thread_id": "sender-self"},
    )

    assert outside.status_code == 403
    assert "Local only project" in outside.json()["detail"]
    assert chat.status_code == 200


@pytest.mark.asyncio
async def test_the_create_tool_refuses_the_same_worker_and_allows_one_inside_the_project(
    deleg_db, held_client, monkeypatch,
):
    from tools import delegation
    from tools.delegation import DelegateCreateThreadTool

    monkeypatch.setattr(delegation, "eligible_delegation_model", lambda model, **_kwargs: (model, None))

    deleg_db.execute("INSERT INTO projects (id,name,created_at,updated_at) VALUES ('wall','Wall','t','t')")
    deleg_db.execute(
        "INSERT INTO project_inference_policies(project_id,cloud_models_off) VALUES ('wall',1)",
    )
    insert_thread(deleg_db, "insider", model="omlx@local/qwen", project_id="wall")
    tool = DelegateCreateThreadTool()
    spec = {"model": "omlx@local/qwen", "thread_kind": "subagent", "project_id": "wall"}

    refused = await tool.execute(**spec, _thread_id="sender-self")
    allowed = await tool.execute(**spec, _thread_id="insider")

    assert refused.startswith("Error creating delegated thread:")
    assert "Local only project" in refused
    assert json.loads(allowed)["ok"] is True


def test_the_dock_row_and_chats_list_item_come_from_the_same_pending_rows(deleg_db, held_client):
    from plans import held_attention

    insert_thread(deleg_db, "worker", spawned_by="sender-self", model="omlx@local/qwen", title="Hidden worker")
    import infra.held_replies as held_replies

    held_replies.hold(
        user_id="u-test", sender_thread_id="worker", recipient_thread_id="asker",
        text="from the worker", via="tool", db=deleg_db,
    )

    [row] = held_attention.queue_rows(deleg_db, "u-test")
    chats = held_attention.items_by_thread(deleg_db, "u-test")

    assert row["detail"]["source"]["thread_id"] == "sender-self"
    assert row["detail"]["title"] == "Send Sender Thread's reply to the Researcher agent on Anthropic?"
    assert list(chats) == ["sender-self"]
    assert chats["sender-self"][0]["kind"] == "hold"
    assert "text" not in held_attention.pending(deleg_db, "u-test", with_text=False)[0]
    assert held_attention.pending(deleg_db, "u-test")[0]["text"] == "from the worker"


@pytest.mark.asyncio
async def test_decision_refreshes_visible_parent_chat(deleg_db, held_client, seen, monkeypatch):
    from infra import held_replies, state

    insert_thread(deleg_db, "worker", spawned_by="sender-self", model="omlx@local/qwen", local_only=1)
    held = held_replies.hold(user_id="u-test", sender_thread_id="worker", recipient_thread_id="asker",
                             text="answer", via="tool", db=deleg_db)
    events = []
    monkeypatch.setattr(state, "broadcast_to_user", lambda _user, event: events.append(event))
    assert _post(held_client, held["id"], "decline").json()["state"] == "declined"
    assert events[-1]["thread_ids"] == ["worker", "sender-self"]


@pytest.mark.parametrize(("provider", "harness", "expected"), [
    ("external", "native", "external"),
    ("claude-code", "claude-code", "Anthropic"),
    ("codex", "codex", "OpenAI"),
])
def test_provider_label_uses_vendor_or_catalog_name(deleg_db, held_client, provider, harness, expected):
    from infra import held_replies
    from plans.held_attention import pending

    selector = f"{provider}@server/model"
    deleg_db.execute("UPDATE models SET provider=?,harness=?,litellm_model=? WHERE id=?",
                     (provider, harness, selector, CLOUD))
    deleg_db.execute("UPDATE threads SET model=? WHERE id='asker'", (selector,))
    held_replies.hold(user_id="u-test", sender_thread_id="sender-self", recipient_thread_id="asker",
                      text="answer", via="tool", db=deleg_db)
    assert pending(deleg_db, "u-test")[0]["provider_label"] == expected


@pytest.mark.asyncio
async def test_recovery_returns_interrupted_approval_to_held(deleg_db, held_client, seen):
    from infra import held_replies
    from infra.held_reply_messages import RESTART_ERROR

    held = await _send_by_tool()
    held_replies.settle(held["held_reply_id"], expect="held", state="approved")
    held_replies.recover_interrupted()
    assert _rows(deleg_db, "state", "error") == [("held", RESTART_ERROR)]
    assert _post(held_client, held["held_reply_id"], "approve").json()["state"] == "sent"


@pytest.mark.asyncio
async def test_approval_finishes_after_request_is_cancelled(deleg_db, held_client, seen, monkeypatch):
    import asyncio
    from api import held_replies as held_api

    held = await _send_by_tool()
    started, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_note = held_api.note_asker

    async def deliver(*_args):
        started.set()
        await release.wait()
        return {"ok": True}

    async def note(*args):
        await original_note(*args)
        finished.set()

    monkeypatch.setattr(held_api, "_deliver", deliver)
    monkeypatch.setattr(held_api, "note_asker", note)
    request = asyncio.create_task(held_api.approve(held["held_reply_id"], held_api.DecisionBody(token=_TOKEN)))
    await asyncio.wait_for(started.wait(), 2)
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    release.set()
    await asyncio.wait_for(finished.wait(), 2)
    assert _rows(deleg_db, "state", "text") == [("sent", "")]


@pytest.mark.asyncio
async def test_cancellation_during_claim_still_finishes(deleg_db, held_client, seen, monkeypatch):
    import asyncio
    from api import held_replies as held_api

    held = await _send_by_tool()
    claimed, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_claim, original_finish = held_api._claim, held_api._finish_approval

    async def claim(*args):
        result = await original_claim(*args)
        claimed.set()
        await release.wait()
        return result

    async def finish(*args):
        result = await original_finish(*args)
        finished.set()
        return result

    monkeypatch.setattr(held_api, "_claim", claim)
    monkeypatch.setattr(held_api, "_finish_approval", finish)
    request = asyncio.create_task(held_api.approve(held["held_reply_id"], held_api.DecisionBody(token=_TOKEN)))
    await asyncio.wait_for(claimed.wait(), 2)
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    release.set()
    await asyncio.wait_for(finished.wait(), 2)
    assert _rows(deleg_db, "state", "text") == [("sent", "")]


@pytest.mark.asyncio
async def test_recovered_approval_deduplicates_an_admitted_delivery(deleg_db, held_client, monkeypatch):
    from api import held_replies as held_api
    from events import delivery
    from infra import held_replies, send_service
    from infra.send_types import SendResult
    from tools import delegate_send

    async def no_plan(*_args, **_kwargs):
        return None

    async def delivered(*_args, **_kwargs):
        return SendResult(driver="test")

    async def told(*_args, **_kwargs):
        return True

    monkeypatch.setattr(delegate_send, "_prepare_plan_task", no_plan)
    monkeypatch.setattr(send_service, "_deliver_admission", delivered)
    monkeypatch.setattr(delivery, "deliver_to_thread", told)
    held = await _send_by_tool()
    held_id = held["held_reply_id"]
    row, sender, recipient, _name = held_api._load(held_id, "u-test")
    assert held_replies.settle(held_id, expect="held", state="approved")
    assert isinstance(await held_api._deliver(row, sender, recipient), dict)
    held_replies.recover_interrupted()
    assert _post(held_client, held_id, "approve").json()["state"] == "sent"
    rows = deleg_db.execute(
        "SELECT id FROM messages WHERE thread_id='asker' AND role='user' AND content='the answer'",
    ).fetchall()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_moved_recipient_is_not_sent(deleg_db, held_client, seen):
    held = await _send_by_tool()
    deleg_db.execute("INSERT INTO projects (id,name,created_at,updated_at) VALUES ('other','Other','t','t')")
    deleg_db.execute("UPDATE threads SET project_id='other' WHERE id='asker'")
    deleg_db.commit()
    result = _post(held_client, held["held_reply_id"], "approve")
    assert result.json()["state"] == "not_sent"
    assert "is no longer reachable" in result.json()["line"]
    assert _rows(deleg_db, "state") == [("not_sent",)]
    assert seen["delivered"] == []
    assert "is no longer in a scope" in seen["told"][-1][2]
    from infra.held_replies import get
    from infra.held_reply_messages import hold_response
    retry = await hold_response(get(held["held_reply_id"], "u-test", db=deleg_db))
    assert retry["status"] == "not_sent"
    assert "is no longer in a scope" in retry["message"]
    rows = deleg_db.execute("SELECT content FROM messages WHERE thread_id='asker' AND role='system' ORDER BY rowid").fetchall()
    assert "could not be sent" in rows[-1][0]


@pytest.mark.asyncio
async def test_sender_delivery_false_is_logged(monkeypatch):
    from events import delivery
    from infra.held_reply_messages import tell_sender
    from loguru import logger

    async def refused(*_args, **_kwargs):
        return False

    messages = []
    sink = logger.add(lambda message: messages.append(str(message)), level="WARNING")
    monkeypatch.setattr(delivery, "deliver_to_thread", refused)
    try:
        assert await tell_sender({"id": "hold-1", "sender_thread_id": "sender"}, "sent", recipient="R") is False
    finally:
        logger.remove(sink)
    assert any("hold-1" in message and "sent" in message for message in messages)
