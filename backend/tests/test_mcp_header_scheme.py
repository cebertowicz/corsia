"""A header value like ``Bearer <token>`` is stored as its token and sent with its scheme back."""
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import AsyncExitStack
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException

from tests.test_mcp_http import http_mcp  # noqa: F401  (the shared HTTP MCP fixture)
from tools import mcp_import, mcp_secrets
from tools.mcp import _StoredHeadersAuth, connect_mcp_servers
from tools.mcp_config import MCPConfigError
from tools.registry import ToolRegistry

BEARER = {"header": "Authorization", "scheme": "Bearer"}
TOKEN = "fixture-token-7c1e"


def _json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _write_source(home: Path, source: str, url: str, header: str) -> None:
    config = {"url": url, "headers": {"Authorization": header}}
    if source == "cursor":
        _json(home / ".cursor/mcp.json", {"mcpServers": {"Hosted": config}})
    else:
        _json(home / ".claude.json", {"mcpServers": {"Hosted": {"type": "http", **config}}})


@pytest.mark.parametrize("source", ["cursor", "claude_code"])
@pytest.mark.asyncio
async def test_imported_bearer_header_is_stored_as_its_token_and_sent_as_bearer(
    tmp_path, http_mcp, isolated_db, caplog, source,  # noqa: F811
):
    from connections.app_lifecycle import approve_proposal
    from connections.app_reach import get_app, key_entry_names, runtime_app_config
    from infra import key_store
    from infra.database import get_db

    caplog.set_level(logging.DEBUG)
    http_mcp.token = TOKEN
    http_mcp.echo = True
    _write_source(tmp_path, source, http_mcp.url, f"Bearer {TOKEN}")
    def prepare():  # database writes stay off the event loop
        db = get_db()
        try:
            imported = mcp_import.import_selected(
                [{"source": source, "name": "Hosted"}], db=db, home=tmp_path, actor="agent",
            )
            [row] = imported["rows"]
            assert row["state"] == "awaiting_approval"
            app = approve_proposal(row["id"], db=db)
            assert app["config"]["key_refs"] == [{"kind": "header", "name": "Authorization"}]
            assert app["config"][mcp_secrets.HEADER_SCHEMES] == [BEARER]
            [entry] = key_entry_names(app["id"], app["config"])
            assert key_store.resolve_names([entry], db=db) == {entry: TOKEN}
            assert TOKEN not in json.dumps({"app": get_app(app["id"], db=db), "response": imported})
            return runtime_app_config(app["id"], db=db)
        finally:
            db.close()

    name, config = await asyncio.to_thread(prepare)
    registry = ToolRegistry()
    async with AsyncExitStack() as stack:
        assert not (await connect_mcp_servers({name: config}, registry, stack))
        [tool_name] = [n for n in registry.tool_names if n.endswith("read_profile")]
        result = await registry.get(tool_name).execute()
    assert set(http_mcp.headers) == {f"Bearer {TOKEN}"}
    assert TOKEN not in result
    assert TOKEN not in caplog.text


def test_a_value_with_internal_spaces_is_still_refused_without_echoing_it(tmp_path, isolated_db, caplog):
    from infra.database import get_db

    caplog.set_level(logging.DEBUG)
    _write_source(tmp_path, "cursor", "https://example.test/mcp", f"Bearer {TOKEN} extra")
    db = get_db()
    try:
        imported = mcp_import.import_selected(
            [{"source": "cursor", "name": "Hosted"}], db=db, home=tmp_path, actor="agent",
        )
        assert imported == {"rows": [{"name": "Hosted", "status": "failed"}]}
        assert db.execute("SELECT count(*) FROM connected_apps").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM key_store_entries").fetchone()[0] == 0
    finally:
        db.close()
    assert TOKEN not in json.dumps(imported) and TOKEN not in caplog.text


@pytest.mark.asyncio
async def test_codex_bearer_token_env_var_gets_a_key_that_is_sent_as_bearer(
    tmp_path, http_mcp, isolated_db, caplog,  # noqa: F811
):
    from connections.app_lifecycle import approve_proposal
    from connections.app_reach import get_app, key_entry_names, key_ref_descriptors, runtime_app_config
    from infra import key_store
    from infra.database import get_db

    caplog.set_level(logging.DEBUG)
    http_mcp.token = TOKEN
    codex = tmp_path / ".codex" / "config.toml"
    codex.parent.mkdir()
    codex.write_text(
        f'[mcp_servers.Hosted]\nurl = "{http_mcp.url}"\nbearer_token_env_var = "HOSTED_API_TOKEN"\n',
        encoding="utf-8",
    )
    def prepare():  # database writes stay off the event loop
        db = get_db()
        try:
            imported = mcp_import.import_selected(
                [{"source": "codex", "name": "Hosted"}], db=db, home=tmp_path, actor="agent",
            )
            app = approve_proposal(imported["rows"][0]["id"], db=db)
            assert app["config"]["needs_token"] is True
            assert key_ref_descriptors(app["id"], app["config"], db=db) == [
                {"kind": "header", "name": "Authorization", "set": False},
            ]
            [entry] = key_entry_names(app["id"], app["config"])
            key_store.set_managed_value(entry, TOKEN, db=db)  # the person pastes the token alone
            assert get_app(app["id"], db=db)["config"][mcp_secrets.HEADER_SCHEMES] == [BEARER]
            return runtime_app_config(app["id"], db=db)
        finally:
            db.close()

    name, config = await asyncio.to_thread(prepare)
    async with AsyncExitStack() as stack:
        assert not (await connect_mcp_servers({name: config}, ToolRegistry(), stack))
    assert set(http_mcp.headers) == {f"Bearer {TOKEN}"}
    assert TOKEN not in caplog.text


def test_a_sign_in_import_keeps_no_scheme_from_a_discarded_credential(tmp_path, isolated_db):
    from connections.app_reach import list_apps
    from infra.database import get_db

    _json(tmp_path / ".cursor/mcp.json", {"mcpServers": {"Hosted": {
        "url": "https://example.test/mcp", "oauth": {}, "headers": {"Authorization": f"Bearer {TOKEN}"},
    }}})
    db = get_db()
    try:
        imported = mcp_import.import_selected(
            [{"source": "cursor", "name": "Hosted"}], db=db, home=tmp_path, actor="agent",
        )
        assert imported["rows"][0]["state"] == "awaiting_approval"
        [app] = list_apps(db=db)
    finally:
        db.close()
    assert mcp_secrets.HEADER_SCHEMES not in app["config"] and TOKEN not in json.dumps(app)


def test_split_secrets_lifts_only_the_token_and_keeps_the_scheme():
    clean, lifted = mcp_secrets.split_secrets(
        "app", {"headers": {"Authorization": f"Basic {TOKEN}", "X-Api-Key": "plainvalue", "Accept": "x"}},
        headers=True,
    )

    assert sorted(lifted.values()) == sorted([TOKEN, "plainvalue"])
    assert clean["headers"] == {"Accept": "x"}
    assert clean[mcp_secrets.HEADER_SCHEMES] == [{"header": "Authorization", "scheme": "Basic"}]
    assert sorted(clean["secret_headers"]) == ["Authorization", "X-Api-Key"]


def test_a_value_that_is_not_scheme_and_token_is_lifted_whole_for_the_store_to_refuse():
    _clean, lifted = mcp_secrets.split_secrets(
        "app", {"headers": {"Authorization": "Bearer a b"}}, headers=True,
    )

    assert list(lifted.values()) == ["Bearer a b"]


def test_an_unknown_first_word_is_lifted_whole_so_it_never_reaches_plain_config():
    clean, lifted = mcp_secrets.split_secrets(
        "app", {"headers": {"Authorization": "acct42 opaque99"}}, headers=True,
    )

    assert list(lifted.values()) == ["acct42 opaque99"]
    assert mcp_secrets.HEADER_SCHEMES not in clean and "acct42" not in json.dumps(clean)


@pytest.mark.parametrize("scheme", ["Bearer", "bearer", "BASIC", "Token", "Bot"])
def test_a_known_scheme_is_split_off_in_any_case(scheme):
    clean, lifted = mcp_secrets.split_secrets(
        "app", {"headers": {"Authorization": f"{scheme} opaque99"}}, headers=True,
    )

    assert list(lifted.values()) == ["opaque99"]
    assert clean[mcp_secrets.HEADER_SCHEMES] == [{"header": "Authorization", "scheme": scheme}]


def test_only_authorization_has_a_scheme_split_off_other_keys_are_lifted_whole():
    clean, lifted = mcp_secrets.split_secrets(
        "app", {"headers": {"X-Api-Key": "part one"}}, headers=True,
    )

    assert list(lifted.values()) == ["part one"]
    assert mcp_secrets.HEADER_SCHEMES not in clean and "part" not in json.dumps(clean)


@pytest.mark.parametrize("schemes", [
    [{"header": "Authorization", "scheme": "Bearer abc"}],
    [{"header": "Authorization", "scheme": "Be\r\narer"}],
    [{"header": "Authorization", "scheme": "Bearer\n"}],
    [{"header": "Authorization", "scheme": ""}],
    [{"header": "Authorization", "scheme": 5}],
    [{"header": "X-Other", "scheme": "Bearer"}],
    [{"header": "Authorization"}],
    [BEARER, BEARER],
    {"Authorization": "Bearer"},
    ["Bearer"],
])
def test_a_scheme_must_be_one_word_for_a_listed_header(schemes):
    config = {"secret_headers": ["Authorization"], mcp_secrets.HEADER_SCHEMES: schemes}

    with pytest.raises(MCPConfigError) as refused:
        mcp_secrets.check_references(config)

    assert "abc" not in str(refused.value)


def test_stored_headers_auth_sends_the_scheme_and_redacts_the_token_alone_too():
    auth = _StoredHeadersAuth({"Authorization": TOKEN}, "https://example.test/mcp", {"Authorization": "Bearer"})

    request = next(auth.auth_flow(httpx.Request("POST", "https://example.test/mcp")))

    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    assert auth.redact(f"sent Bearer {TOKEN}, echoed {TOKEN}") == "sent [REDACTED], echoed [REDACTED]"


def test_an_agent_config_may_name_a_scheme_only_for_a_header_key_and_only_as_one_word():
    from api.connected_apps import _config

    url = "https://example.test/mcp"
    refs = [{"kind": "header", "name": "Authorization"}]
    accepted = _config({"config": {"url": url, mcp_secrets.HEADER_SCHEMES: [BEARER]}, "key_refs": refs})
    assert accepted[mcp_secrets.HEADER_SCHEMES] == [BEARER]

    for schemes, key_refs in (([{"header": "Authorization", "scheme": "Bearer x"}], refs), ([BEARER], [])):
        with pytest.raises(HTTPException) as refused:
            _config({"config": {"url": url, mcp_secrets.HEADER_SCHEMES: schemes}, "key_refs": key_refs})
        assert refused.value.status_code == 422
