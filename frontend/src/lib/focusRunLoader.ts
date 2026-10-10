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
import type { RunLoad, RunStub } from './focusRuns';

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
