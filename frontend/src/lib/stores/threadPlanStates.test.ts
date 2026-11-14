import { get } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({
	read: vi.fn(),
	handlers: new Map<string, (message: unknown) => void>(),
}));

vi.mock('$lib/api/plans', () => ({ readThreadPlanStates: mocks.read }));
vi.mock('$lib/ws', () => ({
	on: (type: string, handler: (message: unknown) => void) => {
		mocks.handlers.set(type, handler);
		return () => mocks.handlers.delete(type);
	},
}));

import { token } from './auth';
import {
	listedNeedsYou,
	reloadThreadPlanStates,
	resetThreadPlanStatesForTests,
	stopThreadPlanStates,
	threadPlanStates,
	watchOpenThreadPlanStates,
	watchThreadPlanStates,
} from './threadPlanStates';

async function settle(): Promise<void> {
	for (let i = 0; i < 10; i++) await Promise.resolve();
}

beforeEach(() => {
	mocks.read.mockReset();
	token.set('bearer');
});

afterEach(() => {
	resetThreadPlanStatesForTests();
	token.set(null);
});

describe('threadPlanStates', () => {
	it('reads listed threads in chunks under the server cap', async () => {
		mocks.read.mockResolvedValue({});
		const ids = Array.from({ length: 230 }, (_, i) => `t${i}`);
		watchThreadPlanStates(ids);
		await settle();
		expect(mocks.read.mock.calls.map(([chunk]) => chunk.length)).toEqual([100, 100, 30]);
	});

	it('reads only threads new to the list', async () => {
		mocks.read.mockResolvedValue({});
		watchThreadPlanStates(['a']);
		await settle();
		watchThreadPlanStates(['a', 'b']);
		await settle();
		expect(mocks.read.mock.calls.map(([chunk]) => chunk)).toEqual([['a'], ['b']]);
	});

	it('drops a thread the server no longer reports', async () => {
		mocks.read.mockResolvedValueOnce({ a: { state: 'paused', label_key: 'waiting' } });
		watchThreadPlanStates(['a']);
		await settle();
		expect(get(threadPlanStates).a?.state).toBe('paused');

		mocks.read.mockResolvedValueOnce({});
		mocks.handlers.get('plans_changed')?.({ plans: [{ owner: { kind: 'thread', id: 'a' } }] });
		await settle();
		expect(get(threadPlanStates).a).toBeUndefined();
	});

	it('keeps the last reading when a read fails', async () => {
		mocks.read.mockResolvedValueOnce({ a: { state: 'moving', label_key: 'working' } });
		watchThreadPlanStates(['a']);
		await settle();

		mocks.read.mockResolvedValueOnce(null);
		mocks.handlers.get('plans_changed')?.({ plans: [{ owner: { kind: 'thread', id: 'a' } }] });
		await settle();
		expect(get(threadPlanStates).a?.state).toBe('moving');
	});

	it('never lets a slow older read overwrite a newer one', async () => {
		let releaseOld: (value: unknown) => void = () => {};
		mocks.read
			.mockReturnValueOnce(new Promise((resolve) => (releaseOld = resolve)))
			.mockResolvedValueOnce({ a: { state: 'blocked_on_you', label_key: 'needs_you' } });
		watchThreadPlanStates(['a']);
		mocks.handlers.get('plans_changed')?.({ plans: [{ owner: { kind: 'thread', id: 'a' } }] });
		await settle();
		releaseOld({ a: { state: 'moving', label_key: 'working' } });
		await settle();
		expect(get(threadPlanStates).a?.state).toBe('blocked_on_you');
	});

	it('folds nudges that arrive during a read into one read after it, which shows the latest state', async () => {
		let releaseFirst: (value: unknown) => void = () => {};
		mocks.read
			.mockReturnValueOnce(new Promise((resolve) => (releaseFirst = resolve)))
			.mockResolvedValueOnce({
				a: { state: 'blocked_on_you', label_key: 'needs_you' },
				b: { state: 'moving', label_key: 'working' },
			});
		watchThreadPlanStates(['a', 'b', 'c']);
		await settle();
		const nudge = (id: string) => mocks.handlers.get('plans_changed')?.({ plans: [{ owner: { kind: 'thread', id } }] });
		nudge('a');
		nudge('b');
		nudge('a');
		mocks.handlers.get('thread_updates_changed')?.({ thread_ids: ['b'] });
		await settle();
		expect(mocks.read).toHaveBeenCalledOnce();

		releaseFirst({ a: { state: 'moving', label_key: 'working' } });
		await settle();
		expect(mocks.read.mock.calls.map(([chunk]) => chunk)).toEqual([['a', 'b', 'c'], ['a', 'b']]);
		expect(get(threadPlanStates).a?.state).toBe('blocked_on_you');
		expect(get(threadPlanStates).b?.state).toBe('moving');
	});

	it('reads a thread named at any moment as a read ends', async () => {
		// Name 'b' a few microtasks after the read is released, at each depth in turn,
		// so one lands between the read ending and the next one being allowed to start.
		for (let depth = 0; depth < 8; depth++) {
			resetThreadPlanStatesForTests();
			mocks.read.mockReset();
			let release: (value: unknown) => void = () => {};
			mocks.read
				.mockReturnValueOnce(new Promise((resolve) => (release = resolve)))
				.mockResolvedValue({ b: { state: 'moving', label_key: 'working' } });
			watchThreadPlanStates(['a', 'b']);
			await settle();
			release({});
			let hop: () => void = () => void reloadThreadPlanStates(['b']);
			for (let i = 0; i < depth; i++) {
				const next = hop;
				hop = () => queueMicrotask(next);
			}
			queueMicrotask(hop);
			await settle();
			await settle();
			expect(get(threadPlanStates).b?.state, `named ${depth} microtasks after release`).toBe('moving');
		}
	});

	it('reads a lone nudge at once when no read is out', async () => {
		mocks.read.mockResolvedValue({});
		watchThreadPlanStates(['a']);
		await settle();
		mocks.handlers.get('plans_changed')?.({ plans: [{ owner: { kind: 'thread', id: 'a' } }] });
		// Synchronously, with no timer: a single notice adds no delay.
		expect(mocks.read).toHaveBeenCalledTimes(2);
	});

	it('re-reads only the listed thread a host-access nudge names', async () => {
		mocks.read.mockResolvedValue({});
		watchThreadPlanStates(['a', 'b']);
		await settle();
		mocks.handlers.get('host_access_changed')?.({ lease_id: 'lease-1', thread_ids: ['b', 'z'] });
		await settle();
		expect(mocks.read.mock.calls.map(([chunk]) => chunk)).toEqual([['a', 'b'], ['b']]);
	});

	it('re-reads only the listed root and visible rows a host-access nudge maps', async () => {
		mocks.read.mockResolvedValue({});
		watchThreadPlanStates(['a', 'b', 'c']);
		await settle();
		mocks.handlers.get('host_access_changed')?.({ lease_id: 'lease-1', thread_ids: ['hidden-child', 'a', 'c'] });
		await settle();
		expect(mocks.read.mock.calls.map(([chunk]) => chunk)).toEqual([['a', 'b', 'c'], ['a', 'c']]);
	});

	it('re-reads every listed thread when a host-access nudge names none (lookup failed or older server)', async () => {
		mocks.read.mockResolvedValue({});
		watchThreadPlanStates(['a', 'b']);
		await settle();
		mocks.handlers.get('host_access_changed')?.({ lease_id: 'lease-1' });
		await settle();
		expect(mocks.read.mock.calls.map(([chunk]) => chunk)).toEqual([['a', 'b'], ['a', 'b']]);
	});

	it('reads nothing for a host-access nudge whose named threads are not listed or invalid', async () => {
		mocks.read.mockResolvedValue({});
		watchThreadPlanStates(['a', 'b']);
		await settle();
		mocks.handlers.get('host_access_changed')?.({ lease_id: 'lease-1', thread_ids: [] });
		mocks.handlers.get('host_access_changed')?.({ lease_id: 'lease-2', thread_ids: ['z'] });
		mocks.handlers.get('host_access_changed')?.({ lease_id: 'lease-3', thread_ids: 'a' });
		await settle();
		expect(mocks.read).toHaveBeenCalledTimes(1);
	});

	it('re-reads only the listed threads a plans nudge names', async () => {
		mocks.read.mockResolvedValue({});
		watchThreadPlanStates(['a', 'b', 'c']);
		await settle();
		mocks.handlers.get('plans_changed')?.({
			plans: [{ owner: { kind: 'thread', id: 'c' } }, { owner: { kind: 'project_scope', id: 'a' } }],
		});
		await settle();
		expect(mocks.read.mock.calls.map(([chunk]) => chunk)).toEqual([['a', 'b', 'c'], ['c']]);
	});

	it('issues no read on a timer, however long the list stays open', async () => {
		vi.useFakeTimers();
		mocks.read.mockResolvedValue({ a: { state: 'idle', label_key: null, paused: false, attention_items: [] } });
		watchThreadPlanStates(['a']);
		await settle();
		await vi.advanceTimersByTimeAsync(60_000 * 3);
		await settle();
		expect(mocks.read).toHaveBeenCalledOnce();
		stopThreadPlanStates();
		vi.useRealTimers();
	});

	it('ignores nudges for plans outside the list', async () => {
		mocks.read.mockResolvedValue({});
		watchThreadPlanStates(['a']);
		await settle();
		mocks.handlers.get('plans_changed')?.({ plans: [{ owner: { kind: 'thread', id: 'z' } }] });
		await settle();
		expect(mocks.read).toHaveBeenCalledTimes(1);
	});

	it('re-reads only the listed threads a seen-state nudge names', async () => {
		mocks.read.mockResolvedValue({});
		watchThreadPlanStates(['a', 'b']);
		await settle();
		mocks.handlers.get('thread_updates_changed')?.({ thread_ids: ['b', 'z', 42] });
		await settle();
		expect(mocks.read.mock.calls.map(([chunk]) => chunk)).toEqual([['a', 'b'], ['b']]);
	});

	it('reloads a thread this device just marked, and nothing outside the list', async () => {
		mocks.read.mockResolvedValue({ a: { state: 'idle', label_key: null, paused: false, attention_items: [], has_update: true } });
		watchThreadPlanStates(['a']);
		await settle();
		reloadThreadPlanStates(['a', 'z']);
		await settle();
		expect(mocks.read.mock.calls.map(([chunk]) => chunk)).toEqual([['a'], ['a']]);
		expect(get(threadPlanStates).a?.has_update).toBe(true);
	});
});

/**
 * The phone's plan chip reads the open thread's row state by the menu's own rule,
 * including a thread the menu does not list (a hidden subagent's chat, another
 * scope) — without ever moving the menu's needs-you dot.
 */
describe('the open thread', () => {
	const entry = (state: string) => ({ state, label_key: null, paused: state === 'paused', attention_items: [] });

	it('reads and refreshes an open thread the menu does not list, and never counts it in the menu’s dot', async () => {
		mocks.read.mockImplementation(async (ids: string[]) => Object.fromEntries(ids.map((id) => [id, entry(id === 'open' ? 'needs_input' : 'idle')])));
		watchThreadPlanStates(['a']);
		await settle();
		watchOpenThreadPlanStates(['open']);
		await settle();

		expect(get(threadPlanStates).open?.state).toBe('needs_input');
		expect(get(listedNeedsYou), 'an open thread off the menu does not light the menu’s dot').toBe(false);

		// Pausing its item nudges only the plans owner, and refreshes the chip.
		mocks.read.mockImplementation(async (ids: string[]) => Object.fromEntries(ids.map((id) => [id, entry('paused')])));
		mocks.handlers.get('plans_changed')?.({ plans: [{ owner: { kind: 'thread', id: 'open' } }] });
		await settle();
		expect(mocks.read.mock.calls.map(([chunk]) => chunk)).toEqual([['a'], ['open'], ['open']]);
		expect(get(threadPlanStates).open?.state).toBe('paused');
		expect(get(listedNeedsYou)).toBe(false);
	});

	it('stops following the thread once its chat closes', async () => {
		mocks.read.mockResolvedValue({});
		watchOpenThreadPlanStates(['open']);
		await settle();
		watchOpenThreadPlanStates([]);
		mocks.handlers.get('plans_changed')?.({ plans: [{ owner: { kind: 'thread', id: 'open' } }] });
		await settle();
		expect(mocks.read.mock.calls.map(([chunk]) => chunk)).toEqual([['open']]);
	});

	it('keeps refreshing the open thread after the menu stops watching', async () => {
		mocks.read.mockResolvedValue({});
		watchThreadPlanStates(['a']);
		watchOpenThreadPlanStates(['open']);
		await settle();
		stopThreadPlanStates();
		mocks.handlers.get('plans_changed')?.({ plans: [{ owner: { kind: 'thread', id: 'open' } }] });
		await settle();
		expect(mocks.read.mock.calls.map(([chunk]) => chunk)).toEqual([['a'], ['open'], ['open']]);
	});
});

describe('listedNeedsYou', () => {
	it('is true only while a listed thread owes the person, and clears when none does', async () => {
		mocks.read.mockImplementation(async (ids: string[]) => Object.fromEntries(ids.map((id) => [id, {
			state: id === 'b' ? 'blocked_on_you' : 'idle', label_key: null, paused: false, attention_items: [],
		}])));
		watchThreadPlanStates(['a', 'b']);
		await settle();
		expect(get(listedNeedsYou)).toBe(true);

		// A thread off the menu (another scope) keeps its last reading but no longer counts.
		watchThreadPlanStates(['a']);
		expect(get(listedNeedsYou)).toBe(false);

		watchThreadPlanStates(['a', 'b']);
		await settle();
		expect(get(listedNeedsYou)).toBe(true);
		threadPlanStates.update((states) => ({ ...states, b: { ...states.b, state: 'idle' } }));
		expect(get(listedNeedsYou)).toBe(false);
	});
});
