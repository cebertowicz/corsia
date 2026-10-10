"""A loaded lane validates the submitting actor, independent of its opener."""
from pathlib import Path

import pytest

from doceditor.document_save import DocumentSaveError
from doceditor.live_lane import Operation
from doceditor.live_publication_record import WriteActor
from doceditor.live_runtime import LiveDocuments
from tests.test_shared_document_ownership import ownership_scope as ownership_scope, _write


@pytest.mark.asyncio
@pytest.mark.parametrize("foreign_user", [False, True])
async def test_second_opener_cannot_borrow_first_project_authority(ownership_scope, tmp_path, foreign_user):
    scope = ownership_scope
    path = scope["category"] / "actor.md"
    _write(scope, "actor.md", "base")
    manager = LiveDocuments(tmp_path / "live", workspace=scope["workspace"],
        db_path=Path(scope["db"].execute("PRAGMA database_list").fetchone()[2]))
    owner = WriteActor(scope["owner"], "project-alpha")
    foreign = WriteActor(scope["owner"], "project-alpha", user_id="foreign-user") if foreign_user else WriteActor(scope["owner"], "missing-project")
    try:
        first = await manager.open("actor-document", path)
        second = await manager.open("actor-document", path)
        assert first is second
        with pytest.raises(DocumentSaveError) as raised:
            refused = await second.lane.submit(Operation("replace", content="unauthorized", actor=foreign))
            assert refused.outcome == "refused"
            refused.require_landed()
        assert raised.value.kind == ("thread_not_found" if foreign_user else "project_not_found")
        assert path.read_text() == "base"
        result = await first.lane.submit(Operation("replace", content="authorized", actor=owner))
        assert result.server_saved and path.read_text() == "authorized"
    finally:
        await manager.close()
