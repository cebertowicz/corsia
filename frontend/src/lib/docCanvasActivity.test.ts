import { afterEach, describe, expect, it, vi } from 'vitest';
import { UnseenDocActivity, bottomNoticesHeight, watchCanvasAndBand, type ActivityPosition } from './docCanvasActivity';
import type { DocActivityFrame } from './docPresence';

const frame = (id: string, from = 100): DocActivityFrame => ({
	type: 'doc_activity', artifact_id: 'doc', thread_id: 'agent', agent: 'Reviewer',
	kind: 'comment', span: { from, to: from + 10 }, comment_id: id, revision: null, at: id,
});
const position = (from: number): ActivityPosition => from < 100
	? { direction: 'above', distance: 20 }
	: { direction: 'below', distance: 20 };

describe('unseen document arrivals', () => {
	it('counts only the nearest direction, below wins a tie, and switches once seen', () => {
		const unseen = new UnseenDocActivity();
		unseen.add(frame('above', 20), position);
		unseen.add(frame('below-1', 100), position);
		unseen.add(frame('below-2', 200), position);
		expect(unseen.pill(position)).toMatchObject({ count: 2, direction: 'below', span: { from: 100, to: 110 } });
		unseen.seen((from) => from >= 100 ? { direction: 'visible', distance: 0 } : position(from));
		expect(unseen.pill(position)).toMatchObject({ count: 1, direction: 'above' });
	});
	it('never counts visible, unlocatable or duplicate activity', () => {
		const unseen = new UnseenDocActivity();
		const locate = (from: number): ActivityPosition | null => from === 0 ? null : from === 10 ? { direction: 'visible', distance: 0 } : position(from);
		unseen.add(frame('invalid', 0), locate);
		unseen.add(frame('visible', 10), locate);
		unseen.add(frame('new'), locate);
		unseen.add(frame('new'), locate);
		expect(unseen.pill(locate)?.count).toBe(1);
		unseen.seen(() => ({ direction: 'visible', distance: 0 }));
		unseen.add(frame('new'), locate);
		expect(unseen.pill(locate)).toBeNull();
	});
	it('uses nearest viewport-edge distance and removes invalid destinations', () => {
		const unseen = new UnseenDocActivity();
		const locate = (from: number): ActivityPosition => ({ direction: from < 100 ? 'above' : 'below', distance: from });
		unseen.add(frame('above', 20), locate);
		unseen.add(frame('below', 200), locate);
		expect(unseen.pill(locate)?.direction).toBe('above');
		unseen.seen(() => null);
		expect(unseen.pill(locate)).toBeNull();
	});
	it('maps unseen offsets through edits and clears its session on leaving', () => {
		const unseen = new UnseenDocActivity();
		unseen.add(frame('new'), position);
		unseen.map((offset) => offset + 20);
		expect(unseen.pill(position)?.span.from).toBe(120);
		unseen.clear();
		expect(unseen.pill(position)).toBeNull();
	});
});

describe('bottomNoticesHeight', () => {
	const clip = new DOMRect(0, 0, 393, 700);
	const band = (height: number) => new DOMRect(0, 700 - height, 393, height);

	it('is nothing with no notice showing', () => {
		expect(bottomNoticesHeight(clip, undefined, null)).toBe(0);
	});

	it('follows the band as it grows from one notice to a stack, so a stacked band is covered as fully as a single one', () => {
		expect(bottomNoticesHeight(clip, band(70), null)).toBe(70);
		expect(bottomNoticesHeight(clip, band(122), null)).toBe(122);
	});

	it('reaches the top of a below pill standing over the band, or alone at the foot', () => {
		expect(bottomNoticesHeight(clip, band(70), 700 - 70 - 48)).toBe(118);
		expect(bottomNoticesHeight(clip, undefined, 700 - 48)).toBe(48);
	});
});

describe('watchCanvasAndBand', () => {
	afterEach(() => vi.unstubAllGlobals());

	it('watches the size of a bottom band that appears later, so the pill moves when the band grows in place', async () => {
		const observed: Element[] = [];
		vi.stubGlobal('ResizeObserver', class { observe(node: Element) { observed.push(node); } disconnect() {} });
		const root = document.createElement('div');
		const changed = vi.fn();
		const stop = watchCanvasAndBand(root, changed);
		expect(observed).toEqual([root]);
		const band = root.appendChild(document.createElement('div'));
		band.setAttribute('data-doc-bottom-band', '');
		await Promise.resolve();
		expect(observed).toEqual([root, band]);
		expect(changed).toHaveBeenCalledTimes(2);
		stop();
		root.removeChild(band);
		await Promise.resolve();
		expect(changed).toHaveBeenCalledTimes(2);
	});

	it('stops watching a band once it is removed, and watches the one that replaces it', async () => {
		const observed = new Set<Element>();
		vi.stubGlobal('ResizeObserver', class {
			observe(node: Element) { observed.add(node); }
			unobserve(node: Element) { observed.delete(node); }
			disconnect() { observed.clear(); }
		});
		const root = document.createElement('div');
		const addBand = () => {
			const band = document.createElement('div');
			band.setAttribute('data-doc-bottom-band', '');
			return root.appendChild(band);
		};
		const stop = watchCanvasAndBand(root, vi.fn());
		const first = addBand();
		await Promise.resolve();
		expect([...observed]).toEqual([root, first]);
		root.removeChild(first);
		await Promise.resolve();
		expect([...observed]).toEqual([root]);
		const second = addBand();
		await Promise.resolve();
		expect([...observed]).toEqual([root, second]);
		stop();
	});
});
