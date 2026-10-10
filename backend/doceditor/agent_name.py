"""The one rule for how an agent is named beside a document."""
from __future__ import annotations


def agent_display_name(title: str | None, thread_id: str) -> str:
    """The thread title, or the thread id while the thread is untitled."""
    return (title or "").strip() or thread_id
