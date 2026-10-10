"""Trusted review tasks for lesson trials and captured hypotheses.

A trial lesson is offered only while its owner holds an open review of that
exact version. This module creates that review on the owner's plan: a pending
task, linked to the lesson file, scheduled for the review date. It is never
started and never admitted here, so it costs the owner no executor slot.

At the date the ordinary scheduled wake names it. That named wake is not
guaranteed: a paused plan's wait stays armed until the user resumes, and a
wake that stalls leaves the review pending with no wait, as ready work the
liveness wake carries by node id once the owner's executor slot is free.
Neither path completes the review; that is the owner's.

One open review per version, found again by its namespaced key
(``lesson:<id>:<behavioral_sha256>`` or ``capture:<id>:<capture_hash>``), so a
retry or appended evidence creates nothing. A settled review is history: the
same version placed again after its review closed gets a new one, under the
same key. A new version, a capture completed into a trial, or a change of
owner closes the review it replaces in the same write, with the reason. That
is a system close and tells nobody: the previous owner's task closes with only
the reason on its row.

The same version placed again with another review date keeps its review, and
the review's own wake moves to that date while the review is still pending and
unselected. A review its owner has started, or handed for review, is the
owner's: its waits and its attempt are left as they are.
"""

from __future__ import annotations

from plans import admission_attempts, artifact_links, closure, plan_write, scheduled_wait, service
from plans.node_states import _TERMINAL_STATES, in_work_sql

PROVENANCE = "lesson.trial_review"
LESSON_PREFIX = "lesson"
CAPTURE_PREFIX = "capture"
SCHEDULE_REASON = "Its review date"
SUPERSEDED_BY_VERSION = "Superseded: the lesson changed and a review of its new version replaces this one"
SUPERSEDED_BY_TRIAL = "Superseded: the captured hypothesis became a trial lesson with its own review"
SUPERSEDED_BY_CAPTURE = "Superseded: the captured hypothesis changed and a new review replaces this one"
SUPERSEDED_BY_OWNER = "Superseded: the lesson has a new owner, who holds its review"


class LessonOwnerError(service.PlanError):
    """The named owner cannot hold a review: no live thread or plan resolves."""


def lesson_key(lesson_id: str, behavioral_sha256: str) -> str:
    """The idempotency key of one lesson version's review."""
    return f"{LESSON_PREFIX}:{lesson_id}:{behavioral_sha256}"


def capture_key(capture_id: str, capture_hash: str) -> str:
    """The idempotency key of one captured hypothesis's review."""
    return f"{CAPTURE_PREFIX}:{capture_id}:{capture_hash}"


def _require_live_owner(connection, owner_thread_id: str) -> None:
    from plans.archival import _child_thread_reachable

    if not _child_thread_reachable(connection, owner_thread_id):
        raise LessonOwnerError(f"lesson owner {owner_thread_id} is not a live thread")
    plan = connection.execute(
        "SELECT state FROM plans WHERE owner_kind='thread' AND owner_id=?", (owner_thread_id,),
    ).fetchone()
    if plan is not None and plan["state"] == "archived":
        raise LessonOwnerError(f"lesson owner {owner_thread_id} has an archived plan")


def _existing(connection, owner_thread_id: str, key: str) -> str | None:
    """The owner's open review under ``key``; a closed or archived one never counts."""
    return next(
        (
            str(row["node_id"]) for row in _open_reviews(connection, key)
            if row["source_event_id"] == key and row["owner_kind"] == "thread"
            and row["owner_id"] == owner_thread_id
        ),
        None,
    )


def _align_wake(connection, node_id: str, review_after: str) -> None:
    """Move a pending review's own wake to the date its file now carries.

    Only a pending, unselected review is aligned: once its owner has started
    it or asked for its review, or an attempt on it is open, nothing here
    touches its waits. No operation moves a deadline, so the armed wait is
    resolved and the task scheduled again in this write: one wait stays armed.
    A wake already armed for that date, by this module or by the owner, is
    left as it is, and one that already fired for it is not armed again. The
    owner's own waits on the task are never resolved here.
    """
    from automation import watches

    node = connection.execute("SELECT state FROM plan_nodes WHERE id=?", (node_id,)).fetchone()
    if node["state"] != "pending" or admission_attempts.open_attempt(connection, node_id):
        return
    due = scheduled_wait._parse(review_after)
    waits = connection.execute(
        """SELECT id,status,condition_value,armed_by_actor_id FROM expected_event_watches
           WHERE plan_node_id=? AND condition_type='deadline' AND expected_event=?
           ORDER BY armed_at DESC, rowid DESC""",
        (node_id, scheduled_wait.SCHEDULED_EVENT),
    ).fetchall()
    at_date = {row["id"] for row in waits if scheduled_wait._parse(row["condition_value"]) == due}
    own = [row for row in waits if row["armed_by_actor_id"] is None]
    for row in own:
        if row["status"] == "armed" and row["id"] not in at_date:
            watches.resolve_watch(str(row["id"]), db=connection)
    armed = any(row["status"] == "armed" and row["id"] in at_date for row in waits)
    fired = bool(own) and own[0]["id"] in at_date
    if not armed and not fired:
        scheduled_wait.schedule_task(
            connection, node_id, reason=SCHEDULE_REASON, deadline=review_after,
            provenance=PROVENANCE,
        )


def _open_review_sql(alias: str) -> str:
    states = ",".join(f"'{state}'" for state in sorted(_TERMINAL_STATES))
    return f"{in_work_sql(alias)} AND {alias}.state NOT IN ({states})"


def _open_reviews(connection, prefix: str) -> list:
    """Open review nodes whose key starts with ``prefix``, with key and owner."""
    return connection.execute(
        f"""SELECT DISTINCT l.node_id, l.source_event_id, p.owner_kind, p.owner_id
            FROM plan_node_artifact_links l
            JOIN plan_nodes n ON n.id=l.node_id
            JOIN plans p ON p.id=n.plan_id
            WHERE l.provenance=? AND substr(l.source_event_id, 1, ?)=?
              AND {_open_review_sql('n')}
            ORDER BY l.created_at, l.node_id""",
        (PROVENANCE, len(prefix), prefix),
    ).fetchall()


def _close(connection, node_id: str, reason: str) -> None:
    closure.close_node(node_id, reason=reason, actor_id=None, provenance=PROVENANCE, db=connection)


def _create(
    connection, owner_thread_id: str, key: str, *, artifact_id: str, title: str,
    acceptance: str, review_after: str, description: str | None,
) -> str:
    plan = service.ensure_plan("thread", owner_thread_id, db=connection)
    shown = " ".join(title.split())
    if len(shown) > service.MAX_NODE_TITLE:
        shown = f"{shown[:service.MAX_NODE_TITLE - 1]}…"
    node = service.add_node(
        plan.id, kind="task", title=shown, acceptance_criteria=acceptance,
        description=description, actor_id=None, provenance=PROVENANCE, db=connection,
    )
    artifact_links.link(
        node.id, artifact_id, "input", actor_id=None, provenance=PROVENANCE,
        source_event_id=key, db=connection,
    )
    scheduled_wait.schedule_task(
        connection, node.id, reason=SCHEDULE_REASON, deadline=review_after,
        provenance=PROVENANCE,
    )
    return node.id


def ensure_lesson_review(
    db, owner_thread_id: str, *, lesson_id: str, behavioral_sha256: str, artifact_id: str,
    title: str, acceptance: str, review_after: str, description: str | None = None,
    capture_id: str | None = None,
) -> tuple[str, bool]:
    """Return the owner's review of this lesson version, creating it once.

    The review is a pending task scheduled for ``review_after``; it is not
    started and admission is not reconciled. In the same write, every review
    this one replaces is closed as superseded: the lesson's capture review, a
    review of another version, and this version's review under another owner.
    ``capture_id`` names the capture when it is not the lesson's own id: a
    captured hypothesis completed as a revision of an existing lesson. It is
    trusted as given: nothing here proves the caller owns that capture.
    A review found pending is returned with its wake at ``review_after``; one
    its owner is working is returned untouched.

    Raises:
        LessonOwnerError: the owner is missing or archived; nothing is closed.
    """
    key = lesson_key(lesson_id, behavioral_sha256)
    with plan_write.transaction(db) as connection:
        _require_live_owner(connection, owner_thread_id)
        existing = _existing(connection, owner_thread_id, key)
        for row in _open_reviews(connection, f"{CAPTURE_PREFIX}:{capture_id or lesson_id}:"):
            _close(connection, str(row["node_id"]), SUPERSEDED_BY_TRIAL)
        for row in _open_reviews(connection, f"{LESSON_PREFIX}:{lesson_id}:"):
            if row["source_event_id"] != key:
                _close(connection, str(row["node_id"]), SUPERSEDED_BY_VERSION)
            elif row["owner_id"] != owner_thread_id:
                _close(connection, str(row["node_id"]), SUPERSEDED_BY_OWNER)
        if existing is not None:
            _align_wake(connection, existing, review_after)
            return existing, False
        return _create(
            connection, owner_thread_id, key, artifact_id=artifact_id, title=title,
            acceptance=acceptance, review_after=review_after, description=description,
        ), True


def ensure_capture_review(
    db, owner_thread_id: str, *, capture_id: str, capture_hash: str, artifact_id: str,
    title: str, acceptance: str, review_after: str, description: str | None = None,
) -> tuple[str, bool]:
    """Return the owner's review of this captured hypothesis, creating it once.

    The hash covers the hypothesis and its classification inputs only, so
    appended evidence finds the same review. A changed hypothesis, or a new
    owner, gets a new review and closes the one it replaces.

    Raises:
        LessonOwnerError: the owner is missing or archived; nothing is closed.
    """
    key = capture_key(capture_id, capture_hash)
    with plan_write.transaction(db) as connection:
        _require_live_owner(connection, owner_thread_id)
        existing = _existing(connection, owner_thread_id, key)
        for row in _open_reviews(connection, f"{CAPTURE_PREFIX}:{capture_id}:"):
            if row["source_event_id"] != key:
                _close(connection, str(row["node_id"]), SUPERSEDED_BY_CAPTURE)
            elif row["owner_id"] != owner_thread_id:
                _close(connection, str(row["node_id"]), SUPERSEDED_BY_OWNER)
        if existing is not None:
            _align_wake(connection, existing, review_after)
            return existing, False
        return _create(
            connection, owner_thread_id, key, artifact_id=artifact_id, title=title,
            acceptance=acceptance, review_after=review_after, description=description,
        ), True


def lesson_review_refusal(
    db, node_id: str, *, owner_thread_id: str, lesson_id: str, behavioral_sha256: str,
) -> str | None:
    """Why ``node_id`` is not an open review of this lesson version, or None.

    The check a trial's ``review_ref`` must pass before the trial is offered:
    a reference alone proves nothing, so a hand-written one, or one left behind
    by a behavioral edit, keeps the trial out.
    """
    row = db.execute(
        """SELECT n.state, n.archived_at, p.owner_kind, p.owner_id
           FROM plan_node_artifact_links l
           JOIN plan_nodes n ON n.id=l.node_id
           JOIN plans p ON p.id=n.plan_id
           WHERE l.node_id=? AND l.provenance=? AND l.source_event_id=?""",
        (node_id, PROVENANCE, lesson_key(lesson_id, behavioral_sha256)),
    ).fetchone()
    if row is None:
        return "no review covers this version of the trial"
    if row["archived_at"] is not None or row["state"] in _TERMINAL_STATES:
        return "its review is settled"
    if row["owner_kind"] != "thread" or row["owner_id"] != owner_thread_id:
        return "its review belongs to another owner"
    return None


def lesson_review_opened_at(db, node_id: str) -> str | None:
    """When a review task was created: the order trials entered the index."""
    row = db.execute("SELECT created_at FROM plan_nodes WHERE id=?", (node_id,)).fetchone()
    return str(row["created_at"]) if row else None



def open_capture_review_counts(db) -> dict[str, int]:
    """Open captured-hypothesis reviews per owning thread: the batching signal."""
    counts: dict[str, int] = {}
    for row in _open_reviews(db, f"{CAPTURE_PREFIX}:"):
        counts[str(row["owner_id"])] = counts.get(str(row["owner_id"]), 0) + 1
    return counts
