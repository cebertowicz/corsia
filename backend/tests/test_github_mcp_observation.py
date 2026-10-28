"""GitHub MCP observation using the upstream tool result's created-object field."""

import json

import pytest

from events.connectors import app_matches, artifact_extractor
from events.identity import PR_TYPE
from infra.webhook_delivery_log import object_key_for
from tools.mcp_stamp import app_identity


# Constructed examples of legacy content JSON emitted by GitHub MCP server
# b98d115bbb080e9555466a96656ec4df605aea21:
# pkg/github/pullrequests.go CreatePullRequest and pkg/github/issues.go AddIssueComment.
# Both marshal MinimalResponse {id, url}; the combined comment/reaction nests comment.
CREATE_PR = {"id": "123456", "url": "https://github.com/example/repo/pull/42"}
PR_COMMENT = {"id": "987", "url": "https://github.com/example/repo/pull/42#issuecomment-987"}
ISSUE_COMMENT = {"id": "987", "url": "https://github.com/example/repo/issues/42#issuecomment-987"}


def extract(value):
    return artifact_extractor("github", PR_TYPE)(json.dumps(value))


def test_created_pr_and_comment_use_the_created_objects_url():
    pr = extract(CREATE_PR)
    comment = extract(PR_COMMENT)
    assert (pr.external_id, pr.created_event_ids) == ("example/repo#42", ())
    assert (comment.external_id, comment.created_event_ids) == (
        "example/repo#42", (object_key_for("issue_comment", {"action": "created", "comment": {"id": 987}}),)
    )
    assert extract({"comment": PR_COMMENT, "reaction": {"id": "555", "url": "elsewhere"}}) == comment


@pytest.mark.parametrize("value", [
    ISSUE_COMMENT,
    {"comment": {"id": "987", "url": "https://github.com/example/repo/pull/42"}},
    {"id": "7", "url": "https://github.com/example/repo/issues/42"},
    {"id": "987", "url": "https://github.com/example/repo/pull/42#issuecomment-988"},
    {"id": "987", "url": "https://github.com/example/repo/pull/42#discussion_r987"},
    {"id": "987", "url": "https://github.com/example/repo/pull/42/extra"},
    {"id": "987", "body": "https://github.com/example/repo/pull/42"},
    {"id": True, "url": "https://github.com/example/repo/pull/42"},
    [],
])
def test_other_results_do_not_name_a_pr(value):
    assert extract(value) is None


def test_shell_url_line_still_works_and_review_text_has_no_artifact():
    extractor = artifact_extractor("github", PR_TYPE)
    assert extractor("https://github.com/example/repo/pull/42\n").external_id == "example/repo#42"
    assert extractor("pull request review submitted successfully") is None
    assert extractor("Exit code: 1\nhttps://github.com/example/repo/pull/42\n") is None


@pytest.mark.parametrize("config", [
    {"url": "https://api.githubcopilot.com/mcp/"},
    {"command": "docker", "args": ["run", "-i", "--rm", "-e", "GITHUB_PERSONAL_ACCESS_TOKEN",
                                    "ghcr.io/github/github-mcp-server"]},
])
def test_official_github_server_configs_match_stamps(config):
    # README.md at the commit above documents both configurations.
    assert app_matches(app_identity(config), "github")
    assert not app_matches(app_identity(config), "gmail")
    assert not app_matches(None, "github")
    assert not app_matches(app_identity(config), "unknown")
