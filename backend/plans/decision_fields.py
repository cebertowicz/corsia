"""Tool-enforced structure for one plan decision (PRD-PLANS E2-E4).

PRD-PLANS E3 requires a "hard max-length cap" so a decision is self-contained by
construction, but neither the PRD, `RFC-STRUCTURED-PLANS`, nor `PLANS-ACCEPTANCE`
names a number. The bounds below are therefore this module's declared contract:
a worst-case decision is about 4.2 kB, enough for self-contained context without
letting one decision dominate the operator payload.
"""

from __future__ import annotations

from typing import Any

import plans.service as service


class DecisionInputError(service.PlanError):
    """A refusal the caller can fix in place, without anything having moved.

    Distinct from the revision/state conflicts that share ``PlanError``: those
    mean the decision changed underneath the caller and the only cure is to
    re-read it. Keeping the two apart is what lets the answer route reply 422
    for "fix this and resend" and reserve 409 for "it actually moved" — the
    client cannot give truthful direction otherwise.
    """


MAX_TITLE = 120
MAX_CONTEXT = 1200
MAX_TRADEOFF = 800
MAX_RATIONALE = 800
MAX_OPTION_LABEL = 80
MAX_OPTION_DETAIL = 240
MAX_OTHER_TEXT = 2000

MIN_OPTIONS = 2
MAX_OPTIONS = 4
#: Canonical option labels. A decision's ids are always this exact prefix (E4).
OPTION_IDS = ("A", "B", "C", "D")
#: The reserved id for the always-present free-form answer (E4: never strand).
OTHER_OPTION_ID = "other"

#: A decision's pick kind: exactly one option (the default), or any subset.
PICK_ONE = "one"
PICK_ANY = "any"
PICKS = (PICK_ONE, PICK_ANY)

TEXT_FIELDS = {
    "title": MAX_TITLE,
    "context": MAX_CONTEXT,
    "tradeoff": MAX_TRADEOFF,
    "rationale": MAX_RATIONALE,
}
CONTENT_FIELDS = (*TEXT_FIELDS, "recommendation", "options", "other_enabled", "pick")
#: `pick` is optional on draft: a body without it reads as pick one everywhere.
_OPTIONAL_CONTENT = frozenset({"pick"})

OPERATION_FIELDS = {
    "draft": {"source_node_id", *CONTENT_FIELDS},
    "update": {"decision_id", "expected_revision", *CONTENT_FIELDS},
    "reorder": {"decision_id", "order_key", "expected_revision"},
    "parent_review": {"decision_id", "expected_revision"},
    "raise": {"decision_id", "expected_revision"},
    "invalidate": {"decision_id", "expected_revision"},
}
OPERATION_REQUIRED_FIELDS = {
    "draft": {"source_node_id", *(set(CONTENT_FIELDS) - _OPTIONAL_CONTENT)},
    "update": {"decision_id", "expected_revision"},
    "reorder": {"decision_id", "order_key", "expected_revision"},
    "parent_review": {"decision_id", "expected_revision"},
    "raise": {"decision_id", "expected_revision"},
    "invalidate": {"decision_id", "expected_revision"},
}


def bounded_text(value, field: str, limit: int) -> str:
    """Normalize one required decision field or reject it before any write."""
    if not isinstance(value, str):
        raise DecisionInputError(f"decision {field} is required")
    normalized = value.strip()
    if not normalized:
        raise DecisionInputError(f"decision {field} is required")
    if len(normalized) > limit:
        raise DecisionInputError(
            f"decision {field} is too long (limit {limit} characters)"
        )
    return normalized


def _option(value, index: int) -> dict:
    if not isinstance(value, dict):
        raise DecisionInputError("decision options must be objects")
    expected = OPTION_IDS[index]
    if value.get("id") != expected:
        raise DecisionInputError(
            f"decision option {index + 1} must be labelled {expected!r}"
        )
    detail = value.get("detail") or ""
    if not isinstance(detail, str):
        raise DecisionInputError("decision option detail must be text")
    if len(detail.strip()) > MAX_OPTION_DETAIL:
        raise DecisionInputError(
            f"decision option detail is too long (limit {MAX_OPTION_DETAIL})"
        )
    return {
        "id": expected,
        "label": bounded_text(value.get("label"), "option label", MAX_OPTION_LABEL),
        "detail": detail.strip(),
    }


def options(value) -> list[dict]:
    """Validate the bounded, ordered, unique A/B/C/D option set (E4)."""
    if not isinstance(value, list):
        raise DecisionInputError("decision options must be a list")
    if not MIN_OPTIONS <= len(value) <= MAX_OPTIONS:
        raise DecisionInputError(
            f"a decision needs {MIN_OPTIONS}-{MAX_OPTIONS} options"
        )
    resolved = [_option(item, index) for index, item in enumerate(value)]
    labels = {option["label"].casefold() for option in resolved}
    if len(labels) != len(resolved):
        raise DecisionInputError("decision option labels must be distinct")
    return resolved


def pick_of(body: dict) -> str:
    """The one place a stored or projected body's pick kind is read.

    A body written before pick-any has no ``pick``; it is pick one.
    """
    return PICK_ANY if body.get("pick") == PICK_ANY else PICK_ONE


def _recommendation(value, pick: str, valid: list[str]):
    """Validate a recommendation in the form the decision's pick kind takes."""
    if pick == PICK_ANY:
        if not isinstance(value, list):
            raise DecisionInputError(
                "A pick-any decision's recommendation is a list of the option "
                'ids to pre-tick, for example ["A", "C"]. Use [] to recommend none.'
            )
        seen: set[str] = set()
        for item in value:
            if not isinstance(item, str) or item not in valid:
                raise DecisionInputError(
                    f'Recommendation names "{item}", which isn\'t one of the '
                    f"options supplied ({valid[0]}-{valid[-1]})."
                )
            if item in seen:
                raise DecisionInputError(f'Recommendation lists "{item}" twice.')
            seen.add(item)
        return [option_id for option_id in valid if option_id in seen]
    if isinstance(value, list):
        raise DecisionInputError(
            "A pick-one decision's recommendation is one option id, for example "
            '"B". If the person may choose several, set pick to "any".'
        )
    if not isinstance(value, str) or value not in valid:
        raise DecisionInputError(
            "decision recommendation must name one supplied option"
        )
    return value


def content(payload: dict) -> dict:
    """Validate one whole decision body; callers merge patches before this."""
    resolved: dict[str, Any] = {
        field: bounded_text(payload.get(field), field, limit)
        for field, limit in TEXT_FIELDS.items()
    }
    resolved["options"] = options(payload.get("options"))
    pick = PICK_ONE if payload.get("pick") is None else payload["pick"]
    if pick not in PICKS:
        raise DecisionInputError('pick must be "one" or "any".')
    resolved["pick"] = pick
    resolved["recommendation"] = _recommendation(
        payload.get("recommendation"), pick,
        [option["id"] for option in resolved["options"]],
    )
    if payload.get("other_enabled") is not True:
        raise DecisionInputError(
            "a decision must always offer the free-form Other option"
        )
    resolved["other_enabled"] = True
    return resolved


def answer(option_id, other_text, available: list[dict]) -> dict:
    """Validate one user answer against the decision's own option set.

    Every refusal here is a ``DecisionInputError``: nothing has moved, and the
    user can correct it without re-reading the decision.
    """
    if not isinstance(option_id, str) or not option_id.strip():
        raise DecisionInputError("Choose an option before answering.")
    option_id = option_id.strip()
    if option_id == OTHER_OPTION_ID:
        return {
            "option_id": OTHER_OPTION_ID,
            "text": bounded_text(other_text, "answer text", MAX_OTHER_TEXT),
        }
    if option_id not in {option["id"] for option in available}:
        raise DecisionInputError("That option is not one of the choices offered.")
    if other_text is not None and str(other_text).strip():
        raise DecisionInputError(
            "Free text belongs to the Other option, not a listed option."
        )
    return {"option_id": option_id, "text": None}


def answer_any(option_ids, note, available: list[dict]) -> dict:
    """Validate one pick-any answer: any subset of the options, even none.

    The stored shape is exactly ``{option_ids, text}``: ids unique and in
    option order, and an empty note stored as null.
    """
    offered = [option["id"] for option in available]
    if option_ids is None:
        raise DecisionInputError(
            "Send option_ids: a list of the chosen ids, [] for none."
        )
    if not isinstance(option_ids, list) or any(
        not isinstance(item, str) or item.strip() not in offered
        for item in option_ids
    ):
        raise DecisionInputError("That option is not one of the choices offered.")
    picked = {item.strip() for item in option_ids}
    text = None
    if note is not None and str(note).strip():
        text = bounded_text(note, "answer text", MAX_OTHER_TEXT)
    return {
        "option_ids": [option_id for option_id in offered if option_id in picked],
        "text": text,
    }


def answer_for(decision: dict, option_id, option_ids, other_text) -> dict:
    """Validate an answer in the shape the decision's pick kind takes.

    Every refusal is a ``DecisionInputError``, so a revision conflict that
    precedes it still reaches the caller as a conflict.
    """
    any_pick = pick_of(decision) == PICK_ANY
    if option_id is not None and option_ids is not None:
        raise DecisionInputError(
            "Send option_ids for this decision, not option_id."
            if any_pick
            else "Send option_id for this decision, not option_ids."
        )
    if any_pick:
        if option_id is not None:
            raise DecisionInputError(
                "This decision lets you choose several options. Reload to answer it."
            )
        return answer_any(option_ids, other_text, decision["options"])
    if option_ids is not None:
        raise DecisionInputError("This decision takes one option.")
    return answer(option_id, other_text, decision["options"])


def recommended_ids(decision: dict) -> list[str]:
    """The option ids the author recommends, whichever pick kind it is."""
    recommendation = decision.get("recommendation")
    if pick_of(decision) == PICK_ANY:
        return list(recommendation or [])
    if isinstance(recommendation, str) and recommendation:
        return [recommendation]
    return []


def chosen_ids(decision: dict, answer: dict | None) -> list[str]:
    """The listed option ids an answer chose; none for pick-one Other."""
    if not isinstance(answer, dict):
        return []
    if pick_of(decision) == PICK_ANY:
        return list(answer.get("option_ids") or [])
    option_id = answer.get("option_id")
    listed = {option["id"] for option in decision.get("options") or []}
    return [option_id] if isinstance(option_id, str) and option_id in listed else []


def answer_label(decision: dict, answer: dict) -> str:
    """One short string naming what was chosen: ids joined, or the pick-one id."""
    if pick_of(decision) == PICK_ANY:
        return ",".join(chosen_ids(decision, answer))
    return str((answer or {}).get("option_id") or "")


def answer_note(answer: dict | None) -> str | None:
    """The free text an answer carries: a pick-one Other, or a pick-any note."""
    if not isinstance(answer, dict):
        return None
    return answer.get("text") or None


def patched(existing: dict, patch: dict) -> dict:
    """Merge a partial update onto the stored body, rejecting unknown fields."""
    unknown = sorted(set(patch) - set(CONTENT_FIELDS))
    if unknown:
        raise DecisionInputError(f"unknown decision fields: {', '.join(unknown)}")
    if not patch:
        raise DecisionInputError("a decision update must change something")
    merged = {
        field: existing[field] for field in CONTENT_FIELDS if field != "pick"
    }
    merged["pick"] = pick_of(existing)
    merged.update(patch)
    return content(merged)
