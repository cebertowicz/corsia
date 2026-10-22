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
		track(a, 10);
		track(b, 11);
		capture.beginComposing('t1');
		show([a, true], [b, true]);
		show([a, false]);
		expect(capture.takeReplyContext('t1')).toEqual({ in_view: [[10, 11, 2]] });
	});

	it('does not track a row with no seq until it has one', async () => {
		const track = await load();
		const capture = await import('$lib/stores/replyContextCapture');
		const node = row();
		const action = track(node, undefined);
		expect(observed.has(node)).toBe(false);
		action.update(7);
		expect(observed.has(node)).toBe(true);
		capture.beginComposing('t1');
		show([node, true]);
		expect(capture.takeReplyContext('t1')).toEqual({ in_view: [[7, 7, 1]] });
	});

	it('stops reporting a row that is destroyed', async () => {
		const track = await load();
		const capture = await import('$lib/stores/replyContextCapture');
		const node = row();
		const action = track(node, 3);
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
		const action = track(row(), 1);
		action.update(2);
		action.destroy();
		capture.beginComposing('t1');
		expect(capture.takeReplyContext('t1')).toBeNull();
	});
});
