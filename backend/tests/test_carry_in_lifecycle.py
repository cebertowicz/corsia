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
    db.commit()
    db.close()
    drains: list[Path | None] = []
    request_drain = carry_in_drain.request_drain
    monkeypatch.setattr(carry_in_drain, "request_drain", lambda workspace=None: drains.append(workspace))
    return SimpleNamespace(workspace=workspace, owner=owner, drains=drains, request_drain=request_drain)


def _lesson_path(c1, name="demo-lesson") -> Path:
    return c1.workspace / ".threads" / c1.owner / "skills" / name / "SKILL.md"


def _write_lesson(
    c1, *, actions=ACTIONS, text="Read the rule before closing.", state="trial",
    name="demo-lesson", lesson_id=LESSON_ID, target=TARGET, commit=True,
) -> Path:
    from core.lesson_files import render_trial
    from core.lesson_trials import Trial

    case = {"what_happened": "A node closed unread.", "right_call": "Read first.", "does_not_apply": "No rule."}
    if actions is not None:
        case["actions"] = actions
    path = _lesson_path(c1, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_trial(
        lesson_id=lesson_id, owner=c1.owner, name=name, description="Use when closing a node.",
        text=text, trial=Trial(kind="hypothesis", review_after="2099-01-01", state=state),
        source_refs=[{"kind": "message", "reference": "m1"}],
        case_metadata={"target_file": target, "case": case},
    ))
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


# --- recorder ---------------------------------------------------------------


def test_a_written_commit_records_one_pending_fact_and_requests_a_drain(c1):
    _write_lesson(c1)
    result = _save(c1, "rule v1\n")

    assert result.outcome == "written"
    fact = _pending_fact()
    assert fact is not None
    assert (fact.lesson_id, fact.target_path, fact.target_commit) == (LESSON_ID, TARGET, result.sha)
    assert fact.owner_thread_id == c1.owner
    assert c1.drains == [c1.workspace]
    assert "what_happened" not in json.dumps(_events())  # ids and hashes only


def test_no_git_commit_means_no_run(c1, monkeypatch):
    from infra import guidance_git

    _write_lesson(c1)
    _save(c1, "rule v1\n")
    assert len(_events()) == 1
    assert _save(c1, "rule v1\n").outcome == "unchanged"
    monkeypatch.setattr(guidance_git, "workspace_repository", lambda *_a, **_k: (None, "off"))
    assert _save(c1, "rule v2\n").outcome == "history-off"

    assert len(_events()) == 1
    assert len(c1.drains) == 1


def test_an_unsynced_index_commit_still_records_its_case(c1, monkeypatch):
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
    assert _pending_fact().target_commit == result.sha


def test_a_retried_recorder_is_a_no_op_and_a_later_commit_is_a_new_identity(c1):
    from core.carry_in_scan import record_committed_save

    _write_lesson(c1)
    first = _save(c1, "rule v1\n")
    assert record_committed_save(c1.workspace, [c1.workspace / TARGET], first.sha) is None
    assert len(_events("lesson.carry_in.pending")) == 1

    second = _save(c1, "rule v2\n")
    pending = _events("lesson.carry_in.pending")
    assert [p["target_commit"] for p in pending] == [first.sha, second.sha]
    assert pending[0]["identity"] != pending[1]["identity"]


def test_a_save_of_an_unrelated_file_or_a_retired_lesson_records_nothing(c1):
    _write_lesson(c1, state="retired")
    assert _save(c1, "rule v1\n").outcome == "written"
    assert _events() == []


@pytest.mark.asyncio
async def test_a_recorder_failure_keeps_the_save_owes_its_scan_and_the_drain_recovers_it(c1, monkeypatch, no_samples):
    """C11/C9: the receipt names who acts with no raw error text; the scan is owed durably."""
    from core import carry_in_facts, carry_in_scan
    from infra.carry_in_drain import wake

    _write_lesson(c1)
    committed_cases = carry_in_facts.committed_cases

    def broken(*_args, **_kwargs):
        raise RuntimeError("secret /private/path grep failed")

    monkeypatch.setattr(carry_in_facts, "committed_cases", broken)
    result = _save(c1, "rule v1\n")

    assert result.outcome == "written"
    assert (c1.workspace / TARGET).read_text() == "rule v1\n"
    assert carry_in_scan.SCAN_RETRYING in result.line
    assert "grep failed" not in result.line and "/private" not in result.line
    owed = _events("lesson.carry_in.scan_owed")
    assert [(e["commit"], e["target_path"], e["reason"]) for e in owed] == [(result.sha, TARGET, "failed")]
    assert _events("lesson.carry_in.pending") == [] and c1.drains == [c1.workspace]

    monkeypatch.setattr(carry_in_facts, "committed_cases", committed_cases)
    _local(monkeypatch, None)
    await wake(c1.workspace)
    assert _pending_fact().target_commit == result.sha
    assert [e["pending"] for e in _events("lesson.carry_in.scanned")] == [1]
    await wake(c1.workspace)  # a closed scan never runs again
    assert len(_events("lesson.carry_in.pending")) == 1


def test_an_unschedulable_scan_says_what_the_person_can_do_without_the_error(c1, monkeypatch):
    from core import carry_in_facts, carry_in_scan

    _write_lesson(c1)

    def broken(*_args, **_kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(carry_in_facts, "committed_cases", broken)
    monkeypatch.setattr(carry_in_scan, "_owe_scans", broken)
    result = _save(c1, "rule v1\n")

    assert result.outcome == "written"
    assert carry_in_scan.SCAN_LOST.format(ref=result.sha[:12]) in result.line
    assert "locked" not in result.line
    assert _events() == [] and c1.drains == []


# --- request-path budget (EAS21) --------------------------------------------


def test_a_differently_named_symlink_target_is_matched_on_save(c1):
    """The save scan resolves a target the way the matcher does, never by its spelling."""
    (c1.workspace / "context").mkdir()
    (c1.workspace / "context" / "alias.md").symlink_to("demo.md")
    _write_lesson(c1, target="context/alias.md")

    result = _save(c1, "rule v1\n")

    fact = _pending_fact()
    assert (fact.lesson_id, fact.target_path, fact.target_commit) == (LESSON_ID, TARGET, result.sha)


def test_the_save_scan_drops_only_lessons_proven_to_target_another_file(c1):
    from infra.database import get_db

    from core.carry_in_scan import candidate_lessons, lesson_keys

    _write_lesson(c1)
    _write_lesson(c1, name="dotted", lesson_id="8b1c2a9e-3b7d-4c2e-9a1f-0d5e8b7c4a21", target="context/x/../demo.md")
    _write_lesson(c1, name="other", lesson_id="7a1c2a9e-3b7d-4c2e-9a1f-0d5e8b7c4a21", target="context/other.md")
    unprovable = {
        "block": f"target_file: >-\n      {TARGET}",
        "quoted": f'target_file: "{TARGET}"',
        "flow": f"{{target_file: {TARGET}}}",
        "continued": "target_file: context/\n      demo.md",
    }
    for name, line in unprovable.items():
        path = _lesson_path(c1, name)
        path.parent.mkdir(parents=True)
        path.write_text(_lesson_path(c1).read_text().replace(f"target_file: {TARGET}", line))
    _git(["add", "-A"], cwd=c1.workspace)
    _git([*_IDENTITY, "commit", "-qm", "forms"], cwd=c1.workspace)

    keys = lesson_keys(c1.workspace)
    rel = lambda name: _lesson_path(c1, name).relative_to(c1.workspace).as_posix()  # noqa: E731
    assert keys[rel("demo-lesson")].owner == c1.owner and keys[rel("demo-lesson")].target == TARGET
    assert all(keys[rel(name)].target is None for name in unprovable)
    db = get_db()
    try:
        found = candidate_lessons(db, c1.workspace, {TARGET}, keys)
        assert sorted(found) == sorted([rel("demo-lesson"), rel("dotted"), *map(rel, unprovable)])
        assert rel("other") in candidate_lessons(db, c1.workspace, {"context/other.md"}, keys)
        assert candidate_lessons(db, c1.workspace, {TARGET}, keys, owner_budget=0) is None
    finally:
        db.close()


@pytest.mark.parametrize("raw", [
    "context/demo.md", "'context/it''s.md'", "context/a b.md", "~/notes.md", "context/x\t#tab-comment",
    "context/x: y", "context/x:\ty", "'unclosed", "*alias", '"double"', "null", "0o17", "true",
])
def test_the_scalar_reader_returns_what_yaml_reads_or_nothing(raw):
    import yaml

    from core.carry_in_scan import _scalar

    value = _scalar(raw)
    if value is not None:  # whatever it proves, YAML itself reads the same string
        assert yaml.safe_load(f"k: {raw}") == {"k": value}


@pytest.mark.asyncio
async def test_a_save_over_its_owner_budget_defers_the_whole_scan_to_the_drain(c1, monkeypatch, no_samples):
    from core import carry_in_scan
    from infra.carry_in_drain import wake

    _write_lesson(c1)
    monkeypatch.setattr(carry_in_scan, "SAVE_OWNER_BUDGET", 0)

    result = _save(c1, "rule v1\n")
    assert _events("lesson.carry_in.pending") == []
    owed = _events("lesson.carry_in.scan_owed")
    assert [(e["target_path"], e["reason"]) for e in owed] == [(TARGET, "over_budget")]

    _local(monkeypatch, None)
    await wake(c1.workspace)  # the drain has no owner budget
    assert _pending_fact().target_commit == result.sha


def test_an_inline_save_scan_resolves_each_owner_once(c1, monkeypatch):
    from core import placement_answer

    _write_lessons(c1, 3)
    places = []
    thread_place = placement_answer.thread_place
    monkeypatch.setattr(placement_answer, "thread_place", lambda db, owner: places.append(owner) or thread_place(db, owner))

    _save(c1, "rule v1\n")
    assert len(_events("lesson.carry_in.pending")) == 3
    assert places == [c1.owner]


@pytest.mark.asyncio
async def test_a_save_over_its_case_budget_defers_the_whole_scan_to_the_drain(c1, monkeypatch, no_samples):
    from core import carry_in_facts, carry_in_scan
    from infra.carry_in_drain import wake

    _write_lessons(c1, carry_in_scan.SAVE_CASE_BUDGET + 1)
    read = []
    real = carry_in_facts._read_lesson
    monkeypatch.setattr(carry_in_facts, "_read_lesson", lambda path: read.append(path) or real(path))

    result = _save(c1, "rule v1\n")
    assert result.line is None or "CORSIA" not in result.line  # nothing failed: no receipt line
    assert read == [] and _events("lesson.carry_in.pending") == []
    owed = _events("lesson.carry_in.scan_owed")
    assert [(e["target_path"], e["reason"]) for e in owed] == [(TARGET, "over_budget")]
    assert c1.drains == [c1.workspace]

    _local(monkeypatch, None)
    await wake(c1.workspace)
    pending = _events("lesson.carry_in.pending")
    assert len(pending) == carry_in_scan.SAVE_CASE_BUDGET + 1  # no eligible case dropped
    assert {p["target_commit"] for p in pending} == {result.sha}


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
    oldest, younger = [e["identity"] for e in _events()]
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
    fact = _pending_fact()
    with write_transaction(get_db(), scope="test") as db:
        record_claim(db, fact, "2026-10-24T00:00:00+00:00", "omlx@srv/model", "an-earlier-boot")
        db.execute(
            "INSERT INTO threads (id,user_id,title,model,created_at,updated_at) VALUES (?,?,?,?,?,?)",
            (f"eval-c1-{fact.identity[:16]}-b1", "steve", "x", "native/test", "t", "t"),
        )
    deleted = []

    async def delete(thread_id, user_id, drives):
        deleted.append(thread_id)
        return "ok"

    monkeypatch.setattr("api.thread_deletion.delete_owned_thread", delete)

    assert await carry_in_drain.wake(c1.workspace) is False
    assert _events()[-1]["event"] == "lesson.carry_in.coverage"
    assert _events()[-1]["reason"] == "interrupted"
    assert deleted == [f"eval-c1-{fact.identity[:16]}-b1"]


async def _run_scored(c1, monkeypatch, results):
    from infra import carry_in_drain

    _write_lesson(c1)
    first = _save(c1, "rule v1\n")
    _save(c1, "rule v2\n")
    _local(monkeypatch)
    calls = []

    async def sample(fact, owner, model, actions, prompt, label, created):
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
    oldest, younger = [e["identity"] for e in _events("lesson.carry_in.pending")]
    _local(monkeypatch)
    labels = []

    async def sample(fact, owner, model, actions, prompt, label, created):
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

    child = f"eval-c1-{fact.identity[:16]}-b1"
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
    assert checks == [("omlx@srv/m0", f"eval-c1-{fact.identity[:16]}-c1")]
    assert dispatched == []


@pytest.mark.asyncio
async def test_a_git_fault_reading_the_target_waits_instead_of_retiring_the_case(c1, monkeypatch, no_samples):
    from core import carry_in_facts
    from infra import carry_in_capacity
    from infra.carry_in_drain import wake
    from infra.guidance_git import GitFault

    _write_lesson(c1)
    _save(c1, "rule v1\n")
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
