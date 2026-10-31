"""Schema 167: convert ambient harness accounts to account-owned homes.

Frozen: a migration reads and writes its tables directly and never imports
account, registry or catalog code, which moves on after this step ships.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

_VENDORS = ('claude-code', 'codex')
_OWNER_STATES = ('owner-pinned', 'owner-active')
# The provider session each vendor keeps under an account's home.
_SESSION_TYPE = {'claude-code': 'session', 'codex': 'thread'}
_REFERENCES = (
    ('thread_vendor_accounts', 'account_id'),
    ('thread_vendor_accounts', 'pending_account_id'),
    ('thread_vendor_account_holds', 'account_id'),
    ('vendor_account_billing_revisions', 'account_id'),
    ('vendor_account_billing_facts', 'account_id'),
    ('active_turns', 'vendor_account_id'),
)


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _referenced(conn: sqlite3.Connection, account_id: str) -> bool:
    if _has_table(conn, 'provider_policies'):
        for column in ('order_json', 'disabled_json'):
            if conn.execute(
                f"SELECT 1 FROM provider_policies, json_each({column}) WHERE json_each.value=? LIMIT 1",
                (account_id,),
            ).fetchone():
                return True
    return any(
        _has_table(conn, table)
        and conn.execute(f"SELECT 1 FROM {table} WHERE {column}=? LIMIT 1", (account_id,)).fetchone()
        for table, column in _REFERENCES
    )


def _allocate_homes(account_id: str) -> tuple[str, str]:
    """Private source and runtime homes, laid out as a newly added account's are."""
    from infra.config import RUNTIME_DIR

    root = RUNTIME_DIR / 'vendor-accounts' / account_id
    source, runtime = root / 'source', root / 'runtime'
    for directory in (root.parent, root, source, runtime):
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        directory.chmod(0o700)
    return str(source), str(runtime)


def _retire_provider_session(conn: sqlite3.Connection, thread_id: str, provider: str, now: str) -> None:
    """Archive the thread's live provider session: it sits under the ambient home."""
    if not (_has_table(conn, 'artifact_identities') and _has_table(conn, 'artifact_registrations')):
        return
    owners = ','.join('?' for _ in _OWNER_STATES)
    ids = [row[0] for row in conn.execute(
        "SELECT DISTINCT i.id FROM artifact_identities i JOIN artifact_registrations r ON r.artifact_id=i.id "
        f"WHERE r.parent_thread_id=? AND i.integration=? AND i.type=? AND i.state='active' "
        f"AND r.relationship_state IN ({owners})",
        (thread_id, provider, _SESSION_TYPE[provider], *_OWNER_STATES),
    ).fetchall()]
    for artifact_id in ids:
        conn.execute("UPDATE artifact_identities SET state='archived', updated_at=? WHERE id=?", (now, artifact_id))
        conn.execute(
            "UPDATE artifact_registrations SET relationship_state='subscriber-archived', updated_at=? "
            "WHERE artifact_id=? AND relationship_state NOT IN "
            "('owner-pinned','owner-active','subscriber-parent-chain')",
            (now, artifact_id),
        )


def _model_vendor(conn: sqlite3.Connection, model: str) -> str | None:
    model = model.strip()
    if not model:
        return None
    row = conn.execute("SELECT harness FROM models WHERE id=?", (model,)).fetchone()
    if row is None:
        row = conn.execute("SELECT harness FROM models WHERE litellm_model=? LIMIT 1", (model,)).fetchone()
    return row[0] if row is not None and row[0] in _VENDORS else None


def migrate_166_to_167(conn: sqlite3.Connection) -> None:
    """Move each ambient Claude Code or Codex row onto its own homes, or remove one never used."""
    if not _has_table(conn, 'vendor_accounts') or not _has_table(conn, 'connections'):
        return
    accounts = conn.execute(
        "SELECT a.id,a.identity_json,c.provider FROM vendor_accounts a "
        "JOIN connections c ON c.id=a.connection_id "
        "WHERE c.provider IN ('claude-code','codex') AND a.source_home IS NULL AND a.state!='removed'"
    ).fetchall()
    converted: set[str] = set()
    for account_id, identity_json, provider in accounts:
        converted.add(provider)
        identity = json.loads(identity_json or '{}')
        comparison = identity.get('default_comparison') or {}
        facts = comparison.get('facts') or comparison.get('last_known') or {}
        known = comparison.get('auth_state') == 'known' or bool(facts)
        now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        if known or _referenced(conn, account_id):
            source, runtime = _allocate_homes(account_id)
            hint = identity.get('hint') if isinstance(identity.get('hint'), dict) else {}
            for key in ('email', 'plan'):
                if isinstance(facts.get(key), str) and facts[key]:
                    hint = dict(hint, **{key: facts[key]})
            saved: dict[str, Any] = {'upgrade_reason': 'sign_in_to_keep_using_account'}
            if hint:
                saved['hint'] = hint
            conn.execute(
                "UPDATE vendor_accounts SET source_home=?,runtime_home=?,state='signed_out',"
                "identity_json=?,discovery_json=NULL,usage_json=NULL,revision=revision+1,updated_at=? WHERE id=?",
                (source, runtime, json.dumps(saved, sort_keys=True), now, account_id),
            )
        else:
            conn.execute(
                "UPDATE vendor_accounts SET state='removed',removed_at=?,updated_at=?,"
                "revision=revision+1,identity_json='{}',discovery_json=NULL,usage_json=NULL WHERE id=?",
                (now, now, account_id),
            )
        if not _has_table(conn, 'threads'):
            continue
        bound = _has_table(conn, 'thread_vendor_accounts')
        threads = conn.execute(
            "SELECT t.id,t.model,b.account_id FROM threads t LEFT JOIN thread_vendor_accounts b "
            "ON b.thread_id=t.id AND b.provider=? WHERE b.account_id=? OR b.account_id IS NULL",
            (provider, account_id),
        ).fetchall() if bound else []
        for thread_id, model, bound_account in threads:
            running = _model_vendor(conn, str(model or '')) == provider
            if bound_account != account_id and not running:
                continue
            _retire_provider_session(conn, thread_id, provider, now)
            if running:
                conn.execute("UPDATE threads SET runtime_context_epoch=runtime_context_epoch+1 WHERE id=?", (thread_id,))
    for provider in converted:
        _record_needs_sign_in(conn, provider)


def _record_needs_sign_in(conn: sqlite3.Connection, provider: str) -> None:
    """Record sign-in needed on the vendor's source once no signed-in account remains.

    The frozen counterpart of ``source_state.record_last_account_left``: the ambient
    login no longer makes the source ready, so its row must not keep reading ready.
    """
    if conn.execute(
        "SELECT 1 FROM vendor_accounts a JOIN connections c ON c.id=a.connection_id "
        "WHERE c.provider=? AND a.state='signed_in' LIMIT 1", (provider,),
    ).fetchone():
        return
    row = conn.execute("SELECT status,config FROM connections WHERE provider=?", (provider,)).fetchone()
    if row is None or row[0] == 'not_installed':
        return
    now = datetime.now(timezone.utc).isoformat()
    config = json.loads(row[1]) if row[1] else {}
    config['source_check'] = {'state': 'needs_auth', 'prior_state': None,
                              'reason': 'Not signed in', 'checked_at': now}
    conn.execute("UPDATE connections SET status='needs_auth',config=?,updated_at=? WHERE provider=?",
                 (json.dumps(config), now, provider))
