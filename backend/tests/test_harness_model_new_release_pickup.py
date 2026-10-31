"""A model the provider releases server-side reaches the picker without a CLI bump.

Codex and Claude Code list models from their backends, so a new model can
appear with the installed CLI version unchanged. The age-gated refresh cannot
see that; a restart must re-ask, and a running instance must re-ask within
``CACHE_MAX_AGE`` of its last success.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from vendor_account_fixtures import seed_catalog

pytestmark = pytest.mark.usefixtures("signed_in_defaults")  # a harness turn needs a signed-in Default: no ambient login

_VERSION = "1.0.0"
# Placeholder selectors: the test pins the refresh rule, never a model name.
_KNOWN = "known-model"
_RELEASED = "released-since-last-ask"


def _codex_payload(*model_ids: str) -> dict:
    return {"data": [
        {"id": model_id, "model": model_id, "displayName": model_id,
         "description": "", "hidden": False,
         "contextMetadata": {"context_window": 272_000}}
        for model_id in model_ids
    ]}


def _seed_codex(monkeypatch, listed: list[str]) -> list[str]:
    """An available Codex connector whose backend currently lists ``listed``."""
    import connections.harness_models as harness_models
    from connections.models import ConnectionStatus, ConnectionType, upsert_connection

    upsert_connection(
        type=ConnectionType.CLI, provider="codex",
        status=ConnectionStatus.AVAILABLE, config={"binary": "/bin/codex"},
    )
    discoveries: list[str] = []

    async def _discover(binary: str, context=None):
        from connections.account_discovery_types import StagedDiscovery

        discoveries.append(binary)
        return StagedDiscovery(payload=_codex_payload(*listed))

    async def _version(_binary: str, env=None) -> str:
        return _VERSION

    # Only Codex: no test may spawn a real harness binary.
    monkeypatch.setattr(harness_models, "_ADAPTERS", {
        "codex": (
            _discover, harness_models.normalize_codex_models,
            harness_models.CODEX_SOURCE,
        ),
    })
    monkeypatch.setattr(harness_models, "binary_version", _version)
    monkeypatch.setattr(harness_models, "_REFRESH_TASKS", {})
    return discoveries


def _store(model_ids: list[str], at: datetime) -> None:
    from connections.harness_models import (
        CODEX_SOURCE, normalize_codex_models,
    )

    seed_catalog(
        "codex",
        normalize_codex_models(
            _codex_payload(*model_ids), harness_version=_VERSION,
            timestamp=at.isoformat(),
        ),
        harness_version=_VERSION, source=CODEX_SOURCE, timestamp=at.isoformat(),
    )


def _available_codex() -> set[str]:
    """The Codex models readers see as available: what the signed-in account lists now."""
    from infra.database import get_db
    from llm.harness_catalog import project_stored_rows

    db = get_db()
    try:
        stored = db.execute("SELECT * FROM models WHERE harness = 'codex'").fetchall()
        return {row["provider_model_id"] for row in project_stored_rows(stored, db=db) if row["available"]}
    finally:
        db.close()


@pytest.mark.asyncio
async def test_startup_re_asks_behind_a_fresh_cache(isolated_db, monkeypatch):
    import connections.harness_models as harness_models
    import connections.harness_refresh as harness_refresh

    discoveries = _seed_codex(
        monkeypatch, listed=[_KNOWN, _RELEASED],
    )
    _store([_KNOWN], datetime.now(timezone.utc))
    # The age gate alone would skip: cache minutes old, version unchanged.
    harness_refresh.schedule_stale_refresh()
    await asyncio.gather(*harness_models._CHECK_TASKS.values())
    assert discoveries == []

    results = await harness_refresh.refresh_harness_catalogs(reason="startup")

    assert discoveries == ["/bin/codex"]
    assert [(r["provider"], r["ok"], r["reason"]) for r in results] == [
        ("codex", True, "startup"),
    ]
    assert _available_codex() == {_KNOWN, _RELEASED}


def test_running_instance_re_asks_within_the_age_bound(isolated_db, monkeypatch):
    from connections.harness_models import CACHE_MAX_AGE, cache_refresh_reason

    _seed_codex(monkeypatch, listed=[_KNOWN])
    stored = datetime(2026, 9, 22, 12, tzinfo=timezone.utc)
    _store([_KNOWN], stored)

    def reason(after: timedelta) -> str | None:
        return cache_refresh_reason(
            "codex", harness_version=_VERSION, now=stored + after,
        )

    assert CACHE_MAX_AGE <= timedelta(hours=1)
    assert reason(timedelta(minutes=30)) is None
    assert reason(CACHE_MAX_AGE + timedelta(minutes=1)) == "stale"
