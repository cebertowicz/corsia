"""SQL-level retirement eligibility shared by registration storage seams.

One owner for the retirement-exemption predicate so node settlement, pin
transitions, and pane payloads cannot drift apart.

A row is exempt when the artifact outlives the task that happened to touch
it. Two kinds qualify: a durable workspace document the user has commented
on, and a GitHub pull request that is still open, whose own lifecycle ends at
merge or close rather than whenever the task that opened it settles. Once a
pull request has ended it is no longer exempt: its end is itself a retirement
(``registry.node_retirement.retire_registrations_for_ended_artifact``).
Everything else -- a document in a ``shared/`` tree, a thread's own folder,
working-tray scratch, repo/worktree files, uploads -- is task-scoped and
retires; the document itself stays, and pin keeps its row.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from infra.github_pr_identity import GITHUB_INTEGRATION, PR_TYPE

#: A durable document living under the project workspace: the clause the
#: user-comment exemption builds on, and the one schema migration 21->22 pins.
DURABLE_WORKSPACE_EXEMPT = """(i.retention='durable'
               AND json_extract(i.metadata, '$.zone')='workspace')"""

#: A durable workspace document the user has commented on, wherever it lives:
#: a document with a comment for the user never leaves the pane
#: (``doceditor.feedback_comments.make_user_document_durable``).
USER_COMMENTED_DOCUMENT_EXEMPT = f"""({DURABLE_WORKSPACE_EXEMPT}
               AND EXISTS (
                   SELECT 1 FROM doc_comments AS c
                    WHERE c.artifact_id=i.id AND c.author_kind='user'
               ))"""

#: The states that end a pull request's life.
PR_TERMINAL_STATES = ("merged", "closed")

_TERMINAL_STATES_SQL = ", ".join(f"'{state}'" for state in PR_TERMINAL_STATES)

#: A GitHub pull request whose recorded state is terminal.
ENDED_PULL_REQUEST = f"""(i.integration='{GITHUB_INTEGRATION}'
               AND i.type='{PR_TYPE}'
               AND COALESCE(json_extract(i.metadata, '$.pr_state'), '')
                   IN ({_TERMINAL_STATES_SQL}))"""

def identity_ended(identity: Mapping[str, Any] | None) -> bool:
    """``ENDED_PULL_REQUEST`` for an identity row already read, e.g. the one
    ``routing_registrations`` returns; a test holds the two to one answer."""
    if not identity or (identity.get("integration"), identity.get("type")) != (
            GITHUB_INTEGRATION, PR_TYPE):
        return False
    metadata = identity.get("metadata") or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except ValueError:
            return False
    return isinstance(metadata, dict) and metadata.get("pr_state") in PR_TERMINAL_STATES


#: A GitHub pull request that has not ended yet.
PULL_REQUEST_EXEMPT = f"""(i.integration='{GITHUB_INTEGRATION}'
               AND i.type='{PR_TYPE}'
               AND NOT {ENDED_PULL_REQUEST})"""


def not_exempt_clause(*exemptions: str) -> str:
    """Return the SQL true when ``r``'s artifact matches none of ``exemptions``.

    Correlates on ``r``, so every caller must alias artifact_registrations as
    ``r``. Taking the exempt kinds as arguments lets a schema migration pin
    itself to the exemptions that existed when it was written, instead of
    silently widening whenever a new kind is added here.
    """
    return f"""NOT EXISTS (
        SELECT 1
        FROM artifact_identities AS i
        WHERE i.id=r.artifact_id
          AND ({' OR '.join(exemptions)})
    )"""


RETIREMENT_NOT_EXEMPT_CLAUSE = not_exempt_clause(
    USER_COMMENTED_DOCUMENT_EXEMPT, PULL_REQUEST_EXEMPT
)


def artifact_ended_clause(artifact_id_column: str) -> str:
    """Return the SQL true when the artifact in ``artifact_id_column`` has ended.

    An ended artifact's retirement is a fact about the artifact itself, not
    about the task that registered it, so a re-registration must not undo it
    (``registry.identity_bridge``).
    """
    return f"""EXISTS (
        SELECT 1
        FROM artifact_identities AS i
        WHERE i.id={artifact_id_column}
          AND {ENDED_PULL_REQUEST}
    )"""


@dataclass(frozen=True)
class RetirementRestore:
    """The retirement to reapply on unpin."""

    surfacing: str


def retirement_to_restore(
    db, registration_id
) -> RetirementRestore | None:
    """Return the retirement to reapply on unpin, or None if none applies.

    ``retired_at`` is the single, permanent record of a row's
    retirement, whether its originating node settled, its artifact ended, or
    only an ownership change put the row there
    (``registry.node_retirement``). Pinning never clears it (see
    ``registry.pinning``), so it stays exactly as retirement wrote it until a
    real re-registration clears a task-scoped one
    (``registry.identity_bridge``). Unpin simply
    re-reads that one column — there is nothing to stash, replay, or match
    an origin against. A row is still gated on current exemption: one that
    became exempt after it was stamped stays visible, and so does an exempt
    row an ownership change retired: once pinned it is there by the user's
    choice. A later deliberate
    quiet mirrors the pin branch's own guard and stays a removal
    (``update_pinned`` docstring).
    """
    row = db.execute(
        f"""SELECT r.surfacing
             FROM artifact_registrations AS r
            WHERE r.id=? AND r.retired_at IS NOT NULL
              AND {RETIREMENT_NOT_EXEMPT_CLAUSE}""",
        (registration_id,),
    ).fetchone()
    if row is None:
        return None
    surfacing = "settled" if row["surfacing"] == "visible" else row["surfacing"]
    return RetirementRestore(surfacing=surfacing)
