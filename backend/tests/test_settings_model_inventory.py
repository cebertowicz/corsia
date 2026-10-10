"""Settings inventory window facts stay aligned with runtime resolution."""

from __future__ import annotations

import sqlite3


def test_settings_inventory_exposes_saved_and_resolved_windows(isolated_db):
    from api.discovered_models import _settings_inventory
    from infra.database import get_db, init_model_profiles

    codex_id = "codex/gpt-5.6-luna"
    omlx_id = "omlx@srv-test/Qwen3.8-Flash-Next"
    db = get_db()
    try:
        init_model_profiles()
        db.executemany(
            "INSERT INTO models (id,harness,provider,provider_model_id,litellm_model,"
            "display_name,context_window,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,1000000,'t0','t0')",
            [
                (codex_id, "codex", "codex", "gpt-5.6-luna", codex_id, "GPT-5.6-Luna"),
                (omlx_id, "native", "omlx", "Qwen3.8-Flash-Next", omlx_id, "Qwen3.8 Flash Next"),
            ],
        )
        db.executemany(
            "INSERT INTO model_profiles (model_id,context_window) VALUES (?,?)",
            [(codex_id, 262_144), (omlx_id, 262_144)],
        )
        db.commit()
    finally:
        db.close()

    rows = {row["id"]: row for row in _settings_inventory()}

    for model_id in (codex_id, omlx_id):
        assert rows[model_id]["provider_context_length"] == 1_000_000
        assert rows[model_id]["context_window_override"] == 262_144
        assert rows[model_id]["context_length"] == 262_144


def test_settings_inventory_projects_claude_display_name_from_resolved_identity(isolated_db):
    from api.discovered_models import _settings_inventory
    from infra.database import get_db

    model_id = "claude-code/settings-sonnet"
    db = get_db()
    try:
        db.execute(
            "INSERT INTO models (id,harness,provider,provider_model_id,litellm_model,display_name,alias,"
            "metadata_json,context_window,created_at,updated_at) "
            "VALUES (?,'claude-code','claude-code-settings','sonnet',?,'Sonnet',NULL,?,200000,'t0','t0')",
            (model_id, model_id, '{"resolved_identity":"claude-sonnet-5-5"}'),
        )
        db.commit()
    finally:
        db.close()

    rows = {row["id"]: row for row in _settings_inventory()}

    assert rows[model_id]["display_name"] == "Sonnet 5.5"


def test_settings_inventory_projects_harness_alias_of_only_for_alias_rows(isolated_db):
    from api.discovered_models import _settings_inventory
    from infra.database import get_db

    alias_id = "codex/settings-sol[1m]"
    plain_id = "codex/settings-sol"
    db = get_db()
    try:
        db.executemany(
            "INSERT INTO models (id,harness,provider,provider_model_id,litellm_model,display_name,"
            "metadata_json,context_window,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,200000,'t0','t0')",
            [
                (alias_id, "codex", "codex", "settings-sol[1m]", alias_id, "Sol 1M", '{"alias_of":"codex/settings-sol"}'),
                (plain_id, "codex", "codex", "settings-sol", plain_id, "Sol", '{}'),
            ],
        )
        db.commit()
    finally:
        db.close()

    rows = {row["id"]: row for row in _settings_inventory()}

    assert rows[alias_id]["alias_of"] == "codex/settings-sol"
    assert "alias_of" not in rows[plain_id]


def test_omlx_rows_keep_server_capacity_and_add_the_resolved_checkpoint_cap(isolated_db):
    from llm.network_catalog_ids import add_catalog_ids
    from infra.database import get_db, init_model_profiles

    model_id = "omlx@srv-test/Qwen3.8-Flash-Next"
    db = get_db()
    try:
        init_model_profiles()
        db.execute(
            "INSERT INTO models (id,harness,provider,provider_model_id,litellm_model,"
            "display_name,context_window,created_at,updated_at) "
            "VALUES (?,'native','omlx','Qwen3.8-Flash-Next',?,'Qwen3.8 Flash Next',1000000,'t0','t0')",
            (model_id, model_id),
        )
        db.execute(
            "INSERT INTO model_profiles (model_id,context_window) VALUES (?,?)",
            (model_id, 262_144),
        )
        db.commit()
    finally:
        db.close()

    models = [{"model_id": "Qwen3.8-Flash-Next", "context_length": 1_000_000}]
    add_catalog_ids(models, "omlx")

    assert models == [{
        "model_id": "Qwen3.8-Flash-Next",
        "context_length": 262_144,
        "provider_context_length": 1_000_000,
        "context_window_source": "saved",
        "context_window_override": 262_144,
        "db_id": model_id,
    }]


def test_network_rows_keep_catalog_id_when_profile_batch_read_fails(isolated_db, monkeypatch):
    from llm import context_window
    from llm.network_catalog_ids import add_catalog_ids
    from infra.database import get_db

    model_id = "omlx@srv-test/Qwen3.8-Flash-Next"
    db = get_db()
    try:
        db.execute(
            "INSERT INTO models (id,harness,provider,provider_model_id,litellm_model,"
            "display_name,context_window,created_at,updated_at) "
            "VALUES (?,'native','omlx','Qwen3.8-Flash-Next',?,'Qwen3.8 Flash Next',1000000,'t0','t0')",
            (model_id, model_id),
        )
        db.commit()
    finally:
        db.close()
    monkeypatch.setattr(
        context_window,
        "saved_overrides_by_catalog_id",
        lambda rows, *, db: (_ for _ in ()).throw(sqlite3.OperationalError("profile read failed")),
    )

    models = [{"model_id": "Qwen3.8-Flash-Next", "context_length": 1_000_000}]
    add_catalog_ids(models, "omlx")

    assert models[0]["db_id"] == model_id
    assert models[0]["context_window_override"] is None
