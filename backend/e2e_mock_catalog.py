"""The mock mode's model catalog: the seeded test, pressure and fixture-harness models."""

from __future__ import annotations

import json
import os

from loguru import logger

from e2e_mock_harnesses import connect_fixture_harnesses
import e2e_pressure_fixture as pressure_fixture


def seed_mock_catalog() -> None:
    """Insert a synthetic model row for mock-mode UI tests."""
    from infra.database import get_db
    from infra import state as _state

    db = get_db()
    now = "2026-01-01T00:00:00Z"
    db.execute(
        """INSERT OR REPLACE INTO models
           (id, provider, provider_model_id, litellm_model, display_name,
            backend, source, enabled, available, context_window,
            supports_vision, supports_tools, supports_reasoning,
            default_temperature, default_max_tokens,
            discovered_at, last_seen_at, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,1,1,128000,1,1,1,NULL,8192,?,?,?,?)""",
        ("mock-test-model", "mock", "test-model", "mock/test-model",
         "Mock Test Model", "api", "mock", now, now, now, now),
    )
    db.execute(
        """INSERT OR REPLACE INTO models
           (id, provider, provider_model_id, litellm_model, display_name,
            backend, source, enabled, available, context_window,
            supports_vision, supports_tools, supports_reasoning,
            default_temperature, default_max_tokens,
            discovered_at, last_seen_at, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,1,1,?,1,1,1,NULL,8192,?,?,?,?)""",
        (pressure_fixture.PRESSURE_MODEL_BY_HARNESS["native"], "mock", "pressure-model", "mock/pressure-model",
         "Mock Pressure Model", "api", "mock", pressure_fixture.PRESSURE_CONTEXT_WINDOW,
        now, now, now, now),
    )
    fixture_binaries = {
        harness: os.environ.get(env_name)
        for harness, env_name in (
            ("claude-code", "CORSIA_E2E_CC_BINARY"),
            ("codex", "CORSIA_E2E_CODEX_BINARY"),
        )
    }
    if any(fixture_binaries.values()) and not all(fixture_binaries.values()):
        raise RuntimeError("matrix fixture binaries must be configured together")
    harness_rows = (
        (
            "claude-code/fixture-cc", "claude-code", "fixture-cc",
            "claude-code/fixture-cc", "Mock Claude Code", "cc", 393_216,
        ),
        (
            pressure_fixture.PRESSURE_MODEL_BY_HARNESS["claude-code"], "claude-code",
            "fixture-cc-pressure", pressure_fixture.PRESSURE_MODEL_BY_HARNESS["claude-code"],
            "Mock Claude Code Pressure", "cc", pressure_fixture.PRESSURE_CONTEXT_WINDOW,
        ),
        (
            "codex/fixture-codex", "codex", "fixture-codex",
            "codex/fixture-codex", "Mock Codex", "codex", 393_216,
        ),
        (
            pressure_fixture.PRESSURE_MODEL_BY_HARNESS["codex"], "codex",
            "fixture-codex-pressure", pressure_fixture.PRESSURE_MODEL_BY_HARNESS["codex"],
            "Mock Codex Pressure", "codex", pressure_fixture.PRESSURE_CONTEXT_WINDOW,
        ),
    )
    if all(fixture_binaries.values()):
        db.executemany(
            """INSERT OR REPLACE INTO models
               (id,harness,provider,provider_model_id,litellm_model,display_name,
                backend,source,enabled,available,context_window,supports_vision,
                supports_tools,supports_reasoning,default_temperature,
                default_max_tokens,metadata_json,discovered_at,last_seen_at,
                created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,'mock',1,1,?,1,1,1,NULL,8192,?,?,?,?,?)""",
            (
                (
                    model_id, harness, harness, provider_model_id, litellm_model,
                    display_name, backend, context_window,
                    json.dumps({"resolved_identity": provider_model_id}),
                    now, now, now, now,
                )
                for (
                    model_id, harness, provider_model_id, litellm_model,
                    display_name, backend, context_window,
                ) in harness_rows
            ),
        )
    connect_fixture_harnesses(db, fixture_binaries, harness_rows, now)
    db.commit()
    db.close()

    logger.info("Mock catalog seeded: mock/test-model")
