import { flushSync, mount, tick, unmount } from 'svelte';
import { get } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import ReviewBar from './ReviewBar.svelte';
import { DRAFT_OPEN, SEND_FAILED } from '$lib/reviewBarState.svelte';
import { ReviewRefusal, type ReviewBarRead } from '$lib/api/reviewBar';
import { pendingAnswers } from '$lib/stores/cardAnswers';

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
async function render(draftOpen = false, onAnswered?: () => void) {
	component = mount(ReviewBar, { target: host, props: { artifactId: 'doc-1', draftOpen, onAnswered } });
	await vi.waitFor(() => expect(host.querySelector('.review-bar')).toBeTruthy());
	flushSync();
}
const button = (name: string) => [...host.querySelectorAll('button')].find((b) => b.textContent?.trim() === name) as HTMLButtonElement | undefined;

beforeEach(() => {
	host = document.createElement('div');
	document.body.appendChild(host);
	api.fetchReviewBar.mockReset();
	api.answerReview.mockReset();
	pendingAnswers.set({});
});
afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
});

describe('ReviewBar', () => {
	it('labels the primary action by the round feedback, not by a click', async () => {
		api.fetchReviewBar.mockResolvedValue(read(0));
		await render();
		expect(button('Approve')).toBeTruthy();
		expect(button('Approve with changes')).toBeUndefined();
		unmount(component!); component = null; host.innerHTML = '';
		api.fetchReviewBar.mockResolvedValue(read(2));
		await render();
		expect(button('Approve with changes')).toBeTruthy();
	});

	it('shows nothing when no ask is raised', async () => {
		api.fetchReviewBar.mockResolvedValue({ ...read(0), ask_raised: false });
		component = mount(ReviewBar, { target: host, props: { artifactId: 'doc-1' } });
		await vi.waitFor(() => expect(api.fetchReviewBar).toHaveBeenCalled());
		await tick();
		expect(host.querySelector('.review-bar')).toBeNull();
	});

	it('R1: a failed send restores the bar with the retry message and no receipt', async () => {
		api.fetchReviewBar.mockResolvedValue(read(0));
		api.answerReview.mockRejectedValue(new Error('500'));
		await render();
		button('Request changes')!.click();
		flushSync();
		const input = host.querySelector<HTMLInputElement>('input')!;
		input.value = 'Tighten the rollout'; input.dispatchEvent(new Event('input', { bubbles: true }));
		flushSync();
		button('Request changes')!.click();
		await vi.waitFor(() => expect(host.textContent).toContain(SEND_FAILED));
		expect(host.querySelector('.review-bar.receipt')).toBeNull();
		expect(host.querySelector<HTMLInputElement>('input')!.value).toBe('Tighten the rollout');
	});

	it('R4: a send from the note keeps the note, busies only the pressed button, and returns focus to the note on failure', async () => {
		api.fetchReviewBar.mockResolvedValue(read(0));
		let fail!: (error: Error) => void;
		api.answerReview.mockReturnValue(new Promise((_, reject) => { fail = reject; }));
		await render();
		button('Request changes')!.click();
		flushSync();
		const input = host.querySelector<HTMLInputElement>('input')!;
		input.value = 'Tighten the rollout'; input.dispatchEvent(new Event('input', { bubbles: true }));
		flushSync();
		button('Request changes')!.click();
		flushSync();

		// Mid-send: the same input is still mounted, the bar is still the note layout, and only the sender spins.
		expect(host.querySelector('input')).toBe(input);
		expect(host.querySelector('.review-bar.noting')).toBeTruthy();
		expect(button('Approve')).toBeUndefined();
		expect(host.querySelectorAll('.spinner')).toHaveLength(1);
		expect(button('Request changes')!.querySelector('.spinner')).toBeTruthy();
		expect(button('Cancel')!.disabled).toBe(true);
		input.focus();

		fail(new Error('500'));
		await vi.waitFor(() => expect(host.textContent).toContain(SEND_FAILED));
		expect(host.querySelector('input')).toBe(input);
		expect(input.value).toBe('Tighten the rollout');
		await vi.waitFor(() => expect(document.activeElement).toBe(input));
	});

	it('R4: a failed Approve returns focus to Approve and spins only Approve while it sends', async () => {
		api.fetchReviewBar.mockResolvedValue(read(0));
		let fail!: (error: Error) => void;
		api.answerReview.mockReturnValue(new Promise((_, reject) => { fail = reject; }));
		await render();
		button('Approve')!.click();
		flushSync();
		expect(button('Approve')!.querySelector('.spinner')).toBeTruthy();
		expect(button('Request changes')!.disabled).toBe(true);
		expect(button('Request changes')!.querySelector('.spinner')).toBeNull();
		fail(new Error('500'));
		await vi.waitFor(() => expect(host.textContent).toContain(SEND_FAILED));
		await vi.waitFor(() => expect(document.activeElement).toBe(button('Approve')));
	});

	it('marks the matching card answer pending before the request and clears it when the request fails', async () => {
		api.fetchReviewBar.mockResolvedValue(read(0));
		let fail!: (error: Error) => void;
		api.answerReview.mockReturnValue(new Promise((_, reject) => { fail = reject; }));
		await render();

		button('Approve')!.click();
		flushSync();
		expect(get(pendingAnswers)).toEqual({ h1: { state: 'approved', feedback: counts(0) } });

		fail(new Error('500'));
		await vi.waitFor(() => expect(host.textContent).toContain(SEND_FAILED));
		expect(get(pendingAnswers)).toEqual({});
	});

	it('announces the send through a status region that is already mounted when it starts', async () => {
		api.fetchReviewBar.mockResolvedValue(read(0));
		let fail!: (error: Error) => void;
		api.answerReview.mockReturnValue(new Promise((_, reject) => { fail = reject; }));
		await render();
		const region = [...host.querySelectorAll<HTMLElement>('[role="status"].sr-only')][0];
		expect(region).toBeTruthy();
		expect(region.textContent).toBe('');
		button('Approve')!.click();
		flushSync();
		expect(region.isConnected).toBe(true);
		expect(region.textContent).toBe('Sending your review');
		fail(new Error('500'));
		await vi.waitFor(() => expect(region.textContent).toBe(''));
	});

	it('R2: the receipt takes focus and is announced as status', async () => {
		api.fetchReviewBar.mockResolvedValue(read(0));
		api.answerReview.mockResolvedValue({ verdict: 'approved', note: null, feedback_counts: counts(0) });
		await render();
		button('Approve')!.click();
		await vi.waitFor(() => expect(host.querySelector('.rb-receipt')).toBeTruthy());
		const receipt = host.querySelector<HTMLElement>('.rb-receipt')!;
		await vi.waitFor(() => expect(document.activeElement).toBe(receipt));
		expect(receipt.closest('[role="status"]') ?? receipt.getAttribute('role')).toBeTruthy();
		expect(host.textContent).toContain('You approved this');
	});

	it('R3: an open comment draft blocks Approve with changes and Request changes', async () => {
		api.fetchReviewBar.mockResolvedValue(read(2));
		await render(true);
		button('Approve with changes')!.click();
		flushSync();
		expect(host.textContent).toContain(DRAFT_OPEN);
		expect(api.answerReview).not.toHaveBeenCalled();
		button('Request changes')!.click();
		flushSync();
		expect(host.querySelector('input')).toBeNull();
	});

	it('a plain Approve is not blocked by a draft', async () => {
		api.fetchReviewBar.mockResolvedValue(read(0));
		api.answerReview.mockResolvedValue({ verdict: 'approved', note: null, feedback_counts: counts(0) });
		await render(true);
		button('Approve')!.click();
		await vi.waitFor(() => expect(api.answerReview).toHaveBeenCalledWith('doc-1', 'approved', ''));
	});

	it('a stale refusal shows the replaced notice', async () => {
		api.fetchReviewBar.mockResolvedValue(read(0));
		api.answerReview.mockRejectedValue(new ReviewRefusal('ask_stale', 'gone'));
		await render();
		api.fetchReviewBar.mockResolvedValue({ ...read(0), ask_raised: false });
		button('Approve')!.click();
		await vi.waitFor(() => expect(host.querySelector('.review-bar.stale')).toBeTruthy());
	});

	it('tells the viewer to re-read its pending batch once the answer is confirmed, and not on a failed send', async () => {
		api.fetchReviewBar.mockResolvedValue(read(2));
		const onAnswered = vi.fn();
		await render(false, onAnswered);
		api.answerReview.mockRejectedValueOnce(new Error('500'));
		button('Approve with changes')!.click();
		await vi.waitFor(() => expect(host.textContent).toContain(SEND_FAILED));
		expect(onAnswered).not.toHaveBeenCalled();
		api.answerReview.mockResolvedValueOnce({ verdict: 'approved_with_changes', note: null, feedback_counts: counts(2) });
		button('Approve with changes')!.click();
		await vi.waitFor(() => expect(onAnswered).toHaveBeenCalledTimes(1));
	});
});
