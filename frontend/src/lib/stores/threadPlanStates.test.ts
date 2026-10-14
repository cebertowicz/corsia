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
	reloadThreadPlanStates,
	resetThreadPlanStatesForTests,
	stopThreadPlanStates,
	threadPlanStates,
	watchThreadPlanStates,
} from './threadPlanStates';

async function settle(): Promise<void> {
	for (let i = 0; i < 5; i++) await Promise.resolve();
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
		expect(mocks.read.mock.calls.map(([, chunk]) => chunk.length)).toEqual([100, 100, 30]);
	});

	it('reads only threads new to the list', async () => {
		mocks.read.mockResolvedValue({});
		watchThreadPlanStates(['a']);
		await settle();
		watchThreadPlanStates(['a', 'b']);
		await settle();
		expect(mocks.read.mock.calls.map(([, chunk]) => chunk)).toEqual([['a'], ['b']]);
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

	it('re-reads only the listed thread a host-access nudge names', async () => {
		mocks.read.mockResolvedValue({});
		watchThreadPlanStates(['a', 'b']);
		await settle();
		mocks.handlers.get('host_access_changed')?.({ lease_id: 'lease-1', thread_ids: ['b', 'z'] });
		await settle();
		expect(mocks.read.mock.calls.map(([, chunk]) => chunk)).toEqual([['a', 'b'], ['b']]);
	});

	it('re-reads only the listed root and visible rows a host-access nudge maps', async () => {
		mocks.read.mockResolvedValue({});
		watchThreadPlanStates(['a', 'b', 'c']);
		await settle();
		mocks.handlers.get('host_access_changed')?.({ lease_id: 'lease-1', thread_ids: ['hidden-child', 'a', 'c'] });
		await settle();
		expect(mocks.read.mock.calls.map(([, chunk]) => chunk)).toEqual([['a', 'b', 'c'], ['a', 'c']]);
	});

	it('re-reads every listed thread when a host-access nudge names none (lookup failed or older server)', async () => {
		mocks.read.mockResolvedValue({});
		watchThreadPlanStates(['a', 'b']);
		await settle();
		mocks.handlers.get('host_access_changed')?.({ lease_id: 'lease-1' });
		await settle();
		expect(mocks.read.mock.calls.map(([, chunk]) => chunk)).toEqual([['a', 'b'], ['a', 'b']]);
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
		expect(mocks.read.mock.calls.map(([, chunk]) => chunk)).toEqual([['a', 'b', 'c'], ['c']]);
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
		expect(mocks.read.mock.calls.map(([, chunk]) => chunk)).toEqual([['a', 'b'], ['b']]);
	});

	it('reloads a thread this device just marked, and nothing outside the list', async () => {
		mocks.read.mockResolvedValue({ a: { state: 'idle', label_key: null, paused: false, attention_items: [], has_update: true } });
		watchThreadPlanStates(['a']);
		await settle();
		reloadThreadPlanStates(['a', 'z']);
		await settle();
		expect(mocks.read.mock.calls.map(([, chunk]) => chunk)).toEqual([['a'], ['a']]);
		expect(get(threadPlanStates).a?.has_update).toBe(true);
	});


});
