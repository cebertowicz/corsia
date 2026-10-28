"""The session integration app must not drive recovery against per-test databases."""

import asyncio
import threading


def test_session_startup_recovery_cannot_reach_a_later_isolated_db(request, monkeypatch):
    from automation import restart_startup
    from infra.database import get_db

    release = threading.Event()
    queried = threading.Event()
    paths = []

    async def delayed_drive(*_args):
        await asyncio.to_thread(release.wait)
        db = get_db()
        try:
            paths.append(db.execute("PRAGMA database_list").fetchone()[2])
        finally:
            db.close()
            queried.set()

    monkeypatch.setattr(restart_startup, "run_startup_drive", delayed_drive)
    request.getfixturevalue("integration_client")
    isolated_path = request.getfixturevalue("isolated_db")
    release.set()
    assert not queried.wait(1), f"startup recovery accessed {paths}, including {isolated_path}"
