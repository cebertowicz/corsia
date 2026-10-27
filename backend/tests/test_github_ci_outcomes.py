"""Completed CI jobs and check runs become one outcome event each, through real admission.

Fixtures are GitHub's published ``workflow_job`` and ``check_run`` webhook examples,
trimmed to the fields the mapper reads. The example job carries one attempt only, so
the rerun probes below are built by changing that payload as GitHub documents a rerun:
a new job id and a higher ``run_attempt``.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

import events.connectors.github_webhook as webhook_module
from events.connectors import github_outcome_input as shape
from events.connectors.github_admission import admit_github_delivery_async
from events.connectors.github_ci_outcomes import RUNNER_LABELS_MAX_CHARS
from events.connectors.github_outcomes import map_github_outcomes
from infra import drain
from infra.outcome_event_attributes import ATTRIBUTE_ALLOWLISTS, filter_attributes

FIXTURES = Path(__file__).parent / "fixtures/github_outcomes"
REPO = "codertocat/hello-world"


@pytest.fixture(autouse=True)
def _unrouted_scheduler(monkeypatch):
    """Neither type routes anywhere, so the scheduler leg is a no-op that settles the delivery."""
    monkeypatch.setattr(drain, "_turn_admissions", 0)
    monkeypatch.setattr(webhook_module, "schedule_github_webhook", lambda *a, **k: None)


def delivery(name: str) -> tuple[str, dict[str, Any]]:
    recorded = json.loads((FIXTURES / f"{name}.json").read_text())
    return recorded["event"], copy.deepcopy(recorded["payload"])


def job(**changes: Any) -> dict[str, Any]:
    _, payload = delivery("workflow_job_success")
    payload["workflow_job"].update(changes)
    return payload


def events(db) -> list[tuple[str, str, dict[str, Any]]]:
    rows = db.execute("SELECT event_name, dedupe_key, attributes FROM outcome_events ORDER BY dedupe_key")
    return [(name, key, json.loads(attributes)) for name, key, attributes in rows]


def counters(db) -> dict[tuple[str, str], int]:
    return {(r[0], r[1]): r[2] for r in db.execute("SELECT counter, event_name, n FROM outcome_event_counters")}


async def admit(event_type: str, payload: dict[str, Any], delivery_id: str):
    return await admit_github_delivery_async(event_type, payload, delivery_id)


async def test_a_completed_job_is_one_event_with_its_facts_and_queue_time(main_db):
    event_type, payload = delivery("workflow_job_success")

    outcome = await admit(event_type, payload, "d-job")

    assert outcome.content["status"] == "ignored", "no routed event: the fact is the whole effect"
    ((name, key, attrs),) = events(main_db)
    assert (name, key) == ("github.workflow_job.completed", f"github:workflow_job:{REPO}:289782451")
    assert attrs == {
        "repo": REPO, "run_id": "2202229078", "job_id": "289782451", "run_attempt": 1,
        "name": "linters", "workflow_name": "CodeQL", "runner_labels": "ubuntu-latest",
        "head_sha": "3484a3fb816e0859fd6e1cea078d76385ff50625", "conclusion": "success",
        "queue_seconds": 60,
    }
    assert counters(main_db) == {}, "every fact passed the allowlist"
    (draft,) = map_github_outcomes(event_type, payload)
    assert draft.ts == "2021-08-05T10:38:16Z"
    assert draft.subject_key == f"github:commit:{REPO}@3484a3fb816e0859fd6e1cea078d76385ff50625"


@pytest.mark.parametrize("changes", [
    {"started_at": None},
    {"created_at": None},
    {"started_at": "2021-08-05T10:33:57Z"},
])
async def test_queue_seconds_is_absent_when_a_time_is_missing_or_end_precedes_start(main_db, changes):
    await admit("workflow_job", job(**changes), "d-queue")

    ((_, _, attrs),) = events(main_db)
    assert "queue_seconds" not in attrs
    assert attrs["conclusion"] == "success", "the rest of the job is still recorded"


async def test_a_job_that_started_the_moment_it_was_created_queued_zero_seconds(main_db):
    await admit("workflow_job", job(started_at="2021-08-05T10:33:58Z"), "d-zero")

    ((_, _, attrs),) = events(main_db)
    assert attrs["queue_seconds"] == 0


async def test_a_rerun_attempt_is_a_distinct_event_and_a_redelivery_is_not(main_db):
    first = job()
    rerun = job(id=289790000, run_attempt=2, conclusion="failure")

    await admit("workflow_job", first, "d-first")
    await admit("workflow_job", rerun, "d-rerun")
    again = await admit("workflow_job", first, "d-first")
    redelivered = await admit("workflow_job", first, "d-redelivered")

    assert again.content["status"] == "duplicate", "the same delivery id is the same delivery"
    assert redelivered.content["status"] == "ignored", "a new delivery of a recorded job is admitted"
    rows = events(main_db)
    assert [(key.rsplit(":", 1)[1], attrs["run_attempt"], attrs["conclusion"]) for _, key, attrs in rows] == [
        ("289782451", 1, "success"), ("289790000", 2, "failure"),
    ]
    assert counters(main_db) == {("duplicate_ignored", "github.workflow_job.completed"): 1}


@pytest.mark.parametrize("labels,expected", [
    (["self-hosted", "macOS", "arm64"], "arm64,macOS,self-hosted"),
    (["b", "a", "b"], "a,b"),
    ([], None),
])
async def test_runner_labels_are_one_sorted_comma_joined_text(main_db, labels, expected):
    await admit("workflow_job", job(labels=labels), "d-labels")

    ((_, _, attrs),) = events(main_db)
    assert attrs.get("runner_labels") == expected


async def test_runner_labels_are_cut_at_whole_labels_inside_the_bound(main_db):
    labels = [f"label-{n:03d}-{'x' * 20}" for n in range(40)]

    await admit("workflow_job", job(labels=labels), "d-many-labels")

    ((_, _, attrs),) = events(main_db)
    text = attrs["runner_labels"]
    assert len(text) <= RUNNER_LABELS_MAX_CHARS
    assert text.split(",") == sorted(labels)[: len(text.split(","))], "whole labels from the sorted front"
    assert len(text.split(",")) > 1
    assert counters(main_db) == {}, "the bound is applied before the allowlist, so nothing is dropped"


async def test_a_job_without_labels_or_workflow_name_still_records_the_job(main_db):
    payload = job()
    del payload["workflow_job"]["labels"], payload["workflow_job"]["workflow_name"]

    await admit("workflow_job", payload, "d-bare")

    ((_, _, attrs),) = events(main_db)
    assert "runner_labels" not in attrs and "workflow_name" not in attrs and attrs["queue_seconds"] == 60


@pytest.mark.parametrize("action", ["queued", "in_progress", "waiting"])
async def test_a_job_action_other_than_completed_writes_nothing(main_db, action):
    payload = job()
    payload["action"] = action

    outcome = await admit("workflow_job", payload, "d-other")

    assert outcome.content["status"] == "ignored"
    assert events(main_db) == [] and counters(main_db) == {}
    assert map_github_outcomes("workflow_job", payload) == []


@pytest.mark.parametrize("field", ["id", "run_id", "run_attempt", "head_sha", "name", "completed_at"])
async def test_a_job_missing_a_consumed_field_is_refused_counted_and_recorded(main_db, field):
    payload = job()
    del payload["workflow_job"][field]
    with pytest.raises(shape.PayloadRefusal):
        map_github_outcomes("workflow_job", payload)

    await admit("workflow_job", payload, "d-refused")

    assert events(main_db) == []
    assert counters(main_db) == {("mapping_failed", "workflow_job"): 1}


async def test_a_job_with_a_malformed_time_is_refused_not_given_a_guessed_queue_time(main_db):
    await admit("workflow_job", job(started_at="2021-08-05"), "d-bad-time")

    assert events(main_db) == []
    assert counters(main_db) == {("mapping_failed", "workflow_job"): 1}


async def test_a_completed_check_run_is_one_event_with_its_times(main_db):
    event_type, payload = delivery("check_run_completed")

    outcome = await admit(event_type, payload, "d-check")

    assert outcome.content["status"] == "ignored"
    ((name, key, attrs),) = events(main_db)
    assert (name, key) == (
        "github.check_run.completed", f"github:check_run:{REPO}:128620228:completed:2019-05-15T15:21:12Z",
    )
    assert attrs == {
        "repo": REPO, "check_run_id": "128620228", "name": "Octocoders-linter",
        "head_sha": "ec26c3e57ca3a959ca5aad62de7213c562f8c821", "conclusion": "success",
        "started_at": "2019-05-15T15:21:12Z", "completed_at": "2019-05-15T15:21:12Z",
    }
    assert counters(main_db) == {}


async def test_a_check_run_completing_again_is_a_new_event_and_a_redelivery_is_not(main_db):
    _, first = delivery("check_run_completed")
    _, rerun = delivery("check_run_completed")
    rerun["check_run"].update(completed_at="2019-05-15T15:30:00Z", conclusion="failure")

    await admit("check_run", first, "d-run-1")
    await admit("check_run", rerun, "d-run-2")
    await admit("check_run", first, "d-run-3")

    assert [attrs["conclusion"] for _, _, attrs in events(main_db)] == ["success", "failure"]


async def test_a_check_run_without_a_start_time_keeps_the_rest(main_db):
    _, payload = delivery("check_run_completed")
    del payload["check_run"]["started_at"]

    await admit("check_run", payload, "d-no-start")

    ((_, _, attrs),) = events(main_db)
    assert "started_at" not in attrs and attrs["completed_at"] == "2019-05-15T15:21:12Z"


@pytest.mark.parametrize("action", ["created", "rerequested", "requested_action"])
async def test_a_check_run_action_other_than_completed_writes_nothing(main_db, action):
    _, payload = delivery("check_run_completed")
    payload["action"] = action

    outcome = await admit("check_run", payload, "d-check-other")

    assert outcome.content["status"] == "ignored"
    assert events(main_db) == [] and counters(main_db) == {}


async def test_a_check_run_without_completed_at_is_refused_and_counted(main_db):
    _, payload = delivery("check_run_completed")
    del payload["check_run"]["completed_at"]

    await admit("check_run", payload, "d-no-end")

    assert events(main_db) == []
    assert counters(main_db) == {("mapping_failed", "check_run"): 1}


def test_every_fact_the_mappers_emit_is_on_the_allowlist():
    for name in ("workflow_job_success", "check_run_completed"):
        event_type, payload = delivery(name)
        for draft in map_github_outcomes(event_type, payload):
            kept, dropped = filter_attributes(draft.event_name, draft.attributes)
            assert dropped == [] and kept == draft.attributes
            assert draft.event_name in ATTRIBUTE_ALLOWLISTS
    assert "queue_seconds" not in ATTRIBUTE_ALLOWLISTS["github.workflow_run.completed"], (
        "a workflow run's own queue span is always zero, so no run row carries one"
    )
