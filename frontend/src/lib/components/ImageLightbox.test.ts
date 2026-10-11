import { createRawSnippet, flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const viewport = vi.hoisted(() => ({ mobile: null as unknown as import('svelte/store').Writable<boolean> }));
vi.mock('$lib/stores/viewport', async () => {
	const { writable: make } = await import('svelte/store');
	viewport.mobile = make(false);
	return { isMobile: viewport.mobile, prefersReducedMotion: make(false) };
});

import ImageLightbox from './ImageLightbox.svelte';

// The image is 1000 x 500 in a 548 x 400 viewport: Fit is 0.5 (500 of room over 1000).
const NATURAL = { width: 1000, height: 500 };
let host: HTMLElement;
let component: Record<string, unknown> | null = null;
let realRO: typeof ResizeObserver;
const sizes = { width: 548, height: 400 };

function define(name: string, get: () => number) {
	Object.defineProperty(HTMLElement.prototype, name, { configurable: true, get });
}
const settle = async () => { for (let i = 0; i < 4; i++) { await Promise.resolve(); flushSync(); } };

function render(props: Record<string, unknown> = {}) {
	component = mount(ImageLightbox, { target: host, props: { src: '/api/files/content?path=%2Fws%2Fa.png', alt: 'Mock screen', ...props } });
	flushSync();
	return host.querySelector<HTMLImageElement>('.pad img')!;
}
async function loaded(props: Record<string, unknown> = {}) {
	const img = render(props);
	Object.defineProperty(img, 'naturalWidth', { configurable: true, value: NATURAL.width });
	Object.defineProperty(img, 'naturalHeight', { configurable: true, value: NATURAL.height });
	img.dispatchEvent(new Event('load'));
	await settle();
	return img;
}
const button = (name: string | RegExp) => [...host.querySelectorAll<HTMLButtonElement>('button')].find((b) => (b.getAttribute('aria-label') ?? b.textContent ?? '').match(name))!;
const width = (img: HTMLImageElement) => Number.parseFloat((img.closest('.frame') as HTMLElement).style.width);

beforeEach(() => {
	realRO = globalThis.ResizeObserver;
	globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as unknown as typeof ResizeObserver;
	define('clientWidth', () => sizes.width);
	define('clientHeight', () => sizes.height);
	viewport.mobile.set(false);
	host = document.createElement('div');
	document.body.appendChild(host);
});
afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	globalThis.ResizeObserver = realRO;
	delete (HTMLElement.prototype as unknown as Record<string, unknown>).clientWidth;
	delete (HTMLElement.prototype as unknown as Record<string, unknown>).clientHeight;
	vi.unstubAllGlobals();
});

describe('ImageLightbox', () => {
	it('shows the image at Fit with a margin once it has loaded, and never above its own size', async () => {
		const img = await loaded();
		expect(width(img)).toBeCloseTo(500);
		expect(button('Fit').getAttribute('aria-pressed')).toBe('true');
		expect(host.querySelector('.level')!.textContent).toBe('50%');
	});

	it('never enlarges an image that fits at its own size', async () => {
		sizes.width = 4000; sizes.height = 4000;
		const img = await loaded();
		expect(width(img)).toBe(1000);
		expect(button('Zoom out').disabled).toBe(true);
		sizes.width = 548; sizes.height = 400;
	});

	it('draws the bar with Back, Fit, 100%, minus, the level and plus on a desktop', async () => {
		await loaded({ onBack: () => {}, docName: 'walkthrough.md' });
		expect(button('Back to walkthrough.md')).toBeDefined();
		for (const name of ['Fit', '100%', 'Zoom out', 'Zoom in']) expect(button(name), name).toBeDefined();
	});

	it('zooms to 100% and steps in and out, and Fit returns to the fitted size', async () => {
		const img = await loaded();
		button('100%').click();
		await settle();
		expect(width(img)).toBeCloseTo(1000);
		expect(button('100%').getAttribute('aria-pressed')).toBe('true');
		expect(button('Fit').getAttribute('aria-pressed')).toBe('false');

		button('Zoom in').click();
		await settle();
		expect(width(img)).toBeCloseTo(1250);
		button('Zoom out').click();
		await settle();
		expect(width(img)).toBeCloseTo(1000);

		button('Fit').click();
		await settle();
		expect(width(img)).toBeCloseTo(500);
		expect(button('Zoom out').disabled).toBe(true);
	});

	it('stops at 8x', async () => {
		const img = await loaded();
		for (let i = 0; i < 20; i++) { button('Zoom in').click(); await settle(); }
		expect(width(img)).toBeCloseTo(8000);
		expect(button('Zoom in').disabled).toBe(true);
	});

	it('zooms on Cmd-scroll and on a trackpad pinch, and leaves a plain scroll to the browser', async () => {
		await loaded();
		const region = host.querySelector<HTMLElement>('.viewport')!;
		const img = host.querySelector<HTMLImageElement>('.pad img')!;
		const plain = new WheelEvent('wheel', { deltaY: -40, bubbles: true, cancelable: true });
		region.dispatchEvent(plain);
		expect(plain.defaultPrevented).toBe(false);
		expect(width(img)).toBeCloseTo(500);

		const meta = new WheelEvent('wheel', { deltaY: -40, metaKey: true, bubbles: true, cancelable: true });
		region.dispatchEvent(meta);
		expect(meta.defaultPrevented).toBe(true);
		expect(width(img)).toBeGreaterThan(500);
		const afterMeta = width(img);

		region.dispatchEvent(new WheelEvent('wheel', { deltaY: -10, ctrlKey: true, bubbles: true, cancelable: true }));
		expect(width(img)).toBeGreaterThan(afterMeta);
	});

	it('zooms on the keyboard from the focused image, but not with a browser-zoom chord', async () => {
		const img = await loaded();
		const region = host.querySelector<HTMLElement>('.viewport')!;
		const press = (key: string, init: KeyboardEventInit = {}) => region.dispatchEvent(new KeyboardEvent('keydown', { key, bubbles: true, cancelable: true, ...init }));
		press('1');
		await settle();
		expect(width(img)).toBeCloseTo(1000);
		press('=', { metaKey: true });
		await settle();
		expect(width(img)).toBeCloseTo(1000);
		press('0');
		await settle();
		expect(width(img)).toBeCloseTo(500);
	});

	it('goes Back from its button and on Escape, once, and Escape means nothing without a Back', async () => {
		const back = vi.fn();
		await loaded({ onBack: back, docName: 'doc.md' });
		button('Back to doc.md').click();
		expect(back).toHaveBeenCalledTimes(1);
		const esc = new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true });
		window.dispatchEvent(esc);
		expect(back).toHaveBeenCalledTimes(2);
		expect(esc.defaultPrevented).toBe(true);

		const claimed = new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true });
		claimed.preventDefault();
		window.dispatchEvent(claimed);
		expect(back).toHaveBeenCalledTimes(2);

	});

	it('has no Back and ignores Escape when it is not opened from a document', async () => {
		await loaded();
		const esc = new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true });
		window.dispatchEvent(esc);
		expect(esc.defaultPrevented).toBe(false);
		expect(host.querySelector('.back')).toBeNull();
	});

	it('offers only Back and Fit on a phone', async () => {
		viewport.mobile.set(true);
		await loaded({ onBack: () => {}, docName: 'walkthrough.md' });
		const labels = [...host.querySelectorAll('button')].map((b) => b.getAttribute('aria-label') ?? b.textContent?.trim());
		expect(labels).toEqual(['Back to walkthrough.md', 'Fit']);
		expect(host.querySelector('.back-text')!.textContent).toBe('Back');
	});

	it('leaves room in the bar for another action, drawn before the zoom controls', async () => {
		const seam = createRawSnippet(() => ({ render: () => '<button type="button">Seam</button>' }));
		await loaded({ barActions: seam });
		const labels = [...host.querySelectorAll('.bar button')].map((b) => b.textContent?.trim());
		expect(labels[0]).toBe('Seam');
		expect(labels).toContain('Fit');
	});

	it('draws an overlay inside a frame of exactly the image\'s size, once the image has loaded, with the scale', async () => {
		const layer = createRawSnippet<[{ scale: number }]>((args) => ({ render: () => `<i class="layer">${args().scale}</i>` }));
		const img = render({ overlay: layer });
		expect(host.querySelector('.layer')).toBeNull();
		Object.defineProperty(img, 'naturalWidth', { configurable: true, value: NATURAL.width });
		Object.defineProperty(img, 'naturalHeight', { configurable: true, value: NATURAL.height });
		img.dispatchEvent(new Event('load'));
		await settle();

		const frame = host.querySelector<HTMLElement>('.frame')!;
		expect(frame.contains(host.querySelector('.layer'))).toBe(true);
		expect(host.querySelector('.layer')!.textContent).toBe('0.5');
		expect([frame.style.width, frame.style.height]).toEqual(['500px', '250px']);
		button('100%').click();
		await settle();
		expect([frame.style.width, frame.style.height]).toEqual(['1000px', '500px']);
	});

	it('scrolls a fraction of the image to the centre of the viewport on request, and stays put when nothing is loaded', async () => {
		let api: { revealFraction: (x: number, y: number) => void } | undefined;
		component = mount(ImageLightbox, { target: host, props: { src: '/a.png', alt: 'a' } });
		api = component as unknown as typeof api;
		flushSync();
		const region = host.querySelector<HTMLElement>('.viewport')!;
		api!.revealFraction(0.5, 0.5);
		expect([region.scrollLeft, region.scrollTop]).toEqual([0, 0]);

		const img = host.querySelector<HTMLImageElement>('.pad img')!;
		Object.defineProperty(img, 'naturalWidth', { configurable: true, value: NATURAL.width });
		Object.defineProperty(img, 'naturalHeight', { configurable: true, value: NATURAL.height });
		img.dispatchEvent(new Event('load'));
		await settle();
		// The viewport spans x 0..548 and y 0..400; the image is drawn from x -300, y -100 at 1000 x 500.
		vi.spyOn(region, 'getBoundingClientRect').mockReturnValue({ left: 0, top: 0, width: 548, height: 400, right: 548, bottom: 400, x: 0, y: 0, toJSON() {} } as DOMRect);
		vi.spyOn(img, 'getBoundingClientRect').mockReturnValue({ left: -300, top: -100, width: 1000, height: 500, right: 700, bottom: 400, x: -300, y: -100, toJSON() {} } as DOMRect);
		api!.revealFraction(0.9, 2);
		// 0.9 of the width is at x 600, to be centred at 274; the vertical fraction clamps to the bottom edge, y 400 to 200.
		expect(region.scrollLeft).toBeCloseTo(326);
		expect(region.scrollTop).toBeCloseTo(200);
	});

	it('shows why an image did not load, with a way to try again, and the bar stays', async () => {
		vi.stubGlobal('fetch', vi.fn(async () => ({ status: 404, body: null })));
		const failed = vi.fn();
		const img = render({ onBack: () => {}, docName: 'doc.md', onFailed: failed });
		img.dispatchEvent(new Event('error'));
		await vi.waitFor(() => expect(host.querySelector('.load-error code')?.textContent).toBe('Mock screen · Image not found'));
		expect(failed).toHaveBeenCalled();
		expect(host.querySelector('.load-error')!.getAttribute('role')).toBe('alert');
		expect(button('Back to doc.md')).toBeDefined();
		expect(host.querySelector('.pad img')).toBeNull();

		button('Try again').click();
		await settle();
		expect(host.querySelector('.pad img')).not.toBeNull();
		expect(host.querySelector('.load-error')).toBeNull();
	});

	it('hands the retry to the host when it has its own reload', async () => {
		vi.stubGlobal('fetch', vi.fn(async () => ({ status: 500, body: null })));
		const retry = vi.fn();
		render({ onRetry: retry });
		host.querySelector('.pad img')!.dispatchEvent(new Event('error'));
		await vi.waitFor(() => expect(host.querySelector('.load-error')).not.toBeNull());
		expect(host.querySelector('.load-error code')).toBeNull();
		button('Try again').click();
		expect(retry).toHaveBeenCalledTimes(1);
	});

	it('draws a pending image hidden behind a load bar until it has loaded', async () => {
		render();
		expect(host.querySelector('.loadbar')).not.toBeNull();
		expect(host.querySelector('.viewport')!.classList.contains('pending')).toBe(true);
		const img = host.querySelector<HTMLImageElement>('.pad img')!;
		Object.defineProperty(img, 'naturalWidth', { configurable: true, value: 10 });
		Object.defineProperty(img, 'naturalHeight', { configurable: true, value: 10 });
		img.dispatchEvent(new Event('load'));
		await settle();
		expect(host.querySelector('.loadbar')).toBeNull();
		expect(host.querySelector('.viewport')!.classList.contains('pending')).toBe(false);
	});
});
