"""Convert ambient harness accounts to account-owned homes at upgrade."""
from __future__ import annotations

import json


def migrate_166_to_167(db) -> None:
    from connections.account_leftovers import _referenced
    from connections.account_selection import retire_provider_sessions
    from connections.vendor_accounts import allocate_account_homes
    from infra.time import utc_now_iso
    from infra.vendor_turn_binding import model_vendor

    accounts = db.execute(
        "SELECT a.id,a.identity_json,c.provider FROM vendor_accounts a "
        "JOIN connections c ON c.id=a.connection_id "
        "WHERE c.provider IN ('claude-code','codex') AND a.source_home IS NULL "
        "AND a.state!='removed'"
    ).fetchall()
    for account in accounts:
        identity = json.loads(account['identity_json'] or '{}')
        comparison = identity.get('default_comparison') or {}
        facts = comparison.get('facts') or comparison.get('last_known') or {}
        known = comparison.get('auth_state') == 'known' or bool(facts)
        ordered = db.execute(
            "SELECT 1 FROM provider_policies, json_each(order_json) "
            "WHERE json_each.value=? LIMIT 1", (account['id'],)
        ).fetchone() is not None
        referenced = ordered or _referenced(account['id'], db=db)
        now = utc_now_iso()
        if known or referenced:
            _, source, runtime = allocate_account_homes(account['provider'], account['id'])
            hint = identity.get('hint') if isinstance(identity.get('hint'), dict) else {}
            for key in ('email', 'plan'):
                if isinstance(facts.get(key), str) and facts[key]:
                    hint = dict(hint, **{key: facts[key]})
            saved = {'upgrade_reason': 'sign_in_to_keep_using_account'}
            if hint:
                saved['hint'] = hint
            db.execute(
                "UPDATE vendor_accounts SET source_home=?,runtime_home=?,state='signed_out',"
                "identity_json=?,discovery_json=NULL,usage_json=NULL,revision=revision+1,updated_at=? WHERE id=?",
                (str(source), str(runtime), json.dumps(saved, sort_keys=True), now, account['id']),
            )
        else:
            db.execute(
                "UPDATE vendor_accounts SET state='removed',removed_at=?,updated_at=?,"
                "revision=revision+1,identity_json='{}',discovery_json=NULL,usage_json=NULL WHERE id=?",
                (now, now, account['id']),
            )
        threads = db.execute(
            "SELECT t.id,t.model,b.account_id FROM threads t LEFT JOIN thread_vendor_accounts b "
            "ON b.thread_id=t.id AND b.provider=? WHERE b.account_id=? OR b.account_id IS NULL",
            (account['provider'], account['id']),
        ).fetchall()
        for thread in threads:
            running = model_vendor(str(thread['model'] or ''), db=db) == account['provider']
            if thread['account_id'] != account['id'] and not running:
                continue
            retire_provider_sessions(thread['id'], account['provider'], db=db)
            if running:
                db.execute("UPDATE threads SET runtime_context_epoch=runtime_context_epoch+1 WHERE id=?", (thread['id'],))
