"""The session integration app must not drive recovery against per-test databases."""

import asyncio
import threading


def test_session_startup_recovery_cannot_reach_a_later_isolated_db(request, monkeypatch):
    from automation import restart_startup
    from infra.database import get_db
    from plans import public as plans

    release = threading.Event()
    queried = threading.Event()
    recovery_queried = threading.Event()
    paths = []

    async def delayed_drive(*_args):
        await asyncio.to_thread(release.wait)
        db = get_db()
        try:
            paths.append(db.execute("PRAGMA database_list").fetchone()[2])
        finally:
            db.close()
            queried.set()

    async def delayed_comment_recovery():
        await asyncio.to_thread(release.wait)
        recovery_queried.set()

    monkeypatch.setattr(restart_startup, "run_startup_drive", delayed_drive)
    monkeypatch.setattr(plans, "recover_person_comments", delayed_comment_recovery)
    request.getfixturevalue("integration_client")
    isolated_path = request.getfixturevalue("isolated_db")
    release.set()
    assert not queried.wait(1), f"startup recovery accessed {paths}, including {isolated_path}"
    assert not recovery_queried.wait(1), "comment recovery outlived integration startup"
