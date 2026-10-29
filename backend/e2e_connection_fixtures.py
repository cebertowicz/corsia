"""Seed disposable CLI connections for browser tests."""
import json
import os


def seed_connections(db, fixture_binaries, now):
    """Create synthetic connections, optionally preparing the recovery scenario."""
    for harness, binary in fixture_binaries.items():
        if binary is None:
            continue
        config = {"binary": binary}
        if harness == "codex":
            config["app_server"] = True
        db.execute(
            """INSERT OR REPLACE INTO connections
               (id,type,provider,config,status,scope,created_at,updated_at)
               VALUES (?, 'cli', ?, ?, 'available', 'global', ?, ?)""",
            (f"mock-{harness}", harness, json.dumps(config), now, now),
        )
    recovery_cli = os.environ.get("CORSIA_E2E_RECOVERY_CLI")
    if recovery_cli:
        db.execute("DELETE FROM models WHERE harness = 'claude-code' OR provider = 'claude-code'")
        db.execute("DELETE FROM connections WHERE provider = 'claude-code'")
        db.execute(
            """INSERT INTO connections (id,type,provider,config,status,scope,created_at,updated_at)
               VALUES ('recovery-claude', 'cli', 'claude-code', ?, 'needs_auth', 'global', ?, ?)""",
            (json.dumps({"binary": recovery_cli}), now, now),
        )
