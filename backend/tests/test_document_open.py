from __future__ import annotations

import asyncio
import json

import pytest

from delegation_fixtures import deleg_db  # noqa: F401


def test_document_open_tool_surface_is_trigger_first():
    from tools.document_open import DocumentOpenTool

    tool = DocumentOpenTool()

    assert tool.name == "document_open"
    assert tool.description == "Show a document to the user in this thread's canvas"
    assert tool.parameters["required"] == []
    assert set(tool.parameters["properties"]) == {"path", "artifact_id"}


@pytest.mark.asyncio
async def test_document_open_already_open_precedes_another_eligible_session(deleg_db, monkeypatch):
    from infra import config, presence, state
    from registry.service import register_write_strict
    from tools import document_open
    from tools.document_open import DocumentOpenTool

    path = config.WORKSPACE / "shared" / "open-me.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# Open me\n")
    artifact = register_write_strict("sender-self", str(path), db=deleg_db, notify=False)
    snapshots: list[tuple[str, str, str]] = []
    sent: list[tuple[str, frozenset[str], dict]] = []
    events: list[tuple[str, str, dict]] = []

    def snapshot(user_id: str, thread_id: str, artifact_id: str):
        snapshots.append((user_id, thread_id, artifact_id))
        return presence.DocumentOpenSnapshot("here", frozenset({"session-a"}), True)

    def send(thread_id: str, sessions: frozenset[str], message: dict) -> int:
        sent.append((thread_id, sessions, message))
        return 1

    monkeypatch.setattr(presence, "document_open_snapshot", snapshot)
    monkeypatch.setattr(state, "send_to_thread_sessions", send)
    monkeypatch.setattr(document_open, "emit_product_event", lambda *args, **kwargs: events.append((*args, kwargs)))

    result = json.loads(await DocumentOpenTool().execute(path=str(path), _thread_id="sender-self"))

    assert snapshots == [("u-test", "sender-self", artifact["id"])]
    assert sent == []
    assert result == {"outcome": "already_open", "reason": None, "count": 0}
    assert events == [("document", "agent_open", {
        "thread_id": "sender-self", "user_id": "u-test",
        "properties": {"outcome": "already_open", "reason": None, "count": 0},
    })]


@pytest.mark.asyncio
async def test_document_open_returns_link_when_session_handoff_is_dropped(deleg_db, monkeypatch):
    """The tool's real worker-thread sender must not report a dropped request."""
    from infra import app_loop, config, presence
    from registry.service import register_write_strict
    from tools import document_open
    path = config.WORKSPACE / "shared" / "dropped-open.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# Dropped open\n")
    register_write_strict("sender-self", str(path), db=deleg_db, notify=False)
    monkeypatch.setattr(app_loop, "_LOOP", None)
    monkeypatch.setattr(
        presence,
        "document_open_snapshot",
        lambda *_args: presence.DocumentOpenSnapshot("here", frozenset({"session-a"}), False),
    )
    result = await asyncio.to_thread(
        document_open._open_document,
        "sender-self",
        {"user_id": "u-test"},
        None,
        str(path),
    )

    assert result["outcome"] == "link"
    assert result["reason"] == "user_view_unknown"


@pytest.mark.asyncio
async def test_document_open_child_returns_a_link_without_reading_presence(deleg_db, monkeypatch):
    from infra import config, presence
    from registry.service import register_write_strict
    from tools import document_open
    from tools.document_open import DocumentOpenTool

    path = config.WORKSPACE / "shared" / "child-open.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# Child open\n")
    register_write_strict("sender-self", str(path), db=deleg_db, notify=False)
    deleg_db.execute(
        "INSERT INTO threads (id,user_id,spawned_by,created_at,updated_at) "
        "VALUES ('child-open','u-test','sender-self','t','t')",
    )
    deleg_db.commit()
    events: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(presence, "document_open_snapshot", lambda *_args: pytest.fail("presence read"))
    monkeypatch.setattr(document_open, "emit_product_event", lambda *args, **kwargs: events.append((*args, kwargs)))

    result = json.loads(await DocumentOpenTool().execute(path=str(path), _thread_id="child-open"))

    assert result["outcome"] == "link"
    assert result["reason"] == "delegated_thread"
    assert result["link"] == f"[child-open.md](<{path}>)"
    assert events == [("document", "agent_open", {
        "thread_id": "child-open", "user_id": "u-test",
        "properties": {"outcome": "link", "reason": "delegated_thread", "count": 0},
    })]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("thread_exists", "resolve_error", "resolution", "artifact", "presence_state", "delivery", "outcome", "reason"),
    [
        (False, None, ("artifact", "/a.md", None), {"type": "document"}, ("here", frozenset({"s"}), False), 1, "refused", "no_access"),
        (True, PermissionError, None, {"type": "document"}, ("here", frozenset({"s"}), False), 1, "refused", "no_access"),
        (True, ValueError, None, {"type": "document"}, ("here", frozenset({"s"}), False), 1, "refused", "not_found"),
        (True, None, (None, "/a.md", None), {"type": "document"}, ("here", frozenset({"s"}), False), 1, "refused", "not_found"),
        (True, None, ("artifact", "/a.md", None), None, ("here", frozenset({"s"}), False), 1, "refused", "not_found"),
        (True, None, ("artifact", "/a.md", None), {"type": "image"}, ("here", frozenset({"s"}), False), 1, "refused", "not_a_document"),
        (True, None, ("artifact", "/a.md", None), {"type": "document"}, ("unknown", frozenset(), False), 1, "link", "user_view_unknown"),
        (True, None, ("artifact", "/a.md", None), {"type": "document"}, ("away", frozenset(), False), 1, "link", "user_not_viewing"),
        (True, None, ("artifact", "/a.md", None), {"type": "document"}, ("here", frozenset(), True), 1, "already_open", None),
        (True, None, ("artifact", "/a.md", None), {"type": "document"}, ("here", frozenset(), False), 1, "link", "user_cannot_show"),
        (True, None, ("artifact", "/a.md", None), {"type": "document"}, ("here", frozenset({"s"}), False), 0, "link", "user_view_unknown"),
    ],
    ids=(
        "thread-is-not-owned", "target-not-authorized", "target-is-missing", "resolver-has-no-target",
        "artifact-was-retired", "artifact-is-not-document", "presence-is-unknown", "user-left-thread",
        "already-open", "user-cannot-show", "delivery-lost-its-session",
    ),
)
async def test_document_open_has_a_result_for_each_nonrequest_outcome(
    monkeypatch, thread_exists, resolve_error, resolution, artifact, presence_state, delivery, outcome, reason,
):
    from infra import presence, state
    from registry import models as registry_models
    from tools import document_open

    class Db:
        closed = False

        def close(self):
            self.closed = True

    db = Db()
    monkeypatch.setattr(document_open, "get_db", lambda: db)
    monkeypatch.setattr(
        document_open,
        "get_thread_for_user",
        lambda *_args, **_kwargs: {"user_id": "u-test"} if thread_exists else None,
    )

    def resolve(**_kwargs):
        if resolve_error:
            raise resolve_error()
        return resolution

    monkeypatch.setattr(document_open, "_resolve_doc_target", resolve)
    monkeypatch.setattr(registry_models, "get_artifact", lambda *_args, **_kwargs: artifact)
    monkeypatch.setattr(document_open, "is_subagent", lambda _thread: False)
    monkeypatch.setattr(
        presence,
        "document_open_snapshot",
        lambda *_args: presence.DocumentOpenSnapshot(*presence_state),
    )
    monkeypatch.setattr(state, "send_to_thread_sessions", lambda *_args: delivery)
    monkeypatch.setattr(document_open, "_require_context", lambda _kwargs: ("thread", {"user_id": "u-test"}))

    async def run_in_db(fn, *args):
        return fn(*args)

    events: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(document_open, "run_in_db", run_in_db)
    monkeypatch.setattr(document_open, "emit_product_event", lambda *args, **kwargs: events.append((*args, kwargs)))

    result = json.loads(await document_open.DocumentOpenTool().execute(artifact_id="artifact"))

    assert result["outcome"] == outcome
    assert result["reason"] == reason
    assert db.closed
    assert events == [("document", "agent_open", {
        "thread_id": "thread", "user_id": "u-test",
        "properties": {"outcome": outcome, "reason": reason, "count": result.get("count", 0)},
    })]


def test_document_open_link_escapes_markdown_label_and_destination():
    from tools.document_open import _link_result

    result = _link_result(
        "user_not_viewing",
        "Plan [α]\\draft",
        "/workspace/Meeting notes (α) <draft>.md",
    )

    assert result["link"] == "[Plan \\[α\\]\\\\draft](</workspace/Meeting notes (α) %3Cdraft%3E.md>)"


def test_document_open_guidance_names_post_update():
    from tools.document_open import _REQUESTED, _VIEW_LINK

    for guidance in (_REQUESTED, _VIEW_LINK):
        assert "`post_update`" in guidance


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome, reason", [
    ("requested", None),
    ("link", "user_not_viewing"),
    ("refused", "not_found"),
])
async def test_document_open_emits_every_agent_visible_outcome(monkeypatch, outcome, reason):
    from tools import document_open

    events: list[tuple[str, str, dict]] = []

    async def run_in_db(*_args):
        return {"outcome": outcome, "reason": reason, "count": 1 if outcome == "requested" else 0}

    monkeypatch.setattr(document_open, "_require_context", lambda _kwargs: ("thread", {"user_id": "user"}))
    monkeypatch.setattr(document_open, "run_in_db", run_in_db)
    monkeypatch.setattr(document_open, "emit_product_event", lambda *args, **kwargs: events.append((*args, kwargs)))

    result = json.loads(await document_open.DocumentOpenTool().execute(artifact_id="artifact"))

    assert result["outcome"] == outcome
    assert events == [("document", "agent_open", {
        "thread_id": "thread", "user_id": "user",
        "properties": {"outcome": outcome, "reason": reason, "count": result.get("count", 0)},
    })]
