"""Staged account adapter execution inside the canonical harness refresh owner."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from connections.account_discovery_commit import commit_discovery_bundle, record_discovery_failure
from connections.account_discovery_types import StagedDiscovery
from infra.db_async import run_in_db

log = logging.getLogger(__name__)


def _version_environment(context):
    from connections.account_profiles import profile_for

    if not context.source_home:
        raise RuntimeError('account_discovery_failed')
    profile = profile_for(context.provider)
    env = profile.clean_env()
    env[profile.home_variable] = context.source_home
    return env


async def refresh_account_once(provider, *, context, binary, harness_version, adapter, db_call, version_read, db):
    discover, normalize, _source = adapter
    staged = None
    try:
        try:
            env = _version_environment(context)
        except Exception:
            return await db_call(record_discovery_failure, provider, context=context,
                                 cause='account_discovery_failed', harness_version=harness_version, db=db)
        try:
            version = harness_version or await version_read(binary, env=env)
        except Exception as error:
            # A person reads only that the check failed or timed out; the error names the binary's
            # path and the CLI's stderr, so it goes to the log alone.
            from connections.source_state import UNVERIFIED, user_reason

            log.warning('%s version check failed: %s', provider, error)
            timed_out = 'timed out' if 'timed out' in str(error) else None
            return await db_call(record_discovery_failure, provider, context=context,
                                 cause='account_discovery_failed', harness_version=harness_version,
                                 reason=user_reason(UNVERIFIED, timed_out), db=db)
        try:
            staged = await discover(binary, context=context)
            if not isinstance(staged, StagedDiscovery):
                raise RuntimeError('account_discovery_failed')
            if provider == 'claude-code':
                from connections.claude_discovery import _discover_claude_context
                from connections.claude_unlisted import add_accepted_unlisted

                prior = (context.prior_discovery or {}).get('discovery', {}).get('models', {})
                previous = [{'selector': key.partition('/')[2],
                    'resolved_identity': row.get('resolved_identity') or '',
                    'display_name': row.get('display_name') or key.partition('/')[2]}
                    for key, row in prior.items()]
                await add_accepted_unlisted(staged.payload, previous,
                    lambda selector: _discover_claude_context(binary, selector, context=context))
            rows = normalize(staged.payload, harness_version=version,
                             timestamp=datetime.now(timezone.utc).isoformat())
        except Exception:
            # An adapter's raw diagnostics are never stored on account evidence: only a failed
            # version check (above) keeps a reason, in the person's terms.
            return await db_call(record_discovery_failure, provider, context=context,
                                 cause='account_discovery_failed', harness_version=harness_version, db=db)
        # Transaction/restoration/terminal cleanup failures must remain loud, not a failed adapter report.
        return await db_call(commit_discovery_bundle, provider, staged, rows,
                             context=context, harness_version=version, db=db)
    finally:
        if staged is not None:
            await run_in_db(staged.close)


async def refresh_signed_in_accounts(provider, *, reason, harness_version, binary, db):
    """Refresh every signed-in account of ``provider``; return ``{provider, reason, ok, error}``."""
    import connections.harness_models as harness_models
    from connections.vendor_accounts import list_accounts

    accounts = await harness_models._db_call(list_accounts, provider=provider, db=db)
    results = [await harness_models.refresh_provider(
        provider, reason=reason, harness_version=harness_version, db=db, binary=binary,
        account_id=account['id']) for account in accounts if account['state'] == 'signed_in']
    ready = any(result.status == 'ready' for result in results)
    causes = [result.cause for result in results if result.cause]
    error = None if ready else (next(iter(causes), None) if results else 'account_not_configured')
    # The vendor clock records the sweep, never one account: an account's own refresh, ready or
    # failed, stays on its discovery and must not back off or reset the others' checks.
    if ready or ('account_refresh_superseded' not in causes and (causes or not results)):
        from infra.time import utc_now_iso

        read = next((result.harness_version for result in results if result.status == 'ready'), None)
        version = read or harness_version or await harness_models._db_call(
            harness_models._cached_harness_version, provider, db=db)
        stamp = dict(harness_version=version, source=harness_models._ADAPTERS[provider][2],
                     timestamp=utc_now_iso(), db=db)
        if ready:
            await harness_models._db_call(_store_sweep_success, provider, **stamp)
        else:
            await harness_models._db_call(harness_models.store_failure, provider, error, **stamp)
    return {'provider': provider, 'reason': reason, 'ok': ready, 'error': error}


def _store_sweep_success(provider, *, harness_version, source, timestamp, db=None):
    """Stamp a sweep that read at least one account; each account committed its own rows."""
    from connections.harness_models import record_refresh_attempt
    from infra.database import write_transaction

    with write_transaction(db, scope='harness_model_catalog') as connection:
        record_refresh_attempt(provider, harness_version=harness_version, source=source,
                               timestamp=timestamp, error=None, db=connection)
