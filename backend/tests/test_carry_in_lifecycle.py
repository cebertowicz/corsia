"""C1 carry-in lifecycle: committed save -> pending fact -> bounded drain -> terminal fact."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from infra.platform_paths import WorkspaceSelection
from tests.registry_helpers import make_session
from tools.base import Tool

_IDENTITY = ["-c", "user.email=t@t", "-c", "user.name=T"]
TARGET = "context/demo.md"
LESSON_ID = "6f1c2a9e-3b7d-4c2e-9a1f-0d5e8b7c4a21"
ACTIONS = {
    "required_actions": [
        {"tool": "read_file", "params": {"path": TARGET}},
        {"tool": "plan_update", "params": {"operation": "mark_done"}},
    ],
    "forbidden_actions": [{"tool": "write_file", "params": {"path": TARGET}}],
    "decisive_action": {"tool": "plan_update", "params": {"operation": "mark_done"}},
}


def _git(args: list[str], *, cwd: Path) -> str:
    done = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    return done.stdout


@pytest.fixture
def c1(isolated_db, tmp_path, monkeypatch):
    """A git workspace, an owning thread with one trial lesson aimed at ``TARGET``."""
    from infra import carry_in_drain, config
    from infra.database import get_db

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _git(["init", "--quiet"], cwd=workspace)
    _git([*_IDENTITY, "commit", "--allow-empty", "-qm", "root"], cwd=workspace)
    monkeypatch.setattr(config, "WORKSPACE", workspace)
    monkeypatch.setattr(config, "WORKSPACE_SELECTION", WorkspaceSelection("c1-tests", workspace.resolve(), "Tests"))
    db = get_db()
    owner = make_session(db, title="Owner", prefix="own")
    db.execute(
        "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
        ("workspace_context", json.dumps({"bootloader_files": [TARGET]}), "now"),
    )
    db.execute(
        """INSERT INTO artifact_identities
           (id,integration,type,external_id,title,state,created_at,updated_at)
           VALUES ('artifact','workspace','skill','workspace://artifact','artifact','active','t','t')"""
    )
    db.commit()
    db.close()
    drains: list[Path | None] = []
    request_drain = carry_in_drain.request_drain
    monkeypatch.setattr(carry_in_drain, "request_drain", lambda workspace=None: drains.append(workspace))
    return SimpleNamespace(workspace=workspace, owner=owner, drains=drains, request_drain=request_drain)


def _lesson_path(c1, name="demo-lesson", root: Path | None = None) -> Path:
    return (root or c1.workspace) / ".threads" / c1.owner / "skills" / name / "SKILL.md"


def _write_lesson(
    c1, *, actions=ACTIONS, text="Read the rule before closing.", state="trial",
    name="demo-lesson", lesson_id=LESSON_ID, target=TARGET, commit=True, offered=True,
    root: Path | None = None,
) -> Path:
    """Write one trial lesson; ``offered`` gives its version the review the index requires."""
    from core.lesson_files import read_trial, render_trial, with_review_ref
    from core.lesson_trials import Trial

    case = {"what_happened": "A node closed unread.", "right_call": "Read first.", "does_not_apply": "No rule."}
    if actions is not None:
        case["actions"] = actions
    path = _lesson_path(c1, name, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = render_trial(
        lesson_id=lesson_id, owner=c1.owner, name=name, description="Use when closing a node.",
        text=text, trial=Trial(kind="hypothesis", review_after="2099-01-01", state=state),
        source_refs=[{"kind": "message", "reference": "m1"}],
        case_metadata={"target_file": target, "case": case},
    )
    if offered and state == "trial":
        from infra.database import get_db
        from plans.lesson_reviews import ensure_lesson_review

        trial = read_trial(body)
        db = get_db()
        try:
            node_id, _created = ensure_lesson_review(
                db, c1.owner, lesson_id=lesson_id, behavioral_sha256=trial.behavioral_sha256,
                artifact_id="artifact", title=f"Review trial lesson: {name}",
                acceptance="Reviewed.", review_after=trial.lesson.review_after,
            )
            db.commit()
        finally:
            db.close()
        body = with_review_ref(body, node_id)
    path.write_text(body)
    if commit:
        _git(["add", "-A"], cwd=c1.workspace)
        _git([*_IDENTITY, "commit", "-qm", "lesson"], cwd=c1.workspace)
    return path


def _write_lessons(c1, count: int) -> None:
    import uuid

    for index in range(count):
        _write_lesson(c1, name=f"lesson-{index}", lesson_id=str(uuid.uuid4()), commit=False)
    _git(["add", "-A"], cwd=c1.workspace)
    _git([*_IDENTITY, "commit", "-qm", "lessons"], cwd=c1.workspace)


def _save(c1, text: str):
    from infra.guidance_git import save_guidance
    from infra.guidance_targets import guidance_target

    path = c1.workspace / TARGET
    target = guidance_target(path, c1.workspace)
    assert target is not None
    return save_guidance({path: text.encode()}, workspace=c1.workspace, target=target,
                         author="Tester <t@agents.corsia.local>")


def _events(name: str | None = None) -> list[dict]:
    from infra.database import get_db

    db = get_db()
    rows = db.execute(
        "SELECT event_name, dedupe_key, attributes FROM outcome_events "
        "WHERE event_name LIKE 'lesson.carry_in.%' AND (? IS NULL OR event_name=?) ORDER BY observed_ts, id",
        (name, name),
    ).fetchall()
    return [{"event": r[0], "key": r[1], **json.loads(r[2])} for r in rows]


def _pending_fact():
    from core.carry_in_facts import CarryInFact

    pending = _events("lesson.carry_in.pending")
    assert len(pending) == 1
    return CarryInFact.from_attributes(pending[0])


# --- committed-save recorder ------------------------------------------------


def _saved() -> list[dict]:
    return _events("lesson.carry_in.saved")


@pytest.mark.asyncio
async def test_a_written_commit_records_only_a_saved_fact_without_reading_lessons(c1, monkeypatch):
    from core import carry_in_facts
    from infra.carry_in_drain import wake

    _write_lesson(c1)
    read_lesson = carry_in_facts._read_lesson
    monkeypatch.setattr(carry_in_facts, "_read_lesson", lambda _path: pytest.fail("save read a lesson"))
    result = _save(c1, "rule v1\n")

    assert result.outcome == "written"
    assert [(event["commit"], event["target_path"]) for event in _saved()] == [(result.sha, TARGET)]
    assert _events("lesson.carry_in.pending") == []
    assert c1.drains == [c1.workspace]

    monkeypatch.setattr(carry_in_facts, "_read_lesson", read_lesson)
    assert await wake(c1.workspace) is True
    fact = _pending_fact()
    assert (fact.lesson_id, fact.target_path, fact.target_commit) == (LESSON_ID, TARGET, result.sha)
    assert "what_happened" not in json.dumps(_events())


def test_no_git_commit_means_no_saved_fact(c1, monkeypatch):
    from infra import guidance_git

    _write_lesson(c1)
    _save(c1, "rule v1\n")
    assert len(_saved()) == 1
    assert _save(c1, "rule v1\n").outcome == "unchanged"
    monkeypatch.setattr(guidance_git, "workspace_repository", lambda *_a, **_k: (None, "off"))
    assert _save(c1, "rule v2\n").outcome == "history-off"
    assert len(_saved()) == 1 and len(c1.drains) == 1


def test_an_unsynced_index_commit_still_records_its_saved_target(c1, monkeypatch):
    from infra import guidance_git

    _write_lesson(c1)
    real = guidance_git._git

    def failing(repo, *args, env=None, **kwargs):
        if args[0] == "update-index" and env is None:
            raise guidance_git.GitFault("index.lock exists")
        return real(repo, *args, env=env, **kwargs)

    monkeypatch.setattr(guidance_git, "_git", failing)
    result = _save(c1, "rule v1\n")
    assert (result.outcome, result.reason) == ("written", "index-unsynced")
    assert _saved()[0]["commit"] == result.sha


def test_a_retried_saved_commit_is_idempotent_and_a_later_commit_is_distinct(c1):
    from core.carry_in_facts import record_committed_save

    _write_lesson(c1)
    first = _save(c1, "rule v1\n")
    record_committed_save(c1.workspace, [c1.workspace / TARGET], first.sha)
    assert len(_saved()) == 1

    second = _save(c1, "rule v2\n")
    assert [event["commit"] for event in _saved()] == [first.sha, second.sha]
    assert _saved()[0]["identity"] != _saved()[1]["identity"]


@pytest.mark.asyncio
async def test_a_malformed_target_is_a_non_match_and_cannot_block_a_later_saved_item(c1, monkeypatch):
    from core import carry_in_facts
    from infra.carry_in_drain import wake

    _write_lesson(c1, target="~otheruser/never-resolves")
    first = _save(c1, "rule v1\n")
    canonical_target = carry_in_facts.canonical_target
    monkeypatch.setattr(
        carry_in_facts, "canonical_target",
        lambda tray, workspace, target, *args: (_ for _ in ()).throw(RuntimeError("bad target"))
        if target.startswith("~otheruser") else canonical_target(tray, workspace, target, *args),
    )

    assert await wake(c1.workspace) is True
    assert [event["identity"] for event in _events("lesson.carry_in.saved_processed")] == [_saved()[0]["identity"]]
    assert _events("lesson.carry_in.pending") == []

    _write_lesson(c1, name="later", lesson_id="8b1c2a9e-3b7d-4c2e-9a1f-0d5e8b7c4a21")
    second = _save(c1, "rule v2\n")
    assert await wake(c1.workspace) is True
    assert _pending_fact().target_commit == second.sha
    assert first.sha != second.sha


@pytest.mark.asyncio
async def test_one_oldest_saved_fact_is_processed_per_wake(c1):
    from infra.carry_in_drain import wake

    _write_lesson(c1)
    first = _save(c1, "rule v1\n")
    second = _save(c1, "rule v2\n")

    assert await wake(c1.workspace) is True
    assert [event["identity"] for event in _events("lesson.carry_in.saved_processed")] == [_saved()[0]["identity"]]
    assert await wake(c1.workspace) is True
    assert len(_events("lesson.carry_in.saved_processed")) == 2
    assert {event["target_commit"] for event in _events("lesson.carry_in.pending")} == {first.sha, second.sha}


# --- drain ------------------------------------------------------------------


async def _make_pending(c1) -> None:
    """Match one saved item only, leaving its pending case for the wake under test."""
    from infra.carry_in_drain import _run_saved_commit

    assert await _run_saved_commit(c1.workspace) is True


# --- drain ------------------------------------------------------------------


@pytest.fixture
def no_samples(monkeypatch):
    from infra import carry_in_drain

    async def forbidden(*_args, **_kwargs):
        pytest.fail("a sample ran")

    monkeypatch.setattr(carry_in_drain, "_sample", forbidden)


def _local(monkeypatch, value="omlx@srv/model"):
    from infra import carry_in_capacity

    async def ready_idle(bound=None, exclude_thread=None):
        return value

    monkeypatch.setattr(carry_in_capacity, "ready_idle", ready_idle)


@pytest.mark.asyncio
async def test_a_changed_lesson_version_retires_its_pending_case(c1, monkeypatch, no_samples):
    from infra.carry_in_drain import wake

    _write_lesson(c1)
    _save(c1, "rule v1\n")
    await _make_pending(c1)
    _write_lesson(c1, text="A reworded rule.")
    _local(monkeypatch)

    assert await wake(c1.workspace) is True
    assert [(e["event"], e.get("reason")) for e in _events()][-1] == ("lesson.carry_in.retired", "version_changed")
    assert await wake(c1.workspace) is False


@pytest.mark.asyncio
async def test_an_action_less_case_is_terminal_before_any_model_selection(c1, monkeypatch, no_samples):
    from infra import carry_in_capacity, carry_in_drain

    _write_lesson(c1, actions=None)
    _save(c1, "rule v1\n")
    await _make_pending(c1)
    monkeypatch.setattr(carry_in_capacity, "ready_idle", lambda *a, **k: pytest.fail("selected a model"))

    assert await carry_in_drain.wake(c1.workspace) is True
    assert _events()[-1]["reason"] == "not_mechanically_executable"


@pytest.mark.asyncio
async def test_no_ready_local_model_marks_only_the_oldest_case_waiting(c1, monkeypatch, no_samples):
    from core import carry_in_facts
    from infra.carry_in_drain import wake

    _write_lesson(c1)
    _save(c1, "rule v1\n")
    _save(c1, "rule v2\n")
    await _make_pending(c1)
    await _make_pending(c1)
    oldest, younger = [e["identity"] for e in _events("lesson.carry_in.pending")]
    _local(monkeypatch, None)
    reads = []
    open_facts = carry_in_facts.open_facts

    def counted(db, *, claimed, limit=-1):
        found = open_facts(db, claimed=claimed, limit=limit)
        reads.append((claimed, limit, len(found)))
        return found

    monkeypatch.setattr(carry_in_facts, "open_facts", counted)

    assert await wake(c1.workspace) is False
    assert await wake(c1.workspace) is False
    waiting = [e for e in _events() if e["event"] == "lesson.carry_in.waiting"]
    assert [(e["identity"], e["reason"]) for e in waiting] == [(oldest, "no_ready_local_model")]
    assert younger != oldest
    assert [r for r in reads if r[0] is False] == [(False, 1, 1), (False, 1, 1)]


@pytest.mark.asyncio
async def test_an_interrupted_claim_becomes_coverage_and_its_eval_thread_is_reaped(c1, monkeypatch, no_samples):
    from core.carry_in_facts import record_claim
    from infra import carry_in_drain
    from infra.database import get_db, write_transaction

    _write_lesson(c1)
    _save(c1, "rule v1\n")
    await _make_pending(c1)
    fact = _pending_fact()
    with write_transaction(get_db(), scope="test") as db:
        record_claim(db, fact, "2026-10-24T00:00:00+00:00", "omlx@srv/model", "an-earlier-boot")
        db.execute(
            "INSERT INTO threads (id,user_id,title,model,created_at,updated_at) VALUES (?,?,?,?,?,?)",
            (f"eval-c1-{fact.identity}-b1", "steve", "x", "native/test", "t", "t"),
        )
    deleted = []

    async def delete(thread_id, user_id, drives):
        deleted.append(thread_id)
        return "ok"

    monkeypatch.setattr("api.thread_deletion.delete_owned_thread", delete)

    assert await carry_in_drain.wake(c1.workspace) is False
    assert _events()[-1]["event"] == "lesson.carry_in.coverage"
    assert _events()[-1]["reason"] == "interrupted"
    assert deleted == [f"eval-c1-{fact.identity}-b1"]


async def _run_scored(c1, monkeypatch, results):
    from infra import carry_in_drain

    _write_lesson(c1)
    first = _save(c1, "rule v1\n")
    _save(c1, "rule v2\n")
    await _make_pending(c1)
    await _make_pending(c1)
    _local(monkeypatch)
    calls = []

    async def sample(fact, owner, model, actions, prompt, label, created, live_oid=None):
        calls.append((fact.target_commit, label, prompt))
        created.append((f"eval-c1-{label}", owner["user_id"]))
        outcome = results.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(carry_in_drain, "_sample", sample)
    monkeypatch.setattr(carry_in_drain, "_delete_threads", _record_deleted(calls))
    assert await carry_in_drain.wake(c1.workspace) is True
    return first, calls


def _record_deleted(calls):
    async def delete(threads):
        if threads:
            calls.append(("deleted", [t for t, _u in threads], _events()[-1]["event"]))
    return delete


@pytest.mark.asyncio
async def test_baseline_must_fail_then_candidate_must_pass(c1, monkeypatch):
    first, calls = await _run_scored(c1, monkeypatch, [False, False, True, True])
    terminal = _events()[-1]

    assert (terminal["event"], terminal["verdict"]) == ("lesson.carry_in.result", "passed")
    assert (terminal["baseline_failures"], terminal["candidate_passes"], terminal["samples_completed"]) == (2, 2, 4)
    labels = [c[1] for c in calls if c[0] == first.sha]
    assert labels == ["b1", "b2", "c1", "c2"]
    baseline_prompt, candidate_prompt = calls[0][2], calls[2][2]
    assert "There is no file at `context/demo.md`" in baseline_prompt  # the pre-lesson parent
    assert "rule v1" in candidate_prompt and "Read first." not in candidate_prompt
    assert calls[-1] == ("deleted", ["eval-c1-b1", "eval-c1-b2", "eval-c1-c1", "eval-c1-c2"], "lesson.carry_in.result")


@pytest.mark.asyncio
async def test_a_passing_baseline_is_not_discriminating(c1, monkeypatch):
    _first, calls = await _run_scored(c1, monkeypatch, [True, True])
    terminal = _events()[-1]

    assert terminal["verdict"] == "not_discriminating"
    assert [c[1] for c in calls if c[0] != "deleted"] == ["b1", "b2"]


@pytest.mark.asyncio
async def test_an_unscorable_sample_never_counts_and_ends_in_coverage(c1, monkeypatch):
    from infra.carry_in_drain import _SampleFailed

    await _run_scored(c1, monkeypatch, [False, _SampleFailed("provider", "boom")])
    terminal = _events()[-1]

    assert (terminal["event"], terminal["reason"], terminal["samples_completed"]) == (
        "lesson.carry_in.coverage", "provider", 1,
    )


@pytest.mark.asyncio
async def test_one_drain_request_settles_only_the_oldest_runnable_case(c1, monkeypatch):
    """Spec section 3: one bounded oldest fact per wake, and one wake per request."""
    from infra import app_loop, carry_in_drain

    _write_lesson(c1)
    _save(c1, "rule v1\n")
    _save(c1, "rule v2\n")
    await _make_pending(c1)
    await _make_pending(c1)
    oldest, younger = [e["identity"] for e in _events("lesson.carry_in.pending")]
    _local(monkeypatch)
    labels = []

    async def sample(fact, owner, model, actions, prompt, label, created, live_oid=None):
        labels.append((fact.identity, label))
        return label.startswith("c")  # baseline fails, candidate passes: both runnable cases score

    submitted = []
    monkeypatch.setattr(carry_in_drain, "_sample", sample)
    monkeypatch.setattr(app_loop, "submit", submitted.append)

    c1.request_drain(c1.workspace)  # the real request route, once
    assert len(submitted) == 1
    await submitted.pop()

    by_identity = {}
    for event in _events():
        by_identity.setdefault(event["identity"], []).append(event["event"])
    assert by_identity[oldest] == [
        "lesson.carry_in.pending", "lesson.carry_in.claimed", "lesson.carry_in.result",
    ]
    assert by_identity[younger] == ["lesson.carry_in.pending"]
    assert {identity for identity, _label in labels} == {oldest}


@pytest.mark.asyncio
async def test_a_request_during_a_wake_earns_exactly_one_more_wake(c1, monkeypatch):
    from infra import carry_in_drain

    wakes = []

    async def wake(workspace):
        wakes.append(workspace)
        if len(wakes) == 1:
            await carry_in_drain.drain(workspace)  # arrives while this wake runs
        return len(wakes) < 5  # a wake that says "more" must not schedule another

    monkeypatch.setattr(carry_in_drain, "wake", wake)
    await carry_in_drain.drain(c1.workspace)
    assert len(wakes) == 2
    await carry_in_drain.drain(c1.workspace)
    assert len(wakes) == 3


@pytest.mark.asyncio
async def test_a_manifest_without_the_lesson_version_retires_the_case(c1, monkeypatch):
    from infra.carry_in_drain import _ManifestMismatch

    await _run_scored(c1, monkeypatch, [_ManifestMismatch("no")])
    assert (_events()[-1]["event"], _events()[-1]["reason"]) == ("lesson.carry_in.retired", "manifest_mismatch")


@pytest.mark.asyncio
async def test_a_sample_is_a_fresh_isolated_child_of_the_owner_checked_before_dispatch(c1, monkeypatch):
    from eval import endpoint, turn_result
    from eval.tool_allowlist import is_carry_in_record_only
    from infra import carry_in_drain
    from infra.database import get_db

    _write_lesson(c1)
    _save(c1, "rule v1\n")
    await _make_pending(c1)
    fact = _pending_fact()
    seen = {}

    async def run_eval_turn(*, thread_id, thread_row, prompt, model, timeout_seconds, after_manifest_before_dispatch):
        seen.update(thread=dict(thread_row), model=model, isolated=is_carry_in_record_only("write_file"))
        try:
            await after_manifest_before_dispatch("absent-manifest")
        except Exception as exc:
            return None, endpoint.EvalTurnFailure("handoff", f"Eval turn errored: {exc}")
        return "turn-1", None

    monkeypatch.setattr(endpoint, "run_eval_turn", run_eval_turn)
    monkeypatch.setattr(turn_result, "read_turn_result", lambda *_a: pytest.fail("read an unchecked turn"))
    created = []
    owner = {"user_id": "steve", "project_id": None}
    with pytest.raises(carry_in_drain._ManifestMismatch):
        await carry_in_drain._sample(fact, owner, "omlx@srv/m0", ACTIONS, "prompt", "b1", created)

    child = f"eval-c1-{fact.identity}-b1"
    assert created == [(child, "steve")]
    assert seen["isolated"] is True and seen["model"] == "omlx@srv/m0"
    row = get_db().execute("SELECT parent_thread_id, model FROM threads WHERE id=?", (child,)).fetchone()
    assert tuple(row) == (c1.owner, "omlx@srv/m0")
    assert not is_carry_in_record_only("write_file")


@pytest.mark.asyncio
async def test_each_sample_rechecks_exact_idle_capacity_right_before_its_provider_call(c1, monkeypatch):
    from eval import endpoint, turn_result
    from infra import carry_in_capacity, carry_in_drain

    _write_lesson(c1)
    _save(c1, "rule v1\n")
    await _make_pending(c1)
    fact = _pending_fact()
    checks, dispatched = [], []

    async def ready_idle(bound=None, exclude_thread=None):
        checks.append((bound, exclude_thread))
        return None  # a person started a local Turn after the claim

    async def run_eval_turn(*, thread_id, thread_row, prompt, model, timeout_seconds, after_manifest_before_dispatch):
        try:
            await after_manifest_before_dispatch("m1")
        except Exception as exc:
            return None, endpoint.EvalTurnFailure("handoff", f"Eval turn errored: {exc}")
        dispatched.append(model)
        return "turn-1", None

    monkeypatch.setattr(carry_in_drain, "_manifest_carries", lambda *_a: True)
    monkeypatch.setattr(carry_in_capacity, "ready_idle", ready_idle)
    monkeypatch.setattr(endpoint, "run_eval_turn", run_eval_turn)
    monkeypatch.setattr(turn_result, "read_turn_result", lambda *_a: pytest.fail("read an undispatched turn"))
    owner = {"user_id": "steve", "project_id": None}

    with pytest.raises(carry_in_drain._Unavailable):
        await carry_in_drain._sample(fact, owner, "omlx@srv/m0", ACTIONS, "prompt", "c1", [])
    assert checks == [("omlx@srv/m0", f"eval-c1-{fact.identity}-c1")]
    assert dispatched == []


@pytest.mark.asyncio
async def test_a_git_fault_reading_the_target_waits_instead_of_retiring_the_case(c1, monkeypatch, no_samples):
    from core import carry_in_facts
    from infra import carry_in_capacity
    from infra.carry_in_drain import wake
    from infra.guidance_git import GitFault

    _write_lesson(c1)
    _save(c1, "rule v1\n")
    await _make_pending(c1)
    committed_bytes = carry_in_facts.committed_bytes

    def fault(*_args):
        raise GitFault("cat-file: object store unreadable")

    monkeypatch.setattr(carry_in_facts, "committed_bytes", fault)
    monkeypatch.setattr(carry_in_capacity, "ready_idle", lambda *a, **k: pytest.fail("selected a model"))
    assert await wake(c1.workspace) is False
    assert [(e["event"], e.get("reason")) for e in _events()][-1] == ("lesson.carry_in.waiting", "history_unavailable")
    assert _events("lesson.carry_in.retired") == []

    monkeypatch.setattr(carry_in_facts, "committed_bytes", committed_bytes)
    _local(monkeypatch, None)
    await wake(c1.workspace)  # the history is back: the same case advances
    assert _events()[-1]["reason"] == "no_ready_local_model"


@pytest.mark.asyncio
async def test_an_unexpected_check_error_ends_in_error_coverage_and_reaps_after_it(c1, monkeypatch):
    _first, calls = await _run_scored(c1, monkeypatch, [False, KeyError("what_happened")])
    terminal = _events()[-1]
    assert (terminal["event"], terminal["reason"], terminal["samples_completed"]) == (
        "lesson.carry_in.coverage", "error", 1,
    )
    assert calls[-1] == ("deleted", ["eval-c1-b1", "eval-c1-b2"], "lesson.carry_in.coverage")


@pytest.mark.asyncio
async def test_a_claim_this_boot_left_open_is_an_error_not_a_restart(c1, monkeypatch, no_samples):
    from core.carry_in_facts import CarryInFact, record_claim
    from core.turn_core import BOOT_EPOCH
    from infra import carry_in_drain
    from infra.database import get_db, write_transaction

    _write_lesson(c1)
    _save(c1, "rule v1\n")
    _save(c1, "rule v2\n")
    await _make_pending(c1)
    await _make_pending(c1)
    this_boot, earlier = (CarryInFact.from_attributes(e) for e in _events("lesson.carry_in.pending"))
    with write_transaction(get_db(), scope="test") as db:
        record_claim(db, this_boot, "2026-10-24T00:00:00+00:00", "omlx@srv/model", BOOT_EPOCH)
        record_claim(db, earlier, "2026-10-24T00:00:00+00:00", "omlx@srv/model", "an-earlier-boot")

    await carry_in_drain.wake(c1.workspace)
    reasons = {e["identity"]: e["reason"] for e in _events("lesson.carry_in.coverage")}
    assert reasons == {this_boot.identity: "error", earlier.identity: "interrupted"}


@pytest.mark.asyncio
async def test_a_failed_sample_keeps_the_eval_routes_failure_kind_whatever_its_text(c1, monkeypatch):
    from eval import endpoint
    from infra import carry_in_drain

    _write_lesson(c1)
    _save(c1, "rule v1\n")
    await _make_pending(c1)
    fact = _pending_fact()

    async def run_eval_turn(**_kwargs):
        return None, endpoint.EvalTurnFailure("provider", "Eval turn errored: httpx connect timed out")

    monkeypatch.setattr(endpoint, "run_eval_turn", run_eval_turn)
    with pytest.raises(carry_in_drain._SampleFailed) as failed:
        await carry_in_drain._sample(fact, {"user_id": "steve", "project_id": None}, "omlx@srv/m0", ACTIONS, "p", "b1", [])
    assert failed.value.reason == "provider"


def _eval_route(monkeypatch, *, admit=None, held=False, batch="batch", execute=None, task=None):
    from infra import thread_driver, thread_execution
    from llm import resolution

    def admit_input(*_a, **_k):
        if admit:
            raise admit
        return SimpleNamespace(held_for_restart=held, batch=batch)

    async def execute_claimed_batch(*_a, **_k):
        if execute:
            raise execute
        return SimpleNamespace(turn_task=asyncio.ensure_future(task()), turn_id="turn-1")

    monkeypatch.setattr(resolution, "resolve_execution_target", lambda model: model)
    monkeypatch.setattr(thread_driver, "admit_input", admit_input)
    monkeypatch.setattr(thread_execution, "execute_claimed_batch", execute_claimed_batch)


async def _raises_timed_out():
    raise RuntimeError("provider connect timed out")


async def _hangs():
    await asyncio.sleep(5)


@pytest.mark.asyncio
@pytest.mark.parametrize(("route", "kind"), [
    ({"admit": RuntimeError("timed out acquiring lock")}, "admission"),
    ({"held": True}, "interrupted"),
    ({"batch": None}, "admission"),
    ({"execute": RuntimeError("Eval turn errored")}, "handoff"),
    ({"task": _raises_timed_out}, "provider"),
    ({"task": _hangs}, "timeout"),
])
async def test_the_eval_route_names_its_failure_kind_by_step(isolated_db, monkeypatch, route, kind):
    from core.carry_in_facts import COVERAGE_REASONS
    from eval.endpoint import EVAL_FAILURES, run_eval_turn

    _eval_route(monkeypatch, **route)
    turn_id, failure = await run_eval_turn(
        thread_id="t", thread_row={"user_id": "steve"}, prompt="p", model="m", timeout_seconds=0.05,
    )
    assert turn_id is None and failure.kind == kind
    assert set(EVAL_FAILURES) <= set(COVERAGE_REASONS)


@pytest.mark.asyncio
async def test_capacity_lost_between_samples_ends_the_claimed_case_in_visible_coverage(c1, monkeypatch):
    from infra.carry_in_drain import _Unavailable

    _first, calls = await _run_scored(c1, monkeypatch, [False, _Unavailable("busy")])
    terminal = _events()[-1]
    assert (terminal["event"], terminal["reason"], terminal["samples_completed"]) == (
        "lesson.carry_in.coverage", "no_ready_local_model", 1,
    )
    assert calls[-1][0] == "deleted" and calls[-1][2] == "lesson.carry_in.coverage"


def test_manifest_check_reads_the_skills_index_lessons(isolated_db):
    from infra.carry_in_drain import _manifest_carries
    from infra.database import get_db

    db = get_db()
    entries = [{"source_id": "dynamic:skills-index", "lessons": [{"id": "l1", "behavioral_sha256": "v1"}]}]
    db.execute(
        "INSERT INTO context_manifests (id,harness,entries_json,entry_count,created_at) VALUES ('m1','native',?,1,'t')",
        (json.dumps(entries),),
    )
    db.commit()

    assert _manifest_carries(db, "m1", "l1", "v1") is True
    assert _manifest_carries(db, "m1", "l1", "v2") is False
    assert _manifest_carries(db, None, "l1", "v1") is False


# --- local model selection --------------------------------------------------


def _rows(*statuses):
    return [
        {"id": f"m{i}", "litellm_model": f"omlx@srv/m{i}", "status": s, "provider": "omlx", "is_local": True}
        for i, s in enumerate(statuses)
    ]


def test_local_selection_needs_exactly_one_ready_native_row_and_no_other_local_turn(isolated_db, monkeypatch):
    from infra.carry_in_capacity import local_row
    from infra.database import get_db

    target = SimpleNamespace(harness="native", provider="omlx", available=True, catalog_id="m0")
    monkeypatch.setattr("llm.resolution.resolve_execution_target", lambda model: target)
    db = get_db()

    monkeypatch.setattr("llm.model_status.get_model_status_rows", lambda: _rows("ready", "ready"))
    assert local_row(db) is None
    monkeypatch.setattr("llm.model_status.get_model_status_rows", lambda: _rows("available"))
    assert local_row(db) is None
    monkeypatch.setattr("llm.model_status.get_model_status_rows", lambda: _rows("ready"))
    assert local_row(db) == ("omlx@srv/m0", "srv")

    owner = make_session(db, prefix="busy")
    db.execute(
        "INSERT INTO active_turns (id,thread_id,execution_epoch,model,status,started_at,boot_epoch) "
        "VALUES ('t1',?,0,'omlx@srv/m0','running','t','b')",
        (owner,),
    )
    db.commit()
    assert local_row(db) is None
    assert local_row(db, exclude_thread=owner) == ("omlx@srv/m0", "srv")  # a sample's own Turn

    target.harness = "codex"
    db.execute("DELETE FROM active_turns")
    db.commit()
    assert local_row(db) is None


def _status(active=0, waiting=0, total=7):
    return {"status": "ok", "active_requests": active, "waiting_requests": waiting, "total_requests": total}


@pytest.mark.asyncio
@pytest.mark.parametrize(("readings", "idle"), [
    ([_status(), _status()], True),
    ([_status(active=1), _status()], False),
    ([_status(), _status(waiting=1)], False),
    ([_status(total=7), _status(total=8)], False),  # a request started and finished between reads
    ([{"status": "ok"}, _status()], False),  # no activity fields: unproven, so not idle
    ([None, _status()], False),  # unauthenticated or unreachable
])
async def test_omlx_idle_needs_two_authenticated_idle_reads_with_no_new_request(monkeypatch, readings, idle):
    from infra import carry_in_capacity

    seen = []

    async def probe(client, server_id, host, port):
        seen.append((server_id, host, port))
        payload = readings[len(seen) - 1]
        return None if payload is None else (host, 1, payload)

    async def address(*_a):
        return ("127.0.0.1", 8210)

    monkeypatch.setattr("llm.providers.omlx_public_probe.probe_registered_status", probe)
    monkeypatch.setattr(carry_in_capacity, "run_in_db", address)
    monkeypatch.setattr(carry_in_capacity, "IDLE_GAP_SECONDS", 0)

    assert await carry_in_capacity.omlx_idle("srv") is idle
    assert seen[0] == ("srv", "127.0.0.1", 8210)
    assert len(seen) == (1 if carry_in_capacity.idle_reading(readings[0]) is None else 2)


@pytest.mark.asyncio
async def test_omlx_idle_fails_closed_without_a_live_registered_endpoint(monkeypatch):
    from infra import carry_in_capacity

    async def no_address(*_a):
        return None

    monkeypatch.setattr(carry_in_capacity, "run_in_db", no_address)
    monkeypatch.setattr(
        "llm.providers.omlx_public_probe.probe_registered_status", lambda *a: pytest.fail("probed"),
    )
    assert await carry_in_capacity.omlx_idle("srv") is False


@pytest.mark.asyncio
async def test_ready_idle_keeps_the_bound_model_and_never_substitutes_another_row(monkeypatch):
    from infra import carry_in_capacity

    probed = []

    async def row(_fn, exclude_thread):
        return ("omlx@srv/other", "srv")

    async def idle(server_id):
        probed.append(server_id)
        return True

    monkeypatch.setattr(carry_in_capacity, "run_off_db", row)
    monkeypatch.setattr(carry_in_capacity, "omlx_idle", idle)

    assert await carry_in_capacity.ready_idle("omlx@srv/model") is None
    assert probed == []
    assert await carry_in_capacity.ready_idle() == "omlx@srv/other"
    assert probed == ["srv"]


# --- scoring ----------------------------------------------------------------


def _call(tool, **params):
    return {"name": tool, "arguments": json.dumps(params)}


@pytest.mark.parametrize(("calls", "passes"), [
    ([_call("read_file", path=TARGET), _call("plan_update", operation="mark_done", node="n")], True),
    ([_call("plan_update", operation="mark_done")], False),
    ([_call("plan_update", operation="mark_done"), _call("read_file", path=TARGET),
      _call("plan_update", operation="mark_done")], True),
    ([_call("read_file", path=TARGET), _call("write_file", path=TARGET),
      _call("plan_update", operation="mark_done")], False),
    ([_call("tool_call", name="read_file", arguments={"path": TARGET}),
      _call("plan_update", operation="mark_done")], True),
    ([], False),
])
def test_ordered_scoring(calls, passes):
    from core.carry_in_scoring import sample_passes

    assert sample_passes(ACTIONS, calls) is passes


def test_the_prompt_carries_the_situation_and_target_never_the_right_call():
    from infra.carry_in_drain import sample_prompt

    case = {"what_happened": "It closed.", "right_call": "Read first.", "does_not_apply": "x", "actions": ACTIONS}
    prompt = sample_prompt(case, TARGET, b"```inner```\n")

    assert "It closed." in prompt and "Read first." not in prompt and "mark_done" not in prompt
    assert "````\n```inner```\n````" in prompt
    assert "no file" in sample_prompt(case, TARGET, None)


# --- record-only fence and dispatch hook ------------------------------------


class _Spy(Tool):
    """A registered stand-in for a non-read-only tool whose only effect is recording that it ran."""

    def __init__(self, name, properties):
        self._name, self._properties, self.ran = name, properties, []

    @property
    def name(self):
        return self._name

    @property
    def description(self):
        return f"Spy {self._name}."

    @property
    def parameters(self):
        return {"type": "object", "properties": self._properties, "required": list(self._properties)}

    async def execute(self, **params):
        self.ran.append(params)
        return f"executed {self._name}"


@pytest.mark.asyncio
async def test_isolation_records_non_read_only_calls_without_executing_them(isolated_db):
    """Through the public ToolRegistry.execute path, under a real thread and admitted Turn."""
    from eval.tool_allowlist import carry_in_tool_isolation, is_carry_in_record_only
    from infra import state
    from infra.database import get_db
    from infra.thread_driver import admit_input
    from tools.registry import ToolRegistry
    from tools.tool_surface import ToolCallTool

    db = get_db()
    thread_id = make_session(db, prefix="eval-c1-fence")
    admission = admit_input(
        thread_id, role="user", content="sample", intent="apply_now", message_id="fence-input",
        turn_id="fence-turn", boot_epoch="test-boot", admitted_at="t", db=db,
    )
    assert admission.batch is not None
    registry = ToolRegistry()
    write_spy = _Spy("write_file", {"path": {"type": "string"}, "content": {"type": "string"}})
    plan_spy = _Spy("plan_update", {"operation": {"type": "string"}})
    for tool in (write_spy, plan_spy, ToolCallTool()):
        registry.register(tool)
    write = {"path": TARGET, "content": "x"}
    close = {"operation": "mark_done"}
    thread_token = state._current_thread_id.set(thread_id)
    turn_token = state._current_turn_identity.set(admission.batch.identity)
    try:
        # Unfenced, the same identity and route reach the real tool and return its success.
        assert await registry.execute("plan_update", close) == "executed plan_update"
        (ran,) = plan_spy.ran
        assert ran["operation"] == "mark_done" and ran["_thread_id"] == thread_id
        assert ran["_host_access_turn_identity"].turn_id == "fence-turn"
        plan_spy.ran.clear()
        with carry_in_tool_isolation():
            results = {
                "direct write": await registry.execute("write_file", write),
                "direct close": await registry.execute("plan_update", close),
                "child task": await asyncio.create_task(registry.execute("write_file", write)),
                "tool_call write": await registry.execute("tool_call", {"name": "write_file", "arguments": write}),
                "tool_call close": await registry.execute("tool_call", {"name": "plan_update", "arguments": close}),
            }
            assert not is_carry_in_record_only("read_file")
    finally:
        state._current_turn_identity.reset(turn_token)
        state._current_thread_id.reset(thread_token)

    recorded = {label: json.loads(result) for label, result in results.items()}
    for label, result in recorded.items():
        tool = "plan_update" if label.endswith("close") else "write_file"
        assert result == {"ok": True, "record_only": True, "tool": tool}, label
    assert write_spy.ran == [] and plan_spy.ran == []
    assert not is_carry_in_record_only("write_file")


@pytest.mark.asyncio
async def test_the_manifest_hook_runs_after_recording_and_can_refuse_dispatch(monkeypatch):
    from infra import thread_execution

    order = []
    monkeypatch.setattr(thread_execution, "persist_prepared_installed_context", lambda *a: None)
    monkeypatch.setattr(thread_execution, "record_dispatch_manifest", lambda *a: order.append("manifest") or "m1")

    async def dispatch(provider, context):
        order.append("dispatch")

    monkeypatch.setattr("infra.send_dispatch.dispatch", dispatch)
    monkeypatch.setattr(thread_execution, "_dispatch_context", lambda *a, **k: None)

    async def refuse(manifest_id):
        order.append(f"hook:{manifest_id}")
        raise RuntimeError("missing lesson")

    batch = SimpleNamespace(thread_id="t", turn_id="u")
    prepared = SimpleNamespace(provider="codex", target=None, prepared_context=None)
    with pytest.raises(RuntimeError, match="missing lesson"):
        await thread_execution._dispatch_prepared(
            batch, prepared, SimpleNamespace(), attempt_id="a", user=None, ws=None,
            after_persist_before_dispatch=None, after_manifest_before_dispatch=refuse,
        )
    assert order == ["manifest", "hook:m1"]


# --- correctness reset: delivery, discovery, baseline, eligibility, hook ----


async def _real_drain(c1, monkeypatch) -> None:
    """One save-time request through the real request route, awaited."""
    from infra import app_loop

    submitted = []
    monkeypatch.setattr(app_loop, "submit", submitted.append)
    c1.request_drain(c1.workspace)
    assert len(submitted) == 1
    await submitted.pop()


@pytest.mark.asyncio
async def test_a_save_and_its_one_drain_request_advance_that_saves_own_case(c1, monkeypatch):
    from infra import carry_in_drain

    _write_lesson(c1)
    _save(c1, "rule v1\n")
    _local(monkeypatch)
    labels = []

    async def sample(fact, owner, model, actions, prompt, label, created, live_oid=None):
        labels.append(label)
        return label.startswith("c")

    monkeypatch.setattr(carry_in_drain, "_sample", sample)
    await _real_drain(c1, monkeypatch)

    assert [e["event"] for e in _events() if e["event"] != "lesson.carry_in.saved"][-2:] == [
        "lesson.carry_in.claimed", "lesson.carry_in.result",
    ]
    assert labels == ["b1", "b2", "c1", "c2"]


@pytest.mark.asyncio
async def test_a_project_tray_lesson_is_found_and_a_repository_tray_is_not(c1, monkeypatch):
    from infra.database import get_db
    from tests.project_ref_helpers import project_ref, select_workspace, write_project_context

    select_workspace(monkeypatch, c1.workspace)
    project = write_project_context(c1.workspace, "p")
    db = get_db()
    db.execute(
        "INSERT INTO projects (id,name,path,created_at,updated_at) VALUES ('p','P',?,'t','t')",
        (project_ref("p"),),
    )
    db.execute("UPDATE threads SET project_id='p' WHERE id=?", (c1.owner,))
    db.commit()
    db.close()
    _write_lesson(c1, root=project)
    # A repository checked out under the project carries its own trays CORSIA never loads.
    _write_lesson(c1, root=project / "source" / "repo", name="repo-copy",
                  lesson_id="9c1c2a9e-3b7d-4c2e-9a1f-0d5e8b7c4a21")
    _save(c1, "rule v1\n")
    await _make_pending(c1)

    fact = _pending_fact()
    assert fact.lesson_path == f"projects/p/.threads/{c1.owner}/skills/demo-lesson/SKILL.md"


async def _projected_case(c1, *, claim=True):
    """A pending case whose pre-lesson baseline, saved candidate and live target all differ."""
    from core.carry_in_facts import record_claim
    from infra.database import get_db, write_transaction

    _save(c1, "rule v0\n")
    await _make_pending(c1)  # before the lesson exists: matches nothing
    _write_lesson(c1)
    _save(c1, "rule v1\n")
    await _make_pending(c1)
    fact = _pending_fact()
    (c1.workspace / TARGET).write_text("rule LIVE\n")  # edited since, never saved
    if claim:
        with write_transaction(get_db(), scope="test") as db:
            record_claim(db, fact, "2026-10-24T00:00:00+00:00", "omlx@srv/model", "boot")
    return fact


def _eval_thread(c1, fact, label: str) -> str:
    from core.context_projection import c1_thread_id
    from infra.database import get_db

    thread_id = c1_thread_id(fact.identity, label)
    db = get_db()
    db.execute(
        "INSERT INTO threads (id,user_id,title,model,parent_thread_id,created_at,updated_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (thread_id, "steve", "x", "native/test", c1.owner, "t", "t"),
    )
    db.commit()
    db.close()
    return thread_id


def _target_row(c1, thread_id: str):
    from core.dynamic_context_registry import collect_context_sources
    from infra.database import get_db

    db = get_db()
    try:
        collection = collect_context_sources(
            thread_id=thread_id, project_id=None, workspace=c1.workspace, db=db,
        )
    finally:
        db.close()
    target = (c1.workspace / TARGET).resolve()
    rows = [
        source for source in collection.sources
        if (source.metadata or {}).get("path")
        and Path(source.metadata["path"]).resolve() == target
    ]
    assert len(rows) == 1, [source.source_id for source in collection.sources]
    return rows[0]


@pytest.mark.asyncio
async def test_each_side_sees_its_revision_in_context_and_a_normal_thread_sees_the_live_file(c1):
    from core.guidance_manifest import git_blob_oid

    fact = await _projected_case(c1)
    baseline = _target_row(c1, _eval_thread(c1, fact, "b1"))
    candidate = _target_row(c1, _eval_thread(c1, fact, "c1"))
    normal = _target_row(c1, c1.owner)

    assert "rule v0" in baseline.prompt_content and "rule LIVE" not in baseline.prompt_content
    assert baseline.raw_version["git_blob_oid"] == git_blob_oid(b"rule v0\n")
    assert "rule v1" in candidate.prompt_content and "rule LIVE" not in candidate.prompt_content
    assert candidate.raw_version["git_blob_oid"] == git_blob_oid(b"rule v1\n")
    assert "rule LIVE" in normal.prompt_content
    assert normal.raw_version["git_blob_oid"] == git_blob_oid(b"rule LIVE\n")


@pytest.mark.asyncio
async def test_an_eval_thread_without_a_claimed_case_fails_closed_before_dispatch(c1):
    from core.context_projection import ProjectionUnavailable

    fact = await _projected_case(c1, claim=False)
    with pytest.raises(ProjectionUnavailable):
        _target_row(c1, _eval_thread(c1, fact, "b1"))


def _sandbox(monkeypatch, root: Path) -> None:
    """Authorize file tools for this workspace alone, as the media read tool tests do."""
    from tools import sandbox_config

    monkeypatch.setattr(sandbox_config, "_STATIC_ALLOWED_RW", [root])
    monkeypatch.setattr(sandbox_config, "_STATIC_ALLOWED_RO", [])
    monkeypatch.setattr(sandbox_config, "_list_authoritative_repo_policies", lambda: [])


@pytest.mark.asyncio
async def test_read_only_tools_see_the_sides_revision_and_ordinary_threads_the_live_file(c1, monkeypatch):
    from tools.filesystem_rw import ReadFileTool
    from tools.grep import GrepTool

    fact = await _projected_case(c1)
    b1, c1_side = _eval_thread(c1, fact, "b1"), _eval_thread(c1, fact, "c1")
    ws = c1.workspace.resolve()
    _sandbox(monkeypatch, ws)
    read = ReadFileTool(workspace=ws, allowed_dir=ws)
    grep = GrepTool(workspace=ws, allowed_dir=ws)
    target = str(ws / TARGET)

    assert "rule v0" in await read.execute(path=target, _thread_id=b1)
    assert "rule v1" in await read.execute(path=target, _thread_id=c1_side)
    assert "rule LIVE" in await read.execute(path=target, _thread_id=c1.owner)
    for scope in (ws, ws / "context", ws / TARGET):
        found = await grep.execute(pattern="rule", path=str(scope), output_mode="content", _thread_id=b1)
        assert "rule v0" in found and "LIVE" not in found, (scope, found)
    live = await grep.execute(pattern="rule", path=str(ws / "context"), output_mode="content", _thread_id=c1.owner)
    assert "LIVE" in live


@pytest.mark.asyncio
async def test_a_later_regression_keeps_the_pre_lesson_baseline_and_fails(c1, monkeypatch):
    from core.carry_in_facts import RETIRED, CarryInFact, record_terminal, side_bytes
    from infra import carry_in_drain
    from infra.database import get_db, write_transaction

    _save(c1, "rule v0\n")
    await _make_pending(c1)
    _write_lesson(c1)
    fix = _save(c1, "rule v1\n")
    await _make_pending(c1)
    regression = _save(c1, "rule v0 again\n")
    await _make_pending(c1)
    first, later = (CarryInFact.from_attributes(e) for e in _events("lesson.carry_in.pending"))
    assert (first.baseline_from, later.baseline_from) == (fix.sha, fix.sha)
    assert later.target_commit == regression.sha
    assert side_bytes(c1.workspace, later, "b") == b"rule v0\n"

    # With the fix's own case settled, a model that follows only v1 fails both sides of the regression.
    with write_transaction(get_db(), scope="test") as db:
        record_terminal(db, first, "2026-10-24T00:00:00+00:00", RETIRED, reason="case_changed")
    _local(monkeypatch)

    async def sample(fact, owner, model, actions, prompt, label, created, live_oid=None):
        return "rule v1" in prompt

    monkeypatch.setattr(carry_in_drain, "_sample", sample)
    assert await carry_in_drain.wake(c1.workspace) is True
    terminal = _events()[-1]
    assert (terminal["identity"], terminal["verdict"]) == (later.identity, "failed")


@pytest.mark.asyncio
async def test_a_trial_the_index_would_not_offer_retires_before_any_claim(c1, monkeypatch, no_samples):
    from infra import carry_in_capacity
    from infra.carry_in_drain import wake

    _write_lesson(c1, offered=False)
    _save(c1, "rule v1\n")
    monkeypatch.setattr(carry_in_capacity, "ready_idle", lambda *a, **k: pytest.fail("selected a model"))

    assert await wake(c1.workspace) is True
    assert [(e["event"], e.get("reason")) for e in _events()][-1] == ("lesson.carry_in.retired", "not_offered")
    assert _events("lesson.carry_in.claimed") == []


def test_a_recorder_fault_after_the_commit_keeps_the_save_written_with_a_warning(c1, monkeypatch):
    import sqlite3

    from core import carry_in_facts

    def fault(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(carry_in_facts, "record_committed_save", fault)
    result = _save(c1, "rule v1\n")

    assert result.outcome == "written" and result.sha
    assert "re-test was not queued" in result.line
    assert (c1.workspace / TARGET).read_text() == "rule v1\n"


class _NativeAgent:
    """The Native agent slice the eval route uses, over a real ToolRegistry."""

    model = "openai/test-checkpoint"
    max_iterations = 3
    max_tokens = 100

    def __init__(self, workspace, tools):
        from core.context_builder import ContextBuilder

        self.context = ContextBuilder(workspace)
        self.workspace = workspace
        self.tools = tools

    async def _connect_mcp(self):
        return None

    def _strip_think(self, content):
        return content

    def _tool_hint(self, tool_calls) -> str:
        return ", ".join(tc.name for tc in tool_calls)


@pytest.mark.asyncio
async def test_real_eval_turns_see_only_their_sides_revision_and_cannot_write(c1, monkeypatch):
    """The drain's own samples through run_eval_turn, the Native loop and a real ToolRegistry."""
    from unittest.mock import AsyncMock

    from core import turns_steps
    from infra import carry_in_drain
    from llm import router
    from tools.filesystem_rw import ReadFileTool, WriteFileTool
    from tools.registry import ToolRegistry

    await _projected_case(c1, claim=False)
    ws = c1.workspace.resolve()
    _sandbox(monkeypatch, ws)
    target = str(ws / TARGET)
    registry = ToolRegistry()
    for tool in (ReadFileTool(workspace=ws, allowed_dir=ws), WriteFileTool(workspace=ws, allowed_dir=ws)):
        registry.register(tool)
    dispatched: list[list[dict]] = []

    async def dispatch_openai(messages, tools, **kwargs):
        dispatched.append(messages)
        if messages[-1].get("role") == "tool":
            return router.LLMResponse(content="done", usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})
        return router.LLMResponse(content="", tool_calls=[
            router.ToolCallRequest(id="r", name="read_file", arguments={"path": target}),
            router.ToolCallRequest(id="w", name="write_file", arguments={"path": target, "content": "overwritten"}),
        ])

    monkeypatch.setattr("core.agent.get_agent", AsyncMock(return_value=_NativeAgent(ws, registry)))
    monkeypatch.setattr(router, "_dispatch_openai", dispatch_openai)
    monkeypatch.setattr(turns_steps, "emit_reasoning", AsyncMock())
    monkeypatch.setattr(carry_in_drain, "_delete_threads", AsyncMock())
    _local(monkeypatch, _NativeAgent.model)

    assert await carry_in_drain.wake(c1.workspace) is True

    terminal = _events()[-1]
    assert terminal["event"] == "lesson.carry_in.result", terminal
    assert (terminal["verdict"], terminal["samples_completed"]) == ("failed", 4)
    assert (ws / TARGET).read_text() == "rule LIVE\n"  # write_file was recorded, never run
    assert len(dispatched) == 8
    for index, messages in enumerate(dispatched):
        text = json.dumps(messages)
        revision = "rule v0" if index < 4 else "rule v1"
        assert revision in text and "rule LIVE" not in text, index
    tool_results = [m for messages in dispatched[1::2] for m in messages if m.get("role") == "tool"]
    assert any("rule v0" in str(m.get("content")) for m in tool_results)
    assert any("record_only" in str(m.get("content")) for m in tool_results)
