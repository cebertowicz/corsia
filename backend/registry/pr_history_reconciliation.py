"""Reconcile historical PR registrations whose terminal webhook was not recorded.

Run only as a one-time cutover operation after reviewing a dry run. GitHub is
queried for the current PR state; all terminal writes use ``pr_lifecycle``.
This does not repair future webhook ingress or routing failures (#676).
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
from typing import Callable


_PR_ID = re.compile(r"([A-Za-z0-9][A-Za-z0-9-]*)/([A-Za-z0-9._-]+)#([1-9][0-9]*)\Z")
_MAX_IDENTITIES = 1000


def _candidates(db: sqlite3.Connection) -> list[tuple[str, int]]:
    """Select canonical PRs that still have an unstamped registration."""
    rows = db.execute(
        """
        SELECT i.external_id, COUNT(*) AS registration_count
          FROM artifact_identities AS i
          JOIN artifact_registrations AS r ON r.artifact_id = i.id
         WHERE i.integration = 'github' AND i.type = 'pull-request'
           AND i.state = 'active' AND r.retired_at IS NULL
         GROUP BY i.id ORDER BY i.external_id
        """
    ).fetchall()
    if len(rows) > _MAX_IDENTITIES:
        raise RuntimeError(f"{len(rows)} candidate PRs exceed the {_MAX_IDENTITIES} identity limit")
    candidates = [(str(row[0]), int(row[1])) for row in rows]
    for external_id, _count in candidates:
        if _PR_ID.fullmatch(external_id) is None:
            raise ValueError(f"Invalid canonical PR identity: {external_id!r}")
    return candidates


def _github_state(external_id: str) -> str:
    """Return the current GitHub PR state without exposing its content."""
    match = _PR_ID.fullmatch(external_id)
    if match is None:
        raise ValueError(f"Invalid canonical PR identity: {external_id!r}")
    owner, repo, number = match.groups()
    result = subprocess.run(
        ["gh", "api", f"repos/{owner}/{repo}/pulls/{number}",
         "--jq", "{state,merged_at}"],
        check=True, capture_output=True, text=True, timeout=15,
    )
    payload = json.loads(result.stdout)
    if payload.get("state") == "open":
        return "open"
    if payload.get("state") != "closed":
        raise ValueError(f"Unrecognized GitHub PR state for {external_id!r}")
    return "merged" if payload.get("merged_at") else "closed"


def reconcile(
    db: sqlite3.Connection,
    *,
    lookup_state: Callable[[str], str] = _github_state,
    apply: bool = False,
    expected_terminal_count: int | None = None,
) -> dict[str, int]:
    """Plan all terminal writes before applying any; reruns are idempotent."""
    candidates = _candidates(db)
    terminal: list[tuple[str, str]] = []
    counts = {"candidates": len(candidates), "open": 0, "merged": 0, "closed": 0,
              "terminal_identities": 0, "terminal_registrations": 0, "applied": 0}
    for external_id, registration_count in candidates:
        state = lookup_state(external_id)
        if state not in {"open", "merged", "closed"}:
            raise ValueError(f"Unrecognized PR state for {external_id!r}: {state!r}")
        counts[state] += 1
        if state != "open":
            terminal.append((external_id, state))
            counts["terminal_registrations"] += registration_count
    # The value a reviewer passes back as --expected-terminal-count.
    counts["terminal_identities"] = len(terminal)
    if apply:
        if expected_terminal_count is None:
            raise ValueError(
                "Apply requires terminal_identities from a reviewed dry run as --expected-terminal-count"
            )
        if len(terminal) != expected_terminal_count:
            raise ValueError(
                f"Terminal identity count changed: expected {expected_terminal_count}, "
                f"observed {len(terminal)}; repeat the dry run"
            )
        from registry.pr_lifecycle import record_pull_request_ended

        for external_id, state in terminal:
            row = record_pull_request_ended(
                "github", "pull-request", external_id, pr_state=state, db=db,
            )
            if row is None:
                raise RuntimeError(f"Registered PR disappeared during reconciliation: {external_id!r}")
            counts["applied"] += 1
    return counts


def main() -> None:
    """Require an explicit state target and print only aggregate results."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--expected-terminal-count", type=int,
        help="terminal_identities printed by the reviewed dry run",
    )
    args = parser.parse_args()
    workspace = os.environ.get("CORSIA_WORKSPACE")
    state_dir = os.environ.get("CORSIA_STATE_DIR")
    if not workspace or not state_dir:
        parser.error("Set explicit CORSIA_WORKSPACE and CORSIA_STATE_DIR")
    from infra.config import DB_PATH

    if not Path(DB_PATH).resolve().is_relative_to(Path(state_dir).resolve()):
        parser.error("Resolved database is outside CORSIA_STATE_DIR")
    if args.apply:
        from infra.database import get_db

        db = get_db()
    else:
        db = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        counts = reconcile(
            db, apply=args.apply,
            expected_terminal_count=args.expected_terminal_count,
        )
        print(json.dumps(counts, sort_keys=True))
    finally:
        db.close()


if __name__ == "__main__":
    main()
