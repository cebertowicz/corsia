"""Issue #841 — a long-running CC session must not keep serving a pre-cutover
working directory.

The root cause: CC derived its per-turn working directory from cached/registered
sources that were only checked with ``os.path.isdir``. A pre-cutover workspace
root can still exist on disk yet sit outside every currently-authorized scope,
so it passed the existence check, was served verbatim in the per-turn context,
and derived as the default tool cwd — which host access then refused, reading
like a Safehouse error. The fix validates the cwd against the same allow-list
the shell guard enforces and rebinds only a genuinely stale one.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest

from subagents.cc_runtime_helpers import (
    native_stale_working_dir,
    working_dir_in_current_scope,
)

from cc_runtime_fixtures import (  # noqa: F401
    _seed_parent,
    _wait,
    child_db,
    cc_target,
    fake_claude,
    feed_with_persisted_input,
)

_TURN_ID = "11111111-2222-4333-8444-555555555555"


@pytest.fixture
def workspace() -> Path:
    """The session's workspace root, which the sandbox authorizes.

    ``integration_workspace`` is that same root, so ``infra.config.WORKSPACE``
    and ``sandbox_config._STATIC_ALLOWED_RW`` agree whatever ran first (#2949).
    """
    import infra.config

    return infra.config.WORKSPACE


@pytest.fixture
def in_scope_dir(workspace) -> Path:
    """A directory under the (ephemeral, test) workspace — always authorized."""
    d = workspace / "issue841-in-scope"
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture
def stale_dir(tmp_path) -> Path:
    """An existing directory OUTSIDE every current scope (the #841 condition)."""
    d = tmp_path / "Documents" / "projects" / "corsia"
    d.mkdir(parents=True, exist_ok=True)
    return d


class TestScopePredicate:
    """``working_dir_in_current_scope`` is the exact stale-vs-valid predicate."""

    def test_rejects_missing_dir(self):
        assert working_dir_in_current_scope("/no/such/dir/anywhere") is False

    def test_rejects_none_and_empty(self):
        assert working_dir_in_current_scope(None) is False
        assert working_dir_in_current_scope("") is False

    def test_rejects_existing_out_of_scope_dir(self, stale_dir):
        # Reproduces the bug: the path exists (old isdir check passed) but is
        # outside the current workspace/project scope.
        assert stale_dir.is_dir()
        assert working_dir_in_current_scope(str(stale_dir)) is False

    def test_accepts_dir_under_workspace(self, in_scope_dir):
        assert working_dir_in_current_scope(str(in_scope_dir)) is True


class TestNativeStaleWorkingDir:
    """The rebind decision: correct a stale native cwd only when the correction
    is a real fix. Foreign/out-of-band cwds and legitimate in-scope custom dirs
    are left untouched (issue #841)."""

    def test_rebinds_out_of_scope_when_default_in_scope(self, stale_dir, workspace):
        result = native_stale_working_dir(str(stale_dir), str(workspace))
        assert result == str(workspace)

    def test_rebinds_missing_when_default_in_scope(self, workspace):
        assert native_stale_working_dir(
            "/gone/pre/cutover/root", str(workspace)
        ) == str(workspace)

    def test_no_rebind_for_in_scope_cwd(self, in_scope_dir, workspace):
        # HARD CONSTRAINT: an in-scope custom cwd is left untouched (None = keep).
        assert native_stale_working_dir(str(in_scope_dir), str(workspace)) is None

    def test_no_rebind_when_default_also_out_of_scope(self, stale_dir):
        # If rebinding would not actually land in scope, leave the cwd untouched
        # rather than swap one out-of-scope dir for another.
        other = stale_dir.parent / "other-out-of-scope"
        other.mkdir()
        assert native_stale_working_dir(str(stale_dir), str(other)) is None

    def test_none_cwd_is_not_stale(self, workspace):
        assert native_stale_working_dir(None, str(workspace)) is None


class TestLiveSessionRespawn:
    """A live process launched pre-cutover is respawned into a corrected cwd,
    rather than relabelled (which would lie: its frozen process cwd, native
    header, and tool registry all still point at the stale root)."""

    def test_random_corsia_resume_is_checked_for_stale_cwd(
        self, child_db, monkeypatch, stale_dir, in_scope_dir,
    ):
        from subagents import cc_feed_transition

        _seed_parent(child_db)
        monkeypatch.setattr(
            cc_feed_transition,
            "_main_working_dir",
            lambda *_args, **_kwargs: str(in_scope_dir),
        )
        provider_id = str(uuid.uuid4())
        live = SimpleNamespace(session=SimpleNamespace(
            returncode=None,
            working_dir=str(stale_dir),
            session_id=provider_id,
            resume_session_id=provider_id,
            reconstructed_transcript_path=None,
        ))
        transition = SimpleNamespace(thread_id="parent", db=child_db)

        assert cc_feed_transition._stale_working_dir(transition, live) == (
            True,
            str(stale_dir),
        )

    @pytest.mark.asyncio
    async def test_stale_cwd_respawns_into_corrected_dir(
        self, child_db, fake_claude, monkeypatch, tmp_path, in_scope_dir,
    ):
        import connections.models as cm
        import subagents.cc_runtime as rt
        from subagents import cc_destination, cc_feed_transition

        isolated_claude_dir = tmp_path / ".claude"
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(isolated_claude_dir))
        monkeypatch.setattr(
            cm, "get_connection",
            lambda provider, db=None: {"config": {"binary": fake_claude}},
        )
        # The corrected default the respawn must land in — an in-scope dir.
        def corrected_dir(*_args, **_kwargs):
            return str(in_scope_dir)

        monkeypatch.setattr(cc_destination, "_main_working_dir", corrected_dir)
        monkeypatch.setattr(cc_feed_transition, "_main_working_dir", corrected_dir)

        # A stale-but-existing pre-cutover root, outside every current scope.
        stale = tmp_path / "old-workspace" / "corsia"
        stale.mkdir(parents=True, exist_ok=True)
        assert working_dir_in_current_scope(str(stale)) is False

        from core.turn_core import BOOT_EPOCH
        from infra.thread_driver import admit_input

        _seed_parent(child_db)
        child_db.execute(
            "UPDATE threads SET model='claude-code/sonnet' WHERE id='parent'"
        )
        child_db.commit()
        parent_admission = admit_input(
            "parent",
            role="user",
            content="spawn a child",
            intent="apply_now",
            message_id="parent:active-input",
            turn_id="parent:active-turn",
            boot_epoch=BOOT_EPOCH,
            admitted_at="t",
            db=child_db,
        )
        assert parent_admission.batch is not None
        child_id = await rt.spawn(
            "parent", "do it", binary=fake_claude,
            target=cc_target("sonnet"),
            working_dir=str(stale),
            broadcast=lambda _sid, _message: None,
        )
        assert await _wait(lambda: rt.is_live(child_id))
        # Initial admission replaces the stale process before its first write.
        rebound = rt._LIVE.get(child_id).session.working_dir
        assert rebound != str(stale)
        assert working_dir_in_current_scope(rebound) is True
        # Let the spawn turn settle so the next send is a fresh turn, not a steer.
        assert await _wait(lambda: child_db.execute(
            "SELECT status FROM active_turns WHERE thread_id=? "
            "ORDER BY turn_order DESC LIMIT 1",
            (child_id,),
        ).fetchone()["status"] == "completed")

        def _settled() -> bool:
            live = rt._LIVE.get(child_id)
            if live is None or live.turn_active:
                return False
            term = live.turn_terminal
            return term is None or term.is_set()

        assert await _wait(_settled)

        did = await feed_with_persisted_input(
            child_id, "again", target=cc_target("sonnet"), turn_id=_TURN_ID,
        )
        assert did is not None and rt.is_live(child_id)

        # The corrected live handle remains bound to the in-scope directory.
        rebound = rt._LIVE.get(child_id).session.working_dir
        assert rebound != str(stale)
        assert working_dir_in_current_scope(rebound) is True

        ok = await _wait(lambda: any(
            "echo:" in (r["content"] or "") and "again" in (r["content"] or "")
            for r in child_db.execute(
                "SELECT content FROM messages WHERE thread_id=? AND role='assistant'",
                (child_id,)).fetchall()))
        assert ok, "respawned child didn't process the reply"
        await rt.retire(child_id)
