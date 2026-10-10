"""Delivery edge cases for System bulletins: dead letters, silent withdrawal, clamping, rollback."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from events.delivery import _event_idempotency_key, enqueue_event_wake
from infra import bulletin_ledger, bulletin_live, bulletins
from infra.database import get_db, write_transaction
from tests.event_routing_helpers import mk_session
from tests.test_bulletins import (
    _clear_ledger, _feed_transport, _recording_delivery, _signed_bulletin, _use_transport,
)
from tools.bulletins import BulletinDispositionTool


def _clear_dead_letters() -> None:
    db = get_db()
    try:
        with write_transaction(db, scope="test_bulletin_clear_dead_letters"):
            db.execute("DELETE FROM dead_letter_events WHERE internal_event='system_bulletin.infrastructure'")
    finally:
        db.close()


def _delivery_dead_letters() -> int:
    db = get_db()
    try:
        return db.execute(
            "SELECT count(*) AS n FROM dead_letter_events "
            "WHERE internal_event='system_bulletin.infrastructure' AND reason='bulletin_delivery'",
        ).fetchone()["n"]
    finally:
        db.close()


def _row_state(bulletin_id: str) -> str | None:
    db = get_db()
    try:
        return bulletin_ledger.delivery_state(db, bulletin_id)
    finally:
        db.close()


def _make_due() -> None:
    db = get_db()
    try:
        with write_transaction(db, scope="test_bulletin_due"):
            bulletins.save_live_runtime_state(db, next_live_at="2000-01-01T00:00:00+00:00")
    finally:
        db.close()


def _set_row(bulletin_id: str, state: str) -> None:
    db = get_db()
    try:
        with write_transaction(db, scope="test_bulletin_row"):
            db.execute(
                "INSERT INTO system_bulletins(id,source,state,delivered_at,updated_at) VALUES(?,?,?,?,?)",
                (bulletin_id, "live", state, "now", "now"),
            )
    finally:
        db.close()


def _start(monkeypatch: pytest.MonkeyPatch, bulletin_id: str, *, withdrawn: bool = False, conductor: str | None = "conductor"):
    _clear_ledger()
    _clear_dead_letters()
    content, signature, public = _signed_bulletin(bulletin_id=bulletin_id, withdrawn=withdrawn)
    monkeypatch.setattr(bulletins, "PUBLIC_KEYS", (public,))
    _use_transport(monkeypatch, _feed_transport(content, signature))
    monkeypatch.setattr("infra.conductor.get_conductor", lambda *_args, **_kwargs: conductor)
    return public


@pytest.mark.parametrize("conductor, delivered", [(None, True), ("conductor", False)])
def test_undelivered_live_bulletin_is_dead_lettered_and_retryable(
    monkeypatch: pytest.MonkeyPatch, conductor: str | None, delivered: bool,
) -> None:
    _start(monkeypatch, "dl-live", conductor=conductor)
    _recording_delivery(monkeypatch, delivered=delivered)
    assert asyncio.run(bulletin_live.check_live_bulletins()) is False
    assert _delivery_dead_letters() == 1
    assert _row_state("dl-live") == "pending"


@pytest.mark.parametrize("conductor, delivered", [(None, True), ("conductor", False)])
def test_undelivered_withdrawal_is_dead_lettered_and_retryable(
    monkeypatch: pytest.MonkeyPatch, conductor: str | None, delivered: bool,
) -> None:
    _start(monkeypatch, "dl-withdraw", withdrawn=True, conductor=conductor)
    _set_row("dl-withdraw", "delivered")
    _recording_delivery(monkeypatch, delivered=delivered)
    assert asyncio.run(bulletin_live.check_live_bulletins()) is False
    assert _delivery_dead_letters() == 1
    assert _row_state("dl-withdraw") == "delivered"


def _accepting_delivery(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Deliver like the real path: durably accept the event under its deterministic key."""
    events: list[str] = []

    async def send(thread_id, internal_event, summary, details=None, *, delivery_id=None, **_kwargs):
        events.append(internal_event)
        enqueue_event_wake(
            thread_id, internal_event, summary, details,
            message_id=_event_idempotency_key(internal_event, summary, details, delivery_id=delivery_id),
        )
        return True

    monkeypatch.setattr("events.delivery.deliver_to_thread", send)
    return events


def _swap_feed(monkeypatch: pytest.MonkeyPatch, bulletin_id: str, *, withdrawn: bool) -> None:
    content, signature, public = _signed_bulletin(bulletin_id=bulletin_id, withdrawn=withdrawn)
    monkeypatch.setattr(bulletins, "PUBLIC_KEYS", (public,))
    _use_transport(monkeypatch, _feed_transport(content, signature))
    _make_due()


def test_withdrawal_after_accepted_dispatch_with_failed_completion_is_announced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _start(monkeypatch, "accepted-then-withdrawn", conductor="edges-conductor")
    mk_session("edges-conductor")
    events = _accepting_delivery(monkeypatch)
    real = bulletin_live._complete_live_delivery

    def fail_once(ids):
        monkeypatch.setattr(bulletin_live, "_complete_live_delivery", real)
        raise RuntimeError("database is locked")

    monkeypatch.setattr(bulletin_live, "_complete_live_delivery", fail_once)
    with pytest.raises(RuntimeError):
        asyncio.run(bulletin_live.check_live_bulletins())
    assert _row_state("accepted-then-withdrawn") == "pending"
    _swap_feed(monkeypatch, "accepted-then-withdrawn", withdrawn=True)
    assert asyncio.run(bulletin_live.check_live_bulletins()) is True
    assert events == ["system.bulletins", "system.bulletin.withdrawn"]
    assert _row_state("accepted-then-withdrawn") == "withdrawn"


def test_withdrawal_of_a_dispatch_that_was_never_accepted_stays_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    _start(monkeypatch, "refused-then-withdrawn")
    calls = _recording_delivery(monkeypatch, delivered=False)
    assert asyncio.run(bulletin_live.check_live_bulletins()) is False
    db = get_db()
    try:
        assert bulletin_ledger.delivery_id(db, "refused-then-withdrawn") is not None
    finally:
        db.close()
    _swap_feed(monkeypatch, "refused-then-withdrawn", withdrawn=True)
    calls.clear()
    assert asyncio.run(bulletin_live.check_live_bulletins()) is False
    assert calls == []
    assert _row_state("refused-then-withdrawn") == "withdrawn"


def test_withdrawal_of_an_undispatched_claim_stays_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    _start(monkeypatch, "claimed-then-withdrawn", withdrawn=True)
    db = get_db()
    try:
        with write_transaction(db, scope="test_bulletin_claim"):
            bulletin_ledger.claim_delivery(db, "claimed-then-withdrawn", "live")
    finally:
        db.close()
    calls = _recording_delivery(monkeypatch)
    assert asyncio.run(bulletin_live.check_live_bulletins()) is False
    assert calls == []
    assert _row_state("claimed-then-withdrawn") == "withdrawn"


def test_live_fetch_delay_is_clamped_to_one_day() -> None:
    now = datetime.now(timezone.utc)
    far = {"next_live_at": (now + timedelta(days=365)).isoformat()}
    assert bulletin_live._seconds_until_live_fetch(far, now) == bulletin_live._DAILY_SECONDS
    soon = {"next_live_at": (now + timedelta(hours=1)).isoformat()}
    assert 0 < bulletin_live._seconds_until_live_fetch(soon, now) <= 3600


def test_rollback_to_an_acknowledged_system_identity_is_announced(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_ledger()
    mk_session("rollback-conductor")
    monkeypatch.setattr("infra.conductor.get_conductor", lambda *_args, **_kwargs: "rollback-conductor")
    identities = {name: bulletins.SystemIdentity(name, name, (("a", name),)) for name in ("a0", "a1", "a2")}
    current = identities["a0"]
    monkeypatch.setattr(bulletins, "system_identity", lambda: current)
    calls = _recording_delivery(monkeypatch)
    tool = BulletinDispositionTool()
    asyncio.run(bulletin_live.check_live_bulletins())
    for name in ("a1", "a2"):
        current = identities[name]
        assert asyncio.run(bulletin_live.check_live_bulletins()) is True
        assert '"recorded": true' in asyncio.run(
            tool.execute(f"system-change:{name}", "acted", f"adapted to {name}", _thread_id="rollback-conductor"),
        )
    current = identities["a1"]
    assert asyncio.run(bulletin_live.check_live_bulletins()) is True
    assert [call[0][3]["id"] for call in calls] == ["system-change:a1", "system-change:a2", "system-change:a1"]
    assert asyncio.run(bulletin_live.check_live_bulletins()) is False
    assert '"recorded": true' in asyncio.run(
        tool.execute("system-change:a1", "acted", "rolled back", _thread_id="rollback-conductor"),
    )
    db = get_db()
    try:
        assert db.execute("SELECT digest FROM system_bulletin_identity WHERE id=1").fetchone()["digest"] == "a1"
    finally:
        db.close()
