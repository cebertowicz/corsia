from __future__ import annotations

from dataclasses import replace

import pytest

from tests.test_context_preview_contracts import (
    _checkpointed_preview_connection,
    _target,
)


def test_no_sealed_boundary_is_ineligible(isolated_db):
    from core.checkpoint_eligibility import checkpoint_eligibility

    connection, thread_row = _checkpointed_preview_connection(isolated_db)
    connection.execute("DELETE FROM messages")
    connection.execute("DELETE FROM checkpoints")
    connection.commit()

    result = checkpoint_eligibility(
        "preview-checkpoint", "steve", db=connection, thread_row=thread_row,
    )

    assert result.eligible is False
    assert result.reason_key == "no_sealed_boundary"
    connection.close()


def test_no_new_history_since_the_installed_checkpoint_is_ineligible(isolated_db, monkeypatch):
    from core import checkpoint_eligibility as module
    import llm.resolution

    connection, thread_row = _checkpointed_preview_connection(isolated_db)
    monkeypatch.setattr(llm.resolution, "resolve_execution_target", lambda *_a, **_k: _target())

    result = module.checkpoint_eligibility(
        "preview-checkpoint", "steve", db=connection, thread_row=thread_row,
    )

    assert result.eligible is False
    assert result.reason_key == "no_new_history"
    connection.close()


def test_unavailable_model_is_ineligible(isolated_db, monkeypatch):
    from core import checkpoint_eligibility as module
    from dataclasses import replace
    import llm.resolution

    connection, thread_row = _checkpointed_preview_connection(isolated_db)
    unavailable = replace(_target(), available=False)
    monkeypatch.setattr(llm.resolution, "resolve_execution_target", lambda *_a, **_k: unavailable)

    result = module.checkpoint_eligibility(
        "preview-checkpoint", "steve", db=connection, thread_row=thread_row,
    )

    assert result.eligible is False
    assert result.reason_key == "model_unavailable"
    connection.close()


def test_new_sealed_history_past_the_checkpoint_is_eligible(isolated_db, monkeypatch):
    from core import checkpoint_eligibility as module
    import llm.resolution

    connection, thread_row = _checkpointed_preview_connection(isolated_db)
    connection.execute(
        "UPDATE active_turns SET status='completed', ended_at='t' WHERE id='turn-live'"
    )
    connection.commit()
    monkeypatch.setattr(llm.resolution, "resolve_execution_target", lambda *_a, **_k: _target())

    result = module.checkpoint_eligibility(
        "preview-checkpoint", "steve", db=connection, thread_row=thread_row,
    )

    assert result.eligible is True
    assert result.reason_key is None
    connection.close()


@pytest.mark.parametrize(
    ("scenario", "reason_key"),
    [
        ("running_only", "no_new_history"),
        ("completed_after_checkpoint", None),
        ("no_messages_after_checkpoint", "no_new_history"),
        ("model_unavailable", "model_unavailable"),
    ],
)
def test_shared_projection_matches_canonical_eligibility(
    isolated_db, monkeypatch, scenario, reason_key,
):
    from core import checkpoint_eligibility as module
    from core.checkpoint_resume import installed_replay_with_exact_seal
    from history.projection import render_portable_history
    from infra.checkpoint_queries import current_installed
    import llm.resolution

    connection, thread_row = _checkpointed_preview_connection(isolated_db)
    if scenario in {"completed_after_checkpoint", "model_unavailable"}:
        connection.execute(
            "UPDATE active_turns SET status='completed', ended_at='t' WHERE id='turn-live'"
        )
    elif scenario == "no_messages_after_checkpoint":
        connection.execute("DELETE FROM messages WHERE seq > 2")
        connection.execute("DELETE FROM active_turns WHERE id='turn-live'")
    connection.commit()
    target = replace(_target(), available=False) if scenario == "model_unavailable" else _target()
    monkeypatch.setattr(llm.resolution, "resolve_execution_target", lambda *_a, **_k: target)
    previous = current_installed("preview-checkpoint", db=connection)
    selection, exact_seal = installed_replay_with_exact_seal(previous, db=connection)
    portable = render_portable_history(
        "preview-checkpoint", db=connection, start_exclusive=selection.checkpoint_boundary,
    )
    shared = module.CheckpointEligibilityProjection(
        start_exclusive=selection.checkpoint_boundary, sealed_boundary=exact_seal,
    )

    assert exact_seal == portable.sealed_boundary
    canonical = module.checkpoint_eligibility(
        "preview-checkpoint", "steve", db=connection, thread_row=thread_row,
    )
    def re_rendered(*_args, **_kwargs):
        raise AssertionError("re-rendered")

    monkeypatch.setattr(module, "render_portable_history", re_rendered)
    reused = module.checkpoint_eligibility(
        "preview-checkpoint", "steve", db=connection, thread_row=thread_row,
        rendered_projection=shared,
    )

    assert (canonical.eligible, canonical.reason_key) == (reused.eligible, reused.reason_key)
    assert (reused.eligible, reused.reason_key) == (reason_key is None, reason_key)
    connection.close()


def test_unowned_thread_returns_none(monkeypatch):
    from core import checkpoint_eligibility as module
    import infra.queries

    monkeypatch.setattr(infra.queries, "get_thread_for_user", lambda *_a, **_k: None)

    result = module.checkpoint_eligibility("missing-thread", "steve", db=None)

    assert result is None
