"""SQL-level retirement eligibility shared by registration storage seams.

One owner for the retirement-exemption predicate so node settlement, pin
transitions, and pane payloads cannot drift apart.

A row is exempt when the artifact outlives the task that happened to touch
it. Three kinds qualify: a durable document in a ``shared/`` tree, a durable
workspace document the user has commented on, and a GitHub pull request that
is still open, whose own lifecycle ends at merge or close rather than
whenever the task that opened it settles. Once a pull
request has ended it is no longer exempt: its end is itself a retirement
(``registry.node_retirement.retire_registrations_for_ended_artifact``).
Everything else -- a thread's own folder, working-tray scratch, repo/worktree
files, uploads -- is task-scoped and still retires.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass

from infra.github_pr_identity import GITHUB_INTEGRATION, PR_TYPE

#: A durable document living under the project workspace: the clause both
#: workspace exemptions build on, and the one schema migration 21->22 pins.
DURABLE_WORKSPACE_EXEMPT = """(i.retention='durable'
               AND json_extract(i.metadata, '$.zone')='workspace')"""

_SHARED_DOCUMENT_FUNCTION = "corsia_shared_document"

#: A durable document under the workspace's ``shared/`` or a project's
#: ``shared/``. "Shared" has one definition,
#: ``registry.shared_principals.shared_roots``; this clause calls it through
#: a connection function, so a connection must be given it with
#: ``bind_shared_documents`` before running any statement that uses the
#: clause. It reads the same location the pane and ownership read: the
#: stored path, else the identity.
SHARED_DOCUMENT_EXEMPT = f"""({DURABLE_WORKSPACE_EXEMPT}
               AND {_SHARED_DOCUMENT_FUNCTION}(
                   COALESCE(NULLIF(i.path, ''), i.external_id))=1)"""

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

#: A GitHub pull request that has not ended yet.
PULL_REQUEST_EXEMPT = f"""(i.integration='{GITHUB_INTEGRATION}'
               AND i.type='{PR_TYPE}'
               AND NOT {ENDED_PULL_REQUEST})"""


logger = logging.getLogger(__name__)

#: The shared roots the last bind read.
_shared_roots: tuple = ()

#: Locations already warned about. The pane classifies every row on every
#: read, so an unrepaired location is reported once per process, not per read.
_unclassifiable: set[str] = set()


class SharedRootsUnreadable(RuntimeError):
    """The shared roots could not be read, so no row can be classified.

    Never answered as "shared" or "not shared": retirement acts on one answer
    and unpin, the pane and restore act on the other, so a guess is wrong for
    one of them. Every consumer fails instead, and task settlement, which
    must not fail, hands its rows to the next start
    (``registry.startup_backfill.settlement_retirement``).
    """


def _is_shared_document(location: object) -> int:
    """The connection function: 1 when ``location`` is under a shared root.

    One stored location that cannot be classified is answered as shared, for
    every consumer alike: the row is treated as a shared document until its
    location is repaired. The warning names it, once per process.
    """
    from registry.shared_principals import is_under_shared_roots

    if not isinstance(location, str) or not location:
        return 0
    try:
        return int(is_under_shared_roots(location, list(_shared_roots)))
    except RecursionError:
        # Not about this location: a failing classifier stays loud.
        raise
    # RuntimeError: pathlib cannot expand a ``~user`` it does not know, and it
    # is the only type pathlib raises for that.
    except (OSError, ValueError, RuntimeError):
        if location not in _unclassifiable:
            _unclassifiable.add(location)
            logger.warning(
                "shared-document check failed for %r; keeping its row", location,
                exc_info=True,
            )
        return 1


def bind_shared_documents(db) -> None:
    """Give ``db`` the current shared roots for ``SHARED_DOCUMENT_EXEMPT``.

    The roots are one process-wide value, refreshed here from the projects
    table, because every connection serves the same workspace. The function
    itself is added to a connection once: SQLite refuses to redefine a
    function while any statement on that connection is still open.

    Raises:
        SharedRootsUnreadable: A project row could not be resolved.
    """
    # Imported here: shared_principals imports identity_bridge, which imports
    # this module.
    from infra import config
    from registry.shared_principals import shared_roots

    global _shared_roots
    try:
        _shared_roots = tuple(shared_roots(workspace=config.WORKSPACE, db=db))
    except (OSError, ValueError) as exc:
        raise SharedRootsUnreadable(f"shared roots could not be read: {exc}") from exc
    try:
        db.execute(f"SELECT {_SHARED_DOCUMENT_FUNCTION}(NULL)").fetchone()
    except sqlite3.OperationalError as exc:
        if "no such function" not in str(exc):
            raise
        db.create_function(_SHARED_DOCUMENT_FUNCTION, 1, _is_shared_document)


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
    SHARED_DOCUMENT_EXEMPT, USER_COMMENTED_DOCUMENT_EXEMPT, PULL_REQUEST_EXEMPT
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
    choice, like any other shared document. A later deliberate
    quiet mirrors the pin branch's own guard and stays a removal
    (``update_pinned`` docstring).
    """
    bind_shared_documents(db)
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
