"""Schema v76 moves legacy harness-usage verdicts into the scoped map (#2477)."""

from __future__ import annotations

from tests.schema_rewind import rewind_to_v88_policy

import json
import sqlite3
from contextlib import closing

from connections.harness_usage import stored_usage
from infra.database import initialize_schema
from infra.database_migrations import migrate_to_current
from infra.schema import SCHEMA_VERSION
from vendor_account_fixtures import default_id

OLDER = "2026-09-29T00:00:00+00:00"
NEWER = "2026-09-30T00:00:00+00:00"


def _migrate(tmp_path, configs: dict[str, dict]) -> tuple[dict[str, dict], dict[str, dict]]:
    path = tmp_path / "usage-verdicts-v75.db"
    with closing(sqlite3.connect(path)) as db:
        initialize_schema(db)
        for provider, config in configs.items():
            db.execute(
                "INSERT INTO connections (id,type,provider,config,created_at,updated_at) "
                "VALUES (?,'cli',?,?,'t0','t0')",
                (provider, provider, json.dumps(config)),
            )
        rewind_to_v88_policy(db)

        db.execute('ALTER TABLE project_vendor_accounts DROP COLUMN switch_on_limit')
        db.execute("DROP TABLE system_bulletins")
        db.execute("DROP TABLE system_bulletin_identity")
        db.execute("PRAGMA user_version=75")
        db.commit()
    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row
        result = migrate_to_current(db, SCHEMA_VERSION)
        assert (result.migrated, result.from_version, result.to_version) == (
            True, 75, SCHEMA_VERSION,
        )
        persisted_configs = {
            provider: json.loads(config)
            for provider, config in db.execute("SELECT provider, config FROM connections")
        }
        assert all("usage" not in config for config in persisted_configs.values())
        usages = {provider: stored_usage(provider=provider, db=db, account_id=default_id(provider, db=db)) for provider in persisted_configs}
        return persisted_configs, usages


def test_single_vendor_verdict_becomes_the_account_scope(tmp_path):
    verdict = {"state": "limited", "reason": "five_hour_limit", "observed_at": NEWER}
    config, usages = _migrate(tmp_path, {"claude-code": {"usage": {"vendor_verdict": verdict}}})

    usage = usages["claude-code"]
    assert usage["vendor_verdicts"] == {"null": {**verdict, "scope_model": None}}
    assert "vendor_verdict" not in usage


def test_untimed_legacy_entry_never_overwrites_a_newer_canonical_verdict(tmp_path):
    canonical = {"state": "allowed", "source": "rate_limit_update", "observed_at": NEWER}
    config, usages = _migrate(tmp_path, {"codex": {
        "usage_limit": {"reason": "usage_limit_reached", "source": "turn_error"},
        "usage": {"vendor_verdicts": {"null": canonical},
                  "vendor_verdict": {"state": "limited", "observed_at": OLDER}},
    }})

    assert usages["codex"]["vendor_verdicts"] == {"null": canonical}


def test_usage_limit_with_check_error_migrates_and_no_legacy_key_remains(tmp_path):
    config, usages = _migrate(tmp_path, {
        "codex": {"usage_limit": {"reason": "limit", "source": "test", "observed_at": OLDER,
                                  "check_error": "timeout", "checked_at": NEWER}},
        "claude-code": {"usage": {"vendor_verdict": {"state": "allowed"}}},
    })

    codex = config["codex"]
    assert "usage_limit" not in codex
    assert usages["codex"]["vendor_verdicts"]["null"]["state"] == "limited"
    assert usages["codex"]["vendor_verdicts"]["null"]["observed_at"] == OLDER
    assert usages["codex"]["last_check"] == {"outcome": "unknown", "at": NEWER, "error": "timeout"}
    for provider, provider_config in config.items():
        assert "usage_limit" not in provider_config
        assert "usage" not in provider_config
        assert "vendor_verdict" not in usages[provider]
    assert "observed_at" not in usages["claude-code"]["vendor_verdicts"]["null"]
