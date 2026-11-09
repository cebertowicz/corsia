// An unsent reply belongs to its comment, not to the surface showing it: the same thread is a margin card, a
// popover or a row beneath its message, and the canvas swaps between them as it narrows or widens.
const drafts = $state<Record<string, string>>({});

/** Unsent chat comment replies by comment id, shared by every surface that shows the thread. */
export const chatReplyDrafts = {
	get(commentId: string): string {
		return drafts[commentId] ?? '';
	},
	set(commentId: string, text: string): void {
		if (text) drafts[commentId] = text;
		else delete drafts[commentId];
	},
};
