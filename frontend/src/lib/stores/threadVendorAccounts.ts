import { get, writable } from 'svelte/store';
import { token } from './auth';
import { threads } from './threads';
import type { VendorAccount } from './vendorAccounts';
import { plainErrorText, SERVER_UNREACHABLE } from '$lib/plainError';

export interface ThreadVendorAccount extends VendorAccount { hold_reason: string | null }
interface SelectionRead {
	key: string;
	accounts: ThreadVendorAccount[];
	error?: string;
	loading: boolean;
}
/** Current binding comes only from the authenticated account GET, never a history receipt. */
export const threadVendorAccounts = writable(new Map<string, SelectionRead>());
const pending = new Map<string, Promise<void>>();
const latestReads = new Map<string, symbol>();

function publish(threadId: string, read: SelectionRead) {
	threadVendorAccounts.update((rows) => new Map(rows).set(threadId, read));
}
export function loadThreadVendorAccounts(threadId: string, key: string): Promise<void> {
	const existing = get(threadVendorAccounts).get(threadId);
	if (existing?.key === key && !existing.error) return pending.get(threadId) ?? Promise.resolve();
	const auth = get(token);
	if (!auth) return Promise.resolve();
	const read = Symbol(); latestReads.set(threadId, read);
	publish(threadId, { key, accounts: existing?.accounts ?? [], loading: true });
	const work = (async () => {
		let accounts: ThreadVendorAccount[] = []; let error: string | undefined;
		try {
			const response = await fetch(`/api/threads/${encodeURIComponent(threadId)}/vendor-account?token=${encodeURIComponent(auth)}`);
			const body = await response.json().catch(() => null);
			if (!response.ok) error = plainErrorText(body?.detail, 'Could not load this thread’s account.');
			else if (!Array.isArray(body?.accounts) || body.accounts.some((row: ThreadVendorAccount) =>
				!row || typeof row.id !== 'string' || typeof row.provider !== 'string' || typeof row.name !== 'string'))
				error = 'Could not read this thread’s account.';
			else accounts = body.accounts;
		} catch { error = SERVER_UNREACHABLE; }
		if (latestReads.get(threadId) !== read) return;
		publish(threadId, { key, accounts: error ? [] : accounts, error, loading: false });
		pending.delete(threadId);
	})();
	pending.set(threadId, work);
	return work;
}

/** A late committed event can remain in history, but cannot reverse current selection. */
export function acceptsThreadAccountFrame(data: Record<string, unknown>): boolean {
	if (data.account_id === undefined) return true;
	if (typeof data.account_id !== 'string' || !data.account_id
		|| typeof data.runtime_context_epoch !== 'number' || !Number.isInteger(data.runtime_context_epoch)
		|| data.runtime_context_epoch < 0) return false;
	const thread = get(threads).find((row) => row.id === data.thread_id);
	const epoch = thread?.runtime_context_epoch;
	if (typeof epoch !== 'number') return true;
	return data.runtime_context_epoch > epoch || (data.runtime_context_epoch === epoch
		&& (!thread?.account_id || thread.account_id === data.account_id));
}
export function threadAccountFields(data: Record<string, unknown>) {
	return data.account_id === undefined ? {} : {
		account_id: data.account_id as string,
		runtime_context_epoch: data.runtime_context_epoch as number,
	};
}
