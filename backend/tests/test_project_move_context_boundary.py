"""A project move keeps history while resolving the destination context."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.project_ref_helpers import project_ref, select_workspace


def _write_bootloader(root: Path, marker: str) -> None:
    context = root / "context"
    context.mkdir(parents=True)
    (context / "PROJECT.md").write_text(marker, encoding="utf-8")
    (context / "COORDINATION-MODEL.md").write_text("Coordination.", encoding="utf-8")


def _seed_thread_with_checkpoint_and_continuity(old_root: Path, new_root: Path) -> None:
    from core.checkpoint_types import CheckpointRequest
    from infra.checkpoints import capture, create_running, install
    from infra.continuity_store import add_entry
    from infra.database import get_db

    db = get_db()
    try:
        for project_id, name, root in (
            ("old", "Old", old_root),
            ("new", "New", new_root),
        ):
            db.execute(
                "INSERT INTO projects (id,name,path,created_at,updated_at) "
                "VALUES (?,?,?,?,?)",
                (project_id, name, project_ref(root.name), "t0", "t0"),
            )
        db.execute(
            "INSERT INTO threads "
            "(id,user_id,title,project_id,model,next_message_seq,created_at,updated_at) "
            "VALUES ('thread','user','Thread','old','native/test',2,'t0','t0')"
        )
        db.execute(
            "INSERT INTO messages (id,thread_id,role,content,created_at,seq) "
            "VALUES ('message','thread','user','Continue.','t0',1)"
        )
        request = CheckpointRequest(
            id="checkpoint",
            thread_id="thread",
            history_epoch=0,
            previous_generation_id=None,
            through_seq=1,
            through_message_id="message",
            trigger="manual",
            mode="warm",
            source_harness="native",
            source_model="native/test",
            writer_harness="native",
            writer_model="native/test",
            originating_turn_id="turn-1",
            first_included_seq=1,
            started_at="t1",
        )
        create_running(request, db=db)
        capture("checkpoint", raw_result="checkpoint", usage_json="{}", captured_at="t2", db=db)
        install("checkpoint", installed_at="t3", db=db)
        add_entry(
            db,
            thread_id="thread",
            history_epoch=0,
            field="facts",
            body={"statement": "Keep this fact.", "source_refs": []},
        )
        db.commit()
    finally:
        db.close()


@pytest.mark.asyncio
async def test_project_move_preserves_history_and_loads_destination_bootloader(
    isolated_db, monkeypatch, tmp_path,
):
    from core.dynamic_context_registry import collect_context_sources, provider_context_prompt
    from infra.checkpoint_queries import current_installed
    from infra.continuity_store import active_entries
    from infra.database import get_db
    from tools.query_projects import SetConversationProjectTool

    select_workspace(monkeypatch, tmp_path)
    old_root = tmp_path / "projects" / "old"
    new_root = tmp_path / "projects" / "new"
    _write_bootloader(old_root, "OLD PROJECT BOOTLOADER")
    _write_bootloader(new_root, "NEW PROJECT BOOTLOADER")
    _seed_thread_with_checkpoint_and_continuity(old_root, new_root)

    async def no_runtime_effects(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        "thread.runtime_boundary.post_context_boundary_effects", no_runtime_effects,
    )
    await SetConversationProjectTool().execute("thread", "new")

    db = get_db()
    try:
        thread = db.execute(
            "SELECT project_id,history_epoch FROM threads WHERE id='thread'"
        ).fetchone()
        assert dict(thread) == {"project_id": "new", "history_epoch": 0}
        assert active_entries(db, "thread", 0)["facts"][0]["statement"] == "Keep this fact."
        assert current_installed("thread", db=db).id == "checkpoint"
        collection = collect_context_sources(
            thread_id="thread",
            project_id=thread["project_id"],
            workspace=tmp_path,
            db=db,
            selected_model="native/test",
        )
    finally:
        db.close()

    delivered = provider_context_prompt(collection.sources)
    assert "NEW PROJECT BOOTLOADER" in delivered
    assert "OLD PROJECT BOOTLOADER" not in delivered


@pytest.mark.asyncio
async def test_project_move_ignores_retained_old_project_sources(
    isolated_db, monkeypatch, tmp_path,
):
    """Sources frozen by a dispatch under the old project are not redelivered."""
    from core.dynamic_context_registry import collect_context_sources, provider_context_prompt
    from core.installed_context import persist_installed_delivery
    from infra.checkpoint_queries import current_installed
    from infra.continuity_store import active_entries
    from infra.database import get_db
    from tools.query_projects import SetConversationProjectTool

    select_workspace(monkeypatch, tmp_path)
    old_root = tmp_path / "projects" / "old"
    new_root = tmp_path / "projects" / "new"
    _write_bootloader(old_root, "OLD PROJECT BOOTLOADER")
    _write_bootloader(new_root, "NEW PROJECT BOOTLOADER")
    _seed_thread_with_checkpoint_and_continuity(old_root, new_root)

    def collect(project_id):
        db = get_db()
        try:
            return collect_context_sources(
                thread_id="thread",
                project_id=project_id,
                workspace=tmp_path,
                db=db,
                selected_model="native/test",
            )
        finally:
            db.close()

    # First dispatch after install: freeze the old project's sources through
    # the production seam the dispatch path uses.
    first = collect("old")
    assert "OLD PROJECT BOOTLOADER" in provider_context_prompt(first.sources)
    assert persist_installed_delivery(first.installed) is True
    # Delete the old file: only retained bytes can now deliver the old text.
    (old_root / "context" / "PROJECT.md").unlink()
    assert "OLD PROJECT BOOTLOADER" in provider_context_prompt(collect("old").sources)

    async def no_runtime_effects(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        "thread.runtime_boundary.post_context_boundary_effects", no_runtime_effects,
    )
    await SetConversationProjectTool().execute("thread", "new")

    db = get_db()
    try:
        thread = db.execute(
            "SELECT project_id,history_epoch FROM threads WHERE id='thread'"
        ).fetchone()
        assert dict(thread) == {"project_id": "new", "history_epoch": 0}
        assert active_entries(db, "thread", 0)["facts"][0]["statement"] == "Keep this fact."
        assert current_installed("thread", db=db).id == "checkpoint"
    finally:
        db.close()

    delivered = provider_context_prompt(collect("new").sources)
    assert "NEW PROJECT BOOTLOADER" in delivered
    assert "OLD PROJECT BOOTLOADER" not in delivered
