"""Recover an unchecked account when a second sign-in repeats its identity."""
from connections.account_leftovers import commit_if_leftover
from connections.account_signin_commit import unchecked_repeat_receipt
from connections.corsia_agent_account import finish_signin
from connections.vendor_accounts import get_account
from infra.db_async import run_in_db


async def retry_unchecked(operation, account_id, binary, cleanup):
    operation.duplicate_of = account_id
    await cleanup(await run_in_db(get_account, operation.account_id))
    receipt = await run_in_db(unchecked_repeat_receipt, account_id)
    if receipt is None:
        operation.status, operation.cause = 'failed', 'same_account'
        return
    await run_in_db(commit_if_leftover, operation.account_id)
    await finish_signin(operation, receipt, binary, target_account_id=account_id)
    operation.status, operation.cause = 'failed', 'same_account'
