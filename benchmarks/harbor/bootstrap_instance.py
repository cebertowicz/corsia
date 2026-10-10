"""Seed a disposable CORSIA instance: proxy model server row and a throwaway token.

Runs only under the disposable instance's environment (test mode, fresh state
root). The server row keeps the production selector's exact server id so the
catalog row ``omlx@<server_id>/<model>`` resolves identically, while its only
endpoint is the loopback metering proxy. Prints ``{"token": ...}`` on stdout.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
from datetime import datetime, timezone


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-id", required=True)
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    if os.environ.get("CORSIA_TEST_MODE") != "1":
        raise SystemExit("bootstrap_instance runs only with CORSIA_TEST_MODE=1")

    from infra.accounts import create_user, issue_token
    from infra.database import write_transaction
    from llm.network_endpoints import upsert_endpoint

    now = datetime.now(timezone.utc).isoformat()
    with write_transaction(None, scope="harbor_bootstrap_server") as connection:
        connection.execute(
            """INSERT INTO network_servers
                   (id, provider, kind, dialect, instance_id, machine_id,
                    instance_name, port, status, source,
                    last_seen_at, created_at, updated_at)
               VALUES (?, 'omlx', 'managed', NULL, ?, NULL, 'harbor-meter-proxy', ?, 'offline',
                       'manual', ?, ?, ?)""",
            (args.server_id, f"harbor-proxy-{args.server_id}", args.port, now, now, now),
        )
        # Seeded offline: the backend's own health sweep then observes the
        # offline -> online transition and runs catalog discovery through the proxy.
        upsert_endpoint(args.server_id, "127.0.0.1", ok=True, db=connection)

    # Disposable account; its password is random and discarded with the state root.
    user_id = create_user("harbor", secrets.token_urlsafe(24), first_run_wake=False)
    token, _ = issue_token(user_id, kind="api", label="harbor-disposable")
    print(json.dumps({"token": token}))


if __name__ == "__main__":
    main()
