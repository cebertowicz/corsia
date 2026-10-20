"""Test fixtures for connected Google accounts."""

from __future__ import annotations

import sqlite3

from connections.connected_accounts import GOOGLE_PROVIDER, ONLY_CORSIA, create_account
from infra.database import write_transaction

ALL_PROJECTS = {"mode": "all", "project_ids": [], "outside_projects": False}
ONLY_CORSIA_REACH = {"mode": ONLY_CORSIA, "project_ids": [], "outside_projects": False}


def make_only_corsia(account_id: str, *, db: sqlite3.Connection | None = None) -> None:
    """Give an account the Only CORSIA reach; in production only the legacy Google migration writes it."""
    with write_transaction(db, scope="test_only_corsia") as conn:
        conn.execute("DELETE FROM connected_account_reach WHERE account_id=?", (account_id,))
        conn.execute(
            "UPDATE connected_accounts SET reach_mode=?,outside_projects=0 WHERE id=?", (ONLY_CORSIA, account_id),
        )


def only_corsia_account(address: str, *, db: sqlite3.Connection | None = None, **fields: object) -> dict[str, object]:
    """Create a Google account that only the CORSIA agent reaches; ``fields`` go to ``create_account``."""
    account = create_account(GOOGLE_PROVIDER, address, reach=ALL_PROJECTS, db=db, **fields)  # type: ignore[arg-type]
    make_only_corsia(str(account["id"]), db=db)
    return {**account, "reach": ONLY_CORSIA_REACH}
