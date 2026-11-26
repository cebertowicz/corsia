"""Shared SQL predicate for chats visible in attention surfaces.

Queries using this predicate join threads as ``t`` and projects as ``p``.
"""

VISIBLE_THREAD_SQL = """t.purged_at IS NULL AND t.archived_at IS NULL
AND COALESCE(t.lifecycle_state,'active')!='archived'
AND COALESCE(p.status,'active')!='archived'"""
