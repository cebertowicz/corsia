import { createHash } from 'node:crypto';
import { get } from 'svelte/store';
import { afterEach, describe, expect, it, vi } from 'vitest';
import * as Y from 'yjs';

const mocks = vi.hoisted(() => ({ fetchComments: vi.fn() }));
vi.mock('$lib/api/docs', () => ({
	fetchComments: mocks.fetchComments, postComment: vi.fn(), patchComment: vi.fn(), persistPositions: vi.fn(async () => {}), markCommentThreadViewed: vi.fn(),
}));

import { docComments, commentLoads, loadComments } from './docComments';
import { liveDocumentBindings } from './liveDocuments';
import { LiveDocumentSession } from './liveDocumentSession';
import { decodeUpdate, encodeUpdate } from './documentWire';
import { commentSurfaceStatus, commentsInDocumentOrder } from '$lib/docCanvasComments';

const wireDigest = (text: string) => createHash('sha256').update(text, 'utf8').digest('hex').slice(0, 16);
const settled = async () => { for (let i = 0; i < 5; i++) await new Promise(resolve => setTimeout(resolve, 0)); };
const serverRow = (id: string, anchor: string, from: number) => ({ id, from_off: from, to_off: from + anchor.length, anchor_text: anchor, author: 'agent',
	author_kind: 'agent', body: id, created_at: '2026-10-03T00:00:00Z', resolved: false, orphaned: false, review: false, replies: [] });

/** A live session on a controllable socket, beside the server document it talks to. */
function live(text: string) {
	const server = new Y.Doc();
	server.getText('text').insert(0, text);
	const sent: Record<string, any>[] = [];
	const session = new LiveDocumentSession({ artifactId: 'artifact', storeId: 'store', doc: new Y.Doc(), writable: true, send: (frame) => { sent.push(frame); return true; } });
	session.opened(Y.encodeStateAsUpdate(server), Y.encodeStateVector(server));
	const frame = (fields: Record<string, any>) => session.handle({ artifact_id: 'artifact', store_id: 'store', ...fields } as any);
	const ack = (...messages: Record<string, any>[]) => frame({ type: 'doc_ack', message_ids: messages.map(m => m.message_id) });
	ack(sent.at(-1)!);
	const serverText = () => server.getText('text').toString();
	return {
		session, sent, frame, ack, serverText,
		asks: () => sent.filter(f => f.type === 'doc_positions' && !f.probe),
		probes: () => sent.filter(f => f.type === 'doc_positions' && f.probe),
		updates: () => sent.filter(f => f.type === 'doc_update'),
		remote: (edit: (text: Y.Text) => void) => {
			const vector = Y.encodeStateVector(server);
			edit(server.getText('text'));
			frame({ type: 'doc_update', update: encodeUpdate(Y.encodeStateAsUpdate(server, vector)) });
		},
		merge: (message: Record<string, any>) => Y.applyUpdate(server, decodeUpdate(message.update)),
		/** The server answers a request, describing its own text now. */
		answer: (request: Record<string, any>, ranges: { id: string; from_off: number; to_off: number }[]) => frame({ type: 'doc_positions', seq: request.seq,
			request_id: request.request_id, digest: wireDigest(serverText()), vector: encodeUpdate(Y.encodeStateVector(server)), ranges }),
	};
}

const row = (id = 'c1') => (get(docComments).artifact ?? []).find(comment => comment.id === id)!;
const liveText = () => [...Object.values(get(liveDocumentBindings))][0].text.toString();
const status = () => commentSurfaceStatus(commentsInDocumentOrder(get(docComments).artifact ?? [], liveText()));
const named = (...ids: string[]) => ids.map(id => ({ id, from_off: 0, to_off: 1 }));

/** 'alpha beta gamma' with c1 on 'beta', placed; then the server's text loses 'beta ', which detaches c1. */
async function detached() {
	const doc = live('alpha beta gamma');
	liveDocumentBindings.set({ tab: doc.session });
	mocks.fetchComments.mockResolvedValue({ comments: [serverRow('c1', 'beta', 6)], feedback: {} });
	await loadComments('artifact');
	doc.answer(doc.asks()[0], [{ id: 'c1', from_off: 6, to_off: 10 }]);
	await vi.waitFor(() => expect(row().placement).toBe('placed'));
	expect(doc.probes()).toHaveLength(0);
	doc.remote(text => text.delete(6, 5));
	return doc;
}

afterEach(() => {
	liveDocumentBindings.set({});
	docComments.set({});
	commentLoads.set({});
	mocks.fetchComments.mockReset();
	vi.restoreAllMocks();
});

describe('which unplaced comments Place again could place', () => {
	it('probes once the text has settled and a comment is unplaced, and keeps the verdict on the row', async () => {
		const doc = await detached();
		expect(doc.probes()).toEqual([expect.objectContaining({ type: 'doc_positions', reload: true, probe: true })]);
		expect(status()).toMatchObject({ orphanedCount: 1, replaceableCount: 1 }); // before the answer, as the footer was
		doc.answer(doc.probes()[0], []);
		await vi.waitFor(() => expect(row().replaceable).toBe(false));
		expect(status()).toMatchObject({ orphanedCount: 1, replaceableCount: 0 });
		doc.remote(text => text.insert(6, 'beta ')); // the text changed again, so it is asked again, and this time the quote is found
		await vi.waitFor(() => expect(doc.probes()).toHaveLength(2));
		doc.answer(doc.probes()[1], named('c1'));
		await vi.waitFor(() => expect(row().replaceable).toBe(true));
		expect(status()).toMatchObject({ replaceableCount: 1 });
	});

	it('asks nothing while every comment is placed', async () => {
		const doc = live('alpha beta gamma');
		liveDocumentBindings.set({ tab: doc.session });
		mocks.fetchComments.mockResolvedValue({ comments: [serverRow('c1', 'beta', 6)], feedback: {} });
		await loadComments('artifact');
		doc.remote(text => text.insert(0, 'X '));
		await settled();
		expect(doc.probes()).toHaveLength(0);
	});

	it('does not ask while the tab holds unsaved edits, and asks once the last one is acked', async () => {
		const doc = await detached();
		doc.answer(doc.probes()[0], []);
		doc.session.doc.transact(() => doc.session.text.insert(0, 'X '));
		const edit = doc.updates().at(-1)!;
		doc.remote(text => text.insert(0, 'Y '));
		await settled();
		expect(doc.probes()).toHaveLength(1);
		doc.merge(edit);
		doc.ack(edit);
		await vi.waitFor(() => expect(doc.probes()).toHaveLength(2));
	});

	it('keeps one probe in flight, and drops an answer for text that has since changed, asking again', async () => {
		const doc = await detached();
		doc.remote(text => text.insert(0, 'X '));
		doc.remote(text => text.insert(0, 'Y '));
		expect(doc.probes()).toHaveLength(1);
		doc.answer(doc.probes()[0], []);
		await vi.waitFor(() => expect(doc.probes()).toHaveLength(2));
		expect(row().replaceable).toBeUndefined();
		doc.answer(doc.probes()[1], []);
		await vi.waitFor(() => expect(row().replaceable).toBe(false));
		expect(doc.probes()).toHaveLength(2);
	});

	it('leaves the footer as it was when a probe fails, and forgets an earlier verdict', async () => {
		const doc = await detached();
		doc.answer(doc.probes()[0], []);
		await vi.waitFor(() => expect(row().replaceable).toBe(false));
		doc.remote(text => text.insert(0, 'X '));
		await vi.waitFor(() => expect(doc.probes()).toHaveLength(2));
		doc.frame({ type: 'doc_error', kind: 'refused', request_id: doc.probes()[1].request_id, message: 'refused' });
		await vi.waitFor(() => expect(row().replaceable).toBeUndefined());
		expect(status()).toMatchObject({ orphanedCount: 1, replaceableCount: 1 });
		await settled();
		expect(doc.probes()).toHaveLength(2);
		expect(doc.asks()).toHaveLength(1);
	});

	it('keeps a verdict through a comments load', async () => {
		const doc = await detached();
		doc.answer(doc.probes()[0], []);
		await vi.waitFor(() => expect(row().replaceable).toBe(false));
		await loadComments('artifact');
		expect(row().replaceable).toBe(false);
	});
});
