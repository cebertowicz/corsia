"""Account bundle commit used by the canonical harness refresh owner."""
from __future__ import annotations

from connections.account_discovery_types import AccountRefreshResult
from connections.account_evidence import mark_discovery
from connections.account_receipts import receipt_is_current
from connections.vendor_accounts import get_account
from infra.database import TransactionOwnershipError, write_transaction
from infra.write_effects import after_commit


def _publish_cache(account_id, stage, cache_receipt, db):
    """Rename the staged cache into place only while its committed receipt is current."""
    with write_transaction(db, scope='account_cache_publish') as connection:
        account = get_account(account_id, db=connection)
        stored = ((account or {}).get('discovery') or {}).get('discovery') or {}
        if stored.get('cache_receipt') == cache_receipt:
            stage.promote()


def _store_account_catalog(provider, rows, harness_version, *, db, unlisted_errors=None):
    """Project current account rows using the shared, current discovery of every account."""
    from connections.account_evidence import current_ready_discovery
    from connections.harness_models import upsert_discovered_rows
    from connections.vendor_accounts import list_accounts
    from infra.time import utc_now_iso

    discoveries = [current_ready_discovery(account) for account in list_accounts(provider=provider, db=db)]
    by_model = {}
    for discovery in discoveries:
        if discovery is None:
            continue
        for model, evidence in discovery.get('models', {}).items():
            if isinstance(evidence, dict):
                by_model.setdefault(model, []).append(evidence)
    current = {provider + '/' + row['selector_id']: row for row in rows}
    combined = []
    for model, evidence in by_model.items():
        if not model.startswith(provider + '/'):
            continue
        entry = dict(current.get(model) or evidence[0])
        entry['selector_id'] = model[len(provider) + 1:]
        windows = [item.get('context_window') for item in evidence
                   if isinstance(item.get('context_window'), int) and item['context_window'] > 0]
        entry['context_window'] = min(windows) if windows else None
        entry['available'] = any(item.get('available') is True for item in evidence)
        combined.append(entry)
    upsert_discovered_rows(provider, combined, harness_version=harness_version,
                           source='account_discovery', timestamp=utc_now_iso(), db=db,
                           unlisted_errors=unlisted_errors)


def commit_discovery_bundle(provider, staged, rows, *, context, harness_version, db=None):
    """Fence model, usage and recovery effects in one account writer, then publish the cache.

    The cache file is renamed into place after the commit, so the writer never
    has a file effect to undo; it must own the outermost transaction for that.
    """
    from connections import harness_usage
    from connections.account_holds import refresh_account_holds

    if db is not None and db.in_transaction:
        raise TransactionOwnershipError('account_discovery_bundle must own the outermost write transaction')
    receipt = context.receipt
    with write_transaction(db, scope='account_discovery_bundle') as connection:
        if not receipt_is_current(receipt, db=connection):
            return AccountRefreshResult(provider, context.account_id, cause='account_refresh_superseded')
        from connections.harness_models import retain_discovered_context

        account = get_account(context.account_id, db=connection)
        prior = ((account.get('discovery') or {}).get('discovery') or {}).get('models') or {}
        models = {}
        for row in rows:
            selector = row.get('selector_id')
            if not isinstance(selector, str) or not selector or '/' in selector:
                raise ValueError('invalid account discovery selector')
            key = provider + '/' + selector
            if key in models:
                raise ValueError('duplicate account discovery selector')
            models[key] = retain_discovered_context(row, prior.get(key))
        if provider == 'claude-code':
            from connections.claude_discovery import _store_catalog_success

            _store_catalog_success(staged.usage_report, account_id=context.account_id,
                                   receipt=receipt, db=connection)
        elif provider == 'codex':
            if isinstance(staged.usage_report, dict):
                harness_usage.record_rate_limits_read(staged.usage_report, provider=provider,
                    receipt=receipt, db=connection)
            else:
                harness_usage.mark_readings_stale(provider=provider, account_id=context.account_id, db=connection)
        stage = staged.cache_stage
        cache_receipt = stage.receipt() if stage is not None else None
        if not mark_discovery(context.account_id, receipt=receipt, status='ready', models=models,
                              harness_version=harness_version, cache_receipt=cache_receipt, db=connection):
            raise RuntimeError('account_refresh_superseded')
        _store_account_catalog(provider, list(models.values()), harness_version, db=connection,
                               unlisted_errors=staged.unlisted_errors)
        from connections.harness_models import _ADAPTERS, record_refresh_attempt
        from infra.time import utc_now_iso

        record_refresh_attempt(provider, harness_version=harness_version, source=_ADAPTERS[provider][2],
                               timestamp=utc_now_iso(), error=None, db=connection)
        refresh_account_holds(context.account_id, db=connection)
        from connections.account_rollover import offer_destination

        offer_destination(provider, context.account_id, db=connection)
        from infra.state.ws import schedule_models_changed

        if stage is not None:
            after_commit(connection, lambda: _publish_cache(context.account_id, stage, cache_receipt, db))
        after_commit(connection, schedule_models_changed)
        return AccountRefreshResult(provider, context.account_id, status='ready',
            model_count=len(models), effective_receipt=receipt)


def record_discovery_failure(provider, *, context, cause, harness_version=None, db=None):
    with write_transaction(db, scope='account_discovery_failed') as connection:
        if not receipt_is_current(context.receipt, db=connection):
            return AccountRefreshResult(provider, context.account_id, cause='account_refresh_superseded')
        mark_discovery(context.account_id, receipt=context.receipt, status='failed', cause=cause,
                       harness_version=harness_version, db=connection)
        from connections.harness_models import _ADAPTERS, _cached_harness_version, record_refresh_attempt
        from infra.time import utc_now_iso

        record_refresh_attempt(provider, harness_version=harness_version or _cached_harness_version(provider, db=connection),
                               source=_ADAPTERS[provider][2], timestamp=utc_now_iso(), error=cause, db=connection)
        return AccountRefreshResult(provider, context.account_id, cause=cause,
                                    effective_receipt=context.receipt)
