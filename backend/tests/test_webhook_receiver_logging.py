"""The webhook front door's measurement lines: body arrival, rejection attribution, mid-body abort.

Each test drives the real ``handle_receiver`` with an ASGI request whose body arrives in
chunks, so the logged split between body arrival and handler work comes from a body that
really was slow to arrive, not from a patched clock.
"""

from __future__ import annotations

import asyncio
import json
import logging

import pytest
from fastapi import Request
from starlette.requests import ClientDisconnect

from webhook_ingress_fixtures import (  # noqa: F401
    WEBHOOK_SECRET,
    _isolated_db,
    _make_signature,
)
from test_webhook_receivers import TEST_KEY, _test_manifest

from api import webhook_receivers
from events.connectors import _REGISTRY, get_manifest, register
from infra import key_store

LOGGER = "api.webhook_receivers"
#: The gap between two body chunks: the whole of a slow delivery's body_ms.
GAP_SECONDS = 0.15
#: Patched far below the shipped threshold, so the sleep above is what trips the line.
THRESHOLD_MS = 50
DELIVERY_ID = "delivery-id-must-not-be-logged"


def _streaming_request(
    chunks: list[bytes],
    headers: dict[str, str],
    *,
    gap_seconds: float = 0.0,
    cut_after: int | None = None,
) -> Request:
    """An ASGI request whose body arrives in ``chunks``, ``gap_seconds`` apart.

    ``cut_after`` closes the connection after that many chunks, as a sender that
    stalls and then disconnects mid-body does.
    """
    position = {"index": 0}

    async def receive():
        index = position["index"]
        if index:
            await asyncio.sleep(gap_seconds)
        if cut_after is not None and index >= cut_after:
            position["index"] = index + 1
            return {"type": "http.disconnect"}
        position["index"] = index + 1
        return {
            "type": "http.request",
            "body": chunks[index],
            "more_body": position["index"] < len(chunks),
        }

    scope = {
        "type": "http", "method": "POST", "path": "/webhooks/github",
        "headers": [(key.encode(), value.encode()) for key, value in headers.items()],
    }
    return Request(scope, receive)


def _github_headers(
    body: bytes,
    *,
    event: str = "pull_request",
    signed: bool = True,
    **overrides: str,
) -> dict[str, str]:
    """GitHub's own delivery headers, signed unless asked otherwise, plus any overrides."""
    headers = {
        "x-github-event": event,
        "x-github-delivery": DELIVERY_ID,
        "content-type": "application/json",
        "content-length": str(len(body)),
        "user-agent": "GitHub-Hookshot/2.14",
    }
    if signed:
        headers["x-hub-signature-256"] = _make_signature(body)
    headers.update(overrides)
    return headers


def _fields(record_message: str) -> dict[str, str]:
    """The logged ``key=value`` fields of one line."""
    return dict(token.split("=", 1) for token in record_message.split() if "=" in token)


def _lines(caplog, prefix: str) -> list[str]:
    return [record.message for record in caplog.records if record.message.startswith(prefix)]


@pytest.fixture(autouse=True)
def _fast_threshold_and_configured_secret(monkeypatch):
    monkeypatch.setattr(webhook_receivers, "SLOW_DELIVERY_MS", THRESHOLD_MS)
    monkeypatch.setattr(webhook_receivers, "receiver_secret", lambda decl: WEBHOOK_SECRET)


@pytest.fixture()
def _warn_caplog(caplog):
    caplog.set_level(logging.WARNING, logger=LOGGER)


@pytest.mark.asyncio
async def test_a_slow_streamed_body_is_logged_apart_from_the_handler(_isolated_db, _warn_caplog, caplog):
    """An unsigned delivery whose upload took 150 ms: the body owns the time, not the 401."""
    body = json.dumps({"action": "opened"}).encode()
    request = _streaming_request(
        [body[:7], body[7:]], _github_headers(body, signed=False), gap_seconds=GAP_SECONDS,
    )

    response = await webhook_receivers.handle_receiver("github", request)

    assert response.status_code == 401
    lines = _lines(caplog, "slow webhook")
    assert len(lines) == 1
    fields = _fields(lines[0])
    assert fields["connector"] == "github"
    assert fields["status"] == "401"
    assert float(fields["body_ms"]) >= GAP_SECONDS * 1_000 * 0.8
    assert float(fields["handler_ms"]) < 1_000
    assert fields["body_bytes"] == str(len(body))
    assert fields["chunks"] == "2"
    assert fields["declared_length"] == str(len(body))
    assert fields["agent"] == "GitHub-Hookshot"


@pytest.mark.asyncio
async def test_a_slow_signed_delivery_splits_body_from_handler_time(_isolated_db, _warn_caplog, caplog):
    """The same slowly arriving body on an accepted delivery: status 200, same split."""
    body = json.dumps({"action": "created"}).encode()
    request = _streaming_request(
        [body[:7], body[7:]], _github_headers(body, event="deployment"), gap_seconds=GAP_SECONDS,
    )

    content = await webhook_receivers.handle_receiver("github", request)

    assert content["status"] == "ignored"
    lines = _lines(caplog, "slow webhook")
    assert len(lines) == 1
    fields = _fields(lines[0])
    assert fields["status"] == "200"
    assert float(fields["body_ms"]) >= GAP_SECONDS * 1_000 * 0.8
    assert float(fields["handler_ms"]) < 1_000
    assert fields["body_bytes"] == str(len(body))
    assert fields["agent"] == "GitHub-Hookshot"


@pytest.mark.asyncio
async def test_a_rejected_delivery_names_the_sender_in_one_line(_isolated_db, _warn_caplog, caplog):
    """The line that says who sends the unsigned deliveries, and nothing secret."""
    body = json.dumps({"action": "opened", "pull_request": {"title": "Test PR"}}).encode()
    signature = _make_signature(body)
    request = _streaming_request(
        [body],
        _github_headers(
            body,
            signed=False,
            **{
                "user-agent": "curl/8.4.0",
                "x-github-hook-id": "4711",
                "x-github-hook-installation-target-type": "Repository",
                "x-hub-signature": "sha1=0e2b3c4d",
            },
        ),
    )

    response = await webhook_receivers.handle_receiver("github", request)

    assert response.status_code == 401
    lines = _lines(caplog, "webhook rejected")
    assert len(lines) == 1, "one attributed line per rejection"
    fields = _fields(lines[0])
    assert fields["connector"] == "github"
    assert fields["status"] == "401"
    assert fields["reason"] == "missing_signature"
    assert fields["agent"] == "curl"
    assert fields["x-github-event"] == "pull_request"
    assert fields["x-github-hook-id"] == "4711"
    assert fields["x-github-hook-installation-target-type"] == "Repository"
    assert fields["declared_length"] == str(len(body))
    assert fields["body_bytes"] == str(len(body))
    assert DELIVERY_ID not in lines[0]
    assert signature not in lines[0]
    assert "sha1=0e2b3c4d" not in lines[0]
    assert "Test PR" not in lines[0]


@pytest.mark.asyncio
async def test_a_body_over_the_declared_limit_logs_its_rejection(_isolated_db, _warn_caplog, caplog):
    """The 413 path logs too, with the length the sender declared."""
    body = b"{}"
    limit = get_manifest("github").receiver.max_body_bytes
    declared = str(limit + 1)
    request = _streaming_request([body], _github_headers(body, **{"content-length": declared}))

    response = await webhook_receivers.handle_receiver("github", request)

    assert response.status_code == 413
    lines = _lines(caplog, "webhook rejected")
    assert len(lines) == 1
    fields = _fields(lines[0])
    assert fields["status"] == "413"
    assert fields["reason"] == "body_too_large"
    assert fields["declared_length"] == declared
    assert fields["body_bytes"] == "0"


@pytest.mark.asyncio
async def test_a_client_that_disconnects_mid_body_is_logged_and_the_error_propagates(
    _isolated_db, _warn_caplog, caplog,
):
    """The sender went away before the body ended: one line, and the same exception upward."""
    body = b'{"action": "opened", "pull_request": {"number": 42}}'
    request = _streaming_request(
        [body[:8], body[8:]], _github_headers(body), gap_seconds=GAP_SECONDS, cut_after=1,
    )

    with pytest.raises(ClientDisconnect):
        await webhook_receivers.handle_receiver("github", request)

    lines = _lines(caplog, "webhook body aborted")
    assert len(lines) == 1
    fields = _fields(lines[0])
    assert fields["connector"] == "github"
    assert fields["bytes_so_far"] == "8"
    assert fields["declared_length"] == str(len(body))
    assert float(fields["elapsed_ms"]) >= GAP_SECONDS * 1_000 * 0.8
    assert _lines(caplog, "slow webhook") == [], "an abandoned body is not a served delivery"


@pytest.mark.asyncio
async def test_a_fault_in_admission_is_still_logged_as_slow_and_propagates(
    _isolated_db, _warn_caplog, caplog, monkeypatch,
):
    """The try/finally: a translator fault is timed as a 500 and the fault itself is unchanged."""

    async def translate(delivery):
        await asyncio.sleep(GAP_SECONDS)
        raise RuntimeError("boom")

    monkeypatch.setitem(key_store.BUILTINS, TEST_KEY.name, TEST_KEY)
    register(_test_manifest("slowfault", translate))
    body = b"{}"
    headers = {
        "x-test-signature": _make_signature(body),
        "x-test-delivery": "slowfault-delivery",
        "content-length": str(len(body)),
        "user-agent": "SlowFault/1.0 (runner)",
    }
    try:
        with pytest.raises(RuntimeError, match="boom"):
            await webhook_receivers.handle_receiver("slowfault", _streaming_request([body], headers))
    finally:
        _REGISTRY.pop("slowfault", None)

    lines = _lines(caplog, "slow webhook")
    assert len(lines) == 1, "a fault is timed too, and reported as the 500 the route answers"
    fields = _fields(lines[0])
    assert fields["connector"] == "slowfault"
    assert fields["status"] == "500"
    assert float(fields["handler_ms"]) >= GAP_SECONDS * 1_000 * 0.8
    assert fields["agent"] == "SlowFault"


@pytest.mark.asyncio
async def test_a_cancelled_slow_delivery_is_logged_as_cancelled_not_500(
    _isolated_db, _warn_caplog, caplog, monkeypatch,
):
    """A request cancelled mid-admission answered nothing, so its slow line must not say 500."""

    async def translate(delivery):
        await asyncio.sleep(GAP_SECONDS)
        raise asyncio.CancelledError

    monkeypatch.setitem(key_store.BUILTINS, TEST_KEY.name, TEST_KEY)
    register(_test_manifest("slowcancel", translate))
    body = b"{}"
    headers = {
        "x-test-signature": _make_signature(body),
        "x-test-delivery": "slowcancel-delivery",
        "content-length": str(len(body)),
    }
    try:
        with pytest.raises(asyncio.CancelledError):
            await webhook_receivers.handle_receiver("slowcancel", _streaming_request([body], headers))
    finally:
        _REGISTRY.pop("slowcancel", None)

    lines = _lines(caplog, "slow webhook")
    assert len(lines) == 1
    assert _fields(lines[0])["status"] == "cancelled"


@pytest.mark.asyncio
async def test_a_connector_without_sender_headers_logs_no_empty_sender_fields(
    _isolated_db, _warn_caplog, caplog, monkeypatch,
):
    """The rejection line carries only the sender headers the connector declared."""

    async def translate(delivery):
        raise AssertionError("an unsigned delivery never reaches the translator")

    monkeypatch.setitem(key_store.BUILTINS, TEST_KEY.name, TEST_KEY)
    register(_test_manifest("plain", translate))
    body = b"{}"
    headers = {"content-length": str(len(body)), "x-github-event": "push"}
    try:
        response = await webhook_receivers.handle_receiver("plain", _streaming_request([body], headers))
    finally:
        _REGISTRY.pop("plain", None)

    assert response.status_code == 401
    lines = _lines(caplog, "webhook rejected")
    assert len(lines) == 1
    assert "x-github-event" not in lines[0]
    assert _fields(lines[0])["agent"] == "-"
