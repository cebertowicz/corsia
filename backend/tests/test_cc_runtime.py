from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.registry_helpers import artifact_rows_for_thread

from cc_runtime_fixtures import (  # noqa: F401
    _done_count,
    _ready_count,
    _seed_parent as _seed_parent_row,
    _wait,
    child_db,
    cc_target,
    fake_claude,
    feed_with_persisted_input,
    prepared_cc_input,
)

_TURN_ID = "11111111-2222-4333-8444-555555555555"


def _seed_parent(db, sid="parent", user="steve"):
    """Create the active modeled parent represented by these child tests."""
    from core.turn_core import BOOT_EPOCH
    from infra.thread_driver import admit_input

    _seed_parent_row(db, sid=sid, user=user)
    db.execute(
        "UPDATE threads SET model='claude-code/sonnet' WHERE id=?",
        (sid,),
    )
    db.commit()
    admission = admit_input(
        sid,
        role="user",
        content="spawn a child",
        intent="apply_now",
        message_id=f"{sid}:active-input",
        turn_id=f"{sid}:active-turn",
        boot_epoch=BOOT_EPOCH,
        admitted_at="t",
        db=db,
    )
    assert admission.batch is not None


def _seed_agent_roster(db) -> None:
    """Provide one remaining dynamic-tail source for ship-on-change tests."""
    db.execute(
        "INSERT INTO threads "
        "(id,user_id,title,agent_enabled,agent_status,agent_responsibility,"
        "created_at,updated_at) VALUES "
        "('roster-agent','steve','Roster Agent',1,'active','Own test scope','t','t')"
    )
    db.commit()


async def _resistant_tool(release: asyncio.Event, cancelled: asyncio.Event) -> None:
    try:
        await release.wait()
    except asyncio.CancelledError:
        cancelled.set()
        await release.wait()


class TestSpawn:
    @pytest.mark.asyncio
    async def test_spawn_commits_seed_map_before_process_start(
        self, child_db, fake_claude, monkeypatch,
    ):
        import sqlite3

        from infra import database
        from subagents.cc_stream import CCStreamSession
        import subagents.cc_runtime as rt

        _seed_parent(child_db)
        _seed_agent_roster(child_db)
        original_start = CCStreamSession.start
        writer_available: list[bool] = []

        async def start(session):
            check = sqlite3.connect(str(database.DB_PATH))
            check.row_factory = sqlite3.Row
            try:
                row = check.execute(
                    "SELECT metadata FROM messages WHERE thread_id=? "
                    "AND json_extract(metadata,'$.type')='seed'",
                    (session.corsia_thread_id,),
                ).fetchone()
                assert row is not None
                assert "responsibility_map_snapshot" in json.loads(row["metadata"])
            finally:
                check.close()
            writer = sqlite3.connect(str(database.DB_PATH))
            writer.execute("PRAGMA busy_timeout=1")
            try:
                writer.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError:
                writer_available.append(False)
            else:
                writer_available.append(True)
                writer.rollback()
            finally:
                writer.close()
            await original_start(session)

        monkeypatch.setattr(CCStreamSession, "start", start)
        child_id = await rt.spawn(
            "parent", "do the work", binary=fake_claude,
            target=cc_target("sonnet"), broadcast=lambda _sid, _message: None,
        )
        await rt.retire(child_id)
        assert writer_available == [True]

    @pytest.mark.asyncio
    async def test_spawn_seed_retains_responsibility_map_before_process_start(
        self, child_db, fake_claude,
    ):
        from core.dynamic_context_registry import collect_context_sources
        from infra.config import WORKSPACE
        from subagents.cc_spawn_prepare import CCSpawnRequest, prepare_spawn

        _seed_parent(child_db)
        _seed_agent_roster(child_db)
        target = cc_target("sonnet")
        prepared = await prepare_spawn(
            CCSpawnRequest(
                "parent", "do the work", fake_claude, target, None, None,
                "Child", None, None, lambda _sid, _message: None,
            ),
            child_db,
        )
        frozen = prepared.live.context_snapshot["agent_facts"][
            "responsibility_map_snapshot"
        ]
        child_db.execute(
            "UPDATE threads SET agent_responsibility='Changed scope' "
            "WHERE id='roster-agent'"
        )
        child_db.commit()

        relaunch = collect_context_sources(
            thread_id=prepared.child_id, project_id=None, workspace=WORKSPACE,
            db=child_db, selected_model=target.catalog_id,
        )
        restarted = next(
            row for row in relaunch.sources
            if row.source_id == "role:agent-owner-map"
        )
        assert relaunch.installed.adoption == "retained"

        assert restarted.prompt_content == frozen["content"]
        assert "Own test scope" in (restarted.prompt_content or "")
        assert "Changed scope" not in (restarted.prompt_content or "")

    @pytest.mark.asyncio
    async def test_spawn_builds_registry_before_dml_then_registers_before_bridge(
        self, child_db, fake_claude, monkeypatch,
    ):
        import host_access.supervisor as supervisor
        import subagents.cc_runtime as rt
        import subagents.harness_tool_bridge as bridge

        _seed_parent(child_db)
        events: list[str] = []
        original_build = bridge.build_tool_registry
        original_definitions = bridge.dynamic_tool_definitions

        def register(*, parent_thread_id, thread_id, user_id):
            row = child_db.execute(
                "SELECT spawned_by,user_id FROM threads WHERE id=?",
                (thread_id,),
            ).fetchone()
            assert row is not None
            assert (row["spawned_by"], row["user_id"]) == (
                parent_thread_id,
                user_id,
            )
            assert events == ["registry_built"]
            events.append("registered")
            return True

        def build(working_dir):
            child_count = child_db.execute(
                "SELECT COUNT(*) FROM threads WHERE spawned_by='parent'"
            ).fetchone()[0]
            if not events:
                assert child_count == 0
                events.append("registry_built")
            else:
                # Initial-task destination rendering builds its own exact tool
                # surface after the durable child exists.
                assert child_count == 1
            return original_build(working_dir)

        def definitions(registry):
            if events == ["registry_built", "registered"]:
                events.append("bridge_configured")
            else:
                assert events == [
                    "registry_built", "registered", "bridge_configured",
                ]
            return original_definitions(registry)

        monkeypatch.setattr(
            supervisor,
            "register_host_access_descendant",
            register,
        )
        monkeypatch.setattr(bridge, "build_tool_registry", build)
        monkeypatch.setattr(bridge, "dynamic_tool_definitions", definitions)

        child_id = await rt.spawn(
            "parent",
            "list the files",
            binary=fake_claude,
            target=cc_target("sonnet"),
            broadcast=lambda _sid, _message: None,
        )
        assert events[:3] == [
            "registry_built",
            "registered",
            "bridge_configured",
        ]
        await rt.retire(child_id)

    @pytest.mark.asyncio
    async def test_registry_build_failure_leaves_no_committed_child(
        self, child_db, fake_claude, monkeypatch,
    ):
        import subagents.cc_runtime as rt
        import subagents.harness_tool_bridge as bridge

        _seed_parent(child_db)

        def fail_build(_working_dir):
            raise RuntimeError("registry build failed")

        monkeypatch.setattr(bridge, "build_tool_registry", fail_build)
        with pytest.raises(RuntimeError, match="registry build failed"):
            await rt.spawn(
                "parent",
                "list the files",
                binary=fake_claude,
                target=cc_target("sonnet"),
                broadcast=lambda _sid, _message: None,
            )

        child_count = child_db.execute(
            "SELECT COUNT(*) FROM threads WHERE spawned_by='parent'"
        ).fetchone()[0]
        assert child_count == 0

    @pytest.mark.asyncio
    async def test_spawn_delivers_conductor_template_without_persisting_template_bytes(
        self,
        child_db,
        fake_claude,
        monkeypatch,
    ):
        import json
        from pathlib import Path

        from core import role_context_sources
        import subagents.cc_runtime as rt

        _seed_parent(child_db)
        monkeypatch.setattr(
            role_context_sources,
            "get_conductor_scopes",
            lambda _thread_id, db=None: [
                {"scope": "workspace", "scope_type": "workspace"}
            ],
        )

        child_id = await rt.spawn(
            "parent",
            "coordinate the work",
            binary=fake_claude,
            target=cc_target("sonnet"),
            broadcast=lambda _sid, _message: None,
        )
        live = rt._LIVE.get(child_id)
        assert live is not None
        provider_seed = Path(live.session.seed_file).read_text(encoding="utf-8")
        seed_rows = child_db.execute(
            "SELECT content,metadata FROM messages "
            "WHERE thread_id=? AND role='system'",
            (child_id,),
        ).fetchall()
        persisted_seed = next(
            row["content"]
            for row in seed_rows
            if json.loads(row["metadata"] or "{}").get("type") == "seed"
        )

        template_marker = "You are the designated conductor for this scope."
        assert template_marker in provider_seed
        assert template_marker not in persisted_seed
        await rt.retire(child_id)

    @pytest.mark.asyncio
    async def test_spawn_persists_broadcasts_registers(
        self, child_db, fake_claude, monkeypatch,
    ):
        import sqlite3

        import subagents.cc_runtime as rt
        from infra import database
        from infra import thread_execution
        from infra.db_loop_guard import LOOP_WRITE_STRICT_ENV

        _seed_parent(child_db)
        child_db.execute(
            "UPDATE threads SET model='omlx@local/qwen' WHERE id='parent'"
        )
        child_db.commit()
        deliver = AsyncMock(wraps=thread_execution.execute_claimed_batch)
        parent_wake = AsyncMock(return_value=None)
        monkeypatch.setattr(thread_execution, "execute_claimed_batch", deliver)
        monkeypatch.setattr(thread_execution, "drive_thread", parent_wake)
        casts: list[tuple[str, dict]] = []
        marker_visibility: list[str] = []

        def broadcast(sid, message):
            casts.append((sid, message))
            if sid != "parent" or message["type"] not in {
                "message_added", "subagent_spawned",
            }:
                return
            reader = sqlite3.connect(str(database.DB_PATH))
            try:
                marker = reader.execute(
                    "SELECT id FROM messages WHERE thread_id=? "
                    "AND json_extract(metadata, '$.type')='subagent_ref' "
                    "AND json_extract(metadata, '$.provider')='cc'",
                    ("parent",),
                ).fetchone()
            finally:
                reader.close()
            assert marker is not None
            marker_visibility.append(message["type"])

        monkeypatch.setenv(LOOP_WRITE_STRICT_ENV, "1")
        child_id = await rt.spawn(
            "parent", "list the files", binary=fake_claude,
            target=cc_target("sonnet"),
            broadcast=broadcast,
        )
        assert await _wait(lambda: _ready_count(casts) >= 1), "no subagent_status ready"

        row = dict(child_db.execute("SELECT * FROM threads WHERE id = ?", (child_id,)).fetchone())
        assert row["parent_thread_id"] == "parent"
        assert row["model"] == "claude-code/sonnet"
        assert row["spawned_by"] is not None

        assert any(sid == "parent" and m["type"] == "message_added"
                   and (m["message"].get("metadata") or {}).get("type") == "subagent_ref"
                   for sid, m in casts)
        assert any(sid == "parent" and m["type"] == "subagent_spawned" for sid, m in casts)
        assert marker_visibility == ["message_added", "subagent_spawned"]

        child_types = {m["type"] for sid, m in casts if sid == child_id}
        assert "step_text" in child_types and "turn_complete" in child_types
        texts = [m["content"] for sid, m in casts
                 if sid == child_id and m["type"] == "step_text"]
        assert any(
            t.startswith("echo:") and "list the files" in t
            for t in texts
        )

        # The terminal transaction adds one canonical parent input, then asks
        # the same thread driver to wake the parent.
        assert await _wait(
            lambda: parent_wake.await_count >= 1
        ), "no parent completion wake"
        assert _done_count(casts) == 0
        parent_wake.assert_awaited_once_with("parent")
        deliver.assert_awaited_once()

        rows = child_db.execute(
            "SELECT role,content,metadata FROM messages WHERE thread_id = ?",
            (child_id,),
        ).fetchall()
        roles = [row["role"] for row in rows]
        assert "system" in roles and "user" in roles and "assistant" in roles
        initial_user = next(row for row in rows if row["role"] == "user")
        assert initial_user["content"] == "list the files"
        metadata = json.loads(initial_user["metadata"])
        assert metadata["sender_kind"] == "agent"
        assert metadata["from_thread"] == "parent"

        # The live first frame gets exactly one provider-facing agent envelope;
        # durable history keeps raw text so a later reconstruction does not
        # double-wrap it.
        first_frame = rt._LIVE.get(child_id).session.input_frame_log[-1]["message"]["content"]
        assert first_frame.count("[AGENT-SEND") == 1
        assert first_frame.count("[To reply, call delegate_send(thread_id=") == 1

        # This test guards the spawn path; retirement has independent durable
        # completion work outside that scope.
        monkeypatch.delenv(LOOP_WRITE_STRICT_ENV)
        assert rt.is_live(child_id)
        stopped = await rt.retire(
            child_id, broadcast=lambda sid, m: casts.append((sid, m)),
        )
        assert stopped and not rt.is_live(child_id)
        assert any(sid == child_id and m["type"] == "subagent_closed" for sid, m in casts)
        assert any(sid == "parent" and m["type"] == "subagent_status"
                   and m["status"] == "closed" for sid, m in casts)

    @pytest.mark.asyncio
    async def test_provider_frame_uses_fifo_without_dynamic_tail(
        self, child_db, fake_claude,
    ):
        """The provider sees the FIFO batch while durable user text stays clean."""
        import subagents.cc_runtime as rt

        _seed_parent(child_db)
        _seed_agent_roster(child_db)
        casts: list[tuple[str, dict]] = []
        child_id = await rt.spawn(
            "parent", "list the files", binary=fake_claude,
            target=cc_target("sonnet"),
            broadcast=lambda sid, message: casts.append((sid, message)),
        )
        assert await _wait(lambda: _ready_count(casts) >= 1), "no ready event"

        frame = rt._LIVE.get(child_id).session.input_frame_log[-1]["message"][
            "content"
        ]
        assert "[PASSIVE CONTEXT DIGEST]" in frame
        assert "[2/2]\n[AGENT-SEND" in frame
        assert "list the files" in frame
        assert "DYNAMIC CONTEXT DELTA" not in frame

        user_rows = child_db.execute(
            "SELECT content FROM messages WHERE thread_id=? AND role='user'",
            (child_id,),
        ).fetchall()
        assert [row["content"] for row in user_rows] == ["list the files"]
        await rt.retire(child_id)

    @pytest.mark.asyncio
    async def test_feed_steers_running_suppresses_per_turn_notify(self, child_db, fake_claude):
        import subagents.cc_runtime as rt

        _seed_parent(child_db)
        casts: list[tuple[str, dict]] = []
        child_id = await rt.spawn("parent", "first", binary=fake_claude,
                                  target=cc_target("sonnet"),
                                  broadcast=lambda sid, m: casts.append((sid, m)))
        assert await _wait(lambda: (
            _ready_count(casts) >= 1
            and (live := rt._LIVE.get(child_id)) is not None
            and live.turn_terminal is not None
            and live.turn_terminal.is_set()
        ))
        # Completion is one ordinary parent FIFO message, never a process-local
        # doorbell or a second delivery ledger.
        assert _done_count(casts) == 0
        completions = child_db.execute(
            "SELECT id FROM messages WHERE thread_id='parent' "
            "AND json_valid(metadata) "
            "AND json_extract(metadata,'$.completion')=1 "
            "AND json_extract(metadata,'$.from_thread')=?",
            (child_id,),
        ).fetchall()
        assert len(completions) == 1

        target = cc_target("sonnet")
        did = await feed_with_persisted_input(
            child_id, "second", target=target, turn_id=_TURN_ID,
        )
        assert did is not None
        await _wait(lambda: _ready_count(casts) >= 2)

        texts = [m["content"] for sid, m in casts
                 if sid == child_id and m["type"] == "step_text"]
        # A spawned CC child now receives the required agent envelope, so its
        # first echo begins with that provenance rather than raw task text.
        assert any("first" in t for t in texts) and any(
            t.startswith("echo:second") for t in texts
        )
        # Every successful child Turn adds one parent FIFO input, while the old
        # process-local notification remains absent.
        assert _done_count(casts) == 0
        completions = child_db.execute(
            "SELECT id FROM messages WHERE thread_id='parent' "
            "AND json_valid(metadata) "
            "AND json_extract(metadata,'$.completion')=1 "
            "AND json_extract(metadata,'$.from_thread')=?",
            (child_id,),
        ).fetchall()
        assert len(completions) == 2

        await rt.retire(
            child_id, broadcast=lambda sid, m: casts.append((sid, m)),
        )
        # Retire's user-driving logical-close notice is unchanged by Wave F.
        assert _done_count(casts) == 1

    @pytest.mark.asyncio
    async def test_feed_none_when_not_live(self, child_db, monkeypatch):
        import subagents.cc_runtime as rt
        import subagents.cc_feed_transition as cc_feed_transition

        monkeypatch.setattr(
            cc_feed_transition, "require_cc_provider_fit", lambda *_args: None
        )
        monkeypatch.setattr(
            cc_feed_transition, "resume_child", AsyncMock(return_value=None)
        )
        assert await rt.feed(
            "nonexistent", "x", target=cc_target("sonnet"), turn_id=_TURN_ID,
        ) is None

    @pytest.mark.asyncio
    async def test_feed_cold_starts_fresh_delegated_cc_subagent(
        self, child_db, tmp_path, monkeypatch,
    ):
        """First /send to a delegation-created CC subagent thread cold-starts.

        A delegated CC subagent thread (``POST /api/delegation/threads``) has an
        8-char id and NO registered CC session: there is nothing to ``--resume``,
        and the thread id is not a valid CC ``--session-id`` UUID. The first send
        must mint a UUID CC session, register the thread->session mapping, and
        start the CC process FRESH (``--session-id <uuid>``, not ``--resume``),
        then deliver. Regression #501 (the 409 "Thread can't be continued as a
        Claude Code session" on a freshly-created delegated CC worker).
        """
        import uuid as _uuid
        import subagents.cc_runtime as rt
        import subagents.cc_destination as cc_destination
        import subagents.cc_feed_transition as cc_feed_transition
        import subagents.cc_runtime_helpers as cc_runtime_helpers
        import subagents.cc_startup_process as cc_startup_process

        thread_id = "5ad99c01"  # 8-char delegation id (not a UUID)
        child_db.execute(
            "INSERT INTO threads (id, user_id, model, spawned_by, "
            "created_at, updated_at) VALUES (?, 'u', 'claude-code/sonnet', "
            "'parent', 't', 't')",
            (thread_id,),
        )
        child_db.commit()

        started: dict = {}

        class _FakeSession:
            returncode = None  # so _proc_is_alive treats it as alive

            def __init__(self, *a, **k):
                self._invocation_kwargs = k
                self.session_id = k["session_id"]
                self.resume_session_id = k.get("resume_session_id")
                self.working_dir = k.get("working_dir")
                self.reconstructed_transcript_path = k.get(
                    "reconstructed_transcript_path"
                )
                started.update(k)

            def invocation(self):
                from subagents.cc_stream import ClaudeInvocation

                values = self._invocation_kwargs
                return ClaudeInvocation(
                    binary=values["binary"],
                    permission_mode=values.get(
                        "permission_mode", "bypassPermissions"
                    ),
                    session_id=values["session_id"],
                    resume_session_id=values.get("resume_session_id"),
                    seed_file=values.get("seed_file"),
                    mcp_config=values.get("mcp_config"),
                    working_dir=values.get("working_dir"),
                    model=values.get("model"),
                    effort=values.get("effort"),
                )

            async def start(self):
                return None

            async def feed(self, text, *, on_write_observed, on_accepted, input_id=None):
                on_write_observed()
                on_accepted()
                started["fed"] = text

        monkeypatch.setattr(cc_startup_process, "CCStreamSession", _FakeSession)
        monkeypatch.setattr(
            cc_runtime_helpers, "_validate_working_dir", lambda *a, **k: None
        )
        monkeypatch.setattr(
            cc_destination, "_main_working_dir", lambda *a, **k: str(tmp_path)
        )
        monkeypatch.setattr(
            cc_feed_transition,
            "_main_working_dir",
            lambda *a, **k: str(tmp_path),
        )
        monkeypatch.setattr(
            "connections.models.get_connection",
            lambda *a, **k: {"config": {"binary": "claude"}},
        )

        try:
            target = cc_target("sonnet")
            delivered = await feed_with_persisted_input(
                thread_id, "kick off the task",
                target=target, turn_id=_TURN_ID, record=False,
            )
        finally:
            rt._LIVE.pop(thread_id, None)  # don't leak fake into other tests

        assert delivered is not None
        # Cold-start, NOT resume: no --resume key, a freshly minted UUID session.
        assert started.get("resume_session_id") is None
        sid = started.get("session_id")
        _uuid.UUID(sid)  # raises if the CC --session-id is not a valid UUID
        assert sid != thread_id
        # Event routing stays keyed to the CORSIA thread id.
        assert started.get("corsia_thread_id") == thread_id
        # Mapping registered so subsequent sends resume the same CC session.
        art = next(
            (
                row for row in artifact_rows_for_thread(child_db, thread_id)
                if row["integration"] == "claude-code" and row["type"] == "session"
            ),
            None,
        )
        assert art is not None and art["external_id"] == sid

    @pytest.mark.asyncio
    async def test_stop_false_when_not_live(self, child_db):
        import subagents.cc_runtime as rt
        thread_id = "nonexistent-stop"
        rt._LIVE.pop(thread_id, None)

        assert rt._LIVE.existing_lock(thread_id) is None
        assert await rt.stop(
            thread_id, expected_turn_id="missing-turn",
        ) is False
        assert rt._LIVE.existing_lock(thread_id) is None


class TestFeedEdited:
    @pytest.mark.asyncio
    async def test_feed_records_edited_frame(self, child_db, fake_claude):
        import json
        import subagents.cc_runtime as rt

        _seed_parent(child_db)
        child_id = await rt.spawn("parent", "first", binary=fake_claude,
                                  target=cc_target("sonnet"),
                                  broadcast=lambda s, m: None)
        assert await _wait(lambda: (
            (live := rt._LIVE.get(child_id)) is not None
            and live.turn_terminal is not None
            and live.turn_terminal.is_set()
        ))

        target = cc_target("sonnet")
        assert await feed_with_persisted_input(
            child_id, "edited reply", target=target,
            turn_id=_TURN_ID, edited=True,
        ) is not None
        rows = child_db.execute(
            "SELECT content, metadata FROM messages WHERE thread_id=? "
            "AND role='user' ORDER BY created_at, id", (child_id,)).fetchall()
        by_content = {r["content"]: json.loads(r["metadata"] or "{}") for r in rows}
        assert by_content["first"].get("edited") is not True
        assert by_content["edited reply"].get("edited") is True
        await rt.retire(child_id)

    @pytest.mark.asyncio
    async def test_feed_default_not_edited(self, child_db, fake_claude):
        import json
        import subagents.cc_runtime as rt

        _seed_parent(child_db)
        child_id = await rt.spawn("parent", "task", binary=fake_claude,
                                  target=cc_target("sonnet"),
                                  broadcast=lambda s, m: None)
        assert await _wait(lambda: (
            (live := rt._LIVE.get(child_id)) is not None
            and live.turn_terminal is not None
            and live.turn_terminal.is_set()
        ))
        target = cc_target("sonnet")
        assert await feed_with_persisted_input(
            child_id, "plain reply", target=target,
            turn_id=_TURN_ID,
        ) is not None
        row = child_db.execute(
            "SELECT metadata FROM messages WHERE thread_id=? AND content='plain reply'",
            (child_id,)).fetchone()
        assert "edited" not in json.loads(row["metadata"] or "{}")
        await rt.retire(child_id)


class TestStopRace:

    @pytest.mark.asyncio
    async def test_has_pending_or_live_tracks_live_and_spawn_lock(self, child_db, fake_claude):
        import subagents.cc_runtime as rt

        rt._LIVE.reset()
        assert rt.has_pending_or_live("race-sess") is False

        lock = rt._LIVE.lock("race-sess")
        await lock.acquire()
        try:
            assert rt.has_pending_or_live("race-sess") is True
        finally:
            lock.release()

        assert rt.has_pending_or_live("race-sess") is False

    @pytest.mark.asyncio
    async def test_stop_preempts_exact_work_before_waiting_for_transition_lock(
        self, child_db, fake_claude, monkeypatch, tmp_path,
    ):
        from host_access import processes
        import subagents.cc_runtime as rt
        from subagents.cc_runtime_state import activate_bound_turn
        import connections.models as cm
        monkeypatch.setattr(cm, "get_connection",
                            lambda provider, db=None: {"config": {"binary": fake_claude}})
        process_owner = processes.ProcessOwner(
            runtime_dir=tmp_path / "process-owner",
            term_grace_seconds=0.1,
            kill_grace_seconds=1.0,
        )
        owner_cleanup_started = asyncio.Event()
        release_owner_cleanup = asyncio.Event()
        terminate_turn = process_owner.terminate_turn
        async def blocked_cleanup(*args, **kwargs):
            owner_cleanup_started.set()
            await release_owner_cleanup.wait()
            return await terminate_turn(*args, **kwargs)
        monkeypatch.setattr(process_owner, "terminate_turn", blocked_cleanup)
        monkeypatch.setattr(processes, "PROCESS_OWNER", process_owner)
        _seed_parent(child_db)
        child_id = await rt.spawn("parent", "do it", binary=fake_claude,
                                  target=cc_target("sonnet"),
                                  broadcast=lambda s, m: None)
        assert await _wait(lambda: rt.is_live(child_id))
        live = rt._LIVE.get(child_id)
        assert live is not None and live.corsia_turn is not None
        expected_turn_id = live.corsia_turn.turn_id
        # The spawn's own result also cancels admitted tools; let it settle so
        # only Stop can cancel the tool below.
        assert await _wait(lambda: not live.turn_active and child_db.execute(
            "SELECT status FROM active_turns WHERE id=?", (expected_turn_id,),
        ).fetchone()[0] == "completed")
        release_tool = asyncio.Event()
        tool_cancelled = asyncio.Event()
        tool_task = asyncio.create_task(_resistant_tool(release_tool, tool_cancelled))
        live.admitted_tool_tasks.add(tool_task)
        tool_task.add_done_callback(live.admitted_tool_tasks.discard)
        process_task = asyncio.create_task(process_owner.spawn(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            thread_id=child_id,
            turn_id=expected_turn_id,
            cwd=Path.cwd(),
            env=os.environ,
            timeout_seconds=30.0,
            background=False,
        ))
        assert await _wait(
            lambda: bool(process_owner.running_records()), tries=50, delay=0.01,
        )
        lock = rt._LIVE.lock(child_id)
        await lock.acquire()
        await live.event_lock.acquire()
        assert activate_bound_turn(live, live.corsia_turn)
        stop_task = asyncio.create_task(rt.stop(
            child_id, expected_turn_id=expected_turn_id,
        ))
        result = None
        try:
            await asyncio.wait_for(owner_cleanup_started.wait(), timeout=0.5)
            assert not stop_task.done(), "Stop settled before the transition lock"
            assert rt._LIVE.get(child_id) is live
            assert await _wait(
                lambda: live.session.returncode is not None,
                tries=50,
                delay=0.01,
            ), "Stop waited on the transition lock before killing Claude Code"
            assert await _wait(
                tool_cancelled.is_set, tries=50, delay=0.01,
            ), "Stop waited on the transition lock before cancelling its tool task"
            assert process_task.done() is False
            release_owner_cleanup.set()
            live.event_lock.release()
            assert await _wait(
                process_task.done, tries=50, delay=0.01,
            ), "Stop waited on the transition lock before killing its tool process"
        finally:
            release_owner_cleanup.set()
            release_tool.set()
            await tool_task
            if live.event_lock.locked():
                live.event_lock.release()
            lock.release()
            result = await stop_task
            await process_owner.shutdown()
            await process_task
        assert result is True
        assert not rt.is_live(child_id)


class TestDeadChildResume:
    @pytest.mark.asyncio
    async def test_feed_resumes_a_stopped_child(
        self, child_db, fake_claude, monkeypatch, tmp_path,
    ):
        import subagents.cc_runtime as rt
        import connections.models as cm
        isolated_claude_dir = tmp_path / ".claude"
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(isolated_claude_dir))
        monkeypatch.setattr(cm, "get_connection",
                            lambda provider, db=None: {"config": {"binary": fake_claude}})

        _seed_parent(child_db)
        casts: list = []
        child_id = await rt.spawn("parent", "do it", binary=fake_claude,
                                  target=cc_target("sonnet"),
                                  broadcast=lambda s, m: casts.append((s, m)))
        assert await _wait(lambda: rt.is_live(child_id))
        await rt.retire(child_id)
        assert not rt.is_live(child_id)

        target = cc_target("sonnet")
        did = await feed_with_persisted_input(
            child_id, "again", target=target, turn_id=_TURN_ID,
        )
        assert did is not None and rt.is_live(child_id)
        ok = await _wait(lambda: any(
            (r["content"] or "").startswith("echo:again") for r in child_db.execute(
                "SELECT content FROM messages WHERE thread_id=? AND role='assistant'",
                (child_id,)).fetchall()))
        assert ok, "resumed child didn't process the reply"

        # The resume path reconstructs and installs a CC transcript file
        # (cc_context_reconstruction.install_cc_prepared_transcript) — prove
        # it landed under the isolated CC home, never the developer's real one.
        art = next(
            (
                row for row in artifact_rows_for_thread(child_db, child_id)
                if row["integration"] == "claude-code" and row["type"] == "session"
            ),
            None,
        )
        assert art is not None
        from subagents.cc_sessions import resolve_claude_dir
        installed_root = Path(resolve_claude_dir(None))
        assert installed_root == isolated_claude_dir.resolve()
        assert any(installed_root.rglob(f"{art['external_id']}.jsonl")), (
            "resumed child's transcript was not installed under the isolated CC home"
        )
        await rt.retire(child_id)

    @pytest.mark.asyncio
    async def test_feed_cold_starts_fresh_delegated_subagent_end_to_end(
        self, child_db, fake_claude, tmp_path, monkeypatch,
    ):
        """Real-subprocess #501 repro: feeding a fresh delegated CC subagent
        (8-char id, no prior CC session) cold-starts a real process, delivers
        the turn, and persists the reply — instead of dead-ending on the 409."""
        import subagents.cc_runtime as rt
        import subagents.cc_destination as cc_destination
        import subagents.cc_feed_transition as cc_feed_transition
        import subagents.cc_runtime_helpers as cc_runtime_helpers
        import connections.models as cm
        monkeypatch.setattr(cm, "get_connection",
                            lambda provider, db=None: {"config": {"binary": fake_claude}})
        monkeypatch.setattr(
            cc_runtime_helpers, "_validate_working_dir", lambda *a, **k: None
        )
        monkeypatch.setattr(
            cc_destination, "_main_working_dir", lambda *a, **k: str(tmp_path)
        )
        # The stale-cwd respawn gate resolves the session's default the same way;
        # keep the fixture's project-cwd override consistent across both seams so
        # tmp_path (this test's stand-in project dir) is not treated as stale.
        monkeypatch.setattr(
            cc_feed_transition,
            "_main_working_dir",
            lambda *a, **k: str(tmp_path),
        )

        thread_id = "1eb24504"  # 8-char delegation id, never spawned as a CC child
        _seed_parent_row(child_db, sid="parent", user="u")
        child_db.execute(
            "INSERT INTO threads (id, user_id, model, spawned_by, "
            "created_at, updated_at) VALUES (?, 'u', 'claude-code/sonnet', "
            "'parent', 't', 't')",
            (thread_id,),
        )
        child_db.commit()

        target = cc_target("sonnet")
        did = await feed_with_persisted_input(
            thread_id, "ping", target=target,
            turn_id=_TURN_ID, record=False,
        )
        assert did is not None and rt.is_live(thread_id)
        ok = await _wait(lambda: any(
            (r["content"] or "").startswith("echo:ping") for r in child_db.execute(
                "SELECT content FROM messages WHERE thread_id=? AND role='assistant'",
                (thread_id,)).fetchall()))
        assert ok, "cold-started delegated subagent didn't process the first send"
        # A UUID CC session was minted + registered for subsequent resumes.
        import uuid as _uuid
        art = next(
            (
                row for row in artifact_rows_for_thread(child_db, thread_id)
                if row["integration"] == "claude-code" and row["type"] == "session"
            ),
            None,
        )
        assert art is not None
        _uuid.UUID(art["external_id"])
        await rt.retire(thread_id)

    @pytest.mark.asyncio
    async def test_feed_none_for_non_cc_session(self, child_db):
        import subagents.cc_runtime as rt
        child_db.execute(
            "INSERT INTO threads (id, user_id, model, created_at, updated_at) "
            "VALUES ('plain', 'steve', 'anthropic/claude-sonnet-4-5', 't', 't')")
        child_db.commit()
        with pytest.raises(ValueError, match="source model"):
            target = cc_target("sonnet")
            await rt.feed(
                "plain", "hi", target=target, turn_id=_TURN_ID,
                prepared_context=await prepared_cc_input("plain", "hi", target),
            )


class TestLockLifecycle:

    def test_facade_exports_single_lifecycle_owner(self):
        import subagents.cc_runtime as rt
        import subagents.cc_runtime_lifecycle as lifecycle

        assert rt._finalize is lifecycle.finalize
        assert rt._emit_logical_close is lifecycle.emit_logical_close
        assert rt._close_live is lifecycle.retire_live
        assert rt.stop is lifecycle.stop
        assert rt.is_live is lifecycle.is_live
        assert rt.has_pending_or_live is lifecycle.has_pending_or_live
        assert rt.shutdown_all is lifecycle.shutdown_all

    @pytest.mark.asyncio
    async def test_finalize_does_not_touch_spawn_lock(self):
        import types
        import subagents.cc_runtime as rt

        lock = rt._LIVE.lock("lock-test")

        live_stub = types.SimpleNamespace(
            seed_file="/nonexistent/seed",  # OSError on unlink → caught
            parent_thread_id=None,
            admitted_tool_tasks=set(),
        )
        rt._LIVE.put("lock-test", live_stub)

        rt._finalize("lock-test", lambda s, m: None)

        assert "lock-test" not in rt._LIVE
        assert rt._LIVE.existing_lock("lock-test") is lock, (
            "_finalize must NOT pop the spawn lock — model-switch respawn "
            "relies on the same lock object surviving the _finalize call"
        )

    @pytest.mark.asyncio
    async def test_failed_spawn_does_not_remain_pending(self, monkeypatch):
        import subagents.cc_runtime as rt
        import subagents.cc_feed_transition as cc_feed_transition

        rt._LIVE.reset()

        async def _bad_spawn(*a, **kw):
            raise RuntimeError("spawn failed")
        # feed_main reaches the start core through the single accept path; a
        # failed start surfaces from _resume_child (the merged start core).
        monkeypatch.setattr(cc_feed_transition, "resume_child", _bad_spawn)
        monkeypatch.setattr(
            cc_feed_transition, "require_cc_provider_fit", lambda *_args: None
        )

        with pytest.raises(RuntimeError, match="spawn failed"):
            await rt.feed_main(
                "fail-sess", "hi", target=cc_target("opus"), turn_id=_TURN_ID,
            )

        lock = rt._LIVE.existing_lock("fail-sess")
        if lock is not None:
            assert not lock.locked(), "lock must not remain held after failed spawn"
        assert not rt.has_pending_or_live("fail-sess"), \
            "has_pending_or_live must return False after failed spawn"

    @pytest.mark.asyncio
    async def test_stop_without_exact_live_waits_bounded_on_transition_lock(
        self, monkeypatch,
    ):
        import subagents.cc_runtime as rt
        from subagents import live_registry

        monkeypatch.setattr(live_registry, "STOP_LOCK_TIMEOUT_S", 0.02)
        thread_id = "stop-timeout"
        rt._LIVE.reset()
        lock = rt._LIVE.lock(thread_id)
        await lock.acquire()  # hold the lock — simulates a wedged spawn
        try:
            result = await asyncio.wait_for(
                rt.stop(thread_id, expected_turn_id="turn-timeout"),
                timeout=0.1,
            )
            assert result is False
        finally:
            lock.release()
