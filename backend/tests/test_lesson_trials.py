"""A trial lesson is a skill with a ``corsia.lesson`` block.

- The block is validated with the skill: a malformed lesson is a skill error,
  never an offered skill. A plain skill is untouched.
- What a trial tells an agent (its trigger and body) has one hash; its name,
  review date, evidence and review reference do not change it.
- A capture's hash covers the hypothesis and its classification inputs only.
- A trial file is written without a review reference, and is not offered until
  one is written and its date is still ahead.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core import lesson_files, lesson_trials
from core.lesson_trials import Trial
from core.skill_validation import validate_skill_text

LESSON = "0f3a6f0e-7f0e-5a55-9d40-6f1f0e6f7a11"
NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)
AFTER = (NOW + timedelta(days=14)).isoformat()
REFS = [{"kind": "turn", "reference": "turn-1"}]
TRIAL = Trial(kind="hypothesis", review_after=AFTER, boundary="A one-line typo fix.")


def _trial_text(**changes) -> str:
    fields = {
        "lesson_id": LESSON, "owner": "owner", "name": "cite-the-head",
        "description": "Use when reporting a check result on a pull request.",
        "text": "Name the exact commit the check ran on.", "trial": TRIAL,
        "source_refs": REFS,
    }
    return lesson_files.render_trial(**{**fields, **changes})


def _skill(lesson: str, *, body: str = "# Body\n", description: str = "Use when testing.") -> str:
    return f"---\nname: sample\ndescription: {description}\ncorsia:\n  lesson:\n{lesson}---\n{body}"


VALID_BLOCK = (
    f"    id: {LESSON}\n    state: trial\n    owner: owner\n"
    f"    review_after: '{AFTER}'\n    source_refs:\n    - kind: turn\n      reference: turn-1\n"
)


def test_a_plain_skill_and_a_valid_lesson_both_validate():
    plain = validate_skill_text("---\nname: plain\ndescription: Use when testing.\n---\n# Body\n")
    assert plain.ok and lesson_trials.parse_lesson(plain.metadata) is None

    lesson = validate_skill_text(_skill(VALID_BLOCK))
    assert lesson.ok, lesson.errors
    assert lesson_trials.parse_lesson(lesson.metadata).id == LESSON


@pytest.mark.parametrize(
    ("block", "error"),
    [
        (VALID_BLOCK + "    scope: project\n", "corsia.lesson.scope is an unknown key"),
        (VALID_BLOCK.replace(LESSON, "not-a-uuid"), "corsia.lesson.id must be a UUID"),
        (VALID_BLOCK.replace("state: trial", "state: active"),
         "corsia.lesson.state must be one of watch, trial, retired, promoted"),
        (VALID_BLOCK.replace("    owner: owner\n", ""), "corsia.lesson.owner is required"),
        (VALID_BLOCK.replace(f"'{AFTER}'", "soon"),
         "corsia.lesson.review_after must be an ISO date or instant"),
        (VALID_BLOCK.split("    source_refs")[0],
         "corsia.lesson.source_refs must list at least one {kind, reference}"),
        (VALID_BLOCK.replace("state: trial", "state: promoted"),
         "corsia.lesson.destination {path, version} is required when promoted"),
        (VALID_BLOCK + "    review_ref: ''\n", "corsia.lesson.review_ref must be a non-empty string"),
    ],
)
def test_a_malformed_lesson_is_a_skill_error(block, error):
    result = validate_skill_text(_skill(block))

    assert not result.ok
    assert error in result.errors


def test_a_lesson_block_that_is_not_a_mapping_is_a_skill_error():
    result = validate_skill_text(
        "---\nname: sample\ndescription: Use when testing.\ncorsia:\n  lesson: trial\n---\n# Body\n"
    )

    assert result.errors == ["corsia.lesson must be a mapping"]


def test_a_lesson_over_its_word_bounds_is_a_skill_error():
    long_body = validate_skill_text(_skill(VALID_BLOCK, body="word " * 151))
    long_trigger = validate_skill_text(_skill(VALID_BLOCK, description="word " * 41))

    assert "a lesson body is at most 150 words; link the procedure instead" in long_body.errors
    assert "a lesson description is at most 40 words" in long_trigger.errors
    assert validate_skill_text(
        "---\nname: plain\ndescription: Use when testing.\n---\n" + "word " * 151
    ).ok


def test_the_trial_payload_is_checked_field_by_field():
    trial = lesson_trials.validate_trial({
        "kind": "hypothesis", "review_after": "2026-10-15", "derivation": "  From  the note. ",
        "literal_ref": {"entry_id": 4},
    })

    assert trial == Trial(
        kind="hypothesis", review_after="2026-10-15T00:00:00+00:00",
        derivation="From the note.", literal_ref={"entry_id": 4},
    )
    assert lesson_trials.validate_trial(None) is None
    assert lesson_trials.validate_trial({"kind": "instruction", "review_after": "2026-10-15"}).kind \
        in lesson_trials.PERMANENT_KINDS


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("hypothesis", "trial must be an object or null"),
        ({"kind": "hypothesis", "review_after": "2026-10-15", "why": "x"}, "unknown keys: why"),
        ({"kind": "guess", "review_after": "2026-10-15"}, "trial.kind must be one of"),
        ({"kind": "hypothesis"}, "trial.review_after must be an ISO date or instant"),
        ({"kind": "hypothesis", "review_after": "2026-10-15", "literal_ref": {"quote": "x"}},
         "trial.literal_ref names where the person's exact words are kept"),
        ({"kind": "hypothesis", "review_after": "2026-10-15",
          "literal_ref": {"entry_id": 2, "quote": "x"}}, "not both"),
    ],
)
def test_a_malformed_trial_payload_says_what_to_correct(raw, message):
    with pytest.raises(ValueError, match=message):
        lesson_trials.validate_trial(raw)


def test_only_the_trigger_and_body_change_the_behavioral_version():
    first = lesson_files.read_trial(_trial_text())
    renamed = lesson_files.read_trial(_trial_text(name="cite-head"))
    redated = lesson_files.read_trial(
        _trial_text(trial=Trial(kind="hypothesis", review_after=AFTER.replace("15", "16"),
                                boundary=TRIAL.boundary))
    )
    more_evidence = lesson_files.read_trial(
        _trial_text(source_refs=[*REFS, {"kind": "turn", "reference": "turn-2"}])
    )
    reworded = lesson_files.read_trial(_trial_text(text="Name the branch the check ran on."))
    retriggered = lesson_files.read_trial(_trial_text(description="Use when reporting a result."))

    assert renamed.behavioral_sha256 == first.behavioral_sha256
    assert redated.behavioral_sha256 == first.behavioral_sha256
    assert redated.lesson.review_after != first.lesson.review_after
    assert more_evidence.behavioral_sha256 == first.behavioral_sha256
    assert reworded.behavioral_sha256 != first.behavioral_sha256
    assert retriggered.behavioral_sha256 != first.behavioral_sha256


def test_frontmatter_only_changes_keep_the_behavioral_version():
    text = _trial_text()
    first = lesson_files.read_trial(text)
    activated = lesson_files.with_review_ref(text, "node-1")
    read = lesson_files.read_trial(activated)

    assert read.behavioral_sha256 == first.behavioral_sha256
    assert read.lesson.review_node_id == "node-1"
    assert lesson_files.raw_sha256(activated.encode()) != lesson_files.raw_sha256(text.encode())
    assert lesson_trials.behavioral_sha256("Use  when\nx.", "body \r\nline  \n\n") == \
        lesson_trials.behavioral_sha256("Use when x.", "body\nline")


def test_a_capture_hash_ignores_evidence_and_follows_the_hypothesis():
    held = Trial(kind="hypothesis", review_after=AFTER, derivation=None)
    derived = Trial(kind="hypothesis", review_after=AFTER, derivation="From the note.")
    later = Trial(kind="hypothesis", review_after=AFTER.replace("15", "20"), boundary="Never.")

    assert lesson_trials.capture_hash("Cite  the head.", held) == \
        lesson_trials.capture_hash("Cite the head.", later)
    assert lesson_trials.capture_hash("Cite the head.", held) != \
        lesson_trials.capture_hash("Cite the head.", derived)
    assert lesson_trials.capture_hash("Cite the head.", held) != \
        lesson_trials.capture_hash("Cite the branch.", held)


def test_one_continuity_entry_always_names_the_same_lesson():
    assert lesson_trials.lesson_id("thread", 0, 7) == lesson_trials.lesson_id("thread", 0, 7)
    assert lesson_trials.lesson_id("thread", 0, 7) != lesson_trials.lesson_id("thread", 1, 7)
    assert lesson_trials.lesson_id("thread", 0, 7) != lesson_trials.lesson_id("other", 0, 7)


def test_a_written_trial_is_not_offered_until_its_review_is_recorded_and_ahead():
    text = _trial_text()
    written = lesson_files.read_trial(text).lesson
    active = lesson_files.read_trial(lesson_files.with_review_ref(text, "node-1")).lesson

    assert validate_skill_text(text).ok
    assert "Does not apply: A one-line typo fix." in text
    assert "An unproven hypothesis under review, not a standing rule" in text
    assert lesson_trials.offer_refusal(written, NOW) == "trial has no scheduled review"
    assert lesson_trials.offer_refusal(active, NOW) is None
    assert lesson_trials.offer_refusal(active, NOW + timedelta(days=14)) == \
        "trial review date has passed"


@pytest.mark.parametrize("state", ["retired", "promoted"])
def test_a_settled_lesson_is_not_offered(state):
    block = VALID_BLOCK.replace("state: trial", f"state: {state}") + "    review_ref: plan-node:n\n"
    if state == "promoted":
        block += "    destination:\n      path: skills/x/SKILL.md\n      version: abc\n"
    parsed = validate_skill_text(_skill(block))

    assert parsed.ok, parsed.errors
    assert lesson_trials.offer_refusal(lesson_trials.parse_lesson(parsed.metadata), NOW) == \
        f"lesson is {state}"


def test_a_capture_file_records_its_identity_and_is_never_a_skill():
    held = Trial(
        kind="hypothesis", review_after=AFTER, literal_ref={"entry_id": 3},
        boundary="A one-line typo fix.",
    )
    text = lesson_files.render_capture(
        capture_id=LESSON, capture_hash="c" * 64, owner="owner", reason="missing-derivation",
        name="cite-the-head", description="Use when reporting a check result.",
        text="Name the exact commit the check ran on.", trial=held, source_refs=[*REFS, *REFS],
        originating_subjects=("message:corrected",),
    )
    capture = lesson_files.read_capture(text)

    assert capture == lesson_files.CaptureFile(
        id=LESSON, hash="c" * 64, source_refs=({"kind": "turn", "reference": "turn-1"},),
        originating_subjects=("message:corrected",),
    )
    assert "Not offered as guidance: it cites what a person said and gives no derivation" in text
    assert not validate_skill_text(text).ok
    assert lesson_files.read_trial(text) is None
    assert lesson_files.read_capture("# Just a note\n") is None


def test_a_watch_keeps_subject_anchors_and_is_never_offered():
    text = _trial_text(
        trial=Trial(kind="hypothesis", review_after=AFTER, state="watch"),
        originating_subjects=("message:first", "message:second", "message:first"),
    )
    watch = lesson_files.read_trial(lesson_files.with_review_ref(text, "node-1")).lesson

    assert watch.state == "watch"
    assert watch.originating_subjects == ("message:first", "message:second")
    assert lesson_trials.offer_refusal(watch, NOW) == "lesson is watch"
