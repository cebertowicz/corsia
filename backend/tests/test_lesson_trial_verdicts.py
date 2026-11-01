"""Model-free lesson trial verdicts: the arms, the plan, the owner and the nightly write path."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from loguru import logger

from core import lesson_files, workflow_verdict_evaluator as evaluator
from core import lesson_trial_verdicts as lessons
from core.lesson_trials import Trial
from core.workflow_verdict_rules import Definition, FrozenPlan, TwoArms, decide_arms
from core.workflow_verdicts import COST, Run
from infra.outcome_events import record_outcome_event
from tests.plan_writer_authorizer import raw_plan_seed

NOW = datetime(2026, 10, 30, tzinfo=timezone.utc)
LESSON = "00000000-0000-0000-0000-000000000001"
ENDED = "2026-10-05T00:00:00Z"


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir()
    monkeypatch.setattr("infra.config.WORKSPACE", root)
    return root


@pytest.fixture
def db(isolated_db, workspace):
    from infra.database import get_db

    connection = get_db()
    connection.execute("INSERT INTO threads (id,user_id,title,agent_enabled,agent_status,created_at,updated_at) "
                       "VALUES ('root','steve','root',1,'active','t','t')")
    connection.execute("INSERT INTO plans (id,owner_kind,owner_id,state,revision,created_at,updated_at) "
                       "VALUES ('plan','thread','root','active',1,'t','t')")
    try:
        yield connection
    finally:
        connection.close()


def write_trial(path: Path, *, text: str = "Check the diff before closing.", target: str = ".threads/root/context/x.md",
                state: str = "trial", lesson_id: str = LESSON) -> lessons.OpenTrial:
    """A valid trial file at ``path`` and the open trial it makes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(lesson_files.render_trial(
        lesson_id=lesson_id, owner="root", name="check-diff", description="Use when closing a task.", text=text,
        trial=Trial(kind="hypothesis", review_after="2030-01-01T00:00:00+00:00", state=state),
        source_refs=[{"kind": "turn", "reference": "turn-1"}], case_metadata={"new_target_path": target}),
        encoding="utf-8")
    trial = lesson_files.read_trial(path.read_text(encoding="utf-8"))
    assert trial is not None
    return lessons.OpenTrial(lesson_id, trial.behavioral_sha256, path, "2026-10-01T00:00:00Z", trial)


def task(db, node_id: str, *, state: str = "accepted", sent_back: bool = False, step: str | None = None,
         ended: str = ENDED) -> None:
    """A task row; a sent-back round is a closed task carrying the row fact."""
    state = "closed" if sent_back else state
    with raw_plan_seed(db):
        db.execute("INSERT INTO plan_nodes (id,plan_id,order_key,kind,title,acceptance_criteria,state,commitment_id,"
                   "workflow_step,sent_back,requirements_revision,revision,created_at,updated_at) "
                   "VALUES (?,'plan',1,'task','t','done',?,?,?,?,1,1,'2026-10-01T00:00:00Z',?)",
                   (node_id, state, node_id, step, int(sent_back), ended))
        if state in {"accepted", "failed", "closed"}:
            db.execute("INSERT INTO plan_node_events (id,node_id,provenance,event,prior_state,new_state,created_at) "
                       "VALUES (?,?,'test',?,'active',?,?)",
                       (f"end-{node_id}", node_id, "review_rejected" if sent_back else state, state, ended))


def receipt(db, open_trial: lessons.OpenTrial, node_id: str | None, variant: str | None, *, at: str = "2026-10-02T00:00:00Z",
            omission: str | None = None) -> None:
    db.execute("INSERT INTO guidance_loads (id,thread_id,path,raw_sha256,git_blob_oid,delivered_sha256,byte_count,"
               "partial,created_at,node_id,variant,lesson_id,lesson_behavioral_sha256,omission_reason) "
               "VALUES (?,?,?,'r','g','d',1,0,?,?,?,?,?,?)",
               (f"load-{node_id}-{variant}-{at}", f"thread-{node_id}", str(open_trial.path), at, node_id, variant,
                open_trial.lesson_id, open_trial.sha, omission))


def read_of_file(db, open_trial: lessons.OpenTrial, node_id: str, *, at: str, thread: str | None = None) -> None:
    """A mid-task read of the trial's own file by a thread (no lesson identity on the row)."""
    db.execute("INSERT INTO guidance_loads (id,thread_id,path,raw_sha256,git_blob_oid,delivered_sha256,byte_count,"
               "partial,created_at) VALUES (?,?,?,'r','g','d',1,0,?)",
               (f"read-{node_id}", thread or f"thread-{node_id}", str(open_trial.path), at))


def deliver(db, open_trial: lessons.OpenTrial, prefix: str, variant: str, count: int, *,
            flagged: int = 0, **options) -> list[str]:
    """``count`` finished tasks in one half, the first ``flagged`` of them sent back."""
    ids = []
    for index in range(count):
        node_id = f"{prefix}-{index}"
        task(db, node_id, sent_back=index < flagged, **options)
        receipt(db, open_trial, node_id, variant)
        ids.append(node_id)
    return ids


def trial_at(workspace: Path, db, **options) -> lessons.OpenTrial:
    return write_trial(workspace / ".threads" / "root" / "skills" / "check-diff" / "SKILL.md", **options)


# --- the arms -------------------------------------------------------------------------------------------------

def test_open_trials_are_delivered_versions_whose_file_is_still_that_trial(db, workspace) -> None:
    trial = trial_at(workspace, db)
    assert lessons.open_trials(db) == []
    receipt(db, trial, "n1", "with")
    assert [item.subject for item in lessons.open_trials(db)] == [f"lesson:{LESSON}@{trial.sha}"]
    write_trial(trial.path, text="Check the diff and the tests before closing.")
    assert lessons.open_trials(db) == []  # the file moved on to another version
    write_trial(trial.path, state="retired")
    assert lessons.open_trials(db) == []
    write_trial(trial.path)
    record_outcome_event(db, dedupe_key="verdict:reached:s", event_name=evaluator.REACHED_EVENT, source="corsia",
                         provenance="instrumented", ts="2026-10-05T00:00:00Z", subject_key=trial.subject,
                         attributes={"verdict": "keep"})
    assert lessons.open_trials(db) == []  # a reached verdict closes it


def test_a_stale_pair_does_not_hold_a_place_in_the_batch(db, workspace) -> None:
    live = trial_at(workspace, db)
    stale = lessons.OpenTrial("00000000-0000-0000-0000-000000000002", "f" * 64, workspace / "gone" / "SKILL.md",
                              "2026-09-01T00:00:00Z", live.trial)
    receipt(db, stale, "n0", "with", at="2026-09-01T00:00:00Z")  # older than the live pair, and its file is gone
    receipt(db, live, "n1", "with", at="2026-10-02T00:00:00Z")
    messages: list[str] = []
    sink = logger.add(lambda message: messages.append(str(message)), level="WARNING")
    try:
        assert [item.subject for item in lessons.open_trials(db, limit=1)] == [live.subject]
    finally:
        logger.remove(sink)
    assert len(messages) == 1
    assert stale.lesson_id in messages[0] and stale.sha in messages[0] and str(stale.path) in messages[0]
    assert [item.subject for item in lessons.open_trials(db)] == [live.subject]


def test_a_look_takes_the_earliest_settled_tasks_whatever_their_ids(db, workspace) -> None:
    trial = trial_at(workspace, db)
    for node_id, ended in (("a-late", "2026-10-09T00:00:00Z"), ("z-early", "2026-10-02T00:00:00Z"),
                           ("m-mid", "2026-10-05T00:00:00Z")):
        task(db, node_id, ended=ended)
        receipt(db, trial, node_id, "with")
    task(db, "b-open", state="active")
    receipt(db, trial, "b-open", "with")
    task(db, "same-b", ended="2026-10-05T00:00:00Z")
    receipt(db, trial, "same-b", "with")
    arms = lessons.read_arms(db, trial)
    assert [run.run_id for run in arms.with_runs] == ["z-early", "m-mid", "same-b", "a-late", "b-open"]


def test_each_task_is_one_run_in_its_half_and_its_workflow_step(db, workspace) -> None:
    trial = trial_at(workspace, db)
    deliver(db, trial, "w", "with", 2, flagged=1, step="review")
    deliver(db, trial, "o", "without", 1, step=None)
    arms = lessons.read_arms(db, trial)
    assert [(run.version, run.group) for run in arms.with_runs] == [("with", ("review", "none", "none"))] * 2
    assert [(run.version, run.group) for run in arms.without_runs] == [("without", ("none", "none", "none"))]
    sent_back = {run.run_id: run.kpi_rows[lessons.SENT_BACK][0].value for run in arms.with_runs}
    assert sent_back == {"w-0": 1.0, "w-1": 0.0}


def test_crossed_over_omitted_and_opened_without_tasks_belong_to_neither_half(db, workspace) -> None:
    trial = trial_at(workspace, db)
    deliver(db, trial, "w", "with", 1)
    deliver(db, trial, "o", "without", 1)
    task(db, "x")
    receipt(db, trial, "x", "crossover")
    task(db, "skip")
    receipt(db, trial, "skip", None, omission="count_cap")
    deliver(db, trial, "peek", "without", 1)
    read_of_file(db, trial, "peek-0", at="2026-10-03T00:00:00Z")
    deliver(db, trial, "late", "without", 1)
    read_of_file(db, trial, "late-0", at="2026-10-09T00:00:00Z")  # after the task ended: it did not affect it
    arms = lessons.read_arms(db, trial)
    assert [run.run_id for run in arms.with_runs] == ["w-0"]
    assert sorted(run.run_id for run in arms.without_runs) == ["late-0", "o-0"]
    assert (arms.crossed_over, arms.omitted) == (2, 1)


def test_a_worker_the_task_was_handed_to_that_opens_the_lesson_crosses_it_over(db, workspace) -> None:
    trial = trial_at(workspace, db)
    deliver(db, trial, "w", "with", 1)
    deliver(db, trial, "held", "without", 1)  # the receipt is the parent's; the task then went to a worker
    deliver(db, trial, "kept", "without", 1)  # a worker that never opens the lesson leaves it in the half
    for worker, node_id in (("worker-held", "held-0"), ("worker-kept", "kept-0")):
        db.execute("INSERT INTO threads (id,user_id,title,created_at,updated_at) VALUES (?,'steve','w','t','t')",
                   (worker,))
        with raw_plan_seed(db):
            db.execute("UPDATE plan_nodes SET spawned_thread_id=? WHERE id=?", (worker, node_id))
    read_of_file(db, trial, "held-0", at="2026-10-03T00:00:00Z", thread="worker-held")
    arms = lessons.read_arms(db, trial)
    assert [run.run_id for run in arms.without_runs] == ["kept-0"]
    assert arms.crossed_over == 1


def test_an_open_task_is_a_run_without_facts_and_a_restart_is_a_failed_or_changed_attempt(db, workspace) -> None:
    trial = trial_at(workspace, db)
    deliver(db, trial, "w", "with", 2)
    deliver(db, trial, "open", "with", 1, state="active")
    db.execute("INSERT INTO threads (id,user_id,title,created_at,updated_at) VALUES ('ex','steve','x','t','t')")
    with raw_plan_seed(db):
        for ordinal, (node, reason) in enumerate([("w-0", "requirements_changed"), ("w-1", "released")], 1):
            db.execute("INSERT INTO plan_node_attempts (id,plan_id,node_id,executor_thread_id,requirements_revision,"
                       "attempt_ordinal,provenance,selected_at,released_at,release_reason) VALUES "
                       "(?,'plan',?,'ex',1,?,'test','2026-10-02 00:00:00','2026-10-03 00:00:00',?)",
                       (f"a{ordinal}", node, ordinal, reason))
    arms = lessons.read_arms(db, trial)
    by_id = {run.run_id: run for run in arms.with_runs}
    assert [by_id[name].kpi_rows[lessons.RESTARTED][0].value for name in ("w-0", "w-1")] == [1.0, 0.0]
    assert by_id["open-0"].kpi_rows == {} and by_id["open-0"].terminal_at is None


# --- the verdict -------------------------------------------------------------------------------------------------

def evaluate(db):
    (evaluation,) = lessons.evaluate_trials(db, NOW)
    return evaluation


def test_a_trial_collects_until_each_half_has_twenty_tasks_then_judges(db, workspace) -> None:
    trial = trial_at(workspace, db)
    deliver(db, trial, "w", "with", 19)
    deliver(db, trial, "o", "without", 19, flagged=19)
    assert evaluate(db).verdict.word == "collecting"
    task(db, "w-19")
    receipt(db, trial, "w-19", "with")
    task(db, "o-19", sent_back=True)
    receipt(db, trial, "o-19", "without")
    keep = evaluate(db)
    assert (keep.verdict.word, keep.verdict.reason, keep.verdict.look) == ("keep", "quality_better", 1)
    assert keep.plan_is_new and keep.plan.subject == trial.subject
    assert {key: keep.verdict.inputs[key] for key in ("with", "without", "crossed_over", "omitted")} == {
        "with": 20, "without": 20, "crossed_over": 0, "omitted": 0}


def test_equal_halves_wait_for_the_next_look_then_revert_after_the_third(db, workspace) -> None:
    trial = trial_at(workspace, db)
    for added, expected in ((20, "collecting"), (20, "collecting"), (40, "revert")):
        batch = f"b{len(db.execute('SELECT id FROM plan_nodes').fetchall())}"
        deliver(db, trial, f"{batch}w", "with", added, flagged=added // 10)
        deliver(db, trial, f"{batch}o", "without", added, flagged=added // 10)
        assert evaluate(db).verdict.word == expected
    final = evaluate(db).verdict
    assert (final.reason, final.look) == ("unclear_after_look_3", 3)


def test_the_plan_is_frozen_once_and_a_later_evaluation_reuses_it(db, workspace, monkeypatch) -> None:
    monkeypatch.setattr(evaluator, "VERDICT_WRITES_ENABLED", True)
    trial = trial_at(workspace, db)
    deliver(db, trial, "w", "with", 20)
    deliver(db, trial, "o", "without", 20, flagged=20)
    first = evaluate(db)
    assert evaluator.write_verdicts(db, [first]) == 1
    names = {row["event_name"] for row in db.execute("SELECT event_name FROM outcome_events").fetchall()}
    assert names == {evaluator.PLANNED_EVENT, evaluator.REACHED_EVENT}
    assert lessons.evaluate_trials(db, NOW) == []  # reached: no longer open
    assert FrozenPlan.decoded(first.plan.encoded()) == first.plan


# --- the deciding measures: look 2 and 3 (synthetic runs, the same rule) ---------------------------------------

def synthetic(version: str, flags: list[int]) -> tuple[Run, ...]:
    runs = []
    for index, flag in enumerate(flags):
        end = (datetime(2026, 10, 1, tzinfo=timezone.utc) + timedelta(hours=index)).isoformat()
        runs.append(Run(f"{version}{index}", version, ("s", "none", "none"), end, end, "accepted",
                        kpi_rows=lessons._kpi_rows(bool(flag), False, end)))
    return tuple(runs)


def rate(count: int, per_ten: int) -> list[int]:
    return [int(index % 10 < per_ten) for index in range(count)]


def plan_of(db, workspace) -> FrozenPlan:
    return lessons.freeze_plan(trial_at(workspace, db), (), None, (), NOW)


def judge(plan: FrozenPlan, without: list[int], with_: list[int]):
    return decide_arms(plan, TwoArms(synthetic("without", without), synthetic("with", with_), matched=True), NOW)


def test_look_two_decides_when_forty_per_half_separate_what_twenty_did_not(db, workspace) -> None:
    plan = plan_of(db, workspace)
    without, with_ = rate(20, 0) + rate(20, 5), rate(20, 1) + rate(20, 0)
    assert judge(plan, without[:20], with_[:20]).word == "collecting"
    second = judge(plan, without, with_)
    assert (second.word, second.reason, second.look) == ("keep", "quality_better", 2)
    worse = judge(plan, rate(40, 0), rate(20, 1) + rate(20, 1))
    assert (worse.word, worse.reason, worse.look) == ("revert", "quality_worse", 2)


def test_look_three_is_the_last_and_a_tie_reverts(db, workspace) -> None:
    plan = plan_of(db, workspace)
    verdict = judge(plan, rate(80, 3), rate(80, 3))
    assert (verdict.word, verdict.reason, verdict.look) == ("revert", "unclear_after_look_3", 3)
    assert judge(plan, rate(80, 3), rate(80, 3)[:79]).word == "collecting"  # one task short of 80 per half


def test_a_restart_margin_is_a_quarter_of_the_restarted_share_not_of_the_share_without_one(db, workspace) -> None:
    def restarts(version: str, flags: list[int]) -> tuple[Run, ...]:
        end = "2026-10-01T00:00:00+00:00"
        return tuple(Run(f"{version}{index}", version, ("s", "none", "none"), end, end, "accepted",
                         kpi_rows=lessons._kpi_rows(False, bool(flag), end)) for index, flag in enumerate(flags))

    plan = plan_of(db, workspace)
    # 90% finished without a restart falls to 80%: within a quarter of 0.9, but four times a quarter of 0.1.
    verdict = decide_arms(plan, TwoArms(restarts("without", rate(80, 1)), restarts("with", rate(80, 2)),
                                        matched=True), NOW)
    call = next(call for call in verdict.calls if call.name == lessons.RESTARTED)
    assert call.margin == pytest.approx(0.025)
    assert call.call == "worse"


def test_a_trials_minimum_detectable_change_is_none_until_its_without_half_has_two_runs(db, workspace) -> None:
    trial = trial_at(workspace, db)
    empty = lessons.freeze_plan(trial, (), None, (), NOW)
    assert empty.mdc[lessons.SENT_BACK] == (None, None, None) and empty.mdc[COST] == (None, None, None)
    started = lessons.freeze_plan(trial, (), None, synthetic("without", rate(20, 3)), NOW)
    assert all(figure is not None and figure > 0 for figure in started.mdc[lessons.SENT_BACK])


def test_a_declared_spend_kpi_alone_leaves_sent_back_and_restart_deciding(db, workspace) -> None:
    trial = trial_at(workspace, db)
    events = Definition("event", "qg.escape", 1, {"attributes": {"pr": "id"}, "dedupe": ["pr"]},
                        "2026-09-01T00:00:00Z", False, "/workflows/w/measures.yaml")

    def kpi(name: str, decides_as: str) -> Definition:
        spec = {"formula": "ratio", "num": {"event": "qg.escape"}, "den": {"event": "qg.escape"}, "min_n": 1, "better": "lower", "can_decide": True,
                "coverage": {"of": "qg.escape", "by": "audit"}, "target": 0.1, "review": 0.2,
                "decides_as": decides_as}
        return Definition("kpi", name, 1, spec, "2026-09-01T00:00:00Z", False, "/workflows/w/measures.yaml")

    spend = lessons.freeze_plan(trial, [events, kpi("hours", "spend")], "w", (), NOW)
    roles = {measure.name: measure.role for measure in spend.measures}
    assert roles["hours"] == "spend" and roles[lessons.SENT_BACK] == roles[lessons.RESTARTED] == "quality"
    quality = lessons.freeze_plan(trial, [events, kpi("escapes", "quality")], "w", (), NOW)
    roles = {measure.name: measure.role for measure in quality.measures}
    assert roles["escapes"] == "quality" and roles[lessons.SENT_BACK] == roles[lessons.RESTARTED] == "shown"


# --- the owner -------------------------------------------------------------------------------------------------

def test_a_file_in_a_thread_tray_belongs_to_that_thread(db, workspace) -> None:
    trial = write_trial(workspace / "skills" / "check-diff" / "SKILL.md", target=".threads/abc/context/GUIDE.md")
    assert lessons.trial_owner(db, trial) == "abc"


def test_a_project_file_goes_to_its_conductor_and_other_files_to_the_corsia_agent(db, workspace, monkeypatch) -> None:
    scopes = []
    def coordinator(scope: str, db=None) -> str:
        scopes.append(scope)
        return "corsia-agent" if scope == lessons.WORKSPACE_SCOPE else "conductor"

    monkeypatch.setattr(lessons, "get_scope_coordinator", coordinator)
    project = write_trial(workspace / "skills" / "check-diff" / "SKILL.md", target="projects/demo/context/X.md")
    monkeypatch.setattr(lessons, "project_id_for_root", lambda root, workspace, _db: "demo")
    assert lessons.trial_owner(db, project) == "conductor"
    monkeypatch.setattr(lessons, "project_id_for_root", lambda root, workspace, _db: None)
    assert lessons.trial_owner(db, project) == "corsia-agent"
    assert scopes == ["demo", lessons.WORKSPACE_SCOPE]


def test_a_target_leaving_the_workspace_is_not_resolved_to_a_foreign_tray(db, workspace, tmp_path, monkeypatch) -> None:
    """The owner woken is the one for the file the trial is bound to, so a target that escapes the workspace
    (by ``..`` or through a symlink) falls back to the trial's own file."""
    monkeypatch.setattr(lessons, "get_scope_coordinator", lambda scope, db=None: "corsia-agent")
    monkeypatch.setattr(lessons, "project_id_for_root", lambda root, workspace, _db: None)
    outside = tmp_path / "outside" / ".threads" / "intruder" / "context"
    outside.mkdir(parents=True)
    dotted = write_trial(workspace / "skills" / "check-diff" / "SKILL.md",
                         target="../outside/.threads/intruder/context/GUIDE.md")
    assert lessons.trial_owner(db, dotted) == "corsia-agent"
    (workspace / "link").symlink_to(outside.parent.parent)
    linked = write_trial(workspace / "skills" / "check-diff" / "SKILL.md",
                         target="link/.threads/intruder/context/GUIDE.md")
    assert lessons.trial_owner(db, linked) == "corsia-agent"
    unresolvable = write_trial(workspace / "skills" / "check-diff" / "SKILL.md", target="bad\0name.md")
    assert lessons.trial_owner(db, unresolvable) == "corsia-agent"


# --- the nightly job ---------------------------------------------------------------------------------------------

def seed_finished_trial(db, workspace) -> lessons.OpenTrial:
    trial = trial_at(workspace, db)
    deliver(db, trial, "w", "with", 20)
    deliver(db, trial, "o", "without", 20, flagged=20)
    return trial


def test_the_job_records_nothing_while_writes_are_switched_off(db, workspace, monkeypatch) -> None:
    from jobs import workflow_verdicts

    seed_finished_trial(db, workspace)
    db.commit()
    monkeypatch.setattr(evaluator, "VERDICT_WRITES_ENABLED", False)
    report = workflow_verdicts.run_daily(now=NOW)
    assert (report.evaluated, report.reached, report.recorded, report.by_word) == (1, 1, 0, {"keep": 1})
    assert db.execute("SELECT COUNT(*) FROM outcome_events").fetchone()[0] == 0


def test_the_job_records_the_plan_and_verdict_once_and_wakes_the_owner_when_switched_on(db, workspace, monkeypatch) -> None:
    from events import delivery
    from jobs import workflow_verdicts

    trial = seed_finished_trial(db, workspace)
    db.commit()
    wakes = []

    def wake(*args, message_id, db):
        # Accepted the way the real wake is: its message row exists afterwards, so the next run doesn't retry it.
        wakes.append(args)
        db.execute("INSERT INTO messages (id,thread_id,role,content,created_at) VALUES (?,?,'system','',?)",
                   (message_id, args[0], NOW.isoformat()))

    monkeypatch.setattr(evaluator, "VERDICT_WRITES_ENABLED", True)
    monkeypatch.setattr(delivery, "enqueue_event_wake", wake)
    assert workflow_verdicts.run_daily(now=NOW).recorded == 1
    assert workflow_verdicts.run_daily(now=NOW).recorded == 0
    rows = db.execute("SELECT event_name,subject_key FROM outcome_events ORDER BY event_name").fetchall()
    assert [(row["event_name"], row["subject_key"]) for row in rows] == [
        (evaluator.PLANNED_EVENT, trial.subject), (evaluator.REACHED_EVENT, trial.subject)]
    lesson, _, version = trial.subject.partition(":")[2].partition("@")
    assert len(wakes) == 1 and wakes[0][0] == "root" and wakes[0][2] == (
        f"Lesson {lesson} ({version[:7]}) verdict reached: keep the change.")


def test_an_unavailable_owner_keeps_the_verdict_and_skips_the_wake(db, workspace, monkeypatch) -> None:
    from events import delivery
    from jobs import workflow_verdicts

    seed_finished_trial(db, workspace)
    db.commit()

    def refuse(*_args, **_kwargs):
        raise ValueError("unavailable")

    monkeypatch.setattr(evaluator, "VERDICT_WRITES_ENABLED", True)
    monkeypatch.setattr(delivery, "enqueue_event_wake", refuse)
    assert workflow_verdicts.run_daily(now=NOW).recorded == 1
    assert db.execute("SELECT COUNT(*) FROM outcome_events WHERE event_name=?", (evaluator.REACHED_EVENT,)).fetchone()[0] == 1


def test_the_job_line_reports_the_batch_by_kind_with_halves_send_backs_and_delegated_receipts(db, workspace) -> None:
    from jobs import workflow_verdicts
    from tests.test_workflow_verdict_read import node

    trial = seed_finished_trial(db, workspace)
    # A send-back's revision is its own task with its own receipt, drawn into the other arm; the sent-back task
    # also drew a second receipt of its own.
    task(db, "rev")
    receipt(db, trial, "rev", "with")
    receipt(db, trial, "o-0", "without", at="2026-10-02T00:00:01Z")
    # Tasks handed to a delegated worker: a step with a receipt, a step without one, and a plain task without one.
    for node_id, step in (("d-step-seen", "review"), ("d-step-missed", "review"), ("d-plain", None)):
        task(db, node_id, step=step, sent_back=node_id == "d-step-seen")
    receipt(db, trial, "d-step-seen", "without")
    with raw_plan_seed(db):
        db.execute("UPDATE plan_nodes SET follow_up_of='o-0' WHERE id='rev'")
        db.execute("UPDATE plan_nodes SET spawned_thread_id='root', created_at='2026-10-03T00:00:00Z' "
                   "WHERE id LIKE 'd-%'")
    node(db, "wf-run")
    task(db, "wf-sent-back", sent_back=True)
    with raw_plan_seed(db):
        db.execute("UPDATE plan_nodes SET workflow_ref='sdlc-process', workflow_version='v1' WHERE id='wf-sent-back'")
    db.commit()
    lines: list[str] = []
    sink = logger.add(lambda message: lines.append(str(message)), level="INFO")
    try:
        report = workflow_verdicts.run_daily(now=NOW)
    finally:
        logger.remove(sink)

    detail = report.detail
    assert (report.open_subjects, report.evaluated, report.reached) == (2, 2, 1)
    assert detail["by_kind"] == {
        "workflow": {"open": 1, "evaluated": 1, "by_word": {"not_enough_data": 1}},
        "lesson": {"open": 1, "evaluated": 1, "by_word": {"keep": 1}}}
    (reached,) = detail["reached_detail"]
    assert reached == {"kind": "lesson", "word": "keep", "reason": "quality_better", "look": 1, "new": 20, "base": 20}
    # with: 20 + the revision; without: 20 + the delegated step's receipt + the sent-back task's second receipt,
    # which the arms count as a second run.
    assert detail["lesson_halves"] == [[21, 22]] and detail["lesson_both_20"] == 1
    assert detail["sent_back"] == {
        "lesson_arms": {"receipt_rows": 43, "on_sent_back_nodes": 22, "nodes_with_many": 1, "revision_receipts": 1,
                        "revision_after_source_receipt": 1, "revision_in_other_arm": 1},
        "workflow_runs": {"settled": 2, "root_sent_back": 1, "continued_by_revision": 0}}
    assert detail["delegated_receipts"] == {
        "step": {"with_receipt": 1, "without_receipt": 1, "by_variant": {"without": 1}},
        "other": {"with_receipt": 0, "without_receipt": 1, "by_variant": {}}}
    assert set(detail["phase_seconds"]) == {  # type: ignore[call-overload]
        "workflow_read", "workflow_pricing", "lesson_open_trials", "lesson_setup", "lesson_read_arms",
        "lesson_pricing", "decide", "detail"}
    (line,) = [text for text in lines if "workflow_verdict_daily:" in text]
    for field_name in ("phase_seconds=", "by_kind=", "reached_detail=", "lesson_halves=", "lesson_both_20=1",
                       "sent_back=", "delegated_receipts="):
        assert field_name in line
    assert LESSON not in line and trial.subject not in line and "wf-run" not in line
