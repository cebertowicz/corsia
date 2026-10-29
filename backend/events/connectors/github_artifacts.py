"""Extract created GitHub pull request artifacts from tool results."""
import json
import re

from events.connectors import ObservedArtifactDecl
from events.identity import GITHUB_INTEGRATION, PR_TYPE, pr_external_id
from infra.webhook_delivery_log import comment_object_key

_PR_URL_RE = re.compile(
    r"https://github\.com/([^/\s?#]+/[^/\s?#]+)/pull/([1-9]\d*)"
)
_FAILED_EXIT_RE = re.compile(r"^Exit code: [1-9]\d*$")


def _pr_artifact_in_output(result_text: str) -> ObservedArtifactDecl | None:
    """Return the one canonical PR URL named by a successful command's output."""
    lines = [line.strip() for line in result_text.splitlines() if line.strip()]
    if any(_FAILED_EXIT_RE.fullmatch(line) for line in lines):
        return None
    matches = [
        match
        for line in lines
        if (match := _PR_URL_RE.fullmatch(line)) is not None
    ]
    if len(matches) != 1:
        return None
    repo, number_text = matches[0].groups()
    number = int(number_text)
    return ObservedArtifactDecl(
        integration=GITHUB_INTEGRATION,
        type=PR_TYPE,
        external_id=pr_external_id(repo, number),
        title=f"PR #{number}",
        path=f"https://github.com/{repo}/pull/{number}",
    )


def pr_artifact_in_result(result_text: str) -> ObservedArtifactDecl | None:
    """Read a GitHub MCP created object's URL field, or the shell's PR URL line."""
    try:
        result = json.loads(result_text)
    except (TypeError, ValueError):
        return _pr_artifact_in_output(result_text)
    if not isinstance(result, dict):
        return None
    item = result.get("comment", result)
    if not isinstance(item, dict):
        return None
    url, object_id = item.get("url"), item.get("id")
    if not isinstance(url, str) or not isinstance(object_id, str) or not object_id.isdigit():
        return None
    match = re.fullmatch(_PR_URL_RE.pattern + r"(?:#issuecomment-([1-9]\d*))?", url)
    if match is None:
        return None
    repo, number, comment_id = match.groups()
    if ("comment" in result and comment_id is None) or (comment_id is not None and comment_id != object_id):
        return None
    return ObservedArtifactDecl(
        integration=GITHUB_INTEGRATION, type=PR_TYPE,
        external_id=pr_external_id(repo, number), title=f"PR #{number}",
        created_event_ids=(comment_object_key("issue_comment", comment_id),) if comment_id else (),
    )
