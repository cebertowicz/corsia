import { EditorSelection } from '@codemirror/state';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { A, B, hiddenOf, lane, trackRigs, type Rig } from './typed-reveal-rig';
import { endPreview, previewSlot } from './typed-reveal-preview';
import { settleTypedReveals } from './typed-reveal';

const { make, cleanup } = trackRigs();
beforeEach(() => { vi.useFakeTimers(); });
afterEach(() => { cleanup(); vi.useRealTimers(); document.body.innerHTML = ''; });

const DOC = 'Hello world. The rest stays.';
const WORLD = { from: 6, to: 11 };

function stream(r: Rig, text: string, options: { who?: typeof A; callId?: string; range?: { from: number; to: number } | null; slot?: number } = {}) {
	const who = options.who ?? A;
	const range = options.range === undefined ? WORLD : options.range;
	r.view.dispatch({ effects: previewSlot.of({
		callId: options.callId ?? 'call-1', threadId: who.threadId, name: who.name, slot: options.slot ?? 0,
		from: range?.from ?? null, to: range?.to ?? null, text,
	}) });
}

const previewText = (r: Rig) => [...r.view.dom.querySelectorAll('.cm-presence-preview')].map((node) => node.textContent).join('|');
const typedOf = (r: Rig, id = A.threadId) => lane(r, id)?.previews.reduce((sum, preview) => sum + preview.typed, 0) ?? 0;
const commit = (r: Rig, text: string, callId = 'call-1', who = A) =>
	r.edit({ ...who, callId }, (t) => { t.delete(WORLD.from, WORLD.to - WORLD.from); t.insert(WORLD.from, text); });

describe('a streamed edit previews in its agent typing lane', () => {
	it('selects the old text, then types the streamed text as it arrives, without touching the document', () => {
		const r = make(DOC);
		stream(r, 'there');
		expect(lane(r, A.threadId)?.select?.from).toBe(WORLD.from);
		vi.advanceTimersByTime(4_000);
		expect(lane(r, A.threadId)?.select).toBeNull();
		expect(typedOf(r)).toBe('there'.length);
		expect(previewText(r)).toBe('there');
		stream(r, 'there, friend');
		vi.advanceTimersByTime(4_000);
		expect(previewText(r)).toBe('there, friend');
		expect(r.view.state.doc.toString()).toBe(DOC);
		expect(r.text.toString()).toBe(DOC);
	});

	it('types at most what has arrived and keeps the lane while it waits for more', () => {
		const r = make(DOC);
		stream(r, 'th');
		vi.advanceTimersByTime(6 * 60_000);
		expect(lane(r, A.threadId)?.previews).toHaveLength(1);
		expect(typedOf(r)).toBe(2);
	});

	it('hands over on commit: nothing typed retypes, and the rest continues from where it was', () => {
		const r = make(DOC);
		stream(r, 'there, friend');
		vi.advanceTimersByTime(4_000);
		const typed = typedOf(r);
		expect(typed).toBeGreaterThan(0);
		commit(r, 'there, friend and more');
		expect(lane(r, A.threadId)?.previews).toHaveLength(0);
		expect(lane(r, A.threadId)?.select).toBeNull();
		expect(hiddenOf(r, A.threadId)).toBe('there, friend and more'.length - typed);
		expect(previewText(r)).toBe('');
		vi.advanceTimersByTime(13_000);
		expect(hiddenOf(r, A.threadId)).toBe(0);
		expect(r.view.state.doc.toString()).toBe('Hello there, friend and more. The rest stays.');
	});

	it('a fully typed preview commits with nothing left to type', () => {
		const r = make(DOC);
		stream(r, 'there');
		vi.advanceTimersByTime(8_000);
		commit(r, 'there');
		expect(hiddenOf(r, A.threadId)).toBe(0);
		expect(lane(r, A.threadId)?.previews ?? []).toHaveLength(0);
	});

	it('a commit that differs from its preview drops the preview and reveals the commit once', () => {
		const r = make(DOC);
		stream(r, 'there');
		vi.advanceTimersByTime(8_000);
		commit(r, 'goodbye');
		expect(lane(r, A.threadId)?.previews).toHaveLength(0);
		expect(hiddenOf(r, A.threadId)).toBe('goodbye'.length);
		vi.advanceTimersByTime(13_000);
		expect(hiddenOf(r, A.threadId)).toBe(0);
		expect(previewText(r)).toBe('');
	});

	it('an update from another call never takes over this preview', () => {
		const r = make(DOC);
		stream(r, 'there');
		r.edit({ ...A, callId: 'call-9' }, (t) => t.insert(t.length, ' tail'));
		expect(lane(r, A.threadId)?.previews).toHaveLength(1);
	});

	it('a call that ends without committing removes its preview and leaves the document as it was', () => {
		const r = make(DOC);
		stream(r, 'there');
		vi.advanceTimersByTime(4_000);
		r.view.dispatch({ effects: endPreview.of({ callId: 'call-1' }) });
		expect(lane(r, A.threadId)?.previews ?? []).toHaveLength(0);
		expect(previewText(r)).toBe('');
		expect(r.view.state.doc.toString()).toBe(DOC);
		expect(r.view.dom.textContent).toContain('world');
	});

	it('two agents streaming at once each keep their own lane', () => {
		const r = make(DOC);
		stream(r, 'there', { who: A, callId: 'call-a' });
		stream(r, 'Kept', { who: B, callId: 'call-b', range: { from: 17, to: 21 } });
		expect(lane(r, A.threadId)?.previews.map((p) => p.callId)).toEqual(['call-a']);
		expect(lane(r, B.threadId)?.previews.map((p) => p.callId)).toEqual(['call-b']);
		vi.advanceTimersByTime(8_000);
		expect(previewText(r)).toBe('there|Kept');
	});

	it('the user acting inside a preview drops it there', () => {
		const r = make(DOC);
		stream(r, 'there');
		vi.advanceTimersByTime(4_000);
		r.view.dispatch({ selection: EditorSelection.cursor(8), userEvent: 'select' });
		expect(lane(r, A.threadId)?.previews ?? []).toHaveLength(0);
	});

	it('no preview is drawn when typing is not allowed, and settling removes any shown', () => {
		const r = make(DOC);
		r.options.canType = false;
		stream(r, 'there');
		expect(r.lanes()).toHaveLength(0);
		r.options.canType = true;
		stream(r, 'there', { callId: 'call-2' });
		settleTypedReveals(r.view);
		expect(r.lanes().flatMap((l) => l.previews)).toHaveLength(0);
	});

	it('text for a slot that was never placed is not drawn', () => {
		const r = make(DOC);
		stream(r, 'there', { range: null });
		expect(r.lanes()).toHaveLength(0);
	});
});
