"""What each user has seen of a thread's plan in the plan pane.

One row per (user, thread), written only by ``plans.pane_views``: the
watermark that decides which finished work is new to the user.
Deleting a thread deletes its rows first (``pane_views.delete_thread``), so the
reference never dangles; a purge keeps the thread row, so its rows stay valid.
"""

from __future__ import annotations

PLAN_PANE_VIEWS_SCHEMA_SQL = """
    CREATE TABLE IF NOT EXISTS plan_pane_views (
        user_id TEXT NOT NULL,
        thread_id TEXT NOT NULL REFERENCES threads(id),
        seen_through TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (user_id, thread_id)
    );
"""
