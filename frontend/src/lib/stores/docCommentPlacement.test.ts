import { createHash } from 'node:crypto';
import { get } from 'svelte/store';
import { afterEach, describe, expect, it, vi } from 'vitest';
import * as Y from 'yjs';

const mocks = vi.hoisted(() => ({ fetchComments: vi.fn(), persistPositions: vi.fn(async () => {}) }));
vi.mock('$lib/api/docs', () => ({
	fetchComments: mocks.fetchComments, postComment: vi.fn(), patchComment: vi.fn(),
	persistPositions: mocks.persistPositions, markCommentThreadViewed: vi.fn(),
}));

import { docComments, commentLoads, flushPositions, loadComments, reloadPlacements, remapComments, retryPlacement, type DocComment } from './docComments';
import { liveDocumentBindings } from './liveDocuments';
import { LiveDocumentSession } from './liveDocumentSession';
import { decodeUpdate, encodeUpdate } from './documentWire';
import { commentsInDocumentOrder } from '$lib/docCanvasComments';
import { captureComment, selectComment } from './commentSelection';

// The server's wire digest, computed independently of the browser's.
const wireDigest = (text: string) => createHash('sha256').update(text, 'utf8').digest('hex').slice(0, 16);
const settled = async () => { for (let i = 0; i < 5; i++) await new Promise(resolve => setTimeout(resolve, 0)); };

function serverRow(id: string, anchor: string, from: number, to = from + anchor.length) {
	return { id, from_off: from, to_off: to, anchor_text: anchor, author: 'agent', author_kind: 'agent', body: id,
		created_at: '2026-10-03T00:00:00Z', resolved: false, orphaned: false, review: false, replies: [] };
}

/** A live session on a controllable socket, beside the server document it talks to. */
function live(text: string, storeId = 'store') {
	const server = new Y.Doc();
	server.getText('text').insert(0, text);
	const sent: Record<string, any>[] = [];
	const session = new LiveDocumentSession({ artifactId: 'artifact', storeId, doc: new Y.Doc(), writable: true,
		send: (frame) => { sent.push(frame); return true; } });
	session.opened(Y.encodeStateAsUpdate(server), Y.encodeStateVector(server));
	const frame = (fields: Record<string, any>) => session.handle({ artifact_id: 'artifact', store_id: storeId, ...fields } as any);
	const ack = (...messages: Record<string, any>[]) => frame({ type: 'doc_ack', message_ids: messages.map(m => m.message_id) });
	ack(sent.at(-1)!); // the opening sync is acknowledged, so the tab starts clean
	const serverText = () => server.getText('text').toString();
	return {
		server, session, sent, frame, ack, serverText,
		asks: () => sent.filter(f => f.type === 'doc_positions'),
		updates: () => sent.filter(f => f.type === 'doc_update'),
		/** The server merges a browser message. */
		merge: (message: Record<string, any>) => Y.applyUpdate(server, decodeUpdate(message.update)),
		/** Another session edits; the server forwards the update. */
		remote: (edit: (text: Y.Text) => void) => {
			const vector = Y.encodeStateVector(server);
			edit(server.getText('text'));
			frame({ type: 'doc_update', update: encodeUpdate(Y.encodeStateAsUpdate(server, vector)) });
		},
		/** The server's answer to `ask`, describing its own text now. */
		answer: (ask: Record<string, any>, fields: Record<string, any> = {}) => frame({
			type: 'doc_positions', seq: ask.seq, request_id: ask.request_id, digest: wireDigest(serverText()),
			vector: encodeUpdate(Y.encodeStateVector(server)),
			ranges: [{ id: 'c1', from_off: serverText().indexOf('beta'), to_off: serverText().indexOf('beta') + 4 }],
			...fields,
		}),
	};
}

const rows = (): DocComment[] => get(docComments).artifact ?? [];
const row = (id = 'c1') => rows().find(comment => comment.id === id)!;

async function opened(text = 'alpha beta gamma', comments = [serverRow('c1', 'beta', 6)]) {
	const doc = live(text);
	liveDocumentBindings.set({ tab: doc.session });
	mocks.fetchComments.mockResolvedValue({ comments, feedback: {} });
	await loadComments('artifact');
	return doc;
}

afterEach(() => {
	liveDocumentBindings.set({});
	docComments.set({});
	commentLoads.set({});
	mocks.fetchComments.mockReset();
	mocks.persistPositions.mockClear();
	vi.restoreAllMocks();
});

describe('comment placement in a live document (4.2)', () => {
	it('isolates capture and placement replies with the same sequence by request identity', async () => {
		const doc = await opened(), placement = doc.asks()[0];
		const capture = captureComment(selectComment(doc.session, 6, 10), placement.seq, new AbortController().signal);
		await vi.waitFor(() => expect(doc.asks()).toHaveLength(2));
		const captureAsk = doc.asks()[1];
		expect(captureAsk.seq).toBe(placement.seq);
		expect(captureAsk.request_id).not.toBe(placement.request_id);
		doc.answer(captureAsk, { reserved: true });
		await expect(capture).resolves.toMatchObject({ quote: 'beta' });
		expect(row().placement).toBe('placing');
		doc.answer(placement);
		await vi.waitFor(() => expect(row()).toMatchObject({ placement: 'placed', from: 6, to: 10 }));
	});

	it('asks over the document socket and places a row from a matching answer, then moves it by its own positions', async () => {
		const doc = await opened();
		expect(row()).toMatchObject({ placement: 'placing', drawn: false });
		expect(doc.asks()).toEqual([expect.objectContaining({ type: 'doc_positions', artifact_id: 'artifact', store_id: 'store',
			seq: expect.any(Number), request_id: expect.any(String), reload: false })]);
		doc.answer(doc.asks()[0]);
		await vi.waitFor(() => expect(row()).toMatchObject({ placement: 'placed', drawn: true, from: 6, to: 10 }));
		doc.remote(text => text.insert(0, 'X '));
		doc.remote(text => text.insert(12, '!')); // exactly at the end: outside the range
		doc.remote(text => text.insert(8, '>')); // exactly at the start: outside the range
		expect(row()).toMatchObject({ from: 9, to: 13 });
		expect(doc.session.text.toString().slice(9, 13)).toBe('beta');
		remapComments('artifact', position => position + 100); // the editor's mapping never moves a live row
		expect(row()).toMatchObject({ from: 9, to: 13 });
	});

	it('never remaps or queues for saving a live row that is still being placed', async () => {
		await opened();
		expect(row()).toMatchObject({ placement: 'placing', drawn: false, from: 6, to: 10 });
		remapComments('artifact', position => position + 100);
		expect(row()).toMatchObject({ from: 6, to: 10 });
		await flushPositions('artifact');
		expect(mocks.persistPositions).not.toHaveBeenCalled();
	});

	it('does not let an older delayed digest overwrite a newer completed Reload placement', async () => {
		const doc = await opened();
		const [older] = doc.asks();
		const digest = crypto.subtle.digest.bind(crypto.subtle);
		let release!: () => void;
		vi.spyOn(crypto.subtle, 'digest').mockImplementationOnce(async (algorithm, data) => {
			await new Promise<void>(resolve => { release = resolve; });
			return digest(algorithm, data);
		});
		doc.answer(older);
		await vi.waitFor(() => expect(release).toBeTypeOf('function'));

		expect(reloadPlacements('artifact')).toBe(true);
		const newer = doc.asks()[1];
		doc.answer(newer, { ranges: [{ id: 'c1', from_off: 0, to_off: 5 }] });
		await vi.waitFor(() => expect(row()).toMatchObject({ placement: 'placed', from: 0, to: 5, placementReloading: false }));
		release();
		await settled();
		expect(row()).toMatchObject({ placement: 'placed', from: 0, to: 5, placementReloading: false });
	});

	it('places only the rows a Reload answer names; a row the server left orphaned keeps no place', async () => {
		const orphan = { ...serverRow('c2', 'delta', 11, 11), orphaned: true };
		const doc = await opened('alpha beta gamma', [serverRow('c1', 'beta', 6), orphan]);
		doc.answer(doc.asks()[0]);
		await vi.waitFor(() => expect(row()).toMatchObject({ placement: 'placed', drawn: true }));
		expect(reloadPlacements('artifact')).toBe(true);
		// The server's answer for an unresolved quote names only the rows its resolution placed.
		doc.answer(doc.asks()[1]);
		await vi.waitFor(() => expect(row('c2').placementReloading).toBe(false));
		expect(row()).toMatchObject({ placement: 'placed', drawn: true, from: 6, to: 10 });
		expect(row('c2')).toMatchObject({ placement: undefined, drawn: false, orphaned: true });
		expect(commentsInDocumentOrder(rows(), doc.session.text.toString()).filter(comment => comment.orphaned).map(comment => comment.id))
			.toEqual(['c2']);
	});

	it('re-queues an older Reload after a newer refresh completes while its digest is delayed', async () => {
		const doc = await opened();
		doc.answer(doc.asks()[0]);
		await vi.waitFor(() => expect(row().placement).toBe('placed'));
		expect(reloadPlacements('artifact')).toBe(true);
		const older = doc.asks()[1];
		const digest = crypto.subtle.digest.bind(crypto.subtle);
		let release!: () => void;
		vi.spyOn(crypto.subtle, 'digest').mockImplementationOnce(async (algorithm, data) => {
			await new Promise<void>(resolve => { release = resolve; });
			return digest(algorithm, data);
		});
		doc.answer(older);
		await vi.waitFor(() => expect(release).toBeTypeOf('function'));

		await loadComments('artifact');
		const newer = doc.asks()[2];
		doc.answer(newer, { ranges: [{ id: 'c1', from_off: 0, to_off: 5 }] });
		await vi.waitFor(() => expect(row()).toMatchObject({ placement: 'placed', from: 0, to: 5, placementReloading: true }));
		release();
		await vi.waitFor(() => expect(doc.asks()).toHaveLength(4));
		expect(doc.asks()[3]).toMatchObject({ reload: true });
		doc.answer(doc.asks()[3], { ranges: [{ id: 'c1', from_off: 0, to_off: 5 }] });
		await vi.waitFor(() => expect(row()).toMatchObject({ placement: 'placed', from: 0, to: 5, placementReloading: false }));
	});

	it('keeps one request in flight plus one marked ask, and an older answer releases nothing', async () => {
		const doc = await opened();
		const [first] = doc.asks();
		mocks.fetchComments.mockResolvedValue({ comments: [serverRow('c1', 'beta', 6), serverRow('c2', 'gamma', 11)], feedback: {} });
		await loadComments('artifact');
		await loadComments('artifact');
		expect(doc.asks()).toHaveLength(1);
		const both = { ranges: [{ id: 'c1', from_off: 6, to_off: 10 }, { id: 'c2', from_off: 11, to_off: 16 }] };
		doc.answer(first, both);
		await vi.waitFor(() => expect(doc.asks()).toHaveLength(2));
		const second = doc.asks()[1];
		expect(second.seq).toBeGreaterThan(first.seq);
		doc.answer(first, { ranges: [{ id: 'c1', from_off: 0, to_off: 5 }] }); // stale: the newer request stays in flight
		await settled();
		expect(row('c1')).toMatchObject({ placement: 'placed', from: 6, to: 10 });
		expect(row('c2')).toMatchObject({ placement: 'placed', from: 11, to: 16 });
		retryPlacement('c2');
		await settled();
		expect(doc.asks()).toHaveLength(2);
		doc.answer(second, both);
		await vi.waitFor(() => expect(doc.asks()).toHaveLength(3)); // the one marked ask, sent once the answer releases
		await settled();
		expect(doc.asks()).toHaveLength(3);
		expect(row('c2').placement).toBe('placed');
	});

	it('ignores an answer or a store_mismatch refusal from another store, and the request stays in flight', async () => {
		const doc = await opened();
		const [ask] = doc.asks();
		doc.session.handle({ type: 'doc_positions', artifact_id: 'artifact', store_id: 'other', seq: ask.seq,
			digest: wireDigest(doc.serverText()), ranges: [{ id: 'c1', from_off: 0, to_off: 4 }] } as any);
		doc.session.handle({ type: 'doc_error', kind: 'store_mismatch', artifact_id: 'artifact', store_id: 'other',
			request_id: ask.request_id } as any);
		await settled();
		expect(row().placement).toBe('placing');
		await loadComments('artifact');
		expect(doc.asks()).toHaveLength(1);
		doc.answer(ask);
		await vi.waitFor(() => expect(row().placement).toBe('placed'));
	});

	it('releases the request on a disconnect, keeps rows as they were, and asks again when reconnect sync completes', async () => {
		const doc = await opened();
		const [lost] = doc.asks();
		doc.session.disconnected();
		await loadComments('artifact');
		expect(row().placement).toBe('placing');
		expect(doc.asks()).toHaveLength(1);
		doc.session.opened(Y.encodeStateAsUpdate(doc.server), Y.encodeStateVector(doc.server));
		doc.frame({ type: 'doc_opened', live: true });
		expect(doc.asks()).toHaveLength(2);
		doc.answer(lost); // the answer lost with the old connection
		await settled();
		expect(row().placement).toBe('placing');
		doc.answer(doc.asks()[1]);
		await vi.waitFor(() => expect(row().placement).toBe('placed'));
	});

	it('preserves a queued Reload through a comment refresh and disconnect, then retries it on reconnect', async () => {
		const doc = await opened();
		expect(reloadPlacements('artifact')).toBe(true);
		doc.session.disconnected();
		await loadComments('artifact');
		expect(row().placementReloading).toBeFalsy();
		doc.session.opened(Y.encodeStateAsUpdate(doc.server), Y.encodeStateVector(doc.server));
		doc.frame({ type: 'doc_opened', live: true });
		expect(doc.asks()).toHaveLength(2);
		expect(doc.asks()[1]).toMatchObject({ reload: true });
	});

	it('shows Not placed when the request errors, asks again after reconnect, and Retry asks with reload', async () => {
		const doc = await opened();
		doc.frame({ type: 'doc_error', kind: 'refused', request_id: doc.asks()[0].request_id, message: 'Comment positions need a nonnegative sequence' });
		await settled();
		expect(row()).toMatchObject({ placement: 'not_placed', drawn: false });
		expect(doc.asks()).toHaveLength(1);
		doc.session.disconnected();
		doc.session.opened(Y.encodeStateAsUpdate(doc.server), Y.encodeStateVector(doc.server));
		doc.frame({ type: 'doc_opened', live: true });
		expect(row().placement).toBe('placing');
		doc.frame({ type: 'doc_error', kind: 'refused', request_id: doc.asks()[1].request_id, message: 'no' });
		await settled();
		expect(row().placement).toBe('not_placed');
		retryPlacement('c1');
		expect(row().placement).toBe('placing');
		expect(doc.asks()[2]).toMatchObject({ reload: true });
		doc.answer(doc.asks()[2]);
		await vi.waitFor(() => expect(row().placement).toBe('placed'));
	});

	it('drops every position and Placing state with a reset document, and ignores the old store\'s answer', async () => {
		const old = await opened();
		old.answer(old.asks()[0]);
		await vi.waitFor(() => expect(row().drawn).toBe(true));
		mocks.fetchComments.mockResolvedValue({ comments: [serverRow('c1', 'beta', 6, 9)], feedback: {} });
		await loadComments('artifact');
		const inFlight = old.asks()[1];
		const next = live('alpha beta gamma', 'rebuilt');
		liveDocumentBindings.set({ tab: next.session });
		expect(row()).toMatchObject({ placement: 'placing', drawn: false });
		expect(next.asks()).toHaveLength(1);
		old.answer(inFlight, { ranges: [{ id: 'c1', from_off: 0, to_off: 5 }] });
		await settled();
		expect(row()).toMatchObject({ placement: 'placing', drawn: false });
		next.answer(next.asks()[0]);
		await vi.waitFor(() => expect(row()).toMatchObject({ placement: 'placed', drawn: true, from: 6, to: 10 }));
	});

	it('does not ask for an unchanged placed load, and keeps a changed row drawn where it moved while it is Placing', async () => {
		const doc = await opened();
		doc.answer(doc.asks()[0]);
		await vi.waitFor(() => expect(row().placement).toBe('placed'));
		doc.remote(text => text.insert(0, 'X '));
		await loadComments('artifact');
		expect(doc.asks()).toHaveLength(1);
		expect(row()).toMatchObject({ placement: 'placed', from: 8, to: 12 });
		mocks.fetchComments.mockResolvedValue({ comments: [serverRow('c1', 'beta', 8)], feedback: {} });
		await loadComments('artifact');
		expect(row()).toMatchObject({ placement: 'placing', drawn: true, from: 8, to: 12 });
		expect(doc.asks()).toHaveLength(2);
	});

	it('re-resolves orphaned placements once through the canonical request lifecycle', async () => {
		const doc = await opened();
		doc.answer(doc.asks()[0]);
		await vi.waitFor(() => expect(row().placement).toBe('placed'));

		expect(reloadPlacements('artifact')).toBe(true);
		expect(reloadPlacements('artifact')).toBe(false);
		expect(doc.asks()).toHaveLength(2);
		expect(row().placementReloading).toBe(true);
		doc.answer(doc.asks()[1]);
		await vi.waitFor(() => expect(row()).toMatchObject({ placement: 'placed', placementReloading: false }));
		expect(doc.asks()).toHaveLength(2);
	});

	it('settles a refused Reload without leaving its control busy', async () => {
		const doc = await opened();
		doc.answer(doc.asks()[0]);
		await vi.waitFor(() => expect(row().placement).toBe('placed'));
		expect(reloadPlacements('artifact')).toBe(true);
		const reload = doc.asks()[1];
		doc.frame({ type: 'doc_error', kind: 'refused', request_id: reload.request_id, message: 'positions refused' });
		await vi.waitFor(() => expect(row()).toMatchObject({ placement: 'not_placed', placementReloading: false, placementReloadError: true }));
	});

	it('keeps a never-drawn orphan eligible after a refused Reload and through a reconnect retry', async () => {
		const doc = await opened('alpha gamma', [{ ...serverRow('c1', 'beta', 6), orphaned: true }]);
		const projectedOrphans = () => commentsInDocumentOrder(rows(), doc.session.text.toString())
			.filter(comment => comment.orphaned).map(comment => comment.id);
		expect(doc.asks()).toHaveLength(0);
		expect(projectedOrphans()).toEqual(['c1']);

		expect(reloadPlacements('artifact')).toBe(true);
		const refused = doc.asks()[0];
		doc.frame({ type: 'doc_error', kind: 'refused', request_id: refused.request_id, message: 'positions refused' });
		await vi.waitFor(() => expect(row()).toMatchObject({ placement: undefined, placementReloading: false, placementReloadError: true }));
		expect(row().drawn).toBeFalsy();
		expect(projectedOrphans()).toEqual(['c1']);

		expect(reloadPlacements('artifact')).toBe(true);
		const interrupted = doc.asks()[1];
		doc.session.disconnected();
		doc.session.opened(Y.encodeStateAsUpdate(doc.server), Y.encodeStateVector(doc.server));
		doc.frame({ type: 'doc_opened', live: true });
		expect(doc.asks()).toHaveLength(3);
		const retried = doc.asks()[2];
		doc.answer(interrupted, { ranges: [{ id: 'c1', from_off: 6, to_off: 10 }] });
		await settled();
		expect(projectedOrphans()).toEqual(['c1']);
		doc.frame({ type: 'doc_error', kind: 'refused', request_id: retried.request_id, message: 'positions refused again' });
		await vi.waitFor(() => expect(row()).toMatchObject({ placement: undefined, placementReloading: false, placementReloadError: true }));
		expect(row().drawn).toBeFalsy();
		expect(projectedOrphans()).toEqual(['c1']);
	});

	it('leaves a row the matching answer does not name unplaced, with no place to draw', async () => {
		const doc = await opened();
		doc.answer(doc.asks()[0], { ranges: [] });
		await vi.waitFor(() => expect(row().placement).toBeUndefined());
		expect(row()).toMatchObject({ drawn: false, from: undefined, to: undefined });
	});
});

describe('placement classification by cleanliness (4.2 revision 13)', () => {
	it('keeps a row Placing after a deletion-only edit with equal vectors, places it on the re-ask once acked late, and reports nothing', async () => {
		const errors = vi.spyOn(console, 'error').mockImplementation(() => {});
		const doc = await opened();
		const [ask] = doc.asks();
		doc.session.text.delete(0, 6);
		const deletion = doc.updates().at(-1)!;
		expect(Y.encodeStateVector(doc.session.doc)).toEqual(Y.encodeStateVector(doc.server));
		doc.answer(ask);
		await settled();
		expect(row().placement).toBe('placing');
		expect(doc.asks()).toHaveLength(1);
		doc.merge(deletion);
		doc.ack(deletion);
		expect(doc.asks()).toHaveLength(2);
		doc.answer(doc.asks()[1]);
		await vi.waitFor(() => expect(row()).toMatchObject({ placement: 'placed', from: 0, to: 4 }));
		expect(errors).not.toHaveBeenCalled();
	});

	it('keeps a row Placing while a refused deletion batch is retried, then places it when the retry is acked', async () => {
		const errors = vi.spyOn(console, 'error').mockImplementation(() => {});
		const doc = await opened();
		doc.session.text.delete(0, 6);
		const deletion = doc.updates().at(-1)!;
		doc.frame({ type: 'doc_error', kind: 'store_refused', request_id: deletion.request_id, message: 'disk full', blocking: true });
		doc.answer(doc.asks()[0]);
		await settled();
		expect(row().placement).toBe('placing');
		doc.session.retryStore();
		expect(doc.sent.at(-1)).toEqual(deletion);
		expect(doc.asks()).toHaveLength(1);
		doc.merge(deletion);
		doc.ack(deletion);
		doc.answer(doc.asks()[1]);
		await vi.waitFor(() => expect(row()).toMatchObject({ placement: 'placed', from: 0, to: 4 }));
		expect(errors).not.toHaveBeenCalled();
	});

	it('keeps a row Placing for a local transaction made after the server queued its answer', async () => {
		const doc = await opened();
		const [ask] = doc.asks();
		const queued = { digest: wireDigest(doc.serverText()) };
		doc.session.text.insert(0, '> ');
		const edit = doc.updates().at(-1)!;
		doc.answer(ask, queued);
		await settled();
		expect(row().placement).toBe('placing');
		doc.merge(edit);
		doc.ack(edit);
		doc.answer(doc.asks()[1]);
		await vi.waitFor(() => expect(row()).toMatchObject({ placement: 'placed', from: 8, to: 12 }));
	});

	it('control: an insertion-only local edit stays Placing, then is placed once acked', async () => {
		const doc = await opened();
		doc.session.text.insert(16, '!');
		const edit = doc.updates().at(-1)!;
		doc.answer(doc.asks()[0]);
		await settled();
		expect(row().placement).toBe('placing');
		doc.merge(edit);
		doc.ack(edit);
		doc.answer(doc.asks()[1]);
		await vi.waitFor(() => expect(row().placement).toBe('placed'));
	});

	it('control: a mismatch while clean, with equal vectors and nothing pending, is Not placed with exactly one error report', async () => {
		const errors = vi.spyOn(console, 'error').mockImplementation(() => {});
		const doc = await opened();
		expect(doc.session.durability.clean).toBe(true);
		doc.answer(doc.asks()[0], { digest: wireDigest('text the browser does not have') });
		await vi.waitFor(() => expect(row().placement).toBe('not_placed'));
		expect(errors).toHaveBeenCalledTimes(1);
		expect(errors.mock.calls[0][1]).toMatchObject({
			server_digest: wireDigest('text the browser does not have'), browser_digest: wireDigest('alpha beta gamma'),
			server_vector: expect.any(String), browser_vector: expect.any(String),
		});
		doc.ack();
		await settled();
		expect(doc.asks()).toHaveLength(1); // it never asks again on its own
	});

	it('asks once more when the tab becomes clean while a row is Placing, and not on later acks', async () => {
		const doc = await opened();
		doc.session.text.insert(16, '!');
		const edit = doc.updates().at(-1)!;
		doc.answer(doc.asks()[0]);
		await settled();
		doc.ack();
		expect(doc.asks()).toHaveLength(1); // still not clean
		doc.merge(edit);
		doc.ack(edit);
		expect(doc.asks()).toHaveLength(2);
		doc.answer(doc.asks()[1]);
		await vi.waitFor(() => expect(row().placement).toBe('placed'));
		doc.ack();
		expect(doc.asks()).toHaveLength(2);
	});

	it('keeps Placing under sustained typing with acks delayed, and asks once when typing stops and the last batch is acked', async () => {
		const doc = await opened();
		const typed: Record<string, any>[] = [];
		doc.session.text.insert(16, '1'); typed.push(doc.updates().at(-1)!);
		doc.answer(doc.asks()[0]);
		await settled();
		for (const key of '234') {
			doc.session.text.insert(doc.session.text.length, key); typed.push(doc.updates().at(-1)!);
			doc.merge(typed.at(-2)!);
			doc.ack(typed.at(-2)!); // each ack names a message built before the latest keystroke
		}
		expect(row().placement).toBe('placing');
		expect(doc.asks()).toHaveLength(1);
		doc.merge(typed.at(-1)!);
		doc.ack(typed.at(-1)!);
		expect(doc.asks()).toHaveLength(2);
	});

	it.each([
		['a remote update', (doc: ReturnType<typeof live>) => doc.remote(text => text.insert(0, 'X '))],
		['a local edit', (doc: ReturnType<typeof live>) => { doc.session.text.insert(0, '> '); }],
		['an ack', (doc: ReturnType<typeof live>) => doc.ack()],
	])('discards an answer whose hash is overtaken by %s, and asks again', async (_name, overtake) => {
		const errors = vi.spyOn(console, 'error').mockImplementation(() => {});
		const doc = await opened();
		const digest = crypto.subtle.digest.bind(crypto.subtle);
		let release!: () => void;
		vi.spyOn(crypto.subtle, 'digest').mockImplementationOnce(async (algorithm, data) => {
			await new Promise<void>(resolve => { release = resolve; });
			return digest(algorithm, data);
		});
		doc.answer(doc.asks()[0], { digest: wireDigest('anything') });
		await vi.waitFor(() => expect(release).toBeTypeOf('function'));
		overtake(doc);
		release();
		await settled();
		expect(row()).toMatchObject({ placement: 'placing', drawn: false });
		expect(errors).not.toHaveBeenCalled();
		expect(doc.asks()).toHaveLength(2);
	});
});
