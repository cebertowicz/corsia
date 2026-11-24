import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import DocViewer from './DocViewer.svelte';
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
let target: DocViewerTarget;

async function open(width: number) {
	Object.defineProperty(window, 'innerWidth', { configurable: true, value: width });
	phone.isMobile.set(width <= 768);
	host = document.createElement('div');
	document.body.appendChild(host);
	component = mount(DocViewer, { target: host, props: { target } });
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
	if (component) await unmount(component);
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
});
