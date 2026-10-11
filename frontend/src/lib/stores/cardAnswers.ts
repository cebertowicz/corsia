/**
 * Submitted review-bar answers by ask hold for cards that do not refetch after a send.
 *
 * State table:
 * pending -> kept on confirm
 * pending -> removed on failure
 * ignored -> untouched
 *
 * Holds, rather than artifacts, identify review rounds, so a later ask for the same document never inherits
 * an earlier submitted verdict.
 */
import { writable } from 'svelte/store';
import type { MessageCard, MessageCardState } from '$lib/api/messageCards';

export interface SubmittedAnswer {
	state: Exclude<MessageCardState, 'needs_review'>;
	feedback?: MessageCard['feedback'];
}

export const submittedAnswers = writable<Record<string, SubmittedAnswer>>({});

export function answerSubmitted(holdIds: readonly string[], answer: SubmittedAnswer): void {
	submittedAnswers.update((all) => ({
		...all,
		...Object.fromEntries(holdIds.map((holdId) => [holdId, answer])),
	}));
}

export function answerSubmissionFailed(holdIds: readonly string[]): void {
	submittedAnswers.update((all) => {
		const rest = { ...all };
		for (const holdId of holdIds) delete rest[holdId];
		return rest;
	});
}
