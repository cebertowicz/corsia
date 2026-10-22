import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import ReviewBar from './ReviewBar.svelte';
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
async function render(pendingSuggestions: number, onAcceptAll?: () => Promise<void>) {
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
	component = null;
	host.remove();
});

describe('ReviewBar accept all and approve', () => {
	it('keeps plain Approve when no suggestion is open', async () => {
		await render(0, vi.fn());
		expect(button('Approve')).toBeTruthy();
		expect(host.textContent).not.toContain('Accept 0');
	});

	it('names how many suggestions the approve accepts', async () => {
		await render(2, vi.fn());
		expect(button('Accept 2 suggestions and approve')).toBeTruthy();
		expect(button('Approve')).toBeUndefined();
		unmount(component!); component = null; host.innerHTML = '';
		await render(1, vi.fn());
		expect(button('Accept 1 suggestion and approve')).toBeTruthy();
	});

	it('accepts first, then sends the verdict', async () => {
		const order: string[] = [];
		const accept = vi.fn(async () => { order.push('accept'); });
		api.answerReview.mockImplementation(async () => { order.push('answer'); return { verdict: 'approved', note: null, feedback_counts: counts(0) }; });
		await render(2, accept);
		button('Accept 2 suggestions and approve')!.click();
		await vi.waitFor(() => expect(host.querySelector('.review-bar.receipt')).toBeTruthy());
		expect(order).toEqual(['accept', 'answer']);
		expect(api.answerReview).toHaveBeenCalledWith('doc-1', 'approved', '', ['h1']);
	});

	it('shows the busy state while the accept runs', async () => {
		let finish: () => void = () => {};
		await render(2, () => new Promise<void>((resolve) => { finish = resolve; }));
		button('Accept 2 suggestions and approve')!.click();
		flushSync();
		expect(host.querySelector('.review-bar')?.getAttribute('aria-busy')).toBe('true');
		expect(api.answerReview).not.toHaveBeenCalled();
		finish();
		await vi.waitFor(() => expect(api.answerReview).toHaveBeenCalled());
	});

	it('sends no verdict when the accept fails, and says so on the bar', async () => {
		const accept = vi.fn().mockRejectedValueOnce(new Error('stale')).mockResolvedValueOnce(undefined);
		await render(2, accept);
		button('Accept 2 suggestions and approve')!.click();
		await vi.waitFor(() => expect(host.textContent).toContain("Couldn't accept the suggestions. Try again."));
		expect(api.answerReview).not.toHaveBeenCalled();
		expect(host.querySelector('.review-bar')?.getAttribute('aria-busy')).toBe('false');
		button('Accept 2 suggestions and approve')!.click();
		await vi.waitFor(() => expect(api.answerReview).toHaveBeenCalledTimes(1));
	});

	it('still approves with changes when the person has comments in the round', async () => {
		api.fetchReviewBar.mockResolvedValue(read(1));
		api.answerReview.mockResolvedValue({ verdict: 'approved_with_changes', note: null, feedback_counts: counts(1) });
		await render(1, vi.fn(async () => {}));
		button('Accept 1 suggestion and approve')!.click();
		await vi.waitFor(() => expect(api.answerReview).toHaveBeenCalledWith('doc-1', 'approved_with_changes', '', ['h1']));
	});

	it('leaves the approve button plain where no accept is wired in', async () => {
		await render(3);
		expect(button('Approve')).toBeTruthy();
	});
});
