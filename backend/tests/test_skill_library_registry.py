"""The discard confirmation must survive the registry's model-visible result sanitizer."""

from __future__ import annotations

import json

import pytest

from tests.event_routing_helpers import mk_session


@pytest.mark.asyncio
async def test_discard_confirmation_round_trips_through_the_tool_registry(isolated_db, tmp_path, monkeypatch):
    from infra import state
    from tools import skill_library
    from tools.registry import ToolRegistry

    folder = tmp_path / "pdf"
    folder.mkdir()
    (folder / "SKILL.md").write_bytes(b"edited by the adopter")
    confirmed: list[bool] = []

    def fake_execute(action, thread_id, thread, query, name, discard, digest=None):
        refusal = skill_library._discard_confirmation("owner", "pdf", folder, ["SKILL.md"], True, digest)
        if discard and not refusal:
            confirmed.append(True)
            return {"outcome": "updated"}
        return refusal

    monkeypatch.setattr(skill_library, "_execute", fake_execute)
    registry = ToolRegistry()
    registry.register(skill_library.SkillLibraryTool())
    bound = state._current_thread_id.set(mk_session("library-discard-owner"))
    try:
        first = json.loads(await registry.execute("skill_library", {"action": "update", "name": "pdf"}))
        assert first["error"] == "discard_confirmation_required"
        digest = first["local_edits_digest"]
        assert isinstance(digest, str) and len(digest) == 64 and "REDACTED" not in digest

        second = json.loads(await registry.execute("skill_library", {
            "action": "update", "name": "pdf", "discard_local_edits": True, "local_edits_digest": digest,
        }))
    finally:
        state._current_thread_id.reset(bound)
    assert second == {"outcome": "updated"}
    assert confirmed == [True]
