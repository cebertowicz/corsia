"""Person-only route blocklist for the conductor's in-process API tool."""
from __future__ import annotations

import re
from typing import Any

# The person-only blocklist, the one place to edit. Everything not matched here
# is callable. Each row is (proposal row, method pattern, path pattern, reason).
OFF_LIMITS_ROWS = (
    ("B1", ".*", r"^/api/(?:auth|users?|sessions?|tokens?|api-tokens?|passwords?)(?:/|$)",
     "identity: sign in as the person, create users, mint credentials"),
    ("B2", ".*", r"^/api/host[-_]access(?:/|$)",
     "a conductor could approve, extend or revoke its own host-access leases"),
    ("B3", ".*", r"(?:^|/)(?:credentials?|secrets?|api[-_]keys?|passwords?|tokens?)(?:/|$)",
     "direct credential-store reads and writes"),
    ("B4", ".*", r"^/api/keys/[^/]+/(?:value|clear|remove)$",
     "only the person sets, replaces or deletes a stored secret"),
    ("B4", "DELETE", r"^/api/settings/providers/[^/]+$", "removes a stored key"),
    ("B4", "DELETE", r"^/api/settings/network-servers/[^/]+$", "removes a stored key"),
    ("B5", ".*", r"^/api/plans/decisions/[^/]+/answer$",
     "a conductor could answer the person's own decisions"),
    ("B5", ".*", r"^/api/comments/[^/]+/decision$",
     "a conductor could answer the person's own decisions"),
    ("B6", "PUT", r"^/api/(conductor|corsia-agent)$",
     "a conductor could grant or revoke conductor or CORSIA agent authority, including its own"),
    # B7 (corsia-agent-account) and PUT /threads/{id}/vendor-account are open: both
    # enforce the project's account allowlist (connections/account_policy.admit_account).
    ("B8", "POST", r"^/api/vendor-accounts/[^/]+/signin$",
     "signs the person's subscription accounts in"),
    ("B8", ".*", r"^/api/vendor-accounts/[^/]+/signout$",
     "signs the person's subscription accounts out"),
    ("B8", "DELETE", r"^/api/vendor-accounts/[^/]+/signin$",
     "cancels the person's sign-in in progress"),
    ("B8", "DELETE", r"^/api/vendor-accounts/[^/]+$",
     "removes the person's subscription account"),
    ("B9", ".*", r"^/api/(?:cc/)?(?:_tool-bridge|tools?|corsia[-_]api)(?:/|$)",
     "recursion: runs tools outside per-tool identity and admission checks"),
    ("B10", ".*", r"^/api/webhooks(?:/|$)",
     "a conductor could forge inbound GitHub and other external events"),
    # B11: UI handlers that record the person as actor (audit-trail impersonation).
    ("B11", "POST", r"^/api/plans/[^/]+/mutations$", "records the person as actor"),
    ("B11", "PATCH|DELETE", r"^/api/threads/[^/]+$", "records the person as actor"),
    ("B11", "POST", r"^/api/artifacts/[^/]+/comments$", "records the person as actor"),
    ("B11", "POST", r"^/api/doc-feedback(?:/|$)", "records the person as actor"),
    ("B11", "PATCH", r"^/api/artifacts/[^/]+/display$", "records the person as actor"),
    ("B11", "POST", r"^/api/attention/[^/]+/(?:clear|dispose)$", "records the person as actor"),
    ("B11", "POST", r"^/api/threads/[^/]+/seen$", "records the person as actor"),
    ("B11", "POST", r"^/api/plans/operator/threads/[^/]+/seen$", "records the person as actor"),
    ("B11", "POST", r"^/api/artifacts/[^/]+/comments/[^/]+/viewed$", "records the person as actor"),
    ("B11", "POST|DELETE", r"^/api/doc-reviews(?:/|$)", "records the person as actor"),
    ("B11", "POST", r"^/api/threads/[^/]+/messages/[^/]+/(?:retry|cancel)$", "records the person as actor"),
    ("B12", "DELETE", r"^/api/security(?:/|$)",
     "dismissing the person's security alert would hide it from them; reading it stays open"),
)
OFF_LIMITS = tuple((method, path) for _, method, path, _ in OFF_LIMITS_ROWS)

# Body rules: a route that stays callable but refuses one request field or value.
# Each row is (row, method, path pattern, field, reason).
BODY_FIELD_ROWS = (
    ("B13", "PUT", r"^/api/settings/providers/[^/]+$", "api_base",
     "corsia_api does not move a provider endpoint"),
)


def refused_body_field(path: str, method: str, body: Any) -> str | None:
    """The reason a body field is refused on this route, if one is."""
    if not isinstance(body, dict):
        return None
    for _, method_pattern, path_pattern, field, reason in BODY_FIELD_ROWS:
        if field in body and re.fullmatch(method_pattern, method) and re.search(path_pattern, path):
            return reason
    return None


def refused_repo_link(path: str, method: str, body: Any) -> str | None:
    """B14: linking a repo is limited to a directory inside the workspace."""
    if not (method == "POST" and isinstance(body, dict) and body.get("mode") == "link"
            and re.search(r"^/api/projects/[^/]+/repos$", path)):
        return None
    from pathlib import Path

    from infra.config import WORKSPACE

    target = body.get("path")
    if not isinstance(target, str) or not target.strip():
        return None
    resolved = Path(target).expanduser().resolve()
    if resolved == WORKSPACE.resolve() or WORKSPACE.resolve() in resolved.parents:
        return None
    return "corsia_api links only a directory inside the workspace"
_SECRET_KEY = re.compile(r"token$|password|secret|credential|api.?key|authorization|host.?access|cookie", re.I)
# The one host-access setting a conductor may set (the reviewer model). Every other
# host-access key, Auto-approve above all, stays refused. Matched exactly, never by pattern.
_CONDUCTOR_SETTABLE_KEYS = frozenset({"host_access_reviewer_model"})
_RECOGNIZED_KEY = re.compile(
    r"(?<![A-Za-z0-9_-])(?:sk-(?:proj-|ant-)?[A-Za-z0-9_-]{8,}"
    r"|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})(?![A-Za-z0-9_-])"
)


def off_limits(path: str, method: str = "GET") -> bool:
    """Check permanent path families and operations that require a human actor."""
    return any(re.fullmatch(method_pattern, method) and re.search(path_pattern, path)
               for method_pattern, path_pattern in OFF_LIMITS)


def contains_forbidden_fields(value: Any) -> bool:
    """Reject authority/credential fields and recognizable secret values."""
    if isinstance(value, dict):
        return any((str(key) not in _CONDUCTOR_SETTABLE_KEYS and _SECRET_KEY.search(str(key)))
                   or contains_forbidden_fields(item)
                   for key, item in value.items())
    if isinstance(value, list):
        return any(contains_forbidden_fields(item) for item in value)
    if isinstance(value, str):
        from host_access.credential_sanitizer import contains_credential

        return contains_credential(value) or bool(_RECOGNIZED_KEY.search(value))
    return False


def safe_response(value: Any) -> Any:
    """Remove credential fields and sanitize both labeled and recognized keys."""
    if isinstance(value, dict):
        return {key: safe_response(item) for key, item in value.items()
                if not _SECRET_KEY.search(str(key))}
    if isinstance(value, list):
        return [safe_response(item) for item in value]
    if isinstance(value, str):
        from host_access.credential_sanitizer import sanitize_credential_text

        return _RECOGNIZED_KEY.sub("[REDACTED]", sanitize_credential_text(value))
    return value


def registered_routes(router):
    """Support both eager and lazy FastAPI include_router implementations."""
    candidates = router.effective_candidates() if hasattr(router, "effective_candidates") else router.routes
    for route in candidates:
        if hasattr(route, "effective_candidates"):
            yield from registered_routes(route)
        else:
            yield route


def route_class(route) -> str:
    """Classify a registered method/path: open unless the blocklist matches."""
    path = getattr(route, "path", "")
    methods = frozenset(getattr(route, "methods", ()) or ()) - {"HEAD", "OPTIONS"}
    if any(off_limits(path, method) for method in methods):
        return "off-limits"
    return "open"
