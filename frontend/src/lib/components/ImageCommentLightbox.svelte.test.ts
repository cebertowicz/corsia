import { flushSync, mount, unmount, type ComponentProps } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({ fetchComments: vi.fn() }));
vi.mock('$lib/ws', () => ({ on: vi.fn(() => () => {}) }));
vi.mock('$lib/api/docs', () => ({
	fetchComments: mocks.fetchComments,
	postComment: vi.fn(),
	patchComment: vi.fn(),
	persistPositions: vi.fn(),
}));
vi.mock('$lib/stores/viewport', async () => {
	const { writable: make } = await import('svelte/store');
	return { isMobile: make(false), prefersReducedMotion: make(false) };
});

import ImageCommentLightbox from './ImageCommentLightbox.svelte';
import { docComments } from '$lib/stores/docComments';

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
	// Tags are placed against the layer's measured size, which jsdom reports as zero.
	const measured = HTMLElement.prototype.getBoundingClientRect;
	vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
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
		expect(pill()!.getAttribute('aria-label')).toBe('2 comments');
		// The tags sit inside the zoomed frame, so they pan and zoom with the image.
		const tags = [...host.querySelectorAll<HTMLElement>('.frame [data-region-tag]')];
		expect(tags.map((tag) => tag.dataset.regionTag)).toEqual(['1', '3']);
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
		const textarea = host.querySelector<HTMLTextAreaElement>('textarea')!;
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
		expect(host.querySelector('textarea')).toBeNull();
		expect(props.draftOpen).toBe(false);
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
		expect(pill()!.getAttribute('aria-label')).toBe('2 comments');
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
});
