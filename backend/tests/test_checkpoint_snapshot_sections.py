"""Installed checkpoints carry the thread's artifacts and plan status (#1602).

The two snapshot columns freeze at install: the artifact section lists the
thread's owned and followed visible pane rows (capped at 20 with a stated
omission), and the plan-status section reuses the live ``plan_read`` status
projection's own caps. Both render on every harness from the same stored
row — the native ``checkpoint_material``, the Claude Code seed, and the
Codex reconstruction — and a NULL column (no capture ran) keeps the prefix
byte-identical to the pre-change shape.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace

import pytest

from core.checkpoint_render import render_installed_checkpoint
from core.checkpoint_snapshot_render import (
    CAPTURE_FAILED_JSON,
    ARTIFACT_HEADER,
    PLAN_HEADER,
    render_artifact_snapshot,
    render_plan_status_snapshot,
)

THREAD = "snap-thread"
HARNESS_MODELS = {
    "native": "native/Fortissimo",
    "claude-code": "claude-code/sonnet",
    "codex": "codex/luna",
}
_UNREADABLE_PREFIX = "[CORSIA warning: snapshot could not be read"


@pytest.fixture()
def snap(isolated_db):
    """One workspace thread with three messages a checkpoint can cut through."""
    from infra.database import get_db

    db = get_db()
    db.execute(
        "INSERT INTO threads (id,user_id,title,model,next_message_seq,"
        "created_at,updated_at) VALUES (?, 'steve', 'Snapshots', ?, 4, 't0', 't0')",
        (THREAD, HARNESS_MODELS["native"]),
    )
    for seq in range(1, 4):
        db.execute(
            "INSERT INTO messages (id,thread_id,role,content,created_at,seq,"
            "active_turn_id) VALUES (?,?,?,'body','t0',?,'turn-1')",
            (f"{THREAD}-m{seq}", THREAD, "user" if seq == 1 else "assistant", seq),
        )
    db.execute(
        "INSERT INTO active_turns (id,thread_id,execution_epoch,model,status,"
        "started_at,ended_at,boot_epoch,turn_order) VALUES "
        "('turn-1',?,0,?,'completed','t0','t0','boot',1)",
        (THREAD, HARNESS_MODELS["native"]),
    )
    db.commit()
    db.close()


def _db():
    from infra.database import get_db

    return get_db()


def _register(
    thread_id: str,
    name: str,
    *,
    path: str | None = None,
    metadata: dict | None = None,
) -> None:
    """Register one visible workspace document owned by ``thread_id``."""
    from registry import models

    models.register_artifact(
        integration="workspace", type="document", external_id=name,
        parent_thread_id=thread_id, title=f"Doc {name}", path=path,
        metadata=metadata,
    )


def _release(thread_id: str, name: str) -> None:
    """Move ``thread_id``'s registration of one artifact to subscriber-recent."""
    from registry import models

    models.release_artifact(
        integration="workspace", type="document", external_id=name,
        actor_thread_id=thread_id, authority="current-owner-handoff",
        reason="test follows", source_path="test",
    )


def _plan_with_task(task_title: str = "Working task", *, open_leaves: int = 1):
    """Ensure the thread's plan and return (plan, selected node, extra nodes)."""
    from plans import admission, service

    db = _db()
    try:
        plan = service.ensure_plan("thread", THREAD, db=db)
        selected = service.add_node(
            plan.id, kind="task", title=task_title,
            acceptance_criteria="Done", db=db,
        )
        service.transition_node(selected.id, "active", actor_id=THREAD, db=db)
        admission.select(
            selected.id, actor_id=THREAD, provenance="test", db=db, touch=False,
        )
        extras = [
            service.add_node(
                plan.id, kind="task", title=f"Open {i}",
                acceptance_criteria="Done", db=db,
            )
            for i in range(open_leaves - 1)
        ]
        db.commit()
        return plan, selected, extras
    finally:
        db.close()


def _install_checkpoint(checkpoint_id: str = "cp-1") -> None:
    """Create, capture, and install one manual warm checkpoint for THREAD."""
    from core.checkpoint_types import CheckpointRequest
    from infra.checkpoints import capture, create_running, install

    db = _db()
    try:
        request = CheckpointRequest(
            id=checkpoint_id, thread_id=THREAD, history_epoch=0,
            previous_generation_id=None, through_seq=3,
            through_message_id=f"{THREAD}-m3", trigger="manual", mode="warm",
            source_harness="native", source_model=HARNESS_MODELS["native"],
            writer_harness="native", writer_model=HARNESS_MODELS["native"],
            originating_turn_id="turn-1", first_included_seq=1, started_at="t1",
        )
        create_running(request, db=db)
        capture(
            checkpoint_id, raw_result="Narrative body.", usage_json="{}",
            captured_at="t2", db=db,
        )
        install(checkpoint_id, installed_at="t3", db=db)
        db.commit()
    finally:
        db.close()


def _record(checkpoint_id: str = "cp-1"):
    from infra import checkpoint_queries

    db = _db()
    try:
        return checkpoint_queries.read(checkpoint_id, db=db)
    finally:
        db.close()


# --- capture: content, emptiness, caps, failure ------------------------------


def test_install_freezes_artifacts_and_plan_status(snap):
    _register(THREAD, "doc-a", path="/tmp/doc-a.md")
    _register(THREAD, "doc-b")  # released below: follower, no path
    _release(THREAD, "doc-b")
    _plan_with_task()
    db = _db()
    try:
        # The capture path is the only reader of origin_plan_node_id (#1602
        # in-lane decision); point doc-a's registration at one plan node.
        db.execute(
            "UPDATE artifact_registrations SET origin_plan_node_id="
            "(SELECT id FROM plan_nodes ORDER BY id LIMIT 1) "
            "WHERE artifact_id=(SELECT id FROM artifact_identities "
            "WHERE external_id='doc-a')"
        )
        db.commit()
    finally:
        db.close()

    _install_checkpoint()
    record = _record()
    artifacts = json.loads(record.artifact_snapshot_json)
    assert artifacts["total_count"] == 2
    assert artifacts["omitted_count"] == 0
    row_a = next(row for row in artifacts["rows"] if row["title"] == "Doc doc-a")
    row_b = next(row for row in artifacts["rows"] if row["title"] == "Doc doc-b")
    assert row_a["locator"] == "/tmp/doc-a.md"
    assert row_a["owns"] is True
    assert row_a["origin_plan_node_id"]
    assert row_b["owns"] is False  # subscriber-recent renders as follows
    assert row_b["locator"] == "doc-b"  # no path: external id
    assert row_b["origin_plan_node_id"] is None

    plan_status = json.loads(record.plan_status_snapshot_json)
    assert plan_status["plan"]["id"]
    assert plan_status["current_task"]["title"] == "Working task"

    rendered = render_installed_checkpoint(record)
    assert ARTIFACT_HEADER in rendered
    assert f"id:{row_a['id']} Doc doc-a (workspace/document) /tmp/doc-a.md owns" in rendered
    assert f"id:{row_b['id']} Doc doc-b (workspace/document) doc-b follows" in rendered
    assert PLAN_HEADER in rendered
    assert record.plan_status_snapshot_json in rendered
    assert "Working task" in rendered


def test_empty_capture_renders_none_and_no_plan(snap):
    _install_checkpoint()
    record = _record()

    assert json.loads(record.artifact_snapshot_json) == {
        "rows": [], "omitted_count": 0, "total_count": 0,
    }
    assert record.plan_status_snapshot_json == "null"

    rendered = render_installed_checkpoint(record)
    assert f"{ARTIFACT_HEADER}\nnone" in rendered
    assert f"{PLAN_HEADER}\nno plan" in rendered


def test_artifact_rows_are_capped_at_twenty_with_stated_omission(snap):
    for i in range(25):
        _register(THREAD, f"doc-{i:02d}", path=f"/tmp/doc-{i:02d}.md")

    _install_checkpoint()
    record = _record()
    artifacts = json.loads(record.artifact_snapshot_json)

    assert artifacts["total_count"] == 25
    assert len(artifacts["rows"]) == 20
    assert artifacts["omitted_count"] == 5

    section = render_artifact_snapshot(record.artifact_snapshot_json)
    assert section.count("\n- ") == 20
    assert section.endswith("5 more — artifact_list(owned=true)")
    # Ordering is the pane's (pinned, then most recently updated, then id);
    # same-timestamp registrations tie-break on id, so only the shape is fixed.
    titles = {row["title"] for row in artifacts["rows"]}
    assert titles <= {f"Doc doc-{i:02d}" for i in range(25)}


def test_owned_shared_rows_render_trigger_and_freshness_without_changing_followed_rows(
    snap, tmp_path, monkeypatch,
):
    from datetime import datetime, timedelta, timezone
    from infra import config

    monkeypatch.setattr(config, "WORKSPACE", tmp_path)
    shared = tmp_path / "shared" / "documents"
    shared.mkdir(parents=True)
    now = datetime.now(timezone.utc)
    long_trigger = "x" * 70
    _register(
        THREAD,
        "overdue",
        path=str(shared / "overdue.md"),
        metadata={
            "update_trigger": long_trigger,
            "last_verified_at": (now - timedelta(days=31)).isoformat(),
        },
    )
    _register(
        THREAD,
        "never",
        path=str(shared / "never.md"),
        metadata={"update_trigger": "when facts change"},
    )
    _register(THREAD, "untriggered", path=str(shared / "untriggered.md"))
    _register(
        THREAD,
        "followed",
        path=str(shared / "followed.md"),
        metadata={"update_trigger": "must not render"},
    )
    _release(THREAD, "followed")

    _install_checkpoint()
    record = _record()
    rows = json.loads(record.artifact_snapshot_json)["rows"]
    overdue = next(row for row in rows if row["title"] == "Doc overdue")
    never = next(row for row in rows if row["title"] == "Doc never")
    followed = next(row for row in rows if row["title"] == "Doc followed")

    assert overdue["trigger"] == "x" * 60 + "…"
    assert overdue["freshness"] == "overdue"
    assert never["freshness"] == "never_verified"
    assert "trigger" not in followed and "freshness" not in followed
    rendered = render_artifact_snapshot(record.artifact_snapshot_json)
    assert "trigger: " + "x" * 60 + "… overdue" in rendered
    assert "trigger: when facts change never verified" in rendered
    assert "must not render" not in rendered
    # A missing trigger renders nothing rather than a bare label.
    untriggered = next(
        line for line in rendered.splitlines() if "Doc untriggered" in line
    )
    assert "trigger:" not in untriggered
    assert untriggered.endswith("owns never verified")


def test_overdue_owned_shared_document_is_listed_after_its_pane_row_is_retired(
    snap, tmp_path, monkeypatch,
):
    """Ownership no longer adds a pane row; the owner is still told what is overdue."""
    from datetime import datetime, timedelta, timezone
    from infra import config
    from registry import node_retirement, view

    monkeypatch.setattr(config, "WORKSPACE", tmp_path)
    shared = tmp_path / "shared" / "documents"
    shared.mkdir(parents=True)
    now = datetime.now(timezone.utc)
    _register(
        THREAD, "overdue", path=str(shared / "overdue.md"),
        metadata={"last_verified_at": (now - timedelta(days=31)).isoformat()},
    )
    _register(
        THREAD, "current", path=str(shared / "current.md"),
        metadata={"last_verified_at": now.isoformat()},
    )
    _register(THREAD, "elsewhere", path=str(tmp_path / "notes" / "elsewhere.md"))
    db = _db()
    try:
        for name in ("overdue", "current", "elsewhere"):
            artifact_id = db.execute(
                "SELECT id FROM artifact_identities WHERE external_id=?", (name,),
            ).fetchone()["id"]
            node_retirement.retire_ownership_row(
                db, artifact_id=artifact_id, thread_id=THREAD, trigger="test",
            )
        db.commit()
        assert view._artifacts_for_thread(db, THREAD) == []
    finally:
        db.close()

    _install_checkpoint()
    snapshot = json.loads(_record().artifact_snapshot_json)

    assert [row["title"] for row in snapshot["rows"]] == ["Doc overdue"]
    assert snapshot["rows"][0]["owns"] is True
    assert snapshot["rows"][0]["freshness"] == "overdue"
    assert snapshot["total_count"] == 1


def test_byte_cap_keeps_overdue_owned_row_and_renders_artifact_list_pointer(
    snap, tmp_path, monkeypatch,
):
    from datetime import datetime, timedelta, timezone
    from infra import config
    from infra.checkpoint_snapshots import ARTIFACT_BYTES

    monkeypatch.setattr(config, "WORKSPACE", tmp_path)
    shared = tmp_path / "shared" / "documents"
    shared.mkdir(parents=True)
    _register(
        THREAD,
        "overdue-priority",
        path=str(shared / "overdue-priority.md"),
        metadata={
            "update_trigger": "when a very long upstream source changes " * 4,
            "last_verified_at": (
                datetime.now(timezone.utc) - timedelta(days=31)
            ).isoformat(),
        },
    )
    for index in range(30):
        _register(
            THREAD,
            f"ordinary-{index:02d}-" + "z" * 50,
            path=str(shared / (f"ordinary-{index:02d}-" + "p" * 80 + ".md")),
            metadata={"update_trigger": "when another long source changes " * 4},
        )

    _install_checkpoint()
    record = _record()
    snapshot = json.loads(record.artifact_snapshot_json)
    rendered = render_artifact_snapshot(record.artifact_snapshot_json)

    assert len(record.artifact_snapshot_json.encode()) <= ARTIFACT_BYTES
    assert any(
        row.get("title") == "Doc overdue-priority"
        and row.get("freshness") == "overdue"
        for row in snapshot["rows"]
    )
    assert snapshot["omitted_count"] > 0
    assert rendered.endswith(
        f"{snapshot['omitted_count']} more — artifact_list(owned=true)"
    )


def test_plan_snapshot_is_the_status_context_projection_within_its_budget(snap):
    from plans import status_context

    _plan_with_task(open_leaves=6)

    _install_checkpoint()
    record = _record()
    plan_status = json.loads(record.plan_status_snapshot_json)

    assert len(record.plan_status_snapshot_json.encode()) <= (
        status_context.CHECKPOINT_BYTES
    )
    assert plan_status["status_read"] == "plan_read()"
    assert "current" not in plan_status and "recent" not in plan_status
    assert render_plan_status_snapshot(record.plan_status_snapshot_json) == (
        f"{PLAN_HEADER}\n{record.plan_status_snapshot_json}"
    )


def test_capture_failure_stores_marker_and_renders_unavailable_without_blocking_install(
    snap, monkeypatch,
):
    from infra import checkpoint_snapshots

    def boom(*_args, **_kwargs):
        raise sqlite3.OperationalError("registry unavailable")

    monkeypatch.setattr(checkpoint_snapshots, "_artifact_snapshot", boom)
    monkeypatch.setattr(checkpoint_snapshots, "_plan_status_snapshot", boom)
    _install_checkpoint()
    record = _record()

    assert record.artifact_snapshot_json == CAPTURE_FAILED_JSON
    assert record.plan_status_snapshot_json == CAPTURE_FAILED_JSON
    unavailable = "unavailable: capture failed at install"
    assert unavailable in render_artifact_snapshot(record.artifact_snapshot_json)
    assert unavailable in render_plan_status_snapshot(record.plan_status_snapshot_json)
    assert _legacy_prefix("Narrative body.") in render_installed_checkpoint(record)


def test_programming_error_in_a_capture_fails_the_install_loudly(snap, monkeypatch):
    from infra import checkpoint_snapshots

    def boom(*_args, **_kwargs):
        raise KeyError("renamed column")

    monkeypatch.setattr(checkpoint_snapshots, "_artifact_snapshot", boom)
    with pytest.raises(KeyError):
        _install_checkpoint()


# --- render: pure, paused, off-contract, legacy ------------------------------


def _legacy_prefix(body: str) -> str:
    """The exact pre-#1602 render: narrative plus continuity, nothing else."""
    return (
        "[CORSIA checkpoint narrative]\n" + body
        + "\n[CORSIA continuity entries]\n"
    )


def test_null_columns_keep_the_prefix_byte_identical_to_before(snap):
    _install_checkpoint()
    record = _record()
    legacy = replace(
        record, artifact_snapshot_json=None, plan_status_snapshot_json=None,
        through_at=None,
    )
    assert render_installed_checkpoint(legacy) == _legacy_prefix("Narrative body.")
    assert render_artifact_snapshot(None) == ""
    assert render_plan_status_snapshot(None) == ""


def test_paused_plan_is_delivered_exactly_as_status_context_rendered_it():
    from plans import status_context

    thin = {
        "plan": {"id": "p1", "revision": 2, "state": "paused"},
        "note": "plan paused",
        "next_step": {"id": "n2", "detail_read": "plan_read(node_id='n2')"},
        "status_read": "plan_read()",
    }
    stored = status_context.render(thin)
    assert render_plan_status_snapshot(stored) == f"{PLAN_HEADER}\n{stored}"


def test_off_contract_snapshot_degrades_to_a_marker_with_raw_bytes():
    raw = "{not the contract"
    assert _UNREADABLE_PREFIX in render_plan_status_snapshot(raw)
    assert raw in render_plan_status_snapshot(raw)
    assert _UNREADABLE_PREFIX in render_artifact_snapshot(raw)
    assert raw in render_artifact_snapshot(raw)


def test_render_is_deterministic_for_a_stored_row():
    snapshot = json.dumps({
        "rows": [{"id": "a1", "title": "T", "integration": "workspace",
                  "type": "document", "locator": "/tmp/t.md", "owns": True,
                  "origin_plan_node_id": None}],
        "omitted_count": 0, "total_count": 1,
    })
    assert render_artifact_snapshot(snapshot) == render_artifact_snapshot(snapshot)


def test_artifact_id_is_one_line_and_legacy_rows_without_it_still_render():
    snapshot = json.dumps({
        "rows": [
            {"id": "a\n1", "title": "Current", "integration": "workspace",
             "type": "document", "locator": "/tmp/current.md", "owns": True},
            {"title": "Legacy", "integration": "workspace", "type": "document",
             "locator": "/tmp/legacy.md", "owns": False},
        ],
        "omitted_count": 0, "total_count": 2,
    })

    assert render_artifact_snapshot(snapshot) == (
        f"{ARTIFACT_HEADER}\n"
        "- id:a\\n1 Current (workspace/document) /tmp/current.md owns\n"
        "- Legacy (workspace/document) /tmp/legacy.md follows"
    )


def test_truncated_artifact_row_keeps_an_actionable_id():
    snapshot = json.dumps({
        "rows": [{"id": "artifact-42", "integration": "workspace",
                  "type": "document", "truncated": True}],
        "omitted_count": 0, "total_count": 1,
    })

    assert render_artifact_snapshot(snapshot) == (
        f"{ARTIFACT_HEADER}\n"
        "- id:artifact-42 (workspace/document) truncated"
    )


# --- the three harness render paths ------------------------------------------


def _native_checkpoint_text() -> str:
    """The native prefix's checkpoint component, from its own call site."""
    from core.context_replay import read_replay_candidates
    from core.native_context_sources import checkpoint_material

    db = _db()
    try:
        candidates = read_replay_candidates(THREAD, db=db)
        assert candidates.checkpoint_tail is not None
        prompt, _component = checkpoint_material(candidates.checkpoint_tail)
        return prompt
    finally:
        db.close()


def _checkpoint_text() -> str:
    """The shared render every harness destination consumes."""
    from core.checkpoint_resume import installed_replay
    from core.context_replay import checkpoint_prompt

    db = _db()
    try:
        selection = installed_replay(_record(), db=db)
        return checkpoint_prompt(selection)
    finally:
        db.close()


def _claude_code_seed_text() -> str:
    """The Claude Code seed, built the way ``cc_destination`` builds it."""
    from subagents.cc_context_seed import build_cc_context_reconstruction

    db = _db()
    try:
        seed = build_cc_context_reconstruction(
            thread_id=THREAD, base_seed="BASE",
            catalog_model_id=HARNESS_MODELS["claude-code"],
            model_alias="sonnet", working_dir=None, project_id=None,
            is_subagent=False, provider_handle_opaque=True,
            checkpoint_text=_checkpoint_text(), db=db,
        )
        return seed.seed_text
    finally:
        db.close()


def _codex_task_text() -> str:
    """The Codex reconstruction, built the way ``codex_destination_render`` does.

    The checkpoint prompt rides as a prior history item, which is exactly
    where the destination's ``checkpoint_prompt`` result lands.
    """
    from history.types import PortableHistoryProjection
    from subagents.codex_context_reconstruction import (
        build_codex_context_reconstruction,
    )

    projection = PortableHistoryProjection(
        format_version=1, thread_id=THREAD, history_epoch=0,
        start_exclusive=None, sealed_boundary=None, turns=(),
        full_raw_replay_available=True,
    )
    db = _db()
    try:
        reconstruction = build_codex_context_reconstruction(
            thread_id=THREAD, user_text="go", working_dir=None, project_id=None,
            model=HARNESS_MODELS["codex"], model_context_window=200000,
            history_projection=projection, checkpoint_text=_checkpoint_text(),
            db=db,
        )
        texts = [reconstruction.task_text]
        for item in reconstruction.history_items:
            texts.extend(
                str(part.get("text"))
                for part in item.get("content", ())
                if isinstance(part, dict) and part.get("type") == "input_text"
            )
        return "\n".join(texts)
    finally:
        db.close()


_HARNESS_TEXT = {
    "native": _native_checkpoint_text,
    "claude-code": _claude_code_seed_text,
    "codex": _codex_task_text,
}


def _set_thread_model(model: str) -> None:
    db = _db()
    try:
        db.execute("UPDATE threads SET model=? WHERE id=?", (model, THREAD))
        db.commit()
    finally:
        db.close()


@pytest.mark.parametrize("harness", sorted(_HARNESS_TEXT))
def test_harness_path_carries_both_sections_empty(snap, harness):
    _set_thread_model(HARNESS_MODELS[harness])
    _install_checkpoint()
    text = _HARNESS_TEXT[harness]()

    assert ARTIFACT_HEADER in text
    assert f"{ARTIFACT_HEADER}\nnone" in text
    assert f"{PLAN_HEADER}\nno plan" in text


@pytest.mark.parametrize("harness", sorted(_HARNESS_TEXT))
def test_harness_path_carries_both_sections_capped(snap, harness):
    _set_thread_model(HARNESS_MODELS[harness])
    for i in range(25):
        _register(THREAD, f"doc-{i:02d}", path=f"/tmp/doc-{i:02d}.md")
    _plan_with_task(open_leaves=6)
    _install_checkpoint()
    text = _HARNESS_TEXT[harness]()

    assert text.count("\n- id:") == 20
    assert "5 more — artifact_list(owned=true)" in text
    assert "plan_read()" in text


def test_plan_snapshot_that_is_not_an_object_degrades_instead_of_raising():
    section = render_plan_status_snapshot("[1, 2]")
    assert section.startswith("[CORSIA plan status]\n[CORSIA warning")
