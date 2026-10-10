"""Selection-only eligibility for agent-selected delegation models.

This deliberately does not alter ``resolve_execution_target``: existing pinned
threads may continue on an operator-hidden harness row. New selection resolves
aliases to a public canonical catalog row and accepts only its current ready
status.
"""

from __future__ import annotations

import json


def _raw_row(selector: str, db) -> tuple[dict | None, str | None]:
    row = db.execute("SELECT * FROM models WHERE id = ?", (selector,)).fetchone()
    if row is not None:
        return dict(row), None
    rows = db.execute(
        "SELECT * FROM models WHERE litellm_model = ?", (selector,)
    ).fetchall()
    if len(rows) == 1:
        return dict(rows[0]), None
    if len(rows) > 1:
        return None, "ambiguous"
    return None, "unknown"


def _canonical_row(selector: str, db) -> tuple[dict | None, dict | None, str | None]:
    """Return requested and canonical rows without changing runtime resolution."""
    requested, error = _raw_row(selector, db)
    if error or requested is None:
        return None, None, error or "unknown"
    canonical: dict = requested
    seen = {str(canonical["id"])}
    while True:
        try:
            metadata = json.loads(canonical.get("metadata_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            return requested, None, "invalid alias metadata"
        alias = metadata.get("alias_of") if isinstance(metadata, dict) else None
        if not isinstance(alias, str) or not alias:
            return requested, canonical, None
        target, error = _raw_row(alias, db)
        if error or target is None:
            return requested, None, f"alias target is {error or 'unknown'}"
        canonical = target
        canonical_id = str(canonical["id"])
        if canonical_id in seen:
            return requested, None, "alias cycle"
        seen.add(canonical_id)


def _usage_summary(row: dict) -> str:
    """Render the shared row's usage payload without interpreting its schema."""
    usage = row.get("usage")
    return (
        f"usage {json.dumps(usage, sort_keys=True)}"
        if usage is not None else "usage unavailable"
    )


def _ready_alternatives(rows: list[dict]) -> str:
    """Every row ready on the account its turn would run on; a harness row names no usage, its account does."""
    from llm.account_catalog import effective_status

    ready = [
        row["id"] if "runs_on" in row else f"{row['id']} ({_usage_summary(row)})"
        for row in rows if effective_status(row)[0] == "ready"
    ][:8]
    return "; ".join(ready) if ready else "none"


def _scoped_rows(thread_id, project_id, account_id) -> tuple[list[dict] | None, str | None]:
    """Rows read on the account a scoped dispatch runs on; no scope keeps the workspace Default."""
    from llm.model_status import get_model_status_rows, scoped_status_rows

    if thread_id is None and project_id is None and account_id is None:
        return get_model_status_rows(), None
    from connections.vendor_accounts import AccountError

    try:
        return scoped_status_rows(thread_id=thread_id, project_id=project_id, account_id=account_id), None
    except AccountError as exc:
        return None, f"account {account_id!r} cannot be used: {exc}"


def dispatch_scope(kwargs: dict, project_id: str | None) -> dict:
    """Eligibility scope for a dispatch: the account its child binds to.

    ``project_accounts`` resolves it as the binding does (``turn_account``): the
    named account, else the actor project's default, else the workspace Default,
    checked against the project's allowlist. Only a dispatch with neither a
    project nor an account keeps the unscoped read.
    """
    project = kwargs.get("project_id") or project_id
    if project is None and kwargs.get("account_id") is None:
        return {}
    return {"account_id": kwargs.get("account_id"), "project_id": project}


def caller_dispatch_scope(thread_id: str) -> dict:
    """The scope a delegated child of ``thread_id`` binds to, never the caller's own binding.

    The one place the caller's project becomes a dispatch scope, so the
    ``model_catalog`` read and eligibility admission agree on the account.
    """
    from connections.account_policy import thread_scope
    from infra.database import get_db

    db = get_db()
    try:
        return dispatch_scope({}, thread_scope(thread_id, db=db)["project_id"])
    finally:
        db.close()


def _account_label(account_id: str) -> str:
    """Name a scoped account with the same identity label the user sees."""
    from connections.account_profiles import account_label
    from connections.vendor_accounts import get_account

    return account_label(get_account(account_id))["text"]


def eligible_delegation_model(
    selector: str | None, *, thread_id: str | None = None, project_id: str | None = None,
    account_id: str | None = None,
) -> tuple[str | None, str | None]:
    """Return the canonical ready catalog id, or a repairable no-op refusal.

    A thread, project or explicit ``account_id`` scopes the read to the account
    the dispatch will run on; without one, harness rows read the workspace Default.
    """
    requested = str(selector or "").strip()
    if not requested:
        return None, "model is required; there is no silent default."

    from infra.database import get_db
    from llm.account_catalog import effective_status

    db = get_db()
    try:
        original, canonical, resolution_error = _canonical_row(requested, db)
    finally:
        db.close()
    rows, account_error = _scoped_rows(thread_id, project_id, account_id)
    if rows is None:
        return None, account_error
    alternatives = _ready_alternatives(rows)
    if resolution_error == "unknown":
        return None, f"model {requested!r} is unknown; choose a ready selector: {alternatives}."
    if resolution_error == "ambiguous":
        return None, f"model {requested!r} is ambiguous; choose a ready selector: {alternatives}."
    if resolution_error or original is None or canonical is None:
        return None, (
            f"model {requested!r} cannot resolve to a canonical selector "
            f"({resolution_error}); choose a ready selector: {alternatives}."
        )
    canonical_id = str(canonical["id"])
    if not bool(original.get("enabled")):
        return None, (
            f"model {requested!r} resolves to {canonical_id!r} but is hidden and "
            f"cannot be selected; do not retry it. Choose a ready selector: {alternatives}."
        )
    status_row = next((row for row in rows if row.get("id") == canonical_id), None)
    if status_row is None:
        return None, (
            f"model {requested!r} resolves to {canonical_id!r} but is hidden and "
            f"cannot be selected; do not retry it. Choose a ready selector: {alternatives}."
        )
    on_account = f" on account {_account_label(account_id)!r} ({account_id})" if account_id is not None else ""
    status, status_reason = effective_status(status_row)
    selected = next((item["account_id"] for item in status_row.get("runs_on") or () if item["selected"]), None)
    if account_id is not None and status_row.get("connection") in ("codex", "claude-code") and (
            selected != account_id and status == "ready"):
        return None, f"model {requested!r} does not run{on_account}: it belongs to another vendor."
    if status != "ready":
        reason = status_reason or status or "unknown"
        return None, (
            f"model {requested!r} resolves to {canonical_id!r} but is "
            f"{status}{on_account} ({reason}); choose a ready selector: "
            f"{alternatives}."
        )
    return canonical_id, None
