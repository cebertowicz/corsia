
from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import json  # noqa: E402

from core.step_handler import StepHandler  # noqa: E402
from core.turn_core import TurnIdentity # noqa: E402
from infra import config  # noqa: E402
from infra.database import get_db  # noqa: E402
from registry import service  # noqa: E402
from registry.observer import observe_record  # noqa: E402
from tests.registry_helpers import (  # noqa: E402
    artifact_rows_for_thread,
    cc_child,
    make_session,
)

def _mk_session(db) -> str:
    return make_session(db, prefix="obs")


def _workspace() -> Path:
    return config.WORKSPACE


def _artifacts(db, sid: str) -> list[dict]:
    return artifact_rows_for_thread(db, sid)


async def _noop_broadcast(_msg: dict) -> None:
    pass


def _handler(sid: str) -> StepHandler:
    handler = StepHandler(
        sid, {"model": "m", "model_name": "m", "is_cloud": False},
        _noop_broadcast, turn_id="turn-test",
    )
    handler.bind_turn_identity(TurnIdentity("turn-test", 1))
    return handler


async def _native_call(sh: StepHandler, name: str, args: dict, result: str,
                       tc_id: str = "tc1") -> None:
    await sh.handle("tool_calls_start", {
        "tools": [{"id": tc_id, "name": name, "hint": "", "args": ""}],
        "content": "",
        "tool_call_dicts": [{
            "id": tc_id, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)},
        }],
    })
    await sh.handle("tool_complete", {"id": tc_id, "name": name, "result": result})


class TestNativeSource:
    async def test_write_file_success_registers_with_description(self):
        db = get_db()
        sid = _mk_session(db)
        p = _workspace() / "notes" / f"{uuid.uuid4().hex[:6]}.md"
        await _native_call(_handler(sid), "write_file",
                           {"path": str(p), "content": "x", "description": "a doc"},
                           f"Successfully wrote 1 bytes to {p}")
        arts = _artifacts(db, sid)
        assert len(arts) == 1
        assert arts[0]["type"] == "document"
        assert arts[0]["description"] == "a doc"
        assert json.loads(arts[0]["metadata"])["zone"] == "workspace"

    async def test_write_file_through_tool_call_registers_like_a_direct_call(self):
        from tools.tool_surface import dispatch_metadata

        db = get_db()
        sid = _mk_session(db)
        p = _workspace() / "notes" / f"{uuid.uuid4().hex[:6]}.md"
        args = {
            "name": "write_file",
            "arguments": {"path": str(p), "content": "x", "description": "via"},
        }
        sh = _handler(sid)
        await sh.handle("tool_calls_start", {
            "tools": [{"id": "tc1", "name": "tool_call", "hint": "", "args": ""}],
            "content": "",
            "tool_call_dicts": [{
                "id": "tc1", "type": "function",
                "function": {"name": "tool_call", "arguments": json.dumps(args)},
            }],
        })
        await sh.handle("tool_complete", {
            "id": "tc1", "name": "tool_call",
            "result": f"Successfully wrote 1 bytes to {p}",
            **dispatch_metadata("tool_call", args),
        })
        arts = _artifacts(db, sid)
        assert len(arts) == 1
        assert arts[0]["description"] == "via"

    async def test_workspace_relative_path_resolves(self):
        db = get_db()
        sid = _mk_session(db)
        rel = f"notes/{uuid.uuid4().hex[:6]}.md"
        await _native_call(_handler(sid), "write_file",
                           {"path": rel, "content": "x"},
                           f"Successfully wrote 1 bytes to {_workspace() / rel}")
        arts = _artifacts(db, sid)
        assert len(arts) == 1
        assert arts[0]["external_id"].startswith("workspace://")
        assert arts[0]["path"] == str((_workspace() / rel).resolve())
        assert arts[0]["description"] is None  # pending — hygiene backfills

    async def test_basename_write_registers_materialized_thread_working_path(self):
        db = get_db()
        sid = _mk_session(db)
        basename = f"scratch-{uuid.uuid4().hex[:6]}.md"
        target = _workspace() / ".threads" / sid / "working" / basename
        target.parent.mkdir(parents=True)
        target.write_text("x", encoding="utf-8")

        await _native_call(
            _handler(sid),
            "write_file",
            {"path": basename, "content": "x"},
            f"Successfully wrote 1 bytes to {target}",
        )

        arts = _artifacts(db, sid)
        assert len(arts) == 1
        assert arts[0]["path"] == str(target.resolve())
        assert arts[0]["retention"] == "ephemeral"
        assert arts[0]["path"] != str((_workspace() / basename).resolve())

    async def test_edit_file_registers_pending(self):
        db = get_db()
        sid = _mk_session(db)
        p = _workspace() / f"{uuid.uuid4().hex[:6]}.py"
        await _native_call(_handler(sid), "edit_file",
                           {"path": str(p), "old_string": "a", "new_string": "b"},
                           f"Successfully edited {p}")
        arts = _artifacts(db, sid)
        assert len(arts) == 1 and arts[0]["type"] == "code"

    async def test_multi_edit_registers_pending(self):
        db = get_db()
        sid = _mk_session(db)
        p = _workspace() / f"{uuid.uuid4().hex[:6]}.py"
        await _native_call(
            _handler(sid),
            "multi_edit",
            {"path": str(p), "edits": [{"old_string": "a", "new_string": "b"}]},
            f"Successfully applied 1 edit(s), 1 replacement(s), to {p}",
        )
        arts = _artifacts(db, sid)
        assert len(arts) == 1 and arts[0]["type"] == "code"

    async def test_error_warning_and_cancelled_results_do_not_register(self):
        db = get_db()
        sid = _mk_session(db)
        p = str(_workspace() / "x.md")
        for i, result in enumerate([
            f"Error: File not found: {p}",
            "Warning: old_string appears 3 times. Provide more context.",
            "[Tool execution was cancelled]",
        ]):
            await _native_call(_handler(sid), "edit_file",
                               {"path": p, "old_string": "a", "new_string": "b"},
                               result, tc_id=f"tc{i}")
        assert _artifacts(db, sid) == []

    async def test_non_writer_tool_ignored(self):
        db = get_db()
        sid = _mk_session(db)
        await _native_call(_handler(sid), "read_file",
                           {"path": str(_workspace() / "x.md")}, "contents...")
        assert _artifacts(db, sid) == []


class TestCCSource:
    async def _cc(self, db, parent: str):
        return cc_child(db, parent)

    async def _start(self, on_event, tool_id: str, name: str, arguments: dict):
        await on_event({
            "kind": "assistant_step",
            "text": "",
            "reasoning": "",
            "tool_uses": [{
                "id": tool_id, "name": name, "input": arguments,
            }],
        })

    async def test_cc_write_registers_to_child_session(self):
        db = get_db()
        parent = _mk_session(db)
        child, on_event = await self._cc(db, parent)
        p = _workspace() / "working" / f"{uuid.uuid4().hex[:6]}.md"
        await self._start(
            on_event,
            "t1",
            "mcp__corsia__write_file",
            {"path": str(p), "content": "x"},
        )
        await on_event({"kind": "tool_result", "tool_use_id": "t1",
                        "content": f"File created successfully at: {p}",
                        "is_error": False})
        arts = _artifacts(db, child)
        assert len(arts) == 1
        assert arts[0]["retention"] == "ephemeral"  # working/ tray
        assert _artifacts(db, parent) == []  # the child IS the owning thread

    async def test_cc_error_result_does_not_register(self):
        db = get_db()
        parent = _mk_session(db)
        child, on_event = await self._cc(db, parent)
        await self._start(
            on_event,
            "t1",
            "mcp__corsia__edit_file",
            {"path": str(_workspace() / "y.md")},
        )
        await on_event({"kind": "tool_result", "tool_use_id": "t1",
                        "content": "File has not been read yet", "is_error": True})
        assert _artifacts(db, child) == []

    async def test_cc_mcp_warning_result_does_not_register(self):
        db = get_db()
        parent = _mk_session(db)
        child, on_event = await self._cc(db, parent)
        p = _workspace() / f"runbook-{uuid.uuid4().hex[:6]}.md"
        await self._start(
            on_event,
            "t1",
            "mcp__corsia__write_file",
            {"path": str(p), "content": "Warning: hot"},
        )
        await on_event({"kind": "tool_result", "tool_use_id": "t1",
                        "content": "Warning: hot\n...(file preview)",
                        "is_error": False})
        assert _artifacts(db, child) == []

    async def test_cc_mcp_write_registers_notebook_path(self):
        db = get_db()
        parent = _mk_session(db)
        child, on_event = await self._cc(db, parent)
        p = _workspace() / f"{uuid.uuid4().hex[:6]}.ipynb"
        await self._start(
            on_event,
            "t1",
            "mcp__corsia__write_file",
            {"path": str(p), "content": "{}"},
        )
        await on_event({"kind": "tool_result", "tool_use_id": "t1",
                        "content": "ok", "is_error": False})
        arts = _artifacts(db, child)
        assert len(arts) == 1
        assert arts[0]["external_id"].startswith("workspace://")
        assert arts[0]["path"] == str(p.resolve())


class TestRootPolicy:
    def test_structurally_synthetic_writer_result_never_registers(self):
        db = get_db()
        sid = _mk_session(db)
        path = _workspace() / f"synthetic-{uuid.uuid4().hex[:6]}.md"
        step_id = f"step-{uuid.uuid4().hex[:8]}"
        db.execute(
            "INSERT INTO messages (id, thread_id, role, content, metadata, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (
                step_id, sid, "assistant", "",
                json.dumps({"step_type": "tool_calls", "llm_tool_calls": [{
                    "id": "cancelled-write", "type": "function",
                    "function": {
                        "name": "write_file",
                        "arguments": json.dumps({"path": str(path), "content": "x"}),
                    },
                }]}),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        db.commit()

        observation = observe_record(
            sid,
            "tool",
            "This tool call was cancelled by user interruption. If it is still "
            "relevant after the interruption, run it again.",
            {
                "tool_call_id": "cancelled-write",
                "assistant_step_id": step_id,
                "name": "write_file",
                "synthetic_tool_result": True,
                "synthetic_reason": "user_interruption",
            },
            db=db,
        )

        assert observation is None
        observed = observation.artifact if observation is not None else None
        assert observed is None
        assert _artifacts(db, sid) == []

    def test_authoritative_step_selects_exact_args_when_call_id_is_reused(self):
        db = get_db()
        sid = _mk_session(db)
        old_path = _workspace() / f"old-{uuid.uuid4().hex[:6]}.md"
        new_path = _workspace() / f"new-{uuid.uuid4().hex[:6]}.md"
        for step_id, path, created_at in [
            ("old-step", old_path, "2026-06-01T00:00:00+00:00"),
            ("new-step", new_path, "2026-06-01T00:00:01+00:00"),
        ]:
            db.execute(
                "INSERT INTO messages (id, thread_id, role, content, metadata, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (
                    f"{step_id}-{uuid.uuid4().hex[:6]}", sid, "assistant", "",
                    json.dumps({"step_type": "tool_calls", "llm_tool_calls": [{
                        "id": "reused-call", "type": "function",
                        "function": {
                            "name": "write_file",
                            "arguments": json.dumps({"path": str(path)}),
                        },
                    }]}),
                    created_at,
                ),
            )
        old_step_id = db.execute(
            "SELECT id FROM messages WHERE thread_id = ? "
            "ORDER BY created_at ASC LIMIT 1",
            (sid,),
        ).fetchone()["id"]
        db.commit()

        observation = observe_record(
            sid,
            "tool",
            "Successfully wrote 5 bytes",
            {
                "tool_call_id": "reused-call",
                "assistant_step_id": old_step_id,
                "name": "write_file",
            },
            db=db,
        )

        assert observation is not None
        observed = observation.artifact
        assert observed is not None
        assert observed["path"] == str(old_path.resolve())
        assert all(
            artifact["path"] != str(new_path.resolve())
            for artifact in _artifacts(db, sid)
        )

    def test_registry_managed_receipt_is_not_observed_twice(self):
        db = get_db()
        sid = _mk_session(db)
        target = _workspace() / ".threads" / sid / "working" / "already-registered.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        artifact = service.register_write_strict(sid, str(target), db=db)
        event_count = db.execute(
            "SELECT COUNT(*) FROM artifact_registration_events"
        ).fetchone()[0]

        observation = observe_record(
            sid,
            "tool",
            json.dumps(
                {
                    "ok": True,
                    "artifact": {"id": artifact["id"]},
                    "registry_managed": True,
                }
            ),
            {"tool_call_id": "managed-write", "name": "write_file"},
            db=db,
        )

        assert observation is None
        observed = observation.artifact if observation is not None else None
        assert observed is None
        assert [row["id"] for row in _artifacts(db, sid)] == [artifact["id"]]
        assert db.execute(
            "SELECT COUNT(*) FROM artifact_registration_events"
        ).fetchone()[0] == event_count

    def test_out_of_roots_is_a_loud_skip(self):
        db = get_db()
        sid = _mk_session(db)
        ok = service.register_write(sid, "/tmp/elsewhere/never.md", db=db, notify=False)
        assert ok is None
        assert _artifacts(db, sid) == []

    def test_project_source_link_is_not_an_agent_repo_zone(self, tmp_path):
        db = get_db()
        sid = _mk_session(db)
        repo = tmp_path / "ext-repo"
        (repo / ".worktrees" / "wt").mkdir(parents=True)
        proj = _workspace() / "projects" / f"p-{uuid.uuid4().hex[:6]}"
        proj.mkdir(parents=True, exist_ok=True)
        (proj / "project_source").symlink_to(repo)

        target = repo / ".worktrees" / "wt" / "fix.py"
        ok = service.register_write(sid, str(target), db=db, notify=False)
        assert ok is None
        assert _artifacts(db, sid) == []

    def test_observer_missing_call_record_skips(self):
        db = get_db()
        sid = _mk_session(db)
        observation = observe_record(
            sid,
            "tool",
            "Successfully wrote 1 bytes",
            {"tool_call_id": "ghost", "name": "write_file"},
            db=db,
        )
        assert observation is None
        n = observation.artifact if observation is not None else None
        assert n is None and _artifacts(db, sid) == []

    def test_replay_accepts_json_metadata(self):
        db = get_db()
        sid = _mk_session(db)
        p = _workspace() / f"replay-{uuid.uuid4().hex[:6]}.md"
        now = datetime.now(timezone.utc).isoformat()
        db.execute(
            "INSERT INTO messages (id, thread_id, role, content, metadata, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (uuid.uuid4().hex[:8], sid, "assistant", "",
             json.dumps({"step_type": "tool_calls", "llm_tool_calls": [{
                 "id": "r1", "type": "function",
                 "function": {"name": "write_file",
                              "arguments": json.dumps({"path": str(p)})}}]}),
             now),
        )
        db.commit()
        observation = observe_record(
            sid,
            "tool",
            "Successfully wrote 5 bytes",
            json.dumps({"tool_call_id": "r1", "name": "write_file"}),
            db=db,
        )
        assert observation is not None
        n = observation.artifact
        assert n is not None and len(_artifacts(db, sid)) == 1

    def test_replay_anchors_scan_at_the_result_row(self):
        from registry import observer as obs_mod

        db = get_db()
        sid = _mk_session(db)
        p = _workspace() / f"anchor-{uuid.uuid4().hex[:6]}.md"

        def _row(role, meta, ts):
            db.execute(
                "INSERT INTO messages (id, thread_id, role, content, metadata, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (uuid.uuid4().hex[:8], sid, role, "", json.dumps(meta), ts))

        _row("assistant", {"step_type": "tool_calls", "llm_tool_calls": [{
            "id": "old1", "type": "function",
            "function": {"name": "write_file",
                         "arguments": json.dumps({"path": str(p)})}}]},
             "2026-06-01T00:00:00+00:00")
        result_ts = "2026-06-01T00:00:01+00:00"
        for i in range(obs_mod._CALL_SCAN_LIMIT + 5):
            _row("assistant", {"step_type": "text"}, f"2026-06-02T00:00:{i:02d}+00:00")
        db.commit()

        observation = observe_record(
            sid,
            "tool",
            "Successfully wrote 5 bytes",
            {"tool_call_id": "old1", "name": "write_file"},
            db=db,
            created_at=result_ts,
        )
        assert observation is not None
        n = observation.artifact
        assert n is not None


class TestInToolHooksDeleted:
    async def test_write_file_tool_no_longer_registers(self, monkeypatch):
        from tools import filesystem_rw
        from tools.filesystem_gate import AuthorizedPath
        from tools.filesystem_rw import WriteFileTool

        db = get_db()
        sid = _mk_session(db)
        tool = WriteFileTool(workspace=_workspace())
        target = _workspace() / ".threads" / sid / "working" / (
            f"hookless-{uuid.uuid4().hex[:6]}.md"
        )
        target.parent.mkdir(parents=True, exist_ok=True)

        async def authorize(**_kwargs):
            return AuthorizedPath(target, reviewed=False)

        monkeypatch.setattr(filesystem_rw, "_resolve_path_for_action", authorize)
        result = await tool.execute(path=str(target), content="x",
                                    description="d", _thread_id=sid)
        assert result.startswith("Successfully wrote")
        assert _artifacts(db, sid) == []


class TestObserverDoesNotOwnBootloaderChangeNotice:
    """Task 3: the late observer never emits a context-source notice.

    File tools record the notice immediately after a confirmed save, before
    Native, Claude Code, or Codex persist and replay their exact result.
    """

    @staticmethod
    def _context_changes(db, label: str) -> list[dict]:
        rows = db.execute(
            "SELECT object_kind,object_id,summary,details_json,actor_thread_id "
            "FROM context_change_journal WHERE object_kind='workspace_context' "
            "ORDER BY seq"
        ).fetchall()
        return [
            dict(row)
            for row in rows
            if json.loads(row["details_json"] or "{}").get("path") == label
        ]

    async def test_native_observer_does_not_late_journal_bootloader_path(self):
        db = get_db()
        sid = _mk_session(db)
        name = f"{uuid.uuid4().hex[:6]}.md"
        p = _workspace() / "context" / name
        # A persisted result alone cannot be proof of a confirmed file-tool
        # save, so the late observer must not emit a context notice.
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x", encoding="utf-8")
        sh = _handler(sid)
        await _native_call(
            sh, "write_file", {"path": str(p), "content": "x"},
            f"File created successfully at: {p}",
        )
        changes = self._context_changes(db, f"context/{name}")
        assert changes == []

    async def test_claude_code_observer_does_not_late_journal_bootloader_path(self):
        db = get_db()
        parent = _mk_session(db)
        child, on_event = cc_child(db, parent)
        name = f"{uuid.uuid4().hex[:6]}.md"
        p = _workspace() / "context" / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x", encoding="utf-8")
        await on_event({
            "kind": "assistant_step", "text": "", "reasoning": "",
            "tool_uses": [{
                "id": "t1", "name": "mcp__corsia__write_file",
                "input": {"path": str(p), "content": "x"},
            }],
        })
        await on_event({"kind": "tool_result", "tool_use_id": "t1",
                        "content": f"File created successfully at: {p}",
                        "is_error": False})
        changes = self._context_changes(db, f"context/{name}")
        assert changes == []

    async def test_an_ordinary_write_journals_no_context_change(self):
        db = get_db()
        sid = _mk_session(db)
        name = f"{uuid.uuid4().hex[:6]}.md"
        p = _workspace() / "working" / name
        sh = _handler(sid)
        await _native_call(
            sh, "write_file", {"path": str(p), "content": "x"},
            f"File created successfully at: {p}",
        )
        assert self._context_changes(db, f"working/{name}") == []
        assert self._context_changes(db, f"context/{name}") == []
