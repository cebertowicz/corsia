import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { holdHeight } from './holdHeight';

let height = 100;
let notify: () => void = () => {};

beforeEach(() => {
	height = 100;
	vi.stubGlobal('ResizeObserver', class {
		constructor(callback: () => void) { notify = callback; }
		observe() {}
		disconnect() {}
	});
	vi.stubGlobal('requestAnimationFrame', (callback: () => void) => { callback(); return 1; });
	vi.stubGlobal('cancelAnimationFrame', () => {});
});
afterEach(() => vi.unstubAllGlobals());

function slot() {
	const node = document.createElement('div');
	node.getBoundingClientRect = () => ({ height }) as DOMRect;
	return node;
}

describe('holdHeight', () => {
	it('floors the node at the tallest it has been, so a shorter state cannot pull the scroller up', () => {
		const node = slot();
		holdHeight(node, 'a');
		notify();
		expect(node.style.minHeight).toBe('100px');
		height = 170;
		notify();
		expect(node.style.minHeight).toBe('170px');
		height = 24;
		notify();
		expect(node.style.minHeight).toBe('170px');
	});

	it('lets go when the key changes and holds again from the height after the change, even if no resize follows', () => {
		const node = slot();
		const action = holdHeight(node, 'a');
		notify();
		action.update('a');
		expect(node.style.minHeight).toBe('100px');
		height = 40;
		action.update('b');
		expect(node.style.minHeight).toBe('40px');
		height = 10;
		notify();
		expect(node.style.minHeight).toBe('40px');
	});

	it('is not released by a click inside it, so selecting text or following a link keeps the floor', () => {
		const node = slot();
		holdHeight(node, 'a');
		notify();
		node.dispatchEvent(new Event('click'));
		expect(node.style.minHeight).toBe('100px');
	});
});
