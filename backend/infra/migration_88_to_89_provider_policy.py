"""Replace project_vendor_accounts with workspace/project provider policies."""
from __future__ import annotations

import json
import sqlite3


def migrate_88_to_89(conn) -> None:
    # This migration reads rows by name; leave the startup connection as found.
    caller_factory, conn.row_factory = conn.row_factory, sqlite3.Row
    try:
        conn.executescript("""
            CREATE TABLE provider_policies (
                project_id TEXT REFERENCES projects(id) ON DELETE CASCADE,
                provider_key TEXT NOT NULL CHECK (length(trim(provider_key)) > 0),
                rollover INTEGER NOT NULL DEFAULT 0 CHECK (rollover IN (0,1)),
                order_json TEXT CHECK (order_json IS NULL OR (json_valid(order_json) AND json_type(order_json)='array')),
                disabled_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(disabled_json) AND json_type(disabled_json)='array'),
                revision INTEGER NOT NULL DEFAULT 0
            );
            CREATE UNIQUE INDEX idx_provider_policies_scope ON provider_policies(ifnull(project_id,''), provider_key);
            CREATE TABLE project_policy_overrides (
                project_id TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
                revision INTEGER NOT NULL DEFAULT 0
            );
        """)
        revisions: dict[str, int] = {}
        rows = conn.execute("SELECT project_id,provider,allowlist_json,default_account_id,revision,switch_on_limit FROM project_vendor_accounts").fetchall()
        for row in rows:
            allowlist = json.loads(row["allowlist_json"]) if row["allowlist_json"] else None
            default = row["default_account_id"]
            rollover = bool(row["switch_on_limit"])
            if allowlist is None and default is None and not rollover:
                continue
            order = []
            for account_id in [default, *(allowlist or [])]:
                if account_id is not None and account_id not in order:
                    order.append(account_id)
            accounts = conn.execute("""SELECT a.id FROM vendor_accounts a JOIN connections c ON c.id=a.connection_id
                                      WHERE c.provider=? AND a.state!='removed'""", (row["provider"],)).fetchall()
            allowed = set(allowlist) if allowlist is not None else {account["id"] for account in accounts}
            disabled = [account["id"] for account in accounts if account["id"] not in allowed]
            revisions[row["project_id"]] = max(revisions.get(row["project_id"], 0), row["revision"])
            conn.execute("""INSERT INTO provider_policies(project_id,provider_key,rollover,order_json,disabled_json,revision)
                            VALUES (?,?,?,?,?,?)""",
                         (row["project_id"], row["provider"], int(rollover),
                          json.dumps(order) if order else None, json.dumps(disabled), row["revision"]))
        for project_id, revision in revisions.items():
            conn.execute("INSERT INTO project_policy_overrides(project_id,revision) VALUES (?,?)", (project_id, revision))
        conn.execute("DROP TABLE project_vendor_accounts")
    finally:
        conn.row_factory = caller_factory
