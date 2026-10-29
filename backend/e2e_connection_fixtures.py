"""Seed disposable CLI connections for browser tests."""
import json


def seed_connections(db, fixture_binaries, now):
    """Create synthetic CLI connections for the mock application."""
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
