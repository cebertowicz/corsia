"""Focused route and identity coverage for ``corsia-cli mcp`` connected-app commands."""

from __future__ import annotations

from typing import Any

import pytest

import cli


@pytest.mark.parametrize(
    ("argv", "method", "path", "body", "query"),
    [
        (["mcp", "list"], "GET", "/api/mcp/servers", None, None),
        (
            [
                "mcp", "add", "--name", "docs", "--config", '{"url":"https://mcp.test"}',
                "--key-refs", '[{"kind":"header","name":"Authorization"}]',
                "--reach", '{"mode":"all","project_ids":[],"outside_projects":false}',
            ],
            "POST",
            "/api/mcp/servers",
            {
                "name": "docs",
                "config": {"url": "https://mcp.test"},
                "key_refs": [{"kind": "header", "name": "Authorization"}],
                "reach": {"mode": "all", "project_ids": [], "outside_projects": False},
            },
            None,
        ),
        (
            [
                "mcp", "edit", "server-1", "--config", '{"command":"updated"}',
                "--key-refs", "[]", "--expected-revision", "3",
            ],
            "PATCH",
            "/api/mcp/servers/server-1",
            {"config": {"command": "updated"}, "key_refs": [], "expected_revision": 3},
            None,
        ),
        (["mcp", "remove", "server-1"], "DELETE", "/api/mcp/servers/server-1", None, None),
        (["mcp", "reconnect", "server-1"], "POST", "/api/mcp/servers/server-1/reconnect", None, None),
        (
            ["mcp", "approve", "server-1", "--expected-revision", "2"],
            "POST", "/api/mcp/servers/server-1/approve", {"expected_revision": 2}, None,
        ),
    ],
)
def test_connected_app_commands_use_the_server_routes(
    monkeypatch, capsys, argv, method, path, body, query,
) -> None:
    from scripts import corsia_api

    calls: list[tuple] = []
    monkeypatch.setattr(corsia_api, "resolve_base", lambda _explicit: "http://api.test")
    monkeypatch.setattr(corsia_api, "resolve_token", lambda _explicit: "cli-token")

    def request(*args, **kwargs):
        calls.append((args, kwargs))
        return {"server": {"id": "server-1"}}

    monkeypatch.setattr(corsia_api, "request", request)

    assert cli.main(argv) == 0

    assert calls == [(
        (method, "http://api.test", path, "cli-token", body, query),
        {"header_auth": True},
    )]
    assert '"id": "server-1"' in capsys.readouterr().out


def test_list_projects_uses_the_server_query(monkeypatch) -> None:
    from scripts import corsia_api

    call: dict[str, Any] = {}
    monkeypatch.setattr(corsia_api, "resolve_base", lambda _explicit: "http://api.test")
    monkeypatch.setattr(corsia_api, "resolve_token", lambda _explicit: "cli-token")
    monkeypatch.setattr(
        corsia_api,
        "request",
        lambda *args, **kwargs: call.update(args=args, kwargs=kwargs) or {"servers": []},
    )

    assert cli.main(["mcp", "list", "--project", "project-1"]) == 0

    assert call == {
        "args": ("GET", "http://api.test", "/api/mcp/servers", "cli-token", None, {"project": "project-1"}),
        "kwargs": {"header_auth": True},
    }


def test_server_ids_are_path_escaped(monkeypatch) -> None:
    from scripts import corsia_api

    call: dict[str, Any] = {}
    monkeypatch.setattr(corsia_api, "resolve_base", lambda _explicit: "http://api.test")
    monkeypatch.setattr(corsia_api, "resolve_token", lambda _explicit: "cli-token")
    monkeypatch.setattr(
        corsia_api,
        "request",
        lambda *args, **kwargs: call.update(args=args, kwargs=kwargs) or {},
    )

    assert cli.main(["mcp", "remove", "server/with/slash"]) == 0

    assert call["args"][2] == "/api/mcp/servers/server%2Fwith%2Fslash"
