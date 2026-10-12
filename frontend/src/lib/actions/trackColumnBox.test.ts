import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { trackColumnBox, type ColumnBox } from './trackColumnBox';

let left = 100;
let frames: Array<() => void> = [];
let now = 0;
let reads = 0;
let resized: () => void = () => {};

beforeEach(() => {
	left = 100; frames = []; now = 0; reads = 0;
	vi.stubGlobal('requestAnimationFrame', (callback: () => void) => frames.push(callback));
	vi.stubGlobal('cancelAnimationFrame', () => {});
	vi.spyOn(performance, 'now').mockImplementation(() => now);
	vi.stubGlobal('ResizeObserver', class {
		constructor(callback: () => void) { resized = callback; }
		observe() {}
		disconnect() {}
	});
});
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

function column() {
	const node = document.createElement('div');
	document.body.append(node);
	node.getBoundingClientRect = () => { reads++; return { top: 0, bottom: 600, left, right: left + 500, height: 600 } as DOMRect; };
	return node;
}
function transition(type: string, target: Element, propertyName: string) {
	target.dispatchEvent(Object.assign(new Event(type, { bubbles: true }), { propertyName }));
}
/** Runs the queued animation frames one by one, `step` ms apart, until none is queued or `limit` frames ran. */
function run(limit: number, step = 16, each: () => void = () => {}) {
	let ran = 0;
	while (frames.length && ran < limit) { const next = frames.shift()!; now += step; each(); next(); ran++; }
	return ran;
}

describe('trackColumnBox', () => {
	it('reports the box once, then only when it changes', () => {
		const seen: ColumnBox[] = [];
		const stop = trackColumnBox(column(), (box) => seen.push(box));
		expect(seen).toHaveLength(1);
		resized(); run(3);
		expect(seen).toHaveLength(1);
		left = 130; resized(); run(3);
		expect(seen.map((box) => box.left)).toEqual([100, 130]);
		stop();
	});

	it('restarts the follow when a delayed layout transition starts moving, not only when it is created', () => {
		const seen: ColumnBox[] = [];
		const stop = trackColumnBox(column(), (box) => seen.push(box));
		const neighbour = document.createElement('aside');
		document.body.append(neighbour);
		transition('transitionrun', neighbour, 'width');
		// The delay passes with the box still: the follow gives up after two still frames.
		run(100);
		now += 300;
		transition('transitionstart', neighbour, 'width');
		left = 160;
		run(100);
		expect(seen.at(-1)?.left).toBe(160);
		stop();
	});

	it('follows a neighbour layout transition frame by frame, then stops once the box holds still', () => {
		const seen: ColumnBox[] = [];
		const stop = trackColumnBox(column(), (box) => seen.push(box));
		const neighbour = document.createElement('aside');
		document.body.append(neighbour);
		transition('transitionrun', neighbour, 'width');
		let moved = 0;
		const ran = run(100, 16, () => { if (moved < 5) { left += 10; moved++; } });
		expect(seen.at(-1)?.left).toBe(150);
		expect(ran).toBeLessThan(10);
		expect(frames).toHaveLength(0);
		stop();
	});

	it('gives up after the follow limit even when its end event never arrives', () => {
		const stop = trackColumnBox(column(), () => {});
		const neighbour = document.createElement('aside');
		document.body.append(neighbour);
		transition('transitionrun', neighbour, 'width');
		neighbour.remove();
		run(1000, 16, () => { left += 1; });
		expect(frames).toHaveLength(0);
		expect(now).toBeLessThanOrEqual(420 + 16);
		stop();
	});

	it('ignores a press or hover that animates transform, and the sheet itself', () => {
		const stop = trackColumnBox(column(), () => {});
		const before = reads;
		const button = document.createElement('button');
		document.body.append(button);
		transition('transitionrun', button, 'transform');
		const sheet = document.createElement('div');
		sheet.setAttribute('data-adaptive-drawer', '');
		const inner = document.createElement('div');
		sheet.append(inner); document.body.append(sheet);
		transition('transitionrun', inner, 'height');
		expect(frames).toHaveLength(0);
		expect(reads).toBe(before);
		stop();
	});

	it('restarts the follow when another layout transition begins', () => {
		const stop = trackColumnBox(column(), () => {});
		const neighbour = document.createElement('aside');
		document.body.append(neighbour);
		transition('transitionrun', neighbour, 'width');
		run(1);
		now += 300;
		transition('transitionrun', neighbour, 'margin-left');
		run(1000, 16, () => { left += 1; });
		expect(now).toBeLessThanOrEqual(300 + 16 + 420 + 16 * 2);
		stop();
	});
});
