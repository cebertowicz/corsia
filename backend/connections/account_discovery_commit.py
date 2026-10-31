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
        from connections.harness_model_rows import retain_discovered_context

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
        from connections.harness_legacy_fallback import remove_legacy_fallback
        from infra.time import utc_now_iso
        from llm.model_status import versioned_name

        remove_legacy_fallback(provider, db=connection)
        now = utc_now_iso()
        # The stored row only registers the model; readers project the rest from signed-in accounts.
        for key, row in models.items():
            connection.execute(
                "INSERT INTO models (id,harness,provider,provider_model_id,litellm_model,display_name,backend,"
                "source,enabled,available,metadata_json,default_temperature,default_max_tokens,discovered_at,"
                "created_at,updated_at) VALUES (?,?,?,?,?,?,?,'account_discovery',1,0,'{}',NULL,NULL,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET source=excluded.source,enabled=1 WHERE models.source='reference'",
                (key, provider, provider, key[len(provider) + 1:], key,
                 versioned_name({'display_name': row.get('display_name') or key[len(provider) + 1:]}, row),
                 'cc' if provider == 'claude-code' else 'codex', now, now, now))
        refresh_account_holds(context.account_id, db=connection)
        from connections.account_rollover import offer_destination

        offer_destination(provider, context.account_id, db=connection)
        from infra.state.ws import schedule_models_changed

        if stage is not None:
            after_commit(connection, lambda: _publish_cache(context.account_id, stage, cache_receipt, db))
        after_commit(connection, schedule_models_changed)
        return AccountRefreshResult(provider, context.account_id, status='ready',
            model_count=len(models), effective_receipt=receipt, harness_version=harness_version)


def record_discovery_failure(provider, *, context, cause, harness_version=None, reason=None, db=None):
    with write_transaction(db, scope='account_discovery_failed') as connection:
        if not receipt_is_current(context.receipt, db=connection):
            return AccountRefreshResult(provider, context.account_id, cause='account_refresh_superseded')
        mark_discovery(context.account_id, receipt=context.receipt, status='failed', cause=cause,
                       harness_version=harness_version, reason=reason, db=connection)
        return AccountRefreshResult(provider, context.account_id, cause=cause,
                                    effective_receipt=context.receipt)
