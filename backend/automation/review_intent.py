"""The task intent a pull request's review start carries, and its revision on the result.

At the claim, in-process and never in a worker's sandbox, a start whose subject is a
GitHub pull request reads its intent: the PR's registered artifact to the one open plan
node that owns it (``plans.public.linked_node_for_artifact``), then that node's canonical
intent (``plans.public.intent_of``). A start without one carries the reason instead
(``no_linked_task``, ``several_linked_tasks`` or ``lookup_failed``, which is also any
exception in either read), and still starts. The node is never inferred from the PR's
text or branch. When the result settles, :func:`stamp` reads the canonical node's
revision again, so a verdict is comparable with the intent as it is now.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from typing import Any

from loguru import logger

from plans.public import REVISION_BRIEF_MAX_BYTES, bounded_label, intent_of, linked_node_for_artifact

GITHUB = "github"
PULL_REQUEST = "pull-request"
LOOKUP_FAILED = "lookup_failed"
NONE = "none"
_REASONS = ("no_linked_task", "several_linked_tasks", LOOKUP_FAILED)


def attach(row: Mapping[str, Any], skill: Any, *, db: sqlite3.Connection) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """The start's intent and the record its result is stamped from, ``(None, None)`` when
    its subject is not a GitHub pull request or its workflow does not declare ``task_intent``.
    With no intent the record is its reason. The workflow opts in because the intent carries a
    person's own words."""
    if ((getattr(skill, "corsia", None) or {}).get("workflow") or {}).get("task_intent") is not True:
        return None, None
    subject = dict(row)
    if (subject.get("artifact_integration"), subject.get("artifact_type")) != (GITHUB, PULL_REQUEST):
        return None, None
    try:
        linked = linked_node_for_artifact(GITHUB, PULL_REQUEST, subject["artifact_external_id"], db=db)
        if linked["status"] != "ok":
            return None, {"reason": linked["status"] if linked["status"] in _REASONS else LOOKUP_FAILED}
        read = intent_of(linked["node_id"], db=db)
        intent = {
            "node_id": read["node_id"], "linked_node": linked["node_id"], "title": read["title"],
            "requirements_revision": read["requirements_revision"],
            "intent": bounded_label(str(read["intent"] or ""), REVISION_BRIEF_MAX_BYTES),
            "acceptance_criteria": bounded_label(str(read["acceptance_criteria"] or ""), REVISION_BRIEF_MAX_BYTES),
            "intent_words": [{key: word[key] for key in ("author", "role", "quote", "added_at")}
                             for word in read["intent_words"]],
        }
    except Exception:  # noqa: BLE001 - a Plans failure is never "no linked task", and the review still starts
        logger.exception("the review's intent lookup failed")
        return None, {"reason": LOOKUP_FAILED}
    return intent, {key: intent[key] for key in ("node_id", "linked_node", "requirements_revision",
                                                  "intent", "acceptance_criteria")} | {
        "quotes": [word["quote"] for word in intent["intent_words"]]}


def stamp(record: Mapping[str, Any] | None, *, db: sqlite3.Connection) -> dict[str, Any]:
    """The ``worker.result`` facts that say which intent the run had and its revision now.
    All but the fresh revision come from the claim's record, never from the worker's answer;
    ``intent_text`` is what publish checks its words against."""
    if record is None:
        return {}
    if "reason" in record:
        return {"intent_node": NONE, "intent_reason": record["reason"]}
    facts = {
        "intent_node": record["node_id"], "intent_linked_node": record["linked_node"],
        "intent_revision": record["requirements_revision"],
        "intent_text": {"intent": record["intent"], "acceptance_criteria": record["acceptance_criteria"],
                        "quotes": list(record["quotes"])},
    }
    try:
        facts["intent_current_revision"] = int(intent_of(record["node_id"], db=db)["requirements_revision"])
    except Exception:  # noqa: BLE001 - say the read failed; never copy the claim-time revision
        logger.exception("the review's intent revision could not be read")
        facts["intent_reason"] = LOOKUP_FAILED
    return facts
