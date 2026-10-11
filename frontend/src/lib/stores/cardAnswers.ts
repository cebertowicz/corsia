/**
 * Answers submitted in the review bar that the server has not confirmed yet, by artifact.
 *
 * An ask card shows the verdict the moment the person answers (CCR-4d) instead of staying "Needs your
 * review" until the next refresh. The review bar calls `answerPending` when it sends and `answerFailed`
 * when the send fails, which returns the card to its server state; the review bar's own error is the one
 * explanation. A confirmed answer needs no call: the card's server state changes and the entry is ignored.
 */
import { writable } from 'svelte/store';
import type { MessageCard, MessageCardState } from '$lib/api/messageCards';

export interface PendingAnswer {
	state: Exclude<MessageCardState, 'needs_review'>;
	feedback?: MessageCard['feedback'];
}

export const pendingAnswers = writable<Record<string, PendingAnswer>>({});

export function answerPending(artifactId: string, answer: PendingAnswer): void {
	pendingAnswers.update((all) => ({ ...all, [artifactId]: answer }));
}

export function answerFailed(artifactId: string): void {
	pendingAnswers.update((all) => {
		const { [artifactId]: _dropped, ...rest } = all;
		return rest;
	});
}
