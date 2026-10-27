"""Outcome drafts for completed CI jobs and check runs.

``github_outcomes`` hands a ``workflow_job`` or ``check_run`` ``completed`` payload here.
A job is keyed on its id alone: GitHub gives each rerun attempt new jobs, so an attempt
is a distinct event and a redelivery of the same job is the same one. A check run is keyed
on its id plus ``completed_at``, the way ``check_suite`` is keyed on its ``updated_at``.
Names, ids, counts, timestamps and a conclusion only: no log, step or output text.
"""
from __future__ import annotations

from typing import Any

from events.connectors import github_outcome_input as shape
from events.connectors import github_outcome_measurements as measurements
from events.connectors.github_outcomes import OutcomeDraft

# The attribute allowlist's bound on the one text that stands in for a label list.
RUNNER_LABELS_MAX_CHARS = 200


def runner_labels_text(labels: Any) -> str | None:
    """Sorted, comma-joined runner labels cut at whole labels to fit the bound; None when empty."""
    names = sorted({shape.text(label) for label in shape.members(labels)})
    kept = ""
    for name in names:
        joined = f"{kept},{name}" if kept else name
        if len(joined) > RUNNER_LABELS_MAX_CHARS:
            break
        kept = joined
    return kept or None


def workflow_job_drafts(payload: dict[str, Any], repo: str) -> list[OutcomeDraft]:
    """One completed job: queue time is ``started_at - created_at`` when both are usable."""
    job = shape.mapping(shape.required(payload, "workflow_job"))
    job_id = shape.number(shape.required(job, "id"))
    head_sha = shape.text(shape.required(job, "head_sha"))
    completed = shape.timestamp(shape.required(job, "completed_at"))
    attrs: dict[str, Any] = {
        "repo": repo, "run_id": str(shape.number(shape.required(job, "run_id"))),
        "job_id": str(job_id), "run_attempt": shape.number(shape.required(job, "run_attempt")),
        "name": shape.text(shape.required(job, "name")), "head_sha": head_sha,
        "conclusion": job.get("conclusion", "none"),
    }
    if job.get("workflow_name") is not None:
        attrs["workflow_name"] = shape.text(job["workflow_name"])
    if job.get("labels") is not None:
        labels = runner_labels_text(job["labels"])
        if labels is not None:
            attrs["runner_labels"] = labels
    queued = measurements.seconds_between(job.get("created_at"), job.get("started_at"))
    if queued is not None:
        attrs["queue_seconds"] = queued
    return [OutcomeDraft(
        f"github:workflow_job:{repo}:{job_id}", "github.workflow_job.completed",
        completed, f"github:commit:{repo}@{head_sha}", attrs,
    )]


def check_run_drafts(payload: dict[str, Any], repo: str) -> list[OutcomeDraft]:
    """One completed check run, with the times GitHub reported for it."""
    run = shape.mapping(shape.required(payload, "check_run"))
    run_id = shape.number(shape.required(run, "id"))
    head_sha = shape.text(shape.required(run, "head_sha"))
    completed = shape.timestamp(shape.required(run, "completed_at"))
    attrs: dict[str, Any] = {
        "repo": repo, "check_run_id": str(run_id),
        "name": shape.text(shape.required(run, "name")), "head_sha": head_sha,
        "conclusion": run.get("conclusion", "none"), "completed_at": completed,
    }
    if run.get("started_at") is not None:
        attrs["started_at"] = shape.timestamp(run["started_at"])
    return [OutcomeDraft(
        f"github:check_run:{repo}:{run_id}:completed:{completed}",
        "github.check_run.completed", completed,
        f"github:commit:{repo}@{head_sha}", attrs,
    )]
