"""Direct, text-free correction sources for failures CORSIA catches itself.

These adapters only identify the authoritative target and source identity.  The
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


def record_failed_plan_node(db, node_id: str, thread_id: str | None) -> bool:
    """Record a failed node for the agent that failed it, never another reporter."""
    if not thread_id:
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
    """Record a correction against the thread that originally settled its source."""
    settled = db.execute(
        """SELECT actor_id,created_at FROM plan_node_events
           WHERE node_id=? AND event='state_transition'
             AND new_state IN ('accepted','accepted_with_followup','failed')
             AND actor_id IS NOT NULL AND actor_id NOT LIKE 'user:%'
           ORDER BY rowid DESC LIMIT 1""",
        (source_node_id,),
    ).fetchone()
    if settled is None:
        return False
    return _record(
        db,
        source_id=f"plan-correction:{source_node_id}:{correction_node_id}",
        thread_id=str(settled[0]),
        created_at=str(settled[1]),
    )


def _artifact_owner(db, repo: str, pr: str) -> str | None:
    """Read CORSIA's canonical PR ownership record, never a webhook reporter."""
    from events.identity import pr_external_id
    from infra.github_pr_identity import GITHUB_INTEGRATION, PR_TYPE
    from registry.identity_bridge import resolve_normalized_owner

    artifact = resolve_normalized_owner(
        db, GITHUB_INTEGRATION, PR_TYPE, pr_external_id(repo, pr),
    )
    owner = None if artifact is None else artifact.get("parent_thread_id")
    return str(owner) if isinstance(owner, str) and owner else None


def record_reopened_issue(db, *, repo: str, issue: str, source_id: str, created_at: str) -> int:
    """Record one correction per owned merged PR that explicitly closed a reopened issue."""
    rows = db.execute(
        """SELECT json_extract(c.attributes,'$.repo') AS repo,
                  json_extract(c.attributes,'$.pr') AS pr
           FROM outcome_events c
           WHERE c.event_name='github.pr.closes_issue' AND c.retracted_at IS NULL
             AND json_extract(c.attributes,'$.issue_repo')=?
             AND json_extract(c.attributes,'$.issue')=?
             AND EXISTS (
                 SELECT 1 FROM outcome_events m
                 WHERE m.event_name='github.pr.merged' AND m.retracted_at IS NULL
                   AND m.subject_key=c.subject_key
             )""",
        (repo, issue),
    ).fetchall()
    recorded = 0
    for row in rows:
        pr_repo, pr = str(row[0]), str(row[1])
        owner = _artifact_owner(db, pr_repo, pr)
        if owner:
            recorded += _record(
                db, source_id=f"{source_id}:closed-by:{pr_repo}#{pr}",
                thread_id=owner, created_at=created_at,
            )
    return recorded


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
        elif draft.event_name == "github.pr.reverts":
            recorded += int(record_merged_revert(
                db,
                reverted_repo=str(draft.attributes["reverted_repo"]),
                reverted_pr=str(draft.attributes["reverted_pr"]),
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
