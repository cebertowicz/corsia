"""User-attention HTTP surface (coordination MVP S6b).

Thin endpoints over the tested attention lifecycle (S6a), parent disposition
(S5b), and review-profile execution (S5a):

- ``GET  /api/attention/raised``            — raised rows, optionally scoped to a thread
- ``GET  /api/attention/rollup``            — pull-only parent roll-up (no badge/wake)
- ``GET  /api/attention/{id}/review``       — the review-profile result for a card
- ``POST /api/attention/{id}/clear``        — clearing actions (S6a)
- ``GET  /api/attention/queue``             — the one queue the user works through
- ``POST /api/attention/{id}/dispose``      — parent disposition (S5b)

``/raised`` and ``/dispose`` currently have no product consumer. Their lifecycle
semantics remain covered by backend tests while #921 decides whether raised rows
receive a typed surface or stop being raised.
"""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from infra.auth import authenticate, may_access_artifact, owns_thread, require_user
from infra.database import get_db
from infra.db_async import run_in_db
from parent_review import disposition as D
from parent_review.profiles import ProfileExecutionError, run_review_profile
from doceditor import reply_attention, review_bar
from plans import public
from registry import artifact_record as AR
from registry import attention

router = APIRouter(prefix="/api/attention", tags=["attention"])


def _require_user(token: str) -> dict:
    user = authenticate(token)
    if user is None:
        raise HTTPException(status_code=401, detail="invalid token")
    return user


def _resolve_access(artifact_id: str, user_id: str, *, db=None) -> tuple[str | None, bool, bool]:
    """Return (owner_thread, found, accessible) for an artifact by id/external_id.

    Reuses the project's per-artifact authz convention (``may_access_artifact``):
    a caller may act only on an artifact owned by a thread they own (or a
    workspace/project sentinel) — the same check the registry API uses.
    """
    _db = db or get_db()
    try:
        row = AR.identity_row_for(artifact_id, db=_db)
        if row is None:
            return None, False, False
        owner = AR.register_or_resolve(
            {"integration": row["integration"], "type": row["type"],
             "external_id": row["external_id"]}, db=_db).owner_thread
        # An ownerless (None) artifact must NOT be treated as accessible —
        # may_access_artifact(None) is True by the sentinel convention, which would
        # expose ownerless items to any caller. Require a concrete owner (real
        # ui/project sentinels are non-None strings and still resolve normally).
        accessible = owner is not None and may_access_artifact(owner, user_id, db=_db)
        return owner, True, accessible
    finally:
        if not db:  # close only the handle we self-opened (project convention)
            _db.close()


async def _authorize(artifact_id: str, user: dict) -> None:
    """404 if the artifact is absent, 403 if the caller may not act on it."""
    _, found, accessible = await run_in_db(
        lambda: _resolve_access(artifact_id, user["id"]))
    if not found:
        raise HTTPException(status_code=404, detail="artifact not found")
    if not accessible:
        raise HTTPException(status_code=403, detail="forbidden")


def _review_bar(artifact_id: str) -> dict | None:
    """The review bar's read for a document or image, or None for any other type."""
    db = get_db()
    try:
        row = AR.identity_row_for(artifact_id, db=db)
        if row is None or row["type"] not in public.ASKABLE_TYPES:
            return None
        person_step = public.person_step_instructions(str(row["id"]), db=db)
        return {
            "artifact_id": artifact_id, "artifact_type": row["type"],
            **review_bar.round_state(db, str(row["id"])),
            **({"person_step": person_step} if person_step is not None else {}),
        }
    finally:
        db.close()


def _summarize(item: dict) -> dict:
    """Shape one already-resolved list_raised item into a scannable surface row.

    No re-resolution here — owner/levels/metadata come from list_raised's single
    resolution pass.
    """
    ident = item["identity"]
    meta = item.get("metadata") or {}
    return {
        "external_id": item["external_id"],
        "name": item.get("title"),
        "integration": ident["integration"],
        "type": ident["type"],
        "subtype": meta.get("subtype"),
        "attention_state": meta.get("attention_state"),
        "requested_action": meta.get("requested_action"),
        "urgency": meta.get("urgency"),
        "confidence": meta.get("confidence"),
        "disposition_outcome": meta.get("disposition_outcome"),
        "defer_condition": meta.get("defer_condition"),
        "owner_thread": item.get("owner_thread"),
        "levels": item.get("levels", []),
        "hold_ids": item.get("hold_ids", []),
    }


def _authorized_raised(thread_id: str | None, user_id: str, *, db) -> list[dict]:
    """Raised rows this caller may act on — the single authorization pass."""
    return [
        item
        for item in attention.list_raised(thread_id=thread_id, db=db)
        # Reject ownerless (None) items — may_access_artifact(None) is True by
        # the sentinel convention and would expose them to any caller.
        if item.get("owner_thread") is not None
        and may_access_artifact(item["owner_thread"], user_id, db=db)
    ]


def _summarize_raised(thread_id: str | None, user_id: str, *, db=None) -> list[dict]:
    """Summarize the raised items no typed renderer draws (runs on the DB pool).

    Rows the queue renders are omitted here so the user is never shown the same
    item twice — but only because the server proved it can draw them, never
    because their type asserted someone would.
    """
    _db = db or get_db()
    try:
        annotated = public.annotate_attention(
            _authorized_raised(thread_id, user_id, db=_db), db=_db,
        )
        return [_summarize(item) for item in public.generic_attention(annotated)]
    finally:
        if not db:  # close only the handle we self-opened (project convention)
            _db.close()


# Upper bound on the ordered `items` the queue response carries.
QUEUE_ITEMS_CAP = 50


def _queue_for(user_id: str, *, db=None) -> list[dict]:
    """The one queue, user-global: it names no plan target, by design.

    A decision is listed only where Activity would count it, so the dock and
    Activity's awaiting count never disagree about an archived plan or project.
    Annotation marks a non-ask item whose every live link is paused, so the dock
    never shows it until resume; a paused document or image ask never reaches
    here, because ``list_raised`` already drops it.
    """
    _db = db or get_db()
    try:
        annotated = public.annotate_attention(
            _authorized_raised(None, user_id, db=_db), db=_db,
        )
        ordered = public.attention_queue(annotated)
        counted = public.activity_counted_plan_ids(
            {row["detail"]["plan_id"] for row in ordered if row["renderer"] == "decision"},
            actor=public.PlanActor(None, user_id, None, kind="user"), db=_db,
        )
        return [
            {"external_id": row["external_id"], "renderer": row["renderer"],
             "detail": row["detail"]}
            for row in ordered
            if row["renderer"] != "decision" or row["detail"]["plan_id"] in counted
        ]
    finally:
        if not db:
            _db.close()


def _reply_items_for(user_id: str) -> list[dict]:
    db = get_db()
    try:
        return reply_attention.unread_reply_items(db, user_id)
    finally:
        db.close()


# ── Lists ────────────────────────────────────────────────────────────────────

# GETs authenticate via the shared require_user seam (Bearer header OR query) so
# the token need not ride in the URL; the frontend sends it as a header.

@router.get("/raised")
async def get_raised(user: dict = Depends(require_user), thread_id: str | None = None):
    """Requires-user items, optionally scoped to a subscribed thread."""
    rows = await run_in_db(lambda: _summarize_raised(thread_id, user["id"]))
    return {"items": rows}


@router.get("/queue")
async def get_queue(user: dict = Depends(require_user)):
    """The one queue the user works through — head item plus how many wait.

    User-global on purpose: it takes no plan id, so a decision raised by a
    subagent two levels down is reachable from wherever the user already is.
    One item at a time (PRD-PLANS E8). `items` is the same ordered read, capped,
    so the user can choose which waiting item to answer next; `current` is
    always `items[0]`.
    """
    rows = await run_in_db(lambda: _queue_for(user["id"]))
    replies = await run_in_db(lambda: _reply_items_for(user["id"]))
    return {
        "current": rows[0] if rows else None,
        "count": len(rows),
        "items": rows[:QUEUE_ITEMS_CAP],
        **({"reply_items": replies} if replies else {}),
    }


@router.get("/rollup")
async def get_rollup(parent_thread_id: str, user: dict = Depends(require_user)):
    """Pull-only roll-up of attention items visible to a parent via the subscriber
    chain. A GET — never badges, never wakes (parent_chain stays digest)."""
    rows = await run_in_db(lambda: _summarize_raised(parent_thread_id, user["id"]))
    return {"parent_thread_id": parent_thread_id, "items": rows, "pull_only": True}


@router.get("/{artifact_id}/review")
async def get_review(artifact_id: str, user: dict = Depends(require_user),
                     profile_id: str | None = None):
    """The Artifact Review Profile result for a card (S5a).

    A document or image has no profile: its read is the review bar's, the newest
    raised ask and the round's feedback (DCOL-FR12.4).
    """
    await _authorize(artifact_id, user)
    bar = await run_in_db(lambda: _review_bar(artifact_id))
    if bar is not None:
        return bar
    try:
        result = await run_in_db(
            lambda: run_review_profile(artifact_id, profile_id=profile_id))
    except ProfileExecutionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return asdict(result)


# ── Actions ──────────────────────────────────────────────────────────────────

class ClearBody(BaseModel):
    token: str
    outcome: str
    condition: dict | None = None
    reason: str | None = None
    verdict: str | None = None
    note: str | None = None
    hold_ids: list[str] | None = None


@router.post("/{artifact_id}/clear")
async def post_clear(artifact_id: str, body: ClearBody):
    """A user clearing action (S6a). Closing a viewer does NOT hit this.

    An answered ask wakes its asker inside the write; that thread is driven
    once the write has committed, so its Turn starts now.
    """
    user = _require_user(body.token)
    await _authorize(artifact_id, user)
    woken: list[str] = []
    try:
        result = await run_in_db(lambda: attention.clear_attention(
            artifact_id, outcome=body.outcome, condition=body.condition,
            reason=body.reason, verdict=body.verdict, note=body.note,
            hold_ids=body.hold_ids, require_hold_ids=True, drives=woken,
        ))
    except review_bar.AnswerRefused as exc:
        status = 409 if exc.code == "ask_stale" else 422
        raise HTTPException(
            status_code=status, detail={"code": exc.code, "message": str(exc)},
        ) from exc
    except attention.AttentionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await public.drive_threads(woken)
    return asdict(result)


class DisposeBody(BaseModel):
    token: str
    outcome: str
    reason: str | None = None
    answer: str | None = None
    authority: str | None = None
    override_blocking: bool = False
    new_owner: str | None = None
    condition: dict | None = None
    replacement_id: str | None = None
    actor_thread: str | None = None
    hold_ids: list[str] | None = None


@router.post("/{artifact_id}/dispose")
async def post_dispose(artifact_id: str, body: DisposeBody):
    """A parent disposition (S5b). A blocking review failure halts approve_to_user
    unless an authorized override (authority + reason) is supplied."""
    user = _require_user(body.token)
    await _authorize(artifact_id, user)
    # The actor a disposition acts AS (override / reroute) must be a thread the
    # caller can access — so a conductor thread cannot merely be named by id.
    if body.actor_thread is not None and not await run_in_db(
            lambda: owns_thread(body.actor_thread, user["id"])):
        raise HTTPException(status_code=403, detail="actor_thread not accessible")
    woken: list[str] = []
    try:
        result = await run_in_db(lambda: D.dispose(
            artifact_id, body.outcome, reason=body.reason, answer=body.answer,
            authority=body.authority, override_blocking=body.override_blocking,
            new_owner=body.new_owner, condition=body.condition,
            replacement_id=body.replacement_id, actor_thread=body.actor_thread,
            hold_ids=body.hold_ids, require_hold_ids=True, drives=woken))
    except D.BlockingProfileError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except review_bar.AnswerRefused as exc:
        status = 409 if exc.code == "ask_stale" else 422
        raise HTTPException(
            status_code=status, detail={"code": exc.code, "message": str(exc)},
        ) from exc
    except D.DispositionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await public.drive_threads(woken)
    return asdict(result)
