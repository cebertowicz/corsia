"""Account-owned discovery and projection metadata, with no credential material."""
from __future__ import annotations

import copy
import json
from pathlib import Path

from connections.account_home import missing_home_reason
from connections.account_receipts import ReceiptFence, receipt_is_current
from connections.vendor_accounts import get_account
from infra.time import utc_now_iso


def _with_defaults(record: dict, account: dict) -> dict:
    record.setdefault('version', 1)
    record.setdefault('login_seq', 0)
    record.setdefault('login_revision', account['revision'])
    record.setdefault('auth_state', 'unknown')
    record.setdefault('discovery', {'status': 'unknown', 'cause': None, 'observed_at': None,
                                  'harness_version': None, 'receipt': None,
                                  'cache_receipt': None, 'models': {}})
    record.setdefault('auth_projection', {'failed': None, 'stale': [], 'homes': {}})
    return record


def discovery_record(account: dict) -> dict:
    """The account's discovery record with defaults filled, as a private copy to change."""
    raw = account.get('discovery')
    return _with_defaults(copy.deepcopy(raw) if isinstance(raw, dict) else {}, account)


def discovery_view(account: dict) -> dict:
    """The same record for a reader: only the top level is copied, so nothing in it may be changed.

    A reader of one answer (readiness, a model's availability) must not pay a deep copy of
    every discovered model per call; a caller that mutates takes ``discovery_record``.
    """
    raw = account.get('discovery')
    return _with_defaults(dict(raw) if isinstance(raw, dict) else {}, account)


def store_record(account_id: str, record: dict, *, db) -> None:
    """Only the canonical account transaction may publish this private record."""
    db.execute('UPDATE vendor_accounts SET discovery_json=? WHERE id=?',
               (json.dumps(record, sort_keys=True), account_id))


def invalidate_discovery(account: dict, *, login_seq: int, auth_state: str, db) -> None:
    record = discovery_record(account)
    record.update(login_seq=login_seq, login_revision=account['revision'], auth_state=auth_state)
    record['discovery'] = {'status': 'unknown', 'cause': None, 'observed_at': None,
                           'harness_version': None, 'receipt': None,
                           'cache_receipt': None, 'models': {}}
    store_record(account['id'], record, db=db)


def change_login(account: dict, *, revision: int, db) -> None:
    """New explicit custom login invalidates receipts and retains managed cleanup evidence."""
    record = discovery_record(account)
    generation = record['login_seq'] + 1
    projection = record['auth_projection']
    stale = projection['stale']
    for home in projection['homes'].values():
        stale.append({key: home[key] for key in ('source', 'destination', 'home', 'kind')} | {
            'old_login_seq': home['login_seq'], 'cleanup_login_seq': generation,
            'cleanup_revision': revision, 'phase': 'pending', 'at': utc_now_iso()})
    for item in stale:
        if item['phase'] == 'pending':
            item.update(cleanup_login_seq=generation, cleanup_revision=revision)
    record.update(login_seq=generation, login_revision=revision, auth_state='known')
    record['discovery'] = {'status': 'unknown', 'cause': None, 'observed_at': None,
                           'harness_version': None, 'receipt': None,
                           'cache_receipt': None, 'models': {}}
    record['auth_projection'] = {'failed': None, 'stale': stale, 'homes': {}}
    store_record(account['id'], record, db=db)


def mark_discovery(account_id: str, *, receipt: ReceiptFence, status: str,
                   cause: str | None = None, models: dict | None = None,
                   harness_version: str | None = None, cache_receipt: dict | None = None, db) -> bool:
    """Publish pending/success/failure only for the captured lifecycle AND login."""
    if not receipt_is_current(receipt, db=db) or receipt.account_id != account_id:
        return False
    account = get_account(account_id, db=db)
    record = discovery_record(account)
    fence = {'account_revision': receipt.account_revision, 'login_seq': receipt.login_seq}
    last = record['discovery']
    if status == 'failed' and last.get('status') == 'ready' and last.get('receipt') == fence:
        # A run that cannot finish says nothing new about this login's models: the last good
        # discovery stays until a later run succeeds, and the failure rides along beside it.
        last['refresh_failure'] = {'cause': cause, 'observed_at': utc_now_iso()}
        store_record(account_id, record, db=db)
        return True
    record['discovery'] = {'status': status, 'cause': cause, 'observed_at': utc_now_iso(),
                           'harness_version': harness_version,
                           'receipt': {'account_revision': receipt.account_revision, 'login_seq': receipt.login_seq},
                           'cache_receipt': cache_receipt, 'models': models or {}}
    store_record(account_id, record, db=db)
    return True


def projection_failure_moved(account: dict, failure: dict) -> bool:
    """Whether the failure names an earlier account source home."""
    source_home, failed_source = account.get('source_home'), failure.get('source')
    return bool(source_home and failed_source and failed_source != str(Path(source_home).absolute() / 'auth.json'))


def readiness_reason(account: dict, *, model: str | None = None) -> str | None:
    """One evaluator for claim, catalog and batched advisory readers.

    Reads stored records, plus at most two stats of a custom account's source home
    (``missing_home_reason``); never a vendor or network call.
    """
    from connections.account_receipts import account_receipt
    from connections.vendor_accounts import DEFAULT_COMPARISON

    record = discovery_view(account)
    # A built-in Default runs on the ambient login its own CLI already holds, and
    # its models come from the vendor catalog rather than from an account-scoped
    # discovery record. Nothing that record can do may hide those rows.
    ambient = account['source_home'] is None
    if ambient:
        comparison = account['identity'].get(DEFAULT_COMPARISON)
        if not isinstance(comparison, dict) or 'auth_state' not in comparison:
            return None  # Existing unobserved legacy Default compatibility.
        if comparison['auth_state'] != 'known':
            return 'account_login_unverified'
    # A custom account whose homes cannot launch is held before its receipt is read: the
    # same rule launch validation applies, so a hold never lifts on a home launch refuses.
    if reason := missing_home_reason(account):
        return reason
    receipt = account_receipt(account)
    if receipt is None:
        return 'account_model_not_discovered'
    projection = record['auth_projection']
    failed = projection['failed']
    if isinstance(failed, dict) and not projection_failure_moved(account, failed) and (
            failed.get('account_revision'), failed.get('login_seq')) == (
            receipt.account_revision, receipt.login_seq):
        return failed['cause']
    if any(item.get('phase') == 'pending' for item in projection['stale']):
        return 'codex_auth_projection_changed'
    if ambient:
        return None
    discovery = record['discovery']
    if discovery.get('status') != 'ready' or discovery.get('receipt') != {
            'account_revision': receipt.account_revision, 'login_seq': receipt.login_seq}:
        return 'account_model_not_discovered'
    if model is not None and model not in discovery.get('models', {}):
        return 'account_model_not_discovered'
    return None
