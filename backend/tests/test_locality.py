"""Local-only classification and email-screen compatibility tests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest


def _row(**overrides) -> dict:
    row = {
        "selector": "omlx@local/model",
        "litellm_model": "omlx@local/model",
        "connection": "omlx",
        "provider": "omlx",
        "backend": "network",
    }
    row.update(overrides)
    return row


@pytest.mark.parametrize(
    ("row", "private_servers", "api_base", "expected"),
    [
        (_row(), set(), "http://127.0.0.1:8000/v1", True),
        (_row(), set(), "http://192.168.1.2:8000/v1", False),
        (_row(), {"local"}, "http://192.168.1.2:8000/v1", True),
        (_row(selector="external@cloud/model", litellm_model="external@cloud/model", connection="external", provider="external"), set(), "http://127.0.0.1:8000/v1", False),
        (_row(selector="external@cloud/model", litellm_model="external@cloud/model", connection="external", provider="external"), {"cloud"}, "https://example.test/v1", True),
        (_row(backend="api", selector="anthropic/model", litellm_model="anthropic/model", connection="anthropic", provider="anthropic"), set(), None, False),
        (_row(connection="codex", provider="codex"), set(), "http://127.0.0.1:8000/v1", False),
    ],
)
def test_row_is_local_matrix(monkeypatch, row, private_servers, api_base, expected):
    from llm import capabilities
    from llm.locality import row_is_local

    monkeypatch.setattr(capabilities, "make_provider_params", lambda _model: {"api_base": api_base})
    assert row_is_local(row, private_servers=private_servers) is expected


def test_row_is_local_fails_closed_when_endpoint_cannot_be_read(monkeypatch):
    from llm import capabilities
    from llm.locality import row_is_local

    monkeypatch.setattr(capabilities, "make_provider_params", lambda _model: (_ for _ in ()).throw(ValueError()))
    assert row_is_local(_row()) is False


def test_email_screen_keeps_external_loopback_behavior():
    from connections.google.email_screen import local_endpoint_refusal

    target = SimpleNamespace(harness="native", backend="network", transport_model="external@cloud/model")
    assert local_endpoint_refusal(target, lambda _model: {"api_base": "http://127.0.0.1:8000/v1"}) is None
