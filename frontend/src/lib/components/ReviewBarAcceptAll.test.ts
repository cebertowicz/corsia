import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import ReviewBar from './ReviewBar.svelte';
import MessageArtifactCards from './MessageArtifactCards.svelte';
import type { ReviewBarRead } from '$lib/api/reviewBar';
import { submittedAnswers } from '$lib/stores/cardAnswers';

// "Accept N suggestions and approve": the bar's own accept runs first, and a verdict follows only if it worked.

const api = vi.hoisted(() => ({ fetchReviewBar: vi.fn(), answerReview: vi.fn() }));
vi.mock('$lib/api/reviewBar', async (importOriginal) => ({ ...(await importOriginal<object>()), ...api }));
vi.mock('$lib/ws', () => ({ on: vi.fn(() => () => {}) }));

const counts = (comments = 0) => ({ comments, suggestions: 0, edits: 0 });
function read(comments = 0): ReviewBarRead {
	return {
		ask_raised: true, hold_ids: ['h1'], ask_reason: 'Is the rollout plan right?', ask_raised_at: '2026-10-10T10:00:00',
		asker: { id: 'a1', name: 'Docs agent' }, other_askers: [], other_asks: [], other_request_count: 0,
		round_feedback_count: comments, round_feedback_counts: counts(comments),
	};
}

let host: HTMLElement;
let component: Record<string, unknown> | null = null;
let cardsComponent: Record<string, unknown> | null = null;
async function render(pendingSuggestions: number, onAcceptAll?: () => Promise<number | void>) {
	component = mount(ReviewBar, { target: host, props: { artifactId: 'doc-1', pendingSuggestions, onAcceptAll } });
	await vi.waitFor(() => expect(host.querySelector('.review-bar')).toBeTruthy());
	flushSync();
}
const button = (name: string) => [...host.querySelectorAll('button')].find((b) => b.textContent?.trim() === name) as HTMLButtonElement | undefined;

beforeEach(() => {
	host = document.createElement('div');
	document.body.appendChild(host);
	api.fetchReviewBar.mockReset().mockResolvedValue(read(0));
	api.answerReview.mockReset().mockResolvedValue({ verdict: 'approved', note: null, feedback_counts: counts(0) });
	submittedAnswers.set({});
});
afterEach(() => {
	if (component) unmount(component);
	if (cardsComponent) unmount(cardsComponent);
	component = null;
	cardsComponent = null;
	host.remove();
});

describe('ReviewBar accept all and approve', () => {
	it('keeps plain Approve when no suggestion is open', async () => {
		await render(0, vi.fn());
		expect(button('Approve')).toBeTruthy();
		expect(host.textContent).not.toContain('Accept 0');
	});

	it('keeps the Approve label and says how many suggestions it also accepts', async () => {
		await render(2, vi.fn());
		expect(button('Approve')?.title).toBe('Accepts the 2 suggestions, then approves');
		expect(host.querySelector('.rb-derived')?.textContent).toBe('Also accepts 2 suggestions');
		unmount(component!); component = null; host.innerHTML = '';
		await render(1, vi.fn());
		expect(button('Approve')?.title).toBe('Accepts the 1 suggestion, then approves');
		expect(host.querySelector('.rb-derived')?.textContent).toBe('Also accepts 1 suggestion');
	});

	it('accepts first, then sends the verdict', async () => {
		const order: string[] = [];
		const accept = vi.fn(async () => { order.push('accept'); });
		api.answerReview.mockImplementation(async () => { order.push('answer'); return { verdict: 'approved', note: null, feedback_counts: counts(0) }; });
		await render(2, accept);
		button('Approve')!.click();
		await vi.waitFor(() => expect(host.querySelector('.review-bar.receipt')).toBeTruthy());
		expect(order).toEqual(['accept', 'answer']);
		expect(api.answerReview).toHaveBeenCalledWith('doc-1', 'approved', '', ['h1']);
	});

	it('shows the busy state while the accept runs', async () => {
		let finish: () => void = () => {};
		await render(2, () => new Promise<void>((resolve) => { finish = resolve; }));
		button('Approve')!.click();
		flushSync();
		expect(host.querySelector('.review-bar')?.getAttribute('aria-busy')).toBe('true');
		expect(api.answerReview).not.toHaveBeenCalled();
		finish();
		await vi.waitFor(() => expect(api.answerReview).toHaveBeenCalled());
	});

	it('sends no verdict when the accept fails, and says so on the bar', async () => {
		const accept = vi.fn().mockRejectedValueOnce(new Error('stale')).mockResolvedValueOnce(undefined);
		await render(2, accept);
		button('Approve')!.click();
		await vi.waitFor(() => expect(host.querySelector('.rb-status')?.textContent).toBe('Couldn\u2019t accept the suggestions. Try again.'));
		expect(api.answerReview).not.toHaveBeenCalled();
		expect(host.querySelector('.review-bar')?.getAttribute('aria-busy')).toBe('false');
		button('Approve')!.click();
		await vi.waitFor(() => expect(api.answerReview).toHaveBeenCalledTimes(1));
	});

	it('approves cleanly when the person has comments in the round', async () => {
		api.fetchReviewBar.mockResolvedValue(read(1));
		api.answerReview.mockResolvedValue({ verdict: 'approved', note: null, feedback_counts: counts(1) });
		await render(1, vi.fn(async () => {}));
		button('Approve')!.click();
		await vi.waitFor(() => expect(api.answerReview).toHaveBeenCalledWith('doc-1', 'approved', '', ['h1']));
	});

	it('leaves pending suggestions undecided when sending back and shows the returned counts on the card', async () => {
		const feedback = { comments: 2, suggestions: 1, edits: 0 };
		api.fetchReviewBar.mockResolvedValue({ ...read(2), round_feedback_counts: feedback });
		api.answerReview.mockResolvedValue({ verdict: 'changes_requested', note: 'Tighten it', feedback_counts: feedback });
		cardsComponent = mount(MessageArtifactCards, { target: host, props: { threadId: 't1', cards: [{ kind: 'ask', artifact_id: 'doc-1', title: 'Launch plan', type: 'document', icon: 'file-text', name: 'launch-plan.md', path: 'launch-plan.md', available: true, opens: true, hold_id: 'h1', state: 'needs_review' }] } });
		const accept = vi.fn(async () => {});
		await render(2, accept);
		button('Send back')!.click();
		await vi.waitFor(() => expect(host.querySelector('.rb-note')).toBeTruthy());
		const note = host.querySelector<HTMLTextAreaElement>('.rb-note')!;
		note.value = 'Tighten it'; note.dispatchEvent(new Event('input', { bubbles: true })); flushSync();
		[...host.querySelectorAll<HTMLButtonElement>('.rb-actions button')].find((b) => b.textContent?.trim() === 'Send back')!.click();
		await vi.waitFor(() => expect(api.answerReview).toHaveBeenCalledWith('doc-1', 'changes_requested', 'Tighten it', ['h1']));
		await vi.waitFor(() => expect(host.querySelector('.tile .name')?.textContent).toBe('2 comments, 1 suggestion'));
		expect(accept).not.toHaveBeenCalled();
	});

	it('names the accepted suggestions and the comments sent in the receipt', async () => {
		api.fetchReviewBar.mockResolvedValue(read(1));
		api.answerReview.mockResolvedValue({ verdict: 'approved', note: null, feedback_counts: counts(1) });
		await render(3, vi.fn(async () => 3));
		button('Approve')!.click();
		await vi.waitFor(() => expect(host.querySelector('.review-bar.receipt')).toBeTruthy());
		expect(host.querySelector('.rb-receipt')?.textContent?.replace(/\s+/g, ' ')).toMatch(/^Approved · 3 suggestions accepted · 1 comment sent · /);
		// Whole segments wrap and a separator leads the segment it introduces, so no wrapped line can end on one; the separators are hidden from screen readers.
		const segments = [...host.querySelectorAll('.rb-receipt .rb-seg')].map((seg) => seg.textContent?.replace(/\s+/g, ' ').trim() ?? '');
		expect(segments.slice(0, 3)).toEqual(['Approved', '· 3 suggestions accepted', '· 1 comment sent']);
		expect(segments.every((seg) => !seg.endsWith('·'))).toBe(true);
		const dots = [...host.querySelectorAll('.rb-receipt .rb-dot')];
		expect(dots.length).toBeGreaterThanOrEqual(3);
		expect(dots.every((dot) => dot.getAttribute('aria-hidden') === 'true')).toBe(true);
	});

	it('says one suggestion in the singular, and omits the count when none were accepted', async () => {
		await render(1, vi.fn(async () => 1));
		button('Approve')!.click();
		await vi.waitFor(() => expect(host.querySelector('.review-bar.receipt')).toBeTruthy());
		expect(host.querySelector('.rb-receipt')?.textContent).toContain('1 suggestion accepted');
		expect(host.querySelector('.rb-receipt')?.textContent).not.toContain('1 suggestions');
		unmount(component!); component = null; host.innerHTML = '';
		await render(2, vi.fn(async () => 0));
		button('Approve')!.click();
		await vi.waitFor(() => expect(host.querySelector('.review-bar.receipt')).toBeTruthy());
		expect(host.querySelector('.rb-receipt')?.textContent).not.toMatch(/accepted/);
	});

	it('shows what Approve also accepts beside it, and hides it while the Send back note is open', async () => {
		await render(3, vi.fn(async () => 3));
		expect(host.querySelector('.rb-derived')?.textContent).toBe('Also accepts 3 suggestions');
		button('Send back')!.click();
		await vi.waitFor(() => expect(host.querySelector('.rb-note')).toBeTruthy());
		expect(host.querySelector('.rb-derived')).toBeNull();
	});

	it('leaves the approve button plain where no accept is wired in', async () => {
		await render(3);
		expect(button('Approve')).toBeTruthy();
		expect(host.querySelector('.rb-derived')).toBeNull();
	});
});
