"""Review and record access to run a standing authenticated script."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from automation.script_schedules import pin_script
from events.delivery import deliver_to_thread
from host_access import reviewer_target
from host_access.reviewer import STANDING_SCRIPT_REVIEW, review_application
from host_access.supervisor import _reviewer_binary
from infra.config import WORKSPACE
from infra.db_async import run_in_db, run_off_db
from infra.settings import get_host_access_auto_approve


async def review_script(thread_id: str, params: dict[str, Any]) -> tuple[Path, str, dict[str, Any]]:
    """Review the exact bytes and return a schedule-ready application record."""
    purpose = params.get("purpose")
    user_request = params.get("user_request")
    if not isinstance(purpose, str) or not purpose.strip():
        raise ValueError("purpose is required for an authenticated script")
    if not isinstance(user_request, str) or not user_request.strip():
        raise ValueError("user_request is required for an authenticated script")
    if not params.get("script_path"):
        raise ValueError("script_path is required for a script schedule")
    path, sha = await run_off_db(pin_script, str(params["script_path"]), workspace=WORKSPACE)
    raw = await run_off_db(path.read_bytes)
    if hashlib.sha256(raw).hexdigest() != sha:
        raise ValueError("script changed while preparing review; request it again")
    text = raw.decode("utf-8", errors="backslashreplace")
    trigger = {"cadence_seconds": params.get("cadence_seconds")}
    application = {"purpose": purpose, "user_request": user_request, "trigger": trigger,
                   "script": text, "script_path": str(path), "script_sha256": sha}
    application_hash = hashlib.sha256(json.dumps(
        application, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    auto = await run_in_db(get_host_access_auto_approve)
    target = await reviewer_target.resolve_reviewer_target(requester_thread_id=thread_id)
    review = await review_application(
        target, application, auto_approve=auto, review_spec=STANDING_SCRIPT_REVIEW,
        binary=_reviewer_binary(target), usage_thread_id=thread_id,
    )
    if review.verdict == "reject":
        await deliver_to_thread(thread_id, "script_access.rejected", "Authenticated script rejected.",
                                {"reason": review.reason})
        raise ValueError(f"authenticated script rejected: {review.reason}")
    approved = review.verdict == "allow" and auto and await run_in_db(get_host_access_auto_approve)
    record = {"state": "approved" if approved else "pending", "purpose": purpose,
              "user_request": user_request, "trigger": trigger, "application_hash": application_hash,
              "approved_sha256": sha if approved else None, "review": asdict(review)}
    return path, sha, record
