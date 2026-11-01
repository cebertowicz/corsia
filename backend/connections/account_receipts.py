"""Private lifecycle/login fences shared by account producers and consumers."""
from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import TypeGuard

from connections.vendor_accounts import get_account, resolve_account_id

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ReceiptFence:
    """A generation of an account row, never a vendor login-session guarantee."""

    account_id: str
    account_revision: int
    login_seq: int


def nonnegative_integer(value: object) -> TypeGuard[int]:
    return type(value) is int and value >= 0



def generation_revision(account: dict) -> int:
    """The row revision the account's current login generation was established at.

    ``revision`` counts every edit to the row, including a rename that changes no
    login fact, so stamping a receipt with it would age evidence the edit did not
    change. The account's own evidence record carries the generation stamp; a row
    that predates it falls back to the row revision.
    """
    record = account.get('discovery')
    revision = record.get('login_revision') if isinstance(record, dict) else None
    return revision if nonnegative_integer(revision) else account['revision']


def account_receipt(account: dict) -> ReceiptFence | None:
    """Read only the account's canonical generation owner; never mint evidence."""
    if account['state'] != 'signed_in':
        return None
    record = account['discovery']
    if not isinstance(record, dict) or record.get('auth_state') != 'known':
        return None
    generation = record.get('login_seq')
    revision = generation_revision(account)
    if not nonnegative_integer(generation) or not nonnegative_integer(revision):
        return None
    return ReceiptFence(str(account['id']), revision, generation)


def capture_receipt(provider: str, *, account_id: str, db) -> ReceiptFence | None:
    """The current login of one of ``provider``'s accounts."""
    return account_receipt(get_account(resolve_account_id(provider, account_id, db=db), db=db))


def receipt_is_current(receipt: ReceiptFence | None, *, db) -> bool:
    """A lost historical receipt cannot borrow current same-row login evidence."""
    if receipt is None:
        return False
    from connections.vendor_accounts import AccountError

    try:
        return receipt == account_receipt(get_account(receipt.account_id, db=db))
    except AccountError:
        return False


def write_account(provider: str, receipt: ReceiptFence | None, *, db) -> str | None:
    """The account a fenced write may change, or ``None`` (logged) when its login is gone."""
    from connections.vendor_accounts import AccountError

    try:
        account = get_account(receipt.account_id, db=db) if receipt is not None else None
    except AccountError:
        account = None
    if receipt is not None and account is not None and account['provider'] == provider \
            and receipt == account_receipt(account):
        return receipt.account_id
    log.warning('%s account write refused: login receipt for account %s is %s', provider,
                receipt.account_id if receipt else None, 'missing' if receipt is None else 'stale')
    return None


def limit_subject(provider: str, scope: str, *, receipt: ReceiptFence):
    """One scope of the account and login ``receipt`` names."""
    from connections.usage_verdicts import LimitSubject

    return LimitSubject(provider, scope, receipt.account_id, receipt)


def subject_is_current(subject, *, db) -> bool:
    """Effects of a captured limit apply only while that exact login is current."""
    return (subject.receipt is not None and subject.receipt.account_id == subject.account_id
            and receipt_is_current(subject.receipt, db=db))


def subject_matches_thread(subject, thread_id, *, db):
    from connections.account_policy import chosen_account_id

    return subject_is_current(subject, db=db) and chosen_account_id(
        thread_id, subject.provider, db=db) == subject.account_id
