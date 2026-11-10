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

	it('treats a field holding exactly its cap of text as fitting, though the cap is fractional', () => {
		// Five lines read scrollHeight 126 against a computed cap of 125.953px: a subpixel over must not make it scrollable.
		const node = field(() => 126, '125.953px');
		autosize(node, 'five lines');
		expect(node.style.overflowY).toBe('hidden');
		expect(node.style.height).toBe('125.953px');
		const sixth = field(() => 148, '125.953px');
		autosize(sixth, 'six lines');
		expect(sixth.style.overflowY).toBe('auto');
	});

	it('keeps the text at the top while the height eases below the cap, and scrolls freely past it', () => {
		const small = field(() => 54);
		const action = autosize(small, 'a line');
		small.scrollTop = 18; small.dispatchEvent(new Event('scroll'));
		expect(small.scrollTop).toBe(0);
		action.destroy?.();
		const capped = field(() => 240);
		autosize(capped, 'many lines');
		capped.scrollTop = 18; capped.dispatchEvent(new Event('scroll'));
		expect(capped.scrollTop).toBe(18);
	});

	it('with fit and no cap grows until its scroller is full, in whole lines, then scrolls', () => {
		const owner = document.createElement('div');
		owner.style.overflowY = 'auto';
		document.body.append(owner);
		const node = field(() => 640, 'none');
		node.style.lineHeight = '20px';
		node.style.borderTopWidth = node.style.borderBottomWidth = '5px';
		owner.append(node);
		// The scroller shows 300px and holds 120px besides the field, so at full height (650px) it overflows by 470px.
		Object.defineProperty(owner, 'clientHeight', { get: () => 300, configurable: true });
		Object.defineProperty(owner, 'scrollHeight', { get: () => 120 + (Number.parseFloat(node.style.height) || 0), configurable: true });
		autosize(node, { value: 'a pasted page', fit: true });
		// 650 - 470 = 180px, less 10px of border, holds 8 whole 20px lines: 160px of text plus the border.
		expect(node.style.height).toBe('170px');
		expect(node.style.overflowY).toBe('auto');
	});

	it('with fit never drops below six lines, however full its scroller already is', () => {
		const owner = document.createElement('div');
		owner.style.overflowY = 'auto';
		document.body.append(owner);
		const node = field(() => 640, 'none');
		node.style.lineHeight = '20px';
		owner.append(node);
		Object.defineProperty(owner, 'clientHeight', { get: () => 300, configurable: true });
		Object.defineProperty(owner, 'scrollHeight', { get: () => 2000, configurable: true });
		autosize(node, { value: 'a pasted page', fit: true });
		expect(node.style.height).toBe('120px');
	});

	it('without fit and no cap takes its whole text, as a margin card does', () => {
		const owner = document.createElement('div');
		owner.style.overflowY = 'auto';
		document.body.append(owner);
		Object.defineProperty(owner, 'clientHeight', { get: () => 300, configurable: true });
		Object.defineProperty(owner, 'scrollHeight', { get: () => 2000, configurable: true });
		const node = field(() => 640, 'none');
		owner.append(node);
		autosize(node, 'a pasted page');
		expect(node.style.height).toBe('640px');
		expect(node.style.overflowY).toBe('hidden');
	});

	it('brings the caret at the end back into view when a paste takes the field past its cap', () => {
		let tall = 54;
		const node = field(() => tall);
		node.value = 'short';
		const action = autosize(node, 'short');
		node.focus();
		node.value = 'a pasted page'; node.setSelectionRange(node.value.length, node.value.length);
		tall = 240;
		let written = -1;
		Object.defineProperty(node, 'scrollTop', { get: () => (written < 0 ? 0 : written), set: (value: number) => { written = value; }, configurable: true });
		action.update?.('a pasted page');
		expect(node.style.overflowY).toBe('auto');
		expect(written).toBe(240);
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
