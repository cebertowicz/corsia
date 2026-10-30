"""Stop CORSIA's recorded managed processes that outlived a stopped service.

The desktop runs this with the bundled Python before it moves the data folder.
A process is signalled only through its recorded process group, and only while
its creation fingerprint still matches (the TERM then KILL rule restart
recovery uses), never by a bare pid or name. Rows are read, never written: the
next start records each stopped process as ended. Prints one JSON line per
handle; exits 1 when any process could not be proven stopped.
"""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import sys


def stop_recorded_processes(state_root: Path) -> list[dict[str, str]]:
    from automation.managed_process_recovery import _reconcile_orphan_group
    from infra.managed_process_store import unsettled_handles
    from infra.platform_paths import runtime_db_paths

    database = runtime_db_paths(state_root)[0]
    connection = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        rows = unsettled_handles(db=connection)
    finally:
        connection.close()
    return [
        {"handle": row["handle"], "thread_id": row["thread_id"], "result": _reconcile_orphan_group(row)}
        for row in rows
    ]


def main(argv: list[str]) -> int:
    results = stop_recorded_processes(Path(argv[1]))
    for result in results:
        print(json.dumps(result, sort_keys=True))
    return 1 if any(result["result"] == "unverified" for result in results) else 0


if __name__ == "__main__":
    # Run isolated (-I): the backend and, in an app bundle, its packages by path.
    backend = Path(__file__).resolve().parents[1]
    sys.path[:0] = [str(backend), str(backend.parent / "python" / "backend-packages")]
    raise SystemExit(main(sys.argv))
