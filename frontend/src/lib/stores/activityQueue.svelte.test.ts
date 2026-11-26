import { beforeEach, describe, expect, it, vi } from 'vitest';

const listeners = new Map<string, () => void>();
vi.mock('$lib/ws/events', () => ({
	on: (event: string, handler: () => void) => {
		listeners.set(event, handler);
		return () => listeners.delete(event);
	},
}));

type Pending = { scope: string | null; signal: AbortSignal; resolve: (value: unknown) => void };
const reads: Pending[] = [];
vi.mock('$lib/api/activityQueue', () => ({
	ActivityQueueError: class extends Error {
		constructor(public status: number) {
			super(String(status));
		}
	},
	fetchActivityQueue: (scope: string | null, signal: AbortSignal) =>
		new Promise((resolve, reject) => {
			reads.push({ scope, signal, resolve });
			signal.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')));
		}),
}));

const { createActivityQueue } = await import('./activityQueue.svelte');

const body = (title: string) => ({ count: 0, items: [], ranker: null, notifications: { count: 0, items: [] }, title });
const open = () => reads.filter((read) => !read.signal.aborted);
const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

describe('Activity queue reads', () => {
	beforeEach(() => {
		reads.length = 0;
		listeners.clear();
	});

	it('keeps one read open while changes arrive, then reads once more', async () => {
		const store = createActivityQueue(() => null);
		store.start();
		for (let i = 0; i < 5; i += 1) listeners.get('plans_changed')!();
		expect(reads).toHaveLength(1);
		reads[0].resolve(body('first'));
		await settle();
		expect(reads).toHaveLength(2);
		reads[1].resolve(body('second'));
		await settle();
		expect(reads).toHaveLength(2);
		expect((store.queue as unknown as { title: string }).title).toBe('second');
	});

	it('cancels the open read when the scope changes, so only the new scope stays open', async () => {
		let scope: string | null = 'a';
		const store = createActivityQueue(() => scope);
		store.start();
		listeners.get('plans_changed')!();
		scope = 'b';
		store.start();
		await settle();
		expect(reads[0].signal.aborted).toBe(true);
		expect(open().map((read) => read.scope)).toEqual(['b']);
		open()[0].resolve(body('b'));
		await settle();
		// The replaced run's pending change never starts a second read for the old scope.
		expect(reads).toHaveLength(2);
		expect(store.status).toBe('ready');
	});

	it('cancels the open read on stop and shows no failure for it', async () => {
		const store = createActivityQueue(() => null);
		const stop = store.start();
		stop();
		await settle();
		expect(reads[0].signal.aborted).toBe(true);
		expect(open()).toHaveLength(0);
		expect(store.failure).toBeNull();
	});
});
