import { get } from 'svelte/store';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { createReviewBarState } from './reviewBarState.svelte';
import { answerSubmitted, submittedAnswers } from './stores/cardAnswers';
import type { ReviewBarRead } from './api/reviewBar';

const api = vi.hoisted(() => ({ fetchReviewBar: vi.fn(), answerReview: vi.fn() }));
vi.mock('./api/reviewBar', async (importOriginal) => ({ ...(await importOriginal<object>()), ...api }));

const read: ReviewBarRead = {
	ask_raised: true, hold_ids: ['h1'], ask_reason: 'Is this ready?', ask_raised_at: '2026-10-10T10:00:00',
	asker: { id: 'a1', name: 'Docs agent' }, other_askers: [], other_asks: [], other_request_count: 0,
	round_feedback_count: 0, round_feedback_counts: { comments: 0, suggestions: 0, edits: 0 },
};

function startAnswer(state: ReturnType<typeof createReviewBarState>) {
	return state.answer('approved', () => answerSubmitted(read.hold_ids, { state: 'approved', feedback: read.round_feedback_counts }));
}

beforeEach(() => {
	api.fetchReviewBar.mockReset();
	api.answerReview.mockReset();
	submittedAnswers.set({});
});

describe('review-bar submission outcomes', () => {
	it('keeps the submitted verdict when an artifact switch happens before a successful send resolves', async () => {
		api.fetchReviewBar.mockResolvedValue(read);
		let succeed!: (value: { verdict: 'approved'; note: null; feedback_counts: typeof read.round_feedback_counts }) => void;
		api.answerReview.mockReturnValue(new Promise((resolve) => { succeed = resolve; }));
		const state = createReviewBarState();
		state.setTarget('doc-1');
		await state.load();

		const outcome = startAnswer(state);
		expect(get(submittedAnswers)).toEqual({ h1: { state: 'approved', feedback: read.round_feedback_counts } });
		state.setTarget('doc-2');
		succeed({ verdict: 'approved', note: null, feedback_counts: read.round_feedback_counts });

		expect(await outcome).toBe('sent');
		expect(get(submittedAnswers)).toEqual({ h1: { state: 'approved', feedback: read.round_feedback_counts } });
	});
});
