"""Synchronous registration operation used by the artifact tool boundary."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from infra.database import get_db
from infra.github_pr_identity import (
    GITHUB_INTEGRATION,
    PR_TYPE,
    canonicalize_github_pr_type,
    validate_github_pr_external_id,
)
from infra.queries import get_thread_for_user
from registry import models, service
from registry.file_identity import external_identity_for_path
from registry.registration_writer import RegistrationOrigin
from tools.artifact_tool_helpers import tool_path


def _identity_from_path(
    path: str,
    *,
    db,
) -> tuple[str, str, str, str, bool, dict[str, Any]]:
    normalized = service.normalize_file_path(path, db=db)
    if normalized is None:
        raise ValueError(f"path is outside the registrable roots: {path}")
    canonical, zone, ephemeral_default = normalized
    return (
        "workspace",
        service.classify(Path(canonical)),
        external_identity_for_path(canonical, zone, db=db),
        canonical,
        ephemeral_default,
        {"zone": zone},
    )


def _resolve_tool_identity(kwargs: dict[str, Any], *, workspace: Path, db):
    metadata = kwargs.get("metadata") or {}
    path = kwargs.get("path")
    retention_default = False
    integration: str | None
    type_: str | None
    external_id: str | None
    canonical: str | None
    if path:
        (
            integration,
            type_,
            external_id,
            canonical,
            retention_default,
            path_meta,
        ) = _identity_from_path(tool_path(path, workspace), db=db)
        if not Path(canonical).exists():
            return None, f"path does not exist: {canonical} (resolved from {path!r})"
        metadata = {**metadata, **path_meta}
    else:
        raw_integration = kwargs.get("integration")
        raw_type = kwargs.get("type")
        raw_external_id = kwargs.get("external_id")
        integration = raw_integration if isinstance(raw_integration, str) else None
        type_ = raw_type if isinstance(raw_type, str) else None
        external_id = raw_external_id if isinstance(raw_external_id, str) else None
        canonical = None
    if not (integration and type_ and external_id):
        return (
            None,
            "integration, type and external_id are required unless path is provided",
        )
    if integration == GITHUB_INTEGRATION:
        type_ = canonicalize_github_pr_type(type_)
        if type_ == PR_TYPE:
            error = validate_github_pr_external_id(external_id)
            if error:
                return None, error
    return (
        (
            integration,
            type_,
            external_id,
            canonical,
            retention_default,
            metadata,
        ),
        None,
    )


def register_for_tool(
    parent_thread_id: str,
    user_id: str,
    kwargs: dict[str, Any],
    *,
    workspace: Path,
    origin: RegistrationOrigin | None,
):
    """Register one tool-requested artifact under verified caller ownership.

    A relative ``path`` is read against the calling tool's ``workspace``.
    """
    db = get_db()
    try:
        if get_thread_for_user(parent_thread_id, user_id, db=db) is None:
            return "not_found", None
        identity, error = _resolve_tool_identity(kwargs, workspace=workspace, db=db)
        if error is not None:
            return "bad_request", error
        assert identity is not None
        (
            integration,
            type_,
            external_id,
            canonical,
            retention_default,
            metadata,
        ) = identity
        path = kwargs.get("path")
        common = {
            "title": kwargs.get("title"),
            "description": kwargs.get("description"),
            "retention": kwargs.get("retention"),
            "reject_retention_change": True,
            "metadata": metadata,
            "origin": origin,
            "db": db,
        }
        if path:
            assert canonical is not None
            row = service.register_write_strict(
                parent_thread_id,
                canonical,
                **common,
            )
        else:
            row = models.register_artifact(
                integration=integration,
                type=type_,
                external_id=external_id,
                parent_thread_id=parent_thread_id,
                retention_default=retention_default,
                **common,
            )
        return "ok", {
            "artifact": row,
            "notification": row
            or {
                "integration": integration,
                "type": type_,
                "external_id": external_id,
            },
        }
    except models.RetentionChangeDenied as exc:
        return "bad_request", str(exc)
    finally:
        db.close()
