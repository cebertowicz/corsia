"""Private staged discovery receipts; routes expose only the existing public shape."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from connections.account_receipts import ReceiptFence, account_receipt
from connections.vendor_accounts import get_account, resolve_account_id
from infra.database import get_db


@dataclass(frozen=True, slots=True)
class DiscoveryContext:
    provider: str
    account_id: str | None
    receipt: ReceiptFence | None
    source_home: str | None
    runtime_home: str | None
    prior_discovery: dict | None = field(repr=False)

    @property
    def key(self):
        return (self.provider, self.account_id,
                self.receipt.account_revision if self.receipt else None,
                self.receipt.login_seq if self.receipt else None)


@dataclass
class StagedDiscovery:
    payload: dict
    usage_report: object = None
    cache_stage: object | None = None
    cleanup_owner: Callable[[], None] | None = None

    def close(self):
        if self.cleanup_owner is not None:
            cleanup, self.cleanup_owner = self.cleanup_owner, None
            cleanup()


@dataclass(frozen=True, slots=True)
class AccountRefreshResult:
    provider: str
    account_id: str = field(repr=False)
    status: str = 'failed'
    cause: str | None = None
    model_count: int = 0
    effective_receipt: ReceiptFence | None = field(default=None, repr=False)
    harness_version: str | None = None


def capture_discovery_context(provider: str, *, account_id: str | None = None,
                              expected_receipt: ReceiptFence | None = None, db=None) -> DiscoveryContext | None:
    """The account's discovery inputs from one row read: the commit re-checks the receipt it carries."""
    connection = db or get_db()
    try:
        identity = resolve_account_id(provider, account_id, db=connection)
        if identity is None:
            return None
        account = get_account(identity, db=connection)
        receipt = account_receipt(account)
        if expected_receipt is not None and expected_receipt != receipt:
            return None
        if account['state'] != 'signed_in' or receipt is None:
            return None
        return DiscoveryContext(provider, identity, receipt,
            account['source_home'], account['runtime_home'], account['discovery'])
    finally:
        if db is None:
            connection.close()
