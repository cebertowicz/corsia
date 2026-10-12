import { describe, expect, it } from 'vitest';
import { autosize } from './autosize';

function field(scrollHeight: () => number, maxHeight = '100px') {
	const node = document.createElement('textarea');
	document.body.append(node);
	Object.defineProperty(node, 'scrollHeight', { get: scrollHeight, configurable: true });
	node.style.maxHeight = maxHeight;
	node.style.boxSizing = 'border-box';
	return node;
}

describe('autosize', () => {
	it('sizes the field to its text', () => {
		const node = field(() => 54);
		autosize(node, 'a long reply that wraps');
		expect(node.style.height).toBe('54px');
		expect(node.style.overflowY).toBe('hidden');
	});

	it('caps the height and scrolls past it', () => {
		const node = field(() => 240);
		autosize(node, 'many lines');
		expect(node.style.height).toBe('100px');
		expect(node.style.overflowY).toBe('auto');
	});

	it('refits when the value changes, and leaves the transition to CSS', () => {
		let tall = 54;
		const node = field(() => tall);
		const action = autosize(node, 'one');
		tall = 36;
		action.update('');
		expect(node.style.height).toBe('36px');
		expect(node.style.transition).toBe('');
	});
});
