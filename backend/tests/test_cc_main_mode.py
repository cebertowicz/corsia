from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from subagents.cc_runtime_helpers import CCWorkingDirMissing
from tests.registry_helpers import artifact_rows_for_thread

from tests.cc_main_mode_fixtures import (  # noqa: F401
    fake_claude_crash,
    fake_claude_init_then_reject_resume,
    fake_claude_oneshot,
    fake_claude_toolless_bridge,
    fake_claude_poisoned_resume,
    fake_claude_reject_resume_after_window_once,
    fake_claude_reject_resume_delayed_once,
    fake_claude_reject_resume_once,
    fake_claude_resume_hangs,
    fake_claude_synthetic_resume_needs_fast_feed,
)
from tests.conftest import _wait
from tests.cc_runtime_fixtures import (
    cc_target,
    prepared_cc_input,
    settle_test_input,
)

_BACKEND_DIR = os.path.dirname(os.path.dirname(__file__))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)


def _last_cc_input_frame(live) -> str:
    return live.session.input_frame_log[-1]["message"]["content"]


def _insert_legacy_history(
    db,
    *,
    message_id: str,
    thread_id: str,
    role: str,
    content: str,
    metadata: str | dict | None,
    created_at: str,
) -> None:
    """Persist a pre-Turn-binding fixture through the production allocator."""
    from infra.queries import insert_message

    parsed = json.loads(metadata) if isinstance(metadata, str) else dict(metadata or {})
    parsed["turn_unbound_reason"] = "legacy_import"
    insert_message(
        thread_id,
        role,
        content,
        parsed,
        msg_id=message_id,
        db=db,
        created_at=created_at,
    )


async def _feed_main(
    thread_id: str, text: str, *, model: str = "opus", **kwargs,
):
    from core.turn_core import BOOT_EPOCH
    from infra.thread_driver import admit_input
    from infra.time import utc_now_iso
    import subagents.cc_runtime as rt

    turn_id = kwargs.setdefault("turn_id", str(uuid.uuid4()))
    target = cc_target(model)
    admission = admit_input(
        thread_id,
        role="user",
        content=text,
        intent="apply_now",
        message_id=f"main-mode-input:{turn_id}",
        turn_id=turn_id,
        boot_epoch=BOOT_EPOCH,
        admitted_at=utc_now_iso(),
    )
    if admission.batch is None:
        raise AssertionError(f"test input did not start Turn {turn_id}")
    kwargs.setdefault("preadmitted_turn", admission.batch.identity)
    kwargs.setdefault("persist_input_once", _no_persist)
    kwargs.setdefault(
        "mark_accepted", lambda: settle_test_input(admission.batch),
    )
    if not {
        "prepared_context", "refresh_prepared_context",
    }.intersection(kwargs):
        claude_dir = kwargs.get("claude_dir")

        async def refresh_prepared_context():
            if claude_dir:
                with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": claude_dir}):
                    return await prepared_cc_input(
                        thread_id, text, target, preadmitted_turn_id=turn_id,
                    )
            return await prepared_cc_input(
                thread_id, text, target, preadmitted_turn_id=turn_id,
            )

        kwargs["refresh_prepared_context"] = refresh_prepared_context
    explicit_home = kwargs.get("claude_dir")
    if explicit_home:
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": explicit_home}):
            return await rt.feed_main(thread_id, text, target=target, **kwargs)
    return await rt.feed_main(thread_id, text, target=target, **kwargs)


async def _feed(
    thread_id: str, text: str, *, model: str = "opus", **kwargs,
):
    from core.turn_core import BOOT_EPOCH
    from infra.thread_driver import admit_input
    from infra.time import utc_now_iso
    import subagents.cc_runtime as rt

    turn_id = kwargs.setdefault("turn_id", str(uuid.uuid4()))
    target = cc_target(model)
    admission = admit_input(
        thread_id,
        role="user",
        content=text,
        intent="apply_now",
        message_id=f"child-mode-input:{turn_id}",
        turn_id=turn_id,
        boot_epoch=BOOT_EPOCH,
        admitted_at=utc_now_iso(),
    )
    if admission.batch is None:
        raise AssertionError(f"test input did not start Turn {turn_id}")
    kwargs.setdefault("preadmitted_turn", admission.batch.identity)
    kwargs.setdefault("persist_input_once", _no_persist)
    kwargs.setdefault(
        "mark_accepted", lambda: settle_test_input(admission.batch),
    )
    if not {
        "prepared_context", "refresh_prepared_context",
    }.intersection(kwargs):
        claude_dir = kwargs.get("claude_dir")

        async def refresh_prepared_context():
            if claude_dir:
                with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": claude_dir}):
                    return await prepared_cc_input(
                        thread_id, text, target, preadmitted_turn_id=turn_id,
                    )
            return await prepared_cc_input(
                thread_id, text, target, preadmitted_turn_id=turn_id,
            )

        kwargs["refresh_prepared_context"] = refresh_prepared_context
    return await rt.feed(thread_id, text, target=target, **kwargs)


async def _no_persist() -> None:
    return None


class TestMainMode:
    @pytest.mark.asyncio
    async def test_cold_start_delivers_conductor_template_without_persisting_it(
        self,
        main_db,
        fake_claude,
        tmp_path,
    ):
        from pathlib import Path

        from infra.corsia_agent import set_corsia_agent
        import subagents.cc_runtime as rt

        assert set_corsia_agent("main-1", db=main_db) is True

        await _feed_main(
            "main-1",
            "coordinate this workspace",
            binary=fake_claude,
            claude_dir=str(tmp_path / "empty-claude-home"),
            broadcast=lambda _sid, _message: None,
        )
        live = rt._LIVE.get("main-1")
        assert live is not None
        provider_seed = Path(live.session.seed_file).read_text(encoding="utf-8")
        seed_rows = main_db.execute(
            "SELECT content,metadata FROM messages "
            "WHERE thread_id='main-1' AND role='system'"
        ).fetchall()
        persisted_seed = next(
            row["content"]
            for row in seed_rows
            if json.loads(row["metadata"] or "{}").get("type") == "seed"
        )

        template_marker = "You are the CORSIA agent: the person's one workspace-level agent."
        assert template_marker in provider_seed
        assert template_marker not in persisted_seed
        await rt.retire("main-1")

    @pytest.mark.asyncio
    async def test_feed_main_spawns_with_zero_parent_surfaces(
        self, main_db, fake_claude, tmp_path
    ):
        import subagents.cc_runtime as rt

        casts: list[tuple[str, dict]] = []
        await _feed_main(
            "main-1", "hello there", binary=fake_claude,
            claude_dir=str(tmp_path / "empty-claude-home"),
            broadcast=lambda sid, m: casts.append((sid, m)),
        )
        ok = await _wait(lambda: any(
            m.get("type") == "turn_complete" for _, m in casts))
        assert ok, "no turn_complete from CC main turn"

        types = {m["type"] for sid, m in casts if sid == "main-1"}
        assert "step_text" in types and "turn_complete" in types
        texts = [m["content"] for sid, m in casts
                 if sid == "main-1" and m["type"] == "step_text"]
        # Per-turn frame now carries the dynamic tail after the user text, so the
        # echoed step text is prefixed by the clean user message.
        assert any(t.startswith("echo:hello there") for t in texts)

        assert not any(m["type"] in ("subagent_spawned", "subagent_status")
                       for _, m in casts)
        assert not any(m["type"] == "message_added" for _, m in casts)
        markers = main_db.execute(
            "SELECT metadata FROM messages WHERE metadata IS NOT NULL").fetchall()
        metas = [json.loads(r["metadata"]) for r in markers]
        assert not any(m.get("type") in ("subagent_ref", "subagent_done")
                       for m in metas)

        roles = [r["role"] for r in main_db.execute(
            "SELECT role FROM messages WHERE thread_id = 'main-1'").fetchall()]
        assert roles.count("user") == 1, "accepted input must persist exactly once"
        assert "assistant" in roles and "system" in roles

        assert rt.is_live("main-1")
        frame_text = _last_cc_input_frame(rt._LIVE.get("main-1"))
        assert frame_text.startswith("hello there")

        await _feed_main("main-1", "again", claude_dir=str(tmp_path / "empty-claude-home"))
        ok = await _wait(lambda: any(
            sid == "main-1" and m.get("type") == "step_text"
            and (m.get("content") or "").startswith("echo:again") for sid, m in casts))
        assert ok, "second feed_main did not steer the live process"
        frame_text = _last_cc_input_frame(rt._LIVE.get("main-1"))
        assert frame_text.startswith("again")

        await rt.retire(
            "main-1", broadcast=lambda sid, m: casts.append((sid, m)),
        )
        assert not rt.is_live("main-1")
        assert any(sid == "main-1" and m["type"] == "subagent_closed"
                   for sid, m in casts)
        assert not any(m["type"] == "subagent_status" for _, m in casts)

        await _feed_main(
            "main-1", "back", binary=fake_claude,
            claude_dir=str(tmp_path / "empty-claude-home"),
            broadcast=lambda sid, m: casts.append((sid, m)),
        )
        assert rt.is_live("main-1")
        ok = await _wait(lambda: any(
            sid == "main-1" and m.get("type") == "step_text"
            and (m.get("content") or "").startswith("echo:back") for sid, m in casts))
        assert ok, "feed_main after stop did not respawn"
        await rt.retire("main-1")


class TestMainModeRobustness:
    @pytest.mark.asyncio
    async def test_resume_delivers_conductor_without_duplicate_seed_row(
        self,
        main_db,
        fake_claude,
        tmp_path,
    ):
        from infra.corsia_agent import set_corsia_agent
        import subagents.cc_runtime as rt

        assert set_corsia_agent("main-1", db=main_db)
        home = tmp_path / "cc-home"
        (home / "projects" / "-x").mkdir(parents=True)
        kw = dict(binary=fake_claude, claude_dir=str(home),
                  broadcast=lambda sid, m: None)
        await _feed_main("main-1", "first", **kw)
        cc_sid = rt._LIVE.get("main-1").session.session_id
        assert await rt._close_live("main-1") is True
        (home / "projects" / "-x" / f"{cc_sid}.jsonl").write_text(
            '{"type":"user","cwd":"/x","message":{"role":"user","content":"first"}}\n')
        await _feed_main("main-1", "second", **kw)
        live = rt._LIVE.get("main-1")
        provider_seed = open(live.session.seed_file, encoding="utf-8").read()
        cmd = live.session.build_cmd()
        await rt.retire("main-1")
        seed_rows = main_db.execute(
            "SELECT content,metadata FROM messages WHERE thread_id='main-1' "
            "AND role='system'"
        ).fetchall()
        persisted_seeds = [
            row["content"]
            for row in seed_rows
            if json.loads(row["metadata"] or "{}").get("type") == "seed"
        ]
        template_marker = "You are the CORSIA agent: the person's one workspace-level agent."
        assert "--resume" in cmd and cc_sid in cmd
        assert "--append-system-prompt-file" in cmd
        assert template_marker in provider_seed
        assert len(persisted_seeds) == 1
        assert template_marker not in persisted_seeds[0]


class TestModelSwitch:
    def test_session_model_resolves_through_canonical_target(self, main_db):
        from llm.resolution import ModelUnavailableError, resolve_execution_target

        target = resolve_execution_target("claude-code/opus", db=main_db)
        assert target.catalog_id == "claude-code/opus"
        assert target.transport_model == "opus"
        native = resolve_execution_target(
            "anthropic/claude-3-5-sonnet", db=main_db,
        )
        assert native.harness == "native"
        with pytest.raises(ModelUnavailableError):
            resolve_execution_target(None, db=main_db)

    @pytest.mark.asyncio
    async def test_feed_main_plumbs_alias_to_cc_model_flag(self, main_db, fake_claude, tmp_path):
        import subagents.cc_runtime as rt
        await _feed_main("main-1", "hi", model="opus", binary=fake_claude,
                           claude_dir=str(tmp_path / "empty"),
                           broadcast=lambda s, m: None)
        live = rt._LIVE.get("main-1")
        assert live.model == "opus"
        cmd = live.session.build_cmd()
        assert "--model" in cmd and "opus" in cmd
        await rt.retire("main-1")

    @pytest.mark.asyncio
    async def test_feed_main_seed_file_contains_effective_context_snapshot(
        self, main_db, fake_claude, tmp_path,
    ):
        import subagents.cc_runtime as rt
        main_db.execute(
            "INSERT INTO threads ("
            "id, user_id, title, model, created_at, updated_at, agent_enabled, "
            "agent_responsibility, agent_domains, agent_status"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "agent-release",
                "steve",
                "Release Agent",
                "claude-code/opus",
                "t",
                "t2",
                1,
                "Coordinates release sequencing.",
                json.dumps(["release coordination"]),
                "active",
            ),
        )
        main_db.commit()

        await _feed_main(
            "main-1",
            "hi",
            model="opus",
            binary=fake_claude,
            claude_dir=str(tmp_path / "empty"),
            broadcast=lambda s, m: None,
        )
        live = rt._LIVE.get("main-1")
        seed_text = open(live.session.seed_file, encoding="utf-8").read()

        assert "You are Claude Code running as the agent for a CORSIA chat thread." in seed_text
        # Phase 2: the heavyweight effective-context DIGEST JSON is dropped from
        # the seed; a one-line snapshot reference remains. Context is delivered
        # as one static PREFIX block. The compact responsibility map is installed there;
        # full roster details remain available through the roster tool.
        assert "--- BEGIN CORSIA EFFECTIVE CONTEXT DIGEST ---" not in seed_text
        assert "--- BEGIN CORSIA DYNAMIC CONTEXT DELTA ---" not in seed_text
        assert "--- CORSIA CONTEXT SNAPSHOT: " in seed_text
        assert '"frozen_prompt_material"' not in seed_text
        assert '"cc_seed_text"' not in seed_text
        assert seed_text.count(
            "You are Claude Code running as the agent for a CORSIA chat thread."
        ) == 1
        assert "Release Agent" in seed_text
        assert "Coordinates release sequencing." in seed_text
        # The request-local snapshot still carries the agent facts.
        assert live.context_snapshot["agent_facts"]["agent_count"] == 1
        frame_text = _last_cc_input_frame(live)
        assert frame_text.startswith("hi")
        assert '"frozen_prompt_material"' not in frame_text
        assert '"cc_user_frame_text"' not in frame_text
        assert live.context_snapshot_id
        assert live.context_snapshot["reason"] == "cc-user-frame"
        assert live.context_snapshot["agent_facts"]["agent_count"] == 1
        assert live.context_snapshot["agent_facts"]["agent_thread_ids"] == ["agent-release"]
        assert [artifact["method"] for artifact in live.source_artifacts] == [
            "cc-user-frame",
        ]
        assert live.source_artifacts[0]["harness"] == "claude-code"
        assert live.source_artifacts[0]["snapshot_id"] == live.context_snapshot_id
        assert live.source_artifacts[0]["payload"]["frame_text"] == frame_text
        await rt.retire("main-1")

    @pytest.mark.asyncio
    async def test_feed_main_reconstructs_resume_jsonl_from_corsia_history(
        self, main_db, fake_claude, tmp_path, caplog,
    ):
        import subagents.cc_runtime as rt
        from infra.config import WORKSPACE
        from subagents.cc_context_reconstruction import _transcript_path_for_cwd

        stale_sid = str(uuid.uuid4())
        home = tmp_path / "cc-home"
        stale = home / "projects" / "-x" / f"{stale_sid}.jsonl"
        stale.parent.mkdir(parents=True)
        stale.write_text(json.dumps({
            "type": "user",
            "cwd": "/stale",
            "sessionId": stale_sid,
            "message": {"role": "user", "content": "stale provider-only text"},
        }) + "\n")
        for row in [
            ("u1", "user", "original CORSIA task", '{"cc":true}', "2026-06-30T10:00:00Z"),
            (
                "a1", "assistant", "CORSIA-visible answer",
                '{"cc":true,"reasoning":"secret thinking must not be synthesized"}',
                "2026-06-30T10:01:00Z",
            ),
            ("u2", "user", "latest CORSIA follow-up", '{"cc":true}', "2026-06-30T10:02:00Z"),
        ]:
            _insert_legacy_history(
                main_db,
                message_id=row[0],
                thread_id="main-1",
                role=row[1],
                content=row[2],
                metadata=row[3],
                created_at=row[4],
            )
        main_db.commit()

        await _feed_main(
            "main-1",
            "new live turn",
            binary=fake_claude,
            claude_dir=str(home),
            broadcast=lambda s, m: None,
        )
        live = rt._LIVE.get("main-1")
        cc_sid = live.session.session_id

        assert live.session.resume_session_id == cc_sid
        transcript_path = live.session.reconstructed_transcript_path
        expected_path = _transcript_path_for_cwd(
            claude_dir=str(home),
            working_dir=str(WORKSPACE),
            cc_session_id=cc_sid,
        )
        assert transcript_path == expected_path
        assert transcript_path != str(stale)
        transcript = open(transcript_path, encoding="utf-8").read()
        assert "original CORSIA task" in transcript
        assert "CORSIA-visible answer" in transcript
        assert "latest CORSIA follow-up" in transcript
        assert "stale provider-only text" not in transcript
        assert "secret thinking must not be synthesized" not in transcript
        assert f'"sessionId":"{cc_sid}"' in transcript
        assert "new live turn" not in transcript

        seed_text = open(live.session.seed_file, encoding="utf-8").read()
        # Phase 2: the reconstructed-transcript source is metadata-only — it is
        # recorded in the snapshot inventory (observability), not the seed text
        # (the DIGEST JSON that used to carry it is gone).
        transcript_sources = [
            s for s in live.context_snapshot["source_inventory"]
            if s["source_id"] == "cc-reconstructed-transcript"
        ]
        assert transcript_sources
        assert transcript_sources[0]["stable_reference"] == expected_path
        assert str(stale) not in seed_text
        assert _last_cc_input_frame(live).startswith("new live turn")

        await rt.retire("main-1")
        caplog.clear()
        caplog.set_level(logging.WARNING)
        await _feed_main(
            "main-1",
            "second live turn",
            binary=fake_claude,
            claude_dir=str(home),
            broadcast=lambda s, m: None,
        )
        live = rt._LIVE.get("main-1")
        second_path = live.session.reconstructed_transcript_path
        assert second_path != expected_path
        assert live.session.resume_session_id == live.session.session_id
        assert live.session.session_id in second_path
        assert "stale cwd bucket" not in caplog.text

        await rt.retire("main-1")

    @pytest.mark.asyncio
    async def test_feed_main_creates_reconstructed_resume_jsonl_for_fresh_switch_in(
        self, main_db, fake_claude, tmp_path,
    ):
        import subagents.cc_runtime as rt
        from infra.config import WORKSPACE

        home = tmp_path / "cc-home"
        marker = "fresh switch-in history marker"
        for row in [
            ("u1", "user", marker, '{"native":true}', "2026-06-30T10:00:00Z"),
            (
                "a1", "assistant", "native-visible answer",
                '{"native":true}',
                "2026-06-30T10:01:00Z",
            ),
        ]:
            _insert_legacy_history(
                main_db,
                message_id=row[0],
                thread_id="main-1",
                role=row[1],
                content=row[2],
                metadata=row[3],
                created_at=row[4],
            )
        main_db.commit()

        await _feed_main(
            "main-1",
            "current cc turn",
            binary=fake_claude,
            claude_dir=str(home),
            broadcast=lambda s, m: None,
        )
        live = rt._LIVE.get("main-1")
        cc_sid = live.session.session_id

        assert live.session.resume_session_id == cc_sid
        cmd = live.session.build_cmd()
        assert "--resume" in cmd and cc_sid in cmd
        transcript_path = live.session.reconstructed_transcript_path
        assert transcript_path
        transcript = open(transcript_path, encoding="utf-8").read()
        transcript_lines = [
            json.loads(line)
            for line in transcript.splitlines()
            if line.strip()
        ]
        assert marker in transcript
        assert "native-visible answer" in transcript
        assert "current cc turn" not in transcript
        assert all(line.get("uuid") for line in transcript_lines)
        assert transcript_lines[0]["parentUuid"] is None
        assert transcript_lines[1]["parentUuid"] == transcript_lines[0]["uuid"]
        assert all(line.get("timestamp") for line in transcript_lines)
        assert all(line.get("userType") == "external" for line in transcript_lines)
        assert all(line.get("isSidechain") is False for line in transcript_lines)
        assert all(line.get("cwd") == str(WORKSPACE) for line in transcript_lines)
        assert all(line.get("version") for line in transcript_lines)

        assert [artifact["method"] for artifact in live.source_artifacts] == [
            "cc-reconstructed-transcript",
            "cc-user-frame",
        ]
        assert live.source_artifacts[0]["payload"]["path"] == transcript_path
        assert _last_cc_input_frame(live).startswith("current cc turn")

        row = next(
            (
                row for row in artifact_rows_for_thread(main_db, "main-1")
                if row["integration"] == "claude-code" and row["type"] == "session"
            ),
            None,
        )
        assert row is not None
        assert row["external_id"] == cc_sid
        history = (home / "history.jsonl").read_text(encoding="utf-8")
        history_rows = [json.loads(line) for line in history.splitlines()]
        assert any(
            row.get("sessionId") == cc_sid
            and row.get("project") == str(WORKSPACE)
            and row.get("display") == marker
            for row in history_rows
        )

        await rt.retire("main-1")

    @pytest.mark.asyncio
    async def test_synthetic_resume_is_fed_before_provider_exits_pre_input(
        self, main_db, fake_claude_synthetic_resume_needs_fast_feed, tmp_path,
    ):
        import subagents.cc_runtime as rt

        _insert_legacy_history(
            main_db,
            message_id="u1",
            thread_id="main-1",
            role="user",
            content="history anchor",
            metadata=None,
            created_at="2026-06-30T10:00:00Z",
        )
        main_db.commit()

        casts: list[tuple[str, dict]] = []
        await _feed_main(
            "main-1",
            "synthetic resume turn",
            binary=fake_claude_synthetic_resume_needs_fast_feed,
            claude_dir=str(tmp_path / "cc-home"),
            broadcast=lambda sid, m: casts.append((sid, m)),
        )

        ok = await _wait(lambda: any(
            sid == "main-1"
            and m.get("type") == "step_text"
            and (m.get("content") or "").startswith("echo:synthetic resume turn")
            for sid, m in casts
        ))
        assert ok, "synthetic resume was allowed to exit before feed"
        assert rt._LIVE.get("main-1").synthetic_resume is True
        await rt.retire("main-1")

    @pytest.mark.asyncio
    async def test_feed_main_respawns_on_tier_change(self, main_db, fake_claude, tmp_path):
        import subagents.cc_runtime as rt
        casts: list = []
        home = tmp_path / "cc-home"
        (home / "projects" / "-x").mkdir(parents=True)
        kw = dict(binary=fake_claude, claude_dir=str(home),
                  broadcast=lambda s, m: casts.append((s, m)))
        await _feed_main("main-1", "first", model="opus", **kw)
        assert await _wait(lambda: any(m.get("type") == "turn_complete"
                                       for _, m in casts))
        first_proc = rt._LIVE.get("main-1").session
        assert rt._LIVE.get("main-1").model == "opus"
        cc_sid = first_proc.session_id
        (home / "projects" / "-x" / f"{cc_sid}.jsonl").write_text(
            '{"type":"user","cwd":"/x","message":{"role":"user","content":"first"}}\n')

        main_db.execute(
            "INSERT INTO models "
            "(id,harness,provider,provider_model_id,litellm_model,display_name,"
            "backend,source,context_window,metadata_json,created_at,updated_at) "
            "VALUES ('claude-code/fable','claude-code','claude-code','fable',"
            "'claude-code/fable','Fable','cc','test',967000,"
            "'{\"resolved_identity\":\"claude-fable-5\"}','t','t')"
        )
        main_db.execute(
            "UPDATE threads SET model='claude-code/fable' WHERE id='main-1'"
        )
        main_db.commit()
        await _feed_main("main-1", "second", model="fable", **kw)
        live = rt._LIVE.get("main-1")
        assert live.model == "fable", "live tier did not switch"
        assert live.session is not first_proc, "process was not respawned"
        cmd = live.session.build_cmd()
        assert "--model" in cmd and "fable" in cmd
        assert live.session.session_id == cc_sid
        assert "--resume" in cmd and cc_sid in cmd
        await rt.retire("main-1")

    @pytest.mark.asyncio
    async def test_feed_main_same_tier_steers_live_process(self, main_db, fake_claude, tmp_path):
        import subagents.cc_runtime as rt
        casts: list = []
        kw = dict(binary=fake_claude, claude_dir=str(tmp_path / "empty"),
                  broadcast=lambda s, m: casts.append((s, m)))
        await _feed_main("main-1", "first", model="opus", **kw)
        assert await _wait(lambda: any(m.get("type") == "turn_complete"
                                       for _, m in casts))
        proc = rt._LIVE.get("main-1").session
        assert proc.input_frame_log[-1]["message"]["content"].startswith("first")
        await _feed_main("main-1", "second", model="opus", **kw)
        assert rt._LIVE.get("main-1").session is proc, "same tier should not respawn"
        assert len(proc.input_frame_log) == 2
        assert proc.input_frame_log[-1]["message"]["content"].startswith("second")
        await rt.retire("main-1")

    @pytest.mark.asyncio
    async def test_attached_main_honors_row_model_on_feed(
        self, main_db, fake_claude, tmp_path, monkeypatch,
    ):
        import subagents.cc_runtime as rt
        main_db.execute(
            "INSERT INTO models "
            "(id,harness,provider,provider_model_id,litellm_model,display_name,"
            "backend,source,context_window,metadata_json,created_at,updated_at) "
            "VALUES ('claude-code/fable','claude-code','claude-code','fable',"
            "'claude-code/fable','Fable','cc','test',967000,"
            "'{\"resolved_identity\":\"claude-fable-5\"}','t','t')"
        )
        main_db.execute(
            "INSERT INTO threads (id, user_id, model, "
            "spawned_by, created_at, updated_at) "
            "VALUES ('att-1', 'steve', 'claude-code/fable', NULL, 't', 't')")
        main_db.commit()
        from registry.models import register_artifact
        orig = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        home = tmp_path / "cc-home"
        (home / "projects" / "-x").mkdir(parents=True)
        (home / "projects" / "-x" / f"{orig}.jsonl").write_text(json.dumps(
            {"type": "user", "cwd": str(tmp_path),
             "message": {"role": "user", "content": "hello"}}) + "\n")
        register_artifact(integration="claude-code", type="session",
                          external_id=orig, parent_thread_id="att-1", db=main_db)
        # Isolate attach lookup to this test's fixture home rather than the
        # developer's real ~/.claude.
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
        from connections import models as cm
        import subagents.cc_runtime as _rt
        _rt._default_broadcast  # noqa: B018
        import unittest.mock as _m
        with _m.patch.object(cm, "get_connection",
                             lambda provider, db=None: {"config": {"binary": fake_claude}}):
            ok = await _feed_main("att-1", "carry on", model="fable")
        assert ok
        live = rt._LIVE.get("att-1")
        assert live.model == "fable", "attached main did not honor row tier"
        cmd = live.session.build_cmd()
        assert "--model" in cmd and "fable" in cmd
        await rt.retire("att-1")


class TestAttachedMainResume:
    @pytest.mark.asyncio
    async def test_artifact_session_id_wins_over_uuid5(
        self, main_db, fake_claude, tmp_path, monkeypatch,
    ):
        from pathlib import Path

        from infra.config import WORKSPACE
        from subagents import cc_accept
        from subagents.child_thread import record_user_frame
        import subagents.cc_runtime as rt
        from registry.models import register_artifact

        orig = "11111111-2222-3333-4444-555555555555"
        home = tmp_path / "cc-home"
        live_cwd = Path(WORKSPACE) / "orig-cwd"
        live_cwd.mkdir()
        (home / "projects" / "-x").mkdir(parents=True)
        transcript = home / "projects" / "-x" / f"{orig}.jsonl"
        transcript.write_text(json.dumps({
            "type": "user", "cwd": str(live_cwd),
            "message": {"role": "user", "content": "hello"},
        }) + "\n")
        register_artifact(integration="claude-code", type="session",
                          external_id=orig, parent_thread_id="main-1",
                          metadata={"cwd": str(live_cwd)}, db=main_db)
        record_user_frame(
            "main-1", "hello", db=main_db,
            created_at="2026-06-09T10:00:00Z", turn_id=None,
            turn_unbound_reason="legacy_import",
        )
        main_db.commit()

        await _feed_main("main-1", "go", turn_id="turn-attached-main", binary=fake_claude,
                           claude_dir=str(home), broadcast=lambda s, m: None)
        live = rt._LIVE.get("main-1")
        assert live.session.resume_session_id == orig
        cmd = live.session.build_cmd()
        assert "--resume" in cmd and orig in cmd
        assert live.session.working_dir == str(live_cwd)
        material = live.context_snapshot["frozen_prompt_material"]
        assert material["history"] == []
        await rt.retire("main-1")

    @pytest.mark.asyncio
    async def test_first_spawn_registers_fresh_artifact(self, main_db, fake_claude, tmp_path):
        import uuid

        import subagents.cc_runtime as rt
        await _feed_main("main-1", "hi", binary=fake_claude,
                           claude_dir=str(tmp_path / "empty-home"),
                           broadcast=lambda s, m: None)
        provider_id = rt._LIVE.get("main-1").session.session_id
        row = next(
            (
                row for row in artifact_rows_for_thread(main_db, "main-1")
                if row["integration"] == "claude-code" and row["type"] == "session"
            ),
            None,
        )
        assert row is not None
        assert row["external_id"] == provider_id
        assert str(uuid.UUID(provider_id)) == provider_id
        await rt.retire("main-1")


class TestCCSessionId:
    @pytest.mark.asyncio
    async def test_spawn_main_passes_uuid_not_slug_to_cc(self, main_db, fake_claude):
        import uuid as _uuid
        import subagents.cc_runtime as rt
        await _feed_main("main-1", "hi", binary=fake_claude,
                           claude_dir="/nonexistent", broadcast=lambda s, m: None)
        cc_sid = rt._LIVE.get("main-1").session.session_id
        assert cc_sid != "main-1"
        assert str(_uuid.UUID(cc_sid)) == cc_sid
        await rt.retire("main-1")


class TestMainWorkingDir:
    def test_no_project_falls_back_to_workspace(self, main_db):
        from infra.config import WORKSPACE
        from subagents.cc_runtime_helpers import _main_working_dir
        assert _main_working_dir(None, main_db) == str(WORKSPACE)

    def test_project_planning_root_wins(self, main_db, monkeypatch):
        from infra.config import WORKSPACE
        from subagents.cc_runtime_helpers import _main_working_dir
        from tests.project_ref_helpers import project_ref, select_workspace

        select_workspace(monkeypatch, WORKSPACE)
        main_db.execute(
            "INSERT INTO projects (id, name, path, created_at, updated_at) "
            "VALUES ('p1', 'test', ?, 't', 't')",
            (project_ref("proj"),),
        )
        main_db.commit()
        assert _main_working_dir("p1", main_db) == str(
            (WORKSPACE / "projects" / "proj").resolve()
        )

    def test_missing_project_row_fails_loudly(self, main_db):
        from subagents.cc_runtime_helpers import _main_working_dir
        with pytest.raises(CCWorkingDirMissing, match="Project working directory unresolved"):
            _main_working_dir("nope", main_db)

    @pytest.mark.asyncio
    async def test_spawned_main_chat_runs_in_workspace(self, main_db, fake_claude):
        import subagents.cc_runtime as rt
        from infra.config import WORKSPACE
        await _feed_main("main-1", "hi", binary=fake_claude,
                           claude_dir="/nonexistent", broadcast=lambda s, m: None)
        assert rt._LIVE.get("main-1").session.working_dir == str(WORKSPACE)
        seed = main_db.execute(
            "SELECT content FROM messages WHERE thread_id='main-1' "
            "AND role='system' AND content LIKE 'You are Claude Code%'"
        ).fetchone()["content"]
        assert f"Working directory: {WORKSPACE}" in seed
        await rt.retire("main-1")


def _seed_native_resume(main_db, tmp_path, monkeypatch) -> tuple[list[int], str]:
    """Seed CORSIA history plus a provider transcript Claude will resume."""
    from infra.config import WORKSPACE
    from subagents import cc_startup_process
    from subagents.cc_context_reconstruction import _transcript_path_for_cwd

    original_new_live = cc_startup_process._new_live
    launches: list[int] = []

    def provider_then_rebuilt_live(*args, **kwargs):
        launches.append(len(launches) + 1)
        live = original_new_live(*args, **kwargs)
        if len(launches) == 1:
            # The canonical preflight labels its installed transcript as
            # synthetic; the first launch must be a provider-native resume,
            # whose verdict the feed waits for after writing the input.
            live.synthetic_resume = False
        return live

    monkeypatch.setattr(cc_startup_process, "_new_live", provider_then_rebuilt_live)
    now = "2026-07-02T13:41:00Z"
    for message_id, role, content in (
        ("prior-user", "user", "prior anchor"),
        ("prior-assistant", "assistant", "prior reply"),
    ):
        _insert_legacy_history(
            main_db, message_id=message_id, thread_id="main-1", role=role,
            content=content, metadata=None, created_at=now,
        )
    main_db.commit()
    cc_sid = str(uuid.uuid4())
    transcript_path = _transcript_path_for_cwd(
        claude_dir=str(tmp_path / "claude"),
        working_dir=str(WORKSPACE),
        cc_session_id=cc_sid,
    )
    os.makedirs(os.path.dirname(transcript_path), exist_ok=True)
    with open(transcript_path, "w", encoding="utf-8") as f:
        f.write('{"type":"user","sessionId":"%s","message":{"role":"user","content":"stale"}}\n' % cc_sid)
    return launches, cc_sid


def _track_resume_verdict_wait(monkeypatch) -> asyncio.Event:
    """Signal once the feed is inside its wait for Claude's resume verdict."""
    from subagents import cc_feed

    entered = asyncio.Event()
    original = cc_feed._await_resume_verdict

    async def tracked(live, verdict):
        entered.set()
        return await original(live, verdict)

    monkeypatch.setattr(cc_feed, "_await_resume_verdict", tracked)
    return entered


class TestLoudFailure:
    @pytest.mark.parametrize(
        "fake_claude_fixture",
        [
            "fake_claude_reject_resume_once",
            "fake_claude_reject_resume_delayed_once",
            "fake_claude_reject_resume_after_window_once",
        ],
    )
    @pytest.mark.asyncio
    async def test_rejected_resume_rebuilds_from_corsia_history_and_retries_once(
        self, main_db, fake_claude_fixture, tmp_path, request, monkeypatch
    ):
        import subagents.cc_runtime as rt
        fake_claude = request.getfixturevalue(fake_claude_fixture)
        _, cc_sid = _seed_native_resume(main_db, tmp_path, monkeypatch)

        casts: list[tuple[str, dict]] = []
        await _feed_main(
            "main-1",
            "resume after rejection",
            binary=fake_claude,
            claude_dir=str(tmp_path / "claude"),
            broadcast=lambda sid, m: casts.append((sid, m)),
        )

        ok = await _wait(lambda: any(
            sid == "main-1"
            and m.get("type") == "step_text"
            and (m.get("content") or "").startswith("echo:resume after rejection")
            for sid, m in casts
        ))
        assert ok, "turn did not retry after the rejected Claude Code resume"
        assert not any(
            m.get("message", {}).get("metadata", {}).get("type") == "cc_error"
            and "No conversation found" in m.get("message", {}).get("content", "")
            for _, m in casts
        )
        live = rt._LIVE.get("main-1")
        rebuilt_path = live.session.reconstructed_transcript_path
        rebuilt = open(rebuilt_path, encoding="utf-8").read()
        assert "prior anchor" in rebuilt
        assert live.session.resume_session_id == live.session.session_id
        assert live.session.session_id != cc_sid
        await rt.retire("main-1")

    @pytest.mark.asyncio
    async def test_stop_during_resume_verdict_wait_ends_turn_without_rebuild(
        self, main_db, fake_claude_resume_hangs, tmp_path, monkeypatch
    ):
        import subagents.cc_runtime as rt

        launches, _ = _seed_native_resume(main_db, tmp_path, monkeypatch)
        feed = asyncio.create_task(_feed_main(
            "main-1", "never answered", binary=fake_claude_resume_hangs,
            claude_dir=str(tmp_path / "claude"),
            broadcast=lambda *_args: None,
        ))
        try:
            assert await _wait(lambda: (
                (live := rt._LIVE.get("main-1")) is not None
                and live.resume_verdict is not None
                and live.turn_active
                and live.session.input_frame_log
            )), "feed never wrote the input and waited for Claude's verdict"
            live = rt._LIVE.get("main-1")
            turn_id = live.corsia_turn.turn_id
            assert rt._LIVE.lock("main-1").locked()

            assert await asyncio.wait_for(
                rt.stop("main-1", expected_turn_id=turn_id), timeout=15,
            )
            await asyncio.wait_for(feed, timeout=15)
        finally:
            if not feed.done():
                feed.cancel()
        assert launches == [1], "Stop must not rebuild the stopped resume"
        assert live.resume_verdict is None
        assert not rt._LIVE.lock("main-1").locked()
        assert rt._LIVE.get("main-1") is None
        turn = main_db.execute(
            "SELECT status FROM active_turns WHERE id=?", (turn_id,),
        ).fetchone()
        assert turn["status"] == "cancelled"

    @pytest.mark.asyncio
    async def test_cancelled_resume_verdict_wait_releases_lock_and_later_refusal_ends_turn(
        self, main_db, fake_claude_resume_hangs, tmp_path, monkeypatch
    ):
        import subagents.cc_runtime as rt

        launches, cc_sid = _seed_native_resume(main_db, tmp_path, monkeypatch)
        waiting = _track_resume_verdict_wait(monkeypatch)
        casts: list[tuple[str, dict]] = []
        feed = asyncio.create_task(_feed_main(
            "main-1", "never answered", binary=fake_claude_resume_hangs,
            claude_dir=str(tmp_path / "claude"),
            broadcast=lambda sid, m: casts.append((sid, m)),
        ))
        live = None
        try:
            await asyncio.wait_for(waiting.wait(), timeout=15)
            await asyncio.sleep(0.05)
            live = rt._LIVE.get("main-1")
            turn_id = live.corsia_turn.turn_id

            feed.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(feed, timeout=15)
            assert live.resume_verdict is None
            assert not rt._LIVE.lock("main-1").locked()
            assert live.turn_active

            await live.session._on_exit(
                1, f"No conversation found with session ID: {cc_sid}\n",
            )
            assert await _wait(lambda: any(
                (m.get("message", {}).get("metadata") or {}).get("type") == "cc_error"
                for _, m in casts
            )), "a refusal after the feed left must end its Turn visibly"
            turn = main_db.execute(
                "SELECT status FROM active_turns WHERE id=?", (turn_id,),
            ).fetchone()
            assert turn["status"] == "error"
            assert launches == [1]
        finally:
            if not feed.done():
                feed.cancel()
            if live is not None and live.session is not None:
                await live.session.terminate()

    @pytest.mark.asyncio
    async def test_cancel_after_refusal_settled_the_verdict_still_ends_turn(
        self, main_db, fake_claude_resume_hangs, tmp_path, monkeypatch
    ):
        import subagents.cc_runtime as rt
        from subagents import cc_startup_process

        launches, cc_sid = _seed_native_resume(main_db, tmp_path, monkeypatch)
        waiting = _track_resume_verdict_wait(monkeypatch)
        feed_task: list[asyncio.Task] = []
        original_settle = cc_startup_process.settle_resume_verdict

        def settle_then_cancel(live, verdict):
            # Cancel in the same step the refusal hands over its terminal,
            # before the waiting feed can resume.
            settled = original_settle(live, verdict)
            feed_task[0].cancel()
            return settled

        monkeypatch.setattr(
            cc_startup_process, "settle_resume_verdict", settle_then_cancel,
        )
        casts: list[tuple[str, dict]] = []
        feed = asyncio.create_task(_feed_main(
            "main-1", "never answered", binary=fake_claude_resume_hangs,
            claude_dir=str(tmp_path / "claude"),
            broadcast=lambda sid, m: casts.append((sid, m)),
        ))
        feed_task.append(feed)
        live = None
        try:
            await asyncio.wait_for(waiting.wait(), timeout=15)
            await asyncio.sleep(0.05)
            live = rt._LIVE.get("main-1")
            turn_id = live.corsia_turn.turn_id

            await live.session._on_exit(
                1, f"No conversation found with session ID: {cc_sid}\n",
            )
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(feed, timeout=15)
            assert await _wait(lambda: any(
                (m.get("message", {}).get("metadata") or {}).get("type") == "cc_error"
                for _, m in casts
            )), "a cancelled feed must still end the refused Turn visibly"
            turn = main_db.execute(
                "SELECT status FROM active_turns WHERE id=?", (turn_id,),
            ).fetchone()
            assert turn["status"] == "error"
            assert not rt._LIVE.lock("main-1").locked()
            assert launches == [1]
        finally:
            if not feed.done():
                feed.cancel()
            if live is not None and live.session is not None:
                await live.session.terminate()

    @pytest.mark.parametrize("dropped_by", ["no_identity", "replaced_live", "accept_raises"])
    @pytest.mark.asyncio
    async def test_refusal_during_accept_runs_its_terminal_once(
        self, main_db, fake_claude_resume_hangs, tmp_path, monkeypatch, dropped_by
    ):
        import subagents.cc_runtime as rt
        from subagents import cc_feed, cc_resume_transition, cc_startup_process

        launches, cc_sid = _seed_native_resume(main_db, tmp_path, monkeypatch)
        original_accept = cc_feed._record_and_accept
        original_finish = cc_startup_process._finish_process_exit
        original_retire = cc_resume_transition.retire_rejected_provider_session
        finished: list[int] = []
        retired: list[str | None] = []
        seen: list = []

        async def counted_finish(*args, **kwargs):
            finished.append(1)
            await original_finish(*args, **kwargs)

        def counted_retire(thread_id, missing_session_id, db):
            retired.append(missing_session_id)
            return original_retire(thread_id, missing_session_id, db)

        async def refused_while_accepting(transition, live, acceptance):
            # Claude refuses the resume before the accept await returns, so
            # the exit callback hands its terminal to this feed and returns.
            identity = await original_accept(transition, live, acceptance)
            seen.append((live, identity))
            await live.session._on_exit(
                1, f"No conversation found with session ID: {cc_sid}\n",
            )
            assert live.resume_verdict.done(), "refusal was not handed to the feed"
            if dropped_by == "no_identity":
                return None
            if dropped_by == "replaced_live":
                rt._LIVE.pop(transition.thread_id)
                return identity
            raise RuntimeError("accept failed after the refusal")

        monkeypatch.setattr(cc_startup_process, "_finish_process_exit", counted_finish)
        monkeypatch.setattr(
            cc_resume_transition, "retire_rejected_provider_session", counted_retire,
        )
        monkeypatch.setattr(cc_feed, "_record_and_accept", refused_while_accepting)
        feed = _feed_main(
            "main-1", "never answered", binary=fake_claude_resume_hangs,
            claude_dir=str(tmp_path / "claude"), broadcast=lambda *_args: None,
        )
        try:
            if dropped_by == "accept_raises":
                with pytest.raises(RuntimeError, match="accept failed"):
                    await asyncio.wait_for(feed, timeout=15)
            else:
                await asyncio.wait_for(feed, timeout=15)
            live, identity = seen[0]
            assert finished == [1], "the handed-off refusal terminal must run once"
            assert retired == [live.session.resume_session_id], (
                "the refused session was not retired"
            )
            assert live.resume_verdict is None
            assert rt._LIVE.get("main-1") is None, "the dead handle stayed bound"
            if dropped_by != "replaced_live":
                # A replaced handle's Turn belongs to whoever replaced it.
                turn = main_db.execute(
                    "SELECT status FROM active_turns WHERE id=?", (identity.turn_id,),
                ).fetchone()
                assert turn["status"] == "error"
            assert not rt._LIVE.lock("main-1").locked()
            assert launches == [1]
        finally:
            if seen and seen[0][0].session is not None:
                await seen[0][0].session.terminate()

    @pytest.mark.asyncio
    async def test_rejection_after_provider_output_stays_visible_without_rebuild(
        self, main_db, fake_claude_init_then_reject_resume, tmp_path, monkeypatch
    ):
        import subagents.cc_runtime as rt

        launches, _ = _seed_native_resume(main_db, tmp_path, monkeypatch)
        casts: list[tuple[str, dict]] = []
        await _feed_main(
            "main-1", "read then refused",
            binary=fake_claude_init_then_reject_resume,
            claude_dir=str(tmp_path / "claude"),
            broadcast=lambda sid, m: casts.append((sid, m)),
        )
        assert await _wait(lambda: any(
            (m.get("message", {}).get("metadata") or {}).get("type") == "cc_error"
            and "No conversation found" in m.get("message", {}).get("content", "")
            for _, m in casts
        )), "a rejection after Claude spoke must keep its visible error"
        assert await _wait(lambda: rt._LIVE.get("main-1") is None)
        assert launches == [1], "Claude may have read the input; never redeliver it"

    @pytest.mark.asyncio
    async def test_empty_text_block_rejection_retires_only_the_poisoned_session(
        self, main_db, fake_claude_poisoned_resume, tmp_path, monkeypatch
    ):
        """#1138: an empty assistant turn in Claude's transcript must not be resumed again."""
        import subagents.cc_runtime as rt
        from registry.listing import find_for_thread
        from registry.service import register_cc_session

        launches, cc_sid = _seed_native_resume(main_db, tmp_path, monkeypatch)
        claude_dir = str(tmp_path / "claude")
        transcript = next((tmp_path / "claude").rglob(f"{cc_sid}.jsonl"))
        with open(transcript, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "type": "assistant", "sessionId": cc_sid,
                "message": {
                    "role": "assistant", "content": [{"type": "text", "text": ""}],
                    "stop_reason": "end_turn", "model": "claude-opus-5",
                },
            }) + "\n")
        register_cc_session("main-1", cc_sid, db=main_db)
        main_db.commit()
        casts: list[tuple[str, dict]] = []
        await _feed_main(
            "main-1", "first after the empty turn", binary=fake_claude_poisoned_resume,
            claude_dir=claude_dir, broadcast=lambda sid, m: casts.append((sid, m)),
        )
        assert await _wait(lambda: any(
            (m.get("message", {}).get("metadata") or {}).get("cause")
            == "provider_transcript_poisoned"
            for _, m in casts
        )), "the poisoned rejection must end the Turn visibly with its own cause"
        assert find_for_thread("main-1", "claude-code", "session", db=main_db) is None, (
            "the poisoned provider session stayed registered for the next resume"
        )
        live = rt._LIVE.get("main-1")
        assert live is None or live.provider_context_untrusted
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": claude_dir}):
            prepared = await prepared_cc_input("main-1", "second", cc_target("opus"))
        assert prepared.mode != "provider_resume", (
            "the next input must not resume the poisoned session"
        )
        assert launches == [1]
        if live is not None and live.session is not None:
            await live.session.terminate()

    @pytest.mark.asyncio
    async def test_ready_but_toolless_corsia_bridge_never_runs_the_turn(
        self, main_db, fake_claude_toolless_bridge, tmp_path
    ):
        """#1137: a ready CORSIA server with zero CORSIA tools is refused, never run toolless."""
        import subagents.cc_runtime as rt

        casts: list[tuple[str, dict]] = []
        identity = await _feed_main(
            "main-1", "needs tools", binary=fake_claude_toolless_bridge,
            claude_dir=str(tmp_path / "empty-claude-home"),
            broadcast=lambda sid, m: casts.append((sid, m)),
        )
        assert await _wait(lambda: any(
            (m.get("message", {}).get("metadata") or {}).get("type") == "cc_error"
            for _, m in casts
        )), "the refused Turn must end visibly"
        assert not any(
            m.get("type") == "step_text" and "toolless answer" in (m.get("content") or "")
            for _, m in casts
        ), "the Turn ran toolless"
        turn = main_db.execute(
            "SELECT status FROM active_turns WHERE id=?", (identity.turn_id,),
        ).fetchone()
        assert turn["status"] == "error"
        assert await _wait(lambda: rt._LIVE.get("main-1") is None)

    @pytest.mark.parametrize("model", ["fable", "opus"])
    @pytest.mark.asyncio
    async def test_next_input_replaces_unowned_rejected_resume_on_its_target(
        self, main_db, fake_claude_reject_resume_once, tmp_path, monkeypatch, model
    ):
        import subagents.cc_runtime as rt
        from subagents.cc_feed_transition import CCFeedTransition

        launches, _ = _seed_native_resume(main_db, tmp_path, monkeypatch)
        claude_dir = str(tmp_path / "claude")
        opus = cc_target("opus")

        async def refresh():
            with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": claude_dir}):
                return await prepared_cc_input("main-1", "warm", opus)

        # Claude refuses a resume no feed owns yet: the dead handle stays bound.
        spawn = CCFeedTransition(
            thread_id="main-1", text="warm", target=opus, effort=None,
            claude_dir=claude_dir, binary=fake_claude_reject_resume_once,
            broadcast=None, is_subagent=False, bcast=lambda *_args: None,
            db=None, prepared_context=None, refresh_callback=refresh,
            held_lock=rt._LIVE.lock("main-1"),
        )
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": claude_dir}):
            async with spawn.held_lock:
                rejected = await spawn.resume()
        assert await _wait(lambda: rejected.resume_rejected)
        assert rt._LIVE.get("main-1") is rejected

        if model == "fable":
            main_db.execute(
                "INSERT INTO models "
                "(id,harness,provider,provider_model_id,litellm_model,display_name,"
                "backend,source,context_window,metadata_json,created_at,updated_at) "
                "VALUES ('claude-code/fable','claude-code','claude-code','fable',"
                "'claude-code/fable','Fable','cc','test',967000,"
                "'{\"resolved_identity\":\"claude-fable-5\"}','t','t')"
            )
            main_db.execute(
                "UPDATE threads SET model='claude-code/fable' WHERE id='main-1'"
            )
            main_db.commit()
        casts: list[tuple[str, dict]] = []
        await _feed_main(
            "main-1", f"answer on {model}", model=model, effort="high",
            binary=fake_claude_reject_resume_once, claude_dir=claude_dir,
            broadcast=lambda sid, m: casts.append((sid, m)),
        )

        assert await _wait(lambda: any(
            m.get("type") == "step_text"
            and (m.get("content") or "").startswith(f"echo:answer on {model}")
            for _, m in casts
        ))
        live = rt._LIVE.get("main-1")
        assert live is not rejected
        assert live.model == model
        assert live.catalog_model_id == f"claude-code/{model}"
        cmd = live.session.build_cmd()
        assert cmd[cmd.index("--model") + 1] == model
        assert cmd[cmd.index("--effort") + 1] == "high"
        # The missing session is retired first, so it is never resumed again.
        assert rejected.session.resume_session_id not in cmd
        assert launches == [1, 2]
        await rt.retire("main-1")

    @pytest.mark.asyncio
    async def test_cc_crash_surfaces_error_to_canvas(self, main_db, fake_claude_crash):
        import subagents.cc_runtime as rt

        casts: list[tuple[str, dict]] = []
        await _feed_main("main-1", "hi", binary=fake_claude_crash,
                           claude_dir="/nonexistent",
                           broadcast=lambda sid, m: casts.append((sid, m)))
        ok = await _wait(lambda: any(
            sid == "main-1" and m.get("type") == "message_added"
            and (m["message"].get("metadata") or {}).get("type") == "cc_error"
            for sid, m in casts))
        assert ok, "no cc_error surfaced on CC crash"
        err = next(m for sid, m in casts if m.get("type") == "message_added"
                   and (m["message"].get("metadata") or {}).get("type") == "cc_error")
        assert "Invalid session ID" in err["message"]["content"]
        row = main_db.execute(
            "SELECT content FROM messages WHERE thread_id='main-1' "
            "AND metadata LIKE '%cc_error%'").fetchone()
        assert row is not None
        assert any(sid == "main-1" and m.get("type") == "turn_complete" for sid, m in casts)
        # The handle is retired by the exit handler's finally, which can land
        # after turn_complete is broadcast; wait for the retirement rather than
        # racing it.
        assert await _wait(lambda: not rt.is_live("main-1")), "live handle survived the crash"

    @pytest.mark.asyncio
    async def test_clean_exit_surfaces_nothing(self, main_db, fake_claude_oneshot):
        import subagents.cc_runtime as rt
        casts: list[tuple[str, dict]] = []
        await _feed_main("main-1", "once", binary=fake_claude_oneshot,
                           claude_dir="/nonexistent",
                           broadcast=lambda sid, m: casts.append((sid, m)))
        await _wait(lambda: not rt.is_live("main-1"))
        assert not any(m.get("type") == "message_added"
                       and (m["message"].get("metadata") or {}).get("type") == "cc_error"
                       for sid, m in casts)
