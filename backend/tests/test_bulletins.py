from __future__ import annotations

import asyncio
import base64
import json
from datetime import date, timedelta

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from infra import bulletin_fetch, bulletin_ledger, bulletin_live, bulletins
from infra.database import get_db, write_transaction
from tests.event_routing_helpers import mk_session
from tools.bulletins import BulletinDispositionTool


def _signed_bulletin(
    *, bulletin_id: str = "test", expires: str | None = None, version: str = ">=0", providers: list[str] | None = None,
    withdrawn: bool = False, body: str = "Body",
) -> tuple[bytes, bytes, str]:
    private = Ed25519PrivateKey.generate()
    public = base64.b64encode(private.public_key().public_bytes_raw()).decode()
    optional = f"providers: {json.dumps(providers)}\n" if providers is not None else ""
    optional += "withdrawn: true\n" if withdrawn else ""
    published = (date.today() - timedelta(days=1)).isoformat()
    expires = expires or (date.today() + timedelta(days=30)).isoformat()
    content = (
        f"---\nid: {bulletin_id}\ntitle: Test\npublished: {published}\n"
        f"expires: {expires}\nsystem_version: '{version}'\n{optional}---\n{body}\n"
    ).encode()
    return content, private.sign(content), public


def _verified(content: bytes, signature: bytes) -> dict | None:
    return bulletin_fetch._verify(content, signature)[0]


def _clear_ledger() -> None:
    db = get_db()
    try:
        with write_transaction(db, scope="test_bulletin_clear"):
            db.execute("DELETE FROM system_bulletins")
            db.execute("DELETE FROM system_bulletin_identity")
            db.execute("DELETE FROM settings WHERE key LIKE 'live_bulletins%'")
            _record_served_turn(db)
    finally:
        db.close()


def _record_served_turn(db) -> None:
    """Make this an install where an agent has run, so a System change is news to it."""
    db.execute(
        "INSERT OR IGNORE INTO threads (id,user_id,title,model,created_at,updated_at) "
        "VALUES ('served-thread','user-1','T','opus','t','t')"
    )
    db.execute(
        "INSERT OR IGNORE INTO active_turns (id,thread_id,execution_epoch,model,status,started_at,boot_epoch) "
        "VALUES ('served-turn','served-thread',0,'opus','completed','t','test')"
    )


def _feed_transport(content: bytes, signature: bytes, *, name: str = "test.md", etag: str = '"v1"') -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/bulletins/index.json"):
            return httpx.Response(200, stream=httpx.ByteStream(json.dumps([name]).encode()), headers={"ETag": etag})
        if request.url.path.endswith(f"/bulletins/{name}"):
            return httpx.Response(200, stream=httpx.ByteStream(content))
        if request.url.path.endswith(f"/bulletins/{name}.sig"):
            return httpx.Response(200, stream=httpx.ByteStream(signature))
        return httpx.Response(404)
    return httpx.MockTransport(handler)


def _unexpected_request(seen: list[httpx.Request]):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(500)
    return handler


def _use_transport(monkeypatch: pytest.MonkeyPatch, transport: httpx.MockTransport) -> None:
    async def fetch(**kwargs):
        return await bulletin_fetch.fetch_live_bulletins(transport=transport, **kwargs)
    monkeypatch.setattr(bulletin_live, "fetch_live_bulletins", fetch)


def _recording_delivery(monkeypatch: pytest.MonkeyPatch, delivered: bool = True) -> list[tuple]:
    calls: list[tuple] = []

    async def send(*args, **kwargs):
        calls.append((args, kwargs))
        return delivered

    monkeypatch.setattr("events.delivery.deliver_to_thread", send)
    return calls


def test_valid_tampered_unsigned_and_wrong_key_are_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    content, signature, public = _signed_bulletin()
    monkeypatch.setattr(bulletins, "PUBLIC_KEYS", (public,))
    verified = _verified(content, signature)
    assert verified is not None and verified["id"] == "test"
    assert _verified(content + b"x", signature) is None
    assert _verified(content, b"") is None
    monkeypatch.setattr(bulletins, "PUBLIC_KEYS", (_signed_bulletin()[2],))
    assert _verified(content, signature) is None


def test_empty_key_list_disables_fetch_without_request(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[httpx.Request] = []
    monkeypatch.setattr(bulletins, "PUBLIC_KEYS", ())
    fetched = asyncio.run(bulletin_live.fetch_live_bulletins(
        transport=httpx.MockTransport(_unexpected_request(seen)),
    ))
    assert fetched.bulletins == []
    assert seen == []


def test_live_index_limit_fits_the_sequential_fetch_deadline() -> None:
    assert bulletins.MAX_INDEX_ENTRIES == 20


def test_fetch_uses_mock_transport_and_bounded_index(monkeypatch: pytest.MonkeyPatch) -> None:
    content, signature, public = _signed_bulletin()
    monkeypatch.setattr(bulletins, "PUBLIC_KEYS", (public,))
    fetched = asyncio.run(bulletin_live.fetch_live_bulletins(
        base_url="https://feed.test/bulletins", transport=_feed_transport(content, signature),
    ))
    assert fetched.complete and [item["id"] for item in fetched.bulletins] == ["test"]


@pytest.mark.asyncio
async def test_verified_live_bulletin_reaches_real_event_delivery(monkeypatch: pytest.MonkeyPatch) -> None:
    content, signature, public = _signed_bulletin(body="Signed delivery evidence")
    monkeypatch.setattr(bulletins, "PUBLIC_KEYS", (public,))
    fields = _verified(content, signature)
    assert fields is not None
    mk_session("conductor")
    received: list[str] = []

    async def send(*args, **_kwargs):
        received.append(args[2])

    monkeypatch.setattr("events.delivery.send_thread_input", send)
    from events.delivery import deliver_to_thread

    assert await deliver_to_thread(
        "conductor", "system.bulletins", "Here are new System bulletins.",
        {"bulletins": [fields]}, delivery_id="live:test", notice=bulletins.NOTICE,
    )
    assert f'"published": "{(date.today() - timedelta(days=1)).isoformat()}"' in received[0]
    assert '"body": "Signed delivery evidence"' in received[0]


@pytest.mark.asyncio
async def test_system_change_reaches_real_event_delivery(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_ledger()
    first = bulletins.SystemIdentity("1", "first", (("a", "1"),))
    second = bulletins.SystemIdentity("2", "second", (("a", "2"),))
    identity = first
    monkeypatch.setattr(bulletins, "system_identity", lambda: identity)
    assert await bulletin_live.check_live_bulletins() is False
    identity = second
    mk_session("system-conductor")
    monkeypatch.setattr("infra.conductor.get_conductor", lambda *_args, **_kwargs: "system-conductor")
    received: list[str] = []

    async def send(*args, **_kwargs):
        received.append(args[2])

    monkeypatch.setattr("events.delivery.send_thread_input", send)
    assert await bulletin_live.check_live_bulletins() is True
    assert received[0].startswith("[EVENT: system.bulletin]")
    assert bulletins.NOTICE in received[0]


def test_oversized_verified_body_is_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    content, signature, public = _signed_bulletin(
        body="x" * (bulletins.MAX_BULLETIN_BODY_BYTES + 1),
    )
    monkeypatch.setattr(bulletins, "PUBLIC_KEYS", (public,))
    fetched = asyncio.run(bulletin_live.fetch_live_bulletins(
        base_url="https://feed.test/bulletins", transport=_feed_transport(content, signature),
    ))
    assert fetched.bulletins == []
    assert fetched.failures == [bulletin_live.FetchFailure("oversized", "test.md")]


def test_fetch_preserves_signed_bulletins_over_the_delivery_batch_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    private = Ed25519PrivateKey.generate()
    public = base64.b64encode(private.public_key().public_bytes_raw()).decode()
    names = [f"{number}.md" for number in range(5)]
    body = "x" * (bulletins.MAX_BULLETIN_BODY_BYTES - 1)
    entries = {
        name: (
            "---\n"
            f"id: {name}\n"
            "title: Test\n"
            f"published: {(date.today() - timedelta(days=1)).isoformat()}\n"
            f"expires: {(date.today() + timedelta(days=30)).isoformat()}\n"
            "system_version: '>=0'\n"
            f"---\n{body}\n"
        ).encode()
        for name in names
    }
    signatures = {name: private.sign(content) for name, content in entries.items()}
    monkeypatch.setattr(bulletins, "PUBLIC_KEYS", (public,))

    def handler(request: httpx.Request) -> httpx.Response:
        name = request.url.path.rsplit("/", 1)[-1]
        if name == "index.json":
            return httpx.Response(200, stream=httpx.ByteStream(json.dumps(names).encode()))
        if name.endswith(".sig"):
            return httpx.Response(200, stream=httpx.ByteStream(signatures[name.removesuffix(".sig")]))
        return httpx.Response(200, stream=httpx.ByteStream(entries[name]))

    fetched = asyncio.run(bulletin_live.fetch_live_bulletins(
        base_url="https://feed.test/bulletins", transport=httpx.MockTransport(handler),
    ))
    assert [item["id"] for item in fetched.bulletins] == names
    assert fetched.failures == []


@pytest.mark.asyncio
async def test_reconcile_delivers_pending_bulletins_in_bounded_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_ledger()
    mk_session("chunk-conductor")
    body = "x" * bulletins.MAX_BULLETIN_BODY_BYTES
    candidates = [
        {
            "id": f"chunk-{number}", "title": "Test", "published": "2026-10-02",
            "expires": "2099-12-02", "system_version": ">=0", "body": body,
        }
        for number in range(5)
    ]
    received: list[str] = []

    async def send(*args, **_kwargs):
        received.append(args[2])

    monkeypatch.setattr("events.delivery.send_thread_input", send)
    db = get_db()
    try:
        fetched = bulletin_fetch.FetchedBulletins(candidates, True, [])
        assert await bulletin_live._reconcile_fetch(fetched, "chunk-conductor")
        assert [message.count('"body":') for message in received] == [4, 1]
        assert db.execute(
            "SELECT count(*) FROM system_bulletins WHERE state='delivered'"
        ).fetchone()[0] == 5
    finally:
        db.close()


@pytest.mark.asyncio
async def test_reconcile_skips_delivered_bodies_before_chunking(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_ledger()
    mk_session("delivered-conductor")
    body = "x" * bulletins.MAX_BULLETIN_BODY_BYTES
    candidates = [
        {
            "id": f"known-{number}", "title": "Test", "published": "2026-10-02",
            "expires": "2099-12-02", "system_version": ">=0", "body": body,
        }
        for number in range(5)
    ]
    db = get_db()
    try:
        with write_transaction(db, scope="test_delivered_bulletins"):
            for fields in candidates[:4]:
                db.execute(
                    "INSERT INTO system_bulletins(id,source,state,delivered_at,updated_at) VALUES(?,?,?,?,?)",
                    (fields["id"], "live", "delivered", "now", "now"),
                )
        received: list[str] = []

        async def send(*args, **_kwargs):
            received.append(args[2])

        monkeypatch.setattr("events.delivery.send_thread_input", send)
        fetched = bulletin_fetch.FetchedBulletins(candidates, True, [])
        assert await bulletin_live._reconcile_fetch(fetched, "delivered-conductor")
        assert len(received) == 1 and '"id": "known-4"' in received[0]
        assert db.execute(
            "SELECT count(*) FROM system_bulletins WHERE state='delivered'"
        ).fetchone()[0] == 5
    finally:
        db.close()


def test_failure_parking_is_deduplicated_per_day() -> None:
    db = get_db()
    try:
        with write_transaction(db, scope="test_bulletin_dead_letter_clear"):
            db.execute("DELETE FROM dead_letter_events WHERE internal_event='system_bulletin.infrastructure'")
        failure = bulletin_live.FetchFailure("network", "test.md")
        bulletin_live._record_failure(db, failure)
        bulletin_live._record_failure(db, failure)
        assert db.execute(
            "SELECT count(*) FROM dead_letter_events WHERE internal_event='system_bulletin.infrastructure'"
        ).fetchone()[0] == 1
    finally:
        db.close()


def test_system_identity_change_lists_paths() -> None:
    current = bulletins.SystemIdentity("v1", "new", (("changed", "new"), ("added", "a")))
    changes = bulletins._identity_changes('[ ["changed", "old"], ["removed", "x"] ]', current)
    assert changes == {"added": ["added"], "changed": ["changed"], "removed": ["removed"]}


def test_fresh_install_baselines_without_notice(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_ledger()
    identity = bulletins.SystemIdentity("1", "first", (("a", "1"),))
    monkeypatch.setattr(bulletins, "system_identity", lambda: identity)
    calls = _recording_delivery(monkeypatch)
    assert asyncio.run(bulletin_live.check_live_bulletins()) is False
    assert calls == []
    db = get_db()
    try:
        assert db.execute("SELECT digest FROM system_bulletin_identity").fetchone()["digest"] == "first"
    finally:
        db.close()


def test_system_change_before_any_turn_becomes_the_baseline(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_ledger()
    db = get_db()
    try:
        with write_transaction(db, scope="test_bulletin_no_turns"):
            db.execute("DELETE FROM active_turns")
    finally:
        db.close()
    first = bulletins.SystemIdentity("1", "first", (("a", "1"),))
    second = bulletins.SystemIdentity("2", "second", (("a", "2"),))
    monkeypatch.setattr(bulletins, "system_identity", lambda: first)
    asyncio.run(bulletin_live.check_live_bulletins())
    monkeypatch.setattr(bulletins, "system_identity", lambda: second)
    monkeypatch.setattr("infra.conductor.get_conductor", lambda *_args, **_kwargs: "conductor")
    calls = _recording_delivery(monkeypatch)
    assert asyncio.run(bulletin_live.check_live_bulletins()) is False
    assert calls == []
    db = get_db()
    try:
        row = db.execute("SELECT digest,reconciled_at FROM system_bulletin_identity").fetchone()
        assert row["digest"] == "second" and row["reconciled_at"] is not None
        assert db.execute(
            "SELECT count(*) FROM system_bulletins WHERE source='system_change' AND state='pending'"
        ).fetchone()[0] == 0
    finally:
        db.close()


def test_changed_system_notifies_once_until_disposition(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_ledger()
    first = bulletins.SystemIdentity("1", "first", (("a", "1"),))
    second = bulletins.SystemIdentity("2", "second", (("a", "2"),))
    monkeypatch.setattr(bulletins, "system_identity", lambda: first)
    asyncio.run(bulletin_live.check_live_bulletins())
    monkeypatch.setattr(bulletins, "system_identity", lambda: second)
    monkeypatch.setattr("infra.conductor.get_conductor", lambda *_args, **_kwargs: "conductor")
    calls = _recording_delivery(monkeypatch)
    assert asyncio.run(bulletin_live.check_live_bulletins()) is True
    assert asyncio.run(bulletin_live.check_live_bulletins()) is False
    assert len(calls) == 1


def test_system_change_is_held_without_a_conductor(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_ledger()
    identities = iter((
        bulletins.SystemIdentity("1", "first", (("a", "1"),)),
        bulletins.SystemIdentity("2", "second", (("a", "2"),)),
    ))
    monkeypatch.setattr(bulletins, "system_identity", lambda: next(identities, bulletins.SystemIdentity("2", "second", (("a", "2"),))))
    assert asyncio.run(bulletin_live.check_live_bulletins()) is False
    assert asyncio.run(bulletin_live.check_live_bulletins()) is False
    monkeypatch.setattr("infra.conductor.get_conductor", lambda *_args, **_kwargs: "conductor")
    calls = _recording_delivery(monkeypatch)
    assert asyncio.run(bulletin_live.check_live_bulletins()) is True
    assert len(calls) == 1


def test_new_system_change_supersedes_an_older_pending_row(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_ledger()
    first = bulletins.SystemIdentity("1", "first", (("a", "1"),))
    second = bulletins.SystemIdentity("2", "second", (("a", "2"),))
    third = bulletins.SystemIdentity("3", "third", (("a", "3"),))
    identity = first
    monkeypatch.setattr(bulletins, "system_identity", lambda: identity)
    assert asyncio.run(bulletin_live.check_live_bulletins()) is False
    identity = second
    assert asyncio.run(bulletin_live.check_live_bulletins()) is False
    identity = third
    mk_session("supersede-conductor")
    monkeypatch.setattr("infra.conductor.get_conductor", lambda *_args, **_kwargs: "supersede-conductor")
    _recording_delivery(monkeypatch)
    assert asyncio.run(bulletin_live.check_live_bulletins()) is True
    db = get_db()
    try:
        rows = {
            row["id"]: (row["state"], row["disposition"])
            for row in db.execute(
                "SELECT id,state,disposition FROM system_bulletins WHERE source='system_change'",
            )
        }
        assert rows["system-change:second"] == ("skipped", "superseded by system-change:third")
        assert rows["system-change:third"][0] == "delivered"
        assert bulletin_ledger.has_pending_system_change(db) is False
    finally:
        db.close()


def test_acknowledging_older_system_change_leaves_current_one_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_ledger()
    first = bulletins.SystemIdentity("1", "first", (("a", "1"),))
    second = bulletins.SystemIdentity("2", "second", (("a", "2"),))
    third = bulletins.SystemIdentity("3", "third", (("a", "3"),))
    identity = first
    monkeypatch.setattr(bulletins, "system_identity", lambda: identity)
    assert asyncio.run(bulletin_live.check_live_bulletins()) is False
    identity = second
    mk_session("ack-conductor")
    monkeypatch.setattr("infra.conductor.get_conductor", lambda *_args, **_kwargs: "ack-conductor")
    _recording_delivery(monkeypatch)
    assert asyncio.run(bulletin_live.check_live_bulletins()) is True
    identity = third
    monkeypatch.setattr("infra.conductor.get_conductor", lambda *_args, **_kwargs: None)
    assert asyncio.run(bulletin_live.check_live_bulletins()) is False
    monkeypatch.setattr("infra.conductor.get_conductor", lambda *_args, **_kwargs: "ack-conductor")
    tool = BulletinDispositionTool()
    assert '"recorded": true' in asyncio.run(
        tool.execute("system-change:second", "acted", "reviewed", _thread_id="ack-conductor"),
    )
    db = get_db()
    try:
        assert db.execute(
            "SELECT digest FROM system_bulletin_identity WHERE id=1",
        ).fetchone()["digest"] == "first"
        assert bulletin_ledger.delivery_state(db, "system-change:third") == "pending"
    finally:
        db.close()
    calls = _recording_delivery(monkeypatch)
    assert asyncio.run(bulletin_live.check_live_bulletins()) is True
    assert len(calls) == 1


def test_reverting_to_a_superseded_system_change_reclaims_it(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_ledger()
    first = bulletins.SystemIdentity("1", "first", (("a", "1"),))
    second = bulletins.SystemIdentity("2", "second", (("a", "2"),))
    third = bulletins.SystemIdentity("3", "third", (("a", "3"),))
    identity = first
    monkeypatch.setattr(bulletins, "system_identity", lambda: identity)
    assert asyncio.run(bulletin_live.check_live_bulletins()) is False
    identity = second
    assert asyncio.run(bulletin_live.check_live_bulletins()) is False
    identity = third
    assert asyncio.run(bulletin_live.check_live_bulletins()) is False
    identity = second
    mk_session("revert-conductor")
    monkeypatch.setattr("infra.conductor.get_conductor", lambda *_args, **_kwargs: "revert-conductor")
    _recording_delivery(monkeypatch)
    assert asyncio.run(bulletin_live.check_live_bulletins()) is True
    db = get_db()
    try:
        assert bulletin_ledger.delivery_state(db, "system-change:second") == "delivered"
    finally:
        db.close()


def test_pending_system_change_does_not_accelerate_live_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_ledger()
    first = bulletins.SystemIdentity("1", "first", (("a", "1"),))
    second = bulletins.SystemIdentity("2", "second", (("a", "2"),))
    identity = first
    monkeypatch.setattr(bulletins, "system_identity", lambda: identity)
    monkeypatch.setattr(bulletins, "PUBLIC_KEYS", ())
    assert asyncio.run(bulletin_live.check_live_bulletins()) is False
    identity = second
    monkeypatch.setattr(bulletins, "PUBLIC_KEYS", ("test-key",))
    monkeypatch.setattr("infra.conductor.get_conductor", lambda *_args, **_kwargs: "conductor")
    _recording_delivery(monkeypatch, delivered=False)
    fetches = []

    async def fetch(**_kwargs):
        fetches.append(True)
        return bulletin_fetch.FetchedBulletins([], True, [])

    monkeypatch.setattr(bulletin_live, "fetch_live_bulletins", fetch)
    assert asyncio.run(bulletin_live.check_live_bulletins()) is False
    assert asyncio.run(bulletin_live.check_live_bulletins()) is False
    assert fetches == [True]


def test_live_delivery_is_batched_and_idempotent_across_restart(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_ledger()
    one, sig_one, public = _signed_bulletin(bulletin_id="one")
    two, sig_two, _ = _signed_bulletin(bulletin_id="two")
    # Use a single key to sign both entries for this public-feed fixture.
    private = Ed25519PrivateKey.generate()
    public = base64.b64encode(private.public_key().public_bytes_raw()).decode()
    one = one.replace(b"id: one", b"id: one")
    sig_one, sig_two = private.sign(one), private.sign(two)
    monkeypatch.setattr(bulletins, "PUBLIC_KEYS", (public,))
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/bulletins/index.json"):
            return httpx.Response(200, stream=httpx.ByteStream(b'["one.md", "two.md"]'), headers={"ETag": '"batch"'})
        if request.url.path.endswith("/bulletins/one.md"):
            return httpx.Response(200, stream=httpx.ByteStream(one))
        if request.url.path.endswith("/bulletins/two.md"):
            return httpx.Response(200, stream=httpx.ByteStream(two))
        if request.url.path.endswith("/bulletins/one.md.sig"):
            return httpx.Response(200, stream=httpx.ByteStream(sig_one))
        return httpx.Response(200, stream=httpx.ByteStream(sig_two))
    _use_transport(monkeypatch, httpx.MockTransport(handler))
    monkeypatch.setattr("infra.conductor.get_conductor", lambda *_args, **_kwargs: "conductor")
    calls = _recording_delivery(monkeypatch)
    assert asyncio.run(bulletin_live.check_live_bulletins()) is True
    assert asyncio.run(bulletin_live.check_live_bulletins()) is False
    assert len(calls) == 1 and calls[0][0][2] == "Here are new System bulletins."


def test_expired_and_inapplicable_bulletins_are_ledgered(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_ledger()
    expired, expired_sig, public = _signed_bulletin(bulletin_id="expired", expires="2000-01-01")
    monkeypatch.setattr(bulletins, "PUBLIC_KEYS", (public,))
    _use_transport(monkeypatch, _feed_transport(expired, expired_sig))
    assert asyncio.run(bulletin_live.check_live_bulletins()) is False
    db = get_db()
    try:
        row = db.execute("SELECT state,disposition FROM system_bulletins WHERE id='expired'").fetchone()
        assert dict(row) == {"state": "skipped", "disposition": "expired"}
    finally:
        db.close()


def test_provider_and_version_applicability(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_ledger()
    content, signature, public = _signed_bulletin(bulletin_id="version", version=">=9999")
    monkeypatch.setattr(bulletins, "PUBLIC_KEYS", (public,))
    _use_transport(monkeypatch, _feed_transport(content, signature))
    assert asyncio.run(bulletin_live.check_live_bulletins()) is False
    db = get_db()
    try:
        assert db.execute("SELECT state FROM system_bulletins WHERE id='version'").fetchone()["state"] == "skipped"
    finally:
        db.close()
    _clear_ledger()
    content, signature, public = _signed_bulletin(bulletin_id="provider", providers=["missing"])
    monkeypatch.setattr(bulletins, "PUBLIC_KEYS", (public,))
    _use_transport(monkeypatch, _feed_transport(content, signature))
    assert asyncio.run(bulletin_live.check_live_bulletins()) is False
    db = get_db()
    try:
        assert db.execute("SELECT state FROM system_bulletins WHERE id='provider'").fetchone()["state"] == "skipped"
    finally:
        db.close()


def test_opt_out_stops_fetch_and_network_failure_is_non_blocking(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_ledger()
    seen: list[httpx.Request] = []
    monkeypatch.setattr(bulletins, "PUBLIC_KEYS", (_signed_bulletin()[2],))
    _use_transport(monkeypatch, httpx.MockTransport(_unexpected_request(seen)))
    db = get_db()
    try:
        bulletins.save_live_preferences(db, enabled=False)
    finally:
        db.close()
    assert asyncio.run(bulletin_live.check_live_bulletins()) is False and seen == []
    _clear_ledger()
    _use_transport(monkeypatch, httpx.MockTransport(lambda _request: (_ for _ in ()).throw(httpx.ConnectError("offline"))))
    assert asyncio.run(bulletin_live.check_live_bulletins()) is False


def test_refused_delivery_is_deferred_and_retried_despite_etag(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_ledger()
    content, signature, public = _signed_bulletin()
    monkeypatch.setattr(bulletins, "PUBLIC_KEYS", (public,))
    _use_transport(monkeypatch, _feed_transport(content, signature))
    monkeypatch.setattr("infra.conductor.get_conductor", lambda *_args, **_kwargs: "conductor")
    _recording_delivery(monkeypatch, delivered=False)
    assert asyncio.run(bulletin_live.check_live_bulletins()) is False
    calls = _recording_delivery(monkeypatch, delivered=True)
    assert asyncio.run(bulletin_live.check_live_bulletins()) is False
    assert calls == []
    db = get_db()
    try:
        with write_transaction(db, scope="test_live_fetch_due"):
            bulletins.save_live_runtime_state(db, next_live_at="2000-01-01T00:00:00+00:00")
    finally:
        db.close()
    assert asyncio.run(bulletin_live.check_live_bulletins()) is True
    assert len(calls) == 1


def test_withdrawal_is_only_ledgered_after_delivery(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_ledger()
    content, signature, public = _signed_bulletin(bulletin_id="withdraw", withdrawn=True)
    monkeypatch.setattr(bulletins, "PUBLIC_KEYS", (public,))
    _use_transport(monkeypatch, _feed_transport(content, signature))
    db = get_db()
    try:
        with write_transaction(db, scope="test_delivered"):
            db.execute("INSERT INTO system_bulletins(id,source,state,delivered_at,updated_at) VALUES('withdraw','live','delivered','now','now')")
    finally:
        db.close()
    monkeypatch.setattr("infra.conductor.get_conductor", lambda *_args, **_kwargs: "conductor")
    _recording_delivery(monkeypatch, delivered=False)
    assert asyncio.run(bulletin_live.check_live_bulletins()) is False
    db = get_db()
    try:
        row = db.execute(
            "SELECT state,withdrawn_at FROM system_bulletins WHERE id='withdraw'",
        ).fetchone()
        assert dict(row) == {"state": "delivered", "withdrawn_at": None}
    finally:
        db.close()


def test_disposition_tool_accepts_only_known_or_current_pending_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_ledger()
    monkeypatch.setattr("infra.conductor.get_conductor", lambda *_args, **_kwargs: "conductor")
    tool = BulletinDispositionTool()
    refusal = asyncio.run(tool.execute("anything", "acted", "x", _thread_id="ordinary"))
    assert "Only the designated" in refusal and "no action is needed from you" in refusal
    assert "Unknown bulletin id" in asyncio.run(tool.execute("system-change:stale", "acted", "x", _thread_id="conductor"))
    assert "Unknown bulletin id" in asyncio.run(tool.execute("invented", "acted", "x", _thread_id="conductor"))
    db = get_db()
    try:
        with write_transaction(db, scope="test_known_bulletin"):
            db.execute("INSERT INTO system_bulletins(id,source,state,delivered_at,updated_at) VALUES('known','live','delivered','now','now')")
            db.execute("INSERT INTO system_bulletins(id,source,state,delivered_at,updated_at) VALUES('expired','live','skipped',NULL,'now')")
    finally:
        db.close()
    assert '"recorded": true' in asyncio.run(tool.execute("known", "acted", "reviewed", _thread_id="conductor"))
    skipped = asyncio.run(tool.execute("expired", "acted", "reviewed", _thread_id="conductor"))
    assert "never delivered to you (state: skipped)" in skipped and "ids from a notice you received" in skipped
