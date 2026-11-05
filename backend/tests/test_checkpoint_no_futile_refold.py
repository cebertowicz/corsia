"""An optional fold stays off while the newest install landed at the launch point.

The fork and the live Turn share one seat, so the tail the Turn adds during an
optional fold can leave its install at or above the launch point; the next
boundary then launched another fold at once, back to back. The install's size
at first adoption is kept on the checkpoint row, and an optional fold is futile
while it sits at or above the launch point. The required path is unchanged.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from core import checkpoint_resume
from core.checkpoint_resume import (
    CheckpointWaitScope, InstalledContinuation, ProviderBoundary, maintain_before_provider_call,
)
from core.fork_fit import BoundaryDecision, ProviderPayload, evaluate_boundary
from core.fork_pressure import soft_pressure_remaining
from core.turn_core import TurnIdentity

WINDOW = 41_681
LAUNCH = WINDOW - soft_pressure_remaining(WINDOW)  # 31,261 on the measured 32 GB seat


def _payload(**changes) -> ProviderPayload:
    base = ProviderPayload(
        context_window=WINDOW, canonical_input_tokens=LAUNCH + 200, input_tokens=LAUNCH + 200,
        minimum_tokens=5_000, checkpoint_status=None, installed_input_tokens=None,
        checkpoint_admissible=True, fixed_tokens=2_000,
    )
    return replace(base, **changes)


def test_launch_point_is_the_measured_one():
    assert LAUNCH == 31_261


def test_install_at_or_above_launch_turns_the_optional_fold_off():
    assert evaluate_boundary(_payload(adopted_input_tokens=31_562)) is BoundaryDecision.PROCEED
    assert evaluate_boundary(_payload(adopted_input_tokens=LAUNCH)) is BoundaryDecision.PROCEED


def test_install_below_launch_leaves_the_optional_fold_on():
    assert evaluate_boundary(_payload(adopted_input_tokens=LAUNCH - 1_000)) is BoundaryDecision.PROCEED_AND_FOLD


def test_no_installed_checkpoint_or_unknown_size_is_unchanged():
    assert evaluate_boundary(_payload()) is BoundaryDecision.PROCEED_AND_FOLD
    assert evaluate_boundary(_payload(adopted_input_tokens=None)) is BoundaryDecision.PROCEED_AND_FOLD


def test_the_window_is_untouched_by_a_high_install():
    over = _payload(adopted_input_tokens=31_562, input_tokens=WINDOW + 1, canonical_input_tokens=WINDOW + 1)
    assert evaluate_boundary(over) is BoundaryDecision.WAIT
    assert evaluate_boundary(replace(over, installed_input_tokens=WINDOW - 100)) is BoundaryDecision.ADOPT_AND_PROCEED


class _Thread:
    """One thread's checkpoint state and the boundaries a Turn reaches on it."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.records: dict[str, SimpleNamespace] = {}
        self.installed: SimpleNamespace | None = None
        self.optional_starts = 0
        self.required_starts = 0
        self.next_install_tokens = 0
        self.count = 0
        monkeypatch.setattr(checkpoint_resume, "_start_checkpoint", self._start)
        monkeypatch.setattr(checkpoint_resume, "await_checkpoint_or_stop", self._await)
        import infra.checkpoint_adoption as checkpoint_adoption

        monkeypatch.setattr(checkpoint_adoption, "record_adopted_input_tokens", self._adopt)

    def _record(self, tokens: int) -> SimpleNamespace:
        self.count += 1
        record = SimpleNamespace(
            id=f"c{self.count}", status="installed", error=None, originating_turn_id="turn",
            adopted_input_tokens=None, render_tokens=tokens,
        )
        self.records[record.id] = record
        return record

    async def _start(self, boundary, *, required):
        if required:
            self.required_starts += 1
        else:
            self.optional_starts += 1
        return self._record(self.next_install_tokens)

    async def _await(self, _scope, checkpoint_id, _await, on_stop=None):
        self.installed = self.records[checkpoint_id]
        return self.installed

    def _adopt(self, checkpoint_id, tokens, *, db=None):
        record = self.records[checkpoint_id]
        if record.adopted_input_tokens is None:
            record.adopted_input_tokens = tokens

    def install_optional(self, tokens: int) -> None:
        """A background fold installed; its first render at adoption measured ``tokens``."""
        self.installed = self._record(tokens)

    async def boundary(self, current: int, *, eager: bool = False):
        async def render(record):
            return InstalledContinuation(record.render_tokens, object())

        async def prepare(*_args):
            raise AssertionError("_start_checkpoint is replaced")

        installed = self.installed
        return await maintain_before_provider_call(ProviderBoundary(
            thread_id="t", turn=TurnIdentity("turn", 0), context_window=WINDOW,
            canonical_input_tokens=current, provider_input_tokens=current,
            minimum_input_tokens=5_000, adopted_checkpoint_id=None, active_checkpoint=None,
            installed_checkpoint=installed, prepare_checkpoint=prepare, render_installed=render,
            wait_scope=CheckpointWaitScope(),
            eager_checkpoint_id=installed.id if eager and installed is not None else None,
            fixed_input_tokens=2_000,
        ))


@pytest.mark.asyncio
async def test_high_install_then_window_then_required_runs_unchanged(monkeypatch):
    thread = _Thread(monkeypatch)
    # An optional fold installs at carried + summary + tail = 31,562, above launch; the
    # next boundary adopts it eagerly and the size is kept.
    thread.install_optional(31_562)
    adopted = await thread.boundary(31_562, eager=True)
    assert adopted.decision is BoundaryDecision.ADOPT_AND_PROCEED
    assert thread.installed.adopted_input_tokens == 31_562

    # 1: under pressure, below the window: no optional launch.
    held = await thread.boundary(LAUNCH + 800)
    assert held.decision is BoundaryDecision.PROCEED and held.checkpoint_id is None
    assert (thread.optional_starts, thread.required_starts) == (0, 0)

    # 2: the Turn reaches the window; the installed render is over it, so the required
    # fold runs and is awaited, exactly as before.
    thread.installed.render_tokens = WINDOW + 500
    thread.next_install_tokens = LAUNCH + 150  # 3: it lands above launch, below the window
    required = await thread.boundary(WINDOW + 200)
    assert required.decision is BoundaryDecision.ADOPT_AND_PROCEED
    assert (thread.optional_starts, thread.required_starts) == (0, 1)
    assert thread.installed.adopted_input_tokens == LAUNCH + 150

    # 3: optional folds stay off; the Turn proceeds between required folds (no stall) ...
    for current in (LAUNCH + 400, LAUNCH + 2_000, WINDOW - 100):
        step = await thread.boundary(current)
        assert step.decision is BoundaryDecision.PROCEED and step.checkpoint_id is None
    assert (thread.optional_starts, thread.required_starts) == (0, 1)

    # ... and the next window hit runs one more required fold (no loop, no repeat).
    thread.installed.render_tokens = WINDOW + 300
    thread.next_install_tokens = LAUNCH + 90
    again = await thread.boundary(WINDOW + 50)
    assert again.decision is BoundaryDecision.ADOPT_AND_PROCEED
    assert (thread.optional_starts, thread.required_starts) == (0, 2)


@pytest.mark.asyncio
async def test_a_below_launch_install_rearms_optional_folds(monkeypatch):
    thread = _Thread(monkeypatch)
    thread.install_optional(31_562)
    await thread.boundary(31_562, eager=True)
    assert (await thread.boundary(LAUNCH + 800)).decision is BoundaryDecision.PROCEED

    thread.installed.render_tokens = WINDOW + 500
    thread.next_install_tokens = LAUNCH - 9_000  # a required fold that lands well below launch
    await thread.boundary(WINDOW + 200)
    assert thread.installed.adopted_input_tokens == LAUNCH - 9_000

    # Next time the Turn is under pressure the optional fold launches again.
    launched = await thread.boundary(LAUNCH + 50)
    assert launched.decision is BoundaryDecision.PROCEED_AND_FOLD
    assert (thread.optional_starts, thread.required_starts) == (1, 1)


@pytest.mark.asyncio
async def test_no_installed_checkpoint_still_launches_an_optional_fold(monkeypatch):
    thread = _Thread(monkeypatch)
    launched = await thread.boundary(LAUNCH + 50)
    assert launched.decision is BoundaryDecision.PROCEED_AND_FOLD
    assert (thread.optional_starts, thread.required_starts) == (1, 0)


def _insert_installed(checkpoint_id: str, thread_id: str = "t1") -> None:
    from infra.database import get_db

    db = get_db()
    db.execute(
        "INSERT OR IGNORE INTO threads (id,user_id,model,created_at,updated_at) VALUES (?,'u','m','t','t')",
        (thread_id,),
    )
    db.execute(
        "INSERT INTO checkpoints (id,thread_id,history_epoch,through_seq,through_message_id,trigger,"
        " mode,status,source_harness,source_model,writer_harness,writer_model,"
        " continuity_base_revision,continuity_base_snapshot_json,started_at,raw_result,"
        " continuity_snapshot_json,captured_at,installed_at) "
        "VALUES (?,?,0,1,'m1','pressure','warm','installed','native','m','native','m',0,'[]','t','x','[]','t','t')",
        (checkpoint_id, thread_id),
    )
    db.commit()
    db.close()


def test_adopted_size_is_written_once_and_read_back(isolated_db):
    from infra.checkpoint_queries import current_installed
    from infra.checkpoint_adoption import record_adopted_input_tokens

    _insert_installed("c1")
    assert current_installed("t1").adopted_input_tokens is None  # NULL: not known to be over
    record_adopted_input_tokens("c1", 31_562)
    record_adopted_input_tokens("c1", 12_000)  # a later adoption never overwrites the first
    assert current_installed("t1").adopted_input_tokens == 31_562


@pytest.mark.asyncio
async def test_adoption_writes_the_size_through_the_real_writer(isolated_db):
    from infra.checkpoint_queries import current_installed

    _insert_installed("c1")
    record = current_installed("t1")

    async def render(_record):
        return InstalledContinuation(31_562, object())

    boundary = ProviderBoundary(
        thread_id="t1", turn=TurnIdentity("turn", 0), context_window=WINDOW,
        canonical_input_tokens=WINDOW + 10, provider_input_tokens=WINDOW + 10,
        minimum_input_tokens=5_000, adopted_checkpoint_id=None, active_checkpoint=None,
        installed_checkpoint=record, prepare_checkpoint=None, render_installed=render,
        wait_scope=CheckpointWaitScope(), eager_checkpoint_id="c1",
    )
    continuation = await maintain_before_provider_call(boundary)
    assert continuation.decision is BoundaryDecision.ADOPT_AND_PROCEED
    assert current_installed("t1").adopted_input_tokens == 31_562
    assert checkpoint_resume._payload(
        replace(boundary, installed_checkpoint=current_installed("t1")), None, None,
    ).adopted_input_tokens == 31_562
