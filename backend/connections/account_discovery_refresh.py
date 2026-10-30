"""Staged account adapter execution inside the canonical harness refresh owner."""
from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import replace

from connections.account_discovery_commit import commit_discovery_bundle, record_discovery_failure
from connections.account_discovery_types import StagedDiscovery
from infra.db_async import run_in_db


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
            result = await db_call(record_discovery_failure, provider, context=context,
                                   cause='account_discovery_failed', harness_version=harness_version, db=db)
            return replace(result, source_error=str(error))
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
                staged.unlisted_errors = await add_accepted_unlisted(staged.payload, previous,
                    lambda selector: _discover_claude_context(binary, selector, context=context))
            rows = normalize(staged.payload, harness_version=version,
                             timestamp=datetime.now(timezone.utc).isoformat())
        except Exception:
            # The adapter's raw diagnostics are never stored on account evidence.
            return await db_call(record_discovery_failure, provider, context=context,
                                 cause='account_discovery_failed', harness_version=harness_version, db=db)
        # Transaction/restoration/terminal cleanup failures must remain loud, not a failed adapter report.
        return await db_call(commit_discovery_bundle, provider, staged, rows,
                             context=context, harness_version=version, db=db)
    finally:
        if staged is not None:
            await run_in_db(staged.close)
