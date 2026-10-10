import { get } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { loadRunRange } from '$lib/focusRunLoader';
import type { RunLoad, RunStub } from '$lib/focusRuns';
import { token } from '$lib/stores/auth';
import { activeThreadId, messages } from '$lib/stores/threads';

const stub: RunStub = {
	firstId: 'm2', lastId: 'm9', firstSeq: 2, lastSeq: 9, count: 8, turnIds: ['t1'], weight: 8, kinds: ['step'], continuesOlder: false,
};
const row = (id: string, seq: number) => ({
	id, thread_id: 'thread-1', role: 'assistant', content: '', seq, created_at: '2026-10-02T10:00:00Z',
});

let loads: Record<string, RunLoad>;
const summaries: unknown[] = [];
const deps = () => ({
	pageSize: 50,
	loads: () => loads,
	setLoad: (key: string, load: RunLoad) => { loads = { ...loads, [key]: load }; },
	onSummaries: (value: unknown) => summaries.push(value),
});

const originalFetch = globalThis.fetch;
function serve(pages: Array<{ ok?: boolean; body?: unknown }>) {
	const calls: URL[] = [];
	globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
		calls.push(new URL(String(input), 'http://app.test'));
		const next = pages.shift() ?? { ok: false };
		return { ok: next.ok ?? true, status: next.ok === false ? 500 : 200, json: async () => next.body };
	}) as unknown as typeof fetch;
	return calls;
}

beforeEach(() => {
	loads = {};
	summaries.length = 0;
	token.set('tok');
	activeThreadId.set('thread-1');
	messages.set([]);
	vi.spyOn(console, 'error').mockImplementation(() => {});
});
afterEach(() => {
	globalThis.fetch = originalFetch;
	vi.restoreAllMocks();
	messages.set([]);
	activeThreadId.set(null);
});

describe('reading a stub run', () => {
	it('asks for its inclusive range, newest first, and keeps the rows in the thread', async () => {
		const calls = serve([{ body: {
			messages: [row('m8', 8), row('m9', 9)], has_more: true, oldest_id: 'm8',
			turn_summaries: { t1: { status: 'completed' } },
		} }]);

		await loadRunRange(stub, deps());

		const [call] = calls;
		expect(call.pathname).toBe('/api/threads/thread-1/messages');
		expect(call.searchParams.get('from_id')).toBe('m2');
		expect(call.searchParams.get('to_id')).toBe('m9');
		expect(call.searchParams.get('limit')).toBe('50');
		expect(call.searchParams.has('before')).toBe(false);
		expect(get(messages).map((message) => message.id)).toEqual(['m8', 'm9']);
		expect(loads.m9).toMatchObject({ oldestId: 'm8', hasMore: true, loading: false, error: false });
		expect(summaries).toEqual([{ t1: { status: 'completed' } }]);
	});

	it('continues before the oldest row it has, until the range is read', async () => {
		const calls = serve([
			{ body: { messages: [row('m8', 8), row('m9', 9)], has_more: true, oldest_id: 'm8' } },
			{ body: { messages: [row('m2', 2)], has_more: false, oldest_id: 'm2' } },
		]);

		await loadRunRange(stub, deps());
		await loadRunRange(stub, deps());

		expect(calls[1].searchParams.get('before')).toBe('m8');
		expect(loads.m9).toMatchObject({ oldestId: 'm2', hasMore: false });
	});

	it('records a failed read so it can be retried, and a retry reads the same page', async () => {
		const calls = serve([{ ok: false }, { body: { messages: [row('m9', 9)], has_more: false, oldest_id: 'm9' } }]);

		await loadRunRange(stub, deps());
		expect(loads.m9).toMatchObject({ error: true, loading: false });
		expect(get(messages)).toEqual([]);

		await loadRunRange(stub, deps());
		expect(loads.m9).toMatchObject({ error: false, hasMore: false });
		expect(calls).toHaveLength(2);
		expect(calls[1].searchParams.has('before')).toBe(false);
	});

	it('does not repeat a read already in flight', async () => {
		const calls = serve([{ body: { messages: [], has_more: false, oldest_id: null } }]);
		const first = loadRunRange(stub, deps());
		await loadRunRange(stub, deps());
		await first;

		expect(calls).toHaveLength(1);
	});

	it('drops rows that arrive after the thread on screen changed', async () => {
		serve([{ body: { messages: [row('m9', 9)], has_more: false, oldest_id: 'm9' } }]);
		const read = loadRunRange(stub, deps());
		activeThreadId.set('thread-2');
		await read;

		expect(get(messages)).toEqual([]);
	});
});
