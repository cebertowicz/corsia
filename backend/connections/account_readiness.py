"""Batched account facts for the existing claim evaluator: stored records plus its live home check."""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from typing import TypedDict


class AccountReadiness(TypedDict):
    ready: bool
    cause: str | None
    is_default: bool | None


def _targets(ids, *, db):
    marks = ','.join('?' for _ in ids)
    rows = db.execute(
        f"SELECT t.id AS thread_id,t.project_id,t.model,m.id AS catalog_id,m.harness "
        f"FROM threads t LEFT JOIN models m ON m.id=trim(t.model) OR m.litellm_model=trim(t.model) "
        f"WHERE t.id IN ({marks})", ids).fetchall()
    grouped: dict = {}
    for row in rows:
        grouped.setdefault(row['thread_id'], []).append(row)
    targets = {}
    projected: dict[str, dict | None] = {}
    for thread, candidates in grouped.items():
        model = str(candidates[0]['model'] or '').strip()
        exact = [r for r in candidates if r['catalog_id'] == model]
        chosen = exact or [r for r in candidates if r['catalog_id'] is not None]
        if len(chosen) == 1 and chosen[0]['harness'] in ('codex', 'claude-code'):
            targets[thread] = chosen[0]
        elif not chosen and model.startswith(('codex/', 'claude-code/')):
            from llm.harness_catalog import harness_row

            if model not in projected:
                projected[model] = harness_row(model, db=db)
            row = projected[model]
            if row is not None:
                targets[thread] = {**dict(candidates[0]), 'catalog_id': row['id'], 'harness': row['harness']}
    return targets



def _accounts(choices, *, db):
    ids = tuple(dict.fromkeys(identity for identity, *_ in choices.values() if identity is not None))
    if not ids:
        return {}
    marks = ','.join('?' for _ in ids)
    rows = db.execute(
        f"SELECT a.id,a.state,a.revision,a.is_default,a.source_home,a.discovery_json,c.provider, "
        f"CASE WHEN json_type(a.identity_json,'$.default_comparison.auth_state') IS NOT NULL "
        f"THEN json_object('auth_state',json_extract(a.identity_json,'$.default_comparison.auth_state'),"
        f"'login_seq',json_extract(a.identity_json,'$.default_comparison.login_seq')) END AS comparison_json "
        f"FROM vendor_accounts a JOIN connections c ON c.id=a.connection_id WHERE a.id IN ({marks})", ids).fetchall()
    return {r['id']: {**dict(r), 'identity': {'default_comparison': json.loads(r['comparison_json'])
                    if r['comparison_json'] else None},
                    'discovery': json.loads(r['discovery_json']) if r['discovery_json'] else None}
            for r in rows}


def _batch(ids, *, db) -> dict[str, AccountReadiness]:
    from connections.account_policy import NOT_CONFIGURED, account_reason, turn_account
    from connections.vendor_accounts import AccountError

    targets = _targets(ids, db=db)
    choices = {}
    for thread_id, target in targets.items():
        try:
            choices[thread_id] = turn_account(
                target['harness'], thread_id=thread_id,
                model=str(target['model'] or '').strip(), db=db,
            )
        except AccountError:
            # A deleted selected account is absent evidence, not a readiness result.
            # Preserve the existing unread projection rather than letting this
            # advisory batch fail its caller.
            continue
    accounts = _accounts(choices, db=db)
    result: dict[str, AccountReadiness] = {}
    for thread, (identity, choice_reason) in choices.items():
        target = targets[thread]
        account = accounts.get(identity)
        if identity is not None and account is None:
            continue  # Missing evidence never becomes ready.
        if choice_reason == NOT_CONFIGURED:
            choice_reason = None
        project_id = target['project_id']
        cause = choice_reason or (account_reason(
            account, provider=target['harness'], project_ids=[project_id] if project_id is not None else [],
            execution=True, model=str(target['model'] or '').strip(), db=db) if account is not None else None)
        result[thread] = {'ready': cause is None, 'cause': cause,
                          'is_default': None if cause == 'account_project_denied' else bool(account['is_default']) if account else None}
    return result


def read_account_readiness(thread_ids: Sequence[str], *, db: sqlite3.Connection
                           ) -> dict[str, AccountReadiness]:
    """Advisory reads through the canonical account selector and claim evaluator.

    Plans owns the consistent read snapshot of the stored records and authorization of
    the selected account-stalled threads. The evaluator's custom-account home check
    (``account_home.missing_home_reason``) reads the filesystem at call time, outside
    that snapshot. These facts are advisory, never paid admission.
    """
    ids = tuple(dict.fromkeys(thread_ids))
    result: dict[str, AccountReadiness] = {}
    for start in range(0, len(ids), 900):
        result.update(_batch(ids[start:start + 900], db=db))
    return result
