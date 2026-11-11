// Drafts belong to the comment, not the surface showing it: a margin card,
// popover and drawer can replace one another without losing the typed text.
import type { ChatCommentAnchor } from '$lib/chatCommentAnchors';
type ReplyDraft = { text: string; inputId?: string; error?: string; threadId?: string; targetMessageId?: string; commentId?: string; anchor?: ChatCommentAnchor; seq?: number };
const replies = $state<Record<string, ReplyDraft>>({});
type RootDraft = ReplyDraft & { threadId: string; targetMessageId: string };
const roots = $state<Record<string, RootDraft>>({});
const rootKey = (threadId: string, messageId: string) => JSON.stringify([threadId, messageId]);

export const chatReplyDrafts = {
	get(commentId: string): string { return replies[commentId]?.text ?? ''; },
	reply(commentId: string): ReplyDraft | undefined { return replies[commentId]; },
	set(commentId: string, text: string): void {
		const prior = replies[commentId];
		if (text || prior?.inputId) replies[commentId] = { ...prior, text };
		else delete replies[commentId];
	},
	postReply(threadId: string, messageId: string, commentId: string, text: string, inputId: string | null): void {
		replies[commentId] = { text, threadId, targetMessageId: messageId, commentId,
			...(inputId ? { inputId } : { error: 'Connection lost before CORSIA could accept this reply.' }) };
	},
	root(threadId: string, messageId: string): RootDraft | undefined {
		return roots[rootKey(threadId, messageId)];
	},
	setRoot(threadId: string, messageId: string, text: string): void {
		const key = rootKey(threadId, messageId);
		const prior = roots[key];
		if (text || prior?.inputId) roots[key] = { ...prior, text, threadId, targetMessageId: messageId };
		else delete roots[key];
	},
	postRoot(threadId: string, messageId: string, text: string, inputId: string | null, anchor: ChatCommentAnchor, seq: number): void {
		roots[rootKey(threadId, messageId)] = {
			text, threadId, targetMessageId: messageId, anchor, seq,
			...(inputId ? { inputId } : { error: 'Connection lost before CORSIA could accept this comment.' }),
		};
	},
	settle(threadId: string, inputId: string, error?: string): RootDraft | undefined {
		const entry = Object.entries(roots).find(([, draft]) => draft.threadId === threadId && draft.inputId === inputId);
		if (entry) {
			const [key, draft] = entry;
			if (error) roots[key] = { ...draft, inputId: undefined, error };
			else delete roots[key];
			return draft;
		}
		const reply = Object.entries(replies).find(([, draft]) => draft.threadId === threadId && draft.inputId === inputId);
		if (!reply) return undefined;
		const [key, draft] = reply;
		if (error) replies[key] = { ...draft, inputId: undefined, error };
		else delete replies[key];
		return draft as RootDraft;
	},
};
