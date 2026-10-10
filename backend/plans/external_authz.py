"""Authority for an external raise — read access is not raise authority.

`infra.auth.may_access_artifact` answers "may this caller *see* this artifact?".
A raise is a different effect: it writes a node into a plan the caller does not
name, and it places an item into the attention pile of every user who can reach
that artifact. Reusing the read predicate for that write is the escalation that
`api/attention.py` avoids by only ever *filtering* reads with it.

So this module states the raise predicate separately:

* a concrete owning thread → the actor must own it;
* a `project:<id>` sentinel → the actor must be a member of that project;
* the `ui` workspace sentinel → any authenticated workspace user, because the
  workspace substrate has no owning user (same posture as `GET /api/files/*`);
* no owner at all → refused, never "shared" by default.

Every refusal is one indistinguishable `PermissionError`: the caller supplies a
guessable external id, so "unknown", "inactive", "unreachable" and "not yours"
must not be separable.
"""

from __future__ import annotations

from infra.auth import may_access_artifact, owns_thread
from infra.queries import user_in_project

WORKSPACE_SENTINEL = "ui"
PROJECT_SENTINEL_PREFIX = "project:"


class RaiseRefused(PermissionError):
    """One opaque refusal for every reason a raise may not proceed."""


def may_raise_about(owner_thread_id: str | None, actor, *, db) -> bool:
    """True iff this actor may raise an attention item derived from this owner."""
    if not isinstance(owner_thread_id, str) or not owner_thread_id:
        return False
    if owner_thread_id == WORKSPACE_SENTINEL:
        return True
    if owner_thread_id.startswith(PROJECT_SENTINEL_PREFIX):
        project_id = owner_thread_id.split(":", 1)[1]
        return user_in_project(project_id, actor.user_id, db=db)
    return owns_thread(owner_thread_id, actor.user_id, db=db)


def authorize_raise(owner_thread_id: str | None, actor, *, db) -> None:
    """Refuse before any target-plan resolution or write, with no disclosure."""
    if actor is None or not getattr(actor, "user_id", None):
        raise RaiseRefused("external review refused")
    if not may_access_artifact(owner_thread_id, actor.user_id, db=db):
        raise RaiseRefused("external review refused")
    if not may_raise_about(owner_thread_id, actor, db=db):
        raise RaiseRefused("external review refused")
