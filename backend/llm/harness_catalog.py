"""Read-only harness catalog rows from current account discovery."""
from __future__ import annotations

import json

from connections import fact_memo


@fact_memo.memoized_reader
def added_rows(*, db, provider: str | None = None) -> dict[str, dict]:
    """Project added-only models from current signed-in account evidence."""
    from connections.vendor_accounts import list_accounts

    accounts = [account for vendor in ((provider,) if provider else ('claude-code', 'codex'))
                for account in list_accounts(provider=vendor, db=db)]
    return added_rows_from_accounts(accounts)


def added_rows_from_accounts(accounts: list[dict]) -> dict[str, dict]:
    """Project discovery from account rows a caller already read."""
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
            if previous is not None and (previous['available'] or not available):
                continue
            capabilities = evidence.get('capabilities') or {}
            metadata = {'resolved_identity': evidence.get('resolved_identity'),
                        'discovered_capabilities': capabilities, 'is_default': False,
                        'availability_reason': evidence.get('availability_reason')}
            rows[model] = {
                'id': model, 'litellm_model': model, 'provider': vendor, 'harness': vendor,
                'provider_model_id': model[len(vendor) + 1:], 'backend': 'cc' if vendor == 'claude-code' else 'codex',
                'display_name': evidence.get('display_name') or model[len(vendor) + 1:],
                'description': evidence.get('description') or '', 'source': 'account_discovery',
                'enabled': 1, 'available': available, 'context_window': discovered_window(evidence),
                'supports_vision': 'image' in (capabilities.get('input_modalities') or []),
                'supports_tools': 1, 'supports_reasoning': bool(capabilities.get('effort_levels') or capabilities.get('reasoning_efforts')),
                'metadata_json': json.dumps(metadata), 'last_error': None,
            }
    return rows


def catalog_rows(stored: list[dict]) -> list[dict]:
    """Append each signed-in account's listed models that have no stored catalog row."""
    from infra.database import get_db

    db = get_db()
    try:
        existing = {row[0] for row in db.execute(
            "SELECT id FROM models WHERE harness IN ('claude-code','codex')")}
        projected = added_rows(db=db)
        return [*stored, *(row for model, row in projected.items() if model not in existing)]
    finally:
        db.close()


def harness_base_from_row(row: dict, *, db) -> dict:
    """Project an already-read harness catalog row for an account decision."""
    from connections.models import provider_may_run
    from llm.context_window import resolve_window_for_row
    from llm.model_status import _harness_status

    row = dict(row)
    status, reason = _harness_status(row['harness'], row, json.loads(row.get('metadata_json') or '{}'),
                                     resolve_window_for_row(row, db=db), provider_may_run(row['harness'], db=db))
    return {'litellm_model': row['litellm_model'], 'status': status, 'status_reason': reason}


def project_stored_row(row, *, db) -> dict:
    """Add other accounts' availability to an unavailable stored row without rereading it."""
    result = dict(row)
    if not result['available']:
        added = added_rows(db=db).get(result['litellm_model'])
        result['added_available'] = bool(added and added['available'])
    return result


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
        return project_stored_row(row, db=db)
    return added_rows(db=db).get(model)
