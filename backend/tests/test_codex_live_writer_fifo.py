"""An input claimed while a Codex writer still owns its thread stays queued.

The writer's Turn has committed its terminal but its live handle is not yet
released (``release_and_drive_terminal`` has not run) when a new input claims
the idle thread.  The runtime refuses the dispatch with ``CodexLiveTurnBusy``;
that refusal means "the input remains in the message FIFO", so the input must
still be queued, in order, and be delivered by the writer's terminal release.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from codex_runtime_fixtures import (
    _active_live,
    _codex_target,
    _make_mock_app_session,
    _reset_runtime,
)

THREAD = "codex-writer-owned"


@pytest.fixture(autouse=True)
def _isolated_runtime():
    _reset_runtime()
    yield
    _reset_runtime()


def _message(message_id: str) -> dict:
    from infra.database import get_db

    db = get_db()
    try:
        return dict(db.execute(
            "SELECT delivery_state,write_outcome,active_turn_id FROM messages WHERE id=?",
            (message_id,),
        ).fetchone())
    finally:
        db.close()


def _claim(message_id: str, turn_id: str):
    from core.turn_core import BOOT_EPOCH
    from infra.thread_driver import admit_input
    from infra.time import utc_now_iso

    admission = admit_input(
        THREAD, role="user", content=message_id, intent="apply_now",
        message_id=message_id, turn_id=turn_id, boot_epoch=BOOT_EPOCH,
        admitted_at=utc_now_iso(),
    )
    return admission.batch


@pytest.mark.asyncio
async def test_input_claimed_under_a_live_writer_is_delivered_after_its_release(
    isolated_db,
) -> None:
    from core.turn_core import commit_turn_terminal
    from infra.database import get_db
    from infra.thread_execution import execute_claimed_batch
    from subagents.codex_runtime_errors import CodexLiveTurnBusy
    from subagents.codex_runtime_terminal import release_and_drive_terminal

    session = _make_mock_app_session("provider-writer")
    session.steer = AsyncMock(return_value=False)
    live = _active_live(THREAD, session, pending_tool=False)
    settlement = await commit_turn_terminal(live.corsia_turn.turn_id, "completed")
    assert settlement.successor is None  # nothing was queued at the terminal

    batch = _claim("input-after-terminal", "turn-successor")
    assert batch is not None  # the thread is idle, so the claim succeeds
    db = get_db()
    try:
        thread_row = dict(db.execute("SELECT * FROM threads WHERE id=?", (THREAD,)).fetchone())
    finally:
        db.close()
    try:
        await execute_claimed_batch(
            batch, thread_row=thread_row, target=_codex_target(),
            user={"id": "u-test"},
        )
    except CodexLiveTurnBusy:
        pass

    # The writer still owns the thread, so the input waits in the FIFO.
    assert _message("input-after-terminal")["delivery_state"] == "queued"
    session.start_turn.assert_not_awaited()

    # The writer's terminal release delivers it.
    fresh = _make_mock_app_session("provider-writer")
    conn = {"status": "available", "config": {"binary": "codex", "app_server": True}}
    with (
        patch("connections.models.get_connection", return_value=conn),
        patch("subagents.codex_app_server.CodexAppServerSession", return_value=fresh),
    ):
        await release_and_drive_terminal(
            SimpleNamespace(thread_id=THREAD, live=live), settlement,
        )

    assert _message("input-after-terminal")["delivery_state"] == "delivered"
    assert sum(m.await_count for m in (
        session.start, session.start_turn, fresh.start, fresh.start_turn,
    )) == 1


def _register_writer(turn_id: str = "turn-writer"):
    """A live Codex handle in this process, owned by a Turn other than the refused input's."""
    from subagents import codex_runtime

    live = codex_runtime._LiveCodex(
        codex_thread_id=f"provider-{THREAD}", session=None, parent_thread_id=None,
        name="codex", corsia_turn=SimpleNamespace(turn_id=turn_id), model="gpt-5-codex",
    )
    codex_runtime._LIVE.put(THREAD, live)
    return live


def _refusal(kind: str) -> BaseException:
    from subagents.codex_app_server_errors import CodexAppServerError
    from subagents.codex_runtime_errors import CodexLiveTurnBusy

    if kind == "busy":
        return CodexLiveTurnBusy("Codex still owns the previous Turn")
    message = "thread 1 already has an active writer"
    return CodexAppServerError(message, {"code": -32600, "message": message})


@pytest.mark.parametrize("kind", ["busy", "active_writer"])
def test_live_writer_refusal_holds_the_head_and_later_inputs_wait_behind_it(
    isolated_db, kind,
) -> None:
    from infra.input_writes import begin_batch_write, settle_batch_write
    from infra.time import utc_now_iso
    from infra.thread_inputs import start_idle_turn
    from core.turn_core import BOOT_EPOCH
    from shared_send_fixtures import _insert_session

    _insert_session(THREAD, model="codex/gpt-5-codex", spawned_by=None)
    _register_writer()
    batch = _claim("head", "turn-head")
    assert batch is not None
    begin_batch_write(batch, attempt_id="attempt", started_at=utc_now_iso())
    settle_batch_write("attempt", outcome="proved_no_write", error=_refusal(kind))
    from infra.thread_driver import admit_input

    admit_input(
        THREAD, role="user", content="later", intent="apply_now",
        message_id="later", turn_id="turn-later", boot_epoch=BOOT_EPOCH,
        admitted_at=utc_now_iso(),
    )
    from infra.thread_driver import finish_turn

    settlement = finish_turn(
        "turn-head", outcome="error", ended_at=utc_now_iso(),
        successor_turn_id="turn-z", boot_epoch=BOOT_EPOCH, error=_refusal(kind),
    )
    assert settlement.successor is None  # the held head blocks every successor claim
    assert _message("head")["delivery_state"] == "queued"
    assert start_idle_turn(
        THREAD, turn_id="turn-x", boot_epoch=BOOT_EPOCH, started_at=utc_now_iso(),
    ) is None  # the held head is not skipped
    from infra.live_writer_hold import release_live_writer_holds

    assert release_live_writer_holds(THREAD) is True
    claimed = start_idle_turn(
        THREAD, turn_id="turn-y", boot_epoch=BOOT_EPOCH, started_at=utc_now_iso(),
    )
    ids = [item.message_id for item in claimed.items]
    assert [i for i in ids if i in {"head", "later"}] == ["head", "later"]


def _begin_refused_write(kind: str):
    from infra.input_writes import begin_batch_write, settle_batch_write
    from infra.time import utc_now_iso
    from shared_send_fixtures import _insert_session

    _insert_session(THREAD, model="codex/gpt-5-codex", spawned_by=None)
    batch = _claim("head", "turn-head")
    assert batch is not None
    begin_batch_write(batch, attempt_id="attempt", started_at=utc_now_iso())
    return lambda: settle_batch_write(
        "attempt", outcome="proved_no_write", error=_refusal(kind),
    )


@pytest.mark.parametrize("kind", ["busy", "active_writer"])
def test_refusal_after_the_writer_released_fails_visibly_instead_of_holding(
    isolated_db, kind,
) -> None:
    """Lost wakeup: finalize_live and the release both ran before the hold write."""
    from infra.live_writer_hold import release_live_writer_holds
    from subagents.codex_runtime_state import finalize_live

    settle = _begin_refused_write(kind)
    live = _register_writer()
    finalize_live(THREAD, live)
    assert release_live_writer_holds(THREAD) is False  # matched no row yet
    settle()
    assert _message("head")["delivery_state"] == "failed"


def test_active_writer_refusal_this_process_does_not_own_fails_visibly(isolated_db) -> None:
    """A -32600 from another backend's writer: no local terminal will ever fire."""
    settle = _begin_refused_write("active_writer")
    settle()
    assert _message("head")["delivery_state"] == "failed"


def test_refusal_while_only_the_refused_input_owns_the_handle_fails_visibly(isolated_db) -> None:
    """The successor's own live handle is not a prior writer that will release the hold."""
    settle = _begin_refused_write("active_writer")
    _register_writer("turn-head")
    settle()
    assert _message("head")["delivery_state"] == "failed"


def test_boot_recovery_releases_a_live_writer_hold_and_delivers_the_input(isolated_db) -> None:
    from infra.database import get_db
    from infra.thread_recovery import recover_threads
    from subagents import codex_runtime

    settle = _begin_refused_write("busy")
    _register_writer()
    settle()
    assert _message("head")["delivery_state"] == "queued"
    codex_runtime._LIVE.pop(THREAD, None)  # a restart leaves no live handle behind

    db = get_db()
    try:
        recovered = recover_threads(
            boot_epoch="new-boot", recovered_at="2026-10-14T20:00:00+00:00",
            successor_id=lambda _thread_id: "turn-boot", db=db,
        )
    finally:
        db.close()

    claims = [s.successor for s in recovered.settlements if s.successor] + list(recovered.batches)
    assert [i.message_id for c in claims for i in c.items if i.message_id == "head"] == ["head"]
