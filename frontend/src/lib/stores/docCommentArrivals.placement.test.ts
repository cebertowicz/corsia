import { createHash } from 'node:crypto';
import { get } from 'svelte/store';
import { afterEach, describe, expect, it, vi } from 'vitest';
import * as Y from 'yjs';

const mocks = vi.hoisted(() => ({ fetchComments: vi.fn() }));
vi.mock('$lib/api/docs', () => ({
	fetchComments: mocks.fetchComments, postComment: vi.fn(), patchComment: vi.fn(),
	persistPositions: vi.fn(async () => {}), markCommentThreadViewed: vi.fn(),
}));

import { holdCommentArrival, prepareCommentArrival, releaseCommentArrival, releaseCommentArrivals, revealCommentArrival } from './docCommentArrivals';
import { commentLoads, docComments, loadComments } from './docComments';
import { encodeUpdate } from './documentWire';
import { liveDocumentBindings } from './liveDocuments';
import { LiveDocumentSession } from './liveDocumentSession';

const TEXT = 'alpha beta gamma delta';
const wireDigest = (text: string) => createHash('sha256').update(text, 'utf8').digest('hex').slice(0, 16);
const serverRow = (id: string, anchor: string) => ({
	id, from_off: TEXT.indexOf(anchor), to_off: TEXT.indexOf(anchor) + anchor.length, anchor_text: anchor, author: 'agent',
	author_kind: 'agent', body: id, created_at: '2026-10-03T00:00:00Z', resolved: false, orphaned: false, review: false, replies: [],
});
const ROWS = [serverRow('c0', 'alpha'), serverRow('c1', 'gamma')];

/** A live session on a controllable socket, so an ask for positions is observable. */
function live() {
	const server = new Y.Doc();
	server.getText('text').insert(0, TEXT);
	const sent: Record<string, any>[] = [];
	const session = new LiveDocumentSession({ artifactId: 'artifact', storeId: 'store', doc: new Y.Doc(), writable: true,
		send: (frame) => { sent.push(frame); return true; } });
	session.opened(Y.encodeStateAsUpdate(server), Y.encodeStateVector(server));
	const frame = (fields: Record<string, any>) => session.handle({ artifact_id: 'artifact', store_id: 'store', ...fields } as any);
	frame({ type: 'doc_ack', message_ids: [sent.at(-1)!.message_id] });
	return {
		session,
		asks: () => sent.filter((f) => f.type === 'doc_positions'),
		answer: (ask: Record<string, any>, ids: string[]) => frame({
			type: 'doc_positions', seq: ask.seq, request_id: ask.request_id, digest: wireDigest(TEXT), text_length_utf16: TEXT.length,
			vector: encodeUpdate(Y.encodeStateVector(server)),
			ranges: ids.map((id) => {
				const row = ROWS.find((r) => r.id === id)!;
				return { id, from_off: row.from_off, to_off: row.to_off };
			}),
		}),
	};
}

const settled = async () => { for (let i = 0; i < 5; i++) await new Promise((resolve) => setTimeout(resolve, 0)); };
const rows = () => get(docComments).artifact ?? [];
const row = (id: string) => rows().find((comment) => comment.id === id);

/** Both comments loaded and placed on a live document. */
async function opened() {
	const doc = live();
	liveDocumentBindings.set({ tab: doc.session });
	mocks.fetchComments.mockResolvedValue({ comments: ROWS, feedback: {} });
	await loadComments('artifact');
	doc.answer(doc.asks().at(-1)!, ['c0', 'c1']);
	await settled();
	expect(rows().map((comment) => comment.placement)).toEqual(['placed', 'placed']);
	return doc;
}

afterEach(() => {
	releaseCommentArrivals('artifact');
	liveDocumentBindings.set({});
	docComments.set({});
	commentLoads.set({});
	mocks.fetchComments.mockReset();
});

describe('a held comment is placed by the placement owner when it becomes visible', () => {
	it('holding a shown comment hides it without asking again for the others', async () => {
		const doc = await opened();
		const asks = doc.asks().length;
		holdCommentArrival('artifact', 'c1');
		expect(rows().map((comment) => comment.id)).toEqual(['c0']);
		expect(row('c0')?.placement).toBe('placed');
		expect(doc.asks()).toHaveLength(asks);
	});

	it('a reveal places the comment and asks for its position', async () => {
		const doc = await opened();
		holdCommentArrival('artifact', 'c1');
		const asks = doc.asks().length;
		const arrival = await prepareCommentArrival('artifact', 'c1');
		revealCommentArrival('artifact', arrival);
		expect(row('c1')?.placement).toBe('placing');
		expect(doc.asks()).toHaveLength(asks + 1);
		doc.answer(doc.asks().at(-1)!, ['c0', 'c1']);
		await settled();
		expect(row('c1')?.placement).toBe('placed');
	});

	it('a missing-frame release places the comment and asks for its position', async () => {
		const doc = await opened();
		holdCommentArrival('artifact', 'c1');
		await loadComments('artifact');
		expect(rows().map((comment) => comment.id)).toEqual(['c0']);
		const asks = doc.asks().length;
		releaseCommentArrival('artifact', 'c1');
		expect(row('c1')?.placement).toBe('placing');
		expect(doc.asks()).toHaveLength(asks + 1);
	});

	it('releasing every hold places each released comment', async () => {
		const doc = await opened();
		holdCommentArrival('artifact', 'c0');
		holdCommentArrival('artifact', 'c1');
		await loadComments('artifact');
		expect(rows()).toEqual([]);
		const asks = doc.asks().length;
		releaseCommentArrivals('artifact');
		expect(rows().map((comment) => comment.placement)).toEqual(['placing', 'placing']);
		expect(doc.asks().length).toBeGreaterThan(asks);
	});

	it('a comment never loaded is loaded, and placed, when its hold is released', async () => {
		const doc = live();
		liveDocumentBindings.set({ tab: doc.session });
		mocks.fetchComments.mockResolvedValue({ comments: ROWS, feedback: {} });
		holdCommentArrival('artifact', 'c1');
		releaseCommentArrival('artifact', 'c1');
		await vi.waitFor(() => expect(rows().map((comment) => comment.id).sort()).toEqual(['c0', 'c1']));
		expect(doc.asks().length).toBeGreaterThan(0);
	});
});
