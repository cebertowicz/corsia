"""The two files a lesson capture can become, as text.

A complete hypothesis becomes a trial skill (``skills/<name>/SKILL.md``); an
incomplete one becomes a captured-hypothesis file in the owner's tray, which no
skill scan and no bootloader scan reads. Both are rendered and read here so the
placement writer, the skills index and the tests agree on one shape. Nothing
here writes: the versioned guidance writer owns the bytes.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.lesson_trials import (
    LESSON_KEY,
    REVIEW_REF_PREFIX,
    Lesson,
    Trial,
    behavioral_sha256,
    parse_instant,
    parse_lesson,
)

CAPTURE_KEY = "capture"
#: Under ``context/`` so the workspace's history tracks it, and one level down
#: so the bootloader scan (``context/*.md``) never delivers it.
CAPTURE_DIR = ("context", "captures")

_TRIAL_BODY_PREFIX = (
    "# Trial lesson\n\n"
    "An unproven hypothesis under review, not a standing rule. It grants no "
    "new permission.\n\n- "
)
_TRIAL_BOUNDARY_PREFIX = "\nDoes not apply: "

#: Why a hypothesis is captured rather than offered, in the owner's words.
HOLD_REASONS = {
    "missing-derivation": (
        "it cites what a person said and gives no derivation, so the rule is not "
        "yet shown to follow from their words"
    ),
    "literal-unresolved": (
        "it cites what a person said and does not point at where their exact "
        "words are kept"
    ),
    "no-selective-trigger": (
        "it has no trigger and boundary that say when it applies and when it does not"
    ),
    "over-budget": "the trial lessons already offered fill the lesson index budget",
}


@dataclass(frozen=True)
class TrialFile:
    """One valid trial skill as read from its text."""

    lesson: Lesson
    name: str
    description: str
    body: str
    behavioral_sha256: str
    statement: str


@dataclass(frozen=True)
class CaptureFile:
    """One captured-hypothesis file as read from its text."""

    id: str
    hash: str
    source_refs: tuple[dict[str, str], ...]
    originating_subjects: tuple[str, ...]


def _one_line(value: object) -> str:
    return " ".join(str(value).split())


def capture_path(tray: Path, capture_id: str) -> Path:
    """Where one captured hypothesis lives in its owner's tray."""
    return tray.joinpath(*CAPTURE_DIR, f"{capture_id}.md")


def merged_refs(*groups: Any) -> list[dict[str, str]]:
    """Evidence references in first-seen order, each once."""
    return _refs([ref for group in groups for ref in group if isinstance(ref, dict)])


def _refs(refs: list[dict[str, Any]]) -> list[dict[str, str]]:
    seen: list[dict[str, str]] = []
    for ref in refs:
        clean = {"kind": _one_line(ref.get("kind")), "reference": _one_line(ref.get("reference"))}
        if clean not in seen:
            seen.append(clean)
    return seen


def _dump(metadata: dict[str, Any], body: str) -> str:
    import yaml

    front = yaml.safe_dump(metadata, sort_keys=False, allow_unicode=True, width=10_000)
    return f"---\n{front}---\n\n{body.strip()}\n"


def _day(review_after: str) -> str:
    when = parse_instant(review_after)
    return when.date().isoformat() if when else review_after


def _trial_body(text: str, boundary: str | None) -> str:
    """Render the canonical body shape shared by trial writes and list reads."""
    body = _TRIAL_BODY_PREFIX + _one_line(text) + "\n"
    if boundary:
        body += _TRIAL_BOUNDARY_PREFIX + boundary + "\n"
    return body


def trial_statement(body: str) -> str:
    """Return a trial's one-line statement from its validated body.

    Canonical bodies expose their hypothesis directly. A lesson validator also
    accepts hand-edited bodies, so their complete body is the visible statement
    rather than a second, stricter validity check.
    """
    if body.startswith("\n"):
        body = body[1:]
    if not body.startswith(_TRIAL_BODY_PREFIX):
        return _one_line(body)
    remainder = body.removeprefix(_TRIAL_BODY_PREFIX)
    statement, separator, suffix = remainder.partition("\n")
    if not statement:
        return _one_line(body)
    if not separator or suffix == "":
        return statement
    if not suffix.startswith(_TRIAL_BOUNDARY_PREFIX):
        return _one_line(body)
    boundary = suffix.removeprefix(_TRIAL_BOUNDARY_PREFIX).removesuffix("\n")
    return statement if boundary and "\n" not in boundary else _one_line(body)


def render_trial(
    *, lesson_id: str, owner: str, name: str, description: str, text: str,
    trial: Trial, source_refs: list[dict[str, Any]], originating_subjects: tuple[str, ...] | None = None,
    origin: dict[str, str] | None = None, case_metadata: dict[str, Any] | None = None,
) -> str:
    """A trial skill with no ``review_ref``: written, and not yet offered.

    The body carries neither the name nor the review date, so a rename or an
    extension leaves the behavioral version, and its review, in place.
    ``origin`` names the retired lesson this one captures again; ``case_metadata``
    is its validated case metadata (``lesson_case``).
    """
    body = _trial_body(text, trial.boundary)
    lesson = {
        "id": lesson_id, "state": trial.state, "owner": owner,
        "review_after": trial.review_after, "source_refs": _refs(source_refs),
    }
    if originating_subjects is not None:
        lesson["originating_subjects"] = list(originating_subjects)
    if origin:
        lesson["origin"] = dict(origin)
    lesson.update(case_metadata or {})
    return _dump(
        {"name": name, "description": _one_line(description), "corsia": {LESSON_KEY: lesson}},
        body,
    )


def read_trial(text: str) -> TrialFile | None:
    """The trial a ``SKILL.md`` text is, or None when it is not a valid lesson."""
    from core.skill_validation import validate_skill_text

    parsed = validate_skill_text(text)
    lesson = parse_lesson(parsed.metadata) if parsed.ok else None
    if lesson is None or not parsed.name or not parsed.description:
        return None
    return TrialFile(
        lesson=lesson, name=parsed.name, description=parsed.description, body=parsed.body,
        behavioral_sha256=behavioral_sha256(parsed.description, parsed.body),
        statement=trial_statement(parsed.body),
    )


def _with_lesson(text: str, **fields: Any) -> str:
    """The same skill with administrative lesson fields changed; its body untouched."""
    from core.skill_validation import validate_skill_text

    parsed = validate_skill_text(text)
    metadata = dict(parsed.metadata)
    corsia = dict(metadata["corsia"])
    corsia[LESSON_KEY] = {**corsia[LESSON_KEY], **fields}
    metadata["corsia"] = corsia
    return _dump(metadata, parsed.body)


def with_review_ref(text: str, node_id: str) -> str:
    """The same trial with its review reference written: the activating edit."""
    return _with_lesson(text, review_ref=f"{REVIEW_REF_PREFIX}{node_id}")


def with_source_refs(text: str, source_refs: list[dict[str, Any]]) -> str:
    """The same trial with its evidence list replaced: its version does not change."""
    return _with_lesson(text, source_refs=_refs(source_refs))


def raw_sha256(data: bytes) -> str:
    """The exact-bytes version of a lesson file, as a guidance read records it."""
    return hashlib.sha256(data).hexdigest()


def render_capture(
    *, capture_id: str, capture_hash: str, owner: str, reason: str, name: str | None,
    description: str | None, text: str, trial: Trial, source_refs: list[dict[str, Any]],
    originating_subjects: tuple[str, ...] = (),
) -> str:
    """A captured hypothesis: kept and reviewed, never offered as guidance."""
    capture: dict[str, Any] = {
        "id": capture_id, "hash": capture_hash, "state": "captured", "reason": reason,
        "kind": trial.kind, "owner": owner, "review_after": trial.review_after,
        "source_refs": _refs(source_refs),
        "originating_subjects": list(originating_subjects),
    }
    if trial.derivation:
        capture["derivation"] = trial.derivation
    if trial.literal_ref:
        capture["literal_ref"] = dict(trial.literal_ref)
    body = (
        f"# Captured hypothesis: {name or capture_id}\n\n"
        f"Not offered as guidance: {HOLD_REASONS[reason]}. Review by "
        f"{_day(trial.review_after)}.\n\n- {_one_line(text)}\n"
    )
    if description:
        body += f"\nWhen: {_one_line(description)}\n"
    if trial.boundary:
        body += f"\nDoes not apply: {trial.boundary}\n"
    return _dump({"corsia": {CAPTURE_KEY: capture}}, body)


def read_capture(text: str) -> CaptureFile | None:
    """The capture a file text records, or None when it is not one."""
    import yaml

    from core.skill_validation import parse_frontmatter

    try:
        metadata, _body = parse_frontmatter(text)
    except (ValueError, yaml.YAMLError):  # frontmatter that does not parse is not a capture
        return None
    corsia = metadata.get("corsia")
    capture = corsia.get(CAPTURE_KEY) if isinstance(corsia, dict) else None
    if not isinstance(capture, dict) or not capture.get("id") or not capture.get("hash"):
        return None
    refs = capture.get("source_refs")
    subjects = capture.get("originating_subjects")
    return CaptureFile(
        id=str(capture["id"]), hash=str(capture["hash"]),
        source_refs=tuple(_refs(refs if isinstance(refs, list) else [])),
        originating_subjects=tuple(subject for subject in subjects if isinstance(subject, str))
        if isinstance(subjects, list) else (),
    )


def superseded_capture(text: str, lesson_id: str) -> str | None:
    """The same capture marked as the trial it became; None when it is not one."""
    from core.skill_validation import parse_frontmatter

    if read_capture(text) is None:
        return None
    metadata, body = parse_frontmatter(text)
    capture = {**metadata["corsia"][CAPTURE_KEY], "state": "superseded", "lesson": lesson_id}
    return _dump({"corsia": {CAPTURE_KEY: capture}}, body)
