"""A live write of a SKILL.md is validated before any byte reaches the file."""
import pytest

from doceditor.document_save import DocumentSaveError
from doceditor.live_lane import Operation
from doceditor.live_publication_record import WriteActor
from tests.test_document_live_file_tools import opened
from tests.test_shared_document_ownership import ownership_scope as ownership_scope

VALID = "---\nname: live-demo\ndescription: A valid live fixture\n---\n# live-demo\n"
INVALID = VALID.replace("name: live-demo", "name:")


@pytest.mark.asyncio
async def test_live_write_refuses_an_invalid_skill_and_leaves_the_file(ownership_scope, tmp_path, monkeypatch):
    scope = ownership_scope
    manager, _, _ = await opened(scope, tmp_path, monkeypatch)
    skill = scope["category"] / "skills" / "live-demo" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(VALID, encoding="utf-8")
    from registry import service
    artifact = service.register_write_strict(scope["owner"], str(skill), db=scope["db"])
    entry = await manager.open(artifact["id"], skill)
    actor = WriteActor(scope["owner"], "project-alpha")
    try:
        refused = await entry.lane.submit(Operation("replace", content=INVALID, actor=actor))
        with pytest.raises(DocumentSaveError) as error:
            refused.require_landed()
        assert error.value.kind == "invalid_skill"
        assert "it would not load as a skill" in str(error.value)
        assert skill.read_text(encoding="utf-8") == VALID
        repaired = await entry.lane.submit(Operation("replace", content=VALID.replace("live fixture", "repaired"), actor=actor))
        assert repaired.require_landed().server_saved
        assert "repaired" in skill.read_text(encoding="utf-8")
    finally:
        await manager.close()
