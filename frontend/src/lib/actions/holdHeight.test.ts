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

	it('lets go when the key changes, as when the person collapses a step', () => {
		const node = slot();
		const action = holdHeight(node, 'a');
		notify();
		action.update('a');
		expect(node.style.minHeight).toBe('100px');
		action.update('b');
		expect(node.style.minHeight).toBe('');
		height = 40;
		notify();
		expect(node.style.minHeight).toBe('40px');
	});
});
