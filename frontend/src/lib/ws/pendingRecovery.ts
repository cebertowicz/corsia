import { get } from 'svelte/store';
import { token } from '../stores/auth';
import { pendingInputThreads } from './turnState';
import { handleRecoveredInput } from './handlersTurns';
import { resendUnconfirmedInput } from './send';

const RECOVERY_PAGE_SIZE = 50;

// `isCurrent` reports whether the socket that started this recovery still owns
// the connection. Once it does not, no further page is read and nothing read
// under the retired session is written.
export async function recoverPendingInputs(
	fetchPage: typeof fetch = fetch,
	isCurrent: () => boolean = () => true,
): Promise<void> {
	const authToken = get(token);
	if (!authToken || !isCurrent()) return;
	for (const { threadId, inputIds } of pendingInputThreads()) {
		const unresolved = new Set(inputIds);
		let before: string | null = null;
		const seenCursors = new Set<string>();
		// Only reading the history to its end proves an input is not there. Every
		// other way out — a thrown fetch, a refused page, a cursor that repeats —
		// leaves the answer unknown, so nothing is resent on its strength.
		let readToEnd = false;
		while (unresolved.size > 0) {
			if (!isCurrent()) return;
			const query = new URLSearchParams({
				token: authToken,
				limit: String(RECOVERY_PAGE_SIZE),
			});
			if (before) query.set('before', before);
			let response: Response;
			try {
				response = await fetchPage(
					`/api/threads/${threadId}/messages?${query.toString()}`,
				);
			} catch {
				break;
			}
			if (!isCurrent()) return;
			if (!response.ok) break;
			const page = await response.json();
			if (!isCurrent()) return;
			for (const message of page.messages ?? []) {
				// An input's row is stored under the input id it was sent with, and a
				// receipt row names it in metadata too. Either one settles the row.
				const found = message?.metadata?.input_id ?? message?.id;
				if (typeof found !== 'string' || !unresolved.has(found)) continue;
				handleRecoveredInput(threadId, found, message);
				unresolved.delete(found);
			}
			const nextBefore = page.oldest_id;
			if (!page.has_more || typeof nextBefore !== 'string') {
				readToEnd = true;
				break;
			}
			if (seenCursors.has(nextBefore)) break;
			seenCursors.add(nextBefore);
			before = nextBefore;
		}
		if (!readToEnd || unresolved.size === 0 || !isCurrent()) continue;
		// The server never received these. Their frames go back on this socket in
		// the order they were sent; the server's duplicate check keeps an input
		// that did arrive from landing twice, and its receipt settles the row.
		for (const inputId of unresolved) resendUnconfirmedInput(inputId);
	}
}
