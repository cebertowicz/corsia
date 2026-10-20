import { flushSync, mount, tick, unmount } from 'svelte';
import { get } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import ReviewBar from './ReviewBar.svelte';
import ReviewBarHarness from '$lib/__tests__/fixtures/ReviewBarHarness.svelte';
import { DRAFT_OPEN, SEND_FAILED } from '$lib/reviewBarState.svelte';
import { ReviewRefusal, type ReviewBarRead } from '$lib/api/reviewBar';
import { submittedAnswers } from '$lib/stores/cardAnswers';
import { token } from '$lib/stores/auth';

const api = vi.hoisted(() => ({ fetchReviewBar: vi.fn(), answerReview: vi.fn() }));
const planApi = vi.hoisted(() => ({ mutateUserTask: vi.fn() }));
const ws = vi.hoisted(() => ({ on: vi.fn(() => () => {}) }));
vi.mock('$lib/api/reviewBar', async (importOriginal) => ({ ...(await importOriginal<object>()), ...api }));
vi.mock('$lib/api/plans', async (importOriginal) => ({ ...(await importOriginal<object>()), ...planApi }));
vi.mock('$lib/ws', () => ws);

const counts = (comments = 0, suggestions = 0, edits = 0) => ({ comments, suggestions, edits });
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
	planApi.mutateUserTask.mockReset();
	ws.on.mockClear();
	token.set(null);
	submittedAnswers.set({});
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

	it('keeps a long ask compact and reveals both askers only when expanded', async () => {
		api.fetchReviewBar.mockResolvedValue({
			...read(0),
			ask_reason: 'Please review the detailed deployment and rollback plan before the handoff.',
			other_askers: ['Research'],
			other_asks: [{ name: 'Research', reason: 'Check the evidence links.' }],
			other_request_count: 1,
		});
		await render();
		expect(host.textContent).toContain('Docs agent');
		expect(host.querySelector('.rb-lead .rb-ask')).toBeNull();
		expect(host.querySelector('.rb-line > .rb-ask')?.textContent).toContain('Please review the detailed deployment');
		expect(host.textContent).not.toContain('Check the evidence links.');
		host.querySelector<HTMLButtonElement>('.rb-line')!.click();
		flushSync();
		expect(host.textContent).toContain('Please review the detailed deployment and rollback plan before the handoff.');
		expect(host.textContent).toContain('Research: Check the evidence links.');
	});

	it('shows nothing when no ask is raised', async () => {
		api.fetchReviewBar.mockResolvedValue({ ...read(0), ask_raised: false });
		component = mount(ReviewBar, { target: host, props: { artifactId: 'doc-1' } });
		await vi.waitFor(() => expect(api.fetchReviewBar).toHaveBeenCalled());
		await tick();
		expect(host.querySelector('.review-bar')).toBeNull();
	});

	it('shows Done and Won’t do for a person-step document, never review actions', async () => {
		api.fetchReviewBar.mockResolvedValue({
			...read(0), ask_raised: false,
			person_step: {
				node_id: 'step-1', node_title: 'Set up the workspace', node_revision: 3,
				plan_id: 'plan-1', owner_thread_id: 'agent-1', owner_name: 'Setup agent',
			},
		});
		await render();
		expect(host.textContent).toContain('Setup agent needs you to do this: Set up the workspace');
		expect(button('Done')).toBeTruthy();
		expect(button('Won’t do')).toBeTruthy();
		expect(button('Approve')).toBeUndefined();
		expect(button('Request changes')).toBeUndefined();
	});

	it('settles the person step through its existing Done action without waiting on comments', async () => {
		token.set('user-token');
		planApi.mutateUserTask.mockResolvedValue({ status: 'success' });
		api.fetchReviewBar.mockResolvedValue({
			...read(0), ask_raised: false,
			person_step: {
				node_id: 'step-1', node_title: 'Set up the workspace', node_revision: 3,
				plan_id: 'plan-1', owner_thread_id: 'agent-1', owner_name: 'Setup agent',
			},
		});
		const onAnswered = vi.fn();
		await render(true, onAnswered);
		button('Done')!.click();
		await vi.waitFor(() => expect(planApi.mutateUserTask).toHaveBeenCalledWith(
			'user-token', 'plan-1', 'mark_done', 'step-1', 3, 'mark_done:step-1:3',
		));
		await vi.waitFor(() => expect(host.textContent).toContain('Done'));
		expect(onAnswered).toHaveBeenCalledOnce();
	});

	it('reloads the bar read after a person-step conflict', async () => {
		token.set('user-token');
		planApi.mutateUserTask.mockResolvedValue({ status: 'conflict', detail: 'This step changed. Refreshing the latest state.' });
		api.fetchReviewBar
			.mockResolvedValueOnce({ ...read(0), ask_raised: false, person_step: { node_id: 'step-1', node_title: 'Set up the workspace', node_revision: 3, plan_id: 'plan-1', owner_thread_id: 'agent-1', owner_name: 'Setup agent' } })
			.mockResolvedValueOnce({ ...read(0), ask_raised: false });
		await render();
		button('Done')!.click();
		await vi.waitFor(() => expect(api.fetchReviewBar).toHaveBeenCalledTimes(2));
	});

	it('keeps the plan-row receipt after the plans refresh removes the settled person step', async () => {
		token.set('user-token');
		planApi.mutateUserTask.mockResolvedValue({ status: 'success' });
		api.fetchReviewBar
			.mockResolvedValueOnce({ ...read(0), ask_raised: false, person_step: { node_id: 'step-1', node_title: 'Set up the workspace', node_revision: 3, plan_id: 'plan-1', owner_thread_id: 'agent-1', owner_name: 'Setup agent' } })
			.mockResolvedValueOnce({ ...read(0), ask_raised: false });
		await render();
		button('Done')!.click();
		await vi.waitFor(() => expect(host.textContent).toContain('Done'));
		const refresh = ws.on.mock.calls.find(([event]) => event === 'plans_changed')?.[1] as (() => void);
		refresh();
		await vi.waitFor(() => expect(api.fetchReviewBar).toHaveBeenCalledTimes(2));
		expect(host.textContent).toContain('Done');
		expect(button('Approve')).toBeUndefined();
	});

	it('R1: a failed send restores the bar with the retry message and no receipt', async () => {
		api.fetchReviewBar.mockResolvedValue(read(0));
		api.answerReview.mockRejectedValue(new Error('500'));
		await render();
		button('Request changes')!.click();
		flushSync();
		const input = host.querySelector<HTMLTextAreaElement>('textarea')!;
		input.value = 'Tighten the rollout'; input.dispatchEvent(new Event('input', { bubbles: true }));
		flushSync();
		button('Request changes')!.click();
		await vi.waitFor(() => expect(host.textContent).toContain(SEND_FAILED));
		expect(host.querySelector('.review-bar.receipt')).toBeNull();
		expect(host.querySelector<HTMLTextAreaElement>('textarea')!.value).toBe('Tighten the rollout');
	});

	it('R4: a send from the note keeps the note, busies only the pressed button, and returns focus to the note on failure', async () => {
		api.fetchReviewBar.mockResolvedValue(read(0));
		let fail!: (error: Error) => void;
		api.answerReview.mockReturnValue(new Promise((_, reject) => { fail = reject; }));
		await render();
		button('Request changes')!.click();
		flushSync();
		const input = host.querySelector<HTMLTextAreaElement>('textarea')!;
		input.value = 'Tighten the rollout'; input.dispatchEvent(new Event('input', { bubbles: true }));
		flushSync();
		button('Request changes')!.click();
		flushSync();

		// Mid-send: the same input is still mounted, the bar is still the note layout, and only the sender spins.
		expect(host.querySelector('textarea')).toBe(input);
		expect(host.querySelector('.review-bar.noting')).toBeTruthy();
		expect(button('Approve')).toBeUndefined();
		expect(host.querySelectorAll('.spinner')).toHaveLength(1);
		expect(button('Request changes')!.querySelector('.spinner')).toBeTruthy();
		expect(button('Cancel')!.disabled).toBe(true);
		input.focus();

		fail(new Error('500'));
		await vi.waitFor(() => expect(host.textContent).toContain(SEND_FAILED));
		expect(host.querySelector('textarea')).toBe(input);
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

	it('keeps the submitted verdict on a duplicate click while sending and clears it on actual failure', async () => {
		api.fetchReviewBar.mockResolvedValue(read(0));
		let fail!: (error: Error) => void;
		api.answerReview.mockReturnValue(new Promise((_, reject) => { fail = reject; }));
		await render();

		button('Approve')!.click();
		flushSync();
		button('Approve')!.dispatchEvent(new MouseEvent('click', { bubbles: true }));
		flushSync();
		expect(api.answerReview).toHaveBeenCalledTimes(1);
		expect(get(submittedAnswers)).toEqual({ h1: { state: 'approved', feedback: counts(0) } });

		fail(new Error('500'));
		await vi.waitFor(() => expect(host.textContent).toContain(SEND_FAILED));
		expect(get(submittedAnswers)).toEqual({});
	});

	it('reconciles the submitted verdict and feedback with the server response after success', async () => {
		api.fetchReviewBar.mockResolvedValue(read(0));
		api.answerReview.mockResolvedValue({
			verdict: 'approved_with_changes', note: null,
			feedback_counts: { comments: 1, suggestions: 2, edits: 3 },
		});
		await render();

		button('Approve')!.click();

		await vi.waitFor(() => expect(get(submittedAnswers)).toEqual({
			h1: { state: 'approved_with_changes', feedback: { comments: 1, suggestions: 2, edits: 3 } },
		}));
	});

	it('does not flash a submitted verdict for an empty required note', async () => {
		api.fetchReviewBar.mockResolvedValue(read(0));
		await render();

		button('Request changes')!.click();
		flushSync();
		button('Request changes')!.click();
		flushSync();

		expect(api.answerReview).not.toHaveBeenCalled();
		expect(get(submittedAnswers)).toEqual({});
	});

	it('reconciles the original hold with the server response when the ReviewBar switches artifacts before a successful send resolves', async () => {
		const response = { verdict: 'approved_with_changes' as const, note: null, feedback_counts: { comments: 1, suggestions: 2, edits: 3 } };
		api.fetchReviewBar.mockResolvedValueOnce(read(0)).mockResolvedValueOnce({ ...read(0), hold_ids: ['h2'] });
		let succeed!: (value: typeof response) => void;
		api.answerReview.mockReturnValue(new Promise<typeof response>((resolve) => { succeed = resolve; }));
		const harness = mount(ReviewBarHarness, { target: host });
		component = harness as Record<string, unknown>;
		await vi.waitFor(() => expect(button('Approve')).toBeTruthy());

		button('Approve')!.click();
		flushSync();
		expect(get(submittedAnswers)).toEqual({ h1: { state: 'approved', feedback: counts(0) } });
		harness.switchArtifact('doc-2');
		await vi.waitFor(() => expect(api.fetchReviewBar).toHaveBeenCalledTimes(2));
		succeed(response);

		await vi.waitFor(() => expect(get(submittedAnswers)).toEqual({ h1: { state: 'approved_with_changes', feedback: { comments: 1, suggestions: 2, edits: 3 } } }));
	});

	it('clears the original hold overlay when the ReviewBar switches artifacts before a failed send resolves', async () => {
		api.fetchReviewBar.mockResolvedValueOnce(read(0)).mockResolvedValueOnce({ ...read(0), hold_ids: ['h2'] });
		let fail!: (error: Error) => void;
		api.answerReview.mockReturnValue(new Promise((_, reject) => { fail = reject; }));
		const harness = mount(ReviewBarHarness, { target: host });
		component = harness as Record<string, unknown>;
		await vi.waitFor(() => expect(button('Approve')).toBeTruthy());

		button('Approve')!.click();
		flushSync();
		expect(get(submittedAnswers)).toEqual({ h1: { state: 'approved', feedback: counts(0) } });
		harness.switchArtifact('doc-2');
		await vi.waitFor(() => expect(api.fetchReviewBar).toHaveBeenCalledTimes(2));
		fail(new Error('500'));

		await vi.waitFor(() => expect(get(submittedAnswers)).toEqual({}));
	});

	it('announces the send through a status region that is already mounted when it starts', async () => {
		api.fetchReviewBar.mockResolvedValue(read(0));
		let fail!: (error: Error) => void;
		api.answerReview.mockReturnValue(new Promise((_, reject) => { fail = reject; }));
		await render();
		const region = host.querySelector<HTMLElement>('.rb-status[role="status"]')!;
		expect(region).toBeTruthy();
		expect(region.textContent).toBe('');
		button('Approve')!.click();
		flushSync();
		expect(region.isConnected).toBe(true);
		expect(region.textContent).toBe('Sending your review…');
		fail(new Error('500'));
		await vi.waitFor(() => expect(region.textContent).toBe(SEND_FAILED));
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
		expect(receipt.textContent).not.toMatch(/sent \d/);
	});

	it('lists delivered feedback kinds in canonical order with singular and plural wording', async () => {
		api.fetchReviewBar.mockResolvedValue(read(0));
		api.answerReview.mockResolvedValue({ verdict: 'approved', note: null, feedback_counts: counts(2, 1, 3) });
		await render();
		button('Approve')!.click();
		await vi.waitFor(() => expect(host.querySelector('.rb-receipt')?.textContent).toContain('sent 2 comments, 1 suggestion, 3 edits'));
		unmount(component!); component = null; host.innerHTML = '';
		api.fetchReviewBar.mockResolvedValue(read(0));
		api.answerReview.mockResolvedValue({ verdict: 'approved', note: null, feedback_counts: counts(0, 0, 1) });
		await render();
		button('Approve')!.click();
		await vi.waitFor(() => expect(host.querySelector('.rb-receipt')?.textContent).toContain('sent 1 edit'));
		expect(host.querySelector('.rb-receipt')?.textContent).not.toContain('comments');
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
		expect(host.querySelector('textarea')).toBeNull();
	});

	it('a plain Approve is not blocked by a draft, and answers the request it shows', async () => {
		api.fetchReviewBar.mockResolvedValue(read(0));
		api.answerReview.mockResolvedValue({ verdict: 'approved', note: null, feedback_counts: counts(0) });
		await render(true);
		button('Approve')!.click();
		await vi.waitFor(() => expect(api.answerReview).toHaveBeenCalledWith('doc-1', 'approved', '', ['h1']));
	});

	it('a stale refusal says the request ended', async () => {
		api.fetchReviewBar.mockResolvedValue(read(0));
		api.answerReview.mockRejectedValue(new ReviewRefusal('ask_stale', 'gone'));
		await render();
		api.fetchReviewBar.mockResolvedValue({ ...read(0), ask_raised: false });
		button('Approve')!.click();
		await vi.waitFor(() => expect(host.querySelector('.review-bar.stale')).toBeTruthy());
		expect(host.querySelector('.review-bar.stale')!.textContent).toContain('This review request has ended');
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
