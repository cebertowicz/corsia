import { afterEach, describe, expect, it, vi } from 'vitest';
import { EditorState } from '@codemirror/state';
import { EditorView } from '@codemirror/view';
import { createTypedReveal, typedReveals } from '$lib/cm/typed-reveal';
import { attachPreviewFeed, CLAIM_GRACE_MS } from './livePreviewFeed';
import { textDigest, type DocumentFrame } from './documentWire';

const DOC = 'Hello world. The rest stays.';

// A live view keeps its typing driver ticking after the test, and a tick after the file's
// environment is gone throws on the missing document; every view is destroyed after its test.
const disposers = new Set<() => void>();

function harness(doc = DOC, holdsCall: (callId: string) => boolean = () => false) {
	const view = new EditorView({
		state: EditorState.create({ doc, extensions: [createTypedReveal({ canType: () => true, isInView: () => true })] }),
		parent: document.body,
	});
	let listener: ((frame: DocumentFrame) => void) | null = null;
	const stop = attachPreviewFeed(view, { onFrame: (next) => { listener = next; return () => { listener = null; }; }, holdsCall });
	const dispose = () => { if (!disposers.delete(dispose)) return; stop(); view.destroy(); };
	disposers.add(dispose);
	return { view, send: (frame: Record<string, unknown>) => listener?.(frame as unknown as DocumentFrame), stop, dispose, listening: () => listener !== null };
}

const previews = (view: EditorView) => view.state.field(typedReveals).flatMap((lane) => lane.previews.map((p) => ({ ...p, threadId: lane.threadId })));
const settle = async () => { for (let i = 0; i < 5; i++) await new Promise((resolve) => setTimeout(resolve, 0)); };
const preview = (digest: string, slots: unknown[], callId = 'call-1', kind?: string) =>
	({ type: 'doc_preview', call_id: callId, thread_id: 'thread-a', name: 'Alpha', digest, slots, ...(kind ? { kind } : {}) });
const end = (outcome: string) => ({ type: 'doc_preview_end', call_id: 'call-1', thread_id: 'thread-a', outcome });

afterEach(() => { vi.useRealTimers(); [...disposers].forEach((dispose) => dispose()); document.body.innerHTML = ''; });

describe('live preview feed', () => {
	it('places a slot when the frame was computed on the text this editor shows', async () => {
		const h = harness();
		h.send(preview(await textDigest(DOC), [{ slot: 0, from: 6, to: 11, at: 0, text: 'the' }]));
		h.send(preview(await textDigest(DOC), [{ slot: 0, from: 6, to: 11, at: 3, text: 're' }]));
		await settle();
		expect(previews(h.view)).toMatchObject([{ callId: 'call-1', threadId: 'thread-a', from: 6, to: 11, text: 'there' }]);
	});

	it('holds a slot back while the texts differ, then places it with everything received so far', async () => {
		const h = harness();
		h.send(preview('0000000000000000', [{ slot: 0, from: 6, to: 11, at: 0, text: 'the' }]));
		await settle();
		expect(previews(h.view)).toEqual([]);
		h.send(preview(await textDigest(DOC), [{ slot: 0, from: 6, to: 11, at: 3, text: 're' }]));
		await settle();
		expect(previews(h.view)).toMatchObject([{ text: 'there', from: 6, to: 11 }]);
	});

	it('counts UTF-16 units across pieces, so an emoji does not break the stream', async () => {
		const h = harness();
		const digest = await textDigest(DOC);
		h.send(preview(digest, [{ slot: 0, from: 6, to: 11, at: 0, text: '\u{1F389}' }]));
		h.send(preview(digest, [{ slot: 0, from: 6, to: 11, at: 2, text: '!' }]));
		await settle();
		expect(previews(h.view)).toMatchObject([{ text: '\u{1F389}!' }]);
	});

	it('drops a call whose pieces arrive out of step instead of drawing a gap', async () => {
		const h = harness();
		const digest = await textDigest(DOC);
		h.send(preview(digest, [{ slot: 0, from: 6, to: 11, at: 0, text: 'the' }]));
		h.send(preview(digest, [{ slot: 0, from: 6, to: 11, at: 9, text: 'x' }]));
		h.send(preview(digest, [{ slot: 0, from: 6, to: 11, at: 3, text: 're' }]));
		await settle();
		expect(previews(h.view)).toEqual([]);
	});

	it('an end removes the preview at once and later frames for that call are ignored', async () => {
		const h = harness();
		const digest = await textDigest(DOC);
		h.send(preview(digest, [{ slot: 0, from: 6, to: 11, at: 0, text: 'the' }]));
		await settle();
		h.send(preview(digest, [{ slot: 0, from: 6, to: 11, at: 3, text: 're' }]));
		h.send({ type: 'doc_preview_end', call_id: 'call-1', thread_id: 'thread-a', outcome: 'failed' });
		expect(previews(h.view)).toEqual([]);
		h.send(preview(digest, [{ slot: 0, from: 6, to: 11, at: 5, text: '!' }]));
		await settle();
		expect(previews(h.view)).toEqual([]);
	});

	it('passes each frame\'s kind to the lane, an edit when the frame names none', async () => {
		const h = harness();
		const digest = await textDigest(DOC);
		h.send(preview(digest, [{ slot: 0, from: 6, to: 11, at: 0, text: '' }], 'call-c', 'comment'));
		h.send(preview(digest, [{ slot: 0, from: 13, to: 16, at: 0, text: 'A' }], 'call-e'));
		await settle();
		expect(previews(h.view).map((p) => [p.callId, p.mode])).toEqual([['call-c', 'comment'], ['call-e', 'edit']]);
	});

	it('ignores a frame of a kind it does not know', async () => {
		const h = harness();
		h.send(preview(await textDigest(DOC), [{ slot: 0, from: 6, to: 11, at: 0, text: 'x' }], 'call-1', 'review'));
		await settle();
		expect(previews(h.view)).toEqual([]);
	});

	it('a call that turns from a comment into a suggestion is drawn again as a suggestion', async () => {
		const h = harness();
		const digest = await textDigest(DOC);
		h.send(preview(digest, [{ slot: 0, from: 6, to: 11, at: 0, text: '' }], 'call-1', 'comment'));
		h.send(preview(digest, [{ slot: 0, from: 6, to: 11, at: 0, text: 'the' }], 'call-1', 'suggest'));
		await settle();
		expect(previews(h.view)).toMatchObject([{ mode: 'suggest', text: 'the' }]);
	});

	it('a settled comment or suggestion stays briefly for its arrival to take it over; any other end goes at once', async () => {
		for (const [kind, outcome, kept] of [['comment', 'settled', true], ['suggest', 'settled', true], ['comment', 'failed', false], ['edit', 'settled', false]] as const) {
			vi.useRealTimers();
			const h = harness();
			h.send(preview(await textDigest(DOC), [{ slot: 0, from: 6, to: 11, at: 0, text: kind === 'comment' ? '' : 'the' }], 'call-1', kind));
			await settle();
			vi.useFakeTimers();
			h.send(end(outcome));
			expect(previews(h.view).length, `${kind} ${outcome}`).toBe(kept ? 1 : 0);
			vi.advanceTimersByTime(CLAIM_GRACE_MS);
			expect(previews(h.view), `${kind} ${outcome}`).toEqual([]);
			h.dispose();
		}
	});

	it('a suggestion held for its arrival keeps its preview past the grace, and loses it one grace after the hold ends', async () => {
		vi.useRealTimers();
		let holding = true;
		const h = harness(DOC, () => holding);
		h.send(preview(await textDigest(DOC), [{ slot: 0, from: 6, to: 11, at: 0, text: 'the' }], 'call-1', 'suggest'));
		await settle();
		vi.useFakeTimers();
		h.send(end('settled'));
		vi.advanceTimersByTime(CLAIM_GRACE_MS * 3);
		expect(previews(h.view)).toHaveLength(1);
		holding = false;
		vi.advanceTimersByTime(CLAIM_GRACE_MS);
		expect(previews(h.view)).toEqual([]);
		h.dispose();
	});

	it('a suggestion whole in one frame and settled at once is still placed, and held for its arrival', async () => {
		const h = harness();
		const digest = await textDigest(DOC);
		h.send(preview(digest, [{ slot: 0, from: 6, to: 11, at: 0, text: 'the whole insert' }], 'call-1', 'suggest'));
		h.send(end('settled'));
		await settle();
		expect(previews(h.view)).toMatchObject([{ mode: 'suggest', text: 'the whole insert', typed: 0 }]);
	});

	it('a frame still waiting to be placed when its call ends any other way is never placed', async () => {
		const h = harness();
		h.send(preview(await textDigest(DOC), [{ slot: 0, from: 6, to: 11, at: 0, text: 'the' }], 'call-1', 'suggest'));
		h.send(end('refused'));
		await settle();
		expect(previews(h.view)).toEqual([]);
	});

	it('stops listening when detached', () => {
		const h = harness();
		h.stop();
		expect(h.listening()).toBe(false);
	});
});
