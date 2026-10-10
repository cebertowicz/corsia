import { get } from 'svelte/store';
import { token } from '../stores/auth';
import { pendingInputThreads } from './turnState';
import { handleRecoveredInput } from './handlersTurns';

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
				const inputId = message?.metadata?.input_id;
				if (typeof inputId !== 'string' || !unresolved.has(inputId)) continue;
				handleRecoveredInput(threadId, inputId, message);
				unresolved.delete(inputId);
			}
			const nextBefore = page.oldest_id;
			if (!page.has_more || typeof nextBefore !== 'string') break;
			if (seenCursors.has(nextBefore)) break;
			seenCursors.add(nextBefore);
			before = nextBefore;
		}
	}
}
