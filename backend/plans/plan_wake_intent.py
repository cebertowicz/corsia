"""Revision wakes that carry a canonical task's changed intent to workers."""

from __future__ import annotations

from plans import node_states, service


def notify(db, node_id: str, *, causing_executor: str | None) -> None:
    """Append one current-revision wake to every eligible recipient, once."""
    from plans import delegation, plan_wake

    node = service._node_row(db, node_id)
    recipients: dict[str, bool] = {}
    if (
        node["spawned_thread_id"] is not None
        and node["state"] != "review_requested"
        and delegation._valid_structured_epoch(node, db) is not None
    ):
        recipients[str(node["spawned_thread_id"])] = True
    for inheriting in _open_inheriting_nodes(db, node_id):
        executor = _executor(db, inheriting)
        if executor is not None:
            recipients.setdefault(executor, False)
    for recipient, attached_worker in recipients.items():
        if recipient == causing_executor:
            continue
        plan_wake.append(
            db,
            recipient=recipient,
            kind=plan_wake.INTENT_REVISED,
            subject_id=node_id,
            revision=int(node["requirements_revision"]),
            plan_id=str(node["plan_id"]),
            about=recipient,
            delegated_step_id=node_id if attached_worker else None,
            delegated_worker=recipient if attached_worker else None,
            content=_content(db, node, recipient),
        )


def _open_inheriting_nodes(db, intent_node_id: str):
    return db.execute(
        f"""SELECT n.* FROM plan_nodes n
            WHERE n.intent_node_id=? AND {node_states.open_sql('n')} ORDER BY n.id""",
        (intent_node_id,),
    ).fetchall()


def _executor(db, node) -> str | None:
    from plans import admission

    plan = service._plan_row(db, node["plan_id"])
    return admission._projection().executor_of(node, plan)


def _content(db, node, recipient: str) -> str:
    from plans import plan_wake
    from plans.close_next_bounds import REVISION_BRIEF_MAX_BYTES as cap, bounded
    from prompts import plan_wakes as wakes

    own_plan = service.ensure_plan("thread", recipient, db=db)
    revision = int(node["requirements_revision"])
    return plan_wake.with_next(
        db,
        own_plan.id,
        recipient,
        wakes.WAKE_INTENT_REVISED.format(
            title=bounded(str(node["title"]), plan_wake.RETURNED_TITLE_MAX_BYTES),
            requirements_revision=revision,
        ),
        intent_revision={
            "node_id": str(node["id"]),
            "requirements_revision": revision,
            "title": bounded(str(node["title"]), plan_wake.RETURNED_TITLE_MAX_BYTES),
            "intent": bounded(str(node["intent"] or ""), cap),
            "acceptance_criteria": bounded(str(node["acceptance_criteria"]), cap),
            "intent_words": _words_since_last_wake(db, node, recipient),
        },
    )


def _words_since_last_wake(db, node, recipient: str) -> list[dict[str, object]]:
    from plans.close_next_bounds import REVISION_BRIEF_MAX_BYTES as cap, bounded

    last_revision = _last_wake_revision(db, str(node["id"]), recipient)
    revision = int(node["requirements_revision"])
    rows = db.execute(
        """SELECT author,role,quote,added_at,requirements_revision
           FROM plan_node_intent_words
           WHERE node_id=? AND removed_at IS NULL AND requirements_revision>?
           ORDER BY requirements_revision,added_at,rowid""",
        (node["id"], last_revision),
    ).fetchall()
    if not rows:
        rows = db.execute(
            """SELECT author,role,quote,added_at,requirements_revision
               FROM plan_node_intent_words
               WHERE node_id=? AND removed_at IS NULL AND requirements_revision=?
               ORDER BY added_at,rowid""",
            (node["id"], revision),
        ).fetchall()
    return [
        {
            "author": row["author"], "role": row["role"],
            "quote": bounded(str(row["quote"]), cap), "added_at": row["added_at"],
            "requirements_revision": int(row["requirements_revision"]),
        }
        for row in rows
    ]


def _last_wake_revision(db, node_id: str, recipient: str) -> int:
    prefix = f"plan-wake:intent_revised:{node_id}:"
    rows = db.execute(
        """SELECT id FROM messages WHERE thread_id=? AND id LIKE ?
           ORDER BY input_order,id""",
        (recipient, f"{prefix}%"),
    ).fetchall()
    revisions = []
    for row in rows:
        revision, _separator, wake_recipient = str(row["id"])[len(prefix):].partition(":")
        if wake_recipient == recipient and revision.isdigit():
            revisions.append(int(revision))
    return max(revisions, default=0)
