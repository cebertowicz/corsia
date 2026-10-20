"""Legacy own-oMLX adoption cleanup preserves crash ordering and recovery records."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_startup_retries_cleanup_after_row_retirement_crashes_before_marker_cleanup(
        isolated_db, monkeypatch, tmp_path) -> None:
    """Retirement drops the adopted key before a later restart clears choices."""
    import app_startup_runtime
    from infra.db_async import run_in_db
    from infra.key_store_servers import resolve_server, set_server_key
    from llm import local_model, local_setup_upgrade, network_discovery
    from llm.local_setup_managed import adoption_record_paths
    from llm.local_setup_runtime import LOCAL_SERVER_NAME, retire_legacy_setup_omlx_rows

    monkeypatch.setattr("infra.config.STATE_ROOT", tmp_path)
    originals, consent, use_mine = adoption_record_paths()
    originals.write_text('{"originals_global": {"keep": "me"}}', encoding="utf-8")
    consent.write_text("consented\n", encoding="utf-8")
    use_mine.write_text("use mine\n", encoding="utf-8")
    adopted = await run_in_db(network_discovery.upsert_server,
        provider="omlx", instance_id="127.0.0.1:10240", instance_name=LOCAL_SERVER_NAME,
        host="127.0.0.1", port=10240, status="online", kind="managed", source="manual")
    await run_in_db(set_server_key, adopted, "test-key")

    # Simulate a crash after the retirement transaction committed, before cleanup.
    assert await run_in_db(retire_legacy_setup_omlx_rows) == [adopted]
    assert await run_in_db(resolve_server, adopted) is None
    assert consent.exists()
    assert use_mine.exists()

    async def resume() -> bool:
        return False

    async def notice() -> None:
        return None

    monkeypatch.setattr(local_model, "backfill_legacy_record", lambda: None)
    monkeypatch.setattr("llm.local_setup_recovery.resume_pending_setup", resume)
    monkeypatch.setattr("onboarding.local_download_wake.resume_pending_local_download", notice)
    monkeypatch.setattr(local_setup_upgrade, "start_upgrade", lambda: None)

    await app_startup_runtime.resume_local_setup()
    await app_startup_runtime.resume_local_setup()

    assert originals.exists()
    assert not consent.exists()
    assert not use_mine.exists()
