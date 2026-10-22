import { flushSync, mount, unmount, type ComponentProps } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Writable } from 'svelte/store';

const mocks = vi.hoisted(() => ({
	fetchComments: vi.fn(),
	fetchRevision: vi.fn(),
	send: vi.fn(),
	leave: vi.fn(),
	ret: vi.fn(),
	retry: vi.fn(),
}));
vi.mock('$lib/ws', () => ({ on: vi.fn(() => () => {}) }));
vi.mock('$lib/api/docs', () => ({
	fetchComments: mocks.fetchComments,
	fetchRevision: mocks.fetchRevision,
	postComment: vi.fn(),
	patchComment: vi.fn(),
	persistPositions: vi.fn(),
}));
vi.mock('$lib/api/docFeedback', () => ({
	sendDocFeedback: mocks.send,
	leaveDocFeedback: mocks.leave,
	returnDocFeedback: mocks.ret,
	retryDocFeedback: mocks.retry,
}));
vi.mock('$lib/stores/viewport', async () => {
	const { writable: make } = await import('svelte/store');
	return { isMobile: make(false), prefersReducedMotion: make(false) };
});

import ImageCommentLightbox from './ImageCommentLightbox.svelte';
import DocFeedbackStatus from './DocFeedbackStatus.svelte';
import ImageViewer from './ImageViewer.svelte';
import { docComments } from '$lib/stores/docComments';
import { isMobile } from '$lib/stores/viewport';

const SRC = '/api/files/content?path=%2Fws%2Fshot.png&raw=true';
const pending = (open_count: number) => ({ owner_label: 'Design', grace_closes_at: null, undelivered_batches: [], open_count });
const onImage = (id: string, number: number) => ({
	id, author: 'you', author_kind: 'user', body: id, created_at: '2026-10-10T00:00:00Z',
	region_x: 0.1, region_y: 0.2, region_w: 0.2, region_h: 0.1, region_number: number, region_stale: false,
	anchor_text: '![Mock](a.png)', from_off: 10, to_off: 24, image_src: '/ws/a.png',
});
const settle = async () => { for (let i = 0; i < 6; i++) { await Promise.resolve(); flushSync(); } };

let hosts: HTMLElement[] = [];
let mounted: Record<string, unknown>[] = [];
let realRO: typeof ResizeObserver;
const realWidth = window.innerWidth;
function host() {
	const element = document.createElement('div');
	document.body.appendChild(element);
	hosts.push(element);
	return element;
}
async function drawn(target: HTMLElement) {
	await settle();
	const img = target.querySelector<HTMLImageElement>('.pad img')!;
	Object.defineProperty(img, 'naturalWidth', { configurable: true, value: 1000 });
	Object.defineProperty(img, 'naturalHeight', { configurable: true, value: 500 });
	img.dispatchEvent(new Event('load'));
	await settle();
}
async function viewer(props: ComponentProps<typeof ImageCommentLightbox>) {
	const target = host();
	const component = mount(ImageCommentLightbox, { target, props });
	mounted.push(component);
	await drawn(target);
	return { target, component };
}
/** An image artifact in its own tab, which holds the image's batch for as long as the tab is open. */
async function imageTab() {
	const target = host();
	const component = mount(ImageViewer, {
		target,
		props: { target: { instanceId: 'tab-img', path: '/ws/shot.png', name: 'shot.png', projectId: null, threadId: null, artifactId: 'art-img' } },
	}) as unknown as { reload: () => void };
	mounted.push(component as unknown as Record<string, unknown>);
	await drawn(target);
	return { target, component };
}
/** The document's own status row, which owns its batch while the viewer is over it. */
function documentStatus() {
	const target = host();
	const component = mount(DocFeedbackStatus, { target, props: { artifactId: 'art-doc', targetInstanceId: 'tab-1' } }) as unknown as {
		controller: () => import('$lib/docFeedback.svelte').DocFeedback;
		sync: (id: string, feedback: ReturnType<typeof pending>) => void;
	};
	mounted.push(component as unknown as Record<string, unknown>);
	flushSync();
	return { target, component };
}
const sendIn = (element: ParentNode) => [...element.querySelectorAll<HTMLButtonElement>('button')].find((button) => /^Send/.test(button.textContent?.trim() ?? '')) ?? null;
/** The comments drawer's pinned footer, where an unsent batch is named and sent (RSR-5). */
const footerIn = (element: ParentNode) => element.querySelector<HTMLElement>('[data-drawer-unsent]');
async function openDrawer(target: HTMLElement) {
	target.querySelector<HTMLButtonElement>('[data-image-comments-pill]')!.click();
	await settle();
}

beforeEach(() => {
	realRO = globalThis.ResizeObserver;
	globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as unknown as typeof ResizeObserver;
	for (const [name, size] of [['clientWidth', 548], ['clientHeight', 400]] as const) {
		Object.defineProperty(HTMLElement.prototype, name, { configurable: true, get: () => size });
	}
	Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1440 });
	// The composer opens against the layer's measured size, which jsdom reports as zero.
	const measured = HTMLElement.prototype.getBoundingClientRect;
	vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
		return this.hasAttribute('data-image-regions') ? new DOMRect(24, 50, 500, 250) : measured.call(this);
	});
	Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' });
	docComments.set({});
	for (const mock of [mocks.send, mocks.leave, mocks.ret, mocks.retry]) mock.mockReset().mockResolvedValue({ woken: [], undelivered_batches: [] });
	mocks.fetchComments.mockReset().mockResolvedValue({ comments: [onImage('here', 1)], feedback: pending(0) });
	mocks.fetchRevision.mockReset().mockResolvedValue({ exists: true, path: '/ws/shot.png', revision: 'r-1', media_type: 'image/png', artifact_id: 'art-img' });
});
afterEach(() => {
	for (const component of mounted.reverse()) unmount(component);
	mounted = [];
	for (const element of hosts) element.remove();
	hosts = [];
	globalThis.ResizeObserver = realRO;
	delete (HTMLElement.prototype as unknown as Record<string, unknown>).clientWidth;
	delete (HTMLElement.prototype as unknown as Record<string, unknown>).clientHeight;
	Object.defineProperty(window, 'innerWidth', { configurable: true, value: realWidth });
	(isMobile as Writable<boolean>).set(false);
	vi.restoreAllMocks();
});

describe('Send in the image viewer (DCOL-FR13.12)', () => {
	it('keeps send controls in the document review surface, and going Back is not leaving it', async () => {
		const doc = documentStatus();
		doc.component.sync('art-doc', pending(2));
		mocks.fetchComments.mockResolvedValue({ comments: [onImage('here', 1)], feedback: pending(2) });
		flushSync();
		expect(sendIn(doc.target)!.textContent!.trim()).toBe('Send 2 comments to Design');

		const shown = await viewer({
			src: SRC, alt: 'Mock', artifactId: 'art-doc', docName: 'notes.md', onBack: vi.fn(),
			inDocument: { occurrence: { from: 10, to: 24 }, post: vi.fn(async () => 'new') }, feedback: doc.component.controller(),
		});
		// The in-document lightbox has no review controls; the document bar remains the one place to send.
		expect(sendIn(shown.target.querySelector('.bar')!)).toBeNull();
		expect(sendIn(doc.target)!.textContent!.trim()).toBe('Send 2 comments to Design');

		doc.component.sync('art-doc', pending(1));
		flushSync();
		unmount(shown.component);
		mounted = mounted.filter((component) => component !== shown.component);
		await settle();
		expect(mocks.leave).not.toHaveBeenCalled();
		expect(sendIn(doc.target)!.textContent!.trim()).toBe('Send 1 comment to Design');
	});

	it('counts a comment made in the viewer into the document’s batch', async () => {
		const doc = documentStatus();
		const shown = await viewer({
			src: SRC, alt: 'Mock', artifactId: 'art-doc',
			inDocument: { occurrence: { from: 10, to: 24 }, post: vi.fn(async () => 'new') }, feedback: doc.component.controller(),
		});
		shown.target.querySelector<HTMLButtonElement>('[data-image-comment]')!.click();
		await settle();
		shown.target.querySelector<HTMLElement>('[data-image-regions] .surface')!.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }));
		await settle();
		const textarea = document.body.querySelector<HTMLTextAreaElement>('textarea')!;
		textarea.value = 'Too dark';
		textarea.dispatchEvent(new Event('input', { bubbles: true }));
		await settle();
		document.body.querySelector<HTMLButtonElement>('button.submit')!.click();
		await settle();
		// The document’s review surface owns the pending batch; the in-document lightbox has no duplicate control.
		expect(sendIn(shown.target.querySelector('.bar')!)).toBeNull();
		expect(sendIn(doc.target)!.textContent!.trim()).toBe('Send 1 comment to Design');
	});

	it('keeps an image artifact’s own batch, sent to its owner from the drawer footer, never the bar', async () => {
		mocks.fetchComments.mockResolvedValue({ comments: [onImage('here', 1)], feedback: pending(1) });
		const shown = await imageTab();
		expect(sendIn(shown.target.querySelector('.bar')!), 'the bar has no Send (RSR-5)').toBeNull();
		await openDrawer(shown.target);
		const footer = footerIn(shown.target)!;
		expect(footer.querySelector('[role="status"]')!.textContent).toBe('1 message not sent yet · sending to Design shortly.');
		const send = [...footer.querySelectorAll('button')].find((button) => button.textContent!.trim() === 'Send now')!;
		send.click();
		await settle();
		expect(mocks.send).toHaveBeenCalledOnce();
		expect(mocks.send).toHaveBeenCalledWith('art-img');
		// One batch: the footer and the bar's status read the same batch, so nothing is left to send in either.
		expect(footerIn(shown.target)).toBeNull();
		expect(sendIn(shown.target)).toBeNull();
	});

	it('marks the pill while the image artifact\'s own batch has messages not sent yet, with the drawer closed', async () => {
		mocks.fetchComments.mockResolvedValue({ comments: [onImage('here', 1), { ...onImage('done', 2), resolved: true }], feedback: pending(3) });
		const shown = await imageTab();
		const pill = shown.target.querySelector<HTMLButtonElement>('[data-image-comments-pill]')!;
		expect(pill.getAttribute('aria-expanded')).toBe('false');
		expect(pill.getAttribute('aria-label')).toBe('1 open thread, 3 messages not sent yet');
		expect(pill.querySelector('.count-unit')!.textContent).toBe('thread');
		expect(pill.querySelector('.unsent-dot')).not.toBeNull();
	});

	it('marks the in-document lightbox\'s pill from the document\'s batch, counting this image\'s open threads', async () => {
		mocks.fetchComments.mockResolvedValue({ comments: [onImage('here', 1), onImage('there', 2), { ...onImage('done', 3), resolved: true }], feedback: pending(2) });
		const doc = documentStatus();
		doc.component.sync('art-doc', pending(2));
		const shown = await viewer({
			src: SRC, alt: 'Mock', artifactId: 'art-doc',
			inDocument: { occurrence: { from: 10, to: 24 }, post: vi.fn(async () => 'new') }, feedback: doc.component.controller(),
		});
		const pill = shown.target.querySelector<HTMLButtonElement>('[data-image-comments-pill]')!;
		expect(pill.getAttribute('aria-label')).toBe('2 open threads, 2 messages not sent yet');
		expect(pill.querySelector('.unsent-dot')).not.toBeNull();
		doc.component.sync('art-doc', pending(0));
		flushSync();
		expect(pill.getAttribute('aria-label')).toBe('2 open threads');
		expect(pill.querySelector('.unsent-dot')).toBeNull();
	});

	it('shows nothing to send until comments are pending', async () => {
		const shown = await imageTab();
		await openDrawer(shown.target);
		expect(sendIn(shown.target)).toBeNull();
		expect(footerIn(shown.target)).toBeNull();
	});

	it('reloading an image artifact is not leaving it: its batch stays pending', async () => {
		mocks.fetchComments.mockResolvedValue({ comments: [onImage('here', 1)], feedback: pending(1) });
		const shown = await imageTab();
		await openDrawer(shown.target);
		expect(footerIn(shown.target)!.textContent).toContain('1 message not sent yet');

		mocks.fetchRevision.mockResolvedValue({ exists: true, path: '/ws/shot.png', revision: 'r-2', media_type: 'image/png', artifact_id: 'art-img' });
		shown.component.reload();
		await drawn(shown.target);

		expect(mocks.send).not.toHaveBeenCalled();
		// The reload redraws the viewer with its drawer closed; reopened, it still names the pending batch.
		await openDrawer(shown.target);
		expect(footerIn(shown.target)!.textContent).toContain('1 message not sent yet');
		// Closing the tab is leaving it.
		unmount(shown.component as unknown as Record<string, unknown>);
		mounted = mounted.filter((component) => component !== (shown.component as unknown));
		await settle();
		expect(mocks.send).toHaveBeenCalledWith('art-img');
	});

	it('on a phone, keeps the in-document lightbox bar free of review controls', async () => {
		(isMobile as Writable<boolean>).set(true);
		mocks.fetchComments.mockResolvedValue({ comments: [onImage('here', 1)], feedback: pending(2) });
		const doc = documentStatus();
		doc.component.sync('art-doc', pending(2));
		const shown = await viewer({
			src: SRC, alt: 'Mock', artifactId: 'art-doc', docName: 'notes.md', onBack: vi.fn(),
			inDocument: { occurrence: { from: 10, to: 24 }, post: vi.fn(async () => 'new') }, feedback: doc.component.controller(),
		});
		const bar = shown.target.querySelector('.bar')!;
		expect(sendIn(bar)).toBeNull();
		// The document's own drawer is the one place to send, so the viewer's drawer has no footer either.
		await openDrawer(shown.target);
		expect(footerIn(shown.target)).toBeNull();
		expect(bar.querySelector('[data-image-comment]')).not.toBeNull();
		expect(bar.querySelector('[data-image-comments-pill]')).not.toBeNull();
		expect([...bar.querySelectorAll('button')].map((button) => button.textContent!.trim())).toEqual(expect.arrayContaining(['Back', 'Fit']));
	});
});
