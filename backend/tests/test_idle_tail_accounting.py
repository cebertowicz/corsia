"""A large tail past the installed checkpoint must reach idle admission.

No idle checkpoint folded between 2026-09-18 and 2026-09-21 while threads
carried hundreds of thousands of tokens past their last installed checkpoint.
This binds the accounting that exonerated the minimum-delta gate: a completed
Turn whose tail is far above ``IDLE_MIN_DELTA_TOKENS`` yields an eligible
context, never a ``below_minimum`` skip.
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

CC_MODEL = "claude-code/test"


def _db() -> sqlite3.Connection:
    from infra.schema import SCHEMA_SQL

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA_SQL)
    db.execute(
        "INSERT INTO models (id,harness,provider,provider_model_id,litellm_model,"
        "display_name,created_at,updated_at) VALUES (?,'claude-code','anthropic',"
        "?,?,?,'t0','t0')",
        (CC_MODEL,) * 4,
    )
    db.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
        "VALUES ('thread-1','user',?,'t0','t0')",
        (CC_MODEL,),
    )
    return db


def _installed_checkpoint(db: sqlite3.Connection, *, through_seq: int) -> None:
    db.execute(
        "INSERT INTO checkpoints "
        "(id,thread_id,history_epoch,through_seq,through_message_id,trigger,"
        " mode,status,source_harness,source_model,writer_harness,writer_model,"
        " continuity_base_revision,continuity_base_snapshot_json,started_at,"
        " raw_result,continuity_snapshot_json,captured_at,installed_at) "
        "VALUES ('cp-1','thread-1',0,?,?,'pressure','cold','installed',"
        " 'claude-code',?,'claude-code',?,0,'[]','t0','narrative','[]','t0','t0')",
        (through_seq, f"m-{through_seq}", CC_MODEL, CC_MODEL),
    )


def _delivered(
    db: sqlite3.Connection,
    *,
    seq: int,
    content: str,
    turn_id: str | None,
    role: str = "assistant",
) -> None:
    db.execute(
        "INSERT INTO messages (id,thread_id,role,content,created_at,seq,"
        " active_turn_id,delivery_state) "
        "VALUES (?,'thread-1',?,?,'t1',?,?,'delivered')",
        (f"m-{seq}", role, content, seq, turn_id),
    )


def _tail(db: sqlite3.Connection, *, after_seq: int, messages: int, chars: int) -> None:
    """One completed Turn carrying a large assistant tail past the checkpoint."""
    db.execute(
        "INSERT INTO active_turns "
        "(id,thread_id,execution_epoch,model,status,started_at,ended_at,"
        " boot_epoch,idle_checkpoint_pending) "
        "VALUES ('turn-0','thread-1',0,?,'completed','t0','2025-12-31T00:00:00',"
        " 'boot',0)",
        (CC_MODEL,),
    )
    db.execute(
        "INSERT INTO active_turns "
        "(id,thread_id,execution_epoch,model,status,started_at,ended_at,"
        " boot_epoch,idle_checkpoint_pending) "
        "VALUES ('turn-1','thread-1',0,?,'completed','t1','2026-01-01T00:00:00',"
        " 'boot',1)",
        (CC_MODEL,),
    )
    # The boundary message the installed checkpoint names, in its own Turn.
    _delivered(db, seq=after_seq, content="covered", turn_id="turn-0")
    _delivered(db, seq=after_seq + 1, content="go on", turn_id="turn-1", role="user")
    for index in range(messages):
        seq = after_seq + 2 + index
        _delivered(
            db, seq=seq, content="word " * (chars // 5), turn_id="turn-1",
        )


def test_large_tail_reaches_admission(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.checkpoint_types import IdleCheckpointSkip
    from infra.checkpoint_admission import IDLE_MIN_DELTA_TOKENS
    from infra.idle_checkpoints import IdleCheckpointDue
    from llm import resolution
    from thread.idle_checkpoint import IdleCheckpointContext, read_idle_context

    monkeypatch.setattr(
        resolution,
        "resolve_execution_target",
        lambda *_a, **_k: SimpleNamespace(
            harness="claude-code",
            available=True,
            availability_reason=None,
            context_window=200_000,
            catalog_id=CC_MODEL,
            expected_reported_model=None,
            transport_model=CC_MODEL,
        ),
    )

    db = _db()
    try:
        _installed_checkpoint(db, through_seq=100)
        _tail(db, after_seq=100, messages=200, chars=2_000)
        raw_chars = db.execute(
            "SELECT SUM(LENGTH(content)) FROM messages WHERE seq>100"
        ).fetchone()[0]
        assert raw_chars // 4 > IDLE_MIN_DELTA_TOKENS * 2
        result = read_idle_context(
            IdleCheckpointDue("turn-1", 1, "thread-1", "2026-01-01T00:00:00"),
            db=db,
        )
        assert not isinstance(result, IdleCheckpointSkip), (
            f"a {raw_chars // 4}-token tail was skipped: {result!r}"
        )
        assert isinstance(result, IdleCheckpointContext)
        assert result.boundary.seq > 100
    finally:
        db.close()
