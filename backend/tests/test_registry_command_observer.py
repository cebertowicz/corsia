from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import pytest

from infra.database import get_db
from registry.observer import observe_record
from registry.service import register_observed_artifact
from tests.registry_helpers import artifact_rows_for_thread, make_session


_URL = "https://github.com/owner/repo/pull/42"


def _insert_call(
    db,
    thread_id: str,
    tool_call_id: str,
    *,
    name: str,
    command: str,
) -> None:
    db.execute(
        "INSERT INTO messages (id, thread_id, role, content, metadata, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (
            uuid.uuid4().hex[:8],
            thread_id,
            "assistant",
            "",
            json.dumps(
                {
                    "step_type": "tool_calls",
                    "llm_tool_calls": [
                        {
                            "id": tool_call_id,
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps({"command": command}),
                            },
                        }
                    ],
                }
            ),
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    db.commit()


def _pr_rows(db, thread_id: str) -> list[dict]:
    return [
        row
        for row in artifact_rows_for_thread(db, thread_id)
        if row["integration"] == "github" and row["type"] == "pull-request"
    ]


def _insert_result(
    db,
    thread_id: str,
    tool_call_id: str,
    *,
    name: str,
    content: str = _URL + "\n",
) -> dict:
    metadata = {
        "tool_call_id": tool_call_id,
        "name": name,
        "is_error": False,
    }
    db.execute(
        "INSERT INTO messages (id, thread_id, role, content, metadata, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (
            uuid.uuid4().hex[:8],
            thread_id,
            "tool",
            content,
            json.dumps(metadata),
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    db.commit()
    return metadata


def test_the_shipped_row_is_the_github_pr_create_rule(isolated_db):
    db = get_db()
    shipped = db.execute(
        "SELECT connector, tool, argv_prefix, item_type FROM event_rules WHERE scope='global'"
    ).fetchall()

    assert [tuple(row) for row in shipped] == [
        ("github", "bash", '["gh","pr","create"]', "pull-request")
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["bash", "mcp__corsia__bash"])
async def test_pr_create_observation_is_cross_harness(
    isolated_db,
    monkeypatch,
    tool_name,
):
    db = get_db()
    thread_id = make_session(db, prefix="command-observer")
    tool_call_id = f"create-{tool_name}"
    _insert_call(
        db,
        thread_id,
        tool_call_id,
        name=tool_name,
        command="gh pr create --title reliable",
    )

    from tests.test_shell_pr_registration import _direct_shell_context, _stub_process
    from tools.shell import BashTool

    _stub_process(monkeypatch, stdout=_URL + "\n")
    await BashTool().execute("gh pr create --title reliable", **_direct_shell_context(thread_id))
    observation = observe_record(
        thread_id,
        "tool",
        _URL + "\n",
        {"tool_call_id": tool_call_id, "name": tool_name, "is_error": False},
        db=db,
    )

    assert observation is None
    rows = _pr_rows(db, thread_id)
    assert len(rows) == 1
    assert rows[0]["external_id"] == "owner/repo#42"
    assert rows[0]["relationship_state"] == "owner-active"
    decisions = db.execute("SELECT outcome FROM event_route_decisions WHERE thread_id=?", (thread_id,)).fetchall()
    assert [row["outcome"] for row in decisions] == ["registered"]


def test_observed_registration_rejects_invalid_caller_thread(isolated_db):
    db = get_db()

    with pytest.raises(PermissionError, match="valid caller thread"):
        register_observed_artifact(
            "missing-thread",
            integration="github",
            type="pull-request",
            external_id="owner/repo#missing-caller",
            db=db,
            notify=False,
        )

    assert db.execute(
        "SELECT 1 FROM artifact_identities WHERE external_id = ?",
        ("owner/repo#missing-caller",),
    ).fetchone() is None


def test_observed_registration_rejects_orphaned_owner_thread(isolated_db):
    db = get_db()
    owner_id = make_session(db, prefix="command-orphan-owner")
    caller_id = make_session(db, prefix="command-orphan-caller")
    registered = register_observed_artifact(
        owner_id,
        integration="github",
        type="pull-request",
        external_id="owner/repo#orphan-owner",
        db=db,
        notify=False,
    )
    assert registered is not None

    db.execute("DELETE FROM threads WHERE id = ?", (owner_id,))
    db.commit()

    with pytest.raises(PermissionError, match="no verifiable owner thread"):
        register_observed_artifact(
            caller_id,
            integration="github",
            type="pull-request",
            external_id="owner/repo#orphan-owner",
            db=db,
            notify=False,
        )

    assert _pr_rows(db, caller_id) == []


@pytest.mark.parametrize(
    "command",
    [
        "gh pr create --title x && echo done",
        "gh pr create --title x > /tmp/pr-url",
        'gh pr create --title "$(whoami)"',
        "gh pr create --title $$",
        "$(gh pr create --title nested)",
        "env GH_TOKEN=[REDACTED] gh pr create --title wrapped",
        "/opt/homebrew/bin/gh pr create --title qualified",
        "command gh pr create --title aliased",
        "gh pr view 42",
        "gh pr create --title x | tee /tmp/pr-url",
    ],
)
def test_command_trigger_requires_one_literal_direct_argv(
    isolated_db,
    command,
):
    db = get_db()
    thread_id = make_session(db, prefix="command-shape")
    tool_call_id = "unsafe-shape"
    _insert_call(
        db,
        thread_id,
        tool_call_id,
        name="Bash",
        command=command,
    )

    observation = observe_record(
        thread_id,
        "tool",
        _URL + "\n",
        {"tool_call_id": tool_call_id, "name": "Bash", "is_error": False},
        db=db,
    )

    assert observation is None
    artifact = observation.artifact if observation is not None else None
    assert artifact is None
    assert _pr_rows(db, thread_id) == []


@pytest.mark.parametrize(
    ("content", "is_error"),
    [
        (_URL + "\n\nExit code: 1", False),
        (_URL + "\n", True),
        (_URL + "\n" + _URL + "\n", False),
        ("Created: " + _URL + "\n", False),
    ],
)
def test_command_trigger_requires_confirmed_success_result(
    isolated_db,
    content,
    is_error,
):
    db = get_db()
    thread_id = make_session(db, prefix="command-result")
    tool_call_id = "bad-result"
    _insert_call(
        db,
        thread_id,
        tool_call_id,
        name="Bash",
        command="gh pr create --title result",
    )

    observation = observe_record(
        thread_id,
        "tool",
        content,
        {"tool_call_id": tool_call_id, "name": "Bash", "is_error": is_error},
        db=db,
    )

    assert observation is None
    artifact = observation.artifact if observation is not None else None
    assert artifact is None
    assert _pr_rows(db, thread_id) == []


def test_persisted_observation_is_quiet_after_immediate_registration(isolated_db):
    from registry.service import register_observed_artifact

    db = get_db()
    thread_id = make_session(db, prefix="command-dedup")
    tool_call_id = "dedup-create"
    _insert_call(
        db,
        thread_id,
        tool_call_id,
        name="bash",
        command="gh pr create --title dedup",
    )
    immediate = register_observed_artifact(
        thread_id,
        integration="github",
        type="pull-request",
        external_id="owner/repo#42",
        title="PR #42",
        path=_URL,
        db=db,
        notify=False,
    )

    observation = observe_record(
        thread_id,
        "tool",
        _URL + "\n",
        {"tool_call_id": tool_call_id, "name": "bash", "is_error": False},
        db=db,
    )

    assert immediate is not None
    assert observation is None
    persisted = observation.artifact if observation is not None else None
    assert persisted is None
    assert len(_pr_rows(db, thread_id)) == 1


async def _drive_durable_result_seam(
    harness: str,
    thread_id: str,
    db,
) -> None:
    command = "gh pr create --title durable-seam"
    tool_call_id = f"{harness}-create"

    if harness == "native":
        from core.step_handler import StepHandler
        from core.turn_core import persist_turn_start
        from infra.thread_driver import admit_input

        turn_id = uuid.uuid4().hex
        db.execute(
            "UPDATE threads SET model='native/test' WHERE id=?",
            (thread_id,),
        )
        db.commit()
        admission = admit_input(
            thread_id,
            role="user",
            content="Native",
            intent="apply_now",
            message_id=f"{turn_id}-input",
            turn_id=turn_id,
            boot_epoch="boot",
            admitted_at="t",
            db=db,
        )
        assert admission.batch is not None
        identity = persist_turn_start(
            turn_id,
            thread_id,
            "native/test",
            "Native",
            db=db,
        )
        db.commit()

        async def broadcast(_payload):
            return None

        handler = StepHandler(
            thread_id,
            {"model": "native/test", "model_name": "Native"},
            broadcast,
            identity.turn_id,
        )
        handler.bind_turn_identity(identity)
        await handler.handle(
            "tool_calls_start",
            {
                "tools": [
                    {
                        "id": tool_call_id,
                        "name": "bash",
                        "hint": command,
                        "args": json.dumps({"command": command}),
                    }
                ],
                "content": "",
                "tool_call_dicts": [
                    {
                        "id": tool_call_id,
                        "type": "function",
                        "function": {
                            "name": "bash",
                            "arguments": json.dumps({"command": command}),
                        },
                    }
                ],
                "skip_broadcast": True,
            },
        )
        await handler.handle(
            "tool_complete",
            {
                "id": tool_call_id,
                "name": "bash",
                "result": _URL + "\n",
                "is_error": False,
            },
        )
        return

    tool_name = "mcp__corsia__bash"
    _insert_call(
        db,
        thread_id,
        tool_call_id,
        name=tool_name,
        command=command,
    )
    metadata = _insert_result(
        db,
        thread_id,
        tool_call_id,
        name=tool_name,
    )
    if harness == "claude":
        from subagents.cc_event_tool_settlement import observe_tool_result

        await observe_tool_result(thread_id, _URL + "\n", metadata, db)
    else:
        from subagents.codex_runtime_events import _observe_tool_result

        await _observe_tool_result(thread_id, _URL + "\n", metadata)


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", ["native", "claude", "codex"])
async def test_each_durable_result_seam_registers_one_routable_pr(
    isolated_db,
    monkeypatch,
    harness,
):
    from unittest.mock import patch

    from events.router import RouteOutcome
    from tests.event_routing_helpers import pr_payload, route

    db = get_db()
    thread_id = make_session(db, prefix=f"command-{harness}-durable")

    from tests.test_shell_pr_registration import _direct_shell_context, _stub_process
    from tools.shell import BashTool

    _stub_process(monkeypatch, stdout=_URL + "\n")
    await BashTool().execute("gh pr create --title durable-seam", **_direct_shell_context(thread_id))
    await _drive_durable_result_seam(harness, thread_id, db)

    rows = _pr_rows(db, thread_id)
    assert len(rows) == 1
    assert rows[0]["relationship_state"] == "owner-active"
    db.close()

    with patch(
        "events.router.delivery.deliver_to_thread",
        return_value=True,
    ) as mock_deliver:
        routed = await route(
            "webhook.github.pr_opened",
            pr_payload("owner/repo", 42),
        )

    assert routed.outcome == RouteOutcome.ACCEPTED
    assert routed.thread_id == thread_id
    mock_deliver.assert_called_once()
    assert mock_deliver.call_args.args[0] == thread_id


@pytest.mark.asyncio
async def test_observed_pr_routes_later_webhook_to_creator_thread(isolated_db, monkeypatch):
    from unittest.mock import patch

    from events.router import RouteOutcome
    from tests.event_routing_helpers import pr_payload, route

    db = get_db()
    thread_id = make_session(db, prefix="command-route")
    tool_call_id = "route-create"
    _insert_call(
        db,
        thread_id,
        tool_call_id,
        name="mcp__corsia__bash",
        command="gh pr create --title routed",
    )
    from tests.test_shell_pr_registration import _direct_shell_context, _stub_process
    from tools.shell import BashTool

    _stub_process(monkeypatch, stdout=_URL + "\n")
    await BashTool().execute("gh pr create --title routed", **_direct_shell_context(thread_id))
    observation = observe_record(
        thread_id,
        "tool",
        _URL + "\n",
        {
            "tool_call_id": tool_call_id,
            "name": "mcp__corsia__bash",
            "is_error": False,
        },
        db=db,
    )
    assert observation is None
    assert len(_pr_rows(db, thread_id)) == 1
    db.close()

    with patch(
        "events.router.delivery.deliver_to_thread",
        return_value=True,
    ) as mock_deliver:
        routed = await route(
            "webhook.github.pr_opened",
            pr_payload("owner/repo", 42),
        )

    assert routed.outcome == RouteOutcome.ACCEPTED
    assert routed.thread_id == thread_id
    mock_deliver.assert_called_once()
    assert mock_deliver.call_args.args[0] == thread_id
