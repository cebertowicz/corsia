import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest';
import { flushSync, tick } from 'svelte';
import { createClassComponent } from 'svelte/legacy';
import ReviewSurface from './ReviewSurface.svelte';
import { createDocumentReviewState } from '$lib/docReviewState.svelte';
import type { DocumentReview } from '$lib/api/docReviews';

let server: DocumentReview[] = [];
let answers: (() => void)[] = [];
vi.mock('$lib/api/docReviews', () => ({
	fetchDocumentReviews: vi.fn(async () => ({ reviews: [...server], reviewers: [] })),
	requestDocumentReview: vi.fn(async () => undefined),
	// Each cancel is held until the test answers it, as a slow server would.
	cancelDocumentReview: vi.fn((id: string) => new Promise<void>((resolve) => { answers.push(() => { server = server.filter((request) => request.id !== id); resolve(); }); })),
	reviewFailure: (_error: unknown, fallback: string) => fallback,
}));

const review = (id: string): DocumentReview => ({
	id, reviewer_thread_id: `thread-${id}`, reviewer_name: `Agent ${id}`, requester_name: 'Person', created_at: '2026-11-16T10:00:00Z',
	may_cancel: true, state: 'forked', outcome: 'open', comment_count: 0,
} as DocumentReview);

let host: HTMLElement;
let anchor: HTMLElement;
let component: { $destroy(): void } | null = null;
/** The document beneath: its Escape closes it unless a layer above already took the key, as DocViewer's window handler does. */
let closeDocument: Mock<() => void>;
const documentEscape = (event: KeyboardEvent) => { if (event.key === 'Escape' && !event.defaultPrevented) closeDocument(); };

async function render(canRequest: boolean) {
	const model = createDocumentReviewState();
	model.setTarget('doc', 'thread', 'key');
	await model.load();
	host = document.createElement('div'); document.body.appendChild(host);
	anchor = document.createElement('button'); anchor.textContent = 'Request review'; document.body.appendChild(anchor);
	const onHide = vi.fn();
	component = createClassComponent({ target: host, component: ReviewSurface, props: { model, id: 'reviews', anchor, narrow: false, canRequest, onSent: vi.fn(), onHide, restoreFocus: vi.fn() } });
	flushSync();
	await vi.waitFor(() => expect(document.querySelector('[data-popover]')).not.toBeNull());
	return { model, onHide };
}
const cancelButton = (id: string) => document.querySelector<HTMLButtonElement>(`[aria-label="Cancel request to Agent ${id}"]`);
async function settle() { for (let i = 0; i < 4; i++) { flushSync(); await tick(); await Promise.resolve(); } }

beforeEach(() => {
	server = [review('a'), review('b')];
	answers = [];
	closeDocument = vi.fn();
	// Registered before the surface mounts, as the document's handler is.
	window.addEventListener('keydown', documentEscape);
});
afterEach(() => {
	window.removeEventListener('keydown', documentEscape);
	component?.$destroy(); component = null;
	host?.remove(); anchor?.remove();
	vi.clearAllMocks();
});

describe('review list Cancel keeps the focus in the surface', () => {
	it('refuses a second press while pending, moves focus to the next Cancel, then to the surface, so Escape closes the surface and not the document', async () => {
		const api = await import('$lib/api/docReviews');
		const { onHide } = await render(false);
		const first = cancelButton('a')!;
		first.focus();
		first.click();
		await settle();
		// Pending: the button keeps the focus and refuses a second press, which must not cancel twice or reach the next row.
		expect(document.activeElement).toBe(first);
		first.click();
		await settle();
		expect(vi.mocked(api.cancelDocumentReview).mock.calls.map(([id]) => id)).toEqual(['a']);
		expect(answers).toHaveLength(1);

		answers.shift()!();
		await vi.waitFor(() => expect(cancelButton('a')).toBeNull());
		await settle();
		expect(document.activeElement).toBe(cancelButton('b'));
		expect(vi.mocked(api.cancelDocumentReview).mock.calls.map(([id]) => id)).toEqual(['a']);

		cancelButton('b')!.click();
		await settle();
		answers.shift()!();
		await vi.waitFor(() => expect(document.querySelector('[data-review-request]')).toBeNull());
		await settle();
		const surface = document.querySelector<HTMLElement>('[data-popover]')!;
		expect(document.activeElement).toBe(surface);

		document.activeElement!.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }));
		expect(onHide).toHaveBeenCalledTimes(1);
		expect(closeDocument).not.toHaveBeenCalled();
	});

	it('after the last request is cancelled, Escape closes the surface and not the document', async () => {
		server = [review('a')];
		const { onHide } = await render(false);
		const only = cancelButton('a')!;
		only.focus();
		only.click();
		await settle();
		answers.shift()!();
		await vi.waitFor(() => expect(document.querySelector('[data-review-request]')).toBeNull());
		await settle();
		(document.activeElement ?? document.body).dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }));
		expect(closeDocument).not.toHaveBeenCalled();
		expect(onHide).toHaveBeenCalledTimes(1);
	});

	it('gives the focus to the search field when the last request goes and the person can request again', async () => {
		server = [review('a')];
		await render(true);
		const only = cancelButton('a')!;
		only.focus();
		only.click();
		await settle();
		answers.shift()!();
		await vi.waitFor(() => expect(document.querySelector('[data-review-request]')).toBeNull());
		await settle();
		expect(document.activeElement).toBe(document.querySelector('[data-review-autofocus]'));
	});

	it('leaves the focus where the person moved it while the request was pending', async () => {
		await render(false);
		const first = cancelButton('a')!;
		first.focus();
		first.click();
		await settle();
		anchor.focus();
		answers.shift()!();
		await vi.waitFor(() => expect(cancelButton('a')).toBeNull());
		await settle();
		expect(document.activeElement).toBe(anchor);
	});
});
