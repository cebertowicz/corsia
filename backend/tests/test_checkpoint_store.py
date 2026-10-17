"""Durable contracts for the one-table append-only checkpoint store."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import replace

import pytest


def _db() -> sqlite3.Connection:
    from infra.schema import SCHEMA_SQL

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA_SQL)
    return db


def _seed_thread(
    db: sqlite3.Connection,
    thread_id: str,
    *,
    epoch: int = 0,
    model: str = "native/Fortissimo",
) -> None:
    db.execute(
        "INSERT INTO threads "
        "(id,user_id,model,history_epoch,next_message_seq,created_at,updated_at) "
        "VALUES (?, 'user-1', ?, ?, 4, 't0', 't0')",
        (thread_id, model, epoch),
    )
    for seq in range(1, 4):
        db.execute(
            "INSERT INTO messages (id,thread_id,role,content,created_at,seq) "
            "VALUES (?,?,?,'body','t0',?)",
            (f"{thread_id}-m{seq}", thread_id, "user" if seq == 1 else "assistant", seq),
        )
    db.commit()


def _request(
    thread_id: str,
    operation_id: str,
    *,
    through_seq: int = 3,
    trigger: str = "pressure",
    source_harness: str = "native",
    source_model: str = "native/Fortissimo",
):
    from core.checkpoint_types import CheckpointRequest

    return CheckpointRequest(
        id=operation_id,
        thread_id=thread_id,
        history_epoch=0,
        previous_generation_id=None,
        through_seq=through_seq,
        through_message_id=f"{thread_id}-m{through_seq}",
        trigger=trigger,
        mode="warm",
        source_harness=source_harness,
        source_model=source_model,
        writer_harness=source_harness,
        writer_model=source_model,
        originating_turn_id="turn-1",
        first_included_seq=1,
        started_at="t1",
    )


def _capture(db, checkpoint_id: str, *, raw: str = "raw"):
    from infra.checkpoints import capture

    return capture(
        checkpoint_id,
        raw_result=raw,
        usage_json='{"input_tokens":100}',
        captured_at="t2",
        db=db,
    )


def _install(db, checkpoint_id: str):
    from infra.checkpoints import install

    return install(
        checkpoint_id,
        installed_at="t3",
        db=db,
    )


def test_normal_install_preflights_presence_and_artifact_before_writer(monkeypatch) -> None:
    from infra import checkpoint_install_preflight, checkpoint_receipts
    from infra import checkpoint_snapshots, checkpoints
    from infra.checkpoints import create_running

    db = _db()
    _seed_thread(db, "thread-1")
    checkpoint = create_running(_request("thread-1", "cp-1"), db=db)
    _capture(db, checkpoint.id)
    order: list[str] = []

    def preflight_connection():
        order.append("preflight")
        return db

    @contextmanager
    def writer(owned_db, *, scope):
        assert owned_db is None
        assert scope == "checkpoint_install"
        order.append("writer")
        yield db

    def artifact_snapshot(connection, thread_id):
        assert connection is db
        assert thread_id == "thread-1"
        order.append("artifact")
        return '{"rows":[]}'

    def snapshots(connection, thread_id, *, thread_row, artifact_snapshot_json=None):
        assert connection is db
        assert thread_id == "thread-1"
        assert artifact_snapshot_json == '{"rows":[]}'
        order.append("snapshot-consumed")
        return artifact_snapshot_json, "null"

    def publish(record, model, installed_at, connection, *, presence_line):
        assert record.id == checkpoint.id
        assert connection is db
        assert presence_line == "Away"
        order.append("presence-consumed")

    monkeypatch.setattr(checkpoint_install_preflight, "get_db", preflight_connection)
    monkeypatch.setattr(checkpoint_receipts, "_presence_line_at", lambda *_: "Away")
    monkeypatch.setattr(checkpoint_snapshots, "capture_artifact_snapshot", artifact_snapshot)
    monkeypatch.setattr(checkpoint_snapshots, "capture_checkpoint_snapshots", snapshots)
    monkeypatch.setattr(checkpoints, "write_transaction", writer)
    monkeypatch.setattr(checkpoint_receipts, "publish_install", publish)
    monkeypatch.setattr(checkpoints, "index_checkpoint_entries", lambda *_: None)
    monkeypatch.setattr("infra.input_writes.release_checkpoint_inputs", lambda *_args, **_kwargs: None)

    assert checkpoints.install(checkpoint.id, installed_at="t3").status == "installed"
    assert order == [
        "preflight", "artifact", "writer", "snapshot-consumed", "presence-consumed",
    ]


def test_install_replays_preflight_failure_only_for_eligible_checkpoint(monkeypatch) -> None:
    from infra import checkpoint_install_preflight
    from infra.checkpoints import create_running

    db = _db()
    _seed_thread(db, "thread-1")
    checkpoint = create_running(_request("thread-1", "cp-1"), db=db)
    _capture(db, checkpoint.id)
    failure = RuntimeError("preflight read failed")
    monkeypatch.setattr(
        checkpoint_install_preflight,
        "prepare_install_preflight",
        lambda *_args: (None, failure),
    )

    with pytest.raises(RuntimeError, match="preflight read failed"):
        _install(db, checkpoint.id)
    assert db.execute(
        "SELECT status FROM checkpoints WHERE id=?", (checkpoint.id,),
    ).fetchone()["status"] == "captured"

    db.execute("UPDATE threads SET history_epoch=1 WHERE id='thread-1'")
    db.commit()
    assert _install(db, checkpoint.id).status == "overtaken"


def test_clean_connection_preflight_freezes_artifacts_before_writer(monkeypatch) -> None:
    """An interleaving artifact write cannot replace normal preflight bytes."""
    from infra import checkpoints
    from infra.checkpoints import create_running
    from registry.models import register_artifact

    db = _db()
    _seed_thread(db, "thread-1")
    register_artifact(
        integration="workspace", type="document", external_id="before",
        parent_thread_id="thread-1", title="Before", db=db,
    )
    checkpoint = create_running(_request("thread-1", "cp-1"), db=db)
    _capture(db, checkpoint.id)
    original_writer = checkpoints.write_transaction

    @contextmanager
    def writer(owned_db, *, scope):
        assert owned_db is db
        assert scope == "checkpoint_install"
        register_artifact(
            integration="workspace", type="document", external_id="after",
            parent_thread_id="thread-1", title="After", db=db,
        )
        with original_writer(owned_db, scope=scope) as connection:
            yield connection

    monkeypatch.setattr(checkpoints, "write_transaction", writer)

    assert checkpoints.install(checkpoint.id, installed_at="t3", db=db).status == "installed"
    snapshot = json.loads(db.execute(
        "SELECT artifact_snapshot_json FROM checkpoints WHERE id=?", (checkpoint.id,),
    ).fetchone()["artifact_snapshot_json"])
    assert [row["title"] for row in snapshot["rows"]] == ["Before"]


def test_outer_transaction_keeps_checkpoint_snapshot_in_its_owned_connection(monkeypatch) -> None:
    from infra import checkpoint_install_preflight
    from infra.checkpoints import create_running
    from infra.database import write_transaction

    db = _db()
    _seed_thread(db, "thread-1")
    checkpoint = create_running(_request("thread-1", "cp-1"), db=db)
    _capture(db, checkpoint.id)
    monkeypatch.setattr(
        checkpoint_install_preflight,
        "get_db",
        lambda: pytest.fail("caller-owned install must not open a preflight connection"),
    )

    with write_transaction(db, scope="outer_install"):
        assert _install(db, checkpoint.id).status == "installed"
        assert db.in_transaction


def test_normal_install_moves_150_artifact_pane_read_before_writer(
    isolated_db, monkeypatch,
) -> None:
    """Measure the candidate's boundary against the former in-writer path."""
    import infra.database as database
    from infra import checkpoint_install_preflight, checkpoint_snapshots
    from infra.checkpoints import capture, create_running, install
    from registry.models import register_artifact

    db = database.get_db()

    def seed(thread_id: str, checkpoint_id: str):
        _seed_thread(db, thread_id)
        db.execute(
            "UPDATE messages SET created_at='2026-10-06T13:00:00+00:00' "
            "WHERE thread_id=?", (thread_id,),
        )
        for entry_id in range(1, 451):
            db.execute(
                "INSERT INTO continuity_entries "
                "(thread_id,history_epoch,entry_id,field,body,revision,state,created_at,updated_at) "
                "VALUES (?,0,?,'facts',?,1,'active','t0','t0')",
                (thread_id, entry_id, json.dumps({"body": "e" * 80})),
            )
        db.commit()
        for artifact_id in range(150):
            register_artifact(
                integration="workspace", type="document",
                external_id=f"{thread_id}-doc-{artifact_id}",
                parent_thread_id=thread_id, title=f"Doc {artifact_id}", db=db,
            )
        checkpoint = create_running(_request(thread_id, checkpoint_id), db=db)
        capture(
            checkpoint.id, raw_result="raw", usage_json="{}", captured_at="t2", db=db,
        )
        return checkpoint

    candidate = seed("measured-candidate", "cp-candidate")
    baseline = seed("measured-baseline", "cp-baseline")
    phase = "candidate"
    holder: dict[str, dict[str, float]] = {"candidate": {}, "baseline": {}}
    pane: dict[str, dict[str, float]] = {"candidate": {}, "baseline": {}}
    original_finish = database._finish_write_hold
    original_pane = checkpoint_snapshots.capture_artifact_snapshot

    def finish(scope, started_at):
        if scope == "checkpoint_install":
            holder[phase]["started"] = started_at
            holder[phase]["finished"] = time.monotonic()
        return original_finish(scope, started_at)

    def capture_pane(connection, thread_id):
        pane[phase]["started"] = time.monotonic()
        try:
            return original_pane(connection, thread_id)
        finally:
            pane[phase]["finished"] = time.monotonic()

    monkeypatch.setattr(database, "_finish_write_hold", finish)
    monkeypatch.setattr(checkpoint_snapshots, "capture_artifact_snapshot", capture_pane)

    assert install(
        candidate.id, installed_at="2026-10-06T14:00:00+00:00",
    ).status == "installed"
    phase = "baseline"
    monkeypatch.setattr(
        checkpoint_install_preflight, "prepare_install_preflight", lambda *_args: (None, None),
    )
    assert install(
        baseline.id, installed_at="2026-10-06T14:00:00+00:00",
    ).status == "installed"

    assert pane["candidate"]["finished"] <= holder["candidate"]["started"]
    assert holder["baseline"]["started"] <= pane["baseline"]["started"]
    assert pane["baseline"]["finished"] <= holder["baseline"]["finished"]
    measurements = {
        name: (
            (holder[name]["finished"] - holder[name]["started"]) * 1_000,
            (pane[name]["finished"] - pane[name]["started"]) * 1_000,
        )
        for name in ("candidate", "baseline")
    }
    print(
        "checkpoint_install "
        f"candidate_hold_ms={measurements['candidate'][0]:.3f} "
        f"candidate_pane_ms={measurements['candidate'][1]:.3f} "
        f"baseline_hold_ms={measurements['baseline'][0]:.3f} "
        f"baseline_pane_ms={measurements['baseline'][1]:.3f}"
    )


def test_only_one_running_or_captured_checkpoint_exists_per_thread() -> None:
    from infra.checkpoints import (
        CheckpointConflict,
        CheckpointConflictKind,
        cancel,
        create_running,
    )

    db = _db()
    _seed_thread(db, "thread-1")
    first = create_running(_request("thread-1", "cp-1"), db=db)

    with pytest.raises(CheckpointConflict, match="active checkpoint") as excinfo:
        create_running(_request("thread-1", "cp-2"), db=db)
    assert excinfo.value.kind is CheckpointConflictKind.ACTIVE_CHECKPOINT

    assert cancel(first.id, ended_at="t2", db=db).status == "cancelled"
    assert create_running(_request("thread-1", "cp-2"), db=db).status == "running"


def test_read_preserves_legacy_flat_usage_json_without_reinterpretation() -> None:
    """Historical checkpoint usage remains inspectable in its original shape."""
    from infra.checkpoints import create_running
    from infra.checkpoint_queries import read

    db = _db()
    _seed_thread(db, "legacy")
    create_running(_request("legacy", "cp-legacy"), db=db)
    _capture(db, "cp-legacy")

    record = read("cp-legacy", db=db)

    assert record is not None
    assert record.usage_json == '{"input_tokens":100}'
    assert json.loads(record.usage_json) == {"input_tokens": 100}


def test_capture_and_stop_are_one_exact_compare_and_swap() -> None:
    from infra.checkpoints import CheckpointTransitionError, cancel, create_running

    db = _db()
    _seed_thread(db, "captured")
    create_running(_request("captured", "cp-captured"), db=db)
    assert _capture(db, "cp-captured").status == "captured"
    assert cancel("cp-captured", ended_at="t3", db=db) is None

    _seed_thread(db, "cancelled")
    create_running(_request("cancelled", "cp-cancelled"), db=db)
    assert cancel("cp-cancelled", ended_at="t2", db=db).status == "cancelled"
    with pytest.raises(CheckpointTransitionError, match="capture"):
        _capture(db, "cp-cancelled")


def test_failure_is_terminal_only_while_nonterminal() -> None:
    from infra.checkpoints import (
        CheckpointTransitionError,
        create_running,
        fail,
    )

    db = _db()
    _seed_thread(db, "failed")
    create_running(_request("failed", "cp-failed"), db=db)
    failed = fail("cp-failed", error="provider refused", ended_at="t2", db=db)
    assert (failed.status, failed.error) == ("failed", "provider refused")
    with pytest.raises(CheckpointTransitionError, match="capture"):
        _capture(db, "cp-failed")
    with pytest.raises(CheckpointTransitionError, match="failure"):
        fail("cp-failed", error="again", ended_at="t3", db=db)

    # A captured row is failed only by its own install raising; its bytes stay.
    _seed_thread(db, "captured-fail")
    create_running(_request("captured-fail", "cp-captured-fail"), db=db)
    captured = _capture(db, "cp-captured-fail")
    install_failed = fail("cp-captured-fail", error="install raised", ended_at="t3", db=db)
    assert (install_failed.status, install_failed.error) == ("failed", "install raised")
    assert install_failed.raw_result == captured.raw_result


def test_install_preserves_raw_bytes_and_advances_monotonic_head() -> None:
    from infra.checkpoints import create_running
    from infra.checkpoint_queries import current_installed

    db = _db()
    _seed_thread(db, "thread-1")
    first = create_running(_request("thread-1", "cp-1", through_seq=2), db=db)
    raw = "{unknown:\u0000}\nplain prose\n"
    _capture(db, first.id, raw=raw)
    installed = _install(db, first.id)
    assert installed.raw_result == raw
    assert installed.status == "installed"

    second_request = replace(
        _request("thread-1", "cp-2"), previous_generation_id=first.id
    )
    second = create_running(second_request, db=db)
    _capture(db, second.id, raw="second")
    _install(db, second.id)

    assert current_installed("thread-1", db=db).id == second.id
    assert db.execute(
        "SELECT COUNT(*) FROM checkpoints WHERE thread_id='thread-1'"
    ).fetchone()[0] == 2


def test_create_running_returns_exact_installed_race_winner() -> None:
    from infra.checkpoints import create_running

    db = _db()
    _seed_thread(db, "winner")
    request = _request("winner", "cp-candidate")
    winning_request = replace(request, id="cp-winner")
    winner = create_running(winning_request, db=db)
    _capture(db, winner.id)
    installed = _install(db, winner.id)

    admitted = create_running(request, db=db)

    assert admitted == installed
    assert db.execute(
        "SELECT COUNT(*) FROM checkpoints WHERE thread_id='winner'"
    ).fetchone()[0] == 1


@pytest.mark.parametrize(
    "candidate",
    (
        lambda request: replace(request, trigger="manual"),
        lambda request: replace(request, mode="cold"),
        lambda request: replace(request, writer_model="native/other"),
        lambda request: replace(request, first_included_seq=2),
        lambda request: replace(request, through_seq=2, through_message_id="invalid"),
    ),
    ids=(
        "unrelated-trigger",
        "unrelated-mode",
        "unrelated-writer",
        "unrelated-first-seq",
        "invalid-boundary",
    ),
)
def test_create_running_rejects_non_winning_installed_head(candidate) -> None:
    from infra.checkpoints import CheckpointConflict, create_running

    db = _db()
    _seed_thread(db, "not-winner")
    request = _request("not-winner", "cp-candidate")
    winner = create_running(replace(request, id="cp-winner"), db=db)
    _capture(db, winner.id)
    _install(db, winner.id)

    with pytest.raises(CheckpointConflict):
        create_running(candidate(request), db=db)


def test_create_running_rejects_stale_epoch_despite_installed_head() -> None:
    from infra.checkpoints import CheckpointConflict, CheckpointConflictKind, create_running

    db = _db()
    _seed_thread(db, "stale-epoch")
    request = _request("stale-epoch", "cp-candidate")
    winner = create_running(replace(request, id="cp-winner"), db=db)
    _capture(db, winner.id)
    _install(db, winner.id)
    db.execute("UPDATE threads SET history_epoch=1 WHERE id='stale-epoch'")
    db.commit()

    with pytest.raises(CheckpointConflict, match="history epoch") as excinfo:
        create_running(request, db=db)
    assert excinfo.value.kind is CheckpointConflictKind.STALE_EPOCH


def test_create_running_rejects_unchanged_installed_head() -> None:
    from infra.checkpoints import CheckpointConflict, CheckpointConflictKind, create_running

    db = _db()
    _seed_thread(db, "unchanged-head")
    first = create_running(_request("unchanged-head", "cp-1"), db=db)
    _capture(db, first.id)
    _install(db, first.id)
    request = replace(
        _request("unchanged-head", "cp-2"), previous_generation_id=first.id,
    )

    with pytest.raises(CheckpointConflict, match="does not advance") as excinfo:
        create_running(request, db=db)
    assert excinfo.value.kind is CheckpointConflictKind.BOUNDARY_NOT_ADVANCING


def test_create_running_rejects_candidate_older_than_winner_predecessor() -> None:
    from infra.checkpoints import CheckpointConflict, CheckpointConflictKind, create_running

    db = _db()
    _seed_thread(db, "older-candidate")
    first = create_running(
        _request("older-candidate", "cp-1", through_seq=2), db=db,
    )
    _capture(db, first.id)
    _install(db, first.id)
    second = create_running(
        replace(
            _request("older-candidate", "cp-2"),
            previous_generation_id=first.id,
        ),
        db=db,
    )
    _capture(db, second.id)
    _install(db, second.id)

    with pytest.raises(CheckpointConflict, match="predecessor") as excinfo:
        create_running(_request("older-candidate", "cp-candidate"), db=db)
    assert excinfo.value.kind is CheckpointConflictKind.STALE_PREDECESSOR


def test_create_running_rejects_boundary_no_longer_durable() -> None:
    from infra.checkpoints import CheckpointConflict, CheckpointConflictKind, create_running

    db = _db()
    _seed_thread(db, "missing-boundary")
    request = replace(
        _request("missing-boundary", "cp-1"), through_message_id="does-not-exist",
    )

    with pytest.raises(CheckpointConflict, match="no longer durable") as excinfo:
        create_running(request, db=db)
    assert excinfo.value.kind is CheckpointConflictKind.BOUNDARY_NOT_DURABLE


def _owner_map(thread_id: str, db):
    """The map row the collector delivers against the current boundary."""
    from core.agent_context_sources import installed_owner_map
    from core.installed_context import read_boundary

    _boundary_id, stored = read_boundary(thread_id, db)
    return installed_owner_map(thread_id=thread_id, db=db, stored=stored)[0]


def test_install_receipt_freezes_responsibility_map_for_checkpoint_adoption() -> None:
    from core.agent_context_sources import current_responsibility_map_snapshot
    from core.installed_context import (
        InstalledDelivery,
        persist_installed_delivery,
        read_boundary,
    )
    from infra.checkpoints import create_running

    db = _db()
    _seed_thread(db, "thread-1")
    db.execute(
        "UPDATE threads SET title='Current',agent_enabled=1,"
        "agent_responsibility='Owns this lane.' WHERE id='thread-1'"
    )
    db.execute(
        "INSERT INTO threads "
        "(id,user_id,title,model,agent_enabled,agent_responsibility,created_at,updated_at) "
        "VALUES ('owner-2','user-1','Other','native/Fortissimo',1,"
        "'Before checkpoint.','t0','t0')"
    )
    initial = current_responsibility_map_snapshot("thread-1", db)
    boundary_id, stored = read_boundary("thread-1", db)
    assert persist_installed_delivery(InstalledDelivery(
        thread_id="thread-1", adoption="current-no-anchor",
        boundary_id=boundary_id, stored=stored,
        snapshot={**initial, "sources": [], "role_facts": {}},
    ), db=db)
    checkpoint = create_running(_request("thread-1", "cp-1"), db=db)
    _capture(db, checkpoint.id)
    db.execute(
        "UPDATE threads SET agent_responsibility='At checkpoint.' WHERE id='owner-2'"
    )

    _install(db, checkpoint.id)
    metadata = db.execute(
        "SELECT metadata FROM messages WHERE id='checkpoint-receipt:cp-1'"
    ).fetchone()["metadata"]
    snapshot = json.loads(metadata)["responsibility_map_snapshot"]
    assert snapshot["model"] == "native/Fortissimo"
    assert "At checkpoint." in snapshot["content"]

    db.execute(
        "UPDATE threads SET agent_responsibility='After checkpoint.' WHERE id='owner-2'"
    )
    source = _owner_map("thread-1", db)
    assert "At checkpoint." in (source.prompt_content or "")
    assert "After checkpoint." not in (source.prompt_content or "")


def test_model_switch_checkpoint_freezes_map_for_destination_model() -> None:
    from infra.checkpoints import create_running

    db = _db()
    _seed_thread(db, "thread-1", model="claude-code/source")
    db.execute(
        "UPDATE threads SET title='Current',agent_enabled=1,"
        "agent_responsibility='Destination responsibility map.' WHERE id='thread-1'"
    )
    db.execute(
        "UPDATE threads SET pending_model='codex/target',pending_execution_epoch=1 "
        "WHERE id='thread-1'"
    )
    request = replace(
        _request(
            "thread-1", "cp-switch", trigger="model_switch",
            source_harness="claude-code", source_model="claude-code/source",
        ),
        mode="cold",
        writer_harness="codex",
        writer_model="codex/target",
    )
    checkpoint = create_running(request, db=db)
    _capture(db, checkpoint.id)
    _install(db, checkpoint.id)
    db.execute(
        "UPDATE threads SET model='codex/target',execution_epoch=1,"
        "pending_model=NULL,pending_execution_epoch=NULL,"
        "agent_responsibility='Changed after adoption.' WHERE id='thread-1'"
    )

    source = _owner_map("thread-1", db)
    assert "Destination responsibility map." in (source.prompt_content or "")
    assert "Changed after adoption." not in (source.prompt_content or "")


def test_source_written_switch_checkpoint_freezes_destination_map() -> None:
    from infra.checkpoints import create_running

    db = _db()
    _seed_thread(db, "thread-1", model="claude-code/source")
    db.execute(
        "UPDATE threads SET pending_model='codex/target',pending_execution_epoch=1,"
        "title='Current',agent_enabled=1,"
        "agent_responsibility='Destination responsibility map.' "
        "WHERE id='thread-1'"
    )
    checkpoint = create_running(
        _request(
            "thread-1", "cp-switch", trigger="model_switch",
            source_harness="claude-code", source_model="claude-code/source",
        ),
        db=db,
    )
    _capture(db, checkpoint.id)
    _install(db, checkpoint.id)
    metadata = json.loads(db.execute(
        "SELECT metadata FROM messages WHERE id='checkpoint-receipt:cp-switch'"
    ).fetchone()["metadata"])
    snapshot = metadata["responsibility_map_snapshot"]
    assert (snapshot["model"], snapshot["execution_epoch"]) == (
        "codex/target", 1,
    )

    db.execute(
        "UPDATE threads SET model='codex/target',execution_epoch=1,"
        "pending_model=NULL,pending_execution_epoch=NULL,"
        "agent_responsibility='Changed after adoption.' WHERE id='thread-1'"
    )
    source = _owner_map("thread-1", db)
    assert "Destination responsibility map." in (source.prompt_content or "")
    assert "Changed after adoption." not in (source.prompt_content or "")


@pytest.mark.parametrize(
    ("trigger", "harness", "model", "integration", "artifact_type"),
    (
        ("manual", "claude-code", "claude-code/sonnet", "claude-code", "session"),
        ("pressure", "claude-code", "claude-code/sonnet", "claude-code", "session"),
        ("idle", "claude-code", "claude-code/sonnet", "claude-code", "session"),
        ("manual", "codex", "codex/luna", "codex", "thread"),
        ("pressure", "codex", "codex/luna", "codex", "thread"),
        ("idle", "codex", "codex/luna", "codex", "thread"),
    ),
)
def test_install_retires_matching_external_continuation(
    trigger: str,
    harness: str,
    model: str,
    integration: str,
    artifact_type: str,
) -> None:
    """A usable install makes the old durable provider context ineligible."""
    from infra.checkpoints import create_running
    from registry.models import find_for_thread, register_artifact

    db = _db()
    _seed_thread(db, "external", model=model)
    register_artifact(
        integration=integration,
        type=artifact_type,
        external_id="old-provider-context",
        parent_thread_id="external",
        db=db,
    )
    checkpoint = create_running(
        _request(
            "external",
            "cp-external",
            trigger=trigger,
            source_harness=harness,
            source_model=model,
        ),
        db=db,
    )
    _capture(db, checkpoint.id)

    assert _install(db, checkpoint.id).status == "installed"
    assert find_for_thread(
        "external", integration, artifact_type, db=db,
    ) is None
    from infra.runtime_truth import provider_boundary_snapshot

    _, installed, eager = provider_boundary_snapshot("external", harness, model, db=db)
    assert eager is not None and eager.id == installed.id == checkpoint.id


@pytest.mark.parametrize(
    ("trigger", "source_harness", "source_model", "selected_model", "overtaken"),
    (
        ("manual", "native", "native/Fortissimo", "native/Fortissimo", False),
        ("model_switch", "claude-code", "claude-code/sonnet", "claude-code/sonnet", False),
        ("pressure", "claude-code", "claude-code/sonnet", "claude-code/opus", False),
        ("manual", "claude-code", "claude-code/sonnet", "claude-code/sonnet", True),
    ),
)
def test_install_keeps_external_continuation_when_cutover_is_not_eligible(
    trigger: str,
    source_harness: str,
    source_model: str,
    selected_model: str,
    overtaken: bool,
) -> None:
    """Only a current manual/pressure checkpoint retires its source handle."""
    from infra.checkpoints import create_running
    from registry.models import find_for_thread, register_artifact

    db = _db()
    _seed_thread(db, "external", model=source_model)
    registration = register_artifact(
        integration="claude-code",
        type="session",
        external_id="keep-provider-context",
        parent_thread_id="external",
        db=db,
    )
    checkpoint = create_running(
        _request(
            "external",
            "cp-keep",
            trigger=trigger,
            source_harness=source_harness,
            source_model=source_model,
        ),
        db=db,
    )
    _capture(db, checkpoint.id)
    db.execute(
        "UPDATE threads SET model=?,history_epoch=? WHERE id='external'",
        (selected_model, 1 if overtaken else 0),
    )
    db.commit()

    expected_status = "overtaken" if overtaken else "installed"
    assert _install(db, checkpoint.id).status == expected_status
    assert find_for_thread(
        "external", "claude-code", "session", db=db,
    )["id"] == registration["id"]


def test_install_rolls_back_continuation_retirement_with_receipt_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Checkpoint publication and source-handle retirement are one commit."""
    from infra import checkpoint_receipts
    from infra.checkpoints import create_running
    from registry.models import find_for_thread, register_artifact

    db = _db()
    model = "codex/luna"
    _seed_thread(db, "external", model=model)
    registration = register_artifact(
        integration="codex",
        type="thread",
        external_id="rollback-provider-context",
        parent_thread_id="external",
        db=db,
    )
    checkpoint = create_running(
        _request(
            "external",
            "cp-rollback",
            trigger="pressure",
            source_harness="codex",
            source_model=model,
        ),
        db=db,
    )
    _capture(db, checkpoint.id)

    def fail_receipt(*_args, **_kwargs) -> None:
        raise RuntimeError("receipt failed")

    monkeypatch.setattr(checkpoint_receipts, "record_install_receipt", fail_receipt)
    with pytest.raises(RuntimeError, match="receipt failed"):
        _install(db, checkpoint.id)

    assert db.execute(
        "SELECT status FROM checkpoints WHERE id=?", (checkpoint.id,),
    ).fetchone()["status"] == "captured"
    assert find_for_thread(
        "external", "codex", "thread", db=db,
    )["id"] == registration["id"]


def test_install_is_atomic_with_concurrent_canonical_writers(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Receipt, FTS, continuity and held-input release publish in one commit."""
    from infra import checkpoint_receipts
    from infra.checkpoints import create_running
    from infra.database import initialize_schema
    from infra.queries import insert_message
    from memory.checkpoint_search import search_checkpoints
    from plans import service

    path = tmp_path / "checkpoint-install.db"

    def connect() -> sqlite3.Connection:
        connection = sqlite3.connect(path, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    installer = connect()
    installer.execute("PRAGMA journal_mode=WAL")
    initialize_schema(installer)
    _seed_thread(installer, "thread-1")
    checkpoint = create_running(_request("thread-1", "cp-atomic"), db=installer)
    _capture(installer, checkpoint.id, raw="Atomic checkpoint narrative")

    observer = connect()
    receipt_staged = threading.Event()
    release_install = threading.Event()
    original_publish = checkpoint_receipts.publish_install

    def pause_after_receipt(record, selected_model, installed_at, db, **kwargs) -> None:
        original_publish(record, selected_model, installed_at, db, **kwargs)
        receipt_staged.set()
        assert release_install.wait(5), "test did not release checkpoint install"

    monkeypatch.setattr(checkpoint_receipts, "publish_install", pause_after_receipt)
    errors: list[BaseException] = []

    def guarded(action) -> None:
        try:
            action()
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    install_thread = threading.Thread(
        target=lambda: guarded(lambda: _install(installer, checkpoint.id)),
    )
    install_thread.start()
    assert receipt_staged.wait(5)

    receipt_id = checkpoint_receipts.completion_receipt_id(checkpoint.id)
    assert observer.execute(
        "SELECT 1 FROM messages WHERE id=?", (receipt_id,),
    ).fetchone() is None
    assert observer.execute(
        "SELECT status FROM checkpoints WHERE id=?", (checkpoint.id,),
    ).fetchone()[0] == "captured"

    tool_started = threading.Event()
    plan_started = threading.Event()

    def write_tool() -> None:
        connection = connect()
        try:
            tool_started.set()
            insert_message(
                "thread-1", "tool", "concurrent canonical tool result",
                {"tool_call_id": "call-1", "turn_unbound_reason": "legacy_import"},
                msg_id="concurrent-tool", db=connection,
            )
        finally:
            connection.close()

    def write_plan() -> None:
        connection = connect()
        try:
            plan_started.set()
            service.ensure_plan("thread", "thread-1", db=connection)
        finally:
            connection.close()

    tool_thread = threading.Thread(target=lambda: guarded(write_tool))
    plan_thread = threading.Thread(target=lambda: guarded(write_plan))
    tool_thread.start()
    plan_thread.start()
    assert tool_started.wait(5) and plan_started.wait(5)
    assert observer.execute(
        "SELECT 1 FROM messages WHERE id='concurrent-tool'"
    ).fetchone() is None
    assert observer.execute(
        "SELECT 1 FROM plans WHERE owner_kind='thread' AND owner_id='thread-1'"
    ).fetchone() is None

    release_install.set()
    for thread in (install_thread, tool_thread, plan_thread):
        thread.join(10)
        assert not thread.is_alive()
    assert errors == []

    rows = observer.execute(
        "SELECT id,seq FROM messages WHERE id IN (?, 'concurrent-tool') ORDER BY seq",
        (receipt_id,),
    ).fetchall()
    assert [row["id"] for row in rows] == [receipt_id, "concurrent-tool"]
    assert observer.execute(
        "SELECT 1 FROM plans WHERE owner_kind='thread' AND owner_id='thread-1'"
    ).fetchone() is not None
    assert [hit["checkpoint_id"] for hit in search_checkpoints(
        observer, "atomic", caller_thread_id="thread-1",
    )] == [checkpoint.id]
    installer.close()
    observer.close()


def test_model_switch_may_replace_the_same_installed_boundary() -> None:
    """A smaller destination can rewrite continuity without fake history."""
    from infra.checkpoints import create_running
    from infra.checkpoint_queries import current_installed

    db = _db()
    _seed_thread(db, "thread-1")
    first = create_running(_request("thread-1", "cp-1"), db=db)
    _capture(db, first.id, raw="source-authored")
    _install(db, first.id)

    refresh = replace(
        _request("thread-1", "cp-2"),
        previous_generation_id=first.id,
        trigger="model_switch",
        mode="cold",
        writer_model="native/target",
    )
    create_running(refresh, db=db)
    _capture(db, refresh.id, raw="target-authored")
    installed = _install(db, refresh.id)

    assert installed.status == "installed"
    assert current_installed("thread-1", db=db).id == refresh.id
    assert db.execute(
        "SELECT COUNT(*) FROM checkpoints WHERE thread_id='thread-1'",
    ).fetchone()[0] == 2


def test_late_candidate_is_retained_as_overtaken() -> None:
    from infra.checkpoints import create_running

    db = _db()
    _seed_thread(db, "thread-1")
    first = create_running(_request("thread-1", "cp-1", through_seq=1), db=db)
    _capture(db, first.id)
    _install(db, first.id)
    candidate = create_running(
        replace(
            _request("thread-1", "cp-late", through_seq=2),
            previous_generation_id=first.id,
        ),
        db=db,
    )
    _capture(db, candidate.id)
    db.execute(
        "INSERT INTO checkpoints "
        "(id,thread_id,history_epoch,previous_generation_id,through_seq,"
        "through_message_id,trigger,mode,status,source_harness,source_model,"
        "writer_harness,writer_model,continuity_base_revision,"
        "continuity_base_snapshot_json,raw_result,continuity_snapshot_json,"
        "started_at,captured_at,installed_at,ended_at) VALUES "
        "('cp-new','thread-1',0,'cp-1',3,'thread-1-m3','pressure','warm',"
        "'installed','native','native/Fortissimo','native','native/Fortissimo',"
        "7,'[]','new','[]','t2','t3','t4','t4')"
    )
    db.commit()

    assert _install(db, candidate.id).status == "overtaken"


def test_overtaken_candidate_retains_provider_diagnostic() -> None:
    from infra.checkpoints import create_running

    db = _db()
    _seed_thread(db, "thread-1")
    candidate = create_running(_request("thread-1", "cp-1"), db=db)
    from infra.checkpoints import capture
    capture(
        candidate.id,
        raw_result="paid body",
        usage_json=None,
        captured_at="t2",
        diagnostic="provider process exited after output",
        db=db,
    )
    db.execute("UPDATE threads SET history_epoch=1 WHERE id='thread-1'")
    db.commit()

    overtaken = _install(db, candidate.id)

    assert overtaken.status == "overtaken"
    assert overtaken.error == (
        "provider process exited after output; checkpoint boundary was overtaken"
    )


def test_epoch_change_overtakes_candidate_and_hides_old_head() -> None:
    from infra.checkpoints import create_running
    from infra.checkpoint_queries import current_installed

    db = _db()
    _seed_thread(db, "thread-1")
    candidate = create_running(_request("thread-1", "cp-1"), db=db)
    _capture(db, candidate.id)
    db.execute("UPDATE threads SET history_epoch=1 WHERE id='thread-1'")
    db.commit()

    assert _install(db, candidate.id).status == "overtaken"
    assert current_installed("thread-1", db=db) is None


def test_startup_fails_running_publishes_captured_and_keeps_installed() -> None:
    from infra.checkpoints import create_running, reconcile_startup

    db = _db()
    for thread_id in ("running", "captured", "installed"):
        _seed_thread(db, thread_id)
        create_running(_request(thread_id, f"cp-{thread_id}"), db=db)
    _capture(db, "cp-captured")
    _capture(db, "cp-installed")
    _install(db, "cp-installed")

    reconciled = reconcile_startup(reconciled_at="restart", db=db)

    assert {record.id: record.status for record in reconciled} == {
        "cp-running": "failed",
        "cp-captured": "installed",
    }
    assert db.execute(
        "SELECT status FROM checkpoints WHERE id='cp-installed'"
    ).fetchone()[0] == "installed"
