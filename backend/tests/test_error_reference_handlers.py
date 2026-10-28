import logging
import re
from contextlib import contextmanager

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

import main
from infra.internal_principal import internal_request


def _assert_reference(value: str) -> None:
    assert re.fullmatch(r"[0-9ABCDEFGHJKMNPQRSTVWXYZ]{6}", value)


@contextmanager
def _client_for(path: str, endpoint):
    route = APIRoute(path, endpoint=endpoint, methods=["GET"])
    main.app.router.routes.insert(0, route)
    client = TestClient(main.app, raise_server_exceptions=False)
    try:
        yield client
    finally:
        client.close()
        main.app.router.routes.remove(route)


def _capture_events(monkeypatch):
    events = []
    monkeypatch.setattr(main, "emit_product_event", lambda *args, **kwargs: events.append((args, kwargs)))
    return events


def _assert_logged_and_emitted(caplog, events, reference: str, *, error_type: str, status: int, path: str) -> None:
    assert caplog.text.count(reference) == 1
    assert len(events) == 1
    args, kwargs = events[0]
    assert args == ("error", "api")
    assert kwargs["properties"].items() >= {
        "error_type": error_type,
        "path": path,
        "method": "GET",
        "status": status,
        "reference": reference,
    }.items()


def test_unhandled_error_returns_one_reference_in_log_and_event(monkeypatch, caplog):
    events = _capture_events(monkeypatch)
    caplog.set_level(logging.ERROR, logger=main.logger.name)
    path = "/_test/error-reference/runtime"

    def endpoint() -> None:
        raise RuntimeError("runtime test failure")

    with _client_for(path, endpoint) as client:
        response = client.get(path)

    assert response.status_code == 500
    assert response.json()["detail"] == "Something went wrong in CORSIA. Try again."
    reference = response.json()["reference"]
    _assert_reference(reference)
    _assert_logged_and_emitted(caplog, events, reference, error_type="RuntimeError", status=500, path=path)


def test_server_http_error_returns_route_detail_and_reference(monkeypatch, caplog):
    events = _capture_events(monkeypatch)
    caplog.set_level(logging.ERROR, logger=main.logger.name)
    path = "/_test/error-reference/http-500"

    def endpoint() -> None:
        raise HTTPException(500, "route text")

    with _client_for(path, endpoint) as client:
        response = client.get(path)

    assert response.status_code == 500
    assert response.json()["detail"] == "route text"
    reference = response.json()["reference"]
    _assert_reference(reference)
    _assert_logged_and_emitted(caplog, events, reference, error_type="HTTPException", status=500, path=path)


def test_server_http_error_preserves_status_headers_and_reference(monkeypatch, caplog):
    events = _capture_events(monkeypatch)
    caplog.set_level(logging.ERROR, logger=main.logger.name)
    path = "/_test/error-reference/http-503"

    def endpoint() -> None:
        raise HTTPException(503, "unavailable", headers={"Retry-After": "1"})

    with _client_for(path, endpoint) as client:
        response = client.get(path)

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "1"
    assert response.json()["detail"] == "unavailable"
    reference = response.json()["reference"]
    _assert_reference(reference)
    _assert_logged_and_emitted(caplog, events, reference, error_type="HTTPException", status=503, path=path)


def test_internal_server_http_error_event_hides_detail(monkeypatch):
    events = _capture_events(monkeypatch)

    def endpoint() -> None:
        raise HTTPException(500, "private-detail-sentinel")

    with internal_request("user", "actor"), _client_for("/_test/error-reference/internal", endpoint) as client:
        response = client.get("/_test/error-reference/internal")

    assert response.status_code == 500
    assert events[0][1]["properties"]["error_message"] == "In-process conductor request failed"
    assert "private-detail-sentinel" not in str(events)


def test_server_http_error_event_message_is_capped(monkeypatch):
    events = _capture_events(monkeypatch)
    path = "/_test/error-reference/http-500-long"

    def endpoint() -> None:
        raise HTTPException(500, {"text": "x" * 500})

    with _client_for(path, endpoint) as client:
        response = client.get(path)

    assert response.json()["detail"] == {"text": "x" * 500}
    assert len(events[0][1]["properties"]["error_message"]) == 200


def test_client_and_validation_errors_do_not_receive_references(monkeypatch):
    _capture_events(monkeypatch)

    def bad_request() -> None:
        raise HTTPException(400, "bad request")

    def validation(value: int) -> dict[str, int]:
        return {"value": value}

    with _client_for("/_test/error-reference/http-400", bad_request) as client:
        assert client.get("/api/error-reference/missing").json() == {"error": "Not found"}
        assert client.get("/_test/error-reference/http-400").json() == {"detail": "bad request"}
    with _client_for("/_test/error-reference/validation", validation) as client:
        response = client.get("/_test/error-reference/validation", params={"value": "nope"})

    assert response.status_code == 422
    assert "reference" not in response.json()
