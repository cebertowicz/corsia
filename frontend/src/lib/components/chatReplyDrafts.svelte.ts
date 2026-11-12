// Drafts hold only text the server has not stored. Once it stores a comment or reply, that input's row
// in the messages store carries its text and delivery state, and the comment's thread shows them.
// Drafts belong to the comment, not the surface showing it: a margin card, popover and drawer can
// replace one another without losing the typed text.
import type { ChatCommentAnchor } from '$lib/chatCommentAnchors';
type Draft = { text: string; threadId?: string; inputId?: string; error?: string };
const replies = $state<Record<string, Draft>>({});
const roots = $state<Record<string, Draft>>({});
// A root draft belongs to the passage it comments on, so two passages of one message never share text, an error or a pending post.
const rootKey = (threadId: string, messageId: string, anchor: Pick<ChatCommentAnchor, 'from_off' | 'to_off'>) => JSON.stringify([threadId, messageId, anchor.from_off, anchor.to_off]);
const sendFailure = (noun: 'comment' | 'reply') => `Couldn’t send this ${noun}. Try again.`;
// The box that posted an input learns here whether the server stored it (true) or refused it (false),
// and says whether it is still on screen to show a refusal itself.
const waiting = new Map<string, { resolve: (stored: boolean) => void; shown: () => boolean }>();

function post(store: Record<string, Draft>, key: string, threadId: string, text: string, inputId: string | null, error: string | undefined, noun: 'comment' | 'reply', shown: () => boolean): Promise<boolean> {
	store[key] = { text, threadId, ...(inputId ? { inputId } : { error: error || sendFailure(noun) }) };
	if (!inputId) return Promise.resolve(false);
	return new Promise(resolve => waiting.set(inputId, { resolve, shown }));
}

function find(threadId: string, inputId: string): { store: Record<string, Draft>; key: string; noun: 'comment' | 'reply' } | null {
	for (const [store, noun] of [[roots, 'comment'], [replies, 'reply']] as const) {
		const key = Object.keys(store).find(item => store[item].threadId === threadId && store[item].inputId === inputId);
		if (key) return { store, key, noun };
	}
	return null;
}

function answer(inputId: string, stored: boolean): void {
	waiting.get(inputId)?.resolve(stored);
	waiting.delete(inputId);
}

export const chatReplyDrafts = {
	get(commentId: string): string { return replies[commentId]?.text ?? ''; },
	reply(commentId: string): Draft | undefined { return replies[commentId]; },
	set(commentId: string, text: string): void {
		const prior = replies[commentId];
		if (text || prior?.inputId) replies[commentId] = { ...prior, text };
		else delete replies[commentId];
	},
	postReply(threadId: string, commentId: string, text: string, inputId: string | null, error: string | undefined, shown: () => boolean): void {
		void post(replies, commentId, threadId, text, inputId, error, 'reply', shown);
	},
	/** The draft the composer on this passage shows. */
	root(threadId: string, messageId: string, anchor: ChatCommentAnchor): Draft | undefined {
		return roots[rootKey(threadId, messageId, anchor)];
	},
	setRoot(threadId: string, messageId: string, anchor: ChatCommentAnchor, text: string): void {
		const key = rootKey(threadId, messageId, anchor);
		const prior = roots[key];
		if (text || prior?.inputId) roots[key] = { ...prior, text, threadId };
		else delete roots[key];
	},
	/** Cancel drops the passage's text and failure; a post on its way keeps its draft until the server answers. */
	dropRoot(threadId: string, messageId: string, anchor: ChatCommentAnchor): void {
		const key = rootKey(threadId, messageId, anchor);
		if (!roots[key]?.inputId) delete roots[key];
	},
	/** Resolves true once the server stores the comment, false when the post is refused. */
	postRoot(threadId: string, messageId: string, anchor: ChatCommentAnchor, text: string, inputId: string | null, error: string | undefined, shown: () => boolean): Promise<boolean> {
		return post(roots, rootKey(threadId, messageId, anchor), threadId, text, inputId, error, 'comment', shown);
	},
	/** The server stored this input, so its row owns the text from here. True when it was a draft's. */
	release(threadId: string, inputId: string): boolean {
		const hit = find(threadId, inputId);
		if (!hit) return false;
		delete hit.store[hit.key];
		answer(inputId, true);
		return true;
	},
	/** The server refused this input before storing it: the draft keeps the text and the cause. Returns the cause and whether the box that posted it still shows it; null when it was no draft's. */
	refuse(threadId: string, inputId: string, error: string): { cause: string; shown: boolean } | null {
		const hit = find(threadId, inputId);
		if (!hit) return null;
		const cause = error || sendFailure(hit.noun);
		const shown = waiting.get(inputId)?.shown() ?? false;
		hit.store[hit.key] = { ...hit.store[hit.key], inputId: undefined, error: cause };
		answer(inputId, false);
		return { cause, shown };
	},
};
