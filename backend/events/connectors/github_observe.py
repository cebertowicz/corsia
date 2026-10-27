"""GitHub's structured connected-app result reader."""
from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlsplit

from events.connectors import ObservedArtifactDecl, ObservedResult
from events.identity import GITHUB_INTEGRATION, PR_TYPE, pr_external_id

_PR_PATH = re.compile(r"^/([^/]+)/([^/]+)/pull/([1-9][0-9]*)$")


def _repo(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    repo = value.get("repo")
    return repo.get("full_name") if isinstance(repo, dict) and isinstance(repo.get("full_name"), str) else None


def extract(result: str, tool: str, args: dict[str, Any]) -> ObservedResult | None:
    """Read only fields of the JSON object; never search its free text."""
    try:
        obj, _ = json.JSONDecoder().raw_decode(result.lstrip())
    except (ValueError, TypeError):
        return None
    if not isinstance(obj, dict):
        return None
    repo = _repo(obj.get("base")) or _repo(obj.get("head"))
    number = obj.get("number")
    if not repo or type(number) is not int or number < 1:
        for key in ("html_url", "url"):
            url = obj.get(key)
            if not isinstance(url, str):
                continue
            try:
                parts = urlsplit(url)
            except ValueError:
                continue
            match = _PR_PATH.fullmatch(parts.path)
            if parts.scheme == "https" and parts.netloc == "github.com" and not parts.query and not parts.fragment and match:
                repo = f"{match[1]}/{match[2]}"
                number = int(match[3])
                break
        else:
            repo, number = None, None
    artifact = (ObservedArtifactDecl(GITHUB_INTEGRATION, PR_TYPE, pr_external_id(repo, number), title=f"PR #{number}")
                if repo and number else None)
    kinds = {"add_issue_comment": "issue_comment", "add_comment_to_pending_review": "review_comment",
             "pull_request_review_write": "review"}
    kind, event_id = kinds.get(tool), obj.get("id")
    events = ((kind, str(event_id)),) if kind and type(event_id) is int and event_id > 0 else ()
    return ObservedResult(artifact, events)
