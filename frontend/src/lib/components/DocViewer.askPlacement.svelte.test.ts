import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import DocViewer from './DocViewer.svelte';
import DocumentBarTestHost from './DocumentBarTestHost.test.svelte';
import type { DocViewerTarget } from '$lib/docViewerTarget';
import type { ReviewBarRead } from '$lib/api/reviewBar';
import { connectionState } from '$lib/stores/connection';
import { clearDraft } from '$lib/stores/docSaveQueue';

const api = vi.hoisted(() => ({ fetchContent: vi.fn(), fetchRevision: vi.fn(), fetchComments: vi.fn() }));
const review = vi.hoisted(() => ({ fetchReviewBar: vi.fn(), answerReview: vi.fn() }));
const phone = vi.hoisted(() => ({ isMobile: null as unknown as import('svelte/store').Writable<boolean>, isShortTouch: null as unknown as import('svelte/store').Writable<boolean> }));
vi.mock('$lib/api/docs', async importOriginal => ({ ...await importOriginal<typeof import('$lib/api/docs')>(), ...api }));
vi.mock('$lib/api/reviewBar', async importOriginal => ({ ...await importOriginal<typeof import('$lib/api/reviewBar')>(), ...review }));
vi.mock('$lib/stores/deviceDocument', () => ({ DeviceDocument: class {
	ready = Promise.resolve(); saved = true; error = ''; destroy = async () => {};
	subscribe(listener: (copy: unknown) => void) { listener(this); return () => {}; }
} }));
vi.mock('$lib/stores/liveDocuments', async importOriginal => ({ ...await importOriginal<typeof import('$lib/stores/liveDocuments')>(), openLiveDocument: vi.fn(async () => null) }));
vi.mock('$lib/stores/viewport', async importOriginal => {
	const { writable: store } = await import('svelte/store');
	phone.isMobile = store(false);
	phone.isShortTouch = store(false);
	return { ...await importOriginal<typeof import('$lib/stores/viewport')>(), isMobile: phone.isMobile, isShortTouch: phone.isShortTouch };
});
vi.mock('$lib/stores/deviceRecovery', () => ({ readDeviceRecovery: async () => [] }));

const ASK: ReviewBarRead = {
	ask_raised: true, hold_ids: ['h1'], ask_reason: 'Is the rollout plan right?', ask_raised_at: '2026-11-16T10:00:00',
	asker: { id: 'a1', name: 'Docs agent' }, other_askers: [], other_asks: [], other_request_count: 0,
	round_feedback_count: 0, round_feedback_counts: { comments: 0, suggestions: 0, edits: 0 },
};

let host: HTMLElement;
let component: ReturnType<typeof mount> | undefined;
let bar: ReturnType<typeof mount> | undefined;
let target: DocViewerTarget;

async function open(width: number) {
	Object.defineProperty(window, 'innerWidth', { configurable: true, value: width });
	phone.isMobile.set(width <= 768);
	host = document.createElement('div');
	document.body.appendChild(host);
	component = mount(DocViewer, { target: host, props: { target } });
	// Comments and Details are drawn by the bar that holds the canvas; a stand-in draws them here.
	bar = mount(DocumentBarTestHost, { target: host });
	flushSync();
	await vi.waitFor(() => expect(host.querySelector('[aria-label="Review request"]')).not.toBeNull());
}
const ask = () => host.querySelector('[aria-label="Review request"]')!;
const answers = () => [...ask().querySelectorAll('button')].map((b) => b.textContent?.trim()).filter((text) => text === 'Send back' || text === 'Approve');

beforeEach(() => {
	vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} });
	vi.stubGlobal('IntersectionObserver', class { observe() {} unobserve() {} disconnect() {} });
	vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ open_count: 0, undelivered_batches: [], reviews: [], reviewers: [] }), { status: 200 })));
	HTMLElement.prototype.scrollIntoView = vi.fn();
	Range.prototype.getClientRects = vi.fn(() => [] as unknown as DOMRectList);
	Range.prototype.getBoundingClientRect = vi.fn(() => new DOMRect());
	api.fetchComments.mockResolvedValue({ comments: [], feedback: { open_count: 0, owner_label: null, undelivered_batches: [] } });
	api.fetchContent.mockResolvedValue({ text: 'Alpha opens the plan.\n', writable: true, artifact: { id: 'doc-ask' }, freshness: null });
	api.fetchRevision.mockResolvedValue({ exists: true, writable: true, artifact_id: 'doc-ask', freshness: null });
	review.fetchReviewBar.mockResolvedValue(ASK);
	target = { instanceId: 'ask-placement', path: '/documents/doc-ask.md', name: 'doc-ask.md', projectId: 'project', threadId: 'thread', artifactId: 'doc-ask' };
	connectionState.set('connected');
});
afterEach(async () => {
	if (bar) await unmount(bar);
	if (component) await unmount(component);
	bar = undefined;
	component = undefined;
	host?.remove();
	clearDraft(target);
	phone.isMobile.set(false);
	phone.isShortTouch.set(false);
	vi.restoreAllMocks();
	vi.unstubAllGlobals();
	for (const mock of [...Object.values(api), ...Object.values(review)]) mock.mockReset();
});

describe('where an open document shows the review ask', () => {
	it('puts the question and both answers in the document header on a desktop canvas', async () => {
		await open(1440);
		expect(ask().closest('header.document-header')).not.toBeNull();
		expect(host.querySelector('[data-ask-card]')).toBeNull();
		expect(ask().textContent).toContain('Is the rollout plan right?');
		expect(answers()).toEqual(['Send back', 'Approve']);
	});

	it.each([393, 320])('keeps the ask on the card docked to the canvas edge at %ipx', async (width) => {
		await open(width);
		expect(host.querySelector('[data-ask-card]')).toBe(ask());
		expect(host.querySelector('.rb-slot')).toBeNull();
		expect(answers()).toEqual(['Send back', 'Approve']);
	});

	it('docks the ask on a short touch screen too, whatever its width', async () => {
		phone.isShortTouch.set(true);
		await open(852);
		expect(host.querySelector('[data-ask-card]')).toBe(ask());
	});

	// The open Details panel drops over the top of the document, where the header's ask row sits, as it reaches the phone card
	// at the bottom: either way the ask steps aside and Details carries it. A rendered layout is not available here, so the
	// boxes are set to the geometry the real page has: the panel from the top, the ask's own box under it.
	describe('while Details is open', () => {
		const detailsAsk = () => host.querySelector('[data-details-ask]');
		const press = () => { host.querySelector<HTMLButtonElement>('[data-document-details]')!.click(); flushSync(); };
		function layOut(form: 'header' | 'docked') {
			// On a phone the panel is tall enough to reach the card at the bottom; in the header it starts on the row.
			const panelHeight = form === 'docked' ? 700 : 300;
			const box = (top: number, height: number) => ({ x: 0, y: top, top, height, bottom: top + height, left: 0, right: 600, width: 600, toJSON() {} }) as DOMRect;
			vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
				if (this.classList.contains('doc-panel')) return box(4, panelHeight);
				if (this.hasAttribute('data-ask-card')) return box(640, 80);
				return form === 'header' && this.classList.contains('review-bar') && this.classList.contains('ask') ? box(40, 60) : box(0, 0);
			});
			vi.spyOn(HTMLElement.prototype, 'scrollHeight', 'get').mockImplementation(function (this: HTMLElement) { return this.classList.contains('doc-panel') ? panelHeight : 0; });
		}

		it('carries the header ask, which the panel covers, and hands it back when it closes', async () => {
			await open(1440);
			layOut('header');
			expect(detailsAsk()).toBeNull();
			press();
			await vi.waitFor(() => expect(detailsAsk()?.textContent).toContain('Is the rollout plan right?'));
			expect(host.querySelector('header.document-header')!.classList.contains('ask-covered')).toBe(true);
			press();
			await vi.waitFor(() => expect(detailsAsk()).toBeNull());
			expect(host.querySelector('header.document-header')!.classList.contains('ask-covered')).toBe(false);
		});

		it('carries the docked ask the same way on a phone', async () => {
			await open(393);
			layOut('docked');
			press();
			await vi.waitFor(() => expect(detailsAsk()?.textContent).toContain('Is the rollout plan right?'));
		});
	});
});
