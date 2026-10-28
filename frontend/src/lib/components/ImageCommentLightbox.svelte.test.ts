import { flushSync, mount, unmount, type ComponentProps } from 'svelte';
import type { Writable } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it, onTestFinished, vi } from 'vitest';

const mocks = vi.hoisted(() => ({ fetchComments: vi.fn() }));
vi.mock('$lib/ws', () => ({ on: vi.fn(() => () => {}) }));
vi.mock('$lib/api/docs', () => ({
	fetchComments: mocks.fetchComments,
	postComment: vi.fn(),
	patchComment: vi.fn(),
	persistPositions: vi.fn(),
}));
// An image artifact's viewer keeps its feedback batch (ImageCommentLightboxSend.svelte.test.ts covers it).
vi.mock('$lib/api/docFeedback', () => {
	const result = () => Promise.resolve({ woken: [], undelivered_batches: [], open_count: 0, document_held_count: 0, image_held_count: 0 });
	return { sendDocFeedback: vi.fn(result), retryDocFeedback: vi.fn(result) };
});
vi.mock('$lib/stores/viewport', async () => {
	const { writable: make } = await import('svelte/store');
	return { isMobile: make(false), prefersReducedMotion: make(false) };
});

import ImageCommentLightbox from './ImageCommentLightbox.svelte';
import { docComments } from '$lib/stores/docComments';
import { prefersReducedMotion } from '$lib/stores/viewport';

const SRC = '/api/files/content?path=%2Fws%2Fshot.png&raw=true';
const row = (id: string, number: number, x: number) => ({
	id, author: 'you', author_kind: 'user', body: id, created_at: '2026-10-10T00:00:00Z',
	region_x: x, region_y: 0.2, region_w: 0.2, region_h: 0.1, region_number: number, region_stale: false,
});
let host: HTMLElement;
let component: Record<string, unknown> | null = null;
let realRO: typeof ResizeObserver;
const realWidth = window.innerWidth;

const settle = async () => { for (let i = 0; i < 6; i++) { await Promise.resolve(); flushSync(); } };
function define(name: string, get: () => number) {
	Object.defineProperty(HTMLElement.prototype, name, { configurable: true, get });
}
async function render(props: ComponentProps<typeof ImageCommentLightbox>) {
	component = mount(ImageCommentLightbox, { target: host, props });
	await settle();
	const img = host.querySelector<HTMLImageElement>('.pad img')!;
	Object.defineProperty(img, 'naturalWidth', { configurable: true, value: 1000 });
	Object.defineProperty(img, 'naturalHeight', { configurable: true, value: 500 });
	img.dispatchEvent(new Event('load'));
	await settle();
}
const pill = () => host.querySelector<HTMLButtonElement>('[data-image-comments-pill]');
const commentButton = () => host.querySelector<HTMLButtonElement>('[data-image-comment]');

beforeEach(() => {
	realRO = globalThis.ResizeObserver;
	globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as unknown as typeof ResizeObserver;
	define('clientWidth', () => 548);
	define('clientHeight', () => 400);
	Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1440 });
	// jsdom reports zero sizes: give the image layer and its clipping canvas realistic bounds.
	const measured = HTMLElement.prototype.getBoundingClientRect;
	vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
		if (this.classList.contains('layout')) return new DOMRect(0, 0, 800, 600);
		return this.hasAttribute('data-image-regions') ? new DOMRect(24, 50, 500, 250) : measured.call(this);
	});
	docComments.set({});
	mocks.fetchComments.mockReset().mockResolvedValue({
		comments: [row('first', 1, 0.1), row('second', 3, 0.6)],
		feedback: { owner_label: null, undelivered_batches: [] },
	});
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
	Object.defineProperty(window, 'innerWidth', { configurable: true, value: realWidth });
	vi.restoreAllMocks();
});

describe('ImageCommentLightbox', () => {
	it('reads the image\'s comments, counts them on the pill and numbers their tags on the image', async () => {
		await render({ src: SRC, alt: 'Mock screen', artifactId: 'art-img' });
		expect(mocks.fetchComments).toHaveBeenCalledWith('art-img');
		expect(pill()!.getAttribute('aria-label')).toBe('2 open threads');
		// The tags sit inside the zoomed frame, so they pan and zoom with the image.
		const tags = [...host.querySelectorAll<HTMLElement>('.frame [data-region-tag]')];
		expect(tags.map((tag) => tag.dataset.regionTag)).toEqual(['1', '3']);
	});

	it('counts open threads on the pill, as the document\'s pill does: resolved ones are not counted', async () => {
		mocks.fetchComments.mockResolvedValue({
			comments: [row('first', 1, 0.1), row('second', 3, 0.6), { ...row('done', 2, 0.4), resolved: true }],
			feedback: { owner_label: null, undelivered_batches: [] },
		});
		await render({ src: SRC, alt: 'Mock screen', artifactId: 'art-img' });
		expect(pill()!.getAttribute('aria-label')).toBe('2 open threads');
		expect(pill()!.querySelector('.count')!.textContent).toBe('2');
		expect(pill()!.querySelector('.count-unit')!.textContent).toBe('threads');
		// Nothing waits to be sent, so no dot.
		expect(pill()!.querySelector('.unsent-dot')).toBeNull();
	});

	it('lists the image\'s comments without a thumbnail of the image already on screen', async () => {
		await render({ src: SRC, alt: 'Mock screen', artifactId: 'art-img' });
		pill()!.click();
		await settle();
		const drawer = host.querySelector<HTMLElement>('[data-doc-comments-drawer]')!;
		expect(drawer.querySelectorAll('[data-comment-id]')).toHaveLength(2);
		expect(drawer.querySelector('[data-region-thumb]')).toBeNull();
	});

	it('keeps Comment disabled until the image has loaded, like Fit and zoom', async () => {
		component = mount(ImageCommentLightbox, { target: host, props: { src: SRC, alt: 'Mock screen', artifactId: 'art-img' } });
		await settle();
		expect(commentButton()!.disabled).toBe(true);
		const img = host.querySelector<HTMLImageElement>('.pad img')!;
		Object.defineProperty(img, 'naturalWidth', { configurable: true, value: 1000 });
		Object.defineProperty(img, 'naturalHeight', { configurable: true, value: 500 });
		img.dispatchEvent(new Event('load'));
		await settle();
		expect(commentButton()!.disabled).toBe(false);
	});

	it('comments on the whole image from the keyboard, and holds the review bar while the comment has text', async () => {
		const props = $state({ src: SRC, alt: 'Mock screen', artifactId: 'art-img', draftOpen: false });
		await render(props);
		commentButton()!.click();
		await settle();
		expect(commentButton()!.getAttribute('aria-pressed')).toBe('true');
		const surface = host.querySelector<HTMLElement>('[data-image-regions] .surface')!;
		expect(document.activeElement).toBe(surface);

		surface.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }));
		await settle();
		// One comment per press of Comment.
		expect(commentButton()!.getAttribute('aria-pressed')).toBe('false');
		const textarea = document.body.querySelector<HTMLTextAreaElement>('textarea')!;
		expect(textarea).not.toBeNull();
		expect(props.draftOpen).toBe(false);
		textarea.value = 'The whole layout is cramped';
		textarea.dispatchEvent(new Event('input', { bubbles: true }));
		await settle();
		expect(props.draftOpen).toBe(true);

		// Escape closes the composer before it can reach the viewer's own Back.
		const escape = new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true });
		textarea.dispatchEvent(escape);
		await settle();
		expect(escape.defaultPrevented).toBe(true);
		expect(document.body.querySelector('textarea')).toBeNull();
		expect(props.draftOpen).toBe(false);
	});

	describe('the drawer and the image, linked', () => {
		let scrollTo: ReturnType<typeof vi.fn>;
		let scrollIntoView: ReturnType<typeof vi.fn>;
		beforeEach(() => {
			scrollTo = vi.fn();
			scrollIntoView = vi.fn();
			Object.defineProperty(Element.prototype, 'scrollTo', { configurable: true, value: scrollTo });
			Object.defineProperty(Element.prototype, 'scrollIntoView', { configurable: true, value: scrollIntoView });
		});
		afterEach(() => {
			delete (Element.prototype as unknown as Record<string, unknown>).scrollTo;
			delete (Element.prototype as unknown as Record<string, unknown>).scrollIntoView;
		});
		const drawer = () => host.querySelector<HTMLElement>('[data-doc-comments-drawer]');
		const card = (id: string) => drawer()!.querySelector<HTMLElement>(`[data-comment-id="${id}"]`)!;
		const tag = (n: number) => host.querySelector<HTMLButtonElement>(`[data-region-tag="${n}"]`)!;
		const box = (id: string) => host.querySelector<HTMLElement>(`[data-region-box="${id}"]`)!;
		async function openDrawer() { pill()!.click(); await settle(); }

		it('drops Show on image: each card selects its tag itself', async () => {
			await render({ src: SRC, alt: 'Mock screen', artifactId: 'art-img' });
			await openDrawer();
			expect(drawer()!.querySelector('[data-region-thumb]'), 'no thumbnail repeats the image on screen').toBeNull();
			expect(drawer()!.textContent).not.toContain('Show on image');
			card('second').querySelector<HTMLElement>('.ct')!.click();
			await settle();
			expect(drawer()).not.toBeNull();
			expect(tag(3).getAttribute('aria-pressed')).toBe('true');
			// The card's number is a button too, so the keyboard can select from the drawer.
			const number = card('first').querySelector<HTMLButtonElement>('button.ct-num')!;
			expect(number.getAttribute('aria-label')).toBe('Show comment 1 on the image');
			number.click();
			await settle();
			expect(tag(1).getAttribute('aria-pressed')).toBe('true');
		});

		it('highlights a card’s box and tag on hover or focus, and a tag’s card on hover, without scrolling', async () => {
			await render({ src: SRC, alt: 'Mock screen', artifactId: 'art-img' });
			await openDrawer();
			scrollTo.mockClear(); scrollIntoView.mockClear();
			card('second').dispatchEvent(new PointerEvent('pointerenter'));
			await settle();
			expect(box('second').classList.contains('shown')).toBe(true);
			expect(tag(3).classList.contains('faint')).toBe(false);
			card('second').dispatchEvent(new PointerEvent('pointerleave'));
			await settle();
			expect(box('second').classList.contains('shown')).toBe(false);
			card('first').dispatchEvent(new FocusEvent('focusin', { bubbles: true }));
			await settle();
			expect(box('first').classList.contains('shown')).toBe(true);
			tag(3).dispatchEvent(new PointerEvent('pointerenter'));
			await settle();
			expect(card('second').querySelector('.ct')!.classList.contains('linked')).toBe(true);
			expect(scrollTo).not.toHaveBeenCalled();
			expect(scrollIntoView).not.toHaveBeenCalled();
		});

		it('keeps the drawer open when a tag is picked, selects it, and scrolls the drawer smoothly to its card', async () => {
			await render({ src: SRC, alt: 'Mock screen', artifactId: 'art-img' });
			await openDrawer();
			scrollTo.mockClear();
			tag(3).dispatchEvent(new MouseEvent('click', { bubbles: true, detail: 0 }));
			await settle();
			expect(drawer()).not.toBeNull();
			expect(host.querySelector('[data-popover]')).toBeNull();
			expect(tag(3).getAttribute('aria-pressed')).toBe('true');
			expect(card('second').querySelector('.ct')!.classList.contains('focused')).toBe(true);
			expect(scrollTo).toHaveBeenCalledWith(expect.objectContaining({ behavior: 'smooth' }));
			expect(scrollIntoView).not.toHaveBeenCalled();
		});

		it('scrolls the drawer back to the card when its already selected tag is picked again', async () => {
			await render({ src: SRC, alt: 'Mock screen', artifactId: 'art-img' });
			await openDrawer();
			card('second').querySelector<HTMLElement>('.ct')!.click();
			await settle();
			scrollTo.mockClear();
			tag(3).dispatchEvent(new MouseEvent('click', { bubbles: true, detail: 0 }));
			await settle();
			expect(scrollTo).toHaveBeenCalledWith(expect.objectContaining({ behavior: 'smooth' }));
		});

		it('opens the drawer already at the selected comment’s card, marked, so only the drawer moves', async () => {
			await render({ src: SRC, alt: 'Mock screen', artifactId: 'art-img' });
			tag(3).dispatchEvent(new MouseEvent('click', { bubbles: true, detail: 0 }));
			await settle();
			scrollTo.mockClear();
			await openDrawer();
			expect(card('second').querySelector('.ct')!.classList.contains('focused')).toBe(true);
			expect(scrollTo).toHaveBeenCalledWith(expect.objectContaining({ behavior: 'instant' }));
			expect(scrollTo).not.toHaveBeenCalledWith(expect.objectContaining({ behavior: 'smooth' }));
		});

		it('lands keyboard focus on the card’s Reply field when a tag is picked', async () => {
			await render({ src: SRC, alt: 'Mock screen', artifactId: 'art-img' });
			tag(1).focus();
			tag(1).dispatchEvent(new MouseEvent('click', { bubbles: true, detail: 0 }));
			await settle();
			expect(document.activeElement?.classList.contains('ct-input')).toBe(true);
		});

		it('does not focus a reply whose anchor is outside the canvas', async () => {
			await render({ src: SRC, alt: 'Mock screen', artifactId: 'art-img' });
			const root = host.querySelector<HTMLElement>('.layout')!;
			Object.defineProperty(root, 'getBoundingClientRect', { configurable: true, value: () => new DOMRect(0, 0, 20, 20) });
			tag(1).focus();
			tag(1).dispatchEvent(new MouseEvent('click', { bubbles: true, detail: 0 }));
			await settle();
			expect(document.body.querySelector('[data-popover]')?.getAttribute('aria-hidden')).toBe('true');
			expect(document.activeElement?.classList.contains('ct-input')).toBe(false);
		});

		it('opens the drawer by growing its track, sized to leave the image its fitted height', async () => {
			await render({ src: SRC, alt: 'Mock screen', artifactId: 'art-img' });
			const layout = host.querySelector<HTMLElement>('.layout')!;
			expect(layout.classList.contains('drawer-open')).toBe(false);
			await openDrawer();
			expect(layout.classList.contains('drawer-open')).toBe(true);
			expect(layout.style.getPropertyValue('--drawer-size')).toMatch(/^\d+(\.\d+)?px$/);
			pill()!.click();
			await settle();
			expect(layout.classList.contains('drawer-open')).toBe(false);
		});

	});

	describe('the image while the drawer closes', () => {
		const reduced = prefersReducedMotion as unknown as Writable<boolean>;
		let instant: typeof Element.prototype.animate;
		beforeEach(() => {
			// jsdom's stand-in animations end at once; these end after their duration, on timers the test advances.
			instant = Element.prototype.animate;
			Element.prototype.animate = function animate(_keyframes, options) {
				const duration = typeof options === 'number' ? options : Number(options?.duration ?? 0);
				const animation = { cancel() {}, finish() {}, currentTime: 0, playState: 'running' } as unknown as Animation;
				Object.defineProperty(animation, 'onfinish', {
					set(callback: ((event: Event) => void) | null) {
						if (callback) setTimeout(() => callback(new Event('finish')), duration);
					},
				});
				return animation;
			};
			vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
			Object.defineProperty(Element.prototype, 'scrollTo', { configurable: true, value: vi.fn() });
			Object.defineProperty(Element.prototype, 'scrollIntoView', { configurable: true, value: vi.fn() });
			document.documentElement.style.setProperty('--dur-tight', '120ms');
			document.documentElement.style.setProperty('--dur-drawer', '320ms');
		});
		afterEach(() => {
			Element.prototype.animate = instant;
			vi.useRealTimers();
			delete (Element.prototype as unknown as Record<string, unknown>).scrollTo;
			delete (Element.prototype as unknown as Record<string, unknown>).scrollIntoView;
			reduced.set(false);
			document.documentElement.style.removeProperty('--dur-tight');
			document.documentElement.style.removeProperty('--dur-drawer');
		});
		// A sheet is placed outside the viewer, so the drawer is looked up in the whole document.
		const drawer = () => document.querySelector('[data-doc-comments-drawer]');
		/** Opens the drawer, closes it from the pill, and returns the layout and every change of its open track since the close. */
		async function closeAfterOpening(width: number) {
			Object.defineProperty(window, 'innerWidth', { configurable: true, value: width });
			await render({ src: SRC, alt: 'Mock screen', artifactId: 'art-img' });
			const layout = host.querySelector<HTMLElement>('.layout')!;
			pill()!.click();
			await settle();
			await vi.advanceTimersByTimeAsync(400);
			await settle();
			expect(drawer()).not.toBeNull();
			// A pane-sized canvas takes the drawer in a sheet; a wide one beside the image.
			expect(drawer()!.closest('.drawer-panel') !== null).toBe(width <= 1024);
			expect(layout.classList.contains('drawer-open')).toBe(true);
			const changes: boolean[] = [];
			const watch = new MutationObserver(() => changes.push(layout.classList.contains('drawer-open')));
			watch.observe(layout, { attributes: true, attributeFilter: ['class'] });
			pill()!.click();
			await settle();
			await vi.advanceTimersByTimeAsync(40);
			await settle();
			return { layout, changes, watch };
		}

		for (const [form, width] of [['beside the image', 1440], ['in a sheet', 393]] as const) {
			it(`with reduced motion, holds the image in place while the drawer ${form} fades, then moves it once`, async () => {
				reduced.set(true);
				const { layout, changes, watch } = await closeAfterOpening(width);
				// Mid-crossfade: the drawer is still on screen, so its track is still open and the image has not moved.
				expect(drawer()).not.toBeNull();
				expect(layout.classList.contains('drawer-open')).toBe(true);
				await vi.advanceTimersByTimeAsync(300);
				await settle();
				// The drawer has gone: the track closed once, as it left, and not before.
				expect(drawer()).toBeNull();
				expect(layout.classList.contains('drawer-open')).toBe(false);
				watch.disconnect();
				expect(changes).toEqual([false]);
			});
		}

		it('in full motion, closes the track as the close commits, so the image glides back alongside the drawer\'s outro', async () => {
			const { layout, changes, watch } = await closeAfterOpening(1440);
			expect(drawer()).not.toBeNull();
			expect(layout.classList.contains('drawer-open')).toBe(false);
			await vi.advanceTimersByTimeAsync(400);
			await settle();
			expect(drawer()).toBeNull();
			watch.disconnect();
			expect(changes).toEqual([false]);
		});
	});

	it('shows the image alone until its artifact is known', async () => {
		await render({ src: SRC, alt: 'Mock screen', artifactId: null });
		expect(mocks.fetchComments).not.toHaveBeenCalled();
		expect(commentButton()).toBeNull();
		expect(pill()).toBeNull();
		expect(host.querySelector('[data-image-regions]')).toBeNull();
	});

	it('keeps the count and the tags but offers no Comment button when read-only', async () => {
		await render({ src: SRC, alt: 'Mock screen', artifactId: 'art-img', readOnly: true });
		expect(commentButton()).toBeNull();
		expect(pill()!.getAttribute('aria-label')).toBe('2 open threads');
		expect(host.querySelectorAll('[data-region-tag]')).toHaveLength(2);
	});

	it('reads the comments again when a new revision of the image arrives, so pins on the old one show as stale', async () => {
		const props = $state({ src: SRC, alt: 'Mock screen', artifactId: 'art-img' });
		await render(props);
		expect(mocks.fetchComments).toHaveBeenCalledTimes(1);
		mocks.fetchComments.mockResolvedValueOnce({
			comments: [{ ...row('first', 1, 0.1), region_stale: true }],
			feedback: { owner_label: null, undelivered_batches: [] },
		});
		props.src = `${SRC}&v=2`;
		await settle();
		expect(mocks.fetchComments).toHaveBeenCalledTimes(2);
	});

	describe('an image inside a document', () => {
		const onImageAt = (id: string, number: number, x: number, from: number) => ({
			...row(id, number, x), anchor_text: '![Mock](a.png)', from_off: from, to_off: from + 14, image_src: '/ws/a.png',
		});
		beforeEach(() => {
			mocks.fetchComments.mockResolvedValue({
				comments: [
					onImageAt('here', 1, 0.1, 10), onImageAt('also-here', 2, 0.6, 10), onImageAt('same-file-later', 1, 0.3, 40),
					{ id: 'text', author: 'you', author_kind: 'user', body: 'A text comment', created_at: '2026-10-10T00:00:00Z', anchor_text: '![', from_off: 10, to_off: 12 },
				],
				feedback: { owner_label: null, undelivered_batches: [] },
			});
		});
		const inDocument = (post = vi.fn(async () => 'new')) => ({ occurrence: { from: 10, to: 24 }, post });

		it('shows only the comments on this image, with the way back to the document', async () => {
			await render({ src: SRC, alt: 'Mock', artifactId: 'art-doc', docName: 'notes.md', onBack: vi.fn(), inDocument: inDocument() });
			expect(mocks.fetchComments).toHaveBeenCalledWith('art-doc');
			expect(pill()!.getAttribute('aria-label')).toBe('2 open threads');
			expect(host.querySelectorAll('[data-region-tag]')).toHaveLength(2);
			expect(host.querySelector('button[aria-label="Back to notes.md"]')).not.toBeNull();
		});

		it('posts a new comment through the document, anchored on the image', async () => {
			const post = vi.fn(async () => 'new');
			await render({ src: SRC, alt: 'Mock', artifactId: 'art-doc', inDocument: inDocument(post) });
			commentButton()!.click();
			await settle();
			host.querySelector<HTMLElement>('[data-image-regions] .surface')!.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }));
			await settle();
			const textarea = document.body.querySelector<HTMLTextAreaElement>('textarea')!;
			textarea.value = 'Too dark';
			textarea.dispatchEvent(new Event('input', { bubbles: true }));
			await settle();
			document.body.querySelector<HTMLButtonElement>('button.submit')!.click();
			await settle();
			expect(post).toHaveBeenCalledWith({ x: 0, y: 0, w: 1, h: 1 }, 'Too dark');
		});

		it('opens at the comment it was opened on, its card at its tag', async () => {
			for (const name of ['scrollTo', 'scrollIntoView']) {
				Object.defineProperty(Element.prototype, name, { configurable: true, value: vi.fn() });
				onTestFinished(() => { delete (Element.prototype as unknown as Record<string, unknown>)[name]; });
			}
			await render({ src: SRC, alt: 'Mock', artifactId: 'art-doc', inDocument: inDocument(), focus: { id: 'also-here' } });
			expect(host.querySelector('[data-region-tag="2"]')!.getAttribute('aria-pressed')).toBe('true');
			expect(document.body.querySelector('[data-popover] [data-comment-id="also-here"], [data-popover] .ct')).not.toBeNull();
		});

		it('opened to move a stale pin, starts placing it again', async () => {
			await render({ src: SRC, alt: 'Mock', artifactId: 'art-doc', inDocument: inDocument(), focus: { id: 'here', move: true } });
			expect(commentButton()!.getAttribute('aria-pressed')).toBe('true');
		});
	});
});
