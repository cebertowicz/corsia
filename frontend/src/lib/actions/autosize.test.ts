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

	it('with no cap takes its whole text and never scrolls, however full its scroller is', () => {
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

	it('restores the caret after pasting into an already scrolling field', () => {
		const node = field(() => 240);
		node.value = 'first long draft';
		const action = autosize(node, node.value);
		node.focus();
		node.value = 'first long draft followed by a paste';
		node.setSelectionRange(node.value.length, node.value.length);
		node.scrollTop = 0;
		action.update(node.value);
		expect(node.scrollTop).toBe(240);
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

	it('waits for the height ease before a refit, so the ease is not aborted', async () => {
		let view = 200, tall = 100; const refit: Array<() => void> = [];
		const ResizeObserver = class {
			constructor(callback: () => void) { refit.push(callback); }
			observe() {} disconnect() {}
		};
		(globalThis as Record<string, unknown>).ResizeObserver = ResizeObserver;
		const owner = document.createElement('div'); owner.style.overflowY = 'auto'; document.body.append(owner);
		const node = field(() => tall, 'none'); owner.append(node);
		Object.defineProperty(owner, 'clientWidth', { get: () => view, configurable: true });
		let easeRunning = true, settleEase: () => void = () => {};
		const finished = new Promise<void>((resolve) => { settleEase = resolve; });
		// A finished ease stays listed for a frame, so only a running one may hold the refit.
		node.getAnimations = () => [{ finished, playState: easeRunning ? 'running' : 'finished' } as unknown as Animation];
		const action = autosize(node, 'a reply');
		const fitted = node.style.height;
		// The field widens while it is still easing to its last height, rewrapping its text: measuring now would write over the ease.
		view = 400; tall = 40;
		refit[0]?.();
		await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
		expect(node.style.height).toBe(fitted);
		// The ease ends without a `transitionend` — a replaced transition never fires one — and its record lingers.
		easeRunning = false; settleEase();
		await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
		expect(node.style.height).toBe('40px');
		action.destroy();
		delete (globalThis as Record<string, unknown>).ResizeObserver;
	});
});
