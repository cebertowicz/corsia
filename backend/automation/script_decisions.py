"""User decisions on reviewed standing-script applications."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from infra.database import get_db, write_transaction


def _public(row) -> dict:
    payload = json.loads(row["payload_json"])
    auth = payload["authentication"]
    return {
        "id": row["id"], "kind": "script", "thread_id": row["thread_id"],
        "root_thread_id": row["thread_id"], "root_thread_title": None, "state": "awaiting_user",
        "revision": 1, "application_hash": auth["application_hash"],
        "script_name": Path(row["script_path"]).name, "trigger": auth["trigger"],
        "purpose": auth["purpose"], "user_request": auth["user_request"],
        "plan": [], "requested_duration_seconds": 0, "expected_resources": None,
        "persistent_effects": None, "rollback_plan": None, "forbidden_effects": None,
        "requesting_project_id": None, "repo_policy_snapshot": [], "repo_policy_hash": "",
        "requested_policy_exceptions": [], "reviewer_model": None, "reviewer": None,
        "application_review_result": None, "expires_at": None, "terminal_reason": None,
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


def pending(user_id: str, thread_id: str | None) -> list[dict]:
    db = get_db()
    try:
        rows = db.execute(
            "SELECT s.* FROM automation_schedules s JOIN threads t ON t.id=s.thread_id "
            "WHERE t.user_id=? AND s.kind='script' AND s.state='paused' "
            "AND json_extract(s.payload_json, '$.authentication.state')='pending' "
            "ORDER BY s.created_at,s.id",
            (user_id,),
        ).fetchall()
        if thread_id is not None:
            from core.thread_identity import root_visible_thread_ids
            visible = root_visible_thread_ids([row["thread_id"] for row in rows], db=db)
            rows = [row for row in rows if row["thread_id"] == thread_id or visible.get(row["thread_id"]) == thread_id]
        result = [_public(row) for row in rows]
        if thread_id is not None:
            for item in result:
                item["root_thread_id"] = thread_id
        return result
    finally:
        db.close()


def decide(schedule_id: str, user_id: str, application_hash: str, approve: bool) -> tuple[str, str]:
    """Apply a decision once, bound to the owner and reviewed application hash."""
    with write_transaction(None, scope="script_access_decide") as db:
        row = db.execute(
            "SELECT s.* FROM automation_schedules s JOIN threads t ON t.id=s.thread_id "
            "WHERE s.id=? AND t.user_id=? AND s.kind='script'", (schedule_id, user_id),
        ).fetchone()
        if row is None:
            raise LookupError("Script request not found")
        payload = json.loads(row["payload_json"] or "{}")
        auth = payload.get("authentication") or {}
        if row["state"] != "paused" or auth.get("state") != "pending" or auth.get("application_hash") != application_hash:
            raise ValueError("Script request is no longer current")
        auth["state"] = "approved" if approve else "denied"
        auth["approved_sha256"] = row["script_sha256"] if approve else None
        now = datetime.now(timezone.utc).isoformat()
        db.execute(
            "UPDATE automation_schedules SET state=?, payload_json=?, updated_at=? WHERE id=?",
            ("active" if approve else "cancelled", json.dumps(payload, sort_keys=True), now, schedule_id),
        )
        return row["thread_id"], auth["state"]
