"""Protect current added-account discoveries during Default catalog refresh."""
import json

from connections.account_evidence import current_ready_discovery
from connections.vendor_accounts import list_accounts


def protected_selectors(provider, *, db):
    return {
        model.partition('/')[2]
        for account in list_accounts(provider=provider, db=db)
        if (discovery := current_ready_discovery(account)) is not None
        for model, evidence in discovery.get('models', {}).items()
        if model.startswith(provider + '/') and evidence.get('available', True)
    }


def retire_unlisted(connection, prior, current, timestamp, errors):
    """Mark catalog rows omitted by Default unavailable, excluding protected rows."""
    from connections.harness_models import _metadata

    retained = sorted(set(prior) - set(current), key=lambda selector: (
        _metadata(prior[selector]).get('discovery_order', 2147483647), selector))
    for order, selector in enumerate(retained, start=len(current)):
        row = prior[selector]
        meta = _metadata(row)
        if meta.get('reference_only'):
            continue
        meta['availability_reason'] = 'not_returned_by_discovery'
        meta['discovery_order'] = order
        connection.execute(
            'UPDATE models SET available = 0, last_error = ?, metadata_json = ?, updated_at = ? WHERE id = ?',
            (errors.get(selector), json.dumps(meta, sort_keys=True), timestamp, row['id']))


def save_success_envelope(provider, harness_version, source, timestamp, *, db):
    from connections.harness_models import _save_discovery_envelope, read_discovery_envelope

    previous = read_discovery_envelope(provider, db=db) or {}
    _save_discovery_envelope(provider, {**previous, 'harness_version': harness_version, 'source': source,
        'refreshed_at': timestamp, 'attempted_at': timestamp, 'last_error': None}, db=db)
