import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import ReviewBar from './ReviewBar.svelte';
import type { ReviewBarRead } from '$lib/api/reviewBar';
import { submittedAnswers } from '$lib/stores/cardAnswers';

// The accepted-suggestions count belongs to the receipt it produced: a later round on the same mounted bar never repeats it.

const api = vi.hoisted(() => ({ fetchReviewBar: vi.fn(), answerReview: vi.fn() }));
vi.mock('$lib/api/reviewBar', async (importOriginal) => ({ ...(await importOriginal<object>()), ...api }));
const handlers = vi.hoisted(() => ({ map: new Map<string, () => void>() }));
vi.mock('$lib/ws', () => ({
	on: vi.fn((event: string, handler: () => void) => { handlers.map.set(event, handler); return () => {}; }),
}));

const counts = { comments: 0, suggestions: 0, edits: 0 };
const read = (raisedAt: string): ReviewBarRead => ({
	ask_raised: true, hold_ids: ['h1'], ask_reason: 'Is the rollout plan right?', ask_raised_at: raisedAt,
	asker: { id: 'a1', name: 'Docs agent' }, other_askers: [], other_asks: [], other_request_count: 0,
	round_feedback_count: 0, round_feedback_counts: counts,
});

let host: HTMLElement;
let component: Record<string, unknown> | null = null;
const button = (name: string) => [...host.querySelectorAll('button')].find((b) => b.textContent?.trim() === name) as HTMLButtonElement | undefined;
const receiptText = () => host.querySelector('.rb-receipt')?.textContent?.replace(/\s+/g, ' ') ?? '';

beforeEach(() => {
	host = document.createElement('div');
	document.body.appendChild(host);
	handlers.map.clear();
	api.fetchReviewBar.mockReset().mockResolvedValue(read('2026-10-10T10:00:00'));
	api.answerReview.mockReset().mockResolvedValue({ verdict: 'approved', note: null, feedback_counts: counts });
	submittedAnswers.set({});
});
afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
});

describe('ReviewBar accepted count across rounds', () => {
	it('keeps round 1’s accepted count out of round 2’s receipt', async () => {
		const props = $state({ artifactId: 'doc-1', pendingSuggestions: 2, onAcceptAll: vi.fn(async () => 2) });
		component = mount(ReviewBar, { target: host, props });
		await vi.waitFor(() => expect(button('Approve all')).toBeTruthy());

		button('Approve all')!.click();
		await vi.waitFor(() => expect(host.querySelector('.review-bar.receipt')).toBeTruthy());
		expect(receiptText()).toContain('2 suggestions accepted');

		// A second ask arrives on the same mounted bar, with nothing left to accept.
		props.pendingSuggestions = 0;
		api.fetchReviewBar.mockResolvedValue(read('2026-10-10T11:00:00'));
		handlers.map.get('artifacts_changed')?.();
		await vi.waitFor(() => expect(button('Approve')).toBeTruthy());
		flushSync();

		button('Approve')!.click();
		await vi.waitFor(() => expect(host.querySelector('.review-bar.receipt')).toBeTruthy());
		expect(receiptText()).toMatch(/^Approved/);
		expect(receiptText()).not.toContain('accepted');
	});

	it('shows only the run that succeeded when a failed Approve all is redone', async () => {
		const onAcceptAll = vi.fn().mockRejectedValueOnce(new Error('stale')).mockResolvedValueOnce(3);
		const props = $state({ artifactId: 'doc-1', pendingSuggestions: 3, onAcceptAll });
		component = mount(ReviewBar, { target: host, props });
		await vi.waitFor(() => expect(button('Approve all')).toBeTruthy());

		button('Approve all')!.click();
		await vi.waitFor(() => expect(host.querySelector('.rb-status')?.textContent).toContain('Try again'));
		button('Approve all')!.click();
		await vi.waitFor(() => expect(host.querySelector('.review-bar.receipt')).toBeTruthy());
		expect(receiptText()).toMatch(/^Approved · 3 suggestions accepted · /);
		expect(onAcceptAll).toHaveBeenCalledTimes(2);
	});
});
