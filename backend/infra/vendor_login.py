"""Turn-associated private login provenance in the existing input metadata."""
from __future__ import annotations

import json

from connections.account_receipts import ReceiptFence, nonnegative_integer

LOGIN_KEY = '_corsia_vendor_login'


def without_login_sql() -> str:
    """One safe detach expression; legacy NULL/nonobjects/invalid JSON keep bytes."""
    return ("CASE WHEN json_valid(metadata) THEN CASE WHEN json_type(metadata)='object' "
            "THEN json_remove(metadata,'$._corsia_vendor_login') ELSE metadata END "
            "ELSE metadata END")


def public_metadata(metadata: dict) -> dict:
    """Exclude exactly the persistence stamp, retaining every other payload key."""
    return {key: value for key, value in metadata.items() if key != LOGIN_KEY}


def read_turn_receipt(turn_id: str, *, db) -> ReceiptFence | None:
    """Read raw matching associations, including delivered initial input history."""
    turn = db.execute('SELECT thread_id,vendor_account_id FROM active_turns WHERE id=?',
                      (turn_id,)).fetchone()
    if turn is None or turn['vendor_account_id'] is None:
        return None
    receipt = None
    rows = db.execute('SELECT metadata FROM messages WHERE active_turn_id=? AND thread_id=?',
                      (turn_id, turn['thread_id']))
    for row in rows:
        try:
            metadata = json.loads(row['metadata']) if row['metadata'] else {}
        except (TypeError, ValueError):
            return None
        if not isinstance(metadata, dict) or LOGIN_KEY not in metadata:
            continue
        stamp = metadata[LOGIN_KEY]
        if not isinstance(stamp, dict) or set(stamp) != {'turn_id', 'account_revision', 'login_seq'}:
            return None
        if (stamp['turn_id'] != turn_id or not nonnegative_integer(stamp['account_revision'])
                or not nonnegative_integer(stamp['login_seq'])):
            return None
        candidate = ReceiptFence(str(turn['vendor_account_id']), stamp['account_revision'], stamp['login_seq'])
        if receipt is not None and receipt != candidate:
            return None
        receipt = candidate
    return receipt


def thread_login(thread_id: str, provider: str, *, db=None) -> ReceiptFence | None:
    """The login ``thread_id``'s latest ``provider`` Turn ran on: the one its vendor transcript was written under."""
    if db is None:
        from infra.database import get_db

        db = get_db()
        try:
            return thread_login(thread_id, provider, db=db)
        finally:
            db.close()
    row = db.execute('SELECT t.id FROM active_turns t JOIN vendor_accounts a ON a.id=t.vendor_account_id '
                     'JOIN connections c ON c.id=a.connection_id '
                     'WHERE t.thread_id=? AND c.provider=? ORDER BY t.turn_order DESC LIMIT 1',
                     (thread_id, provider)).fetchone()
    return read_turn_receipt(row['id'], db=db) if row else None


def stamp_initial_claim(db, turn_id: str, message_id: str) -> ReceiptFence | None:
    """Capture the inserted Turn's frozen row inside its initial claim transaction."""
    from connections.account_receipts import account_receipt
    from connections.vendor_accounts import get_account

    turn = db.execute('SELECT vendor_account_id FROM active_turns WHERE id=?', (turn_id,)).fetchone()
    if turn is None or turn['vendor_account_id'] is None:
        return None
    account = get_account(str(turn['vendor_account_id']), db=db)
    receipt = account_receipt(account)
    if receipt is None:
        from connections.account_policy import AccountAdmissionError

        raise AccountAdmissionError('account_login_unverified')
    stamp = json.dumps({'turn_id': turn_id, 'account_revision': receipt.account_revision,
                        'login_seq': receipt.login_seq}, sort_keys=True)
    db.execute("UPDATE messages SET metadata=json_set(COALESCE(metadata,'{}'),"
               "'$._corsia_vendor_login',json(?)) WHERE id=? AND active_turn_id=?",
               (stamp, message_id, turn_id))
    return receipt
