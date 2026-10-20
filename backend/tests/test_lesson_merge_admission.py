from __future__ import annotations


from core.lesson_files import render_trial
from core.lesson_trials import Trial


def test_target_trial_metadata_round_trips_for_cross_file_discovery():
    text = render_trial(
        lesson_id="00000000-0000-0000-0000-000000000001", owner="owner", name="trial",
        description="Use for this test.", text="Take the right call.",
        trial=Trial("hypothesis", "2027-01-01", boundary="Never for drafts."),
        source_refs=[{"kind": "turn", "reference": "turn"}], target_file="skills/target/SKILL.md",
        target_section="Checks", cause="process_gap",
        case={"what_happened": "Missing check.", "right_call": "Check it.", "does_not_apply": "Drafts.", "correction_subject": "message:1"},
    )
    from core.lesson_files import read_trial

    trial = read_trial(text)
    assert trial is not None
    assert (trial.lesson.target_file, trial.lesson.target_section, trial.lesson.cause) == (
        "skills/target/SKILL.md", "Checks", "process_gap",
    )
