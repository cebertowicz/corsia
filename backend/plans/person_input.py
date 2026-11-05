"""Deliver a joined document reply to the owner already working its task."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from plans import node_states, service

WORDS_MAX_BYTES = 1200
_RUNNING = ("running", "review")


@dataclass(frozen=True)
class Source:
    """The delivered reply and its original source."""

    kind: str
    id: str
    parts: tuple[str, ...]
    said_at: str
    thread_id: str
    project_id: str | None
    links: tuple[str, ...] = ()

    @property
    def words(self) -> str:
        return "\n".join(self.parts)


def running(db, nodes) -> list[tuple]:
    """Keep the tasks that are selected, delegated or awaiting review, with their plans."""
    from plans import admission, delegation

    plans: dict[str, sqlite3.Row] = {}
    pairs = []
    for node in nodes:
        if node_states.retired(node) or node["state"] not in ("active", "review_requested"):
            continue
        plan = plans.get(str(node["plan_id"]))
        if plan is None:
            plan = plans[str(node["plan_id"])] = service._plan_row(db, node["plan_id"])
        if plan["state"] == "active":
            pairs.append((node, plan))
    projected = admission.project_many(pairs, db=db) if pairs else {}
    return [
        (node, plan) for node, plan in pairs
        if projected[str(node["id"])]["condition"] in _RUNNING
        or (node["state"] == "active" and delegation._valid_structured_epoch(node, db) is not None)
    ]


def joined_reply(db, node_id: str, source: Source, comment_ids: list[str]) -> str | None:
    """Wake the owner working a task a person's reply just joined."""
    from plans import admission_projection

    node = service._node_row(db, node_id)
    recipient = admission_projection.executor_of(node, service._plan_row(db, node["plan_id"]))
    return None if recipient is None else _append(db, source, node, recipient, answer=comment_ids)


def _append(db, source: Source, node, recipient: str, *, answer: list[str]) -> str | None:
    from plans import plan_wake

    return plan_wake.append(
        db, recipient=recipient, kind=plan_wake.INTENT_INPUT, subject_id=str(node["id"]),
        revision=source.kind, about=source.id, plan_id=str(node["plan_id"]),
        open_node_id=str(node["id"]), content=_content(db, node, source, recipient, answer),
    )


def _content(db, node, source: Source, recipient: str, answer: list[str]) -> str:
    from plans import plan_wake
    from plans.close_next_bounds import bounded
    from prompts import plan_wakes as wakes

    title = bounded(str(node["title"]), plan_wake.RETURNED_TITLE_MAX_BYTES)
    revision = int(node["requirements_revision"])
    return plan_wake.with_next(
        db, service.ensure_plan("thread", recipient, db=db).id, recipient,
        " ".join([
            wakes.WAKE_INTENT_INPUT.format(title=title, requirements_revision=revision),
            *(wakes.INTENT_INPUT_ANSWER_REPLY.format(comment_id=comment_id) for comment_id in answer),
        ]),
        intent_input={
            "node_id": str(node["id"]), "requirements_revision": revision, "title": title,
            "source_kind": source.kind, "said_at": source.said_at,
            "words": source.words.encode()[:WORDS_MAX_BYTES].decode(errors="ignore"),
        },
    )
