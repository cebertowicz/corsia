"""Reads and claim guards for releasing a harness account-limit scope.

The reset deadline lives on the limited verdict itself (its ``release`` marker,
owned by ``connections.harness_usage``); nothing here stores a second copy.
These helpers find the held FIFO heads a scope release drives and guard the
probe claim inside its write transaction.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from connections.usage_verdicts import LimitSubject

_CANDIDATE_PAGE = 128

#: Probe attempts and the base backoff between them for a stored limit release.
LIMIT_RELEASE_MAX_ATTEMPTS = 5
LIMIT_RELEASE_BACKOFF_SECONDS = 180

_QUEUED_HEADS = """FROM threads AS t JOIN messages AS m ON m.thread_id=t.id
           WHERE t.lifecycle_state='active' AND t.model IS NOT NULL AND trim(t.model)<>''
             AND m.delivery_state='queued' AND m.active_turn_id IS NULL
             AND m.input_order IS NOT NULL
             AND m.target_execution_epoch=t.execution_epoch AND m.target_model=t.model"""


@dataclass(frozen=True, slots=True)
class CandidatePage:
    """Canonically filtered rows plus continuation of the raw SQL keyset page.

    ``next_after`` advances over the raw page even when no row belongs to the
    requested account. Callers must use it rather than infer exhaustion from
    the filtered row count.
    """
    rows: tuple[tuple[str, str, str], ...]
    next_after: tuple[str, str] | None

    def __iter__(self):
        return iter(self.rows)

    def __len__(self):
        return len(self.rows)

    def __bool__(self):
        return bool(self.rows)

    def __getitem__(self, index):
        return self.rows[index]


@dataclass(frozen=True, slots=True)
class LimitProbe:
    """One probe for an expired scope's reset window; ``source`` names its driver."""

    subject: LimitSubject
    resets_at: str
    source: str


@dataclass(frozen=True, slots=True)
class ExpiredScopeSettlement:
    """What a terminal settlement derived for one expired scope: release or chain."""

    subject: LimitSubject
    resets_at: str
    action: str


def model_connection(model: str, db) -> str | None:
    """The harness connection (account key) that serves ``model``, if any."""
    from llm.model_status import harness_connection_for_model

    return harness_connection_for_model(model, db=db)


def _matching_models(subject: LimitSubject, db) -> list[str]:
    """Resolve each distinct queued-head model once and keep those in ``subject``."""
    from connections.usage_verdicts import scope_matches

    rows = db.execute(f"SELECT DISTINCT t.model AS model {_QUEUED_HEADS}").fetchall()
    return [
        str(row["model"]) for row in rows
        if scope_matches(str(row["model"]), subject.scope_key)
        and model_connection(str(row["model"]), db) == subject.provider
    ]


def candidate_rows(
    subject: LimitSubject, *, after: tuple[str, str] | None = None, db=None,
) -> CandidatePage:
    """Read one raw SQL page, canonically filter it, and retain its cursor.

    The model filter runs in SQL on the resolved model set. ``next_after`` is
    the raw page cursor, so a later page never re-reads earlier rows even when
    canonical filtering rejects every row in this page.
    """
    from infra.database import get_db

    owned = db is None
    db = db or get_db()
    try:
        from connections.account_receipts import subject_is_current

        if not subject_is_current(subject, db=db):
            return CandidatePage((), None)
        models = _matching_models(subject, db)
        if not models:
            return CandidatePage((), None)
        marks = ",".join("?" for _ in models)
        keyset = "AND (MIN(m.created_at || ':' || printf('%020d',m.input_order)),t.id) > (?,?)"
        rows = db.execute(
            f"""SELECT t.id AS thread_id,t.model AS model,
                      MIN(m.created_at || ':' || printf('%020d',m.input_order)) AS head_key
               {_QUEUED_HEADS} AND t.model IN ({marks})
               GROUP BY t.id,t.model
               HAVING SUM(CASE WHEN m.role<>'system'
                         OR json_extract(m.metadata,'$.type') IS NOT 'context_event'
                         OR json_extract(m.metadata,'$.event_disposition') IS NOT 'passive'
                         THEN 1 ELSE 0 END)>0 {keyset if after is not None else ""}
               ORDER BY head_key,t.id LIMIT ?""", (*models, *(after or ()), _CANDIDATE_PAGE),
        ).fetchall()
        from connections.account_policy import turn_account
        candidates = tuple(
            (str(row["thread_id"]), str(row["model"]), str(row["head_key"])) for row in rows
            if turn_account(subject.provider, thread_id=str(row["thread_id"]),
                            model=str(row["model"]), db=db)[0] == subject.account_id
        )
        next_after = (str(rows[-1]["head_key"]), str(rows[-1]["thread_id"])) if len(rows) == _CANDIDATE_PAGE else None
        return CandidatePage(candidates, next_after)
    finally:
        if owned:
            db.close()


def limited_workspace_threads(provider: str, *, db) -> list[str]:
    """Idle workspace-following threads whose FIFO head waits on ``provider``."""
    from infra.held_admission import limited_admission
    from infra.vendor_turn_binding import model_vendor

    rows = db.execute("""SELECT t.id AS thread_id,t.model AS model
        FROM threads t JOIN messages m ON m.thread_id=t.id
        LEFT JOIN project_policy_overrides o ON o.project_id=t.project_id
        WHERE t.lifecycle_state='active' AND t.model IS NOT NULL AND trim(t.model)<>''
          AND m.delivery_state='queued' AND m.active_turn_id IS NULL AND m.input_order IS NOT NULL
          AND m.target_execution_epoch=t.execution_epoch AND m.target_model=t.model
          AND o.project_id IS NULL
          AND NOT EXISTS (SELECT 1 FROM active_turns a WHERE a.thread_id=t.id AND a.status='running')
        GROUP BY t.id,t.model
        ORDER BY MIN(m.created_at || ':' || printf('%020d',m.input_order)),t.id""").fetchall()
    return [
        str(row["thread_id"]) for row in rows
        if model_vendor(str(row["model"]), db=db) == provider
        and limited_admission(str(row["thread_id"]), str(row["model"]), db=db) is not None
    ]


def limited_project_threads(project_id: str, provider: str, *, db) -> list[str]:
    """Idle project threads whose claimable FIFO head waits on a limited ``provider`` account.

    Oldest head first. A pure read: ``start_idle_turn`` still decides whether
    each one moves or stays held.
    """
    from infra.held_admission import limited_admission
    from infra.vendor_turn_binding import model_vendor

    rows = db.execute(
        f"""SELECT t.id AS thread_id,t.model AS model {_QUEUED_HEADS} AND t.project_id=?
              AND NOT EXISTS (SELECT 1 FROM active_turns a WHERE a.thread_id=t.id AND a.status='running')
            GROUP BY t.id,t.model
            ORDER BY MIN(m.created_at || ':' || printf('%020d',m.input_order)),t.id""",
        (project_id,),
    ).fetchall()
    return [
        str(row["thread_id"]) for row in rows
        if model_vendor(str(row["model"]), db=db) == provider
        and limited_admission(str(row["thread_id"]), str(row["model"]), db=db) is not None
    ]


def _scope_verdict(subject: LimitSubject, db) -> dict | None:
    from connections.harness_usage import stored_usage
    from connections.usage_verdicts import verdicts

    from connections.account_receipts import subject_is_current

    if not subject_is_current(subject, db=db):
        return None
    usage = stored_usage(provider=subject.provider, account_id=subject.account_id, db=db)
    return verdicts(usage).get(subject.scope_key) if isinstance(usage, dict) else None


def probe_claim_allowed(probe: LimitProbe, model: str, *, thread_id: str | None = None, db) -> bool:
    """Check, inside the claim write, that this head may probe the expired window.

    The model must be in the probed subject, the scope's verdict must still be
    the same expired limit, and no Turn on that subject may be running.
    """
    from connections.usage_verdicts import parse_time, scope_matches

    subject = probe.subject
    if not scope_matches(model, subject.scope_key) or model_connection(model, db) != subject.provider:
        return False
    from connections.account_policy import chosen_account_id

    if thread_id is not None and chosen_account_id(thread_id, subject.provider, db=db) != subject.account_id:
        return False
    verdict = _scope_verdict(subject, db) or {}
    reset = parse_time(verdict.get("resets_at"))
    if (
        verdict.get("state") != "limited" or reset is None
        or reset != parse_time(probe.resets_at) or reset > datetime.now(timezone.utc)
    ):
        return False
    return not running_turns_on_scope(subject, db=db)


def running_turns_on_scope(subject: LimitSubject, *, db) -> list[tuple[str, str]]:
    """(thread_id, turn_id) of every running Turn on ``subject``'s exact login, oldest first."""
    from connections.usage_verdicts import scope_matches
    from connections.account_receipts import subject_is_current
    from infra.vendor_login import read_turn_receipt

    if not subject_is_current(subject, db=db):
        return []
    bound: dict[str, bool] = {}
    running = []
    for row in db.execute(
        "SELECT id,thread_id,model,vendor_account_id FROM active_turns WHERE status='running' ORDER BY turn_order"
    ).fetchall():
        model = str(row["model"])
        if model not in bound:
            bound[model] = (
                scope_matches(model, subject.scope_key)
                and model_connection(model, db) == subject.provider
            )
        if (bound[model] and row['vendor_account_id'] == subject.account_id
                and read_turn_receipt(str(row['id']), db=db) == subject.receipt):
            running.append((str(row["thread_id"]), str(row["id"])))
    return running


def apply_plan_limit_effect(
    provider: str, plan_limits: list[tuple[str | None, dict]], *, receipt, db,
) -> None:
    """Stop each new plan-limit scope's running Turns once, after the verdict commits.

    The Stops are registered on the verdict's own write transaction, so the
    ``limited`` verdict is durable before any Stop terminal and its successor
    claim can read it. The verdict carries its own reset deadline, so nothing
    is armed here.
    """
    from connections.usage_verdicts import scope_key
    from infra.connection_limit_drive import schedule_plan_limit_stops
    from infra.write_effects import after_commit

    turns: list[tuple[str, str]] = []
    for scope, _verdict in plan_limits:
        from connections.account_receipts import limit_subject

        subject = limit_subject(provider, scope_key(scope), receipt=receipt)
        turns.extend(turn for turn in running_turns_on_scope(subject, db=db) if turn not in turns)
    if turns:
        after_commit(db, lambda turns=tuple(turns): schedule_plan_limit_stops(turns))


def claim_probe(probe: LimitProbe, thread_id: str, *, db) -> bool:
    """Compare-and-set the window's ``started`` marker in the probe's claim write.

    Two sweeps, or a sweep and a sign-in release, cannot both start a probe for
    one window: the loser sees the marker and claims nothing.
    """
    from connections.harness_usage import write_release

    return write_release(
        probe.subject, probe.resets_at, "started",
        at=datetime.now(timezone.utc).isoformat(), thread_id=thread_id, db=db,
    )


def startup_expired_admission(thread_id: str, model: str, *, db):
    """Hold a head whose expired scope a release will probe, during startup recovery.

    Only a window still due is held; the sweep phase then drives its probe. A
    window already settled (no eligible head, exhausted) admits normally.
    """
    from connections.harness_usage import stored_usage
    from connections.usage_verdicts import ACCOUNT_SCOPE, release_due, scope_matches
    from infra.thread_input_types import HeldAdmission

    connection = model_connection(model, db)
    from connections.account_policy import chosen_account_id

    identity = chosen_account_id(thread_id, connection, db=db) if connection is not None else None
    if connection is None or identity is None:
        return None
    usage = stored_usage(provider=connection, account_id=identity, db=db)
    if not isinstance(usage, dict):
        return None
    due = [
        item for item in release_due(
            usage, datetime.now(timezone.utc), account=LimitSubject(connection, ACCOUNT_SCOPE, identity),
            max_attempts=LIMIT_RELEASE_MAX_ATTEMPTS, backoff_seconds=LIMIT_RELEASE_BACKOFF_SECONDS,
        )
        if not item.exhausted and scope_matches(model, item.subject.scope_key)
    ]
    if not due:
        return None
    return HeldAdmission(connection, due[0].reason, due[0].resets_at, thread_id, model)
