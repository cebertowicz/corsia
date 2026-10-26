"""Per-connector webhook receivers (event-rules design §5.3, R31)."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
import uuid
from unittest.mock import patch

import httpx
import pytest

from webhook_ingress_fixtures import (  # noqa: F401
    WEBHOOK_SECRET,
    _isolated_db,
    _make_signature,
    _pr_opened_payload,
)

from events.connectors import (
    _REGISTRY,
    ConnectorManifest,
    EventTypeDecl,
    register,
)
from events.connectors.receiver import ReceiverDecl, ReceiverDelivery, ReceiverOutcome

SECRETS = {"alpha": "alpha-secret-value", "beta": "beta-secret-value", "github": WEBHOOK_SECRET}


def _sign(body: bytes, secret: str) -> str:
    return _make_signature(body, secret)


def _test_manifest(name: str, translator, *, max_body_bytes: int = 1024 * 1024) -> ConnectorManifest:
    return ConnectorManifest(
        connector=name,
        namespace=name,
        version="1",
        event_types={"ping.seen": EventTypeDecl("ping.seen", ("id",), ("id",))},
        classify=lambda raw: "ping.seen",
        extract_facts=lambda raw, routed: {},
        build_artifact_ref=lambda event_type, facts, raw: None,
        receiver=ReceiverDecl(
            translator=translator,
            signature_header="x-test-signature",
            signature_prefix="sha256=",
            delivery_id_header="x-test-delivery",
            max_body_bytes=max_body_bytes,
        ),
    )


@pytest.fixture()
def receivers(_isolated_db, monkeypatch):
    """Two in-tree test connectors, each with its own secret, and what their translators saw."""
    seen: list[ReceiverDelivery] = []

    async def translate(delivery: ReceiverDelivery) -> ReceiverOutcome:
        seen.append(delivery)
        return ReceiverOutcome()

    monkeypatch.setattr("api.webhook_receivers.receiver_secret", lambda connector: SECRETS.get(connector))
    for name in ("alpha", "beta"):
        register(_test_manifest(name, translate))
    yield seen
    for name in ("alpha", "beta"):
        _REGISTRY.pop(name, None)


@pytest.fixture()
def client():
    from main import app

    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", follow_redirects=False,
    )


def _headers(body: bytes, secret: str, delivery_id: str | None = None) -> dict[str, str]:
    return {
        "x-test-signature": _sign(body, secret),
        "x-test-delivery": delivery_id or str(uuid.uuid4()),
        "content-type": "application/json",
    }


@pytest.mark.asyncio
async def test_each_connector_verifies_only_its_own_secret(receivers, client):
    body = json.dumps({"id": 1}).encode()
    async with client:
        own = await client.post("/webhooks/alpha/", content=body, headers=_headers(body, SECRETS["alpha"]))
        other = await client.post("/webhooks/alpha/", content=body, headers=_headers(body, SECRETS["beta"]))
        beta_own = await client.post("/webhooks/beta", content=body, headers=_headers(body, SECRETS["beta"]))
        beta_other = await client.post("/webhooks/beta", content=body, headers=_headers(body, SECRETS["alpha"]))
        sub = await client.post("/webhooks/beta/events/x", content=body, headers=_headers(body, SECRETS["beta"]))
    assert (own.status_code, other.status_code) == (202, 401)
    assert (beta_own.status_code, beta_other.status_code) == (202, 401)
    assert sub.status_code == 202
    assert [d.connector for d in receivers] == ["alpha", "beta", "beta"]
    assert receivers[2].subpath == "events/x"


@pytest.mark.asyncio
async def test_unknown_connector_and_connector_without_receiver_get_404(receivers, client):
    body = b"{}"
    async with client:
        unknown = await client.post("/webhooks/nope/", content=body, headers=_headers(body, "x"))
        no_receiver = await client.post("/webhooks/document/", content=body, headers=_headers(body, "x"))
    assert (unknown.status_code, no_receiver.status_code) == (404, 404)
    assert receivers == []


@pytest.mark.asyncio
async def test_missing_secret_or_signature_gets_401_and_no_translator_runs(receivers, client, monkeypatch):
    body = b"{}"
    async with client:
        unsigned = await client.post("/webhooks/alpha/", content=body)
        monkeypatch.setattr("api.webhook_receivers.receiver_secret", lambda connector: None)
        no_secret = await client.post("/webhooks/alpha/", content=body, headers=_headers(body, "x"))
    assert (unsigned.status_code, no_secret.status_code) == (401, 401)
    assert receivers == []


@pytest.mark.asyncio
async def test_oversized_body_gets_413_before_a_signature_is_checked(_isolated_db, monkeypatch, client):
    async def translate(delivery):
        raise AssertionError("an oversized body must never reach a translator")

    monkeypatch.setattr("api.webhook_receivers.receiver_secret", lambda connector: SECRETS.get(connector))
    register(_test_manifest("alpha", translate, max_body_bytes=64))
    try:
        body = b"x" * 65

        async def chunks():
            yield b"x" * 40
            yield b"x" * 40

        async with client:
            declared = await client.post("/webhooks/alpha/", content=body, headers=_headers(body, SECRETS["alpha"]))
            chunked = await client.post(
                "/webhooks/alpha/",
                content=chunks(),
                headers=_headers(body, SECRETS["alpha"]),
            )
    finally:
        _REGISTRY.pop("alpha", None)
    assert declared.status_code == 413
    assert chunked.status_code == 413


def test_receiver_defaults_and_github_limit():
    from events.connectors.github_receiver import GITHUB_RECEIVER
    from events.connectors.receiver import DEFAULT_BODY_LIMIT, SIGNATURE_SCHEMES

    assert DEFAULT_BODY_LIMIT == 1024 * 1024
    assert GITHUB_RECEIVER.max_body_bytes == 25 * 1024 * 1024
    assert SIGNATURE_SCHEMES[0] == "hmac_sha256"
    assert (GITHUB_RECEIVER.signature_header, GITHUB_RECEIVER.signature_prefix) == (
        "x-hub-signature-256", "sha256=",
    )
    with pytest.raises(ValueError):
        ReceiverDecl(translator=None, signature_header="x", signature_prefix="", delivery_id_header="y",
                     signature_scheme="rot13")


def _post_github(client, payload: dict, delivery_id: str, path: str = "/webhooks/github"):
    body = json.dumps(payload).encode()
    return client.post(
        path,
        content=body,
        headers={
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": delivery_id,
            "X-Hub-Signature-256": _make_signature(body),
            "Content-Type": "application/json",
        },
    )


async def _no_route(internal_event, payload, delivery_id=None):
    return None


@pytest.mark.asyncio
async def test_redelivered_github_delivery_id_is_dropped(_isolated_db, monkeypatch, client):
    monkeypatch.setattr("api.webhook_receivers.receiver_secret", lambda connector: WEBHOOK_SECRET)
    with patch("events.ingress.route_leg", side_effect=_no_route), patch("events.ingress.review_leg"):
        async with client:
            first = await _post_github(client, _pr_opened_payload(), "gh-delivery-1")
            again = await _post_github(client, _pr_opened_payload(), "gh-delivery-1")
    assert first.status_code == 200 and first.json()["status"] == "ok"
    assert (again.status_code, again.json()["status"]) == (200, "duplicate")


@pytest.mark.asyncio
async def test_legacy_github_paths_answer_2xx_with_no_redirect(_isolated_db, monkeypatch, client):
    monkeypatch.setattr("api.webhook_receivers.receiver_secret", lambda connector: WEBHOOK_SECRET)
    with patch("events.ingress.route_leg", side_effect=_no_route), patch("events.ingress.review_leg"):
        async with client:
            plain = await _post_github(client, _pr_opened_payload(), "gh-plain", "/webhooks/github")
            slash = await _post_github(client, _pr_opened_payload(pr_number=43), "gh-slash", "/webhooks/github/")
    for response in (plain, slash):
        assert 200 <= response.status_code < 300
        assert "location" not in response.headers


def _hold_writer(db_path, acquired: threading.Event, seconds: float) -> threading.Thread:
    def hold() -> None:
        conn = sqlite3.connect(str(db_path), timeout=0, isolation_level=None)
        conn.execute("BEGIN IMMEDIATE")
        acquired.set()
        time.sleep(seconds)
        conn.execute("ROLLBACK")
        conn.close()

    thread = threading.Thread(target=hold, daemon=True)
    thread.start()
    return thread


@pytest.mark.asyncio
async def test_signed_delivery_answers_within_10s_while_the_writer_is_held_15s(_isolated_db, monkeypatch, client):
    monkeypatch.setattr("api.webhook_receivers.receiver_secret", lambda connector: WEBHOOK_SECRET)
    acquired = threading.Event()
    announced: list[tuple] = []
    monkeypatch.setattr(
        "events.connectors.github_admission.emit_product_event",
        lambda category, action, **kw: announced.append((category, action)),
    )
    holder = _hold_writer(_isolated_db, acquired, 15.0)
    assert acquired.wait(5.0)
    with patch("events.ingress.route_leg", side_effect=_no_route), patch("events.ingress.review_leg"):
        async with client:
            started = time.monotonic()
            response = await _post_github(client, _pr_opened_payload(), "gh-held")
            elapsed = time.monotonic() - started
            assert 200 <= response.status_code < 300
            assert elapsed < 10.0
            assert response.json()["status"] == "accepted"
            await asyncio.to_thread(holder.join)

            def recorded() -> str | None:
                conn = sqlite3.connect(str(_isolated_db))
                try:
                    row = conn.execute(
                        "SELECT status FROM webhook_deliveries WHERE delivery_id=?", ("gh-held",),
                    ).fetchone()
                finally:
                    conn.close()
                return row[0] if row else None

            for _ in range(100):
                if recorded() == "dispatched":
                    break
                await asyncio.sleep(0.2)
    assert recorded() == "dispatched"
    assert announced.count(("webhook", "received")) == 1, "a re-run of the record step must not re-announce"


@pytest.mark.asyncio
async def test_slow_admission_is_answered_202_and_finishes_after_the_request(_isolated_db, monkeypatch, client):
    finished = asyncio.Event()

    async def translate(delivery):
        await asyncio.sleep(0.3)
        finished.set()
        return ReceiverOutcome(200, {"status": "ok"})

    monkeypatch.setattr("api.webhook_receivers.receiver_secret", lambda connector: SECRETS.get(connector))
    monkeypatch.setattr("api.webhook_receivers.ADMIT_WAIT_SECONDS", 0.05)
    register(_test_manifest("alpha", translate))
    try:
        body = b"{}"
        async with client:
            response = await client.post("/webhooks/alpha/", content=body, headers=_headers(body, SECRETS["alpha"]))
            assert response.status_code == 202
            assert not finished.is_set()
            await asyncio.wait_for(finished.wait(), 5)
    finally:
        _REGISTRY.pop("alpha", None)


@pytest.mark.asyncio
async def test_the_secret_reaches_no_translator_event_log_or_row(receivers, client, caplog):
    caplog.set_level("DEBUG")
    body = json.dumps({"id": 1}).encode()
    async with client:
        await client.post("/webhooks/alpha/", content=body, headers=_headers(body, SECRETS["alpha"]))
        await client.post("/webhooks/alpha/", content=body, headers=_headers(body, SECRETS["beta"]))
        await client.post("/webhooks/alpha/", content=body)
    secret = SECRETS["alpha"]
    delivery = receivers[0]
    assert secret not in repr(delivery)
    assert "x-test-signature" not in delivery.headers
    assert all(secret not in value for value in delivery.headers.values())
    assert secret not in caplog.text and SECRETS["beta"] not in caplog.text
    from infra.database import get_db

    rows = get_db().execute("SELECT * FROM webhook_deliveries").fetchall()
    assert secret not in repr([tuple(row) for row in rows])


def _route_table(routes, prefix: str = ""):
    """Every (full path, route), through the included routers FastAPI nests."""
    for route in routes:
        nested = getattr(route, "original_router", None)
        if nested is not None:
            context_prefix = getattr(getattr(route, "include_context", None), "prefix", "")
            yield from _route_table(nested.routes, prefix + context_prefix)
        elif hasattr(route, "path"):
            yield prefix + route.path, route


@pytest.mark.asyncio
async def test_every_webhooks_route_verifies_a_signature_first(receivers, client, monkeypatch):
    from main import app

    monkeypatch.setattr("api.webhook_receivers.receiver_secret", lambda connector: WEBHOOK_SECRET)
    routes = [(path, route) for path, route in _route_table(app.routes) if path.startswith("/webhooks")]
    assert len(routes) >= 4
    async with client:
        for route_path, route in routes:
            assert route.methods == {"POST"}, route_path
            assert route.endpoint.__module__ == "api.webhooks", route_path
            for connector in ("github", "alpha", "nope"):
                path = route_path.replace("{connector}", connector).replace("{subpath:path}", "a/b")
                if "{" in path or (route_path.startswith("/webhooks/github") and connector != "github"):
                    continue
                response = await client.post(path, content=b'{"x": 1}')
                assert response.status_code in (401, 404), (path, response.status_code)
    assert receivers == []


@pytest.mark.asyncio
async def test_only_a_code_owned_webhook_entry_is_a_secret(_isolated_db, tmp_path, monkeypatch, client):
    from infra import key_store

    monkeypatch.setattr("infra.config.STATE_ROOT", tmp_path)
    seen: list[ReceiverDelivery] = []

    async def translate(delivery: ReceiverDelivery) -> ReceiverOutcome:
        seen.append(delivery)
        return ReceiverOutcome()

    for name in ("alpha", "othertool"):
        register(_test_manifest(name, translate))
    try:
        key_store.set_managed_value("webhook-alpha", SECRETS["alpha"])
        # An entry an agent can add under the old `<connector>-webhook` form is never read.
        key_store.add_entry(
            "othertool-webhook", "Forged", "", added_by_thread_id=None, added_by_name=None,
        )
        key_store.set_value("othertool-webhook", "forged-secret-value")
        body = b"{}"
        async with client:
            real = await client.post("/webhooks/alpha/", content=body, headers=_headers(body, SECRETS["alpha"]))
            upper = await client.post("/webhooks/ALPHA/", content=body, headers=_headers(body, SECRETS["alpha"]))
            forged = await client.post(
                "/webhooks/othertool/", content=body, headers=_headers(body, "forged-secret-value"),
            )
    finally:
        for name in ("alpha", "othertool"):
            _REGISTRY.pop(name, None)
    assert (real.status_code, upper.status_code, forged.status_code) == (202, 202, 401)
    assert [d.connector for d in seen] == ["alpha", "alpha"]


def test_the_receiver_secret_reader(_isolated_db, tmp_path, monkeypatch):
    from api.webhook_receivers import receiver_secret
    from infra import key_store

    monkeypatch.setattr("infra.config.STATE_ROOT", tmp_path)
    key_store.set_value("github-webhook", "gh-secret-value")
    key_store.set_managed_value("webhook-slack", "slack-secret-value")
    assert receiver_secret("github") == "gh-secret-value"
    assert receiver_secret("slack") == "slack-secret-value"
    for bad in ("", "Slack", "9slack", "sl ack", "../x", "x" * 40, "unset"):
        assert receiver_secret(bad) is None


@pytest.mark.asyncio
async def test_a_failure_after_the_202_is_reported_without_its_text(_isolated_db, monkeypatch, client):
    events: list[tuple[str, str, dict]] = []
    finished = asyncio.Event()

    async def translate(delivery):
        await asyncio.sleep(0.2)
        try:
            raise RuntimeError("a value from the delivery body")
        finally:
            asyncio.get_running_loop().call_later(0.05, finished.set)

    monkeypatch.setattr("api.webhook_receivers.receiver_secret", lambda connector: SECRETS.get(connector))
    monkeypatch.setattr("api.webhook_receivers.ADMIT_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(
        "api.webhook_receivers.emit_product_event",
        lambda category, action, **kw: events.append((category, action, kw.get("properties", {}))),
    )
    register(_test_manifest("alpha", translate))
    try:
        body = b"{}"
        async with client:
            response = await client.post(
                "/webhooks/alpha/", content=body, headers=_headers(body, SECRETS["alpha"], "late-1"),
            )
            assert response.status_code == 202
            await asyncio.wait_for(finished.wait(), 5)
    finally:
        _REGISTRY.pop("alpha", None)
    failed = [props for category, action, props in events if action == "admission_failed"]
    assert failed == [{"connector": "alpha", "delivery_id": "late-1", "error": "RuntimeError"}]


@pytest.mark.asyncio
async def test_a_fault_inside_the_deadline_answers_500_and_no_late_event(_isolated_db, monkeypatch):
    events: list[str] = []

    async def translate(delivery):
        raise RuntimeError("boom")

    monkeypatch.setattr("api.webhook_receivers.receiver_secret", lambda connector: SECRETS.get(connector))
    monkeypatch.setattr(
        "api.webhook_receivers.emit_product_event", lambda category, action, **kw: events.append(action),
    )
    register(_test_manifest("alpha", translate))
    try:
        from main import app

        body = b"{}"
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            response = await c.post("/webhooks/alpha/", content=body, headers=_headers(body, SECRETS["alpha"]))
            await asyncio.sleep(0.1)
    finally:
        _REGISTRY.pop("alpha", None)
    assert response.status_code == 500
    assert "admission_failed" not in events


@pytest.mark.asyncio
async def test_the_record_wait_retries_only_a_lock_refusal_and_only_until_its_deadline(monkeypatch):
    from events.connectors import github_writer_wait as wait

    monkeypatch.setattr(wait, "_PAUSE_SECONDS", 0.01)
    firsts: list[bool] = []

    def flaky(refusals: int, error: Exception):
        async def attempt(first: bool) -> str:
            firsts.append(first)
            if len(firsts) <= refusals:
                raise error
            return "recorded"
        return attempt

    locked = sqlite3.OperationalError("database is locked")
    assert await wait.record_when_writer_free(flaky(2, locked), "d-1") == "recorded"
    assert firsts == [True, False, False], "a re-run never repeats the first attempt's side effects"

    firsts.clear()
    with pytest.raises(sqlite3.OperationalError, match="disk I/O"):
        await wait.record_when_writer_free(flaky(1, sqlite3.OperationalError("disk I/O error")), "d-2")
    assert firsts == [True], "only a lock refusal is waited out"

    firsts.clear()
    monkeypatch.setattr(wait, "RECORD_WAIT_SECONDS", 0.05)
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        await wait.record_when_writer_free(flaky(10**6, locked), "d-3")
    assert len(firsts) > 1, "it waited, then gave up at the deadline"
