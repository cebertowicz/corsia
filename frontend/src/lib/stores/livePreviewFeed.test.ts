import { afterEach, describe, expect, it } from 'vitest';
import { EditorState } from '@codemirror/state';
import { EditorView } from '@codemirror/view';
import { createTypedReveal, typedReveals } from '$lib/cm/typed-reveal';
import { attachPreviewFeed } from './livePreviewFeed';
import { textDigest, type DocumentFrame } from './documentWire';

const DOC = 'Hello world. The rest stays.';

function harness(doc = DOC) {
	const view = new EditorView({
		state: EditorState.create({ doc, extensions: [createTypedReveal({ canType: () => true, isInView: () => true })] }),
		parent: document.body,
	});
	let listener: ((frame: DocumentFrame) => void) | null = null;
	const stop = attachPreviewFeed(view, { onFrame: (next) => { listener = next; return () => { listener = null; }; } });
	return { view, send: (frame: Record<string, unknown>) => listener?.(frame as unknown as DocumentFrame), stop, listening: () => listener !== null };
}

const previews = (view: EditorView) => view.state.field(typedReveals).flatMap((lane) => lane.previews.map((p) => ({ ...p, threadId: lane.threadId })));
const settle = async () => { for (let i = 0; i < 5; i++) await new Promise((resolve) => setTimeout(resolve, 0)); };
const preview = (digest: string, slots: unknown[], callId = 'call-1') =>
	({ type: 'doc_preview', call_id: callId, thread_id: 'thread-a', name: 'Alpha', digest, slots });

afterEach(() => { document.body.innerHTML = ''; });

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

	it('stops listening when detached', () => {
		const h = harness();
		h.stop();
		expect(h.listening()).toBe(false);
	});
});
