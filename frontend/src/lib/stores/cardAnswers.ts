/**
 * Submitted review-bar answers by ask hold for cards that do not refetch after a send.
 *
 * State table:
 * pending -> replaced with the server's canonical response on confirm
 * pending -> removed on failure
 * ignored -> untouched
 *
 * This category-5 session overlay is needed because card state is computed when the transcript is read and there
 * is no per-card refetch after an answer. It is inert once the server snapshot is settled (`AttachmentRow` applies
 * it only over `needs_review`) and stores one small record per hold the user answers for the session. Holds, rather
 * than artifacts, identify review rounds, so a later ask for the same document never inherits an earlier verdict.
 * Wrong if an answered hold's overlay differs from `answerReview`'s response, or if it applies to a card the server
 * already reports as settled; `ReviewBar.test.ts` covers canonical reconciliation and `AttachmentRow` enforces the
 * settled-server boundary.
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
