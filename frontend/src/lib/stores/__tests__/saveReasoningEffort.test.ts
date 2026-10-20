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

		expect(await saveReasoningEffort('s1', 'high', 'tok')).toBeNull();
		expect(listed()).toBe('high');
		expect(get(reasoningEffort)).toBe('high');
	});

	it("saves a worker's effort on its own row and leaves the open thread's alone", async () => {
		vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, status: 200 })));

		expect(await saveReasoningEffort('w1', 'high', 'tok')).toBeNull();
		expect(worker()).toBe('high');
		expect(get(reasoningEffort)).toBe('low');
		expect(listed()).toBe('low');
	});

	it('puts the previous effort back and says what happened when the PATCH is refused', async () => {
		vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, status: 500 })));

		expect(await saveReasoningEffort('s1', 'high', 'tok')).toMatch(/back to what it was\. Try again/);
		expect(listed()).toBe('low');
		expect(get(reasoningEffort)).toBe('low');
	});

	it("puts a worker's previous effort back when the request fails", async () => {
		vi.stubGlobal('fetch', vi.fn(async () => { throw new Error('network down'); }));

		expect(await saveReasoningEffort('w1', 'high', 'tok')).toMatch(/Couldn't reach the server/);
		expect(worker()).toBe('low');
	});

	it('leaves a newer pick alone when an older save fails after it', async () => {
		const first = Promise.withResolvers<{ ok: boolean; status: number }>();
		vi.stubGlobal('fetch', vi.fn()
			.mockReturnValueOnce(first.promise)
			.mockResolvedValueOnce({ ok: true, status: 200 }));

		const older = saveReasoningEffort('w1', 'medium', 'tok');
		expect(await saveReasoningEffort('w1', 'high', 'tok')).toBeNull();
		first.resolve({ ok: false, status: 500 });

		expect(await older).toBeNull();
		expect(worker()).toBe('high');
	});

	it('puts back the last effort the server confirmed when the newest save fails', async () => {
		const first = Promise.withResolvers<{ ok: boolean; status: number }>();
		vi.stubGlobal('fetch', vi.fn()
			.mockReturnValueOnce(first.promise)
			.mockResolvedValueOnce({ ok: false, status: 500 }));

		const older = saveReasoningEffort('w1', 'medium', 'tok');
		const newest = await saveReasoningEffort('w1', 'high', 'tok');
		first.resolve({ ok: true, status: 200 });

		expect(newest).toMatch(/back to what it was/);
		expect(await older).toBeNull();
		expect(worker()).toBe('medium');
	});
});
