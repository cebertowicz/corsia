"""Managed oMLX has one private state-root identity; old rows never route."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_register_managed_server_retires_all_prior_omlx_rows(isolated_db) -> None:
    from infra.db_async import run_in_db
    from llm import network_discovery as discovery
    from llm.local_setup_ownership import is_corsia_managed_server, managed_omlx_paths
    from llm.local_setup_runtime import register_managed_server

    legacy_managed = await run_in_db(discovery.upsert_server,
        provider="omlx", instance_id="127.0.0.1:8000", instance_name="Old local oMLX",
        host="127.0.0.1", port=8000, status="online", kind="managed",
    )
    foreign_external = await run_in_db(discovery.upsert_server,
        provider="omlx", instance_id="127.0.0.1:9000", instance_name="Someone else's oMLX",
        host="127.0.0.1", port=9000, status="online", kind="external", dialect="openai",
    )
    paths = managed_omlx_paths()

    server_id, _key = await register_managed_server(
        port=paths.port, base_path=str(paths.base_path), model_dir=str(paths.model_dir),
    )

    registered = await run_in_db(discovery.get_server, server_id)
    assert registered is not None
    assert is_corsia_managed_server(registered)
    assert (await run_in_db(discovery.get_server, legacy_managed))["status"] == "removed"
    assert (await run_in_db(discovery.get_server, foreign_external))["status"] == "online"


def test_legacy_service_migrates_only_after_ownership_proof(monkeypatch) -> None:
    from llm import local_setup_ownership as ownership

    commands: list[list[str]] = []
    monkeypatch.setattr(ownership, "legacy_setup_owns_current_omlx", lambda: True)
    monkeypatch.setattr(ownership.shutil, "which", lambda name: "/brew" if name == "brew" else None)
    monkeypatch.setattr(ownership, "run_command", lambda command: commands.append(command))

    assert ownership.retire_legacy_owned_omlx_service() is True
    assert commands == [["/brew", "services", "stop", ownership.OMLX_FORMULA]]

    monkeypatch.setattr(ownership, "legacy_setup_owns_current_omlx", lambda: False)
    assert ownership.retire_legacy_owned_omlx_service() is False
    assert commands == [["/brew", "services", "stop", ownership.OMLX_FORMULA]]


def test_catalog_never_routes_a_foreign_omlx_row(isolated_db) -> None:
    from infra.database import get_db
    from llm import catalog, network_discovery as discovery

    server_id = discovery.upsert_server(
        provider="omlx", instance_id="127.0.0.1:9000", instance_name="Foreign local oMLX",
        host="127.0.0.1", port=9000, status="online", kind="managed",
    )
    external_id = discovery.upsert_server(
        provider="omlx", instance_id="remote.example.com:9000", instance_name="Configured remote oMLX",
        host="remote.example.com", port=9000, status="online", kind="external", dialect="openai",
    )
    model_id = "foreign-omlx-model"
    litellm_model = f"omlx@{server_id}/someone/model"
    external_model_id = "external-omlx-model"
    external_litellm_model = f"omlx@{external_id}/someone/remote-model"
    db = get_db()
    db.execute(
        """INSERT INTO models
           (id, provider, provider_model_id, litellm_model, display_name, backend,
            source, enabled, available, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 1, 1, 'now', 'now')""",
        (model_id, "omlx", "someone/model", litellm_model, "Foreign model", "network", "discovered"),
    )
    db.execute(
        """INSERT INTO models
           (id, provider, provider_model_id, litellm_model, display_name, backend,
            source, enabled, available, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 1, 1, 'now', 'now')""",
        (external_model_id, "omlx", "someone/remote-model", external_litellm_model,
         "Configured remote model", "network", "discovered"),
    )
    db.commit()
    db.close()

    assert catalog.get_model(model_id) is None
    assert catalog.get_model_by_litellm(litellm_model) is None
    assert catalog.is_valid_model_choice(litellm_model) is False
    assert all(row["id"] != model_id for row in catalog.get_selectable_models())
    assert catalog.get_model(external_model_id)["id"] == external_model_id
    assert catalog.get_model_by_litellm(external_litellm_model)["id"] == external_model_id
    assert catalog.is_valid_model_choice(external_litellm_model) is True
    assert any(row["id"] == external_model_id for row in catalog.get_selectable_models())


def test_only_state_root_row_is_corsia_managed() -> None:
    from infra.config import STATE_ROOT
    from llm.local_setup_ownership import is_corsia_managed_server

    base = STATE_ROOT / "managed-omlx"
    row = {
        "provider": "omlx", "kind": "managed", "instance_id": "corsia-managed",
        "host": "127.0.0.1",
    }
    metadata = {
        "corsia_managed": True,
        "base_path": str(base),
        "model_dir": str(base / "models"),
    }

    assert is_corsia_managed_server(row, metadata)
    assert not is_corsia_managed_server({**row, "instance_id": "legacy"}, metadata)
    assert not is_corsia_managed_server(row, {**metadata, "base_path": "/tmp/foreign"})
