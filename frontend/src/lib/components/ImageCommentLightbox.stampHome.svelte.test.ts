import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import type { CommentReply } from '$lib/stores/docCommentProjection';

const mocks = vi.hoisted(() => ({ fetchComments: vi.fn(), markCommentThreadViewed: vi.fn() }));
vi.mock('$lib/ws', () => ({ on: vi.fn(() => () => {}) }));
vi.mock('$lib/api/docs', async (importOriginal) => ({
	...(await importOriginal<typeof import('$lib/api/docs')>()),
	fetchComments: mocks.fetchComments,
	markCommentThreadViewed: mocks.markCommentThreadViewed,
	postComment: vi.fn(),
	patchComment: vi.fn(),
	persistPositions: vi.fn(),
}));
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

const SRC = '/api/files/content?path=%2Fws%2Fshot.png&raw=true';
/** An image thread the person started, with an agent's answer the server names as unread. */
const row = (id: string, number: number, newReplies: string[]) => ({
	id, author: 'you', author_kind: 'user', body: id, created_at: '2026-10-10T00:00:00Z',
	region_x: 0.1, region_y: 0.2, region_w: 0.2, region_h: 0.1, region_number: number, region_stale: false,
	new_reply_ids: newReplies,
	replies: [{ id: `${id}-a1`, author: 'Browser', author_kind: 'agent', body: 'first answer', created_at: '2026-10-10T00:01:00Z' }],
});
const byArtifact: Record<string, unknown[]> = { 'image-one': [row('first', 1, ['first-a1'])], 'image-two': [row('other', 1, [])] };
let host: HTMLElement;
let component: Record<string, unknown> | null = null;
let realRO: typeof ResizeObserver;
const realWidth = window.innerWidth;
const settle = async () => { for (let i = 0; i < 6; i++) { await Promise.resolve(); flushSync(); } };
function define(name: string, get: () => number) {
	Object.defineProperty(HTMLElement.prototype, name, { configurable: true, get });
}
async function loadImage() {
	const img = host.querySelector<HTMLImageElement>('.pad img')!;
	Object.defineProperty(img, 'naturalWidth', { configurable: true, value: 1000 });
	Object.defineProperty(img, 'naturalHeight', { configurable: true, value: 500 });
	img.dispatchEvent(new Event('load'));
	await settle();
}

beforeEach(() => {
	realRO = globalThis.ResizeObserver;
	globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as unknown as typeof ResizeObserver;
	define('clientWidth', () => 548);
	define('clientHeight', () => 400);
	Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1440 });
	Object.defineProperty(Element.prototype, 'scrollTo', { configurable: true, value: vi.fn() });
	Object.defineProperty(Element.prototype, 'scrollIntoView', { configurable: true, value: vi.fn() });
	const measured = HTMLElement.prototype.getBoundingClientRect;
	vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
		if (this.classList.contains('layout')) return new DOMRect(0, 0, 800, 600);
		return this.hasAttribute('data-image-regions') ? new DOMRect(24, 50, 500, 250) : measured.call(this);
	});
	docComments.set({});
	mocks.fetchComments.mockReset().mockImplementation(async (artifactId: string) => ({ comments: byArtifact[artifactId] ?? [], feedback: { owner_label: null, undelivered_batches: [] } }));
	mocks.markCommentThreadViewed.mockReset().mockImplementation(async (_artifact: string, rootId: string) => ({ root_id: rootId, new_reply_ids: [] }));
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
	delete (Element.prototype as unknown as Record<string, unknown>).scrollTo;
	delete (Element.prototype as unknown as Record<string, unknown>).scrollIntoView;
	Object.defineProperty(window, 'innerWidth', { configurable: true, value: realWidth });
	docComments.set({});
	vi.restoreAllMocks();
});

it('reads a reply that arrived while an image card was open on the image it lives on, when the viewer moves to another image', async () => {
	// ImageViewer keeps one lightbox and hands it the image now shown.
	const props = $state({ src: SRC, alt: 'Mock screen', artifactId: 'image-one' });
	component = mount(ImageCommentLightbox, { target: host, props });
	await settle();
	await loadImage();
	host.querySelector<HTMLButtonElement>('[data-image-comments-pill]')!.click();
	await settle();
	// The person presses the card: it is picked and reads the unread answer.
	host.querySelector<HTMLElement>('[data-doc-comments-drawer] [data-comment-id="first"] .ct')!.click();
	await settle();
	await vi.waitFor(() => expect(mocks.markCommentThreadViewed.mock.calls).toEqual([['image-one', 'first', 'first-a1']]));
	// An answer arrives while it is open: arriving reads nothing.
	const createdAt = '2026-10-10T00:05:00Z';
	const reply: CommentReply = { id: 'first-a2', author: 'Browser', authorKind: 'agent', text: 'a later answer', at: 'now', createdAt };
	docComments.update((all) => ({ ...all, 'image-one': all['image-one'].map((comment) => comment.id === 'first' ? { ...comment, newReplyIds: ['first-a2'], replies: [...(comment.replies ?? []), reply] } : comment) }));
	await settle();
	expect(mocks.markCommentThreadViewed).toHaveBeenCalledTimes(1);
	// The viewer moves to another image: the open card leaves and reads on its own image.
	props.artifactId = 'image-two';
	await settle();
	await vi.waitFor(() => expect(mocks.markCommentThreadViewed).toHaveBeenCalledTimes(2));
	expect(mocks.markCommentThreadViewed.mock.calls[1]).toEqual(['image-one', 'first', 'first-a2']);
});
