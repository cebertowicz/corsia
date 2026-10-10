"""Account selection and launch environment for the PR reviewer.

The reviewer has no thread, so it is a non-thread consumer of the account
contract: ``scope_account`` names the account its project would use,
``account_model_status`` decides whether the configured model can run on it,
``admit_operation`` freezes the account's login and homes, and the CLI
environment is built from those homes. Nothing here keeps a second picker or
a second readiness check, and no failure falls back to the ambient CLI login.
"""

from __future__ import annotations

import types
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path

from connections.vendor_accounts import AccountError
from reviewer.failures import CCUnavailableError


@dataclass(frozen=True, slots=True)
class ReviewerOperation:
    """The request fields the detached-operation owner reads; the reviewer has no thread."""

    target: object
    vendor_account_id: str | None = None
    vendor_account_bound: bool = False
    vendor_account_receipt: object | None = None
    vendor_launch: object | None = None
    thread_id: str | None = None
    actor_thread_id: str | None = None
    warm_source: object | None = None


def _unavailable(reason: str, resets_at: str | None = None) -> CCUnavailableError:
    """The existing model-unavailable outcome, led by the account cause the check shows.

    ``resets_at`` is when a usage limit on the account that decided this lifts.
    """
    error = CCUnavailableError(f"Reviewer account unavailable: {reason}")
    error.reason = reason
    error.resets_at = resets_at
    return error


def _reviewer_project_id(db) -> str | None:
    """The project that owns the reviewed repository; None reads the workspace scope."""
    from core.project_context import resolve_project_row_paths
    from infra.config import WORKSPACE

    root = (Path(WORKSPACE) / "projects" / "corsia").resolve(strict=False)
    for row in db.execute("SELECT id, name, path FROM projects").fetchall():
        try:
            _name, planning_root = resolve_project_row_paths(
                Path(WORKSPACE).expanduser().resolve(strict=False),
                project_name=row["name"], raw_path=row["path"],
            )
        except (ValueError, RuntimeError, OSError):
            continue
        if planning_root is not None and planning_root.resolve(strict=False) == root:
            return str(row["id"])
    return None


def resolve_scope_reviewer_account(target, project_id: str | None, *, db) -> str:
    """Pick the project-policy account for a detached reviewer consumer."""
    from connections.account_policy import permitted_accounts, scope_account
    from connections.account_rollover import rollover_destination
    from connections.vendor_accounts import get_account
    from llm.account_catalog import account_model_status
    from llm.model_status import harness_base_row

    provider, project = target.harness, project_id
    identity, reason = scope_account(provider, project_id=project, db=db)
    if reason or identity is None:
        raise _unavailable(reason or "account_not_configured")
    base = harness_base_row(target.catalog_id, db=db)
    if base is None:
        raise _unavailable("model_not_in_catalog")
    # The target already carries the model's catalog availability, including the
    # provider-owned review selector the user catalog hides; the account decision
    # below adds only what is account-specific.
    base = {**base, "status": "ready", "status_reason": None}
    # Only a project with the opt-in switch moves off a limited account.
    identity = rollover_destination(provider, identity, base, project, db=db) or identity
    if identity not in {item["id"] for item in permitted_accounts(provider, project_ids=[project], db=db)}:
        raise _unavailable("account_project_denied")
    decision = account_model_status(get_account(identity, db=db), base, project_ids=[project], db=db)
    if decision["status"] != "ready":
        raise _unavailable(
            decision["status_reason"] or "model_unavailable",
            (decision.get("usage") or {}).get("resets_at"),
        )
    return identity


def resolve_reviewer_account(target, *, db) -> str:
    """The permitted account the reviewer model runs on, or the cause it cannot."""
    return resolve_scope_reviewer_account(target, _reviewer_project_id(db), db=db)


def _claude_env(request) -> dict[str, str]:
    from subagents.cc_env import apply_claude_config_dir, clean_claude_env

    env = clean_claude_env()
    apply_claude_config_dir(env, request.vendor_launch.claude_dir)
    return env


def _codex_env(request, target) -> dict[str, str]:
    from connections.account_operation import bind_operation_session
    from subagents.codex_env import build_session_env

    session = bind_operation_session(
        request, types.SimpleNamespace(control_only=True),
        kind="detached", model=target.catalog_id,
    )
    return build_session_env(session)


@contextmanager
def reviewer_launch(target) -> Iterator[dict[str, str]]:
    """Admit the reviewer's account and yield the CLI environment bound to its homes."""
    from connections.account_operation import (
        admit_operation, operation_directory, require_operation,
    )
    from infra.database import get_db

    provider, model = target.harness, target.catalog_id
    with ExitStack() as stack:
        try:
            db = get_db()
            try:
                identity = resolve_reviewer_account(target, db=db)
            finally:
                db.close()
            request = admit_operation(
                provider, model, ReviewerOperation(target, vendor_account_id=identity),
            )
            if provider == "codex":
                request, _root = stack.enter_context(operation_directory(
                    request, prefix="corsia-reviewer-", kind="detached",
                    home_subdir="home", model=model,
                ))
            require_operation(provider, model, request)
            env = _codex_env(request, target) if provider == "codex" else _claude_env(request)
        except AccountError as exc:
            raise _unavailable(getattr(exc, "reason", None) or str(exc)) from exc
        yield env
