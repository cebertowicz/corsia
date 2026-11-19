import type { CommentAttention } from '$lib/api/plansTypes';
import type { PersonStepInstruction } from '$lib/api/reviewBar';
import { feedbackLine } from '$lib/api/messageCards';

/** A document's comment task clears by reading or deciding its items, never by Done or Won't do. */
export function isCommentTask(item: { comment_attention?: CommentAttention | null } | null | undefined): boolean {
	return !!item?.comment_attention?.system_task;
}

/** The one action that clears a document's comment task by hand: marks its items read and decides nothing. */
export const DISMISS_ALL = 'Dismiss all';

/** What a person step asks, in words: a comment task says what waits; any other step names the work. */
export function personStepAsk(step: PersonStepInstruction): string {
	if (!isCommentTask(step)) return `Needs you to do this: ${step.node_title}`;
	const waiting = feedbackLine(step.comment_attention ?? {});
	return waiting ? `${waiting} to review` : '';
}
