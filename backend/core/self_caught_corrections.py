"""Direct, text-free correction sources for failures observed without a person's correction.

A failed plan node or a GitHub reopen or revert shows that settled work did not hold; it
does not show who noticed (agents push under the person's login, so a GitHub actor cannot
separate the two). The `self_caught` source kind here therefore means only "observed from a
state change, with no correction message from a person"; it is not a claim about who acted.
These adapters only identify the authoritative target and source identity. The
existing correction event writer remains the single store and the existing note,
triage, and terminal-outcome paths remain the single lifecycle.
"""
from __future__ import annotations

from core.corrections_recording import CorrectionCandidate, SOURCE_KIND_SELF_CAUGHT


def _thread_project(db, thread_id: str) -> str | None:
    row = db.execute("SELECT project_id FROM threads WHERE id=?", (thread_id,)).fetchone()
    return None if row is None or row[0] is None else str(row[0])


def _record(db, *, source_id: str, thread_id: str, created_at: str) -> bool:
    from jobs.corrections_recording import record_self_caught

    return record_self_caught(
        CorrectionCandidate(
            source_kind=SOURCE_KIND_SELF_CAUGHT,
            source_id=source_id,
            thread_id=thread_id,
            project_id=_thread_project(db, thread_id),
            content="",
            created_at=created_at,
            arrived=True,
        ),
        db=db,
    )


def _thread_plan_owner(db, node_id: str) -> str | None:
    """Return the canonical owner of the thread plan holding ``node_id``."""
    row = db.execute(
        """SELECT p.owner_id FROM plan_nodes n
           JOIN plans p ON p.id=n.plan_id
           WHERE n.id=? AND p.owner_kind='thread' AND length(trim(p.owner_id)) > 0""",
        (node_id,),
    ).fetchone()
    return None if row is None else str(row[0])


def record_failed_plan_node(db, node_id: str) -> bool:
    """Record a failed node for its owning thread plan after transition."""
    thread_id = _thread_plan_owner(db, node_id)
    if thread_id is None:
        return False
    row = db.execute(
        """SELECT created_at FROM plan_node_events
           WHERE node_id=? AND event='state_transition' AND new_state='failed'
           ORDER BY rowid DESC LIMIT 1""",
        (node_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError("failed node has no terminal transition event")
    return _record(
        db, source_id=f"plan-failed:{node_id}", thread_id=thread_id, created_at=str(row[0]),
    )


def record_plan_correction(db, source_node_id: str, correction_node_id: str) -> bool:
    """Record an add_correction against its source node's owning thread plan."""
    from plans.public import TERMINAL_STATES

    thread_id = _thread_plan_owner(db, source_node_id)
    if thread_id is None:
        return False
    terminal_states = tuple(sorted(TERMINAL_STATES))
    placeholders = ",".join("?" for _ in terminal_states)
    settled = db.execute(
        f"""SELECT created_at FROM plan_node_events
           WHERE node_id=? AND event='state_transition'
             AND new_state IN ({placeholders})
           ORDER BY rowid DESC LIMIT 1""",
        (source_node_id, *terminal_states),
    ).fetchone()
    if settled is None:
        return False
    return _record(
        db,
        source_id=f"plan-correction:{source_node_id}:{correction_node_id}",
        thread_id=thread_id,
        created_at=str(settled[0]),
    )


def _artifact_owner(db, repo: str, pr: str) -> str | None:
    """Read CORSIA's canonical PR ownership record, never a webhook reporter."""
    from events.identity import pr_external_id
    from infra.github_pr_identity import GITHUB_INTEGRATION, PR_TYPE
    from registry.identity_bridge import resolve_normalized_owner

    external_id = pr_external_id(repo, pr)
    artifact = resolve_normalized_owner(db, GITHUB_INTEGRATION, PR_TYPE, external_id)
    if artifact is None:
        # Outcome drafts lowercase the repo; a PR registered from the payload keeps its case.
        cased = db.execute(
            "SELECT external_id FROM artifact_identities WHERE integration=? AND type=? "
            "AND lower(external_id)=? AND external_id<>? LIMIT 1",
            (GITHUB_INTEGRATION, PR_TYPE, external_id.lower(), external_id),
        ).fetchone()
        artifact = None if cased is None else resolve_normalized_owner(
            db, GITHUB_INTEGRATION, PR_TYPE, str(cased[0]),
        )
    owner = None if artifact is None else artifact.get("parent_thread_id")
    return str(owner) if isinstance(owner, str) and owner else None


def record_reopened_issue(db, *, repo: str, issue: str, source_id: str, created_at: str) -> int:
    """Record one correction against the owner of the merged PR that last stated it closes a reopened issue.

    Earlier PRs that said the same are not blamed again for a later close; the lookup is the
    ``idx_learning_loop_closes_issue`` index, so it does not grow with GitHub history.
    """
    row = db.execute(
        """SELECT json_extract(c.attributes,'$.repo') AS repo,
                  json_extract(c.attributes,'$.pr') AS pr
           FROM outcome_events c INDEXED BY idx_learning_loop_closes_issue
           CROSS JOIN outcome_events m INDEXED BY idx_outcome_events_subject ON m.subject_key=c.subject_key
            AND m.event_name='github.pr.merged' AND m.retracted_at IS NULL AND m.ts<=?
           WHERE c.event_name='github.pr.closes_issue' AND c.retracted_at IS NULL
             AND json_extract(c.attributes,'$.issue_repo')=?
             AND json_extract(c.attributes,'$.issue')=?
           ORDER BY m.ts DESC LIMIT 1""",
        (created_at, repo, issue),
    ).fetchone()
    if row is None:
        return 0
    pr_repo, pr = str(row[0]), str(row[1])
    owner = _artifact_owner(db, pr_repo, pr)
    if not owner:
        return 0
    return _record(
        db, source_id=f"{source_id}:closed-by:{pr_repo}#{pr}",
        thread_id=owner, created_at=created_at,
    )


def record_github_outcome_drafts(db, drafts) -> int:
    """Admit direct GitHub correction sources from already-mapped raw facts only."""
    recorded = 0
    for draft in drafts:
        if draft.event_name == "github.issue.reopened":
            recorded += record_reopened_issue(
                db,
                repo=str(draft.attributes["repo"]),
                issue=str(draft.attributes["issue"]),
                source_id=draft.dedupe_key,
                created_at=draft.ts,
            )
        elif draft.event_name == "github.pr.merged" and "reverts_pr" in draft.attributes:
            recorded += int(record_merged_revert(
                db,
                reverted_repo=str(draft.attributes["repo"]).lower(),
                reverted_pr=str(draft.attributes["reverts_pr"]),
                source_id=draft.dedupe_key,
                created_at=draft.ts,
            ))
    return recorded


def record_merged_revert(
    db, *, reverted_repo: str, reverted_pr: str, source_id: str, created_at: str,
) -> bool:
    """Record an explicit merged PR reversion against the reverted PR's owner."""
    owner = _artifact_owner(db, reverted_repo, reverted_pr)
    return bool(owner) and _record(
        db, source_id=f"{source_id}:reverted:{reverted_repo}#{reverted_pr}",
        thread_id=owner, created_at=created_at,
    )
