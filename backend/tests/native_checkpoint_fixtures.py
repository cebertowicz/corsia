"""Small shared fixtures for Native checkpoint/cache-prefix tests."""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

from cache_warm_fixtures import make_exact_replay_selection


THREAD_ID = "main-1"


def cold_source(content: str = "cold history", *, thread_id: str = "thread-1"):
    """One frozen portable cold source for adapter transport tests."""
    from core.fork_types import ColdForkSource

    selection = make_exact_replay_selection(thread_id, [[("user", content)]])
    return ColdForkSource(selection.tail, ())


def install_prefix_source_isolation(monkeypatch) -> None:
    monkeypatch.setattr(
        "core.native_context_sources._context_sources_for_prefix",
        lambda *_args, **_kwargs: SimpleNamespace(
            installed=None, project_cwd=None,
            agent_facts={},
            sources=[],
        ),
    )


def native_target(context_window: int = 200_000):
    from llm.resolution import ExecutionTarget

    return replace(
        ExecutionTarget(
            catalog_id="openai/test-checkpoint",
            harness="native",
            provider="openai",
            backend="cloud",
            transport_model="openai/test-checkpoint",
            expected_reported_model=None,
            context_window=context_window,
            available=True,
            availability_reason=None,
            display_name="test-checkpoint",
            supports_reasoning=True,
        ),
        context_window=context_window,
    )


def exact_selection(turn_count: int = 2):
    turns = [
        [("user", f"request {index}"), ("assistant", f"answer {index}")]
        for index in range(1, turn_count + 1)
    ]
    return make_exact_replay_selection(THREAD_ID, turns)


def seed_native_thread(
    main_db,
    selection,
    *,
    model: str = "openai/test-checkpoint",
    effort: str | None = None,
) -> None:
    head = selection.sealed_boundary
    main_db.execute(
        "UPDATE threads SET model=?, history_epoch=?, reasoning_effort=? WHERE id=?",
        (model, head.history_epoch, effort, head.thread_id),
    )
    for turn in selection.tail.turns:
        for message in turn.messages:
            main_db.execute(
                "INSERT INTO messages "
                "(id,thread_id,role,content,metadata,created_at,seq) "
                "VALUES (?,?,?,?,?,'t',?)",
                (
                    message.message_id,
                    head.thread_id,
                    message.role,
                    message.content,
                    json.dumps({"turn_id": message.turn_id}),
                    message.seq,
                ),
            )
    main_db.commit()


def seed_vendor_checkpoint_thread(targets, *, thread_id: str = "thread-1") -> None:
    """Seed the durable thread a paid checkpoint adapter's real account admission reads.

    Call from a test that already uses ``isolated_db``. Adds synthetic Claude
    Code and Codex CLI connections, one catalog row per target and the thread.
    """
    from connections.models import upsert_connection
    from infra.database import get_db

    for provider in ("claude-code", "codex"):
        upsert_connection(type="cli", provider=provider, status="available",
                          config={"binary": "/bin/synthetic"})
    db = get_db()
    try:
        for target in targets:
            db.execute(
                "INSERT INTO models(id,harness,provider,provider_model_id,litellm_model,"
                "display_name,created_at,updated_at) VALUES (?,?,?,?,?,?,'t','t')",
                (target.catalog_id, target.harness, target.provider, target.transport_model,
                 target.catalog_id, target.display_name),
            )
        db.execute(
            "INSERT INTO threads(id,user_id,model,created_at,updated_at) "
            "VALUES (?,'test-user',?,'t','t')",
            (thread_id, targets[0].catalog_id),
        )
        db.commit()
    finally:
        db.close()
