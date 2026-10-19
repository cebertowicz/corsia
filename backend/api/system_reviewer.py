"""Host-access reviewer selection: which catalog models can review for which scopes.

A review runs for the requesting thread's project (or the workspace when it has
none), so a model's readiness is the reviewer's own account resolution applied
to each scope. This module reads that decision and keeps no readiness rule of
its own.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from host_access.reviewer_unavailable import (
    FALLBACK_OFF_FIX,
    FALLBACK_PARTIAL,
    FALLBACK_REVIEWING,
    OFF_FAIL,
    OFF_FAIL_UNTIL_THEN,
    OFF_PARTIAL_FAIL,
    OFF_PARTIAL_FAIL_UNTIL_THEN,
)
from infra.settings import (
    get_host_access_reviewer_fallback,
    get_host_access_reviewer_model,
)
from llm import catalog as model_catalog

HARNESSES = ("claude-code", "codex")
WORKSPACE_SCOPE = "agents outside a project"

_REASON_WORDS = {
    "usage_limited": "usage limit",
    "account_project_denied": "no account is allowed for this project",
    "project_cloud_off": "this project uses local models only",
    "account_not_configured": "no signed-in account",
    "account_model_not_discovered": "the account doesn't offer this model",
    "discovered_model_unavailable": "the account doesn't offer this model",
    "model_not_in_catalog": "not in the catalog",
}


@dataclass(frozen=True, slots=True)
class ScopeBlock:
    """One scope whose reviews would fail, why, and when a usage limit resets."""

    scope: str
    reason: str
    reset: str | None = None


def reviewer_row(model_id: object) -> dict | None:
    """The workspace-scope catalog row for one exact public model ID."""
    from llm.model_status import get_model_status_rows

    return next(
        (row for row in get_model_status_rows() if row.get("id") == model_id),
        None,
    )


def _scope_block(target, project_id: str | None, scope: str, db) -> ScopeBlock | None:
    """The scope's block when the reviewer cannot pick an account for it."""
    from llm.model_status import USAGE_LIMITED
    from reviewer.failures import CCUnavailableError
    from reviewer.reviewer_account import resolve_scope_reviewer_account

    try:
        resolve_scope_reviewer_account(target, project_id, db=db)
    except CCUnavailableError as exc:
        reason = getattr(exc, "reason", "unavailable")
        reset = getattr(exc, "resets_at", None) if reason == USAGE_LIMITED else None
        return ScopeBlock(scope, reason, reset)
    return None


def reviewer_scope_blocks(model_id: str) -> tuple[list[ScopeBlock], int]:
    """The scopes whose reviews would fail on ``model_id``, and how many scopes exist.

    The scopes are the workspace plus every project that isn't archived (an
    archived one receives no requests). A model-level problem blocks all of
    them; for a harness model each scope then asks the reviewer's own account
    resolution.
    """
    from infra.database import get_db
    from llm.resolution import ModelUnavailableError, resolve_execution_target

    db = get_db()
    try:
        scopes: list[tuple[str | None, str]] = [(None, WORKSPACE_SCOPE)] + [
            (str(row["id"]), str(row["name"]))
            for row in db.execute(
                "SELECT id, name FROM projects "
                "WHERE COALESCE(status, 'active') != 'archived' ORDER BY name"
            )
        ]
        try:
            target = resolve_execution_target(model_id, db=db)
        except ModelUnavailableError:
            return [ScopeBlock(name, "model_not_in_catalog") for _, name in scopes], len(scopes)
        if not target.available:
            reason = target.availability_reason or "model_unavailable"
            return [ScopeBlock(name, reason) for _, name in scopes], len(scopes)
        if target.harness not in HARNESSES:
            return [], len(scopes)
        blocks = [_scope_block(target, project, name, db) for project, name in scopes]
        return [block for block in blocks if block is not None], len(scopes)
    finally:
        db.close()


def _earliest(resets: list[str | None]) -> str | None:
    """The soonest parseable reset time."""
    moments = []
    for value in resets:
        try:
            moments.append(datetime.fromisoformat(str(value)))
        except ValueError:
            continue
    return min(moments).isoformat() if moments else None


def _why(reason: str, reset: str | None) -> str:
    """The reason in plain words, with the reset time and zone for a usage limit."""
    why = _REASON_WORDS.get(reason, reason.replace("_", " "))
    if reset is not None:
        moment = datetime.fromisoformat(reset).astimezone()
        why += (
            f" until {moment:%b} {moment.day}, "
            f"{moment.hour % 12 or 12}:{moment:%M %p %Z}"
        )
    return why


def _reasons(blocks: list[ScopeBlock]) -> dict[str, str]:
    """Each distinct reason's plain words, keyed by reason, in first-seen order."""
    return {
        reason: _why(reason, _earliest([b.reset for b in blocks if b.reason == reason]))
        for reason in dict.fromkeys(block.reason for block in blocks)
    }


def _lead(name: str, blocks: list[ScopeBlock]) -> str:
    """The first sentence: the model and why no scope can use it."""
    why = "; ".join(_reasons(blocks).values())
    return f"{name} can't review requests right now ({why})."


def reviewer_unavailable_text(name: str, blocks: list[ScopeBlock]) -> str:
    """The sentence for a model no scope can review with, when saving it."""
    return f"{_lead(name, blocks)} Choose another model."


def _reset_known(blocks: list[ScopeBlock]) -> bool:
    """Whether any block says when its limit lifts, so ``until then`` is true."""
    return any(block.reset for block in blocks)


def reviewer_status_text(name: str, blocks: list[ScopeBlock], *, fallback_on: bool) -> str:
    """Settings' sentence for a chosen model no scope can review with."""
    if fallback_on:
        return f"{_lead(name, blocks)} {FALLBACK_REVIEWING}"
    fails = OFF_FAIL_UNTIL_THEN if _reset_known(blocks) else OFF_FAIL
    return f"{_lead(name, blocks)} {fails} {FALLBACK_OFF_FIX}"


def _partial_text(name: str, blocks: list[ScopeBlock], *, fallback_on: bool) -> str:
    """The sentence naming the scopes whose requests fail while others work."""
    parts = [
        f"{name} can't review requests from "
        f"{', '.join(b.scope for b in blocks if b.reason == reason)} right now ({why})."
        for reason, why in _reasons(blocks).items()
    ]
    if fallback_on:
        return " ".join(parts) + FALLBACK_PARTIAL
    fails = OFF_PARTIAL_FAIL_UNTIL_THEN if _reset_known(blocks) else OFF_PARTIAL_FAIL
    return f"{' '.join(parts)} {fails} {FALLBACK_OFF_FIX}"


def reviewer_save_error(model_id: object) -> str | None:
    """Why ``model_id`` cannot be saved as the reviewer, or ``None`` when it can."""
    row = reviewer_row(model_id)
    if row is None:
        return "That model isn't in the catalog. Choose another model."
    blocks, total = reviewer_scope_blocks(str(row["id"]))
    if len(blocks) < total:
        return None
    return reviewer_unavailable_text(str(row.get("name") or row["id"]), blocks)


def host_access_reviewer_status() -> dict[str, str | None]:
    """Shape one exact public-catalog reviewer selection for Settings."""
    configured = get_host_access_reviewer_model()
    if configured is None:
        return {
            "host_access_reviewer_model": None,
            "host_access_reviewer_model_name": None,
            "host_access_reviewer_status": "requester_model",
            "host_access_reviewer_message": (
                "Using the requesting agent's model."
            ),
        }
    row = reviewer_row(configured)
    if row is None:
        configured_row = model_catalog.get_model(configured)
        name = str(
            (
                configured_row.get("alias")
                or configured_row.get("display_name")
                or configured
            )
            if configured_row
            else configured
        )
        blocks, total = [ScopeBlock(WORKSPACE_SCOPE, "model_not_in_catalog")], 1
    else:
        name = str(row.get("name") or row.get("id") or configured)
        blocks, total = reviewer_scope_blocks(str(row["id"]))
    unavailable = len(blocks) >= total
    fallback_on = get_host_access_reviewer_fallback()
    message = None
    if unavailable:
        message = reviewer_status_text(name, blocks, fallback_on=fallback_on)
    elif blocks:
        message = _partial_text(name, blocks, fallback_on=fallback_on)
    return {
        "host_access_reviewer_model": configured,
        "host_access_reviewer_model_name": name,
        "host_access_reviewer_status": "unavailable" if unavailable else "ready",
        "host_access_reviewer_message": message,
    }
