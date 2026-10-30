import { get } from 'svelte/store';
import { token } from './auth';
import { loadComposerModels } from './modelsLoad';
import { threads } from './threads';
import { acceptsThreadAccountFrame, loadThreadVendorAccounts, threadAccountFields } from './threadVendorAccounts';
import { SERVER_UNREACHABLE } from '$lib/plainError';

export type PinResult = { ok: true; changed: boolean } | { ok: false; message: string };

/** What the picker tells a person for each reason the backend refuses a pin; any other is the generic sentence. */
export const TURN_RUNNING = 'A turn is running; try again when it finishes.';
const REFUSALS: Record<string, string> = {
	account_change_requires_idle: TURN_RUNNING,
};
export const PIN_FAILED = "Couldn't switch accounts. Try again.";

/** The refusal's reason code, which the backend writes first in its detail (`<code>; <advice>`). */
export function reasonOf(detail: unknown): string | null {
	return typeof detail === 'string' ? detail.trim().match(/^[a-z0-9]+(?:_[a-z0-9]+)+/)?.[0] ?? null : null;
}

/**
 * Pin a thread to one account. The choice is explicit, so a thread that changed since the picker
 * drew (a model switch, a checkpoint) is still moved; the backend refuses only an account the
 * thread may not use. During a running turn the choice is held and applies at the next turn, as a
 * model choice does. On success the thread's binding follows the response, its
 * account read is reloaded, and the catalog is read again so the row marked for the next turn is
 * the one the backend now resolves.
 */
export async function pinThreadAccount(threadId: string, accountId: string): Promise<PinResult> {
	const auth = get(token);
	if (!auth) return { ok: false, message: PIN_FAILED };
	let body: Record<string, unknown> | null;
	try {
		const response = await fetch(`/api/threads/${encodeURIComponent(threadId)}/vendor-account?token=${encodeURIComponent(auth)}`, {
			method: 'PUT',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ account_id: accountId }),
		});
		body = await response.json().catch(() => null);
		if (!response.ok) {
			const reason = reasonOf(body?.detail);
			return { ok: false, message: (reason && REFUSALS[reason]) || PIN_FAILED };
		}
	} catch {
		return { ok: false, message: SERVER_UNREACHABLE };
	}
	if (body?.pending === true) {
		// Chosen during a running turn, or another vendor's account: it applies at a later turn, and the
		// catalog already marks it. The thread's own binding is unchanged, so its fields stay.
		await loadComposerModels(threadId);
		return { ok: true, changed: false };
	}
	const frame = { thread_id: threadId, account_id: body?.account_id, runtime_context_epoch: body?.runtime_context_epoch };
	if (typeof frame.account_id !== 'string' || !acceptsThreadAccountFrame(frame)) return { ok: false, message: PIN_FAILED };
	threads.update((rows) => rows.map((row) => row.id === threadId ? { ...row, ...threadAccountFields(frame) } : row));
	await Promise.all([loadThreadVendorAccounts(threadId, `epoch-${frame.runtime_context_epoch}`), loadComposerModels(threadId)]);
	return { ok: true, changed: body?.changed === true };
}
