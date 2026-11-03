"""Which quiet-PR wakes are due for one head, and what each one says.

Pure decisions for ``events.connectors.github_pr_quiet_watch``: GitHub's own record of the head
(the pull request and its Actions workflow runs) plus the instance's delivery
log decide, per reason, whether an expected event is overdue. Whether a CI
event was lost is read from the log directly, per workflow run: GitHub finished
the run's latest attempt and the log holds no completed delivery for its check
suite since that attempt started. Each reason fires at most once per
``(PR, head)``, and a CI reason once per CI attempt (``fired_marker``); the
watcher records that durably.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from reviewer.reviewer_categories import STATUS_CHECK_CONTEXT, STATUS_PRESENCE_CONTEXT

#: ``-present`` states that end the reviewer's work on a head: ``success`` once it was
#: reviewed, ``failure`` once ``qa/override-reviewer`` skipped it (``reviewer_gate``).
REVIEWER_DONE = frozenset({"success", "failure"})

CI_EVENT_MISSING = "ci_event_missing"
REVIEW_MISSING = "review_missing"
CI_NOT_STARTED = "ci_not_started"
CI_CANCELLED = "ci_cancelled"

#: A check-suite webhook arrives within seconds; 10 min is the grace for one.
#: It is also how long a cancelled run may wait for a rerun or a successor.
CI_EVENT_GRACE = timedelta(minutes=10)
#: Measured CI-done to reviewer-verdict maximum is 39 min (design §1.3).
REVIEW_GRACE = timedelta(minutes=45)
CI_START_GRACE = timedelta(minutes=30)
#: Allowance for comparing GitHub's clock with this host's when judging a
#: receipt (``run_started_at`` and the coverage floor against ``created_at``, both
#: at 1 s precision). 60 s covers truncation and any sane host skew, and stays
#: well under the minutes between one attempt's completion and a rerun's start:
#: the ``ci_cancelled`` wake that prompts a rerun comes 10 min after the cancel.
CLOCK_SLACK = timedelta(seconds=60)

_GREEN = frozenset({"success", "neutral", "skipped"})
_CANCELLED = "cancelled"
_REVIEWER_TERMINAL = frozenset({"success", "failure", "error"})


@dataclass(frozen=True, slots=True)
class PrView:
    """The open pull request as ``GET pulls/{n}`` reports it."""

    repo: str
    number: int
    head: str
    draft: bool
    mergeable: str
    url: str
    base_ref: str = ""

    @property
    def conflicting(self) -> bool:
        return self.mergeable == "CONFLICTING"


@dataclass(frozen=True, slots=True)
class CiState:
    """The head's latest run of each Actions workflow: started, and finished at
    ``completed_at``. ``cancelled`` names the ones cancelled with no rerun or
    successor, as ``(workflow name, run id)``. ``suites`` is each finished run's
    ``(check suite id, latest attempt started at)``; ``attempt`` fingerprints
    them, so a rerun or a new run is a new CI attempt.
    """

    started: bool
    completed_at: datetime | None = None
    conclusion: str | None = None
    cancelled: tuple[tuple[str, int], ...] = ()
    suites: tuple[tuple[int, datetime], ...] = ()
    attempt: str = ""


@dataclass(frozen=True, slots=True)
class HeadFacts:
    """What this instance received for the head, and what GitHub says of its reviewer.

    ``ci_received_at`` is when the log recorded the last of the head's CI
    completions (each run's suite, since its latest attempt started), or None
    while any is missing. ``log_answers_from`` is the earliest time a CI event
    could have been logged with its suite: after the webhook was configured and
    after the log's last pre-column check-suite row. CI finished before it is
    unknown; after it, a missing delivery is lost, even if the log never
    received a check suite at all. ``review_required`` is whether the base
    branch's merge gate requires ``qa/llm-reviewer-present`` (the repo runs the
    reviewer), and ``reviewer_statuses`` the head's ``qa/llm-reviewer*`` contexts
    and states; both come from GitHub once the head is green and reviewable,
    None meaning not read yet.
    """

    webhook: bool
    ci_received_at: datetime | None
    log_answers_from: datetime | None
    review_required: bool | None = None
    reviewer_statuses: dict[str, str] | None = None


def mergeable_label(value: Any) -> str:
    """REST ``mergeable`` (true, false, null) in the GraphQL words the wording uses."""
    if value is True:
        return "MERGEABLE"
    if value is False:
        return "CONFLICTING"
    return "UNKNOWN"


def parse_time(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _latest_per_workflow(runs: list[dict]) -> list[dict]:
    """A rerun keeps its run; a superseding run of the same workflow and trigger
    is newer. Runs of one workflow from different triggers (``push`` and
    ``pull_request`` on one head) run side by side, so neither hides the other.
    """
    latest: dict[Any, dict] = {}
    for run in runs:
        key = (run.get("workflow_id"), run.get("event"))
        if key not in latest or (run.get("id") or 0) > (latest[key].get("id") or 0):
            latest[key] = run
    return list(latest.values())


def ci_state(body: Any) -> CiState:
    """Summarize ``GET actions/runs?head_sha=H`` by each workflow's latest run.

    A cancelled run superseded by a newer run of its workflow drops out; one
    with no successor is a non-green ``cancelled`` result, never a pass.
    """
    runs = body.get("workflow_runs") if isinstance(body, dict) else None
    latest = _latest_per_workflow([run for run in runs or [] if isinstance(run, dict)])
    if not latest:
        return CiState(started=False)
    if any(run.get("status") != "completed" for run in latest):
        return CiState(started=True)
    times = [parse_time(run.get("updated_at")) for run in latest]
    starts = [parse_time(run.get("run_started_at")) for run in latest]
    suite_ids = [run.get("check_suite_id") for run in latest]
    if None in times or None in starts or not all(isinstance(s, int) for s in suite_ids):
        return CiState(started=True)
    conclusions = {str(run.get("conclusion")) for run in latest}
    failed = sorted(c for c in conclusions if c not in _GREEN and c != _CANCELLED)
    conclusion = failed[0] if failed else _CANCELLED if _CANCELLED in conclusions else "success"
    cancelled = tuple(
        (str(run.get("name") or "workflow"), int(run.get("id") or 0))
        for run in latest if run.get("conclusion") == _CANCELLED
    )
    suites = tuple(sorted(
        (int(run["check_suite_id"]), start)
        for run, start in zip(latest, starts) if start is not None
    ))
    attempt = hashlib.sha256(",".join(
        sorted(f"{run['check_suite_id']}.{run.get('run_attempt') or 1}" for run in latest)
    ).encode()).hexdigest()[:8]
    return CiState(
        True, max(t for t in times if t is not None), conclusion, cancelled, suites, attempt,
    )


def fired_marker(reason: str, ci: CiState) -> str:
    """What a fired reason is recorded as: a CI reason per CI attempt, so a
    rerun that is cancelled or lost again wakes again; any other per head.
    """
    return f"{reason}@{ci.attempt}" if reason in (CI_EVENT_MISSING, CI_CANCELLED) else reason


def _ci_event_lost(ci: CiState, facts: HeadFacts) -> bool:
    """GitHub finished CI for the head, the log can judge it, and holds no delivery."""
    return (
        ci.completed_at is not None and facts.webhook and facts.ci_received_at is None
        and facts.log_answers_from is not None
        and ci.completed_at >= facts.log_answers_from + CLOCK_SLACK
    )


def review_possible(view: PrView, ci: CiState) -> bool:
    """The head's CI finished green and the head is reviewable (not held)."""
    return (
        ci.completed_at is not None and ci.conclusion == "success"
        and not view.draft and not view.conflicting
    )


def _review_expected(view: PrView, ci: CiState, facts: HeadFacts) -> bool:
    """A green, reviewable head the merge gate requires reviewed and GitHub does
    not show reviewed or override-skipped. Until GitHub is read it is expected, so the head does not
    settle early. A head whose CI event was lost is the CI wake's, not the reviewer's.
    """
    if not review_possible(view, ci) or facts.review_required is False:
        return False
    if _ci_event_lost(ci, facts):
        return False
    if facts.review_required is None or facts.reviewer_statuses is None:
        return True
    return facts.reviewer_statuses.get(STATUS_PRESENCE_CONTEXT) not in REVIEWER_DONE


def review_clock_due(
    view: PrView, ci: CiState, *, now: datetime, review_from: datetime | None = None,
) -> bool:
    """The review grace has passed since CI finished, or since a held head became
    reviewable if later.
    """
    if not review_possible(view, ci) or ci.completed_at is None:
        return False
    return now - max(ci.completed_at, review_from or ci.completed_at) >= REVIEW_GRACE


def review_required(body: Any) -> bool:
    """Whether ``GET rules/branches/{ref}`` requires ``qa/llm-reviewer-present``."""
    for rule in body if isinstance(body, list) else []:
        if isinstance(rule, dict) and rule.get("type") == "required_status_checks":
            checks = (rule.get("parameters") or {}).get("required_status_checks") or []
            if any(c.get("context") == STATUS_PRESENCE_CONTEXT for c in checks):
                return True
    return False


def reviewer_statuses(body: Any) -> dict[str, str]:
    """The head's ``qa/llm-reviewer*`` contexts from ``GET commits/{head}/status``."""
    statuses = body.get("statuses") if isinstance(body, dict) else None
    return {
        str(s.get("context")): str(s.get("state")) for s in statuses or []
        if str(s.get("context")).startswith(STATUS_CHECK_CONTEXT)
    }


def due_reasons(
    view: PrView, ci: CiState, facts: HeadFacts, *,
    fired: list[str], first_seen: datetime, now: datetime, review_from: datetime | None = None,
) -> list[str]:
    """The reasons overdue now for this head (and CI attempt) not fired yet.

    The reviewer's clock starts when the head's CI finished (``ci.completed_at``,
    from GitHub's workflow runs), or at ``review_from`` if later: when a held head
    (draft or conflicting) became reviewable. It needs no webhook.
    """
    due: list[str] = []
    if not ci.started:
        if now - first_seen >= CI_START_GRACE:
            due.append(CI_NOT_STARTED)
    elif ci.completed_at is not None:
        ci_overdue = now - ci.completed_at >= CI_EVENT_GRACE
        if _ci_event_lost(ci, facts):
            if ci_overdue:
                due.append(CI_EVENT_MISSING)
        elif ci.conclusion == _CANCELLED and ci_overdue:
            due.append(CI_CANCELLED)
        if (
            facts.review_required is not None and facts.reviewer_statuses is not None
            and _review_expected(view, ci, facts)
            and review_clock_due(view, ci, now=now, review_from=review_from)
        ):
            due.append(REVIEW_MISSING)
    return [reason for reason in due if fired_marker(reason, ci) not in fired]


def settled(view: PrView, ci: CiState, facts: HeadFacts, fired: list[str]) -> bool:
    """Every event expected for this head arrived or was already woken for.

    A settled head costs one ``pulls/{n}`` read per sweep until the head, its
    draft flag or its conflict state changes. Only a green head settles: a red
    or cancelled one keeps its runs read, since a rerun on the same head is a
    new CI attempt whose event can be lost and whose pass arms the reviewer.
    """
    if ci.completed_at is None or ci.conclusion != "success":
        return False
    lost_ok = not _ci_event_lost(ci, facts) or fired_marker(CI_EVENT_MISSING, ci) in fired
    review_ok = not _review_expected(view, ci, facts) or REVIEW_MISSING in fired
    return lost_ok and review_ok


def reviewer_finished(reviewer_state: str | None) -> bool:
    return reviewer_state in _REVIEWER_TERMINAL


def summary(
    view: PrView, reason: str, ci: CiState, now: datetime,
    reviewer_state: str | None = None,
) -> str:
    """The weak-model-readable wake text (design §4)."""
    pr, h7 = f"PR {view.repo}#{view.number}", view.head[:7]
    checks = f"`gh pr checks {view.number}`"
    minutes = int((now - (ci.completed_at or now)).total_seconds() // 60)
    if reason == CI_EVENT_MISSING:
        return (
            f"{pr}: CI finished for head {h7} ({ci.conclusion}), but the CI event never "
            f"reached CORSIA. Treat this as your CI result. Run {checks} once for details, "
            "then continue. CORSIA has reported the delivery fault; do not report it yourself."
        )
    if reason == CI_CANCELLED:
        runs = ", ".join(f"{name} (run {run_id})" for name, run_id in ci.cancelled)
        return (
            f"{pr}: CI was cancelled for head {h7} {minutes} min ago and nothing reran it: "
            f"{runs}. A timeout or a manual cancel stopped it, so this head has no CI result. "
            "Rerun it once with `gh run rerun <run id> --failed`, or fix what hung and push. "
            "Then end your turn: CORSIA wakes you when the rerun finishes. Do not poll."
        )
    if reason == REVIEW_MISSING and reviewer_finished(reviewer_state):
        return (
            f"{pr}: the reviewer finished head {h7} ({reviewer_state}), but its verdict never "
            f"reached you. Run {checks} once and act on the {STATUS_CHECK_CONTEXT} result."
        )
    if reason == REVIEW_MISSING:
        return (
            f"{pr}: CI passed for head {h7} {minutes} min ago, but the reviewer has not "
            "finished. CORSIA has reported it. Do other work; do not poll."
        )
    if view.conflicting:
        return (
            f"{pr}: no CI has started for head {h7} in 30 min because the PR has merge "
            "conflicts. Update the branch from its base and push. CI and review start after that."
        )
    return (
        f"{pr}: no CI has started for head {h7} in 30 min. Run "
        f"`gh pr view {view.number} --json mergeable,statusCheckRollup` once. If nothing "
        "explains it, tell your parent or the user."
    )
