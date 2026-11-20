"""A required fold refused mid-call by a usage limit holds its inputs; the fold runs first after the reset.

Composed through the real path: the boundary starts the fold, the real
checkpoint runtime launches the real Claude Code or Codex adapter, capture
fails the attempt, the boundary reads the recorded limit, and the Turn's
terminal settles the inputs. Only the provider process is substituted.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.checkpoint_resume import InstalledContinuation, ProviderBoundary, _required_checkpoint_continuation
from core.checkpoint_types import CheckpointRequest, CheckpointUnavailable
from core.checkpoint_wait import CheckpointWaitScope
from core.checkpoint_warm_request import CheckpointAdmission
from infra.database import get_db
from native_checkpoint_fixtures import cold_source, seed_vendor_checkpoint_thread
from test_checkpoint_limit_frames import _assistant, _result, limit_frame, use_cc_frame
from test_checkpoint_provider_adapters import _target

pytestmark = pytest.mark.usefixtures("signed_in_defaults")
DISCOVERED_MODELS = {"claude-code": ("catalog-claude-code",), "codex": ("catalog-codex",)}
THREAD, TURN = "thread-1", "thread-1-turn"
PRIOR = "Prior decisions and unfinished work.\n"


@pytest.fixture(autouse=True)
async def _clean_runtime(monkeypatch):
    from core import checkpoint_runtime, fork_runtime

    async def no_wake(_record):
        return None  # the redrive after completion is the Turn driver's, not under test here

    monkeypatch.setattr(checkpoint_runtime, "_notify_completion", no_wake)
    yield
    await fork_runtime.shutdown_forks()
    await checkpoint_runtime.shutdown_checkpoint_wakes()


def _seed(harness: str):
    """A thread on ``harness`` with an installed checkpoint and one admitted, unwritten input."""
    from infra import checkpoints as store
    from infra.thread_driver import admit_input

    seed_vendor_checkpoint_thread([_target(harness)])
    db = get_db()
    try:
        db.execute("UPDATE threads SET history_epoch=0,next_message_seq=4 WHERE id=?", (THREAD,))
        db.execute("UPDATE models SET context_window=262144 WHERE id=?", (f"catalog-{harness}",))
        for seq in range(1, 4):
            db.execute(
                "INSERT INTO messages (id,thread_id,role,content,created_at,seq) VALUES (?,?,?,'body','t0',?)",
                (f"{THREAD}-m{seq}", THREAD, "user" if seq == 1 else "assistant", seq),
            )
        db.commit()
    finally:
        db.close()
    store.create_running(replace(_request(harness, "prior"), through_seq=2, through_message_id=f"{THREAD}-m2"))
    store.capture("prior", raw_result=PRIOR, usage_json=None, captured_at="t2")
    prior = store.install("prior", installed_at="t3")
    db = get_db()
    try:
        admission = admit_input(
            THREAD, role="user", content="answer me", intent="apply_now",
            message_id=f"{THREAD}-input", turn_id=TURN, boot_epoch="boot", admitted_at="t4", db=db,
        )
        assert admission.batch is not None
    finally:
        db.close()
    return prior


def _finish(error):
    """End the Turn with the boundary's error, as its driver does; return its terminal and the input's state."""
    from infra.thread_driver import finish_turn

    db = get_db()
    try:
        terminal = finish_turn(TURN, outcome="error", ended_at="t6", successor_turn_id="next",
                               boot_epoch="boot", error=error, db=db)
        row = db.execute(
            "SELECT delivery_state,write_outcome FROM messages WHERE id=?", (f"{THREAD}-input",),
        ).fetchone()
        return terminal, (row["delivery_state"], row["write_outcome"])
    finally:
        db.close()


def _request(harness: str, checkpoint_id: str, previous: str | None = None,
             first_included_seq: int | None = 1) -> CheckpointRequest:
    """A cold row; a fold admitted at a boundary leaves its range for the writer to record, as production does."""
    model = f"catalog-{harness}"
    return CheckpointRequest(
        id=checkpoint_id, thread_id=THREAD, history_epoch=0, previous_generation_id=previous,
        through_seq=3, through_message_id=f"{THREAD}-m3", trigger="pressure", mode="cold",
        source_harness=harness, source_model=model, writer_harness=harness, writer_model=model,
        originating_turn_id=TURN, first_included_seq=first_included_seq, started_at="t5",
    )


def _boundary(harness: str, prior, window: int, prepared: list[str]) -> ProviderBoundary:
    from core.checkpoint_types import CheckpointProviderForkRequest

    async def prepare(checkpoint_id: str, required: bool):
        prepared.append(checkpoint_id)
        provider = CheckpointProviderForkRequest(
            fork_id=checkpoint_id, thread_id=THREAD, target=_target(harness),
            instruction="Write the current checkpoint.", reasoning_effort="high",
            working_dir="/work/project", cold_source=cold_source(),
        )
        return CheckpointAdmission(_request(harness, checkpoint_id, prior.id, first_included_seq=None), provider)

    return ProviderBoundary(
        thread_id=THREAD, turn=SimpleNamespace(turn_id=TURN, turn_order=1),
        context_window=window, canonical_input_tokens=window + 1, provider_input_tokens=window + 1,
        minimum_input_tokens=1, adopted_checkpoint_id=None, active_checkpoint=None,
        installed_checkpoint=prior, prepare_checkpoint=prepare,
        # The installed checkpoint plus tail no longer fits: the Turn must fold first.
        render_installed=AsyncMock(return_value=InstalledContinuation(window + 1, None)),
        wait_scope=CheckpointWaitScope(),
    )


def _window(harness: str) -> int:
    """The configured window settlement re-resolves, so a refusal is never mistaken for a cap change."""
    from llm.context_window import resolve_window_for_row
    from llm.resolution import _target_row

    db = get_db()
    try:
        # A harness row with no discovered window resolves none; settlement then compares no window.
        return resolve_window_for_row(_target_row(f"catalog-{harness}", db=db), db=db) or 262_144
    finally:
        db.close()


def _use_codex_refusal(monkeypatch, error: dict) -> None:
    """Substitute the Codex app-server process: it reports the turn error as the real server does."""
    from subagents.codex_app_server_events import failed_event
    from subagents.codex_usage_observer import observe_turn_error

    class Session:
        def __init__(self, **kwargs):
            self.on_event = kwargs["on_event"]
            self.on_exit = kwargs["on_exit"]
            self.active_turn_id = None

        async def start(self):
            await self.on_event({"kind": "text_delta", "text": "Partial narrative line\n"})
            await observe_turn_error(self, error)
            await self.on_event(failed_event(error))
            return True

        async def terminate(self):
            return None

    monkeypatch.setattr("subagents.codex_app_server.CodexAppServerSession", Session)


CODEX_LIMIT = {"message": "You've hit your usage limit. Try again later.", "codexErrorInfo": "usageLimitExceeded"}
CODEX_OVERLOAD = {"message": "The model is overloaded.", "codexErrorInfo": "serverOverloaded"}


def _refuse(monkeypatch, harness: str, *, limit: bool) -> None:
    if harness == "codex":
        _use_codex_refusal(monkeypatch, CODEX_LIMIT if limit else CODEX_OVERLOAD)
    elif limit:
        use_cc_frame(monkeypatch, limit_frame("synthetic_and_error", "after",
                                              prefix=_assistant("Partial narrative line\n")))
    else:
        use_cc_frame(monkeypatch, _result("API Error: upstream overloaded", is_error=True))


def _succeed(monkeypatch, harness: str) -> None:
    """The provider answers normally again after the reset."""
    if harness == "claude-code":
        use_cc_frame(monkeypatch, _assistant("Fresh narrative\n") + _result("Fresh narrative\n", is_error=False),
                     returncode=0)
        return

    class Session:
        def __init__(self, **kwargs):
            self.on_event = kwargs["on_event"]
            self.active_turn_id = None

        async def start(self):
            await self.on_event({"kind": "assistant_text", "text": "Fresh narrative\n"})
            await self.on_event({"kind": "result"})
            return True

        async def terminate(self):
            return None

    monkeypatch.setattr("subagents.codex_app_server.CodexAppServerSession", Session)


def _reset(harness: str) -> None:
    from connections import harness_usage
    from infra.database import write_transaction
    from vendor_account_fixtures import default_receipt

    db = get_db()
    try:
        with write_transaction(db, scope="test_reset"):
            receipt = default_receipt(harness, db=db)
            if harness == "claude-code":
                # What the provider reports once the window has rolled over.
                from connections.claude_usage import apply_rate_limit_event

                apply_rate_limit_event({"status": "allowed", "rateLimitType": "five_hour", "utilization": 0.0,
                                        "resetsAt": 4102462800}, receipt=receipt, db=db)
            harness_usage.record_allowed(provider=harness, source="test", receipt=receipt, db=db)
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", ["claude-code", "codex"])
async def test_mid_call_limit_holds_inputs_keeps_checkpoint_and_folds_first_after_reset(
    monkeypatch, harness,
):
    from core.checkpoint_resume import BoundaryDecision
    from infra import checkpoint_queries
    from infra.held_admission import limited_admission
    from infra.thread_input_types import HeldAdmission

    prior = await asyncio.to_thread(_seed, harness)
    window = _window(harness)
    prepared: list[str] = []
    _refuse(monkeypatch, harness, limit=True)

    with pytest.raises(CheckpointUnavailable) as raised:
        await _required_checkpoint_continuation(_boundary(harness, prior, window, prepared), None, prior)

    assert raised.value.limited_target == f"catalog-{harness}"
    assert raised.value.limited_connection == harness
    folded = checkpoint_queries.read(prepared[0])
    assert folded.status == "failed" and folded.raw_result is None  # nothing installed
    assert checkpoint_queries.current_installed(THREAD).id == "prior"
    assert checkpoint_queries.read("prior").raw_result == PRIOR  # previous checkpoint unchanged
    terminal, state = await asyncio.to_thread(_finish, raised.value)
    assert terminal.failed_message_ids == ()
    assert state == ("queued", None)  # held, never failed
    assert isinstance(terminal.successor, HeldAdmission)
    assert terminal.successor.connection == harness

    # The reset passes: the next boundary on this account folds before any provider call.
    await asyncio.to_thread(_reset, harness)
    assert limited_admission(THREAD, f"catalog-{harness}") is None
    _succeed(monkeypatch, harness)
    boundary = _boundary(harness, prior, window, prepared)
    boundary = replace(boundary, render_installed=AsyncMock(side_effect=lambda record: InstalledContinuation(
        window + 1 if record.id == "prior" else 1, None)))

    continuation = await _required_checkpoint_continuation(boundary, None, prior)

    assert continuation.decision is BoundaryDecision.ADOPT_AND_PROCEED
    assert continuation.checkpoint_id == prepared[-1] != prepared[0]
    assert checkpoint_queries.current_installed(THREAD).raw_result == "Fresh narrative\n"


def _second_held_thread(thread_id: str) -> None:
    """Another thread on the same account with an admitted input, held by the same limit."""
    from infra.thread_driver import admit_input

    db = get_db()
    try:
        db.execute("INSERT INTO threads(id,user_id,model,created_at,updated_at) "
                   "VALUES (?,'test-user','catalog-claude-code','t','t')", (thread_id,))
        db.commit()
        admit_input(thread_id, role="user", content="work", intent="apply_now",
                    message_id=f"{thread_id}-input", turn_id=f"{thread_id}-turn", boot_epoch="boot",
                    admitted_at="t5", db=db)
    finally:
        db.close()


def _pass_reset(stored_reset: str) -> None:
    """Move the recorded reset into the past, as the clock would."""
    from datetime import datetime, timedelta, timezone

    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    db = get_db()
    try:
        changed = db.execute("UPDATE vendor_accounts SET usage_json=replace(usage_json,?,?) "
                             "WHERE instr(usage_json,?)>0", (stored_reset, past, stored_reset))
        assert changed.rowcount == 1
        db.commit()
    finally:
        db.close()


@pytest.mark.asyncio
async def test_a_reset_starts_one_held_fold_not_every_held_thread_on_the_account(monkeypatch):
    """The fold's hold is a head of the account's scope, so the release drive probes one thread only."""
    from datetime import datetime, timezone

    from infra import connection_limit_drive as drive
    from infra.held_admission import limited_admission

    prior = await asyncio.to_thread(_seed, "claude-code")
    _refuse(monkeypatch, "claude-code", limit=True)
    with pytest.raises(CheckpointUnavailable) as raised:
        await _required_checkpoint_continuation(
            _boundary("claude-code", prior, _window("claude-code"), []), None, prior)
    _terminal, state = await asyncio.to_thread(_finish, raised.value)
    assert state == ("queued", None)  # the fold's input is a held head, not a failed one
    await asyncio.to_thread(_second_held_thread, "thread-2")
    held = limited_admission("thread-2", "catalog-claude-code")
    assert held is not None and held.resets_at is not None
    await asyncio.to_thread(_pass_reset, held.resets_at)
    started_turns = AsyncMock(return_value=SimpleNamespace(started=True))
    monkeypatch.setattr("infra.turn_admission_driver.drive_turn_admission", started_turns)

    due = await asyncio.to_thread(drive._due_releases, datetime.now(timezone.utc))
    outcomes = [await drive.drive_scope_release(item) for item in due]

    started = [outcome for outcome in outcomes if isinstance(outcome, drive.Started)]
    assert [outcome.thread_id for outcome in started] == [THREAD]  # the oldest held head: the fold's thread
    assert started_turns.await_count == 1
    db = get_db()
    try:
        row = db.execute("SELECT delivery_state FROM messages WHERE id='thread-2-input'").fetchone()
    finally:
        db.close()
    assert row["delivery_state"] == "queued"  # released only after the probe proves the account answers


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", ["claude-code", "codex"])
async def test_mid_call_non_limit_error_still_fails_the_input_visibly(monkeypatch, harness):
    """The control: the hold keys on the recorded limit, not on any failed fold."""
    prior = await asyncio.to_thread(_seed, harness)
    _refuse(monkeypatch, harness, limit=False)

    with pytest.raises(CheckpointUnavailable) as raised:
        await _required_checkpoint_continuation(
            _boundary(harness, prior, _window(harness), []), None, prior)

    assert raised.value.limited_target is None
    _terminal, state = await asyncio.to_thread(_finish, raised.value)
    assert state[0] == "failed"


@pytest.mark.asyncio
async def test_unrecorded_mid_call_limit_fails_visibly_instead_of_holding(monkeypatch):
    """With no verdict nothing would ever release a hold, so the input fails visibly."""
    import sqlite3

    from connections import harness_usage

    prior = await asyncio.to_thread(_seed, "claude-code")
    _refuse(monkeypatch, "claude-code", limit=True)

    def broken_store(**_kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(harness_usage, "record_usage", broken_store)
    with pytest.raises(CheckpointUnavailable) as raised:
        await _required_checkpoint_continuation(
            _boundary("claude-code", prior, _window("claude-code"), []), None, prior)

    assert raised.value.limited_target is None
    assert "usage-limit verdict not recorded" in str(raised.value)
    _terminal, state = await asyncio.to_thread(_finish, raised.value)
    assert state[0] == "failed"
