from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch


def test_rerun_failed_jobs_uses_the_narrow_actions_endpoint():
    """A future positively identified infrastructure retry cannot rerun green jobs."""
    from reviewer.gh_reconciliation import rerun_failed_jobs

    with patch("reviewer.gh_api.run_gh", return_value=SimpleNamespace(returncode=0, stderr="")) as run_gh:
        assert rerun_failed_jobs("owner/repo", 99)

    run_gh.assert_called_once_with(
        ["api", "--method", "POST", "repos/owner/repo/actions/runs/99/rerun-failed-jobs"],
        timeout=30.0,
    )


def test_required_contexts_read_the_live_top_level_branch_rules_array():
    """GitHub branch-rules responses expose rules directly, not under ``rules``."""
    from reviewer.gh_reconciliation import get_branch_required_contexts

    response = [{
        "type": "required_status_checks",
        "parameters": {"required_status_checks": [{"context": "Backend unit tests"}]},
    }]
    with patch("reviewer.gh_reconciliation._json_api", return_value=response):
        assert get_branch_required_contexts("owner/repo", "dev") == {"Backend unit tests"}
