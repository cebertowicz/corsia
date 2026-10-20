import { get } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { ServerComment } from '$lib/api/docs';
import { commentMarks, setCommentRanges } from './cm/comment-marks';
import { suggestionMarks } from './cm/suggestion-marks';
import { startArrivalSelect } from './cm/typed-reveal-arrival';
import { previewSlot } from './cm/typed-reveal-preview';
import { A, trackRigs, type Rig } from './cm/typed-reveal-rig';
import { resetDocPresenceForTests, type DocActivityFrame } from './docPresence';
import { attachDocPresenceSurface, startDocPresence } from './docPresenceRuntime';
import { docComments, loadComments } from './stores/docComments';
import { prepareCommentArrival, revealCommentArrival } from './stores/docCommentArrivals';
import { selectSweepMs } from './typingPace';
import { attachPreviewFeed, CLAIM_GRACE_MS } from './stores/livePreviewFeed';
import * as Y from 'yjs';
import { LiveDocumentSession } from './stores/liveDocumentSession';
import { textDigest, type DocumentFrame } from './stores/documentWire';
import { emit, on } from './ws/events';

const wire = vi.hoisted(() => ({ rows: [] as unknown[] }));
vi.mock('$lib/api/docs', async (original) => ({
	...await original<typeof import('$lib/api/docs')>(),
	fetchComments: async () => ({
		comments: wire.rows,
		feedback: { open_count: 0, owner_label: null, grace_closes_at: null, undelivered_batches: [] },
	}),
}));

const { make, cleanup } = trackRigs();
const ART = 'art';
const DOC = 'Hello world. The rest stays.';
const INSERT = 'there, friend, and a good deal more besides';
let cleanups: Array<() => void> = [];

const serverSuggestion = (): ServerComment => ({
	id: 's1', artifact_id: ART, parent_id: null, author: 'agent-a', author_kind: 'agent', author_thread_id: 'agent-a', body: 'Say it kindly.',
	anchor_text: 'world', from_off: 6, to_off: 11, review: 0, resolved: 0, acked: 0, orphaned: false, replacement: INSERT,
	created_at: '2026-10-08T20:00:00Z', updated_at: '2026-10-08T20:00:00Z', replies: [],
} as ServerComment);

const earlierComment = (): ServerComment => ({ ...serverSuggestion(), id: 'c0', replacement: undefined, from_off: 0, to_off: 5, anchor_text: 'Hello' });

const activity = (callId: string | null, id = 's1', kind: 'suggestion' | 'comment' = 'suggestion'): DocActivityFrame => ({
	type: 'doc_activity', artifact_id: ART, thread_id: 'agent-a', agent: 'agent-a', kind,
	span: { from: 6, to: 11 }, comment_id: id, revision: null, at: new Date().toISOString(), call_id: callId,
} as DocActivityFrame);

const inserts = (r: Rig) => [...r.view.dom.querySelectorAll<HTMLElement>('.cm-suggestion-insert')].map((el) => el.getAttribute('data-insert') ?? '');

/** The document canvas, reduced to what this journey needs: the editor draws the comments store, and the page runs each arrival's select. */
function canvas(r: Rig) {
	cleanups.push(docComments.subscribe((all) => {
		r.view.dispatch({ effects: setCommentRanges.of((all[ART] ?? []).filter((row) => row.replacement).map((row) => ({
			from: row.from!, to: row.to!, id: row.id, suggestion: { quote: row.anchor, insert: row.replacement!, color: '#8b5cf6', inline: true },
		}))) });
	}));
	cleanups.push(attachDocPresenceSurface(ART, 'surface', {
		isViewing: () => true,
		reducedMotion: () => false,
		prepare: async (frame) => {
			const arrival = await prepareCommentArrival(ART, frame.comment_id);
			return { chars: 10, payload: arrival, delayMs: 8_000 };
		},
		select: (frame, prepared) => {
			const comment = (prepared.payload as Awaited<ReturnType<typeof prepareCommentArrival>>).comment;
			if (!comment || frame.kind !== 'suggestion') return null;
			return startArrivalSelect(r.view, {
				key: comment.id, threadId: frame.thread_id, name: A.name, color: '#8b5cf6', from: comment.from!, to: comment.to!,
				sweepMs: selectSweepMs(5), holdMs: 1_000, callId: frame.call_id ?? null, insert: comment.replacement ?? null,
			});
		},
		reveal: (_frame, payload) => revealCommentArrival(ART, payload as Awaited<ReturnType<typeof prepareCommentArrival>>),
	}));
}

/** What the viewer draws of the insert every 50 ms from the first moment the agent's comment is named, for `ms`. */
async function watch(r: Rig, ms: number): Promise<number[]> {
	const lengths: number[] = [];
	for (let t = 0; t < ms; t += 50) {
		await vi.advanceTimersByTimeAsync(50);
		lengths.push((inserts(r)[0] ?? '').length);
	}
	return lengths;
}

beforeEach(() => {
	vi.useFakeTimers();
	resetDocPresenceForTests();
	docComments.set({});
	wire.rows = [serverSuggestion()];
});

afterEach(() => {
	for (const stop of cleanups.reverse()) stop();
	cleanups = [];
	cleanup();
	vi.useRealTimers();
	document.body.innerHTML = '';
	resetDocPresenceForTests();
});

describe('an agent suggestion that commits before its arrival is typed in, never drawn whole first', () => {
	it('nothing streamed, and the agent\'s lane busy with an earlier arrival: the insert waits its turn, then only types in', async () => {
		wire.rows = [earlierComment(), serverSuggestion()];
		const r = make(DOC, [commentMarks, suggestionMarks(() => ({ focus: () => {}, hover: () => {} }))]);
		cleanups.push(startDocPresence());
		cleanups.push(on('doc_comment_changed', () => { void loadComments(ART); }));
		canvas(r);
		emit('doc_comment_changed', { type: 'doc_comment_changed', artifact_id: ART, comment_id: 'c0', author_thread_id: 'agent-a' });
		emit('doc_comment_changed', { type: 'doc_comment_changed', artifact_id: ART, comment_id: 's1', author_thread_id: 'agent-a' });
		emit('doc_activity', activity(null, 'c0', 'comment'));
		await vi.advanceTimersByTimeAsync(500);
		emit('doc_activity', activity(null));
		// Past the five-second wait for an activity frame, which the lane's turn must outlast.
		const waiting = await watch(r, 6_000);
		expect(Math.max(...waiting)).toBe(0);
		const typing = await watch(r, 20_000);
		expect(typing.some((n) => n > 0 && n < INSERT.length)).toBe(true);
		expect(typing.every((n, i) => i === 0 || n >= typing[i - 1])).toBe(true);
		expect(inserts(r)).toEqual([INSERT]);
	});

	it('nothing streamed: the comments load first, the activity frame follows, and the insert only ever types in', async () => {
		const r = make(DOC, [commentMarks, suggestionMarks(() => ({ focus: () => {}, hover: () => {} }))]);
		cleanups.push(startDocPresence());
		cleanups.push(on('doc_comment_changed', () => { void loadComments(ART); }));
		canvas(r);
		emit('doc_comment_changed', { type: 'doc_comment_changed', artifact_id: ART, comment_id: 's1', author_thread_id: 'agent-a' });
		const before = await watch(r, 1_000);
		emit('doc_activity', activity(null));
		const lengths = [...before, ...await watch(r, 15_000)];
		expect(Math.max(...before)).toBe(0);
		expect(lengths.every((n) => n < INSERT.length || n === lengths.at(-1))).toBe(true);
		expect(lengths.some((n) => n > 0 && n < INSERT.length)).toBe(true);
		expect(inserts(r)).toEqual([INSERT]);
	});

	it('streamed first: the committed suggestion takes over the typing with no pause and no restart', async () => {
		const r = make(DOC, [commentMarks, suggestionMarks(() => ({ focus: () => {}, hover: () => {} }))]);
		cleanups.push(startDocPresence());
		cleanups.push(on('doc_comment_changed', () => { void loadComments(ART); }));
		canvas(r);
		r.view.dispatch({ effects: previewSlot.of({
			callId: 'call-1', threadId: 'agent-a', name: A.name, slot: 0, from: 6, to: 11, text: INSERT.slice(0, 18), mode: 'suggest',
		}) });
		const streaming = await watch(r, 3_000);
		emit('doc_comment_changed', { type: 'doc_comment_changed', artifact_id: ART, comment_id: 's1', author_thread_id: 'agent-a' });
		emit('doc_activity', activity('call-1'));
		const lengths = [...streaming, ...await watch(r, 15_000)];
		expect(streaming.at(-1)).toBeGreaterThan(0);
		// Typing never goes backwards: no restart from nothing, and nothing jumps to the whole insert.
		expect(lengths.every((n, i) => i === 0 || n >= lengths[i - 1])).toBe(true);
		expect(lengths.every((n, i) => i === 0 || n - lengths[i - 1] < INSERT.length - 1)).toBe(true);
		expect(inserts(r)).toEqual([INSERT]);
		expect(r.lanes().flatMap((l) => l.previews)).toHaveLength(0);
		expect(get(docComments)[ART]?.map((row) => row.id)).toEqual(['s1']);
	});

	it('streamed, then held behind a busy lane past the claim grace: the preview is kept, and typing continues from it', async () => {
		wire.rows = [earlierComment()];
		const r = make(DOC, [commentMarks, suggestionMarks(() => ({ focus: () => {}, hover: () => {} }))]);
		// The real session is the feed's source, in a document that is itself held (its own `held` field is set).
		const session = new LiveDocumentSession({ artifactId: ART, storeId: 'store', doc: new Y.Doc(), writable: true, send: () => true });
		const send = (frame: Record<string, unknown>) => session.handle({ artifact_id: ART, store_id: 'store', ...frame } as unknown as DocumentFrame);
		send({ type: 'doc_saved', held: { corsia_text: 'a', file_text: 'b', candidate_text: 'c', choice_pending: true } });
		expect(session.held).not.toBeNull();
		cleanups.push(attachPreviewFeed(r.view, session));
		cleanups.push(startDocPresence());
		cleanups.push(on('doc_comment_changed', () => { void loadComments(ART); }));
		canvas(r);
		const digest = await textDigest(DOC);
		emit('doc_comment_changed', { type: 'doc_comment_changed', artifact_id: ART, comment_id: 'c0', author_thread_id: 'agent-a' });
		emit('doc_activity', activity(null, 'c0', 'comment'));
		send({ type: 'doc_preview', call_id: 'call-1', thread_id: 'agent-a', name: A.name, kind: 'suggest', digest,
			slots: [{ slot: 0, from: 6, to: 11, at: 0, text: INSERT.slice(0, 18) }] });
		send({ type: 'doc_preview_end', call_id: 'call-1', thread_id: 'agent-a', outcome: 'settled' });
		const streaming = await watch(r, 2_000);
		expect(streaming.at(-1)).toBeGreaterThan(0);
		// The suggestion commits and is held while the lane is still on the earlier comment.
		wire.rows = [earlierComment(), serverSuggestion()];
		emit('doc_comment_changed', { type: 'doc_comment_changed', artifact_id: ART, comment_id: 's1', author_thread_id: 'agent-a' });
		emit('doc_activity', activity('call-1'));
		const waiting = await watch(r, CLAIM_GRACE_MS + 3_000);
		const typing = await watch(r, 20_000);
		const lengths = [...streaming, ...waiting, ...typing];
		// Typing never goes backwards: the held preview is not lost when its grace runs out.
		expect(lengths.every((n, i) => i === 0 || n >= lengths[i - 1])).toBe(true);
		expect(inserts(r)).toEqual([INSERT]);
		expect(r.lanes().flatMap((l) => l.previews)).toHaveLength(0);
	});
});
