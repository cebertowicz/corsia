"""One consent decision authorizing one read of the person's own files.

A consent is option A of one answered decision in the caller's own plan, raised
with a fixed wording CORSIA renders now, answered by the user, and used once.
``project_discovery`` and ``agent_import`` both check it here; each passes its
receipt operation, the wordings it renders now and the refusal texts that name
its own read. Use is recorded in the caller's transaction before anything is
read, so a crash mid-read still spends the consent rather than leaving it
replayable.

Plan-table reads stay behind the narrow public Plans interface, always through
``public.<name>`` so a test can observe each call.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

#: The option that authorizes the read, in every consent decision.
CONSENT_OPTION_ID = "A"


class ConsentRefused(Exception):
    """The consent decision cannot authorize the read; the message says why."""


@dataclass(frozen=True)
class ConsentTexts:
    """The refusals that name the caller's own read."""

    raise_first: str
    wrong_wording: str
    declined: str
    used: str


@dataclass(frozen=True)
class Consent:
    """A consent that passed every check, with the wording the user read."""

    plan: Any
    decision: dict[str, Any]
    bound: Any
    wording: tuple[str, str, str]


def wording_hash(title: str, context: str, option_label: str) -> str:
    """Hash the three strings the user read when consenting."""
    payload = json.dumps([title, context, option_label], separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def validate(db, decision_id: str, actor, wordings, texts: ConsentTexts) -> Consent:
    """Check one consent decision without spending it, or raise ``ConsentRefused``.

    ``actor`` is the calling thread's ``PlanActor``: the decision must be in the
    plan that thread owns, so a consent stays bound to the thread that raised it.
    ``wordings`` are the (title, context, option A label) triples CORSIA renders
    now; the decision must carry one of them, returned as ``Consent.wording``.
    """
    from plans import public

    try:
        plan, decision, _source, bound = public.resolve_decision(
            db, decision_id, actor, settled_source_ok=True,
        )
    except (PermissionError, public.PlanError) as exc:
        raise ConsentRefused(f"that decision is not in your plan; {texts.raise_first}") from exc
    if plan["owner_id"] != actor.thread_id:
        raise ConsentRefused(f"that decision is not in your plan; {texts.raise_first}")
    state = decision["state"]
    if state in ("draft", "parent_reviewed"):
        raise ConsentRefused(
            "that decision has not been raised to the user; raise it and wait "
            "for the user's answer"
        )
    if state == "raised":
        raise ConsentRefused("the user has not answered that decision yet; wait for the answer")
    if state != "answered":
        raise ConsentRefused(f"that decision was withdrawn; {texts.raise_first}")
    consent_option = next(
        (o for o in decision["options"] if o["id"] == CONSENT_OPTION_ID), None,
    )
    read = wording_hash(
        decision["title"], decision["context"],
        consent_option["label"] if consent_option else "",
    )
    matched = next(
        (tuple(expected) for expected in wordings if wording_hash(*expected) == read), None,
    )
    if matched is None:
        raise ConsentRefused(texts.wrong_wording)
    answered_by = public.decision_answer_actor(db, node_id=decision["node_id"])
    if not (answered_by or "").startswith("user:"):
        raise ConsentRefused("that decision was not answered by the user")
    chosen = public.chosen_ids(decision, decision.get("answer"))
    if public.pick_of(decision) == public.PICK_ANY or chosen != [CONSENT_OPTION_ID]:
        raise ConsentRefused(texts.declined)
    return Consent(plan, decision, bound, matched)


def _key(consent: Consent, operation: str, params: dict[str, Any] | None) -> tuple[str, str]:
    from plans import public

    decision_id = consent.decision["id"]
    fingerprint = public.request_fingerprint(operation, {"decision_id": decision_id, **(params or {})})
    suffix = "" if params is None else f":{fingerprint[:16]}"
    return f"{operation}:{decision_id}{suffix}", fingerprint


def load_receipt(
    db, consent: Consent, operation: str, texts: ConsentTexts,
    params: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """The result recorded under ``operation`` for this consent, or None."""
    from plans import public

    mutation_id, fingerprint = _key(consent, operation, params)
    try:
        return public.load_mutation_receipt(
            db, plan_id=consent.plan["id"], mutation_id=mutation_id,
            actor_id=consent.bound.audit_id, operation=operation, fingerprint=fingerprint,
        )
    except public.PlanError as exc:
        raise ConsentRefused(texts.used) from exc


def record_receipt(
    db, consent: Consent, operation: str, result: dict[str, Any],
    params: dict[str, Any] | None = None,
) -> None:
    """Record ``result`` under ``operation`` for this consent, in the caller's transaction."""
    from plans import public

    mutation_id, fingerprint = _key(consent, operation, params)
    public.record_mutation_receipt(
        db, plan_id=consent.plan["id"], mutation_id=mutation_id,
        actor_id=consent.bound.audit_id, operation=operation,
        fingerprint=fingerprint, result=result,
    )


def consume(
    db, decision_id: str, actor, wordings, texts: ConsentTexts, *,
    operation: str, result: dict[str, Any],
) -> Consent:
    """Accept one answered consent decision once, recording ``result``, or refuse."""
    consent = validate(db, decision_id, actor, wordings, texts)
    if load_receipt(db, consent, operation, texts) is not None:
        raise ConsentRefused(texts.used)
    record_receipt(db, consent, operation, result)
    return consent
