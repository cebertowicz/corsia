import { afterEach, describe, expect, it, vi } from 'vitest';
import { keepInView } from './keepInView';

describe('keepInView', () => {
	const originalObserver = globalThis.ResizeObserver;
	const originalReveal = HTMLElement.prototype.scrollIntoView;
	afterEach(() => {
		globalThis.ResizeObserver = originalObserver;
		Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', { configurable: true, value: originalReveal });
		vi.restoreAllMocks();
	});

	it('brings the node back into view when the node itself grows, not only when its scroller resizes', async () => {
		let notify: () => void = () => undefined;
		const watched: Element[] = [];
		globalThis.ResizeObserver = class { constructor(cb: () => void) { notify = cb; } observe(el: Element) { watched.push(el); } disconnect() {} } as unknown as typeof ResizeObserver;
		const reveal = vi.fn();
		Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', { configurable: true, value: reveal });
		vi.spyOn(globalThis, 'requestAnimationFrame').mockImplementation((cb) => { cb(0); return 1; });
		const owner = document.createElement('div');
		owner.style.overflowY = 'auto';
		const node = document.createElement('div');
		owner.append(node);
		document.body.append(owner);
		keepInView(node, { enabled: true });
		expect(watched).toContain(node);
		expect(watched).toContain(owner);
		await Promise.resolve();
		reveal.mockClear();
		notify();
		expect(reveal).toHaveBeenCalledWith({ block: 'nearest' });
		owner.remove();
	});

	describe('a block taller than the room', () => {
		function tallBlock(height: number) {
			const reveal = vi.fn();
			Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', { configurable: true, value: reveal });
			vi.spyOn(globalThis, 'requestAnimationFrame').mockImplementation((cb) => { cb(0); return 1; });
			const owner = document.createElement('div');
			owner.style.overflowY = 'auto';
			Object.defineProperty(owner, 'clientHeight', { configurable: true, value: 600 });
			const whole = document.createElement('div');
			vi.spyOn(whole, 'getBoundingClientRect').mockReturnValue({ height } as DOMRect);
			const node = document.createElement('div');
			whole.append(node);
			owner.append(whole);
			document.body.append(owner);
			return { owner, whole, node, reveal };
		}

		it('reveals the field alone, as the person types in it', async () => {
			const { owner, whole, node, reveal } = tallBlock(2000);
			keepInView(node, { enabled: true, whole });
			await Promise.resolve();
			expect(reveal.mock.contexts).toContain(node);
			expect(reveal.mock.contexts).not.toContain(whole);
			owner.remove();
		});

		it('reveals the whole block when it fits the room', async () => {
			const { owner, whole, node, reveal } = tallBlock(300);
			keepInView(node, { enabled: true, whole });
			await Promise.resolve();
			expect(reveal.mock.contexts).toContain(whole);
			owner.remove();
		});
	});
});
