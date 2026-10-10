"""Issue #1617 — one owner for a Claude Code thread's subagent role.

``threads.spawned_by`` is the single source of truth for subagent-ness
(``infra/execution_class.is_subagent``). ``threads.parent_thread_id`` is a
different fact: the lineage of a *visible* thread, which
``api/delegation.create_thread`` writes instead of ``spawned_by`` precisely
because ``spawned_by`` hides a thread from the sidebar. Two of the three
subagent creation paths therefore leave it NULL by design.

The runtime used to answer the role question from that lineage column, so a
delegated child whose live handle was rebuilt by a relaunch reported
``is_subagent=False`` to the checkpoint-replacement path while its prepared
destination said ``True``. ``cc_startup_context`` rejected the disagreement and
every subsequent input to the thread died. The same NULL also silenced the
child's parent broadcasts.

Both tests seed a child exactly as ``delegate_create_thread`` does —
``spawned_by`` set, ``parent_thread_id`` NULL — and build its live handle
through the relaunch path (``cc_startup_process._new_live``) rather than a
spawn, because only the spawn request carries the parent id explicitly.
"""

from __future__ import annotations

import pytest

from cc_runtime_fixtures import (  # noqa: F401
    _ready_count,
    _seed_parent,
    _wait,
    cc_target,
    child_db,
    fake_claude,
    feed_with_persisted_input,
    prepared_cc_input,
)

_CHILD_ID = "cc-child-null-lineage"
_TURN_ID = "22222222-3333-4444-8555-666666666666"


def _seed_delegate_created_child(db, *, child_id: str = _CHILD_ID) -> None:
    """Insert the exact row shape ``delegate_create_thread`` produces.

    ``spawned_by`` marks the hidden delegated child; ``parent_thread_id`` stays
    NULL because that column records visible-thread lineage, which this thread
    does not have.
    """
    db.execute(
        "INSERT INTO threads (id,user_id,title,model,spawned_by,"
        "created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
        (child_id, "steve", "Worker", "claude-code/sonnet", "parent", "t", "t"),
    )
    db.commit()
    row = db.execute(
        "SELECT spawned_by, parent_thread_id FROM threads WHERE id=?",
        (child_id,),
    ).fetchone()
    assert row["spawned_by"] == "parent" and row["parent_thread_id"] is None


@pytest.fixture
def cc_binary(monkeypatch, fake_claude):  # noqa: F811
    """Resolve every CC launch in this module to the fake provider."""
    import connections.models as cm

    monkeypatch.setattr(
        cm, "get_connection",
        lambda provider, db=None: {"config": {"binary": fake_claude}},
    )
    return fake_claude


@pytest.mark.asyncio
async def test_relaunched_subagent_survives_initial_boundary_replacement(
    child_db, cc_binary, monkeypatch, tmp_path,  # noqa: F811
):
    """A checkpoint replacement keeps the role its caller already supplied.

    ``cc_feed.feed`` passes the authoritative ``is_subagent=True`` down. The
    initial-boundary replacement must carry it into the relaunch instead of
    recomputing it from the live handle's lineage field, or
    ``cc_startup_context`` rejects the launch and the thread can never be fed
    again.
    """
    import subagents.cc_runtime as rt
    from subagents import harness_checkpoint_resume

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude"))
    _seed_parent(child_db)
    _seed_delegate_created_child(child_db)
    target = cc_target("sonnet")

    async def refresh_prepared_context(_selection=None):
        return await prepared_cc_input(
            _CHILD_ID, "work", target, preadmitted_turn_id=_TURN_ID,
        )

    async def adopt_installed_checkpoint(
        _live, thread_id, _harness, _prepared, _refresh,
    ):
        """Force the installed-checkpoint branch this input must survive.

        Which generation is adopted is the checkpoint subsystem's decision and
        is covered elsewhere; this test owns what the replacement does with the
        thread's role once that decision is taken.
        """
        return await prepared_cc_input(
            thread_id, "work", target,
            preadmitted_turn_id=_TURN_ID, allow_provider_resume=False,
        )

    monkeypatch.setattr(
        harness_checkpoint_resume,
        "maintain_external_initial_boundary",
        adopt_installed_checkpoint,
    )

    identity = await feed_with_persisted_input(
        _CHILD_ID, "work", target=target, turn_id=_TURN_ID,
        refresh_prepared_context=refresh_prepared_context,
    )

    assert identity is not None
    live = rt._LIVE.get(_CHILD_ID)
    assert live is not None
    # The replacement process is the one now bound to the thread, and it was
    # launched as a subagent.
    assert live.parent_thread_id == "parent"
    await rt.retire(_CHILD_ID)


@pytest.mark.asyncio
async def test_relaunched_subagent_still_reaches_its_parent(
    child_db, cc_binary, monkeypatch, tmp_path,  # noqa: F811
):
    """A relaunched child's status still addresses its parent.

    ``_Live.parent_thread_id`` exists to address the parent for broadcasts.
    Built from the lineage column it was NULL for a ``delegate_create_thread``
    child, so ``cc_event_result`` and ``cc_handle_close`` skipped every
    ``subagent_status`` broadcast silently.
    """
    import subagents.cc_runtime as rt

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude"))
    _seed_parent(child_db)
    _seed_delegate_created_child(child_db)
    casts: list[tuple[str, dict]] = []

    identity = await feed_with_persisted_input(
        _CHILD_ID, "work", target=cc_target("sonnet"), turn_id=_TURN_ID,
        broadcast=lambda sid, message: casts.append((sid, message)),
    )

    assert identity is not None
    assert rt._LIVE.get(_CHILD_ID).parent_thread_id == "parent"
    assert await _wait(lambda: _ready_count(casts) >= 1), (
        "relaunched subagent never broadcast subagent_status to its parent"
    )
    await rt.retire(_CHILD_ID, broadcast=lambda sid, m: casts.append((sid, m)))
    assert any(
        sid == "parent" and m["type"] == "subagent_status"
        and m["status"] == "closed"
        for sid, m in casts
    )
