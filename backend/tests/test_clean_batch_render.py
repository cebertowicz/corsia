"""Provider rendering contracts for one claimed FIFO batch."""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace


def _schema_db() -> sqlite3.Connection:
    from infra.schema import SCHEMA_SQL

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA_SQL)
    db.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
        "VALUES ('thread-1','user','test/model','t','t')"
    )
    db.commit()
    return db


def _batch():
    from infra.thread_inputs import ClaimedBatch, ClaimedInput

    return ClaimedBatch(
        thread_id="thread-1",
        turn_id="turn-1",
        turn_order=1,
        target_execution_epoch=7,
        target_model="native/test",
        items=(
            ClaimedInput(
                "input-1", 1, 1, "user", "first", "queue_after_turn",
                {"sender_kind": "agent", "from_thread": "child"},
                {"native_mode": "event"},
                "t1",
            ),
            ClaimedInput(
                "input-2", 2, 1, "user", "second", "apply_now",
                {}, {"native_mode": "ws"}, "t2",
            ),
        ),
    )


def test_batch_render_is_stable_and_preserves_member_fifo() -> None:
    """Every harness receives the same ordered member envelopes and identity."""
    from infra.input_render import render_claimed_batch

    first = render_claimed_batch(_batch())
    second = render_claimed_batch(_batch())
    assert first == second
    assert "[1/2]" in first.content
    assert "[2/2]" in first.content
    assert first.content.index("first") < first.content.index("second")
    assert first.identity.startswith("input-batch:")


def test_projection_preserves_attachments_from_every_fifo_member(monkeypatch):
    from dataclasses import replace
    from infra.input_render import project_claimed_input

    batch = _batch()
    first, second = {"file_id": "first"}, {"file_id": "second"}
    batch = replace(batch, items=(
        replace(batch.items[0], delivery_options={"attachments": [first]}),
        replace(batch.items[1], delivery_options={"attachments": [second]}),
    ))
    seen = []

    def project(attachments, **_kwargs):
        seen.extend(attachments)
        return attachments, SimpleNamespace(digests=("first-digest", "second-digest"))

    monkeypatch.setattr("core.media_delivery.build_delivery_projection", project)
    db = _schema_db()
    try:
        projected = project_claimed_input(
            db, batch, thread_row={"user_id": "owner"}, target=object(),
        )
    finally:
        db.close()
    assert seen == [first, second]
    assert projected.attachments == (first, second)


def test_a_paused_agents_input_opens_with_the_stored_pause_line(main_db) -> None:
    """Pause contract rule 5 (#1973): the claim stores the pause, so replay matches.

    The line is rendered from the input's own stored metadata, never from live
    plan state, so the bytes a provider saw are the bytes history replays.
    """
    import json

    import plans.boundary as boundary
    import plans.service as service
    from core.message_history import frame_agent_envelope
    from history.messages import _INBOUND_SOURCE_KEYS
    from infra.input_admission import accept_thread_input
    from infra.input_render import project_claimed_input
    from infra.thread_inputs import start_idle_turn
    from plans.boundary import PlanActor

    owner = main_db.execute("SELECT user_id FROM threads WHERE id='main-1'").fetchone()[0]
    user = PlanActor(None, owner, None, kind="user")

    def change(operation: str) -> None:
        plan = service.ensure_plan("thread", "main-1", db=main_db)
        boundary.mutate(
            plan.id, operation,
            {"expected_revision": service._plan_row(main_db, plan.id)["revision"]},
            actor=user, provenance="test.pause_line", mutation_id=operation, db=main_db,
        )

    def claim(message_id: str):
        accept_thread_input("main-1", role="user", content="hello", intent="apply_now",
                            message_id=message_id, db=main_db)
        main_db.commit()
        batch = start_idle_turn("main-1", turn_id=f"turn-{message_id}",
                                boot_epoch="test", started_at="t", db=main_db)
        live = project_claimed_input(main_db, batch, thread_row={"user_id": owner},
                                     target=SimpleNamespace())
        row = dict(main_db.execute("SELECT * FROM messages WHERE id=?",
                                   (message_id,)).fetchone())
        main_db.execute("UPDATE active_turns SET status='completed' WHERE thread_id='main-1'")
        return live, row

    change("pause")
    paused, stored = claim("paused")
    change("resume")
    resumed, plain_row = claim("resumed")

    line, rest = paused.content.split("\n\n", 1)
    assert line.startswith("[Paused by the user since ")
    assert line.endswith(" UTC. In this Turn do only what the user's own message here "
                         "asks; otherwise read, reply and update your plan, and change "
                         "nothing else. Only the user resumes.]")
    assert rest == resumed.content == "hello"
    assert paused.agent_envelope.managed_prefix.startswith(line)
    meta = json.loads(stored["metadata"])
    inbound = {key: meta[key] for key in _INBOUND_SOURCE_KEYS if meta.get(key) is not None}
    assert frame_agent_envelope(stored["content"], inbound) == paused.content
    assert "plan_paused_since" not in json.loads(plain_row["metadata"] or "{}")


def test_legacy_declaration_rows_do_not_change_provider_projection() -> None:
    """An ignored legacy ledger cannot append a stale notice to a new input."""
    from infra.input_render import project_claimed_input
    from infra.schema import SCHEMA_SQL

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    try:
        db.executescript(SCHEMA_SQL)
        db.executescript(
            """
            CREATE TABLE thread_context_declarations (
                id TEXT PRIMARY KEY,
                thread_id TEXT NOT NULL,
                declaration_class TEXT NOT NULL,
                statement TEXT NOT NULL,
                subject_message_id TEXT,
                created_at TEXT NOT NULL,
                declared_at TEXT
            );
            INSERT INTO thread_context_declarations VALUES (
                'legacy-notice', 'thread-1', 'input_delivery_unconfirmed',
                'A stale delivery was never confirmed.', NULL, 't0', NULL
            );
            """
        )

        projection = project_claimed_input(
            db,
            _batch(),
            thread_row={"user_id": "user-1"},
            target=SimpleNamespace(),
        )

        assert "[CORSIA Context Notice]" not in projection.content
        assert "A stale delivery was never confirmed." not in projection.content
    finally:
        db.close()


def test_fresh_schema_does_not_create_orphaned_declaration_ledger() -> None:
    """New workspaces need no storage for a declaration system with no producer."""
    from infra.schema import SCHEMA_SQL

    db = sqlite3.connect(":memory:")
    try:
        db.executescript(SCHEMA_SQL)
        assert db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='thread_context_declarations'"
        ).fetchone() is None
    finally:
        db.close()
