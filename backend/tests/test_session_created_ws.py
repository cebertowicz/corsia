
from __future__ import annotations

import os
import sys

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)


def test_session_created_broadcasts_rich_session_frame(monkeypatch):
    from infra import state
    from infra import fresh_threads

    seen: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        state,
        "broadcast_to_user",
        lambda user_id, message: seen.append((user_id, message)),
    )

    session = {
        "id": "thread-1",
        "user_id": "u-test",
        "profile": "personal",
        "title": "Fresh Worker",
        "model": "codex/gpt-5-codex",
        "project_id": "corsia-project",
        "parent_session_id": None,
        "spawned_by": None,
        "created_at": "2026-06-17T00:00:00",
        "updated_at": "2026-06-17T00:00:00",
    }

    fresh_threads.emit_session_created(session)

    assert seen == [
        (
            "u-test",
            {
                "type": "session_created",
                "session": fresh_threads.session_payload(session),
            },
        )
    ]
