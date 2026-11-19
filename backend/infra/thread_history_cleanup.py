"""Remove transcript data when a thread is permanently discarded."""


def delete_thread_history(db, thread_id: str) -> None:
    """Remove the thread's messages, replies held from its sender, and fresh reads of what it wrote."""
    db.execute("DELETE FROM held_replies WHERE sender_thread_id = ?", (thread_id,))
    db.execute("DELETE FROM fresh_reads WHERE writer_thread_id = ?", (thread_id,))
    db.execute("DELETE FROM messages WHERE thread_id = ?", (thread_id,))
