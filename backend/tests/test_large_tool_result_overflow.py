"""One tool result larger than the room left, or than the whole window, still fits after a checkpoint."""

import pytest

from tests import test_native_tail_batch_fits as tail
from tests.test_native_refold_no_boundary import WINDOW, _adopted_state, _outcome, seat  # noqa: F401


@pytest.mark.parametrize("size", [90_000, 300_000], ids=["large-result", "larger-than-window"])
@pytest.mark.parametrize("isolate_preview", [False, True], ids=["normal", "initial-preview"])
@pytest.mark.asyncio
async def test_native_single_large_result_installed_tail_fits(seat, monkeypatch, size, isolate_preview):
    """The installed tail previews the one result, so the Turn replies instead of overflowing."""
    monkeypatch.setattr(tail, "BATCH", 1)
    monkeypatch.setattr(tail, "READS", (tail.READS[0], (tail.READS[1][0], size)))
    checkpoint = tail._seed_batch_turn("single")
    from infra.database import get_db
    db = get_db()
    try:
        assert db.execute("SELECT length(content) FROM messages WHERE id='single-b0'").fetchone()[0] == size
    finally:
        db.close()
    from core import native_context_prefix, turn_iteration_context
    if isolate_preview:
        # Smaller rungs otherwise hide a regression in the initial preview.
        monkeypatch.setattr(turn_iteration_context, "_INSTALLED_TOOL_BUDGETS", (None,))
    render = native_context_prefix.build_real_send_prefix
    attempts = []

    async def capture(**kwargs):
        result = await render(**kwargs)
        attempts.append(result[0])
        return result

    monkeypatch.setattr(native_context_prefix, "build_real_send_prefix", capture)
    state, tokens = await _adopted_state("single", checkpoint)
    assert tokens <= WINDOW
    tool_messages = [message for message in attempts[-1] if message.get("role") == "tool"]
    assert len(tool_messages) == 1
    assert "CORSIA_TOOL_RESULT_PREVIEW" in tool_messages[0]["content"]
    assert await _outcome(state, "single", "turn-1") == "replies"


@pytest.mark.parametrize("harness", ["cc", "codex"])
def test_harness_cold_replay_previews_one_large_tool_result(seat, monkeypatch, tmp_path, harness):
    """A cold rerun's rendered checkpoint tail keeps a large result in preview form."""
    import json
    from types import SimpleNamespace

    from core.checkpoint_resume import installed_replay
    from core.token_estimation import _text_tokens
    from infra.checkpoint_queries import read
    from infra.database import get_db

    monkeypatch.setattr(tail, "BATCH", 1)
    monkeypatch.setattr(tail, "READS", (tail.READS[0], (tail.READS[1][0], 300_000)))
    checkpoint = tail._seed_batch_turn("single")
    db = get_db()
    try:
        assert db.execute("SELECT length(content) FROM messages WHERE id='single-b0'").fetchone()[0] == 300_000
        selection = installed_replay(read(checkpoint, db=db), db=db)
        assert len([message for message in selection.tail.messages if message.role == "tool"]) == 1
        if harness == "cc":
            from subagents import cc_destination as destination
            from subagents.cc_invocation import UNCONFINED

            monkeypatch.setattr(destination, "launch_confinement", lambda *_args: UNCONFINED)
            monkeypatch.setattr(destination, "registered_cwd_or_default", lambda *_args: str(tmp_path))
            monkeypatch.setattr(destination, "_main_working_dir", lambda *_args: str(tmp_path))
            monkeypatch.setattr(destination, "_artifact_cc_session_id", lambda *_args: None)
            monkeypatch.setattr(destination, "launch_tools", lambda *_args: (object(), (
                {"function": {"name": "read_file"}},
            )))
            material = destination._cold_material(
                "single", selection=selection, target=SimpleNamespace(expected_reported_model="test"),
                project_id=None, claude_dir=str(tmp_path), db=db,
            )
            assert material.transcript is not None
            rendered = material.transcript.content
        else:
            from subagents import codex_destination_render as destination

            reconstruction = destination._render_reconstruction(
                thread_id="single", user_text="continue", user_source_text="continue",
                agent_envelope=None, target=SimpleNamespace(catalog_id="codex/test", context_window=WINDOW),
                project_id=None, working_dir=str(tmp_path),
                prepared=SimpleNamespace(delivery_mode="checkpoint_tail", codex_thread_id=None,
                                         history_projection=selection.tail, checkpoint_text="checkpoint"),
                db=db,
            )
            rendered = json.dumps(reconstruction.history_items, ensure_ascii=False)
    finally:
        db.close()
    assert "CORSIA_TOOL_RESULT_PREVIEW" in rendered
    assert _text_tokens(rendered, None) < WINDOW
