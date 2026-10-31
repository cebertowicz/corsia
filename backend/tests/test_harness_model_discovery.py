from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from vendor_account_fixtures import seed_catalog


class _BytesReader:
    """Minimal async byte reader matching the control-I/O owner's stream use."""

    def __init__(self, payload: bytes):
        self._payload = payload

    async def read(self, _limit: int = -1) -> bytes:
        payload, self._payload = self._payload, b""
        return payload


class _ControlInput:
    """Minimal stdin recorder matching the control-I/O owner's write and drain calls."""

    def __init__(self, received):
        self._received = received

    def write(self, payload: bytes) -> None:
        self._received(payload)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        return None


class _PipeTransport:
    """Minimal pipe transport matching the stream reader's final close."""

    def close(self) -> None:
        return None


class _ControlTransport:
    """Expose the one transport method the production stream reader uses."""

    def get_pipe_transport(self, _fd: int) -> _PipeTransport:
        return _PipeTransport()


class _ControlProcess:
    """In-memory process double matching the production control-I/O contract."""

    def __init__(self, stdout: bytes, *, stderr: bytes = b"", received=None):
        self.returncode = 0
        self.stdin = _ControlInput(received or (lambda _payload: None))
        self.stdout = _BytesReader(stdout)
        self.stderr = _BytesReader(stderr)
        self._transport = _ControlTransport()

    async def _read_stream(self, fd: int) -> bytes:
        return await (self.stderr if fd == 2 else self.stdout).read()

    async def wait(self) -> int:
        return self.returncode

    async def communicate(self, payload: bytes | None = None) -> tuple[bytes, bytes]:
        if payload is not None:
            self.stdin.write(payload)
            await self.stdin.drain()
            self.stdin.close()
        stdout, stderr = await asyncio.gather(self.stdout.read(), self.stderr.read())
        await self.wait()
        return stdout, stderr

    def kill(self) -> None:
        self.returncode = -9


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
    yield db
    db.close()


def _account(prior=None):
    """The account a discovery runs for: there is no ambient home."""
    from types import SimpleNamespace

    return SimpleNamespace(source_home="/nonexistent/claude-account", prior_discovery=prior)


def _codex_context():
    """The signed-in Codex Default's discovery context, on the isolated database."""
    from connections.account_discovery_types import capture_discovery_context
    from vendor_account_fixtures import default_id, signed_in_default

    signed_in_default("codex")
    return capture_discovery_context("codex", account_id=default_id("codex"))


def _staged(payload):
    from connections.account_discovery_types import StagedDiscovery

    return StagedDiscovery(payload=payload)


def _seed_connection(db, provider: str, config: dict | None = None) -> None:
    from connections.models import ConnectionStatus, ConnectionType, upsert_connection
    from vendor_account_fixtures import signed_in_default

    upsert_connection(
        type=ConnectionType.CLI,
        provider=provider,
        status=ConnectionStatus.AVAILABLE,
        config=config or {"binary": provider},
        db=db,
    )
    signed_in_default(provider, db=db)  # a refresh asks a signed-in account: no ambient login
    db.commit()


def _claude_payload() -> dict:
    return {
        "models": [
            {
                "value": "default",
                "resolvedModel": "claude-opus-4-8[1m]",
                "displayName": "Default (recommended)",
                "description": "Mutable harness default",
                "supportsEffort": True,
                "supportedEffortLevels": ["low", "high"],
            },
            {
                "value": "claude-fable-5[1m]",
                "resolvedModel": "claude-fable-5",
                "displayName": "Fable",
                "description": "Most capable",
                "supportsEffort": True,
                "supportedEffortLevels": ["low", "medium", "high"],
                "supportsAdaptiveThinking": True,
                "supportsAutoMode": True,
            },
        ]
    }


def _codex_payload(*model_ids: str) -> dict:
    return {
        "data": [
            {
                "id": model_id,
                "model": model_id,
                "displayName": model_id.replace("gpt-", "GPT-").title(),
                "description": f"{model_id} description",
                "hidden": False,
                "isDefault": index == 0,
                "defaultReasoningEffort": "medium",
                "supportedReasoningEfforts": [
                    {"reasoningEffort": "low", "description": "Fast"},
                    {"reasoningEffort": "high", "description": "Deep"},
                ],
                "inputModalities": ["text", "image"],
                "supportsPersonality": False,
            }
            for index, model_id in enumerate(model_ids)
        ],
        "nextCursor": None,
    }


def _codex_payload_with_context(*model_ids: str) -> dict:
    payload = _codex_payload(*model_ids)
    for row in payload["data"]:
        row["contextMetadata"] = {
            "context_window": 272000,
            "max_context_window": 1000000,
            "effective_context_window_percent": 95,
        }
    return payload


def test_claude_normalization_uses_exact_selector_and_hides_mutable_default():
    from connections.harness_models import normalize_claude_models

    rows = normalize_claude_models(
        _claude_payload(),
        harness_version="2.1.206",
        timestamp="2026-07-11T00:00:00+00:00",
    )

    assert [row["selector_id"] for row in rows] == ["claude-fable-5[1m]"]
    assert rows[0] == {
        "selector_id": "claude-fable-5[1m]",
        "display_name": "Fable",
        "resolved_identity": "claude-fable-5",
        "description": "Most capable",
        "capabilities": {
            "supports_effort": True,
            "effort_levels": ["low", "medium", "high"],
            "adaptive_thinking": True,
            "fast_mode": False,
            "auto_mode": True,
        },
        "is_default": False,
        "harness_version": "2.1.206",
        "source": "claude-stream-json:list_models",
        "discovered_at": "2026-07-11T00:00:00+00:00",
        "available": True,
        "last_error": None,
    }


def test_claude_normalization_marks_listed_default_over_alias():
    from connections.claude_unlisted import ALIAS_KEY
    from connections.harness_models import normalize_claude_models

    rows = normalize_claude_models(
        {
            "models": [
                {"value": "default", "resolvedModel": "claude-fable-5"},
                {
                    "value": "claude-fable-5-legacy",
                    "resolvedModel": "claude-fable-5",
                    ALIAS_KEY: "claude-fable-5",
                },
                {
                    "value": "claude-fable-5",
                    "resolvedModel": "claude-fable-5",
                },
            ]
        },
        harness_version="2.1.206",
        timestamp="2026-07-11T00:00:00+00:00",
    )

    assert [row["selector_id"] for row in rows] == [
        "claude-fable-5-legacy", "claude-fable-5"
    ]
    assert [row["is_default"] for row in rows] == [False, True]


def test_claude_normalization_without_vendor_default_marks_no_row():
    from connections.harness_models import normalize_claude_models

    rows = normalize_claude_models(
        {"models": [{"value": "claude-fable-5", "resolvedModel": "claude-fable-5"}]},
        harness_version="2.1.206",
        timestamp="2026-07-11T00:00:00+00:00",
    )

    assert [row["is_default"] for row in rows] == [False]


def test_codex_normalization_preserves_protocol_identity_and_capabilities():
    from connections.harness_models import normalize_codex_models

    rows = normalize_codex_models(
        _codex_payload("gpt-5.6-sol"),
        harness_version="0.144.0",
        timestamp="2026-07-11T00:00:00+00:00",
    )

    assert rows[0]["selector_id"] == "gpt-5.6-sol"
    assert rows[0]["resolved_identity"] == "gpt-5.6-sol"
    assert rows[0]["is_default"] is True
    assert rows[0]["source"] == "codex-app-server:model/list"
    assert rows[0]["capabilities"] == {
        "input_modalities": ["text", "image"],
        "reasoning_efforts": ["low", "high"],
        "default_reasoning_effort": "medium",
        "supports_personality": False,
    }


def test_claude_normalization_uses_exact_context_usage_without_family_defaults():
    from connections.harness_models import normalize_claude_models

    payload = _claude_payload()
    payload["models"][1]["contextUsage"] = {
        "model": "claude-fable-5",
        "rawMaxTokens": 967000,
        "maxTokens": 967000,
        "autoCompactThreshold": 934000,
    }
    rows = normalize_claude_models(
        payload, harness_version="2.1.206",
        timestamp="2026-07-11T00:00:00+00:00",
    )

    assert rows[0]["context_window"] == 967000
    assert rows[0]["context_observation"] == {
        "source": "claude-stream-json:get_context_usage",
        "version": "2.1.206",
        "observed_at": "2026-07-11T00:00:00+00:00",
        "raw_tokens": 967000,
        "max_tokens": 967000,
        "effective_tokens": 967000,
        "effective_percent": 100,
    }


def test_codex_normalization_uses_effective_debug_window_not_static_maximum():
    from connections.harness_models import normalize_codex_models

    payload = _codex_payload("gpt-5.4")
    payload["data"][0]["contextMetadata"] = {
        "context_window": 272000,
        "max_context_window": 1000000,
        "effective_context_window_percent": 95,
    }
    rows = normalize_codex_models(
        payload, harness_version="0.144.0",
        timestamp="2026-07-11T00:00:00+00:00",
    )

    assert rows[0]["context_window"] == 258400
    assert rows[0]["context_observation"] == {
        "source": "codex-debug-models",
        "version": "0.144.0",
        "observed_at": "2026-07-11T00:00:00+00:00",
        "raw_tokens": 272000,
        "max_tokens": 1000000,
        "effective_tokens": 258400,
        "effective_percent": 95,
    }


def test_runtime_context_observation_updates_only_exact_cc_identity(connections_db):
    from connections.harness_models import record_runtime_context_observation

    _seed_connection(connections_db, "claude-code")
    row = _claude_payload()["models"][1]
    row["contextUsage"] = {"rawMaxTokens": 967000, "maxTokens": 967000}
    from connections.harness_models import normalize_claude_models
    seed_catalog(
        "claude-code",
        normalize_claude_models(
            {"models": [row]}, harness_version="2.1.206",
            timestamp="2026-07-11T00:00:00+00:00",
        ),
        harness_version="2.1.206", source="claude-stream-json:list_models",
        timestamp="2026-07-11T00:00:00+00:00", db=connections_db,
    )

    assert record_runtime_context_observation(
        "claude-code", selector="claude-fable-5[1m]",
        reported_identity="wrong-model", effective_tokens=500000,
        source="claude-stream-json:result", db=connections_db,
    ) is None
    assert record_runtime_context_observation(
        "claude-code", selector="claude-fable-5[1m]",
        reported_identity="claude-fable-5", effective_tokens=934000,
        source="claude-stream-json:result",
        observed_at="2026-07-11T01:00:00+00:00", db=connections_db,
    ) == 934000
    stored = _model_rows(connections_db, "claude-code")["claude-fable-5[1m]"]
    assert stored["context_window"] == 934000
    observation = json.loads(stored["metadata_json"])["context_observation"]
    assert observation == {
        "source": "claude-stream-json:result",
        "version": "2.1.206",
        "observed_at": "2026-07-11T01:00:00+00:00",
        "resolved_identity": "claude-fable-5",
        "raw_tokens": 934000,
        "max_tokens": 934000,
        "effective_tokens": 934000,
        "effective_percent": 100,
    }


def test_codex_runtime_effective_window_is_not_scaled_twice(
    connections_db,
):
    from connections.harness_models import (
        normalize_codex_models,
        record_runtime_context_observation,
    )

    _seed_connection(connections_db, "codex")
    payload = _codex_payload("gpt-5.4")
    payload["data"][0]["contextMetadata"] = {
        "context_window": 272000,
        "max_context_window": 1000000,
        "effective_context_window_percent": 95,
    }
    seed_catalog(
        "codex", normalize_codex_models(
            payload, harness_version="0.144.0",
            timestamp="2026-07-11T00:00:00+00:00",
        ),
        harness_version="0.144.0", source="codex-app-server:model/list",
        timestamp="2026-07-11T00:00:00+00:00", db=connections_db,
    )

    assert record_runtime_context_observation(
        "codex", selector="gpt-5.4", reported_identity="gpt-5.4",
        effective_tokens=258400, source="codex-app-server:turn/result",
        observed_at="2026-07-11T01:00:00+00:00", db=connections_db,
    ) == 258400
    stored = _model_rows(connections_db, "codex")["gpt-5.4"]
    assert stored["context_window"] == 258400
    observation = json.loads(stored["metadata_json"])["context_observation"]
    assert observation["source"] == "codex-app-server:turn/result"
    assert observation["debug_source"] == "codex-debug-models"
    assert observation["raw_tokens"] == 272000
    assert observation["max_tokens"] == 1000000
    assert observation["effective_tokens"] == 258400
    assert observation["effective_percent"] == 95


def test_refresh_drops_context_evidence_when_selector_resolves_new_identity(
    connections_db,
):
    from connections.harness_models import (
        normalize_codex_models,
        record_runtime_context_observation,
    )

    _seed_connection(connections_db, "codex")
    first = _codex_payload("mutable")
    first["data"][0]["model"] = "model-a"
    first["data"][0]["contextMetadata"] = {
        "context_window": 272000,
        "max_context_window": 1000000,
        "effective_context_window_percent": 95,
    }
    seed_catalog(
        "codex", normalize_codex_models(
            first, harness_version="0.144.0",
            timestamp="2026-07-11T00:00:00+00:00",
        ),
        harness_version="0.144.0", source="codex-app-server:model/list",
        timestamp="2026-07-11T00:00:00+00:00", db=connections_db,
    )
    assert record_runtime_context_observation(
        "codex", selector="mutable", reported_identity="model-a",
        effective_tokens=258400, source="codex-app-server:turn/result",
        observed_at="2026-07-11T01:00:00+00:00", db=connections_db,
    ) is not None

    second = _codex_payload("mutable")
    second["data"][0]["model"] = "model-b"
    seed_catalog(
        "codex", normalize_codex_models(
            second, harness_version="0.144.0",
            timestamp="2026-07-11T02:00:00+00:00",
        ),
        harness_version="0.144.0", source="codex-app-server:model/list",
        timestamp="2026-07-11T02:00:00+00:00", db=connections_db,
    )

    stored = _model_rows(connections_db, "codex")["mutable"]
    assert stored["context_window"] is None
    metadata = json.loads(stored["metadata_json"])
    assert metadata["resolved_identity"] == "model-b"
    assert "context_observation" not in metadata


def _model_rows(db, provider: str) -> dict[str, dict]:
    """The catalog rows as readers see them: projected from the accounts that list them."""
    from llm.harness_catalog import project_stored_rows

    return {
        row["provider_model_id"]: row
        for row in project_stored_rows(db.execute(
            "SELECT * FROM models WHERE harness = ? ORDER BY id", (provider,)
        ).fetchall(), db=db)
    }


def test_successful_refresh_marks_missing_catalog_rows_unavailable(connections_db):
    from connections.harness_models import (
        normalize_codex_models,
        read_discovery_envelope,
    )

    _seed_connection(connections_db, "codex")
    seed_catalog(
        "codex",
        normalize_codex_models(
            _codex_payload("gpt-5.6-sol", "gpt-5.6-terra"),
            harness_version="0.144.0",
            timestamp="2026-07-11T00:00:00+00:00",
        ),
        harness_version="0.144.0",
        source="codex-app-server:model/list",
        timestamp="2026-07-11T00:00:00+00:00",
        db=connections_db,
    )
    seed_catalog(
        "codex",
        normalize_codex_models(
            _codex_payload_with_context("gpt-5.6-sol"),
            harness_version="0.144.0",
            timestamp="2026-07-11T01:00:00+00:00",
        ),
        harness_version="0.144.0",
        source="codex-app-server:model/list",
        timestamp="2026-07-11T01:00:00+00:00",
        db=connections_db,
    )

    rows = _model_rows(connections_db, "codex")
    assert rows["gpt-5.6-sol"]["available"] == 1
    assert rows["gpt-5.6-terra"]["available"] == 0
    assert json.loads(rows["gpt-5.6-terra"]["metadata_json"])[
        "availability_reason"
    ] == "not_returned_by_discovery"
    envelope = read_discovery_envelope("codex", db=connections_db)
    assert envelope["last_error"] is None
    assert "models" not in envelope


def test_failed_refresh_retains_last_known_rows_and_records_error(connections_db):
    from connections.harness_models import (
        normalize_codex_models,
        read_discovery_envelope,
        store_failure,
    )

    _seed_connection(connections_db, "codex")
    rows = normalize_codex_models(
        _codex_payload("gpt-5.6-sol"),
        harness_version="0.144.0",
        timestamp="2026-07-11T00:00:00+00:00",
    )
    seed_catalog(
        "codex",
        rows,
        harness_version="0.144.0",
        source="codex-app-server:model/list",
        timestamp="2026-07-11T00:00:00+00:00",
        db=connections_db,
    )
    before = _model_rows(connections_db, "codex")
    store_failure(
        "codex",
        "model/list timed out",
        harness_version="0.145.0",
        source="codex-app-server:model/list",
        timestamp="2026-07-11T02:00:00+00:00",
        db=connections_db,
    )

    rows = _model_rows(connections_db, "codex")
    assert rows == before
    assert list(rows) == ["gpt-5.6-sol"]
    assert rows["gpt-5.6-sol"]["available"] == 1
    assert rows["gpt-5.6-sol"]["last_error"] is None
    envelope = read_discovery_envelope("codex", db=connections_db)
    assert envelope["last_error"] == "model/list timed out"
    assert envelope["harness_version"] == "0.145.0"
    assert envelope["refreshed_at"] == "2026-07-11T00:00:00+00:00"
    assert envelope["attempted_at"] == "2026-07-11T02:00:00+00:00"


def test_first_failed_refresh_persists_an_empty_discovery_envelope(connections_db):
    from connections.harness_models import read_discovery_envelope, store_failure

    _seed_connection(connections_db, "codex")
    store_failure(
        "codex",
        "model/list unavailable",
        harness_version="0.144.0",
        source="codex-app-server:model/list",
        timestamp="2026-07-11T02:00:00+00:00",
        db=connections_db,
    )

    envelope = read_discovery_envelope("codex", db=connections_db)
    assert "models" not in envelope
    assert envelope["last_error"] == "model/list unavailable"
    assert envelope["attempted_at"] == "2026-07-11T02:00:00+00:00"
    assert _model_rows(connections_db, "codex") == {}


def test_legacy_fallback_is_a_catalog_row_only_until_first_discovery_attempt(
    connections_db,
):
    from connections.harness_models import store_failure
    from connections.models import ConnectionStatus, ConnectionType, upsert_connection

    upsert_connection(
        type=ConnectionType.CLI, provider="codex",
        status=ConnectionStatus.NOT_INSTALLED, config={"binary": "codex"},
        db=connections_db,
    )
    fallback = _model_rows(connections_db, "codex")
    assert list(fallback) == ["gpt-5-codex"]
    assert fallback["gpt-5-codex"]["id"] == "codex/gpt-5-codex"
    assert fallback["gpt-5-codex"]["source"] == "legacy_fallback"

    upsert_connection(
        type=ConnectionType.CLI, provider="codex",
        status=ConnectionStatus.AVAILABLE, config={"binary": "codex"},
        db=connections_db,
    )
    # Installing records no discovery attempt, so the placeholder stays.
    assert list(_model_rows(connections_db, "codex")) == ["gpt-5-codex"]

    upsert_connection(
        type=ConnectionType.CLI, provider="codex",
        status=ConnectionStatus.NEEDS_AUTH, config={"binary": "codex"},
        db=connections_db,
    )
    store_failure(
        "codex", "needs auth", harness_version="0.144.0",
        source="codex-app-server:model/list",
        timestamp="2026-07-11T02:00:00+00:00", db=connections_db,
    )
    assert _model_rows(connections_db, "codex") == {}


def test_the_pre_discovery_placeholder_keeps_an_operator_hide(connections_db):
    from connections.models import ConnectionStatus, ConnectionType, upsert_connection

    def _connect(status):
        upsert_connection(
            type=ConnectionType.CLI, provider="codex", status=status,
            config={"binary": "codex"}, db=connections_db,
        )

    _connect(ConnectionStatus.NOT_INSTALLED)
    connections_db.execute("UPDATE models SET enabled = 0 WHERE id = 'codex/gpt-5-codex'")
    _connect(ConnectionStatus.NEEDS_AUTH)

    assert _model_rows(connections_db, "codex")["gpt-5-codex"]["enabled"] == 0


def test_refresh_reason_is_event_driven_for_missing_stale_version_and_failure(
    connections_db,
):
    from connections.harness_models import (
        cache_refresh_reason,
        normalize_codex_models,
        store_failure,
    )

    _seed_connection(connections_db, "codex")
    now = datetime(2026, 7, 11, 3, tzinfo=timezone.utc)
    assert cache_refresh_reason("codex", now=now, db=connections_db) == "missing"

    seed_catalog(
        "codex",
        normalize_codex_models(
            _codex_payload_with_context("gpt-5.6-sol"),
            harness_version="0.144.0",
            timestamp=now.isoformat(),
        ),
        harness_version="0.144.0",
        source="codex-app-server:model/list",
        timestamp=now.isoformat(),
        db=connections_db,
    )
    assert cache_refresh_reason(
        "codex", harness_version="0.144.0",
        now=now + timedelta(minutes=5), db=connections_db,
    ) is None
    assert cache_refresh_reason(
        "codex", harness_version="0.145.0",
        now=now + timedelta(minutes=5), db=connections_db,
    ) == "version_changed"
    assert cache_refresh_reason(
        "codex", harness_version="0.144.0",
        now=now + timedelta(days=2), db=connections_db,
    ) == "stale"
    store_failure(
        "codex", "temporarily offline", harness_version="0.144.0",
        source="codex-app-server:model/list",
        timestamp=(now + timedelta(days=2)).isoformat(), db=connections_db,
    )
    assert cache_refresh_reason(
        "codex", harness_version="0.144.0",
        now=now + timedelta(days=2, minutes=1), db=connections_db,
    ) is None
    assert cache_refresh_reason(
        "codex", harness_version="0.144.0",
        now=now + timedelta(days=2, minutes=6), db=connections_db,
    ) == "retry_after_failure"


def test_rows_and_discovery_envelope_commit_to_their_canonical_owners(connections_db):
    from connections.harness_models import normalize_codex_models

    _seed_connection(connections_db, "codex", {"binary": "codex", "app_server": True})
    seed_catalog(
        "codex",
        normalize_codex_models(
            _codex_payload_with_context("gpt-5.6-sol"),
            harness_version="0.144.0",
            timestamp="2026-07-11T00:00:00+00:00",
        ),
        harness_version="0.144.0",
        source="codex-app-server:model/list",
        timestamp="2026-07-11T00:00:00+00:00",
        db=connections_db,
    )

    row = connections_db.execute("SELECT config FROM connections WHERE provider='codex'").fetchone()
    config = json.loads(row["config"])
    assert config["binary"] == "codex"
    assert config["app_server"] is True
    assert "models" not in config["harness_model_cache"]
    assert list(_model_rows(connections_db, "codex")) == ["gpt-5.6-sol"]
    tables = {
        item["name"]
        for item in connections_db.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"connections", "models"} <= tables


@pytest.mark.asyncio
async def test_claude_adapter_uses_stream_json_control_plane_without_a_turn(monkeypatch):
    import connections.harness_models as harness_models

    calls: list[tuple[tuple[str, ...], dict]] = []
    sent: list[dict] = []

    class _Process(_ControlProcess):
        def __init__(self, kind: str):
            if kind == "context":
                responses = [
                    {"type": "control_response", "response": {
                        "subtype": "success", "request_id": "corsia-set-model",
                    }},
                    {"type": "control_response", "response": {
                        "subtype": "success", "request_id": "corsia-context-usage",
                        "response": {
                            "model": "claude-fable-5", "rawMaxTokens": 967000,
                            "maxTokens": 967000, "autoCompactThreshold": 934000,
                        },
                    }},
                ]
                stdout = "".join(json.dumps(item) + "\n" for item in responses).encode()
            else:
                response = {
                    "type": "control_response",
                    "response": {
                        "subtype": "success",
                        "request_id": "corsia-model-list",
                        "response": _claude_payload(),
                    },
                }
                stdout = json.dumps(response).encode() + b"\n"
            super().__init__(
                stdout,
                received=lambda payload: sent.extend(
                    json.loads(line) for line in payload.decode().splitlines()
                ),
            )

    async def _spawn(*cmd, **kwargs):
        calls.append((cmd, kwargs))
        return _Process("list" if len(calls) == 1 else "context")

    from connections import claude_discovery

    monkeypatch.setattr(claude_discovery.asyncio, "create_subprocess_exec", _spawn)

    payload = (await harness_models.discover_claude_models("/bin/claude", context=_account())).payload

    assert [row["value"] for row in payload["models"]] == [
        "default", "claude-fable-5[1m]",
    ]
    assert payload["models"][1]["contextUsage"]["rawMaxTokens"] == 967000
    cmd, kwargs = calls[0]
    assert cmd[0] == "/bin/claude"
    assert "--no-session-persistence" in cmd
    assert "--session-id" not in cmd
    # Both the catalog spawn and the per-selector context spawn are governed.
    assert len(calls) == 2
    for spawned, _kwargs in calls:
        assert spawned[spawned.index("--setting-sources") + 1] == "user,local"
        assert "--settings" in spawned
    assert kwargs["stdin"] is not None and kwargs["stdout"] is not None
    assert [frame["request"]["subtype"] for frame in sent] == [
        "list_models", "get_usage", "end_session", "set_model", "get_context_usage",
        "end_session",
    ]
    assert sent[3]["request"]["model"] == "claude-fable-5[1m]"


@pytest.mark.asyncio
async def test_claude_adapter_reuses_an_unchanged_known_context_window(monkeypatch):
    """Known identity-bound windows do not spawn a redundant control process."""
    from connections import claude_discovery

    calls = 0

    class _Process(_ControlProcess):
        def __init__(self):
            response = {
                "type": "control_response",
                "response": {
                    "subtype": "success",
                    "request_id": "corsia-model-list",
                    "response": _claude_payload(),
                },
            }
            super().__init__(json.dumps(response).encode() + b"\n")

    async def _spawn(*_cmd, **_kwargs):
        nonlocal calls
        calls += 1
        return _Process()

    prior = {"discovery": {"models": {"claude-code/claude-fable-5[1m]": {
        "resolved_identity": "claude-fable-5", "context_window": 967000}}}}
    monkeypatch.setattr(claude_discovery.asyncio, "create_subprocess_exec", _spawn)

    payload = (await claude_discovery.discover_claude_models(
        "/bin/claude", context=_account(prior))).payload

    assert payload["models"][1].get("contextUsage") is None
    assert calls == 1


def _context_probe_call(monkeypatch, responses: list[dict]):
    """Build a ``_discover_claude_context`` call over canned control responses.

    Returns the un-awaited coroutine so each test can assert on how it fails.
    """
    import connections.harness_models as harness_models

    class _Process(_ControlProcess):
        def __init__(self):
            super().__init__(
                "".join(json.dumps(item) + "\n" for item in responses).encode(),
            )

    async def _spawn(*cmd, **kwargs):
        return _Process()

    monkeypatch.setattr(harness_models.asyncio, "create_subprocess_exec", _spawn)
    return harness_models._discover_claude_context("/bin/claude", "fable[1m]", context=_account())


@pytest.mark.asyncio
async def test_rejected_selector_reports_the_harness_reason(monkeypatch):
    """A refused set_model names why, not just the selector (#1911)."""
    probe = _context_probe_call(monkeypatch, [
        {"type": "control_response", "response": {
            "subtype": "error", "request_id": "corsia-set-model",
            "error": "model not available for this account",
        }},
    ])

    with pytest.raises(RuntimeError) as excinfo:
        await probe

    message = str(excinfo.value)
    assert "fable[1m]" in message
    assert "subtype=error" in message
    assert "model not available for this account" in message


@pytest.mark.asyncio
async def test_refused_context_usage_reports_the_harness_reason(monkeypatch):
    """A refused get_context_usage carries the harness detail (#2079)."""
    probe = _context_probe_call(monkeypatch, [
        {"type": "control_response", "response": {
            "subtype": "success", "request_id": "corsia-set-model",
        }},
        {"type": "control_response", "response": {
            "subtype": "error", "request_id": "corsia-context-usage",
            "error": {"message": "context usage unavailable for seat"},
        }},
    ])

    with pytest.raises(RuntimeError) as excinfo:
        await probe

    message = str(excinfo.value)
    assert "invalid context evidence for fable[1m]" in message
    assert "context usage unavailable for seat" in message


@pytest.mark.asyncio
async def test_missing_context_response_lists_what_arrived(monkeypatch):
    """An absent usage response says which control responses did arrive."""
    probe = _context_probe_call(monkeypatch, [
        {"type": "control_response", "response": {
            "subtype": "success", "request_id": "corsia-set-model",
        }},
    ])

    with pytest.raises(RuntimeError) as excinfo:
        await probe

    message = str(excinfo.value)
    assert "no context evidence for fable[1m]" in message
    assert "corsia-set-model" in message


@pytest.mark.asyncio
async def test_non_object_usage_payload_is_named(monkeypatch):
    """A usage payload of the wrong shape says so, not 'no evidence'."""
    probe = _context_probe_call(monkeypatch, [
        {"type": "control_response", "response": {
            "subtype": "success", "request_id": "corsia-set-model",
        }},
        {"type": "control_response", "response": {
            "subtype": "success", "request_id": "corsia-context-usage",
            "response": "1000000",
        }},
    ])

    with pytest.raises(RuntimeError) as excinfo:
        await probe

    assert "usage payload was str, expected object" in str(excinfo.value)


@pytest.mark.asyncio
async def test_empty_error_container_does_not_shadow_the_message(monkeypatch):
    """An empty `error` must not hide a populated `message`."""
    probe = _context_probe_call(monkeypatch, [
        {"type": "control_response", "response": {
            "subtype": "error", "request_id": "corsia-set-model",
            "error": {}, "message": "seat not entitled",
        }},
    ])

    with pytest.raises(RuntimeError) as excinfo:
        await probe

    message = str(excinfo.value)
    assert "seat not entitled" in message
    assert "error={}" not in message


@pytest.mark.asyncio
async def test_control_detail_is_bounded_but_preserved(monkeypatch):
    """A huge harness error is truncated, not discarded."""
    import connections.claude_context_probe as claude_context_probe

    probe = _context_probe_call(monkeypatch, [
        {"type": "control_response", "response": {
            "subtype": "error", "request_id": "corsia-set-model",
            "error": "x" * 5000,
        }},
    ])

    with pytest.raises(RuntimeError) as excinfo:
        await probe

    message = str(excinfo.value)
    # The reason survives truncation rather than being dropped.
    assert "xxx" in message
    assert message.endswith("…")
    assert "x" * (claude_context_probe._CONTROL_DETAIL_MAX + 1) not in message


@pytest.mark.asyncio
async def test_repeated_request_ids_cannot_flood_the_message(monkeypatch):
    """The observed-response list is deduped and capped."""
    probe = _context_probe_call(monkeypatch, [
        {"type": "control_response", "response": {
            "subtype": "success", "request_id": f"noise-{index}",
        }}
        for index in range(500)
    ])

    with pytest.raises(RuntimeError) as excinfo:
        await probe

    message = str(excinfo.value)
    assert "500 output lines" in message
    assert message.count("noise-") <= 20


@pytest.mark.asyncio
async def test_codex_adapter_replaces_stale_isolated_cache_after_online_refresh(
    isolated_db, monkeypatch,
):
    """A restart must refresh the cache later used by managed execution."""
    import connections.codex_discovery as codex_discovery

    calls: list[tuple[str, object]] = []
    context = _codex_context()
    discovery_home = Path(context.runtime_home)
    discovery_home.mkdir(parents=True, exist_ok=True)
    cache = discovery_home / "models_cache.json"
    cache.write_text('{"models":["stale"]}')
    fresh_home: Path | None = None

    class _Session:
        def __init__(self, **kwargs):
            nonlocal fresh_home
            fresh_home = Path(kwargs["codex_home"])
            assert fresh_home != discovery_home
            assert fresh_home.parent == discovery_home.parent
            assert not (fresh_home / "models_cache.json").exists()
            assert cache.read_text() == '{"models":["stale"]}'
            calls.append(("init", kwargs))

        async def start(self):
            calls.append(("start", None))

        async def list_models(self):
            assert fresh_home is not None
            (fresh_home / "models_cache.json").write_text('{"models":["fresh"]}')
            calls.append(("list", None))
            return _codex_payload("gpt-6.1-sol")

        def build_env(self):
            return {"CODEX_HOME": str(fresh_home)}

        async def close(self, timeout: float):
            assert cache.read_text() == '{"models":["stale"]}'
            calls.append(("close", timeout))

    monkeypatch.setattr(codex_discovery, "CodexAppServerSession", _Session)

    async def _debug(_binary: str, *, env=None):
        calls.append(("debug", None))
        return {"models": [{
            "slug": "gpt-6.1-sol", "context_window": 272000,
            "max_context_window": 272000,
            "effective_context_window_percent": 95,
        }, {
            "slug": "debug-only-hidden", "context_window": 128000,
        }]}

    monkeypatch.setattr(codex_discovery, "discover_codex_debug_models", _debug)

    staged = await codex_discovery.discover_codex_models("/bin/codex", context=context)
    payload = staged.payload

    assert [row["id"] for row in payload["data"]] == ["gpt-6.1-sol"]
    assert payload["data"][0]["contextMetadata"]["context_window"] == 272000
    # The commit promotes the staged cache; until then managed sessions keep the last known one.
    assert cache.read_text() == '{"models":["stale"]}'
    staged.cache_stage.promote()
    staged.close()
    assert cache.read_text() == '{"models":["fresh"]}'
    assert fresh_home is not None and not fresh_home.exists()
    assert calls == [
        ("init", {"binary": "/bin/codex", "task": None,
                  "codex_home": str(fresh_home), "control_only": True,
                  "source_codex_home": context.source_home}),
        ("start", None), ("list", None), ("debug", None), ("close", 10.0),
    ]


@pytest.mark.asyncio
async def test_codex_adapter_restores_prior_cache_after_unconfirmed_refresh(
    isolated_db, monkeypatch,
):
    """A bundled fallback after failure cannot replace the last-known cache."""
    import connections.codex_discovery as codex_discovery

    context = _codex_context()
    discovery_home = Path(context.runtime_home)
    discovery_home.mkdir(parents=True, exist_ok=True)
    cache = discovery_home / "models_cache.json"
    prior = '{"models":["last-known"]}'
    cache.write_text(prior)

    class _Session:
        def __init__(self, **kwargs):
            fresh_home = Path(kwargs["codex_home"])
            assert fresh_home != discovery_home
            assert not (fresh_home / "models_cache.json").exists()
            assert cache.read_text() == prior

        async def start(self):
            pass

        async def list_models(self):
            return _codex_payload("gpt-5.6-sol")

        async def close(self, timeout: float):
            assert cache.read_text() == prior

    monkeypatch.setattr(codex_discovery, "CodexAppServerSession", _Session)

    with pytest.raises(RuntimeError, match="online model refresh"):
        await codex_discovery.discover_codex_models("/bin/codex", context=context)

    assert cache.read_text() == prior


@pytest.mark.asyncio
async def test_codex_adapter_cleanup_does_not_mask_discovery_failure(isolated_db, monkeypatch):
    import connections.codex_discovery as codex_discovery

    context = _codex_context()
    discovery_home = Path(context.runtime_home)
    discovery_home.mkdir(parents=True, exist_ok=True)

    class _Session:
        def __init__(self, **kwargs):
            pass

        async def start(self):
            pass

        async def list_models(self):
            raise RuntimeError("model/list unavailable")

        async def close(self, timeout: float):
            raise RuntimeError("close unavailable")

    monkeypatch.setattr(codex_discovery, "CodexAppServerSession", _Session)

    with pytest.raises(RuntimeError, match="model/list unavailable"):
        await codex_discovery.discover_codex_models("/bin/codex", context=context)


@pytest.mark.asyncio
async def test_codex_adapter_cancellation_preserves_persistent_cache(isolated_db, monkeypatch):
    import connections.codex_discovery as codex_discovery

    context = _codex_context()
    discovery_home = Path(context.runtime_home)
    discovery_home.mkdir(parents=True, exist_ok=True)
    cache = discovery_home / "models_cache.json"
    prior = '{"models":["last-known"]}'
    cache.write_text(prior)

    class _Session:
        def __init__(self, **kwargs):
            assert Path(kwargs["codex_home"]) != discovery_home

        async def start(self):
            pass

        async def list_models(self):
            raise asyncio.CancelledError

        async def close(self, timeout: float):
            assert cache.read_text() == prior

    monkeypatch.setattr(codex_discovery, "CodexAppServerSession", _Session)

    with pytest.raises(asyncio.CancelledError):
        await codex_discovery.discover_codex_models("/bin/codex", context=context)

    assert cache.read_text() == prior


def envelope_version(db, provider):
    import connections.harness_models as harness_models

    return harness_models.read_discovery_envelope(provider, db=db)["harness_version"]


@pytest.mark.asyncio
async def test_refresh_provider_persists_success_and_reports_reason(connections_db, monkeypatch):
    import connections.harness_models as harness_models

    _seed_connection(connections_db, "claude-code", {"binary": "/bin/claude"})

    async def _discover(_binary: str, context=None):
        return _staged(_claude_payload())

    async def _version(_binary: str, env=None):
        return "2.1.206"

    monkeypatch.setitem(
        harness_models._ADAPTERS,
        "claude-code",
        (_discover, harness_models.normalize_claude_models, harness_models.CLAUDE_SOURCE),
    )
    monkeypatch.setattr(harness_models, "binary_version", _version)

    result = await harness_models.refresh_provider(
        "claude-code", reason="startup", db=connections_db
    )

    assert result == {"provider": "claude-code", "ok": True, "reason": "startup", "error": None}
    assert envelope_version(connections_db, "claude-code") == "2.1.206"
    envelope = harness_models.read_discovery_envelope(
        "claude-code", db=connections_db
    )
    assert "models" not in envelope
    assert list(_model_rows(connections_db, "claude-code")) == [
        "claude-fable-5[1m]"
    ]


@pytest.mark.asyncio
async def test_refresh_provider_failure_keeps_cached_rows(connections_db, monkeypatch):
    import connections.harness_models as harness_models

    _seed_connection(connections_db, "codex", {"binary": "/bin/codex"})
    seed_catalog(
        "codex",
        harness_models.normalize_codex_models(
            _codex_payload_with_context("gpt-5.6-sol"),
            harness_version="0.144.0",
            timestamp="2026-07-11T00:00:00+00:00",
        ),
        harness_version="0.144.0",
        source=harness_models.CODEX_SOURCE,
        timestamp="2026-07-11T00:00:00+00:00",
        db=connections_db,
    )

    async def _discover(_binary: str, context=None):
        raise RuntimeError("model/list unavailable")

    async def _version(_binary: str, env=None) -> str:
        return "0.145.0"

    monkeypatch.setitem(
        harness_models._ADAPTERS,
        "codex",
        (_discover, harness_models.normalize_codex_models, harness_models.CODEX_SOURCE),
    )
    monkeypatch.setattr(harness_models, "binary_version", _version)

    result = await harness_models.refresh_provider(
        "codex", reason="explicit", db=connections_db
    )

    assert result["ok"] is False
    # The adapter's raw diagnostic is never stored on account evidence: the cause is reported.
    assert result["error"] == "account_discovery_failed"
    envelope = harness_models.read_discovery_envelope("codex", db=connections_db)
    assert list(_model_rows(connections_db, "codex")) == ["gpt-5.6-sol"]
    assert _model_rows(connections_db, "codex")["gpt-5.6-sol"]["available"] == 1
    assert envelope["last_error"] == "account_discovery_failed"


@pytest.mark.asyncio
async def test_selection_refresh_is_unknown_only_and_never_rewrites(connections_db, monkeypatch):
    import connections.harness_models as harness_models
    import connections.harness_refresh as harness_refresh

    _seed_connection(connections_db, "codex", {"binary": "/bin/codex"})
    seed_catalog(
        "codex",
        harness_models.normalize_codex_models(
            _codex_payload_with_context("gpt-5.6-sol"),
            harness_version="0.144.0",
            timestamp=datetime.now(timezone.utc).isoformat(),
        ),
        harness_version="0.144.0",
        source=harness_models.CODEX_SOURCE,
        timestamp=datetime.now(timezone.utc).isoformat(),
        db=connections_db,
    )
    calls: list[tuple[str, str]] = []

    async def _version(_binary: str) -> str:
        return "0.144.0"

    async def _refresh(
        provider: str, *, reason: str, harness_version=None, db=None,
    ):
        calls.append((provider, reason))
        return {"provider": provider, "ok": True, "reason": reason}

    monkeypatch.setattr(harness_models, "binary_version", _version)
    monkeypatch.setattr(harness_models, "refresh_provider", _refresh)

    assert await harness_refresh.refresh_for_selection(
        "codex/gpt-5.6-sol", db=connections_db
    ) is None
    result = await harness_refresh.refresh_for_selection(
        "codex/gpt-5.6-terra", db=connections_db
    )
    assert result["reason"] == "unknown_selection"
    assert calls == [("codex", "unknown_selection")]
    assert await harness_refresh.refresh_for_selection(
        "openai/gpt-5.5", db=connections_db
    ) is None


@pytest.mark.asyncio
async def test_saved_unknown_selectors_batch_one_version_probe_and_refresh_per_provider(
    connections_db, monkeypatch
):
    import connections.harness_models as harness_models
    import connections.harness_refresh as harness_refresh

    _seed_connection(connections_db, "codex", {"binary": "/bin/codex"})
    now = datetime.now(timezone.utc).isoformat()
    seed_catalog(
        "codex",
        harness_models.normalize_codex_models(
            _codex_payload_with_context("gpt-5.6-sol"),
            harness_version="0.144.0",
            timestamp=now,
        ),
        harness_version="0.144.0",
        source=harness_models.CODEX_SOURCE,
        timestamp=now,
        db=connections_db,
    )
    version_calls: list[str] = []
    refresh_calls: list[tuple[str, str]] = []

    async def _version(binary: str):
        version_calls.append(binary)
        return "0.144.0"

    async def _refresh(
        provider: str, *, reason: str, harness_version=None, db=None,
    ):
        refresh_calls.append((provider, reason))
        return {"provider": provider, "ok": True, "reason": reason}

    monkeypatch.setattr(harness_models, "binary_version", _version)
    monkeypatch.setattr(harness_models, "refresh_provider", _refresh)

    result = await harness_refresh.refresh_for_selections(
        [
            "codex/gpt-5.6-sol",
            "codex/gpt-5-codex",
            "codex/gpt-5.6-terra",
        ],
        db=connections_db,
    )

    assert version_calls == ["/bin/codex"]
    assert refresh_calls == [("codex", "unknown_selection")]
    assert result == [{"provider": "codex", "ok": True,
                       "reason": "unknown_selection"}]

    repeated = await harness_refresh.refresh_for_selections(
        ["codex/gpt-5-codex", "codex/gpt-5.6-terra"],
        db=connections_db,
    )
    assert repeated == []
    assert refresh_calls == [("codex", "unknown_selection")]
    assert version_calls == ["/bin/codex", "/bin/codex"]


@pytest.mark.asyncio
@pytest.mark.real_harness_cli
async def test_binary_version_timeout_kills_and_reaps(monkeypatch):
    import connections.harness_models as harness_models

    class _Process:
        returncode = None
        killed = False
        communicate_calls = 0

        async def communicate(self):
            self.communicate_calls += 1
            return b"", b""

        def kill(self):
            self.killed = True

    process = _Process()

    async def _spawn(*_args, **_kwargs):
        return process

    async def _timeout(awaitable, *, timeout):
        awaitable.close()
        raise asyncio.TimeoutError

    monkeypatch.setattr(harness_models.asyncio, "create_subprocess_exec", _spawn)
    monkeypatch.setattr(harness_models.asyncio, "wait_for", _timeout)

    with pytest.raises(RuntimeError, match="--version timed out"):
        await harness_models.binary_version("hung-harness")

    assert process.killed is True
    assert process.communicate_calls == 1


@pytest.mark.asyncio
async def test_concurrent_refreshes_share_one_provider_process(
    connections_db, monkeypatch,
):
    import connections.harness_models as harness_models

    _seed_connection(connections_db, "codex", {"binary": "/bin/codex"})
    started = asyncio.Event()
    release = asyncio.Event()
    discovery_calls = 0

    async def _version(_binary: str, env=None):
        return "0.144.0"

    async def _discover(_binary: str, context=None):
        nonlocal discovery_calls
        discovery_calls += 1
        started.set()
        await release.wait()
        return _staged(_codex_payload("gpt-5.6-sol"))

    monkeypatch.setattr(harness_models, "binary_version", _version)
    monkeypatch.setitem(
        harness_models._ADAPTERS,
        "codex",
        (_discover, harness_models.normalize_codex_models, harness_models.CODEX_SOURCE),
    )

    first = asyncio.create_task(harness_models.refresh_provider(
        "codex", reason="startup", db=connections_db,
    ))
    await started.wait()
    second = asyncio.create_task(harness_models.refresh_provider(
        "codex", reason="selector_load", db=connections_db,
    ))
    release.set()
    first_result, second_result = await asyncio.gather(first, second)

    assert discovery_calls == 1
    assert first_result["reason"] == "startup"
    assert second_result["reason"] == "selector_load"


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_pin_completed_single_flight(
    connections_db, monkeypatch,
):
    import connections.harness_models as harness_models

    _seed_connection(connections_db, "codex", {"binary": "/bin/codex"})
    started = asyncio.Event()
    release = asyncio.Event()
    discovery_calls = 0

    async def _version(_binary: str, env=None):
        return "0.144.0"

    async def _discover(_binary: str, context=None):
        nonlocal discovery_calls
        discovery_calls += 1
        started.set()
        await release.wait()
        return _staged(_codex_payload("gpt-5.6-sol"))

    monkeypatch.setattr(harness_models, "binary_version", _version)
    monkeypatch.setitem(
        harness_models._ADAPTERS,
        "codex",
        (_discover, harness_models.normalize_codex_models, harness_models.CODEX_SOURCE),
    )

    waiter = asyncio.create_task(harness_models.refresh_provider(
        "codex", reason="startup", db=connections_db,
    ))
    await started.wait()
    shared = next(iter(harness_models._REFRESH_TASKS.values()))  # keyed by account generation
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    release.set()
    await shared
    await asyncio.sleep(0)

    assert not harness_models._REFRESH_TASKS
    result = await harness_models.refresh_provider(
        "codex", reason="explicit", db=connections_db,
    )
    assert result["reason"] == "explicit"
    assert discovery_calls == 2
