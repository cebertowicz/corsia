/**
 * Answers submitted in the review bar that the server has not confirmed yet, by ask hold.
 *
 * An ask card shows the verdict the moment the person answers (CCR-4d) instead of staying "Needs your
 * review" until the next refresh. The review bar calls `answerPending` when it sends and `answerFailed`
 * when the send fails, which returns the card to its server state; the review bar's own error is the one
 * explanation. Holds, rather than artifacts, identify review rounds, so a later ask for the same document
 * never inherits an earlier optimistic verdict.
 */
import { writable } from 'svelte/store';
import type { MessageCard, MessageCardState } from '$lib/api/messageCards';

export interface PendingAnswer {
	state: Exclude<MessageCardState, 'needs_review'>;
	feedback?: MessageCard['feedback'];
}

export const pendingAnswers = writable<Record<string, PendingAnswer>>({});

export function answerPending(holdIds: readonly string[], answer: PendingAnswer): void {
	pendingAnswers.update((all) => ({
		...all,
		...Object.fromEntries(holdIds.map((holdId) => [holdId, answer])),
	}));
}

export function answerFailed(holdIds: readonly string[]): void {
	pendingAnswers.update((all) => {
		const rest = { ...all };
		for (const holdId of holdIds) delete rest[holdId];
		return rest;
	});
}
