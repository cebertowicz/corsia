import { flushSync, mount, unmount, type ComponentProps } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Writable } from 'svelte/store';

const mocks = vi.hoisted(() => ({
	fetchComments: vi.fn(),
	fetchRevision: vi.fn(),
	send: vi.fn(),
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
	retryDocFeedback: mocks.retry,
}));
vi.mock('$lib/stores/viewport', async () => {
	const { writable: make } = await import('svelte/store');
	return { isMobile: make(false), prefersReducedMotion: make(false) };
});

import ImageCommentLightbox from './ImageCommentLightbox.svelte';
import DocFeedbackStatus from './DocFeedbackStatus.svelte';
import ImageViewer from './ImageViewer.svelte';
import ArtifactDocumentImageOverlay from './ArtifactDocumentImageOverlay.svelte';
import { DocFeedback } from '$lib/docFeedback.svelte';
import { docComments } from '$lib/stores/docComments';
import { isMobile } from '$lib/stores/viewport';

const SRC = '/api/files/content?path=%2Fws%2Fshot.png&raw=true';
const pending = (open_count: number, lanes: { doc?: number; image?: number } = {}) => ({
	owner_label: 'Design', undelivered_batches: [], open_count, document_held_count: lanes.doc ?? 0, image_held_count: lanes.image ?? 0,
});
const sentAll = (counts: ReturnType<typeof pending>, sent_count: number) => ({ woken: [], ...counts, sent_count });
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
function documentStatus(lane: 'document-held' | null = null, onFeedbackChanged?: () => void | Promise<void>) {
	const target = host();
	const component = mount(DocFeedbackStatus, { target, props: { artifactId: 'art-doc', targetInstanceId: 'tab-1', lane, onFeedbackChanged } }) as unknown as {
		controller: () => import('$lib/docFeedback.svelte').DocFeedback;
		sync: (id: string, feedback: ReturnType<typeof pending>) => void;
	};
	mounted.push(component as unknown as Record<string, unknown>);
	flushSync();
	return { target, component };
}
const sendIn = (element: ParentNode) => [...element.querySelectorAll<HTMLButtonElement>('button')].find((button) => /^Send/.test(button.textContent?.trim() ?? '')) ?? null;
/** The comments drawer has no Send footer: each held lane is sent from its own header or bar. */
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
	for (const mock of [mocks.send, mocks.retry]) mock.mockReset().mockResolvedValue({ woken: [], undelivered_batches: [] });
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
	it('keeps sending disabled until the owning reader settles a sync that overlapped the send', async () => {
		let finishRead!: () => void;
		const read = new Promise<void>((resolve) => { finishRead = resolve; });
		const doc = documentStatus('document-held', () => read);
		const feedback = doc.component.controller();
		doc.component.sync('art-doc', pending(1, { doc: 1 }));
		let finishSend!: (value: ReturnType<typeof sentAll>) => void;
		mocks.send.mockImplementationOnce(() => new Promise((resolve) => { finishSend = resolve; }));
		const sending = feedback.sendDocument();
		// A count read begun before the close can arrive during the send with its old count.
		doc.component.sync('art-doc', pending(1, { doc: 1 }));
		finishSend(sentAll(pending(0), 1));
		await settle();
		expect(feedback.sendingLane).toBe('document-held');
		expect(sendIn(doc.target)!.disabled).toBe(true);
		doc.component.sync('art-doc', pending(0));
		finishRead();
		await sending;
		flushSync();
		expect(feedback.sendingLane).toBeNull();
		expect(feedback.documentHeld).toBe(0);
		expect(sendIn(doc.target)).toBeNull();
	});
	for (const syncFirst of [true, false]) it(`keeps the reopened document header actionable when its sync lands ${syncFirst ? 'before' : 'after'} an image send result`, async () => {
		const refresh = vi.fn();
		const doc = documentStatus('document-held', refresh);
		const feedback = doc.component.controller();
		doc.component.sync('art-doc', pending(2, { image: 2 }));
		let finish!: (value: ReturnType<typeof sentAll>) => void;
		mocks.send.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
		const sending = feedback.sendImage();
		if (syncFirst) doc.component.sync('art-doc', pending(1, { doc: 1 }));
		finish(sentAll(pending(0), 2));
		await sending;
		if (!syncFirst) {
			expect(feedback.imageHeld).toBe(0);
			expect(feedback.documentHeld).toBe(0);
			doc.component.sync('art-doc', pending(1, { doc: 1 }));
		}
		flushSync();
		expect(feedback.documentHeld).toBe(1);
		expect(feedback.imageHeld).toBe(0);
		expect(sendIn(doc.target)!.textContent!.trim()).toBe('Send 1 comment');
		expect(sendIn(doc.target)!.disabled).toBe(false);
		expect(refresh).toHaveBeenCalledOnce();
		mocks.send.mockResolvedValueOnce(sentAll(pending(0), 1));
		sendIn(doc.target)!.click();
		await settle();
		expect(mocks.send).toHaveBeenLastCalledWith('art-doc', 'document-held', false);
		expect(feedback.documentHeld).toBe(0);
	});
	it('sends the document\'s image comments from the viewer bar, leaving its own comments for the document', async () => {
		const doc = documentStatus();
		doc.component.sync('art-doc', pending(3, { doc: 1, image: 2 }));
		mocks.fetchComments.mockResolvedValue({ comments: [onImage('here', 1)], feedback: pending(3, { doc: 1, image: 2 }) });
		mocks.send.mockResolvedValue(sentAll(pending(1, { doc: 1 }), 2));
		flushSync();
		// The document's status row is lane-less here, so it offers no Send; the viewer's bar is the image lane's one sender.
		expect(sendIn(doc.target)).toBeNull();

		const shown = await viewer({
			src: SRC, alt: 'Mock', artifactId: 'art-doc', docName: 'notes.md', onBack: vi.fn(),
			inDocument: { occurrence: { from: 10, to: 24 }, post: vi.fn(async () => 'new') }, feedback: doc.component.controller(),
		});
		const send = sendIn(shown.target.querySelector('.bar')!)!;
		expect(send.textContent!.trim()).toBe('Send 2 comments');
		expect(send.getAttribute('aria-label')).toBe('Send 2 comments to Design');
		send.click();
		await settle();

		expect(mocks.send).toHaveBeenCalledOnce();
		expect(mocks.send).toHaveBeenCalledWith('art-doc', 'image-held', false);
		expect(sendIn(shown.target.querySelector('.bar')!)).toBeNull();
		expect(doc.component.controller().documentHeld).toBe(1);
		expect(footerIn(shown.target)).toBeNull();
	});

	it('sends nothing when only the lightbox goes: leaving the image is the overlay\'s exit, not the viewer\'s unmount', async () => {
		const doc = documentStatus();
		doc.component.sync('art-doc', pending(2, { image: 2 }));
		const shown = await viewer({
			src: SRC, alt: 'Mock', artifactId: 'art-doc', onBack: vi.fn(),
			inDocument: { occurrence: { from: 10, to: 24 }, post: vi.fn(async () => 'new') }, feedback: doc.component.controller(),
		});
		unmount(shown.component);
		mounted = mounted.filter((component) => component !== shown.component);
		await settle();
		expect(mocks.send).not.toHaveBeenCalled();
	});

	it('leaving the image sends what its viewer still holds, once; nothing when nothing is held', async () => {
		const overlayOver = (feedback: DocFeedback) => {
			const target = host();
			const component = mount(ArtifactDocumentImageOverlay, {
				target,
				props: {
					image: { src: SRC, alt: 'Mock', from: 10, to: 24 },
					comments: { artifactId: 'art-doc', projectId: null, readOnly: false, post: vi.fn(async () => 'new'), feedback },
					target: { instanceId: 'tab-1', name: 'notes.md', path: '/ws/notes.md', projectId: null, threadId: null, artifactId: 'art-doc' },
					onBack: vi.fn(), onPlace: vi.fn(),
				} as unknown as ComponentProps<typeof ArtifactDocumentImageOverlay>,
			}) as Record<string, unknown>;
			flushSync();
			return component;
		};
		const held = new DocFeedback();
		held.show('art-doc', 'tab-1');
		held.sync('art-doc', { ...pending(2, { image: 2 }) });
		const open = overlayOver(held);
		mocks.send.mockResolvedValue(sentAll(pending(0), 2));
		unmount(open);
		await settle();
		expect(mocks.send).toHaveBeenCalledOnce();
		expect(mocks.send).toHaveBeenCalledWith('art-doc', 'image-held', false);

		mocks.send.mockClear();
		const empty = new DocFeedback();
		empty.show('art-doc', 'tab-1');
		empty.sync('art-doc', { ...pending(1, { doc: 1 }) });
		unmount(overlayOver(empty));
		await settle();
		expect(mocks.send).not.toHaveBeenCalled();
	});

	it('two exits of one image are one request, and a sender that lands second delivers nothing', async () => {
		const held = new DocFeedback();
		held.show('art-doc', 'tab-1');
		held.sync('art-doc', { ...pending(2, { image: 2 }) });
		mocks.send.mockResolvedValue(sentAll(pending(0), 2));
		await Promise.all([held.leaveImage(), held.sendImage(), held.leaveImage()]);
		expect(mocks.send).toHaveBeenCalledOnce();
		mocks.send.mockClear();
		mocks.send.mockResolvedValue(sentAll(pending(0), 0));
		held.sync('art-doc', { ...pending(2, { image: 2 }) });
		await held.sendImage();
		// The server found it already closed: nothing is claimed as sent.
		expect(held.sent).toBe(0);
		expect(held.imageHeld).toBe(0);
	});

	it('leaves nothing waiting to send after a comment made in the viewer, which goes at once', async () => {
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
		// A person's comment is delivered as it is made: no Send in the lightbox or the document's review surface.
		expect(sendIn(shown.target.querySelector('.bar')!)).toBeNull();
		expect(sendIn(doc.target)).toBeNull();
	});

	it('sends an image artifact\'s own comments from its bar, with no drawer footer', async () => {
		mocks.fetchComments.mockResolvedValue({ comments: [onImage('here', 1)], feedback: pending(1, { image: 1 }) });
		mocks.send.mockImplementation(async () => {
			mocks.fetchComments.mockResolvedValue({ comments: [onImage('here', 1)], feedback: pending(0) });
			return sentAll(pending(0), 1);
		});
		const shown = await imageTab();
		const send = sendIn(shown.target.querySelector('.bar')!)!;
		expect(send.textContent!.trim()).toBe('Send 1 comment');
		await openDrawer(shown.target);
		expect(footerIn(shown.target)).toBeNull();
		send.click();
		await settle();
		expect(mocks.send).toHaveBeenCalledOnce();
		expect(mocks.send).toHaveBeenCalledWith('art-img', 'image-held', false);
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

	it('reloading an image artifact keeps its batch held, and closing the tab sends it', async () => {
		mocks.fetchComments.mockResolvedValue({ comments: [onImage('here', 1)], feedback: pending(1, { image: 1 }) });
		mocks.send.mockResolvedValue(sentAll(pending(0), 1));
		const shown = await imageTab();
		expect(sendIn(shown.target)!.textContent!.trim()).toBe('Send 1 comment');

		mocks.fetchRevision.mockResolvedValue({ exists: true, path: '/ws/shot.png', revision: 'r-2', media_type: 'image/png', artifact_id: 'art-img' });
		shown.component.reload();
		await drawn(shown.target);

		// The reload remounts the lightbox, not the tab: it is not leaving, and the batch is still held.
		expect(mocks.send).not.toHaveBeenCalled();
		expect(sendIn(shown.target)!.textContent!.trim()).toBe('Send 1 comment');
		unmount(shown.component as unknown as Record<string, unknown>);
		mounted = mounted.filter((component) => component !== (shown.component as unknown));
		await settle();
		expect(mocks.send).toHaveBeenCalledOnce();
		expect(mocks.send).toHaveBeenCalledWith('art-img', 'image-held', false);
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
