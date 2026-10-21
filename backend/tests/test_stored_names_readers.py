"""The renamed readers find what the v79 migration converted, and nothing else.

Each test runs the code that reads or re-derives a stored name against a store
written before the rename and then migrated.
"""

from __future__ import annotations

import contextlib
import sqlite3
import uuid
from contextlib import closing
from io import BytesIO
from pathlib import Path

import pytest
from loguru import logger

from infra.database_migrations import migrate_to_current
from infra.schema import SCHEMA_VERSION
from stored_names_fixtures import (
    ARTIFACT_ID,
    DEAD_LETTER_SEED,
    NEW,
    OLD,
    REGISTRATION_SEED,
    RESTART_SEED,
    RESTART_TURN,
    SCOPE_ID,
    SYSTEM_BODY,
    THREAD_ID,
    build_store,
    derived,
)


@contextlib.contextmanager
def _store(tmp_path: Path):
    """The pre-rename store, migrated and opened by the renamed build."""
    path = tmp_path / "stored-names.db"
    build_store(path)
    with closing(sqlite3.connect(path)) as db:
        assert migrate_to_current(db, SCHEMA_VERSION).migrated
        db.row_factory = sqlite3.Row
        yield db


def _system_tier(db: sqlite3.Connection, workspace: Path) -> str:
    from core.installed_context import read_boundary, restore_sources
    from core.system_tier import system_tier_prompt

    _boundary_id, snapshot = read_boundary(THREAD_ID, db)
    assert snapshot is not None
    return system_tier_prompt(workspace, restore_sources(snapshot["sources"]))


def test_an_existing_thread_keeps_its_system_tier(tmp_path: Path) -> None:
    with _store(tmp_path) as db:
        tier = _system_tier(db, tmp_path)

    assert tier == f"{SYSTEM_BODY}\n\n## 20-CONTEXT.md\n\nMore rules."


def _dead_letter_again(db: sqlite3.Connection, monkeypatch, delivery_id: str) -> None:
    """Dead-letter a delivery the way a retried restart replay does."""
    from automation import restart_webhooks

    @contextlib.contextmanager
    def _transaction(*_args, **_kwargs):
        yield db

    monkeypatch.setattr(restart_webhooks, "write_transaction", _transaction)
    db.execute(
        "INSERT INTO webhook_deliveries (id,event_type,delivery_id,payload_hash,status,"
        "created_at) VALUES ('w',?,?,'h','claim','t0')",
        ("push", delivery_id),
    )
    row = {"delivery_id": delivery_id, "event_type": "push", "payload_json": "{}",
           "created_at": "t0"}
    assert restart_webhooks._dead_letter(row, "claim", reason="again") is True


def _dead_letters(db: sqlite3.Connection, delivery_id: str) -> int:
    return db.execute(
        "SELECT COUNT(*) FROM dead_letter_events WHERE external_id=?", (delivery_id,),
    ).fetchone()[0]


def test_a_retried_dead_letter_finds_its_converted_row(tmp_path: Path, monkeypatch) -> None:
    with _store(tmp_path) as db:
        _dead_letter_again(db, monkeypatch, "delivery-old")
        assert _dead_letters(db, "delivery-old") == 1


def _append_again(db: sqlite3.Connection, event_type: str, causal: str, scope: str | None):
    from registry.registration_events import _append_registration_event

    return _append_registration_event(
        db, artifact_id=ARTIFACT_ID, event_type=event_type, actor_thread_id=None,
        authority=None, reason=None, source_path=None, affected_thread_id=None,
        old_owner_thread_id=None, new_owner_thread_id=None, old_relationship_state=None,
        new_relationship_state=None, metadata=None, now="t1", causal_event_id=causal,
        idempotency_scope=scope,
    )


_CAUSAL_EVENTS = (("rehomed", "cause-1", None), ("origin_node_retired", "cause-2", SCOPE_ID))


def _events(db: sqlite3.Connection) -> int:
    return db.execute("SELECT COUNT(*) FROM artifact_registration_events").fetchone()[0]


@pytest.mark.parametrize(("event_type", "causal", "scope"), _CAUSAL_EVENTS)
def test_a_repeated_causal_event_finds_its_converted_row(
    tmp_path: Path, event_type: str, causal: str, scope: str | None,
) -> None:
    with _store(tmp_path) as db:
        before = _events(db)
        assert _append_again(db, event_type, causal, scope) is None
        assert _events(db) == before


def test_a_continuation_registered_again_finds_its_converted_row(tmp_path: Path) -> None:
    from automation.restart_wakes import register_hard_recovery

    with _store(tmp_path) as db:
        registered = register_hard_recovery(THREAD_ID, RESTART_TURN, db=db)

        assert registered == derived(NEW, RESTART_SEED, f"external:{THREAD_ID}:{RESTART_TURN}")
        assert [row["id"] for row in db.execute(
            "SELECT id FROM restart_wake_registrations WHERE turn_id=?", (RESTART_TURN,),
        )] == [registered]


def test_the_migration_derives_ids_from_the_seeds_the_writers_use() -> None:
    from automation import restart_wakes, restart_webhooks
    from registry import registration_events

    assert restart_webhooks._DEAD_LETTER_SEED == NEW + DEAD_LETTER_SEED
    assert restart_wakes._REGISTRATION_SEED == NEW + RESTART_SEED
    assert registration_events._REGISTRATION_EVENT_SEED == NEW + REGISTRATION_SEED
    assert registration_events._event_values(
        artifact_id=ARTIFACT_ID, event_type="rehomed", actor_thread_id=None, authority=None,
        reason=None, source_path=None, affected_thread_id=None, old_owner_thread_id=None,
        new_owner_thread_id=None, old_relationship_state=None, new_relationship_state=None,
        metadata=None, now="t1", causal_event_id="cause-1",
    )[0] == derived(NEW, REGISTRATION_SEED, f"{ARTIFACT_ID}:rehomed:cause-1")


# --- Claude Code transcripts (item 3): renamed in code only, never converted. ---

_SESSION = "resumed-session"
_ROW = "corsia-row"


def _transcript(name: str) -> bytes:
    """A reconstructed transcript as the build that used ``name`` wrote it."""
    import json

    def row_uuid(message_id: str, role: str) -> str:
        return str(uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"{name}:claude-code:reconstructed-transcript:{_SESSION}:{message_id}:{role}",
        ))

    assistant = row_uuid(_ROW, "assistant")
    records = (
        {"type": "user", "uuid": row_uuid("u", "user"),
         "message": {"role": "user", "content": "please fix it"}},
        {"type": "assistant", "uuid": assistant,
         "message": {"id": f"msg_{name}_" + assistant.replace("-", "")[:24],
                     "role": "assistant", "content": [{"type": "text", "text": "Fixed."}]}},
    )
    return b"".join(json.dumps(record).encode() + b"\n" for record in records)


def _boundary():
    from history.types import DurableBoundaryMessage

    return DurableBoundaryMessage(
        role="assistant", content="Fixed.", message_id=_ROW, assistant_step_id=_ROW,
    )


def test_a_transcript_written_by_this_build_matches_warm() -> None:
    from core.fork_cc_boundary_records import boundary_prefix_from_stream
    from subagents.cc_transcript_projection import _assistant_message_id, _stable_uuid

    transcript = _transcript(NEW)
    row_uuid = _stable_uuid(_SESSION, _ROW, "assistant")
    # The fixture is what this build writes: same row uuid, same message id.
    assert row_uuid.encode() in transcript
    assert _assistant_message_id(row_uuid).encode() in transcript
    assert _assistant_message_id(row_uuid).startswith(f"msg_{NEW}_")

    assert boundary_prefix_from_stream(BytesIO(transcript), _boundary(), _SESSION) == len(transcript)


@pytest.mark.asyncio
async def test_a_transcript_with_former_ids_refuses_warm_capture(tmp_path: Path) -> None:
    from core.fork_cc_boundary import _capture_boundary_identity
    from core.fork_cc_boundary_records import boundary_prefix_cut

    transcript = _transcript(OLD)
    path = tmp_path / "old-session.jsonl"
    path.write_bytes(transcript)
    lines: list[str] = []
    sink = logger.add(lambda m: lines.append(m.rstrip("\n")), format="{message}", level="INFO")
    try:
        source = await _capture_boundary_identity(
            thread_id=THREAD_ID, boundary=object(), warm_boundary=_boundary(), target=None,
            binary="claude", identity=(_SESSION, None, None, str(path)), live=None,
        )
    finally:
        logger.remove(sink)

    # Fail closed and visible: no prefix is proved, so the thread takes a cold
    # checkpoint. It is never a match on some other row.
    assert source is None
    assert lines == [
        f"fork warm capture refused for {THREAD_ID} (claude-code): boundary_not_found:assistant"
    ]
    assert boundary_prefix_cut(BytesIO(transcript), _boundary(), _SESSION) == (
        "boundary_not_found:assistant"
    )
