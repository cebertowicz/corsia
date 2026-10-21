"""Lesson trials: an unproven behavioral hypothesis carried as a skill until reviewed.

A trial is an ordinary ``skills/<name>/SKILL.md`` whose ``corsia.lesson`` block
gives it a stable identity, an owner and a review date. The file's scope is its
path; discovery, history and delivery stay the skill registry's. This module is
the one definition of that block, of the two hashes that separate a behavioral
change from an administrative one, and of the ``trial`` object a placement
answer carries. It reads no database and no file.

A trial is only ever a ``hypothesis``. A person's instruction, preference or
constraint keeps its permanent home and never expires here.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

LESSON_KEY = "lesson"
STATES = ("watch", "trial", "retired", "promoted")
KINDS = ("instruction", "preference", "constraint", "hypothesis")
PERMANENT_KINDS = frozenset(KINDS) - {"hypothesis"}

#: Provisional bounds (lesson-trials design, section 3): to be validated on the
#: fixture set, not empirical optima.
BODY_WORDS = 150
DESCRIPTION_WORDS = 40
# Trial bodies never enter the cached skills index.
REVIEW_REF_PREFIX = "plan-node:"
_FIELDS = frozenset({
    "id", "state", "owner", "review_after", "source_refs", "review_ref",
    "origin", "destination", "originating_subjects", "target_file", "new_target_path", "target_section",
    "cause", "case",
})

TRIAL_SCHEMA: dict[str, Any] = {
    "type": ["object", "null"],
    "additionalProperties": False,
    "required": ["kind", "review_after"],
    "properties": {
        "kind": {"type": "string", "enum": list(KINDS)},
        "state": {"type": "string", "enum": ["watch", "trial"]},
        "review_after": {"type": "string", "description": "Future ISO date to review it."},
        # The hold replies name these three and say what each takes.
        "boundary": {"type": ["string", "null"]},
        "incident_detail_reason": {"type": ["string", "null"]},
        "derivation": {"type": ["string", "null"]},
        "literal_ref": {
            "type": ["object", "null"],
            "description": "Where a person's exact words are kept.",
            "additionalProperties": False,
            "properties": {
                "entry_id": {"type": "integer"},
                "destination": {"type": "string"},
                "quote": {"type": "string"},
            },
        },
    },
}


@dataclass(frozen=True)
class Trial:
    """The ``trial`` object of one placement answer."""

    kind: str
    review_after: str
    state: str = "trial"
    boundary: str | None = None
    incident_detail_reason: str | None = None
    derivation: str | None = None
    literal_ref: dict[str, Any] | None = None


@dataclass(frozen=True)
class Lesson:
    """One validated ``corsia.lesson`` block."""

    id: str
    state: str
    owner: str
    review_after: str
    source_refs: tuple[dict[str, str], ...]
    originating_subjects: tuple[str, ...] = ()
    review_ref: str | None = None
    origin: dict[str, str] | None = None
    destination: dict[str, str] | None = None
    target_file: str | None = None
    new_target_path: str | None = None
    target_section: str | None = None
    cause: str | None = None
    case: dict[str, Any] | None = None

    @property
    def review_node_id(self) -> str | None:
        """The plan node its review reference names, if it names one."""
        ref = self.review_ref or ""
        return ref.removeprefix(REVIEW_REF_PREFIX) if ref.startswith(REVIEW_REF_PREFIX) else None


def words(text: str) -> int:
    """Word count, as the trial bounds measure it."""
    return len(text.split())


def parse_instant(value: object) -> datetime | None:
    """An ISO date or instant as an aware UTC time; None when it is neither."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def _clean(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    return " ".join(value.split()) or None


def validate_trial(raw: object) -> Trial | None:
    """Check one answer's ``trial`` object; ``ValueError`` says what to correct."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("trial must be an object or null")
    unknown = sorted(set(raw) - set(TRIAL_SCHEMA["properties"]))
    if unknown:
        raise ValueError(f"trial has unknown keys: {', '.join(unknown)}")
    if raw.get("kind") not in KINDS:
        raise ValueError(f"trial.kind must be one of {', '.join(KINDS)}")
    if raw.get("state", "trial") not in ("watch", "trial"):
        raise ValueError("trial.state must be watch or trial")
    when = parse_instant(raw.get("review_after"))
    if when is None:
        raise ValueError(
            "trial.review_after must be an ISO date or instant, for example 2026-10-15"
        )
    return Trial(
        kind=str(raw["kind"]),
        review_after=when.isoformat(),
        state=str(raw.get("state", "trial")),
        boundary=_clean(raw.get("boundary")),
        incident_detail_reason=_clean(raw.get("incident_detail_reason")),
        derivation=_clean(raw.get("derivation")),
        literal_ref=_literal_ref(raw.get("literal_ref")),
    )


def _literal_ref(raw: object) -> dict[str, Any] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("trial.literal_ref must be an object or null")
    entry_id, destination, quote = raw.get("entry_id"), raw.get("destination"), raw.get("quote")
    if isinstance(entry_id, int) and not isinstance(entry_id, bool) and entry_id > 0:
        if destination is not None or quote is not None:
            raise ValueError("trial.literal_ref takes entry_id, or destination and quote, not both")
        return {"entry_id": entry_id}
    if _clean(destination) and isinstance(quote, str) and quote.strip():
        return {"destination": str(destination).strip(), "quote": quote.strip()}
    raise ValueError(
        "trial.literal_ref names where the person's exact words are kept: "
        "{entry_id} for a continuity entry, or {destination, quote} for a file line"
    )


def lesson_id(thread_id: str, history_epoch: int, entry_id: int) -> str:
    """The stable identity of the capture one continuity entry becomes.

    Derived, so a retried placement names the same capture and the same trial.
    """
    name = f"corsia-lesson:{thread_id}:{history_epoch}:{entry_id}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, name))


def _sha256(payload: Any) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def behavioral_sha256(description: str, body: str) -> str:
    """The version of what a trial tells an agent: its trigger and its body.

    The name and the ``corsia.lesson`` block are administrative: a rename, a
    new review date or appended evidence keeps this hash.
    """
    lines = [line.rstrip() for line in body.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    return _sha256({"description": " ".join(description.split()), "body": "\n".join(lines).strip()})


def capture_hash(statement: str, trial: Trial) -> str:
    """The version of one capture: its hypothesis and classification inputs only.

    Evidence is not hashed, so appending evidence never asks for another review.
    """
    return _sha256({
        "statement": " ".join(statement.split()), "kind": trial.kind,
        "derivation": trial.derivation, "literal_ref": trial.literal_ref,
    })


def _refs(value: object) -> tuple[dict[str, str], ...] | None:
    if not isinstance(value, list) or not value:
        return None
    refs = []
    for ref in value:
        if not isinstance(ref, dict) or not _clean(ref.get("kind")) or not _clean(ref.get("reference")):
            return None
        refs.append({"kind": str(ref["kind"]).strip(), "reference": str(ref["reference"]).strip()})
    return tuple(refs)


def _subjects(value: object) -> tuple[str, ...] | None:
    if not isinstance(value, list):
        return None
    subjects = []
    for raw in value:
        if not _clean(raw):
            return None
        subject = str(raw).strip()
        if subject not in subjects:
            subjects.append(subject)
    return tuple(subjects)


def _case(value: object) -> dict[str, Any] | None:
    keys = ("what_happened", "right_call", "does_not_apply", "correction_subject")
    allowed = {*keys, "actions"}
    if not isinstance(value, dict) or set(value) - allowed or not set(keys) <= set(value):
        return None
    cleaned = {key: _clean(value.get(key)) for key in keys}
    if not all(cleaned.values()):
        return None
    case: dict[str, Any] = {key: text for key, text in cleaned.items() if text is not None}
    if "actions" in value:
        try:
            from core.lesson_carry_in import validate_actions

            actions = validate_actions(value["actions"])
        except ValueError:
            return None
        if actions is not None:
            case["actions"] = actions
    return case


def _pair(value: object, first: str, second: str) -> dict[str, str] | None:
    """A mapping carrying ``first`` and optionally ``second``, both text."""
    if not isinstance(value, dict) or not _clean(value.get(first)) or set(value) - {first, second}:
        return None
    if second in value and not _clean(value.get(second)):
        return None
    return {key: str(value[key]).strip() for key in (first, second) if key in value}


def lesson_errors(block: object, description: str | None, body: str) -> list[str]:
    """Every reason a ``corsia.lesson`` block is not a valid lesson."""
    if not isinstance(block, dict):
        return ["corsia.lesson must be a mapping"]
    errors = [f"corsia.lesson.{key} is an unknown key" for key in sorted(set(block) - _FIELDS)]
    try:
        uuid.UUID(str(block.get("id")))
    except ValueError:
        errors.append("corsia.lesson.id must be a UUID")
    state = block.get("state")
    if state not in STATES:
        errors.append(f"corsia.lesson.state must be one of {', '.join(STATES)}")
    if not _clean(block.get("owner")):
        errors.append("corsia.lesson.owner is required")
    if parse_instant(block.get("review_after")) is None:
        errors.append("corsia.lesson.review_after must be an ISO date or instant")
    if _refs(block.get("source_refs")) is None:
        errors.append("corsia.lesson.source_refs must list at least one {kind, reference}")
    if "originating_subjects" in block and _subjects(block["originating_subjects"]) is None:
        errors.append("corsia.lesson.originating_subjects must be a list of non-empty subject keys")
    if "review_ref" in block and not _clean(block.get("review_ref")):
        errors.append("corsia.lesson.review_ref must be a non-empty string")
    if "origin" in block and _pair(block["origin"], "id", "behavioral_sha256") is None:
        errors.append("corsia.lesson.origin must be {id, behavioral_sha256}")
    if "target_file" in block and not _clean(block.get("target_file")):
        errors.append("corsia.lesson.target_file must be a non-empty string")
    if "new_target_path" in block and not _clean(block.get("new_target_path")):
        errors.append("corsia.lesson.new_target_path must be a non-empty string")
    if "target_section" in block and not _clean(block.get("target_section")):
        errors.append("corsia.lesson.target_section must be a non-empty string")
    if "cause" in block and block.get("cause") not in (
        "wrong_rule", "no_foundation", "rule_gap", "process_gap", "not_delivered", "not_followed", "cant_be_done",
    ):
        errors.append("corsia.lesson.cause must be a System correction cause")
    if "case" in block and _case(block.get("case")) is None:
        errors.append("corsia.lesson.case must name what_happened, right_call, does_not_apply and correction_subject; optional actions must be a valid carry-in action contract")
    if state == "promoted" and _pair(block.get("destination"), "path", "version") is None:
        errors.append("corsia.lesson.destination {path, version} is required when promoted")
    elif "destination" in block and _pair(block["destination"], "path", "version") is None:
        errors.append("corsia.lesson.destination must be {path, version}")
    if words(body) > BODY_WORDS:
        errors.append(f"a lesson body is at most {BODY_WORDS} words; link the procedure instead")
    if words(description or "") > DESCRIPTION_WORDS:
        errors.append(f"a lesson description is at most {DESCRIPTION_WORDS} words")
    return errors


def lesson_block(metadata: dict[str, Any]) -> object | None:
    """The raw ``corsia.lesson`` value of parsed skill frontmatter, if present."""
    corsia = metadata.get("corsia")
    return corsia.get(LESSON_KEY) if isinstance(corsia, dict) else None


def parse_lesson(metadata: dict[str, Any]) -> Lesson | None:
    """The lesson of already-validated skill frontmatter; None for a plain skill."""
    block = lesson_block(metadata)
    if not isinstance(block, dict):
        return None
    when = parse_instant(block.get("review_after"))
    return Lesson(
        id=str(block.get("id")), state=str(block.get("state")),
        owner=str(block.get("owner") or "").strip(),
        review_after=when.isoformat() if when else "",
        source_refs=_refs(block.get("source_refs")) or (),
        originating_subjects=_subjects(block.get("originating_subjects")) or (),
        review_ref=_clean(block.get("review_ref")),
        origin=_pair(block.get("origin"), "id", "behavioral_sha256"),
        destination=_pair(block.get("destination"), "path", "version"),
        target_file=_clean(block.get("target_file")),
        new_target_path=_clean(block.get("new_target_path")),
        target_section=_clean(block.get("target_section")),
        cause=_clean(block.get("cause")),
        case=_case(block.get("case")),
    )


def offer_refusal(lesson: Lesson, now: datetime) -> str | None:
    """Why a lesson's own fields keep it out of a newly installed index, or None."""
    if lesson.state != "trial":
        return f"lesson is {lesson.state}"
    if lesson.review_node_id is None:
        return "trial has no scheduled review"
    due = parse_instant(lesson.review_after)
    if due is None or due <= now:
        return "trial review date has passed"
    return None
