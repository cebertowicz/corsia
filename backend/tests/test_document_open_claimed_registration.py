"""A live open whose snapshot read just registered the document takes no write."""
from pathlib import Path

import pytest

from doceditor.live_runtime import LiveDocuments
from tests.test_shared_document_ownership import ownership_scope as ownership_scope

WRITES = ("BEGIN", "SAVEPOINT", "INSERT", "UPDATE", "DELETE", "REPLACE")


@pytest.mark.asyncio
async def test_claimed_live_open_takes_no_write_and_a_wrong_claim_registers(ownership_scope, tmp_path, monkeypatch):
    from api import files_load
    from doceditor import live_runtime
    from infra.database import get_db

    scope = ownership_scope
    db = scope["db"]
    manager = LiveDocuments(tmp_path / "live", workspace=scope["workspace"],
                            db_path=Path(db.execute("PRAGMA database_list").fetchone()[2]))
    monkeypatch.setattr(live_runtime, "_manager", manager)
    statements = []

    def traced():
        connection = get_db()
        connection.set_trace_callback(statements.append)
        return connection
    monkeypatch.setattr(files_load, "get_db", traced)
    user = db.execute("SELECT user_id FROM threads WHERE id=?", (scope["owner"],)).fetchone()[0]
    note = scope["root"] / "note.md"
    note.write_text("plain note\n", encoding="utf-8")

    def live_open(thread, claim):
        statements.clear()
        result = files_load.read_document(str(note), user, "project-alpha", thread, live_open=True,
                                          registered_artifact_id=claim)
        return result, [s for s in statements if s.lstrip().upper().startswith(WRITES)]

    try:
        snapshot = files_load.read_document(str(note), user, "project-alpha", scope["owner"])
        artifact_id = snapshot["artifact"]["id"]

        claimed, writes = live_open(scope["owner"], artifact_id)
        assert writes == []
        assert claimed["artifact"]["id"] == artifact_id
        assert claimed["artifact"]["updated_at"] == snapshot["artifact"]["updated_at"]

        wrong, writes = live_open(scope["owner"], "another-document")
        assert writes and wrong["artifact"]["id"] == artifact_id
        assert wrong["artifact"]["updated_at"] > snapshot["artifact"]["updated_at"]

        # A thread that never read it has nothing to claim: its open registers it.
        other, writes = live_open(scope["other"], artifact_id)
        assert writes and other["artifact"]["id"] == artifact_id
        assert db.execute("SELECT relationship_state FROM artifact_registrations WHERE artifact_id=? "
                          "AND parent_thread_id=?", (artifact_id, scope["other"])).fetchone()[0] == "subscriber-recent"

        # A subscriber's own snapshot read registered it, so its claimed open takes no write either.
        subscribed, writes = live_open(scope["other"], artifact_id)
        assert writes == []
        assert subscribed["artifact"] == other["artifact"]
    finally:
        get_db().set_trace_callback(None)
        await manager.close()
