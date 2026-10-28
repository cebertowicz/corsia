"""``start_worker`` rules start one confined worker per request (R19, R24, design §4.2, §8.1).

Through the real tool, intake, evaluation, runner, spawn and Claude Code runtime; only the
``claude`` binary is a fake that answers with the test's reply.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tests.rule_worker_helpers import (  # noqa: F401
    OWNER, atool, make_rule, owner_wakes, request, settle_all, starts, world, worker_events,
)


def _launch(worker: str) -> dict:
    from infra.database import get_db

    row = get_db().execute(
        "SELECT i.metadata FROM artifact_identities i JOIN artifact_registrations r "
        "ON r.artifact_id=i.id WHERE r.parent_thread_id=? AND i.integration='claude-code' "
        "AND i.type='session'", (worker,)).fetchone()
    return json.loads(row["metadata"])["claude_invocation"]


def _decision(rule: str) -> int:
    from infra.database import get_db

    return get_db().execute("SELECT id FROM event_route_decisions WHERE rule_id=? ORDER BY id",
                            (rule,)).fetchone()[0]


def _workers() -> list[str]:
    from infra.database import get_db

    return [row[0] for row in get_db().execute("SELECT id FROM threads WHERE spawned_by=?", (OWNER,))]


# 1 -- a matching rule starts one worker with the review profile, reading only its inputs.
@pytest.mark.asyncio
async def test_a_matching_rule_starts_one_review_worker_that_reads_only_its_inputs(world):
    from automation import worker_runner
    from infra.database import get_db

    rule = await make_rule()
    await request()
    assert starts(rule) == [("queued", None, "pr-1@a1", None)]

    assert await worker_runner.drive() == 1
    [(outcome, _reason, identity, worker)] = starts(rule)
    assert outcome == "started" and identity == "pr-1@a1" and _workers() == [worker]
    child = get_db().execute("SELECT spawned_by, model FROM threads WHERE id=?", (worker,)).fetchone()
    assert (child["spawned_by"], child["model"]) == (OWNER, "claude-code/sonnet")
    assert get_db().execute("SELECT title FROM threads WHERE id=?", (worker,)).fetchone()[0] == "worker-flow"
    launch = _launch(worker)
    start_dir = world["folder"] / "workers" / str(_decision(rule))
    assert launch["confinement"] == "review"
    assert launch["add_dirs"] == [str(world["staging"].resolve()), str(start_dir)]

    await settle_all()
    assert starts(rule)[0][0] == "returned"
    assert [kind for kind, _ in worker_events()] == ["worker.result"]
    assert not start_dir.exists()


# 2 -- a repeated request starts nothing, while queued and after its result returned.
@pytest.mark.asyncio
async def test_a_repeated_request_starts_nothing(world):
    rule = await make_rule()
    await request()
    await request()
    await settle_all()
    await request()
    await settle_all()
    assert [row[:2] for row in starts(rule)] == [
        ("returned", None), ("skipped", "duplicate request"), ("skipped", "duplicate request")]
    assert len(_workers()) == 1


# 8 -- a staging path outside the owner's staging folder refuses the start and tells the owner.
@pytest.mark.asyncio
async def test_a_staging_path_outside_the_folder_is_refused_and_the_owner_told(world):
    rule = await make_rule()
    await request(staging_path="../../..")
    await settle_all()
    [(outcome, reason, _identity, worker)] = starts(rule)
    assert outcome == "refused" and worker is None
    assert reason.startswith("staging_path ../../.. is outside your staging folder")
    assert _workers() == [] and [kind for kind, _ in worker_events()] == ["worker.no_result"]
    [wake] = owner_wakes()
    assert "couldn’t start worker-flow: staging_path ../../.. is outside your staging folder" in wake
    # The refusal released the identity: the same request with a good path starts.
    await request()
    await settle_all()
    assert [row[0] for row in starts(rule)] == ["refused", "returned"]


def _repo(tmp: Path) -> tuple[Path, str]:
    repo = tmp / "repo"
    (repo / ".claude").mkdir(parents=True)
    (repo / ".claude" / "settings.json").write_text("{}\n")
    (repo / "app.py").write_text("x = 1\n")
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"}
    for args in (["init", "-q"], ["add", "."], ["commit", "-qm", "c"]):
        subprocess.run(["git", *args], cwd=repo, env=env, check=True)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True,
                         check=True).stdout.strip()
    return repo, sha


def _register(world, repo: Path) -> None:
    from infra.database import write_transaction
    from infra.project_repo_policy import register_repo_policy

    with write_transaction(None, scope="rule_worker_repo") as db:
        register_repo_policy(world["project"], "app", repo, pinned_branch=None,
                             protect_base_checkout=False, db=db)


# 1 (head) -- the head tree comes from the object store into the start's own directory.
@pytest.mark.asyncio
async def test_the_head_tree_is_written_from_the_object_store_beside_never_above_the_cwd(world, tmp_path):
    from automation import worker_runner

    repo, sha = _repo(tmp_path)
    _register(world, repo)
    world["reply"].write_text("HANG")
    rule = await make_rule()
    await request(head_repo="app", head_sha=sha)
    assert await worker_runner.drive() == 1
    head = world["folder"] / "workers" / str(_decision(rule)) / "head"
    assert (head / "app.py").read_text() == "x = 1\n"
    worker = starts(rule)[0][3]
    cwd = Path(_launch(worker)["working_dir"])
    assert cwd.name == "confined" and cwd.is_dir()
    assert head not in cwd.parents and cwd != head and cwd not in head.parents


# 9 -- a head SHA not in the object store, or a head with no SHA, refuses the start.
@pytest.mark.asyncio
@pytest.mark.parametrize(("sha", "cause"), [
    ("0" * 40, "commit 000000000000 is not in app's object store"),
    (None, "head_repo needs head_sha"),
])
async def test_a_missing_head_sha_refuses_the_start(world, tmp_path, sha, cause):
    repo, _sha = _repo(tmp_path)
    _register(world, repo)
    rule = await make_rule()
    await request(head_repo="app", head_sha=sha)
    await settle_all()
    [(outcome, reason, _identity, _worker)] = starts(rule)
    assert outcome == "refused" and reason.startswith(cause)
    assert _workers() == [] and cause in owner_wakes()[0]


# 10 -- a paused owner's start is skipped, not replayed; a fresh request after resume starts.
@pytest.mark.asyncio
async def test_a_paused_owner_skips_and_a_request_after_resume_starts(world, monkeypatch):
    paused = {"on": True}
    monkeypatch.setattr("plans.public.thread_paused_since",
                        lambda _db, thread: "2026-01-01" if paused["on"] and thread == OWNER else None)
    rule = await make_rule()
    await request()
    await settle_all()
    assert starts(rule) == [("skipped", "owner paused", "pr-1@a1", None)]
    paused["on"] = False
    await settle_all()
    assert [row[0] for row in starts(rule)] == ["skipped"]  # no replay
    await request()
    await settle_all()
    assert [row[0] for row in starts(rule)] == ["skipped", "returned"]
    assert len(_workers()) == 1


# 12 -- a Local only owner's start runs local or waits: test_rule_workers_local_only.

# A Codex owner: Codex has no confined launch, so the start is refused and the owner told.
@pytest.mark.asyncio
async def test_an_owner_on_a_harness_without_a_confined_launch_is_refused(world):
    from infra.database import get_db, write_transaction

    other = get_db().execute("SELECT id FROM models WHERE harness='codex' AND available=1 "
                             "ORDER BY id LIMIT 1").fetchone()
    rule = await make_rule()
    with write_transaction(None, scope="rule_worker_model") as db:
        db.execute("UPDATE threads SET model=? WHERE id=?", (other[0], OWNER))
    await request()
    await settle_all()
    outcome, reason = starts(rule)[0][:2]
    assert outcome == "refused" and reason.startswith("Couldn't start this read-only agent on")
    assert _workers() == [] and "Couldn't start this read-only agent on" in owner_wakes()[0]


# 13 -- worker.* is CORSIA's alone: an agent's emit and a script or CLI line are refused.
@pytest.mark.asyncio
async def test_worker_events_from_anyone_but_corsia_are_refused(world):
    from events.intake import admit_event

    emitted = await atool(OWNER, operation="emit", event_type="worker.result",
                          facts={"run_id": "1", "result": "{}"})
    assert isinstance(emitted, str) and "reserved_type" in emitted
    for origin in ("emitter", "connector"):
        admitted = await admit_event(event_type="worker.no_result", facts={"run_id": "2"},
                                     origin=origin, producer="script:x", event_id=f"spoof-{origin}")
        assert admitted.refusal is not None and admitted.refusal.code == "reserved_type"
    assert worker_events() == []


# 14 -- create refuses a workflow with no result, one the writer can't read, and other actions' fields.
@pytest.mark.asyncio
@pytest.mark.parametrize(("extra", "said"), [
    ({"workflow_id": "nope"}, "nope isn't a workflow you can read"),
    ({"to": "self"}, "A start_worker rule takes no to"),
    ({"mode": "wake"}, "A start_worker rule takes no mode"),
    ({"note": "hi"}, "A start_worker rule takes no note"),
    ({"script_id": "s1"}, "A start_worker rule takes no script_id"),
    ({"request_identity_fact": "colour"}, "request_identity_fact names the"),
])
async def test_create_refuses_what_a_start_worker_rule_cannot_take(world, extra, said):
    args = {"workflow_id": "worker-flow", "request_identity_fact": "request", **extra}
    refused = await atool(OWNER, operation="create", connector="qa", event_type="request",
                          action="start_worker", match=[{"field": "request", "op": "present"}], **args)
    assert isinstance(refused, str) and said in refused


@pytest.mark.asyncio
async def test_create_refuses_a_workflow_that_declares_no_result(world):
    sidecar = world["root"] / "skills" / "worker-flow" / "corsia.yaml"
    sidecar.write_text("handler: {kind: instruction}\nworkflow:\n  steps:\n"
                       "    - {id: read, title: Read it, required: true, check: read}\n")
    refused = await atool(OWNER, operation="create", connector="qa", event_type="request",
                          action="start_worker", workflow_id="worker-flow", request_identity_fact="request",
                          match=[{"field": "request", "op": "present"}])
    assert isinstance(refused, str) and "declares no result" in refused
