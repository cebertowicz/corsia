import { flushSync, mount, tick, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import ReviewBar from './ReviewBar.svelte';
import { DRAFT_OPEN, SEND_FAILED } from '$lib/reviewBarState.svelte';
import { ReviewRefusal, type ReviewBarRead } from '$lib/api/reviewBar';

const api = vi.hoisted(() => ({ fetchReviewBar: vi.fn(), answerReview: vi.fn() }));
vi.mock('$lib/api/reviewBar', async (importOriginal) => ({ ...(await importOriginal<object>()), ...api }));
vi.mock('$lib/ws', () => ({ on: vi.fn(() => () => {}) }));

const counts = (comments = 0) => ({ comments, suggestions: 0, edits: 0 });
function read(comments = 0): ReviewBarRead {
	return {
		ask_raised: true, ask_reason: 'Is the rollout plan right?', ask_raised_at: '2026-10-10T10:00:00',
		asker: { id: 'a1', name: 'Docs agent' }, other_askers: [], other_asks: [], other_request_count: 0,
		round_feedback_count: comments, round_feedback_counts: counts(comments),
	};
}

let host: HTMLElement;
let component: Record<string, unknown> | null = null;
async function render(draftOpen = false) {
	component = mount(ReviewBar, { target: host, props: { artifactId: 'doc-1', draftOpen } });
	await vi.waitFor(() => expect(host.querySelector('.review-bar')).toBeTruthy());
	flushSync();
}
const button = (name: string) => [...host.querySelectorAll('button')].find((b) => b.textContent?.trim() === name) as HTMLButtonElement | undefined;

beforeEach(() => {
	host = document.createElement('div');
	document.body.appendChild(host);
	api.fetchReviewBar.mockReset();
	api.answerReview.mockReset();
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
});
