"""Live policy decisions preserve ownership without a write transaction."""
from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3

import pytest

from registry.shared_documents import resolve_shared_target
from registry.shared_write_errors import DurableWritePolicyError
from tests.test_shared_document_ownership import ownership_scope as ownership_scope, _write, _enforce


def decision(scope, path, actor, *, direct_user=False, promoted_hash=None):
    db_path = Path(scope["db"].execute("PRAGMA database_list").fetchone()[2])
    with closing(sqlite3.connect(db_path, timeout=1, isolation_level=None)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("PRAGMA busy_timeout=1000")
        assert not db.in_transaction
        result = resolve_shared_target(
            path, workspace=scope["workspace"], thread_id=actor,
            project_id="project-alpha", description=None, direct_user=direct_user,
            proposed_content=b"changed", db=db, setup=False, promoted_hash=promoted_hash,
        )
        assert not db.in_transaction
        return result


@pytest.mark.parametrize("actor, enforced, direct_user, refused", [
    ("owner", False, False, False),
    ("owner", True, False, False),
    ("worker", True, False, False),
    ("conductor", False, False, False),
    ("other", False, False, True),
    ("other", True, False, True),
    ("other", True, True, False),
])
def test_read_only_decision_matches_existing_owner_gate(
    ownership_scope, actor, enforced, direct_user, refused,
):
    scope = ownership_scope
    _write(scope, "live.md")
    if enforced:
        _enforce(scope)
    if refused:
        with pytest.raises(DurableWritePolicyError):
            decision(scope, scope["category"] / "live.md", scope[actor])
    else:
        target = decision(scope, scope["category"] / "live.md", scope[actor], direct_user=direct_user)
        assert target.authority is not None and target.authority.allowed
        assert target.owner_thread_id == scope["owner"]


def test_query_only_unowned_gate_requests_setup_then_conductor_can_write(ownership_scope):
    from registry.shared_write_errors import SharedIdentitySetupRequired

    scope = ownership_scope
    path = scope["category"] / "unowned.md"
    path.write_text("old")
    _enforce(scope)
    with pytest.raises(SharedIdentitySetupRequired, match="being set up"):
        decision(scope, path, scope["conductor"])
    resolve_shared_target(
        path, workspace=scope["workspace"], thread_id=scope["conductor"],
        project_id="project-alpha", description=None, direct_user=False, db=scope["db"],
    )
    scope["db"].commit()
    assert decision(scope, path, scope["conductor"]).owner_thread_id == scope["conductor"]


def test_query_only_moved_identity_requests_existing_setup(ownership_scope):
    from registry.shared_write_errors import SharedIdentitySetupRequired
    from registry import service

    scope = ownership_scope
    receipt = _write(scope, "old.md")
    old = scope["category"] / "old.md"
    path = old.with_name("moved.md")
    old.rename(path)
    with pytest.raises(SharedIdentitySetupRequired):
        decision(scope, path, scope["owner"])
    identity = service.find_file_identity(str(old), db=scope["db"])
    assert identity["id"] == receipt["artifact"]["id"]
    resolve_shared_target(
        path, workspace=scope["workspace"], thread_id=scope["owner"],
        project_id="project-alpha", description=None, direct_user=False, db=scope["db"],
    )
    scope["db"].commit()
    assert decision(scope, path, scope["owner"]).owner_thread_id == scope["owner"]


def test_unpublished_write_is_not_outside_change(ownership_scope):
    scope = ownership_scope
    _write(scope, "live.md", "old")
    path = scope["category"] / "live.md"
    path.write_text("unpublished")
    promoted_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    assert not decision(scope, path, scope["owner"], promoted_hash=promoted_hash).outside_change
    path.write_text("outside")
    assert decision(scope, path, scope["owner"], promoted_hash=promoted_hash).outside_change
