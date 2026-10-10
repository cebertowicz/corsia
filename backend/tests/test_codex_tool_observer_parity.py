
from __future__ import annotations

import json
import os
import sys
import uuid
from types import SimpleNamespace

import pytest

_BACKEND_DIR = os.path.dirname(os.path.dirname(__file__))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

from infra import config  # noqa: E402
from infra.database import get_db  # noqa: E402
from subagents import codex_runtime  # noqa: E402
from subagents.codex_app_server_events import (  # noqa: E402
    failed_event,
    item_completed_event,
    item_started_event,
)
from tests.registry_helpers import (  # noqa: E402
    artifact_rows_for_thread,
    make_session,
    noop_bcast,
)
def _artifacts(db, sid: str) -> list[dict]:
    return artifact_rows_for_thread(db, sid)


async def _codex_on_event(thread_id: str, bcast=noop_bcast):
    from tests.codex_runtime_fixtures import _admit_active_turn

    db = get_db()
    try:
        db.execute(
            "UPDATE threads SET model='codex/test' WHERE id=?",
            (thread_id,),
        )
        db.commit()
    finally:
        db.close()
    identity = _admit_active_turn(
        thread_id,
        turn_id=f"turn-{uuid.uuid4()}",
        content="observer parity",
    )
    live = codex_runtime._LiveCodex(
        codex_thread_id="codex-thread-test",
        session=SimpleNamespace(
            active_turn_id="provider-turn-test",
            peek_active_usage=lambda: None,
        ),
        parent_thread_id=None,
        name="Codex test",
        corsia_turn=identity,
    )
    return codex_runtime._make_on_event(live, thread_id, bcast)


class TestCodexToolObserverParity:
    def test_app_server_context_window_error_is_stable_cause(self):
        ev = failed_event({
            "message": "Codex ran out of room in the model's context window.",
            "codexErrorInfo": {"contextWindowExceeded": True},
        })

        assert ev["kind"] == "turn_failed"
        assert ev["cause"] == "context_window_exceeded"

    async def test_codex_tool_result_reaches_observer_after_persistence(self, isolated_db):
        db = get_db()
        sid = make_session(db, prefix="codex-obs")
        target = config.WORKSPACE / f"codex-write-{uuid.uuid4().hex[:6]}.md"
        casts: list[tuple[str, dict]] = []
        on_event = await _codex_on_event(
            sid, lambda thread_id, message: casts.append((thread_id, message)),
        )

        started = item_started_event({
            "type": "dynamicToolCall",
            "id": "codex-write-1",
            "tool": "Write",
            "arguments": {"file_path": str(target), "content": "hello"},
        }, provider_thread_id="codex-thread-test",
           provider_turn_id="provider-turn-test")
        completed = item_completed_event({
            "type": "dynamicToolCall",
            "id": "codex-write-1",
            "result": f"File created successfully at: {target}",
            "success": True,
        }, provider_thread_id="codex-thread-test",
           provider_turn_id="provider-turn-test")

        await on_event(started)
        # In production the Codex app-server executes the dynamic tool through
        # the backend bridge, persisting the canonical result before Codex
        # echoes item_completed; replicate that execution write here.
        from core.tool_result_delivery import persist_tool_execution_result

        turn_id = db.execute(
            "SELECT id FROM active_turns WHERE thread_id=?", (sid,),
        ).fetchone()["id"]
        step_id = db.execute(
            "SELECT id FROM messages WHERE thread_id=? AND role='assistant'",
            (sid,),
        ).fetchone()["id"]
        persist_tool_execution_result(
            thread_id=sid,
            turn_id=turn_id,
            invocation_id="codex-write-1",
            assistant_step_id=step_id,
            provider_tool_call_id="codex-write-1",
            name="Write",
            arguments=None,
            content=f"File created successfully at: {target}",
            is_error=False,
            db=db,
        )
        db.commit()
        await on_event(completed)

        rows = [dict(r) for r in db.execute(
            "SELECT id, role, content, metadata, created_at FROM messages "
            "WHERE thread_id=? ORDER BY created_at", (sid,),
        ).fetchall()]
        assert [r["role"] for r in rows] == [
            "user", "system", "assistant", "tool",
        ]
        assert json.loads(rows[1]["metadata"])["event_type"] == "context.digest"
        assistant_row, tool_row = rows[2:]
        assert json.loads(assistant_row["metadata"])["llm_tool_calls"][0]["function"]["name"] == "Write"
        tool_meta = json.loads(tool_row["metadata"])
        assert tool_meta["tool_call_id"] == "codex-write-1"
        assert tool_meta["assistant_step_id"] == assistant_row["id"]
        assert tool_meta["provider_tool_call_id"] == "codex-write-1"
        assert tool_meta["name"] == "Write"
        assert tool_meta["tool_invocation_id"] == "codex-write-1"

        arts = _artifacts(db, sid)
        assert len(arts) == 1
        assert arts[0]["external_id"].startswith("workspace://")
        assert arts[0]["path"] == str(target.resolve())
        final = next(
            message for _, message in reversed(casts)
            if message.get("type") == "step_text"
        )
        assert final["message_id"] == assistant_row["id"]
        assert final["finalized_tool_ids"] == ["codex-write-1"]
        assert final["metadata"]["tool_calls"][0]["result"].startswith(
            "File created successfully"
        )

    async def test_codex_tools_keep_the_latest_assistant_boundary_live_and_on_reload(
        self, isolated_db,
    ):
        """A -> tools A -> B -> tools B -> C keeps two Assistant Step owners."""
        from api.thread_paging import page_messages
        from core.tool_result_delivery import persist_tool_execution_result

        db = get_db()
        sid = make_session(db, prefix="codex-tool-boundaries")
        casts: list[tuple[str, dict]] = []
        on_event = await _codex_on_event(
            sid, lambda thread_id, message: casts.append((thread_id, message)),
        )
        provider = {
            "provider_thread_id": "codex-thread-test",
            "provider_turn_id": "provider-turn-test",
        }
        turn_id = db.execute(
            "SELECT id FROM active_turns WHERE thread_id=?", (sid,),
        ).fetchone()["id"]

        def assistant_step_for(tool_id: str) -> str:
            rows = db.execute(
                "SELECT id,metadata FROM messages "
                "WHERE thread_id=? AND role='assistant' ORDER BY seq",
                (sid,),
            ).fetchall()
            for row in rows:
                calls = json.loads(row["metadata"] or "{}").get("llm_tool_calls") or []
                if any(call.get("id") == tool_id for call in calls):
                    return str(row["id"])
            raise AssertionError(f"missing Assistant Step for {tool_id}")

        async def assistant(text: str) -> None:
            event = item_completed_event({"type": "agentMessage", "text": text}, **provider)
            assert event is not None
            await on_event(event)

        async def tool(tool_id: str, path: str, content: str) -> None:
            started = item_started_event({
                "type": "dynamicToolCall",
                "id": tool_id,
                "tool": "read_file",
                "arguments": {"path": path},
            }, **provider)
            completed = item_completed_event({
                "type": "dynamicToolCall",
                "id": tool_id,
                "result": content,
                "success": True,
            }, **provider)
            assert started is not None and completed is not None
            await on_event(started)
            persist_tool_execution_result(
                thread_id=sid,
                turn_id=turn_id,
                invocation_id=tool_id,
                assistant_step_id=assistant_step_for(tool_id),
                provider_tool_call_id=tool_id,
                name="read_file",
                arguments={"path": path},
                content=content,
                is_error=False,
                db=db,
            )
            db.commit()
            await on_event(completed)

        await assistant("Assistant A")
        await tool("tool-a", "A.md", "result A")
        await assistant("Assistant B")
        await tool("tool-b", "B.md", "result B")
        await assistant("Assistant C")

        live_by_content = {
            frame["content"]: frame
            for _, frame in casts
            if frame.get("type") == "step_text" and frame.get("content")
        }
        assert [
            call["id"] for call in live_by_content["Assistant A"]["metadata"]["tool_calls"]
        ] == ["tool-a"]
        assert [
            call["id"] for call in live_by_content["Assistant B"]["metadata"]["tool_calls"]
        ] == ["tool-b"]
        assert live_by_content["Assistant C"]["metadata"].get("tool_calls") is None

        hydrated, has_more, _ = page_messages(db, sid, 8, None)
        assert has_more is False
        hydrated_by_content = {row["content"]: row for row in hydrated if row["content"]}
        assert [
            call["id"]
            for call in hydrated_by_content["Assistant A"]["metadata"]["tool_calls"]
        ] == ["tool-a"]
        assert [
            call["id"]
            for call in hydrated_by_content["Assistant B"]["metadata"]["tool_calls"]
        ] == ["tool-b"]
        assert hydrated_by_content["Assistant C"]["metadata"].get("tool_calls") is None

    def test_codex_non_durable_plan_item_is_tool_boundary_violation(self):
        item = {
            "type": "plan",
            "id": "provider-plan-1",
            "items": [{"step": "Pretend to update", "status": "in_progress"}],
        }

        started = item_started_event(item)
        completed = item_completed_event(item)

        assert started == {
            "kind": "provider_tool_violation",
            "provider": "codex",
            "id": "provider-plan-1",
            "tool_type": "plan",
            "phase": "started",
        }
        assert completed == {
            **started,
            "phase": "completed",
        }

    def test_codex_command_execution_is_tool_boundary_violation(self):
        ev = item_started_event(
            {
                "type": "commandExecution",
                "id": "cmd-pr-1",
                "command": "gh pr create --title 'Codex parity' --base dev",
                "cwd": str(config.WORKSPACE),
            },
            provider_thread_id="codex-thread-test",
            provider_turn_id="provider-turn-test",
        )

        assert ev == {
            "kind": "provider_tool_violation",
            "provider": "codex",
            "id": "cmd-pr-1",
            "tool_type": "commandExecution",
            "phase": "started",
            "provider_thread_id": "codex-thread-test",
            "provider_turn_id": "provider-turn-test",
        }

    async def test_codex_command_execution_does_not_auto_register_artifacts(self, isolated_db):
        db = get_db()
        sid = make_session(db, prefix="codex-pr")
        on_event = await _codex_on_event(sid)

        started = item_started_event(
            {
                "type": "commandExecution",
                "id": "cmd-pr-1",
                "command": "gh pr create --title 'Codex parity' --base dev",
                "cwd": str(config.WORKSPACE),
            },
            provider_thread_id="codex-thread-test",
            provider_turn_id="provider-turn-test",
        )
        completed = item_completed_event(
            {
                "type": "commandExecution",
                "id": "cmd-pr-1",
                "aggregatedOutput": "https://github.com/owner/repo/pull/461\n",
                "exitCode": 0,
            },
            provider_thread_id="codex-thread-test",
            provider_turn_id="provider-turn-test",
        )

        with pytest.raises(RuntimeError, match="capability boundary"):
            await on_event(started)
        with pytest.raises(RuntimeError, match="capability boundary"):
            await on_event(completed)

        messages = [dict(r) for r in db.execute(
            "SELECT role, content, metadata FROM messages WHERE thread_id=?",
            (sid,),
        ).fetchall()]
        assert [m["role"] for m in messages] == ["system", "user", "system"]
        assert json.loads(messages[0]["metadata"])["event_type"] == "context.digest"
        violations = [
            message for message in messages
            if json.loads(message["metadata"] or "{}").get("event_type")
            == "harness.tool_boundary_violation"
        ]
        assert len(violations) == 1
        assert "Host access cannot authorize provider-native tools" in (
            violations[0]["content"]
        )
        arts = _artifacts(db, sid)
        assert arts == []
