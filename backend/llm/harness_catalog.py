"""Read-only harness catalog rows from Default and current added-account discovery."""
from __future__ import annotations

import json

from connections import fact_memo


@fact_memo.memoized_reader
def added_rows(*, db) -> dict[str, dict]:
    """Project each added-only model once from current signed-in account evidence."""
    from connections.account_evidence import current_ready_discovery
    from connections.vendor_accounts import list_accounts

    rows: dict[str, dict] = {}
    for provider in ('claude-code', 'codex'):
        for account in list_accounts(provider=provider, db=db):
            discovery = current_ready_discovery(account)
            if discovery is None:
                continue
            for model, evidence in discovery.get('models', {}).items():
                if not model.startswith(provider + '/') or not isinstance(evidence, dict):
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
                    'id': model, 'litellm_model': model, 'provider': provider, 'harness': provider,
                    'provider_model_id': model[len(provider) + 1:], 'backend': 'cc' if provider == 'claude-code' else 'codex',
                    'display_name': evidence.get('display_name') or model[len(provider) + 1:],
                    'description': evidence.get('description') or '', 'source': 'account_discovery',
                    'enabled': 1, 'available': available, 'context_window': evidence.get('context_window'),
                    'supports_vision': 'image' in (capabilities.get('input_modalities') or []),
                    'supports_tools': 1, 'supports_reasoning': bool(capabilities.get('effort_levels') or capabilities.get('reasoning_efforts')),
                    'metadata_json': json.dumps(metadata), 'last_error': None, 'default_listed': False,
                }
    return rows


def catalog_rows(stored: list[dict]) -> list[dict]:
    """Append projected rows, preserving stored and hidden Default model identities."""
    from infra.database import get_db

    db = get_db()
    try:
        existing = {row[0] for row in db.execute(
            "SELECT id FROM models WHERE harness IN ('claude-code','codex')")}
        projected = added_rows(db=db)
        return [*stored, *(row for model, row in projected.items() if model not in existing)]
    finally:
        db.close()


def harness_row(model: str, *, db) -> dict | None:
    """Return Default's stored row, or an added-account-only projected row."""
    row = db.execute(
        "SELECT id,harness,provider,provider_model_id,litellm_model,display_name,alias,description,backend,source,"
        "enabled,available,context_window,supports_vision,supports_tools,supports_reasoning,default_temperature,"
        "default_max_tokens,metadata_json,last_used_at,runtime_state,last_error,load_started_at,load_completed_at,"
        "inference_overrides,load_overrides,estimated_memory_gb,discovered_at,last_seen_at,created_at,updated_at "
        "FROM models WHERE harness IN ('claude-code','codex') AND (id=? OR litellm_model=?) "
        "ORDER BY id=? DESC, enabled DESC, id LIMIT 1", (model, model, model)
    ).fetchone()
    if row is not None:
        result = dict(row)
        if not result['available']:
            added = added_rows(db=db).get(result['litellm_model'])
            result['added_available'] = bool(added and added['available'])
        return result
    return added_rows(db=db).get(model)
