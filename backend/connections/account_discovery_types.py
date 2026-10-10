"""Private staged discovery receipts; routes expose only the existing public shape."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from connections.account_receipts import ReceiptFence, account_receipt
from connections.vendor_accounts import get_account, resolve_account_id
from infra.database import write_transaction


@dataclass(frozen=True, slots=True)
class DiscoveryContext:
    provider: str
    account_id: str | None
    is_default: bool
    receipt: ReceiptFence | None
    source_home: str | None
    runtime_home: str | None
    observation_token: object | None
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


def capture_discovery_context(provider: str, *, account_id: str | None = None,
                              expected_receipt: ReceiptFence | None = None, db=None) -> DiscoveryContext | None:
    from connections.account_observation import begin_default_observation

    with write_transaction(db, scope='capture_account_discovery') as connection:
        identity = resolve_account_id(provider, account_id, db=connection)
        if identity is None:
            return DiscoveryContext(provider, None, True, None, None, None, None, None)
        account = get_account(identity, db=connection)
        receipt = account_receipt(account)
        if expected_receipt is not None and expected_receipt != receipt:
            return None
        if not account['is_default'] and (account['state'] != 'signed_in' or receipt is None):
            return None
        token = begin_default_observation(provider, db=connection) if account['is_default'] else None
        return DiscoveryContext(provider, identity, bool(account['is_default']), receipt,
            account['source_home'], account['runtime_home'], token, account['discovery'])
