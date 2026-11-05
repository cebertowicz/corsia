"""Missing context-window evidence reuses the catalog until it goes stale.

A discovery that lists a model but cannot prove its window leaves that row with
``context_window IS NULL``. Selector loads (``/api/models``, the thread list,
selection refreshes) must not respawn harness discovery on every request for
that state: the connector refresh envelope already owns retry timing.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from vendor_account_fixtures import seed_catalog

pytestmark = pytest.mark.usefixtures("signed_in_defaults")  # a harness turn needs a signed-in Default: no ambient login


async def _stale_pass() -> None:
    """Run the request-path stale check and wait for it to settle."""
    import connections.harness_models as harness_models
    import connections.harness_refresh as harness_refresh

    harness_refresh.schedule_stale_refresh()
    await asyncio.gather(*harness_models._CHECK_TASKS.values())


def _claude_payload(*, with_context: bool) -> dict:
    row = {
        "value": "claude-fable-5-1[1m]",
        "resolvedModel": "claude-fable-5-1",
        "displayName": "Fable",
        "description": "Most capable",
        "supportsEffort": True,
        "supportedEffortLevels": ["low", "high"],
    }
    if with_context:
        row["contextUsage"] = {"rawMaxTokens": 1_000_000, "maxTokens": 1_000_000}
    return {"models": [row]}


def _seed_claude_connection(monkeypatch, *, context_payloads: list[bool]) -> list[str]:
    """Seed an available Claude Code connector whose discovery is recorded."""
    import connections.harness_models as harness_models
    from connections.models import ConnectionStatus, ConnectionType, upsert_connection

    upsert_connection(
        type=ConnectionType.CLI,
        provider="claude-code",
        status=ConnectionStatus.AVAILABLE,
        config={"binary": "/bin/claude"},
    )
    discoveries: list[str] = []

    async def _discover(binary: str, context=None):
        from connections.account_discovery_types import StagedDiscovery

        discoveries.append(binary)
        return StagedDiscovery(payload=_claude_payload(with_context=context_payloads[len(discoveries) - 1]))

    async def _version(_binary: str, env=None) -> str:
        return "2.1.268"

    # Claude Code is the only harness here: the shared fixture seeds Codex as
    # available, and a stale pass would otherwise ask a real codex binary.
    monkeypatch.setattr(harness_models, "_ADAPTERS", {
        "claude-code": (
            _discover, harness_models.normalize_claude_models, harness_models.CLAUDE_SOURCE,
        ),
    })
    monkeypatch.setattr(harness_models, "binary_version", _version)
    return discoveries


def _age_last_attempt(minutes: int, *, refresh: bool = False) -> None:
    """Move a discovery attempt, and optionally its refresh, into the past."""
    import connections.harness_models as harness_models
    from infra.database import write_transaction

    with write_transaction(None, scope="test_age_discovery_attempt") as connection:
        envelope = harness_models.read_discovery_envelope("claude-code", db=connection)
        attempted = datetime.fromisoformat(envelope["attempted_at"])
        envelope["attempted_at"] = (attempted - timedelta(minutes=minutes)).isoformat()
        if refresh:
            refreshed = datetime.fromisoformat(envelope["refreshed_at"])
            envelope["refreshed_at"] = (refreshed - timedelta(minutes=minutes)).isoformat()
        harness_models._save_discovery_envelope("claude-code", envelope, db=connection)


def _window(model_id: str) -> int | None:
    """The window readers see: what the signed-in account lists now."""
    from infra.database import get_db
    from llm.harness_catalog import harness_row

    db = get_db()
    try:
        row = harness_row(model_id, db=db)
    finally:
        db.close()
    return None if row is None else row["context_window"]


def _store_discovery(payload: dict, at: datetime) -> None:
    """Persist one successful Claude Code discovery at ``at``."""
    from connections.harness_models import normalize_claude_models

    seed_catalog(
        "claude-code",
        normalize_claude_models(payload, harness_version="2.1.268", timestamp=at.isoformat()),
        harness_version="2.1.268",
        source="claude-stream-json:list_models",
        timestamp=at.isoformat(),
    )


def _reason(at: datetime, version: str = "2.1.268") -> str | None:
    from connections.harness_models import cache_refresh_reason

    return cache_refresh_reason("claude-code", harness_version=version, now=at)


def test_missing_context_evidence_waits_for_stale_catalog_and_recovers(isolated_db):
    from connections.models import ConnectionStatus, ConnectionType, upsert_connection

    upsert_connection(
        type=ConnectionType.CLI, provider="claude-code",
        status=ConnectionStatus.AVAILABLE, config={"binary": "/bin/claude"},
    )
    now = datetime(2026, 9, 10, 22, tzinfo=timezone.utc)
    _store_discovery(_claude_payload(with_context=False), now)

    assert _reason(now + timedelta(minutes=1)) is None
    assert _reason(now + timedelta(minutes=1), "2.1.269") == "version_changed"
    assert _reason(now + timedelta(minutes=6)) is None
    assert _reason(now + timedelta(hours=1, seconds=1)) == "missing_context_evidence"

    _store_discovery(
        _claude_payload(with_context=True), now + timedelta(hours=1, seconds=1),
    )
    assert _window("claude-code/claude-fable-5-1[1m]") == 1_000_000
    assert _reason(now + timedelta(minutes=13)) is None
    assert _reason(now + timedelta(days=2)) == "stale"


@pytest.mark.asyncio
async def test_selector_loads_reuse_catalog_until_context_evidence_goes_stale(
    isolated_db, monkeypatch,
):
    import connections.harness_models as harness_models
    import connections.harness_refresh as harness_refresh

    discoveries = _seed_claude_connection(
        monkeypatch, context_payloads=[False, True],
    )

    await _stale_pass()
    assert discoveries == ["/bin/claude"]
    assert _window("claude-code/claude-fable-5-1[1m]") is None

    for _ in range(3):
        await _stale_pass()
        await harness_refresh.refresh_for_selections(["claude-code/claude-fable-5-1[1m]"])
    assert discoveries == ["/bin/claude"]
    envelope = harness_models.read_discovery_envelope("claude-code")
    assert envelope["last_error"] is None

    _age_last_attempt(minutes=61, refresh=True)
    await _stale_pass()
    assert discoveries == ["/bin/claude", "/bin/claude"]
    assert _window("claude-code/claude-fable-5-1[1m]") == 1_000_000

    _age_last_attempt(minutes=6)
    await _stale_pass()
    assert discoveries == ["/bin/claude", "/bin/claude"]


@pytest.mark.asyncio
async def test_request_checks_ask_an_unchanged_cli_its_version_once(
    isolated_db, monkeypatch,
):
    import connections.harness_models as harness_models
    import connections.harness_refresh as harness_refresh

    _seed_claude_connection(monkeypatch, context_payloads=[True])
    monkeypatch.setattr(harness_refresh, "_PROBED", {})
    fingerprint = ["/opt/claude/2.1.268/claude", 7, 1024, 1]
    monkeypatch.setattr(harness_refresh, "_fingerprint", lambda _b: tuple(fingerprint))
    probes: list[str] = []

    async def _version(binary: str) -> str:
        if binary == "/bin/claude":  # the stale pass checks every harness
            probes.append(binary)
        return "2.1.268"

    monkeypatch.setattr(harness_models, "binary_version", _version)

    for _ in range(4):
        await _stale_pass()
        await harness_refresh.refresh_for_selections(["claude-code/claude-fable-5-1[1m]"])
    assert probes == ["/bin/claude"]

    fingerprint[3] = 2  # the CLI updated itself in place
    await _stale_pass()
    assert probes == ["/bin/claude", "/bin/claude"]


@pytest.mark.asyncio
async def test_a_catalog_refresh_tells_open_pickers_to_reload(isolated_db, monkeypatch):
    import infra.state.ws as ws

    discoveries = _seed_claude_connection(monkeypatch, context_payloads=[True])
    notified: list[str] = []
    monkeypatch.setattr(ws, "notify_models_changed", lambda: notified.append("models"))

    await _stale_pass()
    assert discoveries == ["/bin/claude"] and notified == ["models"]

    await _stale_pass()  # fresh catalog: nothing re-asked, nothing announced
    assert discoveries == ["/bin/claude"] and notified == ["models"]


@pytest.mark.asyncio
async def test_failed_discovery_keeps_existing_failure_backoff(isolated_db, monkeypatch):
    import connections.harness_models as harness_models

    discoveries = _seed_claude_connection(monkeypatch, context_payloads=[True])

    async def _fail(binary: str, context=None) -> dict:
        discoveries.append(binary)
        raise RuntimeError("Claude Code list_models timed out")

    monkeypatch.setitem(
        harness_models._ADAPTERS,
        "claude-code",
        (_fail, harness_models.normalize_claude_models, harness_models.CLAUDE_SOURCE),
    )

    await _stale_pass()
    await _stale_pass()
    assert discoveries == ["/bin/claude"]
    # The adapter's raw diagnostic is never stored on account evidence: the envelope keeps its cause.
    assert harness_models.read_discovery_envelope("claude-code")["last_error"] == "account_discovery_failed"

    _age_last_attempt(minutes=6)
    await _stale_pass()
    assert discoveries == ["/bin/claude", "/bin/claude"]
