// Drafts belong to the comment, not the surface showing it: a margin card,
// popover and drawer can replace one another without losing the typed text.
import type { ChatCommentAnchor } from '$lib/chatCommentAnchors';
type ReplyDraft = { text: string; inputId?: string; error?: string; threadId?: string; targetMessageId?: string; commentId?: string; anchor?: ChatCommentAnchor; seq?: number };
const replies = $state<Record<string, ReplyDraft>>({});
/** A root comment's draft. `postedId` is the id the posted comment carries; `queued` means the server stored it and it waits to be accepted. */
type RootDraft = ReplyDraft & { threadId: string; targetMessageId: string; postedId?: string; queued?: boolean };
const roots = $state<Record<string, RootDraft>>({});
// A root draft belongs to the passage it comments on, so two passages of one message never share text, an error or a pending post.
const rootKey = (threadId: string, messageId: string, anchor: Pick<ChatCommentAnchor, 'from_off' | 'to_off'>) => JSON.stringify([threadId, messageId, anchor.from_off, anchor.to_off]);
const sendFailure = (noun: 'comment' | 'reply') => `Couldn’t send this ${noun}. Try again.`;

/** What became of a posted draft's input. A stored input that failed shows its failure and Retry in its thread, so its draft is released. */
export type DraftOutcome = 'queued' | 'accepted' | 'stored_failed' | { error: string };

export const chatReplyDrafts = {
	get(commentId: string): string { return replies[commentId]?.text ?? ''; },
	reply(commentId: string): ReplyDraft | undefined { return replies[commentId]; },
	set(commentId: string, text: string): void {
		const prior = replies[commentId];
		if (text || prior?.inputId) replies[commentId] = { ...prior, text };
		else delete replies[commentId];
	},
	postReply(threadId: string, messageId: string, commentId: string, text: string, inputId: string | null, error?: string): void {
		replies[commentId] = { text, threadId, targetMessageId: messageId, commentId,
			...(inputId ? { inputId } : { error: error || sendFailure('reply') }) };
	},
	/** The draft the composer on this passage shows; a stored comment waiting to be accepted is no longer the composer's. */
	root(threadId: string, messageId: string, anchor: ChatCommentAnchor): RootDraft | undefined {
		const draft = roots[rootKey(threadId, messageId, anchor)];
		return draft?.queued ? undefined : draft;
	},
	setRoot(threadId: string, messageId: string, anchor: ChatCommentAnchor, text: string): void {
		const key = rootKey(threadId, messageId, anchor);
		const prior = roots[key];
		if (prior?.queued) return;
		if (text || prior?.inputId) roots[key] = { ...prior, text, threadId, targetMessageId: messageId, anchor };
		else delete roots[key];
	},
	/** Cancel drops the passage's text and failure; a post on its way keeps its draft until the server answers. */
	dropRoot(threadId: string, messageId: string, anchor: ChatCommentAnchor): void {
		const key = rootKey(threadId, messageId, anchor);
		if (!roots[key]?.inputId) delete roots[key];
	},
	postRoot(threadId: string, messageId: string, text: string, inputId: string | null, anchor: ChatCommentAnchor, seq: number, postedId: string, error?: string): void {
		roots[rootKey(threadId, messageId, anchor)] = {
			text, threadId, targetMessageId: messageId, anchor, seq, postedId,
			...(inputId ? { inputId } : { error: error || sendFailure('comment') }),
		};
	},
	settle(threadId: string, inputId: string, outcome: DraftOutcome): RootDraft | undefined {
		const entry = Object.entries(roots).find(([, draft]) => draft.threadId === threadId && draft.inputId === inputId);
		if (entry) {
			const [key, draft] = entry;
			if (outcome === 'queued') roots[key] = { ...draft, queued: true };
			else if (typeof outcome === 'object') roots[key] = { ...draft, inputId: undefined, queued: undefined, error: outcome.error || sendFailure('comment') };
			else delete roots[key];
			return draft;
		}
		const reply = Object.entries(replies).find(([, draft]) => draft.threadId === threadId && draft.inputId === inputId);
		if (!reply) return undefined;
		const [key, draft] = reply;
		if (typeof outcome === 'object') replies[key] = { ...draft, inputId: undefined, error: outcome.error || sendFailure('reply') };
		else delete replies[key];
		return draft as RootDraft;
	},
};
