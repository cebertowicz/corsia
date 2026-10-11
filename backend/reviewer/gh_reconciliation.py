"""GitHub REST reads and Actions reruns used only by reviewer reconciliation."""

from __future__ import annotations

import json
import logging
import re
import subprocess
from typing import Any

from reviewer.gh_helpers import _check_run_recency, _run_gh

log = logging.getLogger(__name__)


def _json_api(args: list[str]) -> Any | None:
    """Read one REST JSON response, reporting malformed data as unreadable."""
    out = _run_gh(args, timeout=60.0)
    if out is None:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        log.warning("Failed to parse GitHub API JSON for %s", args[-1])
        return None


def get_open_pull_requests(repo: str, base_ref: str) -> list[dict[str, Any]] | None:
    """Return open pull requests targeting one base branch."""
    data = _json_api([
        "api", f"repos/{repo}/pulls?state=open&base={base_ref}&per_page=100",
        "--paginate", "--slurp",
    ])
    if not isinstance(data, list):
        return None
    pages = data if all(isinstance(page, list) for page in data) else [data]
    pull_requests: list[dict[str, Any]] = []
    for page in pages:
        for pull_request in page:
            if not isinstance(pull_request, dict):
                log.warning("Unexpected open PR entry for %s", repo)
                return None
            number = pull_request.get("number")
            head = pull_request.get("head")
            if not isinstance(number, int) or not isinstance(head, dict):
                log.warning("Open PR missing number or head for %s", repo)
                return None
            head_sha = head.get("sha")
            if not isinstance(head_sha, str) or not head_sha:
                log.warning("Open PR missing head SHA for %s#%d", repo, number)
                return None
            pull_requests.append({"number": number, "head_sha": head_sha})
    return pull_requests


def get_branch_required_contexts(repo: str, branch: str) -> set[str] | None:
    """Return branch-rule required status-check contexts without hard-coding them.

    GitHub's branch-rules endpoint returns a top-level rule array, not a
    ruleset envelope. An unreadable or shape-mismatched response stays
    distinct from a readable branch that simply has no required contexts.
    """
    data = _json_api(["api", f"repos/{repo}/rules/branches/{branch}"])
    if not isinstance(data, list):
        log.warning("Branch rules unreadable for %s@%s", repo, branch)
        return None
    contexts: set[str] = set()
    for rule in data:
        if not isinstance(rule, dict) or rule.get("type") != "required_status_checks":
            continue
        parameters = rule.get("parameters")
        if not isinstance(parameters, dict):
            continue
        for check in parameters.get("required_status_checks") or []:
            if isinstance(check, dict) and isinstance(check.get("context"), str):
                contexts.add(check["context"])
    return contexts


def get_check_runs(repo: str, head_sha: str) -> list[dict[str, Any]] | None:
    """Return the newest raw check run for each check name on a commit."""
    out = _run_gh([
        "api", f"repos/{repo}/commits/{head_sha}/check-runs", "--paginate", "--jq", ".check_runs[]",
    ], timeout=60.0)
    if out is None:
        return None
    runs: list[dict[str, Any]] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            log.warning("Failed to parse check-run entry for %s@%s", repo, head_sha[:8])
            return None
        if not isinstance(item, dict):
            return None
        runs.append(item)
    newest: dict[str, dict[str, Any]] = {}
    for run in runs:
        name = str(run.get("name") or "")
        previous = newest.get(name)
        if previous is None or _check_run_recency(run) > _check_run_recency(previous):
            newest[name] = run
    return list(newest.values())


def get_workflow_run_for_check(repo: str, check: dict[str, Any]) -> dict[str, Any] | None:
    """Resolve one Actions workflow run plus its job/annotation cancellation detail."""
    details_url = str(check.get("details_url") or "")
    match = re.search(r"/actions/runs/(\d+)", details_url)
    if match is None:
        log.warning("Check run %s has no Actions run URL", check.get("name"))
        return None
    run_id = match.group(1)
    data = _json_api(["api", f"repos/{repo}/actions/runs/{run_id}"])
    if not isinstance(data, dict):
        return None
    jobs = _json_api(["api", f"repos/{repo}/actions/runs/{run_id}/jobs?filter=latest&per_page=100"])
    if not isinstance(jobs, dict) or not isinstance(jobs.get("jobs"), list):
        return None
    check_id = check.get("id")
    annotations: list[Any] = []
    if isinstance(check_id, int):
        raw_annotations = _json_api([
            "api", f"repos/{repo}/check-runs/{check_id}/annotations", "--paginate", "--slurp",
        ])
        if not isinstance(raw_annotations, list):
            return None
        annotations = [annotation for page in raw_annotations for annotation in (
            page if isinstance(page, list) else [page]
        ) if isinstance(annotation, dict)]
    data["jobs"] = jobs["jobs"]
    data["annotations"] = annotations
    data["job_name"] = next(
        (str(job.get("name")) for job in jobs["jobs"] if isinstance(job, dict) and job.get("name")),
        str(data.get("name") or "required check"),
    )
    return data


def rerun_failed_jobs(repo: str, run_id: int) -> bool:
    """Ask GitHub Actions to re-run only failed jobs from one workflow run."""
    from reviewer.gh_api import run_gh

    try:
        result = run_gh(
            ["api", "--method", "POST", f"repos/{repo}/actions/runs/{run_id}/rerun-failed-jobs"], timeout=30.0,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
        log.warning("Failed-job rerun request failed for %s run %s: %s", repo, run_id, type(exc).__name__)
        return False
    if result.returncode != 0:
        log.warning("Failed-job rerun request failed for %s run %s: %s", repo, run_id, result.stderr[:200])
        return False
    return True
