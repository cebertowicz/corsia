from __future__ import annotations

import asyncio
import hashlib
import json
import uuid

import pytest

from core.step_handler import StepHandler
from core.turn_core import TurnIdentity
from doceditor import comments
from infra import config, state
from infra.database import get_db
from registry import observer, service
from subagents.cc_event_tool_settlement import observe_tool_result as observe_cc_result
from subagents.codex_runtime_events import _observe_tool_result as observe_codex_result
from tests.registry_helpers import artifact_rows_for_thread, make_session


pytestmark = pytest.mark.usefixtures("isolated_db")


class _Socket:
    def __init__(self) -> None:
        self.messages: asyncio.Queue[dict] = asyncio.Queue()

    async def send_json(self, message: dict) -> None:
        await self.messages.put(message)


@pytest.fixture
def socket_buckets():
    previous_users = dict(state._ws_user_clients)
    state._ws_user_clients.clear()
    try:
        yield
    finally:
        state._ws_user_clients.clear()
        state._ws_user_clients.update(previous_users)


async def _emit(_message: dict) -> None:
    pass


def _seed_call(db, thread_id: str, call_id: str, name: str, args: dict) -> str:
    step_id = f"step-{uuid.uuid4().hex[:8]}"
    db.execute(
        "INSERT INTO messages (id, thread_id, role, content, metadata, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (
            step_id,
            thread_id,
            "assistant",
            "",
            json.dumps({
                "step_type": "tool_calls",
                "llm_tool_calls": [{
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args)},
                }],
            }),
            "2026-09-29T20:00:00-07:00",
        ),
    )
    db.commit()
    return step_id


async def _settle(
    harness: str,
    thread_id: str,
    name: str,
    args: dict,
    result: str,
    *,
    metadata: dict | None = None,
) -> None:
    call_id = f"call-{uuid.uuid4().hex[:8]}"
    if harness == "native":
        handler = StepHandler(
            thread_id,
            {"model": "m", "model_name": "m", "is_cloud": False},
            _emit,
            turn_id=f"turn-{uuid.uuid4().hex[:8]}",
        )
        handler.bind_turn_identity(TurnIdentity(handler.turn_id, 1))
        await handler.handle("tool_calls_start", {
            "tools": [{"id": call_id, "name": name, "hint": "", "args": ""}],
            "content": "",
            "tool_call_dicts": [{
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)},
            }],
        })
        await handler.handle("tool_complete", {
            "id": call_id,
            "name": name,
            "result": result,
            **(metadata or {}),
        })
        return

    db = get_db()
    raw_name = f"mcp__corsia__{name}" if harness == "claude" else name
    step_id = _seed_call(db, thread_id, call_id, raw_name, args)
    result_metadata = {
        "tool_call_id": call_id,
        "assistant_step_id": step_id,
        "name": raw_name,
        **(metadata or {}),
    }
    if harness == "claude":
        await observe_cc_result(thread_id, result, result_metadata, db)
    else:
        db.close()
        await observe_codex_result(thread_id, result, result_metadata)


async def _activity(socket: _Socket) -> dict:
    while True:
        message = await asyncio.wait_for(socket.messages.get(), timeout=1)
        if message.get("type") == "doc_activity":
            return message


def _document(thread_id: str, text: str = "zero\nnew value\nend\n"):
    path = config.WORKSPACE / "docs" / f"presence-{uuid.uuid4().hex[:8]}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    db = get_db()
    artifact = service.register_write_strict(thread_id, str(path), db=db)
    return db, path, artifact


def _activity_case(db, artifact: dict, name: str):
    if name == "read_file":
        return {"path": artifact["path"], "offset": 2, "limit": 1}, "read", {"from": 5, "to": 15}, None, "ok"
    if name == "edit_file":
        return {"path": artifact["path"], "old_string": "old", "new_string": "new value"}, "edit", {"from": 5, "to": 14}, None, "ok"

    replacement = "replacement" if name == "suggestion" else None
    row = comments.create(
        artifact["id"],
        body="private body",
        author="Agent Alpha",
        author_kind="agent",
        author_thread_id=artifact["parent_thread_id"],
        from_off=2,
        to_off=6,
        replacement=replacement,
        db=db,
    )
    if name == "decision":
        comments.update(
            row["id"],
            {
                "decision": "accept",
                "decided_by": "Agent Alpha",
                "decided_by_thread_id": artifact["parent_thread_id"],
                "decided_at": "2026-09-29T20:00:00-07:00",
            },
            db=db,
        )
        return {"comment_id": row["id"], "decision": "accept"}, "decision", {"from": 2, "to": 6}, row["id"], json.dumps({"comment": row})
    tool_kind = "suggestion" if replacement is not None else "comment"
    return {"artifact_id": artifact["id"], "body": "private body", "replacement": replacement}, tool_kind, {"from": 2, "to": 6}, row["id"], json.dumps({"comment": row})


@pytest.mark.parametrize("harness", ["native", "claude", "codex"])
@pytest.mark.parametrize(
    ("case", "tool_name"),
    [
        ("read_file", "read_file"),
        ("edit_file", "edit_file"),
        ("comment", "doc_comment_create"),
        ("suggestion", "doc_comment_create"),
        ("decision", "doc_comment_update"),
    ],
)
@pytest.mark.asyncio
async def test_each_tool_kind_reaches_only_the_owners_socket(
    socket_buckets, harness: str, case: str, tool_name: str,
):
    db = get_db()
    thread_id = make_session(db, title="Agent Alpha", user_id="steve")
    other_thread = make_session(db, title="Other", user_id="leksi")
    db.close()
    db, path, artifact = _document(thread_id)
    args, kind, span, comment_id, result = _activity_case(db, artifact, case)
    db.close()

    owner_socket, other_socket = _Socket(), _Socket()
    state._ws_user_clients["steve"] = [owner_socket]
    state._ws_user_clients["leksi"] = [other_socket]
    await _settle(harness, thread_id, tool_name, args, result)
    frame = await _activity(owner_socket)
    await asyncio.sleep(0)

    assert set(frame) == {
        "type", "artifact_id", "thread_id", "agent", "kind", "span",
        "comment_id", "revision", "at",
    }
    assert frame["type"] == "doc_activity"
    assert frame["artifact_id"] == artifact["id"]
    assert frame["thread_id"] == thread_id
    assert frame["agent"] == "Agent Alpha"
    assert frame["kind"] == kind
    assert frame["span"] == span
    assert frame["comment_id"] == comment_id
    assert frame["revision"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert isinstance(frame["at"], str)
    assert "private body" not in json.dumps(frame)
    assert other_socket.messages.empty()
    assert other_thread != thread_id


@pytest.mark.parametrize(
    ("name", "args", "expected_span"),
    [
        ("write_file", {"content": "zero\nnew value\nend\n"}, {"from": 0, "to": 19}),
        (
            "multi_edit",
            {"edits": [
                {"old_string": "before", "new_string": "zero"},
                {"old_string": "after", "new_string": "end"},
            ]},
            {"from": 0, "to": 18},
        ),
        ("edit_file", {"old_string": "x", "new_string": "e"}, None),
    ],
)
def test_edit_span_rules(name: str, args: dict, expected_span: dict | None):
    db = get_db()
    thread_id = make_session(db, title="Agent Alpha")
    db.close()
    db, path, artifact = _document(thread_id)
    args = {"path": str(path), **args}
    call_id = f"call-{uuid.uuid4().hex[:8]}"
    step_id = _seed_call(db, thread_id, call_id, name, args)

    result = observer.observe_record(
        thread_id,
        "tool",
        "ok",
        {"tool_call_id": call_id, "assistant_step_id": step_id, "name": name},
        db=db,
    )

    assert result is not None and result.activity is not None
    assert result.artifact is not None
    assert result.activity.frame["artifact_id"] == artifact["id"]
    assert result.activity.frame["span"] == expected_span


@pytest.mark.parametrize(
    ("content", "extra_metadata"),
    [
        ("Error: denied", {"is_error": True}),
        ("[Tool execution was cancelled]", {}),
        ("synthetic", {"synthetic_tool_result": True}),
    ],
)
@pytest.mark.asyncio
async def test_failed_cancelled_and_synthetic_results_emit_nothing(
    socket_buckets, content: str, extra_metadata: dict,
):
    db = get_db()
    thread_id = make_session(db, title="Agent Alpha")
    db.close()
    db, path, _artifact = _document(thread_id)
    db.close()
    socket = _Socket()
    state._ws_user_clients["steve"] = [socket]

    await _settle(
        "codex",
        thread_id,
        "read_file",
        {"path": str(path), "offset": 1, "limit": 1},
        content,
        metadata=extra_metadata,
    )
    await asyncio.sleep(0)

    assert socket.messages.empty()


@pytest.mark.parametrize("harness", ["native", "claude", "codex"])
@pytest.mark.asyncio
async def test_activity_failure_still_registers_confirmed_write(
    socket_buckets, monkeypatch, harness: str,
):
    db = get_db()
    thread_id = make_session(db, title="Agent Alpha")
    path = config.WORKSPACE / "docs" / f"fault-{uuid.uuid4().hex[:8]}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("written\n", encoding="utf-8")
    assert artifact_rows_for_thread(db, thread_id) == []
    db.close()

    def failing_activity(*_args, **_kwargs):
        raise RuntimeError("presence fault")

    monkeypatch.setattr(observer, "observe_document_activity", failing_activity)
    socket = _Socket()
    state._ws_user_clients["steve"] = [socket]

    await _settle(
        harness,
        thread_id,
        "write_file",
        {"path": str(path), "content": "written\n"},
        "ok",
    )
    await asyncio.sleep(0)

    db = get_db()
    rows = artifact_rows_for_thread(db, thread_id)
    db.close()
    assert [row["path"] for row in rows] == [str(path)]
    frames = []
    while not socket.messages.empty():
        frames.append(socket.messages.get_nowait())
    assert [f for f in frames if f.get("type") == "doc_activity"] == []


def test_activity_failure_returns_artifact_without_activity(monkeypatch):
    db = get_db()
    thread_id = make_session(db, title="Agent Alpha")
    path = config.WORKSPACE / "docs" / f"fault-{uuid.uuid4().hex[:8]}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("written\n", encoding="utf-8")
    call_id = f"call-{uuid.uuid4().hex[:8]}"
    step_id = _seed_call(
        db, thread_id, call_id, "write_file",
        {"path": str(path), "content": "written\n"},
    )

    def failing_activity(*_args, **_kwargs):
        raise RuntimeError("presence fault")

    monkeypatch.setattr(observer, "observe_document_activity", failing_activity)
    result = observer.observe_record(
        thread_id,
        "tool",
        "ok",
        {"tool_call_id": call_id, "assistant_step_id": step_id, "name": "write_file"},
        db=db,
    )

    assert result is not None
    assert result.activity is None
    assert result.artifact is not None
    assert result.artifact["path"] == str(path)


def test_unregistered_path_emits_nothing_and_registers_nothing():
    db = get_db()
    thread_id = make_session(db)
    path = config.WORKSPACE / "docs" / "unregistered.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("text", encoding="utf-8")
    call_id = "unregistered-read"
    step_id = _seed_call(db, thread_id, call_id, "read_file", {"path": str(path)})

    result = observer.observe_record(
        thread_id,
        "tool",
        "ok",
        {"tool_call_id": call_id, "assistant_step_id": step_id, "name": "read_file"},
        db=db,
    )

    assert result is None
    artifact = result.artifact if result is not None else None
    assert artifact is None
    assert artifact_rows_for_thread(db, thread_id) == []


def test_non_document_tool_does_no_identity_lookup(monkeypatch):
    db = get_db()
    thread_id = make_session(db)
    called = False

    def unexpected_lookup(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("non-document tool performed an identity lookup")

    monkeypatch.setattr("doceditor.presence.resolve_file_identity", unexpected_lookup)
    result = observer.observe_record(
        thread_id,
        "tool",
        "ok",
        {"tool_call_id": "plain", "name": "glob"},
        db=db,
    )

    assert result is None
    artifact = result.artifact if result is not None else None
    assert artifact is None
    assert called is False


def test_file_over_the_editor_limit_is_never_read(monkeypatch):
    from doceditor import presence

    db = get_db()
    thread_id = make_session(db)
    db, path, artifact = _document(thread_id, text="0123456789" * 3)
    args = {"path": artifact["path"]}
    assert presence.observe_document_activity(
        thread_id, "read_file", args, "ok", False, db,
    ) is not None

    reads: list[object] = []
    real_read_bytes = type(path).read_bytes

    def counting_read_bytes(self):
        reads.append(self)
        return real_read_bytes(self)

    monkeypatch.setattr(presence, "MAX_TEXT_BYTES", 10)
    monkeypatch.setattr(type(path), "read_bytes", counting_read_bytes)
    assert presence.observe_document_activity(
        thread_id, "read_file", args, "ok", False, db,
    ) is None
    assert reads == []
