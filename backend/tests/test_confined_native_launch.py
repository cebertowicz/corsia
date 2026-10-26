"""A ``review`` worker on a native or local model is confined from its creating write,
and no later model switch launches it unconfined: Claude Code reads the row's
confinement, and Codex, which has no confined launch, refuses it."""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cc_runtime_fixtures import _seed_parent, child_db  # noqa: F401
from infra import thread_confinement

_LOCAL = "omlx@local/qwen"


@pytest.fixture
def reviewed_dir():
    from infra.config import WORKSPACE

    path = Path(WORKSPACE) / f"review-src-{uuid.uuid4().hex[:8]}"
    path.mkdir(parents=True)
    yield str(path.resolve())
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def native_spawn(monkeypatch):
    """Eligible native models; the first input is recorded, never run."""
    from host_access import supervisor
    from tools import delegate_task

    monkeypatch.setattr(delegate_task, "eligible_delegation_model", lambda model, **_scope: (model, None))
    monkeypatch.setattr(delegate_task.delegation, "_is_cc_model", lambda _m: False)
    monkeypatch.setattr(delegate_task.delegation, "_is_delisted", lambda _m: False)
    sent = AsyncMock(return_value=SimpleNamespace(driver="native", duplicate=False))
    monkeypatch.setattr(delegate_task, "send_thread_input", sent)
    lineage: list[str] = []
    monkeypatch.setattr(supervisor, "register_host_access_descendant",
                        lambda **kwargs: lineage.append(kwargs["thread_id"]))
    return SimpleNamespace(sent=sent, lineage=lineage, spawn=delegate_task._spawn)


@pytest.mark.asyncio
async def test_a_local_review_worker_is_confined_from_its_creating_write(
    child_db, native_spawn, reviewed_dir,
):
    _seed_parent(child_db)
    started: list[str] = []

    async def rule_start(worker: str) -> None:
        started.append(worker)
        # Recorded before the first turn can run, and already confined then.
        assert thread_confinement.read_dirs(worker, child_db) == (reviewed_dir,)
        native_spawn.sent.assert_not_awaited()

    receipt = await native_spawn.spawn(
        "parent", {"user_id": "steve"}, "review this",
        {"model": _LOCAL, "confinement": "review", "read_paths": [reviewed_dir]},
        rule_start=rule_start,
    )
    assert isinstance(receipt, dict) and receipt["model"] == _LOCAL
    assert started == [receipt["thread_id"]]
    assert thread_confinement.read_dirs(receipt["thread_id"], child_db) == (reviewed_dir,)
    assert native_spawn.lineage == []  # outside any host-access lineage


@pytest.mark.asyncio
async def test_an_unconfined_native_spawn_is_unchanged(child_db, native_spawn):
    _seed_parent(child_db)
    receipt = await native_spawn.spawn("parent", {"user_id": "steve"}, "task", {"model": _LOCAL})
    assert thread_confinement.read_dirs(receipt["thread_id"], child_db) is None
    assert native_spawn.lineage == [receipt["thread_id"]]


@pytest.mark.asyncio
async def test_a_local_review_refuses_a_read_its_parent_lacks(child_db, native_spawn, tmp_path):
    _seed_parent(child_db)
    outside = tmp_path / "outside-workspace"
    outside.mkdir()
    refused = await native_spawn.spawn(
        "parent", {"user_id": "steve"}, "review",
        {"model": _LOCAL, "confinement": "review", "read_paths": [str(outside)]},
    )
    assert refused.startswith(f"Error: read path {outside} refused: ")
    native_spawn.sent.assert_not_awaited()
    assert child_db.execute("SELECT COUNT(*) FROM threads").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_codex_review_is_still_refused_before_any_thread(child_db, native_spawn, reviewed_dir):
    _seed_parent(child_db)
    refused = await native_spawn.spawn(
        "parent", {"user_id": "steve"}, "review",
        {"model": "codex/gpt-5-codex", "confinement": "review", "read_paths": [reviewed_dir]},
    )
    assert refused == ("Error: refused: no confined launch for codex/gpt-5-codex; "
                       "use a claude-code/* or local model.")
    assert child_db.execute("SELECT COUNT(*) FROM threads").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_a_confined_worker_switched_to_another_harness_stays_confined(
    child_db, native_spawn, reviewed_dir,
):
    from subagents.cc_confinement import ConfinementRefused, launch_confinement
    from subagents.codex_destination_render import codex_destination_scope

    _seed_parent(child_db)
    receipt = await native_spawn.spawn(
        "parent", {"user_id": "steve"}, "review",
        {"model": _LOCAL, "confinement": "review", "read_paths": [reviewed_dir]},
    )
    worker = receipt["thread_id"]
    child_db.execute("UPDATE threads SET model='claude-code/sonnet' WHERE id=?", (worker,))
    claude = launch_confinement(worker, None, child_db)
    assert claude.confined and claude.add_dirs == (reviewed_dir,)
    assert claude.working_dir.endswith(f"{worker}/confined") and Path(claude.working_dir).is_dir()

    child_db.execute("UPDATE threads SET model='codex/gpt-5-codex' WHERE id=?", (worker,))
    with pytest.raises(ConfinementRefused, match="no confined launch for codex/gpt-5-codex"):
        codex_destination_scope(worker, target_catalog_id="codex/gpt-5-codex", db=child_db)

    child_db.execute("UPDATE threads SET confined_read_dirs='[]' WHERE id=?", (worker,))
    with pytest.raises(ConfinementRefused, match="damaged"):
        launch_confinement(worker, None, child_db)
