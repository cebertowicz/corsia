"""A review child's confinement, credentials and permission mode each have one owner.

The rows of ``registry.cc_session``'s failure matrix, the credential rule a
review child's environment is cleaned by, and the one permission-mode constant.
"""

from __future__ import annotations

import json

import pytest

from cc_runtime_fixtures import _seed_parent, child_db  # noqa: F401
from subagents.cc_invocation import (
    UNCONFINED,
    ClaudeInvocation,
    build_claude_cmd,
    permission_mode_for,
)

_REVIEW = dict(binary="claude", permission_mode=permission_mode_for("review"),
               confinement="review", add_dirs=("/src",), working_dir="/child/confined")
_PLAIN = dict(binary="claude", permission_mode="bypassPermissions")


def _record(db, thread: str, session: str, invocation: ClaudeInvocation | None) -> None:
    from registry.cc_session import persist_cc_session

    persist_cc_session(thread, session, cwd="/w", invocation=invocation)


def _set_metadata(db, session: str, metadata: str) -> None:
    db.execute("UPDATE artifact_identities SET metadata=? WHERE external_id=?", (metadata, session))
    db.commit()


def _read(db, thread: str):
    from registry.cc_session import registered_confinement

    return registered_confinement(thread, db)


def test_no_record_or_none_without_an_invocation_reads_none(child_db):
    _seed_parent(child_db, sid="plain")
    assert _read(child_db, "plain") == UNCONFINED
    _record(child_db, "plain", "s1", None)
    _record(child_db, "plain", "s2", ClaudeInvocation(**_PLAIN))
    assert _read(child_db, "plain") == UNCONFINED


def test_review_is_sticky_across_newer_records_retirement_and_release(child_db):
    _seed_parent(child_db, sid="child")
    _record(child_db, "child", "first", ClaudeInvocation(**_REVIEW, session_id="first"))
    # (a) a newer session written without an invocation retires the review one.
    _record(child_db, "child", "second", None)
    read = _read(child_db, "child")
    assert (read.mode, read.add_dirs, read.working_dir) == ("review", ("/src",), "/child/confined")
    # Its registration leaves the owner states.
    child_db.execute("UPDATE artifact_registrations SET relationship_state='tombstoned'")
    child_db.commit()
    assert _read(child_db, "child").mode == "review"


def test_add_dirs_come_from_the_newest_review_record(child_db):
    _seed_parent(child_db, sid="child")
    _record(child_db, "child", "first", ClaudeInvocation(**_REVIEW, session_id="first"))
    newer = {**_REVIEW, "add_dirs": ("/newer",)}
    _record(child_db, "child", "second", ClaudeInvocation(**newer, session_id="second"))
    child_db.execute("UPDATE artifact_identities SET updated_at='9999' WHERE external_id='second'")
    child_db.commit()
    assert _read(child_db, "child").add_dirs == ("/newer",)


@pytest.mark.parametrize("metadata", [
    json.dumps({"claude_invocation": {"confinement": "review", "add_dirs": []}}),
    json.dumps({"claude_invocation": {"confinement": "write", "add_dirs": ["/src"]}}),
    json.dumps({"claude_invocation": {"confinement": "review", "add_dirs": ["/src"]}}),
    "{not json",
])
def test_damaged_or_unreadable_records_refuse_even_when_older(child_db, metadata):
    from subagents.cc_confinement import ConfinementRefused

    _seed_parent(child_db, sid="child")
    _record(child_db, "child", "old", ClaudeInvocation(**_PLAIN))
    _record(child_db, "child", "new", ClaudeInvocation(**_PLAIN))
    _set_metadata(child_db, "old", metadata)
    with pytest.raises(ConfinementRefused, match="damaged"):
        _read(child_db, "child")


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
