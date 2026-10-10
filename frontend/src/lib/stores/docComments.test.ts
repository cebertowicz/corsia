import { get } from 'svelte/store';
import { afterEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({ fetchComments: vi.fn(), persistPositions: vi.fn() }));
vi.mock('$lib/api/docs', () => ({
	fetchComments: mocks.fetchComments,
	postComment: vi.fn(),
	patchComment: vi.fn(),
	persistPositions: mocks.persistPositions,
}));

import { prepareCommentArrival, revealCommentArrival } from './docCommentArrivals';
import {
	docComments, commentLoads, loadComments, flushPositions, remapComments,
} from './docComments';

afterEach(() => {
	docComments.set({});
	commentLoads.set({});
	mocks.fetchComments.mockReset();
	mocks.persistPositions.mockReset();
});

describe('comment loading presentation', () => {
	it('shows loading then a factual error while preserving already loaded cards', async () => {
		docComments.set({ artifact: [{ id: 'known', anchor: 'quote', author: 'you', text: 'known', at: 'now', resolved: false, replies: [] }] });
		let reject!: (error: Error) => void;
		mocks.fetchComments.mockReturnValue(new Promise((_resolve, failure) => { reject = failure; }));
		const loading = loadComments('artifact');
		expect(get(commentLoads).artifact.state).toBe('loading');
		reject(new Error('Offline'));
		await expect(loading).rejects.toThrow('Offline');
		expect(get(commentLoads).artifact).toEqual({ state: 'error', error: 'Offline' });
		expect(get(docComments).artifact[0].id).toBe('known');
	});

	it('refreshes comments already shown without going back to loading', async () => {
		mocks.fetchComments.mockResolvedValueOnce({ comments: [], feedback: {} });
		await loadComments('artifact');
		expect(get(commentLoads).artifact.state).toBe('ready');

		let resolve!: (value: unknown) => void;
		mocks.fetchComments.mockReturnValue(new Promise((done) => { resolve = done; }));
		const refresh = loadComments('artifact');
		expect(get(commentLoads).artifact.state).toBe('ready');
		resolve({ comments: [{ id: 'new', author: 'you', author_kind: 'user', body: 'arrived', created_at: '2026-10-02T00:00:00Z' }], feedback: {} });
		await refresh;
		expect(get(commentLoads).artifact.state).toBe('ready');
		expect(get(docComments).artifact.map((comment) => comment.id)).toEqual(['new']);
	});

	it('keeps whether the document has an owner a re-open can reach', async () => {
		mocks.fetchComments.mockResolvedValueOnce({ comments: [], feedback: { owner_label: 'Documents' } });
		await loadComments('artifact');
		expect(get(commentLoads).artifact).toEqual({ state: 'ready', owner: 'Documents' });
		mocks.fetchComments.mockResolvedValueOnce({ comments: [], feedback: { owner_label: null } });
		await loadComments('artifact');
		expect(get(commentLoads).artifact).toEqual({ state: 'ready', owner: null });
	});

	it('runs one more load for a burst of changes, after the one already running', async () => {
		const row = (id: string) => ({ id, author: 'you', author_kind: 'user', body: id, created_at: '2026-10-02T00:00:00Z' });
		const pending: Array<(value: unknown) => void> = [];
		mocks.fetchComments.mockImplementation(() => new Promise((done) => { pending.push(done); }));
		const first = loadComments('artifact');
		const burst = [loadComments('artifact'), loadComments('artifact'), loadComments('artifact')];
		// Nothing overlaps: the burst waits for the load in flight.
		expect(mocks.fetchComments).toHaveBeenCalledTimes(1);
		pending[0]({ comments: [row('before-the-burst')], feedback: { batch: 'old' } });
		await first;
		await vi.waitFor(() => expect(mocks.fetchComments).toHaveBeenCalledTimes(2));
		pending[1]({ comments: [row('after-the-burst')], feedback: { batch: 'new' } });
		expect(await Promise.all(burst)).toEqual([{ batch: 'new' }, { batch: 'new' }, { batch: 'new' }]);
		expect(mocks.fetchComments).toHaveBeenCalledTimes(2);
		expect(get(docComments).artifact.map((comment) => comment.id)).toEqual(['after-the-burst']);
		expect(get(commentLoads).artifact.state).toBe('ready');
	});

	it('still runs the following load when the one in flight fails', async () => {
		let fail!: (error: Error) => void;
		mocks.fetchComments.mockReturnValueOnce(new Promise((_done, failure) => { fail = failure; }));
		mocks.fetchComments.mockResolvedValueOnce({ comments: [], feedback: {} });
		const first = loadComments('artifact');
		const following = loadComments('artifact');
		fail(new Error('Offline'));
		await expect(first).rejects.toThrow('Offline');
		await following;
		expect(get(commentLoads).artifact.state).toBe('ready');
	});

	it('reads as loading again when retrying after an error', async () => {
		mocks.fetchComments.mockRejectedValueOnce(new Error('Offline'));
		await expect(loadComments('artifact')).rejects.toThrow('Offline');
		mocks.fetchComments.mockReturnValue(new Promise(() => {}));
		void loadComments('artifact');
		expect(get(commentLoads).artifact.state).toBe('loading');
	});
});

describe('flushPositions', () => {
	it('does not persist a mapped unresolved comment when its anchor no longer matches restored draft text', async () => {
		docComments.set({ artifact: [{
			id: 'comment', from: 0, to: 5, anchor: 'quote', author: 'you', text: 'note', at: 'now', resolved: false, replies: [],
		}] });
		remapComments('artifact', (position) => position + 1);
		await flushPositions('artifact', 'inserted quote');
		expect(mocks.persistPositions).not.toHaveBeenCalled();
	});
});

describe('prepared comment arrivals', () => {
	it('holds future comment bodies until each queued reveal', async () => {
		mocks.fetchComments.mockResolvedValue({
			comments: [
				{ id: 'first', author: 'Agent', author_kind: 'agent', body: 'First body', anchor_text: 'one', from_off: 0, to_off: 3, resolved: false, replies: [], created_at: '2026-09-30T05:00:00Z' },
				{ id: 'second', author: 'Agent', author_kind: 'agent', body: 'Second body', anchor_text: 'two', from_off: 4, to_off: 7, resolved: false, replies: [], created_at: '2026-09-30T05:00:01Z' },
			],
			feedback: { open_count: 2, owner_label: null, grace_closes_at: null, undelivered_batches: [] },
		});
		const arrival = await prepareCommentArrival('artifact', 'first');
		expect(get(docComments).artifact).toBeUndefined();
		revealCommentArrival('artifact', arrival);
		expect(get(docComments).artifact.map((comment) => comment.id)).toEqual(['first']);
	});
});
