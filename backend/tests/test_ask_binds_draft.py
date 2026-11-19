"""A person's yes at a gate step covers the draft they answered on (plans.draft_binding).

Through the real paths: the step's ask is raised with plan_update block_node,
answered through plans.public.ask_cleared as the review bar does, and the
step is closed with plan_update.
"""

from __future__ import annotations

import pytest

from infra.database import get_db
from plans import public
from plans.draft_binding import DRAFT_KEY
from registry import models
from tests.test_workflow_by import PERSON_GATE
from tests.test_workflow_runs import Run, SHIP_BODY

pytestmark = pytest.mark.asyncio


async def _gate_answered(
    tmp_path, monkeypatch, verdict: str, *, external_id: str = "menu.md", path: str | None | bool = True,
):
    """A gate step asked on a draft and answered with ``verdict``.

    ``path`` True stores the draft's real path; a string stores that path instead; None stores none.
    """
    run = Run(tmp_path, monkeypatch, {"gated": (SHIP_BODY, PERSON_GATE)})
    reply = await run.start("gated", title="Winter menu")
    work, gate = (step["id"] for step in reply["steps"])
    await run.update("mark_done", {
        "node_id": work, "expected_revision": run.revision(work), "evidence": "drafted", "verification_result": "passed",
    })
    draft = tmp_path / "workspace" / "menu.md"
    draft.write_text("Soup, bread, pie.\n", encoding="utf-8")
    db = get_db()
    try:
        models.register_artifact(
            integration="workspace", type="document", external_id=external_id, parent_thread_id=run.thread_id,
            title="Winter menu draft", path=str(draft) if path is True else path, db=db,
        )
        db.commit()
        artifact = db.execute(
            "SELECT id FROM artifact_identities WHERE external_id=?", (external_id,),
        ).fetchone()["id"]
    finally:
        db.close()
    asked = await run.update("block_node", {
        "node_id": gate, "expected_revision": run.revision(gate), "owner_kind": "user", "reason_key": "review",
        "reason": "Is the winter menu ready to print?", "ask_artifact_id": artifact,
    })
    assert not isinstance(asked, str), asked
    assert public.ask_cleared(artifact, "answer", release=public.AskRelease({"verdict": verdict})) == 1
    return run, gate, draft


async def _close(run, gate):
    return await run.update("mark_done", {
        "node_id": gate, "expected_revision": run.revision(gate), "evidence": "approved", "verification_result": "passed",
    })


async def test_the_answer_records_the_draft_it_was_given_on(isolated_db, tmp_path, monkeypatch):
    run, gate, _draft = await _gate_answered(tmp_path, monkeypatch, "approved")

    detail = run.rows(
        "SELECT json_extract(payload_json,'$.detail') AS d FROM plan_node_events WHERE node_id=? AND event='hold_cleared'", gate,
    )[0]["d"]

    assert DRAFT_KEY in detail and "sha256" in detail


async def test_a_yes_closes_the_step_while_the_draft_is_unchanged(isolated_db, tmp_path, monkeypatch):
    run, gate, _draft = await _gate_answered(tmp_path, monkeypatch, "approved")

    result = await _close(run, gate)

    assert not isinstance(result, str), result


async def test_a_yes_on_an_earlier_draft_does_not_close_the_step(isolated_db, tmp_path, monkeypatch):
    run, gate, draft = await _gate_answered(tmp_path, monkeypatch, "approved")
    draft.write_text("Soup, bread, pie, and a new dessert.\n", encoding="utf-8")

    result = await _close(run, gate)

    assert isinstance(result, str) and 'approved an earlier draft of "Winter menu draft"' in result
    assert "Ask them again on this draft" in result


async def test_approved_with_changes_expects_the_draft_to_change(isolated_db, tmp_path, monkeypatch):
    run, gate, draft = await _gate_answered(tmp_path, monkeypatch, "approved_with_changes")
    draft.write_text("Soup, bread, pie, as changed.\n", encoding="utf-8")

    result = await _close(run, gate)

    assert not isinstance(result, str), result


async def test_an_ask_with_no_readable_file_binds_nothing(isolated_db, tmp_path, monkeypatch):
    run, gate, draft = await _gate_answered(
        tmp_path, monkeypatch, "approved", external_id="elsewhere/menu.md", path=None,
    )
    draft.write_text("Changed.\n", encoding="utf-8")

    result = await _close(run, gate)

    assert not isinstance(result, str), result


async def test_a_stale_stored_path_still_binds_through_the_workspace_identity(isolated_db, tmp_path, monkeypatch):
    """A row registered under an earlier workspace root is found by its workspace identity."""
    from infra import config
    from infra.platform_paths import WorkspaceSelection

    monkeypatch.setattr(config, "WORKSPACE_SELECTION", WorkspaceSelection(
        workspace_id="workspace-stable-id", root=(tmp_path / "workspace").resolve(), name="Test workspace",
    ))
    logical = "workspace:" + "//workspace-stable-id/menu.md"
    run, gate, draft = await _gate_answered(
        tmp_path, monkeypatch, "approved", external_id=logical, path=str(tmp_path / "old-root" / "menu.md"),
    )
    draft.write_text("Soup, bread, pie, and a new dessert.\n", encoding="utf-8")

    result = await _close(run, gate)

    assert isinstance(result, str) and "approved an earlier draft" in result


async def test_a_later_ask_that_ends_without_a_verdict_does_not_revive_the_earlier_yes(
    isolated_db, tmp_path, monkeypatch,
):
    """Round 1's yes is refused on a changed draft; round 2 is asked, then dismissed with no verdict."""
    run, gate, draft = await _gate_answered(tmp_path, monkeypatch, "approved")
    artifact = run.rows("SELECT id FROM artifact_identities WHERE external_id='menu.md'")[0]["id"]
    draft.write_text("Soup, bread, pie, and a new dessert.\n", encoding="utf-8")
    assert isinstance(await _close(run, gate), str)
    asked = await run.update("block_node", {
        "node_id": gate, "expected_revision": run.revision(gate), "owner_kind": "user", "reason_key": "review",
        "reason": "Is the changed winter menu ready to print?", "ask_artifact_id": artifact,
    })
    assert not isinstance(asked, str), asked
    assert public.ask_cleared(artifact, "dismiss") == 1

    result = await _close(run, gate)

    assert isinstance(result, str) and "approved an earlier draft" in result
