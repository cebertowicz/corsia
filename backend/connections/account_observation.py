"""CAS for Default identity observations; clocks never establish login continuity."""
from __future__ import annotations

from dataclasses import dataclass, field
import json

from connections.account_receipts import ReceiptFence, account_receipt
from connections.vendor_accounts import DEFAULT_COMPARISON, default_account
from infra.database import write_transaction
from infra.time import utc_now_iso


@dataclass(frozen=True, slots=True)
class ObservationToken:
    account_id: str
    account_revision: int
    observation_seq: int
    login_seq: int


@dataclass(frozen=True, slots=True)
class ObservationResult:
    accepted: bool
    login_changed: bool = False
    receipt: ReceiptFence | None = field(default=None, repr=False)


def begin_default_observation(provider: str, *, db) -> ObservationToken:
    account = default_account(provider, db=db)
    record = account['identity'].get(DEFAULT_COMPARISON) or {}
    return ObservationToken(account['id'], account['revision'],
                            record.get('observation_seq', 0), record.get('login_seq', 0))


def _facts(provider: str, identity: dict) -> dict:
    from connections.account_profiles import profile_for

    profile = profile_for(provider)
    facts = {key: identity[key] for key in profile.stored_facts
             if isinstance(identity.get(key), str) and identity[key]}
    return facts if all(key in facts for key in profile.comparison_keys) else {}


def has_comparison_facts(provider: str, identity: dict) -> bool:
    """Whether a read carries every comparison key, so it can stand as an observation."""
    return bool(_facts(provider, identity))


def _comparison_facts(provider: str, facts: dict) -> dict:
    from connections.account_profiles import profile_for

    return {key: facts[key] for key in profile_for(provider).comparison_keys if key in facts}


def _invalidate(account: dict, *, comparison: dict, previous: dict, generation: int, db) -> None:
    from connections.account_evidence import invalidate_discovery
    from connections import harness_usage

    known = _comparison_facts(account['provider'], previous.get('last_known') or previous.get('facts') or {})
    if known and comparison and known != comparison:
        harness_usage.clear_account_usage(account['provider'], account['id'], db=db)
    else:
        # Identity uncertainty alone cannot grant an allowed verdict or remove a limit.
        harness_usage.mark_readings_stale(provider=account['provider'], account_id=account['id'], db=db)
    invalidate_discovery(account, login_seq=generation, auth_state='known' if comparison else 'unknown', db=db)
    from connections.account_holds import refresh_account_holds

    refresh_account_holds(account['id'], db=db)


def _set_default_visibility(previous: dict, identity: dict, facts: dict) -> bool:
    """Preserve visibility only for an identity-only read without facts."""
    before = bool(previous.get('definitely_signed_out'))
    status = identity.get('_login_status', 'unknown')
    if status == 'not_read' and not facts:
        return False
    value = status == 'signed_out'
    previous['definitely_signed_out'] = value
    return value != before


def commit_default_visibility(provider: str, identity: dict, *, token: ObservationToken, db) -> bool:
    """Publish status from an incomplete discovery without replacing comparison facts."""
    with write_transaction(db, scope='vendor_default_visibility') as connection:
        if token != begin_default_observation(provider, db=connection):
            return False
        account = default_account(provider, db=connection)
        stored = dict(account['identity'])
        previous = dict(stored.get(DEFAULT_COMPARISON) or {})
        if not _set_default_visibility(previous, identity, _facts(provider, identity)):
            return True
        previous['observation_seq'] = token.observation_seq + 1
        stored[DEFAULT_COMPARISON] = previous
        connection.execute('UPDATE vendor_accounts SET identity_json=? WHERE id=?',
                           (json.dumps(stored, sort_keys=True), account['id']))
        return True


def commit_default_observation(provider: str, identity: dict, *, token: ObservationToken,
                               schedule_usage_check: bool = True, db) -> ObservationResult:
    with write_transaction(db, scope='vendor_default_identity') as connection:
        if token != begin_default_observation(provider, db=connection):
            return ObservationResult(False)
        account = default_account(provider, db=connection)
        stored = dict(account['identity'])
        previous = stored.get(DEFAULT_COMPARISON) or {}
        facts = _facts(provider, identity)
        prior_state = previous.get('auth_state')
        state = 'known' if facts else 'unknown'
        comparison = _comparison_facts(provider, facts)
        previous_comparison = _comparison_facts(provider, previous.get('facts') or {})
        changed = prior_state != state or (bool(facts) and comparison != previous_comparison)
        generation = token.login_seq + int(changed)
        last_known = facts or previous.get('last_known') or previous.get('facts') or None
        visibility = {'definitely_signed_out': bool(previous.get('definitely_signed_out'))}
        _set_default_visibility(visibility, identity, facts)
        stored[DEFAULT_COMPARISON] = {'facts': facts, 'last_known': last_known,
            'auth_state': state, 'at': utc_now_iso(), **visibility,
            'observation_seq': token.observation_seq + 1, 'login_seq': generation}
        connection.execute('UPDATE vendor_accounts SET identity_json=? WHERE id=?',
                           (json.dumps(stored, sort_keys=True), account['id']))
        if changed:
            _invalidate(account, comparison=comparison, previous=previous, generation=generation, db=connection)
        # Scheduling belongs to the existing canonical sign-in/co-read effect owner.
        # The caller carries schedule_usage_check to that bundle; this snapshot never releases.
        return ObservationResult(True, changed, account_receipt(account | {'identity': stored}))
