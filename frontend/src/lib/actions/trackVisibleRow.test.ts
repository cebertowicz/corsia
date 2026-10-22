import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { cancelComposing } from '$lib/stores/replyContextCapture';

type Callback = (entries: Array<{ target: Element; isIntersecting: boolean }>) => void;

let callback: Callback;
const observed = new Set<Element>();

class FakeObserver {
	constructor(cb: Callback) {
		callback = cb;
	}
	observe(node: Element) { observed.add(node); }
	unobserve(node: Element) { observed.delete(node); }
}

function row() {
	return document.createElement('div');
}

function show(...rows: Array<[Element, boolean]>) {
	callback(rows.map(([target, isIntersecting]) => ({ target, isIntersecting })));
}

async function load() {
	vi.resetModules();
	return (await import('./trackVisibleRow')).trackVisibleRow;
}

beforeEach(() => {
	observed.clear();
	vi.stubGlobal('IntersectionObserver', FakeObserver);
});

afterEach(() => {
	vi.unstubAllGlobals();
	cancelComposing();
});

describe('trackVisibleRow', () => {
	it('reports the seqs of the rows on screen to the composing window', async () => {
		const track = await load();
		const capture = await import('$lib/stores/replyContextCapture');
		const a = row();
		const b = row();
		track(a, { seq: 10, threadId: 't1' });
		track(b, { seq: 11, threadId: 't1' });
		capture.beginComposing('t1');
		show([a, true], [b, true]);
		show([a, false]);
		expect(capture.takeReplyContext('t1')).toEqual({ in_view: [[10, 11, 2]] });
	});

	it('keeps another thread\'s visible rows out of the composing thread\'s window', async () => {
		const track = await load();
		const capture = await import('$lib/stores/replyContextCapture');
		const a = row();
		const b = row();
		const unbound = row();
		track(a, { seq: 10, threadId: 'a' });
		track(b, { seq: 70, threadId: 'b' });
		track(unbound, { seq: 71, threadId: null });
		capture.beginComposing('a');
		show([a, true]);
		show([b, true], [unbound, true]);
		expect(capture.takeReplyContext('a')).toEqual({ in_view: [[10, 10, 1]] });
	});

	it('does not track a row with no seq until it has one', async () => {
		const track = await load();
		const capture = await import('$lib/stores/replyContextCapture');
		const node = row();
		const action = track(node, { seq: undefined, threadId: 't1' });
		expect(observed.has(node)).toBe(false);
		action.update({ seq: 7, threadId: 't1' });
		expect(observed.has(node)).toBe(true);
		capture.beginComposing('t1');
		show([node, true]);
		expect(capture.takeReplyContext('t1')).toEqual({ in_view: [[7, 7, 1]] });
	});

	it('stops reporting a row that is destroyed', async () => {
		const track = await load();
		const capture = await import('$lib/stores/replyContextCapture');
		const node = row();
		const action = track(node, { seq: 3, threadId: 't1' });
		show([node, true]);
		action.destroy();
		expect(observed.has(node)).toBe(false);
		capture.beginComposing('t1');
		expect(capture.takeReplyContext('t1')).toBeNull();
	});

	it('is a no-op where there is no IntersectionObserver', async () => {
		vi.unstubAllGlobals();
		vi.stubGlobal('IntersectionObserver', undefined);
		const track = await load();
		const capture = await import('$lib/stores/replyContextCapture');
		const action = track(row(), { seq: 1, threadId: 't1' });
		action.update({ seq: 2, threadId: 't1' });
		action.destroy();
		capture.beginComposing('t1');
		expect(capture.takeReplyContext('t1')).toBeNull();
	});
});
