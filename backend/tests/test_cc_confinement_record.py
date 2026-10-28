"""The thread row owns cold-launch confinement; live launches keep their original scope."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from cc_runtime_fixtures import _seed_parent, child_db  # noqa: F401
from subagents.cc_invocation import UNCONFINED, ClaudeInvocation, build_claude_cmd, permission_mode_for

_REVIEW = dict(binary="claude", permission_mode=permission_mode_for("review"),
               confinement="review", add_dirs=("/src",), working_dir="/child/confined")
_PLAIN = dict(binary="claude", permission_mode="bypassPermissions")


def _record(thread: str, session: str, invocation: ClaudeInvocation | None) -> None:
    from registry.cc_session import persist_cc_session
    persist_cc_session(thread, session, cwd="/w", invocation=invocation)


def test_cold_launch_uses_row_when_session_disagrees(child_db, monkeypatch):
    from subagents.cc_confinement import launch_confinement
    from tools.confined_reads import context_roots
    from subagents.codex_destination_render import codex_destination_scope
    from subagents.cc_confinement import ConfinementRefused

    _seed_parent(child_db, sid="child")
    _record("child", "first", ClaudeInvocation(**_REVIEW, session_id="first"))
    assert launch_confinement("child", None, child_db) == UNCONFINED
    child_db.execute("UPDATE threads SET confinement='review', confined_read_dirs=? WHERE id='child'",
                     (json.dumps(["/row"]),))
    child_db.commit()
    _record("child", "second", ClaudeInvocation(**_PLAIN))
    monkeypatch.setattr("subagents.cc_confinement.review_working_dir", lambda *_args: "/child/confined")
    result = launch_confinement("child", None, child_db)
    assert (result.mode, result.add_dirs) == ("review", ("/row",))
    row = dict(child_db.execute("SELECT * FROM threads WHERE id='child'").fetchone())
    assert tuple(map(str, context_roots(row))) == ("/row",)
    with pytest.raises(ConfinementRefused, match="Couldn’t start this read-only agent"):
        codex_destination_scope("child", target_catalog_id="codex/test", db=child_db)


@pytest.mark.parametrize("mode,dirs", [("review", "[]"), ("write", '["/src"]'), ("review", "{bad")])
def test_damaged_row_refuses_cold_launch(child_db, mode, dirs):
    from subagents.cc_confinement import ConfinementRefused, launch_confinement
    _seed_parent(child_db, sid="child")
    child_db.execute("UPDATE threads SET confinement=?, confined_read_dirs=? WHERE id='child'", (mode, dirs))
    child_db.commit()
    with pytest.raises(ConfinementRefused, match="damaged"):
        launch_confinement("child", None, child_db)


def test_live_process_keeps_launch_confinement(child_db):
    from subagents.cc_confinement import launch_confinement
    _seed_parent(child_db, sid="child")
    invocation = ClaudeInvocation(**_REVIEW)
    live = SimpleNamespace(session=SimpleNamespace(invocation=lambda: invocation))
    assert launch_confinement("child", live, child_db).add_dirs == ("/src",)


def test_review_env_holds_no_credential_and_unconfined_env_is_unchanged(monkeypatch):
    from connections.github_agent_home import TOKEN_NAMES
    from subagents.cc_stream import CCStreamSession

    names = (*TOKEN_NAMES, "CORSIA_TOKEN", "CORSIA_API_TOKEN", "CORSIA_SOURCES_SERVICE_TOKEN",
             "CORSIA_EVAL_TOKEN", "CORSIA_RESTART_CALLER_TOKEN")
    for name in names:
        monkeypatch.setenv(name, "secret")
    review = CCStreamSession(binary="claude", session_id="s", confinement="review",
                             add_dirs=("/src",), permission_mode=permission_mode_for("review"))
    plain = CCStreamSession(binary="claude", session_id="s")
    assert not set(names) & set(review._build_env())
    assert set(names) <= set(plain._build_env())


def test_stored_permission_mode_is_the_review_commands_mode():
    invocation = ClaudeInvocation(**_REVIEW)
    cmd = build_claude_cmd(invocation, stream_input=True)
    assert cmd.count("--permission-mode") == 1
    assert cmd[cmd.index("--permission-mode") + 1] == invocation.permission_mode
