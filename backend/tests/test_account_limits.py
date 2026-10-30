"""A limit belongs to one account and one scope, and acting on it must cost one Stop.

Pins: limit subjects, releasing and failing and racing a connection limit, plan-limit stop
effects, fold holds, overage and state fixes, and the Codex usage-limit read."""
from __future__ import annotations

import ast
import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

from fastapi import FastAPI
import pytest

from connections import harness_models, harness_usage
from connections.account_policy import select_account
from connections.account_receipts import capture_receipt, limit_subject
from connections.usage_verdicts import LimitSubject, release_due, release_marker, verdicts
from connections.usage_windows import merged_window, window_length, window_stale
from core.checkpoint_types import CheckpointUnavailable
from core.stop_causes import PLAN_LIMIT_REASON, PlanLimitStop
from infra.database import get_db, write_transaction
from infra.connection_limit_release import (
    LIMIT_RELEASE_BACKOFF_SECONDS,
    LIMIT_RELEASE_MAX_ATTEMPTS,
)
from infra.input_admission import accept_thread_input
from infra.plan_limit_continue import CONTINUE_CHIP, continue_message_id, queue_plan_limit_continue
from infra import thread_execution  # noqa: F401  # imported before any patch of the driver, so it binds the real one
from infra.thread_driver import finish_turn
from infra.thread_input_types import FailedInputBatch, HeldAdmission
from infra.thread_inputs import start_idle_turn
from infra.thread_recovery import recover_threads
from vendor_account_fixtures import (  # noqa: F401
    _known_default,
    _ready,
    accounts,
    claude,
    default_id,
    default_receipt,
    default_subject,
    freeze_turn_on_default,
    model_default_id,
    no_stops,
    ready,
    receipt_of,
)


@pytest.fixture(autouse=True)
def _signed_in_defaults(request):
    """A test on a bare database runs on signed-in Defaults: there is no ambient login to stand in."""
    if "isolated_db" in request.fixturenames and "accounts" not in request.fixturenames:
        request.getfixturevalue("signed_in_defaults")


@pytest.fixture
def _no_codex_connection(isolated_db):
    """Each test starts on its own database with no Codex connection."""
    from infra.database import get_db

    db = get_db()
    try:
        db.execute("DELETE FROM connections WHERE provider = 'codex'")
        db.commit()
    finally:
        db.close()


def _seed_codex(**config) -> None:
    from connections.models import ConnectionStatus, ConnectionType, upsert_connection
    from connections.vendor_accounts import default_account
    from vendor_account_fixtures import record_default_identity, signed_in_default
    from infra.database import get_db

    upsert_connection(
        type=ConnectionType.CLI,
        provider="codex",
        status=ConnectionStatus.AVAILABLE,
        config={"binary": "/bin/codex", **config},
    )
    db = get_db()
    record_default_identity("codex", {"account_fp": "synthetic-codex-usage-login"}, db=db)
    signed_in_default("codex", db=db, models=("codex/test",))


def _bind_default(session):
    """Capture the verified Default login before any simulated vendor callback."""
    from connections.account_receipts import capture_receipt
    from connections.vendor_accounts import default_account
    from infra.database import get_db

    db = get_db()
    receipt = capture_receipt("codex", account_id=default_account("codex", db=db)["id"], db=db)
    assert receipt is not None
    session.vendor_account_bound = True
    session.vendor_account_id = receipt.account_id
    session.vendor_account_receipt = receipt
    return session


def _epoch(delta: timedelta) -> int:
    return int((datetime.now(timezone.utc) + delta).timestamp())


def _dispatch_session():
    """A real app-server session whose event sink and transport are stubbed."""
    from unittest.mock import AsyncMock

    from subagents.codex_app_server import CodexAppServerSession

    session = CodexAppServerSession(binary="codex", task=None)
    session._emit = AsyncMock()
    session._tool_requests = None
    session.thread_id = "thread"
    return _bind_default(session)


async def _start(session, turn_id: str) -> None:
    """Start a turn the way CORSIA does: ``turn/start`` returns its id."""
    from unittest.mock import AsyncMock

    session._request = AsyncMock(return_value={"turn": {"id": turn_id}})
    await session.start_turn("go")


def _completed(turn_id: str) -> dict:
    return {"turn": {"id": turn_id, "status": "completed"}}


def _turn(session, turn_id: str, *pushes: dict):
    """Run one owned turn through dispatch: start, the given pushes, completion."""
    handle = session._handle_notification

    async def _run() -> None:
        await _start(session, turn_id)
        await handle("turn/started", {"turn": {"id": turn_id}})
        for push in pushes:
            await handle("account/rateLimits/updated", {"rateLimits": push})
        await handle("turn/completed", _completed(turn_id))

    return _run()


SONNET = "claude-code/sonnet"


def _subject():
    return default_subject("claude-code", "sonnet")


def _held_by_direct_insert(db, *thread_ids: str) -> None:
    for index, thread_id in enumerate(thread_ids):
        db.execute(
            "INSERT INTO threads (id,user_id,model,created_at,updated_at) VALUES (?, 'user', ?, ?, ?)",
            (thread_id, SONNET, thread_id, thread_id),
        )
        db.commit()
        accept_thread_input(
            thread_id, role="user", content="work", intent="apply_now",
            message_id=f"message-{thread_id}", created_at=f"2026-01-01T00:00:{index:02d}+00:00", db=db,
        )


def _expired_limit(db) -> str:
    reset = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    harness_usage.record_limited(
        provider="claude-code", reason="weekly", resets_at=reset, scope_model="sonnet", source="test", db=db, receipt=default_receipt("claude-code", db=db),
    )
    return reset


OPUS = "claude-code/opus"


REFUSAL = "You've hit your weekly limit · resets Oct 9 at 10pm"


def _at(minutes: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()


def _thread_with_running_turn(db, thread_id: str, model: str = SONNET, *, running_since: str | None = None) -> None:
    db.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) VALUES (?, 'user', ?, 't', 't')",
        (thread_id, model),
    )
    if running_since is not None:
        db.execute(
            "INSERT INTO active_turns (id,thread_id,execution_epoch,model,status,started_at,boot_epoch) "
            "VALUES (?,?,0,?,'running',?,'boot')",
            (f"{thread_id}-turn", thread_id, model, running_since),
        )
    db.commit()
    if running_since is not None:
        freeze_turn_on_default(db, f"{thread_id}-turn")


def _queue_next_input(db, thread_id: str) -> str:
    message_id = f"{thread_id}-next"
    accept_thread_input(
        thread_id, role="user", content="work", intent="apply_now",
        message_id=message_id, created_at="2026-01-01T00:00:00+00:00", db=db,
    )
    return message_id


def _message(db, message_id: str) -> tuple[str, str | None]:
    row = db.execute("SELECT delivery_state,delivery_error FROM messages WHERE id=?", (message_id,)).fetchone()
    return row["delivery_state"], row["delivery_error"]


def _running_count_for_thread(db, thread_id: str) -> int:
    return db.execute(
        "SELECT COUNT(*) FROM active_turns WHERE thread_id=? AND status='running'", (thread_id,),
    ).fetchone()[0]


def _limit_claude_default(db, reset: str, *, scope: str = "sonnet") -> None:
    harness_usage.record_limited(
        provider="claude-code", reason="weekly", resets_at=reset, scope_model=scope, source="test", db=db, receipt=default_receipt("claude-code", db=db),
    )


@pytest.fixture
def wakes(monkeypatch):
    import infra.failed_wake_surface as surface

    record = Mock(wraps=surface.record_failed_wakes)
    monkeypatch.setattr(surface, "record_failed_wakes", record)
    return record


def _events(monkeypatch, module: str) -> Mock:
    events = Mock()
    monkeypatch.setattr(f"{module}.emit_product_event", events)
    return events


def _settle_raises(monkeypatch) -> None:
    def fail(*_args, **_kwargs):
        raise RuntimeError("settlement broke")

    monkeypatch.setattr("connections.limit_settlement._settle", fail)


@pytest.fixture
def driven(monkeypatch):
    drive = AsyncMock()
    monkeypatch.setattr("infra.thread_execution.drive_idle_thread", drive)
    return drive


async def _until_called(mock: AsyncMock) -> None:
    for _ in range(200):
        if mock.await_count:
            return
        await asyncio.sleep(0.01)


def _thread(db, thread_id: str, model: str | None = SONNET) -> None:
    db.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) VALUES (?, 'user', ?, ?, ?)",
        (thread_id, model, thread_id, thread_id),
    )
    db.commit()


def _queue(db, thread_id: str, order: int) -> None:
    accept_thread_input(
        thread_id, role="user", content="work", intent="apply_now",
        message_id=f"message-{thread_id}", created_at=f"2026-01-01T00:00:{order:02d}+00:00", db=db,
    )


def _held(db, *thread_ids: str) -> None:
    for index, thread_id in enumerate(thread_ids):
        _thread(db, thread_id)
        _queue(db, thread_id, index)


def _limit(db, reset: str | None, *, scope: str | None = "sonnet", provider: str = "claude-code") -> None:
    harness_usage.record_limited(
        provider=provider, reason="weekly", resets_at=reset, scope_model=scope, source="test", db=db, receipt=default_receipt(provider, db=db),
    )


def _marker(db, key: str = "sonnet") -> Any:
    return verdicts(harness_usage.stored_usage(provider="claude-code", db=db, account_id=default_id("claude-code", db=db)))[key].get("release")


def _due(db, *, later_minutes: float = 0):
    now = datetime.now(timezone.utc) + timedelta(minutes=later_minutes)
    return release_due(
        harness_usage.stored_usage(provider="claude-code", db=db, account_id=default_id("claude-code", db=db)), now,
        account=default_subject("claude-code", "null", db=db), max_attempts=5, backoff_seconds=180,
    )


def _running(db) -> list[str]:
    rows = db.execute("SELECT thread_id FROM active_turns WHERE status='running' ORDER BY turn_order")
    return [row["thread_id"] for row in rows.fetchall()]


@pytest.fixture
def launched(monkeypatch):
    drive = AsyncMock(return_value=SimpleNamespace(started=True))
    monkeypatch.setattr("infra.turn_admission_driver.drive_turn_admission", drive)
    monkeypatch.setattr("infra.thread_execution.drive_turn_admission", drive)
    return drive


_RESET = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()


def _refused_input(db) -> None:
    from infra.thread_driver import admit_input

    db.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) VALUES ('t1','user-1',?,'t0','t0')",
        (SONNET,),
    )
    db.commit()
    admission = admit_input(
        "t1", role="user", content="answer me", intent="apply_now", message_id="t1-input",
        turn_id="t1-turn", boot_epoch="boot", admitted_at="t1", db=db,
    )
    assert admission.batch is not None


def _plan_limit_sonnet(db) -> None:
    harness_usage.record_usage(provider="claude-code", windows=[{
        "vendor": "claude-code", "kind": "seven_day_sonnet", "scope_model": "sonnet",
        "used_percent": 100, "resets_at": _RESET,
        "observed_at": datetime.now(timezone.utc).isoformat(), "source": "rate_limit_event",
    }], db=db, receipt=default_receipt("claude-code", db=db))


def _limited_fold_error(db) -> CheckpointUnavailable:
    from llm.context_window import resolve_window_for_row
    from llm.resolution import _target_row

    return CheckpointUnavailable(
        "Held: account limit on claude-code.",
        context_window=resolve_window_for_row(_target_row(SONNET, db=db), db=db),
        installed_checkpoint_id=None, limited_target=SONNET, limited_connection="claude-code",
    )


def _sonnet_verdict(db) -> dict:
    return verdicts(harness_usage.stored_usage(provider="claude-code", db=db, account_id=default_id("claude-code", db=db)) or {}).get("sonnet") or {}


def _due_after_reset(db) -> list[LimitSubject]:
    """Scopes the sweep drives once the reset passes: the deadline is the verdict's own field."""
    after_reset = datetime.fromisoformat(_RESET) + timedelta(minutes=1)
    return [due.subject for due in release_due(
        harness_usage.stored_usage(provider="claude-code", db=db, account_id=default_id("claude-code", db=db)), after_reset,
        account=default_subject("claude-code", "null", db=db), max_attempts=LIMIT_RELEASE_MAX_ATTEMPTS, backoff_seconds=LIMIT_RELEASE_BACKOFF_SECONDS,
    )]


_BACKEND = Path(__file__).resolve().parents[1]


_HELD = ("limited_with_reset", PLAN_LIMIT_REASON)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _thread_with_model(db, thread_id: str, model: str = "claude-code/sonnet") -> None:
    db.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) VALUES (?, 'user', ?, ?, ?)",
        (thread_id, model, thread_id, thread_id),
    )
    db.commit()


def _queue_named_message(db, thread_id: str, message_id: str) -> None:
    accept_thread_input(
        thread_id, role="user", content="work", intent="apply_now",
        message_id=message_id, created_at=_now().isoformat(), db=db,
    )


def _window(kind: str = "five_hour", *, percent: int = 100, observed: datetime | None = None,
            minutes: int | None = None) -> dict:
    window = {
        "vendor": "claude-code", "kind": kind, "used_percent": percent,
        "resets_at": _RESET,
        "observed_at": (observed or _now()).isoformat(), "source": "rate_limit_event",
        "scope_model": kind.removeprefix("seven_day_") if kind == "seven_day_sonnet" else None,
    }
    if minutes is not None:
        window["window_minutes"] = minutes
    return window


def _set_overage(db, allow: bool) -> None:
    from connections.harness_usage import set_allow_overage

    set_allow_overage("claude-code", default_id("claude-code", db=db), allow, db=db)


def _state(db, model: str = "claude-code/sonnet") -> tuple[str, str | None]:
    availability = harness_usage.connection_availability(model, db=db, account_id=model_default_id(model, db=db))
    return availability.state, availability.reason


def _claimed_turn(db, thread_id: str = "thread", turn_id: str = "turn") -> None:
    _thread_with_model(db, thread_id)
    _queue_named_message(db, thread_id, f"m1-{thread_id}")
    start_idle_turn(thread_id, turn_id=turn_id, boot_epoch="boot", started_at=_now().isoformat(), db=db)


def _finish(db, error) -> object:
    return finish_turn(
        "turn", outcome="cancelled", ended_at=_now().isoformat(),
        successor_turn_id="next", boot_epoch="boot", error=error, db=db,
    )


def _rows(db, thread_id: str = "thread"):
    return {
        row["id"]: (row["delivery_state"], row["active_turn_id"])
        for row in db.execute("SELECT id,delivery_state,active_turn_id FROM messages WHERE thread_id=?", (thread_id,))
    }


def _running_total(db) -> int:
    return db.execute("SELECT COUNT(*) FROM active_turns WHERE status='running'").fetchone()[0]


def _claimed_turn_on(db, thread_id: str, model: str) -> None:
    _thread_with_model(db, thread_id, model)
    _queue_named_message(db, thread_id, f"m1-{thread_id}")
    start_idle_turn(thread_id, turn_id=f"turn-{thread_id}", boot_epoch="boot",
                    started_at=_now().isoformat(), db=db)


@pytest.fixture
def api(isolated_db, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from api import vendor_accounts as api_accounts
    from infra.auth import require_user

    monkeypatch.setattr("infra.state.ws.notify_models_changed", Mock())
    app = FastAPI()
    app.dependency_overrides[require_user] = lambda: {"id": "test"}
    app.include_router(api_accounts.router)
    return TestClient(app)


_HELD_NO_RESET = ("limited_without_reset", PLAN_LIMIT_REASON)


_HELD_RESET = ("limited_with_reset", PLAN_LIMIT_REASON)


def _no_reset(kind: str = "five_hour", *, percent: int = 100, observed: datetime | None = None) -> dict:
    return {**_window(kind, percent=percent, observed=observed), "resets_at": None}


def _codex_no_reset(*, percent: int = 100) -> dict:
    return {
        "vendor": "codex", "kind": "codex_primary", "used_percent": percent, "resets_at": None,
        "window_minutes": 300, "observed_at": _now().isoformat(), "source": "rate_limit_update",
    }


@pytest.fixture
def pushes(monkeypatch):
    pushed = Mock()
    monkeypatch.setattr("infra.state.ws.schedule_models_changed", pushed)
    return pushed


@pytest.fixture
def releases(monkeypatch):
    released = Mock()
    monkeypatch.setattr("infra.connection_limit_drive.schedule_scope_release", released)
    return released


def _sign_in(db) -> None:
    """The account's sign-in: its catalog refresh commits the new login's own usage read."""
    from connections.claude_discovery import _store_catalog_success

    receipt = default_receipt("claude-code", db=db)
    _store_catalog_success(
        {"rate_limits": {"five_hour": {"utilization": 10}}},
        account_id=receipt.account_id, receipt=receipt, db=db,
    )


def _claimed(db, thread_id: str, *, spawned_by: str | None = None) -> None:
    db.execute(
        "INSERT INTO threads (id,user_id,model,spawned_by,created_at,updated_at) "
        "VALUES (?, 'user', 'claude-code/sonnet', ?, ?, ?)",
        (thread_id, spawned_by, thread_id, thread_id),
    )
    db.commit()
    accept_thread_input(
        thread_id, role="user", content="work", intent="apply_now",
        message_id=f"m-{thread_id}", created_at=datetime.now(timezone.utc).isoformat(), db=db,
    )
    start_idle_turn(
        thread_id, turn_id=f"turn-{thread_id}", boot_epoch="boot",
        started_at=datetime.now(timezone.utc).isoformat(), db=db,
    )


def _arm_wake(db, thread_id: str) -> None:
    from automation import plan_liveness
    from plans.models import PlanLivenessSignal

    assert plan_liveness.arm_plan_wake(
        thread_id, PlanLivenessSignal(1, "selected-work", None, ()), db=db,
    ) is not None


def _wake_status(db, thread_id: str) -> str:
    return db.execute(
        "SELECT status FROM plan_liveness_wakes WHERE thread_id=?", (thread_id,),
    ).fetchone()[0]


@pytest.fixture
def quiet(monkeypatch):
    monkeypatch.setattr("infra.connection_limit_drive.schedule_plan_limit_stops", Mock())


@pytest.fixture
def stop(monkeypatch) -> AsyncMock:
    stop = AsyncMock()
    monkeypatch.setattr("ws.stop_control.stop_turn_for_cause", stop)
    return stop


def _turns(accounts):
    from infra.input_admission import accept_thread_input
    from infra.thread_inputs import start_idle_turn

    db, default, x, y = accounts
    _known_default(db, default)
    for account in (x, y):
        _ready(db, account)
    now = datetime.now(timezone.utc).isoformat()
    for thread, account in (('d', default), ('x', x), ('y', y)):
        db.execute("INSERT INTO threads(id,user_id,model,created_at,updated_at) VALUES (?,'user','codex/test','t','t')", (thread,))
        db.commit()
        select_account(thread, 'codex', account_id=account['id'], db=db)
        accept_thread_input(thread, role='user', content='work', intent='apply_now', message_id=thread+'-input', db=db)
        assert start_idle_turn(thread, turn_id=thread+'-turn', boot_epoch='test', started_at=now, db=db) is not None
    return db, default, x, y


def test_reset_prefers_the_exhausted_window(_no_codex_connection) -> None:
    from connections.harness_usage import reset_from_snapshot

    soon, later = _epoch(timedelta(hours=1)), _epoch(timedelta(days=3))
    snapshot = {
        "primary": {"usedPercent": 40, "resetsAt": soon},
        "secondary": {"usedPercent": 100, "resetsAt": later},
    }
    assert reset_from_snapshot(snapshot) == datetime.fromtimestamp(
        later, tz=timezone.utc
    ).isoformat()
    assert reset_from_snapshot({"primary": None}) is None
    assert reset_from_snapshot(None) is None
    # No window exhausted: an unrelated window's reset must not lift the limit.
    assert reset_from_snapshot({"primary": {"usedPercent": 40, "resetsAt": soon}}) is None


def test_sparse_update_keeps_prior_windows_and_clears_explicit_null(_no_codex_connection) -> None:
    from connections.harness_usage import merge_snapshot

    prior = {"primary": {"resetsAt": 5}, "rateLimitReachedType": "rate_limit_reached"}
    merged = merge_snapshot(prior, {"rateLimitReachedType": None, "secondary": None})
    assert merged["primary"] == {"resetsAt": 5}
    assert merged["rateLimitReachedType"] is None


def test_limit_applies_until_reset_then_lifts_without_a_timer(_no_codex_connection) -> None:
    from connections import harness_usage as codex_usage

    _seed_codex()
    resets = datetime.now(timezone.utc) + timedelta(hours=2)
    codex_usage.record_limited(
        reason="usageLimitExceeded", resets_at=resets.isoformat(), source="turn_error",
        provider="codex", receipt=default_receipt("codex"),
    )
    assert codex_usage.active_limit(provider="codex", account_id=default_id("codex"))["resets_at"] == resets.isoformat()
    assert codex_usage.active_limit(provider="codex", now=resets + timedelta(seconds=1), account_id=default_id("codex")) is None


def test_limit_without_reset_holds_until_codex_reports_allowed(_no_codex_connection) -> None:
    from connections import harness_usage as codex_usage

    _seed_codex()
    codex_usage.record_rate_limits_read(
        {"ordinaryUsageAllowed": False, "rateLimits": {"rateLimitReachedType": None}},
        provider="codex", receipt=default_receipt("codex"),
    )
    assert codex_usage.active_limit(provider="codex", now=datetime.now(timezone.utc) + timedelta(days=30), account_id=default_id("codex"))
    codex_usage.record_rate_limits_read({"ordinaryUsageAllowed": True, "rateLimits": {}}, provider="codex", receipt=default_receipt("codex"))
    assert codex_usage.active_limit(provider="codex", account_id=default_id("codex")) is None


def test_record_without_codex_connection_is_a_no_op(_no_codex_connection) -> None:
    from connections import harness_usage as codex_usage

    receipt = default_receipt("codex")
    assert receipt is None  # no connection, so no Default account and no login to write on
    codex_usage.record_limited(reason="x", resets_at=None, source="turn_error", provider="codex", receipt=receipt)
    from infra.database import get_db
    db = get_db()
    try:
        assert db.execute("SELECT COUNT(*) FROM vendor_accounts WHERE usage_json IS NOT NULL").fetchone()[0] == 0
    finally:
        db.close()


def test_turn_error_and_success_drive_the_state(_no_codex_connection) -> None:
    from connections import harness_usage as codex_usage
    from subagents import codex_usage_observer as observer

    # Vendor verdicts alone: spending past the plan limit is on (amendment 12 off is tested apart).
    _seed_codex()
    codex_usage.set_allow_overage("codex", default_id("codex"), True)
    session = _bind_default(SimpleNamespace())
    later = _epoch(timedelta(days=4))
    asyncio.run(observer.handle_rate_limits_notification(
        session, "account/rateLimits/updated",
        {"rateLimits": {"primary": {"usedPercent": 100, "resetsAt": later}}},
    ))
    assert codex_usage.active_limit(provider="codex", account_id=default_id("codex")) is None  # Not reached: nothing recorded.

    asyncio.run(observer.observe_turn_error(session, {"codexErrorInfo": "rateLimitExceeded"}))
    assert codex_usage.active_limit(provider="codex", account_id=default_id("codex")) is None  # A throttle is not an account limit.

    asyncio.run(observer.observe_turn_error(
        session, {"message": "You've hit your usage limit", "codexErrorInfo": "usageLimitExceeded"},
    ))
    state = codex_usage.active_limit(provider="codex", account_id=default_id("codex"))
    assert state["source"] == "turn_error"
    assert state["resets_at"] == datetime.fromtimestamp(later, tz=timezone.utc).isoformat()

    asyncio.run(observer.observe_turn_success(session, None))
    assert codex_usage.active_limit(provider="codex", account_id=default_id("codex")) is not None  # No start time: nothing proven.

    asyncio.run(observer.observe_turn_success(session, datetime.now(timezone.utc)))
    assert codex_usage.active_limit(provider="codex", account_id=default_id("codex")) is None


def test_reached_push_records_the_limit_immediately(_no_codex_connection) -> None:
    from connections import harness_usage as codex_usage
    from subagents import codex_usage_observer as observer

    _seed_codex()
    asyncio.run(observer.handle_rate_limits_notification(
        _bind_default(SimpleNamespace()), "account/rateLimits/updated",
        {"rateLimits": {"rateLimitReachedType": "workspace_owner_usage_limit_reached"}},
    ))
    assert codex_usage.active_limit(provider="codex", account_id=default_id("codex"))["reason"] == "workspace_owner_usage_limit_reached"


def test_quota_exhausting_turn_completion_keeps_the_pushed_limit(_no_codex_connection) -> None:
    """The turn that uses up the quota completes; the push during it still stands."""
    from connections import harness_usage as codex_usage

    _seed_codex()
    session = _dispatch_session()
    later = _epoch(timedelta(days=4))
    asyncio.run(_turn(session, "t1", {
        "rateLimitReachedType": "rate_limit_reached",
        "primary": {"usedPercent": 100, "resetsAt": later},
    }))
    state = codex_usage.active_limit(provider="codex", account_id=default_id("codex"))
    assert state is not None and state["source"] == "rate_limit_update"


def test_later_turn_in_the_same_session_lifts_an_earlier_limit(_no_codex_connection) -> None:
    """The session still holds the reached push; a later successful turn outranks it."""
    from connections import harness_usage as codex_usage

    _seed_codex()
    session = _dispatch_session()
    asyncio.run(_turn(session, "t1", {"rateLimitReachedType": "rate_limit_reached"}))
    assert codex_usage.active_limit(provider="codex", account_id=default_id("codex")) is not None
    asyncio.run(_turn(session, "t2"))
    assert codex_usage.active_limit(provider="codex", account_id=default_id("codex")) is None
    assert session._active_turn_started is None  # Consumed: nothing outlives the turn.


def test_turn_started_before_another_sessions_limit_keeps_it(_no_codex_connection) -> None:
    """The limit is account-wide: a turn that began earlier cannot vouch past it."""
    from connections import harness_usage as codex_usage

    _seed_codex()
    b = _dispatch_session()

    async def _scenario() -> None:
        await _start(b, "b1")
        codex_usage.record_limited(
            reason="rate_limit_reached", resets_at=None, source="rate_limit_update", provider="codex", receipt=default_receipt("codex"),
        )
        await b._handle_notification("turn/completed", _completed("b1"))

    asyncio.run(_scenario())
    assert codex_usage.active_limit(provider="codex", account_id=default_id("codex")) is not None


def test_stray_turn_start_cannot_let_a_turn_clear_its_own_limit(_no_codex_connection) -> None:
    """A provider turn/started never moves the owned turn's request time."""
    from connections import harness_usage as codex_usage

    _seed_codex()
    session = _dispatch_session()
    handle = session._handle_notification

    async def _scenario() -> None:
        await _start(session, "t1")
        await handle("account/rateLimits/updated", {"rateLimits": {
            "rateLimitReachedType": "rate_limit_reached"}})
        await handle("turn/started", {"turn": {"id": "stray"}})
        await handle("turn/started", {"turn": {"id": "t1"}})
        await handle("turn/completed", _completed("t1"))

    asyncio.run(_scenario())
    assert codex_usage.active_limit(provider="codex", account_id=default_id("codex")) is not None


def test_completion_for_a_rejected_turn_id_clears_nothing(_no_codex_connection) -> None:
    """A turn id the session ignores cannot vouch for the account."""
    from connections import harness_usage as codex_usage

    _seed_codex()
    session = _dispatch_session()
    handle = session._handle_notification

    async def _scenario() -> None:
        await _start(session, "t1")
        codex_usage.record_limited(
            reason="rate_limit_reached", resets_at=None, source="rate_limit_update", provider="codex", receipt=default_receipt("codex"),
        )
        await handle("turn/started", {"turn": {"id": "stray"}})
        await handle("turn/completed", _completed("stray"))

    asyncio.run(_scenario())
    assert codex_usage.active_limit(provider="codex", account_id=default_id("codex")) is not None
    assert session._active_turn_started[0] == "t1"  # The owned turn's, untouched.


def test_failed_owned_turn_releases_its_request_time(_no_codex_connection) -> None:
    from connections import harness_usage as codex_usage

    _seed_codex()
    session = _dispatch_session()

    async def _scenario() -> None:
        await _start(session, "t1")
        await session._handle_notification("error", {
            "turnId": "t1", "error": {"message": "boom"}, "willRetry": False,
        })

    asyncio.run(_scenario())
    assert session._active_turn_started is None
    assert codex_usage.active_limit(provider="codex", account_id=default_id("codex")) is None


def test_cleared_push_lifts_the_limit(_no_codex_connection) -> None:
    from connections import harness_usage as codex_usage

    _seed_codex()
    codex_usage.record_limited(reason="usageLimitExceeded", resets_at=None, source="turn_error", provider="codex", receipt=default_receipt("codex"))
    session = _dispatch_session()

    async def _scenario() -> None:
        await session._handle_notification("account/rateLimits/updated", {"rateLimits": {
            "rateLimitReachedType": "rate_limit_reached"}})
        await session._handle_notification("account/rateLimits/updated", {"rateLimits": {
            "rateLimitReachedType": None}})

    asyncio.run(_scenario())
    assert codex_usage.active_limit(provider="codex", account_id=default_id("codex")) is None


def test_failed_turn_through_dispatch_records_the_limit(_no_codex_connection) -> None:
    from connections import harness_usage as codex_usage

    _seed_codex()
    session = _dispatch_session()
    asyncio.run(session._handle_notification("turn/completed", {"turn": {
        "id": "t1", "status": "failed",
        "error": {"message": "You've hit your usage limit", "codexErrorInfo": "usageLimitExceeded"},
    }}))
    assert codex_usage.active_limit(provider="codex", account_id=default_id("codex"))["source"] == "turn_error"


def test_status_rows_carry_the_reason_and_reset(_no_codex_connection) -> None:
    from connections import harness_usage as codex_usage
    from infra.database import get_db
    from llm import model_status

    db = get_db()
    try:
        db.execute(
            "UPDATE models SET available = 1, enabled = 1, context_window = 272000 "
            "WHERE harness = 'codex'"
        )
        if not db.execute("SELECT 1 FROM models WHERE harness = 'codex'").fetchone():
            db.execute(
                "INSERT INTO models (id, harness, provider, provider_model_id, "
                "litellm_model, backend, source, enabled, available, context_window) "
                "VALUES ('codex/gpt-5.6-terra', 'codex', 'codex', 'gpt-5.6-terra', "
                "'codex/gpt-5.6-terra', 'cli', 'discovered', 1, 1, 272000)"
            )
        db.commit()
        codex_ids = [r["id"] for r in db.execute(
            "SELECT id FROM models WHERE harness = 'codex' AND enabled = 1"
        ).fetchall()]
    finally:
        db.close()
    _seed_codex()  # after the model rows exist: the account's discovery names the models it can run
    resets = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    codex_usage.record_limited(reason="usageLimitExceeded", resets_at=resets, source="turn_error", provider="codex", receipt=default_receipt("codex"))
    rows = {r["id"]: r for r in model_status.get_model_status_rows()}
    row = next(rows[i] for i in codex_ids if i in rows)
    selected = next(item for item in row["runs_on"] if item["selected"])
    assert (selected["status"], selected["status_reason"], selected["resets_at"]) == (
        "unavailable", "usage_limited", resets,
    )


def test_discovery_session_reads_account_limits(_no_codex_connection) -> None:
    from connections import harness_usage as codex_usage
    from subagents import codex_usage_observer as observer

    _seed_codex()
    calls: list[str] = []

    class _Session:
        async def _request(self, method, params):
            calls.append(method)
            return {"ordinaryUsageAllowed": False, "rateLimits": {}}

    asyncio.run(observer.observe_account_limits(_bind_default(_Session())))
    assert calls == ["account/rateLimits/read"]
    assert codex_usage.active_limit(provider="codex", account_id=default_id("codex"))["source"] == "discovery"


def test_discovery_survives_a_cli_without_the_method(_no_codex_connection) -> None:
    from subagents import codex_usage_observer as observer

    class _Old:
        async def _request(self, method, params):
            raise RuntimeError("method not found")

    asyncio.run(observer.observe_account_limits(_Old()))


@pytest.mark.parametrize("limited, expected", [
    (False, ("ready", None)),
    (True, ("unavailable", "usage_limited")),
])
def test_harness_status_applies_the_limit_only_to_a_ready_row(
    limited, expected, _no_codex_connection,
) -> None:
    from connections import harness_usage as codex_usage
    from connections.vendor_accounts import default_account
    from infra.database import get_db
    from llm.account_catalog import account_model_status

    _seed_codex()
    if limited:
        codex_usage.record_limited(reason="usageLimitExceeded", resets_at=None, source="turn_error",
                                   provider="codex", receipt=default_receipt("codex"))
    db = get_db()
    try:
        account = default_account("codex", db=db)
        ready = {"litellm_model": "codex/test", "status": "ready"}
        decision = account_model_status(account, ready, project_ids=[None], db=db)
        assert (decision["status"], decision["status_reason"]) == expected
        # An unconnected harness keeps its own, more basic reason.
        unconnected = {**ready, "status": "unavailable", "status_reason": "connection_unavailable"}
        decision = account_model_status(account, unconnected, project_ids=[None], db=db)
        assert (decision["status"], decision["status_reason"]) == ("unavailable", "connection_unavailable")
    finally:
        db.close()


@pytest.mark.asyncio
async def test_two_concurrent_sweeps_start_one_probe(isolated_db, monkeypatch) -> None:
    from infra.connection_limit_drive import Started, drive_scope_release

    db = get_db()
    _held_by_direct_insert(db, "first", "second")
    _expired_limit(db)
    drive = AsyncMock(return_value=SimpleNamespace(started=True))
    monkeypatch.setattr("infra.turn_admission_driver.drive_turn_admission", drive)
    # Both sweeps pass the probe's own checks, as two sweeps reading before either
    # claims would; only the marker's compare-and-set may keep the second out.
    monkeypatch.setattr("infra.connection_limit_release.probe_claim_allowed", lambda *_a, **_k: True)
    (due,) = release_due(
        harness_usage.stored_usage(provider="claude-code", db=db, account_id=default_id("claude-code", db=db)), datetime.now(timezone.utc),
        account=default_subject("claude-code", "null", db=db), max_attempts=5, backoff_seconds=180,
    )

    outcomes = await asyncio.gather(drive_scope_release(due), drive_scope_release(due))

    started = [outcome for outcome in outcomes if isinstance(outcome, Started)]
    running = db.execute("SELECT thread_id FROM active_turns WHERE status='running'").fetchall()
    assert len(started) == 1
    assert [row["thread_id"] for row in running] == [started[0].thread_id]
    assert drive.await_count == 1
    marker = verdicts(harness_usage.stored_usage(provider="claude-code", db=db, account_id=default_id("claude-code", db=db)))["sonnet"]["release"]
    assert (marker["state"], marker["thread_id"]) == ("started", started[0].thread_id)


def test_a_marker_write_never_loses_a_concurrent_config_write(isolated_db, monkeypatch) -> None:
    db = get_db()
    reset = _expired_limit(db)
    load = harness_usage._load
    other: dict[str, threading.Thread] = {}

    def envelope_write() -> None:
        harness_models.store_failure(
            "claude-code", "catalog down", harness_version="1", source="test", timestamp="t",
        )

    def load_then_race(connection, provider, account_id=None):
        loaded = load(connection, provider, account_id)
        if "writer" not in other:
            other["writer"] = threading.Thread(target=envelope_write)
            other["writer"].start()
            time.sleep(0.3)
            assert other["writer"].is_alive()  # blocked behind this read-modify-write
        return loaded

    monkeypatch.setattr(harness_usage, "_load", load_then_race)
    assert harness_usage.write_release(_subject(), reset, "started", at="t", thread_id="probe", db=db)
    other["writer"].join(timeout=10)

    config = db.execute("SELECT config FROM connections WHERE provider='claude-code'").fetchone()["config"]
    stored = json.loads(config)
    assert stored[harness_models.CACHE_KEY]["last_error"] == "catalog down"
    usage = harness_usage.stored_usage(provider="claude-code", db=db, account_id=default_id("claude-code", db=db))
    assert verdicts(usage)["sonnet"]["release"]["thread_id"] == "probe"


@pytest.mark.parametrize("outcome", ["error", "cancelled"])
def test_unrecorded_refusal_fails_the_successor_prefix_closed(outcome, isolated_db, monkeypatch, wakes) -> None:
    db = get_db()
    _thread_with_running_turn(db, "t1", running_since=_at(-1))
    queued = _queue_next_input(db, "t1")
    events = _events(monkeypatch, "infra.terminal_usage_limits")

    def fail(**_kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr("connections.harness_usage.record_limited", fail)
    terminal = finish_turn(
        "t1-turn", outcome=outcome, ended_at=_at(0), successor_turn_id="next",
        boot_epoch="boot", error=REFUSAL, db=db,
    )

    row = db.execute("SELECT status,error FROM active_turns WHERE id='t1-turn'").fetchone()
    assert row["status"] == "error"
    assert row["error"].startswith(REFUSAL + " (usage-limit verdict not recorded: RuntimeError: disk full")
    assert isinstance(terminal.successor, FailedInputBatch)
    assert terminal.successor.message_ids == (queued,)
    assert _running_count_for_thread(db, "t1") == 0
    assert _message(db, queued) == ("failed", row["error"])
    assert wakes.call_args.args[1] == (queued,)
    assert [call.args[:2] for call in events.call_args_list] == [("usage_limit", "verdict_write_failed")]
    assert harness_usage.stored_usage(provider="claude-code", db=db, account_id=default_id("claude-code", db=db)) is None


@pytest.mark.asyncio
async def test_failed_settlement_leaves_a_marker_the_sweep_drives(isolated_db, monkeypatch) -> None:
    from infra.connection_limit_drive import drive_limit_releases

    db = get_db()
    _limit_claude_default(db, _at(-2))
    _thread_with_running_turn(db, "prover", running_since=_at(-1))
    _thread_with_running_turn(db, "waiting")
    _queue_next_input(db, "waiting")
    _settle_raises(monkeypatch)
    events = _events(monkeypatch, "infra.terminal_usage_limits")

    terminal = finish_turn(
        "prover-turn", outcome="completed", ended_at=_at(0), successor_turn_id="next", boot_epoch="b", db=db,
    )

    assert terminal.result.value == "applied"
    verdict = verdicts(harness_usage.stored_usage(provider="claude-code", db=db, account_id=default_id("claude-code", db=db)))["sonnet"]
    assert verdict["state"] == "limited"  # never cleared by a settlement that raised
    assert (verdict["release"]["state"], verdict["release"]["attempts"]) == ("failed", 1)
    assert events.call_args.args[:2] == ("usage_limit", "settlement_failed")
    assert events.call_args.kwargs["properties"]["marked"] is True

    drive = AsyncMock(return_value=SimpleNamespace(started=True))
    monkeypatch.setattr("infra.turn_admission_driver.drive_turn_admission", drive)
    monkeypatch.setattr("infra.thread_execution.drive_turn_admission", drive)
    assert await drive_limit_releases(datetime.now(timezone.utc) + timedelta(hours=1)) == 1
    assert _running_count_for_thread(db, "waiting") == 1


def test_unmarked_failed_settlement_fails_the_prefix_closed(isolated_db, monkeypatch, wakes) -> None:
    db = get_db()
    _limit_claude_default(db, _at(-2))
    _thread_with_running_turn(db, "prover", running_since=_at(-1))
    queued = _queue_next_input(db, "prover")
    _settle_raises(monkeypatch)

    def no_marker(*_args, **_kwargs):
        raise RuntimeError("marker broke")

    monkeypatch.setattr("connections.harness_usage.write_release", no_marker)
    events = _events(monkeypatch, "infra.terminal_usage_limits")

    terminal = finish_turn(
        "prover-turn", outcome="completed", ended_at=_at(0), successor_turn_id="next", boot_epoch="b", db=db,
    )

    assert isinstance(terminal.successor, FailedInputBatch)
    assert _running_count_for_thread(db, "prover") == 0
    assert _message(db, queued) == ("failed", "weekly")  # the verdict's own refusal reason
    assert wakes.call_args.args[1] == (queued,)
    assert events.call_args.kwargs["properties"]["marked"] is False
    verdict = verdicts(harness_usage.stored_usage(provider="claude-code", db=db, account_id=default_id("claude-code", db=db)))["sonnet"]
    assert "release" not in verdict


@pytest.mark.asyncio
async def test_release_from_a_worker_thread_runs_on_the_captured_loop(isolated_db, driven) -> None:
    import infra.app_loop as app_loop
    from infra.connection_limit_drive import schedule_scope_release

    db = get_db()
    _thread_with_running_turn(db, "held")
    _queue_next_input(db, "held")
    app_loop.capture(asyncio.get_running_loop())
    try:
        await asyncio.to_thread(schedule_scope_release, _subject())
        await _until_called(driven)
    finally:
        app_loop.clear()

    assert [call.args for call in driven.await_args_list] == [("held",)]


def test_release_without_an_app_loop_is_never_silent(driven, isolated_db) -> None:
    import infra.app_loop as app_loop
    from infra.connection_limit_drive import schedule_scope_release

    app_loop.clear()
    with pytest.raises(app_loop.AppLoopUnavailable):
        schedule_scope_release(_subject())
    driven.assert_not_called()


@pytest.mark.asyncio
async def test_lifespan_owns_the_app_loop(monkeypatch) -> None:
    import app_lifecycle
    import infra.app_loop as app_loop
    from test_app_lifecycle_shutdown import _isolate_lifespan

    _isolate_lifespan(monkeypatch)
    async with app_lifecycle.lifespan(FastAPI()):
        assert app_loop._LOOP is asyncio.get_running_loop()
    assert app_loop._LOOP is None


def test_admission_reads_never_write(isolated_db) -> None:
    from infra.connection_limit_release import LimitProbe, startup_expired_admission
    from infra.delegation_status import held_admission_status
    from infra.held_admission import connection_limit_gate, limited_admission

    db = get_db()
    _limit_claude_default(db, _at(60))
    expired = _at(-1)
    _limit_claude_default(db, expired, scope="opus")
    _thread_with_running_turn(db, "limited")
    _queue_next_input(db, "limited")
    _thread_with_running_turn(db, "expired", OPUS)
    _queue_next_input(db, "expired")
    probe = LimitProbe(default_subject("claude-code", "opus", db=db), expired, "sweep")
    db.execute("PRAGMA query_only=ON")
    try:
        assert isinstance(held_admission_status(db, {"id": "limited", "model": SONNET}, None), HeldAdmission)
        assert isinstance(limited_admission("limited", SONNET, db=db), HeldAdmission)
        assert harness_usage.connection_availability(SONNET, db=db, account_id=model_default_id(SONNET, db=db)).state == "limited_with_reset"
        assert harness_usage.connection_availability(OPUS, db=db, account_id=model_default_id(OPUS, db=db)).state == "unknown"
        assert connection_limit_gate("limited", SONNET, db=db)[0] is True
        settled, held = connection_limit_gate("expired", OPUS, probe=probe, hold_expired_at_startup=True, db=db)
        assert settled is True and isinstance(held, HeldAdmission)
        assert isinstance(startup_expired_admission("expired", OPUS, db=db), HeldAdmission)
    finally:
        db.execute("PRAGMA query_only=OFF")


@pytest.mark.asyncio
async def test_due_scope_is_driven_once_by_the_sweep_phase(isolated_db, launched) -> None:
    from infra.connection_limit_drive import drive_limit_releases

    db = get_db()
    _held(db, "oldest", "newer")
    _limit(db, _at(-1))

    assert await drive_limit_releases(datetime.now(timezone.utc)) == 1
    assert _running(db) == ["oldest"]
    marker = _marker(db)
    assert (marker["state"], marker["thread_id"]) == ("started", "oldest")
    assert _due(db, later_minutes=60) == []  # a started window is never driven again
    assert await drive_limit_releases(datetime.now(timezone.utc)) == 0
    assert _running(db) == ["oldest"]


def test_the_probe_marker_is_a_compare_and_set_on_the_window(isolated_db) -> None:
    from infra.connection_limit_release import LimitProbe, claim_probe

    db = get_db()
    reset = _at(-1)
    _limit(db, reset)
    probe = LimitProbe(_subject(), reset, "sweep")

    assert claim_probe(probe, "first", db=db) is True
    assert claim_probe(probe, "second", db=db) is False
    assert _marker(db)["thread_id"] == "first"
    _limit(db, _at(-0.5))  # a newer verdict replaces the entry and its marker
    assert _marker(db) is None


@pytest.mark.asyncio
async def test_restart_drives_a_due_scope_without_reconstruction(isolated_db, launched) -> None:
    from infra.connection_limit_drive import drive_limit_releases

    db = get_db()
    threads = [f"thread-{index:02d}" for index in range(12)]
    _held(db, *threads)
    _limit(db, _at(-1))
    now = datetime.now(timezone.utc).isoformat()
    recovery = recover_threads(
        boot_epoch="reboot", recovered_at=now,
        successor_id=lambda thread_id: f"turn-{thread_id}", db=db,
    )

    assert len(recovery.batches) == 12
    assert all(isinstance(item, HeldAdmission) for item in recovery.batches)
    assert db.execute("SELECT COUNT(*) FROM automation_schedules").fetchone()[0] == 0
    await drive_limit_releases(datetime.now(timezone.utc))
    assert _running(db) == ["thread-00"]


def test_startup_admits_normally_once_the_window_is_settled(isolated_db) -> None:
    from connections.harness_usage import write_release

    db = get_db()
    _held(db, "thread")
    reset = _at(-1)
    _limit(db, reset)
    assert write_release(_subject(), reset, "no_eligible_head", at=_at(0), db=db)
    recovery = recover_threads(
        boot_epoch="reboot", recovered_at=_at(0), successor_id=lambda thread_id: f"turn-{thread_id}", db=db,
    )
    assert not any(isinstance(item, HeldAdmission) for item in recovery.batches)
    assert _running(db) == ["thread"]


@pytest.mark.asyncio
async def test_failed_stays_due_and_no_eligible_head_does_not(isolated_db, monkeypatch, launched) -> None:
    from infra.connection_limit_drive import Failed, NoEligibleHead, drive_scope_release

    db = get_db()
    _limit(db, _at(-1))
    (due,) = _due(db)
    assert isinstance(await drive_scope_release(due), NoEligibleHead)
    assert _marker(db)["state"] == "no_eligible_head"
    assert _due(db, later_minutes=60) == []

    _held(db, "boom")
    _limit(db, _at(-0.5))
    (due,) = _due(db)

    def explode(*_args, **_kwargs):
        raise RuntimeError("claim failed")

    monkeypatch.setattr("infra.thread_inputs.start_idle_turn", explode)
    outcome = await drive_scope_release(due)
    assert isinstance(outcome, Failed) and "claim failed" in outcome.error
    marker = _marker(db)
    assert (marker["state"], marker["attempts"]) == ("failed", 1)
    assert _due(db) == []  # inside the backoff
    assert [item.attempts for item in _due(db, later_minutes=4)] == [1]


@pytest.mark.asyncio
async def test_a_held_result_is_not_counted_as_started(isolated_db, monkeypatch, launched) -> None:
    from infra.connection_limit_drive import Held, drive_scope_release

    db = get_db()
    _held(db, "thread")
    _limit(db, _at(-1))
    _limit(db, _at(60), scope=None)  # the account scope still blocks every model
    events = Mock()
    monkeypatch.setattr("infra.connection_limit_drive.emit_product_event", events)
    (due,) = [item for item in _due(db) if item.subject == _subject()]

    assert isinstance(await drive_scope_release(due), Held)
    assert _running(db) == []
    assert _marker(db) is None
    events.assert_not_called()


@pytest.mark.asyncio
async def test_probe_plan_refusal_fails_visibly_and_the_next_head_probes(
    isolated_db, monkeypatch, launched,
) -> None:
    from infra.connection_limit_drive import Started, drive_scope_release
    from plans.public import PlanError

    db = get_db()
    _held(db, "plan-refused", "eligible")
    _limit(db, _at(-1))

    def snapshot(thread_id: str, *, db):
        if thread_id == "plan-refused":
            raise PlanError("unreconciled")
        return None

    monkeypatch.setattr("plans.public.snapshot_after_admission", snapshot)
    (due,) = _due(db)
    assert await drive_scope_release(due) == Started("eligible")
    refused = db.execute(
        "SELECT delivery_state,delivery_error FROM messages WHERE thread_id='plan-refused'"
    ).fetchone()
    assert refused["delivery_state"] == "failed"
    assert "unreconciled" in refused["delivery_error"]


@pytest.mark.asyncio
async def test_probe_plan_refusal_with_no_other_head_leaves_a_failed_marker(
    isolated_db, monkeypatch, launched,
) -> None:
    from infra.connection_limit_drive import Failed, drive_scope_release
    from plans.public import PlanError

    db = get_db()
    _held(db, "plan-refused")
    _limit(db, _at(-1))

    def snapshot(thread_id: str, *, db):
        raise PlanError("unreconciled")

    monkeypatch.setattr("plans.public.snapshot_after_admission", snapshot)
    (due,) = _due(db)
    assert isinstance(await drive_scope_release(due), Failed)
    marker = _marker(db)
    assert (marker["state"], marker["attempts"]) == ("failed", 1)
    assert "unreconciled" in marker["error"]


@pytest.mark.asyncio
async def test_exhausted_release_settles_every_held_head_once(isolated_db, monkeypatch) -> None:
    from connections.harness_usage import write_release
    from infra.connection_limit_drive import drive_limit_releases

    db = get_db()
    _held(db, "first", "second")
    reset = _at(-10)
    _limit(db, reset)
    for _attempt in range(5):
        assert write_release(_subject(), reset, "failed", at=_at(-10), error="boom", db=db)
    reported: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        "infra.failed_wake_surface.record_failed_wakes", lambda _db, ids: reported.append(tuple(ids)),
    )
    monkeypatch.setattr("infra.turn_admission_driver.drive_failed_wake_owners", AsyncMock())
    events = Mock()
    monkeypatch.setattr("infra.connection_limit_drive.emit_product_event", events)

    await drive_limit_releases(datetime.now(timezone.utc))
    await drive_limit_releases(datetime.now(timezone.utc))

    rows = db.execute("SELECT delivery_state,delivery_error FROM messages ORDER BY thread_id").fetchall()
    assert [tuple(row) for row in rows] == [("failed", "weekly"), ("failed", "weekly")]
    assert sorted(reported) == [("message-first",), ("message-second",)]
    assert _marker(db)["state"] == "exhausted"
    assert [call.args[:2] for call in events.call_args_list] == [("usage_limit", "release_exhausted")]
    assert _running(db) == []


@pytest.mark.asyncio
async def test_scope_batch_pages_past_a_full_raw_page_of_other_account_heads(claude, monkeypatch) -> None:
    """Canonical filtering must not make a full SQL page look exhausted."""
    from infra.connection_limit_drive import _drive_scope_batch

    db = claude.db
    for index in range(128):
        thread_id = f"other-{index:03d}"
        _thread(db, thread_id)
        select_account(thread_id, "claude-code", account_id=claude.other, db=db)
        _queue(db, thread_id, index)
        db.execute("UPDATE messages SET created_at=? WHERE id=?", (f"{index:04d}", f"message-{thread_id}"))
    _thread(db, "subject")
    select_account("subject", "claude-code", account_id=claude.first, db=db)
    _queue(db, "subject", 129)
    db.execute("UPDATE messages SET created_at='9999' WHERE id='message-subject'")
    db.commit()
    driven = AsyncMock()
    monkeypatch.setattr("infra.thread_execution.drive_idle_thread", driven)

    await _drive_scope_batch(_subject())

    assert [call.args[0] for call in driven.await_args_list] == ["subject"]


def test_candidate_query_resolves_each_distinct_model_once(isolated_db, monkeypatch) -> None:
    import llm.model_status as model_status
    from infra import connection_limit_release as release

    db = get_db()
    for index in range(10):
        _thread(db, f"thread-{index}", SONNET if index % 2 else "claude-code/opus")
        _queue(db, f"thread-{index}", index)
    resolve = Mock(wraps=model_status.harness_connection_for_model)
    monkeypatch.setattr(model_status, "harness_connection_for_model", resolve)
    monkeypatch.setattr(release, "_CANDIDATE_PAGE", 2)

    def pages(subject: LimitSubject) -> tuple[list[list[str]], set[int]]:
        found: list[list[str]] = []
        counts: set[int] = set()
        after = None
        while True:
            page = release.candidate_rows(subject, after=after, db=db)
            counts.add(resolve.call_count)
            resolve.reset_mock()
            if not page:
                return found, counts
            found.append([thread_id for thread_id, _model, _key in page])
            after = (page[-1][2], page[-1][0])

    account, account_counts = pages(default_subject("claude-code", "null", db=db))
    sonnet, sonnet_counts = pages(_subject())

    # Each call resolves every distinct matching model once, not once per queued row.
    assert (account_counts, sonnet_counts) == ({2}, {1})
    assert sum(account, []) == [f"thread-{index}" for index in range(10)]
    assert sonnet == [["thread-1", "thread-3"], ["thread-5", "thread-7"], ["thread-9"]]


@pytest.mark.asyncio
async def test_completed_probe_clears_the_scope_and_releases_the_rest(isolated_db, monkeypatch, launched) -> None:
    from infra.connection_limit_drive import drive_limit_releases

    db = get_db()
    _held(db, "probe", "waiting")
    _limit(db, _at(-1))
    released = Mock()
    monkeypatch.setattr("infra.connection_limit_drive.schedule_scope_release", released)
    await drive_limit_releases(datetime.now(timezone.utc))
    turn_id = db.execute("SELECT id FROM active_turns WHERE thread_id='probe'").fetchone()["id"]

    finish_turn(turn_id, outcome="completed", ended_at=_at(0.1), successor_turn_id="next", boot_epoch="b", db=db)

    assert harness_usage.connection_availability(SONNET, db=db, account_id=model_default_id(SONNET, db=db)).state == "allowed"
    assert released.call_args.args == (_subject(),)


@pytest.mark.asyncio
async def test_a_probe_that_ends_without_completing_hands_the_window_back(isolated_db, launched) -> None:
    from infra.connection_limit_drive import drive_limit_releases

    db = get_db()
    _held(db, "probe", "waiting")
    _limit(db, _at(-1))
    await drive_limit_releases(datetime.now(timezone.utc))
    turn_id = db.execute("SELECT id FROM active_turns WHERE thread_id='probe'").fetchone()["id"]

    finish_turn(turn_id, outcome="error", ended_at=_at(0.1), successor_turn_id="next",
                boot_epoch="b", error="socket closed", db=db)

    marker = _marker(db)
    assert (marker["state"], marker["attempts"]) == ("failed", 1)
    assert [item.subject for item in _due(db, later_minutes=4)] == [_subject()]


@pytest.mark.parametrize(
    ("provider", "model", "scope"),
    [("claude-code", SONNET, "sonnet"), ("codex", "codex/gpt-5-codex", None)],
)
def test_limit_recorded_during_the_turn_is_never_cleared(provider, model, scope, isolated_db) -> None:
    db = get_db()
    _thread(db, "thread", model)
    started = _at(0)
    db.execute(
        "INSERT INTO active_turns (id,thread_id,execution_epoch,model,status,started_at,boot_epoch) "
        "VALUES ('turn','thread',0,?,'running',?,'boot')", (model, started),
    )
    db.commit()
    _limit(db, _at(-1), scope=scope, provider=provider)  # observed after the Turn started

    finish_turn("turn", outcome="completed", ended_at=_at(0.1), successor_turn_id="next", boot_epoch="b", db=db)

    verdict = verdicts(harness_usage.stored_usage(provider=provider, db=db, account_id=default_id(provider, db=db)))[scope or "null"]
    assert verdict["state"] == "limited"


def test_an_allowed_signal_releases_its_scope(isolated_db, monkeypatch) -> None:
    db = get_db()
    _held(db, "thread")
    released = Mock()
    monkeypatch.setattr("infra.connection_limit_drive.schedule_scope_release", released)
    from connections.claude_usage import apply_rate_limit_event

    _limit(db, _at(10), scope=None)
    apply_rate_limit_event({"status": "allowed"}, db=db, receipt=default_receipt('claude-code', db=db))
    assert released.call_args.args == (default_subject("claude-code", "null", db=db),)



def test_required_fold_on_plan_limited_own_connection_holds_then_toggle_on_releases(
    isolated_db, monkeypatch,
) -> None:
    from connections.harness_usage import set_allow_overage
    from infra.thread_driver import finish_turn
    from infra.thread_input_types import HeldAdmission

    monkeypatch.setattr("infra.connection_limit_drive.schedule_plan_limit_stops", Mock())
    released = Mock()
    monkeypatch.setattr("infra.connection_limit_drive.schedule_scope_release", released)
    db = get_db()
    _refused_input(db)
    _plan_limit_sonnet(db)

    terminal = finish_turn(
        "t1-turn", outcome="error", ended_at="t3", successor_turn_id="next",
        boot_epoch="boot", error=_limited_fold_error(db), db=db,
    )

    # Held, not failed: the input waits on the plan limit and its reset deadline drives it.
    row = db.execute("SELECT delivery_state,delivery_error FROM messages WHERE id='t1-input'").fetchone()
    assert (row["delivery_state"], row["delivery_error"]) == ("queued", None)
    assert terminal.failed_message_ids == ()
    assert isinstance(terminal.successor, HeldAdmission)
    assert terminal.successor.reason == PLAN_LIMIT_REASON
    verdict = _sonnet_verdict(db)
    assert (verdict.get("state"), verdict.get("resets_at")) == ("limited", _RESET)
    assert release_marker(verdict) is None
    assert _due_after_reset(db) == [_subject()]

    set_allow_overage("claude-code", default_id("claude-code", db=db), True, db=db)

    assert released.call_args.args == (_subject(),)
    assert _sonnet_verdict(db).get("state") == "allowed"
    assert _due_after_reset(db) == []
    assert harness_usage.connection_availability(SONNET, db=db, account_id=model_default_id(SONNET, db=db)).state == "allowed"


def test_full_plan_window_holds_by_default_and_never_with_the_switch_on(isolated_db, no_stops) -> None:
    db = get_db()
    harness_usage.record_usage(provider="claude-code", windows=[_window()], db=db, receipt=default_receipt("claude-code", db=db))
    assert _state(db) == _HELD

    # A vendor ``allowed`` (extra usage) never lifts the policy while the window is spent.
    harness_usage.record_allowed(provider="claude-code", source="rate_limit_event", db=db, receipt=default_receipt("claude-code", db=db))
    assert _state(db) == _HELD

    _set_overage(db, True)
    assert _state(db)[0] == "allowed"
    harness_usage.record_usage(provider="claude-code", windows=[_window()], db=db, receipt=default_receipt("claude-code", db=db))
    assert _state(db)[0] == "allowed"


def test_stale_or_partial_window_never_holds(isolated_db, no_stops) -> None:
    db = get_db()
    stale = _window(observed=_now() - timedelta(hours=6), minutes=300)
    harness_usage.record_usage(provider="claude-code", windows=[stale, _window("seven_day", percent=99)], db=db, receipt=default_receipt("claude-code", db=db))
    assert _state(db)[0] != _HELD[0]


def test_sonnet_window_holds_only_sonnet(isolated_db, no_stops) -> None:
    db = get_db()
    harness_usage.record_usage(provider="claude-code", windows=[_window("seven_day_sonnet")], db=db, receipt=default_receipt("claude-code", db=db))
    assert _state(db, "claude-code/sonnet") == _HELD
    assert _state(db, "claude-code/opus")[0] != _HELD[0]


def test_switch_off_stores_only_and_on_releases_held_work(isolated_db, monkeypatch, no_stops) -> None:
    db = get_db()
    released = Mock()
    monkeypatch.setattr("infra.connection_limit_drive.schedule_scope_release", released)
    harness_usage.record_usage(provider="claude-code", windows=[_window("seven_day_sonnet")], db=db, receipt=default_receipt("claude-code", db=db))

    _set_overage(db, False)
    released.assert_not_called()
    assert _state(db) == _HELD

    _set_overage(db, True)
    assert released.call_args.args == (default_subject("claude-code", "sonnet", db=db),)
    assert harness_usage.stored_usage(provider="claude-code", db=db, account_id=default_id("claude-code", db=db))["allow_overage"] is True


def test_stop_is_scheduled_after_the_verdict_commits_once_per_window(isolated_db, monkeypatch) -> None:
    db = get_db()
    _thread_with_model(db, "thread")
    _queue_named_message(db, "thread", "m1")
    start_idle_turn("thread", turn_id="turn", boot_epoch="boot", started_at=_now().isoformat(), db=db)
    seen = []

    def stops(turns) -> None:
        # Runs after commit: the limited verdict is durable before any Stop.
        seen.append((turns, db.in_transaction, _state(db)))

    monkeypatch.setattr("infra.connection_limit_drive.schedule_plan_limit_stops", stops)
    harness_usage.record_usage(provider="claude-code", windows=[_window()], db=db, receipt=default_receipt("claude-code", db=db))
    harness_usage.record_usage(provider="claude-code", windows=[_window()], db=db, receipt=default_receipt("claude-code", db=db))

    assert seen == [((("thread", "turn"),), False, _HELD)]


def test_observer_schedules_the_stop_and_never_awaits_it(isolated_db, monkeypatch) -> None:
    db = get_db()
    _thread_with_model(db, "thread")
    _queue_named_message(db, "thread", "m1")
    start_idle_turn("thread", turn_id="turn", boot_epoch="boot", started_at=_now().isoformat(), db=db)
    scheduled: list = []
    monkeypatch.setattr("infra.app_loop.submit", scheduled.append)
    stop = AsyncMock()
    monkeypatch.setattr("ws.stop_control.stop_turn_for_cause", stop)

    harness_usage.record_usage(provider="claude-code", windows=[_window()], db=db, receipt=default_receipt("claude-code", db=db))

    stops = [c for c in scheduled if c.__qualname__ == "_stop_at_plan_limit" or c.close()]  # the catalog push too
    assert len(stops) == 1 and stop.await_count == 0
    asyncio.run(stops[0])
    stop.assert_awaited_once_with("thread", "turn")


def test_plan_limit_stop_returns_unwritten_inputs_at_their_seq_without_a_continue(isolated_db, no_stops) -> None:
    db = get_db()
    _claimed_turn(db)
    harness_usage.record_usage(provider="claude-code", windows=[_window()], db=db, receipt=default_receipt("claude-code", db=db))

    _finish(db, PlanLimitStop())

    # The returned input restarts the thread itself; the held scope starts no successor.
    assert _rows(db) == {"m1-thread": ("queued", None)}
    assert _running_total(db) == 0


def test_plan_limit_stop_after_dispatch_holds_one_continue_input(isolated_db, no_stops) -> None:
    db = get_db()
    _claimed_turn(db)
    db.execute("UPDATE messages SET delivery_state='delivered' WHERE id='m1-thread'")
    db.commit()
    harness_usage.record_usage(provider="claude-code", windows=[_window()], db=db, receipt=default_receipt("claude-code", db=db))

    _finish(db, PlanLimitStop())

    assert _rows(db)[continue_message_id("turn")] == ("queued", None)
    assert _running_total(db) == 0
    summary, sender, cause = db.execute(
        "SELECT json_extract(metadata,'$.summary'),json_extract(metadata,'$.sender_kind'),"
        "json_extract(metadata,'$.cause') FROM messages WHERE id=?", (continue_message_id("turn"),),
    ).fetchone()
    assert (summary, sender, cause) == (CONTINUE_CHIP, "system", PLAN_LIMIT_REASON)


def test_user_stop_keeps_cancelling_its_inputs(isolated_db, no_stops) -> None:
    db = get_db()
    _claimed_turn(db)
    harness_usage.record_usage(provider="claude-code", windows=[_window()], db=db, receipt=default_receipt("claude-code", db=db))

    _finish(db, None)

    rows = _rows(db)
    assert rows["m1-thread"][0] == "cancelled"
    assert continue_message_id("turn") not in rows


def test_continue_input_is_skipped_beside_real_input_and_never_duplicated(isolated_db, no_stops) -> None:
    db = get_db()
    _claimed_turn(db)
    queue_plan_limit_continue(db, "turn", "thread")
    queue_plan_limit_continue(db, "turn", "thread")
    db.commit()
    assert list(_rows(db)).count(continue_message_id("turn")) == 1

    _claimed_turn(db, "other", "turn-other")
    _queue_named_message(db, "other", "m2")
    queue_plan_limit_continue(db, "turn-other", "other")
    db.commit()
    assert continue_message_id("turn-other") not in _rows(db, "other")


@pytest.mark.asyncio
async def test_cause_reaches_each_harness_runtime_and_user_stop_is_unchanged(monkeypatch) -> None:
    from subagents import cc_runtime, codex_runtime
    from ws.stop_runtime import stop_runtimes

    for runtime in (cc_runtime, codex_runtime):
        monkeypatch.setattr(runtime, "stop", AsyncMock(return_value=False))
        monkeypatch.setattr(runtime, "has_pending_or_live", lambda _sid: True)
    cause = PlanLimitStop()

    await stop_runtimes("sid", None, None, "turn", Mock(), cause=cause)
    await stop_runtimes("sid", None, None, "turn", Mock())

    for runtime in (cc_runtime, codex_runtime):
        system, user = runtime.stop.await_args_list
        assert system.kwargs["cause"] is cause
        assert "cause" not in user.kwargs


@pytest.mark.asyncio
async def test_system_stop_shares_the_one_stop_writer(isolated_db, monkeypatch) -> None:
    from ws import stop_control

    db = get_db()
    _thread_with_model(db, "thread")
    exact = AsyncMock(return_value=stop_control.StopReceipt("thread", "stopped", "turn"))
    monkeypatch.setattr(stop_control, "_stop_exact_turn", exact)

    await stop_control.stop_turn_for_cause("thread", "turn")

    exact.assert_awaited_once()
    call = exact.await_args
    assert call is not None
    assert (call.args[0], call.args[4]) == ({"id": "user"}, "turn")
    assert isinstance(call.kwargs["cause"], PlanLimitStop)


@pytest.mark.asyncio
async def test_system_stop_of_an_ended_turn_queues_nothing(isolated_db, monkeypatch) -> None:
    from ws import stop_control

    db = get_db()
    _claimed_turn(db)
    _finish(db, None)
    monkeypatch.setattr("ws.stop_runtime.stop_runtimes", AsyncMock(return_value=False))

    await stop_control.stop_turn_for_cause("thread", "turn")

    # The user's Stop won: its inputs stay cancelled and no continue input appears.
    assert _rows(db) == {"m1-thread": ("cancelled", "turn")}


@pytest.mark.asyncio
async def test_stop_deadline_commit_returns_inputs_and_frames_the_cause(isolated_db, no_stops, monkeypatch) -> None:
    from infra import state
    from ws import stop_control

    db = get_db()
    _claimed_turn(db)
    harness_usage.record_usage(provider="claude-code", windows=[_window()], db=db, receipt=default_receipt("claude-code", db=db))
    monkeypatch.setattr("ws.stop_runtime.stop_runtimes", AsyncMock(return_value=False))
    monkeypatch.setattr("ws.stop_runtime.retire_after_stop", AsyncMock())
    frames = []
    monkeypatch.setattr(state, "broadcast_event", lambda _sid, _user, frame: frames.append(frame))

    # The verdict's own Stop is captured, not run, so this call is the one writer.
    no_stops.assert_called_once_with((("thread", "turn"),))
    receipt = await stop_control.stop_turn_for_cause("thread", "turn")

    assert receipt.outcome == "stopped"
    assert _rows(db) == {"m1-thread": ("queued", None)}
    assert [frame.get("cause") for frame in frames if frame["type"] == "turn_cancelled"] == [PLAN_LIMIT_REASON]
    assert _running_total(db) == 0


def test_no_ws_or_api_route_reaches_the_system_stop() -> None:
    callers = []
    for root in ("api", "ws"):
        for path in (_BACKEND / root).rglob("*.py"):
            tree = ast.parse(path.read_text())
            names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
            names |= {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
            names |= {alias.name for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
                      for alias in node.names}
            if "stop_turn_for_cause" in names:
                callers.append(path.relative_to(_BACKEND).as_posix())
    assert callers == []


def test_codex_push_at_full_plan_window_stops_every_running_turn_on_the_connection(
    isolated_db, monkeypatch,
) -> None:
    from types import SimpleNamespace

    from subagents import codex_usage_observer as observer
    from connections.vendor_accounts import default_account
    from connections.account_receipts import capture_receipt
    from connections.account_evidence import mark_discovery
    from vendor_account_fixtures import _known_default

    db = get_db()
    default = default_account('codex', db=db)
    _known_default(db, default)
    receipt = capture_receipt('codex', account_id=default['id'], db=db)
    assert receipt is not None
    assert mark_discovery(default['id'], receipt=receipt, status='ready',
                          models={'codex/gpt-5-codex': {'available': True}}, db=db)
    db.commit()
    for thread_id, model in (("mini", "codex/gpt-5-codex"), ("main", "codex/gpt-5-codex"),
                             ("claude", "claude-code/sonnet")):
        _claimed_turn_on(db, thread_id, model)
    stops = Mock()
    monkeypatch.setattr("infra.connection_limit_drive.schedule_plan_limit_stops", stops)
    reset = int((_now() + timedelta(hours=2)).timestamp())

    asyncio.run(observer.handle_rate_limits_notification(
        SimpleNamespace(vendor_account_bound=True, vendor_account_id=default['id'],
                        vendor_account_receipt=receipt), "account/rateLimits/updated",
        {"rateLimits": {"primary": {"usedPercent": 100, "resetsAt": reset}, "credits": {"hasCredits": True}}},
    ))

    assert _state(db, "codex/gpt-5-codex") == _HELD
    assert sorted(stops.call_args.args[0]) == [("main", "turn-main"), ("mini", "turn-mini")]


@pytest.mark.parametrize("provider", ["claude-code", "codex"])
def test_switch_route_stores_the_flag_identically_for_both_harnesses(api, provider, no_stops) -> None:
    account_id = default_id(provider)
    response = api.put(f"/api/vendor-accounts/{account_id}/usage", json={"allow_overage": True})

    assert response.status_code == 200
    assert (response.json()["id"], response.json()["allow_overage"]) == (account_id, True)
    usage = harness_usage.stored_usage(provider=provider, db=get_db(), account_id=account_id)
    assert harness_usage.project_for_model(usage)["allow_overage"] is True


def test_switch_route_refuses_unknown_accounts_and_non_boolean_values(api) -> None:
    assert api.put("/api/vendor-accounts/missing/usage", json={"allow_overage": True}).status_code == 409
    assert api.put(f"/api/vendor-accounts/{default_id('codex')}/usage", json={"allow_overage": "yes"}).status_code == 422


def test_a_verdict_flip_pushes_models_updated_once(isolated_db, no_stops, pushes, releases) -> None:
    db = get_db()
    harness_usage.record_usage(provider="claude-code", windows=[_window()], db=db, receipt=default_receipt("claude-code", db=db))
    assert pushes.call_count == 1  # allowed -> plan limited

    harness_usage.record_usage(provider="claude-code", windows=[_window()], db=db, receipt=default_receipt("claude-code", db=db))
    assert pushes.call_count == 1  # the same reading changes nothing a reader shows

    _set_overage(db, True)
    assert pushes.call_count == 2  # released


def test_the_push_reaches_clients_from_a_db_worker(monkeypatch) -> None:
    from infra.state import ws

    submitted: list = []
    sent = Mock()
    monkeypatch.setattr("infra.app_loop.submit", submitted.append)
    monkeypatch.setattr(ws, "broadcast_all", sent)
    monkeypatch.setattr("infra.unavailable_model_recovery.schedule_unavailable_model_recovery", Mock())

    ws.schedule_models_changed()
    assert sent.call_count == 0 and len(submitted) == 1
    asyncio.run(submitted[0])
    sent.assert_called_once_with({"type": "models_updated"})


def test_a_sweep_release_pushes_models_updated_once_per_window(isolated_db, monkeypatch) -> None:
    from infra import connection_limit_drive as drive

    pushed, driven = Mock(), []

    async def _drive(item):
        driven.append(item)

    monkeypatch.setattr("infra.state.ws.notify_models_changed", pushed)
    monkeypatch.setattr(drive, "drive_scope_release", _drive)
    db = get_db()
    past = (_now() - timedelta(minutes=1)).isoformat()
    harness_usage.record_limited(
        reason="five_hour_limit", resets_at=past, source="rate_limit_event", provider="claude-code", db=db, receipt=default_receipt("claude-code", db=db),
    )
    asyncio.run(drive.drive_limit_releases(_now()))
    assert pushed.call_count == 1 and len(driven) == 1

    subject = default_subject("claude-code", "null", db=db)
    assert harness_usage.write_release(subject, past, "failed", at=_now().isoformat(), error="x", db=db)
    asyncio.run(drive.drive_limit_releases(_now() + timedelta(hours=1)))
    assert pushed.call_count == 1  # a retry changes nothing a reader shows


def test_switch_off_holds_and_stops_at_once_on_a_stored_full_window(isolated_db, no_stops) -> None:
    db = get_db()
    _thread_with_model(db, "thread")
    _queue_named_message(db, "thread", "m1")
    _set_overage(db, True)
    harness_usage.record_usage(provider="claude-code", windows=[_window()], db=db, receipt=default_receipt("claude-code", db=db))
    start_idle_turn("thread", turn_id="turn", boot_epoch="boot", started_at=_now().isoformat(), db=db)
    assert _state(db)[0] == "unknown" and not no_stops.called

    _set_overage(db, False)

    assert _state(db) == _HELD_RESET
    no_stops.assert_called_once_with((("thread", "turn"),))


def test_switch_off_with_no_full_window_waits_for_the_next_reading(isolated_db, no_stops) -> None:
    db = get_db()
    harness_usage.record_usage(provider="claude-code", windows=[_window(percent=80)], db=db, receipt=default_receipt("claude-code", db=db))
    _set_overage(db, False)
    assert _state(db)[0] != "limited_with_reset" and not no_stops.called


def test_sign_in_releases_a_plan_hold_and_reads_the_new_account(isolated_db, no_stops, releases) -> None:
    db = get_db()
    harness_usage.record_usage(provider="claude-code", windows=[_window()], db=db, receipt=default_receipt("claude-code", db=db))
    assert _state(db) == _HELD_RESET

    _sign_in(db)

    assert _state(db)[0] == "allowed"
    assert releases.call_args.args == (default_subject("claude-code", "null", db=db),)
    window = next(iter(harness_usage.stored_usage(provider="claude-code", db=db, account_id=default_id("claude-code", db=db))["windows"].values()))
    assert window["used_percent"] == 10 and window["kind"] == "five_hour"  # the new login's own reading


def test_a_no_reset_full_window_holds_without_reset(isolated_db, no_stops) -> None:
    db = get_db()
    harness_usage.record_usage(provider="claude-code", windows=[_no_reset()], db=db, receipt=default_receipt("claude-code", db=db))
    assert _state(db) == _HELD_NO_RESET
    harness_usage.record_usage(provider="codex", windows=[_codex_no_reset()], db=db, receipt=default_receipt("codex", db=db))
    assert _state(db, "codex/gpt-5-codex") == _HELD_NO_RESET


def test_a_stale_no_reset_full_window_never_holds(isolated_db, no_stops) -> None:
    db = get_db()
    harness_usage.record_usage(
        provider="claude-code", windows=[_no_reset(observed=_now() - timedelta(hours=6))], db=db, receipt=default_receipt("claude-code", db=db),
    )
    assert _state(db)[0] != "limited_without_reset"


def test_vendor_parity_for_a_no_reset_full_reading(isolated_db, no_stops) -> None:
    db = get_db()
    harness_usage.record_usage(provider="claude-code", windows=[_no_reset()], db=db, receipt=default_receipt("claude-code", db=db))
    harness_usage.record_usage(provider="codex", windows=[_codex_no_reset()], db=db, receipt=default_receipt("codex", db=db))
    projections = [
        harness_usage.project_for_model(harness_usage.stored_usage(provider=p, db=db, account_id=default_id(p, db=db)), model=m)
        for p, m in (("claude-code", "claude-code/sonnet"), ("codex", "codex/gpt-5-codex"))
    ]
    assert [(p["state"], p["usable"], p["reason"], p["resets_at"]) for p in projections] == [
        ("limited", False, PLAN_LIMIT_REASON, None),
    ] * 2


def test_credits_in_the_same_frame_keep_the_no_reset_hold(isolated_db, no_stops) -> None:
    db = get_db()
    harness_usage.record_usage(provider="claude-code", windows=[_no_reset()], db=db, receipt=default_receipt("claude-code", db=db))
    reading = _no_reset()
    harness_usage.record_usage(provider="claude-code", windows=[reading], verdict={
        "state": "allowed", "source": "rate_limit_event", "observed_at": reading["observed_at"],
    }, db=db, receipt=default_receipt("claude-code", db=db))
    assert _state(db) == _HELD_NO_RESET


def test_a_codex_read_with_credits_holds_at_a_no_reset_full_window(isolated_db, no_stops) -> None:
    """One real read: neither the window nor its ``allowed`` verdict carries a time of its own."""
    db = get_db()
    snapshot = {"limitId": "codex", "primary": {"usedPercent": 100, "windowDurationMins": 300, "resetsAt": None}}
    harness_usage.record_rate_limits_read({"ordinaryUsageAllowed": True, "rateLimits": snapshot}, provider="codex", db=db, receipt=default_receipt("codex", db=db))
    assert _state(db, "codex/gpt-5-codex") == _HELD_NO_RESET


def test_a_claude_event_with_overage_holds_at_a_no_reset_full_window(isolated_db, no_stops) -> None:
    from connections import claude_usage

    db = get_db()
    claude_usage.apply_rate_limit_event({"status": "allowed", "rateLimitType": "five_hour", "utilization": 1.0}, db=db, receipt=default_receipt('claude-code', db=db))
    assert _state(db) == _HELD_NO_RESET


def test_a_later_allowed_signal_releases_the_no_reset_hold(isolated_db, no_stops, releases) -> None:
    db = get_db()
    harness_usage.record_usage(
        provider="claude-code", windows=[_no_reset(observed=_now() - timedelta(minutes=5))], db=db, receipt=default_receipt("claude-code", db=db),
    )
    harness_usage.record_allowed(provider="claude-code", source="rate_limit_event", db=db, receipt=default_receipt("claude-code", db=db))
    assert _state(db)[0] == "allowed"
    assert releases.call_args.args == (default_subject("claude-code", "null", db=db),)


def test_a_reading_below_the_limit_releases_the_no_reset_hold(isolated_db, no_stops, releases) -> None:
    db = get_db()
    harness_usage.record_usage(provider="claude-code", windows=[_no_reset()], db=db, receipt=default_receipt("claude-code", db=db))
    harness_usage.record_usage(provider="claude-code", windows=[_no_reset(percent=40)], db=db, receipt=default_receipt("claude-code", db=db))
    assert _state(db)[0] == "allowed"
    assert releases.call_args.args == (default_subject("claude-code", "null", db=db),)


def test_a_reading_below_the_limit_never_lifts_a_hold_with_a_reset(isolated_db, no_stops) -> None:
    db = get_db()
    harness_usage.record_usage(provider="claude-code", windows=[_window()], db=db, receipt=default_receipt("claude-code", db=db))
    harness_usage.record_usage(provider="claude-code", windows=[_window(percent=40)], db=db, receipt=default_receipt("claude-code", db=db))
    assert _state(db) == _HELD_RESET  # it releases at its reset, as before


def test_another_windows_reset_never_names_an_unknown_one(isolated_db, no_stops) -> None:
    db = get_db()
    harness_usage.record_usage(provider="claude-code", windows=[_no_reset(), _window("seven_day")], db=db, receipt=default_receipt("claude-code", db=db))
    assert _state(db) == _HELD_NO_RESET


def test_sign_in_and_switch_on_release_the_no_reset_hold(isolated_db, monkeypatch, no_stops, releases) -> None:
    monkeypatch.setattr("connections.harness_usage_check.schedule_account_check", Mock())
    db = get_db()
    harness_usage.record_usage(provider="claude-code", windows=[_no_reset()], db=db, receipt=default_receipt("claude-code", db=db))
    _sign_in(db)
    assert _state(db)[0] == "allowed"

    harness_usage.record_usage(provider="claude-code", windows=[_no_reset()], db=db, receipt=default_receipt("claude-code", db=db))
    assert _state(db) == _HELD_NO_RESET
    _set_overage(db, True)
    assert _state(db)[0] == "allowed"


def test_model_switch_moves_input_held_without_reset(isolated_db, no_stops) -> None:
    from infra.model_switches import request_switch

    db = get_db()
    harness_usage.record_usage(provider="claude-code", windows=[_no_reset("seven_day_sonnet")], db=db, receipt=default_receipt("claude-code", db=db))
    _thread_with_model(db, "thread")
    _queue_named_message(db, "thread", "m1")
    request = request_switch("thread", "claude-code/opus", operation_id="op", requested_at="t", db=db)
    assert "m1" in request.updated_input_ids
    row = db.execute("SELECT target_model FROM messages WHERE id='m1'").fetchone()
    assert row["target_model"] == "claude-code/opus"


@pytest.mark.parametrize(("kind", "minutes"), [
    ("five_hour", 300), ("seven_day", 10080), ("seven_day_sonnet", 10080), ("codex_primary", None),
])
def test_window_length_comes_from_the_vendor_or_the_kind(kind, minutes) -> None:
    assert window_length({"kind": kind}) == minutes
    assert window_length({"kind": kind, "window_minutes": 60}) == 60


def test_a_no_reset_claude_reading_is_fresh_within_its_window() -> None:
    now = datetime.now(timezone.utc)
    window = {"kind": "five_hour", "used_percent": 100, "observed_at": (now - timedelta(hours=4)).isoformat()}
    assert not window_stale(window, now)
    assert window_stale({**window, "observed_at": (now - timedelta(hours=6)).isoformat()}, now)


def test_a_reading_without_reset_drops_a_reset_that_passed_before_it() -> None:
    now = datetime.now(timezone.utc)
    stored = {"kind": "five_hour", "used_percent": 50, "resets_at": (now - timedelta(hours=1)).isoformat()}
    merged = merged_window(dict(stored), {"kind": "five_hour", "used_percent": 100, "observed_at": now.isoformat()})
    assert "resets_at" not in merged and merged["used_percent"] == 100

    future = {**stored, "resets_at": (now + timedelta(hours=1)).isoformat()}
    kept = merged_window(dict(future), {"kind": "five_hour", "used_percent": 100, "observed_at": now.isoformat()})
    assert kept["resets_at"] == future["resets_at"]


def test_stored_plan_verdict_without_reset_has_no_deadline(isolated_db, no_stops) -> None:
    db = get_db()
    harness_usage.record_usage(provider="claude-code", windows=[_no_reset()], db=db, receipt=default_receipt("claude-code", db=db))
    verdict = verdicts(harness_usage.stored_usage(provider="claude-code", db=db, account_id=default_id("claude-code", db=db)))["null"]
    assert verdict["state"] == "limited" and verdict["resets_at"] is None


@pytest.mark.parametrize("runtime_commits_first", [False, True])
def test_plan_limit_stop_keeps_the_plan_wake_armed_in_either_commit_order(
    isolated_db, quiet, runtime_commits_first,
) -> None:
    from core.turn_core import persist_turn_settlement
    from ws.stop_control import _commit_stop_terminal

    db = get_db()
    _claimed(db, "thread")
    _arm_wake(db, "thread")
    if runtime_commits_first:
        persist_turn_settlement("turn-thread", "cancelled", PlanLimitStop(), db=db)

    status, applied = _commit_stop_terminal("user", "thread", "turn-thread", db=db, cause=PlanLimitStop())

    assert status == "cancelled"
    assert (applied is None) is runtime_commits_first
    assert _wake_status(db, "thread") == "armed"


def test_user_stop_still_cancels_the_plan_wake(isolated_db, quiet) -> None:
    from ws.stop_control import _commit_stop_terminal

    db = get_db()
    _claimed(db, "thread")
    _arm_wake(db, "thread")

    _commit_stop_terminal("user", "thread", "turn-thread", db=db)

    assert _wake_status(db, "thread") == "cancelled"


def test_plan_limit_stop_of_a_delegated_child_sends_the_parent_nothing(isolated_db, quiet) -> None:
    from core.turn_core import persist_turn_settlement

    db = get_db()
    db.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
        "VALUES ('parent','user','claude-code/sonnet','p','p')"
    )
    _claimed(db, "child", spawned_by="parent")
    db.execute(
        "INSERT INTO messages (id,thread_id,role,content,metadata,created_at) "
        "VALUES ('partial','child','assistant','half an answer',?, 't')",
        ('{"turn_id": "turn-child"}',),
    )
    db.execute("UPDATE messages SET delivery_state='delivered' WHERE id='m-child'")
    db.commit()
    before = db.execute("SELECT COUNT(*) FROM messages WHERE thread_id='parent'").fetchone()[0]

    persist_turn_settlement("turn-child", "cancelled", PlanLimitStop(), db=db)

    assert db.execute("SELECT COUNT(*) FROM messages WHERE thread_id='parent'").fetchone()[0] == before
    assert db.execute(
        "SELECT COUNT(*) FROM messages WHERE thread_id='child' AND id LIKE 'overage-continue:%'"
    ).fetchone()[0] == 1


@pytest.mark.asyncio
async def test_plan_limit_stop_scheduled_before_any_terminal_is_not_dropped(stop) -> None:
    """A DB worker schedules the Stop before any Turn terminal ever ran on the loop."""
    import infra.app_loop as app_loop
    from infra.connection_limit_drive import schedule_plan_limit_stops

    app_loop.capture(asyncio.get_running_loop())
    try:
        await asyncio.to_thread(schedule_plan_limit_stops, (("thread", "turn"),))
        for _ in range(200):
            if stop.await_count:
                break
            await asyncio.sleep(0.01)
    finally:
        app_loop.clear()

    stop.assert_awaited_once_with("thread", "turn")


def test_plan_limit_stop_without_an_app_loop_is_never_silent(stop) -> None:
    import infra.app_loop as app_loop
    from infra.connection_limit_drive import schedule_plan_limit_stops

    app_loop.clear()
    with pytest.raises(app_loop.AppLoopUnavailable):
        schedule_plan_limit_stops((("thread", "turn"),))
    stop.assert_not_awaited()


@pytest.mark.parametrize('limited', ['default', 'x'])
def test_plan_limit_selects_only_current_frozen_account_and_release_owns_same_subject(accounts, monkeypatch, limited):
    from infra.connection_limit_release import apply_plan_limit_effect

    db, default, x, y = _turns(accounts)
    account, thread = (default, 'd') if limited == 'default' else (x, 'x')
    receipt = capture_receipt('codex', account_id=account['id'], db=db)
    stops, releases = [], []
    monkeypatch.setattr('infra.connection_limit_drive.schedule_plan_limit_stops', stops.append)
    monkeypatch.setattr('infra.connection_limit_drive.schedule_scope_release', releases.append)
    with write_transaction(db, scope='actual_plan_effect'):
        apply_plan_limit_effect('codex', [(None, {})], receipt=receipt, db=db)
    assert stops == [((thread, thread+'-turn'),)]
    harness_usage.record_limited(provider='codex', receipt=receipt_of('codex', account['id'], db=db), reason='synthetic',
        resets_at='2099-01-01T00:00:00+00:00', source='test', db=db)
    other = {row['id']: harness_usage.stored_usage(provider='codex', account_id=row['id'], db=db)
             for row in (default, x, y) if row != account}
    harness_usage.record_allowed(provider='codex', receipt=receipt_of('codex', account['id'], db=db), source='test', db=db)
    assert len(releases) == 1 and releases[0].receipt == receipt
    assert releases[0].account_id == account['id']
    assert {identity: harness_usage.stored_usage(provider='codex', account_id=identity, db=db) for identity in other} == other


def test_same_account_same_reset_new_login_rejects_old_release_probe_and_stop(accounts):
    from connections.vendor_accounts import set_account_state
    from infra.connection_limit_release import LimitProbe, claim_probe, running_turns_on_scope
    from infra.connection_limit_drive import _turn_stop_current

    db, _, x, _ = _turns(accounts)
    old = limit_subject('codex', 'null', receipt=receipt_of('codex', x['id'], db=db))
    reset = '2000-01-01T00:00:00+00:00'
    harness_usage.record_limited(provider='codex', receipt=receipt_of('codex', x['id'], db=db), reason='old', resets_at=reset, source='test', db=db)
    assert running_turns_on_scope(old, db=db) == [('x', 'x-turn')]
    x = set_account_state(x['id'], 'signed_in', expected_revision=x['revision'], db=db)
    _ready(db, x)
    harness_usage.record_limited(provider='codex', receipt=receipt_of('codex', x['id'], db=db), reason='new', resets_at=reset, source='test', db=db)
    before = harness_usage.stored_usage(provider='codex', account_id=x['id'], db=db)
    assert not harness_usage.write_release(old, reset, 'started', at='t', thread_id='x', db=db)
    assert not claim_probe(LimitProbe(old, reset, 'test'), 'x', db=db)
    assert running_turns_on_scope(old, db=db) == [] and not _turn_stop_current('x-turn')
    assert harness_usage.stored_usage(provider='codex', account_id=x['id'], db=db) == before


def test_one_probe_claim_per_actual_account_scope_and_candidates_follow_binding(accounts):
    from infra.connection_limit_release import LimitProbe, candidate_rows, claim_probe
    from infra.input_admission import accept_thread_input
    from vendor_account_fixtures import signed_in_default

    db, default, x, y = accounts
    default = signed_in_default('codex', db=db)
    for thread, row in (('destination', x), ('actor', y)):
        _ready(db, row)
        select_account(thread, 'codex', account_id=row['id'], db=db)
        accept_thread_input(thread, role='user', content='work', intent='apply_now', message_id=thread+'-input', db=db)
    reset = '2000-01-01T00:00:00+00:00'
    for row in (x, y):
        harness_usage.record_limited(provider='codex', receipt=receipt_of('codex', row['id'], db=db), reason='limit', resets_at=reset, source='test', db=db)
    sx, sy = (limit_subject('codex', 'null', receipt=receipt_of('codex', row['id'], db=db)) for row in (x, y))
    assert [row[0] for row in candidate_rows(sx, db=db)] == ['destination']
    assert [row[0] for row in candidate_rows(sy, db=db)] == ['actor']
    assert not candidate_rows(limit_subject('codex', 'null', receipt=receipt_of('codex', default['id'], db=db)), db=db)
    assert claim_probe(LimitProbe(sx, reset, 'test'), 'destination', db=db)
    assert not claim_probe(LimitProbe(sx, reset, 'test'), 'destination', db=db)
    assert claim_probe(LimitProbe(sy, reset, 'test'), 'actor', db=db)


def test_sessionless_old_turn_terminal_never_clears_new_same_row_limit(accounts):
    from connections.vendor_accounts import set_account_state
    from infra.terminal_usage_limits import settle_terminal_limits

    db, _, x, _ = _turns(accounts)
    x = set_account_state(x['id'], 'signed_in', expected_revision=x['revision'], db=db)
    _ready(db, x)
    harness_usage.record_limited(provider='codex', receipt=receipt_of('codex', x['id'], db=db), reason='new',
        resets_at='2000-01-01T00:00:00+00:00', source='test', db=db)
    before = harness_usage.stored_usage(provider='codex', account_id=x['id'], db=db)
    with write_transaction(db, scope='sessionless_terminal'):
        result = settle_terminal_limits(db, 'x', 'x-turn', outcome='completed',
            ended_at=datetime.now(timezone.utc).isoformat(), error=None)
    assert result.fail_closed is None
    assert harness_usage.stored_usage(provider='codex', account_id=x['id'], db=db) == before


def test_release_revalidates_binding_in_claim_transaction_without_clearing_y_hold(accounts):
    from infra.input_admission import accept_thread_input
    from infra.thread_inputs import start_idle_turn

    db, _, x, y = accounts
    for row in (x, y):
        _ready(db, row)
    select_account('destination', 'codex', account_id=x['id'], db=db)
    subject = limit_subject('codex', 'null', receipt=receipt_of('codex', x['id'], db=db))
    accept_thread_input('destination', role='user', content='work', intent='apply_now', message_id='race', db=db)
    # Defensive mid-flight binding injection; the public queued rebind still refuses.
    db.execute("UPDATE thread_vendor_accounts SET account_id=? WHERE thread_id='destination' AND provider='codex'", (y['id'],))
    db.execute("UPDATE messages SET delivery_error='account_login_unverified',write_outcome='proved_no_write' WHERE id='race'")
    db.commit()
    before = tuple(db.execute("SELECT delivery_error,write_outcome,delivery_revision FROM messages WHERE id='race'").fetchone())
    assert start_idle_turn('destination', turn_id='old-release', boot_epoch='test', started_at='t',
                           connection_limit_subject=subject, db=db) is None
    assert tuple(db.execute("SELECT delivery_error,write_outcome,delivery_revision FROM messages WHERE id='race'").fetchone()) == before
    assert db.execute("SELECT COUNT(*) FROM active_turns WHERE id='old-release'").fetchone()[0] == 0


def test_restart_due_subjects_keep_default_and_custom_receipts_separate(accounts):
    from infra.connection_limit_drive import _due_releases

    db, default, x, y = accounts
    _known_default(db, default)
    _ready(db, x)
    _ready(db, y)
    reset = '2000-01-01T00:00:00+00:00'
    for row in (default, x):
        harness_usage.record_limited(provider='codex', receipt=receipt_of('codex', row['id'], db=db), reason='limit',
                                     resets_at=reset, source='test', db=db)
    due = _due_releases(datetime.now(timezone.utc))
    assert {(item.subject.account_id, item.subject.receipt) for item in due} == {
        (default['id'], capture_receipt('codex', account_id=default['id'], db=db)),
        (x['id'], capture_receipt('codex', account_id=x['id'], db=db))}
    assert all(item.subject.provider == 'codex' for item in due)
