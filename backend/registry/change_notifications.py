"""Shared construction of per-user artifact invalidation frames."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable

from infra.queries import get_thread


def build_artifact_change_messages(
    artifact_id: str,
    *,
    owner_thread_id: str | None,
    affected_thread_ids: Iterable[str],
    db=None,
) -> tuple[tuple[str, dict], ...]:
    """Build the canonical one-artifact invalidation frame for each user."""
    threads_by_user: dict[str, list[str]] = defaultdict(list)
    for thread_id in dict.fromkeys(str(value) for value in affected_thread_ids):
        thread = get_thread(thread_id, db=db)
        if thread is not None and thread["user_id"]:
            threads_by_user[str(thread["user_id"])].append(thread_id)
    return tuple(
        (
            user_id,
            {
                "type": "artifacts_changed",
                "artifact_id": artifact_id,
                "owner_thread_id": (
                    owner_thread_id if owner_thread_id in thread_ids else None
                ),
                "affected_thread_ids": thread_ids,
            },
        )
        for user_id, thread_ids in threads_by_user.items()
    )
