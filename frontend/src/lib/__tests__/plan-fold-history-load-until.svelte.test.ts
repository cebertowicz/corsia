import { flushSync } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createFoldHistory } from '$lib/components/plans/foldHistory.svelte';
import type { FoldedItem } from '$lib/api/plansFolded';
import { token } from '$lib/stores/auth';

/** A focus or reveal that pages a level while Show more is already fetching it waits for that fetch. */

const item = (id: string): FoldedItem => ({
	id, parent_id: null, kind: 'task', title: id, state: 'accepted', folded_at: '2026-09-01T00:00:00Z',
	fold_bucket: 'done', display: { label_key: 'finished', tone_key: 'complete' },
	folds: { done: 0, failed: 0, closed: 0 }, has_folded_children: false,
});

let cleanup: (() => void) | null = null;
beforeEach(() => token.set('test-bearer'));
afterEach(() => {
	cleanup?.();
	cleanup = null;
	vi.unstubAllGlobals();
});

describe('foldHistory.loadUntil', () => {
	it('waits for a page already in flight instead of spinning on it', async () => {
		let release: (() => void) | null = null;
		const gate = new Promise<void>((resolve) => { release = resolve; });
		const fetchMock = vi.fn(async () => {
			await gate;
			return { ok: true, status: 200, headers: { get: () => null },
				json: async () => ({ plan_id: 'p1', parent: 'root', items: [item('a'), item('b')], next_cursor: null }) };
		});
		vi.stubGlobal('fetch', fetchMock);
		let history!: ReturnType<typeof createFoldHistory>;
		cleanup = $effect.root(() => { history = createFoldHistory({ planId: () => 'p1', rootParent: () => null }); });

		flushSync();
		const showMore = history.loadMore('');
		expect(history.level('').status).toBe('loading');

		// Bounded loop guard: each pass of the wait loop scans the level's items, so a
		// spin that never yields to the event loop throws here instead of hanging the runner.
		const items = history.level('').items;
		const scan = items.some.bind(items);
		let scans = 0;
		Object.defineProperty(items, 'some', {
			configurable: true,
			value: (...args: Parameters<typeof scan>) => {
				scans += 1;
				if (scans > 1000) throw new Error('loadUntil spun on a level that is already loading');
				return scan(...args);
			},
		});

		try {
			// The page arrives on a macrotask, like a real network read; a loop that only
			// yields microtasks starves it.
			setTimeout(() => release!(), 0);
			const found = await history.loadUntil('', 'b');
			await showMore;
			expect(found).toBe(true);
			expect(fetchMock).toHaveBeenCalledTimes(1);
		} finally {
			delete (items as { some?: unknown }).some;
		}
	});
});
