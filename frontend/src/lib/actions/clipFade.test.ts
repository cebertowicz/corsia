import { beforeEach, describe, expect, it, vi } from 'vitest';

const reduced = vi.hoisted(() => ({ store: null as unknown as import('svelte/store').Writable<boolean> }));
vi.mock('$lib/stores/viewport', async () => {
	const { writable: make } = await import('svelte/store');
	reduced.store = make(false);
	return { prefersReducedMotion: reduced.store };
});

const carry = vi.hoisted(() => vi.fn());
vi.mock('./settleHeight', () => ({ carryPanelHeight: carry }));

import { clipFade } from './clipFade';

describe('clipFade', () => {
	beforeEach(() => { reduced.store.set(false); });

	it('eases the size and the opacity together', () => {
		const node = document.createElement('div');
		document.body.append(node);
		const config = clipFade(node, { axis: 'y' });
		const css = (config as { css: (t: number, u: number) => string }).css(0.5, 0.5);
		expect(css).toContain('height');
		expect(css).toMatch(/opacity: 0\.5$/);
	});

	const animation = (end: number) => {
		let settle: () => void = () => {};
		const item = Object.assign(new EventTarget(), { effect: { getComputedTiming: () => ({ endTime: end }) }, finished: new Promise((resolve) => { settle = () => resolve(null); }) });
		return { item, settle };
	};
	const later = () => new Promise((resolve) => setTimeout(resolve, 5));

	it('tells a sheet around it at once, and holds until the real animation Svelte starts on the placeholder\'s finish event has finished', async () => {
		carry.mockClear();
		vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => setTimeout(() => callback(0), 0));
		const node = document.createElement('div');
		document.body.append(node);
		const placeholder = animation(0), height = animation(180), endless = animation(Infinity);
		let running = [placeholder.item, endless.item];
		node.getAnimations = (() => running) as unknown as typeof node.getAnimations;
		clipFade(node, { axis: 'y' });
		await Promise.resolve();
		expect(carry).toHaveBeenCalledTimes(1);
		let over = false;
		void (carry.mock.calls[0][1] as Promise<unknown>).then(() => { over = true; });
		// The placeholder's promise settles, and frames pass, before its finish handler starts the real slide.
		placeholder.settle();
		running = [endless.item];
		await later();
		expect(over).toBe(false);
		running = [height.item, endless.item];
		placeholder.item.dispatchEvent(new Event('finish'));
		await later();
		expect(over).toBe(false);
		height.settle();
		await later();
		expect(over).toBe(true);
		vi.unstubAllGlobals();
	});

	it('lets the sheet go when the placeholder is cancelled before any slide starts', async () => {
		carry.mockClear();
		const node = document.createElement('div');
		document.body.append(node);
		const placeholder = animation(0);
		let running = [placeholder.item];
		node.getAnimations = (() => running) as unknown as typeof node.getAnimations;
		clipFade(node, { axis: 'y' });
		await Promise.resolve();
		let over = false;
		void (carry.mock.calls[0][1] as Promise<unknown>).then(() => { over = true; });
		running = [];
		placeholder.item.dispatchEvent(new Event('cancel'));
		await later();
		expect(over).toBe(true);
	});

	it('only fades under reduced motion: nothing slides', () => {
		reduced.store.set(true);
		const node = document.createElement('div');
		document.body.append(node);
		const css = (clipFade(node) as { css: (t: number, u: number) => string }).css(0.5, 0.5);
		expect(css).toBe('opacity: 0.5');
	});
});
