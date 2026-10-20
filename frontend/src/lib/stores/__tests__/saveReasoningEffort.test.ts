import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest';
import { get } from 'svelte/store';
import { activeThreadId, reasoningEffort, threads } from '$lib/stores/threads';
import { saveReasoningEffort } from '$lib/stores/threadEffort';
import { subagentMeta, subagentThreadRows } from '$lib/stores/subagents';

const listed = () => get(threads).find((s) => s.id === 's1')?.reasoning_effort ?? null;
const worker = () => get(subagentThreadRows).get('w1')?.reasoning_effort;

beforeEach(() => {
	activeThreadId.set('s1');
	reasoningEffort.set('low');
	threads.set([{ id: 's1', reasoning_effort: 'low' } as never]);
	subagentMeta.set(new Map([['w1', {
		childThreadId: 'w1', parentThreadId: 's1', name: 'Worker', provider: 'cc', status: 'running',
	}]]));
	subagentThreadRows.set(new Map([['w1', { reasoning_effort: 'low' }]]));
});

afterEach(() => {
	vi.restoreAllMocks();
	activeThreadId.set(null);
	reasoningEffort.set(null);
	threads.set([]);
	subagentMeta.set(new Map());
	subagentThreadRows.set(new Map());
});

describe('saveReasoningEffort', () => {
	it("keeps the open thread's effort in the list row and the global when the PATCH succeeds", async () => {
		vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, status: 200 })));

		expect(await saveReasoningEffort('s1', 'high', 'tok')).toBe(true);
		expect(listed()).toBe('high');
		expect(get(reasoningEffort)).toBe('high');
	});

	it("saves a worker's effort on its own row and leaves the open thread's alone", async () => {
		vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, status: 200 })));

		expect(await saveReasoningEffort('w1', 'high', 'tok')).toBe(true);
		expect(worker()).toBe('high');
		expect(get(reasoningEffort)).toBe('low');
		expect(listed()).toBe('low');
	});

	it('puts the previous effort back and answers false when the PATCH is refused', async () => {
		vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, status: 500 })));

		expect(await saveReasoningEffort('s1', 'high', 'tok')).toBe(false);
		expect(listed()).toBe('low');
		expect(get(reasoningEffort)).toBe('low');
	});

	it("puts a worker's previous effort back when the request fails", async () => {
		vi.stubGlobal('fetch', vi.fn(async () => { throw new Error('network down'); }));

		expect(await saveReasoningEffort('w1', 'high', 'tok')).toBe(false);
		expect(worker()).toBe('low');
	});
});
