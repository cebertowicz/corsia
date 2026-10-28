"""Report whether account verification refreshed that account's model list."""
from __future__ import annotations

import logging

from connections import harness_models
from connections.vendor_accounts import VENDORS
from infra.failure_reference import new_reference

log = logging.getLogger(__name__)


async def custom_account(account_id):
    from connections.vendor_accounts import AccountError, get_account
    from infra.db_async import run_in_db

    account = await run_in_db(get_account, account_id)
    if account['source_home'] is None:
        raise AccountError("the Default account signs in through the vendor's own CLI")
    if account['state'] == 'removed':
        raise AccountError('account was removed; choose another permitted account')
    return account


async def signin_recheck(account_id, verify):
    result = await verify(account_id)
    if result['vendor_signed_in'] is False:
        return None
    if result['vendor_signed_in'] is None:
        return {'account_id': account_id, 'route': None, 'status': 'unavailable', 'cause': result['cause']}
    if result['model_refresh_ok'] is True:
        return {'account_id': account_id, 'route': None, 'status': 'already_signed_in', 'cause': None}
    return {'account_id': account_id, 'route': None, 'status': 'model_refresh_failed',
            'cause': result['model_refresh_cause'], 'ref': result['model_refresh_ref']}


async def refresh_models(account, receipt, binary, current_c6):
    provider, account_id = account['provider'], account['id']
    if provider in VENDORS and receipt is not None and not current_c6:
        try:
            result = await harness_models.refresh_provider(
                provider, reason='account-verify', account_id=account_id,
                expected_receipt=receipt, binary=binary,
            )
            if getattr(result, 'status', None) == 'ready':
                return True, None, None
            cause = getattr(result, 'cause', None) or 'account_refresh_failed'
        except Exception as exc:
            cause = type(exc).__name__
    else:
        cause = 'account_refresh_unavailable'
    ref = new_reference('signin')
    log.warning('account model refresh failed reference=%s cause=%s', ref, cause)
    return False, cause, ref
