import { describe, expect, it } from 'vitest';
import { flushSync } from 'svelte';
import { createHeldUnread } from './docHeldUnread.svelte';

type Listed = { id: string; newComment?: boolean; newReplyIds?: string[] };

function setup(initial: Listed[]) {
	const state = $state({ open: false, comments: initial });
	let held!: ReturnType<typeof createHeldUnread>;
	const cleanup = $effect.root(() => { held = createHeldUnread(() => state.open, () => state.comments); });
	flushSync();
	return { state, held, cleanup };
}

const range = (id: string, unread?: 'comment' | 'reply') => ({ id, unread });

describe('createHeldUnread', () => {
	it('paints nothing extra while no surface is open', () => {
		const { held, cleanup } = setup([{ id: 'a', newReplyIds: ['r1'] }]);
		expect(held.paint(range('a')).unread).toBeUndefined();
		cleanup();
	});

	it('keeps a thread unread after opening reads it, until the surface closes', () => {
		const { state, held, cleanup } = setup([{ id: 'a', newReplyIds: ['r1'] }, { id: 'b', newComment: true }, { id: 'c' }]);
		state.open = true;
		flushSync();
		// Opening a surface reads the threads at once: the store no longer reports them new.
		state.comments = [{ id: 'a', newReplyIds: [] }, { id: 'b', newComment: false }, { id: 'c' }];
		flushSync();
		expect(held.paint(range('a')).unread).toBe('reply');
		expect(held.paint(range('b')).unread).toBe('comment');
		expect(held.paint(range('c')).unread).toBeUndefined();
		state.open = false;
		flushSync();
		expect(held.paint(range('a')).unread).toBeUndefined();
		expect(held.paint(range('b')).unread).toBeUndefined();
		cleanup();
	});

	it('leaves a range that is already unread as it is', () => {
		const { state, held, cleanup } = setup([{ id: 'a', newComment: true }]);
		state.open = true;
		flushSync();
		expect(held.paint(range('a', 'reply')).unread).toBe('reply');
		cleanup();
	});
});
