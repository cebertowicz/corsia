
from __future__ import annotations

import json

import pytest
from cc_runtime_fixtures import (  # noqa: F401
    _seed_parent,
    cc_target,
    child_db,
    fake_claude_oneshot,
)
from core.checkpoint_fork_kind import CHECKPOINT_KIND
from subagents.cc_runtime_helpers import CCWorkingDirMissing


async def _spawn(parent_id: str, task: str | None, *, model: str, **kwargs):
    import subagents.cc_runtime as rt

    return await rt.spawn(
        parent_id, task, target=cc_target(model), **kwargs,
    )


@pytest.fixture(autouse=True)
def _clean_live():
    import subagents.cc_runtime as rt
    rt._LIVE.reset()
    yield
    rt._LIVE.reset()



class TestSpawnCap:
    def test_cap_resolves_from_settings_then_seed(self, monkeypatch):
        from infra import subagent_caps as caps

        monkeypatch.setattr(caps, "get_setting",
                            lambda k: {"max_concurrent_live": 3})
        assert caps.get_max_concurrent_live() == 3

        monkeypatch.setattr(caps, "get_setting", lambda k: None)
        assert caps.get_max_concurrent_live() >= 1

    @pytest.mark.asyncio
    async def test_spawn_refused_at_cap(self, child_db, fake_claude_oneshot, monkeypatch):
        import subagents.cc_runtime as rt
        from infra.subagent_caps import LiveSpawnCapExceeded

        _seed_parent(child_db)
        monkeypatch.setattr("infra.subagent_caps.get_max_concurrent_live",
                            lambda: 1)
        from subagents.cc_runtime_state import _Live

        # Only a working process counts toward the cap (#2064).
        rt._LIVE.put("sentinel", _Live(
            session=None, parent_thread_id=None, name="sentinel", turn_active=True,
        ))

        before = child_db.execute("SELECT COUNT(*) AS c FROM threads").fetchone()["c"]
        with pytest.raises(LiveSpawnCapExceeded) as ei:
            await _spawn("parent", "task", binary=fake_claude_oneshot,
                           model="sonnet", broadcast=lambda s, m: None)
        assert ei.value.cap == 1 and ei.value.active == 1
        after = child_db.execute("SELECT COUNT(*) AS c FROM threads").fetchone()["c"]
        assert after == before  # no child session row was created

    @pytest.mark.asyncio
    async def test_spawn_allowed_below_cap(self, child_db, fake_claude_oneshot, monkeypatch):
        import subagents.cc_runtime as rt

        _seed_parent(child_db)
        monkeypatch.setattr("infra.subagent_caps.get_max_concurrent_live",
                            lambda: 5)
        child_id = await _spawn("parent", "task", binary=fake_claude_oneshot,
                                  model="sonnet", broadcast=lambda s, m: None)
        assert child_id
        row = child_db.execute("SELECT id FROM threads WHERE id = ?",
                               (child_id,)).fetchone()
        assert row is not None



class TestCwdValidation:
    def test_validate_raises_named_error_for_missing_dir(self):
        from subagents.cc_runtime_helpers import _validate_working_dir

        with pytest.raises(CCWorkingDirMissing) as ei:
            _validate_working_dir("/no/such/dir/anywhere", "proj-xyz")
        msg = str(ei.value)
        assert "/no/such/dir/anywhere" in msg
        assert "proj-xyz" in msg
        assert "does not exist" in msg
        assert not isinstance(ei.value, FileNotFoundError)

    def test_validate_noop_for_none_and_existing(self, tmp_path):
        from subagents.cc_runtime_helpers import _validate_working_dir

        _validate_working_dir(None, "p")          # None inherits cwd — fine
        _validate_working_dir(str(tmp_path), "p")  # exists — fine

    @pytest.mark.asyncio
    async def test_spawn_fails_loud_on_stale_cwd(self, child_db, fake_claude_oneshot, monkeypatch):
        import subagents.cc_runtime as rt
        monkeypatch.setattr("infra.subagent_caps.get_max_concurrent_live",
                            lambda: 5)
        _seed_parent(child_db)
        with pytest.raises(CCWorkingDirMissing):
            await _spawn("parent", "task", binary=fake_claude_oneshot,
                           model="sonnet",
                           working_dir="/gone/project/dir",
                           broadcast=lambda s, m: None)



@pytest.mark.asyncio
async def test_detached_checkpoint_does_not_touch_live_or_durable_cc_state(
    child_db,
    monkeypatch,
):
    import asyncio

    from core import fork_cc as checkpoint_fork
    from core.fork_types import ProviderForkRequest
    from native_checkpoint_fixtures import cold_source
    from registry.service import register_cc_session
    import subagents.cc_runtime as rt

    _seed_parent(child_db)
    register_cc_session(
        "parent",
        "existing-provider-resume",
        title="Existing provider handle",
        cwd="/existing/project",
        db=child_db,
    )
    sentinel = object()
    rt._LIVE.put("parent", sentinel)

    def rows(table: str) -> list[tuple]:
        return [
            tuple(row)
            for row in child_db.execute(
                f"SELECT * FROM {table} ORDER BY 1"  # noqa: S608 - fixed table names
            ).fetchall()
        ]

    tables = (
        "threads",
        "artifact_identities",
        "artifact_registrations",
        "artifact_registration_events",
    )
    before = {table: rows(table) for table in tables}
    checkpoint = {
        "schema_version": 1,
        "objective": "Keep detached checkpoint inference isolated.",
        "status": "in_progress",
        "active_constraints": [],
        "approvals_and_preferences": [],
        "decisions": [],
        "rejected_alternatives": [],
        "relevant_state": [],
        "verification": [],
        "known_failures": [],
        "open_questions": [],
        "blockers": [],
        "next_actions": [],
        "recently_completed": [],
        "facts": [],
        "hypotheses": [],
    }
    checkpoint_body = json.dumps(checkpoint, sort_keys=True)
    envelope = {"type": "result", "result": checkpoint_body}

    class DetachedProcess:
        returncode = 0

        def __init__(self):
            self.stdin = self
            self.stdout = asyncio.StreamReader()
            self.stderr = asyncio.StreamReader()
            self.stderr.feed_eof()

        def write(self, payload):
            control = json.loads(payload)
            if control["type"] == "user":
                assert "quoted checkpoint input" in control["message"]["content"]
                self.stdout.feed_data((json.dumps(envelope) + "\n").encode())
                self.stdout.feed_eof()
                return
            self.stdout.feed_data((json.dumps({
                "type": "control_response",
                "response": {
                    "request_id": control["request_id"], "subtype": "success",
                    "response": {"model": "claude-fable-5", "totalTokens": 8000, "maxTokens": 967000},
                },
            }) + "\n").encode())

        async def drain(self):
            return None

        def close(self):
            pass

        async def wait(self):
            return self.returncode

    async def spawn(*_args, **_kwargs):
        return DetachedProcess()

    monkeypatch.setattr(checkpoint_fork, "_spawn_process", spawn)
    request = ProviderForkRequest(
        fork_id="checkpoint-isolation",
        thread_id="parent",
        target=cc_target("fable"),
        instruction="quoted checkpoint input",
        cold_source=cold_source("quoted source", thread_id="parent"),
        reasoning_effort="high",
        working_dir="/tmp",
        binary="/fake/claude",
    )
    result = await checkpoint_fork.invoke_fork(request, asyncio.Event(), kind=CHECKPOINT_KIND)

    assert result.raw_body == checkpoint_body
    assert rt._LIVE.ids() == ("parent",)
    assert rt._LIVE.get("parent") is sentinel
    assert {table: rows(table) for table in tables} == before
