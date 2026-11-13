import type { CommentAttention } from '$lib/api/plansTypes';

/** A document's comment task clears by reading or deciding its items, never by Done or Won't do. */
export function isCommentTask(item: { comment_attention?: CommentAttention | null } | null | undefined): boolean {
	return !!item?.comment_attention?.system_task;
}

/** The one action that clears a document's comment task by hand: marks its items read and decides nothing. */
export const DISMISS_ALL = 'Dismiss all';
