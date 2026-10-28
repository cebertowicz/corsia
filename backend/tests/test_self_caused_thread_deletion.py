"""Thread deletion removes only its own connected-app event markers."""
import pytest

from tests.test_thread_family_delete import stopped  # noqa: F401


@pytest.mark.asyncio
async def test_deleting_thread_removes_its_markers(stopped):
    from infra.database import get_db
    from tests.test_thread_family_delete import _delete, _seed

    _seed(("root", None, 0), ("writer", "root", 0))
    db = get_db()
    try:
        db.executemany(
            "INSERT INTO self_caused_events (connector,event_key,thread_id,at) VALUES ('github','issue:1',?,'t')",
            [("writer",), ("root",)],
        )
        db.commit()
    finally:
        db.close()
    await _delete("writer")
    db = get_db()
    try:
        assert [row[0] for row in db.execute("SELECT thread_id FROM self_caused_events")] == ["root"]
    finally:
        db.close()
