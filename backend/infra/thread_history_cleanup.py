"""Remove transcript data when a thread is permanently discarded."""


def delete_thread_history(db, thread_id: str) -> None:
    """Remove the thread's messages and replies held from its sender."""
    db.execute("DELETE FROM held_replies WHERE sender_thread_id = ?", (thread_id,))
    db.execute("DELETE FROM messages WHERE thread_id = ?", (thread_id,))
