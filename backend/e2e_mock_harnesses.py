"""The matrix's fixture Claude Code and Codex: their connections and the signed-in accounts they run on."""

from __future__ import annotations

import json
from pathlib import Path

from loguru import logger

# What a fixture Codex home holds: file-backed ChatGPT auth, the only kind a Codex launch accepts.
_CODEX_AUTH = {"auth_mode": "chatgpt", "tokens": {"id_token": "matrix-fixture"}}


def connect_fixture_harnesses(db, binaries: dict[str, str | None], models: dict[str, list[str]], now: str) -> None:
    """Connect each configured fixture harness and sign in its Default: a harness runs only on its own account."""
    from infra.database import write_transaction

    with write_transaction(db, scope="fixture_harnesses") as connection:
        for harness, binary in binaries.items():
            if binary is None:
                continue
            config = {"binary": binary, "app_server": True} if harness == "codex" else {"binary": binary}
            connection.execute(
                """INSERT OR REPLACE INTO connections
                   (id,type,provider,config,status,scope,created_at,updated_at)
                   VALUES (?, 'cli', ?, ?, 'available', 'global', ?, ?)""",
                (f"mock-{harness}", harness, json.dumps(config), now, now),
            )
            _sign_in_default(harness, models.get(harness, []), db=connection)


def _sign_in_default(harness: str, models: list[str], *, db) -> None:
    """A signed-in Default whose ready discovery lists the fixture models.

    Idempotent, so a crash-restart over the same state keeps the account its threads are bound to.
    """
    from connections.account_evidence import mark_discovery
    from connections.account_receipts import capture_receipt
    from connections.vendor_accounts import add_account, default_on_first_signin, resolve_account_id, set_account_state

    if resolve_account_id(harness, db=db) is not None:
        return
    account = add_account(harness, "Default", db=db)
    if harness == "codex":
        (Path(account["source_home"]) / "auth.json").write_text(json.dumps(_CODEX_AUTH))
    account = set_account_state(account["id"], "signed_in", expected_revision=account["revision"], db=db)
    default_on_first_signin(account, db=db)
    receipt = capture_receipt(harness, account_id=account["id"], db=db)
    if not mark_discovery(account["id"], receipt=receipt, status="ready", models={m: {} for m in models}, db=db):
        raise RuntimeError(f"fixture {harness} account discovery was not published")
    logger.info("Fixture {} account signed in: {}", harness, account["id"])
