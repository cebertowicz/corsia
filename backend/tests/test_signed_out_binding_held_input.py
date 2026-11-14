"""A held input keeps its own account's cause, with the switch on limit or off.

A thread bound to an account that cannot run it is refused by launch admission, and
``account_rollover.plan_rollover`` moves a bound source only for a usage limit or a project denial,
so the claim stays there. Two reads used to answer otherwise, and #2386's held chat followed:

* ``connections.account_readiness`` scored the account a *future* turn would roll onto, so the held
  head read ready and ``infra.queued_input_settlement.offer_account_head_notice`` returned before
  offering the notice whose action is the recovery;
* ``connections.account_holds.affected_thread_ids`` named only the threads whose next turn selects
  the changed account, so a lifecycle change never wrote the durable hold row that
  ``frontend/src/lib/components/heldAccountNotice.ts`` reads for its existing Sign in row.

The two answers can differ only where the changed account leaves the rollover order at all:
``ordered_accounts`` keeps every permitted, non-removed account in it (saved order first) and
``account_model_status`` reports a lifecycle reason over a usage limit, so the sources that leave
it are the signed-out, the removed and the project-denied. Those three are pinned here against the
movable causes, for both CLI vendors, through the real lifecycle, readiness and admission owners:
the held head answers with its claim-side account and cause; a lifecycle change re-holds the thread
whose binding keeps its claim there, switch on and off; a limit and a movable denial keep reading
the account the next turn runs on; a person's pending choice keeps its precedence without masking
its binding; the execution reasons for the bound account stay its own cause; and a ``project_id`` of
``''`` stays no subject, exactly as the canonical selection already answers it.
"""
from __future__ import annotations

from pathlib import Path
import json
import shutil
from types import SimpleNamespace

import pytest

from connections import harness_usage, read_account_readiness
from connections.account_evidence import mark_discovery
from connections.account_holds import affected_thread_ids, model_account_hold
from connections.account_lifecycle import change_state
from connections.account_moves import hold_pending_account
from connections.account_policy import account_reason, select_account, turn_account
from connections.account_receipts import capture_receipt
from connections.account_rollover import NOT_PERMITTED
from connections.vendor_accounts import add_account, get_account, set_account_state
from prompts.plan_wakes import account_wake_fields
from vendor_account_fixtures import accounts, set_legacy_policy  # noqa: F401

PROJECT = 'destination-project'
LIMIT = dict(reason='usage_limit', resets_at='2099-01-01T00:00:00+00:00', source='rate_limit_update')
#: Each cause the copy already carries, with the ``user``-actor form ``account_wake_fields`` builds.
CAUSES = {
    'signed_out': ('account_signed_out', 'it is signed out',
                   'The user can sign in to the permitted account through the account controls.'),
    'removed': ('account_removed', 'the account was removed',
                'The user can choose another permitted account if one is available; otherwise switch model.'),
}
#: Both CLI vendors, as every account suite names them.
VENDORS = pytest.mark.parametrize('provider', ['codex', 'claude-code'])
SWITCHES = pytest.mark.parametrize('switch', [True, False], ids=['switch-on', 'switch-off'])
STATES = pytest.mark.parametrize('state', ['signed_out', 'removed'], ids=['signed-out', 'removed'])


def _home(provider: str, account: dict) -> None:
    """The account's custom source home, as an available login leaves it."""
    source = Path(account['source_home'])
    source.mkdir(parents=True, exist_ok=True)
    if provider == 'codex':
        source.joinpath('auth.json').write_text(
            json.dumps({'auth_mode': 'chatgpt', 'tokens': {'access_token': 'synthetic'}}))


def _discovered(db, provider: str, account: dict, model: str, models: dict | None = None) -> None:
    """A current login whose discovery names the thread's model (or, with ``models``, names none)."""
    db.execute('UPDATE models SET context_window=200000 WHERE id=?', (model,))
    receipt = capture_receipt(provider, account_id=account['id'], db=db)
    assert mark_discovery(account['id'], receipt=receipt, status='ready',
                          models=models if models is not None else {model: {'available': True}}, db=db)
    db.commit()


def _signed_in(account, db) -> dict:
    return set_account_state(account['id'], 'signed_in', expected_revision=account['revision'], db=db)


def _stage(accounts, provider: str, *, switch: bool = True) -> SimpleNamespace:  # noqa: F811
    """A thread bound to one account in a project with one other usable account, switch as asked.

    The bound account is signed in and healthy here; each test changes it the way its journey does.
    """
    db = accounts[0]
    if provider == 'claude-code':
        from connections.models import upsert_connection

        model, thread = 'claude-code/test', 'claude-bound'
        upsert_connection(type='cli', provider='claude-code', status='available', config={'binary': '/bin/synthetic'})
        db.execute("INSERT INTO models(id,display_name,litellm_model,provider,provider_model_id,harness,created_at,updated_at) "
                   "VALUES (?,'Test',?,'claude-code','test','claude-code','t','t')", (model, model))
        db.execute("INSERT INTO threads(id,user_id,model,project_id,created_at,updated_at) "
                   "VALUES (?,'user',?,?,'t','t')", (thread, model, PROJECT))
        db.commit()
        bound = _signed_in(add_account('claude-code', 'Bound', db=db), db)
        sibling = _signed_in(add_account('claude-code', 'Sibling', db=db), db)
    else:
        _db, _default, bound, sibling = accounts  # noqa: F811
        model, thread = 'codex/test', 'destination'
    for account in (bound, sibling):
        _home(provider, account)
    _discovered(db, provider, bound, model)
    _discovered(db, provider, sibling, model)
    set_legacy_policy(PROJECT, provider, allowlist=[bound['id'], sibling['id']],
                      default_account_id=bound['id'], switch_on_limit=switch, db=db)
    harness_usage.record_allowed(provider=provider, source='discovery', db=db,
                                 receipt=capture_receipt(provider, account_id=sibling['id'], db=db))
    select_account(thread, provider, account_id=bound['id'], db=db)
    return SimpleNamespace(db=db, provider=provider, thread=thread, model=model,
                           bound=get_account(bound['id'], db=db), sibling=get_account(sibling['id'], db=db))


def _lifecycle(staged, state: str = 'signed_out') -> None:
    """The production lifecycle write: it is the only writer of the durable hold rows on a change."""
    change_state(staged.bound['id'], state, expected_revision=staged.bound['revision'], db=staged.db)


def _limit_bound(staged) -> None:
    """A stored limit on the bound account: the cause that makes the next turn move off it."""
    harness_usage.record_limited(provider=staged.provider, db=staged.db, **LIMIT,
                                 receipt=capture_receipt(staged.provider, account_id=staged.bound['id'], db=staged.db))


def _deny_bound(staged, *, switch: bool) -> None:
    """Project policy stops permitting the bound account, switch as asked."""
    set_legacy_policy(PROJECT, staged.provider, allowlist=[staged.sibling['id']],
                      default_account_id=staged.sibling['id'], switch_on_limit=switch, db=staged.db)


def _queue(staged, message_id: str = 'held') -> None:
    from infra.input_admission import accept_thread_input

    accept_thread_input(staged.thread, role='user', content=message_id, intent='apply_now',
                        message_id=message_id, db=staged.db)


def _holds(staged) -> list[tuple[str, str, str]]:
    return [(str(row['provider']), str(row['account_id']), str(row['reason'])) for row in staged.db.execute(
        'SELECT provider,account_id,reason FROM thread_vendor_account_holds WHERE thread_id=?', (staged.thread,))]


def _head(staged):
    return staged.db.execute(
        "SELECT delivery_state,delivery_error,write_outcome FROM messages WHERE id='held'").fetchone()


def _reason(staged) -> str | None:
    """The bound account's own reason under the project policy, from the one evaluator."""
    return account_reason(get_account(staged.bound['id'], db=staged.db), provider=staged.provider,
                          project_ids=[PROJECT], execution=True, model=staged.model, db=staged.db)


def _readiness(staged):
    return read_account_readiness([staged.thread], db=staged.db)[staged.thread]


@VENDORS
@SWITCHES
@STATES
def test_held_head_answers_with_its_own_claim_side_account_and_cause(accounts, provider, switch, state):  # noqa: F811
    """Admission refuses the head for its own account's cause, so readiness names that cause too.

    With the switch on the project's next turn runs on the sibling; that is not this head's account,
    and the notice's action has to be the recovery the head actually waits on.
    """
    cause, phrase, action = CAUSES[state]
    staged = _stage(accounts, provider, switch=switch)
    _lifecycle(staged, state)
    _queue(staged)

    assert _head(staged)[:] == ('queued', cause, 'proved_no_write')
    assert model_account_hold(staged.thread, staged.model, db=staged.db) == cause
    assert _readiness(staged) == {'ready': False, 'cause': cause}
    assert account_wake_fields(_readiness(staged)) == ('unready', {
        'phrase': phrase, 'cause': cause, 'action': action})


@VENDORS
@SWITCHES
@STATES
def test_a_lifecycle_change_reholds_the_thread_whose_binding_keeps_its_claim(accounts, provider, switch, state):  # noqa: F811
    """The fan-out is the only writer of the hold row the existing recovery row reads.

    A thread bound to the account that changed is a subject of the change whatever the project's
    next turn would otherwise run on, so the row exists without touching the thread.
    """
    cause = CAUSES[state][0]
    staged = _stage(accounts, provider, switch=switch)
    assert _holds(staged) == []

    _lifecycle(staged, state)
    _queue(staged)

    assert staged.thread in affected_thread_ids(staged.bound['id'], db=staged.db)
    assert _holds(staged) == [(provider, staged.bound['id'], cause)]
    assert _head(staged)[:] == ('queued', cause, 'proved_no_write')


@VENDORS
def test_a_limit_still_moves_the_thread_off_its_own_account(accounts, provider):  # noqa: F811
    """A limited source is movable: only its destination account is a subject, never the source.

    Neither read widens for the cause the switch exists to serve, so the head keeps reading ready on
    the account its next turn runs on.
    """
    staged = _stage(accounts, provider)
    _limit_bound(staged)
    _queue(staged)

    assert turn_account(provider, thread_id=staged.thread, model=staged.model, db=staged.db)[0] \
        == staged.sibling['id']
    assert affected_thread_ids(staged.bound['id'], db=staged.db) == []
    assert affected_thread_ids(staged.sibling['id'], db=staged.db) == [staged.thread]
    assert _readiness(staged) == {'ready': True, 'cause': None}


@VENDORS
@SWITCHES
def test_a_project_denial_reads_the_move_it_can_make(accounts, provider, switch):  # noqa: F811
    """A denial is movable only when something can take the claim, and the reads follow that.

    With the switch on ``plan_rollover`` moves the denied source, so the head keeps reading ready
    rather than starting an ask to restore policy the platform is already applying, and only the
    destination account is a subject; with it off nothing can move, so the denial and its own
    account are what the head waits on.
    """
    staged = _stage(accounts, provider, switch=switch)
    _deny_bound(staged, switch=switch)
    _queue(staged)

    assert _reason(staged) == NOT_PERMITTED
    assert (staged.thread in affected_thread_ids(staged.bound['id'], db=staged.db)) is not switch
    assert _readiness(staged) == ({'ready': True, 'cause': None} if switch
                                  else {'ready': False, 'cause': NOT_PERMITTED})


@VENDORS
def test_a_pending_choice_keeps_precedence_without_masking_its_binding(accounts, provider):  # noqa: F811
    """A person's choice answers the next turn; the binding still owes the held input its row.

    ``apply_pending_accounts`` runs at the claim (``infra/thread_inputs.py:281``), so until a claim
    moves the binding, admission answers for the bound account and the sign-out re-holds this thread
    for that account.
    """
    staged = _stage(accounts, provider)
    from infra.database import write_transaction

    with write_transaction(staged.db, scope='test_pending_choice'):
        hold_pending_account(staged.thread, provider, staged.sibling['id'], db=staged.db)
    assert turn_account(provider, thread_id=staged.thread, model=staged.model, db=staged.db)[0] \
        == staged.sibling['id']

    _lifecycle(staged)
    _queue(staged)

    assert staged.thread in affected_thread_ids(staged.bound['id'], db=staged.db)
    assert _holds(staged) == [(provider, staged.bound['id'], 'account_signed_out')]
    assert model_account_hold(staged.thread, staged.model, db=staged.db) == 'account_signed_out'


@VENDORS
def test_a_binding_that_never_discovered_the_model_keeps_its_own_cause(accounts, provider):  # noqa: F811
    """Scoring the claim-side account must not lose the execution reasons it carries.

    The model still weighs the claim evaluator, so a bound account that never discovered it reads
    ``account_model_not_discovered`` for itself; its selection cannot move, since
    ``account_model_status`` reports this reason over a usage limit.
    """
    staged = _stage(accounts, provider)
    _discovered(staged.db, provider, staged.bound, staged.model, models={})
    _queue(staged)

    assert affected_thread_ids(staged.bound['id'], db=staged.db) == [staged.thread]
    assert _head(staged)[1] == 'account_model_not_discovered'
    assert _readiness(staged) == {'ready': False, 'cause': 'account_model_not_discovered'}
    assert account_wake_fields(_readiness(staged))[1]['cause'] == 'account_model_not_discovered'


@VENDORS
def test_a_binding_with_a_missing_home_keeps_its_own_cause(accounts, provider):  # noqa: F811
    """A home launch would refuse is a hold before any move, so the head reads that cause and is re-held."""
    staged = _stage(accounts, provider)
    shutil.rmtree(Path(staged.bound['source_home']))
    _queue(staged)

    assert affected_thread_ids(staged.bound['id'], db=staged.db) == [staged.thread]
    assert _head(staged)[1] == 'account_home_mismatch'
    assert _readiness(staged) == {'ready': False, 'cause': 'account_home_mismatch'}
    assert model_account_hold(staged.thread, staged.model, db=staged.db) == 'account_home_mismatch'


@VENDORS
def test_a_literal_empty_project_stays_no_subject(accounts, provider):  # noqa: F811
    """``''`` is not a project: the canonical selection answers it as a missing project, so no row.

    ``scoped_turn_account`` returns no account for it, and the fan-out keeps that answer rather than
    re-deriving a reason from the workspace policy ``effective_policy`` reads for ``''``.
    """
    staged = _stage(accounts, provider)
    staged.db.execute('UPDATE threads SET project_id=? WHERE id=?', ('', staged.thread))
    staged.db.commit()
    _lifecycle(staged)
    _queue(staged)

    assert staged.thread not in affected_thread_ids(staged.bound['id'], db=staged.db)
