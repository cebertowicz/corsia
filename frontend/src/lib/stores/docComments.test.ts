import { get } from 'svelte/store';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ChangeSet } from '@codemirror/state';

const mocks = vi.hoisted(() => ({ fetchComments: vi.fn(), persistPositions: vi.fn(), postComment: vi.fn(), patchComment: vi.fn(), markCommentThreadViewed: vi.fn() }));
vi.mock('$lib/api/docs', () => ({
	fetchComments: mocks.fetchComments,
	postComment: mocks.postComment,
	patchComment: mocks.patchComment,
	persistPositions: mocks.persistPositions,
	markCommentThreadViewed: mocks.markCommentThreadViewed,
}));

import { prepareCommentArrival, revealCommentArrival } from './docCommentArrivals';
import {
	docComments, commentLoads, fromServer, hasUnread, loadComments, flushPositions, markThreadViewed, remapComments, addComment, addReply, resolveComment,
} from './docComments';
import { addRegionComment, moveRegion } from './imageRegionComments';

afterEach(() => {
	docComments.set({});
	commentLoads.set({});
	mocks.fetchComments.mockReset();
	mocks.persistPositions.mockReset();
	mocks.postComment.mockReset();
	mocks.patchComment.mockReset();
	mocks.markCommentThreadViewed.mockReset();
});

describe('tagged comment requests', () => {
	it('sends selected agent IDs and viewing thread for a comment and a reply', async () => {
		const row = { id: 'c', author: 'you', author_kind: 'user', body: '@Agent', created_at: '2026-10-02T00:00:00Z', tagged_agents: [{ id: 'a', name: 'Agent' }] };
		mocks.postComment.mockResolvedValue(row);
		const mentions = [{ agent_id: 'a', start: 3, end: 9 }];
		await addComment('artifact', 0, 1, 'x', '😀 @Agent', 'you', false, undefined, ['a'], 'viewer', mentions);
		expect(mocks.postComment).toHaveBeenLastCalledWith('artifact', expect.objectContaining({ tagged_agent_ids: ['a'], tagged_agent_mentions: mentions, thread_id: 'viewer' }));
		await addReply('artifact', 'c', '😀 @Agent', 'you', ['a'], 'viewer', mentions);
		expect(mocks.postComment).toHaveBeenLastCalledWith('artifact', expect.objectContaining({ parent_id: 'c', tagged_agent_ids: ['a'], tagged_agent_mentions: mentions, thread_id: 'viewer' }));
	});
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

describe('remapComments', () => {
	it('moves a comment onto text typed over all of it, like its highlight, and empties it when the text is deleted', () => {
		const comment = (id: string, from: number, to: number) => ({ id, from, to, anchor: 'quote', author: 'you', text: 'note', at: 'now', resolved: false, replies: [] });
		// The editor's own mapping of an edit to "one quote two", whose comment is on "quote" (4..9): the text deleted, then typed.
		const edit = (insert: string) => ChangeSet.of({ from: 4, to: 9 }, 13).compose(ChangeSet.of({ from: 4, insert }, 8));
		docComments.set({ remap: [comment('typed', 4, 9)] });
		remapComments('remap', edit('words!'));
		expect(get(docComments).remap[0]).toMatchObject({ from: 4, to: 10 });
		docComments.set({ remap: [comment('deleted', 4, 9)] });
		remapComments('remap', edit(''));
		expect(get(docComments).remap[0]).toMatchObject({ from: 4, to: 4 });
	});
});

describe('flushPositions', () => {
	it('does not persist a mapped unresolved comment when its anchor no longer matches restored draft text', async () => {
		docComments.set({ artifact: [{
			id: 'comment', from: 0, to: 5, anchor: 'quote', author: 'you', text: 'note', at: 'now', resolved: false, replies: [],
		}] });
		remapComments('artifact', ChangeSet.of({ from: 0, insert: 'i' }, 5));
		await flushPositions('artifact', 'inserted quote');
		expect(mocks.persistPositions).not.toHaveBeenCalled();
	});
});

describe('image region comments', () => {
	// Its own artifact: an earlier test leaves a load in flight on 'artifact', and loads queue per artifact.
	const row = { id: 'pin', author: 'you', author_kind: 'user', body: 'Too dark', created_at: '2026-10-10T00:00:00Z', region_x: 0.1, region_y: 0.2, region_w: 0.3, region_h: 0.1, region_number: 4, region_stale: true };

	it('reads a region, its number and whether the image changed under it', async () => {
		mocks.fetchComments.mockResolvedValueOnce({ comments: [row, { ...row, id: 'text', region_x: null, region_y: null, region_w: null, region_h: null, region_number: null }], feedback: {} });
		await loadComments('image');
		const [pin, text] = get(docComments).image;
		expect(pin).toMatchObject({ region: { x: 0.1, y: 0.2, w: 0.3, h: 0.1 }, regionNumber: 4, regionStale: true });
		expect(text.region).toBeUndefined();
	});

	it('posts a new region comment as the person and shows it first', async () => {
		mocks.postComment.mockResolvedValueOnce({ ...row, region_stale: false });
		const id = await addRegionComment('image', { x: 0.1, y: 0.2, w: 0.3, h: 0.1 }, 'Too dark');
		expect(id).toBe('pin');
		expect(mocks.postComment).toHaveBeenCalledWith('image', expect.objectContaining({ body: 'Too dark', region: { x: 0.1, y: 0.2, w: 0.3, h: 0.1 }, author_kind: 'user' }));
		expect(get(docComments).image[0]).toMatchObject({ id: 'pin', regionNumber: 4, regionStale: false });
	});

	it('moves a pin and drops its stale mark, keeping its number', async () => {
		mocks.fetchComments.mockResolvedValueOnce({ comments: [row], feedback: {} });
		await loadComments('image');
		mocks.patchComment.mockResolvedValueOnce({ ...row, region_x: 0.5, region_y: 0.5, region_w: 0, region_h: 0, region_stale: false });
		await moveRegion('image', 'pin', { x: 0.5, y: 0.5, w: 0, h: 0 });
		expect(mocks.patchComment).toHaveBeenCalledWith('pin', { region: { x: 0.5, y: 0.5, w: 0, h: 0 } });
		expect(get(docComments).image[0]).toMatchObject({ region: { x: 0.5, y: 0.5, w: 0, h: 0 }, regionNumber: 4, regionStale: false, text: 'Too dark' });
	});
});

describe('comment-thread view projection', () => {
	it('carries the server-named new reply ids and reply timestamps for new-reply presentation', () => {
		const comment = fromServer({
			id: 'root', artifact_id: 'artifact', parent_id: null, author: 'Steve', author_kind: 'user', body: 'Root', anchor_text: 'quote', from_off: 0, to_off: 5,
			review: 0, resolved: 0, created_at: '2026-10-16T17:00:00Z', updated_at: '2026-10-16T17:00:00Z',
			new_reply_ids: ['new'], replies: [{
				id: 'new', artifact_id: 'artifact', parent_id: 'root', author: 'Agent', author_kind: 'agent', body: 'Reply', anchor_text: null, from_off: null, to_off: null,
				review: 0, resolved: 0, created_at: '2026-10-16T17:02:00Z', updated_at: '2026-10-16T17:02:00Z',
			}],
		});
		expect(comment).toMatchObject({ newReplyIds: ['new'], replies: [{ id: 'new', createdAt: '2026-10-16T17:02:00Z' }] });
	});

	it("lights the person's own acknowledgement only, and carries everyone's in order", () => {
		const row = {
			id: 'root', artifact_id: 'artifact', parent_id: null, author: 'Steve', author_kind: 'user' as const, body: 'Root', anchor_text: 'quote',
			from_off: 0, to_off: 5, review: 0, resolved: 0, created_at: '2026-10-16T17:00:00Z', updated_at: '2026-10-16T17:00:00Z',
		};
		const agent = { id: 'infra', name: 'Infrastructure', kind: 'agent' as const };
		const person = { id: 'ui', name: 'Steve', kind: 'user' as const };
		expect(fromServer({ ...row, acknowledged_by: [agent] })).toMatchObject({ acked: false, acknowledgedBy: [agent] });
		expect(fromServer({ ...row, acknowledged_by: [agent, person] })).toMatchObject({ acked: true, acknowledgedBy: [agent, person] });
		expect(fromServer(row)).toMatchObject({ acked: false, acknowledgedBy: [] });
	});

	it('drops every acknowledgement as soon as a thread is re-opened, and restores them if the re-open fails', async () => {
		const acknowledgedBy = [{ id: 'ui', name: 'Steve', kind: 'user' as const }];
		const thread = { id: 'root', anchor: 'quote', author: 'you', text: 'Root', at: 'now', resolved: true, acked: true, acknowledgedBy, replies: [] };
		docComments.set({ artifact: [thread] });
		mocks.patchComment.mockResolvedValueOnce({});
		resolveComment('artifact', 'root', false);
		expect(get(docComments).artifact[0]).toMatchObject({ resolved: false, acked: false, acknowledgedBy: [] });
		expect(mocks.patchComment).toHaveBeenLastCalledWith('root', { resolved: false });

		docComments.set({ artifact: [thread] });
		let reject!: (error: Error) => void;
		mocks.patchComment.mockReturnValueOnce(new Promise((_resolve, failure) => { reject = failure; }));
		const quiet = vi.spyOn(console, 'error').mockImplementation(() => {});
		resolveComment('artifact', 'root', false);
		reject(new Error('Offline'));
		await vi.waitFor(() => expect(get(docComments).artifact[0]).toMatchObject({ resolved: true, acked: true, acknowledgedBy }));
		quiet.mockRestore();
	});

	it('resolving keeps the acknowledgements a thread already has', () => {
		const acknowledgedBy = [{ id: 'infra', name: 'Infrastructure', kind: 'agent' as const }];
		docComments.set({ artifact: [{ id: 'root', anchor: 'quote', author: 'you', text: 'Root', at: 'now', resolved: false, acked: false, acknowledgedBy, replies: [] }] });
		mocks.patchComment.mockResolvedValueOnce({});
		resolveComment('artifact', 'root');
		expect(get(docComments).artifact[0]).toMatchObject({ resolved: true, acknowledgedBy });
	});
});

describe('thread reply attention', () => {
	const thread = (id: string, newReplyIds: string[]) => ({ id, anchor: 'quote', author: 'you', text: id, at: 'now', resolved: false, from: 0, to: 5, replies: [], newReplyIds });
	const waiting = () => get(docComments).artifact.map((comment) => [comment.id, hasUnread(comment)]);

	it('keeps one copy of reply attention: the marker and the ids agree after a stamp', async () => {
		docComments.set({ artifact: [thread('a', ['a1', 'a2']), thread('b', ['b1'])] });
		expect(waiting()).toEqual([['a', true], ['b', true]]);
		mocks.markCommentThreadViewed.mockResolvedValue({ root_id: 'a', new_reply_ids: [] });
		await markThreadViewed('artifact', 'a', 'a2');
		const [a, b] = get(docComments).artifact;
		expect(a.newReplyIds).toEqual([]);
		expect(hasUnread(a)).toBe(false);
		expect(Object.keys(a)).not.toContain('replyWaiting');
		expect(waiting()).toEqual([['a', false], ['b', true]]);
		expect(b.newReplyIds).toEqual(['b1']);
	});

	it('stays waiting while another unseen reply remains in the thread', async () => {
		docComments.set({ artifact: [thread('a', ['a1', 'a2'])] });
		mocks.markCommentThreadViewed.mockResolvedValue({ root_id: 'a', new_reply_ids: ['a2'] });
		await markThreadViewed('artifact', 'a', 'a1');
		expect(get(docComments).artifact[0].newReplyIds).toEqual(['a2']);
		expect(waiting()).toEqual([['a', true]]);
	});
});

describe('prepared comment arrivals', () => {
	it('holds future comment bodies until each queued reveal', async () => {
		mocks.fetchComments.mockResolvedValue({
			comments: [
				{ id: 'first', author: 'Agent', author_kind: 'agent', body: 'First body', anchor_text: 'one', from_off: 0, to_off: 3, resolved: false, replies: [], created_at: '2026-09-30T05:00:00Z' },
				{ id: 'second', author: 'Agent', author_kind: 'agent', body: 'Second body', anchor_text: 'two', from_off: 4, to_off: 7, resolved: false, replies: [], created_at: '2026-09-30T05:00:01Z' },
			],
			feedback: { open_count: 2, owner_label: null, undelivered_batches: [] },
		});
		const arrival = await prepareCommentArrival('artifact', 'first');
		expect(get(docComments).artifact).toBeUndefined();
		revealCommentArrival('artifact', arrival);
		expect(get(docComments).artifact.map((comment) => comment.id)).toEqual(['first']);
	});
});

describe('resolving a comment', () => {
	const row = { id: 'c', anchor: 'quote', author: 'you', text: 'note', at: 'now', resolved: false, replies: [] };
	const resolvedNow = () => get(docComments).artifact[0].resolved;
	it('flips the flag at once and puts it back when the request fails', async () => {
		let refuse!: (error: Error) => void;
		mocks.patchComment.mockReturnValue(new Promise((_ok, no) => { refuse = no; }));
		docComments.set({ artifact: [row] });
		const answered = resolveComment('artifact', 'c');
		expect(resolvedNow()).toBe(true);
		refuse(new Error('Offline'));
		expect(await answered).toBe(false);
		expect(resolvedNow()).toBe(false);
	});
	it('with wait, keeps the old state until the request is answered, then changes it only if it was taken', async () => {
		let accept!: (value: unknown) => void;
		mocks.patchComment.mockReturnValueOnce(new Promise((ok) => { accept = ok; }));
		docComments.set({ artifact: [row] });
		const taken = resolveComment('artifact', 'c', true, true);
		expect(resolvedNow()).toBe(false);
		accept({});
		expect(await taken).toBe(true);
		expect(resolvedNow()).toBe(true);
		docComments.set({ artifact: [row] });
		mocks.patchComment.mockRejectedValueOnce(new Error('Offline'));
		expect(await resolveComment('artifact', 'c', true, true)).toBe(false);
		expect(resolvedNow()).toBe(false);
	});
});
