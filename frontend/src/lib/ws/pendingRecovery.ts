import { get } from 'svelte/store';
import { token } from '../stores/auth';
import { pendingInputThreads } from './turnState';
import { handleRecoveredInput } from './handlersTurns';
import { holdThreadSends, releaseThreadSends } from './send';

const RECOVERY_PAGE_SIZE = 50;
// The longest one history page may hold the thread's new sends. A page can be
// slow, or never arrive, while the socket stays healthy, and nothing else would
// end the hold; past this the held messages go out and the read goes on, so an
// input it still proves lost is resent, behind them, rather than stranded.
export const RECOVERY_PAGE_LIMIT_MS = 5_000;

// `isCurrent` reports whether the socket that started this recovery still owns
// the connection. Once it does not, no further page is read and nothing read
// under the retired session is written.
//
// A thread with pending inputs is held from this call until its own read ends
// or one of its pages passes RECOVERY_PAGE_LIMIT_MS, so a message the person sends on this socket meanwhile cannot be admitted
// ahead of the input they sent first. Threads are read side by side, so one
// thread's slow read never holds another thread's sends. `generation` is this
// run's identity in those holds: a run that ends after a newer socket took over
// cannot release what the newer run set.
export async function recoverPendingInputs(
	fetchPage: typeof fetch = fetch,
	isCurrent: () => boolean = () => true,
): Promise<void> {
	const authToken = get(token);
	if (!authToken || !isCurrent()) return;
	const generation = {};
	const threads = pendingInputThreads();
	// Set before the first await, so nothing can slip onto the socket between
	// this run starting to read and the thread being held.
	holdThreadSends(generation, threads.map(({ threadId }) => threadId));
	const results = await Promise.allSettled(threads.map(({ threadId, inputIds }) => recoverThread(
		threadId, inputIds, authToken, generation, fetchPage, isCurrent,
	)));
	// A thread whose recovery threw leaves its lost input pending and unproved;
	// say why, since nothing else will.
	results.forEach((result, index) => {
		if (result.status === 'rejected') {
			console.warn('[recovery] pending-input recovery failed', threads[index].threadId, result.reason);
		}
	});
}

async function recoverThread(
	threadId: string,
	inputIds: readonly string[],
	authToken: string,
	generation: object,
	fetchPage: typeof fetch,
	isCurrent: () => boolean,
): Promise<void> {
	const unresolved = new Set(inputIds);
	// The inputs this thread's read proved the server never received.
	// Only reading the history to its end proves that. Every other way
	// out — a thrown fetch, a refused page, a cursor that repeats —
	// leaves the answer unknown, so nothing already on the wire is put
	// back on it on such strength.
	const provenAbsent = new Set<string>();
	try {
		let before: string | null = null;
		const seenCursors = new Set<string>();
		while (unresolved.size > 0) {
			if (!isCurrent()) return;
			const query = new URLSearchParams({
				token: authToken,
				limit: String(RECOVERY_PAGE_SIZE),
			});
			if (before) query.set('before', before);
			const page = await readPage(
				fetchPage, `/api/threads/${threadId}/messages?${query.toString()}`,
				() => releaseThreadSends(generation, threadId, new Set(), isCurrent),
			);
			if (!isCurrent()) return;
			if (!page) break;
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
				for (const inputId of unresolved) provenAbsent.add(inputId);
				break;
			}
			if (seenCursors.has(nextBefore)) break;
			seenCursors.add(nextBefore);
			before = nextBefore;
		}
	} finally {
		// The hold ends on every way out, including a page read that
		// throws: a message the person is waiting on must never be left
		// behind a recovery that is over.
		releaseThreadSends(generation, threadId, provenAbsent, isCurrent);
	}
}

// One history page, or null when the fetch threw or the server refused it. A
// body that is not JSON throws. A page still open at RECOVERY_PAGE_LIMIT_MS
// calls `liftHold` and is still read to the end.
async function readPage(fetchPage: typeof fetch, url: string, liftHold: () => void) {
	const timer = setTimeout(liftHold, RECOVERY_PAGE_LIMIT_MS);
	try {
		let response: Response;
		try {
			response = await fetchPage(url);
		} catch {
			return null;
		}
		return response.ok ? await response.json() : null;
	} finally {
		clearTimeout(timer);
	}
}
