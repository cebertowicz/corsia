import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { createClassComponent } from 'svelte/legacy';
import DocViewerStatus from './DocViewerStatus.svelte';
import DocCommentDrawerFoot from './DocCommentDrawerFoot.svelte';
import type { DocFeedback } from '$lib/docFeedback.svelte';
import DocumentBarTestHost from './DocumentBarTestHost.test.svelte';
import docViewerStatusSource from './DocViewerStatus.svelte?raw';
import type { DocumentFreshness } from '$lib/docFreshness';
import { detailsNeedsCount, type DetailsNeeds } from '$lib/docDetailsNeeds';
import type { CommentFeedback } from '$lib/api/docs';
import { deliverCommentsImmediately } from '$lib/commentDeliveryPreference';
import { get } from 'svelte/store';
import { token } from '$lib/stores/auth';
import { toasts } from '$lib/stores/toast';
import { deliveryConfirmation } from '$lib/stores/deliveryConfirmation';
import { canvasCompact } from '$lib/stores/artifactCanvas';

let host: HTMLElement;
let bar: ReturnType<typeof mount> | null = null;
let component: ({ commentCreated: () => void; feedback: () => DocFeedback | undefined; sync: (id: string, feedback: CommentFeedback) => void } & { $destroy: () => void; $set: (props: Record<string, unknown>) => void }) | null = null;
let foot: { $destroy: () => void; $set: (props: Record<string, unknown>) => void } | null = null;

function renderFoot(overrides: Record<string, unknown> = {}) {
	foot = createClassComponent({ target: host, component: DocCommentDrawerFoot, props: { feedback: component?.feedback(), ...overrides } });
	flushSync();
}

function value(overrides: Partial<DocumentFreshness> = {}): DocumentFreshness {
	return {
		class: 'reference',
		verified_at: '2026-09-12T12:00:00Z',
		age_days: 18,
		due_at: '2026-10-20T12:00:00Z',
		state: 'current',
		owner_name: 'Structured Plans',
		...overrides,
	};
}

function render(freshness: DocumentFreshness, onMarkVerified = vi.fn().mockResolvedValue(undefined), overrides: Record<string, unknown> = {}) {
	host = document.createElement('div');
	document.body.appendChild(host);
	component = createClassComponent({
		target: host,
		component: DocViewerStatus,
		props: {
			ready: true,
			stale: false,
			readonly: false,
			commentCount: 0,
			artifactId: 'artifact-1',
			targetInstanceId: 'tab-1',
			onOpenComments: vi.fn(),
			freshness,
			onMarkVerified,
			...overrides,
		},
	}) as unknown as typeof component;
	// Comments and Details are drawn by the bar that holds the canvas; a stand-in draws them here.
	bar = mount(DocumentBarTestHost, { target: host });
	flushSync();
	if (component?.feedback()) renderFoot(overrides);
	return onMarkVerified;
}

afterEach(() => {
	foot?.$destroy(); foot = null;
	if (bar) { unmount(bar); bar = null; }
	component?.$destroy();
	component = null;
	host?.remove();
	vi.useRealTimers();
	deliverCommentsImmediately.set(true);
	canvasCompact.set(false);
	deliveryConfirmation.dismiss();
});

describe('DocViewerStatus freshness metadata', () => {
	it('keeps the owner in Details and zero-comment navigation in the bar', async () => {
		const fetchMock = vi.fn().mockImplementation(() => Promise.resolve(new Response(JSON.stringify({ immediate: false, stored: true }))));
		vi.stubGlobal('fetch', fetchMock);
		token.set('test-token');
		render(value());
		const header = host.querySelector('header');
		expect(header?.textContent).toContain('Owner: Structured Plans');
		expect(host.querySelector('[data-comments-entry]')?.getAttribute('aria-label')).toBe('0 comments');
		const delivery = host.querySelector<HTMLButtonElement>('.delivery button');
		expect(delivery?.getAttribute('aria-pressed')).toBe('true');
		delivery?.click();
		flushSync();
		expect(delivery?.getAttribute('aria-pressed')).toBe('false');
		await vi.waitFor(() => expect(fetchMock.mock.calls.some(([url, init]) => url === '/api/doc-feedback/delivery-preference' && init.method === 'PUT' && init.body === '{"immediate":false}')).toBe(true));
		vi.unstubAllGlobals();
		token.set(null);
		const details = host.querySelector<HTMLButtonElement>('[data-document-details]');
		expect(details?.getAttribute('aria-label')).toBe('Document details');
		expect(details?.getAttribute('aria-expanded')).toBe('false');
		expect(host.querySelector<HTMLElement>('.doc-panel')?.hidden).toBe(true);
		details!.click();
		flushSync();
		expect(details?.getAttribute('aria-expanded')).toBe('true');
		expect(host.querySelector<HTMLElement>('.doc-panel')?.hidden).toBe(false);
	});

	it('keeps the resolved view in Comments rather than Details', () => {
		render(value());
		host.querySelector<HTMLButtonElement>('[data-document-details]')!.click(); flushSync();
		expect(host.querySelector('.doc-panel [data-resolved-entry]')).toBeNull();
		expect(host.querySelector('[data-comments-entry]')).not.toBeNull();
	});

	it('withdraws Comments, Details and File changed from the bar while the document is off screen, and puts them back after', () => {
		render(value(), undefined, { stale: true, offScreen: true });
		const inBar = (selector: string) => host.querySelector(`[data-test-document-bar] ${selector}`);
		expect(host.querySelector('[data-test-document-bar]')).toBeNull();
		component!.$set({ offScreen: false }); flushSync();
		expect(inBar('[data-comments-entry]')).not.toBeNull();
		expect(inBar('[data-document-details]')).not.toBeNull();
		expect(inBar('[data-file-changed]')).not.toBeNull();
		component!.$set({ offScreen: true }); flushSync();
		expect(host.querySelector('[data-test-document-bar]')).toBeNull();
	});

	it('does not claim a zero comment count before the document is ready', () => {
		render(value(), undefined, { ready: false, commentCount: 0 });
		const pill = host.querySelector<HTMLButtonElement>('[data-comments-entry]')!;
		expect(pill.getAttribute('aria-label')).toBe('Comments unavailable until the document opens');
		expect(pill.getAttribute('aria-busy')).toBe('true');
		expect(pill.querySelector('.spinner[aria-hidden="true"]')).not.toBeNull();
		expect(pill.textContent).not.toContain('0');
	});

	it('names and marks the Comments button while its count loads', () => {
		render(value(), undefined, { commentsLoading: true });
		const pill = host.querySelector<HTMLButtonElement>('[data-comments-entry]')!;
		expect(pill.getAttribute('aria-label')).toBe('Loading comments');
		expect(pill.getAttribute('aria-busy')).toBe('true');
		expect(pill.disabled).toBe(true);
		expect(pill.querySelector('.spinner[aria-hidden="true"]')).not.toBeNull();
	});

	it('marks and names unsent feedback on the Comments button; Send waits for held document comments', () => {
		render(value(), undefined, { commentCount: 2 });
		component!.sync('artifact-1', { open_count: 3, image_held_count: 3, owner_label: 'Style', undelivered_batches: [] });
		flushSync();
		const pill = host.querySelector('[data-comments-entry]');
		expect(pill?.getAttribute('aria-label')).toBe('2 comments, 3 waiting for Send');
		expect(pill?.querySelector('.unsent-dot')).not.toBeNull();
		expect(host.textContent).not.toContain('Send 3 comments');
		// Nothing is held back for the document, so no Send shows, not even clipped.
		expect(host.querySelector('.sending-now')).toBeNull();
		expect([...host.querySelectorAll('button')].map((button) => button.textContent?.trim())).not.toContain('Send now');
		expect(host.textContent).not.toContain('Sending to');
	});

	it('does not mark comments going at once as waiting for Send', () => {
		render(value(), undefined, { commentCount: 2 });
		// Open, but in the immediate lane: no Send closes it, so nothing waits for one.
		component!.sync('artifact-1', { open_count: 3, document_held_count: 0, image_held_count: 0, owner_label: 'Style', undelivered_batches: [] });
		flushSync();
		const pill = host.querySelector('[data-comments-entry]');
		expect(pill?.getAttribute('aria-label')).toBe('2 comments');
		expect(pill?.querySelector('.unsent-dot')).toBeNull();
	});

	it('turning immediate delivery on sends the held document lane once with its toast, leaving the image lane held', async () => {
		deliverCommentsImmediately.set(false);
		token.set('test-token');
		const fetchMock = vi.fn().mockImplementation(() => Promise.resolve(new Response(JSON.stringify({
			reviews: [], woken: [], undelivered_batches: [], sent_count: 2,
			open_count: 3, document_held_count: 0, image_held_count: 3,
		}))));
		const sends = () => fetchMock.mock.calls.filter(([url]) => url === '/api/doc-feedback/send');
		vi.stubGlobal('fetch', fetchMock);
		try {
			render(value(), undefined, { commentCount: 5, imageOpen: true });
			component!.sync('artifact-1', { open_count: 5, document_held_count: 2, image_held_count: 3, owner_label: 'Style', undelivered_batches: [] });
			flushSync();
			const toggle = host.querySelector<HTMLButtonElement>('.delivery button')!;
			expect(toggle.getAttribute('aria-pressed')).toBe('false');
			toggle.click(); flushSync();
			await vi.waitFor(() => expect(sends()).toHaveLength(1));
			expect(JSON.parse(sends()[0][1].body)).toEqual({ artifact_id: 'artifact-1', delivery_policy: 'document-held' });
			await vi.waitFor(() => expect(get(deliveryConfirmation)?.message).toBe('2 comments sent'));
			expect(get(toasts).some((toast) => toast.message === '2 comments sent')).toBe(false);
			expect(toggle.getAttribute('aria-pressed')).toBe('true');
			expect(get(deliverCommentsImmediately)).toBe(true);
			await vi.waitFor(() => expect(host.textContent).not.toContain('Send 2 comments'));
			expect(host.querySelector('[data-comments-entry]')?.getAttribute('aria-label')).toContain('3 waiting for Send');
			toggle.click(); flushSync();
			expect(sends()).toHaveLength(1);
		} finally {
			vi.unstubAllGlobals();
			token.set(null);
			for (const toast of get(toasts)) toasts.dismiss(toast.id);
		}
	});

	it('turning immediate delivery on asks the server to send the document lane even before this client has seen anything held', async () => {
		deliverCommentsImmediately.set(false);
		token.set('test-token');
		const fetchMock = vi.fn().mockImplementation(() => Promise.resolve(new Response(JSON.stringify({
			reviews: [], woken: [], undelivered_batches: [], sent_count: 0,
			open_count: 0, document_held_count: 0, image_held_count: 0,
		}))));
		const sends = () => fetchMock.mock.calls.filter(([url]) => url === '/api/doc-feedback/send');
		vi.stubGlobal('fetch', fetchMock);
		try {
			render(value(), undefined, { commentCount: 0 });
			flushSync();
			host.querySelector<HTMLButtonElement>('.delivery button')!.click(); flushSync();
			await vi.waitFor(() => expect(sends()).toHaveLength(1));
			expect(JSON.parse(sends()[0][1].body)).toEqual({ artifact_id: 'artifact-1', delivery_policy: 'document-held' });
			expect(get(deliveryConfirmation)).toBeNull();
		} finally {
			vi.unstubAllGlobals();
			token.set(null);
			for (const toast of get(toasts)) toasts.dismiss(toast.id);
		}
	});

	it('updates the Comments count and its unsent summary as feedback changes', () => {
		render(value(), undefined, { commentCount: 1, unreadCount: 0 });
		const pill = host.querySelector('[data-comments-entry]')!;
		expect(pill.textContent?.trim()).toBe('1');
		expect(pill.getAttribute('aria-label')).toBe('1 comment');
		component!.sync('artifact-1', { open_count: 1, image_held_count: 1, owner_label: 'Style', undelivered_batches: [] });
		flushSync();
		expect(pill.getAttribute('aria-label')).toBe('1 comment, 1 waiting for Send');
		component!.sync('artifact-1', { open_count: 0, owner_label: 'Style', undelivered_batches: [] });
		flushSync();
		expect(pill.getAttribute('aria-label')).toBe('1 comment');
		// Unread shows as text, never a dot.
		component!.$set({ unreadCount: 25 }); flushSync();
		expect(pill.textContent?.trim()).toBe('25 unread');
		expect(pill.getAttribute('aria-label')).toBe('1 comment, 25 unread');
	});

	it('closes Details on scoped Escape, restores its toggle, and leaves Escape elsewhere to its own control', () => {
		render(value({ state: 'overdue' }));
		const toggle = host.querySelector<HTMLButtonElement>('[data-document-details]')!;
		toggle.click(); flushSync();
		const escape = () => new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true });
		host.querySelector<HTMLButtonElement>('[data-comments-entry]')!.dispatchEvent(escape()); flushSync();
		expect(toggle.getAttribute('aria-expanded')).toBe('true');
		const event = escape();
		host.querySelector<HTMLButtonElement>('.verification-action button')!.dispatchEvent(event); flushSync();
		expect(event.defaultPrevented).toBe(true);
		expect(toggle.getAttribute('aria-expanded')).toBe('false');
		expect(document.activeElement).toBe(toggle);
	});

	it('stays open under a picker or review list drawn at the page top level, and closes on a press anywhere else', () => {
		render(value());
		const toggle = host.querySelector<HTMLButtonElement>('[data-document-details]')!;
		toggle.click(); flushSync();
		const press = (target: Element) => { target.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true })); flushSync(); };
		for (const attribute of ['data-popover', 'data-adaptive-drawer']) {
			const layer = document.createElement('div');
			layer.setAttribute(attribute, '');
			const inner = layer.appendChild(document.createElement('button'));
			document.body.appendChild(layer);
			press(inner);
			expect(toggle.getAttribute('aria-expanded'), attribute).toBe('true');
			layer.remove();
		}
		press(document.body);
		expect(toggle.getAttribute('aria-expanded')).toBe('false');
	});

	it.each([
		[value({ state: 'current', due_at: '2026-10-20T12:00:00Z' }), false],
		[value({ state: 'overdue' }), true],
		[value({ state: 'never_verified', verified_at: null, age_days: null }), true],
	])('shows Mark verified only for eligible freshness states', (freshness, visible) => {
		// Pinned so the 'current' row stays outside the seven-day window whatever today's date is.
		vi.useFakeTimers();
		vi.setSystemTime(new Date('2026-09-30T12:00:00Z'));
		render(freshness);
		expect(host.textContent?.includes('Mark verified')).toBe(visible);
	});

	it('shows Mark verified inside the seven-day window', () => {
		vi.useFakeTimers();
		vi.setSystemTime(new Date('2026-09-30T12:00:00Z'));
		render(value({ due_at: '2026-10-07T12:00:00Z' }));
		expect(host.textContent).toContain('Mark verified');
	});

	it('announces success, then clears it when the parent switches documents', async () => {
		const mark = render(value({ state: 'overdue' }));
		host.querySelector<HTMLButtonElement>('button.btn.ghost')!.click();
		await vi.waitFor(() => expect(mark).toHaveBeenCalledOnce());
		await vi.waitFor(() => expect(host.querySelector('.metadata-status[role="status"]')?.textContent).toBe('Document marked verified.'));

		component!.$set({ artifactId: 'artifact-2', freshness: value({ age_days: 94 }) });
		flushSync();
		expect(host.textContent).toContain('verified 94 days ago');
		expect(host.querySelector('.metadata-status[role="status"]')?.textContent).toBe('');
	});

	it.each([
		['unsent only', { commentCount: 2 }, { open_count: 3, image_held_count: 3, owner_label: 'Style', undelivered_batches: [] }, '2 comments, 3 waiting for Send'],
		['neither', { commentCount: 2 }, null, '2 comments'],
	])('names %s comment status once', (_state, props, feedback, expected) => {
		render(value(), undefined, props);
		if (feedback) component!.sync('artifact-1', feedback);
		flushSync();
		const pill = host.querySelector<HTMLButtonElement>('[data-comments-entry]')!;
		expect(pill.getAttribute('aria-label')).toBe(expected);
	});

	it('removes Mark verified when the parent replaces freshness after success', async () => {
		vi.useFakeTimers();
		vi.setSystemTime(new Date('2026-09-30T12:00:00Z'));
		const mark = render(value({ due_at: '2026-10-03T12:00:00Z' }));
		host.querySelector<HTMLButtonElement>('button.btn.ghost')!.click();
		await vi.waitFor(() => expect(mark).toHaveBeenCalledOnce());

		component!.$set({ freshness: value({ age_days: 0, due_at: null, state: 'current' }) });
		flushSync();
		expect(host.textContent).not.toContain('Mark verified');
	});

	it('puts Mark verified on the overdue Status row, which names the last verification date', () => {
		render(value({ state: 'overdue' }));
		const row = host.querySelector('.verification-action')!.closest('.p-row')!;
		expect(row.textContent).toContain('Status: Overdue · verified');
		expect(row.querySelector('.verification-action button')?.textContent?.trim()).toBe('Mark verified');
		// The action keeps its whole label; the Status text is what shortens.
		expect(docViewerStatusSource).toContain('.verification-action { flex: none;');
	});

	it('puts the absolute date in screen-reader time text', () => {
		render(value({ age_days: 94 }));
		expect(host.querySelector('time .sr-only')?.textContent).toContain('Sep 12');
	});

	it('renders the server refusal inline', async () => {
		render(value({ state: 'overdue' }), vi.fn().mockRejectedValue(new Error('Only Structured Plans may verify this document.')));
		host.querySelector<HTMLButtonElement>('button.btn.ghost')!.click();
		await vi.waitFor(() => expect(host.textContent).toContain('Only Structured Plans may verify this document.'));
	});
});

describe('DocViewerStatus toolbar', () => {
	it('announces the stale state through the status region once it starts, and clears it on reload', () => {
		render(value(), undefined, { stale: false });
		const status = () => host.querySelector('.metadata-status[role="status"]')?.textContent ?? '';
		expect(status()).not.toContain('File changed');

		component!.$set({ stale: true });
		flushSync();
		expect(status()).toContain('File changed since you opened it');
		expect(host.querySelector('.bar-stale')?.textContent).toContain('File changed · Reload');
		expect(host.querySelectorAll('[data-file-changed]')).toHaveLength(1);
		canvasCompact.set(true); flushSync();
		expect(host.querySelector('.bar-reload')?.getAttribute('aria-label')).toBe('File changed · Reload');
		expect(host.querySelectorAll('[data-file-changed]')).toHaveLength(1);

		component!.$set({ stale: false });
		flushSync();
		expect(status()).toBe('');
	});

	it('does not announce stale before the document is ready', () => {
		render(value(), undefined, { stale: true, ready: false });
		expect(host.querySelector('.metadata-status[role="status"]')?.textContent).toBe('');
	});

	it('keeps the stale announcement screen-reader only, since the visible control already shows it', () => {
		render(value(), undefined, { stale: true });
		const status = host.querySelector('.metadata-status[role="status"]')!;
		expect(status.textContent).toContain('File changed since you opened it');
		expect(status.classList.contains('sr-only')).toBe(true);
	});

	it('counts beside Details exactly the two things behind it that need the reader, one each', () => {
		// Every combination of the two sources; a third source is a new required field of DetailsNeeds, so this list stops compiling.
		const all: DetailsNeeds[] = [false, true].flatMap((overdue) =>
			[false, true].map((hiddenAsk) => ({ overdue, hiddenAsk })));
		expect(all.map((needs) => detailsNeedsCount(needs))).toEqual(all.map(({ overdue, hiddenAsk }) => Number(overdue) + Number(hiddenAsk)));
	});
});

describe('DocViewerStatus held comments', () => {
	const held = (document_held_count: number, image_held_count: number, undelivered_batches: unknown[] = []) => ({
		open_count: document_held_count + image_held_count, document_held_count, image_held_count, owner_label: 'Style', undelivered_batches,
	}) as unknown as CommentFeedback;
	const fetchMock = vi.fn();
	/** The feedback sends the component made; its other reads (review requests) are not under test. */
	const sends = () => fetchMock.mock.calls.filter(([url]) => String(url).startsWith('/api/doc-feedback/'));

	beforeEach(() => {
		fetchMock.mockReset();
		fetchMock.mockImplementation((url: string) => Promise.resolve(new Response(JSON.stringify(
			String(url).startsWith('/api/doc-feedback/')
				? { woken: [], undelivered_batches: [], sent_count: 1, open_count: 0, document_held_count: 0, image_held_count: 0 }
				: { reviews: [], reviewers: [] },
		), { status: 200 })));
		vi.stubGlobal('fetch', fetchMock);
	});
	afterEach(() => { vi.unstubAllGlobals(); });

	it('shows a Send for the held document comments only, with the owner in its name', () => {
		render(value(), undefined, { commentCount: 2 });
		component!.sync('artifact-1', held(3, 2));
		flushSync();
		const send = [...host.querySelectorAll('button')].find((button) => /^Send/.test(button.textContent?.trim() ?? ''))!;
		expect(send.querySelector('.send-full')!.textContent!.trim()).toBe('Send 3 comments');
		expect(send.querySelector('.send-short')!.textContent!.trim()).toBe('Send · 3');
		expect(send.getAttribute('aria-label')).toBe('Send 3 comments to Style');
		// Send is in the comments drawer's foot.
		expect(send.closest('.drawer-foot .send')).not.toBeNull();
		// The Comments button's name still counts everything unsent on the document.
		expect(host.querySelector('[data-comments-entry]')!.getAttribute('aria-label')).toContain('5 waiting for Send');
	});

	it('keeps a read that arrives before the identity-row sender exists, and shows it once that sender mounts', () => {
		render(value(), undefined, { artifactId: null });
		expect(host.querySelector('.feedback-controls')).toBeNull();
		expect(host.querySelector('.document-notices')).toBeNull();
		component!.sync('artifact-1', held(2, 0));
		component!.$set({ artifactId: 'artifact-1' });
		flushSync();
		renderFoot();
		const send = [...host.querySelectorAll('button')].find((button) => /^Send/.test(button.textContent?.trim() ?? ''));
		expect(send?.querySelector('.send-full')?.textContent?.trim()).toBe('Send 2 comments');
	});

	it('does not carry a read for another document into this one', () => {
		render(value(), undefined, { artifactId: null });
		component!.sync('artifact-2', held(2, 0));
		component!.$set({ artifactId: 'artifact-1' });
		flushSync();
		expect([...host.querySelectorAll('button')].some((button) => /^Send/.test(button.textContent?.trim() ?? ''))).toBe(false);
	});

	it('sends image comments left held when no image viewer is open, once', async () => {
		render(value(), undefined, { imageOpen: false });
		component!.sync('artifact-1', held(0, 2));
		flushSync();
		await vi.waitFor(() => expect(sends()).toHaveLength(1));
		expect(JSON.parse(sends()[0][1].body)).toEqual({ artifact_id: 'artifact-1', delivery_policy: 'image-held' });
		component!.sync('artifact-1', held(0, 2));
		flushSync();
		await Promise.resolve();
		expect(sends()).toHaveLength(1);
	});

	it('leaves the image lane to its viewer while an image is open', async () => {
		render(value(), undefined, { imageOpen: true });
		component!.sync('artifact-1', held(0, 2));
		flushSync();
		await Promise.resolve();
		expect(sends()).toHaveLength(0);
	});

	it('offers History as a Details row and omits it without recorded history', () => {
		const onOpenHistory = vi.fn();
		render(value(), undefined, { historyAvailable: true, onOpenHistory });
		const button = host.querySelector<HTMLButtonElement>('.history-tool')!;
		expect(button.textContent?.trim()).toBe('History');
		expect(button.closest('.doc-panel')).not.toBeNull();
		button.click();
		expect(onOpenHistory).toHaveBeenCalledTimes(1);
		component!.$destroy(); host.remove();
		render(value(), undefined, { historyAvailable: false, onOpenHistory });
		expect(host.querySelector('.history-tool')).toBeNull();
	});
});
