from __future__ import annotations


def test_context_db_helpers_resolve_database_seam_at_call_time(monkeypatch):
    import core.thread_history as thread_history
    import core.token_estimation as token_estimation

    sentinel = object()
    monkeypatch.setattr("infra.database.get_db", lambda: sentinel)

    assert thread_history.get_db() is sentinel
    assert token_estimation.get_db() is sentinel
