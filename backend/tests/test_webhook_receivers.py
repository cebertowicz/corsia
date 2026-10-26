"""Per-connector webhook receivers (event-rules design §5.3, R31)."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
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
from infra import webhook_delivery_log

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
async def test_an_agent_added_entry_is_never_a_receiver_secret(_isolated_db, tmp_path, monkeypatch, client):
    from api.webhook_receivers import receiver_secret
    from infra import key_store

    monkeypatch.setattr("infra.config.STATE_ROOT", tmp_path)
    seen: list[ReceiverDelivery] = []

    async def translate(delivery: ReceiverDelivery) -> ReceiverOutcome:
        seen.append(delivery)
        return ReceiverOutcome()

    register(_test_manifest("othertool", translate))
    try:
        forged = {}
        async with client:
            for name in ("othertool-webhook", "webhook-othertool"):
                key_store.add_entry(name, "Forged", "", added_by_thread_id=None, added_by_name=None)
                key_store.set_value(name, "forged-" + name)
                body = b"{}"
                forged[name] = await client.post(
                    "/webhooks/othertool/", content=body, headers=_headers(body, "forged-" + name),
                )
        key_store.set_value("github-webhook", "gh-secret-value")
        assert receiver_secret("github") == "gh-secret-value"
        assert receiver_secret("othertool") is None
    finally:
        _REGISTRY.pop("othertool", None)
    assert {name: r.status_code for name, r in forged.items()} == {
        "othertool-webhook": 401, "webhook-othertool": 401,
    }
    assert seen == []


@pytest.mark.asyncio
async def test_a_fault_in_admission_answers_500(_isolated_db, monkeypatch):
    async def translate(delivery):
        raise RuntimeError("boom")

    monkeypatch.setattr("api.webhook_receivers.receiver_secret", lambda connector: SECRETS.get(connector))
    register(_test_manifest("alpha", translate))
    try:
        from main import app

        body = b"{}"
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            response = await c.post("/webhooks/alpha/", content=body, headers=_headers(body, SECRETS["alpha"]))
    finally:
        _REGISTRY.pop("alpha", None)
    assert response.status_code == 500


@pytest.mark.asyncio
async def test_a_request_cancelled_mid_admission_is_still_admitted_once(_isolated_db, monkeypatch):
    from api.webhook_receivers import handle_receiver
    from events.connectors import github_webhook
    from test_webhook_endpoint import _direct_webhook_request

    entered, release = threading.Event(), threading.Event()
    scheduled: list[str | None] = []
    real_update = webhook_delivery_log.update_delivery_status

    def schedule(event_type, payload, *, delivery_id=None, on_settled=None):
        scheduled.append(delivery_id)
        if on_settled is not None:
            on_settled()
        return "github.pr.opened"

    def held_settle(*args, **kwargs):
        entered.set()
        release.wait(5.0)
        return real_update(*args, **kwargs)

    monkeypatch.setattr("api.webhook_receivers.receiver_secret", lambda connector: WEBHOOK_SECRET)
    monkeypatch.setattr(github_webhook, "schedule_github_webhook", schedule)
    monkeypatch.setattr(webhook_delivery_log, "update_delivery_status", held_settle)
    request = _direct_webhook_request(_pr_opened_payload(), delivery_id="gh-cancelled")
    task = asyncio.create_task(handle_receiver("github", "", request))
    try:
        assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 5.0), 6.0)
        task.cancel()
        await asyncio.sleep(0)
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5.0)

    conn = sqlite3.connect(str(_isolated_db))
    try:
        rows = conn.execute(
            "SELECT status FROM webhook_deliveries WHERE delivery_id=?", ("gh-cancelled",),
        ).fetchall()
    finally:
        conn.close()
    assert rows == [("dispatched",)], "recorded and settled, once"
    assert scheduled == ["gh-cancelled"]
