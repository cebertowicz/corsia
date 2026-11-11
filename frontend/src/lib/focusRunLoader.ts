/**
 * Reading a collapsed Focus run: the inclusive range from its oldest row to its
 * newest, a page at a time, newest first. A large run loads further pages on
 * request, never all at once. The rows join the thread's own message store, so
 * tool results and live updates reach them like any loaded row.
 */
import { get } from 'svelte/store';
import { token } from '$lib/stores/auth';
import { activeThreadId, messages } from '$lib/stores/threads';
import { hydrateFetchedMessages, mergeFetchedHistoryRows } from './messageHistory';
import { parseRunStubs, type RunLoad, type RunStub } from './focusRuns';

export interface RunLoaderDeps {
	pageSize: number;
	loads: () => Record<string, RunLoad>;
	setLoad: (key: string, load: RunLoad) => void;
	/** The Turn summaries a range page carries. */
	onSummaries: (summaries: unknown) => void;
}

/** Read the next page of `stub`'s range. A read already in flight is not repeated. */
export async function loadRunRange(stub: RunStub, deps: RunLoaderDeps): Promise<void> {
	const key = stub.lastId;
	const prior = deps.loads()[key];
	// A read that returns after the thread on screen changed is dropped.
	const threadId = get(activeThreadId);
	const bearer = get(token);
	if (prior?.loading || !threadId || !bearer) return;
	const base = { oldestId: prior?.oldestId ?? null, coveredFirstId: prior?.coveredFirstId ?? stub.firstId };
	deps.setLoad(key, { ...base, hasMore: true, loading: true, error: false });
	const fail = () => deps.setLoad(key, { ...base, hasMore: true, loading: false, error: true });
	const params = new URLSearchParams({
		limit: String(deps.pageSize), from_id: stub.firstId, to_id: stub.lastId,
	});
	if (base.oldestId) params.set('before', base.oldestId);
	params.set('token', bearer);
	try {
		const res = await fetch(`/api/threads/${encodeURIComponent(threadId)}/messages?${params}`);
		if (get(activeThreadId) !== threadId) return;
		if (!res.ok) return fail();
		const page = await res.json();
		if (get(activeThreadId) !== threadId) return;
		messages.update((current) => mergeFetchedHistoryRows(current, hydrateFetchedMessages(page.messages ?? [])));
		deps.onSummaries(page.turn_summaries);
		deps.setLoad(key, {
			oldestId: page.oldest_id ?? base.oldestId,
			coveredFirstId: stub.firstId,
			hasMore: page.has_more === true,
			loading: false,
			error: false,
		});
	} catch (e) {
		console.error('loadRunRange failed:', e);
		if (get(activeThreadId) === threadId) fail();
	}
}

/**
 * Re-read the stubs holding `turnId` after it settled comment-only, so their counts
 * stay server-owned. A response that arrives after the stub or the thread on screen
 * changed is dropped.
 */
export async function refreshCommentRuns(
	threadId: string,
	turnId: string,
	runs: () => readonly RunStub[],
	setRuns: (next: RunStub[]) => void,
): Promise<void> {
	const bearer = get(token);
	if (!bearer || get(activeThreadId) !== threadId) return;
	for (const stub of runs().filter((run) => run.turnIds.includes(turnId))) {
		const params = new URLSearchParams({
			view: 'focus-run', limit: '1', from_id: stub.firstId, to_id: stub.lastId,
		});
		params.set('token', bearer);
		try {
			const res = await fetch(`/api/threads/${encodeURIComponent(threadId)}/messages?${params}`);
			if (!res.ok) throw new Error(`HTTP ${res.status}`);
			const page = await res.json();
			if (get(activeThreadId) !== threadId || !runs().includes(stub)) continue;
			const next = parseRunStubs(page);
			// The range ends where the stub did, so whether older rows continue it is unchanged.
			if (next.length) next[0].continuesOlder = stub.continuesOlder;
			setRuns(runs().flatMap((run) => (run === stub ? next : [run])));
		} catch (e) {
			console.error('refreshCommentRuns failed:', e);
		}
	}
}
