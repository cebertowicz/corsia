"""Read-only harness catalog rows from current account discovery.

A stored Claude Code or Codex row is an identity registry. Availability and every
discovered field come from what signed-in accounts list now.
"""
from __future__ import annotations

import json

from connections import fact_memo

# Stored metadata a refresh used to write; the live projection owns these now.
_DISCOVERED = ('resolved_identity', 'discovered_capabilities', 'is_default', 'alias_of', 'availability_reason',
               'discovery_order', 'harness_version', 'context_observation', 'reference_only')
_LIVE_COLUMNS = ('available', 'context_window', 'supports_vision', 'supports_reasoning', 'display_name',
                 'description', 'last_error')


@fact_memo.memoized_reader
def account_rows(*, db, provider: str | None = None) -> dict[str, dict]:
    """Project models from current signed-in account evidence."""
    from connections.vendor_accounts import list_accounts

    accounts = [account for vendor in ((provider,) if provider else ('claude-code', 'codex'))
                for account in list_accounts(provider=vendor, db=db)]
    return account_rows_from_accounts(accounts)


def account_rows_from_accounts(accounts: list[dict]) -> dict[str, dict]:
    """Project discovery from account rows a caller already read.

    A model any account lists available is available, with the smallest window those
    accounts report; its order is where an account first listed it.
    """
    from connections.account_evidence import current_ready_discovery
    from llm.context_window import discovered_window

    rows: dict[str, dict] = {}
    for account in accounts:
        vendor = account['provider']
        if vendor not in ('claude-code', 'codex'):
            continue
        discovery = current_ready_discovery(account)
        if discovery is None:
            continue
        for model, evidence in discovery.get('models', {}).items():
            if not model.startswith(vendor + '/') or not isinstance(evidence, dict):
                continue
            previous = rows.get(model)
            available = evidence.get('available') is True
            window = discovered_window(evidence)
            if previous is not None and (previous['available'] or not available):
                if available and window and previous['context_window']:
                    previous['context_window'] = min(previous['context_window'], window)
                continue
            capabilities = evidence.get('capabilities') or {}
            metadata = {'resolved_identity': evidence.get('resolved_identity'),
                        'discovered_capabilities': capabilities, 'is_default': bool(evidence.get('is_default')),
                        'harness_version': evidence.get('harness_version'),
                        'discovery_order': list(rows).index(model) if previous else len(rows)}
            if not available:
                metadata['availability_reason'] = evidence.get('availability_reason') or 'discovered_model_unavailable'
            if evidence.get('alias_of'):
                metadata['alias_of'] = f"{vendor}/{evidence['alias_of']}"
            if isinstance(evidence.get('context_observation'), dict):
                metadata['context_observation'] = evidence['context_observation']
            rows[model] = {
                'id': model, 'litellm_model': model, 'provider': vendor, 'harness': vendor,
                'provider_model_id': model[len(vendor) + 1:], 'backend': 'cc' if vendor == 'claude-code' else 'codex',
                'display_name': evidence.get('display_name') or model[len(vendor) + 1:],
                'description': evidence.get('description') or '', 'source': 'account_discovery',
                'enabled': 1, 'available': available, 'context_window': window,
                'supports_vision': 'image' in (capabilities.get('input_modalities') or []),
                'supports_tools': 1, 'supports_reasoning': bool(capabilities.get('effort_levels') or capabilities.get('reasoning_efforts')),
                'metadata_json': json.dumps(metadata), 'last_error': None,
            }
    return rows


def catalog_rows(stored: list[dict]) -> list[dict]:
    """Project stored rows and append listed models with no stored row, harness rows in discovery order."""
    from infra.database import get_db

    db = get_db()
    try:
        existing = {row[0] for row in db.execute(
            "SELECT id FROM models WHERE harness IN ('claude-code','codex')")}
        projected = account_rows(db=db)
        rows = [*project_stored_rows(stored, db=db),
                *(row for model, row in projected.items() if model not in existing)]
    finally:
        db.close()
    rank = {'claude-code': 1, 'codex': 2}
    return [row for row in rows if row['harness'] not in rank] + sorted(
        (row for row in rows if row['harness'] in rank),
        key=lambda row: (rank[row['harness']], json.loads(row['metadata_json'] or '{}').get(
            'discovery_order', 2147483647), row['display_name'], row['id']))


def harness_base_from_row(row: dict, *, db) -> dict:
    """Project an already-read harness catalog row for an account decision."""
    from connections.models import provider_may_run
    from llm.context_window import resolve_window_for_row
    from llm.model_status import _harness_status

    row = dict(row)
    status, reason = _harness_status(row['harness'], row, json.loads(row.get('metadata_json') or '{}'),
                                     resolve_window_for_row(row, db=db), provider_may_run(row['harness'], db=db))
    return {'litellm_model': row['litellm_model'], 'status': status, 'status_reason': reason}


def project_stored_rows(rows, *, db) -> list[dict]:
    """Overlay each stored Claude Code or Codex row with what signed-in accounts list now.

    The stored row keeps what a person set (enabled, alias, overrides) and a runtime
    window observed for the resolved identity accounts list now. A model no account
    lists keeps its registered name and is unavailable. Other rows pass through.
    """
    from llm.model_status import versioned_name

    rows = [dict(row) for row in rows]
    vendors = {row.get('harness') for row in rows} & {'claude-code', 'codex'}
    live = account_rows(db=db, provider=next(iter(vendors)) if len(vendors) == 1 else None) if vendors else {}
    for row in rows:
        if row.get('harness') in ('claude-code', 'codex'):
            stored = json.loads(row.get('metadata_json') or '{}')
            meta = {key: value for key, value in stored.items() if key not in _DISCOVERED}
            current = live.get(row['litellm_model'])
            # A harness has no catalog inference defaults; an older row's schema-stamped ones never apply.
            row.update(default_temperature=None, default_max_tokens=None)
            if current is None:
                row.update(available=0, context_window=None, supports_vision=0, supports_reasoning=0,
                           display_name=versioned_name(row, stored), last_error=None)
                meta['availability_reason'] = 'not_returned_by_discovery'
            else:
                row.update({key: current[key] for key in _LIVE_COLUMNS})
                meta.update(json.loads(current['metadata_json']))
                observed = stored.get('context_observation')
                if (isinstance(observed, dict) and observed.get('resolved_identity')
                        and observed['resolved_identity'] == meta['resolved_identity']):
                    row['context_window'] = observed['effective_tokens']
                    meta['context_observation'] = observed
            row['metadata_json'] = json.dumps(meta)
    return rows


@fact_memo.memoized_reader
def harness_row(model: str, *, db) -> dict | None:
    """Return the stored catalog row, or the row projected from the accounts that list it."""
    row = db.execute(
        "SELECT id,harness,provider,provider_model_id,litellm_model,display_name,alias,description,backend,source,"
        "enabled,available,context_window,supports_vision,supports_tools,supports_reasoning,default_temperature,"
        "default_max_tokens,metadata_json,last_used_at,runtime_state,last_error,load_started_at,load_completed_at,"
        "inference_overrides,load_overrides,estimated_memory_gb,discovered_at,last_seen_at,created_at,updated_at "
        "FROM models WHERE harness IN ('claude-code','codex') AND (id=? OR litellm_model=?) "
        "ORDER BY id=? DESC, enabled DESC, id LIMIT 1", (model, model, model)
    ).fetchone()
    if row is not None:
        return project_stored_rows([row], db=db)[0]
    return account_rows(db=db).get(model)
