"""A Claude Code update that stops listing a selector must not kill what is pinned to it."""

from __future__ import annotations

import json
import sqlite3

import pytest

from vendor_account_fixtures import signed_in_default


@pytest.fixture
def connections_db(tmp_path, monkeypatch):
    db_path = tmp_path / "connections.db"
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    from infra.schema import SCHEMA_SQL
    db.executescript(SCHEMA_SQL)
    from connections import models

    def _get_db():
        connection = sqlite3.connect(db_path)
        connection.row_factory = sqlite3.Row
        return connection

    monkeypatch.setattr(models, "get_db", _get_db)
    from connections.models import ConnectionStatus, ConnectionType, upsert_connection

    upsert_connection(
        type=ConnectionType.CLI, provider="claude-code",
        status=ConnectionStatus.AVAILABLE, config={"binary": "/bin/claude"}, db=db,
    )
    signed_in_default("claude-code", db=db)  # discovery runs for an account: there is no ambient login
    db.commit()
    yield db
    db.close()


def _model(value: str, resolved: str, name: str) -> dict:
    return {
        "value": value, "resolvedModel": resolved, "displayName": name,
        "supportsEffort": True, "supportedEffortLevels": ["low", "high"],
    }


# Claude Code 2.1.282 listed the 1M variants; 2.1.283 lists the base aliases.
OLD_LIST = [
    _model("opus[1m]", "claude-opus-5-5[1m]", "Opus (1M context)"),
    _model("fable[1m]", "claude-fable-5-1", "Fable"),
    _model("cc-update-required-1", "cc-update-required-1", "Fable 5.1 (disabled)"),
]
NEW_LIST = [
    _model("opus", "claude-opus-5-5", "Opus 5.5"),
    _model("claude-fable-5-1", "claude-fable-5-1", "Fable 5.1"),
]


def _rows(db) -> dict[str, dict]:
    return {
        row["provider_model_id"]: dict(row)
        for row in db.execute("SELECT * FROM models WHERE harness = 'claude-code'")
    }


async def _refresh(db, monkeypatch, listing, accepted: dict[str, str]):
    """Run one discovery; ``accepted`` maps selector -> model the CLI reports."""
    import connections.harness_models as harness_models

    probed: list[str] = []

    async def _discover(_binary, context=None):
        from connections.account_discovery_types import StagedDiscovery

        return StagedDiscovery(payload={"models": [dict(raw) for raw in listing]})

    async def _probe(_binary, selector, context=None):
        probed.append(selector)
        if selector not in accepted:
            raise RuntimeError(f"Claude Code rejected selector {selector}")
        return {"model": accepted[selector], "maxTokens": 1_000_000,
                "rawMaxTokens": 1_000_000}

    async def _version(_binary, env=None):
        return "2.1.283"

    monkeypatch.setitem(
        harness_models._ADAPTERS, "claude-code",
        (_discover, harness_models.normalize_claude_models, harness_models.CLAUDE_SOURCE),
    )
    monkeypatch.setattr("connections.claude_discovery._discover_claude_context", _probe)
    monkeypatch.setattr(harness_models, "binary_version", _version)
    result = await harness_models.refresh_provider(
        "claude-code", reason="version_changed", db=db,
    )
    assert result["ok"] is True, result
    return probed


@pytest.mark.asyncio
async def test_accepted_unlisted_selector_keeps_running_as_an_alias(
    connections_db, monkeypatch,
):
    from llm.resolution import resolve_execution_target

    await _refresh(connections_db, monkeypatch, OLD_LIST, {})
    probed = await _refresh(
        connections_db, monkeypatch, NEW_LIST,
        {"opus[1m]": "claude-opus-5-5[1m]", "fable[1m]": "claude-fable-5-1[1m]"},
    )

    # The update-required placeholder names no model the CLI still lists.
    assert sorted(probed) == ["fable[1m]", "opus[1m]"]
    rows = _rows(connections_db)
    for selector, alias_of in (("opus[1m]", "opus"), ("fable[1m]", "claude-fable-5-1")):
        row = rows[selector]
        meta = json.loads(row["metadata_json"])
        # Discovery never writes the operator's show/hide; pickers read alias_of.
        assert (row["available"], row["enabled"]) == (1, 1)
        assert meta["alias_of"] == f"claude-code/{alias_of}"
        assert "availability_reason" not in meta
    assert json.loads(rows["opus[1m]"]["metadata_json"])[
        "resolved_identity"] == "claude-opus-5-5[1m]"
    placeholder = rows["cc-update-required-1"]
    assert (placeholder["available"], placeholder["enabled"]) == (0, 1)

    target = resolve_execution_target("claude-code/opus[1m]", db=connections_db)
    assert target.available is True
    assert target.transport_model == "opus[1m]"
    assert target.expected_reported_model == "claude-opus-5-5[1m]"


@pytest.mark.asyncio
async def test_a_selector_not_kept_says_why(connections_db, monkeypatch):
    await _refresh(connections_db, monkeypatch, OLD_LIST, {})
    await _refresh(
        connections_db, monkeypatch, NEW_LIST,
        {"opus[1m]": "claude-fable-5-1[1m]"},
    )

    rows = _rows(connections_db)
    remapped, rejected = rows["opus[1m]"], rows["fable[1m]"]
    assert remapped["available"] == 0
    assert json.loads(remapped["metadata_json"])[
        "availability_reason"] == "not_returned_by_discovery"
    assert "now maps it to claude-fable-5-1[1m], not claude-opus-5-5[1m]" in remapped["last_error"]
    assert rejected["available"] == 0
    assert "RuntimeError: Claude Code rejected selector fable[1m]" in rejected["last_error"]


@pytest.mark.asyncio
async def test_discovery_never_undoes_an_operator_show_or_hide(connections_db, monkeypatch):
    await _refresh(connections_db, monkeypatch, OLD_LIST + NEW_LIST, {})
    connections_db.execute(
        "UPDATE models SET enabled = 0 WHERE provider_model_id = 'claude-fable-5-1'"
    )
    connections_db.commit()
    accepted = {"opus[1m]": "claude-opus-5-5[1m]", "fable[1m]": "claude-fable-5-1"}
    await _refresh(connections_db, monkeypatch, NEW_LIST, accepted)
    connections_db.execute(
        "UPDATE models SET enabled = 0 WHERE provider_model_id = 'opus[1m]'"
    )
    connections_db.commit()
    await _refresh(connections_db, monkeypatch, NEW_LIST, accepted)

    rows = _rows(connections_db)
    assert rows["opus[1m]"]["enabled"] == 0
    assert rows["fable[1m]"]["enabled"] == 1
    assert rows["claude-fable-5-1"]["enabled"] == 0

    await _refresh(connections_db, monkeypatch, OLD_LIST + NEW_LIST, {})

    rows = _rows(connections_db)
    assert (rows["opus[1m]"]["available"], rows["opus[1m]"]["enabled"]) == (1, 0)
    assert "alias_of" not in json.loads(rows["opus[1m]"]["metadata_json"])
    assert rows["claude-fable-5-1"]["enabled"] == 0

