"""An added-account Claude thread writes, finds and resumes sessions in one root."""

from __future__ import annotations

from pathlib import Path

import pytest

PROVIDER_ID = "22222222-2222-4222-8222-222222222222"


@pytest.fixture
def added_root(monkeypatch, tmp_path) -> Path:
    """Bind every thread to an added account whose home is not the Default root."""
    from connections import account_runtime

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "default-home"))
    root = tmp_path / "added-home"
    monkeypatch.setattr(account_runtime, "launch_homes", lambda account_id, *, db=None:
        account_runtime.LaunchHomes("added", True, claude_dir=str(root)))
    return root


def test_switch_warm_source_reads_the_added_account_root(isolated_db, added_root, tmp_path):
    """checkpoint_model=target captures the registered session from the thread's own root."""
    from core.checkpoint_cc_fork import _registered_identity
    from infra.database import get_db
    from registry.service import register_cc_session
    from subagents.cc_invocation import ClaudeInvocation

    seed, mcp = tmp_path / "seed.txt", tmp_path / "mcp.json"
    seed.write_text("seed")
    mcp.write_text('{"mcpServers":{}}')
    db = get_db()
    try:
        db.execute("INSERT INTO threads (id,user_id,model,created_at,updated_at) "
                   "VALUES ('switched','user-1','claude-code/sonnet','t0','t0')")
        register_cc_session("switched", PROVIDER_ID, cwd=str(tmp_path), invocation=ClaudeInvocation(
            binary="/synthetic/claude", permission_mode="bypassPermissions", session_id=PROVIDER_ID,
            model="sonnet", seed_file=str(seed), mcp_config=str(mcp), working_dir=str(tmp_path)), db=db)
        db.commit()
    finally:
        db.close()
    transcript = added_root / "projects" / "-switched" / f"{PROVIDER_ID}.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("{}\n")

    identity = _registered_identity("switched")

    assert identity is not None
    assert identity[2] == str(added_root.resolve())
    assert Path(identity[3]) == transcript


@pytest.mark.asyncio
async def test_resume_outside_the_launch_root_refuses_before_spawn(monkeypatch, tmp_path):
    """A rebuilt transcript the launch cannot resume is a named refusal, never a bare exit."""
    from connections.account_policy import AccountAdmissionError
    from core.turn_core import terminal_error_detail
    from subagents import vendor_write_admission
    from subagents.cc_stream import CCStreamSession

    stranded = tmp_path / "default-home" / "projects" / "-w" / f"{PROVIDER_ID}.jsonl"
    stranded.parent.mkdir(parents=True)
    stranded.write_text("{}\n")
    session = CCStreamSession(
        binary="/synthetic/claude", session_id=PROVIDER_ID, working_dir=str(tmp_path),
        claude_dir=str(tmp_path / "added-home"), resume_session_id=PROVIDER_ID,
        reconstructed_transcript_path=str(stranded),
    )
    monkeypatch.setattr(vendor_write_admission, "prepare_session_launch", lambda *_a, **_k: None)

    async def spawn(*_args, **_kwargs):
        raise AssertionError("a mismatched resume reached the provider process")

    monkeypatch.setattr("asyncio.create_subprocess_exec", spawn)
    with pytest.raises(AccountAdmissionError) as refused:
        await session.start()

    assert refused.value.reason == "account_session_home_mismatch"
    # The typed refusal reaches FIFO settlement, which persists its reason on the input.
    assert terminal_error_detail(refused.value) is refused.value
