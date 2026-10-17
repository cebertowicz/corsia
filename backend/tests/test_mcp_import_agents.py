"""OpenClaw and Hermes Agent MCP servers import through the same path as every other source.

The fixtures follow each vendor's documented layout; no real home directory is read.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from connections.app_reach import get_app
from infra.database import initialize_schema
import tools.mcp_import_json5 as mcp_import_json5
import tools.mcp_import as mcp_import

OPENCLAW_CONFIG = """
// JSON5: comments, unquoted keys, single quotes and trailing commas.
{
  agents: { defaults: { workspace: "~/.openclaw/workspace" } },
  mcp: {
    servers: {
      local: { command: 'uvx', args: ['mcp-server-fetch'], env: { REGION: 'us-west-2', API_KEY: 'literal-openclaw-key', }, },
      docs: {
        url: "https://mcp.example.test/mcp",
        transport: "streamable-http",
        connectionTimeoutMs: 5000,
        requestTimeoutMs: 20000,
        headers: { Authorization: "literal-header-token" },
      },
      legacy: { url: "https://mcp.example.test/sse", transport: "sse", headers: { 'X-Api-Key': '${LEGACY_KEY}' } },
      signin: { url: "https://signin.example.test/mcp", auth: "oauth" },
      parked: { command: "node", enabled: false },
      broken: { transport: "sse" },
    },
  },
}
"""

HERMES_CONFIG = """
model: example
mcp_servers:
  filesystem:
    command: npx
    args: ["-y", "@modelcontextprotocol/server-filesystem", "/tmp/projects"]
    env:
      GITHUB_PERSONAL_ACCESS_TOKEN: "${GITHUB_TOKEN}"
      CACHE_DIR: /tmp/cache
    connect_timeout: 30
    timeout: 120
  remote:
    url: https://mcp.example.test/mcp
    headers:
      Authorization: "literal-hermes-token"
  events:
    url: https://mcp.example.test/events
    transport: sse
    headers:
      Authorization: "Bearer ${EVENTS_TOKEN}"
  signin:
    url: https://signin.example.test/mcp
    auth: oauth
  parked:
    command: npx
    enabled: false
"""


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch):
    for name in ("OPENCLAW_HOME", "OPENCLAW_STATE_DIR", "OPENCLAW_CONFIG_PATH", "HERMES_HOME"):
        monkeypatch.delenv(name, raising=False)


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _db():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    initialize_schema(db)
    return db


def _names(detected, source):
    return {item["name"]: item["kind"] for entry in detected if entry["source"] == source for item in entry["servers"]}


def test_finds_openclaw_and_hermes_servers_in_their_default_places(tmp_path):
    _write(tmp_path / ".openclaw/openclaw.json", OPENCLAW_CONFIG)
    _write(tmp_path / ".hermes/config.yaml", HERMES_CONFIG)

    detected = mcp_import.detected_sources(tmp_path)

    assert [entry["app"] for entry in detected] == ["OpenClaw", "Hermes Agent"]
    assert _names(detected, "openclaw") == {
        "local": "stdio", "docs": "remote", "legacy": "remote", "signin": "remote",
    }
    assert _names(detected, "hermes") == {
        "filesystem": "stdio", "remote": "remote", "events": "remote", "signin": "remote",
    }
    rendered = json.dumps(detected)
    assert "literal" not in rendered and "example.test" not in rendered and "uvx" not in rendered


def test_absent_and_malformed_configs_read_as_nothing(tmp_path):
    assert mcp_import.detected_sources(tmp_path) == []

    _write(tmp_path / ".openclaw/openclaw.json", "{ mcp: { servers: { broken: { command: ")
    _write(tmp_path / ".hermes/config.yaml", "mcp_servers: [unclosed\n  - : :")
    assert mcp_import.detected_sources(tmp_path) == []

    _write(tmp_path / ".openclaw/openclaw.json", json.dumps({"mcp": {"servers": ["not", "a", "map"]}}))
    _write(tmp_path / ".hermes/config.yaml", "mcp_servers: just-a-string\n")
    assert mcp_import.detected_sources(tmp_path) == []

    _write(tmp_path / ".hermes/config.yaml", "- a\n- list\n")
    assert mcp_import.detected_sources(tmp_path) == []


def test_openclaw_config_path_override_wins_over_state_dir_and_home(tmp_path, monkeypatch):
    _write(tmp_path / ".openclaw/openclaw.json", '{ mcp: { servers: { default: { command: "a" } } } }')
    _write(tmp_path / "state/openclaw.json", '{ mcp: { servers: { state: { command: "b" } } } }')
    _write(tmp_path / "elsewhere/main.json", '{ mcp: { servers: { explicit: { command: "c" } } } }')
    _write(tmp_path / "relocated/.openclaw/openclaw.json", '{ mcp: { servers: { relocated: { command: "d" } } } }')

    assert _names(mcp_import.detected_sources(tmp_path), "openclaw") == {"default": "stdio"}

    monkeypatch.setenv("OPENCLAW_HOME", str(tmp_path / "relocated"))
    assert _names(mcp_import.detected_sources(tmp_path), "openclaw") == {"relocated": "stdio"}

    monkeypatch.setenv("OPENCLAW_STATE_DIR", str(tmp_path / "state"))
    assert _names(mcp_import.detected_sources(tmp_path), "openclaw") == {"state": "stdio"}

    monkeypatch.setenv("OPENCLAW_CONFIG_PATH", str(tmp_path / "elsewhere/main.json"))
    assert _names(mcp_import.detected_sources(tmp_path), "openclaw") == {"explicit": "stdio"}

    monkeypatch.setenv("OPENCLAW_CONFIG_PATH", "~/elsewhere/main.json")
    assert _names(mcp_import.detected_sources(tmp_path), "openclaw") == {"explicit": "stdio"}


def test_hermes_home_override_selects_the_active_profile(tmp_path, monkeypatch):
    _write(tmp_path / ".hermes/config.yaml", "mcp_servers:\n  default:\n    command: a\n")
    _write(tmp_path / ".hermes/profiles/coder/config.yaml", "mcp_servers:\n  coder:\n    command: b\n")

    assert _names(mcp_import.detected_sources(tmp_path), "hermes") == {"default": "stdio"}

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes/profiles/coder"))
    assert _names(mcp_import.detected_sources(tmp_path), "hermes") == {"coder": "stdio"}

    monkeypatch.setenv("HERMES_HOME", "~/.hermes/profiles/coder")
    assert _names(mcp_import.detected_sources(tmp_path), "hermes") == {"coder": "stdio"}


def test_imports_every_transport_through_the_shared_path(tmp_path):
    _write(tmp_path / ".openclaw/openclaw.json", OPENCLAW_CONFIG)
    _write(tmp_path / ".hermes/config.yaml", HERMES_CONFIG)
    db = _db()

    result = mcp_import.import_selected([
        {"source": "openclaw", "name": "local"},
        {"source": "openclaw", "name": "docs"},
        {"source": "openclaw", "name": "legacy"},
        {"source": "openclaw", "name": "parked"},
        {"source": "openclaw", "name": "broken"},
        {"source": "hermes", "name": "remote"},
    ], db=db, home=tmp_path, actor="agent")

    statuses = {row["name"]: row.get("state") or row.get("status") for row in result["rows"]}
    assert statuses == {
        "local": "awaiting_approval", "docs": "awaiting_approval", "legacy": "awaiting_approval",
        "parked": "failed", "broken": "failed", "remote": "awaiting_approval",
    }
    ids = {row["name"]: row["id"] for row in result["rows"] if "id" in row}
    rendered = json.dumps(result)
    assert "literal" not in rendered

    local = get_app(ids["local"], db=db)["config"]
    assert local["command"] == "uvx" and local["args"] == ["mcp-server-fetch"]
    assert local["env"] == {"REGION": "us-west-2"}
    assert local["key_refs"] == [{"kind": "env", "name": "API_KEY"}]
    assert "literal-openclaw-key" not in json.dumps(local)

    docs = get_app(ids["docs"], db=db)["config"]
    assert docs["url"] == "https://mcp.example.test/mcp" and docs["transport"] == "http"
    assert docs["connect_timeout"] == 5 and docs["tool_timeout"] == 20
    assert docs["key_refs"] == [{"kind": "header", "name": "Authorization"}]

    legacy = get_app(ids["legacy"], db=db)["config"]
    assert legacy["transport"] == "sse"
    assert legacy["key_refs"] == [{"kind": "header", "name": "X-Api-Key"}]
    assert "headers" not in legacy


def test_remote_apps_that_need_sign_in_are_imported_in_that_state(tmp_path):
    _write(tmp_path / ".openclaw/openclaw.json", OPENCLAW_CONFIG)
    _write(tmp_path / ".hermes/config.yaml", HERMES_CONFIG)

    for source in ("openclaw", "hermes"):
        result = mcp_import.import_selected(
            [{"source": source, "name": "signin"}], db=_db(), home=tmp_path, actor="agent",
        )

        assert [(row["name"], row.get("state")) for row in result["rows"]] == [("signin", "needs_sign_in")]


def test_hermes_env_references_keep_only_the_variable_name(tmp_path):
    _write(tmp_path / ".hermes/config.yaml", HERMES_CONFIG)
    db = _db()

    result = mcp_import.import_selected([
        {"source": "hermes", "name": "filesystem"}, {"source": "hermes", "name": "events"},
    ], db=db, home=tmp_path, actor="agent")

    assert [row["state"] for row in result["rows"]] == ["awaiting_approval", "awaiting_approval"]
    stdio = get_app(result["rows"][0]["id"], db=db)["config"]
    assert stdio["env"] == {"CACHE_DIR": "/tmp/cache"}
    assert stdio["key_refs"] == [{"kind": "env", "name": "GITHUB_PERSONAL_ACCESS_TOKEN"}]
    assert stdio["connect_timeout"] == 30 and stdio["tool_timeout"] == 120
    assert "${" not in json.dumps(stdio)

    remote = get_app(result["rows"][1]["id"], db=db)["config"]
    assert remote["transport"] == "sse" and remote["needs_token"] is True
    assert remote["key_refs"] == [{"kind": "header", "name": "Authorization"}]
    assert "${" not in json.dumps(remote)


def test_their_secrets_files_are_never_opened(tmp_path, monkeypatch):
    _write(tmp_path / ".openclaw/openclaw.json", OPENCLAW_CONFIG)
    _write(tmp_path / ".openclaw/.env", "OPENCLAW_SECRET=do-not-read")
    _write(tmp_path / ".hermes/config.yaml", HERMES_CONFIG)
    _write(tmp_path / ".hermes/.env", "HERMES_SECRET=do-not-read")
    opened: list[str] = []
    real_read_text = type(tmp_path).read_text

    def recording(self, *args, **kwargs):
        opened.append(self.name)
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(type(tmp_path), "read_text", recording)
    mcp_import.detected_sources(tmp_path)
    mcp_import.import_selected([{"source": "hermes", "name": "filesystem"}], db=_db(), home=tmp_path, actor="agent")

    assert set(opened) == {"openclaw.json", "config.yaml"}


def test_oversized_config_reads_as_nothing(tmp_path):
    _write(tmp_path / ".hermes/config.yaml", "mcp_servers:\n  big:\n    command: x\n#" + "x" * 1_100_000)

    assert mcp_import.detected_sources(tmp_path) == []


@pytest.mark.parametrize("text,expected", [
    ("{a: 1, 'b': [0x10, -2, +3.5, 1e2,], /* c */ \"c\": null, d: true}", {"a": 1, "b": [16, -2, 3.5, 100.0], "c": None, "d": True}),
    ('{s: "a\\u0041\\x42\\n"}', {"s": "aAB\n"}),
    ("[]", []),
])
def test_json5_reader_reads_the_documented_syntax(text, expected):
    assert mcp_import_json5.loads(text) == expected


@pytest.mark.parametrize("text", ["{", "{a 1}", "{a: }", "[1 2]", '{a: "open', "{} trailing", "/* open", "[" * 200])
def test_json5_reader_rejects_malformed_text(text):
    with pytest.raises(ValueError):
        mcp_import_json5.loads(text)
