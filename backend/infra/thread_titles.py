"""Canonical persisted thread-title write."""


def write_thread_title(db, thread_id: str, title: str, now: str) -> None:
    db.execute(
        'UPDATE threads SET title = ?, updated_at = ? WHERE id = ?',
        (title, now, thread_id),
    )
