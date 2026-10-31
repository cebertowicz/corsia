import { VENDORS } from '$lib/conductorSources';
import type { ThreadVendorAccount } from '$lib/stores/threadVendorAccounts';
import type { TranscriptMessage } from './messageListModel';

/** The one account hold a sign-in clears; the line below is the one an expired login already writes. */
const SIGNED_OUT_HOLD = 'account_signed_out';

/** The oldest input of this chat held for a signed-out account, which the chat's notice follows. */
export function heldSignedOutInput(messages: TranscriptMessage[], threadId: string | null | undefined): TranscriptMessage | undefined {
	return threadId ? messages.find((msg) => msg.thread_id === threadId && msg.role === 'user' && msg.delivery_state === 'queued'
		&& msg.write_outcome === 'proved_no_write' && msg.delivery_error === SIGNED_OUT_HOLD) : undefined;
}

/**
 * The system row for that held input: the same row, same Sign in, Check and Cancel, an expired login
 * shows (`SystemMessageRow`), so one condition has one surface. Its words come from the account's own
 * row, never from here; this only names the account and the cause that row reads.
 */
export function heldSignedOutNotice(msg: TranscriptMessage, accounts: ThreadVendorAccount[]): TranscriptMessage | null {
	const account = accounts.find((row) => row.hold_reason === SIGNED_OUT_HOLD);
	if (!account) return null;
	return { id: `held-signin-${msg.id}`, thread_id: msg.thread_id, role: 'system', content: `${account.name} is signed out of ${VENDORS[account.provider]?.cli ?? 'this tool'}. Sign it in again under Settings › Providers.`,
		metadata: { cc: true, type: 'cc_error', cause_kind: 'sign_in_expired', account_id: account.id } };
}
