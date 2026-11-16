"""Harness catalog checks never hold a request: stale rows serve, a check runs behind."""

from __future__ import annotations

import asyncio

import pytest

import connections.harness_models as harness_models
import connections.harness_refresh as harness_refresh
from vendor_account_fixtures import accounts  # noqa: F401


@pytest.fixture
def blocked_checks(monkeypatch):
    """Every background check parks until released; records who started."""
    monkeypatch.setattr(harness_models, "_CHECK_TASKS", {})
    monkeypatch.setattr(harness_refresh, "_PENDING", {})
    release = asyncio.Event()
    started: list[str] = []

    async def _refresh_if_needed(provider, **_kwargs):
        started.append(provider)
        await release.wait()
        return None

    monkeypatch.setattr(harness_refresh, "_refresh_if_needed", _refresh_if_needed)
    yield started, release
    release.set()


async def _drain():
    tasks = tuple(harness_models._CHECK_TASKS.values())
    await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_models_endpoint_answers_while_the_stale_check_is_still_running(
    monkeypatch, blocked_checks,
):
    import api.models as api_models
    import llm.capabilities as capabilities
    import llm.model_status as model_status

    started, release = blocked_checks
    monkeypatch.setattr(model_status, "get_model_status_rows", lambda: [{"id": "row"}])
    monkeypatch.setattr(capabilities, "get_active_local_threads", lambda _active: [])

    result = await asyncio.wait_for(api_models.list_models(), timeout=1.0)
    await asyncio.sleep(0)

    assert result == {"models": [{"id": "row"}], "active_local_threads": 0}
    assert sorted(started) == sorted(harness_models._ADAPTERS)
    assert all(not task.done() for task in harness_models._CHECK_TASKS.values())
    release.set()
    await _drain()
    assert harness_models._CHECK_TASKS == {}


@pytest.mark.asyncio
async def test_background_check_is_single_flight_per_provider(blocked_checks):
    started, release = blocked_checks

    harness_refresh.schedule_stale_refresh()
    harness_refresh.schedule_stale_refresh()
    harness_refresh.schedule_refresh_for_selections(["codex/gpt-5.6-sol"])
    await asyncio.sleep(0)

    assert sorted(started) == sorted(harness_models._ADAPTERS)
    release.set()
    await _drain()


@pytest.mark.asyncio
async def test_known_selector_is_admitted_without_waiting_for_discovery(monkeypatch):
    scheduled: list[str] = []

    async def _must_not_wait(_model):
        raise AssertionError("a known selector must not wait for discovery")

    # The mocked worker never drains its queued selection; keep that queue local.
    monkeypatch.setattr(harness_refresh, "_PENDING", {})
    monkeypatch.setattr(harness_refresh, "_known_selectors", lambda _p, db=None: {"gpt-5.6-sol"})
    monkeypatch.setattr(harness_refresh, "refresh_for_selection", _must_not_wait)
    monkeypatch.setattr(
        harness_models, "start_background_check",
        lambda provider, _check: scheduled.append(provider),
    )

    await asyncio.wait_for(
        harness_refresh.prepare_selection("codex/gpt-5.6-sol"), timeout=1.0,
    )

    assert scheduled == ["codex"]


@pytest.mark.asyncio
async def test_unknown_selector_waits_for_one_discovery(monkeypatch):
    refreshed: list[str | None] = []

    async def _refresh(model):
        refreshed.append(model)
        return {"ok": True}

    monkeypatch.setattr(harness_refresh, "_known_selectors", lambda _p, db=None: set())
    monkeypatch.setattr(harness_refresh, "refresh_for_selection", _refresh)

    await harness_refresh.prepare_selection("codex/gpt-7-new")
    await harness_refresh.prepare_selection("not-a-harness/model")

    assert refreshed == ["codex/gpt-7-new"]


@pytest.mark.asyncio
async def test_shutdown_cancels_and_joins_background_checks(monkeypatch, blocked_checks):
    monkeypatch.setattr(harness_models, "_REFRESH_TASKS", {})
    harness_refresh.schedule_stale_refresh()
    await asyncio.sleep(0)
    tasks = tuple(harness_models._CHECK_TASKS.values())

    await asyncio.wait_for(harness_models.shutdown_refresh_tasks(), timeout=1.0)

    assert tasks and all(task.cancelled() for task in tasks)
    assert harness_models._CHECK_TASKS == {}


@pytest.mark.asyncio
async def test_failed_background_check_is_logged_not_raised(monkeypatch, caplog):
    monkeypatch.setattr(harness_models, "_CHECK_TASKS", {})

    async def _boom():
        raise RuntimeError("codex --version timed out")

    harness_models.start_background_check("codex", _boom)
    await _drain()
    await asyncio.sleep(0)

    assert harness_models._CHECK_TASKS == {}
    assert "codex --version timed out" in caplog.text


@pytest.mark.asyncio
async def test_selections_that_arrive_mid_check_run_on_its_next_pass(monkeypatch):
    monkeypatch.setattr(harness_models, "_CHECK_TASKS", {})
    monkeypatch.setattr(harness_refresh, "_PENDING", {})
    release = asyncio.Event()
    passes: list[tuple[str, set[str]]] = []

    async def _refresh_if_needed(provider, *, selectors=None, **_kwargs):
        passes.append((provider, set(selectors or ())))
        await release.wait()

    monkeypatch.setattr(harness_refresh, "_refresh_if_needed", _refresh_if_needed)

    # Two more thread lists land while the first check runs; their saved
    # selectors are checked together on one more pass, not dropped.
    harness_refresh.schedule_refresh_for_selections(["codex/a"])
    await asyncio.sleep(0)
    harness_refresh.schedule_refresh_for_selections(["codex/b"])
    harness_refresh.schedule_refresh_for_selections(["codex/c", "codex/b"])
    release.set()
    await _drain()

    assert passes == [("codex", {"a"}), ("codex", {"b", "c"})]
    assert harness_refresh._PENDING == {}


@pytest.mark.asyncio
async def test_an_unchanged_binary_is_probed_once_and_a_replaced_one_again(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr(harness_refresh, "_PROBED", {})
    binary = tmp_path / "codex"
    binary.write_text("#!/bin/sh\necho 1.0.0\n")
    binary.chmod(0o755)
    probes: list[str] = []

    async def _version(path: str) -> str:
        probes.append(path)
        return f"1.0.{len(probes)}"

    monkeypatch.setattr(harness_models, "_version_or_none", _version)

    async def _version_now(*, forced: bool = False) -> str | None:
        return await harness_refresh._harness_version("codex", str(binary), forced=forced)

    assert [await _version_now() for _ in range(3)] == ["1.0.1"] * 3
    assert len(probes) == 1

    # An update installs a new file over the old path.
    update = tmp_path / "codex.new"
    update.write_text("#!/bin/sh\necho 1.0.2 with a longer body\n")
    update.chmod(0o755)
    update.replace(binary)
    assert await _version_now() == "1.0.2"
    assert await _version_now() == "1.0.2"
    assert len(probes) == 2

    # A forced refresh (startup) always re-asks.
    assert await _version_now(forced=True) == "1.0.3"


@pytest.mark.asyncio
async def test_a_stale_check_during_the_startup_re_ask_asks_each_account_once(accounts, monkeypatch):
    """The startup re-ask is the provider's running check: a stale check asked for meanwhile joins it."""
    from collections import Counter
    from datetime import datetime, timedelta, timezone

    import connections.account_discovery_refresh as refresh_module
    from connections.account_discovery_types import StagedDiscovery
    from infra.database import write_transaction

    _db, default, second, third = accounts
    monkeypatch.setattr(harness_models, "_CHECK_TASKS", {})
    monkeypatch.setattr(harness_models, "_REFRESH_TASKS", {})
    monkeypatch.setattr(harness_refresh, "_PENDING", {})
    monkeypatch.setattr(harness_refresh, "_PROBED", {})
    monkeypatch.setattr(harness_refresh, "_notify_models_changed", lambda: None)

    async def discover(_binary, context=None):
        await asyncio.sleep(0.05)
        return StagedDiscovery({"rows": ["test"]})

    def normalize(payload, *, harness_version, timestamp):
        return [{"selector_id": name, "available": True, "context_window": 200000} for name in payload["rows"]]

    async def version(_binary, env=None):
        return "1.0"

    monkeypatch.setattr(harness_models, "_ADAPTERS", {"codex": (discover, normalize, "synthetic")})
    monkeypatch.setattr(harness_models, "binary_version", version)
    monkeypatch.setattr(harness_refresh, "_fingerprint", lambda binary: ("/bin/test", 1, 1, 1))
    await harness_refresh.refresh_harness_catalogs(reason="startup")
    # The backend restarts more than an hour after the last ask, so the catalog reads as stale.
    with write_transaction(None, scope="test_age") as connection:
        envelope = harness_models.read_discovery_envelope("codex", db=connection)
        envelope["refreshed_at"] = envelope["attempted_at"] = (
            datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        harness_models._save_discovery_envelope("codex", envelope, db=connection)
    harness_refresh._PROBED.clear()

    commits: Counter[str] = Counter()
    original = refresh_module.commit_discovery_bundle
    loop = asyncio.get_running_loop()

    def counted(provider, staged, rows, *, context, **kwargs):
        result = original(provider, staged, rows, context=context, **kwargs)
        commits[context.account_id] += 1
        if sum(commits.values()) == 1:
            # The first commit tells the open picker to reload (from the commit's worker thread,
            # through the loop), and its read asks for a stale check.
            loop.call_soon_threadsafe(harness_refresh.schedule_stale_refresh)
        return result

    monkeypatch.setattr(refresh_module, "commit_discovery_bundle", counted)
    await harness_refresh.refresh_harness_catalogs(reason="startup")
    await asyncio.gather(*harness_models._CHECK_TASKS.values())

    assert commits == {default["id"]: 1, second["id"]: 1, third["id"]: 1}
    assert harness_models._CHECK_TASKS == {}
