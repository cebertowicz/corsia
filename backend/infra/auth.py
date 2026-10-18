"""Authentication — token-based user lookup + resource ownership.

The token reaches a handler one of two ways, and both are accepted everywhere:

- ``?token=<t>`` query param — the convention across ``/api/*`` routes.
- ``Authorization: Bearer <t>`` header — what the settings store sends.

``require_user`` resolves either form so a single dependency covers every
caller.
``owns_thread`` is THE ownership seam: a token authenticates *who*, then the
resource's owning thread (``threads.user_id``) decides *what they may touch*.
"""

from __future__ import annotations

from fastapi import Depends, Header, HTTPException, Query

from infra.accounts import resolve_token


def authenticate(token: str | None) -> dict | None:
    """Resolve a raw token to its user record, or None.

    The single lookup seam every route, tool and WebSocket handshake resolves
    a caller through. A token is a row in ``auth_tokens`` — a browser session,
    a CLI-issued api token, or the lifecycle shell's short-lived service token
    — and an expired one resolves to None exactly like an unknown one.
    """
    from infra.internal_principal import current_internal_user

    internal_user = current_internal_user()
    if internal_user is not None:
        return internal_user
    if not token:
        return None
    return resolve_token(token)


def token_from_request(token: str | None, authorization: str | None) -> str | None:
    """Pick the token from the query param or a ``Bearer`` header (query wins)."""
    if token:
        return token
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return None


def narrow_to_listener(ws, user: dict | None) -> dict | None:
    """Narrow a WebSocket's first-frame user by the listener it arrived on.

    A socket on the Devices (LAN) listener accepts a person session only;
    every other listener keeps the full ``authenticate`` rule.
    """
    scope = getattr(ws, "scope", None)
    if (
        user is not None
        and isinstance(scope, dict)
        and scope.get("corsia_listener") == "devices"
        and not is_person_session(user)
    ):
        return None
    return user


def require_user(
    token: str | None = Query(None),
    authorization: str | None = Header(None),
) -> dict:
    """FastAPI dependency — authenticate via query token OR Bearer header, or 401."""
    user = authenticate(token_from_request(token, authorization))
    if not user:
        raise HTTPException(401, "Invalid or expired session")
    return user


def require_user_header_only(
    authorization: str | None = Header(None),
) -> dict:
    """Authenticate a *write* from the ``Authorization`` header alone, or 401.

    `require_user` also accepts ``?token=``, which is right for reads a user may
    paste or link. A URL-borne credential reaches server logs, referrers and
    browser history, so a route that materializes state in another user's
    surface should not accept that form. Deliberately a separate dependency:
    narrowing the shared one is a Governance-owned change, and this leaves every
    existing read exactly as it was.
    """
    user = authenticate(token_from_request(None, authorization))
    if not user:
        raise HTTPException(401, "Invalid or expired session")
    return user


def is_person_session(user: dict) -> bool:
    """True when the credential is a signed-in person session, not a machine token."""
    return user.get("token_kind") == "session"


def require_human_user(user: dict = Depends(require_user)) -> dict:
    """Require session provenance for a human decision, not machine credentials.

    This does not prove physical presence or prevent automation possessing a
    human session. The decision surface must retain its own isolation and
    current capability binding.
    """
    if not is_person_session(user):
        raise HTTPException(
            403,
            "This change needs the signed-in person: open CORSIA and make it there.",
        )
    return user


def owns_thread(thread_id: str, user_id: str, db=None) -> bool:
    """True iff ``thread_id`` exists AND belongs to ``user_id``.

    The one ownership predicate the resource routes share — a thin boolean
    over ``get_thread_for_user`` so handlers read as "does this user own the
    owning thread?" at the callsite. ``None``/empty owning-thread ids (the
    ``ui``/``project:<id>`` sentinels, an unscoped register) are never owned
    by a real user and fall through to False.
    """
    if not thread_id:
        return False
    from infra.queries import get_thread_for_user
    return get_thread_for_user(thread_id, user_id, db=db) is not None


def is_owner_sentinel(parent_thread_id: str | None) -> bool:
    """True iff the artifact's owner is a NON-thread sentinel, not a session.

    Workspace docs registered with no thread in scope take the ``ui``
    sentinel; project bootload designations take ``project:<id>``. These rows
    have no owning *user* — they're the shared workspace/project substrate,
    accessed under the same model as ``GET /api/files/{tree,content}`` (any
    authed operator may read any workspace file). The H1 vuln this unit closes
    is cross-*user* access to another user's *thread*-owned artifacts; a
    sentinel row carries no such ownership, so it is shared, not foreign.
    """
    if not parent_thread_id:
        return True
    return parent_thread_id == "ui" or parent_thread_id.startswith("project:")


def may_access_artifact(parent_thread_id: str | None, user_id: str, db=None) -> bool:
    """Authz predicate for an artifact-backed resource (doc / comment / row).

    Allowed when the caller owns the owning thread, OR the owner is a
    workspace/project sentinel (shared substrate — see ``is_owner_sentinel``).
    Denied only for a real OTHER user's thread.
    """
    if is_owner_sentinel(parent_thread_id):
        return True
    return owns_thread(parent_thread_id, user_id, db=db)
